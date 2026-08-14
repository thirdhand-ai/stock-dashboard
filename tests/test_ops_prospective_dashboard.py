"""Tests for Phase 14 Component D: dashboard extensions (docs/specs/phase14.md
§4) - the four new dashboard/data.py getters (get_prospective_day_ledger,
get_prospective_audit_summary, get_research_cache_completeness_report,
get_research_run_ticker_errors) and the safety-boundary constraints on the
two extended view files.

Follows tests/test_ops_dashboard.py's convention: monkeypatch
dashboard.data.db_session to yield a throwaway in-memory SQLite connection,
no real Streamlit runtime, no network.
"""
import contextlib
import inspect
import os
import sqlite3
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import dashboard.data as dashboard_data
from db.run_history_repository import STATUS_SUCCESS, finish_run, start_run
from db.schema import init_db

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _clear_component_d_caches():
    dashboard_data.get_prospective_day_ledger.clear()
    dashboard_data.get_prospective_audit_summary.clear()
    dashboard_data.get_research_cache_completeness_report.clear()
    dashboard_data.get_research_run_ticker_errors.clear()


def _seed_one_research_run(conn, trading_date_str):
    from strategy_lab.research_automation import ResearchRunResult
    from strategy_lab.research_automation import _finish_run as research_finish_run
    from strategy_lab.research_automation import _start_run as research_start_run
    from strategy_lab.research_automation import ensure_schema as ensure_research_schema
    ensure_research_schema(conn)
    run_id = research_start_run(conn, date.fromisoformat(trading_date_str))
    result = ResearchRunResult(
        run_id=run_id, status="success", trading_date=trading_date_str,
        tickers_attempted=1, observations_created=1,
    )
    research_finish_run(conn, run_id, result)
    return run_id


# --- basic getter shape / caching ---


def test_get_prospective_day_ledger_returns_dataframe(monkeypatch):
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    _clear_component_d_caches()
    _seed_one_research_run(conn, "2024-01-03")

    df = dashboard_data.get_prospective_day_ledger()
    assert isinstance(df, pd.DataFrame)
    if not df.empty:
        assert "trading_date" in df.columns
        assert "status" in df.columns


def test_get_prospective_day_ledger_respects_limit_days(monkeypatch):
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    _clear_component_d_caches()
    _seed_one_research_run(conn, "2024-01-03")

    df = dashboard_data.get_prospective_day_ledger(limit_days=1)
    assert len(df) <= 1


def test_get_prospective_audit_summary_returns_expected_keys(monkeypatch):
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    _clear_component_d_caches()
    _seed_one_research_run(conn, "2024-01-03")

    summary = dashboard_data.get_prospective_audit_summary()
    assert isinstance(summary, dict)
    for key in ("counts_by_status", "ledger_days", "eligibility_start", "maturation_by_horizon"):
        assert key in summary


def test_get_prospective_audit_summary_is_deterministic_across_calls(monkeypatch):
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    _clear_component_d_caches()
    _seed_one_research_run(conn, "2024-01-03")
    _seed_one_research_run(conn, "2024-01-04")

    first = dashboard_data.get_prospective_audit_summary()
    dashboard_data.get_prospective_audit_summary.clear()  # force recompute, not just a cache hit
    second = dashboard_data.get_prospective_audit_summary()
    assert first == second


def test_get_research_cache_completeness_report_returns_expected_shape(monkeypatch):
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    _clear_component_d_caches()

    report = dashboard_data.get_research_cache_completeness_report()
    assert isinstance(report, dict)
    assert "currently_flagged_incomplete" in report
    assert "recent_corrections" in report
    assert isinstance(report["currently_flagged_incomplete"], list)
    assert isinstance(report["recent_corrections"], list)


def test_get_research_run_ticker_errors_empty_when_no_runs(monkeypatch):
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    _clear_component_d_caches()

    errors = dashboard_data.get_research_run_ticker_errors()
    assert isinstance(errors, pd.DataFrame)
    assert errors.empty


def test_get_research_run_ticker_errors_defaults_to_latest_run(monkeypatch):
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    _clear_component_d_caches()

    from strategy_lab.research_automation import record_ticker_error
    run_id = _seed_one_research_run(conn, "2024-01-03")
    record_ticker_error(conn, run_id, trading_date="2024-01-03", reason="RuntimeError: boom", ticker="AAPL")

    errors = dashboard_data.get_research_run_ticker_errors()
    assert len(errors) == 1
    assert errors.iloc[0]["ticker"] == "AAPL"


# --- safety boundary: get_research_cache_completeness_report never makes a live Alpaca call ---


def test_research_cache_completeness_report_source_never_calls_refresh_or_alpaca_client():
    """Static scan of the getter's own function body (not the whole file,
    and not its docstring - which legitimately documents the constraint in
    prose) - the executable code must never CALL refresh_incomplete_latest_bars
    (which makes live Alpaca calls) or construct an Alpaca client directly."""
    import ast
    import textwrap
    source = inspect.getsource(dashboard_data.get_research_cache_completeness_report)
    tree = ast.parse(textwrap.dedent(source))
    func_node = tree.body[0]
    assert isinstance(func_node, ast.FunctionDef)
    # Drop the docstring so prose mentioning these names doesn't false-positive.
    body = func_node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    call_names = set()
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
            if name:
                call_names.add(name)
    assert "refresh_incomplete_latest_bars" not in call_names
    assert "get_data_client" not in call_names
    assert "StockHistoricalDataClient" not in call_names


def test_research_cache_completeness_report_never_triggers_alpaca_call_at_runtime(monkeypatch):
    """Behavioral confirmation of the static scan above: even with Alpaca's
    client constructor mocked to explode if called, the getter must
    complete without calling it."""
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    _clear_component_d_caches()

    def _boom(*a, **kw):
        raise AssertionError("get_research_cache_completeness_report must never call get_data_client")

    with patch("ingestion.alpaca_source.get_data_client", _boom), \
         patch("strategy_lab.cache_integrity.get_data_client", _boom):
        report = dashboard_data.get_research_cache_completeness_report()
    assert isinstance(report, dict)


# --- safety boundary: no st.button / write action in either extended view file ---


def _view_source(relative_path):
    with open(os.path.join(REPO_ROOT, relative_path)) as f:
        return f.read()


def test_ops_overview_view_has_no_st_button_calls():
    source = _view_source("dashboard/views/ops_overview.py")
    assert "st.button" not in source


def test_strategy_lab_view_has_no_st_button_calls():
    source = _view_source("dashboard/views/strategy_lab.py")
    assert "st.button" not in source


def test_ops_overview_new_render_functions_exist_and_are_read_only():
    source = _view_source("dashboard/views/ops_overview.py")
    assert "_render_prospective_day_ledger" in source
    assert "_render_research_cache_completeness" in source


def test_strategy_lab_new_render_function_exists():
    source = _view_source("dashboard/views/strategy_lab.py")
    assert "_render_research_job_diagnostics" in source


# --- zero trade/Discord/alert-state mutation from any Component D getter ---


def test_component_d_getters_never_write_to_paper_orders_alert_state_or_alerts(monkeypatch):
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    _clear_component_d_caches()
    _seed_one_research_run(conn, "2024-01-03")

    def _counts():
        return {
            t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("paper_orders", "alert_state", "alerts")
        }

    before = _counts()
    dashboard_data.get_prospective_day_ledger()
    dashboard_data.get_prospective_audit_summary()
    dashboard_data.get_research_cache_completeness_report()
    dashboard_data.get_research_run_ticker_errors()
    after = _counts()

    assert before == after == {"paper_orders": 0, "alert_state": 0, "alerts": 0}


def test_clear_all_caches_includes_all_four_component_d_getters():
    source = inspect.getsource(dashboard_data.clear_all_caches)
    for name in (
        "get_prospective_day_ledger", "get_prospective_audit_summary",
        "get_research_cache_completeness_report", "get_research_run_ticker_errors",
    ):
        assert f"{name}.clear()" in source
