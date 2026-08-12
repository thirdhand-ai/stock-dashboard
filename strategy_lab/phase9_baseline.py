"""Freezes a compact, human-and-machine-readable Phase 9 baseline (Part A2)
from the full research cache (strategy_lab/report.py's phase9_results.pkl),
so Phase 10 (and any later phase) can compare against a fixed, versioned
result instead of silently recomputing a different one.

Deliberately excludes the large row-level tables (pooled_signal_table,
event_returns - hundreds of thousands of rows) - those remain in the pickle
cache for the dashboard, but the baseline is the *summary* a human or a
later phase actually needs to compare against, kept small and diffable.
Contains no credentials, webhook URLs, or API secrets.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from config.settings import BASE_DIR
from strategy_lab.execution import ASSUMPTION_SETS

BASELINE_DIR = BASE_DIR / "data" / "research_cache"
BASELINE_FILE = BASELINE_DIR / "phase9_baseline.json"

PHASE9_READINESS = "NOT READY"
PHASE9_READINESS_RATIONALE = (
    "The core hypothesis (higher technical score / later confirmation stage -> better forward returns) is not "
    "supported in aggregate: score buckets and confirmation stages show flat-to-inverse relationships with "
    "forward returns pooled across the full sample. The frozen strategy's own whole-history portfolio backtest "
    "underperformed SPY and equal-weight buy-and-hold by a wide margin (+11-14% strategy vs +85.6%/+101.6% "
    "benchmarks over ~5 years), even before penalizing for outlier concentration. The one coherent positive "
    "pattern found was conditioned on the bullish_trend market regime only - promising as a research direction "
    "(see Phase 10), not sufficient grounds for enabling automated paper execution of the unconditional rule set."
)


def _df_to_records(df):
    if df is None:
        return None
    if isinstance(df, pd.DataFrame):
        return df.to_dict("records") if not df.empty else []
    return df


def _dataclass_to_dict(obj):
    if obj is None:
        return None
    return {k: v for k, v in vars(obj).items()}


def build_baseline_summary(results: dict) -> dict:
    """Extracts the durable, comparable summary from a full
    strategy_lab.report.run_full_study() result dict."""
    return {
        "phase": 9,
        "generated_at": results.get("generated_at"),
        "dataset": {
            "coverage": results.get("coverage"),
            "research_universe_size": len(results.get("tickers", [])),
            "research_universe": sorted(results.get("tickers", [])),
            "scoreable_observations": int(len(results["pooled_signal_table"])) if results.get("pooled_signal_table") is not None else None,
        },
        "frozen_strategy": results.get("strategy_snapshot"),
        "friction_assumptions": {
            "idealized": vars(ASSUMPTION_SETS["idealized"]),
            "reasonable": vars(ASSUMPTION_SETS["reasonable"]),
        },
        "score_bucket_results": _df_to_records(results.get("bucket_scores")),
        "stage_results": _df_to_records(results.get("bucket_stages")),
        "event_results": {
            "counts_by_type": (results["event_returns"]["event_type"].value_counts().to_dict()
                                if results.get("event_returns") is not None and not results["event_returns"].empty else {}),
        },
        "benchmark_results": {
            "spy": _dataclass_to_dict(results.get("spy_stats")),
            "equal_weight_universe": _dataclass_to_dict(results.get("equal_weight_stats")),
            "strategy_cumulative_return_pct_idealized": (
                float((results["strategy_curve_idealized"].iloc[-1] / results["strategy_curve_idealized"].iloc[0] - 1) * 100)
                if results.get("strategy_curve_idealized") is not None and not results["strategy_curve_idealized"].empty else None
            ),
            "strategy_cumulative_return_pct_reasonable": (
                float((results["strategy_curve_reasonable"].iloc[-1] / results["strategy_curve_reasonable"].iloc[0] - 1) * 100)
                if results.get("strategy_curve_reasonable") is not None and not results["strategy_curve_reasonable"].empty else None
            ),
        },
        "walk_forward_results": {
            "summary": results.get("wf_summary"),
            "by_year": _df_to_records(results.get("wf_by_year")),
        },
        "regime_results": {
            "distribution": (results["regime_series"]["label"].value_counts().to_dict()
                              if results.get("regime_series") is not None and not results["regime_series"].empty else {}),
        },
        "ticker_robustness": results.get("robustness_results"),
        "readiness_classification": PHASE9_READINESS,
        "readiness_rationale": PHASE9_READINESS_RATIONALE,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
    }


def save_baseline(results: dict) -> Path:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    summary = build_baseline_summary(results)
    with open(BASELINE_FILE, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    return BASELINE_FILE


def load_baseline() -> dict:
    if not BASELINE_FILE.exists():
        return {}
    with open(BASELINE_FILE) as f:
        return json.load(f)
