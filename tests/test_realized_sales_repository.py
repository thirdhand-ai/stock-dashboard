"""Tests for db/realized_sales_repository.py - CRUD against a throwaway
in-memory SQLite database, same pattern tests/test_dividend_payments_repository.py
uses."""
import sqlite3

from db.realized_sales_repository import add_realized_sale, delete_realized_sale, list_realized_sales
from db.schema import init_db


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_add_and_list_realized_sale():
    conn = make_test_db()
    add_realized_sale(
        conn, "META", owner="Mom", purchase_date="2024-01-15", sale_date="2026-03-01",
        shares_sold=75.0, cost_basis_sold=25725.0, proceeds=49950.0, note="test",
    )

    sales = list_realized_sales(conn, ticker="META")
    assert len(sales) == 1
    s = sales[0]
    assert s.owner == "Mom"
    assert s.purchase_date == "2024-01-15"
    assert s.sale_date == "2026-03-01"
    assert s.shares_sold == 75.0
    assert s.cost_basis_sold == 25725.0
    assert s.proceeds == 49950.0
    assert s.note == "test"


def test_dates_left_null_when_not_given_not_guessed():
    conn = make_test_db()
    add_realized_sale(conn, "META", owner="Mom", shares_sold=75.0, cost_basis_sold=25725.0, proceeds=49950.0)

    sale = list_realized_sales(conn, ticker="META")[0]
    assert sale.purchase_date is None
    assert sale.sale_date is None


def test_owner_defaults_to_empty_string_not_none():
    conn = make_test_db()
    add_realized_sale(conn, "AAA", shares_sold=10.0, cost_basis_sold=100.0, proceeds=150.0)

    assert list_realized_sales(conn, ticker="AAA")[0].owner == ""


def test_list_realized_sales_filters_by_ticker_and_owner():
    conn = make_test_db()
    add_realized_sale(conn, "NOW", owner="Mom", shares_sold=10.0, cost_basis_sold=100.0, proceeds=150.0)
    add_realized_sale(conn, "NOW", owner="Tyler", shares_sold=5.0, cost_basis_sold=50.0, proceeds=60.0)

    mom_sales = list_realized_sales(conn, ticker="NOW", owner="Mom")
    assert len(mom_sales) == 1
    assert mom_sales[0].proceeds == 150.0


def test_delete_realized_sale_removes_only_that_row():
    conn = make_test_db()
    keep_id = add_realized_sale(conn, "AAA", shares_sold=1.0, cost_basis_sold=1.0, proceeds=2.0)
    remove_id = add_realized_sale(conn, "AAA", shares_sold=2.0, cost_basis_sold=2.0, proceeds=3.0)

    delete_realized_sale(conn, remove_id)

    remaining = list_realized_sales(conn, ticker="AAA")
    assert [s.id for s in remaining] == [keep_id]
