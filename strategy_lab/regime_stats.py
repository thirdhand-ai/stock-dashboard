"""Phase 10 spec item B4: regime distribution, duration, and transition
statistics from the point-in-time regime series (strategy_lab/regime_history.py).
"""
from typing import Dict

import pandas as pd

REGIME_LABELS = ("bullish_trend", "neutral_mixed", "bearish_trend", "elevated_volatility_risk_off")


def regime_distribution(regime_series: pd.DataFrame) -> pd.DataFrame:
    counts = regime_series["label"].value_counts()
    total = len(regime_series)
    rows = [{"label": label, "trading_days": int(counts.get(label, 0)), "pct_of_sample": round(counts.get(label, 0) / total * 100, 2) if total else None}
            for label in REGIME_LABELS]
    return pd.DataFrame(rows)


def _runs(labels: pd.Series):
    """Consecutive same-label run lengths, in order."""
    runs = []
    current_label, current_len = None, 0
    for label in labels:
        if label == current_label:
            current_len += 1
        else:
            if current_label is not None:
                runs.append((current_label, current_len))
            current_label, current_len = label, 1
    if current_label is not None:
        runs.append((current_label, current_len))
    return runs


def regime_duration_stats(regime_series: pd.DataFrame) -> pd.DataFrame:
    df = regime_series.sort_values("date")
    runs = _runs(df["label"].tolist())
    by_label: Dict[str, list] = {label: [] for label in REGIME_LABELS}
    for label, length in runs:
        by_label.setdefault(label, []).append(length)

    rows = []
    for label in REGIME_LABELS:
        lengths = by_label.get(label, [])
        if not lengths:
            rows.append({"label": label, "n_episodes": 0, "mean_duration_days": None, "median_duration_days": None, "max_duration_days": None})
            continue
        s = pd.Series(lengths)
        rows.append({
            "label": label, "n_episodes": len(lengths),
            "mean_duration_days": round(float(s.mean()), 1),
            "median_duration_days": float(s.median()),
            "max_duration_days": int(s.max()),
        })
    return pd.DataFrame(rows)


def transition_matrix(regime_series: pd.DataFrame) -> pd.DataFrame:
    """Rows = FROM label, columns = TO label, values = count of day-to-day
    transitions (including same-label 'transitions', i.e. staying put)."""
    df = regime_series.sort_values("date").reset_index(drop=True)
    labels = df["label"]
    pairs = list(zip(labels[:-1], labels[1:]))
    matrix = pd.DataFrame(0, index=REGIME_LABELS, columns=REGIME_LABELS)
    for a, b in pairs:
        if a in matrix.index and b in matrix.columns:
            matrix.loc[a, b] += 1
    return matrix


def transition_frequency(regime_series: pd.DataFrame) -> dict:
    """How often the regime actually CHANGES (excludes same-label days)."""
    df = regime_series.sort_values("date").reset_index(drop=True)
    labels = df["label"]
    if len(labels) < 2:
        return {"n_transitions": 0, "n_days": len(labels), "transition_rate_pct": None}
    changes = int((labels.shift(1) != labels).sum()) - 1  # first row isn't a transition
    changes = max(changes, 0)
    return {"n_transitions": changes, "n_days": len(labels), "transition_rate_pct": round(changes / (len(labels) - 1) * 100, 2)}


def is_bullish_sufficiently_selective(distribution_df: pd.DataFrame, max_share_pct: float = 70.0) -> bool:
    """A regime filter that's 'bullish' 95% of the time isn't really
    filtering anything. Flags whether bullish_trend's share of the sample
    is low enough to represent a meaningful selection, not a near-tautology."""
    row = distribution_df[distribution_df["label"] == "bullish_trend"]
    if row.empty or row["pct_of_sample"].iloc[0] is None:
        return False
    return row["pct_of_sample"].iloc[0] <= max_share_pct
