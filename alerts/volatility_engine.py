"""Volatility (day-over-day % move) alert evaluation: detects a ticker
moving more than a configured percentage between two consecutive stored
trading-day closes. Reuses db/price_repository.py's load_price_history
directly for the last two closes - deliberately NOT alerts/price_engine.py's
price_alert_state (that table is scoped to the price-threshold system and
only ever reflects whatever price an evaluation run last observed, not
necessarily yesterday's actual close). Reading straight from the `prices`
table means the "previous" side of the comparison is always the immediately
preceding STORED trading-day bar, recomputed fresh every call - never a
fixed baseline.

One condition: |current_close - previous_close| / previous_close * 100 >=
threshold_percent. Fires on either an up move or a down move; the direction
is exposed on the evaluation as REASON_VOLATILITY_UP / REASON_VOLATILITY_DOWN
for message building, but a single VolatilityAlertConfig covers both
directions (unlike PriceThreshold's separate above/below fields) - the
concept is "moved X% in a day," not "moved X% in a specific direction."

Never fires when there's no prior stored bar to compare against (first-ever
row for a ticker) - same "first observation establishes a baseline, no
alert" contract alerts/price_engine.py uses. A gap between the two most
recent stored rows (e.g. a missed ingestion day) is not specially detected -
the move is still computed across whatever two rows are actually adjacent in
storage, which can measure a multi-day move as if it were a single day's;
this is a documented, deliberate simplification (see tests) rather than an
attempt to reconstruct trading-calendar gaps.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from alerts.volatility_config import VolatilityAlertConfig
from db.price_repository import load_price_history, resolve_source
from db.volatility_alert_config_repository import list_volatility_alert_configs

REASON_VOLATILITY_UP = "volatility_up"
REASON_VOLATILITY_DOWN = "volatility_down"


def compute_day_over_day_move_pct(previous_close: Optional[float], current_close: float) -> Optional[float]:
    """Signed % move from previous_close to current_close, or None if there
    is no usable previous_close (missing, or zero/negative - a % move is
    undefined against a non-positive baseline)."""
    if previous_close is None or previous_close <= 0:
        return None
    return (current_close - previous_close) / previous_close * 100


def determine_volatility_alert_reasons(
    previous_close: Optional[float],
    current_close: float,
    config: VolatilityAlertConfig,
) -> List[str]:
    """Pure decision logic, no DB dependency. Fires when the magnitude of
    the day-over-day move meets or exceeds threshold_percent (>=, so a move
    landing exactly on the threshold fires) - in either direction."""
    move_pct = compute_day_over_day_move_pct(previous_close, current_close)
    if move_pct is None:
        return []
    if abs(move_pct) >= config.threshold_percent:
        return [REASON_VOLATILITY_UP if move_pct > 0 else REASON_VOLATILITY_DOWN]
    return []


@dataclass
class VolatilityAlertEvaluation:
    ticker: str
    ok: bool
    reason_unavailable: Optional[str] = None
    should_alert: bool = False
    reasons: List[str] = field(default_factory=list)
    current_price: Optional[float] = None
    previous_price: Optional[float] = None
    move_pct: Optional[float] = None
    threshold_percent: Optional[float] = None
    data_date: Optional[str] = None
    previous_date: Optional[str] = None
    source: Optional[str] = None


def evaluate_ticker_volatility(conn, ticker: str, config: VolatilityAlertConfig) -> VolatilityAlertEvaluation:
    """Compare the two most recent stored closes for a ticker against its
    configured threshold_percent."""
    price_df = load_price_history(conn, ticker)
    if price_df.empty:
        return VolatilityAlertEvaluation(ticker=ticker, ok=False, reason_unavailable="no price data available")

    source = resolve_source(conn, ticker)
    latest = price_df.iloc[-1]

    if len(price_df) < 2:
        # First-ever stored bar: establish a baseline only, never alert.
        return VolatilityAlertEvaluation(
            ticker=ticker, ok=True, current_price=float(latest["close"]),
            data_date=str(latest["date"]), threshold_percent=config.threshold_percent, source=source,
        )

    previous = price_df.iloc[-2]
    previous_close = float(previous["close"])
    current_close = float(latest["close"])

    reasons = determine_volatility_alert_reasons(previous_close, current_close, config)
    move_pct = compute_day_over_day_move_pct(previous_close, current_close)

    return VolatilityAlertEvaluation(
        ticker=ticker,
        ok=True,
        should_alert=bool(reasons),
        reasons=reasons,
        current_price=current_close,
        previous_price=previous_close,
        move_pct=move_pct,
        threshold_percent=config.threshold_percent,
        data_date=str(latest["date"]),
        previous_date=str(previous["date"]),
        source=source,
    )


def evaluate_volatility_alerts(
    conn, configs: Optional[List[VolatilityAlertConfig]] = None
) -> List[VolatilityAlertEvaluation]:
    """Evaluate only the tickers that have a configured volatility
    threshold - a ticker with no VolatilityAlertConfig entry is never
    evaluated. Defaults to the DB-backed volatility_alert_config table
    (dashboard-editable), not a hardcoded list."""
    configs = configs if configs is not None else list_volatility_alert_configs(conn)
    return [evaluate_ticker_volatility(conn, c.ticker, c) for c in configs]
