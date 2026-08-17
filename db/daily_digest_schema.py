"""Schema for daily_digest_config / daily_digest_log - deliberately kept
OUT of db/schema.py's init_db()/db/database.py's db_session(), same lazy
on-first-use convention as db/price_alerts_schema.py and db/
volatility_alerts_schema.py.

Structurally SEPARATE from the price-threshold and volatility alert
systems: the daily digest never evaluates a crossing condition and never
suppresses on "nothing changed" - it fires once per trading day
unconditionally (when enabled), win or fail, covering every tracked
ticker's current state. It reuses those two systems' CONFIGURED thresholds
only to report distance-to-threshold, never to decide whether to fire.

`daily_digest_config` is a single-row (id=1) toggle - dashboard-editable
(dashboard/views/daily_digest_config.py), same "takes effect on the next
run, no code deploy" contract as price_alert_config/volatility_alert_config.
`daily_digest_log` is the send-dedup + delivery-outcome record: at most one
digest per trading_date (UNIQUE constraint), same "record even on failure,
never retry-loop a day that can't be reached" contract as
db/ops_notification_repository.py, but with per-channel delivery tracking
like price_alerts/volatility_alerts (a digest is delivered over email +
Discord independently).
"""
CREATE_DAILY_DIGEST_CONFIG_TABLE = """
CREATE TABLE IF NOT EXISTS daily_digest_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 0
);
"""

CREATE_DAILY_DIGEST_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS daily_digest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date TEXT NOT NULL UNIQUE,
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    ticker_count INTEGER NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 1,
    delivered INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    delivery_error TEXT,
    discord_delivered INTEGER NOT NULL DEFAULT 0,
    discord_delivered_at TEXT,
    discord_delivery_error TEXT
);
"""


def ensure_daily_digest_schema(conn) -> None:
    """Create daily_digest_config/daily_digest_log if they don't exist yet.
    Idempotent - safe to call on every read/write, same convention as
    db/price_alerts_schema.py::ensure_price_alerts_schema."""
    conn.execute(CREATE_DAILY_DIGEST_CONFIG_TABLE)
    conn.execute(CREATE_DAILY_DIGEST_LOG_TABLE)
    conn.commit()
