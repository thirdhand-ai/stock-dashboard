"""Read helpers for turning stored price rows into indicator-ready DataFrames."""
import pandas as pd

# Preference order when a ticker has price rows from more than one source.
SOURCE_PRIORITY = ["yfinance", "alpaca"]

PRICE_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def load_price_history(conn, ticker, source=None):
    """Return an ascending-by-date DataFrame with columns date/open/high/low/close/volume.

    If `source` is omitted, picks the source with the most stored rows for
    this ticker (preferring SOURCE_PRIORITY on ties/availability).
    """
    if source is None:
        source = resolve_source(conn, ticker)
        if source is None:
            return pd.DataFrame(columns=PRICE_COLUMNS)

    return pd.read_sql_query(
        """
        SELECT date, open, high, low, close, volume
        FROM prices
        WHERE ticker = ? AND source = ?
        ORDER BY date ASC
        """,
        conn,
        params=(ticker, source),
    )


def get_latest_fetched_at(conn, ticker, source=None):
    """When the most recent stored price row for this ticker was actually
    written (db-side fetched_at, not the market date) - used to show
    dashboard staleness (A10) distinctly from the row's own `date` field,
    which can be identical across a stale vs. a freshly-reconfirmed row."""
    if source is None:
        source = resolve_source(conn, ticker)
        if source is None:
            return None
    row = conn.execute(
        "SELECT fetched_at FROM prices WHERE ticker = ? AND source = ? ORDER BY date DESC, fetched_at DESC LIMIT 1",
        (ticker, source),
    ).fetchone()
    return row["fetched_at"] if row else None


def resolve_source(conn, ticker):
    """Which stored source load_price_history() would use for this ticker
    (or None if there's no price data at all). Exposed publicly so callers
    that need to *display* the source (dashboard, verification scripts)
    don't have to duplicate this preference logic."""
    rows = conn.execute(
        "SELECT source, COUNT(*) as n FROM prices WHERE ticker = ? GROUP BY source",
        (ticker,),
    ).fetchall()
    if not rows:
        return None
    counts = {r["source"]: r["n"] for r in rows}
    for preferred in SOURCE_PRIORITY:
        if preferred in counts:
            return preferred
    return max(counts, key=counts.get)
