"""Rules-based composite signal scorer.

Two things are computed and kept deliberately separate:

1. Raw composite score (0-100): each of the 5 conditions below contributes
   its configured weight to the score whenever that condition fires -
   independent of whether any other condition or layer fired. This is a
   granular confidence read, not gated by the sequential chain.

2. Sequential qualification state: trend -> momentum -> volume, where each
   stage requires the previous stage to have fully confirmed. This is the
   "sequential confirmation pattern" from the original spec, expressed as
   explicit booleans plus a highest_confirmed_stage summary - independent of
   the raw score above.

A ticker can score highly on raw confidence while never confirming past the
trend stage (momentum/volume conditions fired on their own merits, without
trend backing them), and the breakdown makes that visible rather than
hiding it behind a single number.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config.settings import WATCHLIST
from indicators.technical import IndicatorResult, compute_indicators_for_ticker
from signals.config import DEFAULT_THRESHOLDS, DEFAULT_WEIGHTS, SignalThresholds, SignalWeights

STAGE_NONE = "none"
STAGE_TREND = "trend"
STAGE_MOMENTUM = "momentum"
STAGE_VOLUME = "volume"


@dataclass
class ConditionResult:
    layer: str            # "trend" | "momentum" | "volume"
    name: str
    description: str
    fired: bool
    points_available: float
    points_awarded: float  # == points_available if fired, else 0.0 - never gated
    values: Dict[str, float]


@dataclass
class SignalScore:
    ticker: str
    ok: bool
    reason: Optional[str] = None
    score: float = 0.0                       # raw composite, sum of points for every fired condition
    trend_confirmed: bool = False
    momentum_confirmed: bool = False
    volume_confirmed: bool = False
    highest_confirmed_stage: str = STAGE_NONE
    conditions: List[ConditionResult] = field(default_factory=list)


def score_indicators(
    indicators: IndicatorResult,
    thresholds: SignalThresholds = DEFAULT_THRESHOLDS,
    weights: SignalWeights = DEFAULT_WEIGHTS,
) -> SignalScore:
    if not indicators.ok:
        return SignalScore(ticker=indicators.ticker, ok=False, reason=indicators.reason)

    conditions: List[ConditionResult] = []

    def add_condition(layer, name, description, fired, points_available, values):
        conditions.append(ConditionResult(
            layer=layer,
            name=name,
            description=description,
            fired=fired,
            points_available=points_available,
            points_awarded=points_available if fired else 0.0,
            values=values,
        ))
        return fired

    # --- Step 1: Trend filter ---
    adx_fired = add_condition(
        "trend", "adx_trending", f"ADX >= {thresholds.adx_trend_threshold}",
        indicators.adx >= thresholds.adx_trend_threshold,
        weights.trend_adx_points, {"adx": indicators.adx},
    )
    ma_fired = add_condition(
        "trend", "price_above_50ma", "Close > 50-day MA",
        indicators.close > indicators.sma_50,
        weights.trend_ma_points, {"close": indicators.close, "sma_50": indicators.sma_50},
    )

    # --- Step 2: Momentum trigger ---
    rsi_fired = add_condition(
        "momentum", "rsi_bullish_not_overbought",
        f"{thresholds.rsi_bullish_min} < RSI < {thresholds.rsi_overbought}",
        thresholds.rsi_bullish_min < indicators.rsi < thresholds.rsi_overbought,
        weights.momentum_rsi_points, {"rsi": indicators.rsi},
    )
    macd_fired = add_condition(
        "momentum", "macd_above_signal", "MACD line > signal line",
        indicators.macd > indicators.macd_signal,
        weights.momentum_macd_points,
        {"macd": indicators.macd, "macd_signal": indicators.macd_signal, "macd_hist": indicators.macd_hist},
    )

    # --- Step 3: Volume confirmation ---
    volume_fired = add_condition(
        "volume", "volume_above_20d_avg", f"Volume >= {thresholds.volume_ratio_min}x 20-day average",
        indicators.volume_ratio >= thresholds.volume_ratio_min,
        weights.volume_points,
        {"volume": indicators.volume, "volume_avg_20": indicators.volume_avg_20, "volume_ratio": indicators.volume_ratio},
    )

    # --- Raw composite score: unconditional sum of whatever fired ---
    total_score = round(sum(c.points_awarded for c in conditions), 2)

    # --- Sequential qualification chain: each stage requires the last ---
    trend_confirmed = adx_fired and ma_fired
    momentum_confirmed = trend_confirmed and rsi_fired and macd_fired
    volume_confirmed = momentum_confirmed and volume_fired

    if volume_confirmed:
        highest_stage = STAGE_VOLUME
    elif momentum_confirmed:
        highest_stage = STAGE_MOMENTUM
    elif trend_confirmed:
        highest_stage = STAGE_TREND
    else:
        highest_stage = STAGE_NONE

    return SignalScore(
        ticker=indicators.ticker,
        ok=True,
        score=total_score,
        trend_confirmed=trend_confirmed,
        momentum_confirmed=momentum_confirmed,
        volume_confirmed=volume_confirmed,
        highest_confirmed_stage=highest_stage,
        conditions=conditions,
    )


def score_ticker(conn, ticker: str, source: Optional[str] = None) -> SignalScore:
    """Load a ticker's history from SQLite, compute indicators, and score it."""
    indicators = compute_indicators_for_ticker(conn, ticker, source=source)
    return score_indicators(indicators)


def score_watchlist(conn, tickers: Optional[List[str]] = None) -> Dict[str, SignalScore]:
    tickers = tickers or WATCHLIST
    return {ticker: score_ticker(conn, ticker) for ticker in tickers}
