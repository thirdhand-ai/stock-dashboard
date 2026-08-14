"""Tests for Phase 15 Component A (evidence provenance capture) and the tiny
shared ops/evidence_provenance.py label helper (docs/specs/phase15.md §1,
§8 items 1-7 and 29).

Everything here is synthetic/deterministic and uses an in-memory SQLite DB.
No test makes a real network call. Covers:
  - config_fingerprint round-tripping through record_observation/
    load_observations, build_todays_observation, and record_event/
    record_events_for_observation.
  - Legacy-row NULL semantics, mirroring the REAL confirmed AAPL
    (observation_date=2026-08-12, id=1) row shape: source=NULL,
    methodology_version=NULL, config_fingerprint=NULL (docs/specs/phase15.md
    §0.2 finding 2) - never fabricated on maturation.
  - Fail-open config_fingerprint capture in run_research_job.
  - A single config_fingerprint shared across every ticker in one job run.
  - compute_outcome_for_horizon producing byte-identical results to the
    still-private _compute_one.
  - Duplicate/retry idempotency: config_fingerprint on an already-inserted
    row is never touched by a later retry.
"""
import sqlite3
from datetime import date
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from automation.trading_calendar import trading_sessions_between
from db.run_history_repository import STATUS_SUCCESS, finish_run, start_run
from db.schema import init_db
from ops.evidence_provenance import PROVENANCE_UNKNOWN_LEGACY, label_provenance_value
from strategy_lab.outcome_maturation import (
    STATUS_MATURED,
    STATUS_PENDING,
    _compute_one,
    compute_outcome_for_horizon,
    load_outcomes,
    mature_outcomes,
)
from strategy_lab.prospective import (
    METHODOLOGY_VERSION,
    TABLE_NAME,
    build_todays_observation,
    ensure_schema as ensure_observations_schema,
    load_observations,
    record_observation,
)
from strategy_lab.prospective_events import load_events, record_event, record_events_for_observation
from strategy_lab.research_automation import (
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
    run_id = start_run(conn, "real")
    finish_run(conn, run_id, status=STATUS_SUCCESS, tickers_attempted=1, tickers_updated=1)


def _canned_obs(ticker, as_of_date, config_fingerprint=None):
    return {
        "observation_date": as_of_date, "ticker": ticker, "score": 90.0, "stage": "volume",
        "regime": "bullish_trend", "control_entry_signal": True, "experiment_a_entry_signal": True,
        "experiment_b_entry_signal": True, "adx": 35.0, "rsi": 70.0, "macd": 2.0, "macd_signal": 1.0,
        "volume_ratio": 2.0, "close": 120.0, "source": "alpaca_adjusted",
        "config_fingerprint": config_fingerprint,
    }


def _seed_price(conn, ticker, d, close, source, volume=1000):
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, d, close, close, close, close, volume, source),
    )
    conn.commit()


# --- #1: round-trip through record_observation/load_observations ---


def test_config_fingerprint_round_trips_through_record_and_load():
    conn = make_test_db()
    record_observation(
        conn, observation_date="2024-01-02", ticker="AAA", score=50.0, stage="trend", regime="bullish_trend",
        control_entry_signal=False, experiment_a_entry_signal=False, experiment_b_entry_signal=False,
        adx=20.0, rsi=40.0, macd=0.1, macd_signal=0.05, volume_ratio=1.0, close=100.0,
        source="alpaca_adjusted", config_fingerprint="abc123",
    )
    rows = load_observations(conn, ticker="AAA")
    assert len(rows) == 1
    assert rows.iloc[0]["config_fingerprint"] == "abc123"


# --- #2: pre-migration-shaped row -> config_fingerprint IS NULL after ensure_schema() ---


def test_pre_migration_row_gets_null_config_fingerprint_never_defaulted():
    conn = make_test_db()
    # Simulate a DB created BEFORE the Phase 15 migration: the table exists
    # with source/methodology_version but no config_fingerprint column at all.
    conn.execute(f"""
        CREATE TABLE {TABLE_NAME} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            score REAL, stage TEXT, regime TEXT,
            control_entry_signal INTEGER, experiment_a_entry_signal INTEGER, experiment_b_entry_signal INTEGER,
            adx REAL, rsi REAL, macd REAL, macd_signal REAL, volume_ratio REAL, close REAL,
            source TEXT, methodology_version TEXT,
            UNIQUE(ticker, observation_date)
        )
    """)
    conn.execute(
        f"INSERT INTO {TABLE_NAME} (created_at, observation_date, ticker, source, methodology_version) "
        "VALUES ('2024-01-01T00:00:00', '2024-01-02', 'AAPL', 'alpaca', 'phase11-v1')"
    )
    conn.commit()

    ensure_observations_schema(conn)  # runs the guarded ALTER TABLE ADD COLUMN

    rows = load_observations(conn, ticker="AAPL")
    assert len(rows) == 1
    assert pd.isna(rows.iloc[0]["config_fingerprint"]) or rows.iloc[0]["config_fingerprint"] is None


# --- #3: build_todays_observation(..., config_fingerprint=None) default ---


def _mocked_build_todays_observation(conn, ticker, as_of_date=None, config_fingerprint=None):
    from indicators.technical import IndicatorResult
    from signals.engine import SignalScore

    fake_indicators = IndicatorResult(
        ticker=ticker, ok=True, latest_date=as_of_date or "2024-01-02",
        close=100.0, rsi=60.0, macd=1.0, macd_signal=0.5, adx=30.0, volume_ratio=1.5,
    )
    fake_score = SignalScore(ticker=ticker, ok=True, score=90.0, highest_confirmed_stage="volume")

    with patch("strategy_lab.prospective.compute_indicators_for_ticker", return_value=fake_indicators), \
         patch("strategy_lab.prospective.score_indicators", return_value=fake_score), \
         patch("strategy_lab.prospective.compute_historical_regime_series", return_value=pd.DataFrame()), \
         patch("db.price_repository.resolve_source", return_value="alpaca_adjusted"):
        return build_todays_observation(conn, ticker, as_of_date=as_of_date, config_fingerprint=config_fingerprint)


def test_build_todays_observation_default_config_fingerprint_is_none():
    conn = make_test_db()
    obs = _mocked_build_todays_observation(conn, "AAA", as_of_date="2024-01-02")
    assert obs is not None
    assert "config_fingerprint" in obs
    assert obs["config_fingerprint"] is None


def test_build_todays_observation_threads_config_fingerprint_value():
    conn = make_test_db()
    obs = _mocked_build_todays_observation(conn, "AAA", as_of_date="2024-01-02", config_fingerprint="fp-xyz")
    assert obs["config_fingerprint"] == "fp-xyz"


# --- #4: single fingerprint computed once, shared across every ticker in one job run ---


def test_single_fingerprint_shared_across_tickers_in_one_run():
    conn = make_test_db()
    today = date.today()
    _seed_successful_production_run(conn, today)

    captured_fingerprints = []

    def fake_build(conn_, ticker, as_of_date=None, config_fingerprint=None):
        captured_fingerprints.append(config_fingerprint)
        return _canned_obs(ticker, today.isoformat(), config_fingerprint=config_fingerprint)

    fake_compute = Mock(side_effect=["fp-1", "fp-2", "fp-3"])
    with patch("strategy_lab.research_automation.build_todays_observation", side_effect=fake_build), \
         patch("ops.experiment_registry.compute_config_fingerprint", fake_compute):
        result = run_research_job(conn, today=today, tickers=["AAA", "BBB"])

    assert result.status == STATUS_SUCCESS_RESEARCH
    assert fake_compute.call_count == 1, "compute_config_fingerprint must be called exactly once per job run"
    assert captured_fingerprints == ["fp-1", "fp-1"], "every ticker in this run must see the SAME fingerprint value"

    rows = load_observations(conn)
    assert set(rows["config_fingerprint"]) == {"fp-1"}


# --- #5: fail-open fingerprint capture never breaks the job ---


def test_fail_open_fingerprint_capture_never_fails_job():
    conn = make_test_db()
    today = date.today()
    _seed_successful_production_run(conn, today)

    def fake_build(conn_, ticker, as_of_date=None, config_fingerprint=None):
        return _canned_obs(ticker, today.isoformat(), config_fingerprint=config_fingerprint)

    with patch("strategy_lab.research_automation.build_todays_observation", side_effect=fake_build), \
         patch("ops.experiment_registry.compute_config_fingerprint", side_effect=RuntimeError("boom fingerprint")):
        result = run_research_job(conn, today=today, tickers=["AAA"])

    # A fingerprinting failure is diagnostic metadata only - must not move
    # status off success (no *other*, unrelated error occurred here).
    assert result.status == STATUS_SUCCESS_RESEARCH
    assert result.observations_created == 1

    rows = load_observations(conn, ticker="AAA")
    assert len(rows) == 1
    assert rows.iloc[0]["config_fingerprint"] is None or pd.isna(rows.iloc[0]["config_fingerprint"])

    ticker_errors = load_ticker_errors_for_run(conn, result.run_id)
    fp_errors = ticker_errors[ticker_errors["ticker"].isna()]
    assert len(fp_errors) == 1
    assert "config_fingerprint" in fp_errors.iloc[0]["reason"]


def test_fail_open_fingerprint_capture_does_not_mask_a_real_unrelated_failure():
    """If config_fingerprint capture fails AND a genuinely different error
    also occurs (e.g. a ticker's observation build blows up), the run must
    still surface as PARTIAL_FAILURE/FAILED for the *real* reason - the
    fingerprint failure must not silently swallow that."""
    conn = make_test_db()
    today = date.today()
    _seed_successful_production_run(conn, today)

    def fake_build(conn_, ticker, as_of_date=None, config_fingerprint=None):
        raise RuntimeError("real unrelated failure")

    with patch("strategy_lab.research_automation.build_todays_observation", side_effect=fake_build), \
         patch("ops.experiment_registry.compute_config_fingerprint", side_effect=RuntimeError("boom fingerprint")):
        result = run_research_job(conn, today=today, tickers=["AAA"])

    assert result.status != STATUS_SUCCESS_RESEARCH
    assert any("real unrelated failure" in e for e in result.errors)


# --- #6: legacy regression, mirroring the REAL confirmed AAPL id=1 row shape ---


def test_legacy_null_provenance_row_never_fabricated_on_maturation():
    conn = make_test_db()
    obs_date = "2026-08-12"
    exit_date = "2026-08-13"
    # Prices under the PRODUCTION source ("alpaca"), matching what
    # _compute_one resolves to when the observation's own `source` is NULL
    # (falls through to resolve_source()'s SOURCE_PRIORITY).
    _seed_price(conn, "AAPL", obs_date, 100.0, "alpaca")
    _seed_price(conn, "AAPL", exit_date, 101.0, "alpaca")

    # Exactly the real id=1 AAPL row's shape: source=NULL,
    # methodology_version=NULL, config_fingerprint=NULL.
    record_observation(
        conn, observation_date=obs_date, ticker="AAPL", score=90.0, stage="volume", regime="bullish_trend",
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=100.0,
        source=None, methodology_version=None, config_fingerprint=None,
    )

    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))

    outcomes = load_outcomes(conn, ticker="AAPL")
    row = outcomes[outcomes["horizon_days"] == 1].iloc[0]
    assert row["status"] == STATUS_MATURED  # genuinely matured (real price history exists)
    assert row["source"] is None or pd.isna(row["source"])
    assert row["methodology_version"] is None or pd.isna(row["methodology_version"])
    assert row["config_fingerprint"] is None or pd.isna(row["config_fingerprint"])


# --- #7: compute_outcome_for_horizon is byte-identical to _compute_one ---


def test_compute_outcome_for_horizon_matches_compute_one_exactly():
    conn = make_test_db()
    obs_date = "2024-01-02"
    exit_date = "2024-01-03"
    _seed_price(conn, "AAA", obs_date, 50.0, "alpaca_adjusted")
    _seed_price(conn, "AAA", exit_date, 55.0, "alpaca_adjusted")

    record_observation(
        conn, observation_date=obs_date, ticker="AAA", score=80.0, stage="momentum", regime="bullish_trend",
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=25.0, rsi=55.0, macd=0.5, macd_signal=0.2, volume_ratio=1.2, close=50.0,
        source="alpaca_adjusted", methodology_version=METHODOLOGY_VERSION, config_fingerprint="fp-1",
    )
    obs_row = load_observations(conn, ticker="AAA").iloc[0]
    today = date(2024, 1, 3)

    direct = _compute_one(conn, obs_row, 1, today, {})
    via_alias = compute_outcome_for_horizon(conn, obs_row, 1, today, {})

    assert direct == via_alias
    assert direct.status == STATUS_MATURED
    assert via_alias.realized_return == pytest.approx(55.0 / 50.0 - 1.0)


def test_compute_outcome_for_horizon_default_price_cache_none_still_works():
    conn = make_test_db()
    obs_date = "2024-01-02"
    _seed_price(conn, "AAA", obs_date, 50.0, "alpaca_adjusted")
    record_observation(
        conn, observation_date=obs_date, ticker="AAA", score=80.0, stage="momentum", regime="bullish_trend",
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=25.0, rsi=55.0, macd=0.5, macd_signal=0.2, volume_ratio=1.2, close=50.0, source="alpaca_adjusted",
    )
    obs_row = load_observations(conn, ticker="AAA").iloc[0]
    # today == obs_date -> elapsed=0 -> PENDING branch, exercised with no explicit price_cache.
    outcome = compute_outcome_for_horizon(conn, obs_row, 1, date(2024, 1, 2))
    assert outcome.status == STATUS_PENDING


# --- #29: duplicate/retry idempotency - config_fingerprint never touched on retry ---


def test_duplicate_retry_never_overwrites_config_fingerprint_on_existing_row():
    conn = make_test_db()
    today = date.today()
    _seed_successful_production_run(conn, today)

    def fake_build(conn_, ticker, as_of_date=None, config_fingerprint=None):
        return _canned_obs(ticker, today.isoformat(), config_fingerprint=config_fingerprint)

    with patch("strategy_lab.research_automation.build_todays_observation", side_effect=fake_build), \
         patch("ops.experiment_registry.compute_config_fingerprint", return_value="fp-first"):
        first = run_research_job(conn, today=today, tickers=["AAA"])
    assert first.observations_created == 1
    assert first.duplicates_skipped == 0

    with patch("strategy_lab.research_automation.build_todays_observation", side_effect=fake_build), \
         patch("ops.experiment_registry.compute_config_fingerprint", return_value="fp-second-must-not-apply"):
        second = run_research_job(conn, today=today, tickers=["AAA"])
    assert second.observations_created == 0
    assert second.duplicates_skipped == 1

    rows = load_observations(conn, ticker="AAA")
    assert len(rows) == 1
    assert rows.iloc[0]["config_fingerprint"] == "fp-first"


# --- Component A on prospective_events.py: source/config_fingerprint threaded through ---


def test_record_event_and_record_events_for_observation_thread_provenance():
    conn = make_test_db()
    first = record_event(
        conn, event_date="2024-01-03", ticker="AAA", event_type="score_crossing_70",
        score=75.0, stage="momentum", regime="bullish_trend",
        source="alpaca_adjusted", config_fingerprint="fp-event-1",
    )
    assert first is True
    rows = load_events(conn, ticker="AAA")
    assert rows.iloc[0]["source"] == "alpaca_adjusted"
    assert rows.iloc[0]["config_fingerprint"] == "fp-event-1"


def test_record_events_for_observation_passes_source_and_fingerprint_from_curr_row():
    conn = make_test_db()
    # Prior baseline row (low score/no entry) so a genuine transition fires.
    record_observation(
        conn, observation_date="2024-01-02", ticker="AAA", score=10.0, stage="none", regime="bullish_trend",
        control_entry_signal=False, experiment_a_entry_signal=False, experiment_b_entry_signal=False,
        adx=10.0, rsi=20.0, macd=-1.0, macd_signal=-0.5, volume_ratio=0.5, close=90.0,
        source="alpaca_adjusted",
    )
    curr_row = {
        "observation_date": "2024-01-03", "ticker": "AAA", "score": 90.0, "stage": "volume",
        "regime": "bullish_trend", "control_entry_signal": True, "experiment_a_entry_signal": True,
        "experiment_b_entry_signal": True, "source": "alpaca_adjusted", "config_fingerprint": "fp-obs-1",
    }
    detected, newly_recorded = record_events_for_observation(conn, curr_row)
    assert newly_recorded, "a genuine transition must produce at least one recorded event"

    events = load_events(conn, ticker="AAA")
    assert not events.empty
    assert set(events["source"].dropna()) == {"alpaca_adjusted"}
    assert set(events["config_fingerprint"].dropna()) == {"fp-obs-1"}


# --- ops/evidence_provenance.py's own tiny helper ---


def test_label_provenance_value_returns_value_when_present():
    assert label_provenance_value("alpaca_adjusted") == "alpaca_adjusted"


def test_label_provenance_value_labels_none_as_unknown_legacy():
    assert label_provenance_value(None) == PROVENANCE_UNKNOWN_LEGACY


def test_label_provenance_value_labels_empty_string_as_unknown_legacy():
    assert label_provenance_value("") == PROVENANCE_UNKNOWN_LEGACY


def test_provenance_unknown_legacy_constant_value():
    assert PROVENANCE_UNKNOWN_LEGACY == "UNKNOWN_LEGACY"
