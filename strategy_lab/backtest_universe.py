"""Runs the existing, unmodified backtest/runner.py (Backtesting.py +
SignalScoreStrategy, the exact production entry/exit rules) across the full
research universe, under two execution-friction assumptions (Phase 9 spec
items 6/7). This is the "portfolio-level" complement to the daily/event
forward-return studies elsewhere in strategy_lab - it produces per-ticker
equity curves that can be equal-weighted into a universe strategy curve and
compared directly against SPY / equal-weight buy-and-hold.

Friction assumptions (Backtesting.py only models a single flat per-trade
commission rate, so slippage and commission are combined into one number
here - documented, not hidden):
  - idealized: commission=0 (no friction at all)
  - reasonable: commission = backtest.config.DEFAULT_EXECUTION.commission
    (0.10%, the project's existing production backtest assumption) plus a
    5bps slippage allowance, matching strategy_lab.execution.REASONABLE's
    slippage_bps=5.0 so the two friction models (event-based and
    portfolio-level) use consistent assumptions.
"""
import logging
from typing import Dict, Iterable, Tuple

import pandas as pd

from backtest.config import DEFAULT_EXECUTION, DEFAULT_RULES, BacktestExecutionConfig
from backtest.runner import BacktestResult, run_backtest
from db.price_repository import load_price_history
from indicators.technical import MIN_REQUIRED_ROWS
from strategy_lab.data import RESEARCH_SOURCE

logger = logging.getLogger(__name__)

IDEALIZED_EXECUTION = BacktestExecutionConfig(initial_cash=DEFAULT_EXECUTION.initial_cash, commission=0.0)
REASONABLE_EXECUTION = BacktestExecutionConfig(
    initial_cash=DEFAULT_EXECUTION.initial_cash, commission=DEFAULT_EXECUTION.commission + 0.0005,
)


def run_universe_backtests(
    conn, tickers: Iterable[str], execution: BacktestExecutionConfig, source: str = RESEARCH_SOURCE,
) -> Tuple[Dict[str, BacktestResult], Dict[str, str]]:
    results: Dict[str, BacktestResult] = {}
    failures: Dict[str, str] = {}
    for t in tickers:
        price_df = load_price_history(conn, t, source=source)
        if len(price_df) < MIN_REQUIRED_ROWS:
            failures[t] = f"insufficient history: {len(price_df)} rows"
            continue
        try:
            results[t] = run_backtest(price_df, t, source=source, rules=DEFAULT_RULES, execution=execution)
        except Exception as e:
            logger.warning("strategy_lab backtest failed for %s: %s", t, e)
            failures[t] = str(e)
    return results, failures


def equal_weight_strategy_curve(results: Dict[str, BacktestResult]) -> pd.Series:
    """Normalizes each ticker's Backtesting.py equity curve to start at 1.0
    and averages across tickers day-by-day (outer-joined on date, forward
    filled only for days a given ticker's backtest was already running) -
    the strategy-side analogue of benchmark.equal_weight_universe_stats."""
    frames = []
    for ticker, r in results.items():
        s = r.equity_curve.copy()
        s = s / s.iloc[0]
        s.name = ticker
        frames.append(s)
    if not frames:
        return pd.Series(dtype=float)
    wide = pd.concat(frames, axis=1)
    return wide.mean(axis=1, skipna=True)


def per_ticker_result_table(results: Dict[str, BacktestResult]) -> pd.DataFrame:
    rows = []
    for ticker, r in results.items():
        rows.append({
            "ticker": ticker,
            "total_return_pct": r.total_return_pct,
            "buy_hold_return_pct": r.buy_hold_return_pct,
            "excess_vs_own_buy_hold_pct": r.total_return_pct - r.buy_hold_return_pct,
            "sharpe_ratio": r.sharpe_ratio,
            "max_drawdown_pct": r.max_drawdown_pct,
            "win_rate_pct": r.win_rate_pct,
            "num_trades": r.num_trades,
        })
    return pd.DataFrame(rows).sort_values("total_return_pct", ascending=False).reset_index(drop=True)
