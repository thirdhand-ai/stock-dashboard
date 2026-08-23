"""Tests for trading/real_holdings.py's analytics: market value, unrealized
P&L, % of portfolio, and the needs_manual_entry no-fabrication contract.
Against a throwaway in-memory SQLite database with hand-inserted price
rows, same pattern tests/test_price_alerts.py uses."""
import sqlite3

from db.real_holdings_repository import upsert_real_holding
from db.schema import init_db
from trading.real_holdings import build_real_holdings_view, portfolio_totals, subtotals_by_owner


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_price_row(conn, ticker, date, close, source="yfinance"):
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, date, close, close, close, close, 1_000_000, source),
    )
    conn.commit()


def test_market_value_and_unrealized_pl_computed_from_stored_price():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2026-08-14", 120.0)
    upsert_real_holding(conn, "AAA", shares=100.0, cost_basis_total=10000.0)  # $100/share cost basis

    views = build_real_holdings_view(conn)
    v = views[0]

    assert v.current_price == 120.0
    assert v.market_value == 12000.0
    assert v.unrealized_pl == 2000.0
    assert v.unrealized_pl_pct == 20.0
    assert v.cost_basis_per_share == 100.0


def test_needs_manual_entry_position_has_no_derived_numbers():
    conn = make_test_db()
    insert_price_row(conn, "STN", "2026-08-14", 45.0)  # price IS known even though shares/cost basis aren't
    upsert_real_holding(conn, "STN", needs_manual_entry=True, note="awarded shares")

    v = build_real_holdings_view(conn)[0]

    assert v.current_price == 45.0  # price still shown - it's independent of quantity
    assert v.shares is None
    assert v.market_value is None
    assert v.unrealized_pl is None
    assert v.unrealized_pl_pct is None
    assert v.weight_pct is None


def test_missing_price_history_gives_none_not_zero():
    conn = make_test_db()
    upsert_real_holding(conn, "GHOST", shares=10.0, cost_basis_total=1000.0)

    v = build_real_holdings_view(conn)[0]

    assert v.current_price is None
    assert v.market_value is None


def test_weight_pct_reflects_share_of_total_market_value():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2026-08-14", 100.0)
    insert_price_row(conn, "BBB", "2026-08-14", 100.0)
    upsert_real_holding(conn, "AAA", shares=75.0, cost_basis_total=5000.0)   # $7,500 mkt value
    upsert_real_holding(conn, "BBB", shares=25.0, cost_basis_total=2000.0)   # $2,500 mkt value

    views = {v.ticker: v for v in build_real_holdings_view(conn)}

    assert views["AAA"].weight_pct == 75.0
    assert views["BBB"].weight_pct == 25.0


def test_weight_pct_excludes_positions_missing_market_value():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2026-08-14", 100.0)
    upsert_real_holding(conn, "AAA", shares=100.0, cost_basis_total=8000.0)
    upsert_real_holding(conn, "STN", needs_manual_entry=True)  # no price row, no shares

    views = {v.ticker: v for v in build_real_holdings_view(conn)}

    assert views["AAA"].weight_pct == 100.0  # sole contributor to the total
    assert views["STN"].weight_pct is None


def test_realized_gain_from_a_partial_sale_is_tracked_independently_of_unrealized():
    """META: 150 @ $343, then 75 sold @ $666 - remaining 75 shares still
    carry the original $343/share cost basis; realized gain is a separate,
    directly-entered figure from the sale, not derived here."""
    conn = make_test_db()
    insert_price_row(conn, "META", "2026-08-14", 589.85)
    upsert_real_holding(conn, "META", shares=75.0, cost_basis_total=75 * 343.0, realized_gain=(666.0 - 343.0) * 75)

    v = build_real_holdings_view(conn)[0]

    assert v.realized_gain == 24225.0
    assert v.cost_basis_total == 25725.0
    assert v.unrealized_pl == (589.85 * 75) - 25725.0


def test_portfolio_totals_sums_across_positions_and_counts_manual_entry():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2026-08-14", 100.0)
    upsert_real_holding(conn, "AAA", shares=100.0, cost_basis_total=8000.0, realized_gain=500.0)
    upsert_real_holding(conn, "STN", needs_manual_entry=True)

    totals = portfolio_totals(build_real_holdings_view(conn))

    assert totals["total_market_value"] == 10000.0
    assert totals["total_cost_basis"] == 8000.0
    assert totals["total_unrealized_pl"] == 2000.0
    assert totals["total_realized_gain"] == 500.0
    assert totals["positions_needing_manual_entry"] == 1


def test_subtotals_by_owner_groups_and_sums_independently_per_owner():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2026-08-14", 100.0)
    insert_price_row(conn, "BBB", "2026-08-14", 50.0)
    upsert_real_holding(conn, "AAA", owner="Mom", shares=100.0, cost_basis_total=8000.0)
    upsert_real_holding(conn, "BBB", owner="Tyler", shares=10.0, cost_basis_total=400.0)

    subtotals = subtotals_by_owner(build_real_holdings_view(conn))

    assert set(subtotals) == {"Mom", "Tyler"}
    assert subtotals["Mom"]["total_market_value"] == 10000.0
    assert subtotals["Mom"]["total_unrealized_pl"] == 2000.0
    assert subtotals["Tyler"]["total_market_value"] == 500.0
    assert subtotals["Tyler"]["total_unrealized_pl"] == 100.0


def test_subtotals_by_owner_sum_to_the_combined_total():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2026-08-14", 100.0)
    insert_price_row(conn, "BBB", "2026-08-14", 50.0)
    upsert_real_holding(conn, "AAA", owner="Mom", shares=100.0, cost_basis_total=8000.0)
    upsert_real_holding(conn, "BBB", owner="Tyler", shares=10.0, cost_basis_total=400.0)

    views = build_real_holdings_view(conn)
    combined = portfolio_totals(views)
    subtotals = subtotals_by_owner(views)

    summed_market_value = sum(s["total_market_value"] for s in subtotals.values())
    assert summed_market_value == combined["total_market_value"]


def test_subtotals_by_owner_groups_a_multi_owner_ticker_separately():
    """Same ticker (NOW), two owners - subtotals_by_owner must keep them in
    separate buckets, not merge them because the ticker matches."""
    conn = make_test_db()
    insert_price_row(conn, "NOW", "2026-08-14", 128.48)
    upsert_real_holding(conn, "NOW", owner="Mom", shares=150.0, cost_basis_total=15300.0)
    upsert_real_holding(conn, "NOW", owner="Tyler", needs_manual_entry=True)

    subtotals = subtotals_by_owner(build_real_holdings_view(conn))

    assert subtotals["Mom"]["total_market_value"] == 128.48 * 150.0
    assert subtotals["Tyler"]["total_market_value"] == 0.0
    assert subtotals["Tyler"]["positions_needing_manual_entry"] == 1
