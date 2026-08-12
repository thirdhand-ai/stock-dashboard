"""Alpaca data source — PAPER TRADING ONLY.

This module is strictly read-only: it checks the paper account connection and
pulls recent bar data. It never places, modifies, or cancels orders.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

import requests
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient

from config.settings import ALPACA_API_KEY, ALPACA_PAPER, ALPACA_SECRET_KEY

logger = logging.getLogger(__name__)

SOURCE_NAME = "alpaca"

# Transient network/DNS-class errors worth a bounded retry. alpaca-py makes
# its HTTP calls via `requests` and only intercepts requests.HTTPError (see
# alpaca.common.rest.RESTClient._request) - a connection-level failure like
# the DNS resolution error this retry was added for (NameResolutionError,
# wrapped by urllib3/requests into ConnectionError) propagates unchanged.
RETRYABLE_EXCEPTIONS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)


def _is_retryable(exc: Exception) -> bool:
    """True for transient network/DNS-class failures only - never for auth,
    bad-request, or no-data errors (HTTPError/ValueError etc.), which retrying
    would never fix and would only delay a fail-closed response."""
    return isinstance(exc, RETRYABLE_EXCEPTIONS)


def retry_request(fn, *, ticker: str, max_retries: int = 2, initial_delay_seconds: float = 1.0,
                   backoff_multiplier: float = 2.0):
    """Call `fn()` with a small, bounded retry for transient connectivity
    failures. Not a general retry framework - just enough to survive a
    momentary DNS/network blip without hammering Alpaca or retrying forever.
    Non-retryable errors (auth, bad request, no data) raise immediately."""
    attempt = 0
    delay = initial_delay_seconds
    while True:
        try:
            return fn()
        except Exception as e:
            if not _is_retryable(e) or attempt >= max_retries:
                raise
            attempt += 1
            logger.warning(
                "alpaca: retryable error for %s (attempt %d/%d), retrying in %.1fs: %s",
                ticker, attempt, max_retries, delay, e,
            )
            time.sleep(delay)
            delay *= backoff_multiplier


def _require_credentials():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY are not set in the environment")


def get_trading_client():
    _require_credentials()
    # paper=True is mandatory for this project: no real-money trading.
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)


def get_data_client():
    _require_credentials()
    return StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


def check_paper_account():
    """Read-only sanity check that credentials work and the account is a paper account."""
    client = get_trading_client()
    account = client.get_account()
    return {
        "account_number_masked": f"...{str(account.account_number)[-4:]}",
        "status": str(account.status),
        "is_paper": ALPACA_PAPER,
        "buying_power": str(account.buying_power),
        "cash": str(account.cash),
    }


def fetch_recent_bars(ticker, days=30):
    """Fetch recent daily bars for a ticker via Alpaca's market data API (read-only)."""
    client = get_data_client()
    request = StockBarsRequest(
        symbol_or_symbols=[ticker],
        timeframe=TimeFrame.Day,
        start=datetime.now(timezone.utc) - timedelta(days=days),
    )
    barset = client.get_stock_bars(request)
    df = barset.df
    if df.empty:
        raise ValueError(f"Alpaca returned no bar data for {ticker}")
    df = df.reset_index()
    df["date"] = df["timestamp"].dt.strftime("%Y-%m-%d")
    return df[["date", "open", "high", "low", "close", "volume"]]


def store_bars(conn, ticker, df):
    cur = conn.cursor()
    rows = [
        (
            ticker,
            row.date,
            float(row.open),
            float(row.high),
            float(row.low),
            float(row.close),
            int(row.volume),
            SOURCE_NAME,
        )
        for row in df.itertuples(index=False)
    ]
    cur.executemany(
        """
        INSERT INTO prices (ticker, date, open, high, low, close, volume, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, date, source) DO UPDATE SET
            open=excluded.open,
            high=excluded.high,
            low=excluded.low,
            close=excluded.close,
            volume=excluded.volume,
            fetched_at=datetime('now')
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def ingest_ticker(conn, ticker, days=30, max_retries=2, retry_initial_delay_seconds=1.0,
                   retry_backoff_multiplier=2.0):
    df = retry_request(
        lambda: fetch_recent_bars(ticker, days=days),
        ticker=ticker, max_retries=max_retries,
        initial_delay_seconds=retry_initial_delay_seconds,
        backoff_multiplier=retry_backoff_multiplier,
    )
    n = store_bars(conn, ticker, df)
    logger.info("alpaca: stored %d rows for %s", n, ticker)
    return n
