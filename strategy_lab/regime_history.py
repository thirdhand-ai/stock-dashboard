"""Historical market-regime classification for every date, not just 'now'
(Phase 9 spec item 9).

research/regime.py's compute_market_regime() only returns the CURRENT regime
(a single point-in-time read). This module reuses its exact thresholds and
labeling logic (research.config.RegimeConfig, imported not duplicated) but
applies them at every historical date via rolling/vectorized pandas
operations, so each date in the 5-year sample can be tagged with the regime
that was in effect at the time - still using only trailing information
available as of that date (rolling SMA/vol/drawdown), so no look-ahead.
"""
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from db.price_repository import load_price_history
from research.config import DEFAULT_REGIME_CONFIG, RegimeConfig
from research.regime import TRADING_DAYS_PER_YEAR
from strategy_lab.data import RESEARCH_SOURCE


def compute_historical_regime_series(conn, config: RegimeConfig = DEFAULT_REGIME_CONFIG, source: str = RESEARCH_SOURCE) -> pd.DataFrame:
    """Returns date, label, sma_short, sma_long, realized_vol_annualized,
    drawdown_pct for every date with enough trailing history for both moving
    averages. Empty DataFrame if there isn't enough SPY history."""
    df = load_price_history(conn, config.primary_benchmark, source=source)
    if df.empty or len(df) < config.sma_long_days:
        return pd.DataFrame(columns=["date", "label", "sma_short", "sma_long", "realized_vol_annualized", "drawdown_pct"])

    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"]

    sma_short = close.rolling(config.sma_short_days).mean()
    sma_long = close.rolling(config.sma_long_days).mean()

    daily_returns = close.pct_change()
    realized_vol_annualized = daily_returns.rolling(config.realized_vol_window_days).std() * np.sqrt(TRADING_DAYS_PER_YEAR)

    rolling_max = close.rolling(config.drawdown_window_days).max()
    drawdown_pct = (close / rolling_max - 1.0) * 100.0

    elevated = realized_vol_annualized >= config.elevated_vol_annualized_threshold
    bullish = (close > sma_short) & (sma_short > sma_long)
    bearish = (close < sma_short) & (sma_short < sma_long)

    label = np.select(
        [elevated, bullish, bearish],
        [config.LABEL_ELEVATED_VOL, config.LABEL_BULLISH, config.LABEL_BEARISH],
        default=config.LABEL_NEUTRAL,
    )

    out = pd.DataFrame({
        "date": df["date"],
        "label": label,
        "sma_short": sma_short,
        "sma_long": sma_long,
        "realized_vol_annualized": realized_vol_annualized,
        "drawdown_pct": drawdown_pct,
    })
    return out.dropna(subset=["sma_long"]).reset_index(drop=True)


def _select_as_of_row(regime_series: pd.DataFrame, as_of_date: Optional[str] = None) -> Optional[pd.Series]:
    """Extracted, not new, selection logic - identical to regime_label_as_of's
    existing body. Returns the selected row (Series with 'date'/'label'
    among its fields) or None under the exact same conditions
    regime_label_as_of already documents (empty series; as_of_date before
    every row)."""
    if regime_series.empty:
        return None
    if as_of_date is None:
        return regime_series.iloc[-1]
    eligible = regime_series[regime_series["date"] <= as_of_date]
    if eligible.empty:
        return None
    return eligible.iloc[-1]


def regime_label_as_of(regime_series: pd.DataFrame, as_of_date: Optional[str] = None) -> Optional[str]:
    """Point-in-time regime lookup used ONLY by
    strategy_lab.prospective.build_todays_observation (never by report.py/
    portfolio_simulator.py/report_phase10.py, which keep their own exact-
    match usage unchanged).

    as_of_date=None: returns the single most recent available label
    (regime_series.iloc[-1]["label"]) - equivalent to today's existing
    behavior for the ad-hoc/no-date CLI path
    (strategy_lab/run_prospective_observation.py:28 calls
    build_todays_observation(conn, ticker) with no as_of_date).

    as_of_date=a date string: returns the label of the most recent row in
    regime_series with date <= as_of_date (regime_series is guaranteed
    sorted ascending by compute_historical_regime_series's own
    .sort_values("date").reset_index(drop=True), :29). NEVER considers a
    date > as_of_date - no look-ahead. Returns None (never fabricates a
    label) if regime_series is empty, or if every row's date is >
    as_of_date (insufficient trailing history existed as of that date -
    a real 'no signal' outcome).

    UNCHANGED public contract (Phase 16 §1.1) - now implemented via
    _select_as_of_row so there is exactly one selection logic, not two
    (Phase 17 §1.2)."""
    row = _select_as_of_row(regime_series, as_of_date)
    return row["label"] if row is not None else None


def regime_label_and_date_as_of(regime_series: pd.DataFrame, as_of_date: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """New companion to regime_label_as_of (Phase 17 Area B) - same
    selection, but also returns the actual `date` of the row selected (the
    real SPY/benchmark bar date backing this label), distinct from
    as_of_date/observation_date. (None, None) under the exact same
    conditions regime_label_as_of returns None. Used ONLY by
    strategy_lab.prospective.build_todays_observation - all other existing
    callers of regime_label_as_of (there are none besides prospective.py
    today) are unaffected."""
    row = _select_as_of_row(regime_series, as_of_date)
    if row is None:
        return None, None
    return row["label"], row["date"]


REGIME_FRESHNESS_FRESH = "FRESH"
REGIME_FRESHNESS_STALE = "STALE"
REGIME_FRESHNESS_UNAVAILABLE = "UNAVAILABLE"


def classify_regime_freshness(observation_date: Optional[str], benchmark_data_date: Optional[str]) -> str:
    """Pure, deterministic, no DB access.
      UNAVAILABLE: benchmark_data_date is None (no eligible history at all
                   as of observation_date - regime_series empty, or every
                   row's date > observation_date).
      FRESH:       benchmark_data_date == observation_date (the benchmark's
                   own latest bar covers the observation's own date - the
                   normal case once Area A's refresh succeeds).
      STALE:       benchmark_data_date is not None and < observation_date
                   (a real, known label exists but it lags - whether
                   because the fetch failed, Alpaca hasn't posted the bar
                   yet, or this ticker's observation_date is itself in the
                   past relative to when this function is called)."""
    if benchmark_data_date is None:
        return REGIME_FRESHNESS_UNAVAILABLE
    if observation_date is not None and benchmark_data_date == observation_date:
        return REGIME_FRESHNESS_FRESH
    return REGIME_FRESHNESS_STALE
