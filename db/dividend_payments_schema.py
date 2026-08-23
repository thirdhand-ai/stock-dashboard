"""Schema for dividend_payments - manually-entered dividend payment history
for real_holdings positions (db/real_holdings_schema.py). Manual entry was
a deliberate choice (asked and confirmed): this dashboard has no existing
data source for dividend history (checked ingestion/yfinance_source.py,
alpaca_source.py, finnhub_source.py - none fetch it), and an automated
source can be added later as a separate decision without changing this
table's shape.

Kept OUT of db/schema.py's init_db() (frozen - see
tests/test_ops_prospective_audit_phase16.py::
test_git_diff_never_touches_frozen_or_forbidden_files), lazily created
here, same convention as real_holdings/price_alerts/volatility_alerts.

One row per payment received (not per ticker) - a holding accumulates many
rows over time. `ticker`/`owner` mirror real_holdings' (ticker, owner)
identity (see that module's docstring for why owner is part of the key -
the same ticker can be held separately by more than one owner) so a
payment is always attributable to a specific holding, not just a ticker.
`owner` is stored as '' for "not yet assigned", same convention as
real_holdings, for the same reason (SQLite NULLs are never equal to each
other). `total_received` is stored as entered rather than derived from
amount_per_share * shares-held-at-the-time - manual entry means the
person entering it already knows the actual dollar amount that hit their
account, which is more reliable than back-computing it from a share count
that may have changed between payments (DRIP). `reinvested` records
whether this specific payment was used to buy more shares (DRIP) versus
paid out in cash - independent per payment, since a holding's DRIP
election can change over time.
"""
CREATE_DIVIDEND_PAYMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS dividend_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    pay_date TEXT NOT NULL,
    amount_per_share REAL NOT NULL,
    total_received REAL NOT NULL,
    reinvested INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_DIVIDEND_PAYMENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_dividend_payments_ticker_owner ON dividend_payments(ticker, owner);
"""


def ensure_dividend_payments_schema(conn) -> None:
    """Idempotent - safe to call on every read/write, same convention as
    db/real_holdings_schema.py::ensure_real_holdings_schema."""
    conn.execute(CREATE_DIVIDEND_PAYMENTS_TABLE)
    conn.execute(CREATE_DIVIDEND_PAYMENTS_INDEX)
    conn.commit()
