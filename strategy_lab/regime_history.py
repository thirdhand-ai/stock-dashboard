"""Historical market-regime classification for every date, not just 'now'
(Phase 9 spec item 9).

research/regime.py's compute_market_regime() only returns the CURRENT regime
(a single point-in-time read). This module reuses its exact thresholds and
labeling logic (research.config.RegimeConfig, imported not duplicated) but
applies them at every historical date via rolling/vectorized pandas
operations, so each date in the 5-year sample can be tagged with the regime
that was in effect at the time - still using only trailing information
available as of that date (rolling SMA/vol/drawdown), so no look-ahead.
"""
import numpy as np
import pandas as pd

from db.price_repository import load_price_history
from research.config import DEFAULT_REGIME_CONFIG, RegimeConfig
from research.regime import TRADING_DAYS_PER_YEAR
from strategy_lab.data import RESEARCH_SOURCE


def compute_historical_regime_series(conn, config: RegimeConfig = DEFAULT_REGIME_CONFIG, source: str = RESEARCH_SOURCE) -> pd.DataFrame:
    """Returns date, label, sma_short, sma_long, realized_vol_annualized,
    drawdown_pct for every date with enough trailing history for both moving
    averages. Empty DataFrame if there isn't enough SPY history."""
    df = load_price_history(conn, config.primary_benchmark, source=source)
    if df.empty or len(df) < config.sma_long_days:
        return pd.DataFrame(columns=["date", "label", "sma_short", "sma_long", "realized_vol_annualized", "drawdown_pct"])

    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"]

    sma_short = close.rolling(config.sma_short_days).mean()
    sma_long = close.rolling(config.sma_long_days).mean()

    daily_returns = close.pct_change()
    realized_vol_annualized = daily_returns.rolling(config.realized_vol_window_days).std() * np.sqrt(TRADING_DAYS_PER_YEAR)

    rolling_max = close.rolling(config.drawdown_window_days).max()
    drawdown_pct = (close / rolling_max - 1.0) * 100.0

    elevated = realized_vol_annualized >= config.elevated_vol_annualized_threshold
    bullish = (close > sma_short) & (sma_short > sma_long)
    bearish = (close < sma_short) & (sma_short < sma_long)

    label = np.select(
        [elevated, bullish, bearish],
        [config.LABEL_ELEVATED_VOL, config.LABEL_BULLISH, config.LABEL_BEARISH],
        default=config.LABEL_NEUTRAL,
    )

    out = pd.DataFrame({
        "date": df["date"],
        "label": label,
        "sma_short": sma_short,
        "sma_long": sma_long,
        "realized_vol_annualized": realized_vol_annualized,
        "drawdown_pct": drawdown_pct,
    })
    return out.dropna(subset=["sma_long"]).reset_index(drop=True)
