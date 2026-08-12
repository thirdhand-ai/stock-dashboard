"""Phase 10 event-level analysis: regime-tagged forward returns (B7),
regime-transition risk (B15), and failed-signal diagnostics (B16).

Reuses strategy_lab.events for the actual event detection (score_cross_70
transitions) and forward-return computation - never reimplements it, so
Phase 10's event definition cannot drift from Phase 9's.
"""
from typing import Tuple

import pandas as pd

from backtest.scoring import compute_score_series
from db.price_repository import load_price_history
from indicators.technical import MIN_REQUIRED_ROWS, enrich_with_indicators
from strategy_lab.analysis import _stats_row
from strategy_lab.data import RESEARCH_SOURCE
from strategy_lab.events import HORIZONS, compute_event_forward_returns, detect_transitions
from strategy_lab.phase10_experiments import BULLISH_LABEL

INDICATOR_SNAPSHOT_COLS = ["rsi", "adx", "macd", "macd_signal", "volume_ratio"]


def detect_ticker_events_with_features(conn, ticker: str, source: str = RESEARCH_SOURCE):
    """Same event transitions as strategy_lab.events.detect_ticker_events,
    additionally carrying the entry-time indicator snapshot needed for B16's
    success/failure feature comparison. A separate function (not a change to
    events.py) so Phase 9's event output shape/behavior is untouched."""
    price_df = load_price_history(conn, ticker, source=source)
    if len(price_df) < MIN_REQUIRED_ROWS:
        return None, None, f"insufficient history: {len(price_df)} rows"

    price_df = price_df.sort_values("date").reset_index(drop=True)
    enriched = enrich_with_indicators(price_df)
    scores = compute_score_series(enriched, ticker)
    df = price_df.merge(scores, on="date", how="inner").reset_index(drop=True)

    events_df = detect_transitions(df)
    if events_df.empty:
        return events_df, df, None

    enriched_indexed = enriched.reset_index(drop=True)
    for col in INDICATOR_SNAPSHOT_COLS:
        events_df[col] = events_df["event_index"].map(enriched_indexed[col])
    events_df.insert(2, "ticker", ticker)
    return events_df, df, None


def tag_events_with_regime(event_returns: pd.DataFrame, regime_series: pd.DataFrame) -> pd.DataFrame:
    """Tags each event with the regime label active on the SIGNAL date
    (the 'date' column - the day the score/stage transition was observed,
    i.e. information available at the time the signal fired), not the
    entry_date (T+1) - point-in-time correct: the regime gate a live system
    would apply is evaluated using T's completed bar, same as the score."""
    regime_by_date = regime_series.set_index("date")["label"]
    out = event_returns.copy()
    out["regime"] = out["date"].map(regime_by_date)
    return out


def split_by_regime(tagged_events: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    bullish = tagged_events[tagged_events["regime"] == BULLISH_LABEL]
    non_bullish = tagged_events[tagged_events["regime"].notna() & (tagged_events["regime"] != BULLISH_LABEL)]
    return tagged_events, bullish, non_bullish


def spy_entry_aligned_returns(spy_price_df: pd.DataFrame, horizons=HORIZONS) -> pd.DataFrame:
    """For every date in SPY's history, the return of entering at that
    date's next-session open and exiting h days later - the identical
    execution convention strategy_lab.events uses, so subtracting this from
    an event's own return is a true apples-to-apples relative comparison."""
    df = spy_price_df.sort_values("date").reset_index(drop=True)
    n = len(df)
    rows = []
    for i in range(n - 1):
        entry_price = df.at[i + 1, "open"]
        row = {"date": df.at[i, "date"]}
        for h in horizons:
            exit_idx = i + 1 + h
            row[f"spy_ret_{h}d"] = float(df.at[exit_idx, "close"] / entry_price - 1.0) if exit_idx < n else None
        rows.append(row)
    return pd.DataFrame(rows)


def event_stats_with_benchmark(events_df: pd.DataFrame, spy_lookup: pd.DataFrame, horizons=HORIZONS, friction: str = "reasonable") -> pd.DataFrame:
    merged = events_df.merge(spy_lookup, on="date", how="left")
    rows = []
    for h in horizons:
        col = f"net_return_{friction}_{h}d"
        stats = _stats_row(merged[col], h)
        spy_col = f"spy_ret_{h}d"
        paired = merged[[col, spy_col]].dropna()
        excess = float((paired[col] - paired[spy_col]).mean()) * 100 if not paired.empty and stats["sufficient_sample"] else None
        rows.append({"horizon_days": h, **stats, "spy_relative_mean_return_pct": excess})
    return pd.DataFrame(rows)


# --- B15: regime-transition risk ---

def time_to_regime_end(regime_series: pd.DataFrame, entry_date: str) -> "int | None":
    """Trading days from entry_date until the regime is next NOT
    bullish_trend (0 if entry_date itself isn't bullish - shouldn't occur
    for a properly-gated entry). None if bullish_trend persists through the
    end of available history (censored, not a known short/long duration)."""
    series = regime_series.sort_values("date").reset_index(drop=True)
    idx = series.index[series["date"] == entry_date]
    if len(idx) == 0:
        return None
    start = idx[0]
    for offset, label in enumerate(series["label"].iloc[start:]):
        if label != BULLISH_LABEL:
            return offset
    return None  # still bullish at end of sample - censored


def bucket_by_time_to_regime_end(events_with_ttl: pd.DataFrame) -> pd.DataFrame:
    """events_with_ttl must have a 'days_to_regime_end' column (None = censored,
    excluded from bucketing since its true value is unknown)."""
    df = events_with_ttl.dropna(subset=["days_to_regime_end"]).copy()
    bins = [(-1, 5, "1-5d"), (5, 20, "6-20d"), (20, float("inf"), ">20d")]
    df["ttl_bucket"] = None
    for lo, hi, label in bins:
        mask = (df["days_to_regime_end"] > lo) & (df["days_to_regime_end"] <= hi)
        df.loc[mask, "ttl_bucket"] = label
    return df


# --- B16: failed-signal analysis (entry-time-only features) ---

def classify_success_failure(events_df: pd.DataFrame, horizon_col: str = "net_return_reasonable_20d") -> pd.DataFrame:
    df = events_df.copy()
    df["outcome"] = df[horizon_col].apply(lambda v: "failed" if pd.notna(v) and v < 0 else ("succeeded" if pd.notna(v) else "unknown"))
    return df


def compare_success_vs_failure_features(events_df: pd.DataFrame, feature_cols) -> pd.DataFrame:
    rows = []
    for col in feature_cols:
        if col not in events_df.columns:
            continue
        succ = events_df.loc[events_df["outcome"] == "succeeded", col].dropna()
        fail = events_df.loc[events_df["outcome"] == "failed", col].dropna()
        rows.append({
            "feature": col, "n_succeeded": len(succ), "n_failed": len(fail),
            "mean_succeeded": float(succ.mean()) if len(succ) else None,
            "mean_failed": float(fail.mean()) if len(fail) else None,
        })
    return pd.DataFrame(rows)
