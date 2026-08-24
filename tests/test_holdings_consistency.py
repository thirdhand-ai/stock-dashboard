"""Tests for trading/holdings_consistency.py: it must flag a real_holdings
position whose lot/dividend ledger doesn't sum to its recorded shares or
cost_basis_total, stay silent for a position whose ledger agrees (within
tolerance) or has no ledger at all, and never fabricate a mismatch for
data it can't check. Against a throwaway in-memory SQLite database, same
pattern tests/test_data_completeness.py uses."""
import sqlite3

from db.dividend_payments_repository import add_dividend_payment
from db.real_holding_lots_repository import add_real_holding_lot
from db.real_holdings_repository import list_real_holdings, upsert_real_holding
from db.schema import init_db
from trading.holdings_consistency import check_all_holdings_consistency, check_holding_consistency


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_consistent_ticker_produces_no_mismatches():
    """A holding whose lot + reinvested-dividend ledger sums exactly to
    its recorded shares and cost basis - the KMI-shaped case this check
    exists to confirm passes."""
    conn = make_test_db()
    add_real_holding_lot(conn, "KMI", owner="Mom", purchase_date="2021-04-26", shares=96.0, cost_per_share=15.061, total_cost=1445.86)
    add_dividend_payment(conn, "KMI", owner="Mom", pay_date="2021-08-16", amount_per_share=15.156, total_received=30.31, reinvested=True)
    # 96 + (30.31 / 15.156) = 98.0 shares; 1445.86 + 30.31 = 1476.17 cost basis.
    upsert_real_holding(conn, "KMI", owner="Mom", shares=98.0, cost_basis_total=1476.17)

    holding = next(h for h in list_real_holdings(conn) if h.ticker == "KMI")
    mismatches = check_holding_consistency(conn, holding)

    assert mismatches == []


def test_deliberately_broken_ticker_is_flagged_for_both_fields():
    """A holding whose recorded shares/cost basis were NOT updated to
    match its lot + dividend ledger - both fields should mismatch."""
    conn = make_test_db()
    add_real_holding_lot(conn, "HPI", owner="Mom", purchase_date="2020-11-17", shares=1382.0, cost_per_share=17.906, total_cost=24746.09)
    add_dividend_payment(conn, "HPI", owner="Mom", pay_date="2021-07-30", amount_per_share=20.571, total_received=162.70, reinvested=True)
    # Ledger actually sums to ~1389.9 shares / ~24908.79 cost basis - nowhere near what's recorded below.
    upsert_real_holding(conn, "HPI", owner="Mom", shares=9999.0, cost_basis_total=50000.0)

    holding = next(h for h in list_real_holdings(conn) if h.ticker == "HPI")
    mismatches = check_holding_consistency(conn, holding)

    fields = {m.field for m in mismatches}
    assert fields == {"shares", "cost_basis_total"}
    shares_mismatch = next(m for m in mismatches if m.field == "shares")
    assert shares_mismatch.recorded == 9999.0
    assert abs(shares_mismatch.reconstructed - 1389.9138) < 0.01


def test_holding_with_no_lot_or_dividend_data_is_skipped_not_flagged():
    """A holding with only a manually-entered aggregate (no ledger at all,
    e.g. NVDA/MSFT/STN/NOW today) has nothing to reconcile against - it
    must be silently skipped, not treated as a mismatch."""
    conn = make_test_db()
    upsert_real_holding(conn, "NVDA", owner="Mom", shares=250.0, cost_basis_total=40050.0)

    holding = next(h for h in list_real_holdings(conn) if h.ticker == "NVDA")
    mismatches = check_holding_consistency(conn, holding)

    assert mismatches == []


def test_needs_manual_entry_holding_with_no_ledger_is_skipped():
    conn = make_test_db()
    upsert_real_holding(conn, "STN", owner="Dad", needs_manual_entry=True)

    holding = next(h for h in list_real_holdings(conn) if h.ticker == "STN")
    mismatches = check_holding_consistency(conn, holding)

    assert mismatches == []


def test_small_float_drift_within_tolerance_is_not_flagged():
    """The reconstructed share/cost totals from floating-point summation
    won't always land on the recorded value to the last bit - a tiny
    (sub-hundredth) drift must not be flagged as a mismatch."""
    conn = make_test_db()
    add_real_holding_lot(conn, "HPI", owner="Mom", purchase_date="2020-11-17", shares=1382.0, cost_per_share=17.906, total_cost=24746.09)
    upsert_real_holding(conn, "HPI", owner="Mom", shares=1382.005, cost_basis_total=24746.10)

    holding = next(h for h in list_real_holdings(conn) if h.ticker == "HPI")
    mismatches = check_holding_consistency(conn, holding)

    assert mismatches == []


def test_check_all_holdings_consistency_aggregates_across_positions():
    conn = make_test_db()
    upsert_real_holding(conn, "NVDA", owner="Mom", shares=250.0, cost_basis_total=40050.0)  # no ledger: skipped
    add_real_holding_lot(conn, "HPI", owner="Mom", purchase_date="2020-11-17", shares=1382.0, cost_per_share=17.906, total_cost=24746.09)
    upsert_real_holding(conn, "HPI", owner="Mom", shares=9999.0, cost_basis_total=50000.0)  # broken: flagged

    mismatches = check_all_holdings_consistency(conn)

    assert {m.ticker for m in mismatches} == {"HPI"}
