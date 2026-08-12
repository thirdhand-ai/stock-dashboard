"""Runs the existing backtest/walkforward.py (chronological, non-overlapping
train/test windows, default_parameter_selector - i.e. the frozen production
rules, never fit on train_df) across the full research universe (Phase 9
spec item 8).

Every test window is genuinely out-of-sample relative to its own train
window - see backtest/walkforward.py's own docstring for the no-leakage
argument, which this module does not alter.
"""
from collections import defaultdict
from typing import Dict, Iterable, Tuple

import numpy as np
import pandas as pd

from backtest.config import DEFAULT_WF_CONFIG, WalkForwardConfig
from backtest.walkforward import WalkForwardResult, run_walk_forward
from db.price_repository import load_price_history
from indicators.technical import MIN_REQUIRED_ROWS
from strategy_lab.backtest_universe import REASONABLE_EXECUTION
from strategy_lab.data import RESEARCH_SOURCE


def run_universe_walk_forward(
    conn, tickers: Iterable[str], wf_config: WalkForwardConfig = DEFAULT_WF_CONFIG,
    execution=REASONABLE_EXECUTION, source: str = RESEARCH_SOURCE,
) -> Tuple[Dict[str, WalkForwardResult], Dict[str, str]]:
    results: Dict[str, WalkForwardResult] = {}
    failures: Dict[str, str] = {}
    for t in tickers:
        price_df = load_price_history(conn, t, source=source)
        if len(price_df) < MIN_REQUIRED_ROWS:
            failures[t] = f"insufficient history: {len(price_df)} rows"
            continue
        try:
            results[t] = run_walk_forward(price_df, t, source=source, wf_config=wf_config, execution=execution)
        except Exception as e:
            failures[t] = str(e)
    return results, failures


def pooled_walk_forward_summary(results: Dict[str, WalkForwardResult]) -> dict:
    all_valid = [w.result for r in results.values() for w in r.windows if w.result is not None]
    if not all_valid:
        return {"n_windows": 0}

    returns = [r.total_return_pct for r in all_valid]
    finite_sharpes = [r.sharpe_ratio for r in all_valid if r.sharpe_ratio == r.sharpe_ratio]
    total_trades = sum(r.num_trades for r in all_valid)
    trade_returns = [r.trades["ReturnPct"] for r in all_valid if not r.trades.empty]
    pooled_trades = pd.concat(trade_returns, ignore_index=True) if trade_returns else pd.Series(dtype=float)

    return {
        "n_tickers": len(results),
        "n_windows_total": len(all_valid),
        "mean_window_return_pct": round(float(np.mean(returns)), 3),
        "median_window_return_pct": round(float(np.median(returns)), 3),
        "pct_windows_profitable": round(float(np.mean([r > 0 for r in returns]) * 100), 1),
        "mean_sharpe": round(float(np.mean(finite_sharpes)), 3) if finite_sharpes else None,
        "total_trades_across_windows": total_trades,
        "pooled_trade_win_rate_pct": round(float((pooled_trades > 0).mean() * 100), 1) if len(pooled_trades) else None,
    }


def walk_forward_by_year(results: Dict[str, WalkForwardResult]) -> pd.DataFrame:
    """Buckets every test-window result by the calendar year its test period
    started in, so out-of-sample performance can be read year-by-year."""
    by_year = defaultdict(list)
    for r in results.values():
        for w in r.windows:
            if w.result is None:
                continue
            year = w.window.test_start[:4]
            by_year[year].append(w.result)

    rows = []
    for year in sorted(by_year):
        rs = by_year[year]
        returns = [r.total_return_pct for r in rs]
        trades = sum(r.num_trades for r in rs)
        rows.append({
            "year": year,
            "n_windows": len(rs),
            "mean_return_pct": round(float(np.mean(returns)), 3),
            "pct_profitable": round(float(np.mean([r > 0 for r in returns]) * 100), 1),
            "total_trades": trades,
        })
    return pd.DataFrame(rows)
