"""Shared diagnostic utilities for Phase 10: drawdown-episode extraction
(B10), bootstrap confidence intervals (B19), and maximum favorable/adverse
excursion (B17). Pure functions operating on already-loaded price/equity
data - no Alpaca/DB access, no side effects.
"""
from typing import Optional

import numpy as np
import pandas as pd


def drawdown_episodes(equity: pd.Series) -> pd.DataFrame:
    """Every underwater episode (peak -> trough -> recovery) in an equity
    curve, sorted worst-first. Columns: peak_date, trough_date, end_date,
    depth_pct, duration_days (peak to recovery; NaT end_date/duration if
    still underwater at the end of the series)."""
    running_max = equity.cummax()
    underwater = equity < running_max

    episodes = []
    start_idx = None
    for i, (idx, is_uw) in enumerate(underwater.items()):
        if is_uw and start_idx is None:
            start_idx = i
        elif not is_uw and start_idx is not None:
            window = equity.iloc[start_idx:i]
            peak_val = running_max.iloc[start_idx]
            trough_idx_local = window.idxmin()
            depth = (window.min() / peak_val - 1.0) * 100
            episodes.append({
                "peak_date": equity.index[start_idx - 1] if start_idx > 0 else equity.index[0],
                "trough_date": trough_idx_local, "end_date": equity.index[i],
                "depth_pct": depth, "duration_days": i - (start_idx - 1 if start_idx > 0 else 0),
            })
            start_idx = None
    if start_idx is not None:
        window = equity.iloc[start_idx:]
        peak_val = running_max.iloc[start_idx]
        depth = (window.min() / peak_val - 1.0) * 100
        episodes.append({
            "peak_date": equity.index[start_idx - 1] if start_idx > 0 else equity.index[0],
            "trough_date": window.idxmin(), "end_date": None, "depth_pct": depth, "duration_days": None,
        })

    df = pd.DataFrame(episodes)
    return df.sort_values("depth_pct").reset_index(drop=True) if not df.empty else df


def bootstrap_mean_ci(values: pd.Series, n_boot: int = 2000, ci: float = 0.95, seed: int = 42) -> dict:
    values = pd.Series(values).dropna().to_numpy()
    n = len(values)
    if n < 5:
        return {"n": n, "mean": None, "ci_low": None, "ci_high": None}
    rng = np.random.default_rng(seed)
    boot_means = np.array([rng.choice(values, size=n, replace=True).mean() for _ in range(n_boot)])
    alpha = (1 - ci) / 2
    return {
        "n": n, "mean": float(values.mean()),
        "ci_low": float(np.quantile(boot_means, alpha)), "ci_high": float(np.quantile(boot_means, 1 - alpha)),
    }


def bootstrap_mean_diff_ci(values_a: pd.Series, values_b: pd.Series, n_boot: int = 2000, ci: float = 0.95, seed: int = 42) -> dict:
    """Bootstrap CI for mean(a) - mean(b), resampling each group
    independently (not a paired design - the two event groups, e.g. bullish
    vs non-bullish events, have different sizes/dates)."""
    a = pd.Series(values_a).dropna().to_numpy()
    b = pd.Series(values_b).dropna().to_numpy()
    if len(a) < 5 or len(b) < 5:
        return {"n_a": len(a), "n_b": len(b), "mean_diff": None, "ci_low": None, "ci_high": None}
    rng = np.random.default_rng(seed)
    diffs = np.array([
        rng.choice(a, size=len(a), replace=True).mean() - rng.choice(b, size=len(b), replace=True).mean()
        for _ in range(n_boot)
    ])
    alpha = (1 - ci) / 2
    return {
        "n_a": len(a), "n_b": len(b), "mean_diff": float(a.mean() - b.mean()),
        "ci_low": float(np.quantile(diffs, alpha)), "ci_high": float(np.quantile(diffs, 1 - alpha)),
    }


def max_favorable_adverse_excursion(price_df: pd.DataFrame, entry_index: int, entry_price: float, window: int) -> Optional[dict]:
    """MFE/MAE over `window` trading days after entry_index, using each
    day's High/Low relative to entry_price (not just closes) - the standard
    MFE/MAE definition. None if the window runs past the end of history."""
    end_index = entry_index + window
    if end_index >= len(price_df):
        return None
    path = price_df.iloc[entry_index + 1: end_index + 1]
    if path.empty:
        return None
    mfe = float(path["high"].max() / entry_price - 1.0) * 100
    mae = float(path["low"].min() / entry_price - 1.0) * 100
    return {"mfe_pct": mfe, "mae_pct": mae}
