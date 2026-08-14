"""Tests for Phase 12 Component C: ops/reconciliation.py.

All Alpaca access is a MagicMock/SimpleNamespace fixture - no real network
call, no real Discord webhook. Covers the MATCHED/WARNING/MISMATCH state
matrix from spec docs/specs/phase12.md §10.3, including the explicit
"never calls a mutating Alpaca endpoint" and "never imports the mutating
trading.reconcile helpers" structural checks.
"""
import ast
import os
import sqlite3
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from alpaca.common.enums import BaseURL

from db.schema import init_db
from ops.reconciliation import (
    STATUS_MATCHED,
    STATUS_MISMATCH,
    STATUS_WARNING,
    build_reconciliation_report,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_paper_order(conn, ticker, intent, status, qty=None, filled_qty=None,
                        filled_avg_price=None, side="buy"):
    conn.execute(
        """
        INSERT INTO paper_orders
            (ticker, side, intent, qty, reason, client_order_id, status, filled_qty, filled_avg_price, filled_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (ticker, side, intent, qty, "test fixture", str(uuid.uuid4()), status, filled_qty, filled_avg_price,
         "2024-01-01T00:00:00" if status == "filled" else None),
    )
    conn.commit()


def make_position(symbol, qty, avg_entry_price):
    return SimpleNamespace(symbol=symbol, qty=qty, avg_entry_price=avg_entry_price)


def make_open_order(symbol):
    return SimpleNamespace(symbol=symbol)


def make_paper_client(positions=None, open_orders=None, account_number="PA123456",
                       account_status="ACTIVE", account_raises=None, base_url=BaseURL.TRADING_PAPER):
    client = MagicMock()
    client._base_url = base_url
    if account_raises is not None:
        client.get_account.side_effect = account_raises
    else:
        status_obj = SimpleNamespace(value=account_status)
        client.get_account.return_value = SimpleNamespace(account_number=account_number, status=status_obj)
    client.get_all_positions.return_value = positions or []
    client.get_orders.return_value = open_orders or []
    return client


# --- MATCHED / MISMATCH / WARNING matrix ---

def test_matched_when_local_and_alpaca_positions_agree():
    conn = make_test_db()
    insert_paper_order(conn, "TICK", "entry", "filled", filled_qty=10, filled_avg_price=100.0)
    client = make_paper_client(positions=[make_position("TICK", 10, 100.0)])
    report = build_reconciliation_report(conn, client=client)
    row = next(r for r in report.tickers if r.ticker == "TICK")
    assert row.status == STATUS_MATCHED
    assert report.overall_status == STATUS_MATCHED
    assert report.alpaca_unreachable is False


def test_mismatch_when_local_position_missing_from_alpaca():
    conn = make_test_db()
    insert_paper_order(conn, "TICK", "entry", "filled", filled_qty=10, filled_avg_price=100.0)
    client = make_paper_client(positions=[])
    report = build_reconciliation_report(conn, client=client)
    row = next(r for r in report.tickers if r.ticker == "TICK")
    assert row.status == STATUS_MISMATCH
    assert any("no matching Alpaca position" in r for r in row.reasons)
    assert report.overall_status == STATUS_MISMATCH


def test_mismatch_when_alpaca_position_missing_locally():
    conn = make_test_db()
    client = make_paper_client(positions=[make_position("TICK", 5, 50.0)])
    report = build_reconciliation_report(conn, client=client)
    row = next(r for r in report.tickers if r.ticker == "TICK")
    assert row.status == STATUS_MISMATCH
    assert any("no local order history" in r for r in row.reasons)


def test_mismatch_on_qty_divergence_beyond_tolerance():
    conn = make_test_db()
    insert_paper_order(conn, "TICK", "entry", "filled", filled_qty=10, filled_avg_price=100.0)
    client = make_paper_client(positions=[make_position("TICK", 10.5, 100.0)])
    report = build_reconciliation_report(conn, client=client)
    row = next(r for r in report.tickers if r.ticker == "TICK")
    assert row.status == STATUS_MISMATCH
    assert any("quantity divergence" in r for r in row.reasons)


def test_qty_divergence_within_tolerance_is_matched():
    """Fractional-share tolerance (0.0001) must not exact-float-equality
    compare - a tiny rounding difference must not falsely MISMATCH."""
    conn = make_test_db()
    insert_paper_order(conn, "TICK", "entry", "filled", filled_qty=10.00001, filled_avg_price=100.0)
    client = make_paper_client(positions=[make_position("TICK", 10.00002, 100.0)])
    report = build_reconciliation_report(conn, client=client)
    row = next(r for r in report.tickers if r.ticker == "TICK")
    assert row.status == STATUS_MATCHED


def test_warning_on_open_order_count_divergence():
    conn = make_test_db()
    insert_paper_order(conn, "TICK", "entry", "filled", filled_qty=10, filled_avg_price=100.0)
    insert_paper_order(conn, "TICK", "exit", "submitted", qty=10)  # locally non-terminal
    client = make_paper_client(positions=[make_position("TICK", 10, 100.0)], open_orders=[])
    report = build_reconciliation_report(conn, client=client)
    row = next(r for r in report.tickers if r.ticker == "TICK")
    assert row.status == STATUS_WARNING
    assert row.local_open_order_count == 1
    assert row.alpaca_open_order_count == 0
    assert any("open order count divergence" in r for r in row.reasons)


def test_warning_on_avg_price_divergence_beyond_tolerance():
    conn = make_test_db()
    insert_paper_order(conn, "TICK", "entry", "filled", filled_qty=10, filled_avg_price=100.0)
    client = make_paper_client(positions=[make_position("TICK", 10, 103.5)])  # 3.5% > 2% tolerance
    report = build_reconciliation_report(conn, client=client)
    row = next(r for r in report.tickers if r.ticker == "TICK")
    assert row.status == STATUS_WARNING
    assert any("avg price divergence" in r for r in row.reasons)


def test_alpaca_unreachable_yields_mismatch_not_silent_matched():
    conn = make_test_db()
    client = make_paper_client(account_raises=ConnectionError("simulated rate limit / network error"))
    report = build_reconciliation_report(conn, client=client)
    assert report.alpaca_unreachable is True
    assert report.alpaca_error is not None
    assert report.overall_status == STATUS_MISMATCH
    assert report.tickers == []
    assert report.amzn is None


def test_amzn_row_always_present_when_amzn_has_any_local_or_alpaca_state():
    conn = make_test_db()
    insert_paper_order(conn, "AMZN", "entry", "filled", filled_qty=3, filled_avg_price=150.0)
    client = make_paper_client(positions=[make_position("AMZN", 3, 150.0)])
    report = build_reconciliation_report(conn, client=client)
    assert report.amzn is not None
    assert report.amzn.ticker == "AMZN"
    assert report.amzn.status == STATUS_MATCHED


def test_amzn_is_none_when_amzn_has_no_state_at_all():
    conn = make_test_db()
    client = make_paper_client(positions=[])
    report = build_reconciliation_report(conn, client=client)
    assert report.amzn is None


# --- structural safety ---

def test_reconciliation_never_calls_submit_cancel_replace_close():
    conn = make_test_db()
    insert_paper_order(conn, "TICK", "entry", "filled", filled_qty=10, filled_avg_price=100.0)
    client = make_paper_client(positions=[make_position("TICK", 10, 100.0)])
    build_reconciliation_report(conn, client=client)
    client.submit_order.assert_not_called()
    client.cancel_order_by_id.assert_not_called()
    client.replace_order_by_id.assert_not_called()
    client.close_position.assert_not_called()
    client.close_all_positions.assert_not_called()


def _module_level_import_names(file_path):
    with open(file_path) as f:
        tree = ast.parse(f.read(), filename=file_path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
            names.add(node.module)
    return names


def test_reconciliation_never_calls_trading_reconcile_module():
    path = os.path.join(REPO_ROOT, "ops", "reconciliation.py")
    imports = _module_level_import_names(path)
    forbidden = {
        i for i in imports
        if i in ("trading.reconcile.reconcile_order", "trading.reconcile.reconcile_open_orders")
    }
    assert not forbidden, f"ops/reconciliation.py must never import the mutating reconcile helpers: {forbidden}"
