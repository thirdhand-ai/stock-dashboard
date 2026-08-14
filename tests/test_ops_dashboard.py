"""Tests for the Phase 12 dashboard/data.py ops getters (get_ops_daily_report,
get_data_quality_report, get_reconciliation_report, get_experiment_registry,
get_experiment_registry_drift, get_prospective_evidence_status,
get_ops_research_run_history).

These are `st.cache_data`-wrapped thin callers with no logic of their own
(the logic lives in ops/*.py, already covered by the other test_ops_*.py
files). Following tests/test_dashboard_data.py's convention of exercising
the data layer directly against a synthetic in-memory SQLite DB (no real
Streamlit runtime, no network) - here that means monkeypatching
dashboard.data.db_session to yield our throwaway connection and clearing
each getter's cache before every call so tests don't leak state into each
other via the process-lifetime st.cache_data cache.
"""
import contextlib
import sqlite3

import pandas as pd

import dashboard.data as dashboard_data
from db.schema import init_db


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _patch_db_session(monkeypatch, conn):
    @contextlib.contextmanager
    def fake_db_session():
        yield conn
    monkeypatch.setattr(dashboard_data, "db_session", fake_db_session)


def _raise_get_client(*_a, **_kw):
    raise RuntimeError("no test Alpaca credentials configured")


def test_get_ops_daily_report_returns_expected_sections(monkeypatch):
    conn = make_test_db()
    monkeypatch.setattr("trading.client.get_client", _raise_get_client)
    _patch_db_session(monkeypatch, conn)
    dashboard_data.get_ops_daily_report.clear()

    result = dashboard_data.get_ops_daily_report()

    assert isinstance(result, dict)
    for key in ("report_date", "generated_at", "production_health", "ticker_signals",
                "paper_portfolio", "research_job", "market_regime"):
        assert key in result
    assert result["paper_portfolio"]["ok"] is False  # Alpaca unreachable in this fixture


def test_get_ops_daily_report_never_persists_a_row(monkeypatch):
    """Spec §7.4 / §3.2: a page load must never write to ops_daily_reports -
    only the CLI's explicit upsert_report call does that."""
    conn = make_test_db()
    monkeypatch.setattr("trading.client.get_client", _raise_get_client)
    _patch_db_session(monkeypatch, conn)
    dashboard_data.get_ops_daily_report.clear()

    dashboard_data.get_ops_daily_report()

    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ops_daily_reports'"
    ).fetchall()
    if tables:
        count = conn.execute("SELECT COUNT(*) as n FROM ops_daily_reports").fetchone()["n"]
        assert count == 0


def test_get_data_quality_report_returns_dict_shape(monkeypatch):
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    dashboard_data.get_data_quality_report.clear()

    result = dashboard_data.get_data_quality_report()

    assert "overall_status" in result
    assert "tickers" in result
    assert result["overall_status"] == "FAILED"  # empty prices table -> every watchlist ticker MISSING


def test_get_reconciliation_report_handles_alpaca_down(monkeypatch):
    conn = make_test_db()
    monkeypatch.setattr("trading.client.get_client", _raise_get_client)
    _patch_db_session(monkeypatch, conn)
    dashboard_data.get_reconciliation_report.clear()

    result = dashboard_data.get_reconciliation_report()

    assert result["alpaca_unreachable"] is True
    assert result["overall_status"] == "MISMATCH"


def test_get_experiment_registry_returns_dataframe(monkeypatch):
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    dashboard_data.get_experiment_registry.clear()

    df = dashboard_data.get_experiment_registry()

    assert isinstance(df, pd.DataFrame)
    assert df.empty  # nothing registered yet in this fixture


def test_get_experiment_registry_drift_returns_dict(monkeypatch):
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    dashboard_data.get_experiment_registry_drift.clear()

    result = dashboard_data.get_experiment_registry_drift()

    assert result == {}  # no ACTIVE experiments registered


def test_get_prospective_evidence_status_returns_expected_shape(monkeypatch):
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    dashboard_data.get_prospective_evidence_status.clear()

    result = dashboard_data.get_prospective_evidence_status()

    assert result == {"n_prospective_trading_days": 0, "status": "INSUFFICIENT_DATA"}


def test_get_ops_research_run_history_returns_dataframe(monkeypatch):
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    dashboard_data.get_ops_research_run_history.clear()

    df = dashboard_data.get_ops_research_run_history()

    assert isinstance(df, pd.DataFrame)
    assert df.empty


def test_clear_all_caches_includes_all_seven_ops_getters():
    import inspect
    source = inspect.getsource(dashboard_data.clear_all_caches)
    for name in (
        "get_ops_daily_report", "get_data_quality_report", "get_reconciliation_report",
        "get_experiment_registry", "get_experiment_registry_drift",
        "get_prospective_evidence_status", "get_ops_research_run_history",
    ):
        assert f"{name}.clear()" in source, f"clear_all_caches() is missing {name}.clear()"
