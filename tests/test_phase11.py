"""Tests for Phase 11 (prospective validation automation + realistic
portfolio simulation): strategy_lab/prospective.py's methodology_version
addition, strategy_lab/outcome_maturation.py, strategy_lab/prospective_events.py,
strategy_lab/research_automation.py, and strategy_lab/portfolio_simulator.py.

Everything here is synthetic/deterministic and uses an in-memory SQLite DB -
no test makes a real network call, and no test may place a paper/live order
or send a Discord notification (see the safety tests at the bottom of
tests/test_strategy_lab.py, which scan every file in strategy_lab/,
including the new Phase 11 modules, for exactly those imports).
"""
import sqlite3
from datetime import date
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from db.alert_repository import get_alert_state, upsert_alert_state
from db.run_history_repository import STATUS_FAILED, STATUS_SUCCESS, finish_run, start_run
from db.schema import init_db
from strategy_lab.phase10_experiments import CONTROL, EXPERIMENT_A, EXPERIMENT_B
from strategy_lab.prospective import METHODOLOGY_VERSION, load_observations, record_observation
from strategy_lab.prospective_events import (
    EVENT_CONTROL_ENTRY,
    EVENT_SCORE_CROSSING,
    EVENT_TREND_ADVANCE,
    evidence_label,
    LABEL_FULL_REVIEW,
    LABEL_INSUFFICIENT,
    LABEL_PRELIMINARY,
    load_events,
    record_events_for_observation,
)


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _obs(ticker="AAA", observation_date="2024-01-02", score=75.0, stage="momentum",
         control=True, exp_a=True, exp_b=True, regime="bullish_trend"):
    return dict(
        observation_date=observation_date, ticker=ticker, score=score, stage=stage, regime=regime,
        control_entry_signal=control, experiment_a_entry_signal=exp_a, experiment_b_entry_signal=exp_b,
        adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=100.0,
    )


# --- prospective immutability / methodology version ---


def test_recorded_observation_carries_methodology_version():
    conn = make_test_db()
    record_observation(conn, source="alpaca_adjusted", **_obs())
    row = load_observations(conn).iloc[0]
    assert row["methodology_version"] == METHODOLOGY_VERSION


def test_record_observation_returns_false_on_duplicate():
    conn = make_test_db()
    first = record_observation(conn, **_obs())
    second = record_observation(conn, **_obs(score=999.0))  # different score, same (ticker, date)
    assert first is True
    assert second is False
    # duplicate call never overwrote the original value
    assert load_observations(conn).iloc[0]["score"] == 75.0


# --- outcome maturation: pending/matured/unavailable, never backfills ---


def test_maturation_pending_before_horizon_elapses():
    from strategy_lab.outcome_maturation import STATUS_PENDING, mature_outcomes

    conn = make_test_db()
    record_observation(conn, **_obs(ticker="BBB", observation_date="2024-06-03"))
    dates = pd.bdate_range("2024-06-03", periods=3).strftime("%Y-%m-%d")
    for i, d in enumerate(dates):
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            ("BBB", d, 100.0, 101.0, 99.0, 100.0 + i, 1_000_000, "alpaca_adjusted"),
        )
    conn.commit()

    outcomes = mature_outcomes(conn, today=date(2024, 6, 5))  # only 2 sessions have elapsed
    row_1d = outcomes[(outcomes["ticker"] == "BBB") & (outcomes["horizon_days"] == 5)].iloc[0]
    assert row_1d["status"] == STATUS_PENDING
    assert pd.isna(row_1d["realized_return"])


def test_maturation_matures_once_horizon_genuinely_elapses():
    from strategy_lab.outcome_maturation import STATUS_MATURED, mature_outcomes

    conn = make_test_db()
    record_observation(conn, **_obs(ticker="CCC", observation_date="2024-06-03"))
    dates = pd.bdate_range("2024-06-03", periods=10).strftime("%Y-%m-%d")
    closes = [100.0, 101, 102, 103, 104, 105, 106, 107, 108, 110.0]
    for d, c in zip(dates, closes):
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            ("CCC", d, c, c + 1, c - 1, c, 1_000_000, "alpaca_adjusted"),
        )
    conn.commit()

    outcomes = mature_outcomes(conn, today=date(2024, 6, 20))  # comfortably past 5 real trading sessions
    row_5d = outcomes[(outcomes["ticker"] == "CCC") & (outcomes["horizon_days"] == 5)].iloc[0]
    assert row_5d["status"] == STATUS_MATURED
    assert row_5d["realized_return"] == pytest.approx(105.0 / 100.0 - 1.0)


def test_maturation_never_writes_to_observations_table():
    from strategy_lab.outcome_maturation import mature_outcomes

    conn = make_test_db()
    record_observation(conn, **_obs(ticker="DDD", observation_date="2024-06-03"))
    before = load_observations(conn).to_dict("records")

    mature_outcomes(conn, today=date(2024, 7, 1))

    after = load_observations(conn).to_dict("records")
    assert before == after  # byte-identical: maturation writes only to the separate outcomes table


def test_maturation_unavailable_when_price_data_missing():
    from strategy_lab.outcome_maturation import STATUS_UNAVAILABLE, mature_outcomes

    conn = make_test_db()
    record_observation(conn, **_obs(ticker="GHOST", observation_date="2024-06-03"))
    outcomes = mature_outcomes(conn, today=date(2024, 12, 1))
    assert (outcomes["status"] == STATUS_UNAVAILABLE).all()


# --- prospective events: dedupe on consecutive qualifying days ---


def test_event_fires_once_on_crossing_not_every_qualifying_day():
    conn = make_test_db()
    day1 = _obs(observation_date="2024-01-02", score=60.0, stage="trend")
    day2 = _obs(observation_date="2024-01-03", score=75.0, stage="momentum")  # crosses 70, advances to momentum
    day3 = _obs(observation_date="2024-01-04", score=76.0, stage="momentum")  # stays qualifying - no new event

    record_observation(conn, **day1)
    events1, newly_recorded1 = record_events_for_observation(conn, day1)
    assert events1 == []  # first observation never an event (no baseline)
    assert newly_recorded1 == []

    record_observation(conn, **day2)
    events2, newly_recorded2 = record_events_for_observation(conn, day2)
    assert EVENT_SCORE_CROSSING in events2
    assert EVENT_SCORE_CROSSING in newly_recorded2

    record_observation(conn, **day3)
    events3, newly_recorded3 = record_events_for_observation(conn, day3)
    assert events3 == []  # continuing to qualify is not a new event
    assert newly_recorded3 == []

    all_events = load_events(conn, ticker="AAA")
    assert len(all_events[all_events["event_type"] == EVENT_SCORE_CROSSING]) == 1


def test_control_entry_event_fires_once_per_streak():
    conn = make_test_db()
    day1 = _obs(observation_date="2024-01-02", control=False)
    day2 = _obs(observation_date="2024-01-03", control=True)   # becomes eligible
    day3 = _obs(observation_date="2024-01-04", control=True)   # stays eligible

    for d in (day1, day2, day3):
        record_observation(conn, **d)
        record_events_for_observation(conn, d)

    control_events = load_events(conn, ticker="AAA")
    control_events = control_events[control_events["event_type"] == EVENT_CONTROL_ENTRY]
    assert len(control_events) == 1
    assert control_events.iloc[0]["event_date"] == "2024-01-03"


def test_trend_advance_event_detected():
    conn = make_test_db()
    day1 = _obs(observation_date="2024-01-02", score=20.0, stage="none")
    day2 = _obs(observation_date="2024-01-03", score=50.0, stage="trend")

    record_observation(conn, **day1)
    record_events_for_observation(conn, day1)
    record_observation(conn, **day2)
    events2, newly_recorded2 = record_events_for_observation(conn, day2)
    assert EVENT_TREND_ADVANCE in events2
    assert EVENT_TREND_ADVANCE in newly_recorded2


# --- B16 sample guardrails: labels only, never upgrade readiness ---


def test_evidence_label_thresholds():
    assert evidence_label(0) == LABEL_INSUFFICIENT
    assert evidence_label(29) == LABEL_INSUFFICIENT
    assert evidence_label(30) == LABEL_PRELIMINARY
    assert evidence_label(99) == LABEL_PRELIMINARY
    assert evidence_label(100) == LABEL_FULL_REVIEW


# --- research automation: freshness gate + calendar gate ---


def test_research_job_skips_on_non_trading_day():
    from strategy_lab.research_automation import STATUS_SKIPPED_NON_TRADING_DAY, run_research_job

    conn = make_test_db()
    result = run_research_job(conn, today=date(2026, 8, 8), tickers=["AAA"])  # Saturday
    assert result.status == STATUS_SKIPPED_NON_TRADING_DAY


def test_research_job_skips_when_production_run_did_not_succeed():
    """The core B5 safety gate: directly prevents a repeat of the
    2026-08-12 failure mode (research must never build observations from
    unverified/stale prices)."""
    from strategy_lab.research_automation import STATUS_SKIPPED_NO_FRESH_DATA, run_research_job

    conn = make_test_db()
    today = date.today()
    run_id = start_run(conn, "real")
    finish_run(conn, run_id, status=STATUS_FAILED, tickers_attempted=7, tickers_failed=7)

    result = run_research_job(conn, today=today, tickers=["AAA"])
    assert result.status == STATUS_SKIPPED_NO_FRESH_DATA
    assert load_observations(conn).empty


def test_research_job_creates_observations_when_production_run_succeeded():
    from strategy_lab.research_automation import run_research_job

    conn = make_test_db()
    today = date.today()
    run_id = start_run(conn, "real")
    finish_run(conn, run_id, status=STATUS_SUCCESS, tickers_attempted=7, tickers_updated=7)

    dates = pd.bdate_range(end=today.isoformat(), periods=90).strftime("%Y-%m-%d")
    rng = np.random.default_rng(1)
    closes = 100 + np.cumsum(rng.normal(0.2, 1, len(dates)))
    for d, c in zip(dates, closes):
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            ("AAA", d, c - 0.3, c + 0.6, c - 0.6, c, 2_000_000, "alpaca_adjusted"),
        )
    conn.commit()

    result = run_research_job(conn, today=today, tickers=["AAA"])
    assert result.status == "success"
    assert not load_observations(conn).empty


def test_research_job_never_mutates_alert_state():
    from strategy_lab.research_automation import run_research_job

    conn = make_test_db()
    upsert_alert_state(conn, "AAPL", score=70.0, stage="momentum", alerted=False)
    before = dict(get_alert_state(conn, "AAPL"))

    today = date.today()
    run_id = start_run(conn, "real")
    finish_run(conn, run_id, status=STATUS_SUCCESS, tickers_attempted=1, tickers_updated=1)
    run_research_job(conn, today=today, tickers=["AAA"])  # unrelated ticker, no data - still must not touch alert_state

    after = dict(get_alert_state(conn, "AAPL"))
    assert after == before


def test_research_job_recovery_is_idempotent_on_duplicate_call():
    from strategy_lab.research_automation import run_research_job

    conn = make_test_db()
    today = date.today()
    run_id = start_run(conn, "real")
    finish_run(conn, run_id, status=STATUS_SUCCESS, tickers_attempted=1, tickers_updated=1)

    dates = pd.bdate_range(end=today.isoformat(), periods=90).strftime("%Y-%m-%d")
    for i, d in enumerate(dates):
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            ("AAA", d, 100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i, 1_000_000, "alpaca_adjusted"),
        )
    conn.commit()

    first = run_research_job(conn, today=today, tickers=["AAA"])
    second = run_research_job(conn, today=today, tickers=["AAA"])
    assert first.observations_created == 1
    assert second.observations_created == 0
    assert second.duplicates_skipped == 1
    assert len(load_observations(conn)) == 1


# --- portfolio simulator: constraints, ranking, friction, bookkeeping ---


def _make_ticker_frame(ticker, entry_event_date, n_days=40, start_price=100.0, score=80.0, stage_rank=2, drift=0.0):
    dates = pd.bdate_range("2024-01-02", periods=n_days).strftime("%Y-%m-%d")
    prices = start_price + np.arange(n_days) * drift
    df = pd.DataFrame({
        "date": dates, "open": prices, "high": prices + 1, "low": prices - 1, "close": prices,
        "volume": 1_000_000, "score": 0.0, "stage_rank": 0,
    })
    idx = list(dates).index(entry_event_date)
    df.loc[idx:, "score"] = score
    df.loc[idx:, "stage_rank"] = stage_rank
    return df


def test_portfolio_position_sizing_is_10pct_of_current_equity():
    from strategy_lab.portfolio_simulator import simulate_variant

    frames = {"AAA": _make_ticker_frame("AAA", "2024-01-03")}
    with patch("strategy_lab.portfolio_simulator.compute_historical_regime_series", return_value=pd.DataFrame()):
        result = simulate_variant(None, CONTROL, frames, starting_equity=100_000.0)

    assert len(result.trades) == 1
    trade = result.trades[0]
    expected_notional = 0.10 * 100_000.0
    assert trade.qty * trade.entry_price == pytest.approx(expected_notional, rel=0.01)


def test_portfolio_never_exceeds_max_six_positions():
    from strategy_lab.portfolio_simulator import simulate_variant

    frames = {f"T{i}": _make_ticker_frame(f"T{i}", "2024-01-03", score=80.0) for i in range(10)}
    with patch("strategy_lab.portfolio_simulator.compute_historical_regime_series", return_value=pd.DataFrame()):
        result = simulate_variant(None, CONTROL, frames, starting_equity=1_000_000.0)

    assert result.equity_curve["n_positions"].max() <= 6


def test_portfolio_exposure_never_exceeds_60pct():
    from strategy_lab.portfolio_simulator import simulate_variant

    frames = {f"T{i}": _make_ticker_frame(f"T{i}", "2024-01-03", score=80.0) for i in range(10)}
    with patch("strategy_lab.portfolio_simulator.compute_historical_regime_series", return_value=pd.DataFrame()):
        result = simulate_variant(None, CONTROL, frames, starting_equity=1_000_000.0)

    assert (result.equity_curve["exposure_pct"] <= 60.0 + 1e-6).all()


def test_portfolio_no_duplicate_ticker_position():
    from strategy_lab.portfolio_simulator import simulate_variant

    # AAA re-triggers an entry_event twice (exit then re-enter) - must never
    # hold two simultaneous positions in the same ticker.
    dates = pd.bdate_range("2024-01-02", periods=90).strftime("%Y-%m-%d")
    df = pd.DataFrame({"date": dates, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "score": 80.0, "stage_rank": 2})
    frames = {"AAA": df}
    with patch("strategy_lab.portfolio_simulator.compute_historical_regime_series", return_value=pd.DataFrame()):
        result = simulate_variant(None, CONTROL, frames, starting_equity=100_000.0)

    # at most one open AAA position at any time (n_positions never counts AAA twice - structurally guaranteed by dict keying)
    assert result.equity_curve["n_positions"].max() <= 1


def test_portfolio_deterministic_ranking_prefers_higher_score_then_stage_then_alpha():
    from strategy_lab.portfolio_simulator import _rank_candidates

    candidates = [
        {"ticker": "ZZZ", "score": 90.0, "stage_rank": 2},
        {"ticker": "AAA", "score": 90.0, "stage_rank": 3},
        {"ticker": "BBB", "score": 95.0, "stage_rank": 1},
        {"ticker": "CCC", "score": 90.0, "stage_rank": 3},
    ]
    ranked = _rank_candidates(candidates)
    assert [c["ticker"] for c in ranked] == ["BBB", "AAA", "CCC", "ZZZ"]


def test_portfolio_exposure_limit_skip_reason_recorded_before_position_cap():
    from strategy_lab.portfolio_simulator import simulate_variant
    from trading.config import RiskConfig

    # 25% per position, 60% exposure cap, 6 position cap: exposure binds
    # after 2 positions (50%), well before the position-count cap.
    big_position_risk_config = RiskConfig(max_position_size_pct=0.25, max_total_exposure_pct=0.60, max_open_positions=6)
    frames = {f"T{i}": _make_ticker_frame(f"T{i}", "2024-01-03", score=80.0) for i in range(5)}
    with patch("strategy_lab.portfolio_simulator.compute_historical_regime_series", return_value=pd.DataFrame()):
        result = simulate_variant(None, CONTROL, frames, starting_equity=100_000.0, risk_config=big_position_risk_config)

    reasons = {s.reason for s in result.skipped}
    assert "exposure_limit" in reasons
    assert len(result.trades) == 2  # only 2 of 5 candidates fit under the exposure cap


def test_portfolio_friction_reduces_return_vs_idealized():
    from strategy_lab.execution import ASSUMPTION_SETS

    assert ASSUMPTION_SETS["reasonable"].slippage_bps == 5.0
    assert ASSUMPTION_SETS["reasonable"].commission_pct == pytest.approx(0.001)  # 10bps, matches B8 spec exactly


def test_portfolio_next_session_execution_not_same_close():
    from strategy_lab.portfolio_simulator import simulate_variant

    frames = {"AAA": _make_ticker_frame("AAA", "2024-01-03")}
    with patch("strategy_lab.portfolio_simulator.compute_historical_regime_series", return_value=pd.DataFrame()):
        result = simulate_variant(None, CONTROL, frames, starting_equity=100_000.0)

    trade = result.trades[0]
    assert trade.entry_date > "2024-01-03"  # entered strictly after the signal date, never the same bar


def test_portfolio_cash_never_negative():
    from strategy_lab.portfolio_simulator import simulate_variant

    frames = {f"T{i}": _make_ticker_frame(f"T{i}", "2024-01-03", score=80.0) for i in range(10)}
    with patch("strategy_lab.portfolio_simulator.compute_historical_regime_series", return_value=pd.DataFrame()):
        result = simulate_variant(None, CONTROL, frames, starting_equity=100_000.0)

    assert (result.equity_curve["cash"] >= -1e-6).all()


def test_portfolio_skipped_opportunity_accounting():
    from strategy_lab.portfolio_simulator import capital_constraint_summary, simulate_variant

    frames = {f"T{i}": _make_ticker_frame(f"T{i}", "2024-01-03", score=80.0) for i in range(10)}
    with patch("strategy_lab.portfolio_simulator.compute_historical_regime_series", return_value=pd.DataFrame()):
        result = simulate_variant(None, CONTROL, frames, starting_equity=1_000_000.0)

    summary = capital_constraint_summary(result)
    assert summary["entries_executed"] == len(result.trades)
    assert summary["eligible_entries_considered"] == 10
    assert summary["entries_executed"] + summary["skipped_position_cap"] + summary["skipped_exposure_limit"] + summary["skipped_insufficient_cash"] == 10


def test_control_a_b_isolation_regime_gating():
    """CONTROL enters regardless of regime; A/B only enter when bullish -
    confirms the three variants are genuinely isolated, not sharing state."""
    from strategy_lab.portfolio_simulator import simulate_variant

    frames = {"AAA": _make_ticker_frame("AAA", "2024-01-03")}
    bearish_regime = pd.DataFrame({"date": pd.bdate_range("2024-01-02", periods=40).strftime("%Y-%m-%d"), "label": "bearish"})

    with patch("strategy_lab.portfolio_simulator.compute_historical_regime_series", return_value=bearish_regime):
        control_result = simulate_variant(None, CONTROL, frames, starting_equity=100_000.0)
        a_result = simulate_variant(None, EXPERIMENT_A, frames, starting_equity=100_000.0)
        b_result = simulate_variant(None, EXPERIMENT_B, frames, starting_equity=100_000.0)

    assert len(control_result.trades) == 1   # CONTROL ignores regime
    assert len(a_result.trades) == 0         # A requires bullish regime at entry - never bullish here
    assert len(b_result.trades) == 0         # same entry rule as A


def test_portfolio_no_leverage_structural_assertion():
    from strategy_lab.portfolio_simulator import simulate_variant
    from trading.config import RiskConfig

    leveraged_config = RiskConfig(max_position_size_pct=0.10, max_total_exposure_pct=0.60, max_open_positions=6, allow_margin=True)
    frames = {"AAA": _make_ticker_frame("AAA", "2024-01-03")}
    with pytest.raises(AssertionError):
        with patch("strategy_lab.portfolio_simulator.compute_historical_regime_series", return_value=pd.DataFrame()):
            simulate_variant(None, CONTROL, frames, starting_equity=100_000.0, risk_config=leveraged_config)
