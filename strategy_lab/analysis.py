"""Bucket/stage forward-return aggregation with basic statistical caution
(Phase 9 spec items 4 and 12).

Extends research/historical_quality.py's bucket_by_score/bucket_by_stage
pattern (same input shape) with standard deviation, a naive confidence
interval, and benchmark-relative return - none of which exist in the Phase 8
version, which this module does not modify.

Overlapping-observation caution: forward returns at horizons > 1 trading day
are constructed from overlapping windows (consecutive rows share most of
their horizon), so they are not independent draws. The standard
mean/std/SE/CI computed here treats them as if they were ("naive"), which
understates the true standard error at longer horizons - every horizon > 1
result is explicitly labeled `overlapping_observations=True` and paired with
a rough `effective_n` (n / horizon) so this isn't overstated as more
statistically powerful than it is. See Phase 9 report section 12/15 for how
this is surfaced.
"""
import math
from typing import Optional

import pandas as pd

from strategy_lab.frozen_strategy import SCORE_BUCKETS, STAGE_LABELS

MIN_SAMPLE_FOR_STATS = 30


def _stats_row(values: pd.Series, horizon: int, min_sample: int = MIN_SAMPLE_FOR_STATS) -> dict:
    values = values.dropna()
    n = int(len(values))
    if n == 0:
        return {
            "n": 0, "mean_return_pct": None, "median_return_pct": None, "std_pct": None,
            "pct_positive": None, "se_pct": None, "ci95_low_pct": None, "ci95_high_pct": None,
            "effective_n": 0, "overlapping_observations": horizon > 1, "sufficient_sample": False,
        }

    mean = float(values.mean())
    median = float(values.median())
    std = float(values.std(ddof=1)) if n > 1 else 0.0
    pct_pos = float((values > 0).mean())
    se = std / math.sqrt(n) if n > 1 else None
    ci_low = (mean - 1.96 * se) if se is not None else None
    ci_high = (mean + 1.96 * se) if se is not None else None
    effective_n = max(1, round(n / horizon))
    sufficient = n >= min_sample

    return {
        "n": n,
        "mean_return_pct": round(mean * 100, 3) if sufficient else None,
        "median_return_pct": round(median * 100, 3) if sufficient else None,
        "std_pct": round(std * 100, 3) if sufficient else None,
        "pct_positive": round(pct_pos * 100, 1) if sufficient else None,
        "se_pct": round(se * 100, 3) if (sufficient and se is not None) else None,
        "ci95_low_pct": round(ci_low * 100, 3) if (sufficient and ci_low is not None) else None,
        "ci95_high_pct": round(ci_high * 100, 3) if (sufficient and ci_high is not None) else None,
        "effective_n": effective_n,
        "overlapping_observations": horizon > 1,
        "sufficient_sample": sufficient,
    }


def _benchmark_relative_row(values: pd.Series, bench_values: pd.Series, sufficient: bool) -> dict:
    paired = pd.concat([values, bench_values], axis=1).dropna()
    if paired.empty or not sufficient:
        return {"n_paired": int(len(paired)), "excess_mean_return_pct": None}
    excess = paired.iloc[:, 0] - paired.iloc[:, 1]
    return {"n_paired": int(len(paired)), "excess_mean_return_pct": round(float(excess.mean()) * 100, 3)}


def attach_benchmark_forward_returns(pooled: pd.DataFrame, benchmark_fwd: pd.DataFrame, horizons) -> pd.DataFrame:
    """Left-joins benchmark (e.g. SPY) forward returns onto the pooled table
    by date, suffixed `_bench`, for benchmark-relative bucket/stage stats."""
    bench_cols = ["date"] + [f"fwd_return_{h}d" for h in horizons]
    bench = benchmark_fwd[bench_cols].rename(columns={f"fwd_return_{h}d": f"fwd_return_{h}d_bench" for h in horizons})
    return pooled.merge(bench, on="date", how="left")


def bucket_by_score_pooled(pooled: pd.DataFrame, horizons) -> pd.DataFrame:
    rows = []
    for lo, hi, label in SCORE_BUCKETS:
        sub = pooled[(pooled["score"] >= lo) & (pooled["score"] <= hi)]
        for h in horizons:
            stats = _stats_row(sub[f"fwd_return_{h}d"], h)
            bench_col = f"fwd_return_{h}d_bench"
            bench_stats = (_benchmark_relative_row(sub[f"fwd_return_{h}d"], sub[bench_col], stats["sufficient_sample"])
                            if bench_col in sub.columns else {"n_paired": 0, "excess_mean_return_pct": None})
            rows.append({"bucket": label, "horizon_days": h, **stats, **bench_stats})
    return pd.DataFrame(rows)


def bucket_by_stage_pooled(pooled: pd.DataFrame, horizons) -> pd.DataFrame:
    rows = []
    for stage in STAGE_LABELS:
        sub = pooled[pooled["stage_label"] == stage]
        for h in horizons:
            stats = _stats_row(sub[f"fwd_return_{h}d"], h)
            bench_col = f"fwd_return_{h}d_bench"
            bench_stats = (_benchmark_relative_row(sub[f"fwd_return_{h}d"], sub[bench_col], stats["sufficient_sample"])
                            if bench_col in sub.columns else {"n_paired": 0, "excess_mean_return_pct": None})
            rows.append({"stage": stage, "horizon_days": h, **stats, **bench_stats})
    return pd.DataFrame(rows)


def per_ticker_bucket_return(pooled: pd.DataFrame, bucket_label: str, horizon: int, min_score: float, max_score: float) -> pd.DataFrame:
    """Per-ticker mean forward return for one score bucket/horizon - the
    cross-sectional robustness input (Phase 9 spec item 10)."""
    sub = pooled[(pooled["score"] >= min_score) & (pooled["score"] <= max_score)]
    col = f"fwd_return_{horizon}d"
    g = sub.groupby("ticker")[col].agg(["count", "mean", "median"]).reset_index()
    g = g.rename(columns={"count": "n", "mean": "mean_return", "median": "median_return"})
    g["mean_return_pct"] = g["mean_return"] * 100
    g["median_return_pct"] = g["median_return"] * 100
    return g.sort_values("mean_return_pct", ascending=False).reset_index(drop=True)
