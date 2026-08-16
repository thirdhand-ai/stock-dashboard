"""CRUD interface for price_alert_config - the per-ticker above/below
price-alert thresholds. Previously a hardcoded PRICE_THRESHOLDS constant in
alerts/price_config.py; now dashboard-editable (dashboard/views/
price_alert_config.py) so a threshold change takes effect on the next
automation run without a code deploy. Structurally separate from
db/price_alert_repository.py (the alert log + per-ticker state) - this
module only manages the threshold *definitions* themselves.

Table lives in db/price_alerts_schema.py, lazily created here - same
convention every function in db/price_alert_repository.py already uses.

A ticker's threshold can be configured in fixed-dollar mode (above/below
entered directly - the original, still-default behavior) or percent mode
(a symmetric band around a baseline_price - see alerts/price_config.py's
resolve_percent_band). Percent mode resolves to concrete above/below values
at write time, right here in upsert_price_alert_config, and stores them in
the same above/below columns fixed mode uses - so every reader of this
table (alerts/price_engine.py, alerts/price_runner.py) needs zero
percent-specific logic; they just see resolved dollar levels either way.
"""
from typing import List, Optional

from alerts.price_config import MODE_FIXED, MODE_PERCENT, PriceThreshold, resolve_percent_band
from db.price_alerts_schema import ensure_price_alerts_schema


def _row_to_threshold(row) -> PriceThreshold:
    return PriceThreshold(
        ticker=row["ticker"], above=row["above"], below=row["below"],
        mode=row["mode"] or MODE_FIXED, percent=row["percent"], baseline_price=row["baseline_price"],
    )


def list_price_alert_configs(conn) -> List[PriceThreshold]:
    """All configured thresholds, ticker-ascending. A ticker with no row
    here is simply never evaluated for a price alert - same contract the
    old hardcoded PRICE_THRESHOLDS list had."""
    ensure_price_alerts_schema(conn)
    rows = conn.execute(
        "SELECT ticker, above, below, mode, percent, baseline_price FROM price_alert_config ORDER BY ticker"
    ).fetchall()
    return [_row_to_threshold(row) for row in rows]


def get_price_alert_config(conn, ticker: str) -> Optional[PriceThreshold]:
    """A single ticker's configured threshold, or None if it has none."""
    ensure_price_alerts_schema(conn)
    row = conn.execute(
        "SELECT ticker, above, below, mode, percent, baseline_price FROM price_alert_config WHERE ticker = ?",
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
    mode: str = MODE_FIXED,
    percent: Optional[float] = None,
    baseline_price: Optional[float] = None,
) -> None:
    """Add a new ticker's threshold, or overwrite an existing one's - the
    same operation handles both add and edit, since this is a
    single-row-per-ticker table.

    mode=MODE_FIXED (default): above/below are stored exactly as passed -
    unchanged behavior for every existing caller that only ever passed
    above/below.

    mode=MODE_PERCENT: percent and baseline_price are required; above/below
    are DERIVED here (via resolve_percent_band) and stored resolved, not
    recomputed later - the crossing-detection engine never needs to know
    percent mode exists.
    """
    ensure_price_alerts_schema(conn)
    if mode == MODE_PERCENT:
        if percent is None or baseline_price is None:
            raise ValueError("percent mode requires both percent and baseline_price")
        above, below = resolve_percent_band(baseline_price, percent)
    elif mode == MODE_FIXED:
        percent = None
        baseline_price = None
    else:
        raise ValueError(f"unknown price alert mode: {mode!r}")

    conn.execute(
        """
        INSERT INTO price_alert_config (ticker, above, below, mode, percent, baseline_price)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            above = excluded.above, below = excluded.below, mode = excluded.mode,
            percent = excluded.percent, baseline_price = excluded.baseline_price
        """,
        (ticker, above, below, mode, percent, baseline_price),
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
