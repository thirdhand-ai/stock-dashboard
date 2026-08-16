"""Tests for Phase 17 Area A: benchmark freshness refresh
(docs/specs/phase17.md §1, §1.3, §7 items 1-2, 8-9).

Covers:
  - strategy_lab/regime_history.py: regime_label_and_date_as_of (no-look-ahead
    date sweep, extending Phase 16's own test_as_of_date_sweep_inside_and_
    outside_range pattern) and classify_regime_freshness (table-driven, 3
    states).
  - strategy_lab/research_automation.py::_refresh_benchmark_data_safe
    (fail-open: fetched/failed/exception) and its wiring into
    run_research_job (fail-open integration test).
"""
import ast
import sqlite3
from datetime import date, timedelta

import pandas as pd
import pytest

from db.schema import init_db
from research.config import DEFAULT_REGIME_CONFIG
from strategy_lab.regime_history import (
    REGIME_FRESHNESS_FRESH,
    REGIME_FRESHNESS_STALE,
    REGIME_FRESHNESS_UNAVAILABLE,
    classify_regime_freshness,
    regime_label_and_date_as_of,
)

LABEL_BULLISH = DEFAULT_REGIME_CONFIG.LABEL_BULLISH
LABEL_NEUTRAL = DEFAULT_REGIME_CONFIG.LABEL_NEUTRAL
LABEL_BEARISH = DEFAULT_REGIME_CONFIG.LABEL_BEARISH
LABEL_ELEVATED_VOL = DEFAULT_REGIME_CONFIG.LABEL_ELEVATED_VOL


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _series(dates_labels):
    return pd.DataFrame({"date": [d for d, _ in dates_labels], "label": [l for _, l in dates_labels]})


# =================== classify_regime_freshness: 3-state table sweep ===================


@pytest.mark.parametrize("observation_date,benchmark_data_date,expected", [
    (None, None, REGIME_FRESHNESS_UNAVAILABLE),
    ("2026-08-14", None, REGIME_FRESHNESS_UNAVAILABLE),
    ("2026-08-14", "2026-08-14", REGIME_FRESHNESS_FRESH),
    ("2026-08-14", "2026-08-13", REGIME_FRESHNESS_STALE),
    (None, "2026-08-14", REGIME_FRESHNESS_STALE),  # benchmark known but obs date unknown - never FRESH by fluke
])
def test_classify_regime_freshness_table_driven(observation_date, benchmark_data_date, expected):
    assert classify_regime_freshness(observation_date, benchmark_data_date) == expected


def test_classify_regime_freshness_constants_exact_strings():
    assert REGIME_FRESHNESS_FRESH == "FRESH"
    assert REGIME_FRESHNESS_STALE == "STALE"
    assert REGIME_FRESHNESS_UNAVAILABLE == "UNAVAILABLE"


# =================== regime_label_and_date_as_of: no-look-ahead sweep ===================
# Extends tests/test_strategy_lab_regime_asof.py::test_as_of_date_sweep_inside_and_outside_range
# to also assert the returned benchmark_data_date, per docs/specs/phase17.md §1.3 item 5.


@pytest.mark.parametrize("as_of_date,expected_label,expected_date", [
    ("2026-01-01", LABEL_BULLISH, "2026-01-01"),   # exact match on first row
    ("2026-01-02", LABEL_BULLISH, "2026-01-01"),   # between rows -> prior row's own date, never as_of_date itself
    ("2026-01-03", LABEL_NEUTRAL, "2026-01-03"),   # exact match on second row
    ("2026-01-04", LABEL_NEUTRAL, "2026-01-03"),   # lagging - date is the PRIOR available row's date
    ("2026-01-05", LABEL_BEARISH, "2026-01-05"),   # exact match on last row
    ("2026-06-01", LABEL_BEARISH, "2026-01-05"),   # far beyond last row -> most recent <=
    ("2025-12-31", None, None),                     # before every row -> (None, None)
])
def test_regime_label_and_date_as_of_sweep(as_of_date, expected_label, expected_date):
    series = _series([
        ("2026-01-01", LABEL_BULLISH), ("2026-01-03", LABEL_NEUTRAL), ("2026-01-05", LABEL_BEARISH),
    ])
    label, benchmark_data_date = regime_label_and_date_as_of(series, as_of_date)
    assert label == expected_label
    assert benchmark_data_date == expected_date


def test_regime_label_and_date_as_of_none_as_of_date_returns_most_recent_row():
    series = _series([("2026-01-01", LABEL_BULLISH), ("2026-01-02", LABEL_NEUTRAL), ("2026-01-03", LABEL_BEARISH)])
    label, benchmark_data_date = regime_label_and_date_as_of(series, None)
    assert label == LABEL_BEARISH
    assert benchmark_data_date == "2026-01-03"


def test_regime_label_and_date_as_of_empty_series_returns_none_none():
    empty = pd.DataFrame(columns=["date", "label"])
    assert regime_label_and_date_as_of(empty, None) == (None, None)
    assert regime_label_and_date_as_of(empty, "2026-01-01") == (None, None)


# --- weekend/holiday boundary, extending Phase 16's exact existing cases with the date return value ---


def test_regime_label_and_date_as_of_no_lookahead_across_weekend_boundary():
    series = _series([
        ("2026-08-12", LABEL_NEUTRAL), ("2026-08-13", LABEL_NEUTRAL), ("2026-08-14", LABEL_BULLISH),
        # 2026-08-15 (Sat), 2026-08-16 (Sun) never appear - no trading
    ])
    label, benchmark_data_date = regime_label_and_date_as_of(series, "2026-08-17")
    assert label == LABEL_BULLISH
    assert benchmark_data_date == "2026-08-14"  # the PRIOR available row's date, never "2026-08-17" itself


def test_regime_label_and_date_as_of_no_lookahead_across_holiday_boundary():
    series = _series([("2026-09-03", LABEL_BEARISH), ("2026-09-04", LABEL_ELEVATED_VOL)])
    label1, date1 = regime_label_and_date_as_of(series, "2026-09-07")  # Labor Day itself
    assert (label1, date1) == (LABEL_ELEVATED_VOL, "2026-09-04")
    label2, date2 = regime_label_and_date_as_of(series, "2026-09-08")  # day after, still no new bar
    assert (label2, date2) == (LABEL_ELEVATED_VOL, "2026-09-04")


# =================== _refresh_benchmark_data_safe ===================


def _get_ticker_errors(conn, run_id):
    from strategy_lab.research_automation import load_ticker_errors_for_run
    return load_ticker_errors_for_run(conn, run_id)


def test_refresh_benchmark_data_safe_fetched_and_cached_no_ticker_error(monkeypatch):
    from strategy_lab import research_automation as ra

    conn = make_test_db()
    run_id = ra._start_run(conn, date(2026, 8, 14))

    def fake_fetch(conn_, tickers):
        assert tickers == ["SPY", "QQQ"]
        return {"SPY": {"status": "fetched", "rows": 10, "error": None}, "QQQ": {"status": "cached", "rows": 20, "error": None}}

    monkeypatch.setattr(ra, "fetch_and_cache_universe", fake_fetch)
    result = ra._refresh_benchmark_data_safe(conn, run_id, date(2026, 8, 14))

    assert result["primary_benchmark"] == "SPY"
    assert result["primary_fetch_status"] == "fetched"
    assert result["secondary_benchmark"] == "QQQ"
    assert result["secondary_fetch_status"] == "cached"

    errors = _get_ticker_errors(conn, run_id)
    assert errors.empty


def test_refresh_benchmark_data_safe_primary_failed_records_one_ticker_error(monkeypatch):
    from strategy_lab import research_automation as ra

    conn = make_test_db()
    run_id = ra._start_run(conn, date(2026, 8, 14))

    def fake_fetch(conn_, tickers):
        return {"SPY": {"status": "failed", "rows": 0, "error": "alpaca timeout"}}

    monkeypatch.setattr(ra, "fetch_and_cache_universe", fake_fetch)
    result = ra._refresh_benchmark_data_safe(conn, run_id, date(2026, 8, 14))

    assert result["primary_fetch_status"] == "failed"
    errors = _get_ticker_errors(conn, run_id)
    assert len(errors) == 1
    assert errors.iloc[0]["ticker"] == "SPY"


def test_refresh_benchmark_data_safe_exception_records_one_ticker_error_with_none_ticker(monkeypatch):
    from strategy_lab import research_automation as ra

    conn = make_test_db()
    run_id = ra._start_run(conn, date(2026, 8, 14))

    def fake_fetch(conn_, tickers):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(ra, "fetch_and_cache_universe", fake_fetch)
    result = ra._refresh_benchmark_data_safe(conn, run_id, date(2026, 8, 14))

    assert result["primary_fetch_status"] == "exception"
    assert result["secondary_fetch_status"] == "exception"
    errors = _get_ticker_errors(conn, run_id)
    assert len(errors) == 1
    assert errors.iloc[0]["ticker"] is None
    assert "RuntimeError" in errors.iloc[0]["reason"]


def test_refresh_benchmark_data_safe_called_with_exactly_default_regime_config_benchmarks(monkeypatch):
    """§1.3 item 2: never strategy_lab.universe's PRIMARY_BENCHMARK/SECONDARY_BENCHMARK."""
    from strategy_lab import research_automation as ra

    conn = make_test_db()
    run_id = ra._start_run(conn, date(2026, 8, 14))
    captured = {}

    def fake_fetch(conn_, tickers):
        captured["tickers"] = tickers
        return {}

    monkeypatch.setattr(ra, "fetch_and_cache_universe", fake_fetch)
    ra._refresh_benchmark_data_safe(conn, run_id, date(2026, 8, 14))

    assert captured["tickers"] == [DEFAULT_REGIME_CONFIG.primary_benchmark, DEFAULT_REGIME_CONFIG.secondary_benchmark]


def test_research_automation_never_imports_universe_benchmark_constants():
    with open("strategy_lab/research_automation.py") as f:
        tree = ast.parse(f.read(), filename="strategy_lab/research_automation.py")
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
    assert "PRIMARY_BENCHMARK" not in imported_names
    assert "SECONDARY_BENCHMARK" not in imported_names


# =================== §1.3 item 3 / §7 item 9: fail-open integration ===================


def _seed_successful_production_run(conn, today):
    from db.run_history_repository import STATUS_SUCCESS, finish_run, start_run
    run_id = start_run(conn, "real", trading_date=today)
    finish_run(conn, run_id, status=STATUS_SUCCESS, tickers_attempted=1, tickers_updated=1)


def _canned_obs(ticker, as_of_date, score=90.0, stage="volume"):
    return {
        "observation_date": as_of_date, "ticker": ticker, "score": score, "stage": stage,
        "regime": "bullish_trend", "control_entry_signal": True, "experiment_a_entry_signal": True,
        "experiment_b_entry_signal": True, "adx": 35.0, "rsi": 70.0, "macd": 2.0, "macd_signal": 1.0,
        "volume_ratio": 2.0, "close": 120.0, "source": "alpaca_adjusted",
        "regime_benchmark": "SPY", "regime_benchmark_source": "alpaca_adjusted",
        "regime_benchmark_data_date": as_of_date, "regime_freshness_status": "FRESH",
        "control_exit_signal": False, "experiment_a_exit_signal": False,
        "experiment_b_exit_technical_signal": False, "experiment_b_exit_regime_loss_signal": False,
    }


def test_run_research_job_survives_benchmark_refresh_exception_never_status_failed(monkeypatch):
    from strategy_lab.research_automation import STATUS_FAILED, run_research_job

    conn = make_test_db()
    today = date(2024, 6, 20)  # a real NYSE trading Thursday
    _seed_successful_production_run(conn, today)

    def raising_fetch(conn_, tickers):
        raise RuntimeError("Alpaca down")

    monkeypatch.setattr("strategy_lab.research_automation.fetch_and_cache_universe", raising_fetch)
    monkeypatch.setattr(
        "strategy_lab.research_automation.build_todays_observation",
        lambda conn_, ticker, as_of_date=None, config_fingerprint=None: _canned_obs(ticker, today.isoformat()),
    )

    result = run_research_job(conn, today=today, tickers=["AAA"])

    assert result.status != STATUS_FAILED
    assert result.observations_created == 1
    assert result.benchmark_refresh_status is not None
    assert result.benchmark_refresh_status["primary_fetch_status"] == "exception"
    # A benchmark-refresh-only failure must never populate result.errors.
    assert result.errors == []


def test_run_research_job_survives_benchmark_refresh_primary_failed_status(monkeypatch):
    from strategy_lab.research_automation import STATUS_FAILED, run_research_job

    conn = make_test_db()
    today = date(2024, 6, 20)
    _seed_successful_production_run(conn, today)

    def failing_fetch(conn_, tickers):
        return {"SPY": {"status": "failed", "rows": 0, "error": "boom"}}

    monkeypatch.setattr("strategy_lab.research_automation.fetch_and_cache_universe", failing_fetch)
    monkeypatch.setattr(
        "strategy_lab.research_automation.build_todays_observation",
        lambda conn_, ticker, as_of_date=None, config_fingerprint=None: _canned_obs(ticker, today.isoformat()),
    )

    result = run_research_job(conn, today=today, tickers=["AAA"])

    assert result.status != STATUS_FAILED
    assert result.benchmark_refresh_status["primary_fetch_status"] == "failed"
    from strategy_lab.research_automation import load_ticker_errors_for_run
    errors = load_ticker_errors_for_run(conn, result.run_id)
    assert (errors["ticker"] == "SPY").any()


# =================== regime_label_as_of's existing test suite passes unmodified ===================
# (docs/specs/phase17.md §1.3 item 6 - executed automatically by the full
# regression run over tests/test_strategy_lab_regime_asof.py; nothing new to
# add here beyond confirming that file still collects/runs cleanly.)


def test_regime_asof_legacy_suite_module_imports_cleanly():
    import importlib
    mod = importlib.import_module("tests.test_strategy_lab_regime_asof")
    assert hasattr(mod, "regime_label_as_of")
