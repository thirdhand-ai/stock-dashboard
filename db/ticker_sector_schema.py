"""Schema for ticker_sector - cached industry classification per ticker,
used by trading/concentration.py's sector/industry allocation view.

Sourced from Finnhub's company_profile2 endpoint (finnhubIndustry field) -
an already-integrated data source (FINNHUB_API_KEY already configured,
the same finnhub.Client already used by ingestion/finnhub_source.py's
fetch_fundamentals/fetch_next_earnings_date), just one more read-only
endpoint call on it, not a new provider/dependency/account. Finnhub's
free tier here returns a single broad industry label (e.g. "Technology"),
not a full GICS sector+industry hierarchy - that's what's actually
available on this account's plan, same "documented limitation, not a
fabrication" convention research/fundamentals.py uses for
ANALYST_TARGET_UNAVAILABLE_REASON.

Kept OUT of db/schema.py's init_db() (frozen - see
tests/test_ops_prospective_audit_phase16.py::
test_git_diff_never_touches_frozen_or_forbidden_files), lazily created
here, same convention as every other supplementary table.

One row per ticker - industry classification is static reference data
(doesn't change day to day the way price does), so this is fetched once
per ticker and cached indefinitely rather than refreshed on a schedule;
see trading/concentration.py::ensure_sector_data for the fetch-if-missing
logic. `industry` is nullable - a ticker Finnhub has no classification
for (or a failed lookup) is stored as NULL and displayed as "Unknown",
never guessed.
"""
CREATE_TICKER_SECTOR_TABLE = """
CREATE TABLE IF NOT EXISTS ticker_sector (
    ticker TEXT PRIMARY KEY,
    industry TEXT,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def ensure_ticker_sector_schema(conn) -> None:
    """Idempotent - safe to call on every read/write, same convention as
    db/real_holdings_schema.py::ensure_real_holdings_schema."""
    conn.execute(CREATE_TICKER_SECTOR_TABLE)
    conn.commit()
