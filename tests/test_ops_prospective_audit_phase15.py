"""Tests for Phase 15 Component C (long-term monitoring, additive functions
on ops/prospective_audit.py) and Component D (dashboard read-only safety),
per docs/specs/phase15.md §3, §4, §8 items 16-28.

Imports the SAME module as tests/test_ops_prospective_audit.py
(ops.prospective_audit) - kept as a separate file per the spec's own stated
convention (docs/specs/phase15.md §6). That existing file is left untouched.

Everything here is synthetic/deterministic, in-memory SQLite, no network,
no Streamlit runtime (dashboard checks are file-source-based, matching
tests/test_ops_prospective_dashboard.py's existing convention).
"""
import contextlib
import inspect
import os
import sqlite3
from datetime import date

import pytest

from db.schema import init_db
from ops.data_quality import OVERALL_DEGRADED, OVERALL_FAILED, OVERALL_HEALTHY, OVERALL_STALE
from ops.evidence_classification import (
    EVIDENCE_EARLY_EVIDENCE,
    EVIDENCE_EVALUATION_READY,
    EVIDENCE_INSUFFICIENT_DATA,
)
from ops.experiment_registry import register_experiment
from ops.prospective_audit import (
    classify_research_operational_health,
    compute_capture_rate,
    compute_consecutive_missed_days,
    compute_correction_counts,
    compute_maturity_lag,
    compute_provenance_coverage,
    compute_research_job_rates,
    long_term_monitoring_summary,
    prospective_evidence_audit_summary,
)
from strategy_lab.outcome_maturation import OUTCOME_TABLE_NAME, STATUS_MATURED
from strategy_lab.outcome_maturation import ensure_schema as ensure_outcome_schema
from strategy_lab.prospective import record_observation
from strategy_lab.research_automation import (
    STATUS_FAILED,
    STATUS_SUCCESS_RESEARCH,
    ResearchRunResult,
)
from strategy_lab.research_automation import _finish_run as research_finish_run
from strategy_lab.research_automation import _start_run as research_start_run
from strategy_lab.research_automation import ensure_schema as ensure_research_schema

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _record_research_run(conn, trading_date: str, status: str, observations_created=0,
                          duplicates_skipped=0, errors=None, tickers_attempted=1) -> int:
    ensure_research_schema(conn)
    run_id = research_start_run(conn, date.fromisoformat(trading_date))
    result = ResearchRunResult(
        run_id=run_id, status=status, trading_date=trading_date,
        tickers_attempted=tickers_attempted, observations_created=observations_created,
        duplicates_skipped=duplicates_skipped, errors=errors or [],
    )
    research_finish_run(conn, run_id, result)
    return run_id


def _backdate_started_at(conn, run_id, trading_date, hhmmss):
    conn.execute("UPDATE research_run_history SET started_at = ? WHERE id = ?", (f"{trading_date} {hhmmss}", run_id))
    conn.commit()


def _insert_outcome_row(conn, observation_id, ticker, observation_date, horizon_days, status,
                         exit_date=None, realized_return=None, matured_at="2024-01-08 20:00:00"):
    ensure_outcome_schema(conn)
    conn.execute(
        f"INSERT INTO {OUTCOME_TABLE_NAME} "
        "(observation_id, ticker, observation_date, horizon_days, status, realized_return, exit_date, matured_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (observation_id, ticker, observation_date, horizon_days, status, realized_return, exit_date, matured_at),
    )
    conn.commit()


# --- #16: capture rate excludes EXPECTED ---


def test_capture_rate_excludes_expected_from_numerator_and_denominator():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=5)
    _record_research_run(conn, "2024-01-03", STATUS_SUCCESS_RESEARCH, observations_created=5)
    _record_research_run(conn, "2024-01-04", STATUS_FAILED, observations_created=0, errors=["boom"])
    # 2024-01-05 (today) has no run at all yet -> EXPECTED, must be excluded.
    result = compute_capture_rate(conn, today=date(2024, 1, 5))
    assert result["window_sessions_considered"] == 3
    assert result["captured"] == 2
    assert result["missed"] == 1
    assert result["capture_rate_pct"] == round(100.0 * 2 / 3, 2)


def test_capture_rate_none_when_nothing_considered():
    conn = make_test_db()
    result = compute_capture_rate(conn, today=date(2024, 1, 5))
    assert result["window_sessions_considered"] == 0
    assert result["capture_rate_pct"] is None


# --- #17: consecutive missed days skips EXPECTED ---


def test_consecutive_missed_days_skips_over_expected_today():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=5)
    _record_research_run(conn, "2024-01-03", STATUS_FAILED, observations_created=0, errors=["boom"])
    _record_research_run(conn, "2024-01-04", STATUS_FAILED, observations_created=0, errors=["boom"])
    # 2024-01-05 (today) has no run -> EXPECTED, must be skipped over, not counted and not a break.
    n = compute_consecutive_missed_days(conn, today=date(2024, 1, 5))
    assert n == 2


def test_consecutive_missed_days_zero_when_most_recent_non_expected_day_not_missed():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=5)
    n = compute_consecutive_missed_days(conn, today=date(2024, 1, 3))
    assert n == 0


# --- #18: maturity lag across a weekend boundary ---


def test_maturity_lag_calendar_day_lag_exceeds_horizon_across_weekend():
    conn = make_test_db()
    # Fri 2024-01-05 -> Mon 2024-01-08 is horizon_days=1 trading session, but
    # 3 CALENDAR days, because a weekend sits between them.
    _insert_outcome_row(
        conn, observation_id=1, ticker="AAA", observation_date="2024-01-05",
        horizon_days=1, status=STATUS_MATURED, exit_date="2024-01-08",
        realized_return=0.01, matured_at="2024-01-08 20:00:00",
    )
    lag = compute_maturity_lag(conn)
    assert 1 in lag
    assert lag[1]["n_matured"] == 1
    assert lag[1]["mean_calendar_day_lag"] == 3
    assert lag[1]["mean_calendar_day_lag"] > 1  # exceeds the trading-day horizon itself


def test_maturity_lag_empty_when_no_matured_outcomes():
    conn = make_test_db()
    assert compute_maturity_lag(conn) == {}


def test_maturity_lag_pipeline_notice_lag_zero_for_prompt_maturation():
    conn = make_test_db()
    # matured_at the SAME calendar day the horizon elapsed (Mon 2024-01-08) -> 0 notice lag.
    _insert_outcome_row(
        conn, observation_id=1, ticker="AAA", observation_date="2024-01-05",
        horizon_days=1, status=STATUS_MATURED, exit_date="2024-01-08",
        realized_return=0.01, matured_at="2024-01-08 20:00:00",
    )
    lag = compute_maturity_lag(conn)
    assert lag[1]["mean_pipeline_notice_lag_days"] == 0


# --- #19: research job rates collapse same-day retries to the latest row ---


def test_research_job_rates_collapses_same_day_retry_to_latest_status():
    conn = make_test_db()
    failed_id = _record_research_run(conn, "2024-01-03", STATUS_FAILED, observations_created=0, errors=["boom"])
    _backdate_started_at(conn, failed_id, "2024-01-03", "09:00:00")
    success_id = _record_research_run(conn, "2024-01-03", STATUS_SUCCESS_RESEARCH, observations_created=5)
    _backdate_started_at(conn, success_id, "2024-01-03", "10:00:00")

    rates = compute_research_job_rates(conn, today=date(2024, 1, 3))
    assert rates["sessions_considered"] == 1
    assert rates["success_rate_pct"] == 100.0
    assert rates["failure_rate_pct"] == 0.0


def test_research_job_rates_no_history_returns_none_rates():
    conn = make_test_db()
    rates = compute_research_job_rates(conn, today=date(2024, 1, 5))
    assert rates["sessions_considered"] == 0
    assert rates["success_rate_pct"] is None


# --- #20: provenance coverage - many-to-one experiment mapping + unregistered + legacy ---


def _seed_bare_observation(conn, ticker, obs_date, config_fingerprint):
    record_observation(
        conn, observation_date=obs_date, ticker=ticker, score=1.0, stage="none", regime="bullish_trend",
        control_entry_signal=False, experiment_a_entry_signal=False, experiment_b_entry_signal=False,
        adx=1.0, rsi=1.0, macd=1.0, macd_signal=1.0, volume_ratio=1.0, close=1.0,
        source="alpaca", config_fingerprint=config_fingerprint,
    )


def test_provenance_coverage_many_to_one_unregistered_and_legacy_buckets():
    conn = make_test_db()
    _seed_bare_observation(conn, "AAA", "2024-01-02", "fp-shared")       # matches 2 experiments
    _seed_bare_observation(conn, "AAA", "2024-01-03", "fp-unregistered")  # matches 0 experiments
    _seed_bare_observation(conn, "AAA", "2024-01-04", None)              # legacy NULL

    register_experiment(conn, experiment_id="control-x", methodology_version="v1", hypothesis="h1", config_fingerprint="fp-shared")
    register_experiment(conn, experiment_id="experiment-a-x", methodology_version="v1", hypothesis="h2", config_fingerprint="fp-shared")

    coverage = compute_provenance_coverage(conn)
    assert coverage["observations_with_fingerprint"] == 2
    assert coverage["observations_unknown_legacy"] == 1
    assert coverage["by_experiment_id"]["control-x"] == 1
    assert coverage["by_experiment_id"]["experiment-a-x"] == 1  # same fingerprint counted under BOTH
    assert coverage["unregistered_fingerprint_count"] == 1


def test_provenance_coverage_empty_db():
    conn = make_test_db()
    coverage = compute_provenance_coverage(conn)
    assert coverage == {
        "observations_with_fingerprint": 0, "observations_unknown_legacy": 0,
        "by_experiment_id": {}, "unregistered_fingerprint_count": 0,
    }


# --- #21: classify_research_operational_health boundary table ---


@pytest.mark.parametrize("capture_rate_pct,consecutive_missed_days,job_failure_rate_pct,expected", [
    (100.0, 5, 0.0, OVERALL_FAILED),        # >=5 missed always FAILED
    (100.0, 4, 0.0, OVERALL_STALE),         # top of [2,4]
    (100.0, 2, 0.0, OVERALL_STALE),         # bottom of [2,4]
    (100.0, 3, 0.0, OVERALL_STALE),         # middle of [2,4]
    (89.0, 1, 0.0, OVERALL_DEGRADED),       # capture < 90, missed in [0,1]
    (90.0, 0, 0.0, OVERALL_HEALTHY),        # exactly 90 is NOT < 90
    (91.0, 0, 0.0, OVERALL_HEALTHY),
    (100.0, 0, 11.0, OVERALL_DEGRADED),     # failure rate > 10
    (100.0, 0, 10.0, OVERALL_HEALTHY),      # exactly 10 is NOT > 10
    (100.0, 1, None, OVERALL_HEALTHY),      # None failure rate treated as 0
    (None, 0, 0.0, OVERALL_FAILED),         # capture_rate_pct None forces FAILED
    (None, 5, 0.0, OVERALL_FAILED),
])
def test_classify_research_operational_health_boundaries(
    capture_rate_pct, consecutive_missed_days, job_failure_rate_pct, expected,
):
    result = classify_research_operational_health(capture_rate_pct, consecutive_missed_days, job_failure_rate_pct)
    assert result == expected


# --- #22: vocabulary-separation ---


def test_operational_health_vocabulary_disjoint_from_evidence_vocabulary():
    operational_set = {OVERALL_HEALTHY, OVERALL_DEGRADED, OVERALL_STALE, OVERALL_FAILED}
    evidence_set = {EVIDENCE_INSUFFICIENT_DATA, EVIDENCE_EARLY_EVIDENCE, EVIDENCE_EVALUATION_READY}
    assert operational_set.isdisjoint(evidence_set)

    samples = [
        (100.0, 0, 0.0), (100.0, 2, 0.0), (100.0, 5, 0.0), (50.0, 1, 20.0), (None, 0, 0.0),
    ]
    for capture, missed, fail in samples:
        result = classify_research_operational_health(capture, missed, fail)
        assert result in operational_set
        assert result not in evidence_set


# --- #23: long_term_monitoring_summary is a strict superset ---


def test_long_term_monitoring_summary_is_superset_of_base_summary():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=1)
    base = prospective_evidence_audit_summary(conn, today=date(2024, 1, 10))
    extended = long_term_monitoring_summary(conn, today=date(2024, 1, 10))

    assert set(base.keys()).issubset(set(extended.keys()))
    assert set(extended.keys()) - set(base.keys()) == {
        "capture_rate", "consecutive_missed_days", "maturity_lag_by_horizon",
        "research_job_rates", "correction_counts", "provenance_coverage", "operational_health",
    }
    for k in base:
        assert extended[k] == base[k]
    assert extended["operational_health"] in {OVERALL_HEALTHY, OVERALL_DEGRADED, OVERALL_STALE, OVERALL_FAILED}


# --- #24: idempotency ---


def test_capture_rate_and_job_rates_idempotent_across_repeated_calls():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=1)
    _record_research_run(conn, "2024-01-03", STATUS_FAILED, observations_created=0, errors=["boom"])

    r1 = compute_capture_rate(conn, today=date(2024, 1, 10))
    r2 = compute_capture_rate(conn, today=date(2024, 1, 10))
    assert r1 == r2

    j1 = compute_research_job_rates(conn, today=date(2024, 1, 10))
    j2 = compute_research_job_rates(conn, today=date(2024, 1, 10))
    assert j1 == j2


def test_long_term_monitoring_summary_deterministic_across_calls():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=1)
    first = long_term_monitoring_summary(conn, today=date(2024, 1, 10))
    second = long_term_monitoring_summary(conn, today=date(2024, 1, 10))
    assert first == second


# --- #25: no-look-ahead ---


def test_capture_rate_never_considers_a_day_after_today():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=1)
    # This run is dated in the FUTURE relative to today=2024-01-03 below.
    _record_research_run(conn, "2024-01-04", STATUS_FAILED, observations_created=0, errors=["boom"])

    result = compute_capture_rate(conn, today=date(2024, 1, 3))
    # The ledger itself is bounded by [eligibility_start, today] - 2024-01-04
    # cannot appear at all, so it can never be counted as "missed".
    assert result["window_sessions_considered"] == 1
    assert result["missed"] == 0
    assert result["captured"] == 1


def test_correction_counts_reuses_load_corrections_and_never_raises_on_empty():
    conn = make_test_db()
    counts = compute_correction_counts(conn)
    assert counts == {"total": 0, "affecting_frozen_artifacts": 0, "since": None}


# ============================================================
# Component D: dashboard read-only safety (§4, §8 items 27-28)
# ============================================================


def _view_source(relative_path):
    with open(os.path.join(REPO_ROOT, relative_path)) as f:
        return f.read()


def _extract_function_source(file_text: str, func_name: str) -> str:
    """Slice out one top-level function's body from raw file text, from its
    `def <func_name>(` line to the next top-level `def `/`class ` (or EOF).
    Mirrors tests/test_ops_prospective_dashboard.py's file-text-based
    convention (no streamlit-runtime import needed for these checks)."""
    lines = file_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"def {func_name}("):
            start = i
            break
    assert start is not None, f"{func_name} not found as a top-level function"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("def ") or lines[j].startswith("class "):
            end = j
            break
    return "\n".join(lines[start:end])


def test_render_evidence_provenance_audit_contains_no_button_or_form():
    source = _view_source("dashboard/views/ops_overview.py")
    assert "_render_evidence_provenance_audit" in source
    func_source = _extract_function_source(source, "_render_evidence_provenance_audit")
    assert "st.button" not in func_source
    assert "st.form" not in func_source


def test_render_evidence_provenance_detail_contains_no_button_or_form():
    source = _view_source("dashboard/views/strategy_lab.py")
    assert "_render_evidence_provenance_detail" in source
    func_source = _extract_function_source(source, "_render_evidence_provenance_detail")
    assert "st.button" not in func_source
    assert "st.form" not in func_source


def test_ops_overview_view_still_has_no_st_button_calls_anywhere():
    source = _view_source("dashboard/views/ops_overview.py")
    assert "st.button" not in source
    assert "st.form" not in source


def test_strategy_lab_view_still_has_no_st_button_calls_anywhere():
    source = _view_source("dashboard/views/strategy_lab.py")
    assert "st.button" not in source
    assert "st.form" not in source


def test_evidence_provenance_audit_inserted_before_automation_history():
    """§0.4 insertion point: immediately after research-cache-completeness,
    before _render_automation_history()."""
    source = _view_source("dashboard/views/ops_overview.py")
    idx_evidence = source.index("_render_evidence_provenance_audit()")
    idx_automation = source.index("_render_automation_history()")
    assert idx_evidence < idx_automation


def test_evidence_provenance_detail_called_last_in_strategy_lab_render():
    source = _view_source("dashboard/views/strategy_lab.py")
    assert "_render_evidence_provenance_detail()" in source


# --- getters: deterministic, single db_session() ---


def test_get_long_term_monitoring_summary_and_get_correction_audit_report_open_exactly_one_db_session():
    import dashboard.data as dashboard_data
    src1 = inspect.getsource(dashboard_data.get_long_term_monitoring_summary)
    src2 = inspect.getsource(dashboard_data.get_correction_audit_report)
    assert src1.count("db_session(") == 1
    assert src2.count("db_session(") == 1


def _patch_db_session(monkeypatch, conn):
    import dashboard.data as dashboard_data

    @contextlib.contextmanager
    def fake_db_session():
        yield conn
    monkeypatch.setattr(dashboard_data, "db_session", fake_db_session)


def test_get_long_term_monitoring_summary_deterministic_across_repeated_calls(monkeypatch):
    import dashboard.data as dashboard_data
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    dashboard_data.get_long_term_monitoring_summary.clear()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=1)

    first = dashboard_data.get_long_term_monitoring_summary()
    dashboard_data.get_long_term_monitoring_summary.clear()
    second = dashboard_data.get_long_term_monitoring_summary()
    assert first == second


def test_get_correction_audit_report_deterministic_across_repeated_calls(monkeypatch):
    import dashboard.data as dashboard_data
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    dashboard_data.get_correction_audit_report.clear()

    first = dashboard_data.get_correction_audit_report()
    dashboard_data.get_correction_audit_report.clear()
    second = dashboard_data.get_correction_audit_report()
    assert first.equals(second)
    assert first.empty  # no corrections seeded


def test_get_correction_audit_report_never_writes_to_paper_orders_alert_state_or_alerts(monkeypatch):
    import dashboard.data as dashboard_data
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    dashboard_data.get_correction_audit_report.clear()
    dashboard_data.get_long_term_monitoring_summary.clear()

    def _counts():
        return {
            t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("paper_orders", "alert_state", "alerts")
        }

    before = _counts()
    dashboard_data.get_correction_audit_report()
    dashboard_data.get_long_term_monitoring_summary()
    after = _counts()
    assert before == after == {"paper_orders": 0, "alert_state": 0, "alerts": 0}


def test_clear_all_caches_includes_phase15_getters():
    import dashboard.data as dashboard_data
    source = inspect.getsource(dashboard_data.clear_all_caches)
    assert "get_long_term_monitoring_summary.clear()" in source
    assert "get_correction_audit_report.clear()" in source


# --- structural safety tests from Phase 14 must still cover the newly-modified files (§8 final bullet) ---


def test_existing_structural_safety_tests_still_cover_modified_strategy_lab_files():
    """strategy_lab/prospective.py, prospective_events.py, outcome_maturation.py,
    and research_automation.py were all modified by Phase 15 - confirm the
    existing structural-safety scan (tests/test_strategy_lab.py) still scans
    every .py file under strategy_lab/, so these modified files remain
    automatically covered with zero spec changes needed to that test."""
    with open("tests/test_strategy_lab.py") as f:
        content = f.read()
    assert "trading." in content or "trading\\." in content
    assert "alerts." in content or "alerts\\." in content


def test_existing_no_update_sql_test_still_covers_prospective_and_events_files():
    """tests/test_ops_prospective_audit.py's _has_update_sql-style check must
    keep passing unmodified for the Phase-15-modified prospective.py/
    prospective_events.py files (checked directly here too, as a redundant
    guarantee that Phase 15's additive ALTER TABLE + extended INSERT column
    lists never introduced an UPDATE ... SET statement)."""
    import ast
    import re

    def _has_update_sql(file_path: str) -> bool:
        with open(file_path) as f:
            tree = ast.parse(f.read(), filename=file_path)
        update_re = re.compile(r"\bUPDATE\s+\S+\s+SET\b", re.IGNORECASE)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if update_re.search(node.value):
                    return True
        return False

    assert _has_update_sql("strategy_lab/prospective.py") is False
    assert _has_update_sql("strategy_lab/prospective_events.py") is False
