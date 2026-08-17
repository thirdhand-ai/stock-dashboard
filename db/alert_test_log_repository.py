"""Read/write interface for alert_test_log - see db/alert_test_log_schema.py's
docstring for why this is a deliberately separate table from
price_alert_state/volatility_alert_state/daily_digest_log.
"""
from typing import Optional

import pandas as pd

from db.alert_test_log_schema import ensure_alert_test_log_schema

ALERT_TYPE_PRICE = "price_alert"
ALERT_TYPE_VOLATILITY = "volatility_alert"
ALERT_TYPE_DIGEST = "daily_digest"


def record_test_send(
    conn,
    alert_type: str,
    email_delivered: bool,
    email_error: Optional[str],
    discord_delivered: bool,
    discord_error: Optional[str],
) -> int:
    """Called by dashboard/data.py's send_*_test_notification wrappers,
    once per "Send Test Alert" click, AFTER delivery already completed -
    this only records the outcome, it never decides whether to send."""
    ensure_alert_test_log_schema(conn)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO alert_test_log (alert_type, email_delivered, email_error, discord_delivered, discord_error)
        VALUES (?, ?, ?, ?, ?)
        """,
        (alert_type, int(email_delivered), email_error, int(discord_delivered), discord_error),
    )
    conn.commit()
    return cur.lastrowid


def load_alert_test_log(conn, limit: int = 200) -> pd.DataFrame:
    """Most-recent-first test-send log, for the Alert Activity dashboard
    page. Read-only."""
    ensure_alert_test_log_schema(conn)
    return pd.read_sql_query(
        "SELECT id, sent_at, alert_type, email_delivered, email_error, discord_delivered, discord_error "
        "FROM alert_test_log ORDER BY sent_at DESC, id DESC LIMIT ?",
        conn, params=(limit,),
    )
