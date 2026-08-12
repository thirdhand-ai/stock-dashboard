"""Per-ticker historical score/stage + forward-return tables for Phase 9.

Reuses indicators.technical.enrich_with_indicators and
backtest.scoring.compute_score_series directly - the exact same production
scoring implementation trading/signals_bridge.py and alerts/engine.py use -
so research and production cannot silently drift (Phase 9 spec item 3).

No-look-ahead: every indicator is a trailing rolling calculation (see
backtest/scoring.py's docstring), so the score/stage computed for row t only
ever uses data available at or before t. Forward returns are computed
strictly *after* scoring, purely as the outcome being measured - see
research/historical_quality.py's docstring for the same reasoning, which
this module follows.
"""
from typing import Dict, Iterable, Optional, Tuple

import pandas as pd

from backtest.scoring import STAGE_ORDER, compute_score_series
from db.price_repository import load_price_history
from indicators.technical import MIN_REQUIRED_ROWS, enrich_with_indicators
from research.historical_quality import compute_forward_returns
from strategy_lab.data import RESEARCH_SOURCE

_STAGE_LABEL_BY_RANK = {rank: label for label, rank in STAGE_ORDER.items()}

HORIZONS = (1, 5, 20, 60)


def build_ticker_signal_table(
    conn, ticker: str, horizons: Tuple[int, ...] = HORIZONS, source: str = RESEARCH_SOURCE,
) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    """Returns (table, failure_reason). table has columns:
    date, ticker, close, score, stage_rank, stage_label, fwd_return_{h}d...
    None (with a reason) if there isn't enough history to score at all -
    never a fabricated/partial table."""
    price_df = load_price_history(conn, ticker, source=source)
    if len(price_df) < MIN_REQUIRED_ROWS:
        return None, f"insufficient history: {len(price_df)} rows, need >= {MIN_REQUIRED_ROWS}"

    enriched = enrich_with_indicators(price_df)
    scores = compute_score_series(enriched, ticker)
    forward_returns = compute_forward_returns(price_df, horizons)

    merged = scores.merge(forward_returns, on="date", how="inner")
    merged = merged.dropna(subset=["score"]).copy()
    if merged.empty:
        return None, "no scoreable observations after indicator warm-up"

    merged["ticker"] = ticker
    merged["stage_label"] = merged["stage_rank"].map(_STAGE_LABEL_BY_RANK)
    merged["close"] = price_df.set_index("date").loc[merged["date"], "close"].values
    cols = ["date", "ticker", "close", "score", "stage_rank", "stage_label"] + [f"fwd_return_{h}d" for h in horizons]
    return merged[cols].reset_index(drop=True), None


def build_universe_signal_table(
    conn, tickers: Iterable[str], horizons: Tuple[int, ...] = HORIZONS, source: str = RESEARCH_SOURCE,
) -> Tuple[pd.DataFrame, Dict[str, str]]:
    """Pooled (concatenated) per-day score/stage/forward-return table across
    every ticker that had sufficient history, tagged by ticker. Returns
    (pooled_df, {ticker: failure_reason for tickers that couldn't be scored})."""
    frames = []
    failures: Dict[str, str] = {}
    for t in tickers:
        table, reason = build_ticker_signal_table(conn, t, horizons=horizons, source=source)
        if table is None:
            failures[t] = reason
            continue
        frames.append(table)

    pooled = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["date", "ticker", "close", "score", "stage_rank", "stage_label"] + [f"fwd_return_{h}d" for h in horizons]
    )
    return pooled, failures
