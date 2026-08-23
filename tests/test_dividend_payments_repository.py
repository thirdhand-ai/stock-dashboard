"""Tests for db/dividend_payments_repository.py - CRUD against a throwaway
in-memory SQLite database, same pattern tests/test_real_holdings_repository.py
uses."""
import sqlite3

from db.dividend_payments_repository import (
    add_dividend_payment,
    delete_dividend_payment,
    list_dividend_payments,
)
from db.schema import init_db


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_add_and_list_dividend_payment():
    conn = make_test_db()
    add_dividend_payment(
        conn, "KMI", owner="Mom", pay_date="2026-06-15",
        amount_per_share=0.2925, total_received=81.90, reinvested=True, note="Q2 DRIP",
    )

    payments = list_dividend_payments(conn, ticker="KMI")
    assert len(payments) == 1
    p = payments[0]
    assert p.owner == "Mom"
    assert p.pay_date == "2026-06-15"
    assert p.amount_per_share == 0.2925
    assert p.total_received == 81.90
    assert p.reinvested is True
    assert p.note == "Q2 DRIP"


def test_owner_defaults_to_empty_string_not_none():
    conn = make_test_db()
    add_dividend_payment(conn, "AAA", pay_date="2026-01-01", amount_per_share=1.0, total_received=10.0)

    payments = list_dividend_payments(conn, ticker="AAA")
    assert payments[0].owner == ""


def test_list_dividend_payments_is_pay_date_ascending():
    conn = make_test_db()
    add_dividend_payment(conn, "KMI", owner="Mom", pay_date="2026-06-15", amount_per_share=0.29, total_received=80.0)
    add_dividend_payment(conn, "KMI", owner="Mom", pay_date="2026-03-15", amount_per_share=0.29, total_received=79.0)

    payments = list_dividend_payments(conn, ticker="KMI")
    assert [p.pay_date for p in payments] == ["2026-03-15", "2026-06-15"]


def test_list_dividend_payments_filters_by_ticker_and_owner():
    conn = make_test_db()
    add_dividend_payment(conn, "NOW", owner="Mom", pay_date="2026-01-01", amount_per_share=1.0, total_received=150.0)
    add_dividend_payment(conn, "NOW", owner="Tyler", pay_date="2026-01-01", amount_per_share=1.0, total_received=10.0)

    mom_payments = list_dividend_payments(conn, ticker="NOW", owner="Mom")
    assert len(mom_payments) == 1
    assert mom_payments[0].total_received == 150.0


def test_delete_dividend_payment_removes_only_that_row():
    conn = make_test_db()
    keep_id = add_dividend_payment(conn, "KMI", owner="Mom", pay_date="2026-03-15", amount_per_share=0.29, total_received=79.0)
    remove_id = add_dividend_payment(conn, "KMI", owner="Mom", pay_date="2026-06-15", amount_per_share=0.29, total_received=80.0)

    delete_dividend_payment(conn, remove_id)

    remaining = list_dividend_payments(conn, ticker="KMI")
    assert [p.id for p in remaining] == [keep_id]
