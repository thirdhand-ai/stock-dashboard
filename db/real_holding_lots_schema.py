"""Schema for real_holding_lots - dated, cash-funded share-acquisition
events for real_holdings positions (see db/real_holdings_schema.py's
docstring for what that table tracks). Deliberately separate from
dividend_payments: that table is for dividend income events, reinvested or
not (see db/dividend_payments_schema.py); this table is for share
acquisitions funded some other way - an original brokerage purchase, an
awarded-shares grant, a one-time cash top-up - which were never a dividend
payment.

Kept OUT of db/schema.py's init_db() (frozen - see
tests/test_ops_prospective_audit_phase16.py::
test_git_diff_never_touches_frozen_or_forbidden_files), lazily created
here, same convention as every other manually-entered ledger table in this
codebase (real_holdings, dividend_payments, realized_sales).

Combined with dividend_payments' reinvested=1 rows, this table lets
trading/portfolio_history.py reconstruct a holding's exact share count as
of any past date (a piecewise timeline) instead of applying today's share
count across its full stored price history - see that module's
_reconstruct_share_timeline for the reconstruction logic, and its
docstring for the fallback rule when a holding has no rows here (or the
combined timeline doesn't add up to the currently recorded share count).
"""
CREATE_REAL_HOLDING_LOTS_TABLE = """
CREATE TABLE IF NOT EXISTS real_holding_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    purchase_date TEXT NOT NULL,
    shares REAL NOT NULL,
    cost_per_share REAL NOT NULL,
    total_cost REAL NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_REAL_HOLDING_LOTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_real_holding_lots_ticker_owner ON real_holding_lots(ticker, owner);
"""


def ensure_real_holding_lots_schema(conn) -> None:
    """Idempotent - safe to call on every read/write, same convention as
    db/real_holdings_schema.py::ensure_real_holdings_schema."""
    conn.execute(CREATE_REAL_HOLDING_LOTS_TABLE)
    conn.execute(CREATE_REAL_HOLDING_LOTS_INDEX)
    conn.commit()
