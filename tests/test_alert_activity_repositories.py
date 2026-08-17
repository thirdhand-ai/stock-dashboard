"""Tests for the two repository-layer additions that feed the Alert
Activity page: db/ops_notification_repository.py's new delivery-status
columns/load function, and the new db/alert_test_log_repository.py.
"""
import sqlite3
from datetime import date

from db.alert_test_log_repository import (
    ALERT_TYPE_DIGEST,
    ALERT_TYPE_PRICE,
    load_alert_test_log,
    record_test_send,
)
from db.ops_notification_repository import (
    already_sent_today,
    load_operational_notifications,
    record_sent,
)
from db.schema import init_db


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


# --- db/ops_notification_repository.py: delivery-status columns ---


def test_record_sent_persists_delivery_status_per_channel():
    conn = make_test_db()
    record_sent(
        conn, date(2026, 8, 12), "ingestion failed",
        email_delivered=True, email_error=None, discord_delivered=False, discord_error="HTTP 400",
    )

    history = load_operational_notifications(conn)
    assert len(history) == 1
    row = history.iloc[0]
    assert row["email_delivered"] == 1
    assert row["email_error"] is None
    assert row["discord_delivered"] == 0
    assert row["discord_error"] == "HTTP 400"


def test_record_sent_defaults_to_not_delivered_when_unspecified():
    """Backward-compatible default (matches the pre-existing call shape
    from before delivery-status tracking existed)."""
    conn = make_test_db()
    record_sent(conn, date(2026, 8, 12), "ingestion failed")

    row = load_operational_notifications(conn).iloc[0]
    assert row["email_delivered"] == 0
    assert row["discord_delivered"] == 0


def test_already_sent_today_still_works_after_delivery_status_migration():
    conn = make_test_db()
    record_sent(conn, date(2026, 8, 12), "failure", email_delivered=True)
    assert already_sent_today(conn, date(2026, 8, 12)) is True
    assert already_sent_today(conn, date(2026, 8, 13)) is False


def test_migration_adds_delivery_columns_to_pre_existing_table_without_touching_existing_rows():
    """Simulates a real production DB: operational_notifications already
    has rows from before delivery-status tracking existed (the pre-Phase
    columns only: id, trading_date, sent_at, error_summary). The migration
    must add the 4 new columns without altering the pre-existing row."""
    conn = make_test_db()
    conn.execute(
        "INSERT INTO operational_notifications (trading_date, error_summary) VALUES ('2026-08-12', 'pre-existing failure')"
    )
    conn.commit()

    history = load_operational_notifications(conn)  # the migration under test
    assert len(history) == 1
    row = history.iloc[0]
    assert row["trading_date"] == "2026-08-12"
    assert row["error_summary"] == "pre-existing failure"
    assert row["email_delivered"] == 0
    assert row["discord_delivered"] == 0


def test_load_operational_notifications_most_recent_first():
    conn = make_test_db()
    record_sent(conn, date(2026, 8, 10), "older")
    record_sent(conn, date(2026, 8, 12), "newer")

    history = load_operational_notifications(conn)
    assert list(history["error_summary"]) == ["newer", "older"]


# --- db/alert_test_log_repository.py ---


def test_record_test_send_persists_all_fields():
    conn = make_test_db()
    record_test_send(conn, ALERT_TYPE_PRICE, True, None, False, "webhook 400")

    log = load_alert_test_log(conn)
    assert len(log) == 1
    row = log.iloc[0]
    assert row["alert_type"] == ALERT_TYPE_PRICE
    assert row["email_delivered"] == 1
    assert row["discord_delivered"] == 0
    assert row["discord_error"] == "webhook 400"


def test_load_alert_test_log_most_recent_first_and_multiple_types():
    conn = make_test_db()
    record_test_send(conn, ALERT_TYPE_PRICE, True, None, True, None)
    record_test_send(conn, ALERT_TYPE_DIGEST, True, None, True, None)

    log = load_alert_test_log(conn)
    assert list(log["alert_type"]) == [ALERT_TYPE_DIGEST, ALERT_TYPE_PRICE]


def test_alert_test_log_empty_by_default():
    conn = make_test_db()
    assert load_alert_test_log(conn).empty
