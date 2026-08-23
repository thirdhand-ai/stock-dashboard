"""Tests for trading/data_completeness.py's aggregation: it must surface
exactly the flags real_holdings/realized_sales already define, invent
nothing new, and never report a false gap for confirmed data. Against a
throwaway in-memory SQLite database, same pattern tests/test_real_holdings.py
uses."""
import sqlite3

from db.real_holdings_repository import upsert_real_holding
from db.realized_sales_repository import add_realized_sale
from db.schema import init_db
from trading.data_completeness import (
    PORTFOLIO_PERFORMANCE_PAGE,
    REAL_HOLDINGS_PAGE,
    REALIZED_GAINS_PAGE,
    build_data_completeness_report,
    scan_dividend_gaps,
    scan_real_holdings_gaps,
    scan_realized_sales_gaps,
)


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_confirmed_holding_produces_no_gaps():
    conn = make_test_db()
    upsert_real_holding(conn, "MSFT", owner="Mom", shares=50.0, cost_basis_total=20900.0)

    assert scan_real_holdings_gaps(conn) == []


def test_needs_manual_entry_holding_is_flagged():
    conn = make_test_db()
    upsert_real_holding(conn, "STN", owner="Mom", needs_manual_entry=True)

    gaps = scan_real_holdings_gaps(conn)
    assert len(gaps) == 1
    assert gaps[0].category == "Needs manual entry"
    assert gaps[0].ticker == "STN"
    assert gaps[0].owner == "Mom"
    assert gaps[0].fix_page == REAL_HOLDINGS_PAGE


def test_needs_manual_entry_produces_exactly_one_gap_not_a_double_count():
    """A needs_manual_entry row also has shares=None/cost_basis_total=None
    by construction - must not ALSO get separate "unknown share
    count"/"unknown cost basis" gaps on top of the one needs_manual_entry
    gap (that would double-report the same underlying fact)."""
    conn = make_test_db()
    upsert_real_holding(conn, "STN", owner="Mom", needs_manual_entry=True)

    categories = [g.category for g in scan_real_holdings_gaps(conn)]
    assert categories == ["Needs manual entry"]


def test_unassigned_owner_displays_as_unassigned_not_blank():
    conn = make_test_db()
    upsert_real_holding(conn, "AAA", needs_manual_entry=True)  # no owner given

    gaps = scan_real_holdings_gaps(conn)
    assert gaps[0].owner == "Unassigned"


def test_share_history_caveat_is_flagged_with_its_own_text():
    conn = make_test_db()
    upsert_real_holding(
        conn, "KMI", owner="Mom", shares=280.0, cost_basis_total=1440.0,
        share_history_caveat="DRIP growth, no dated payment history to reconstruct from.",
    )

    gaps = scan_real_holdings_gaps(conn)
    assert len(gaps) == 1
    assert gaps[0].category == "Approximate share history (DRIP)"
    assert gaps[0].detail == "DRIP growth, no dated payment history to reconstruct from."
    assert gaps[0].fix_page == PORTFOLIO_PERFORMANCE_PAGE


def test_confirmed_holding_with_caveat_still_only_reports_the_caveat():
    """A confirmed (non-manual-entry) holding with a share_history_caveat
    should report exactly the caveat gap, no unknown-shares/cost-basis
    gaps alongside it."""
    conn = make_test_db()
    upsert_real_holding(
        conn, "HPI", owner="Mom", shares=2146.0, cost_basis_total=26327.10,
        share_history_caveat="DRIP growth.",
    )

    categories = [g.category for g in scan_real_holdings_gaps(conn)]
    assert categories == ["Approximate share history (DRIP)"]


def test_dated_realized_sale_produces_no_gap():
    conn = make_test_db()
    add_realized_sale(
        conn, "AAA", owner="Mom", purchase_date="2024-01-01", sale_date="2026-01-01",
        shares_sold=10.0, cost_basis_sold=100.0, proceeds=150.0,
    )

    assert scan_realized_sales_gaps(conn) == []


def test_undated_realized_sale_is_flagged_naming_both_missing_dates():
    conn = make_test_db()
    add_realized_sale(conn, "META", owner="Mom", shares_sold=75.0, cost_basis_sold=25725.0, proceeds=49950.0)

    gaps = scan_realized_sales_gaps(conn)
    assert len(gaps) == 1
    assert gaps[0].category == "Missing sale date(s)"
    assert gaps[0].ticker == "META"
    assert "purchase date" in gaps[0].detail.lower()
    assert "sale date" in gaps[0].detail.lower()
    assert gaps[0].fix_page == REALIZED_GAINS_PAGE


def test_partially_dated_realized_sale_names_only_the_missing_one():
    conn = make_test_db()
    add_realized_sale(
        conn, "AAA", owner="Mom", sale_date="2026-01-01",
        shares_sold=5.0, cost_basis_sold=50.0, proceeds=60.0,
    )

    gaps = scan_realized_sales_gaps(conn)
    assert len(gaps) == 1
    assert "purchase date" in gaps[0].detail.lower()
    assert "sale date" not in gaps[0].detail.lower().replace("this", "")  # only "purchase date" named


def test_scan_dividend_gaps_is_always_empty():
    conn = make_test_db()
    assert scan_dividend_gaps(conn) == []


def test_build_report_matches_the_real_known_gaps_scenario():
    """Reproduces the exact real-system scenario named in the task: Tyler's
    NOW, STN's missing shares, META's missing dates, KMI/HPI's DRIP
    caveats - five gaps total, grouped correctly by category."""
    conn = make_test_db()
    upsert_real_holding(conn, "NOW", owner="Mom", shares=150.0, cost_basis_total=15300.0)
    upsert_real_holding(conn, "NOW", owner="Tyler", needs_manual_entry=True)
    upsert_real_holding(conn, "STN", owner="Mom", needs_manual_entry=True)
    upsert_real_holding(conn, "KMI", owner="Mom", shares=280.0, cost_basis_total=1440.0, share_history_caveat="DRIP.")
    upsert_real_holding(conn, "HPI", owner="Mom", shares=2146.0, cost_basis_total=26327.10, share_history_caveat="DRIP.")
    add_realized_sale(conn, "META", owner="Mom", shares_sold=75.0, cost_basis_sold=25725.0, proceeds=49950.0)

    report = build_data_completeness_report(conn)

    assert len(report.gaps) == 5
    by_category = report.by_category
    assert len(by_category["Needs manual entry"]) == 2  # NOW/Tyler, STN/Mom
    assert len(by_category["Approximate share history (DRIP)"]) == 2  # KMI, HPI
    assert len(by_category["Missing sale date(s)"]) == 1  # META
    assert {g.ticker for g in by_category["Needs manual entry"]} == {"NOW", "STN"}
