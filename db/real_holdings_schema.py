"""Schema for real_holdings - actual, real-money positions (some held by
Tyler's mother), tracked entirely separately from the Alpaca-paper-account
simulation trading/portfolio.py builds on (see that module's docstring:
"built from the real Alpaca paper account"). Nothing here ever touches
Alpaca, paper_orders, or portfolio_snapshots - this is a manually-entered
cost-basis ledger, not a trading engine.

Kept OUT of db/schema.py's init_db() (frozen - see
tests/test_ops_prospective_audit_phase16.py::
test_git_diff_never_touches_frozen_or_forbidden_files), lazily created
here, same convention as every alert table in this codebase.

One row per (ticker, owner) pair (UNIQUE) - the same ticker can appear
more than once if held separately by more than one owner (e.g. NOW: one
row for Tyler's mother's 150-share lot, a separate row for Tyler's own,
unconfirmed lot). `owner` is stored as `''` rather than NULL when not yet
assigned - SQLite treats every NULL as distinct from every other NULL for
UNIQUE-index purposes, which would silently defeat the (ticker, owner)
uniqueness constraint for not-yet-assigned rows; `''` behaves like any
other ordinary value instead. db/real_holdings_repository.py's
_normalize_owner() is the only place that should ever write to this
column, so this convention can't drift.

`shares`/`cost_basis_total` are nullable - a position whose real quantity
or cost basis isn't known yet (e.g. awarded shares still needing manual
entry) gets `needs_manual_entry=1` and NULLs rather than a fabricated
number; trading/real_holdings.py shows those as "-" instead of a computed
gain/loss. `realized_gain` is a plain dollar amount from any partial sale
(e.g. shares sold at a gain while some of the original lot is still held)
- entered directly, not derived, since this system never observed the
actual trade.

`share_history_caveat` is nullable free text, set only when this
holding's CURRENT share count is known to have changed over time at an
UNDATED point (e.g. KMI/HPI's ongoing DRIP reinvestment growth - no
dividend_payments rows exist with dates precise enough to reconstruct
when each share was added). trading/portfolio_history.py's historical
value chart applies today's share count across a ticker's full stored
price history (the best available estimate, absent dated events) but
flags any ticker with this set as approximate rather than presenting a
precise-looking line - the same "never fabricate, flag the gap" rule
needs_manual_entry already follows. A ticker whose only share-count
change is a partial SALE (e.g. META) doesn't need this field - that's
detected automatically from a realized_sales row with sale_date IS NULL,
since that's already a structured, dated table; this field exists only
for the growth case (DRIP), which no structured/dated table currently
captures at all.
"""
CREATE_REAL_HOLDINGS_TABLE = """
CREATE TABLE IF NOT EXISTS real_holdings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    owner TEXT,
    shares REAL,
    cost_basis_total REAL,
    realized_gain REAL NOT NULL DEFAULT 0,
    needs_manual_entry INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    share_history_caveat TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_REAL_HOLDINGS_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_real_holdings_ticker_owner ON real_holdings(ticker, owner);
"""


def _migrate_add_share_history_caveat_column(conn) -> None:
    """Additive: real_holdings gets share_history_caveat for holdings whose
    current share count reflects undated growth (DRIP) this system can't
    reconstruct precisely - see this module's docstring. A pre-existing
    DB's `CREATE TABLE IF NOT EXISTS` won't add a column to an
    already-created table. Pre-existing rows get NULL - never backfilled
    with a guess, same convention as every other additive migration in
    this codebase (e.g. db/price_alerts_schema.py's suppressed_reason)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(real_holdings)").fetchall()}
    if "share_history_caveat" not in cols:
        conn.execute("ALTER TABLE real_holdings ADD COLUMN share_history_caveat TEXT")
        conn.commit()


def _migrate_ticker_only_unique_index_to_ticker_owner(conn) -> None:
    """Additive/corrective: earlier versions of this table had a UNIQUE
    index on `ticker` alone (one row per ticker, full stop), which cannot
    represent the same ticker split across two owners. Backfills any
    NULL owner to '' first (see this module's docstring for why - a
    pre-existing DB always has exactly one row per ticker at this point,
    so this backfill can never create a (ticker, owner) collision), then
    drops the old ticker-only index if present. CREATE_REAL_HOLDINGS_INDEX
    (called right after this, in ensure_real_holdings_schema) creates the
    new compound index. Idempotent - DROP INDEX IF EXISTS is a no-op once
    already migrated."""
    conn.execute("UPDATE real_holdings SET owner = '' WHERE owner IS NULL")
    conn.execute("DROP INDEX IF EXISTS idx_real_holdings_ticker")
    conn.commit()


def ensure_real_holdings_schema(conn) -> None:
    """Idempotent - safe to call on every read/write, same convention as
    db/price_alerts_schema.py::ensure_price_alerts_schema."""
    conn.execute(CREATE_REAL_HOLDINGS_TABLE)
    conn.commit()
    _migrate_ticker_only_unique_index_to_ticker_owner(conn)
    conn.execute(CREATE_REAL_HOLDINGS_INDEX)
    conn.commit()
    _migrate_add_share_history_caveat_column(conn)
