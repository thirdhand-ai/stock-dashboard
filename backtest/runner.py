"""Run a single backtest of SignalScoreStrategy against one ticker's price
history using Backtesting.py, and normalize the result into a plain
BacktestResult - while preserving the underlying Backtesting.py stats/trades
objects so a later Streamlit dashboard can show more detail without
redesigning this layer.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd
from backtesting import Backtest

from backtest.config import (
    DEFAULT_EXECUTION,
    DEFAULT_RULES,
    BacktestExecutionConfig,
    BacktestRules,
)
from backtest.scoring import compute_score_series
from backtest.strategy import SignalScoreStrategy, run_kwargs_for_rules
from indicators.technical import MIN_REQUIRED_ROWS, enrich_with_indicators
from signals.config import DEFAULT_THRESHOLDS, DEFAULT_WEIGHTS, SignalThresholds, SignalWeights

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    ticker: str
    source: str
    start_date: str
    end_date: str
    n_observations: int
    rules: BacktestRules
    total_return_pct: float
    buy_hold_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate_pct: float
    num_trades: int
    avg_trade_pct: float
    best_trade_pct: float
    worst_trade_pct: float
    equity_curve: pd.Series
    trades: pd.DataFrame
    raw_stats: pd.Series


def prepare_backtest_frame(
    price_df: pd.DataFrame,
    ticker: str,
    thresholds: SignalThresholds = DEFAULT_THRESHOLDS,
    weights: SignalWeights = DEFAULT_WEIGHTS,
) -> pd.DataFrame:
    """price_df: date/open/high/low/close/volume (lowercase), any order.

    Returns a Backtesting.py-ready DataFrame: DatetimeIndex, Open/High/Low/
    Close/Volume, plus Score/StageRank columns (NaN during indicator warm-up).
    """
    enriched = enrich_with_indicators(price_df)
    scores = compute_score_series(enriched, ticker, thresholds, weights)
    merged = enriched.merge(scores, on="date", how="left")
    merged["date"] = pd.to_datetime(merged["date"])
    merged = merged.set_index("date").sort_index()

    return pd.DataFrame({
        "Open": merged["open"],
        "High": merged["high"],
        "Low": merged["low"],
        "Close": merged["close"],
        "Volume": merged["volume"],
        "Score": merged["score"],
        "StageRank": merged["stage_rank"],
    })


def _stats_to_result(stats, ticker: str, source: str, rules: BacktestRules) -> BacktestResult:
    n_trades = int(stats["# Trades"])
    return BacktestResult(
        ticker=ticker,
        source=source,
        start_date=str(stats["Start"].date()),
        end_date=str(stats["End"].date()),
        n_observations=int(stats["_equity_curve"].shape[0]),
        rules=rules,
        total_return_pct=float(stats["Return [%]"]),
        buy_hold_return_pct=float(stats["Buy & Hold Return [%]"]),
        sharpe_ratio=float(stats["Sharpe Ratio"]),
        max_drawdown_pct=float(stats["Max. Drawdown [%]"]),
        win_rate_pct=float(stats["Win Rate [%]"]) if n_trades > 0 else float("nan"),
        num_trades=n_trades,
        avg_trade_pct=float(stats["Avg. Trade [%]"]) if n_trades > 0 else float("nan"),
        best_trade_pct=float(stats["Best Trade [%]"]) if n_trades > 0 else float("nan"),
        worst_trade_pct=float(stats["Worst Trade [%]"]) if n_trades > 0 else float("nan"),
        equity_curve=stats["_equity_curve"]["Equity"],
        trades=stats["_trades"],
        raw_stats=stats,
    )


def run_backtest_on_frame(
    bt_df: pd.DataFrame,
    ticker: str,
    source: str = "unknown",
    rules: BacktestRules = DEFAULT_RULES,
    execution: BacktestExecutionConfig = DEFAULT_EXECUTION,
) -> BacktestResult:
    """Run SignalScoreStrategy on an already-prepared Backtesting.py frame
    (Open/High/Low/Close/Volume/Score/StageRank, DatetimeIndex)."""
    bt = Backtest(
        bt_df,
        SignalScoreStrategy,
        cash=execution.initial_cash,
        commission=execution.commission,
        exclusive_orders=True,
    )
    stats = bt.run(**run_kwargs_for_rules(rules))
    return _stats_to_result(stats, ticker, source, rules)


def run_backtest(
    price_df: pd.DataFrame,
    ticker: str,
    source: str = "unknown",
    rules: BacktestRules = DEFAULT_RULES,
    thresholds: SignalThresholds = DEFAULT_THRESHOLDS,
    weights: SignalWeights = DEFAULT_WEIGHTS,
    execution: BacktestExecutionConfig = DEFAULT_EXECUTION,
) -> BacktestResult:
    """Run a single, whole-history backtest for one ticker.

    price_df: date/open/high/low/close/volume (lowercase), any order.
    Raises ValueError if there isn't enough history to compute indicators at all.
    """
    if price_df is None or len(price_df) < MIN_REQUIRED_ROWS:
        n = 0 if price_df is None else len(price_df)
        raise ValueError(
            f"insufficient history for backtest: need >= {MIN_REQUIRED_ROWS} rows, got {n}"
        )

    bt_df = prepare_backtest_frame(price_df, ticker, thresholds, weights)
    return run_backtest_on_frame(bt_df, ticker, source=source, rules=rules, execution=execution)
