"""Tests for trading/portfolio_history.py's analytics: historical value
sum, exclusion/approximation flagging, normalization, and benchmark
alignment. Against a throwaway in-memory SQLite database with hand-
inserted price rows, same pattern tests/test_real_holdings.py uses."""
import sqlite3

from db.dividend_payments_repository import add_dividend_payment
from db.real_holding_lots_repository import add_real_holding_lot
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


def test_falls_back_to_raw_source_when_adjusted_source_is_stale():
    """Reproduces the real META/MSFT/NOW/NVDA case: 'alpaca_adjusted' is a
    research/backtest cache (strategy_lab/data.py) that stopped advancing
    past an old date while 'alpaca' (production) kept ingesting daily. A
    ticker in that state must fall back to 'alpaca' entirely, same as a
    ticker with no adjusted series at all - never silently truncate the
    chart to the adjusted series' stale last date."""
    conn = make_test_db()
    insert_price_row(conn, "NVDA", "2026-08-11", 120.0, source="alpaca")
    insert_price_row(conn, "NVDA", "2026-08-12", 121.0, source="alpaca")
    insert_price_row(conn, "NVDA", "2026-08-31", 130.0, source="alpaca")  # raw kept ingesting
    insert_price_row(conn, "NVDA", "2026-08-11", 119.5, source="alpaca_adjusted")
    insert_price_row(conn, "NVDA", "2026-08-12", 120.5, source="alpaca_adjusted")  # adjusted froze here
    upsert_real_holding(conn, "NVDA", owner="Mom", shares=1.0, cost_basis_total=100.0)

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")

    assert series.dates == ["2026-08-11", "2026-08-12", "2026-08-31"]
    assert series.values == [120.0, 121.0, 130.0]


def test_falls_back_to_raw_source_even_when_raw_row_count_is_thin():
    """Reproduces the real NOW case: production 'alpaca' ingestion for this
    ticker only recently started, so it has far fewer rows than the
    long-backfilled (but stale) 'alpaca_adjusted' cache. A naive fallback to
    load_price_history(conn, ticker) (no explicit source) would re-run
    resolve_source()'s MIN_TRUST_RATIO check and hand 'alpaca_adjusted'
    right back, since 2 rows can't clear 50% of 5 rows - the fix must
    fall back to the already-loaded raw 'alpaca' frame directly, not
    through resolve_source()."""
    conn = make_test_db()
    for d, c in [("2026-08-08", 100.0), ("2026-08-09", 101.0), ("2026-08-10", 102.0),
                 ("2026-08-11", 103.0), ("2026-08-12", 104.0)]:
        insert_price_row(conn, "NOW", d, c, source="alpaca_adjusted")  # 5 rows, frozen at 08-12
    insert_price_row(conn, "NOW", "2026-08-12", 104.5, source="alpaca")
    insert_price_row(conn, "NOW", "2026-08-13", 106.0, source="alpaca")  # only 2 rows, but current
    upsert_real_holding(conn, "NOW", owner="Mom", shares=1.0, cost_basis_total=100.0)

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")

    assert series.dates == ["2026-08-12", "2026-08-13"]
    assert series.values == [104.5, 106.0]


def test_uses_adjusted_source_when_it_is_current_with_raw():
    """Adjusted and raw sharing the same latest date is not staleness -
    the adjusted series should still be preferred (split-adjustment
    consistency), matching the existing NVDA split-cliff test above."""
    conn = make_test_db()
    insert_price_row(conn, "NVDA", "2026-08-12", 121.0, source="alpaca")
    insert_price_row(conn, "NVDA", "2026-08-12", 120.5, source="alpaca_adjusted")
    upsert_real_holding(conn, "NVDA", owner="Mom", shares=1.0, cost_basis_total=100.0)

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")

    assert series.values == [120.5]


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


def test_fully_dated_lot_and_drip_history_gives_a_piecewise_share_count():
    """A holding whose original lot (real_holding_lots) plus every DRIP
    reinvestment (dividend_payments, reinvested=1) fully account for its
    current share count gets an EXACT share count at each price date - 0
    before the lot, growing at each reinvestment - not the flat current
    total applied everywhere. share_history_caveat is set here too, to
    prove the dated reconstruction takes priority and clears the flag."""
    conn = make_test_db()
    insert_price_row(conn, "KMI", "2021-01-01", 10.0)  # before the lot: contributes 0
    insert_price_row(conn, "KMI", "2021-06-01", 20.0)  # after the lot, before the DRIP: 100 sh
    insert_price_row(conn, "KMI", "2021-12-01", 30.0)  # after the DRIP: 105 sh
    upsert_real_holding(
        conn, "KMI", owner="Mom", shares=105.0, cost_basis_total=1500.0,
        share_history_caveat="Stale caveat - should be superseded by the full reconstruction below.",
    )
    add_real_holding_lot(conn, "KMI", owner="Mom", purchase_date="2021-03-01", shares=100.0, cost_per_share=15.0, total_cost=1500.0)
    add_dividend_payment(conn, "KMI", owner="Mom", pay_date="2021-09-01", amount_per_share=25.0, total_received=125.0, reinvested=True)

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")

    assert series.values == [0.0, 100.0 * 20.0, 105.0 * 30.0]
    assert series.approximate == []  # dated reconstruction supersedes the stale caveat


def test_partial_dated_history_falls_back_to_flat_current_count_and_stays_flagged():
    """A lot that DOESN'T add up to the current share count (missing DRIP
    history) is not a full reconstruction - falls back to the old flat
    behavior and keeps showing its caveat, same as before this feature."""
    conn = make_test_db()
    insert_price_row(conn, "KMI", "2021-01-01", 10.0)
    upsert_real_holding(
        conn, "KMI", owner="Mom", shares=280.0, cost_basis_total=1440.0,
        share_history_caveat="DRIP growth, only partial dated history recorded.",
    )
    add_real_holding_lot(conn, "KMI", owner="Mom", purchase_date="2021-03-01", shares=96.0, cost_per_share=15.0, total_cost=1440.0)

    series = build_portfolio_value_series(conn, list_real_holdings(conn), "Combined")

    assert series.values == [280.0 * 10.0]  # flat current count, not the partial 96-share lot
    assert len(series.approximate) == 1
    assert series.approximate[0].ticker == "KMI"
