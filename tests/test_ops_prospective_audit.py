"""Tests for Phase 14 Component B: ops/prospective_audit.py - the read-only
per-trading-day prospective evidence ledger (docs/specs/phase14.md §2).

Everything here is synthetic/deterministic and uses an in-memory SQLite DB.
No test makes a real network call. Tests assert the module is genuinely
read-only (never records an observation/event/outcome as a side effect of
running the audit) and that immutability invariants in
strategy_lab/prospective.py and strategy_lab/prospective_events.py hold.
"""
import ast
import re
import sqlite3
from datetime import date

import pytest

from db.run_history_repository import finish_run as prod_finish_run
from db.run_history_repository import start_run as prod_start_run
from db.schema import init_db
from ops.prospective_audit import (
    PROSPECTIVE_DAY_CAPTURED,
    PROSPECTIVE_DAY_CAPTURED_PARTIAL,
    PROSPECTIVE_DAY_DUPLICATE_SKIPPED,
    PROSPECTIVE_DAY_EXPECTED,
    PROSPECTIVE_DAY_MISSED,
    PROSPECTIVE_DAY_UNAVAILABLE,
    build_prospective_day_ledger,
    eligibility_start_date,
    prospective_evidence_audit_summary,
)
from strategy_lab.prospective import load_observations, record_observation
from strategy_lab.prospective_events import load_events, record_event
from strategy_lab.research_automation import (
    STATUS_FAILED,
    STATUS_PARTIAL_FAILURE_RESEARCH,
    STATUS_SUCCESS_RESEARCH,
    ResearchRunResult,
    _finish_run as research_finish_run,
    _start_run as research_start_run,
    ensure_schema as ensure_research_schema,
)


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _record_research_run(conn, trading_date: str, status: str, observations_created=0,
                          duplicates_skipped=0, errors=None, tickers_attempted=1) -> int:
    """Writes a research_run_history row via the module's own (private but
    stable) start/finish helpers - the same code path run_research_job uses,
    so tests exercise the real persisted row shape."""
    ensure_research_schema(conn)
    run_id = research_start_run(conn, date.fromisoformat(trading_date))
    result = ResearchRunResult(
        run_id=run_id, status=status, trading_date=trading_date,
        tickers_attempted=tickers_attempted, observations_created=observations_created,
        duplicates_skipped=duplicates_skipped, errors=errors or [],
    )
    research_finish_run(conn, run_id, result)
    return run_id


def _leave_stuck_running(conn, trading_date: str) -> int:
    """Simulates a crashed run: _start_run writes status='running' and is
    never followed by _finish_run."""
    ensure_research_schema(conn)
    return research_start_run(conn, date.fromisoformat(trading_date))


def _record_production_run(conn, trading_date: str, status: str) -> int:
    run_id = prod_start_run(conn, "real", trading_date=date.fromisoformat(trading_date))
    prod_finish_run(conn, run_id, status=status, tickers_attempted=7, tickers_updated=7)
    return run_id


def _status_for(ledger, trading_date: str):
    matches = [d for d in ledger if d.trading_date == trading_date]
    assert len(matches) == 1, f"expected exactly one ledger entry for {trading_date}, got {len(matches)}"
    return matches[0]


# --- eligibility_start_date ---


def test_eligibility_start_date_is_none_when_no_research_history():
    conn = make_test_db()
    assert eligibility_start_date(conn) is None


def test_eligibility_start_date_is_min_trading_date():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-05", STATUS_SUCCESS_RESEARCH, observations_created=1)
    _record_research_run(conn, "2024-01-03", STATUS_SUCCESS_RESEARCH, observations_created=1)
    assert eligibility_start_date(conn) == date(2024, 1, 3)


# --- eligibility window boundary: days before start never appear ---


def test_day_before_eligibility_start_never_appears_in_ledger():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-05", STATUS_SUCCESS_RESEARCH, observations_created=1)  # eligibility start
    ledger = build_prospective_day_ledger(conn, today=date(2024, 1, 10))
    trading_dates = {d.trading_date for d in ledger}
    assert "2024-01-02" not in trading_dates  # before eligibility_start_date - never MISSED, simply absent
    assert "2024-01-05" in trading_dates


def test_empty_research_history_yields_empty_ledger():
    conn = make_test_db()
    ledger = build_prospective_day_ledger(conn, today=date(2024, 1, 10))
    assert ledger == []


# --- classification: CAPTURED / CAPTURED_PARTIAL ---


def test_captured_when_clean_success_with_observations():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-03", STATUS_SUCCESS_RESEARCH, observations_created=5)
    ledger = build_prospective_day_ledger(conn, today=date(2024, 1, 10))
    day = _status_for(ledger, "2024-01-03")
    assert day.status == PROSPECTIVE_DAY_CAPTURED


def test_captured_partial_when_partial_failure_with_observations():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-03", STATUS_PARTIAL_FAILURE_RESEARCH, observations_created=3,
                          errors=["AAPL: ValueError: boom"])
    ledger = build_prospective_day_ledger(conn, today=date(2024, 1, 10))
    day = _status_for(ledger, "2024-01-03")
    assert day.status == PROSPECTIVE_DAY_CAPTURED_PARTIAL
    assert "AAPL" in day.reason


# --- DUPLICATE_SKIPPED vs UNAVAILABLE vs MISSED ---


def test_duplicate_skipped_when_retry_found_everything_already_captured():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-03", STATUS_SUCCESS_RESEARCH, observations_created=0,
                          duplicates_skipped=4)
    ledger = build_prospective_day_ledger(conn, today=date(2024, 1, 10))
    day = _status_for(ledger, "2024-01-03")
    assert day.status == PROSPECTIVE_DAY_DUPLICATE_SKIPPED


def test_unavailable_when_job_ran_cleanly_with_zero_progress():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-03", STATUS_SUCCESS_RESEARCH, observations_created=0,
                          duplicates_skipped=0)
    ledger = build_prospective_day_ledger(conn, today=date(2024, 1, 10))
    day = _status_for(ledger, "2024-01-03")
    assert day.status == PROSPECTIVE_DAY_UNAVAILABLE


def test_missed_when_job_never_ran_at_all_but_day_is_within_eligibility_window():
    conn = make_test_db()
    # eligibility_start comes from a run on 2024-01-02; 2024-01-03 itself
    # has NO research_run_history row at all.
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=1)
    before_obs = len(load_observations(conn))
    before_events = len(load_events(conn))

    ledger = build_prospective_day_ledger(conn, today=date(2024, 1, 10))
    day = _status_for(ledger, "2024-01-03")
    assert day.status == PROSPECTIVE_DAY_MISSED
    assert "no research job run recorded" in day.reason

    # Pure read: running the audit must never create observation/event rows.
    assert len(load_observations(conn)) == before_obs
    assert len(load_events(conn)) == before_events


def test_missed_when_job_genuinely_failed_with_zero_observations():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=1)
    _record_research_run(conn, "2024-01-03", STATUS_FAILED, observations_created=0,
                          errors=["AAPL: RuntimeError: boom", "maturation: RuntimeError: boom"])
    ledger = build_prospective_day_ledger(conn, today=date(2024, 1, 10))
    day = _status_for(ledger, "2024-01-03")
    assert day.status == PROSPECTIVE_DAY_MISSED


def test_stuck_running_row_for_past_day_classifies_missed():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=1)
    _leave_stuck_running(conn, "2024-01-03")  # simulated crash: never finished
    ledger = build_prospective_day_ledger(conn, today=date(2024, 1, 10))
    day = _status_for(ledger, "2024-01-03")
    assert day.status == PROSPECTIVE_DAY_MISSED
    assert "running" in day.reason.lower() or "crash" in day.reason.lower() or "interrupted" in day.reason.lower()


def test_expected_for_today_with_no_run_yet():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=1)
    ledger = build_prospective_day_ledger(conn, today=date(2024, 1, 3))
    day = _status_for(ledger, "2024-01-03")
    assert day.status == PROSPECTIVE_DAY_EXPECTED


# --- production_run_status attachment ---


def test_production_run_status_attached_when_present_and_none_when_absent():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=1)
    _record_production_run(conn, "2024-01-02", "success")
    _record_research_run(conn, "2024-01-03", STATUS_SUCCESS_RESEARCH, observations_created=1)
    # no production run recorded for 2024-01-03

    ledger = build_prospective_day_ledger(conn, today=date(2024, 1, 10))
    d1 = _status_for(ledger, "2024-01-02")
    d2 = _status_for(ledger, "2024-01-03")
    assert d1.production_run_status == "success"
    assert d2.production_run_status is None


# --- prospective_evidence_audit_summary ---


def test_audit_summary_counts_match_ledger_and_include_maturation_rollup():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=1)
    _record_research_run(conn, "2024-01-03", STATUS_SUCCESS_RESEARCH, observations_created=0, duplicates_skipped=1)
    summary = prospective_evidence_audit_summary(conn, today=date(2024, 1, 10))
    assert "counts_by_status" in summary
    assert sum(summary["counts_by_status"].values()) == summary["ledger_days"]
    assert summary["eligibility_start"] == "2024-01-02"
    assert "maturation_by_horizon" in summary
    assert summary["maturation_by_horizon"] == {}  # no outcomes matured yet


def test_audit_summary_is_deterministic_across_repeated_calls():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=1)
    _record_research_run(conn, "2024-01-03", STATUS_PARTIAL_FAILURE_RESEARCH, observations_created=1,
                          errors=["AAPL: ValueError: boom"])
    first = prospective_evidence_audit_summary(conn, today=date(2024, 1, 10))
    second = prospective_evidence_audit_summary(conn, today=date(2024, 1, 10))
    assert first == second


# --- Component B is genuinely read-only ---


def test_prospective_audit_module_never_calls_a_write_function():
    """Structural check: ops/prospective_audit.py's own source (excluding
    import statements) must never call anything named record_*/mature_*/
    save_* - it may only ever import and call load_*/is_likely_trading_day/
    trading_sessions_between."""
    with open("ops/prospective_audit.py") as f:
        lines = f.readlines()
    call_pattern = re.compile(r"\b(record_\w*|mature_\w*|save_\w*)\s*\(")
    offenders = []
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("from ") or stripped.startswith("import "):
            continue
        if call_pattern.search(line):
            offenders.append((lineno, line.strip()))
    assert not offenders, f"ops/prospective_audit.py must never call a write function: {offenders}"


def test_prospective_audit_never_imports_a_write_function_by_name():
    with open("ops/prospective_audit.py") as f:
        tree = ast.parse(f.read(), filename="ops/prospective_audit.py")
    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.extend(alias.name for alias in node.names)
    forbidden = [n for n in imported_names if n.startswith("record_") or n.startswith("mature_") or n.startswith("save_")]
    assert not forbidden, f"ops/prospective_audit.py must never import a write function: {forbidden}"


# --- Immutability hardening (§2.4) ---


def _has_update_sql(file_path: str) -> bool:
    """AST-based scan: True iff any string constant in the file looks like
    an actual `UPDATE <table> SET ...` SQL statement (not merely prose that
    happens to contain the word "UPDATE", e.g. a docstring)."""
    with open(file_path) as f:
        tree = ast.parse(f.read(), filename=file_path)
    update_re = re.compile(r"\bUPDATE\s+\S+\s+SET\b", re.IGNORECASE)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if update_re.search(node.value):
                return True
    return False


def test_prospective_module_contains_no_update_sql():
    assert _has_update_sql("strategy_lab/prospective.py") is False


def test_prospective_events_module_contains_no_update_sql():
    assert _has_update_sql("strategy_lab/prospective_events.py") is False


def test_record_observation_twice_never_overwrites_original_values():
    conn = make_test_db()
    record_observation(
        conn, observation_date="2024-01-02", ticker="AAA", score=50.0, stage="trend", regime="bullish_trend",
        control_entry_signal=False, experiment_a_entry_signal=False, experiment_b_entry_signal=False,
        adx=20.0, rsi=40.0, macd=0.1, macd_signal=0.05, volume_ratio=1.0, close=100.0, source="alpaca_adjusted",
    )
    # second call, same (ticker, observation_date), DIFFERENT field values
    inserted_again = record_observation(
        conn, observation_date="2024-01-02", ticker="AAA", score=999.0, stage="momentum", regime="bearish",
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=99.0, rsi=99.0, macd=9.0, macd_signal=9.0, volume_ratio=9.0, close=999.0, source="alpaca_adjusted",
    )
    assert inserted_again is False
    rows = load_observations(conn)
    assert len(rows) == 1
    assert rows.iloc[0]["score"] == 50.0
    assert rows.iloc[0]["stage"] == "trend"
    assert rows.iloc[0]["close"] == 100.0


def test_record_event_twice_never_overwrites_original_row():
    conn = make_test_db()
    first = record_event(conn, event_date="2024-01-03", ticker="AAA", event_type="score_crossing_70",
                          score=75.0, stage="momentum", regime="bullish_trend")
    second = record_event(conn, event_date="2024-01-03", ticker="AAA", event_type="score_crossing_70",
                           score=999.0, stage="volume", regime="bearish")
    assert first is True
    assert second is False
    rows = load_events(conn, ticker="AAA")
    assert len(rows) == 1
    assert rows.iloc[0]["score"] == 75.0
    assert rows.iloc[0]["stage"] == "momentum"
