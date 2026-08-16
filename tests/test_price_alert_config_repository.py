"""Tests for db/price_alert_config_repository.py - the CRUD interface for
the price_alert_config table (dashboard/views/price_alert_config.py's
add/edit/remove UI), plus integration coverage proving alerts/price_engine.py
and alerts/price_runner.py now default to this DB-backed source instead of
the old hardcoded PRICE_THRESHOLDS constant.
"""
import sqlite3
from unittest.mock import patch

import pytest

from alerts.price_config import MODE_FIXED, MODE_PERCENT, PriceThreshold
from alerts.price_engine import evaluate_price_thresholds
from alerts.price_runner import run_price_alert_cycle
from db.price_alert_config_repository import (
    delete_price_alert_config,
    get_price_alert_config,
    list_price_alert_configs,
    upsert_price_alert_config,
)
from db.price_alerts_schema import ensure_price_alerts_schema
from db.schema import init_db
from indicators.technical import IndicatorResult


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


# --- list_price_alert_configs / get_price_alert_config ---


def test_list_price_alert_configs_empty_by_default():
    conn = make_test_db()
    assert list_price_alert_configs(conn) == []


def test_get_price_alert_config_returns_none_for_unknown_ticker():
    conn = make_test_db()
    assert get_price_alert_config(conn, "GHOST") is None


def test_list_price_alert_configs_returns_ticker_ascending():
    conn = make_test_db()
    upsert_price_alert_config(conn, "TSLA", above=400.0, below=300.0)
    upsert_price_alert_config(conn, "AAPL", above=320.0, below=290.0)
    upsert_price_alert_config(conn, "MSFT", above=525.0, below=465.0)

    tickers = [t.ticker for t in list_price_alert_configs(conn)]
    assert tickers == ["AAPL", "MSFT", "TSLA"]


# --- upsert_price_alert_config: add + edit are the same operation ---


def test_upsert_adds_a_new_ticker():
    conn = make_test_db()
    upsert_price_alert_config(conn, "AAPL", above=320.0, below=290.0)

    config = get_price_alert_config(conn, "AAPL")
    assert config == PriceThreshold(ticker="AAPL", above=320.0, below=290.0)


def test_upsert_overwrites_an_existing_tickers_thresholds():
    conn = make_test_db()
    upsert_price_alert_config(conn, "AAPL", above=320.0, below=290.0)

    upsert_price_alert_config(conn, "AAPL", above=350.0, below=300.0)  # edit

    config = get_price_alert_config(conn, "AAPL")
    assert config == PriceThreshold(ticker="AAPL", above=350.0, below=300.0)
    assert len(list_price_alert_configs(conn)) == 1  # still one row, not a duplicate


def test_upsert_allows_only_above_or_only_below():
    conn = make_test_db()
    upsert_price_alert_config(conn, "AAPL", above=320.0, below=None)
    upsert_price_alert_config(conn, "MSFT", above=None, below=465.0)

    assert get_price_alert_config(conn, "AAPL") == PriceThreshold(ticker="AAPL", above=320.0, below=None)
    assert get_price_alert_config(conn, "MSFT") == PriceThreshold(ticker="MSFT", above=None, below=465.0)


# --- delete_price_alert_config ---


def test_delete_removes_a_configured_ticker():
    conn = make_test_db()
    upsert_price_alert_config(conn, "AAPL", above=320.0, below=290.0)

    delete_price_alert_config(conn, "AAPL")

    assert get_price_alert_config(conn, "AAPL") is None
    assert list_price_alert_configs(conn) == []


def test_delete_on_unconfigured_ticker_is_a_noop():
    conn = make_test_db()
    upsert_price_alert_config(conn, "AAPL", above=320.0, below=290.0)

    delete_price_alert_config(conn, "GHOST")  # never configured - must not raise or affect AAPL

    assert len(list_price_alert_configs(conn)) == 1
    assert get_price_alert_config(conn, "AAPL") is not None


def test_delete_only_affects_the_named_ticker():
    conn = make_test_db()
    upsert_price_alert_config(conn, "AAPL", above=320.0, below=290.0)
    upsert_price_alert_config(conn, "MSFT", above=525.0, below=465.0)

    delete_price_alert_config(conn, "AAPL")

    assert get_price_alert_config(conn, "AAPL") is None
    assert get_price_alert_config(conn, "MSFT") is not None


# --- percent mode: upsert resolves percent+baseline_price to above/below ---


def test_upsert_percent_mode_resolves_above_below_from_baseline_and_percent():
    conn = make_test_db()
    upsert_price_alert_config(conn, "AAPL", mode=MODE_PERCENT, percent=6.0, baseline_price=150.0)

    config = get_price_alert_config(conn, "AAPL")
    assert config.mode == MODE_PERCENT
    assert config.percent == pytest.approx(6.0)
    assert config.baseline_price == pytest.approx(150.0)
    assert config.above == pytest.approx(159.0)
    assert config.below == pytest.approx(141.0)


def test_upsert_percent_mode_requires_percent_and_baseline_price():
    conn = make_test_db()
    with pytest.raises(ValueError):
        upsert_price_alert_config(conn, "AAPL", mode=MODE_PERCENT, percent=6.0, baseline_price=None)
    with pytest.raises(ValueError):
        upsert_price_alert_config(conn, "AAPL", mode=MODE_PERCENT, percent=None, baseline_price=150.0)


def test_upsert_rejects_unknown_mode():
    conn = make_test_db()
    with pytest.raises(ValueError):
        upsert_price_alert_config(conn, "AAPL", mode="bogus", above=100.0)


def test_upsert_switching_a_ticker_from_percent_to_fixed_clears_percent_fields():
    conn = make_test_db()
    upsert_price_alert_config(conn, "AAPL", mode=MODE_PERCENT, percent=6.0, baseline_price=150.0)

    upsert_price_alert_config(conn, "AAPL", mode=MODE_FIXED, above=320.0, below=290.0)

    config = get_price_alert_config(conn, "AAPL")
    assert config == PriceThreshold(ticker="AAPL", above=320.0, below=290.0, mode=MODE_FIXED, percent=None, baseline_price=None)


def test_list_price_alert_configs_mixes_fixed_and_percent_mode_tickers():
    conn = make_test_db()
    upsert_price_alert_config(conn, "AAPL", above=320.0, below=290.0)  # fixed, default mode
    upsert_price_alert_config(conn, "MSFT", mode=MODE_PERCENT, percent=6.0, baseline_price=500.0)

    configs = {c.ticker: c for c in list_price_alert_configs(conn)}
    assert configs["AAPL"].mode == MODE_FIXED
    assert configs["MSFT"].mode == MODE_PERCENT
    assert configs["MSFT"].above == pytest.approx(530.0)
    assert configs["MSFT"].below == pytest.approx(470.0)


# --- backward compatibility: existing fixed-dollar rows / pre-migration schema ---


def test_existing_fixed_mode_upsert_call_unaffected_by_new_mode_kwarg():
    """The exact call shape every pre-existing caller (dashboard/data.py,
    older tests) already uses - mode/percent/baseline_price all default,
    producing byte-identical behavior to before this feature existed."""
    conn = make_test_db()
    upsert_price_alert_config(conn, "AAPL", above=320.0, below=290.0)

    config = get_price_alert_config(conn, "AAPL")
    assert config == PriceThreshold(ticker="AAPL", above=320.0, below=290.0)
    assert config.mode == MODE_FIXED
    assert config.percent is None
    assert config.baseline_price is None


def test_migration_adds_percent_columns_to_pre_existing_table_without_touching_existing_rows():
    """Simulates the real production DB: a price_alert_config table created
    before percent mode existed (old 3-column shape: ticker, above, below),
    with rows already in it. ensure_price_alerts_schema's additive migration
    must add mode/percent/baseline_price without altering the pre-existing
    above/below values - this is exactly what happens to the 8 real
    fixed-dollar thresholds already in data/stock_dashboard.db."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.execute("CREATE TABLE price_alert_config (ticker TEXT PRIMARY KEY, above REAL, below REAL)")
    conn.execute("INSERT INTO price_alert_config (ticker, above, below) VALUES ('AAPL', 320.0, 290.0)")
    conn.execute("INSERT INTO price_alert_config (ticker, above, below) VALUES ('MSFT', 525.0, 465.0)")
    conn.commit()

    ensure_price_alerts_schema(conn)  # the migration under test

    aapl = get_price_alert_config(conn, "AAPL")
    assert aapl == PriceThreshold(ticker="AAPL", above=320.0, below=290.0, mode=MODE_FIXED, percent=None, baseline_price=None)
    msft = get_price_alert_config(conn, "MSFT")
    assert msft == PriceThreshold(ticker="MSFT", above=525.0, below=465.0, mode=MODE_FIXED, percent=None, baseline_price=None)
    assert len(list_price_alert_configs(conn)) == 2


# --- integration: alerts/price_engine.py + alerts/price_runner.py default
# to the DB-backed table, not a hardcoded list ---


def insert_placeholder_price_row(conn, ticker, source="yfinance"):
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, "2024-06-03", 149.0, 151.0, 148.0, 150.0, 2_000_000, source),
    )
    conn.commit()


def test_evaluate_price_thresholds_defaults_to_db_backed_config():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "AAPL")
    upsert_price_alert_config(conn, "AAPL", above=200.0, below=None)

    with patch(
        "alerts.price_engine.compute_indicators_for_ticker",
        return_value=IndicatorResult(ticker="AAPL", ok=True, latest_date="2024-06-03", close=210.0),
    ):
        evaluations = evaluate_price_thresholds(conn)  # thresholds=None -> DB default

    assert len(evaluations) == 1
    assert evaluations[0].ticker == "AAPL"
    assert evaluations[0].threshold == PriceThreshold(ticker="AAPL", above=200.0, below=None)


def test_evaluate_price_thresholds_skips_a_ticker_removed_from_config():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "AAPL")
    upsert_price_alert_config(conn, "AAPL", above=200.0, below=None)
    delete_price_alert_config(conn, "AAPL")

    evaluations = evaluate_price_thresholds(conn)  # thresholds=None -> DB default

    assert evaluations == []


def test_run_price_alert_cycle_defaults_to_db_backed_config():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "AAPL")
    upsert_price_alert_config(conn, "AAPL", above=200.0, below=None)

    with patch(
        "alerts.price_engine.compute_indicators_for_ticker",
        return_value=IndicatorResult(ticker="AAPL", ok=True, latest_date="2024-06-03", close=210.0),
    ):
        results = run_price_alert_cycle(conn)  # thresholds=None -> DB default, send=False (dry-run)

    assert len(results) == 1
    assert results[0].evaluation.ticker == "AAPL"
    assert results[0].evaluation.previous_price is None  # first observation - baseline only
