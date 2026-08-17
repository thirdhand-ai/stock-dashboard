"""CRUD interface for daily_digest_config - the single on/off toggle for
the daily digest feature. Dashboard-editable (dashboard/views/
daily_digest_config.py) so flipping it takes effect on the next automation
run without a code deploy - same contract as db/price_alert_config_repository.py
and db/volatility_alert_config_repository.py, just a single global row
instead of one row per ticker (the digest covers every tracked ticker at
once, not a configurable subset).
"""
from db.daily_digest_schema import ensure_daily_digest_schema


def get_digest_enabled(conn) -> bool:
    """False (off) if never configured - the digest is opt-in, same
    fail-safe default posture as alerts/ops_notifications.py's
    OPERATIONAL_ALERTS_ENABLED."""
    ensure_daily_digest_schema(conn)
    row = conn.execute("SELECT enabled FROM daily_digest_config WHERE id = 1").fetchone()
    if row is None:
        return False
    return bool(row["enabled"])


def set_digest_enabled(conn, enabled: bool) -> None:
    """Explicit, user-triggered write from dashboard/views/
    daily_digest_config.py's toggle - never called on page load."""
    ensure_daily_digest_schema(conn)
    conn.execute(
        """
        INSERT INTO daily_digest_config (id, enabled) VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET enabled = excluded.enabled
        """,
        (int(enabled),),
    )
    conn.commit()
