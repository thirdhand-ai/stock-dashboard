"""Benchmark performance stats for Phase 9 spec item 6: SPY buy-and-hold and
equal-weight buy-and-hold of the research universe, computed the same way
for both so they're directly comparable to signal-driven results.

Risk-free rate is assumed 0% (not fetched from any source) - Sharpe ratios
here are therefore return/volatility ratios, not excess-return Sharpe. This
is stated explicitly wherever a Sharpe value is reported (Phase 9 spec item
12's statistical-caution requirement).
"""
from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


@dataclass
class PerformanceStats:
    label: str
    n_days: int
    cumulative_return_pct: float
    annualized_return_pct: float
    annualized_vol_pct: float
    sharpe_ratio_rf0: float
    max_drawdown_pct: float
    win_rate_pct: float
    exposure_pct: float = 100.0


def compute_performance_stats(daily_returns: pd.Series, label: str, exposure_pct: float = 100.0) -> Optional[PerformanceStats]:
    r = daily_returns.dropna()
    if len(r) < 2:
        return None

    cum_factor = float((1.0 + r).prod())
    cumulative_return = cum_factor - 1.0
    years = len(r) / TRADING_DAYS_PER_YEAR
    annualized_return = cum_factor ** (1.0 / years) - 1.0 if years > 0 else float("nan")
    annualized_vol = float(r.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))
    sharpe = (annualized_return / annualized_vol) if annualized_vol > 0 else float("nan")

    equity = (1.0 + r).cumprod()
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    max_dd = float(drawdown.min()) * 100.0

    win_rate = float((r > 0).mean()) * 100.0

    return PerformanceStats(
        label=label, n_days=int(len(r)),
        cumulative_return_pct=round(cumulative_return * 100, 2),
        annualized_return_pct=round(annualized_return * 100, 2),
        annualized_vol_pct=round(annualized_vol * 100, 2),
        sharpe_ratio_rf0=round(sharpe, 3) if sharpe == sharpe else None,
        max_drawdown_pct=round(max_dd, 2),
        win_rate_pct=round(win_rate, 1),
        exposure_pct=exposure_pct,
    )


def spy_buy_hold_stats(spy_price_df: pd.DataFrame) -> Optional[PerformanceStats]:
    df = spy_price_df.sort_values("date").reset_index(drop=True)
    returns = df["close"].pct_change()
    return compute_performance_stats(returns, "SPY buy-and-hold")


def equal_weight_universe_stats(prices_by_ticker: Dict[str, pd.DataFrame]) -> Optional[PerformanceStats]:
    """Equal-weight, daily-rebalanced buy-and-hold across every ticker with
    price data on a given date (rebalancing daily is the standard, simplest
    definition of an equal-weight index return - no drift-and-hold
    approximation)."""
    frames = []
    for ticker, df in prices_by_ticker.items():
        if df.empty:
            continue
        s = df.sort_values("date").set_index("date")["close"].pct_change()
        s.name = ticker
        frames.append(s)
    if not frames:
        return None
    wide = pd.concat(frames, axis=1)
    daily_mean_return = wide.mean(axis=1, skipna=True)
    return compute_performance_stats(daily_mean_return, "Equal-weight universe buy-and-hold")
