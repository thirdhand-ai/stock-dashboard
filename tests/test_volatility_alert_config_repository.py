"""Tests for db/volatility_alert_config_repository.py - the CRUD interface
for the volatility_alert_config table (dashboard/views/
volatility_alert_config.py's enable/edit/disable UI). Mirrors tests/
test_price_alert_config_repository.py's structure.
"""
import sqlite3

import pytest

from alerts.volatility_config import VolatilityAlertConfig
from db.schema import init_db
from db.volatility_alert_config_repository import (
    delete_volatility_alert_config,
    get_volatility_alert_config,
    list_volatility_alert_configs,
    upsert_volatility_alert_config,
)


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


# --- list_volatility_alert_configs / get_volatility_alert_config ---


def test_list_volatility_alert_configs_empty_by_default():
    conn = make_test_db()
    assert list_volatility_alert_configs(conn) == []


def test_get_volatility_alert_config_returns_none_for_unknown_ticker():
    conn = make_test_db()
    assert get_volatility_alert_config(conn, "GHOST") is None


def test_list_volatility_alert_configs_returns_ticker_ascending():
    conn = make_test_db()
    upsert_volatility_alert_config(conn, "TSLA", threshold_percent=8.0)
    upsert_volatility_alert_config(conn, "AAPL", threshold_percent=5.0)
    upsert_volatility_alert_config(conn, "MSFT", threshold_percent=6.0)

    tickers = [c.ticker for c in list_volatility_alert_configs(conn)]
    assert tickers == ["AAPL", "MSFT", "TSLA"]


# --- upsert_volatility_alert_config: add + edit are the same operation ---


def test_upsert_adds_a_new_ticker():
    conn = make_test_db()
    upsert_volatility_alert_config(conn, "AAPL", threshold_percent=5.0)

    config = get_volatility_alert_config(conn, "AAPL")
    assert config == VolatilityAlertConfig(ticker="AAPL", threshold_percent=5.0)


def test_upsert_overwrites_an_existing_tickers_threshold():
    conn = make_test_db()
    upsert_volatility_alert_config(conn, "AAPL", threshold_percent=5.0)

    upsert_volatility_alert_config(conn, "AAPL", threshold_percent=8.0)  # edit

    config = get_volatility_alert_config(conn, "AAPL")
    assert config == VolatilityAlertConfig(ticker="AAPL", threshold_percent=8.0)
    assert len(list_volatility_alert_configs(conn)) == 1  # still one row, not a duplicate


def test_upsert_rejects_zero_or_negative_threshold():
    conn = make_test_db()
    with pytest.raises(ValueError):
        upsert_volatility_alert_config(conn, "AAPL", threshold_percent=0.0)
    with pytest.raises(ValueError):
        upsert_volatility_alert_config(conn, "AAPL", threshold_percent=-5.0)


# --- delete_volatility_alert_config ---


def test_delete_removes_a_configured_ticker():
    conn = make_test_db()
    upsert_volatility_alert_config(conn, "AAPL", threshold_percent=5.0)

    delete_volatility_alert_config(conn, "AAPL")

    assert get_volatility_alert_config(conn, "AAPL") is None
    assert list_volatility_alert_configs(conn) == []


def test_delete_on_unconfigured_ticker_is_a_noop():
    conn = make_test_db()
    upsert_volatility_alert_config(conn, "AAPL", threshold_percent=5.0)

    delete_volatility_alert_config(conn, "GHOST")  # never configured - must not raise or affect AAPL

    assert len(list_volatility_alert_configs(conn)) == 1
    assert get_volatility_alert_config(conn, "AAPL") is not None


def test_delete_only_affects_the_named_ticker():
    conn = make_test_db()
    upsert_volatility_alert_config(conn, "AAPL", threshold_percent=5.0)
    upsert_volatility_alert_config(conn, "MSFT", threshold_percent=6.0)

    delete_volatility_alert_config(conn, "AAPL")

    assert get_volatility_alert_config(conn, "AAPL") is None
    assert get_volatility_alert_config(conn, "MSFT") is not None
