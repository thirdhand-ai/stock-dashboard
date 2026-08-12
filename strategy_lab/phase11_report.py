"""Phase 11 orchestrator: runs the realistic portfolio simulator (B7-B13)
across CONTROL/Experiment A/Experiment B, computes benchmarks and capital-
constraint/concentration diagnostics, and compares against Phase 10's
simplified equal-weight approximation (B13). Caches results to disk (same
pattern as strategy_lab/report.py and strategy_lab/report_phase10.py) so
Strategy Lab's dashboard view never re-runs this on page load.

Never executes an order - see tests/test_strategy_lab.py's structural
safety tests, which scan every file in this package including this one.
"""
import logging
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from config.settings import BASE_DIR
from strategy_lab.benchmark import compute_performance_stats, equal_weight_universe_stats, spy_buy_hold_stats
from strategy_lab.data import RESEARCH_SOURCE, coverage_summary, fetch_and_cache_universe, load_universe_prices
from strategy_lab.phase10_experiments import ALL_VARIANTS
from strategy_lab.portfolio_simulator import (
    STARTING_EQUITY,
    build_signal_frames,
    capital_constraint_summary,
    compute_portfolio_stats,
    concentration_diagnostics,
    simulate_variant,
)
from strategy_lab.universe import PRIMARY_BENCHMARK, RESEARCH_UNIVERSE

logger = logging.getLogger(__name__)

CACHE_DIR = BASE_DIR / "data" / "research_cache"
CACHE_FILE = CACHE_DIR / "phase11_results.pkl"


def _load_phase10_equal_weight_summary() -> dict:
    """B13: pull each variant's already-computed Phase 10 equal-weight-
    approximation summary (strategy_lab.report_phase10's variant_results,
    reasonable friction) from its own cache, rather than re-running ~100
    Backtesting.py passes here. Returns {} if Phase 10 hasn't been run."""
    from strategy_lab.report_phase10 import load_cache
    cache = load_cache()
    variant_results = cache.get("variant_results") if cache else None
    if not variant_results:
        return {}
    try:
        summary = {}
        for variant in ALL_VARIANTS:
            reasonable = variant_results.get(variant.name, {}).get("reasonable", {})
            summary[variant.name] = {
                "cumulative_return_pct": reasonable.get("cumulative_return_pct"),
                "n_tickers": len(reasonable.get("results", {}) or {}),
            }
        summary["source"] = "strategy_lab.report_phase10 cache (Phase 10 B8: equal-weight per-ticker backtest average, no capital constraints)"
        return summary
    except Exception as e:
        logger.warning("phase11: could not load Phase 10 comparison cache: %s", e)
        return {}


def run_full_phase11_study(conn, tickers=None) -> dict:
    tickers = list(tickers or RESEARCH_UNIVERSE)
    t0 = time.time()

    all_tickers = tickers + [PRIMARY_BENCHMARK]
    fetch_report = fetch_and_cache_universe(conn, all_tickers, years=5)
    prices_by_ticker = load_universe_prices(conn, all_tickers)
    coverage = coverage_summary(prices_by_ticker)

    frames = build_signal_frames(conn, tickers=tickers)
    sims = {variant.name: None for variant in ALL_VARIANTS}
    stats = {}
    capital_summaries = {}
    concentration = {}

    for variant in ALL_VARIANTS:
        result = simulate_variant(conn, variant, frames, starting_equity=STARTING_EQUITY)
        sims[variant.name] = result
        stats[variant.name] = compute_portfolio_stats(result)
        capital_summaries[variant.name] = capital_constraint_summary(result)
        concentration[variant.name] = concentration_diagnostics(result, frames)

    spy_stats = spy_buy_hold_stats(prices_by_ticker.get(PRIMARY_BENCHMARK, pd.DataFrame()))
    equal_weight_stats = equal_weight_universe_stats({t: df for t, df in prices_by_ticker.items() if t != PRIMARY_BENCHMARK})

    phase10_comparison = _load_phase10_equal_weight_summary()

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.time() - t0, 1),
        "starting_equity": STARTING_EQUITY,
        "coverage": coverage,
        "fetch_report_failures": {t: r for t, r in fetch_report.items() if r["status"] == "failed"},
        "simulations": sims,
        "portfolio_stats": stats,
        "capital_constraint_summary": capital_summaries,
        "concentration_diagnostics": concentration,
        "benchmarks": {"spy_buy_hold": spy_stats, "equal_weight_universe": equal_weight_stats},
        "phase10_equal_weight_comparison": phase10_comparison,
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
