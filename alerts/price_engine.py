"""Price-threshold alert evaluation: detects meaningful price crossings
worth notifying about, reusing indicators.technical.compute_indicators_for_ticker
directly for the current close/date (the same data source alerts/engine.py
already trusts - no separate, inconsistent price-fetch logic).

Two conditions, either of which can fire independently per ticker:
  - price_above: raw close crosses UP through threshold.above
    (previous < above <= current).
  - price_below: raw close crosses DOWN through threshold.below
    (previous > below >= current).

Never fires on a ticker's first-ever evaluation - with no prior observation
there is nothing to have crossed *from*, so the first run only establishes
a baseline.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from alerts.price_config import PriceThreshold
from db.price_alert_config_repository import list_price_alert_configs
from db.price_alert_repository import get_price_alert_state
from db.price_repository import resolve_source
from indicators.technical import compute_indicators_for_ticker

REASON_PRICE_ABOVE = "price_above"
REASON_PRICE_BELOW = "price_below"


def determine_price_alert_reasons(
    previous_price: Optional[float],
    current_price: float,
    threshold: PriceThreshold,
) -> List[str]:
    """Pure decision logic, no DB/indicator dependency - the core of what
    counts as a "meaningful crossing" worth alerting on."""
    if previous_price is None:
        return []  # first observation: establish baseline, never alert

    reasons = []
    if threshold.above is not None and previous_price < threshold.above <= current_price:
        reasons.append(REASON_PRICE_ABOVE)
    if threshold.below is not None and previous_price > threshold.below >= current_price:
        reasons.append(REASON_PRICE_BELOW)
    return reasons


@dataclass
class PriceAlertEvaluation:
    ticker: str
    ok: bool
    reason_unavailable: Optional[str] = None
    should_alert: bool = False
    reasons: List[str] = field(default_factory=list)
    current_price: Optional[float] = None
    previous_price: Optional[float] = None
    threshold: Optional[PriceThreshold] = None
    data_date: Optional[str] = None
    source: Optional[str] = None


def evaluate_ticker_price(conn, ticker: str, threshold: PriceThreshold) -> PriceAlertEvaluation:
    """Compute the current close price for a ticker and compare against its
    last-observed price (price_alert_state) to decide whether it should
    alert."""
    indicators = compute_indicators_for_ticker(conn, ticker)
    if not indicators.ok:
        return PriceAlertEvaluation(ticker=ticker, ok=False, reason_unavailable=indicators.reason)

    state = get_price_alert_state(conn, ticker)
    previous_price = state["last_price"] if state else None

    reasons = determine_price_alert_reasons(previous_price, indicators.close, threshold)

    return PriceAlertEvaluation(
        ticker=ticker,
        ok=True,
        should_alert=bool(reasons),
        reasons=reasons,
        current_price=indicators.close,
        previous_price=previous_price,
        threshold=threshold,
        data_date=indicators.latest_date,
        source=resolve_source(conn, ticker),
    )


def evaluate_price_thresholds(
    conn, thresholds: Optional[List[PriceThreshold]] = None
) -> List[PriceAlertEvaluation]:
    """Evaluate only the tickers that have a configured threshold - a ticker
    with no PriceThreshold entry is never evaluated for a price alert.
    Defaults to the DB-backed price_alert_config table (dashboard-editable),
    not a hardcoded list."""
    thresholds = thresholds if thresholds is not None else list_price_alert_configs(conn)
    return [evaluate_ticker_price(conn, t.ticker, t) for t in thresholds]
