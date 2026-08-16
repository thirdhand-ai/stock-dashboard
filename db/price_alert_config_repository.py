"""CRUD interface for price_alert_config - the per-ticker above/below
price-alert thresholds. Previously a hardcoded PRICE_THRESHOLDS constant in
alerts/price_config.py; now dashboard-editable (dashboard/views/
price_alert_config.py) so a threshold change takes effect on the next
automation run without a code deploy. Structurally separate from
db/price_alert_repository.py (the alert log + per-ticker state) - this
module only manages the threshold *definitions* themselves.

Table lives in db/price_alerts_schema.py, lazily created here - same
convention every function in db/price_alert_repository.py already uses.
"""
from typing import List, Optional

from alerts.price_config import PriceThreshold
from db.price_alerts_schema import ensure_price_alerts_schema


def _row_to_threshold(row) -> PriceThreshold:
    return PriceThreshold(ticker=row["ticker"], above=row["above"], below=row["below"])


def list_price_alert_configs(conn) -> List[PriceThreshold]:
    """All configured thresholds, ticker-ascending. A ticker with no row
    here is simply never evaluated for a price alert - same contract the
    old hardcoded PRICE_THRESHOLDS list had."""
    ensure_price_alerts_schema(conn)
    rows = conn.execute(
        "SELECT ticker, above, below FROM price_alert_config ORDER BY ticker"
    ).fetchall()
    return [_row_to_threshold(row) for row in rows]


def get_price_alert_config(conn, ticker: str) -> Optional[PriceThreshold]:
    """A single ticker's configured threshold, or None if it has none."""
    ensure_price_alerts_schema(conn)
    row = conn.execute(
        "SELECT ticker, above, below FROM price_alert_config WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_threshold(row)


def upsert_price_alert_config(
    conn,
    ticker: str,
    above: Optional[float] = None,
    below: Optional[float] = None,
) -> None:
    """Add a new ticker's threshold, or overwrite an existing one's - the
    same operation handles both add and edit, since this is a
    single-row-per-ticker table."""
    ensure_price_alerts_schema(conn)
    conn.execute(
        """
        INSERT INTO price_alert_config (ticker, above, below)
        VALUES (?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            above = excluded.above, below = excluded.below
        """,
        (ticker, above, below),
    )
    conn.commit()


def delete_price_alert_config(conn, ticker: str) -> None:
    """Remove a ticker's threshold - it will simply no longer be evaluated
    for a price alert on the next run. Does NOT touch price_alert_state or
    price_alerts history for that ticker; historical records are never
    deleted as a side effect of removing a threshold."""
    ensure_price_alerts_schema(conn)
    conn.execute("DELETE FROM price_alert_config WHERE ticker = ?", (ticker,))
    conn.commit()
