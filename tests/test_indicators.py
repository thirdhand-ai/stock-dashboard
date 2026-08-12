"""Unit tests for indicators/technical.py using synthetic OHLCV data.

These check that the indicator math produces sane values and that bad/short
input is handled gracefully (no exceptions), not that specific real-world
values come out a certain way - that's what scripts/verify_phase2.py
demonstrates against real market data.
"""
import numpy as np
import pandas as pd
import pytest

from indicators.technical import MIN_REQUIRED_ROWS, compute_indicators


def make_ohlcv(n_rows, seed=0, trend=0.3, start_price=100.0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n_rows)
    noise = rng.normal(0, 1.0, n_rows)
    close = start_price + np.cumsum(trend + noise)
    close = np.maximum(close, 1.0)  # keep prices positive
    high = close + rng.uniform(0.1, 1.0, n_rows)
    low = close - rng.uniform(0.1, 1.0, n_rows)
    open_ = close + rng.uniform(-0.5, 0.5, n_rows)
    volume = rng.integers(1_000_000, 2_000_000, n_rows)
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


def test_sufficient_data_produces_sane_values():
    df = make_ohlcv(MIN_REQUIRED_ROWS + 20)
    result = compute_indicators(df, "TEST")

    assert result.ok, result.reason
    assert 0.0 <= result.rsi <= 100.0
    assert result.adx >= 0.0
    assert result.bb_lower <= result.bb_mid <= result.bb_upper
    assert result.sma_50 > 0
    assert result.volume_avg_20 > 0
    assert result.volume_ratio == pytest.approx(result.volume / result.volume_avg_20)
    assert result.history is not None and len(result.history) == MIN_REQUIRED_ROWS + 20


def test_insufficient_history_fails_gracefully():
    df = make_ohlcv(10)
    result = compute_indicators(df, "TEST")

    assert result.ok is False
    assert "insufficient history" in result.reason


def test_empty_dataframe_fails_gracefully():
    result = compute_indicators(pd.DataFrame(), "TEST")

    assert result.ok is False
    assert "no price data" in result.reason


def test_missing_columns_fail_gracefully():
    df = make_ohlcv(MIN_REQUIRED_ROWS + 20).drop(columns=["volume"])
    result = compute_indicators(df, "TEST")

    assert result.ok is False
    assert "missing columns" in result.reason


def test_none_dataframe_fails_gracefully():
    result = compute_indicators(None, "TEST")

    assert result.ok is False
