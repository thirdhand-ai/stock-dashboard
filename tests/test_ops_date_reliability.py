"""Tests for Phase 13 Component C: deterministic date/time reliability
(docs/specs/phase13.md §5 and §0.4's implementation-time amendment).

Deterministic - no test in this file calls date.today() or depends on the
real wall clock; every date is explicit.
"""
import sqlite3
from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from automation.pipeline import run_pipeline
from automation.recovery import _todays_successful_run_exists
from automation.trading_calendar import trading_sessions_between, trading_sessions_elapsed
from db.run_history_repository import (
    STATUS_SUCCESS,
    finish_run,
    load_run_history,
    record_skipped_run,
    start_run,
)
from db.schema import init_db
from strategy_lab.prospective import record_observation
from strategy_lab.research_automation import _todays_production_run_succeeded
from indicators.technical import IndicatorResult


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_placeholder_price_row(conn, ticker, session_date="2024-06-03", source="alpaca"):
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, session_date, 149.0, 151.0, 148.0, 150.0, 2_000_000, source),
    )
    conn.commit()


def fake_ingest_ticker_factory(fail_tickers=None):
    fail_tickers = fail_tickers or set()

    def fake_ingest(conn, ticker, days=5, **kwargs):
        if ticker in fail_tickers:
            raise ConnectionError(f"simulated network failure for {ticker}")
        insert_placeholder_price_row(conn, ticker)
        return 1

    return fake_ingest


def strong_indicator_result(ticker):
    return IndicatorResult(
        ticker=ticker, ok=True, latest_date="2024-06-03", close=150.0,
        rsi=60.0, macd=2.0, macd_signal=1.0, macd_hist=1.0,
        bb_lower=140.0, bb_mid=145.0, bb_upper=150.0,
        adx=30.0, sma_50=140.0, volume=2_000_000.0, volume_avg_20=1_000_000.0, volume_ratio=2.0,
    )


def _obs(ticker="AAA", observation_date="2026-02-01"):
    return dict(
        observation_date=observation_date, ticker=ticker, score=75.0, stage="momentum", regime="bullish_trend",
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=100.0,
    )


# --- automation/recovery.py::_todays_successful_run_exists ---


def test_todays_successful_run_exists_true_via_exact_trading_date_match():
    conn = make_test_db()
    target = date(2026, 3, 10)
    # started_at deliberately on a DIFFERENT calendar date than trading_date,
    # to prove the exact-match path (not a started_at string-prefix match) is
    # what's being exercised.
    run_id = start_run(conn, "real", trading_date=target, started_at="2026-03-11 02:15:00")
    finish_run(conn, run_id, status=STATUS_SUCCESS)
    assert _todays_successful_run_exists(conn, target) is True


def test_todays_successful_run_exists_false_when_trading_date_differs():
    conn = make_test_db()
    run_id = start_run(conn, "real", trading_date=date(2026, 3, 9), started_at="2026-03-09 21:00:00")
    finish_run(conn, run_id, status=STATUS_SUCCESS)
    assert _todays_successful_run_exists(conn, date(2026, 3, 10)) is False


def test_todays_successful_run_exists_legacy_fallback_for_null_trading_date():
    conn = make_test_db()
    # Directly INSERT a raw pre-migration-shaped row: trading_date IS NULL,
    # started_at is a UTC timestamp whose calendar date matches the target
    # local date the legacy fallback is expected to string-match against.
    conn.execute(
        "INSERT INTO automation_runs (started_at, finished_at, status, send_mode, trading_date) "
        "VALUES (?, ?, ?, ?, NULL)",
        ("2026-03-10 05:00:00", "2026-03-10 05:05:00", STATUS_SUCCESS, "real"),
    )
    conn.commit()
    assert _todays_successful_run_exists(conn, date(2026, 3, 10)) is True


def test_research_job_gate_uses_exact_trading_date_not_utc_string_match():
    conn = make_test_db()
    target = date(2026, 3, 10)
    run_id = start_run(conn, "real", trading_date=target, started_at="2026-03-11 02:15:00")
    finish_run(conn, run_id, status=STATUS_SUCCESS)
    assert _todays_production_run_succeeded(conn, target) is True
    assert _todays_production_run_succeeded(conn, date(2026, 3, 9)) is False


# --- db/run_history_repository.py::start_run trading_date defaults (§0.4) ---


def test_start_run_trading_date_defaults_to_today_when_not_passed():
    conn = make_test_db()
    run_id = start_run(conn, "real")  # no started_at override -> normal call path
    row = conn.execute("SELECT trading_date FROM automation_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["trading_date"] == date.today().isoformat()


def test_start_run_trading_date_stays_none_when_started_at_override_and_no_trading_date_passed():
    conn = make_test_db()
    run_id = start_run(conn, "real", started_at="2026-03-11 02:15:00")  # override, trading_date omitted
    row = conn.execute("SELECT trading_date FROM automation_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["trading_date"] is None


def test_record_skipped_run_accepts_trading_date():
    conn = make_test_db()
    target = date(2026, 3, 10)
    run_id = record_skipped_run(conn, "real", "skipped_non_trading_day", "test skip", trading_date=target)
    row = conn.execute("SELECT trading_date, status FROM automation_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["trading_date"] == target.isoformat()
    assert row["status"] == "skipped_non_trading_day"


# --- automation/pipeline.py: trading_date threaded through both call sites ---


def test_pipeline_persists_trading_date_matching_check_date():
    conn = make_test_db()
    check_date = date(2024, 6, 20)  # a real Thursday NYSE trading day
    fake_ingest = fake_ingest_ticker_factory()
    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)):
        run_pipeline(conn, tickers=["AAA"], today=check_date, skip_non_trading_day_check=True)

    history = load_run_history(conn, limit=5)
    assert history.iloc[0]["trading_date"] == check_date.isoformat()


def test_pipeline_persists_trading_date_on_skipped_non_trading_day_run():
    conn = make_test_db()
    check_date = date(2026, 8, 9)  # a Sunday
    run_pipeline(conn, tickers=["AAA"], today=check_date)  # skip_non_trading_day_check defaults False
    history = load_run_history(conn, limit=5)
    assert history.iloc[0]["trading_date"] == check_date.isoformat()
    assert history.iloc[0]["status"] == "skipped_non_trading_day"


# --- DST-boundary / weekend-holiday regression coverage for the trading calendar ---


def test_trading_sessions_elapsed_across_dst_spring_forward_2026():
    # US DST spring-forward 2026: Sunday March 8. Friday March 6 -> Tuesday
    # March 10 spans the transition; only real NYSE sessions after start_date
    # (Mon 3/9, Tue 3/10) should count - weekend (3/7-3/8) excluded.
    start = date(2026, 3, 6)
    through = date(2026, 3, 10)
    assert trading_sessions_elapsed(start, through) == 2


def test_trading_sessions_elapsed_across_dst_fall_back_2026():
    # US DST fall-back 2026: Sunday November 1. Friday Oct 30 -> Tuesday
    # Nov 3 spans the transition.
    start = date(2026, 10, 30)
    through = date(2026, 11, 3)
    assert trading_sessions_elapsed(start, through) == 2


def test_trading_sessions_between_weekend_and_holiday_boundaries():
    # Labor Day 2026 is Monday Sept 7 (confirmed NYSE holiday, reusing the
    # date from tests/test_automation.py's holiday table rather than
    # re-deriving/duplicating the full holiday list here).
    sessions = trading_sessions_between(date(2026, 9, 4), date(2026, 9, 8))
    assert [d.isoformat() for d in sessions] == ["2026-09-04", "2026-09-08"]


def test_outcome_maturation_deterministic_across_explicit_dates_not_wall_clock():
    from strategy_lab.outcome_maturation import mature_outcomes

    conn = make_test_db()
    record_observation(conn, **_obs(ticker="ZZZ", observation_date="2026-02-25"))
    # Sessions spanning the March 8 2026 DST boundary.
    dates = pd.bdate_range("2026-02-25", periods=12).strftime("%Y-%m-%d")
    closes = [100.0 + i for i in range(len(dates))]
    for d, c in zip(dates, closes):
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            ("ZZZ", d, c, c + 1, c - 1, c, 1_000_000, "alpaca_adjusted"),
        )
    conn.commit()

    explicit_today = date(2026, 3, 12)  # comfortably past DST transition
    outcomes_1 = mature_outcomes(conn, today=explicit_today)
    outcomes_2 = mature_outcomes(conn, today=explicit_today)  # re-run, same explicit date

    row_1 = outcomes_1[(outcomes_1["ticker"] == "ZZZ") & (outcomes_1["horizon_days"] == 5)].iloc[0]
    row_2 = outcomes_2[(outcomes_2["ticker"] == "ZZZ") & (outcomes_2["horizon_days"] == 5)].iloc[0]
    assert row_1["status"] == row_2["status"]
    assert row_1["realized_return"] == row_2["realized_return"] or (
        pd.isna(row_1["realized_return"]) and pd.isna(row_2["realized_return"])
    )
