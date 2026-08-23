"""Tests for trading/dividends.py's analytics: trailing-12-month yield on
cost, running totals, and per-owner grouping. Against a throwaway in-memory
SQLite database, same pattern tests/test_real_holdings.py uses."""
import sqlite3
from datetime import date

from db.dividend_payments_repository import add_dividend_payment
from db.real_holdings_repository import upsert_real_holding
from db.schema import init_db
from trading.dividends import build_dividend_summary, dividend_totals_by_owner, portfolio_dividend_totals


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_holding_with_no_payments_is_excluded():
    conn = make_test_db()
    upsert_real_holding(conn, "NOW", owner="Mom", shares=150.0, cost_basis_total=15300.0)

    summaries = build_dividend_summary(conn, as_of=date(2026, 8, 22))
    assert summaries == []


def test_ttm_received_and_yield_on_cost():
    conn = make_test_db()
    upsert_real_holding(conn, "KMI", owner="Mom", shares=280.0, cost_basis_total=1440.0)
    add_dividend_payment(conn, "KMI", owner="Mom", pay_date="2026-03-15", amount_per_share=0.29, total_received=81.2)
    add_dividend_payment(conn, "KMI", owner="Mom", pay_date="2026-06-15", amount_per_share=0.29, total_received=81.2)

    summaries = build_dividend_summary(conn, as_of=date(2026, 8, 22))
    assert len(summaries) == 1
    s = summaries[0]
    assert s.ttm_received == 162.4
    assert s.all_time_received == 162.4
    assert s.payment_count == 2
    assert s.last_payment_date == "2026-06-15"
    assert round(s.ttm_yield_on_cost_pct, 4) == round((162.4 / 1440.0) * 100, 4)


def test_payments_older_than_ttm_window_excluded_from_ttm_but_not_all_time():
    conn = make_test_db()
    upsert_real_holding(conn, "KMI", owner="Mom", shares=280.0, cost_basis_total=1440.0)
    add_dividend_payment(conn, "KMI", owner="Mom", pay_date="2024-01-01", amount_per_share=0.25, total_received=70.0)  # >365 days before as_of
    add_dividend_payment(conn, "KMI", owner="Mom", pay_date="2026-06-15", amount_per_share=0.29, total_received=81.2)

    s = build_dividend_summary(conn, as_of=date(2026, 8, 22))[0]
    assert s.ttm_received == 81.2
    assert s.all_time_received == 151.2


def test_payment_exactly_365_days_before_as_of_is_included():
    conn = make_test_db()
    upsert_real_holding(conn, "KMI", owner="Mom", shares=280.0, cost_basis_total=1440.0)
    add_dividend_payment(conn, "KMI", owner="Mom", pay_date="2025-08-22", amount_per_share=0.25, total_received=70.0)

    s = build_dividend_summary(conn, as_of=date(2026, 8, 22))[0]
    assert s.ttm_received == 70.0


def test_needs_manual_entry_holding_gets_none_yield_not_fabricated():
    conn = make_test_db()
    upsert_real_holding(conn, "STN", owner="Mom", needs_manual_entry=True)
    add_dividend_payment(conn, "STN", owner="Mom", pay_date="2026-06-15", amount_per_share=0.245, total_received=20.0)

    s = build_dividend_summary(conn, as_of=date(2026, 8, 22))[0]
    assert s.cost_basis_total is None
    assert s.ttm_yield_on_cost_pct is None
    assert s.ttm_received == 20.0  # dollar total is still known even though cost basis isn't


def test_portfolio_dividend_totals_sums_across_holdings():
    conn = make_test_db()
    upsert_real_holding(conn, "KMI", owner="Mom", shares=280.0, cost_basis_total=1440.0)
    upsert_real_holding(conn, "HPI", owner="Mom", shares=2146.0, cost_basis_total=26327.10)
    add_dividend_payment(conn, "KMI", owner="Mom", pay_date="2026-06-15", amount_per_share=0.29, total_received=81.2)
    add_dividend_payment(conn, "HPI", owner="Mom", pay_date="2026-06-15", amount_per_share=0.43, total_received=923.0)

    totals = portfolio_dividend_totals(build_dividend_summary(conn, as_of=date(2026, 8, 22)))
    assert totals["total_ttm_received"] == 1004.2
    assert totals["total_all_time_received"] == 1004.2


def test_dividend_totals_by_owner_keeps_a_multi_owner_ticker_separate():
    conn = make_test_db()
    upsert_real_holding(conn, "NOW", owner="Mom", shares=150.0, cost_basis_total=15300.0)
    upsert_real_holding(conn, "NOW", owner="Tyler", needs_manual_entry=True)
    add_dividend_payment(conn, "NOW", owner="Mom", pay_date="2026-06-15", amount_per_share=1.0, total_received=150.0)
    add_dividend_payment(conn, "NOW", owner="Tyler", pay_date="2026-06-15", amount_per_share=1.0, total_received=10.0)

    by_owner = dividend_totals_by_owner(build_dividend_summary(conn, as_of=date(2026, 8, 22)))
    assert by_owner["Mom"]["total_ttm_received"] == 150.0
    assert by_owner["Tyler"]["total_ttm_received"] == 10.0
