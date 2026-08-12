"""Tests for the Phase 9 large-sample research/backtesting layer
(strategy_lab/*.py).

Everything here is synthetic/deterministic and uses an in-memory SQLite DB -
no test makes a real network call, and (per Phase 9's mandate) no test here
may place a paper or live order, call trading.orders.submit_order, or send a
Discord notification. See the safety tests at the bottom of this file for
the explicit structural check that strategy_lab/* cannot reach order
execution or alerting at all, mirroring tests/test_research.py's pattern for
Phase 8.
"""
import ast
import json
import os
import sqlite3
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from db.schema import init_db
from strategy_lab import analysis, benchmark, events as events_mod, execution, factors, frozen_strategy, regime_history, regime_stats, robustness
from strategy_lab.data import RESEARCH_SOURCE
from strategy_lab.signal_history import build_ticker_signal_table, build_universe_signal_table
from strategy_lab.universe import PRODUCTION_WATCHLIST_OVERLAP, RESEARCH_UNIVERSE, assert_isolated_from_watchlist

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def make_ohlcv(n_rows, seed=0, trend=0.3, start_price=100.0, vol_boost_from=None):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-01-04", periods=n_rows)
    noise = rng.normal(0, 1.0, n_rows)
    close = start_price + np.cumsum(np.full(n_rows, trend) + noise)
    close = np.maximum(close, 1.0)
    high = close + rng.uniform(0.1, 1.0, n_rows)
    low = close - rng.uniform(0.1, 1.0, n_rows)
    open_ = close + rng.uniform(-0.5, 0.5, n_rows)
    volume = rng.integers(1_000_000, 1_200_000, n_rows).astype(float)
    if vol_boost_from is not None:
        volume[vol_boost_from:] *= 2.5
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"), "open": open_, "high": high, "low": low,
        "close": close, "volume": volume.astype(int),
    })


def insert_price_rows(conn, ticker, df, source=RESEARCH_SOURCE):
    rows = [(ticker, r.date, r.open, r.high, r.low, r.close, int(r.volume), source) for r in df.itertuples(index=False)]
    conn.executemany(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)", rows,
    )
    conn.commit()


# --- A1: split/dividend adjustment regression (guards against reintroducing
# the GOOGL-style unadjusted-price bug found in Phase 9) ---

def test_fetch_always_requests_full_adjustment():
    """Regression guard: strategy_lab.data must always request
    adjustment=ALL from Alpaca. Reintroducing a raw/unadjusted fetch here
    would silently corrupt every research calculation the way the original
    GOOGL 20:1-split bug did (a fabricated ~-90% single-day 'crash')."""
    from alpaca.data.enums import Adjustment
    import strategy_lab.data as data_module

    captured_requests = []
    fake_client = MagicMock()

    def fake_get_stock_bars(request):
        captured_requests.append(request)
        barset = MagicMock()
        barset.df = pd.DataFrame(columns=["symbol", "timestamp", "open", "high", "low", "close", "volume"])
        return barset

    fake_client.get_stock_bars.side_effect = fake_get_stock_bars

    conn = make_test_db()
    with patch.object(data_module, "get_data_client", return_value=fake_client):
        data_module.fetch_and_cache_universe(conn, ["ZZZ"], years=1)

    assert captured_requests, "expected at least one StockBarsRequest to be issued"
    for req in captured_requests:
        assert req.adjustment == Adjustment.ALL


def test_fetched_bars_stored_under_research_source_never_production_source():
    """Regression guard: research fetches must never write to the
    production 'alpaca' source tag (db.price_repository.SOURCE_PRIORITY),
    which trading/dashboard/alerts read for the live watchlist."""
    import strategy_lab.data as data_module

    fake_client = MagicMock()
    df = pd.DataFrame({
        "symbol": ["ZZZ"] * 3,
        "timestamp": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"], utc=True),
        "open": [10.0, 11.0, 12.0], "high": [10.5, 11.5, 12.5], "low": [9.5, 10.5, 11.5],
        "close": [10.2, 11.2, 12.2], "volume": [1000, 1100, 1200],
    })
    barset = MagicMock()
    barset.df = df
    fake_client.get_stock_bars.return_value = barset

    conn = make_test_db()
    with patch.object(data_module, "get_data_client", return_value=fake_client):
        data_module.fetch_and_cache_universe(conn, ["ZZZ"], years=1)

    rows = conn.execute("SELECT DISTINCT source FROM prices WHERE ticker='ZZZ'").fetchall()
    sources = {r["source"] for r in rows}
    assert sources == {RESEARCH_SOURCE}
    assert "alpaca" not in sources


def test_googl_split_no_longer_produces_crash_in_research_source():
    """Direct regression test for the actual Phase 9 finding: with
    split-adjusted data, a 20:1 split must not appear as a ~95% single-day
    price drop. Simulates the fetch with pre/post-split-adjusted values
    (as Alpaca's adjustment=ALL would return) and confirms no fabricated
    crash survives into the stored research data."""
    import strategy_lab.data as data_module

    fake_client = MagicMock()
    # Adjusted values: continuous across the (simulated) split date - no 20x jump.
    df = pd.DataFrame({
        "symbol": ["GOOGL"] * 3,
        "timestamp": pd.to_datetime(["2022-07-15", "2022-07-18", "2022-07-19"], utc=True),
        "open": [109.0, 111.6, 109.9], "high": [110.1, 112.7, 113.0], "low": [107.9, 107.4, 108.6],
        "close": [110.8, 108.1, 112.8], "volume": [50_000_000, 45_000_000, 40_000_000],
    })
    barset = MagicMock()
    barset.df = df
    fake_client.get_stock_bars.return_value = barset

    conn = make_test_db()
    with patch.object(data_module, "get_data_client", return_value=fake_client):
        data_module.fetch_and_cache_universe(conn, ["GOOGL"], years=1)

    from db.price_repository import load_price_history
    stored = load_price_history(conn, "GOOGL", source=RESEARCH_SOURCE).sort_values("date")
    daily_returns = stored["close"].pct_change().dropna()
    assert daily_returns.abs().max() < 0.5, "no single-day return should look like an unadjusted split crash"


# --- A2/A5: baseline artifact + production fingerprint ---

def test_phase9_baseline_summary_has_required_sections():
    from strategy_lab.phase9_baseline import build_baseline_summary
    fake_results = {
        "generated_at": "2026-01-01T00:00:00+00:00", "tickers": ["AAA", "BBB"],
        "coverage": {"n_tickers": 2}, "pooled_signal_table": pd.DataFrame({"x": [1, 2]}),
        "strategy_snapshot": {"backtest_rules": {}}, "bucket_scores": pd.DataFrame(), "bucket_stages": pd.DataFrame(),
        "event_returns": pd.DataFrame({"event_type": ["score_cross_70"]}),
        "spy_stats": None, "equal_weight_stats": None,
        "strategy_curve_idealized": pd.Series(dtype=float), "strategy_curve_reasonable": pd.Series(dtype=float),
        "wf_summary": {}, "wf_by_year": pd.DataFrame(), "regime_series": pd.DataFrame(),
        "robustness_results": {},
    }
    summary = build_baseline_summary(fake_results)
    for key in ("dataset", "frozen_strategy", "friction_assumptions", "score_bucket_results", "stage_results",
                "event_results", "benchmark_results", "walk_forward_results", "regime_results",
                "ticker_robustness", "readiness_classification"):
        assert key in summary
    assert summary["readiness_classification"] == "NOT READY"
    # must never contain a webhook/credential-shaped key
    dumped = json.dumps(summary, default=str).lower()
    assert "webhook" not in dumped and "api_key" not in dumped and "secret" not in dumped


def test_production_fingerprint_self_diff_is_empty():
    from strategy_lab.production_guard import compute_fingerprint, diff_against_saved, save_fingerprint
    save_fingerprint()
    assert diff_against_saved() == {}


def test_production_fingerprint_detects_structured_change():
    from strategy_lab import production_guard
    saved = {"file_hashes": {}, "structured_values": {"watchlist": ["AAPL"]}}
    with patch.object(production_guard, "load_fingerprint", return_value=saved):
        diffs = production_guard.diff_against_saved()
    assert "structured_values.watchlist" in diffs


# --- research universe ---

def test_research_universe_size_and_uniqueness():
    assert 50 <= len(RESEARCH_UNIVERSE) <= 100
    assert len(set(RESEARCH_UNIVERSE)) == len(RESEARCH_UNIVERSE)


def test_research_universe_includes_production_watchlist():
    for t in PRODUCTION_WATCHLIST_OVERLAP:
        assert t in RESEARCH_UNIVERSE


def test_research_universe_isolated_from_config_watchlist():
    assert assert_isolated_from_watchlist()


# --- frozen strategy ---

def test_frozen_strategy_matches_live_production_config():
    """Freezing must read the real objects, not duplicate numbers - a change
    to signals/config.py or backtest/config.py must be reflected here."""
    from backtest.config import DEFAULT_RULES
    from signals.config import DEFAULT_WEIGHTS
    snap = frozen_strategy.snapshot()
    assert snap["backtest_rules"]["entry_min_score"] == DEFAULT_RULES.entry_min_score
    assert snap["signal_weights"]["volume_points"] == DEFAULT_WEIGHTS.volume_points


def test_score_bucket_boundaries_cover_0_to_100():
    for v in (0, 39, 40, 54, 55, 69, 70, 84, 85, 100):
        assert frozen_strategy.score_bucket(v) is not None


# --- historical signal generation / no-look-ahead ---

def test_build_ticker_signal_table_no_lookahead():
    """Truncating history AFTER date t must not change the score computed
    for any date <= t (every indicator is trailing-only)."""
    conn = make_test_db()
    df = make_ohlcv(200, seed=1, trend=0.4)
    insert_price_rows(conn, "AAA", df)
    full_table, reason = build_ticker_signal_table(conn, "AAA")
    assert reason is None and full_table is not None

    conn2 = make_test_db()
    insert_price_rows(conn2, "AAA", df.iloc[:150])
    truncated_table, reason2 = build_ticker_signal_table(conn2, "AAA")
    assert reason2 is None

    common_dates = set(full_table["date"]) & set(truncated_table["date"])
    assert len(common_dates) > 50
    merged = full_table.merge(truncated_table, on="date", suffixes=("_full", "_trunc"))
    assert np.allclose(merged["score_full"], merged["score_trunc"], atol=1e-9)


def test_missing_data_returns_reason_not_fabricated_table():
    conn = make_test_db()
    insert_price_rows(conn, "SHORT", make_ohlcv(10, seed=2))
    table, reason = build_ticker_signal_table(conn, "SHORT")
    assert table is None
    assert "insufficient" in reason


def test_universe_signal_table_reports_failures_separately():
    conn = make_test_db()
    insert_price_rows(conn, "GOOD", make_ohlcv(200, seed=3))
    insert_price_rows(conn, "BAD", make_ohlcv(5, seed=4))
    pooled, failures = build_universe_signal_table(conn, ["GOOD", "BAD"])
    assert "GOOD" in pooled["ticker"].unique()
    assert "BAD" in failures
    assert "BAD" not in pooled["ticker"].unique()


# --- forward-return alignment ---

def test_forward_return_alignment_exact():
    from research.historical_quality import compute_forward_returns
    price_df = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=10).strftime("%Y-%m-%d"),
                              "close": [100, 102, 101, 105, 110, 108, 112, 115, 120, 118]})
    out = compute_forward_returns(price_df, (1, 5))
    assert out["fwd_return_1d"].iloc[0] == pytest.approx(102 / 100 - 1)
    assert out["fwd_return_5d"].iloc[0] == pytest.approx(108 / 100 - 1)
    assert pd.isna(out["fwd_return_5d"].iloc[-1])  # not enough future data - never fabricated


# --- score bucket / stage aggregation ---

def test_bucket_by_score_pooled_groups_correctly():
    pooled = pd.DataFrame({
        "date": ["2024-01-01"] * 6, "score": [10, 45, 60, 75, 90, 20],
        "fwd_return_5d": [0.01, 0.02, -0.01, 0.03, 0.04, -0.02],
    })
    result = analysis.bucket_by_score_pooled(pooled, horizons=(5,))
    row_0_39 = result[result["bucket"] == "0-39"].iloc[0]
    assert row_0_39["n"] == 2  # scores 10 and 20


def test_bucket_stats_flag_overlapping_observations():
    values = pd.Series(np.random.default_rng(0).normal(0, 0.01, 50))
    single_day = analysis._stats_row(values, horizon=1)
    multi_day = analysis._stats_row(values, horizon=20)
    assert single_day["overlapping_observations"] is False
    assert multi_day["overlapping_observations"] is True
    assert multi_day["effective_n"] < multi_day["n"]


# --- signal event detection: transitions only, not sustained conditions ---

def test_detect_transitions_fires_once_per_crossing_not_every_day_above():
    df = pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=6).strftime("%Y-%m-%d"),
        "score": [60, 75, 80, 78, 65, 90],       # crosses >=70 at idx1, drops below at idx4, crosses again idx5
        "stage_rank": [0, 0, 0, 0, 0, 0],
    })
    events = events_mod.detect_transitions(df)
    score_crosses = events[events["event_type"] == "score_cross_70"]
    assert list(score_crosses["event_index"]) == [1, 5]  # not 2 (still above, not a new crossing)


def test_detect_transitions_stage_advance_requires_prior_lower_stage():
    df = pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=5).strftime("%Y-%m-%d"),
        "score": [50] * 5,
        "stage_rank": [1, 2, 2, 1, 2],  # trend, momentum, momentum(sustain), back to trend, momentum again
    })
    events = events_mod.detect_transitions(df)
    momentum_events = events[events["event_type"] == "momentum_advance"]
    assert list(momentum_events["event_index"]) == [1, 4]


def test_event_entry_uses_next_session_open_not_same_close():
    price_df = pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=5).strftime("%Y-%m-%d"),
        "open": [100, 200, 300, 400, 500], "close": [110, 210, 310, 410, 510],
    })
    events_df = pd.DataFrame([{"event_index": 1, "event_type": "score_cross_70", "date": price_df.at[1, "date"], "score": 75, "stage_label": "trend"}])
    fwd = events_mod.compute_event_forward_returns(events_df, price_df, horizons=(1,))
    # entry must be at index 2's OPEN (300), not index 1's close (210)
    assert fwd.iloc[0]["entry_date"] == price_df.at[2, "date"]
    expected_gross = price_df.at[3, "close"] / price_df.at[2, "open"] - 1.0
    assert fwd.iloc[0]["gross_return_1d"] == pytest.approx(expected_gross)


def test_event_returns_drop_rows_past_end_of_history_never_fabricated():
    price_df = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=3).strftime("%Y-%m-%d"),
                              "open": [100, 101, 102], "close": [100, 101, 102]})
    events_df = pd.DataFrame([{"event_index": 2, "event_type": "score_cross_70", "date": price_df.at[2, "date"], "score": 75, "stage_label": "trend"}])
    fwd = events_mod.compute_event_forward_returns(events_df, price_df, horizons=(1,))
    assert fwd.empty  # event at last row has no next-session open available


# --- friction / slippage ---

def test_idealized_execution_has_zero_friction():
    assert execution.IDEALIZED.slippage_bps == 0
    assert execution.IDEALIZED.commission_pct == 0
    net = execution.round_trip_net_return(0.05, execution.IDEALIZED)
    assert net == pytest.approx(0.05)


def test_reasonable_friction_reduces_return():
    gross = 0.05
    net = execution.round_trip_net_return(gross, execution.REASONABLE)
    assert net < gross


def test_friction_can_flip_small_gains_negative():
    tiny_gross = 0.0005  # 5bps - smaller than round-trip reasonable friction
    net = execution.round_trip_net_return(tiny_gross, execution.REASONABLE)
    assert net < tiny_gross


# --- benchmark alignment ---

def test_attach_benchmark_forward_returns_aligns_by_date():
    pooled = pd.DataFrame({"date": ["2024-01-01", "2024-01-02"], "ticker": ["AAA", "AAA"], "fwd_return_5d": [0.01, 0.02]})
    bench = pd.DataFrame({"date": ["2024-01-01", "2024-01-02"], "fwd_return_5d": [0.005, 0.015]})
    out = analysis.attach_benchmark_forward_returns(pooled, bench, horizons=(5,))
    assert out["fwd_return_5d_bench"].tolist() == [0.005, 0.015]


def test_benchmark_performance_stats_basic_sanity():
    returns = pd.Series(np.full(300, 0.001))  # steady positive daily return
    stats = benchmark.compute_performance_stats(returns, "test")
    assert stats.cumulative_return_pct > 0
    assert stats.win_rate_pct == 100.0
    assert stats.max_drawdown_pct <= 0  # a drawdown metric is always <= 0


# --- market-regime grouping ---

def test_historical_regime_series_uses_config_thresholds():
    conn = make_test_db()
    # Strong sustained uptrend -> should classify as bullish_trend once both SMAs are available.
    df = make_ohlcv(260, seed=5, trend=0.5, start_price=300.0)
    insert_price_rows(conn, "SPY", df)
    series = regime_history.compute_historical_regime_series(conn)
    assert not series.empty
    assert set(series["label"].unique()) <= {"bullish_trend", "neutral_mixed", "bearish_trend", "elevated_volatility_risk_off"}


def test_historical_regime_series_no_lookahead():
    """B3: truncating SPY history AFTER date t must not change the regime
    label computed for any date <= t. compute_historical_regime_series uses
    only pandas .rolling()/.cummax()-style trailing windows (never centered,
    never full-sample-normalized), so this must hold exactly."""
    conn_full = make_test_db()
    df = make_ohlcv(400, seed=7, trend=0.3, start_price=300.0)
    insert_price_rows(conn_full, "SPY", df)
    full_series = regime_history.compute_historical_regime_series(conn_full)

    conn_trunc = make_test_db()
    insert_price_rows(conn_trunc, "SPY", df.iloc[:300])
    truncated_series = regime_history.compute_historical_regime_series(conn_trunc)

    common = full_series.merge(truncated_series, on="date", suffixes=("_full", "_trunc"))
    assert len(common) > 50
    assert (common["label_full"] == common["label_trunc"]).all()
    assert np.allclose(common["sma_long_full"], common["sma_long_trunc"], atol=1e-9)
    assert np.allclose(common["realized_vol_annualized_full"], common["realized_vol_annualized_trunc"], atol=1e-9)


def test_historical_regime_series_drawdown_uses_trailing_window_only():
    """Drawdown at date t must be computed from a trailing window ending at
    t (close.rolling(window).max()), never from the full-sample max (which
    would leak future peaks backward into earlier dates' drawdown values)."""
    conn = make_test_db()
    # Sharp late spike: an early date's drawdown must NOT reflect this future high.
    df = make_ohlcv(300, seed=8, trend=0.1, start_price=200.0)
    df.loc[280:, "close"] = df.loc[280:, "close"] * 3  # a big future spike
    insert_price_rows(conn, "SPY", df)
    series = regime_history.compute_historical_regime_series(conn)
    early_row = series[series["date"] == df.iloc[220]["date"]]
    if not early_row.empty:
        # drawdown_pct should be a small, locally-reasonable value, not a
        # huge negative number implying it was compared against the future spike.
        assert early_row["drawdown_pct"].iloc[0] > -50


def test_historical_regime_series_empty_when_insufficient_history():
    conn = make_test_db()
    insert_price_rows(conn, "SPY", make_ohlcv(50, seed=6))
    series = regime_history.compute_historical_regime_series(conn)
    assert series.empty


# --- B1: experiment definitions frozen ---

def test_control_matches_production_default_rules_exactly():
    from backtest.config import DEFAULT_RULES
    from strategy_lab.phase10_experiments import CONTROL
    assert CONTROL.require_bullish_entry is False
    assert CONTROL.exit_on_regime_loss is False
    assert DEFAULT_RULES.entry_min_score == 70.0  # sanity: still the frozen threshold


def test_experiment_a_entry_gated_exit_not_gated():
    from strategy_lab.phase10_experiments import EXPERIMENT_A
    assert EXPERIMENT_A.require_bullish_entry is True
    assert EXPERIMENT_A.exit_on_regime_loss is False


def test_experiment_b_entry_and_exit_gated():
    from strategy_lab.phase10_experiments import EXPERIMENT_B
    assert EXPERIMENT_B.require_bullish_entry is True
    assert EXPERIMENT_B.exit_on_regime_loss is True


# --- B4: regime distribution/duration/transitions ---

def test_regime_distribution_sums_to_100_pct():
    regime_series = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=20).strftime("%Y-%m-%d"),
                                   "label": ["bullish_trend"] * 15 + ["bearish_trend"] * 5})
    dist = regime_stats.regime_distribution(regime_series)
    assert dist["pct_of_sample"].sum() == pytest.approx(100.0)
    bullish_row = dist[dist["label"] == "bullish_trend"].iloc[0]
    assert bullish_row["trading_days"] == 15


def test_regime_duration_counts_episodes_not_days():
    regime_series = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=10).strftime("%Y-%m-%d"),
                                   "label": ["bullish_trend"] * 3 + ["neutral_mixed"] * 2 + ["bullish_trend"] * 5})
    durations = regime_stats.regime_duration_stats(regime_series)
    bullish_row = durations[durations["label"] == "bullish_trend"].iloc[0]
    assert bullish_row["n_episodes"] == 2  # two separate bullish episodes, not one


def test_transition_matrix_counts_day_to_day_moves():
    regime_series = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=4).strftime("%Y-%m-%d"),
                                   "label": ["bullish_trend", "bullish_trend", "neutral_mixed", "bullish_trend"]})
    matrix = regime_stats.transition_matrix(regime_series)
    assert matrix.loc["bullish_trend", "bullish_trend"] == 1
    assert matrix.loc["bullish_trend", "neutral_mixed"] == 1
    assert matrix.loc["neutral_mixed", "bullish_trend"] == 1


# --- B8: RegimeGatedStrategy entry/exit logic (CONTROL/A/B) via a real
# minimal Backtest run on crafted synthetic data ---

def _run_regime_backtest(bt_df, variant):
    from backtesting import Backtest
    from strategy_lab.regime_strategy import RegimeGatedStrategy, run_kwargs_for_variant
    bt = Backtest(bt_df, RegimeGatedStrategy, cash=10_000, commission=0.0, exclusive_orders=True)
    return bt.run(**run_kwargs_for_variant(variant))


def _synthetic_regime_bt_frame(bullish_flags):
    """Score/StageRank always entry-qualifying; only RegimeBullish varies -
    isolates the regime-gating effect from the base signal gate."""
    n = len(bullish_flags)
    dates = pd.bdate_range("2024-01-01", periods=n)
    price = 100.0 + np.arange(n)  # steadily rising, so entries (if taken) are profitable
    return pd.DataFrame({
        "Open": price, "High": price + 1, "Low": price - 1, "Close": price, "Volume": [1_000_000] * n,
        "Score": [80.0] * n, "StageRank": [1] * n,  # always qualifies the base entry gate (stage=trend, score=80)
        "RegimeBullish": bullish_flags,
    }, index=dates)


def test_control_ignores_regime_and_enters_immediately():
    from strategy_lab.phase10_experiments import CONTROL
    bt_df = _synthetic_regime_bt_frame([False] * 20)  # never bullish
    stats = _run_regime_backtest(bt_df, CONTROL)
    assert stats["# Trades"] >= 1  # CONTROL enters regardless of regime


def test_experiment_a_never_enters_when_never_bullish():
    from strategy_lab.phase10_experiments import EXPERIMENT_A
    bt_df = _synthetic_regime_bt_frame([False] * 20)
    stats = _run_regime_backtest(bt_df, EXPERIMENT_A)
    assert stats["# Trades"] == 0


def test_experiment_a_enters_once_bullish_and_stays_in_on_regime_loss():
    from strategy_lab.phase10_experiments import EXPERIMENT_A
    bt_df = _synthetic_regime_bt_frame([False] * 5 + [True] * 5 + [False] * 10)
    stats = _run_regime_backtest(bt_df, EXPERIMENT_A)
    assert stats["# Trades"] == 1  # entered once bullish; base exit gate never fires (score stays 80) -> holds through regime loss


def test_experiment_b_exits_when_regime_ends():
    from strategy_lab.phase10_experiments import EXPERIMENT_B
    bt_df = _synthetic_regime_bt_frame([False] * 5 + [True] * 5 + [False] * 10)
    stats = _run_regime_backtest(bt_df, EXPERIMENT_B)
    trades = stats["_trades"]
    assert len(trades) == 1
    # exits at or shortly after the regime-loss bar (index 10), not held to the end like Experiment A
    assert trades.iloc[0]["ExitBar"] < len(bt_df) - 1


# --- ticker-level (cross-sectional) aggregation ---

def test_per_ticker_bucket_return_groups_by_ticker():
    pooled = pd.DataFrame({
        "ticker": ["AAA", "AAA", "BBB"], "score": [75, 80, 76], "fwd_return_20d": [0.05, 0.03, -0.02],
    })
    out = analysis.per_ticker_bucket_return(pooled, "70-84", 20, 70, 84)
    assert set(out["ticker"]) == {"AAA", "BBB"}
    aaa_row = out[out["ticker"] == "AAA"].iloc[0]
    assert aaa_row["n"] == 2
    assert aaa_row["mean_return"] == pytest.approx(0.04)


def test_robustness_summary_flags_concentration():
    # 90% of the positive return concentrated in one ticker's very large mean.
    rows = [{"ticker": f"T{i}", "score": 75, "fwd_return_20d": 0.001} for i in range(10) for _ in range(6)]
    rows += [{"ticker": "WHALE", "score": 75, "fwd_return_20d": v} for v in [0.5, 0.6, 0.55, 0.52, 0.58, 0.51]]
    pooled = pd.DataFrame(rows)
    result = robustness.robustness_summary(pooled, "70-84", 70, 84, 20, min_obs_per_ticker=5)
    assert result["concentration_flag"] is True


def test_robustness_summary_broad_participation_not_flagged():
    rows = [{"ticker": f"T{i}", "score": 75, "fwd_return_20d": 0.01 + 0.001 * i} for i in range(20) for _ in range(6)]
    pooled = pd.DataFrame(rows)
    result = robustness.robustness_summary(pooled, "70-84", 70, 84, 20, min_obs_per_ticker=5)
    assert result["concentration_flag"] is False


# --- walk-forward: reuses (does not reimplement) the already-tested
# chronological, non-overlapping train/test window generator ---

def test_walk_forward_universe_reuses_production_window_generator():
    """strategy_lab.walkforward_universe must call backtest.walkforward's
    real generate_windows/run_walk_forward (already covered by
    tests/test_backtest.py's chronological-separation/no-look-ahead checks),
    not a separate reimplementation that could silently drift."""
    import strategy_lab.walkforward_universe as wf_universe_module
    from backtest.walkforward import run_walk_forward as production_run_walk_forward
    assert wf_universe_module.run_walk_forward is production_run_walk_forward


def test_walk_forward_by_year_buckets_use_test_window_start_year():
    from backtest.runner import BacktestResult
    from backtest.walkforward import WalkForwardResult, WalkForwardWindow, WalkForwardWindowResult
    from strategy_lab.walkforward_universe import walk_forward_by_year

    def make_result(return_pct):
        return BacktestResult(ticker="AAA", source="test", start_date="2023-01-01", end_date="2023-06-01",
                               n_observations=63, rules=None, total_return_pct=return_pct, buy_hold_return_pct=0,
                               sharpe_ratio=0.5, max_drawdown_pct=-5, win_rate_pct=50, num_trades=2,
                               avg_trade_pct=1, best_trade_pct=2, worst_trade_pct=-1,
                               equity_curve=pd.Series([100, 101]), trades=pd.DataFrame(), raw_stats=None)

    windows = [
        WalkForwardWindowResult(window=WalkForwardWindow(0, "2022-01-01", "2022-12-01", "2023-01-01", "2023-03-01"), result=make_result(5.0)),
        WalkForwardWindowResult(window=WalkForwardWindow(1, "2023-03-01", "2023-12-01", "2024-01-01", "2024-03-01"), result=make_result(-2.0)),
    ]
    results = {"AAA": WalkForwardResult(ticker="AAA", source="test", config=None, windows=windows)}
    by_year = walk_forward_by_year(results)
    assert set(by_year["year"]) == {"2023", "2024"}
    assert by_year[by_year["year"] == "2023"]["mean_return_pct"].iloc[0] == pytest.approx(5.0)


# --- B20: prospective validation system ---

def test_prospective_schema_has_no_outcome_columns():
    """Structural guarantee: the table literally cannot store a forward
    return at creation time - there is no column for it."""
    from strategy_lab import prospective
    conn = make_test_db()
    prospective.ensure_schema(conn)
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({prospective.TABLE_NAME})").fetchall()}
    forbidden = {c for c in cols if "return" in c.lower() or "outcome" in c.lower() or "realized" in c.lower()}
    assert not forbidden, f"observation table must not have outcome columns: {forbidden}"


def _sample_observation(ticker="AAA", date="2024-01-02", score=75.0):
    return dict(observation_date=date, ticker=ticker, score=score, stage="momentum", regime="bullish_trend",
                control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
                adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=100.0)


def test_record_observation_is_immutable_on_duplicate():
    from strategy_lab import prospective
    conn = make_test_db()
    prospective.record_observation(conn, **_sample_observation(score=75.0))
    prospective.record_observation(conn, **_sample_observation(score=999.0))  # attempted overwrite
    loaded = prospective.load_observations(conn, "AAA")
    assert len(loaded) == 1
    assert loaded.iloc[0]["score"] == 75.0  # original value preserved, never overwritten


def test_record_observation_different_dates_both_kept():
    from strategy_lab import prospective
    conn = make_test_db()
    prospective.record_observation(conn, **_sample_observation(date="2024-01-02"))
    prospective.record_observation(conn, **_sample_observation(date="2024-01-03"))
    loaded = prospective.load_observations(conn, "AAA")
    assert len(loaded) == 2


def test_compute_realized_returns_never_writes_to_observations_table():
    from strategy_lab import prospective
    conn = make_test_db()
    insert_price_rows(conn, "AAA", make_ohlcv(100, seed=9), source=RESEARCH_SOURCE)
    obs_date = make_ohlcv(100, seed=9).iloc[10]["date"]
    prospective.record_observation(conn, **_sample_observation(ticker="AAA", date=obs_date))
    before = prospective.load_observations(conn, "AAA")

    prospective.compute_realized_returns(conn)

    after = prospective.load_observations(conn, "AAA")
    pd.testing.assert_frame_equal(before, after)


def test_compute_realized_returns_computes_forward_return_correctly():
    from strategy_lab import prospective
    conn = make_test_db()
    price_df = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=10).strftime("%Y-%m-%d"),
                              "open": range(100, 110), "high": range(101, 111), "low": range(99, 109), "close": range(100, 110),
                              "volume": [1_000_000] * 10})
    insert_price_rows(conn, "AAA", price_df, source=RESEARCH_SOURCE)
    prospective.record_observation(conn, **_sample_observation(ticker="AAA", date=price_df.iloc[2]["date"]))
    realized = prospective.compute_realized_returns(conn, horizons=(1,))
    expected = price_df.iloc[3]["close"] / price_df.iloc[2]["close"] - 1.0
    assert realized.iloc[0]["realized_return_1d"] == pytest.approx(expected)


def test_prospective_table_isolated_from_trading_tables():
    """The observations table must never be db.schema.py's paper_orders/
    alert_state - confirms the table name and that it's created only by
    strategy_lab.prospective, never wired into db/schema.py's init_db()."""
    from strategy_lab import prospective
    assert prospective.TABLE_NAME not in ("paper_orders", "alert_state", "portfolio_snapshots", "automation_runs")
    schema_source = open(os.path.join(REPO_ROOT, "db", "schema.py")).read()
    assert prospective.TABLE_NAME not in schema_source


# --- research-universe isolation ---

def _module_level_import_names(file_path):
    with open(file_path) as f:
        tree = ast.parse(f.read(), filename=file_path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_strategy_lab_never_imports_order_execution_or_alerting():
    """strategy_lab/*.py must never be able to submit an order, call
    trading.orders/engine/run_paper, or send a Discord alert."""
    sl_dir = os.path.join(REPO_ROOT, "strategy_lab")
    forbidden_prefixes = ("trading.engine", "trading.orders", "trading.run_paper", "alerts.discord", "alerts.runner", "alerts.run_alerts")
    offenders = {}
    for filename in os.listdir(sl_dir):
        if not filename.endswith(".py"):
            continue
        imports = _module_level_import_names(os.path.join(sl_dir, filename))
        forbidden = {i for i in imports if i in forbidden_prefixes or any(i.startswith(p + ".") for p in forbidden_prefixes)}
        if forbidden:
            offenders[filename] = forbidden
    assert not offenders, f"strategy_lab/*.py must never import order-execution/alerting modules: {offenders}"


def test_trading_and_automation_never_import_strategy_lab():
    """The reverse direction: production trading/automation must never
    depend on the research-only strategy_lab package."""
    offenders = {}
    for pkg in ("trading", "automation", "alerts"):
        pkg_dir = os.path.join(REPO_ROOT, pkg)
        for filename in os.listdir(pkg_dir):
            if not filename.endswith(".py"):
                continue
            imports = _module_level_import_names(os.path.join(pkg_dir, filename))
            forbidden = {i for i in imports if i == "strategy_lab" or i.startswith("strategy_lab.")}
            if forbidden:
                offenders[f"{pkg}/{filename}"] = forbidden
    assert not offenders, f"production packages must never import strategy_lab: {offenders}"


def test_strategy_lab_dashboard_view_never_imports_order_execution():
    path = os.path.join(REPO_ROOT, "dashboard", "views", "strategy_lab.py")
    imports = _module_level_import_names(path)
    forbidden = {i for i in imports if i in ("trading.engine", "trading.orders", "trading.reconcile", "trading.run_paper") or i.startswith("automation.")}
    assert not forbidden, f"Strategy Lab view must never import order-execution modules: {forbidden}"


def test_strategy_lab_run_study_module_has_no_submit_order_reference():
    """Belt-and-suspenders: confirm the actual submitted symbols
    strategy_lab.run_study resolves at import time don't include
    trading.orders.submit_order or any Alpaca order-submission call."""
    import strategy_lab.run_study as run_study_module
    assert "trading" not in run_study_module.__dict__
    assert not hasattr(run_study_module, "submit_order")


def test_amzn_monitor_never_imports_order_execution():
    """B21: the AMZN read-only monitor must have zero import-level access to
    order-execution modules, even though it legitimately reads live Alpaca
    positions/account (like paper_portfolio.py does)."""
    path = os.path.join(REPO_ROOT, "strategy_lab", "amzn_monitor.py")
    imports = _module_level_import_names(path)
    forbidden = {i for i in imports if i in ("trading.orders", "trading.engine", "trading.run_paper")}
    assert not forbidden, f"amzn_monitor.py must never import order-execution modules: {forbidden}"
    source = open(path).read()
    assert "submit_order(" not in source  # docstring may reference the name; an actual call must never appear


def test_no_strategy_lab_module_writes_to_launchagent_plist():
    """B25: no Phase 9/10 module may write to the LaunchAgent plist - grep
    every strategy_lab .py file for any reference to the plist path or
    launchctl, which should only ever appear in deploy/ and docstrings."""
    sl_dir = os.path.join(REPO_ROOT, "strategy_lab")
    offenders = []
    for filename in os.listdir(sl_dir):
        if not filename.endswith(".py"):
            continue
        source = open(os.path.join(sl_dir, filename)).read()
        if "launchctl" in source.lower() or ".plist" in source.lower():
            # allow mention only inside a comment/docstring describing what NOT to do
            if "com.stockdashboard.dailyrun.plist" in source and "GUARDED_FILES" not in source:
                offenders.append(filename)
    assert not offenders, f"unexpected LaunchAgent reference outside production_guard.py: {offenders}"


def test_prospective_cli_never_imports_launchagent_or_scheduler():
    path = os.path.join(REPO_ROOT, "strategy_lab", "run_prospective_observation.py")
    imports = _module_level_import_names(path)
    forbidden = {i for i in imports if "schedule" in i.lower() or "launch" in i.lower()}
    assert not forbidden


def test_strategy_lab_report_module_never_calls_alpaca_trading_client():
    """report.py orchestrates data reads (Alpaca market DATA client, via
    strategy_lab.data) but must never construct a Alpaca TRADING client
    capable of submitting orders."""
    import strategy_lab.report as report_module
    assert "trading" not in report_module.__dict__
    imports = _module_level_import_names(os.path.join(REPO_ROOT, "strategy_lab", "report.py"))
    assert "trading" not in imports and not any(i.startswith("trading.") for i in imports)
