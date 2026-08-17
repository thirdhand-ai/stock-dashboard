"""Tests for dashboard/data.py's _load_alert_activity_feed() - the
read-only aggregation/merge across price_alerts, volatility_alerts,
daily_digest_log, operational_notifications, and alert_test_log into one
chronological feed (dashboard/views/alert_activity.py).
"""
import sqlite3
from datetime import date

import pytest

from dashboard.data import ALERT_ACTIVITY_COLUMNS, _channel_status, _load_alert_activity_feed
from db.schema import init_db


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_price_row(conn, ticker, date_str, close, source="yfinance"):
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, date_str, close, close, close, close, 1_000_000, source),
    )
    conn.commit()


# --- _channel_status: pure formatting ---


def test_channel_status_dry_run_takes_priority():
    assert _channel_status(True, False, "some error") == "Dry-run (not sent)"


def test_channel_status_delivered():
    assert _channel_status(False, True, None) == "delivered"


def test_channel_status_failed_includes_error():
    assert _channel_status(False, False, "SMTP timeout") == "failed (SMTP timeout)"


def test_channel_status_pending_when_neither_delivered_nor_errored():
    assert _channel_status(False, False, None) == "pending"


# --- empty feed ---


def test_empty_feed_returns_empty_dataframe_with_expected_columns():
    conn = make_test_db()
    feed = _load_alert_activity_feed(conn)
    assert feed.empty
    assert list(feed.columns) == ALERT_ACTIVITY_COLUMNS


# --- each source table surfaces correctly ---


def test_price_alert_row_present_with_correct_fields():
    from db.price_alert_repository import record_price_alert

    conn = make_test_db()
    record_price_alert(
        conn, ticker="AAPL", alert_type="price_above", price=251.30, previous_price=248.90,
        threshold=250.00, data_date="2026-01-02", source="alpaca", message="AAPL crossed above $250.00",
        dry_run=False,
    )
    from db.price_alert_repository import mark_delivered
    history = _load_alert_activity_feed(conn)
    # mark_delivered needs an alert_id - fetch it back
    alert_id = int(conn.execute("SELECT id FROM price_alerts").fetchone()["id"])
    mark_delivered(conn, alert_id)

    feed = _load_alert_activity_feed(conn)
    assert len(feed) == 1
    row = feed.iloc[0]
    assert row["type"] == "Price Alert"
    assert row["ticker"] == "AAPL"
    assert "AAPL" in row["description"] and "$250.00" in row["description"]
    assert row["email_status"] == "delivered"


def test_volatility_alert_row_present_with_correct_fields():
    from db.volatility_alert_repository import record_volatility_alert

    conn = make_test_db()
    record_volatility_alert(
        conn, ticker="NOW", move_pct=8.0, threshold_percent=5.0, previous_close=124.94, current_close=134.93,
        previous_date="2026-08-12", data_date="2026-08-13", source="alpaca_adjusted",
        message="NOW moved +8.00%", dry_run=False,
    )

    feed = _load_alert_activity_feed(conn)
    assert len(feed) == 1
    row = feed.iloc[0]
    assert row["type"] == "Volatility Alert"
    assert row["ticker"] == "NOW"
    assert "+8.00%" in row["description"]
    assert "5.00%" in row["description"]


def test_daily_digest_row_present_with_correct_fields():
    from db.daily_digest_repository import record_digest_sent

    conn = make_test_db()
    record_digest_sent(conn, "2026-08-16", ticker_count=8, dry_run=False)

    feed = _load_alert_activity_feed(conn)
    assert len(feed) == 1
    row = feed.iloc[0]
    assert row["type"] == "Daily Digest"
    assert row["ticker"] is None
    assert "8 ticker" in row["description"]


def test_operational_notification_row_present_with_correct_fields():
    from db.ops_notification_repository import record_sent

    conn = make_test_db()
    record_sent(
        conn, date(2026, 8, 12), "AAPL: ingestion failed",
        email_delivered=True, email_error=None, discord_delivered=False, discord_error="HTTP 400",
    )

    feed = _load_alert_activity_feed(conn)
    assert len(feed) == 1
    row = feed.iloc[0]
    assert row["type"] == "Operational Failure"
    assert row["ticker"] is None
    assert row["description"] == "AAPL: ingestion failed"
    assert row["email_status"] == "delivered"
    assert row["discord_status"] == "failed (HTTP 400)"


def test_test_send_row_present_with_correct_type_label():
    from db.alert_test_log_repository import ALERT_TYPE_VOLATILITY, record_test_send

    conn = make_test_db()
    record_test_send(conn, ALERT_TYPE_VOLATILITY, True, None, True, None)

    feed = _load_alert_activity_feed(conn)
    assert len(feed) == 1
    row = feed.iloc[0]
    assert row["type"] == "Test Send: Volatility Alert"
    assert row["ticker"] is None


# --- merge/sort across tables ---


def test_feed_combines_all_five_tables_sorted_most_recent_first():
    from db.alert_test_log_repository import ALERT_TYPE_PRICE, record_test_send
    from db.daily_digest_repository import record_digest_sent
    from db.ops_notification_repository import record_sent
    from db.daily_digest_schema import ensure_daily_digest_schema
    from db.price_alerts_schema import ensure_price_alerts_schema
    from db.volatility_alerts_schema import ensure_volatility_alerts_schema

    conn = make_test_db()
    ensure_price_alerts_schema(conn)
    ensure_volatility_alerts_schema(conn)
    ensure_daily_digest_schema(conn)

    conn.execute(
        "INSERT INTO price_alerts (ticker, triggered_at, alert_type, price, previous_price, threshold, "
        "data_date, source, message, dry_run) VALUES ('AAPL', '2026-01-01 09:00:00', 'price_above', "
        "251.3, 248.9, 250.0, '2026-01-01', 'alpaca', 'msg', 0)"
    )
    conn.execute(
        "INSERT INTO volatility_alerts (ticker, triggered_at, move_pct, threshold_percent, previous_close, "
        "current_close, previous_date, data_date, source, message, dry_run) VALUES "
        "('NOW', '2026-01-03 09:00:00', 8.0, 5.0, 124.94, 134.93, '2026-01-02', '2026-01-03', 'alpaca', 'msg', 0)"
    )
    conn.execute(
        "INSERT INTO daily_digest_log (trading_date, sent_at, ticker_count, dry_run) VALUES "
        "('2026-01-02', '2026-01-02 16:35:00', 8, 0)"
    )
    conn.commit()
    # Both record_sent's and record_test_send's `sent_at` default to real
    # "now" (second resolution) - when two DIFFERENT source tables land in
    # the same second there's no cross-table tiebreak (only same-table
    # ties are resolved, via each loader's own `id DESC`), so this test
    # only asserts a strict order among the three rows with distinct,
    # well-separated fixed dates, plus that both "now" rows sort above all
    # three of those.
    record_sent(conn, date(2026, 1, 4), "failure", email_delivered=True, discord_delivered=True)
    record_test_send(conn, ALERT_TYPE_PRICE, True, None, True, None)

    feed = _load_alert_activity_feed(conn)

    assert len(feed) == 5
    now_types = {"Test Send: Price Alert", "Operational Failure"}
    fixed_date_types = ["Volatility Alert", "Daily Digest", "Price Alert"]  # 01-03, 01-02, 01-01 - strict order

    assert set(feed["type"].iloc[:2]) == now_types  # the two "now" rows sort above everything else
    assert list(feed["type"].iloc[2:]) == fixed_date_types

    # strictly descending overall
    timestamps = list(feed["timestamp"])
    assert timestamps == sorted(timestamps, reverse=True)


def test_feed_respects_limit_per_source_table():
    from db.price_alerts_schema import ensure_price_alerts_schema

    conn = make_test_db()
    ensure_price_alerts_schema(conn)
    for i in range(3):
        conn.execute(
            "INSERT INTO price_alerts (ticker, triggered_at, alert_type, price, previous_price, threshold, "
            "data_date, source, message, dry_run) VALUES (?, ?, 'price_above', 1.0, 1.0, 1.0, '2026-01-01', "
            "'alpaca', 'msg', 0)",
            (f"T{i}", f"2026-01-0{i + 1} 09:00:00"),
        )
    conn.commit()

    feed = _load_alert_activity_feed(conn, limit=2)
    price_rows = feed[feed["type"] == "Price Alert"]
    assert len(price_rows) == 2  # per-source limit applied before merge, not after


# --- read-only guarantee ---


def test_aggregation_never_writes_anything():
    """The core "purely a display page" requirement: building the feed
    must never insert/update/delete any row anywhere."""
    from db.price_alert_repository import record_price_alert

    conn = make_test_db()
    record_price_alert(
        conn, ticker="AAPL", alert_type="price_above", price=251.30, previous_price=248.90,
        threshold=250.00, data_date="2026-01-02", source="alpaca", message="msg", dry_run=False,
    )

    tables = [
        row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    before = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()] for t in tables}

    _load_alert_activity_feed(conn)
    _load_alert_activity_feed(conn)  # called twice - still no writes

    after = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()] for t in tables}
    assert after == before
