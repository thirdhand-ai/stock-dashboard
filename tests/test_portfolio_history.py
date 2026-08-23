"""Tests for trading/portfolio_history.py's analytics: historical value
sum, exclusion/approximation flagging, normalization, and benchmark
alignment. Against a throwaway in-memory SQLite database with hand-
inserted price rows, same pattern tests/test_real_holdings.py uses."""
import sqlite3

from db.realized_sales_repository import add_realized_sale
from db.real_holdings_repository import list_real_holdings, upsert_real_holding
from db.schema import init_db
from trading.portfolio_history import (
    build_portfolio_value_series,
    build_portfolio_value_series_by_scope,
    to_series,
)


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


def test_prefers_alpaca_adjusted_source_over_a_split_discontinuous_default():
    """Reproduces the real NVDA case found while building this: 'alpaca'
    (unadjusted) has a 10x cliff on a real split date, while
    'alpaca_adjusted' is smooth. resolve_source's default trust-ratio
    logic would pick 'alpaca' here (764-equivalent share of rows clears
    the floor) - this module must override that and use the adjusted
    series whenever one exists."""
    conn = make_test_db()
    insert_price_row(conn, "NVDA", "2024-06-07", 1208.88, source="alpaca")
    insert_price_row(conn, "NVDA", "2024-06-10", 121.79, source="alpaca")  # unadjusted: false 10x cliff
    insert_price_row(conn, "NVDA", "2024-06-07", 120.68, source="alpaca_adjusted")
    insert_price_row(conn, "NVDA", "2024-06-10", 121.58, source="alpaca_adjusted")  # smooth, correct
    upsert_real_holding(conn, "NVDA", owner="Mom", shares=1.0, cost_basis_total=100.0)

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")

    assert series.values == [120.68, 121.58]


def test_falls_back_to_default_source_when_no_adjusted_series_exists():
    conn = make_test_db()
    insert_price_row(conn, "XLV", "2026-08-20", 174.62, source="alpaca")
    upsert_real_holding(conn, "XLV", owner="Mom", shares=1.0, cost_basis_total=100.0)

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")

    assert series.values == [174.62]


def test_value_is_shares_times_close_per_date():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2026-08-20", 100.0)
    insert_price_row(conn, "AAA", "2026-08-21", 110.0)
    upsert_real_holding(conn, "AAA", owner="Mom", shares=10.0, cost_basis_total=900.0)

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")

    assert series.dates == ["2026-08-20", "2026-08-21"]
    assert series.values == [1000.0, 1100.0]


def test_multiple_tickers_sum_on_shared_dates():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2026-08-20", 100.0)
    insert_price_row(conn, "BBB", "2026-08-20", 50.0)
    upsert_real_holding(conn, "AAA", owner="Mom", shares=10.0, cost_basis_total=900.0)
    upsert_real_holding(conn, "BBB", owner="Mom", shares=4.0, cost_basis_total=180.0)

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")

    assert series.values == [1000.0 + 200.0]


def test_ticker_with_shorter_history_only_contributes_where_priced():
    """A ticker whose price history starts later doesn't zero out or gap
    the earlier dates for OTHER tickers - it just doesn't contribute
    before it has a price, same "broadening universe" reality as this
    project's actual mixed-depth price history."""
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2026-08-19", 100.0)
    insert_price_row(conn, "AAA", "2026-08-20", 100.0)
    insert_price_row(conn, "BBB", "2026-08-20", 50.0)  # BBB starts one day later
    upsert_real_holding(conn, "AAA", owner="Mom", shares=1.0, cost_basis_total=90.0)
    upsert_real_holding(conn, "BBB", owner="Mom", shares=1.0, cost_basis_total=45.0)

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")

    assert series.values == [100.0, 150.0]


def test_needs_manual_entry_holding_is_excluded_not_estimated():
    conn = make_test_db()
    insert_price_row(conn, "STN", "2026-08-20", 74.0)
    upsert_real_holding(conn, "STN", owner="Mom", needs_manual_entry=True)

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")

    assert series.dates == []
    assert series.excluded_tickers == ["STN"]


def test_holding_with_share_history_caveat_is_flagged_approximate():
    conn = make_test_db()
    insert_price_row(conn, "KMI", "2026-08-20", 31.0)
    upsert_real_holding(
        conn, "KMI", owner="Mom", shares=280.0, cost_basis_total=1440.0,
        share_history_caveat="DRIP growth, no dated payment history to reconstruct from.",
    )

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")

    assert len(series.approximate) == 1
    assert series.approximate[0].ticker == "KMI"
    assert series.values == [280.0 * 31.0]  # still included in the total, just flagged


def test_holding_with_undated_realized_sale_is_flagged_approximate():
    conn = make_test_db()
    insert_price_row(conn, "META", "2026-08-20", 550.0)
    upsert_real_holding(conn, "META", owner="Mom", shares=75.0, cost_basis_total=25725.0)
    add_realized_sale(conn, "META", owner="Mom", shares_sold=75.0, cost_basis_sold=25725.0, proceeds=49950.0)  # no sale_date

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")

    assert len(series.approximate) == 1
    assert "sale is recorded" in series.approximate[0].caveat


def test_holding_with_dated_realized_sale_is_not_flagged():
    conn = make_test_db()
    insert_price_row(conn, "META", "2026-08-20", 550.0)
    upsert_real_holding(conn, "META", owner="Mom", shares=75.0, cost_basis_total=25725.0)
    add_realized_sale(
        conn, "META", owner="Mom", sale_date="2025-01-01",
        shares_sold=75.0, cost_basis_sold=25725.0, proceeds=49950.0,
    )

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")
    assert series.approximate == []


def test_build_by_scope_computes_each_owner_independently():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2026-08-20", 100.0)
    insert_price_row(conn, "BBB", "2026-08-20", 50.0)
    upsert_real_holding(conn, "AAA", owner="Mom", shares=10.0, cost_basis_total=900.0)
    upsert_real_holding(conn, "BBB", owner="Tyler", shares=2.0, cost_basis_total=90.0)

    by_scope = build_portfolio_value_series_by_scope(conn)

    assert by_scope["Combined"].values == [1000.0 + 100.0]
    assert by_scope["Mom"].values == [1000.0]
    assert by_scope["Tyler"].values == [100.0]


def test_owner_with_only_manual_entry_holdings_gets_empty_series():
    conn = make_test_db()
    upsert_real_holding(conn, "STN", owner="Tyler", needs_manual_entry=True)

    by_scope = build_portfolio_value_series_by_scope(conn)

    assert by_scope["Tyler"].dates == []
    assert by_scope["Tyler"].excluded_tickers == ["STN"]


def test_ticker_lagging_the_combined_latest_date_truncates_the_tail_and_is_flagged():
    """BBB has one more day of data than AAA - rather than showing a
    misleading partial-total on 08-21 (AAA silently missing, making the
    total look like it dropped), the series is truncated (gapped) at the
    last date EVERY ticker has data for (08-20), and AAA is flagged as
    the cause."""
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2026-08-20", 100.0)
    insert_price_row(conn, "BBB", "2026-08-20", 50.0)
    insert_price_row(conn, "BBB", "2026-08-21", 55.0)
    upsert_real_holding(conn, "AAA", owner="Mom", shares=1.0, cost_basis_total=90.0)
    upsert_real_holding(conn, "BBB", owner="Mom", shares=1.0, cost_basis_total=45.0)

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")

    assert series.dates[-1] == "2026-08-20"  # 08-21 gapped, not shown as a partial/misleading total
    assert series.values[-1] == 150.0  # AAA + BBB, both priced as of 08-20
    assert series.stale_tickers == [{"ticker": "AAA", "last_priced_date": "2026-08-20", "portfolio_latest_date": "2026-08-21"}]


def test_no_stale_tickers_when_all_share_the_latest_date():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2026-08-21", 100.0)
    insert_price_row(conn, "BBB", "2026-08-21", 50.0)
    upsert_real_holding(conn, "AAA", owner="Mom", shares=1.0, cost_basis_total=90.0)
    upsert_real_holding(conn, "BBB", owner="Mom", shares=1.0, cost_basis_total=45.0)

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")
    assert series.stale_tickers == []


def test_to_series_converts_to_a_date_indexed_pandas_series():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2026-08-20", 100.0)
    insert_price_row(conn, "AAA", "2026-08-21", 110.0)
    upsert_real_holding(conn, "AAA", owner="Mom", shares=10.0, cost_basis_total=900.0)

    series = to_series(build_portfolio_value_series(conn, list_real_holdings(conn), "Combined"))

    assert list(series.values) == [1000.0, 1100.0]
    assert series.index[0].strftime("%Y-%m-%d") == "2026-08-20"
