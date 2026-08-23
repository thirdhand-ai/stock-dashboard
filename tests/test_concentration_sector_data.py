"""Tests for trading/concentration.py::ensure_sector_data - the fetch-if-
missing population of ticker_sector. Finnhub is always mocked here - no
test in this suite (or anywhere in the project) ever makes a real network
call."""
import sqlite3
from unittest.mock import patch

from db.schema import init_db
from db.ticker_sector_repository import get_ticker_sector, upsert_ticker_sector
from trading.concentration import ensure_sector_data


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def test_fetches_and_caches_missing_tickers_only():
    conn = make_test_db()
    upsert_ticker_sector(conn, "AAPL", "Technology")  # already cached - should NOT be re-fetched

    with patch("ingestion.finnhub_source.fetch_company_industry", side_effect=lambda t: f"{t}-industry") as mock_fetch:
        ensure_sector_data(conn, ["AAPL", "MSFT"])

    mock_fetch.assert_called_once_with("MSFT")
    assert get_ticker_sector(conn, "AAPL").industry == "Technology"
    assert get_ticker_sector(conn, "MSFT").industry == "MSFT-industry"


def test_failed_lookup_for_one_ticker_does_not_block_others():
    conn = make_test_db()

    def flaky_fetch(ticker):
        if ticker == "BAD":
            raise RuntimeError("simulated Finnhub failure")
        return f"{ticker}-industry"

    with patch("ingestion.finnhub_source.fetch_company_industry", side_effect=flaky_fetch):
        ensure_sector_data(conn, ["BAD", "GOOD"])

    assert get_ticker_sector(conn, "BAD").industry is None  # never fabricated, never blocked GOOD
    assert get_ticker_sector(conn, "GOOD").industry == "GOOD-industry"


def test_empty_ticker_list_makes_no_calls():
    conn = make_test_db()
    with patch("ingestion.finnhub_source.fetch_company_industry") as mock_fetch:
        ensure_sector_data(conn, [])
    mock_fetch.assert_not_called()
