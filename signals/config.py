"""Explicit, configurable rules for the composite signal scorer.

Every threshold and point value the engine uses lives here, by name, so
signals/engine.py never contains an unexplained constant. Tune a strategy by
editing this file, not by hunting through the scoring logic.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SignalThresholds:
    # Step 1 - Trend filter
    adx_trend_threshold: float = 25.0
    """ADX >= this means the market is trending (Wilder's standard cutoff)."""

    # Step 2 - Momentum trigger
    rsi_bullish_min: float = 50.0
    """RSI above this favors upward momentum."""
    rsi_overbought: float = 70.0
    """RSI at/above this is too extended to trust as a fresh trigger."""

    # Step 3 - Volume confirmation
    volume_ratio_min: float = 1.2
    """Current volume must be at least this multiple of the 20-day average."""


@dataclass(frozen=True)
class SignalWeights:
    """Points each condition contributes to the raw 0-100 composite score.

    Each condition awards its points whenever it fires, independent of the
    other conditions or layers - this is the granular confidence score.
    The separate sequential trend -> momentum -> volume qualification chain
    (see signals/engine.py) is tracked independently of these weights.
    Must sum to 100.
    """

    # Step 1 - Trend filter (30 pts total)
    trend_adx_points: float = 15.0
    trend_ma_points: float = 15.0

    # Step 2 - Momentum trigger (40 pts total)
    momentum_rsi_points: float = 20.0
    momentum_macd_points: float = 20.0

    # Step 3 - Volume confirmation (30 pts total)
    volume_points: float = 30.0

    def total(self) -> float:
        return (
            self.trend_adx_points
            + self.trend_ma_points
            + self.momentum_rsi_points
            + self.momentum_macd_points
            + self.volume_points
        )


DEFAULT_THRESHOLDS = SignalThresholds()
DEFAULT_WEIGHTS = SignalWeights()

assert abs(DEFAULT_WEIGHTS.total() - 100.0) < 1e-9, "SignalWeights must sum to 100"
