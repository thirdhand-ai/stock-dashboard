"""Schema for realized_sales - per-transaction sale/partial-sale records
for real_holdings positions, backing the Realized Gains report
(trading/realized_gains.py, dashboard/views/realized_gains.py).

This is new, transaction-level detail real_holdings itself never tracked:
real_holdings.realized_gain (db/real_holdings_schema.py) is a single,
directly-entered dollar amount per (ticker, owner) - accurate as a
running total, but not a per-sale record with its own proceeds, cost
basis, or dates. Nothing here changes real_holdings; this is purely
additive, backfilled from whatever a prior sale's numbers already imply
(see the META backfill in this module's seed data, added via
add_realized_sale directly - not auto-derived from real_holdings.realized_gain,
since a single aggregate number can't be un-summed into shares/proceeds/
cost-basis without already knowing them from elsewhere).

Kept OUT of db/schema.py's init_db() (frozen - see
tests/test_ops_prospective_audit_phase16.py::
test_git_diff_never_touches_frozen_or_forbidden_files), lazily created
here, same convention as every other supplementary table.

`purchase_date`/`sale_date` are BOTH nullable - checked what's actually
stored for the existing META sale before building this (db/
real_holdings_repository.py::RealHolding has no date field at all, and
the free-text note on that row names no dates either), so a real,
already-known transaction can have neither date on record. A row with a
missing date shows "Unknown" and skips short/long-term classification
and tax-year bucketing rather than guessing - see trading/
realized_gains.py's docstring. `realized_gain` is deliberately NOT a
column here - it's proceeds minus cost_basis_sold, always derived, never
stored, so it can't drift out of sync with those two entered numbers (a
different tradeoff than real_holdings.realized_gain, which predates this
table and was entered directly because proceeds/cost-basis-sold weren't
tracked separately at the time).
"""
CREATE_REALIZED_SALES_TABLE = """
CREATE TABLE IF NOT EXISTS realized_sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    purchase_date TEXT,
    sale_date TEXT,
    shares_sold REAL NOT NULL,
    cost_basis_sold REAL NOT NULL,
    proceeds REAL NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_REALIZED_SALES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_realized_sales_ticker_owner ON realized_sales(ticker, owner);
"""


def ensure_realized_sales_schema(conn) -> None:
    """Idempotent - safe to call on every read/write, same convention as
    db/real_holdings_schema.py::ensure_real_holdings_schema."""
    conn.execute(CREATE_REALIZED_SALES_TABLE)
    conn.execute(CREATE_REALIZED_SALES_INDEX)
    conn.commit()
