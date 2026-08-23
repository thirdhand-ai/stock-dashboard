"""Tests for db/real_holdings_repository.py - CRUD against a throwaway
in-memory SQLite database, same pattern every other repository test in
this suite uses."""
import sqlite3

from db.real_holdings_repository import (
    delete_real_holding,
    get_real_holding,
    list_real_holdings,
    upsert_real_holding,
)
from db.schema import init_db


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_upsert_and_get_real_holding():
    conn = make_test_db()
    upsert_real_holding(conn, "AAA", owner="Tyler", shares=100.0, cost_basis_total=10000.0, note="test note")

    holding = get_real_holding(conn, "AAA")
    assert holding.ticker == "AAA"
    assert holding.owner == "Tyler"
    assert holding.shares == 100.0
    assert holding.cost_basis_total == 10000.0
    assert holding.needs_manual_entry is False
    assert holding.note == "test note"


def test_get_real_holding_returns_none_when_absent():
    conn = make_test_db()
    assert get_real_holding(conn, "GHOST") is None


def test_upsert_is_add_or_overwrite_for_the_same_ticker():
    conn = make_test_db()
    upsert_real_holding(conn, "AAA", shares=100.0, cost_basis_total=10000.0)
    upsert_real_holding(conn, "AAA", shares=150.0, cost_basis_total=16000.0)  # same ticker, overwrite

    holding = get_real_holding(conn, "AAA")
    assert holding.shares == 150.0
    assert holding.cost_basis_total == 16000.0
    assert len(list_real_holdings(conn)) == 1


def test_needs_manual_entry_flag_leaves_numbers_unset():
    conn = make_test_db()
    upsert_real_holding(conn, "STN", needs_manual_entry=True, note="awarded shares, quantity unknown")

    holding = get_real_holding(conn, "STN")
    assert holding.needs_manual_entry is True
    assert holding.shares is None
    assert holding.cost_basis_total is None


def test_realized_gain_defaults_to_zero():
    conn = make_test_db()
    upsert_real_holding(conn, "AAA", shares=75.0, cost_basis_total=25725.0)

    assert get_real_holding(conn, "AAA").realized_gain == 0.0


def test_realized_gain_is_stored_as_given():
    conn = make_test_db()
    upsert_real_holding(conn, "META", shares=75.0, cost_basis_total=25725.0, realized_gain=24225.0)

    assert get_real_holding(conn, "META").realized_gain == 24225.0


def test_list_real_holdings_is_ticker_ascending():
    conn = make_test_db()
    upsert_real_holding(conn, "ZZZ", shares=1.0, cost_basis_total=1.0)
    upsert_real_holding(conn, "AAA", shares=1.0, cost_basis_total=1.0)

    tickers = [h.ticker for h in list_real_holdings(conn)]
    assert tickers == ["AAA", "ZZZ"]


def test_delete_real_holding_removes_it():
    conn = make_test_db()
    upsert_real_holding(conn, "AAA", shares=1.0, cost_basis_total=1.0)

    delete_real_holding(conn, "AAA")

    assert get_real_holding(conn, "AAA") is None


def test_same_ticker_can_have_two_owners_as_separate_rows():
    conn = make_test_db()
    upsert_real_holding(conn, "NOW", owner="Mom", shares=150.0, cost_basis_total=15300.0)
    upsert_real_holding(conn, "NOW", owner="Tyler", needs_manual_entry=True)

    rows = [h for h in list_real_holdings(conn) if h.ticker == "NOW"]
    assert len(rows) == 2
    by_owner = {h.owner: h for h in rows}
    assert by_owner["Mom"].shares == 150.0
    assert by_owner["Tyler"].needs_manual_entry is True
    assert by_owner["Tyler"].shares is None


def test_upsert_with_same_ticker_and_owner_overwrites_that_owners_row_only():
    conn = make_test_db()
    upsert_real_holding(conn, "NOW", owner="Mom", shares=150.0, cost_basis_total=15300.0)
    upsert_real_holding(conn, "NOW", owner="Tyler", needs_manual_entry=True)

    upsert_real_holding(conn, "NOW", owner="Mom", shares=160.0, cost_basis_total=16320.0)

    rows = [h for h in list_real_holdings(conn) if h.ticker == "NOW"]
    assert len(rows) == 2
    by_owner = {h.owner: h for h in rows}
    assert by_owner["Mom"].shares == 160.0
    assert by_owner["Tyler"].needs_manual_entry is True  # untouched by the Mom-only upsert


def test_get_real_holding_with_owner_disambiguates_a_multi_owner_ticker():
    conn = make_test_db()
    upsert_real_holding(conn, "NOW", owner="Mom", shares=150.0, cost_basis_total=15300.0)
    upsert_real_holding(conn, "NOW", owner="Tyler", needs_manual_entry=True)

    assert get_real_holding(conn, "NOW", owner="Mom").shares == 150.0
    assert get_real_holding(conn, "NOW", owner="Tyler").needs_manual_entry is True
    assert get_real_holding(conn, "NOW", owner="Nobody") is None


def test_delete_real_holding_with_owner_removes_only_that_owners_row():
    conn = make_test_db()
    upsert_real_holding(conn, "NOW", owner="Mom", shares=150.0, cost_basis_total=15300.0)
    upsert_real_holding(conn, "NOW", owner="Tyler", needs_manual_entry=True)

    delete_real_holding(conn, "NOW", owner="Tyler")

    rows = [h for h in list_real_holdings(conn) if h.ticker == "NOW"]
    assert len(rows) == 1
    assert rows[0].owner == "Mom"
