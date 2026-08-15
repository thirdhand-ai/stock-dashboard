"""Tests for Phase 16 Area B: ops/regime_reconstruction_audit.py
(docs/specs/phase16.md §2.1, §8 items 3, 5, 10).

Same safety-critical convention as tests/test_ops_correction_impact_audit.py:
this module must have zero write capability at all. research_prospective_observations
has no UPDATE path today by design - this module must never add one.
"""
import ast
import re
import sqlite3
from datetime import date, timedelta

import pytest

from db.database import db_session
from db.schema import init_db
from ops.regime_reconstruction_audit import (
    REASON_NO_SPY_HISTORY_AT_OR_BEFORE_DATE,
    REASON_RECONSTRUCTABLE,
    LegacyRegimeGapRow,
    audit_legacy_regime_gaps,
    legacy_regime_gap_summary,
)
from strategy_lab.cache_integrity import CORRECTIONS_TABLE
from strategy_lab.cache_integrity import ensure_schema as ensure_corrections_schema
from strategy_lab.prospective import load_observations, record_observation
from strategy_lab.prospective_events import load_events, record_event
from strategy_lab.outcome_maturation import load_outcomes
from strategy_lab.regime_history import compute_historical_regime_series, regime_label_as_of

MODULE_PATH = "ops/regime_reconstruction_audit.py"


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _seed_price(conn, ticker, d, close, source, volume=1000):
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, d, close, close, close, close, volume, source),
    )
    conn.commit()


def _seed_spy_series(conn, source="alpaca_adjusted", start="2020-01-01", n=260, base=300.0, step=0.5):
    d0 = date.fromisoformat(start)
    price = base
    for i in range(n):
        price += step
        _seed_price(conn, "SPY", (d0 + timedelta(days=i)).isoformat(), price, source)
    return d0


def _seed_legacy_observation(conn, ticker, obs_date):
    """regime=None (legacy row) - the exact shape audit_legacy_regime_gaps scans for."""
    record_observation(
        conn, observation_date=obs_date, ticker=ticker, score=50.0, stage="trend", regime=None,
        control_entry_signal=False, experiment_a_entry_signal=False, experiment_b_entry_signal=False,
        adx=20.0, rsi=50.0, macd=0.5, macd_signal=0.3, volume_ratio=1.0, close=100.0,
    )


def _seed_correction(conn, ticker, date_str, source, old_close=100.0, new_close=101.0):
    ensure_corrections_schema(conn)
    conn.execute(
        f"""
        INSERT INTO {CORRECTIONS_TABLE} (
            ticker, date, source, old_close, new_close, old_volume, new_volume,
            old_fetched_at, new_fetched_at, reason, affects_frozen_artifacts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ticker, date_str, source, old_close, new_close, 1000, 1000, None, None, "test correction", "[]"),
    )
    conn.commit()


# --- §2.1 item 1 / §8 item 5: structural read-only guarantees (mirrors
# tests/test_ops_correction_impact_audit.py's exact 3-scan pattern) ---


def test_module_never_calls_execute_executemany_executescript_or_commit():
    with open(MODULE_PATH) as f:
        lines = f.readlines()
    pattern = re.compile(r"\.execute\(|\.executemany\(|\.executescript\(|\.commit\(")
    offenders = [(i, l.strip()) for i, l in enumerate(lines, start=1) if pattern.search(l)]
    assert not offenders, f"{MODULE_PATH} must never call a DB write method: {offenders}"


def test_module_contains_no_insert_update_delete_sql_string_constants():
    with open(MODULE_PATH) as f:
        tree = ast.parse(f.read(), filename=MODULE_PATH)
    sql_pattern = re.compile(r"\bINSERT\s+INTO\b|\bUPDATE\s+\S+\s+SET\b|\bDELETE\s+FROM\b", re.IGNORECASE)
    offenders = [
        node.value[:120] for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and sql_pattern.search(node.value)
    ]
    assert not offenders, f"{MODULE_PATH} must contain no real SQL mutation string: {offenders}"


def test_module_imports_no_write_capable_function_by_name():
    with open(MODULE_PATH) as f:
        tree = ast.parse(f.read(), filename=MODULE_PATH)
    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.extend(alias.name for alias in node.names)
    forbidden_exact = {"_persist", "_record_correction", "_store_adjusted_bars", "refresh_incomplete_latest_bars"}
    forbidden_prefixes = ("record_", "mature_", "save_")
    offenders = [n for n in imported_names if n in forbidden_exact or n.startswith(forbidden_prefixes)]
    assert not offenders, f"{MODULE_PATH} must never import a write-capable function: {offenders}"


def test_module_never_calls_a_write_function_by_name_in_source():
    with open(MODULE_PATH) as f:
        tree = ast.parse(f.read(), filename=MODULE_PATH)
    forbidden_prefixes = ("record_", "mature_", "save_")
    forbidden_exact = {"_persist", "_record_correction", "_store_adjusted_bars", "refresh_incomplete_latest_bars"}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
        if name and (name in forbidden_exact or name.startswith(forbidden_prefixes)):
            offenders.append((node.lineno, name))
    assert not offenders, f"{MODULE_PATH} must never CALL a write function: {offenders}"


def test_module_has_no_docstring_or_comment_false_positives_in_scans():
    with open(MODULE_PATH) as f:
        content = f.read()
    assert "conn.execute" in content  # present as prose/docstring text only
    assert len(content) > 1500


# --- §2.1 item 2 / §8 item 2: reconstructable legacy row matches direct lookup ---


def test_reconstructable_legacy_row_matches_direct_regime_label_as_of_call():
    conn = make_test_db()
    d0 = _seed_spy_series(conn)
    regime_series = compute_historical_regime_series(conn)
    assert not regime_series.empty
    # An observation dated comfortably inside the reconstructable range.
    obs_date = regime_series.iloc[-1]["date"]
    _seed_legacy_observation(conn, "AAPL", obs_date)

    rows = audit_legacy_regime_gaps(conn)
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, LegacyRegimeGapRow)
    assert row.reconstructable is True
    assert row.reason == REASON_RECONSTRUCTABLE
    assert row.reconstructed_label == regime_label_as_of(regime_series, obs_date)
    assert row.reconstructed_label is not None


# --- §2.1 item 3 / §8 item 3: insufficient-history -> not reconstructable ---


def test_not_reconstructable_before_spy_ever_had_sma_long_days_rows():
    conn = make_test_db()
    d0 = _seed_spy_series(conn)
    regime_series = compute_historical_regime_series(conn)
    assert not regime_series.empty
    first_available_date = regime_series.iloc[0]["date"]
    # An observation dated BEFORE the regime series' own first available row
    # (i.e. before SPY ever accumulated a full 200-row trailing SMA window).
    before_date = (date.fromisoformat(first_available_date) - timedelta(days=5)).isoformat()
    _seed_legacy_observation(conn, "AAPL", before_date)

    rows = audit_legacy_regime_gaps(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row.reconstructable is False
    assert row.reason == REASON_NO_SPY_HISTORY_AT_OR_BEFORE_DATE
    assert row.reconstructed_label is None


def test_not_reconstructable_when_spy_has_zero_history_at_all():
    conn = make_test_db()
    _seed_legacy_observation(conn, "AAPL", "2026-01-01")
    rows = audit_legacy_regime_gaps(conn)
    assert len(rows) == 1
    assert rows[0].reconstructable is False
    assert rows[0].reason == REASON_NO_SPY_HISTORY_AT_OR_BEFORE_DATE
    assert rows[0].reconstructed_label is None


# --- §2.1 items 4-6: correction-sensitivity window ---


def test_correction_inside_window_flags_would_change():
    conn = make_test_db()
    _seed_spy_series(conn)
    regime_series = compute_historical_regime_series(conn)
    obs_date = regime_series.iloc[-1]["date"]
    _seed_legacy_observation(conn, "AAPL", obs_date)
    # A correction dated a few days before the observation, inside the window.
    corr_date = (date.fromisoformat(obs_date) - timedelta(days=3)).isoformat()
    _seed_correction(conn, "SPY", corr_date, "alpaca_adjusted")

    rows = audit_legacy_regime_gaps(conn)
    assert len(rows) == 1
    assert rows[0].corrections_in_window_count >= 1
    assert rows[0].would_change_if_corrections_applied is True

    summary = legacy_regime_gap_summary(conn)
    assert summary["correction_sensitive_count"] == 1


def test_correction_outside_window_after_observation_date_not_flagged():
    conn = make_test_db()
    _seed_spy_series(conn)
    regime_series = compute_historical_regime_series(conn)
    obs_date = regime_series.iloc[-1]["date"]
    _seed_legacy_observation(conn, "AAPL", obs_date)
    corr_date = (date.fromisoformat(obs_date) + timedelta(days=5)).isoformat()
    _seed_correction(conn, "SPY", corr_date, "alpaca_adjusted")

    rows = audit_legacy_regime_gaps(conn)
    assert rows[0].corrections_in_window_count == 0
    assert rows[0].would_change_if_corrections_applied is False


def test_correction_far_before_window_start_not_flagged():
    conn = make_test_db()
    _seed_spy_series(conn)
    regime_series = compute_historical_regime_series(conn)
    obs_date = regime_series.iloc[-1]["date"]
    _seed_legacy_observation(conn, "AAPL", obs_date)
    corr_date = (date.fromisoformat(obs_date) - timedelta(days=2000)).isoformat()
    _seed_correction(conn, "SPY", corr_date, "alpaca_adjusted")

    rows = audit_legacy_regime_gaps(conn)
    assert rows[0].corrections_in_window_count == 0
    assert rows[0].would_change_if_corrections_applied is False


def test_correction_with_non_research_source_never_counted():
    conn = make_test_db()
    _seed_spy_series(conn)
    regime_series = compute_historical_regime_series(conn)
    obs_date = regime_series.iloc[-1]["date"]
    _seed_legacy_observation(conn, "AAPL", obs_date)
    corr_date = (date.fromisoformat(obs_date) - timedelta(days=3)).isoformat()
    _seed_correction(conn, "SPY", corr_date, "alpaca")  # NOT RESEARCH_SOURCE ("alpaca_adjusted")

    rows = audit_legacy_regime_gaps(conn)
    assert rows[0].corrections_in_window_count == 0
    assert rows[0].would_change_if_corrections_applied is False


# --- observations with regime already set are excluded entirely ---


def test_observations_with_non_null_regime_are_excluded():
    conn = make_test_db()
    _seed_spy_series(conn)
    record_observation(
        conn, observation_date="2020-06-01", ticker="AAPL", score=90.0, stage="volume", regime="bullish_trend",
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=100.0,
    )
    rows = audit_legacy_regime_gaps(conn)
    assert rows == []
    summary = legacy_regime_gap_summary(conn)
    assert summary["total_legacy_null_regime"] == 0


def test_empty_db_returns_empty_list_and_zeroed_summary():
    conn = make_test_db()
    assert audit_legacy_regime_gaps(conn) == []
    summary = legacy_regime_gap_summary(conn)
    assert summary == {
        "total_legacy_null_regime": 0, "reconstructable_count": 0,
        "not_reconstructable_count": 0, "correction_sensitive_count": 0,
    }


# --- §2.1 item 7 / §8 item 5: zero-writes proof against a seeded DB ---


def _snapshot(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    regimes = [dict(r) for r in conn.execute(
        "SELECT id, regime FROM research_prospective_observations ORDER BY id"
    ).fetchall()]
    return counts, regimes


def test_zero_writes_proof_seeded_db():
    conn = make_test_db()
    _seed_spy_series(conn)
    regime_series = compute_historical_regime_series(conn)
    obs_date = regime_series.iloc[-1]["date"]
    _seed_legacy_observation(conn, "AAPL", obs_date)
    _seed_legacy_observation(conn, "MSFT", (date.fromisoformat(obs_date) - timedelta(days=1)).isoformat())
    corr_date = (date.fromisoformat(obs_date) - timedelta(days=3)).isoformat()
    _seed_correction(conn, "SPY", corr_date, "alpaca_adjusted")

    before_counts, before_regimes = _snapshot(conn)
    audit_legacy_regime_gaps(conn)
    legacy_regime_gap_summary(conn)
    after_counts, after_regimes = _snapshot(conn)

    assert before_counts == after_counts, f"row counts changed: {before_counts} -> {after_counts}"
    assert before_regimes == after_regimes, f"regime values changed: {before_regimes} -> {after_regimes}"


# --- §2.1 item 8 / §8 item 5: live, read-only verification against the real DB ---


def test_live_db_legacy_regime_gap_summary_runs_cleanly_and_zero_writes():
    with db_session() as conn:
        conn.row_factory = sqlite3.Row
        before_counts, before_regimes = _snapshot(conn)
        real_null_count = conn.execute(
            "SELECT COUNT(*) FROM research_prospective_observations WHERE regime IS NULL"
        ).fetchone()[0]

        summary = legacy_regime_gap_summary(conn)  # must not raise

        after_counts, after_regimes = _snapshot(conn)

    assert summary["total_legacy_null_regime"] == real_null_count
    assert before_counts == after_counts
    assert before_regimes == after_regimes
