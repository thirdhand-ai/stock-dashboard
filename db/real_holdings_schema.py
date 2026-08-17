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

One row per ticker (UNIQUE). `shares`/`cost_basis_total` are nullable -
a position whose real quantity or cost basis isn't known yet (e.g.
awarded shares still needing manual entry) gets `needs_manual_entry=1`
and NULLs rather than a fabricated number; trading/real_holdings.py shows
those as "-" instead of a computed gain/loss. `realized_gain` is a plain
dollar amount from any partial sale (e.g. shares sold at a gain while
some of the original lot is still held) - entered directly, not derived,
since this system never observed the actual trade.
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
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_REAL_HOLDINGS_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_real_holdings_ticker ON real_holdings(ticker);
"""


def ensure_real_holdings_schema(conn) -> None:
    """Idempotent - safe to call on every read/write, same convention as
    db/price_alerts_schema.py::ensure_price_alerts_schema."""
    conn.execute(CREATE_REAL_HOLDINGS_TABLE)
    conn.execute(CREATE_REAL_HOLDINGS_INDEX)
    conn.commit()
