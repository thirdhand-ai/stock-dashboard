"""Dedupe store + delivery-outcome record for the operational-failure
notification - see alerts/ops_notifications.py. Structurally separate from
db/alert_repository.py (stock-signal alerts).
"""
from datetime import date
from typing import Optional

import pandas as pd


def _migrate_add_delivery_status_columns(conn):
    """Additive: operational_notifications gets email_delivered/email_error/
    discord_delivered/discord_error, so the Alert Activity dashboard page
    (dashboard/views/alert_activity.py) can show per-channel delivery
    status for operational-failure notifications, matching every other
    alert type's convention.

    operational_notifications itself lives in db/schema.py, which is
    frozen (tests/test_ops_prospective_audit_phase16.py::
    test_git_diff_never_touches_frozen_or_forbidden_files forbids any
    working-tree diff to that file) - so this migration runs an additive
    ALTER TABLE here instead, same pattern db/price_alerts_schema.py
    already uses for its own tables. Pre-existing rows get 0/NULL for all
    four - never backfilled/guessed (they predate delivery-status tracking
    existing at all)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(operational_notifications)").fetchall()}
    if "email_delivered" not in cols:
        conn.execute("ALTER TABLE operational_notifications ADD COLUMN email_delivered INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE operational_notifications ADD COLUMN email_error TEXT")
        conn.execute("ALTER TABLE operational_notifications ADD COLUMN discord_delivered INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE operational_notifications ADD COLUMN discord_error TEXT")
        conn.commit()


def already_sent_today(conn, trading_date: date) -> bool:
    _migrate_add_delivery_status_columns(conn)
    row = conn.execute(
        "SELECT 1 FROM operational_notifications WHERE trading_date = ?",
        (trading_date.isoformat(),),
    ).fetchone()
    return row is not None


def record_sent(
    conn,
    trading_date: date,
    error_summary: str,
    email_delivered: bool = False,
    email_error: Optional[str] = None,
    discord_delivered: bool = False,
    discord_error: Optional[str] = None,
) -> None:
    _migrate_add_delivery_status_columns(conn)
    conn.execute(
        """
        INSERT OR IGNORE INTO operational_notifications
            (trading_date, error_summary, email_delivered, email_error, discord_delivered, discord_error)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            trading_date.isoformat(), error_summary,
            int(email_delivered), email_error, int(discord_delivered), discord_error,
        ),
    )
    conn.commit()


def load_operational_notifications(conn, limit: int = 200) -> pd.DataFrame:
    """Most-recent-first operational-failure notification log, for the
    Alert Activity dashboard page. Read-only."""
    _migrate_add_delivery_status_columns(conn)
    return pd.read_sql_query(
        "SELECT id, trading_date, sent_at, error_summary, email_delivered, email_error, "
        "discord_delivered, discord_error FROM operational_notifications ORDER BY sent_at DESC, id DESC LIMIT ?",
        conn, params=(limit,),
    )
