"""Tests for Phase 17 Area B: regime provenance on prospective observations
(docs/specs/phase17.md §2, §2.2, §7 items 1, 3, 6-7).

Covers:
  - strategy_lab/prospective.py::ensure_schema - 4 new columns, additive
    migration, legacy rows untouched.
  - record_observation - 4 new optional kwargs, backward compatible.
  - build_todays_observation - regime_benchmark/regime_benchmark_source
    constants, and the FRESH/STALE/UNAVAILABLE wiring end-to-end.
"""
import sqlite3
from datetime import date, timedelta

import pytest

from db.schema import init_db
from research.config import DEFAULT_REGIME_CONFIG
from strategy_lab.data import RESEARCH_SOURCE
from strategy_lab.prospective import (
    TABLE_NAME,
    build_todays_observation,
    ensure_schema,
    load_observations,
    record_observation,
)
from strategy_lab.regime_history import compute_historical_regime_series

NEW_COLUMNS = (
    "regime_benchmark", "regime_benchmark_source", "regime_benchmark_data_date", "regime_freshness_status",
    "control_exit_signal", "experiment_a_exit_signal", "experiment_b_exit_technical_signal",
    "experiment_b_exit_regime_loss_signal",
)


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _cols(conn):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()}


# =================== §2.2 item 1 / §3.4 item 1: schema migration ===================


def test_fresh_db_ensure_schema_adds_all_8_new_columns():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    cols = _cols(conn)
    for c in NEW_COLUMNS:
        assert c in cols, f"expected new Phase 17 column {c!r} in {TABLE_NAME}"


_LEGACY_PRE_PHASE17_CREATE_SQL = f"""
CREATE TABLE {TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    score REAL,
    stage TEXT,
    regime TEXT,
    control_entry_signal INTEGER,
    experiment_a_entry_signal INTEGER,
    experiment_b_entry_signal INTEGER,
    adx REAL,
    rsi REAL,
    macd REAL,
    macd_signal REAL,
    volume_ratio REAL,
    close REAL,
    source TEXT,
    methodology_version TEXT,
    config_fingerprint TEXT,
    UNIQUE(ticker, observation_date)
)
"""


def test_pre_existing_db_migration_adds_columns_and_preserves_existing_row_values():
    conn = sqlite3.connect(":memory:")
    conn.execute(_LEGACY_PRE_PHASE17_CREATE_SQL)
    conn.execute(
        f"""
        INSERT INTO {TABLE_NAME} (
            created_at, observation_date, ticker, score, stage, regime,
            control_entry_signal, experiment_a_entry_signal, experiment_b_entry_signal,
            adx, rsi, macd, macd_signal, volume_ratio, close, source, methodology_version, config_fingerprint
        ) VALUES ('2026-01-01T00:00:00', '2026-01-01', 'AAA', 80.0, 'volume', 'bullish_trend',
                   1, 1, 1, 30.0, 60.0, 1.0, 0.5, 1.5, 100.0, 'alpaca_adjusted', 'phase11-v1', 'fp1')
        """
    )
    conn.commit()

    ensure_schema(conn)  # the migration under test

    cols = _cols(conn)
    for c in NEW_COLUMNS:
        assert c in cols

    row = conn.execute(f"SELECT * FROM {TABLE_NAME} WHERE ticker = 'AAA'").fetchone()
    row = dict(zip([d[0] for d in conn.execute(f"SELECT * FROM {TABLE_NAME}").description], row))
    # Pre-existing columns are completely untouched.
    assert row["score"] == 80.0
    assert row["regime"] == "bullish_trend"
    assert row["config_fingerprint"] == "fp1"
    # New columns are NULL for the legacy row - never fabricated/backfilled.
    for c in NEW_COLUMNS:
        assert row[c] is None


# =================== §2.2 item 6: record_observation without new kwargs ===================


def test_record_observation_without_new_kwargs_succeeds_with_nulls():
    conn = make_test_db()
    inserted = record_observation(
        conn, observation_date="2026-01-01", ticker="AAA", score=80.0, stage="volume", regime="bullish_trend",
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=100.0,
        source="alpaca_adjusted", methodology_version="phase11-v1", config_fingerprint="fp1",
        # NOTE: none of the 8 new Phase 17 kwargs passed - mirrors
        # tests/test_ops_correction_impact_audit.py::_seed_observation exactly.
    )
    assert inserted is True

    stored = load_observations(conn, ticker="AAA")
    assert len(stored) == 1
    row = stored.iloc[0]
    for c in NEW_COLUMNS:
        assert row[c] is None or (isinstance(row[c], float) and row[c] != row[c])  # NaN via pandas read


# =================== §2.2 item 2: constants on every call ===================


def _seed_indicator_inputs(conn, ticker="AAPL", source="alpaca"):
    d0 = date(2025, 1, 1)
    price = 100.0
    for i in range(260):
        price += 0.2
        d = (d0 + timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            (ticker, d, price, price, price, price, 1000, source),
        )
    conn.commit()


def _seed_spy_series(conn, start="2025-01-01", n=260, base=300.0, step=0.5, source=RESEARCH_SOURCE):
    d0 = date.fromisoformat(start)
    price = base
    for i in range(n):
        price += step
        d = (d0 + timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            ("SPY", d, price, price, price, price, 1000, source),
        )
    conn.commit()


def test_build_todays_observation_regime_benchmark_constants_present_regardless_of_data():
    """§2.2 item 2: regime_benchmark/regime_benchmark_source are CONSTANTS -
    present even when SPY has zero rows (missing case)."""
    conn = make_test_db()
    _seed_indicator_inputs(conn)
    obs = build_todays_observation(conn, "AAPL", as_of_date="2025-12-31")
    assert obs is not None
    assert obs["regime_benchmark"] == DEFAULT_REGIME_CONFIG.primary_benchmark == "SPY"
    assert obs["regime_benchmark_source"] == RESEARCH_SOURCE == "alpaca_adjusted"


# =================== §1.3 item 4 / §2.2 items 3-5: FRESH/STALE/UNAVAILABLE wiring ===================


def test_build_todays_observation_fresh_case():
    conn = make_test_db()
    _seed_indicator_inputs(conn)
    _seed_spy_series(conn)

    regime_series = compute_historical_regime_series(conn)
    assert not regime_series.empty
    latest_spy_date = regime_series.iloc[-1]["date"]

    obs = build_todays_observation(conn, "AAPL", as_of_date=latest_spy_date)
    assert obs["regime_benchmark_data_date"] == latest_spy_date
    assert obs["regime_freshness_status"] == "FRESH"
    assert obs["regime"] is not None


def test_build_todays_observation_stale_case_regime_still_correct_and_non_none():
    """The exact Phase 16 §0.1 scenario: SPY's latest bar is before as_of_date.
    regime must still be the correct prior label, freshness must say STALE -
    Area A/B never conflict with Phase 16's fix."""
    conn = make_test_db()
    _seed_indicator_inputs(conn)
    _seed_spy_series(conn)

    regime_series = compute_historical_regime_series(conn)
    last_available_date = regime_series.iloc[-1]["date"]
    as_of_date = (date.fromisoformat(last_available_date) + timedelta(days=1)).isoformat()

    obs = build_todays_observation(conn, "AAPL", as_of_date=as_of_date)
    assert obs["regime_benchmark_data_date"] == last_available_date
    assert obs["regime_freshness_status"] == "STALE"
    assert obs["regime"] is not None
    assert obs["regime"] == regime_series.iloc[-1]["label"]


def test_build_todays_observation_missing_case_zero_spy_history():
    conn = make_test_db()
    _seed_indicator_inputs(conn)
    # No SPY rows at all.
    obs = build_todays_observation(conn, "AAPL", as_of_date="2025-12-31")
    assert obs["regime_benchmark_data_date"] is None
    assert obs["regime_freshness_status"] == "UNAVAILABLE"
    assert obs["regime"] is None


# =================== §2.2 item 7: idempotency ===================


def test_build_todays_observation_twice_identical_freshness_fields():
    conn = make_test_db()
    _seed_indicator_inputs(conn)
    _seed_spy_series(conn)

    as_of_date = "2025-09-15"
    obs1 = build_todays_observation(conn, "AAPL", as_of_date=as_of_date)
    obs2 = build_todays_observation(conn, "AAPL", as_of_date=as_of_date)

    assert obs1["regime_benchmark_data_date"] == obs2["regime_benchmark_data_date"]
    assert obs1["regime_freshness_status"] == obs2["regime_freshness_status"]
    assert obs1["regime_benchmark"] == obs2["regime_benchmark"]
    assert obs1["regime_benchmark_source"] == obs2["regime_benchmark_source"]
