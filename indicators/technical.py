"""Compute technical indicators (RSI, MACD, Bollinger Bands, ADX, 50-day MA,
volume vs. 20-day average) for any ticker's OHLCV history using pandas-ta.

Two entry points, both reusable for any ticker:
  - enrich_with_indicators(df): full per-row indicator series for an entire
    history (used by Phase 3 backtesting, which needs a value at every bar,
    not just the latest one).
  - compute_indicators(df, ticker): the latest-row snapshot used by the
    Phase 2 signal engine / dashboard. Internally calls enrich_with_indicators
    so both entry points share one indicator implementation.

Every indicator here is a trailing rolling calculation (never centered), so
a value at row i only ever depends on rows <= i - no future information
leaks into any row's value. Never raises on bad/short data - callers get
IndicatorResult(ok=False, reason=...) instead.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import pandas_ta as ta

from db.price_repository import load_price_history

logger = logging.getLogger(__name__)

# --- Indicator settings (explicit, not scattered magic numbers) ---
RSI_LENGTH = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BBANDS_LENGTH = 20
BBANDS_STD = 2.0
ADX_LENGTH = 14
MA_LENGTH = 50
VOLUME_AVG_LENGTH = 20

REQUIRED_COLUMNS = {"date", "open", "high", "low", "close", "volume"}

# Longest lookback (50-day MA) plus ADX's internal smoothing warm-up, so the
# latest row is guaranteed a real (non-NaN) value for every indicator.
MIN_REQUIRED_ROWS = MA_LENGTH + ADX_LENGTH


@dataclass
class IndicatorResult:
    ticker: str
    ok: bool
    reason: Optional[str] = None
    latest_date: Optional[str] = None
    close: Optional[float] = None
    rsi: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_mid: Optional[float] = None
    bb_upper: Optional[float] = None
    adx: Optional[float] = None
    sma_50: Optional[float] = None
    volume: Optional[float] = None
    volume_avg_20: Optional[float] = None
    volume_ratio: Optional[float] = None
    history: Optional[pd.DataFrame] = None  # full indicator-enriched frame, useful for charting later


def _first_col_with_prefix(df: pd.DataFrame, prefix: str) -> str:
    for col in df.columns:
        if col.startswith(prefix):
            return col
    raise KeyError(f"no column starting with {prefix!r} in {list(df.columns)}")


def enrich_with_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute the full rolling indicator series for an entire OHLCV history.

    Returns a copy of `df` (sorted ascending by date) with added columns:
    rsi, macd, macd_signal, macd_hist, bb_lower, bb_mid, bb_upper, adx,
    sma_50, volume_avg_20, volume_ratio.

    Early rows within each indicator's warm-up window are NaN by design -
    this function does not enforce MIN_REQUIRED_ROWS or check the latest
    row; callers decide how to handle warm-up (compute_indicators errors if
    the *latest* row is still NaN; backtesting code simply skips those bars).
    Raises ValueError on empty input or missing required columns.
    """
    if df is None or df.empty:
        raise ValueError("no price data available")

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"missing columns: {sorted(missing)}")

    out = df.sort_values("date").reset_index(drop=True).copy()

    out["rsi"] = ta.rsi(out["close"], length=RSI_LENGTH)

    macd = ta.macd(out["close"], fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    out["macd"] = macd[_first_col_with_prefix(macd, "MACD_")]
    out["macd_signal"] = macd[_first_col_with_prefix(macd, "MACDs_")]
    out["macd_hist"] = macd[_first_col_with_prefix(macd, "MACDh_")]

    bbands = ta.bbands(out["close"], length=BBANDS_LENGTH, std=BBANDS_STD)
    out["bb_lower"] = bbands[_first_col_with_prefix(bbands, "BBL_")]
    out["bb_mid"] = bbands[_first_col_with_prefix(bbands, "BBM_")]
    out["bb_upper"] = bbands[_first_col_with_prefix(bbands, "BBU_")]

    adx = ta.adx(out["high"], out["low"], out["close"], length=ADX_LENGTH)
    out["adx"] = adx[_first_col_with_prefix(adx, "ADX_")]

    out["sma_50"] = ta.sma(out["close"], length=MA_LENGTH)
    out["volume_avg_20"] = ta.sma(out["volume"], length=VOLUME_AVG_LENGTH)
    out["volume_ratio"] = out["volume"] / out["volume_avg_20"]

    return out


def compute_indicators(df: pd.DataFrame, ticker: str) -> IndicatorResult:
    """Compute the latest-row indicator snapshot for one ticker's OHLCV history.

    `df` must have columns: date, open, high, low, close, volume, sorted
    ascending by date (duplicate/unsorted input is sorted defensively).
    """
    if df is None or df.empty:
        return IndicatorResult(ticker=ticker, ok=False, reason="no price data available")

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        return IndicatorResult(ticker=ticker, ok=False, reason=f"missing columns: {sorted(missing)}")

    if len(df) < MIN_REQUIRED_ROWS:
        return IndicatorResult(
            ticker=ticker,
            ok=False,
            reason=f"insufficient history: {len(df)} rows, need >= {MIN_REQUIRED_ROWS}",
        )

    try:
        out = enrich_with_indicators(df)
    except Exception as e:
        logger.error("indicator computation failed for %s: %s", ticker, e)
        return IndicatorResult(ticker=ticker, ok=False, reason=f"computation error: {e}")

    latest = out.iloc[-1]
    key_fields = ["rsi", "macd", "macd_signal", "adx", "sma_50", "volume_avg_20"]
    nan_fields = [name for name in key_fields if pd.isna(latest[name])]
    if nan_fields:
        return IndicatorResult(
            ticker=ticker,
            ok=False,
            reason=f"latest row has NaN values for: {nan_fields} (still within indicator warm-up window)",
            history=out,
        )

    return IndicatorResult(
        ticker=ticker,
        ok=True,
        latest_date=str(latest["date"]),
        close=float(latest["close"]),
        rsi=float(latest["rsi"]),
        macd=float(latest["macd"]),
        macd_signal=float(latest["macd_signal"]),
        macd_hist=float(latest["macd_hist"]),
        bb_lower=float(latest["bb_lower"]),
        bb_mid=float(latest["bb_mid"]),
        bb_upper=float(latest["bb_upper"]),
        adx=float(latest["adx"]),
        sma_50=float(latest["sma_50"]),
        volume=float(latest["volume"]),
        volume_avg_20=float(latest["volume_avg_20"]),
        volume_ratio=float(latest["volume_ratio"]),
        history=out,
    )


def compute_indicators_for_ticker(conn, ticker: str, source: Optional[str] = None) -> IndicatorResult:
    """Load a ticker's stored price history from SQLite and compute indicators."""
    df = load_price_history(conn, ticker, source=source)
    return compute_indicators(df, ticker)
