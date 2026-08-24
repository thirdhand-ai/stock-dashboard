"""Tests for db/real_holding_lots_repository.py - CRUD against a throwaway
in-memory SQLite database, same pattern tests/test_dividend_payments_repository.py
uses."""
import sqlite3

from db.real_holding_lots_repository import (
    add_real_holding_lot,
    delete_real_holding_lot,
    list_real_holding_lots,
)
from db.schema import init_db


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_add_and_list_real_holding_lot():
    conn = make_test_db()
    add_real_holding_lot(
        conn, "HPI", owner="Mom", purchase_date="2020-11-17",
        shares=1382.0, cost_per_share=17.906, total_cost=24746.09, note="Original purchase.",
    )

    lots = list_real_holding_lots(conn, ticker="HPI")
    assert len(lots) == 1
    lot = lots[0]
    assert lot.owner == "Mom"
    assert lot.purchase_date == "2020-11-17"
    assert lot.shares == 1382.0
    assert lot.cost_per_share == 17.906
    assert lot.total_cost == 24746.09
    assert lot.note == "Original purchase."


def test_owner_defaults_to_empty_string_not_none():
    conn = make_test_db()
    add_real_holding_lot(conn, "AAA", purchase_date="2026-01-01", shares=1.0, cost_per_share=10.0, total_cost=10.0)

    lots = list_real_holding_lots(conn, ticker="AAA")
    assert lots[0].owner == ""


def test_list_real_holding_lots_is_purchase_date_ascending():
    conn = make_test_db()
    add_real_holding_lot(conn, "KMI", owner="Mom", purchase_date="2024-11-06", shares=145.0, cost_per_share=27.093, total_cost=3928.49)
    add_real_holding_lot(conn, "KMI", owner="Mom", purchase_date="2021-04-26", shares=96.0, cost_per_share=15.061, total_cost=1445.86)

    lots = list_real_holding_lots(conn, ticker="KMI")
    assert [l.purchase_date for l in lots] == ["2021-04-26", "2024-11-06"]


def test_list_real_holding_lots_filters_by_ticker_and_owner():
    conn = make_test_db()
    add_real_holding_lot(conn, "NOW", owner="Mom", purchase_date="2026-01-01", shares=150.0, cost_per_share=100.0, total_cost=15000.0)
    add_real_holding_lot(conn, "NOW", owner="Tyler", purchase_date="2026-01-01", shares=10.0, cost_per_share=100.0, total_cost=1000.0)

    mom_lots = list_real_holding_lots(conn, ticker="NOW", owner="Mom")
    assert len(mom_lots) == 1
    assert mom_lots[0].shares == 150.0


def test_delete_real_holding_lot_removes_only_that_row():
    conn = make_test_db()
    keep_id = add_real_holding_lot(conn, "KMI", owner="Mom", purchase_date="2021-04-26", shares=96.0, cost_per_share=15.061, total_cost=1445.86)
    remove_id = add_real_holding_lot(conn, "KMI", owner="Mom", purchase_date="2024-11-06", shares=145.0, cost_per_share=27.093, total_cost=3928.49)

    delete_real_holding_lot(conn, remove_id)

    remaining = list_real_holding_lots(conn, ticker="KMI")
    assert [l.id for l in remaining] == [keep_id]
