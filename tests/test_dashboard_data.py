"""Tests for the dashboard's data-preparation layer (dashboard/data.py,
dashboard/charts.py pure helpers). Uses a throwaway in-memory SQLite
database, not the real project database, and no Streamlit runtime - these
exercise the `_load_*` functions directly, not the `st.cache_data`-wrapped
`get_*` wrappers.
"""
import sqlite3

import numpy as np
import pandas as pd
import pytest

from dashboard.charts import index_to_100
from dashboard.data import _load_backtest_report, _load_ticker_detail, _load_watchlist_row
from db.run_history_repository import STATUS_FAILED, STATUS_SUCCESS, finish_run, start_run
from db.schema import init_db
from indicators.technical import MIN_REQUIRED_ROWS


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_price_rows(conn, ticker, n_rows, seed=0, source="yfinance", drift=0.3, start="2022-01-01"):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_rows).strftime("%Y-%m-%d")
    close = np.maximum(100 + np.cumsum(drift + rng.normal(0, 1, n_rows)), 1.0)
    rows = [
        (ticker, dates[i], float(close[i] - 0.3), float(close[i] + 0.6), float(close[i] - 0.6),
         float(close[i]), int(rng.integers(1_000_000, 2_000_000)), source)
        for i in range(n_rows)
    ]
    conn.executemany(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return dates, close


# --- _load_watchlist_row ---


def test_watchlist_row_no_data():
    conn = make_test_db()
    row = _load_watchlist_row(conn, "GHOST")
    assert row.ok is False
    assert "no price data" in row.reason


def test_watchlist_row_insufficient_data_still_reports_price_and_change():
    conn = make_test_db()
    insert_price_rows(conn, "AAA", n_rows=10)
    row = _load_watchlist_row(conn, "AAA")

    assert row.ok is False
    assert "insufficient history" in row.reason
    assert row.last_price is not None
    assert row.change is not None  # still computable from the last two rows


def test_watchlist_row_sufficient_data_has_score_and_correct_change():
    conn = make_test_db()
    dates, close = insert_price_rows(conn, "BBB", n_rows=MIN_REQUIRED_ROWS + 30, seed=5)
    row = _load_watchlist_row(conn, "BBB")

    assert row.ok is True
    assert row.score is not None
    assert row.highest_confirmed_stage is not None
    assert row.source == "yfinance"
    assert row.last_price == pytest.approx(close[-1])
    assert row.change == pytest.approx(close[-1] - close[-2])


def test_watchlist_row_marks_stale_after_failed_scheduled_refresh():
    """A10: the dashboard must make it obvious when the displayed signal is
    the last stored result, not today's fresh analysis, after a failed run."""
    conn = make_test_db()
    insert_price_rows(conn, "CCC", n_rows=MIN_REQUIRED_ROWS + 30, seed=7)
    run_id = start_run(conn, "real")
    finish_run(conn, run_id, status=STATUS_FAILED, tickers_attempted=7, tickers_failed=7, error_summary="DNS failure")

    row = _load_watchlist_row(conn, "CCC", latest_run_status=STATUS_FAILED)

    assert row.ok is True  # still shows the last stored signal...
    assert row.is_stale is True  # ...but clearly flagged as stale
    assert row.latest_run_status == STATUS_FAILED


def test_watchlist_row_not_stale_after_successful_scheduled_refresh():
    conn = make_test_db()
    insert_price_rows(conn, "DDD", n_rows=MIN_REQUIRED_ROWS + 30, seed=8)
    run_id = start_run(conn, "real")
    finish_run(conn, run_id, status=STATUS_SUCCESS, tickers_attempted=7, tickers_updated=7)

    row = _load_watchlist_row(conn, "DDD", latest_run_status=STATUS_SUCCESS)
    assert row.is_stale is False


def test_watchlist_row_prefers_yfinance_over_alpaca():
    conn = make_test_db()
    insert_price_rows(conn, "CCC", n_rows=MIN_REQUIRED_ROWS + 10, source="alpaca", seed=1)
    insert_price_rows(conn, "CCC", n_rows=MIN_REQUIRED_ROWS + 10, source="yfinance", seed=2)
    row = _load_watchlist_row(conn, "CCC")

    assert row.source == "yfinance"


# --- _load_ticker_detail ---


def test_ticker_detail_insufficient_data():
    conn = make_test_db()
    insert_price_rows(conn, "DDD", n_rows=20)
    detail = _load_ticker_detail(conn, "DDD")

    assert detail.ok is False
    assert "insufficient history" in detail.reason


def test_ticker_detail_sufficient_data_has_full_history_and_score():
    conn = make_test_db()
    n = MIN_REQUIRED_ROWS + 40
    insert_price_rows(conn, "EEE", n_rows=n, seed=9)
    detail = _load_ticker_detail(conn, "EEE")

    assert detail.ok is True
    assert len(detail.enriched_history) == n
    assert detail.score is not None
    assert detail.indicators.ok is True


# --- _load_backtest_report ---


def test_backtest_report_insufficient_data():
    conn = make_test_db()
    insert_price_rows(conn, "FFF", n_rows=30)
    report = _load_backtest_report(conn, "FFF")

    assert report["ok"] is False
    assert "insufficient history" in report["reason"]


def test_backtest_report_sufficient_data_has_backtest_and_walkforward():
    conn = make_test_db()
    n = 400
    insert_price_rows(conn, "GGG", n_rows=n, seed=3, drift=0.1)
    report = _load_backtest_report(conn, "GGG")

    assert report["ok"] is True
    assert report["backtest"] is not None
    assert report["walk_forward"] is not None
    assert len(report["close_prices"]) == n
    assert report["close_prices"].index.is_monotonic_increasing


# --- chart helpers ---


def test_index_to_100_rebases_correctly():
    series = pd.Series([50.0, 55.0, 40.0], index=pd.date_range("2024-01-01", periods=3))
    rebased = index_to_100(series)

    assert rebased.iloc[0] == pytest.approx(100.0)
    assert rebased.iloc[1] == pytest.approx(110.0)
    assert rebased.iloc[2] == pytest.approx(80.0)
