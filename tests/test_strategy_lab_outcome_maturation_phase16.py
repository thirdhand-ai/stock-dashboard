"""Tests for Phase 16 Area D: strategy_lab/outcome_maturation.py's new
source_resolution_method / effective_price_source_used columns
(docs/specs/phase16.md §4.3, §8 items 8-9).

The safety-critical guarantee under test: a row with a real, explicit
`source` can NEVER reach the resolve_source() fallback path - proven here
by mocking resolve_source to return a different value and asserting it is
never called for that row.
"""
import sqlite3
from datetime import date

import pytest

from db.schema import init_db
from strategy_lab.outcome_maturation import (
    SOURCE_RESOLUTION_EXPLICIT,
    SOURCE_RESOLUTION_FALLBACK,
    SOURCE_RESOLUTION_UNAVAILABLE,
    STATUS_MATURED,
    STATUS_PENDING,
    STATUS_UNAVAILABLE,
    load_outcomes,
    mature_outcomes,
    source_resolution_summary,
)
from strategy_lab.prospective import record_observation


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _seed_price(conn, ticker, d, close, source, volume=1000):
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, d, close, close, close, close, volume, source),
    )
    conn.commit()


def _seed_observation(conn, ticker, obs_date, source=None, methodology_version=None, config_fingerprint=None,
                       score=90.0, stage="volume", regime="bullish_trend", close=100.0):
    record_observation(
        conn, observation_date=obs_date, ticker=ticker, score=score, stage=stage, regime=regime,
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=close,
        source=source, methodology_version=methodology_version, config_fingerprint=config_fingerprint,
    )


# --- §4.3 item 1: explicit source ---


def test_explicit_source_recorded_as_explicit():
    conn = make_test_db()
    obs_date, exit_date = "2026-08-12", "2026-08-13"
    _seed_price(conn, "AAPL", obs_date, 100.0, "alpaca_adjusted")
    _seed_price(conn, "AAPL", exit_date, 105.0, "alpaca_adjusted")
    _seed_observation(conn, "AAPL", obs_date, source="alpaca_adjusted")

    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))
    outcomes = load_outcomes(conn, ticker="AAPL")
    row = outcomes[outcomes["horizon_days"] == 1].iloc[0]

    assert row["source_resolution_method"] == SOURCE_RESOLUTION_EXPLICIT
    assert row["effective_price_source_used"] == "alpaca_adjusted"
    assert row["status"] == STATUS_MATURED


# --- §4.3 item 2: real-shape regression - source=None, price only under "alpaca" ---


def test_legacy_source_none_falls_back_and_source_column_stays_null():
    conn = make_test_db()
    obs_date, exit_date = "2026-08-12", "2026-08-13"
    _seed_price(conn, "AAPL", obs_date, 100.0, "alpaca")
    _seed_price(conn, "AAPL", exit_date, 101.0, "alpaca")
    _seed_observation(conn, "AAPL", obs_date, source=None)  # matches real id=1 shape

    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))
    outcomes = load_outcomes(conn, ticker="AAPL")
    row = outcomes[outcomes["horizon_days"] == 1].iloc[0]

    assert row["source_resolution_method"] == SOURCE_RESOLUTION_FALLBACK
    assert row["effective_price_source_used"] == "alpaca"
    assert row["source"] is None or (isinstance(row["source"], float))  # existing Phase 15 column stays NULL


# --- §4.3 item 3: zero price data anywhere -> unavailable ---


def test_no_price_data_anywhere_marks_unavailable():
    conn = make_test_db()
    obs_date = "2026-08-12"
    _seed_observation(conn, "ZZZZ", obs_date, source=None)

    mature_outcomes(conn, today=date(2026, 8, 20), horizons=(1,))
    outcomes = load_outcomes(conn, ticker="ZZZZ")
    row = outcomes[outcomes["horizon_days"] == 1].iloc[0]

    assert row["status"] == STATUS_UNAVAILABLE
    assert row["source_resolution_method"] == SOURCE_RESOLUTION_UNAVAILABLE
    assert row["effective_price_source_used"] is None or isinstance(row["effective_price_source_used"], float)


# --- §4.3 item 4 / §8 item 8: non-fallback safeguard proof ---


def test_explicit_source_row_never_calls_resolve_source_even_if_it_would_return_different_value(monkeypatch):
    conn = make_test_db()
    obs_date, exit_date = "2026-08-12", "2026-08-13"
    _seed_price(conn, "AAPL", obs_date, 100.0, "alpaca_adjusted")
    _seed_price(conn, "AAPL", exit_date, 105.0, "alpaca_adjusted")
    _seed_observation(conn, "AAPL", obs_date, source="alpaca_adjusted")

    calls = []

    def fake_resolve_source(conn_arg, ticker):
        calls.append(ticker)
        return "SENTINEL_SOURCE_SHOULD_NEVER_BE_USED"

    import db.price_repository as price_repo_module
    monkeypatch.setattr(price_repo_module, "resolve_source", fake_resolve_source)

    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))
    outcomes = load_outcomes(conn, ticker="AAPL")
    row = outcomes[outcomes["horizon_days"] == 1].iloc[0]

    assert row["effective_price_source_used"] == "alpaca_adjusted", (
        "the explicit, real source must be used - never the sentinel from a patched resolve_source"
    )
    assert calls == [], f"resolve_source must never be called for a row with a real explicit source: {calls}"


def test_legacy_row_does_call_resolve_source_and_records_fallback(monkeypatch):
    conn = make_test_db()
    obs_date, exit_date = "2026-08-12", "2026-08-13"
    _seed_price(conn, "AAPL", obs_date, 100.0, "alpaca")
    _seed_price(conn, "AAPL", exit_date, 101.0, "alpaca")
    _seed_observation(conn, "AAPL", obs_date, source=None)

    calls = []
    import db.price_repository as price_repo_module
    real_resolve_source = price_repo_module.resolve_source

    def spying_resolve_source(conn_arg, ticker):
        calls.append(ticker)
        return real_resolve_source(conn_arg, ticker)

    monkeypatch.setattr(price_repo_module, "resolve_source", spying_resolve_source)

    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))
    outcomes = load_outcomes(conn, ticker="AAPL")
    row = outcomes[outcomes["horizon_days"] == 1].iloc[0]

    # docs/specs/phase16.md Sec4.1 explicitly documents that outcome_maturation.py
    # deliberately duplicates the cheap, pure resolve_source read (its own call,
    # separate from the one load_price_history() itself makes when source=None
    # is passed through) rather than changing db/price_repository.py's shared
    # public contract - so >=1 call, not exactly one, is the documented-correct
    # behavior for a legacy row.
    assert calls, f"a legacy (source=None) row must call resolve_source at least once: {calls}"
    assert set(calls) == {"AAPL"}
    assert row["source_resolution_method"] == SOURCE_RESOLUTION_FALLBACK


# --- §4.3 item 5: STATUS_PENDING - both new fields stay None ---


def test_pending_outcome_has_null_resolution_fields():
    conn = make_test_db()
    obs_date = "2026-08-13"
    _seed_price(conn, "AAPL", obs_date, 100.0, "alpaca_adjusted")
    _seed_observation(conn, "AAPL", obs_date, source="alpaca_adjusted")

    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))  # elapsed=0 -> PENDING
    outcomes = load_outcomes(conn, ticker="AAPL")
    row = outcomes[outcomes["horizon_days"] == 1].iloc[0]

    assert row["status"] == STATUS_PENDING
    assert row["source_resolution_method"] is None or (isinstance(row["source_resolution_method"], float))
    assert row["effective_price_source_used"] is None or (isinstance(row["effective_price_source_used"], float))


# --- §4.3 item 6: idempotency across repeated calls ---


def test_idempotent_across_repeated_mature_outcomes_calls():
    conn = make_test_db()
    obs_date, exit_date = "2026-08-12", "2026-08-13"
    _seed_price(conn, "AAPL", obs_date, 100.0, "alpaca")
    _seed_price(conn, "AAPL", exit_date, 101.0, "alpaca")
    _seed_observation(conn, "AAPL", obs_date, source=None)

    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))
    first = load_outcomes(conn, ticker="AAPL")
    first_row = first[first["horizon_days"] == 1].iloc[0]

    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))
    second = load_outcomes(conn, ticker="AAPL")
    second_row = second[second["horizon_days"] == 1].iloc[0]

    assert first_row["source_resolution_method"] == second_row["source_resolution_method"]
    assert first_row["effective_price_source_used"] == second_row["effective_price_source_used"]
    assert first_row["realized_return"] == second_row["realized_return"]


# --- §4.3 item 7 / §8 item 9: legacy real-row (id=1 AAPL shape) regression ---


def test_real_id1_aapl_shape_regression_computation_unchanged_by_new_columns():
    """Exact real shape: source=NULL, methodology_version=NULL,
    config_fingerprint=NULL (docs/specs/phase16.md §0.1's confirmed live
    id=1 row). realized_return/exit_date/status must be exactly what a
    deterministic entry/exit price pair implies - the new visibility
    columns must not alter the computed value."""
    conn = make_test_db()
    obs_date, exit_date = "2026-08-12", "2026-08-13"
    entry_close, exit_close = 301.36, 305.0
    _seed_price(conn, "AAPL", obs_date, entry_close, "alpaca")
    _seed_price(conn, "AAPL", exit_date, exit_close, "alpaca")
    _seed_observation(
        conn, "AAPL", obs_date, source=None, methodology_version=None, config_fingerprint=None,
        score=0.0, stage="none", regime="bullish_trend", close=entry_close,
    )

    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))
    outcomes = load_outcomes(conn, ticker="AAPL")
    row = outcomes[outcomes["horizon_days"] == 1].iloc[0]

    expected_realized_return = exit_close / entry_close - 1.0
    assert row["realized_return"] == pytest.approx(expected_realized_return)
    assert row["exit_date"] == exit_date
    assert row["status"] == STATUS_MATURED
    # Provenance columns (Phase 15) still NULL, unchanged in meaning.
    assert row["source"] is None or isinstance(row["source"], float)
    assert row["methodology_version"] is None or isinstance(row["methodology_version"], float)
    # New Phase 16 columns are additional visibility only.
    assert row["source_resolution_method"] == SOURCE_RESOLUTION_FALLBACK
    assert row["effective_price_source_used"] == "alpaca"


# --- §4.3 item 8: source_resolution_summary across a mixed fixture ---


def test_source_resolution_summary_mixed_fixture():
    conn = make_test_db()
    # explicit
    _seed_price(conn, "AAA", "2026-08-10", 100.0, "alpaca_adjusted")
    _seed_price(conn, "AAA", "2026-08-11", 101.0, "alpaca_adjusted")
    _seed_observation(conn, "AAA", "2026-08-10", source="alpaca_adjusted")
    # fallback
    _seed_price(conn, "BBB", "2026-08-10", 50.0, "alpaca")
    _seed_price(conn, "BBB", "2026-08-11", 51.0, "alpaca")
    _seed_observation(conn, "BBB", "2026-08-10", source=None)
    # unavailable - no price data at all
    _seed_observation(conn, "CCC", "2026-08-10", source=None)
    # pending - today == observation date, elapsed 0
    _seed_price(conn, "DDD", "2026-08-13", 10.0, "alpaca_adjusted")
    _seed_observation(conn, "DDD", "2026-08-13", source="alpaca_adjusted")

    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))

    summary = source_resolution_summary(conn)
    assert summary["explicit"] == 1
    assert summary["resolved_fallback"] == 1
    assert summary["unavailable"] == 1
    assert summary["not_yet_resolved"] == 1
    assert summary["total"] == 4


def test_source_resolution_summary_empty_db():
    conn = make_test_db()
    summary = source_resolution_summary(conn)
    assert summary == {"explicit": 0, "resolved_fallback": 0, "unavailable": 0, "not_yet_resolved": 0, "total": 0}
