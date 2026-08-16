"""Schema for volatility_alert_config / volatility_alert_state /
volatility_alerts - kept OUT of db/schema.py's init_db()/db/database.py's
db_session(), same convention as db/price_alerts_schema.py (see that
module's docstring for why: lazily created on first use by this feature's
own read/write functions, never referenced by db/schema.py).

This is a deliberately separate concept from price_alert_config/price_alerts
(db/price_alerts_schema.py), not a third PriceThreshold mode: a
day-over-day % move doesn't resolve to a static above/below dollar level
the way a baseline-percent threshold does (see alerts/price_config.py's
resolve_percent_band, which anchors to a fixed baseline price) - the
"previous" side of a volatility comparison is always yesterday's close,
recomputed fresh every evaluation, never a value stored once and reused.

`volatility_alert_config` is a single row per ticker holding the configured
day-over-day move threshold (in percent). A ticker with no row here is
never evaluated for a volatility alert - same contract price_alert_config
has for price thresholds. `volatility_alert_state` tracks, per ticker, the
last evaluated data_date that actually fired an alert (`last_alert_date`)
plus the wall-clock cooldown timestamp (`last_alert_at`) - see
alerts/volatility_runner.py for how both are used together to avoid
re-firing on every re-evaluation of the same trading day's move.
`volatility_alerts` is the event log (one row per triggered alert, dry-run
or real), delivered over the same two independent channels price_alerts
uses - email and Discord, each tracked in its own columns.
"""
CREATE_VOLATILITY_ALERT_CONFIG_TABLE = """
CREATE TABLE IF NOT EXISTS volatility_alert_config (
    ticker TEXT PRIMARY KEY,
    threshold_percent REAL NOT NULL
);
"""

CREATE_VOLATILITY_ALERT_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS volatility_alert_state (
    ticker TEXT PRIMARY KEY,
    last_checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_alert_at TEXT,
    last_alert_data_date TEXT
);
"""

CREATE_VOLATILITY_ALERTS_TABLE = """
CREATE TABLE IF NOT EXISTS volatility_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    triggered_at TEXT NOT NULL DEFAULT (datetime('now')),
    move_pct REAL NOT NULL,
    threshold_percent REAL NOT NULL,
    previous_close REAL,
    current_close REAL NOT NULL,
    previous_date TEXT,
    data_date TEXT,
    source TEXT,
    message TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 1,
    delivered INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    delivery_error TEXT,
    discord_delivered INTEGER NOT NULL DEFAULT 0,
    discord_delivered_at TEXT,
    discord_delivery_error TEXT
);
"""

CREATE_VOLATILITY_ALERTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_volatility_alerts_ticker_triggered ON volatility_alerts(ticker, triggered_at);
"""


def ensure_volatility_alerts_schema(conn) -> None:
    """Create volatility_alert_config/volatility_alert_state/volatility_alerts
    if they don't exist yet. Idempotent - safe to call on every read/write,
    same convention as db/price_alerts_schema.py::ensure_price_alerts_schema."""
    conn.execute(CREATE_VOLATILITY_ALERT_CONFIG_TABLE)
    conn.execute(CREATE_VOLATILITY_ALERT_STATE_TABLE)
    conn.execute(CREATE_VOLATILITY_ALERTS_TABLE)
    conn.execute(CREATE_VOLATILITY_ALERTS_INDEX)
    conn.commit()
