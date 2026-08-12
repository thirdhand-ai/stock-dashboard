"""Phase 10 spec item B8: runs CONTROL / Experiment A / Experiment B across
the research universe using strategy_lab.regime_strategy.RegimeGatedStrategy,
under the same two friction assumptions Phase 9 already defined
(strategy_lab.backtest_universe.IDEALIZED_EXECUTION / REASONABLE_EXECUTION -
reused unmodified, per B8's "do not change Phase 9 friction assumptions").

No same-close execution: Backtesting.py's default order-fill behavior fills
a signal generated on bar t's close at bar t+1's open (never the same bar) -
this is a library-level guarantee backtest/strategy.py already relies on for
Phase 9's CONTROL numbers, inherited here unchanged.
"""
from typing import Dict, Iterable, Tuple

import pandas as pd
from backtesting import Backtest

from backtest.runner import _stats_to_result, prepare_backtest_frame
from db.price_repository import load_price_history
from indicators.technical import MIN_REQUIRED_ROWS
from strategy_lab.backtest_universe import IDEALIZED_EXECUTION, REASONABLE_EXECUTION
from strategy_lab.data import RESEARCH_SOURCE
from strategy_lab.phase10_experiments import BULLISH_LABEL, RegimeGatedRules
from strategy_lab.regime_strategy import RegimeGatedStrategy, run_kwargs_for_variant


def prepare_regime_backtest_frame(price_df: pd.DataFrame, ticker: str, regime_series: pd.DataFrame) -> pd.DataFrame:
    """Phase 9's Open/High/Low/Close/Volume/Score/StageRank frame
    (backtest.runner.prepare_backtest_frame, reused unmodified) plus a
    RegimeBullish column. Dates before the regime series has enough
    trailing history (SMA-200 warm-up) default to RegimeBullish=False -
    conservative: no bullish-gated entry can fire before regime is knowable."""
    bt_df = prepare_backtest_frame(price_df, ticker)
    regime_by_date = regime_series.set_index("date")["label"] == BULLISH_LABEL
    regime_by_date.index = pd.to_datetime(regime_by_date.index)
    bt_df["RegimeBullish"] = regime_by_date.reindex(bt_df.index, fill_value=False)
    return bt_df


def run_variant_backtest(bt_df: pd.DataFrame, ticker: str, source: str, variant: RegimeGatedRules, execution) -> "BacktestResult":
    bt = Backtest(bt_df, RegimeGatedStrategy, cash=execution.initial_cash, commission=execution.commission, exclusive_orders=True)
    from backtest.config import DEFAULT_RULES
    stats = bt.run(**run_kwargs_for_variant(variant))
    return _stats_to_result(stats, ticker, source, DEFAULT_RULES)


def run_universe_variant_backtests(
    conn, tickers: Iterable[str], regime_series: pd.DataFrame, variant: RegimeGatedRules, execution, source: str = RESEARCH_SOURCE,
) -> Tuple[Dict[str, "BacktestResult"], Dict[str, str]]:
    results = {}
    failures: Dict[str, str] = {}
    for t in tickers:
        price_df = load_price_history(conn, t, source=source)
        if len(price_df) < MIN_REQUIRED_ROWS:
            failures[t] = f"insufficient history: {len(price_df)} rows"
            continue
        try:
            bt_df = prepare_regime_backtest_frame(price_df, t, regime_series)
            results[t] = run_variant_backtest(bt_df, t, source, variant, execution)
        except Exception as e:
            failures[t] = str(e)
    return results, failures
