"""Historical signal-quality analysis - RESEARCH ONLY.

Measures how the EXISTING production technical signal (signals/engine.py,
via backtest/scoring.py's row-by-row reuse of it) has behaved historically -
it never adjusts signals/config.py or backtest/config.py based on what it
finds, and nothing here feeds back into trading/engine.py.

No-look-ahead guarantee: the score/stage at date t is computed by
backtest.scoring.compute_score_series, which (per that module's own
docstring) only ever uses information available at or before t - every
indicator is a trailing rolling calculation. Forward returns are computed
*after* that, purely for measurement: fwd_return_Nd at row t is
close[t+N]/close[t] - 1, which deliberately uses future data because it is
the OUTCOME being measured, never an input to the signal itself. Rows too
close to the end of history (no close[t+N] yet) get NaN forward returns and
are excluded from aggregation, not zero-filled.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import pandas as pd

from backtest.scoring import STAGE_ORDER, compute_score_series
from db.price_repository import load_price_history
from indicators.technical import MIN_REQUIRED_ROWS, enrich_with_indicators
from research.config import DEFAULT_HISTORICAL_QUALITY_CONFIG, HistoricalQualityConfig

_STAGE_LABEL_BY_RANK = {rank: label for label, rank in STAGE_ORDER.items()}


def compute_forward_returns(price_df: pd.DataFrame, horizons_days: Tuple[int, ...]) -> pd.DataFrame:
    """date/close plus one fwd_return_{h}d column per horizon. NaN wherever
    there aren't yet h future trading days of price history - never
    fabricated or interpolated."""
    df = price_df.sort_values("date").reset_index(drop=True)
    out = df[["date", "close"]].copy()
    for h in horizons_days:
        out[f"fwd_return_{h}d"] = df["close"].shift(-h) / df["close"] - 1.0
    return out


@dataclass
class HistoricalQualityData:
    ticker: str
    ok: bool
    reason: Optional[str] = None
    table: Optional[pd.DataFrame] = None                # per-day: date, score, stage_label, fwd_return_*d
    score_bucket_summary: Optional[pd.DataFrame] = None  # bucket x horizon aggregates
    stage_summary: Optional[pd.DataFrame] = None         # stage x horizon aggregates


def build_score_return_table(
    conn, ticker: str, config: HistoricalQualityConfig = DEFAULT_HISTORICAL_QUALITY_CONFIG,
) -> HistoricalQualityData:
    price_df = load_price_history(conn, ticker)
    if len(price_df) < MIN_REQUIRED_ROWS:
        return HistoricalQualityData(
            ticker=ticker, ok=False,
            reason=f"insufficient history: {len(price_df)} rows, need >= {MIN_REQUIRED_ROWS}",
        )

    enriched = enrich_with_indicators(price_df)
    scores = compute_score_series(enriched, ticker)  # date, score, stage_rank, ...
    forward_returns = compute_forward_returns(price_df, config.forward_return_horizons_days)

    merged = scores.merge(forward_returns, on="date", how="inner")
    merged = merged.dropna(subset=["score"]).copy()  # drop indicator warm-up rows
    merged["stage_label"] = merged["stage_rank"].map(_STAGE_LABEL_BY_RANK)

    if merged.empty:
        return HistoricalQualityData(ticker=ticker, ok=False, reason="no scoreable historical observations after indicator warm-up")

    return HistoricalQualityData(
        ticker=ticker, ok=True, table=merged,
        score_bucket_summary=bucket_by_score(merged, config),
        stage_summary=bucket_by_stage(merged, config),
    )


def _summarize_horizon(sub_df: pd.DataFrame, horizon_col: str, min_sample: int) -> Dict:
    values = sub_df[horizon_col].dropna()
    count = int(len(values))
    if count == 0:
        return {"count": 0, "mean_return_pct": None, "median_return_pct": None, "pct_positive": None, "sufficient_sample": False}

    sufficient = count >= min_sample
    return {
        "count": count,
        "mean_return_pct": round(float(values.mean()) * 100.0, 2) if sufficient else None,
        "median_return_pct": round(float(values.median()) * 100.0, 2) if sufficient else None,
        "pct_positive": round(float((values > 0).mean()) * 100.0, 1) if sufficient else None,
        "sufficient_sample": sufficient,
    }


def bucket_by_score(df: pd.DataFrame, config: HistoricalQualityConfig = DEFAULT_HISTORICAL_QUALITY_CONFIG) -> pd.DataFrame:
    rows = []
    for low, high, label in config.score_buckets:
        sub = df[(df["score"] >= low) & (df["score"] <= high)]
        for horizon in config.forward_return_horizons_days:
            rows.append({
                "bucket": label, "horizon_days": horizon,
                **_summarize_horizon(sub, f"fwd_return_{horizon}d", config.min_sample_size_for_stats),
            })
    return pd.DataFrame(rows)


def bucket_by_stage(df: pd.DataFrame, config: HistoricalQualityConfig = DEFAULT_HISTORICAL_QUALITY_CONFIG) -> pd.DataFrame:
    rows = []
    # Fixed stage order (not alphabetical / not "whatever appeared first")
    # so the table always reads none -> trend -> momentum -> volume.
    for stage_label in sorted(df["stage_label"].dropna().unique(), key=lambda s: STAGE_ORDER.get(s, -1)):
        sub = df[df["stage_label"] == stage_label]
        for horizon in config.forward_return_horizons_days:
            rows.append({
                "stage": stage_label, "horizon_days": horizon,
                **_summarize_horizon(sub, f"fwd_return_{horizon}d", config.min_sample_size_for_stats),
            })
    return pd.DataFrame(rows)
