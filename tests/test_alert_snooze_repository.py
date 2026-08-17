"""Tests for db/alert_snooze_repository.py - CRUD, expiry, and the
ticker-specific-beats-global precedence rule get_active_snooze uses. All
against a throwaway in-memory SQLite database, same pattern every other
alert repository test in this suite uses.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

from db.alert_snooze_repository import (
    ALERT_TYPE_PRICE,
    ALERT_TYPE_VOLATILITY,
    create_snooze,
    delete_snooze,
    get_active_snooze,
    list_active_snoozes,
)
from db.schema import init_db


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def future_ts(**kwargs):
    return (datetime.now(timezone.utc) + timedelta(**kwargs)).strftime("%Y-%m-%d %H:%M:%S")


def past_ts(**kwargs):
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).strftime("%Y-%m-%d %H:%M:%S")


def test_create_and_get_active_snooze_for_a_specific_ticker():
    conn = make_test_db()
    create_snooze(conn, ALERT_TYPE_PRICE, "AAPL", future_ts(hours=1))

    active = get_active_snooze(conn, ALERT_TYPE_PRICE, "AAPL")
    assert active is not None
    assert active.ticker == "AAPL"
    assert active.is_global is False


def test_get_active_snooze_returns_none_for_an_unsnoozed_ticker():
    conn = make_test_db()
    create_snooze(conn, ALERT_TYPE_PRICE, "AAPL", future_ts(hours=1))

    assert get_active_snooze(conn, ALERT_TYPE_PRICE, "MSFT") is None


def test_get_active_snooze_returns_none_once_expired():
    conn = make_test_db()
    create_snooze(conn, ALERT_TYPE_PRICE, "AAPL", past_ts(minutes=1))

    assert get_active_snooze(conn, ALERT_TYPE_PRICE, "AAPL") is None


def test_global_snooze_covers_every_ticker_under_its_alert_type():
    conn = make_test_db()
    create_snooze(conn, ALERT_TYPE_PRICE, None, future_ts(hours=1))

    active = get_active_snooze(conn, ALERT_TYPE_PRICE, "AAPL")
    assert active is not None
    assert active.is_global is True


def test_ticker_specific_snooze_takes_precedence_over_global():
    conn = make_test_db()
    create_snooze(conn, ALERT_TYPE_PRICE, None, future_ts(hours=1))
    create_snooze(conn, ALERT_TYPE_PRICE, "AAPL", future_ts(hours=2))

    active = get_active_snooze(conn, ALERT_TYPE_PRICE, "AAPL")
    assert active.is_global is False
    assert active.ticker == "AAPL"


def test_snooze_is_scoped_to_its_alert_type():
    conn = make_test_db()
    create_snooze(conn, ALERT_TYPE_PRICE, "AAPL", future_ts(hours=1))

    assert get_active_snooze(conn, ALERT_TYPE_VOLATILITY, "AAPL") is None


def test_delete_snooze_removes_it_immediately():
    conn = make_test_db()
    snooze_id = create_snooze(conn, ALERT_TYPE_PRICE, "AAPL", future_ts(hours=1))
    assert get_active_snooze(conn, ALERT_TYPE_PRICE, "AAPL") is not None

    delete_snooze(conn, snooze_id)

    assert get_active_snooze(conn, ALERT_TYPE_PRICE, "AAPL") is None


def test_delete_snooze_on_unknown_id_is_a_harmless_no_op():
    conn = make_test_db()
    delete_snooze(conn, 999999)  # must not raise


def test_list_active_snoozes_excludes_expired_rows():
    conn = make_test_db()
    create_snooze(conn, ALERT_TYPE_PRICE, "AAPL", future_ts(hours=1))
    create_snooze(conn, ALERT_TYPE_PRICE, "MSFT", past_ts(minutes=1))

    df = list_active_snoozes(conn, alert_type=ALERT_TYPE_PRICE)

    assert list(df["ticker"]) == ["AAPL"]


def test_list_active_snoozes_filters_by_alert_type():
    conn = make_test_db()
    create_snooze(conn, ALERT_TYPE_PRICE, "AAPL", future_ts(hours=1))
    create_snooze(conn, ALERT_TYPE_VOLATILITY, "AAPL", future_ts(hours=1))

    price_df = list_active_snoozes(conn, alert_type=ALERT_TYPE_PRICE)
    all_df = list_active_snoozes(conn)

    assert len(price_df) == 1
    assert len(all_df) == 2
