"""Signal EVENT detection and event-based forward returns (Phase 9 spec item
5) - this is the analysis that approximates how the actual alert/trading
engine behaves: it fires once on a *transition* (a crossing/advancement),
not on every consecutive day a condition happens to still be true.

Events detected, mirroring what alerts/engine.py and trading/signals_bridge.py
actually react to:
  - score_cross_70: first day score >= backtest.config.DEFAULT_RULES.entry_min_score
    after a day it was below that threshold (reuses the production threshold,
    not a re-invented one).
  - momentum_advance: first day highest_confirmed_stage reaches "momentum"
    after a day it had not.
  - volume_advance: first day highest_confirmed_stage reaches "volume" after
    a day it had not.

Entry timing follows Phase 9 spec item 7: a signal generated from date T's
completed daily bar enters no earlier than T+1 (the next trading session's
open) - never the same close that generated the signal.
"""
from typing import Dict, Iterable, Tuple

import pandas as pd

from backtest.config import DEFAULT_RULES
from backtest.scoring import STAGE_ORDER, compute_score_series
from db.price_repository import load_price_history
from indicators.technical import MIN_REQUIRED_ROWS, enrich_with_indicators
from strategy_lab.data import RESEARCH_SOURCE
from strategy_lab.execution import ASSUMPTION_SETS, round_trip_net_return

EVENT_TYPES = ("score_cross_70", "momentum_advance", "volume_advance")
HORIZONS = (1, 5, 20, 60)

_MOMENTUM_RANK = STAGE_ORDER["momentum"]
_VOLUME_RANK = STAGE_ORDER["volume"]
_SCORE_THRESHOLD = DEFAULT_RULES.entry_min_score
_STAGE_LABEL_BY_RANK = {rank: label for label, rank in STAGE_ORDER.items()}


def detect_transitions(df: pd.DataFrame) -> pd.DataFrame:
    """Pure transition-detection logic, factored out of detect_ticker_events
    for direct unit testing without needing to reverse-engineer real
    indicator thresholds: given a DataFrame with score/stage_rank columns
    (already computed, in date order), returns one row per event
    (event_index, event_type) for each score/stage transition - never for a
    day a condition merely continues to hold."""
    df = df.copy()
    df["stage_label"] = df["stage_rank"].map(_STAGE_LABEL_BY_RANK)
    df["prev_score"] = df["score"].shift(1)
    df["prev_stage_rank"] = df["stage_rank"].shift(1)

    is_score_cross = (df["score"] >= _SCORE_THRESHOLD) & (df["prev_score"] < _SCORE_THRESHOLD)
    is_momentum_adv = (df["stage_rank"] >= _MOMENTUM_RANK) & (df["prev_stage_rank"] < _MOMENTUM_RANK)
    is_volume_adv = (df["stage_rank"] >= _VOLUME_RANK) & (df["prev_stage_rank"] < _VOLUME_RANK)

    rows = []
    for event_type, mask in (("score_cross_70", is_score_cross), ("momentum_advance", is_momentum_adv), ("volume_advance", is_volume_adv)):
        idxs = df.index[mask.fillna(False)]
        for idx in idxs:
            rows.append({
                "event_index": int(idx), "event_type": event_type,
                "date": df.at[idx, "date"], "score": df.at[idx, "score"], "stage_label": df.at[idx, "stage_label"],
            })
    return pd.DataFrame(rows, columns=["event_index", "event_type", "date", "score", "stage_label"])


def detect_ticker_events(conn, ticker: str, source: str = RESEARCH_SOURCE):
    """(events_df, price_df, failure_reason). events_df has one row per
    detected event (event_index into price_df, event_type, date, score)."""
    price_df = load_price_history(conn, ticker, source=source)
    if len(price_df) < MIN_REQUIRED_ROWS:
        return None, None, f"insufficient history: {len(price_df)} rows"

    price_df = price_df.sort_values("date").reset_index(drop=True)
    enriched = enrich_with_indicators(price_df)
    scores = compute_score_series(enriched, ticker)
    df = price_df.merge(scores, on="date", how="inner").reset_index(drop=True)

    events_df = detect_transitions(df)
    if not events_df.empty:
        events_df.insert(2, "ticker", ticker)
    return events_df, df, None


def compute_event_forward_returns(
    events_df: pd.DataFrame, price_df: pd.DataFrame, horizons: Tuple[int, ...] = HORIZONS,
) -> pd.DataFrame:
    """For each event, enter at next session's open (event_index + 1), then
    compute gross + friction-adjusted (idealized/reasonable) net returns at
    each horizon (close[entry_index + h] vs entry price). Rows where the
    next session or the horizon's exit bar doesn't exist yet are dropped,
    never fabricated."""
    if events_df.empty:
        cols = ["event_index", "event_type", "ticker", "date", "score", "stage_label", "entry_date"]
        for h in horizons:
            cols += [f"gross_return_{h}d", f"net_return_idealized_{h}d", f"net_return_reasonable_{h}d"]
        return pd.DataFrame(columns=cols)

    n = len(price_df)
    out_rows = []
    for _, ev in events_df.iterrows():
        entry_idx = int(ev["event_index"]) + 1
        if entry_idx >= n:
            continue
        entry_price = price_df.at[entry_idx, "open"]
        row = dict(ev)
        row["entry_date"] = price_df.at[entry_idx, "date"]
        for h in horizons:
            exit_idx = entry_idx + h
            if exit_idx >= n:
                row[f"gross_return_{h}d"] = None
                row[f"net_return_idealized_{h}d"] = None
                row[f"net_return_reasonable_{h}d"] = None
                continue
            exit_price = price_df.at[exit_idx, "close"]
            gross = float(exit_price / entry_price - 1.0)
            row[f"gross_return_{h}d"] = gross
            row[f"net_return_idealized_{h}d"] = round_trip_net_return(gross, ASSUMPTION_SETS["idealized"])
            row[f"net_return_reasonable_{h}d"] = round_trip_net_return(gross, ASSUMPTION_SETS["reasonable"])
        out_rows.append(row)

    return pd.DataFrame(out_rows)


def build_universe_event_returns(conn, tickers: Iterable[str], horizons: Tuple[int, ...] = HORIZONS, source: str = RESEARCH_SOURCE) -> Tuple[pd.DataFrame, Dict[str, str]]:
    frames = []
    failures: Dict[str, str] = {}
    for t in tickers:
        events_df, price_df, reason = detect_ticker_events(conn, t, source=source)
        if reason is not None:
            failures[t] = reason
            continue
        if events_df.empty:
            continue
        frames.append(compute_event_forward_returns(events_df, price_df, horizons=horizons))

    pooled = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return pooled, failures
