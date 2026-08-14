"""Structural/safety-boundary tests for the Phase 12 ops/ package (spec
docs/specs/phase12.md §1 hard constraints, §10.6 checklist). Mirrors
tests/test_strategy_lab.py's AST-scan / source-text-scan conventions. Own
file since ops/ is a new package, not appended to test_strategy_lab.py.

No test in this file makes a real network call or sends a real Discord
notification.
"""
import ast
import contextlib
import os
import sqlite3
import sys
from datetime import date
from unittest.mock import MagicMock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_test_db():
    from db.schema import init_db
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _module_level_import_names(file_path):
    with open(file_path) as f:
        tree = ast.parse(f.read(), filename=file_path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _py_files(dir_path):
    return [f for f in os.listdir(dir_path) if f.endswith(".py")]


# --- import-direction isolation ---

def test_ops_never_imports_order_execution_or_alerting():
    ops_dir = os.path.join(REPO_ROOT, "ops")
    forbidden_prefixes = (
        "trading.engine", "trading.orders", "trading.run_paper",
        "alerts.discord", "alerts.runner", "alerts.run_alerts",
    )
    offenders = {}
    for filename in _py_files(ops_dir):
        imports = _module_level_import_names(os.path.join(ops_dir, filename))
        forbidden = {
            i for i in imports
            if i in forbidden_prefixes or any(i.startswith(p + ".") for p in forbidden_prefixes)
        }
        if forbidden:
            offenders[filename] = forbidden
    assert not offenders, f"ops/*.py must never import order-execution/alerting modules: {offenders}"


def test_trading_automation_alerts_never_import_ops():
    """Reverse-direction isolation: production trading/automation/alerts
    must never depend on the reporting/governance-only ops package."""
    offenders = {}
    for pkg in ("trading", "automation", "alerts"):
        pkg_dir = os.path.join(REPO_ROOT, pkg)
        for filename in _py_files(pkg_dir):
            imports = _module_level_import_names(os.path.join(pkg_dir, filename))
            forbidden = {i for i in imports if i == "ops" or i.startswith("ops.")}
            if forbidden:
                offenders[f"{pkg}/{filename}"] = forbidden
    assert not offenders, f"production packages must never import ops: {offenders}"


# --- Alpaca mutating-endpoint isolation ---

def test_ops_never_calls_alpaca_mutating_endpoints():
    ops_dir = os.path.join(REPO_ROOT, "ops")
    forbidden_substrings = (
        "submit_order(", "cancel_order_by_id(", "replace_order_by_id(",
        "close_position(", "close_all_positions(",
    )
    offenders = {}
    for filename in _py_files(ops_dir):
        source = open(os.path.join(ops_dir, filename)).read()
        found = [s for s in forbidden_substrings if s in source]
        if found:
            offenders[filename] = found
    assert not offenders, f"ops/*.py must never call Alpaca mutating endpoints: {offenders}"


# --- write-isolation from trading/alerting/research tables ---

def test_ops_never_writes_alert_state_or_paper_orders_or_prospective_tables(monkeypatch):
    conn = make_test_db()

    from strategy_lab.outcome_maturation import ensure_schema as ensure_outcomes_schema
    from strategy_lab.prospective import ensure_schema as ensure_prospective_schema
    from strategy_lab.prospective_events import ensure_schema as ensure_events_schema
    from strategy_lab.research_automation import ensure_schema as ensure_research_run_schema
    for fn in (ensure_prospective_schema, ensure_events_schema, ensure_outcomes_schema, ensure_research_run_schema):
        fn(conn)

    tables = [
        "alert_state", "alerts", "paper_orders",
        "research_prospective_observations", "research_prospective_events", "research_prospective_outcomes",
        "automation_runs", "research_run_history",
    ]
    before = {t: conn.execute(f"SELECT COUNT(*) as n FROM {t}").fetchone()["n"] for t in tables}

    def _raise_get_client(*_a, **_kw):
        raise RuntimeError("no test Alpaca credentials configured")
    monkeypatch.setattr("trading.client.get_client", _raise_get_client)

    from ops.daily_report import build_daily_report
    from ops.data_quality import check_watchlist_quality
    from ops.reconciliation import build_reconciliation_report

    build_daily_report(conn, today=date(2024, 6, 20))
    check_watchlist_quality(conn, tickers=["AAPL"], today=date(2024, 6, 20))

    unreachable_client = MagicMock()
    unreachable_client._base_url = None  # fails verify_paper_environment -> alpaca_unreachable path
    build_reconciliation_report(conn, client=unreachable_client)

    after = {t: conn.execute(f"SELECT COUNT(*) as n FROM {t}").fetchone()["n"] for t in tables}
    assert before == after, f"ops/* must never write to trading/alerting/prospective tables: before={before} after={after}"


# --- discord summary isolation ---

def test_discord_summary_module_never_imports_requests_or_webhook_config():
    """AST-scan (not a raw string-search) so that discord_summary.py's own
    docstring is free to explain this isolation guarantee in prose (as it
    does) without that explanation itself tripping the check - what must
    never appear is an actual identifier reference (import, attribute
    access, or assignment target) to DISCORD_WEBHOOK_URL/requests."""
    path = os.path.join(REPO_ROOT, "ops", "discord_summary.py")
    imports = _module_level_import_names(path)
    forbidden_imports = {
        i for i in imports
        if i == "requests" or i.startswith("requests.") or i == "config.settings" or i.startswith("config.settings.")
    }
    assert not forbidden_imports, f"ops/discord_summary.py must never import requests/config.settings: {forbidden_imports}"

    with open(path) as f:
        tree = ast.parse(f.read(), filename=path)
    referenced_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced_names.add(node.attr)
    assert "DISCORD_WEBHOOK_URL" not in referenced_names
    assert "requests" not in referenced_names


def test_generate_daily_summary_cli_never_calls_network(monkeypatch):
    conn = make_test_db()

    @contextlib.contextmanager
    def fake_db_session():
        yield conn

    import ops.generate_daily_summary as gen_module
    monkeypatch.setattr(gen_module, "db_session", fake_db_session)

    def _raise_get_client(*_a, **_kw):
        raise RuntimeError("no test Alpaca credentials configured")
    monkeypatch.setattr("trading.client.get_client", _raise_get_client)

    import requests
    post_mock = MagicMock()
    monkeypatch.setattr(requests, "post", post_mock)
    monkeypatch.setattr(sys, "argv", ["ops.generate_daily_summary"])

    gen_module.main()  # must never raise, must never POST

    post_mock.assert_not_called()


# --- no scheduler / LaunchAgent / deploy activation ---

def test_no_ops_module_writes_or_edits_launchagent_or_deploy_files():
    ops_dir = os.path.join(REPO_ROOT, "ops")
    offenders = []
    for filename in _py_files(ops_dir):
        source = open(os.path.join(ops_dir, filename)).read()
        if "launchctl" in source.lower() or ".plist" in source.lower():
            offenders.append(filename)
    assert not offenders, f"unexpected LaunchAgent/deploy reference in ops/: {offenders}"


# --- no write access to guarded config files ---

def test_no_ops_module_imports_or_edits_signals_backtest_trading_alerts_config():
    """Read-only imports of signals.config/backtest.config/trading.config/
    alerts.config ARE expected (ops/experiment_registry.py reuses
    strategy_lab.production_guard.compute_fingerprint(), which hashes them
    by reading their contents) - this test asserts no ops/*.py module ever
    opens a file for writing/appending at all (no component in this phase
    should write to any file on disk, only to the new ops_*/
    experiment_registry* sqlite tables), which is a strictly stronger and
    simpler guarantee than "never edits those 4 files specifically"."""
    ops_dir = os.path.join(REPO_ROOT, "ops")
    offenders = {}
    for filename in _py_files(ops_dir):
        source = open(os.path.join(ops_dir, filename)).read()
        write_constructs = [
            c for c in ("open(", ".write(", ".write_text(", ".write_bytes(")
            if c in source
        ]
        if write_constructs:
            offenders[filename] = write_constructs
    assert not offenders, f"ops/*.py must never write to any file on disk: {offenders}"
