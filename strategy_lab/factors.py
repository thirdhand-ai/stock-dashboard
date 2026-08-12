"""Phase 8 research-factor validation (Phase 9 spec item 11) - tests whether
each Phase 8 research factor is independently associated with future
returns, without optimizing weights or touching research/composite.py's
Research Score.

Only relative strength and market regime can actually be validated
historically here: both are derived purely from price history, which is
available for the full 5-year research universe. Fundamentals
(research/fundamentals.py) and news sentiment (research/sentiment.py) are
NOT historically validated - db.fundamentals/db.news only ever store a
current snapshot (fundamentals.as_of is fetch time, not point-in-time
history; news is a ~7-day rolling window per research/config.py's
NEWS_LOOKBACK_DAYS), and only for the 7 production watchlist tickers, not
the research universe. Per Phase 9 spec item 11/12, that inadequacy is
stated explicitly rather than drawing a conclusion from too little data.
"""
from typing import Dict, Tuple

import numpy as np
import pandas as pd

from db.price_repository import load_price_history
from strategy_lab.data import RESEARCH_SOURCE

RS_LOOKBACK_DAYS = 63  # ~3 trading months, matching research/relative_strength.py's "3m" window
RS_QUANTILES = 4


def compute_historical_relative_strength(conn, ticker: str, benchmark: str = "SPY", source: str = RESEARCH_SOURCE) -> pd.DataFrame:
    """date, trailing_return, benchmark_trailing_return, relative_strength
    (ticker's trailing N-day return minus the benchmark's) for every date
    with enough trailing history. Purely price-derived, no look-ahead: the
    value at date t only uses closes at or before t."""
    df = load_price_history(conn, ticker, source=source).sort_values("date").reset_index(drop=True)
    bench = load_price_history(conn, benchmark, source=source).sort_values("date").reset_index(drop=True)
    if len(df) <= RS_LOOKBACK_DAYS or len(bench) <= RS_LOOKBACK_DAYS:
        return pd.DataFrame(columns=["date", "trailing_return", "benchmark_trailing_return", "relative_strength"])

    merged = df.merge(bench[["date", "close"]], on="date", how="inner", suffixes=("", "_bench"))
    merged["trailing_return"] = merged["close"] / merged["close"].shift(RS_LOOKBACK_DAYS) - 1.0
    merged["benchmark_trailing_return"] = merged["close_bench"] / merged["close_bench"].shift(RS_LOOKBACK_DAYS) - 1.0
    merged["relative_strength"] = merged["trailing_return"] - merged["benchmark_trailing_return"]
    return merged[["date", "trailing_return", "benchmark_trailing_return", "relative_strength"]].dropna()


def relative_strength_forward_return_quantiles(pooled_signal_table: pd.DataFrame, rs_by_ticker: Dict[str, pd.DataFrame], horizon: int) -> pd.DataFrame:
    """Merges each ticker's historical relative-strength value onto the
    pooled score/forward-return table (by ticker+date), buckets into
    quartiles, and reports mean forward return per quartile - answering
    "does relative strength add information beyond the technical score,
    independent of it" via a simple univariate quantile cut (not a
    regression - Phase 9 spec item 11/12 explicitly forbids optimizing
    weights here, so this stays a descriptive, not model-fitting, check)."""
    frames = []
    for ticker, rs_df in rs_by_ticker.items():
        if rs_df.empty:
            continue
        tagged = rs_df.copy()
        tagged["ticker"] = ticker
        frames.append(tagged)
    if not frames:
        return pd.DataFrame()
    rs_all = pd.concat(frames, ignore_index=True)

    merged = pooled_signal_table.merge(rs_all[["ticker", "date", "relative_strength"]], on=["ticker", "date"], how="inner")
    col = f"fwd_return_{horizon}d"
    merged = merged.dropna(subset=["relative_strength", col])
    if merged.empty:
        return pd.DataFrame()

    merged["rs_quartile"] = pd.qcut(merged["relative_strength"], RS_QUANTILES, labels=[f"Q{i+1}" for i in range(RS_QUANTILES)], duplicates="drop")
    g = merged.groupby("rs_quartile", observed=True)[col].agg(["count", "mean", "median"]).reset_index()
    g["mean_return_pct"] = g["mean"] * 100
    g["median_return_pct"] = g["median"] * 100
    return g.rename(columns={"count": "n"})[["rs_quartile", "n", "mean_return_pct", "median_return_pct"]]
