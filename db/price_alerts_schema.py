"""Schema for price_alerts / price_alert_state / price_alert_config -
deliberately kept OUT of db/schema.py's init_db()/db/database.py's
db_session(). Mirrors strategy_lab/prospective.py's own ensure_schema()
convention exactly: created lazily, on first use, by db/
price_alert_repository.py's and db/price_alert_config_repository.py's
read/write functions - never referenced by db/schema.py or db/database.py
(see tests/test_ops_prospective_audit_phase16.py::
test_git_diff_never_touches_frozen_or_forbidden_files, which durably
forbids any working-tree diff to db/schema.py).

`price_alerts` is an event log (one row per triggered alert, dry-run or
real), delivered over two independent channels each tracked in their own
columns - email (`delivered`/`delivered_at`/`delivery_error`, alerts/
email.py) and Discord (`discord_delivered`/`discord_delivered_at`/
`discord_delivery_error`, alerts/discord.py) - since one channel can fail
while the other succeeds. `price_alert_state` is a single row per ticker
holding the last-observed price, updated on every evaluation - see
alerts/price_engine.py. `price_alert_config` is a single row per ticker
holding the above/below threshold values themselves - previously the
hardcoded PRICE_THRESHOLDS constant in alerts/price_config.py, now
dashboard-editable (dashboard/views/price_alert_config.py) so a threshold
change takes effect on the next automation run without a code deploy. A
ticker with no row here is simply never evaluated for a price alert - same
contract the old hardcoded list had.
"""
CREATE_PRICE_ALERTS_TABLE = """
CREATE TABLE IF NOT EXISTS price_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    triggered_at TEXT NOT NULL DEFAULT (datetime('now')),
    alert_type TEXT NOT NULL,
    price REAL NOT NULL,
    previous_price REAL,
    threshold REAL NOT NULL,
    data_date TEXT,
    source TEXT,
    message TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 1,
    delivered INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    delivery_error TEXT,
    discord_delivered INTEGER NOT NULL DEFAULT 0,
    discord_delivered_at TEXT,
    discord_delivery_error TEXT,
    suppressed_reason TEXT
);
"""

CREATE_PRICE_ALERTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_price_alerts_ticker_triggered ON price_alerts(ticker, triggered_at);
"""

CREATE_PRICE_ALERT_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS price_alert_state (
    ticker TEXT PRIMARY KEY,
    last_price REAL,
    last_checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_alert_at TEXT
);
"""

CREATE_PRICE_ALERT_CONFIG_TABLE = """
CREATE TABLE IF NOT EXISTS price_alert_config (
    ticker TEXT PRIMARY KEY,
    above REAL,
    below REAL,
    mode TEXT NOT NULL DEFAULT 'fixed',
    percent REAL,
    baseline_price REAL
);
"""


def _migrate_add_discord_columns_to_price_alerts(conn):
    """Additive: price_alerts gets discord_delivered/discord_delivered_at/
    discord_delivery_error so Discord delivery (alerts/discord.py) can be
    tracked independently of the existing email delivery columns - a
    pre-existing DB's `CREATE TABLE IF NOT EXISTS` won't add columns to an
    already-created table. Pre-existing rows get discord_delivered=0,
    discord_delivered_at/discord_delivery_error=NULL - never
    backfilled/guessed (they predate Discord delivery existing at all)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(price_alerts)").fetchall()}
    if "discord_delivered" not in cols:
        conn.execute("ALTER TABLE price_alerts ADD COLUMN discord_delivered INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE price_alerts ADD COLUMN discord_delivered_at TEXT")
        conn.execute("ALTER TABLE price_alerts ADD COLUMN discord_delivery_error TEXT")
        conn.commit()


def _migrate_add_percent_mode_columns_to_price_alert_config(conn):
    """Additive: price_alert_config gets mode/percent/baseline_price so a
    ticker can be configured with a percentage band instead of fixed
    above/below dollar values (see alerts/price_config.py's
    resolve_percent_band). A pre-existing DB's `CREATE TABLE IF NOT EXISTS`
    won't add columns to an already-created table. mode's NOT NULL DEFAULT
    'fixed' back-fills every pre-existing row as fixed-dollar mode
    automatically - their above/below values are untouched, so existing
    thresholds keep evaluating exactly as before."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(price_alert_config)").fetchall()}
    if "mode" not in cols:
        conn.execute("ALTER TABLE price_alert_config ADD COLUMN mode TEXT NOT NULL DEFAULT 'fixed'")
        conn.execute("ALTER TABLE price_alert_config ADD COLUMN percent REAL")
        conn.execute("ALTER TABLE price_alert_config ADD COLUMN baseline_price REAL")
        conn.commit()


def _migrate_add_suppressed_reason_column_to_price_alerts(conn):
    """Additive: price_alerts gets suppressed_reason so a snoozed ticker's
    fired-but-undelivered alert (alerts/price_runner.py, when
    db/alert_snooze_repository.py's get_active_snooze finds an active
    snooze) shows as 'suppressed (snoozed)' rather than looking identical
    to a plain dry-run or a delivery failure. Pre-existing rows get NULL -
    they predate the snooze feature existing at all, never backfilled."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(price_alerts)").fetchall()}
    if "suppressed_reason" not in cols:
        conn.execute("ALTER TABLE price_alerts ADD COLUMN suppressed_reason TEXT")
        conn.commit()


def ensure_price_alerts_schema(conn) -> None:
    """Create price_alerts/price_alert_state/price_alert_config if they
    don't exist yet, and apply any additive migration for a pre-existing
    table missing newer columns. Idempotent - safe to call on every
    read/write, same convention as strategy_lab/prospective.py::
    ensure_schema."""
    conn.execute(CREATE_PRICE_ALERTS_TABLE)
    conn.execute(CREATE_PRICE_ALERTS_INDEX)
    conn.execute(CREATE_PRICE_ALERT_STATE_TABLE)
    conn.execute(CREATE_PRICE_ALERT_CONFIG_TABLE)
    conn.commit()
    _migrate_add_discord_columns_to_price_alerts(conn)
    _migrate_add_percent_mode_columns_to_price_alert_config(conn)
    _migrate_add_suppressed_reason_column_to_price_alerts(conn)
