"""Tests for Phase 14 Component A: strategy_lab/cache_integrity.py -
the research-cache "incomplete latest bar" repair (docs/specs/phase14.md §1).

Everything here is synthetic/deterministic and uses an in-memory SQLite DB.
All Alpaca calls are mocked - no test makes a real network call. No test
ever writes to the real data/research_cache/*.json|.pkl files - frozen
artifact detection is tested against a monkeypatched FROZEN_ARTIFACT_REGISTRY
pointing at tmp_path fixtures instead.
"""
import json
import sqlite3
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from db.schema import init_db
from strategy_lab import cache_integrity
from strategy_lab.cache_integrity import (
    CORRECTIONS_TABLE,
    check_correction_affects_frozen_artifacts,
    latest_cached_row,
    latest_row_is_incomplete,
    refresh_incomplete_latest_bars,
    session_close_utc,
)
from strategy_lab.data import RESEARCH_SOURCE


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _insert_price(conn, ticker, d, close, volume=1_000_000, source=RESEARCH_SOURCE, fetched_at=None):
    if fetched_at is None:
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            (ticker, d, close - 1, close + 1, close - 2, close, volume, source),
        )
    else:
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ticker, d, close - 1, close + 1, close - 2, close, volume, source, fetched_at),
        )
    conn.commit()


def _mock_barset(rows):
    """rows: list of dicts with timestamp/open/high/low/close/volume. Mimics
    the shape strategy_lab.cache_integrity.refresh_incomplete_latest_bars
    expects from client.get_stock_bars(request).df.reset_index()."""
    df = pd.DataFrame(rows)
    return SimpleNamespace(df=df)


# --- session_close_utc: DST / weekend boundaries ---


def test_session_close_utc_january_is_est_21_00_utc():
    # 16:00 America/New_York in January (EST, UTC-5) = 21:00 UTC
    assert session_close_utc(date(2024, 1, 15)) == datetime(2024, 1, 15, 21, 0, 0)


def test_session_close_utc_july_is_edt_20_00_utc():
    # 16:00 America/New_York in July (EDT, UTC-4) = 20:00 UTC
    assert session_close_utc(date(2024, 7, 15)) == datetime(2024, 7, 15, 20, 0, 0)


def test_session_close_utc_dst_boundary_differs_by_exactly_one_hour():
    jan_close = session_close_utc(date(2024, 1, 15))
    jul_close = session_close_utc(date(2024, 7, 15))
    assert (jan_close.hour, jul_close.hour) == (21, 20)


# --- latest_row_is_incomplete: core classification ---


def test_incomplete_bar_before_session_close_is_flagged():
    conn = make_test_db()
    row_date = "2024-01-03"  # Wednesday, real NYSE trading day
    # fetched_at BEFORE that session's 21:00 UTC close (intraday snapshot)
    _insert_price(conn, "AAA", row_date, 100.0, fetched_at="2024-01-03 18:00:00")
    assert latest_row_is_incomplete(conn, "AAA", today=date(2024, 1, 10)) is True


def test_completed_bar_after_session_close_is_not_flagged():
    conn = make_test_db()
    row_date = "2024-01-03"
    # fetched_at AFTER that session's 21:00 UTC close
    _insert_price(conn, "AAA", row_date, 100.0, fetched_at="2024-01-03 22:00:00")
    assert latest_row_is_incomplete(conn, "AAA", today=date(2024, 1, 10)) is False


def test_weekend_row_date_is_never_flagged():
    conn = make_test_db()
    _insert_price(conn, "AAA", "2024-01-06", 100.0, fetched_at="2024-01-01 10:00:00")  # Saturday
    assert latest_row_is_incomplete(conn, "AAA", today=date(2024, 1, 10)) is False


def test_holiday_row_date_is_never_flagged():
    conn = make_test_db()
    _insert_price(conn, "AAA", "2024-01-01", 100.0, fetched_at="2023-12-31 10:00:00")  # New Year's Day
    assert latest_row_is_incomplete(conn, "AAA", today=date(2024, 1, 10)) is False


def test_missing_fetched_at_fails_closed_to_incomplete():
    conn = make_test_db()
    _insert_price(conn, "AAA", "2024-01-03", 100.0)
    # fetched_at is NOT NULL in the schema (defaults to datetime('now')), so
    # "missing" in practice means an empty string - _parse_naive_utc_fetched_at
    # treats both None and "" as missing via its `if not fetched_at` check.
    conn.execute("UPDATE prices SET fetched_at = '' WHERE ticker='AAA'")
    conn.commit()
    assert latest_row_is_incomplete(conn, "AAA", today=date(2024, 1, 10)) is True


def test_malformed_fetched_at_fails_closed_to_incomplete():
    conn = make_test_db()
    _insert_price(conn, "AAA", "2024-01-03", 100.0, fetched_at="not-a-timestamp")
    assert latest_row_is_incomplete(conn, "AAA", today=date(2024, 1, 10)) is True


def test_no_row_at_all_is_not_incomplete():
    conn = make_test_db()
    assert latest_row_is_incomplete(conn, "ZZZZ", today=date(2024, 1, 10)) is False


def test_row_date_in_the_future_relative_to_today_is_not_flagged():
    conn = make_test_db()
    # row_date (2024-01-03) is AFTER today (2020-01-01) - session hasn't
    # happened yet from the caller's deterministic point of view.
    _insert_price(conn, "AAA", "2024-01-03", 100.0)
    assert latest_row_is_incomplete(conn, "AAA", today=date(2020, 1, 1)) is False


# --- refresh_incomplete_latest_bars: core repair behavior ---


def test_refresh_updates_stale_row_and_records_correction_on_changed_value():
    conn = make_test_db()
    row_date = "2024-01-03"
    _insert_price(conn, "AAA", row_date, 100.0, volume=1000, fetched_at="2024-01-03 18:00:00")

    mock_client = MagicMock()
    mock_client.get_stock_bars.return_value = _mock_barset([
        {"timestamp": pd.Timestamp("2024-01-03"), "open": 99.0, "high": 106.0, "low": 98.0,
         "close": 105.0, "volume": 1200},
    ])
    with patch("strategy_lab.cache_integrity.get_data_client", return_value=mock_client):
        result = refresh_incomplete_latest_bars(conn, ["AAA"], today=date(2024, 1, 10))

    assert result["AAA"]["status"] == "refreshed_changed"
    assert result["AAA"]["old_close"] == 100.0
    assert result["AAA"]["new_close"] == 105.0

    row = latest_cached_row(conn, "AAA")
    assert row["close"] == 105.0
    assert row["volume"] == 1200
    # fetched_at must have been refreshed to "now" by _store_adjusted_bars
    assert row["fetched_at"] != "2024-01-03 18:00:00"

    corrections = conn.execute(f"SELECT * FROM {CORRECTIONS_TABLE}").fetchall()
    assert len(corrections) == 1
    assert corrections[0]["old_close"] == 100.0
    assert corrections[0]["new_close"] == 105.0

    # No longer flagged incomplete on a subsequent check (fetched_at now post-close)
    assert latest_row_is_incomplete(conn, "AAA", today=date(2024, 1, 10)) is False


def test_refresh_unchanged_value_upserts_fetched_at_but_writes_no_correction_row():
    conn = make_test_db()
    row_date = "2024-01-03"
    _insert_price(conn, "AAA", row_date, 100.0, volume=1000, fetched_at="2024-01-03 18:00:00")

    mock_client = MagicMock()
    mock_client.get_stock_bars.return_value = _mock_barset([
        {"timestamp": pd.Timestamp("2024-01-03"), "open": 99.0, "high": 101.0, "low": 98.0,
         "close": 100.0, "volume": 1000},
    ])
    with patch("strategy_lab.cache_integrity.get_data_client", return_value=mock_client):
        result = refresh_incomplete_latest_bars(conn, ["AAA"], today=date(2024, 1, 10))

    assert result["AAA"]["status"] == "refreshed_unchanged"
    corrections = conn.execute(f"SELECT * FROM {CORRECTIONS_TABLE}").fetchall()
    assert len(corrections) == 0
    row = latest_cached_row(conn, "AAA")
    assert row["fetched_at"] != "2024-01-03 18:00:00"  # refreshed even though value unchanged


def test_completed_bar_never_triggers_an_alpaca_call():
    """Core mock-call-count assertion: a genuinely-complete latest bar must
    never result in an Alpaca API call."""
    conn = make_test_db()
    _insert_price(conn, "AAA", "2024-01-03", 100.0, fetched_at="2024-01-03 22:00:00")  # post-close

    mock_get_client = MagicMock()
    with patch("strategy_lab.cache_integrity.get_data_client", mock_get_client):
        result = refresh_incomplete_latest_bars(conn, ["AAA"], today=date(2024, 1, 10))

    assert result["AAA"]["status"] == "no_action_needed"
    mock_get_client.assert_not_called()


def test_ticker_with_no_cached_rows_needs_no_action_and_no_call():
    conn = make_test_db()
    mock_get_client = MagicMock()
    with patch("strategy_lab.cache_integrity.get_data_client", mock_get_client):
        result = refresh_incomplete_latest_bars(conn, ["ZZZZ"], today=date(2024, 1, 10))
    assert result["ZZZZ"]["status"] == "no_action_needed"
    mock_get_client.assert_not_called()


def test_alpaca_failure_leaves_row_untouched_and_writes_no_correction():
    conn = make_test_db()
    _insert_price(conn, "AAA", "2024-01-03", 100.0, volume=1000, fetched_at="2024-01-03 18:00:00")

    mock_client = MagicMock()
    mock_client.get_stock_bars.side_effect = RuntimeError("simulated Alpaca outage")
    with patch("strategy_lab.cache_integrity.get_data_client", return_value=mock_client):
        result = refresh_incomplete_latest_bars(conn, ["AAA"], today=date(2024, 1, 10))

    assert result["AAA"]["status"] == "fetch_failed"
    assert "simulated Alpaca outage" in result["AAA"]["detail"]
    row = latest_cached_row(conn, "AAA")
    assert row["close"] == 100.0
    assert row["volume"] == 1000
    assert row["fetched_at"] == "2024-01-03 18:00:00"
    corrections = conn.execute(f"SELECT * FROM {CORRECTIONS_TABLE}").fetchall()
    assert len(corrections) == 0


def test_no_data_returned_for_exact_date_leaves_row_untouched():
    conn = make_test_db()
    _insert_price(conn, "AAA", "2024-01-03", 100.0, volume=1000, fetched_at="2024-01-03 18:00:00")

    mock_client = MagicMock()
    # Bar returned, but for a DIFFERENT date than the flagged row
    mock_client.get_stock_bars.return_value = _mock_barset([
        {"timestamp": pd.Timestamp("2024-01-02"), "open": 99.0, "high": 101.0, "low": 98.0,
         "close": 99.5, "volume": 900},
    ])
    with patch("strategy_lab.cache_integrity.get_data_client", return_value=mock_client):
        result = refresh_incomplete_latest_bars(conn, ["AAA"], today=date(2024, 1, 10))

    assert result["AAA"]["status"] == "no_data_returned"
    row = latest_cached_row(conn, "AAA")
    assert row["close"] == 100.0
    assert row["fetched_at"] == "2024-01-03 18:00:00"


def test_production_source_row_count_unchanged_by_refresh():
    conn = make_test_db()
    _insert_price(conn, "AAA", "2024-01-03", 100.0, volume=1000, fetched_at="2024-01-03 18:00:00")
    _insert_price(conn, "AAA", "2024-01-03", 100.0, volume=1000, source="alpaca", fetched_at="2024-01-03 22:00:00")

    before = conn.execute("SELECT COUNT(*) AS n FROM prices WHERE source='alpaca'").fetchone()["n"]

    mock_client = MagicMock()
    mock_client.get_stock_bars.return_value = _mock_barset([
        {"timestamp": pd.Timestamp("2024-01-03"), "open": 99.0, "high": 106.0, "low": 98.0,
         "close": 105.0, "volume": 1200},
    ])
    with patch("strategy_lab.cache_integrity.get_data_client", return_value=mock_client):
        refresh_incomplete_latest_bars(conn, ["AAA"], today=date(2024, 1, 10))

    after = conn.execute("SELECT COUNT(*) AS n FROM prices WHERE source='alpaca'").fetchone()["n"]
    assert before == after == 1
    # And the alpaca row's own close is untouched (never overwritten)
    alpaca_row = conn.execute("SELECT close, volume FROM prices WHERE source='alpaca'").fetchone()
    assert alpaca_row["close"] == 100.0
    assert alpaca_row["volume"] == 1000
    # No row ever written with source="alpaca" by this call itself
    corrections = conn.execute(f"SELECT source FROM {CORRECTIONS_TABLE}").fetchall()
    assert all(r["source"] == RESEARCH_SOURCE for r in corrections)


def test_only_latest_row_is_ever_touched_historical_rows_untouched():
    conn = make_test_db()
    _insert_price(conn, "AAA", "2024-01-02", 90.0, volume=500, fetched_at="2024-01-02 22:00:00")  # historical
    _insert_price(conn, "AAA", "2024-01-03", 100.0, volume=1000, fetched_at="2024-01-03 18:00:00")  # latest, stale

    mock_client = MagicMock()
    mock_client.get_stock_bars.return_value = _mock_barset([
        {"timestamp": pd.Timestamp("2024-01-03"), "open": 99.0, "high": 106.0, "low": 98.0,
         "close": 105.0, "volume": 1200},
    ])
    with patch("strategy_lab.cache_integrity.get_data_client", return_value=mock_client):
        refresh_incomplete_latest_bars(conn, ["AAA"], today=date(2024, 1, 10))

    historical = conn.execute(
        "SELECT close, volume, fetched_at FROM prices WHERE ticker='AAA' AND date='2024-01-02'"
    ).fetchone()
    assert historical["close"] == 90.0
    assert historical["volume"] == 500
    assert historical["fetched_at"] == "2024-01-02 22:00:00"

    latest = conn.execute(
        "SELECT close, volume FROM prices WHERE ticker='AAA' AND date='2024-01-03'"
    ).fetchone()
    assert latest["close"] == 105.0
    assert latest["volume"] == 1200


# --- Frozen-artifact detection (monkeypatched registry, never touches real files) ---


def test_frozen_artifact_detected_when_corrected_date_within_coverage(tmp_path, monkeypatch):
    fake_baseline = tmp_path / "fake_phase9_baseline.json"
    fake_baseline.write_text(json.dumps({"dataset": {"coverage": {"date_range": ["2021-01-01", "2024-01-05"]}}}))
    original_bytes = fake_baseline.read_bytes()

    fake_registry = [{"name": "phase9_baseline", "kind": "json", "path": fake_baseline,
                       "date_range_keys": ("dataset", "coverage", "date_range")}]
    monkeypatch.setattr(cache_integrity, "FROZEN_ARTIFACT_REGISTRY", fake_registry)

    # corrected_date <= coverage max_date -> IS included
    affected = check_correction_affects_frozen_artifacts("2024-01-03")
    assert affected == ["phase9_baseline"]

    # corrected_date AFTER coverage max_date -> excluded (artifact predates the correction)
    not_affected = check_correction_affects_frozen_artifacts("2024-06-01")
    assert not_affected == []

    assert fake_baseline.read_bytes() == original_bytes  # read-only, never rewritten


def test_frozen_artifact_missing_file_degrades_to_no_finding(tmp_path, monkeypatch):
    missing_path = tmp_path / "does_not_exist.json"
    fake_registry = [{"name": "phase9_baseline", "kind": "json", "path": missing_path,
                       "date_range_keys": ("dataset", "coverage", "date_range")}]
    monkeypatch.setattr(cache_integrity, "FROZEN_ARTIFACT_REGISTRY", fake_registry)
    assert check_correction_affects_frozen_artifacts("2024-01-03") == []


def test_frozen_artifact_corrupt_file_never_raises(tmp_path, monkeypatch):
    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("{not valid json")
    fake_registry = [{"name": "phase9_baseline", "kind": "json", "path": corrupt_path,
                       "date_range_keys": ("dataset", "coverage", "date_range")}]
    monkeypatch.setattr(cache_integrity, "FROZEN_ARTIFACT_REGISTRY", fake_registry)
    assert check_correction_affects_frozen_artifacts("2024-01-03") == []


def test_refresh_records_frozen_artifact_names_when_flagged(tmp_path, monkeypatch):
    fake_baseline = tmp_path / "fake_phase9_baseline.json"
    fake_baseline.write_text(json.dumps({"dataset": {"coverage": {"date_range": ["2021-01-01", "2024-06-01"]}}}))
    fake_registry = [{"name": "phase9_baseline", "kind": "json", "path": fake_baseline,
                       "date_range_keys": ("dataset", "coverage", "date_range")}]
    monkeypatch.setattr(cache_integrity, "FROZEN_ARTIFACT_REGISTRY", fake_registry)

    conn = make_test_db()
    _insert_price(conn, "AAA", "2024-01-03", 100.0, volume=1000, fetched_at="2024-01-03 18:00:00")
    mock_client = MagicMock()
    mock_client.get_stock_bars.return_value = _mock_barset([
        {"timestamp": pd.Timestamp("2024-01-03"), "open": 99.0, "high": 106.0, "low": 98.0,
         "close": 105.0, "volume": 1200},
    ])
    with patch("strategy_lab.cache_integrity.get_data_client", return_value=mock_client):
        result = refresh_incomplete_latest_bars(conn, ["AAA"], today=date(2024, 1, 10))

    assert result["AAA"]["affects_frozen_artifacts"] == ["phase9_baseline"]
    correction = conn.execute(f"SELECT affects_frozen_artifacts FROM {CORRECTIONS_TABLE}").fetchone()
    assert json.loads(correction["affects_frozen_artifacts"]) == ["phase9_baseline"]


# --- Structural safety: never imports trading/alerts ---


def test_cache_integrity_never_imports_trading_or_alerts():
    import ast
    import strategy_lab.cache_integrity as mod
    with open(mod.__file__) as f:
        tree = ast.parse(f.read(), filename=mod.__file__)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    forbidden = {n for n in names if n == "trading" or n.startswith("trading.") or n == "alerts" or n.startswith("alerts.")}
    assert not forbidden, f"cache_integrity.py must never import trading/alerts: {forbidden}"
