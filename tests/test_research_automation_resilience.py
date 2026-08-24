"""Tests for Phase 14 Component C: strategy_lab/research_automation.py
resilience fixes (docs/specs/phase14.md §3):
  - Bug 1: false "success" status on partial failure (fixed via
    STATUS_PARTIAL_FAILURE_RESEARCH).
  - Bug 2: event creation permanently orphaned on retry after a mid-loop
    failure (fixed by decoupling event-building from `if inserted:`).
  - record_ticker_error / load_ticker_errors_for_run / research_run_ticker_errors.

Everything here is synthetic/deterministic and uses an in-memory SQLite DB -
no test makes a real network call, and no test may place a paper/live order
or send a Discord notification.
"""
import sqlite3
from datetime import date
from unittest.mock import patch

import pytest

from db.alert_repository import get_alert_state, upsert_alert_state
from db.run_history_repository import STATUS_SUCCESS, finish_run, start_run
from db.schema import init_db
from strategy_lab.prospective import load_observations, record_observation
from strategy_lab.prospective_events import load_events
from strategy_lab.research_automation import (
    STATUS_FAILED,
    STATUS_PARTIAL_FAILURE_RESEARCH,
    STATUS_SUCCESS_RESEARCH,
    load_ticker_errors_for_run,
    run_research_job,
)


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _seed_successful_production_run(conn, today):
    run_id = start_run(conn, "real", trading_date=today)
    finish_run(conn, run_id, status=STATUS_SUCCESS, tickers_attempted=1, tickers_updated=1)


def _canned_obs(ticker, as_of_date, score=90.0, stage="volume", control=True):
    return {
        "observation_date": as_of_date, "ticker": ticker, "score": score, "stage": stage,
        "regime": "bullish_trend", "control_entry_signal": control, "experiment_a_entry_signal": control,
        "experiment_b_entry_signal": control, "adx": 35.0, "rsi": 70.0, "macd": 2.0, "macd_signal": 1.0,
        "volume_ratio": 2.0, "close": 120.0, "source": "alpaca_adjusted",
    }


# --- Bug 1: false success on partial failure ---


def test_partial_failure_when_one_ticker_raises_status_is_partial_failure_not_success():
    conn = make_test_db()
    today = date(2026, 8, 10)  # a Monday, confirmed NYSE trading day
    _seed_successful_production_run(conn, today)

    def fake_build(conn_, ticker, as_of_date=None, config_fingerprint=None):
        if ticker == "BBB":
            raise RuntimeError("simulated indicator failure")
        return _canned_obs(ticker, today.isoformat())

    with patch("strategy_lab.research_automation.build_todays_observation", side_effect=fake_build):
        result = run_research_job(conn, today=today, tickers=["AAA", "BBB"])

    assert result.status == STATUS_PARTIAL_FAILURE_RESEARCH
    assert result.status != "success"
    assert result.observations_created == 1

    ticker_errors = load_ticker_errors_for_run(conn, result.run_id)
    assert len(ticker_errors) == 1
    assert ticker_errors.iloc[0]["ticker"] == "BBB"
    assert "RuntimeError" in ticker_errors.iloc[0]["reason"]


def test_maturation_only_failure_still_moves_status_off_success():
    conn = make_test_db()
    today = date(2026, 8, 10)  # a Monday, confirmed NYSE trading day
    _seed_successful_production_run(conn, today)

    with patch("strategy_lab.research_automation.build_todays_observation",
               return_value=_canned_obs("AAA", today.isoformat())), \
         patch("strategy_lab.research_automation.mature_outcomes", side_effect=RuntimeError("maturation boom")):
        result = run_research_job(conn, today=today, tickers=["AAA"])

    assert result.status == STATUS_PARTIAL_FAILURE_RESEARCH
    assert result.observations_created == 1
    assert any("maturation" in e for e in result.errors)

    ticker_errors = load_ticker_errors_for_run(conn, result.run_id)
    assert len(ticker_errors) == 1
    assert ticker_errors.iloc[0]["ticker"] is None
    assert ticker_errors.iloc[0]["source"] is None
    assert "maturation" in ticker_errors.iloc[0]["reason"]


def test_total_failure_zero_progress_and_errors_is_status_failed():
    conn = make_test_db()
    today = date(2026, 8, 10)  # a Monday, confirmed NYSE trading day
    _seed_successful_production_run(conn, today)

    with patch("strategy_lab.research_automation.build_todays_observation",
               side_effect=RuntimeError("total failure")):
        result = run_research_job(conn, today=today, tickers=["BBB"])

    assert result.status == STATUS_FAILED
    assert result.observations_created == 0
    assert result.duplicates_skipped == 0


def test_clean_run_with_no_errors_stays_success():
    conn = make_test_db()
    today = date(2026, 8, 10)  # a Monday, confirmed NYSE trading day
    _seed_successful_production_run(conn, today)

    with patch("strategy_lab.research_automation.build_todays_observation",
               return_value=_canned_obs("AAA", today.isoformat())):
        result = run_research_job(conn, today=today, tickers=["AAA"])

    assert result.status == STATUS_SUCCESS_RESEARCH
    assert result.errors == []


# --- Bug 2: event-building must be retryable, not permanently orphaned ---


def test_bug2_crash_during_event_building_then_retry_recovers_events():
    conn = make_test_db()
    today = date(2026, 8, 10)  # a Monday, confirmed NYSE trading day
    _seed_successful_production_run(conn, today)

    # Seed a PRIOR day's stored observation as the transition baseline -
    # low score/stage/no entry signal - so today's canned observation
    # (high score/stage/entry signal True) produces genuine, real-logic
    # detected events once event-building actually runs.
    from automation.trading_calendar import trading_sessions_between
    candidate_days = [d for d in trading_sessions_between(date(today.year - 1, 1, 1), today) if d < today]
    prior_date = candidate_days[-1].isoformat()
    record_observation(
        conn, observation_date=prior_date, ticker="AAA", score=20.0, stage="none", regime="bullish_trend",
        control_entry_signal=False, experiment_a_entry_signal=False, experiment_b_entry_signal=False,
        adx=15.0, rsi=30.0, macd=-0.5, macd_signal=-0.3, volume_ratio=0.8, close=90.0, source="alpaca_adjusted",
    )

    todays_obs = _canned_obs("AAA", today.isoformat())

    # --- First call: event-building throws for this ticker ---
    with patch("strategy_lab.research_automation.build_todays_observation", return_value=todays_obs), \
         patch("strategy_lab.research_automation.record_events_for_observation",
               side_effect=RuntimeError("simulated event-build crash")):
        first = run_research_job(conn, today=today, tickers=["AAA"])

    assert first.observations_created == 1  # observation IS committed
    assert len(load_observations(conn)) == 2  # prior seed row + today's new row
    assert load_events(conn, ticker="AAA").empty  # events NOT recorded
    assert any("AAA" in e for e in first.errors)
    assert first.status in (STATUS_PARTIAL_FAILURE_RESEARCH, STATUS_FAILED)

    first_ticker_errors = load_ticker_errors_for_run(conn, first.run_id)
    assert len(first_ticker_errors) == 1
    assert first_ticker_errors.iloc[0]["ticker"] == "AAA"
    assert first_ticker_errors.iloc[0]["source"] == "alpaca_adjusted"  # obs is not None guard (§3.3 correction)

    # --- Second call: mock removed - this is the retry ---
    with patch("strategy_lab.research_automation.build_todays_observation", return_value=todays_obs):
        second = run_research_job(conn, today=today, tickers=["AAA"])

    assert second.observations_created == 0
    assert second.duplicates_skipped == 1  # record_observation correctly sees a duplicate
    assert second.errors == []
    assert second.status == STATUS_SUCCESS_RESEARCH

    events = load_events(conn, ticker="AAA")
    assert not events.empty, "Bug 2 regression: retry must actually record events, not stay permanently orphaned"
    event_types = set(events["event_type"])
    assert "score_crossing_70" in event_types
    assert "volume_advance" in event_types
    assert "control_entry" in event_types
    assert second.events_created == len(events)


def test_duplicate_rerun_is_idempotent_events_created_zero_on_clean_second_run():
    conn = make_test_db()
    today = date(2026, 8, 10)  # a Monday, confirmed NYSE trading day
    _seed_successful_production_run(conn, today)

    from automation.trading_calendar import trading_sessions_between
    candidate_days = [d for d in trading_sessions_between(date(today.year - 1, 1, 1), today) if d < today]
    prior_date = candidate_days[-1].isoformat()
    record_observation(
        conn, observation_date=prior_date, ticker="AAA", score=10.0, stage="none", regime="bullish_trend",
        control_entry_signal=False, experiment_a_entry_signal=False, experiment_b_entry_signal=False,
        adx=10.0, rsi=25.0, macd=-1.0, macd_signal=-0.8, volume_ratio=0.5, close=80.0, source="alpaca_adjusted",
    )
    todays_obs = _canned_obs("AAA", today.isoformat())

    with patch("strategy_lab.research_automation.build_todays_observation", return_value=todays_obs):
        first = run_research_job(conn, today=today, tickers=["AAA"])
        second = run_research_job(conn, today=today, tickers=["AAA"])

    assert first.observations_created == 1
    assert first.events_created > 0  # genuine events on the first, real run

    assert second.observations_created == 0
    assert second.duplicates_skipped == 1
    assert second.events_created == 0  # Phase 14 §3.4 fix - no re-inflated count on a clean retry

    events_after_first = load_events(conn, ticker="AAA")
    with patch("strategy_lab.research_automation.build_todays_observation", return_value=todays_obs):
        run_research_job(conn, today=today, tickers=["AAA"])
    events_after_second = load_events(conn, ticker="AAA")
    assert len(events_after_first) == len(events_after_second)  # no duplicate event rows
    assert len(load_observations(conn)) == 2  # prior seed + the single today row, never duplicated


def test_research_job_never_mutates_alert_state_even_on_partial_failure():
    conn = make_test_db()
    upsert_alert_state(conn, "AAPL", score=70.0, stage="momentum", alerted=False)
    before = dict(get_alert_state(conn, "AAPL"))

    today = date(2026, 8, 10)  # a Monday, confirmed NYSE trading day
    _seed_successful_production_run(conn, today)
    with patch("strategy_lab.research_automation.build_todays_observation",
               side_effect=RuntimeError("boom")):
        run_research_job(conn, today=today, tickers=["AAA"])

    after = dict(get_alert_state(conn, "AAPL"))
    assert after == before
