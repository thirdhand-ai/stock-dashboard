"""Phase 9 orchestrator: runs every analysis in strategy_lab/ against the
research universe and caches the results to disk (data/research_cache/), so
the Strategy Lab dashboard page reads a precomputed snapshot rather than
re-running a multi-minute pipeline on every page load - the same
explicit-capture pattern trading/portfolio.py's portfolio_snapshots already
uses (see capture_snapshot()'s docstring). Never runs implicitly; call
run_full_study() explicitly (CLI/one-off script) to refresh the cache.

This module is the top of the Phase 9 dependency tree - it imports every
other strategy_lab module but is never imported by trading/, automation/,
or alerts/ (see tests/test_strategy_lab_safety.py).
"""
import logging
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path

from config.settings import BASE_DIR

from strategy_lab import analysis, benchmark, events as events_mod, factors, frozen_strategy, robustness
from strategy_lab.backtest_universe import (
    IDEALIZED_EXECUTION, REASONABLE_EXECUTION, equal_weight_strategy_curve,
    per_ticker_result_table, run_universe_backtests,
)
from strategy_lab.data import RESEARCH_SOURCE, coverage_summary, fetch_and_cache_universe, load_universe_prices
from strategy_lab.regime_history import compute_historical_regime_series
from strategy_lab.signal_history import HORIZONS, build_universe_signal_table
from strategy_lab.universe import PRIMARY_BENCHMARK, RESEARCH_UNIVERSE, SECONDARY_BENCHMARK
from strategy_lab.walkforward_universe import pooled_walk_forward_summary, run_universe_walk_forward, walk_forward_by_year

logger = logging.getLogger(__name__)

CACHE_DIR = BASE_DIR / "data" / "research_cache"
CACHE_FILE = CACHE_DIR / "phase9_results.pkl"

SCORE_BUCKET_RANGES = [(0, 39, "0-39"), (40, 54, "40-54"), (55, 69, "55-69"), (70, 84, "70-84"), (85, 100, "85-100")]


def run_full_study(conn, tickers=None, years: int = 5) -> dict:
    tickers = list(tickers or RESEARCH_UNIVERSE)
    all_tickers = tickers + [PRIMARY_BENCHMARK, SECONDARY_BENCHMARK]
    t0 = time.time()
    timings = {}

    # 1-3: universe + data + frozen strategy
    fetch_report = fetch_and_cache_universe(conn, all_tickers, years=years)
    prices_by_ticker = load_universe_prices(conn, all_tickers)
    coverage = coverage_summary(prices_by_ticker)
    strategy_snapshot = frozen_strategy.snapshot()
    timings["data_acquisition"] = time.time() - t0

    # 4: pooled daily signal + forward-return table
    t = time.time()
    pooled, signal_failures = build_universe_signal_table(conn, tickers, horizons=HORIZONS)
    spy_fwd, _ = None, None
    spy_table, spy_reason = build_universe_signal_table(conn, [PRIMARY_BENCHMARK], horizons=HORIZONS)
    pooled_with_bench = analysis.attach_benchmark_forward_returns(pooled, spy_table, HORIZONS) if not spy_table.empty else pooled
    bucket_scores = analysis.bucket_by_score_pooled(pooled_with_bench, HORIZONS)
    bucket_stages = analysis.bucket_by_stage_pooled(pooled_with_bench, HORIZONS)
    timings["forward_return_study"] = time.time() - t

    # 5: signal events
    t = time.time()
    event_returns, event_failures = events_mod.build_universe_event_returns(conn, tickers, horizons=HORIZONS)
    timings["event_analysis"] = time.time() - t

    # 6: benchmarks
    t = time.time()
    spy_df = prices_by_ticker.get(PRIMARY_BENCHMARK)
    spy_stats = benchmark.spy_buy_hold_stats(spy_df) if spy_df is not None and not spy_df.empty else None
    universe_prices_only = {k: v for k, v in prices_by_ticker.items() if k in tickers}
    ew_stats = benchmark.equal_weight_universe_stats(universe_prices_only)

    spy_equity_curve = None
    if spy_df is not None and not spy_df.empty:
        s = spy_df.sort_values("date").set_index("date")["close"]
        spy_equity_curve = s / s.iloc[0] * 100.0
    ew_equity_curve = None
    if universe_prices_only:
        import pandas as _pd
        rets = _pd.concat(
            [v.sort_values("date").set_index("date")["close"].pct_change() for v in universe_prices_only.values() if not v.empty],
            axis=1,
        ).mean(axis=1, skipna=True)
        ew_equity_curve = (1.0 + rets.fillna(0)).cumprod() * 100.0
    timings["benchmark"] = time.time() - t

    # 6+7: portfolio-level backtests, idealized vs reasonable friction
    t = time.time()
    results_idealized, bt_failures_i = run_universe_backtests(conn, tickers, IDEALIZED_EXECUTION)
    results_reasonable, bt_failures_r = run_universe_backtests(conn, tickers, REASONABLE_EXECUTION)
    per_ticker_idealized = per_ticker_result_table(results_idealized)
    per_ticker_reasonable = per_ticker_result_table(results_reasonable)
    strategy_curve_idealized = equal_weight_strategy_curve(results_idealized)
    strategy_curve_reasonable = equal_weight_strategy_curve(results_reasonable)
    timings["portfolio_backtest"] = time.time() - t

    # 8: walk-forward
    t = time.time()
    wf_results, wf_failures = run_universe_walk_forward(conn, tickers)
    wf_summary = pooled_walk_forward_summary(wf_results)
    wf_by_year = walk_forward_by_year(wf_results)
    timings["walk_forward"] = time.time() - t

    # 9: regime
    t = time.time()
    regime_series = compute_historical_regime_series(conn)
    regime_by_date = regime_series.set_index("date")["label"] if not regime_series.empty else None
    regime_bucket_results = {}
    if regime_by_date is not None:
        pooled_with_regime = pooled_with_bench.copy()
        pooled_with_regime["regime"] = pooled_with_regime["date"].map(regime_by_date)
        for label in pooled_with_regime["regime"].dropna().unique():
            sub = pooled_with_regime[pooled_with_regime["regime"] == label]
            regime_bucket_results[label] = analysis.bucket_by_score_pooled(sub, HORIZONS)
    timings["regime"] = time.time() - t

    # 10: cross-sectional robustness (70-84 and 85-100 buckets, 20d horizon)
    t = time.time()
    robustness_results = {
        "70-84_20d": robustness.robustness_summary(pooled, "70-84", 70, 84, 20),
        "85-100_20d": robustness.robustness_summary(pooled, "85-100", 85, 100, 20),
    }
    timings["robustness"] = time.time() - t

    # 11: Phase 8 factor validation (relative strength only - see factors.py docstring)
    t = time.time()
    rs_by_ticker = {tk: factors.compute_historical_relative_strength(conn, tk) for tk in tickers}
    rs_quantile_returns = factors.relative_strength_forward_return_quantiles(pooled, rs_by_ticker, horizon=20)
    timings["factor_validation"] = time.time() - t

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tickers": tickers,
        "coverage": coverage,
        "fetch_report_status_counts": {s: sum(1 for r in fetch_report.values() if r["status"] == s) for s in ("cached", "fetched", "failed")},
        "strategy_snapshot": strategy_snapshot,
        "pooled_signal_table": pooled_with_bench,
        "signal_failures": signal_failures,
        "bucket_scores": bucket_scores,
        "bucket_stages": bucket_stages,
        "event_returns": event_returns,
        "event_failures": event_failures,
        "spy_stats": spy_stats,
        "equal_weight_stats": ew_stats,
        "spy_equity_curve": spy_equity_curve,
        "equal_weight_equity_curve": ew_equity_curve,
        "per_ticker_idealized": per_ticker_idealized,
        "per_ticker_reasonable": per_ticker_reasonable,
        "strategy_curve_idealized": strategy_curve_idealized,
        "strategy_curve_reasonable": strategy_curve_reasonable,
        "backtest_failures": {"idealized": bt_failures_i, "reasonable": bt_failures_r},
        "wf_summary": wf_summary,
        "wf_by_year": wf_by_year,
        "wf_failures": wf_failures,
        "regime_series": regime_series,
        "regime_bucket_results": regime_bucket_results,
        "robustness_results": robustness_results,
        "rs_quantile_returns": rs_quantile_returns,
        "timings": timings,
        "total_elapsed_sec": time.time() - t0,
    }
    return results


def save_cache(results: dict) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(results, f)
    return CACHE_FILE


def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    with open(CACHE_FILE, "rb") as f:
        return pickle.load(f)
