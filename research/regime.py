"""Broad market-regime context - RESEARCH ONLY.

Labels the current market backdrop from objective, trailing indicators
(price vs 50/200-day moving averages, realized volatility, drawdown) on SPY
(and optionally QQQ for secondary context) - never used to alter production
trade execution (see trading/config.py's RiskConfig, which this module has
no relationship to).
"""
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from db.price_repository import load_price_history
from research.config import DEFAULT_REGIME_CONFIG, RegimeConfig

TRADING_DAYS_PER_YEAR = 252


@dataclass
class RegimeResult:
    ok: bool
    benchmark: str
    reason: Optional[str] = None
    as_of_date: Optional[str] = None
    close: Optional[float] = None
    sma_short: Optional[float] = None
    sma_long: Optional[float] = None
    realized_vol_annualized: Optional[float] = None
    drawdown_pct: Optional[float] = None
    label: Optional[str] = None
    secondary: Optional["RegimeResult"] = field(default=None, repr=False)


def _compute_single_benchmark_regime(conn, benchmark: str, config: RegimeConfig) -> RegimeResult:
    df = load_price_history(conn, benchmark)
    if df.empty or len(df) < config.sma_long_days:
        return RegimeResult(
            ok=False, benchmark=benchmark,
            reason=f"insufficient {benchmark} history: need >= {config.sma_long_days} rows, have {len(df)}",
        )

    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"]

    sma_short = close.rolling(config.sma_short_days).mean().iloc[-1]
    sma_long = close.rolling(config.sma_long_days).mean().iloc[-1]
    if pd.isna(sma_short) or pd.isna(sma_long):
        return RegimeResult(ok=False, benchmark=benchmark, reason="not enough history for both moving averages yet")

    daily_returns = close.pct_change()
    realized_vol = daily_returns.rolling(config.realized_vol_window_days).std().iloc[-1]
    realized_vol_annualized = float(realized_vol * (TRADING_DAYS_PER_YEAR ** 0.5)) if not pd.isna(realized_vol) else None

    trailing_window = close.tail(config.drawdown_window_days)
    running_max = trailing_window.cummax()
    drawdown_pct = float((trailing_window.iloc[-1] / running_max.iloc[-1] - 1.0) * 100.0) if len(trailing_window) else None

    latest_close = float(close.iloc[-1])
    sma_short = float(sma_short)
    sma_long = float(sma_long)

    if realized_vol_annualized is not None and realized_vol_annualized >= config.elevated_vol_annualized_threshold:
        label = config.LABEL_ELEVATED_VOL
    elif latest_close > sma_short > sma_long:
        label = config.LABEL_BULLISH
    elif latest_close < sma_short < sma_long:
        label = config.LABEL_BEARISH
    else:
        label = config.LABEL_NEUTRAL

    return RegimeResult(
        ok=True, benchmark=benchmark, as_of_date=str(df["date"].iloc[-1]), close=latest_close,
        sma_short=sma_short, sma_long=sma_long, realized_vol_annualized=realized_vol_annualized,
        drawdown_pct=drawdown_pct, label=label,
    )


def compute_market_regime(conn, config: RegimeConfig = DEFAULT_REGIME_CONFIG) -> RegimeResult:
    """Primary regime read off `config.primary_benchmark` (SPY by default).
    If `config.secondary_benchmark` is set and has enough data, it's attached
    as `.secondary` for additional context - the label itself is always
    driven by the primary benchmark only, so there is one authoritative
    regime, not two competing ones."""
    primary = _compute_single_benchmark_regime(conn, config.primary_benchmark, config)
    if config.secondary_benchmark:
        secondary = _compute_single_benchmark_regime(conn, config.secondary_benchmark, config)
        primary.secondary = secondary if secondary.ok else None
    return primary
