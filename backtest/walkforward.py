"""Walk-forward validation: chronological, non-overlapping train/test windows.

For each window:
  1. `train_df` (train_window_days) comes first, purely chronologically.
  2. `test_df` (test_window_days) comes immediately after, with zero overlap.
  3. `parameter_selector(train_df)` decides which SignalThresholds/SignalWeights
     to use for that window's test backtest. In this phase it's a fixed
     no-op (see default_parameter_selector) - Phase 2's defaults are used
     unconditionally and train_df's content is ignored. A future optimizer
     could swap in a real selector with the *same signature*, fitting only
     on train_df, without touching this module's leakage-prevention logic.
  4. The test backtest only trades within test_df's date range. Indicator
     warm-up for the first bars of test_df draws on train_df's tail (a
     50-day MA needs 50 prior days) - that is standard trailing-indicator
     behavior, not leakage: a value at date t still only depends on data
     with date <= t. No date in test_df ever influences an indicator value
     computed for a date in train_df, and no date after the current bar
     ever influences that bar's value.

Windows advance by `step_days` each time. With the default step_days ==
test_window_days, consecutive test windows are back-to-back and never
overlap each other.
"""
import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import numpy as np
import pandas as pd

from backtest.config import DEFAULT_EXECUTION, DEFAULT_RULES, BacktestExecutionConfig, BacktestRules, WalkForwardConfig, DEFAULT_WF_CONFIG
from backtest.runner import BacktestResult, prepare_backtest_frame, run_backtest_on_frame
from indicators.technical import MIN_REQUIRED_ROWS
from signals.config import DEFAULT_THRESHOLDS, DEFAULT_WEIGHTS, SignalThresholds, SignalWeights

logger = logging.getLogger(__name__)

ParameterSelector = Callable[[pd.DataFrame], Tuple[SignalThresholds, SignalWeights]]


def default_parameter_selector(train_df: pd.DataFrame) -> Tuple[SignalThresholds, SignalWeights]:
    """Phase 3 default: always return the fixed Phase 2 defaults, ignoring
    train_df entirely. This keeps the current walk-forward run a pure
    out-of-sample validation of the existing, unmodified signal rules - no
    fitting against any part of the dataset happens here.

    A future optimizer would have this exact signature (train_df in,
    thresholds/weights out) and could be swapped in without changing
    generate_windows() or run_walk_forward()'s leakage guarantees, since
    it would still only ever see train_df, never test_df.
    """
    return DEFAULT_THRESHOLDS, DEFAULT_WEIGHTS


@dataclass
class WalkForwardWindow:
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str


@dataclass
class WalkForwardWindowResult:
    window: WalkForwardWindow
    result: Optional[BacktestResult]
    skipped_reason: Optional[str] = None


@dataclass
class WalkForwardResult:
    ticker: str
    source: str
    config: WalkForwardConfig
    windows: List[WalkForwardWindowResult] = field(default_factory=list)

    def summary(self) -> dict:
        valid = [w.result for w in self.windows if w.result is not None]
        if not valid:
            return {"n_windows": 0}

        returns = [r.total_return_pct for r in valid]
        sharpes = [r.sharpe_ratio for r in valid]
        drawdowns = [r.max_drawdown_pct for r in valid]
        total_trades = sum(r.num_trades for r in valid)
        zero_trade_windows = sum(1 for r in valid if r.num_trades == 0)

        trade_returns = [r.trades["ReturnPct"] for r in valid if not r.trades.empty]
        pooled = pd.concat(trade_returns, ignore_index=True) if trade_returns else pd.Series(dtype=float)
        pooled_win_rate = float((pooled > 0).mean() * 100) if len(pooled) else float("nan")

        # Backtesting.py reports Sharpe as NaN for windows with zero trades
        # (flat equity -> undefined ratio). That's a correct, honest NaN from
        # the library, not missing data - but a plain mean would let a single
        # untraded window blank out every other window's real Sharpe value,
        # so this aggregate excludes them and reports how many were excluded.
        finite_sharpes = [s for s in sharpes if not np.isnan(s)]

        return {
            "n_windows": len(valid),
            "zero_trade_windows": zero_trade_windows,
            "mean_return_pct": float(np.mean(returns)),
            "median_return_pct": float(np.median(returns)),
            "mean_sharpe": float(np.mean(finite_sharpes)) if finite_sharpes else float("nan"),
            "worst_drawdown_pct": float(np.min(drawdowns)),
            "total_trades": total_trades,
            "pooled_win_rate_pct": pooled_win_rate,
            "pct_windows_profitable": float(np.mean([r > 0 for r in returns]) * 100),
        }


def generate_windows(dates: List[str], config: WalkForwardConfig = DEFAULT_WF_CONFIG) -> List[WalkForwardWindow]:
    """Split a sorted, ascending list of trading dates into chronological,
    non-overlapping train/test windows. Returns [] if there isn't enough
    data for even one window - callers should treat that as a graceful,
    reportable outcome, not an error.
    """
    if config.train_window_days < 1 or config.test_window_days < 1 or config.step_days < 1:
        raise ValueError("train_window_days, test_window_days, and step_days must all be positive")

    n = len(dates)
    windows: List[WalkForwardWindow] = []
    i = 0
    idx = 0
    while True:
        train_end_i = i + config.train_window_days       # exclusive
        test_end_i = train_end_i + config.test_window_days  # exclusive
        if test_end_i > n:
            break
        windows.append(WalkForwardWindow(
            index=idx,
            train_start=str(dates[i]),
            train_end=str(dates[train_end_i - 1]),
            test_start=str(dates[train_end_i]),
            test_end=str(dates[test_end_i - 1]),
        ))
        i += config.step_days
        idx += 1
    return windows


def run_walk_forward(
    price_df: pd.DataFrame,
    ticker: str,
    source: str = "unknown",
    wf_config: WalkForwardConfig = DEFAULT_WF_CONFIG,
    rules: BacktestRules = DEFAULT_RULES,
    execution: BacktestExecutionConfig = DEFAULT_EXECUTION,
    parameter_selector: ParameterSelector = default_parameter_selector,
) -> WalkForwardResult:
    df = price_df.sort_values("date").reset_index(drop=True)
    dates = df["date"].tolist()
    windows = generate_windows(dates, wf_config)

    window_results: List[WalkForwardWindowResult] = []
    for w in windows:
        train_slice = df[(df["date"] >= w.train_start) & (df["date"] <= w.train_end)]
        combined_slice = df[(df["date"] >= w.train_start) & (df["date"] <= w.test_end)]

        thresholds, weights = parameter_selector(train_slice)

        try:
            bt_df_full = prepare_backtest_frame(combined_slice, ticker, thresholds, weights)
            test_only = bt_df_full.loc[w.test_start:w.test_end]
            if test_only.empty:
                window_results.append(WalkForwardWindowResult(window=w, result=None, skipped_reason="empty test slice"))
                continue
            result = run_backtest_on_frame(test_only, ticker, source=source, rules=rules, execution=execution)
            window_results.append(WalkForwardWindowResult(window=w, result=result))
        except Exception as e:
            logger.warning("walk-forward window %s failed for %s: %s", w.index, ticker, e)
            window_results.append(WalkForwardWindowResult(window=w, result=None, skipped_reason=str(e)))

    return WalkForwardResult(ticker=ticker, source=source, config=wf_config, windows=window_results)
