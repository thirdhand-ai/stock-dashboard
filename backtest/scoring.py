"""Per-bar signal scoring for backtesting - reuses signals/engine.py's
score_indicators() row-by-row rather than reimplementing the scoring rules.

Because every indicator in indicators/technical.py is a trailing rolling
calculation, computing it once across a whole price history and then reading
off the value at each row is equivalent to recomputing it fresh at each
point in time: row i's value never depends on rows > i. So looping
score_indicators() over each row of an already-enriched history carries no
look-ahead risk - it produces exactly what a live system would have seen at
that bar.
"""
from typing import Dict

import pandas as pd

from indicators.technical import IndicatorResult
from signals.config import DEFAULT_THRESHOLDS, DEFAULT_WEIGHTS, SignalThresholds, SignalWeights
from signals.engine import STAGE_MOMENTUM, STAGE_NONE, STAGE_TREND, STAGE_VOLUME, score_indicators

# Numeric encoding of highest_confirmed_stage, for use inside Backtesting.py
# (which needs plain numeric arrays, not strings).
STAGE_ORDER: Dict[str, int] = {
    STAGE_NONE: 0,
    STAGE_TREND: 1,
    STAGE_MOMENTUM: 2,
    STAGE_VOLUME: 3,
}

INDICATOR_FIELDS = ["rsi", "macd", "macd_signal", "adx", "sma_50", "volume_avg_20"]


def compute_score_series(
    enriched_df: pd.DataFrame,
    ticker: str,
    thresholds: SignalThresholds = DEFAULT_THRESHOLDS,
    weights: SignalWeights = DEFAULT_WEIGHTS,
) -> pd.DataFrame:
    """Apply signals.engine.score_indicators to every row of an
    indicator-enriched history (as produced by indicators.technical.enrich_with_indicators).

    Returns a DataFrame indexed the same as enriched_df with columns:
    score, stage_rank, trend_confirmed, momentum_confirmed, volume_confirmed.
    Rows still inside the indicator warm-up window (any NaN indicator) get
    score=NaN / stage_rank=NaN and all confirmed flags False.
    """
    records = []
    for row in enriched_df.itertuples(index=False):
        if any(pd.isna(getattr(row, field)) for field in INDICATOR_FIELDS):
            records.append({
                "date": row.date,
                "score": float("nan"),
                "stage_rank": float("nan"),
                "trend_confirmed": False,
                "momentum_confirmed": False,
                "volume_confirmed": False,
            })
            continue

        indicators = IndicatorResult(
            ticker=ticker,
            ok=True,
            latest_date=str(row.date),
            close=row.close,
            rsi=row.rsi,
            macd=row.macd,
            macd_signal=row.macd_signal,
            macd_hist=row.macd_hist,
            bb_lower=row.bb_lower,
            bb_mid=row.bb_mid,
            bb_upper=row.bb_upper,
            adx=row.adx,
            sma_50=row.sma_50,
            volume=row.volume,
            volume_avg_20=row.volume_avg_20,
            volume_ratio=row.volume_ratio,
        )
        s = score_indicators(indicators, thresholds=thresholds, weights=weights)
        records.append({
            "date": row.date,
            "score": s.score,
            "stage_rank": STAGE_ORDER[s.highest_confirmed_stage],
            "trend_confirmed": s.trend_confirmed,
            "momentum_confirmed": s.momentum_confirmed,
            "volume_confirmed": s.volume_confirmed,
        })

    return pd.DataFrame.from_records(records)
