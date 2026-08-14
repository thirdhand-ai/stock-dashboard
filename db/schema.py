"""SQLite schema definitions: price/news/fundamentals data (Phase 1), the
alert log and alert-state tables (Phase 4/5), plus automation run history
(Phase 6)."""

CREATE_PRICES_TABLE = """
CREATE TABLE IF NOT EXISTS prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ticker, date, source)
);
"""

CREATE_PRICES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_prices_ticker_date ON prices(ticker, date);
"""

CREATE_NEWS_TABLE = """
CREATE TABLE IF NOT EXISTS news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT,
    source TEXT,
    url TEXT,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ticker, url)
);
"""

CREATE_FUNDAMENTALS_TABLE = """
CREATE TABLE IF NOT EXISTS fundamentals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    as_of TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ticker, metric, as_of)
);
"""

# alerts: one row per triggered (dry-run or real) alert event.
CREATE_ALERTS_TABLE = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    triggered_at TEXT NOT NULL DEFAULT (datetime('now')),
    alert_type TEXT NOT NULL,
    score REAL NOT NULL,
    previous_score REAL,
    highest_confirmed_stage TEXT NOT NULL,
    previous_stage TEXT,
    data_date TEXT,
    source TEXT,
    message TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 1,
    delivered INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    delivery_error TEXT
);
"""

CREATE_ALERTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_alerts_ticker_triggered ON alerts(ticker, triggered_at);
"""

# alert_state: one row per ticker, the last-observed score/stage, updated on
# every evaluation (not just when an alert fires). This is what makes
# crossing-detection possible - comparing "now" against "last time we
# checked", not against "last time we alerted".
CREATE_ALERT_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS alert_state (
    ticker TEXT PRIMARY KEY,
    last_score REAL,
    last_stage TEXT,
    last_checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_alert_at TEXT
);
"""

# automation_runs: one row per automation pipeline execution (Phase 6),
# enough to diagnose scheduled runs after the fact without a log-scraping
# tool. Never holds credentials/webhook values - only counts and short,
# sanitized error summaries.
CREATE_AUTOMATION_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS automation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    send_mode TEXT NOT NULL,
    tickers_attempted INTEGER NOT NULL DEFAULT 0,
    tickers_updated INTEGER NOT NULL DEFAULT 0,
    tickers_failed INTEGER NOT NULL DEFAULT 0,
    alerts_generated INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT
);
"""

CREATE_AUTOMATION_RUNS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_automation_runs_started ON automation_runs(started_at);
"""

# alert_state_corrections: audit trail for any manual/scripted correction to
# alert_state after an incident investigation (e.g. the 2026-08-12 DNS
# outage - see scripts/audit_2026_08_12_incident.py). Append-only; a
# correction with old_value == new_value documents a "verified, no change
# needed" finding rather than an actual value change.
CREATE_ALERT_STATE_CORRECTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS alert_state_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corrected_at TEXT NOT NULL DEFAULT (datetime('now')),
    ticker TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason TEXT NOT NULL
);
"""

CREATE_ALERT_STATE_CORRECTIONS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_alert_state_corrections_ticker ON alert_state_corrections(ticker, corrected_at);
"""

# operational_notifications: dedupe record for the (disabled-by-default)
# operational-failure Discord notification in alerts/ops_notifications.py -
# structurally separate from `alerts` (stock-signal alerts). One row per
# trading_date a notification was sent, so a day with multiple failed runs
# still only notifies once. Never holds credentials/webhook values.
CREATE_OPERATIONAL_NOTIFICATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS operational_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_date TEXT NOT NULL UNIQUE,
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    error_summary TEXT
);
"""

# paper_orders: one row per proposed/submitted paper-trading order intent
# (Phase 7). client_order_id is deterministic (see trading/idempotency.py) so
# a repeated evaluation on unchanged data updates the same row instead of
# inserting a duplicate - that uniqueness constraint IS the idempotency
# guarantee, not just documentation. Never holds API keys/secrets.
CREATE_PAPER_ORDERS_TABLE = """
CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    intent TEXT NOT NULL,
    qty REAL,
    notional REAL,
    signal_score REAL,
    confirmed_stage TEXT,
    reason TEXT NOT NULL,
    reference_price REAL,
    client_order_id TEXT NOT NULL UNIQUE,
    alpaca_order_id TEXT,
    status TEXT NOT NULL,
    risk_checks TEXT,
    rejection_reason TEXT,
    environment TEXT NOT NULL DEFAULT 'paper',
    submitted_at TEXT,
    filled_at TEXT,
    filled_qty REAL,
    filled_avg_price REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_PAPER_ORDERS_TICKER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_paper_orders_ticker ON paper_orders(ticker, created_at);
"""

CREATE_PAPER_ORDERS_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_paper_orders_status ON paper_orders(status);
"""

# portfolio_snapshots: point-in-time captures of the real Alpaca paper
# account, so equity history can be charted from locally captured data
# rather than only showing Alpaca's current value (Phase 7).
CREATE_PORTFOLIO_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    buying_power REAL NOT NULL,
    long_market_value REAL NOT NULL,
    invested_exposure_pct REAL NOT NULL,
    unrealized_pl REAL NOT NULL,
    open_position_count INTEGER NOT NULL,
    environment TEXT NOT NULL DEFAULT 'paper'
);
"""

CREATE_PORTFOLIO_SNAPSHOTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_captured ON portfolio_snapshots(captured_at);
"""

ALL_STATEMENTS = [
    CREATE_PRICES_TABLE,
    CREATE_PRICES_INDEX,
    CREATE_NEWS_TABLE,
    CREATE_FUNDAMENTALS_TABLE,
    CREATE_ALERTS_TABLE,
    CREATE_ALERTS_INDEX,
    CREATE_ALERT_STATE_TABLE,
    CREATE_AUTOMATION_RUNS_TABLE,
    CREATE_AUTOMATION_RUNS_INDEX,
    CREATE_ALERT_STATE_CORRECTIONS_TABLE,
    CREATE_ALERT_STATE_CORRECTIONS_INDEX,
    CREATE_OPERATIONAL_NOTIFICATIONS_TABLE,
    CREATE_PAPER_ORDERS_TABLE,
    CREATE_PAPER_ORDERS_TICKER_INDEX,
    CREATE_PAPER_ORDERS_STATUS_INDEX,
    CREATE_PORTFOLIO_SNAPSHOTS_TABLE,
    CREATE_PORTFOLIO_SNAPSHOTS_INDEX,
]


def _migrate_pre_phase5_alerts_table(conn):
    """The Phase 4 `alerts` table predates alert_type/previous_score/dry_run
    etc. `CREATE TABLE IF NOT EXISTS` won't add columns to an existing
    table, so rebuild it once. Safe because Phase 4 never wrote any rows to
    it - this only ever fires against an empty table."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    if cols and "alert_type" not in cols:
        conn.execute("DROP TABLE alerts")


def _migrate_add_trading_date_to_automation_runs(conn):
    """Additive: automation_runs gets a nullable trading_date column so
    'was there a successful run FOR trading day X' can be an exact match
    instead of string-matching a UTC timestamp against a local calendar
    date (Phase 13 spec §5.1). Pre-existing rows get trading_date=NULL -
    never backfilled/guessed."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(automation_runs)").fetchall()}
    if "trading_date" not in cols:
        conn.execute("ALTER TABLE automation_runs ADD COLUMN trading_date TEXT")
        conn.commit()


def init_db(conn):
    """Create all tables if they don't already exist."""
    _migrate_pre_phase5_alerts_table(conn)
    cur = conn.cursor()
    for statement in ALL_STATEMENTS:
        cur.execute(statement)
    conn.commit()
    _migrate_add_trading_date_to_automation_runs(conn)
