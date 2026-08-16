"""CRUD interface for volatility_alert_config - the per-ticker day-over-day
move threshold (in percent) the volatility-alert engine (alerts/
volatility_engine.py) evaluates. Structurally mirrors db/
price_alert_config_repository.py, but a distinct table: a volatility
threshold is a single percent value, not an above/below dollar pair, since
"moved X% today" is compared against yesterday's close fresh on every
evaluation rather than resolving to a static level (see db/
volatility_alerts_schema.py's docstring).

Dashboard-editable from dashboard/views/volatility_alert_config.py, same
"changes take effect on the next automation run, no code deploy" contract
as the price-alert config page.
"""
from typing import List, Optional

from alerts.volatility_config import VolatilityAlertConfig
from db.volatility_alerts_schema import ensure_volatility_alerts_schema


def _row_to_config(row) -> VolatilityAlertConfig:
    return VolatilityAlertConfig(ticker=row["ticker"], threshold_percent=row["threshold_percent"])


def list_volatility_alert_configs(conn) -> List[VolatilityAlertConfig]:
    """All configured volatility thresholds, ticker-ascending. A ticker with
    no row here is simply never evaluated for a volatility alert."""
    ensure_volatility_alerts_schema(conn)
    rows = conn.execute(
        "SELECT ticker, threshold_percent FROM volatility_alert_config ORDER BY ticker"
    ).fetchall()
    return [_row_to_config(row) for row in rows]


def get_volatility_alert_config(conn, ticker: str) -> Optional[VolatilityAlertConfig]:
    """A single ticker's configured volatility threshold, or None if it has none."""
    ensure_volatility_alerts_schema(conn)
    row = conn.execute(
        "SELECT ticker, threshold_percent FROM volatility_alert_config WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_config(row)


def upsert_volatility_alert_config(conn, ticker: str, threshold_percent: float) -> None:
    """Add a new ticker's volatility threshold, or overwrite an existing
    one's - the same operation handles both add and edit (single-row-per-
    ticker table). This is also the dashboard's enable/reconfigure toggle:
    a ticker gains volatility-alert evaluation the moment a row exists here."""
    ensure_volatility_alerts_schema(conn)
    if threshold_percent <= 0:
        raise ValueError("threshold_percent must be greater than zero")

    conn.execute(
        """
        INSERT INTO volatility_alert_config (ticker, threshold_percent)
        VALUES (?, ?)
        ON CONFLICT(ticker) DO UPDATE SET threshold_percent = excluded.threshold_percent
        """,
        (ticker, threshold_percent),
    )
    conn.commit()


def delete_volatility_alert_config(conn, ticker: str) -> None:
    """Remove a ticker's volatility threshold - the dashboard's disable
    toggle. It will simply no longer be evaluated for a volatility alert on
    the next run. Does NOT touch volatility_alert_state or volatility_alerts
    history for that ticker; historical records are never deleted as a side
    effect of removing a threshold."""
    ensure_volatility_alerts_schema(conn)
    conn.execute("DELETE FROM volatility_alert_config WHERE ticker = ?", (ticker,))
    conn.commit()
