"""Unit tests for signals/engine.py.

Uses hand-constructed IndicatorResult fixtures (rather than real market data)
so each condition's fired/points behavior, and the sequential qualification
chain, can be asserted exactly and independently of each other.
"""
from indicators.technical import IndicatorResult
from signals.config import DEFAULT_WEIGHTS
from signals.engine import (
    STAGE_MOMENTUM,
    STAGE_NONE,
    STAGE_TREND,
    STAGE_VOLUME,
    score_indicators,
)


def make_indicators(**overrides):
    defaults = dict(
        ticker="TEST",
        ok=True,
        latest_date="2026-08-07",
        close=110.0,
        rsi=60.0,
        macd=1.5,
        macd_signal=1.0,
        macd_hist=0.5,
        bb_lower=95.0,
        bb_mid=105.0,
        bb_upper=115.0,
        adx=30.0,
        sma_50=100.0,
        volume=2_000_000.0,
        volume_avg_20=1_000_000.0,
        volume_ratio=2.0,
    )
    defaults.update(overrides)
    return IndicatorResult(**defaults)


def test_weights_sum_to_100():
    assert DEFAULT_WEIGHTS.total() == 100.0


def test_not_ok_indicators_short_circuit():
    bad = IndicatorResult(ticker="TEST", ok=False, reason="insufficient history: 10 rows")
    result = score_indicators(bad)

    assert result.ok is False
    assert result.reason == "insufficient history: 10 rows"
    assert result.score == 0.0
    assert result.highest_confirmed_stage == STAGE_NONE
    assert result.conditions == []


# --- Raw composite score: unconditional, independent of the sequential chain ---


def test_raw_score_all_conditions_fire():
    result = score_indicators(make_indicators())

    assert result.score == 100.0
    assert all(c.fired for c in result.conditions)
    assert all(c.points_awarded == c.points_available for c in result.conditions)


def test_raw_score_counts_fired_conditions_even_when_trend_fails():
    # Trend fails outright (adx too low, price below 50MA), but momentum and
    # volume conditions still fire on their own numbers. The raw score must
    # count them anyway - it is NOT gated by the sequential chain.
    result = score_indicators(make_indicators(adx=10.0, close=90.0, sma_50=100.0))

    trend_conditions = [c for c in result.conditions if c.layer == "trend"]
    momentum_conditions = [c for c in result.conditions if c.layer == "momentum"]
    volume_condition = next(c for c in result.conditions if c.layer == "volume")

    assert all(not c.fired for c in trend_conditions)
    assert all(c.fired for c in momentum_conditions)  # rsi=60, macd>signal by default
    assert volume_condition.fired  # volume_ratio=2.0 by default

    # 0 (adx) + 0 (ma) + 20 (rsi) + 20 (macd) + 30 (volume) = 70, unconditionally
    assert result.score == 70.0

    # ...yet the sequential chain never got past "none", because trend failed.
    assert result.trend_confirmed is False
    assert result.momentum_confirmed is False
    assert result.volume_confirmed is False
    assert result.highest_confirmed_stage == STAGE_NONE


def test_raw_score_zero_when_nothing_fires():
    result = score_indicators(make_indicators(
        adx=10.0, close=90.0, sma_50=100.0, rsi=40.0, macd=1.0, macd_signal=1.5, volume_ratio=0.8,
    ))

    assert result.score == 0.0
    assert all(c.points_awarded == 0.0 for c in result.conditions)


# --- Sequential qualification chain: each stage requires the previous one ---


def test_stage_none_when_trend_fails():
    result = score_indicators(make_indicators(adx=10.0, close=90.0, sma_50=100.0))

    assert result.trend_confirmed is False
    assert result.momentum_confirmed is False
    assert result.volume_confirmed is False
    assert result.highest_confirmed_stage == STAGE_NONE


def test_stage_trend_when_only_trend_confirms():
    # Trend fires; momentum conditions do not (volume's raw condition still
    # fires on its own numbers and counts toward the unconditional score).
    result = score_indicators(make_indicators(rsi=40.0, macd=1.0, macd_signal=1.5))

    assert result.trend_confirmed is True
    assert result.momentum_confirmed is False
    assert result.volume_confirmed is False
    assert result.highest_confirmed_stage == STAGE_TREND
    assert result.score == 60.0  # trend (15+15) + volume (30); momentum's 40 didn't fire


def test_stage_momentum_requires_trend_even_if_momentum_conditions_fire():
    # Momentum's raw conditions (RSI, MACD) both fire, but trend fails ->
    # momentum_confirmed must still be False, because the chain is sequential.
    result = score_indicators(make_indicators(adx=10.0, close=90.0, sma_50=100.0))

    assert result.trend_confirmed is False
    assert result.momentum_confirmed is False
    assert result.highest_confirmed_stage == STAGE_NONE


def test_stage_momentum_when_trend_and_momentum_confirm_but_not_volume():
    result = score_indicators(make_indicators(volume_ratio=0.5))

    assert result.trend_confirmed is True
    assert result.momentum_confirmed is True
    assert result.volume_confirmed is False
    assert result.highest_confirmed_stage == STAGE_MOMENTUM
    assert result.score == 70.0  # everything but the volume condition (30 pts)


def test_stage_volume_when_all_three_layers_confirm():
    result = score_indicators(make_indicators())

    assert result.trend_confirmed is True
    assert result.momentum_confirmed is True
    assert result.volume_confirmed is True
    assert result.highest_confirmed_stage == STAGE_VOLUME
    assert result.score == 100.0
