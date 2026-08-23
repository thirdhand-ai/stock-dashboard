"""Tests for db/ticker_sector_repository.py - CRUD against a throwaway
in-memory SQLite database, same pattern every other repository test in
this suite uses."""
import sqlite3

from db.schema import init_db
from db.ticker_sector_repository import get_ticker_sector, list_ticker_sectors, upsert_ticker_sector


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_upsert_and_get_ticker_sector():
    conn = make_test_db()
    upsert_ticker_sector(conn, "AAPL", "Technology")

    sector = get_ticker_sector(conn, "AAPL")
    assert sector.ticker == "AAPL"
    assert sector.industry == "Technology"


def test_get_ticker_sector_returns_none_when_absent():
    conn = make_test_db()
    assert get_ticker_sector(conn, "GHOST") is None


def test_upsert_with_none_industry_stores_unknown_classification_not_a_guess():
    conn = make_test_db()
    upsert_ticker_sector(conn, "ZZZ", None)

    assert get_ticker_sector(conn, "ZZZ").industry is None


def test_upsert_overwrites_existing_ticker():
    conn = make_test_db()
    upsert_ticker_sector(conn, "AAPL", "Technology")
    upsert_ticker_sector(conn, "AAPL", "Consumer Electronics")

    assert get_ticker_sector(conn, "AAPL").industry == "Consumer Electronics"
    assert len(list_ticker_sectors(conn)) == 1


def test_list_ticker_sectors_is_ticker_ascending():
    conn = make_test_db()
    upsert_ticker_sector(conn, "ZZZ", "Industrials")
    upsert_ticker_sector(conn, "AAA", "Technology")

    tickers = [s.ticker for s in list_ticker_sectors(conn)]
    assert tickers == ["AAA", "ZZZ"]
