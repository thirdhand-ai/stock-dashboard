"""Structural/safety-boundary tests for Phase 13 (docs/specs/phase13.md §1
hard constraints, §8.4 checklist). Mirrors tests/test_ops_safety.py's
AST-scan / source-text-scan conventions, scoped to this phase's new/modified
files.

No test in this file makes a real network call, places an order, or sends a
real Discord notification.
"""
import ast
import os
import sqlite3
from datetime import date
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PHASE13_FILES = [
    os.path.join("ops", "provider_reconciliation.py"),
    os.path.join("ops", "data_quality.py"),
    os.path.join("ops", "run_data_quality_check.py"),
    os.path.join("dashboard", "views", "ops_overview.py"),
    os.path.join("automation", "pipeline.py"),
    os.path.join("automation", "recovery.py"),
    os.path.join("strategy_lab", "research_automation.py"),
    os.path.join("db", "schema.py"),
    os.path.join("db", "run_history_repository.py"),
]


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


# --- import-direction isolation, scoped to provider_reconciliation.py ---


def test_provider_reconciliation_module_never_imports_order_execution_or_alerting():
    path = os.path.join(REPO_ROOT, "ops", "provider_reconciliation.py")
    imports = _module_level_import_names(path)
    forbidden_prefixes = (
        "trading.engine", "trading.orders", "trading.run_paper",
        "alerts.discord", "alerts.runner", "alerts.run_alerts",
    )
    forbidden = {
        i for i in imports
        if i in forbidden_prefixes or any(i.startswith(p + ".") for p in forbidden_prefixes)
    }
    assert not forbidden, f"ops/provider_reconciliation.py must never import order-execution/alerting modules: {forbidden}"


def test_provider_reconciliation_never_calls_alpaca_mutating_endpoints():
    path = os.path.join(REPO_ROOT, "ops", "provider_reconciliation.py")
    source = open(path).read()
    forbidden_substrings = (
        "submit_order(", "cancel_order_by_id(", "replace_order_by_id(",
        "close_position(", "close_all_positions(",
    )
    found = [s for s in forbidden_substrings if s in source]
    assert not found, f"ops/provider_reconciliation.py must never call Alpaca mutating endpoints: {found}"


# --- write-isolation from trading/alerting/prospective tables ---


def test_phase13_changes_never_write_to_prices_alert_state_paper_orders_or_prospective_tables():
    conn = make_test_db()

    from strategy_lab.outcome_maturation import ensure_schema as ensure_outcomes_schema
    from strategy_lab.prospective import ensure_schema as ensure_prospective_schema
    from strategy_lab.prospective_events import ensure_schema as ensure_events_schema
    from strategy_lab.research_automation import ensure_schema as ensure_research_run_schema
    for fn in (ensure_prospective_schema, ensure_events_schema, ensure_outcomes_schema, ensure_research_run_schema):
        fn(conn)

    # Seed a little price history so the checks have something to read.
    for i, d in enumerate(["2024-06-17", "2024-06-18", "2024-06-19", "2024-06-20"]):
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            ("AAPL", d, 100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i, 1_000_000, "alpaca"),
        )
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            ("AAPL", d, 100.05 + i, 101.0 + i, 99.0 + i, 100.05 + i, 1_000_000, "alpaca_adjusted"),
        )
    conn.commit()

    protected_tables = [
        "prices", "alert_state", "alerts", "paper_orders",
        "research_prospective_observations", "research_prospective_events", "research_prospective_outcomes",
    ]
    before = {t: conn.execute(f"SELECT COUNT(*) as n FROM {t}").fetchone()["n"] for t in protected_tables}

    from ops.data_quality import check_watchlist_quality, record_source_provenance, ensure_provenance_schema
    from ops.provider_reconciliation import build_ticker_summary

    report = check_watchlist_quality(conn, tickers=["AAPL"], today=date(2024, 6, 20))
    build_ticker_summary(conn, "AAPL", today=date(2024, 6, 20))
    ensure_provenance_schema(conn)
    record_source_provenance(conn, check_id=1, tickers=["AAPL"])

    # run_pipeline with mocked ingest - automation_runs is allowed to grow,
    # but the protected tables above must not.
    from automation.pipeline import run_pipeline

    def fake_ingest(conn, ticker, days=5, **kwargs):
        raise ConnectionError("mocked ingest - never touches network or writes prices")

    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest):
        run_pipeline(conn, tickers=["AAPL"], today=date(2024, 6, 20), skip_non_trading_day_check=True)

    after = {t: conn.execute(f"SELECT COUNT(*) as n FROM {t}").fetchone()["n"] for t in protected_tables}
    assert before == after, f"Phase 13 code must never write to protected tables: before={before} after={after}"


# --- no credential logging ---


def test_no_credentials_logged_by_phase13_modules():
    forbidden_names = {"ALPACA_API_KEY", "ALPACA_SECRET_KEY", "DISCORD_WEBHOOK_URL"}
    offenders = {}
    for rel_path in [os.path.join("ops", "provider_reconciliation.py"), os.path.join("ops", "data_quality.py")]:
        path = os.path.join(REPO_ROOT, rel_path)
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
        referenced = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
        hit = referenced & forbidden_names
        if hit:
            offenders[rel_path] = hit
    assert not offenders, f"Phase 13 modules must never reference credential/webhook names: {offenders}"


# --- migration safety ---


def test_migration_add_trading_date_column_idempotent():
    from db.schema import _migrate_add_trading_date_to_automation_runs

    conn = make_test_db()  # init_db already ran the migration once
    cols_before = {row[1] for row in conn.execute("PRAGMA table_info(automation_runs)").fetchall()}
    assert "trading_date" in cols_before

    _migrate_add_trading_date_to_automation_runs(conn)  # calling again must not raise
    _migrate_add_trading_date_to_automation_runs(conn)

    cols_after = {row[1] for row in conn.execute("PRAGMA table_info(automation_runs)").fetchall()}
    assert cols_after == cols_before

    col_rows = [row[1] for row in conn.execute("PRAGMA table_info(automation_runs)").fetchall()]
    assert col_rows.count("trading_date") == 1  # never duplicated by repeated migration calls


def test_migration_never_alters_existing_automation_runs_rows():
    from db.schema import CREATE_AUTOMATION_RUNS_TABLE, _migrate_add_trading_date_to_automation_runs

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Simulate the PRE-migration table shape (no trading_date column at all).
    conn.execute(CREATE_AUTOMATION_RUNS_TABLE)
    conn.execute(
        "INSERT INTO automation_runs (started_at, finished_at, status, send_mode, tickers_attempted) "
        "VALUES ('2024-06-01 12:00:00', '2024-06-01 12:05:00', 'success', 'real', 3)"
    )
    conn.commit()

    _migrate_add_trading_date_to_automation_runs(conn)

    row = conn.execute("SELECT * FROM automation_runs").fetchone()
    assert row["started_at"] == "2024-06-01 12:00:00"
    assert row["finished_at"] == "2024-06-01 12:05:00"
    assert row["status"] == "success"
    assert row["send_mode"] == "real"
    assert row["tickers_attempted"] == 3
    assert row["trading_date"] is None


# --- no scheduler / LaunchAgent / deploy activation ---


def test_phase13_never_edits_deploy_or_launchagent_files():
    offenders = []
    for rel_path in PHASE13_FILES:
        path = os.path.join(REPO_ROOT, rel_path)
        if not os.path.exists(path):
            continue
        source = open(path).read()
        if "launchctl" in source.lower() or ".plist" in source.lower():
            offenders.append(rel_path)
    assert not offenders, f"unexpected LaunchAgent/deploy reference in Phase 13 files: {offenders}"
