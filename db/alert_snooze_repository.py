"""Read/write interface for alert_snooze - see db/alert_snooze_schema.py's
docstring for why price and volatility snoozes share one table.

alerts/price_runner.py and alerts/volatility_runner.py call
get_active_snooze() once per fired evaluation, AFTER persisting the alert
row exactly as if delivery were about to happen - a snoozed ticker is
still evaluated and logged as normal, only the send_email_alert/
send_discord_alert calls themselves are skipped in favor of
db/price_alert_repository.py's/db/volatility_alert_repository.py's
mark_suppressed_by_snooze.
"""
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from db.alert_snooze_schema import ensure_alert_snooze_schema
from db.alert_test_log_repository import ALERT_TYPE_PRICE, ALERT_TYPE_VOLATILITY  # noqa: F401 (re-exported)


@dataclass(frozen=True)
class ActiveSnooze:
    id: int
    alert_type: str
    ticker: Optional[str]  # None = applies to every ticker under this alert_type
    snoozed_until: str     # UTC, "%Y-%m-%d %H:%M:%S" - comparable directly against SQLite's datetime('now')

    @property
    def is_global(self) -> bool:
        return self.ticker is None


def create_snooze(conn, alert_type: str, ticker: Optional[str], snoozed_until: str) -> int:
    """ticker=None snoozes every ticker under this alert_type. snoozed_until
    must already be a future UTC "%Y-%m-%d %H:%M:%S" string - see
    alerts/snooze.py for how the dashboard resolves a preset/custom pick to
    that format before calling this; this function does no validation of
    its own, same division of labor db/price_alert_config_repository.py
    keeps from alerts/price_config.py's resolve_percent_band."""
    ensure_alert_snooze_schema(conn)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO alert_snooze (alert_type, ticker, snoozed_until) VALUES (?, ?, ?)",
        (alert_type, ticker, snoozed_until),
    )
    conn.commit()
    return cur.lastrowid


def delete_snooze(conn, snooze_id: int) -> None:
    """Manual unsnooze - removes the row outright rather than mutating an
    'active' flag. Deleting an already-expired or already-deleted id is a
    harmless no-op (0 rows affected)."""
    ensure_alert_snooze_schema(conn)
    conn.execute("DELETE FROM alert_snooze WHERE id = ?", (snooze_id,))
    conn.commit()


def list_active_snoozes(conn, alert_type: Optional[str] = None) -> pd.DataFrame:
    """Every currently-active (not yet expired) snooze, soonest-expiring
    first. Expiry is a live SQL comparison against datetime('now') - an
    expired row simply stops appearing here on its own; nothing deletes it
    (harmless clutter, same tradeoff price_alert_state keeps forever)."""
    ensure_alert_snooze_schema(conn)
    query = (
        "SELECT id, alert_type, ticker, snoozed_until, created_at FROM alert_snooze "
        "WHERE snoozed_until > datetime('now')"
    )
    params = ()
    if alert_type:
        query += " AND alert_type = ?"
        params = (alert_type,)
    query += " ORDER BY snoozed_until ASC"
    return pd.read_sql_query(query, conn, params=params)


def get_active_snooze(conn, alert_type: str, ticker: str) -> Optional[ActiveSnooze]:
    """The snooze currently suppressing delivery for this ticker under this
    alert_type, if any - a ticker-specific snooze takes precedence over a
    global (ticker IS NULL) one covering the same alert_type. Returns None
    once snoozed_until has passed, with no unsnooze action required."""
    ensure_alert_snooze_schema(conn)
    row = conn.execute(
        """
        SELECT id, alert_type, ticker, snoozed_until FROM alert_snooze
        WHERE alert_type = ? AND (ticker = ? OR ticker IS NULL) AND snoozed_until > datetime('now')
        ORDER BY (ticker IS NULL) ASC, snoozed_until DESC
        LIMIT 1
        """,
        (alert_type, ticker),
    ).fetchone()
    if row is None:
        return None
    return ActiveSnooze(id=row[0], alert_type=row[1], ticker=row[2], snoozed_until=row[3])
