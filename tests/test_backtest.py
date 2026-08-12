"""Unit/integration tests for the Phase 3 backtesting + walk-forward layer.

Uses synthetic, deterministic OHLCV data (never real market data - real
results are validated separately by scripts/verify_phase3.py against the
Phase 1 ingestion database, per the project's own rule that synthetic data
is for tests only, not for reported validation results).
"""
import numpy as np
import pandas as pd
import pytest

from backtest.config import BacktestExecutionConfig, BacktestRules, WalkForwardConfig
from backtest.runner import prepare_backtest_frame, run_backtest
from backtest.walkforward import default_parameter_selector, generate_windows, run_walk_forward
from indicators.technical import MIN_REQUIRED_ROWS, enrich_with_indicators
from signals.config import DEFAULT_THRESHOLDS, DEFAULT_WEIGHTS


def make_price_path(n_rows, close_values, seed=0, start="2022-01-01"):
    """Build a synthetic OHLCV DataFrame from an explicit closing-price array."""
    assert len(close_values) == n_rows
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_rows)
    close = np.asarray(close_values, dtype=float)
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": close + rng.uniform(-0.2, 0.2, n_rows),
        "high": close + rng.uniform(0.2, 1.0, n_rows),
        "low": close - rng.uniform(0.2, 1.0, n_rows),
        "close": close,
        "volume": rng.integers(1_000_000, 2_000_000, n_rows),
    })


def make_uptrend(n_rows, seed=0, drift=0.3, noise=1.0, start="2022-01-01"):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(drift + rng.normal(0, noise, n_rows))
    close = np.maximum(close, 1.0)
    return make_price_path(n_rows, close, seed=seed, start=start)


def make_regime_change(n_flat, n_up, n_down, seed=0):
    """Flat/choppy -> strong sustained rise -> sharp decline.

    Designed so trend/momentum/volume conditions clearly do NOT fire during
    the flat section, DO fire during the rise (testing entry logic), and
    trend breaks during the decline (testing exit logic).
    """
    rng = np.random.default_rng(seed)
    flat = 100 + rng.normal(0, 0.5, n_flat)
    rise = flat[-1] + np.cumsum(np.full(n_up, 1.5) + rng.normal(0, 0.3, n_up))
    fall = rise[-1] - np.cumsum(np.full(n_down, 2.0) + rng.normal(0, 0.3, n_down))
    close = np.concatenate([flat, rise, fall])
    n_rows = n_flat + n_up + n_down

    df = make_price_path(n_rows, close, seed=seed)
    # Give the rise section a clear volume surge so the volume condition can fire too.
    vol = df["volume"].to_numpy(copy=True)
    vol[n_flat:n_flat + n_up] = (vol[n_flat:n_flat + n_up] * 2.5).astype(int)
    df["volume"] = vol
    return df


# --- prepare_backtest_frame / basic execution ---


def test_prepare_backtest_frame_has_expected_shape_and_warmup_nans():
    df = make_uptrend(MIN_REQUIRED_ROWS + 50)
    bt_df = prepare_backtest_frame(df, "SYN")

    assert list(bt_df.columns) == ["Open", "High", "Low", "Close", "Volume", "Score", "StageRank"]
    assert len(bt_df) == len(df)
    # SMA-50 alone guarantees at least the first 10 rows are still NaN (it
    # needs 50 rows to produce its first value); MIN_REQUIRED_ROWS is a
    # conservative safety margin for the *latest*-row check elsewhere, not
    # the exact row where every indicator stabilizes, so don't assert an
    # exact boundary here.
    assert bt_df["Score"].iloc[:10].isna().all()
    assert bt_df["Score"].iloc[MIN_REQUIRED_ROWS:].notna().all()


def test_run_backtest_basic_execution():
    df = make_regime_change(n_flat=100, n_up=120, n_down=80)
    result = run_backtest(df, "SYN", source="synthetic")

    assert result.n_observations == len(df)
    assert isinstance(result.total_return_pct, float)
    assert isinstance(result.buy_hold_return_pct, float)
    assert isinstance(result.sharpe_ratio, float)
    assert isinstance(result.max_drawdown_pct, float)
    assert result.num_trades >= 1  # the rise segment should trigger at least one entry
    assert len(result.equity_curve) == len(df)
    assert not result.trades.empty


def test_insufficient_data_raises_value_error():
    df = make_uptrend(MIN_REQUIRED_ROWS - 10)
    with pytest.raises(ValueError, match="insufficient history"):
        run_backtest(df, "SYN")


# --- entry logic / exit logic / score-stage interaction ---


def test_entry_requires_both_stage_and_score_gate():
    df = make_regime_change(n_flat=100, n_up=120, n_down=80)

    # Impossibly high score requirement -> stage gate alone is not enough, no entries.
    strict_rules = BacktestRules(entry_min_stage="trend", entry_min_score=200.0)
    result = run_backtest(df, "SYN", rules=strict_rules)
    assert result.num_trades == 0

    # Default rules (score gate reachable) -> the rise segment should qualify.
    normal_result = run_backtest(df, "SYN")
    assert normal_result.num_trades >= 1


def test_exit_triggers_when_trend_breaks():
    df = make_regime_change(n_flat=100, n_up=120, n_down=80)
    result = run_backtest(df, "SYN")

    assert result.num_trades >= 1
    first_trade = result.trades.iloc[0]
    # Entry should land inside (or very near) the rise segment, and exit
    # should come strictly after entry, once the subsequent decline breaks trend.
    assert pd.Timestamp(first_trade["EntryTime"]) < pd.Timestamp(first_trade["ExitTime"])


def test_score_and_stage_are_consistent_at_entry_bar():
    # At any bar where the strategy enters, the precomputed frame's Score and
    # StageRank at that bar must actually satisfy the configured rules -
    # confirms the strategy reads the same precomputed series it trades on,
    # not some independently-recomputed value.
    df = make_regime_change(n_flat=100, n_up=120, n_down=80)
    bt_df = prepare_backtest_frame(df, "SYN")
    result = run_backtest(df, "SYN")

    assert result.num_trades >= 1
    entry_time = pd.Timestamp(result.trades.iloc[0]["EntryTime"])
    row = bt_df.loc[entry_time]
    assert row["StageRank"] >= 1  # "trend" or higher
    assert row["Score"] >= result.rules.entry_min_score


# --- walk-forward: chronological splitting, no overlap, insufficient data ---


def test_generate_windows_train_never_overlaps_its_own_test():
    dates = pd.bdate_range("2020-01-01", periods=800).strftime("%Y-%m-%d").tolist()
    config = WalkForwardConfig(train_window_days=252, test_window_days=63, step_days=63)
    windows = generate_windows(dates, config)

    assert len(windows) > 0
    for w in windows:
        assert w.train_end < w.test_start


def test_generate_windows_consecutive_test_windows_do_not_overlap():
    dates = pd.bdate_range("2020-01-01", periods=800).strftime("%Y-%m-%d").tolist()
    config = WalkForwardConfig(train_window_days=252, test_window_days=63, step_days=63)
    windows = generate_windows(dates, config)

    for a, b in zip(windows, windows[1:]):
        assert a.test_end < b.test_start


def test_generate_windows_chronological_order():
    dates = pd.bdate_range("2020-01-01", periods=800).strftime("%Y-%m-%d").tolist()
    windows = generate_windows(dates, WalkForwardConfig(train_window_days=252, test_window_days=63, step_days=63))

    starts = [w.train_start for w in windows]
    assert starts == sorted(starts)


def test_generate_windows_insufficient_data_returns_empty_list():
    dates = pd.bdate_range("2020-01-01", periods=100).strftime("%Y-%m-%d").tolist()
    windows = generate_windows(dates, WalkForwardConfig(train_window_days=252, test_window_days=63, step_days=63))

    assert windows == []


# --- walk-forward aggregation ---


def test_walk_forward_aggregation_on_synthetic_data():
    df = make_uptrend(700, seed=7, drift=0.08, noise=1.3)
    result = run_walk_forward(df, "SYN", source="synthetic")

    assert len(result.windows) > 0
    summary = result.summary()
    assert summary["n_windows"] == len([w for w in result.windows if w.result is not None])
    assert isinstance(summary["mean_return_pct"], float)
    assert isinstance(summary["total_trades"], int)


def test_walk_forward_parameter_selector_ignores_train_data_by_default():
    df = make_uptrend(700, seed=3)
    dates = df["date"].tolist()
    windows = generate_windows(dates, WalkForwardConfig(train_window_days=252, test_window_days=63, step_days=63))
    train_slice = df[(df["date"] >= windows[0].train_start) & (df["date"] <= windows[0].train_end)]

    thresholds, weights = default_parameter_selector(train_slice)
    assert thresholds is DEFAULT_THRESHOLDS
    assert weights is DEFAULT_WEIGHTS

    # Sanity: even a completely different (garbage) train_df yields the same fixed output.
    garbage = train_slice.copy()
    garbage["close"] = 0.0001
    thresholds2, weights2 = default_parameter_selector(garbage)
    assert thresholds2 is DEFAULT_THRESHOLDS
    assert weights2 is DEFAULT_WEIGHTS


# --- no look-ahead bias ---


def test_indicator_values_unaffected_by_future_rows():
    """The core anti-leakage guarantee: indicator values for date t must be
    identical whether or not rows after t exist yet."""
    df = make_uptrend(300, seed=11)
    truncated = df.iloc[:200].copy()

    full_enriched = enrich_with_indicators(df)
    truncated_enriched = enrich_with_indicators(truncated)

    compare_cols = ["rsi", "macd", "macd_signal", "adx", "sma_50", "volume_avg_20"]
    pd.testing.assert_frame_equal(
        full_enriched.iloc[:200][compare_cols].reset_index(drop=True),
        truncated_enriched[compare_cols].reset_index(drop=True),
    )
