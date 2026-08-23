"""Tests for trading/realized_gains.py's analytics: derived realized
gain/loss, short/long-term classification, tax-year bucketing, and CSV
row shape. Against a throwaway in-memory SQLite database, same pattern
tests/test_dividends.py uses."""
import sqlite3

from db.realized_sales_repository import add_realized_sale
from db.schema import init_db
from trading.realized_gains import (
    TERM_LONG,
    TERM_SHORT,
    TERM_UNKNOWN,
    build_realized_gains_report,
    to_csv_rows,
    totals_by_owner,
    totals_by_tax_year,
)


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_realized_gain_is_derived_from_proceeds_minus_cost_basis():
    conn = make_test_db()
    add_realized_sale(
        conn, "META", owner="Mom", purchase_date="2024-01-15", sale_date="2026-03-01",
        shares_sold=75.0, cost_basis_sold=25725.0, proceeds=49950.0,
    )

    row = build_realized_gains_report(conn)[0]
    assert row.realized_gain == 24225.0


def test_holding_period_over_365_days_is_long_term():
    conn = make_test_db()
    add_realized_sale(
        conn, "META", owner="Mom", purchase_date="2024-01-01", sale_date="2026-01-02",
        shares_sold=75.0, cost_basis_sold=25725.0, proceeds=49950.0,
    )

    row = build_realized_gains_report(conn)[0]
    assert row.holding_period_days > 365
    assert row.term == TERM_LONG


def test_holding_period_of_exactly_365_days_is_short_term():
    """Strictly greater-than 365 - held for exactly one year (365 days)
    is not yet "more than one year"."""
    conn = make_test_db()
    add_realized_sale(
        conn, "AAA", purchase_date="2025-01-01", sale_date="2026-01-01",
        shares_sold=1.0, cost_basis_sold=100.0, proceeds=150.0,
    )

    row = build_realized_gains_report(conn)[0]
    assert row.holding_period_days == 365
    assert row.term == TERM_SHORT


def test_missing_purchase_date_gives_unknown_term_not_a_guess():
    conn = make_test_db()
    add_realized_sale(
        conn, "META", owner="Mom", sale_date="2026-03-01",
        shares_sold=75.0, cost_basis_sold=25725.0, proceeds=49950.0,
    )

    row = build_realized_gains_report(conn)[0]
    assert row.holding_period_days is None
    assert row.term == TERM_UNKNOWN
    assert row.tax_year == 2026  # sale_date alone is enough for tax year


def test_missing_both_dates_gives_unknown_term_and_no_tax_year():
    conn = make_test_db()
    add_realized_sale(conn, "META", owner="Mom", shares_sold=75.0, cost_basis_sold=25725.0, proceeds=49950.0)

    row = build_realized_gains_report(conn)[0]
    assert row.term == TERM_UNKNOWN
    assert row.tax_year is None


def test_totals_by_owner_sums_independently_per_owner():
    conn = make_test_db()
    add_realized_sale(conn, "META", owner="Mom", shares_sold=75.0, cost_basis_sold=25725.0, proceeds=49950.0)
    add_realized_sale(conn, "NOW", owner="Tyler", shares_sold=5.0, cost_basis_sold=500.0, proceeds=600.0)

    by_owner = totals_by_owner(build_realized_gains_report(conn))
    assert by_owner["Mom"]["total_realized_gain"] == 24225.0
    assert by_owner["Tyler"]["total_realized_gain"] == 100.0
    assert by_owner["Mom"]["transaction_count"] == 1


def test_totals_by_tax_year_buckets_unknown_sale_dates_separately():
    conn = make_test_db()
    add_realized_sale(conn, "AAA", sale_date="2025-06-01", shares_sold=1.0, cost_basis_sold=100.0, proceeds=150.0)
    add_realized_sale(conn, "BBB", sale_date="2026-06-01", shares_sold=1.0, cost_basis_sold=100.0, proceeds=200.0)
    add_realized_sale(conn, "META", shares_sold=75.0, cost_basis_sold=25725.0, proceeds=49950.0)  # no sale_date

    by_year = totals_by_tax_year(build_realized_gains_report(conn))
    assert by_year["2025"]["total_realized_gain"] == 50.0
    assert by_year["2026"]["total_realized_gain"] == 100.0
    assert by_year["Unknown"]["total_realized_gain"] == 24225.0
    assert list(by_year.keys())[-1] == "Unknown"  # Unknown bucket sorts last


def test_to_csv_rows_shape_matches_dashboard_columns():
    conn = make_test_db()
    add_realized_sale(
        conn, "META", owner="Mom", purchase_date="2024-01-15", sale_date="2026-03-01",
        shares_sold=75.0, cost_basis_sold=25725.0, proceeds=49950.0,
    )

    csv_rows = to_csv_rows(build_realized_gains_report(conn))
    assert csv_rows[0] == {
        "Ticker": "META", "Owner": "Mom", "Purchase date": "2024-01-15", "Sale date": "2026-03-01",
        "Shares sold": 75.0, "Cost basis of shares sold": 25725.0, "Proceeds": 49950.0,
        "Realized gain/loss": 24225.0, "Term": TERM_LONG, "Tax year": 2026,
    }


def test_to_csv_rows_shows_unknown_for_missing_dates_not_blank():
    conn = make_test_db()
    add_realized_sale(conn, "META", owner="Mom", shares_sold=75.0, cost_basis_sold=25725.0, proceeds=49950.0)

    csv_row = to_csv_rows(build_realized_gains_report(conn))[0]
    assert csv_row["Purchase date"] == "Unknown"
    assert csv_row["Sale date"] == "Unknown"
    assert csv_row["Tax year"] == "Unknown"
