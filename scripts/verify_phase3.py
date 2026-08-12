"""Demonstrate Phase 3 (backtesting + walk-forward validation) against real
historical data already pulled into SQLite by the Phase 1 ingestion layer.

This is a research/backtesting demonstration only. It does not place any
paper or live trades and does not connect to Alpaca order execution.

Usage:
    python -m scripts.verify_phase3
    python -m scripts.verify_phase3 --tickers AAPL MSFT
"""
import argparse
import logging

from backtest.config import DEFAULT_EXECUTION, DEFAULT_RULES, DEFAULT_WF_CONFIG
from backtest.runner import run_backtest
from backtest.walkforward import run_walk_forward
from config.settings import WATCHLIST
from db.database import db_session
from db.price_repository import load_price_history, resolve_source

logging.basicConfig(level=logging.WARNING)


def print_rules():
    print("Entry/exit rules in effect (backtest/config.py:BacktestRules):")
    print(f"  ENTRY: highest_confirmed_stage >= '{DEFAULT_RULES.entry_min_stage}'"
          f" AND raw score >= {DEFAULT_RULES.entry_min_score}")
    print(f"  EXIT:  highest_confirmed_stage < '{DEFAULT_RULES.exit_stage_floor}'"
          f" OR raw score <= {DEFAULT_RULES.exit_max_score}")
    print(f"  Execution: cash=${DEFAULT_EXECUTION.initial_cash:,.0f}, commission={DEFAULT_EXECUTION.commission:.3%}")
    print(f"  Walk-forward: train={DEFAULT_WF_CONFIG.train_window_days}d, "
          f"test={DEFAULT_WF_CONFIG.test_window_days}d, step={DEFAULT_WF_CONFIG.step_days}d")


def print_ticker_report(ticker, conn):
    print(f"\n{'=' * 70}\n{ticker}\n{'=' * 70}")

    source = resolve_source(conn, ticker)
    if source is None:
        print("  No price data available for this ticker.")
        return

    df = load_price_history(conn, ticker, source=source)
    print(f"  Data source: {source}")
    print(f"  Date range:  {df['date'].iloc[0]} to {df['date'].iloc[-1]}")
    print(f"  Observations: {len(df)}")

    try:
        result = run_backtest(df, ticker, source=source)
    except ValueError as e:
        print(f"  BACKTEST NOT RUN: {e}")
        return

    print("\n  --- Single whole-history backtest (in-sample; see walk-forward below for out-of-sample) ---")
    print(f"  Total return:      {result.total_return_pct:>8.2f}%")
    print(f"  Buy & hold return: {result.buy_hold_return_pct:>8.2f}%")
    print(f"  Sharpe ratio:      {result.sharpe_ratio:>8.2f}")
    print(f"  Max drawdown:      {result.max_drawdown_pct:>8.2f}%")
    print(f"  Win rate:          {result.win_rate_pct:>8.2f}%")
    print(f"  Number of trades:  {result.num_trades:>8d}")
    print(f"  Avg trade return:  {result.avg_trade_pct:>8.2f}%")
    print(f"  Best trade:        {result.best_trade_pct:>8.2f}%")
    print(f"  Worst trade:       {result.worst_trade_pct:>8.2f}%")
    print(f"  Equity curve points: {len(result.equity_curve)}")

    print("\n  --- Walk-forward validation (out-of-sample, chronological, non-overlapping) ---")
    wf = run_walk_forward(df, ticker, source=source)
    n_total_windows = len(wf.windows)
    summary = wf.summary()

    if summary["n_windows"] == 0:
        print(f"  Not enough history for a single walk-forward window "
              f"(need >= {DEFAULT_WF_CONFIG.train_window_days + DEFAULT_WF_CONFIG.test_window_days} rows, "
              f"have {len(df)}).")
    else:
        print(f"  Windows evaluated: {summary['n_windows']} (of {n_total_windows} generated; "
              f"{summary['zero_trade_windows']} had zero trades)")
        print(f"  Mean test-window return:   {summary['mean_return_pct']:>8.2f}%")
        print(f"  Median test-window return: {summary['median_return_pct']:>8.2f}%")
        print(f"  Mean Sharpe (traded windows only): {summary['mean_sharpe']:.2f}")
        print(f"  Worst single-window drawdown: {summary['worst_drawdown_pct']:>8.2f}%")
        print(f"  Total trades across all windows: {summary['total_trades']}")
        print(f"  Pooled win rate (all trades, all windows): {summary['pooled_win_rate_pct']:.2f}%")
        print(f"  Windows profitable: {summary['pct_windows_profitable']:.1f}%")
        print("\n  Per-window detail:")
        for wr in wf.windows:
            if wr.result is None:
                print(f"    [{wr.window.index}] {wr.window.test_start} to {wr.window.test_end}: SKIPPED ({wr.skipped_reason})")
            else:
                r = wr.result
                print(f"    [{wr.window.index}] {wr.window.test_start} to {wr.window.test_end}: "
                      f"return={r.total_return_pct:>7.2f}%  sharpe={r.sharpe_ratio:>6.2f}  "
                      f"trades={r.num_trades}  maxdd={r.max_drawdown_pct:>7.2f}%")


def main():
    parser = argparse.ArgumentParser(description="Verify Phase 3 backtesting + walk-forward on real data")
    parser.add_argument("--tickers", nargs="+", default=WATCHLIST[:2])
    args = parser.parse_args()

    print_rules()
    with db_session() as conn:
        for ticker in args.tickers:
            print_ticker_report(ticker, conn)


if __name__ == "__main__":
    main()
