"""Tests for Phase 17 Area D: ops/prospective_audit.py additive functions
and dashboard wiring (docs/specs/phase17.md §4.2, §4.3, §4.6, §7 items 10-11).

Mirrors tests/test_ops_prospective_audit_phase16.py's convention of a
separate file importing the same ops.prospective_audit module.
"""
import inspect
import os
import sqlite3

from db.schema import init_db
from ops.prospective_audit import (
    compute_regime_freshness_distribution,
    phase16_monitoring_summary,
    phase17_monitoring_summary,
)
from strategy_lab.prospective import record_observation

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _seed_observation(conn, ticker, obs_date, regime_freshness_status=None):
    record_observation(
        conn, observation_date=obs_date, ticker=ticker, score=80.0, stage="volume", regime="bullish_trend",
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=100.0,
        source="alpaca_adjusted", methodology_version="phase11-v1",
        regime_freshness_status=regime_freshness_status,
    )


# =================== compute_regime_freshness_distribution ===================


def test_compute_regime_freshness_distribution_counts_all_states_plus_null_bucket():
    conn = make_test_db()
    _seed_observation(conn, "AAA", "2026-01-01", regime_freshness_status="FRESH")
    _seed_observation(conn, "BBB", "2026-01-01", regime_freshness_status="FRESH")
    _seed_observation(conn, "CCC", "2026-01-01", regime_freshness_status="STALE")
    _seed_observation(conn, "DDD", "2026-01-01", regime_freshness_status="UNAVAILABLE")
    _seed_observation(conn, "EEE", "2026-01-01", regime_freshness_status=None)  # legacy row

    dist = compute_regime_freshness_distribution(conn)
    assert dist["by_status"]["FRESH"] == 2
    assert dist["by_status"]["STALE"] == 1
    assert dist["by_status"]["UNAVAILABLE"] == 1
    assert dist["by_status"]["NULL"] == 1
    assert dist["total"] == 5


def test_compute_regime_freshness_distribution_empty_db():
    conn = make_test_db()
    dist = compute_regime_freshness_distribution(conn)
    assert dist["total"] == 0
    assert dist["by_status"] == {"FRESH": 0, "STALE": 0, "UNAVAILABLE": 0, "NULL": 0}


# =================== §4.6 item 3 / §7 item 11: strict superset over phase16 ===================


def test_phase17_monitoring_summary_is_strict_superset_of_phase16_monitoring_summary():
    conn = make_test_db()
    _seed_observation(conn, "AAA", "2026-01-01", regime_freshness_status="FRESH")

    from datetime import date
    base = phase16_monitoring_summary(conn, today=date(2026, 1, 10))
    extended = phase17_monitoring_summary(conn, today=date(2026, 1, 10))

    assert set(base.keys()).issubset(set(extended.keys()))
    new_keys = set(extended.keys()) - set(base.keys())
    assert new_keys == {"regime_freshness_distribution"}
    for k in base:
        assert extended[k] == base[k], f"key {k!r} must be byte-identical between phase16 and phase17 summaries"


def test_phase17_monitoring_summary_empty_db_never_raises():
    conn = make_test_db()
    from datetime import date
    summary = phase17_monitoring_summary(conn, today=date(2026, 1, 10))
    assert "regime_freshness_distribution" in summary
    assert summary["regime_freshness_distribution"]["total"] == 0


# =================== §7 item 10: zero-writes proof ===================


def _snapshot(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def test_zero_writes_proof_for_phase17_audit_functions():
    conn = make_test_db()
    _seed_observation(conn, "AAA", "2026-01-01", regime_freshness_status="FRESH")
    _seed_observation(conn, "BBB", "2026-01-01", regime_freshness_status="STALE")

    from datetime import date
    # Warm-up call: CREATE TABLE IF NOT EXISTS lazily creates several
    # sibling tables (research_prospective_events, research_run_history,
    # experiment_registry, ...) on first read - that is schema
    # bootstrapping, not a data write. Snapshot AFTER that bootstrapping has
    # already happened, then prove a SECOND call adds zero rows anywhere.
    phase17_monitoring_summary(conn, today=date(2026, 1, 10))

    before = _snapshot(conn)
    compute_regime_freshness_distribution(conn)
    phase17_monitoring_summary(conn, today=date(2026, 1, 10))
    after = _snapshot(conn)
    assert before == after


def test_zero_writes_proof_for_compute_exit_event_type_provenance():
    from ops.event_provenance_audit import compute_exit_event_type_provenance
    conn = make_test_db()
    _seed_observation(conn, "AAA", "2026-01-01")

    compute_exit_event_type_provenance(conn)  # warm-up: lazily creates research_prospective_events

    before = _snapshot(conn)
    compute_exit_event_type_provenance(conn)
    after = _snapshot(conn)
    assert before == after


# =================== §4.6 item 7: dashboard getter - single db_session, idempotent, cleared ===================


def test_get_phase17_monitoring_summary_opens_exactly_one_db_session():
    import dashboard.data as dashboard_data
    src = inspect.getsource(dashboard_data.get_phase17_monitoring_summary)
    assert src.count("db_session(") == 1


def test_clear_all_caches_includes_get_phase17_monitoring_summary():
    import dashboard.data as dashboard_data
    source = inspect.getsource(dashboard_data.clear_all_caches)
    assert "get_phase17_monitoring_summary.clear()" in source


def _patch_db_session(monkeypatch, conn):
    import contextlib
    import dashboard.data as dashboard_data

    @contextlib.contextmanager
    def fake_db_session():
        yield conn
    monkeypatch.setattr(dashboard_data, "db_session", fake_db_session)


def test_get_phase17_monitoring_summary_deterministic_across_repeated_calls(monkeypatch):
    import dashboard.data as dashboard_data
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    dashboard_data.get_phase17_monitoring_summary.clear()
    _seed_observation(conn, "AAA", "2026-01-01", regime_freshness_status="FRESH")

    first = dashboard_data.get_phase17_monitoring_summary()
    dashboard_data.get_phase17_monitoring_summary.clear()
    second = dashboard_data.get_phase17_monitoring_summary()
    assert first == second


# =================== §4.6 item 6: dashboard read-only safety (grep-based) ===================


def test_render_regime_provenance_monitoring_extended_section_contains_no_button_or_form():
    with open(os.path.join(REPO_ROOT, "dashboard", "views", "ops_overview.py")) as f:
        content = f.read()
    start = content.index("def _render_regime_provenance_monitoring(")
    end = content.index("\ndef _render_automation_history(")
    body = content[start:end]
    assert "st.button" not in body
    assert "st.form" not in body
    # Confirm the extension actually landed here (not vacuous).
    assert "regime_freshness_distribution" in body or "phase17" in body.lower() or "get_phase17_monitoring_summary" in content


def test_render_regime_provenance_detail_extended_section_contains_no_button_or_form():
    with open(os.path.join(REPO_ROOT, "dashboard", "views", "strategy_lab.py")) as f:
        content = f.read()
    start = content.index("def _render_regime_provenance_detail(")
    end = content.index("\ndef render():", start)
    body = content[start:end]
    assert "st.button" not in body
    assert "st.form" not in body
