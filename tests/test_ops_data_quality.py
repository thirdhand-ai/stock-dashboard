"""Tests for Phase 12 Component B: ops/data_quality.py.

Synthetic/in-memory SQLite only, no network, no real ingestion call. Covers
the fresh/stale/missing/invalid/gap matrix from spec docs/specs/phase12.md
§10.2, plus the "never calls ingestion" / "never writes prices" safety
checks that belong alongside the matrix (kept in this file rather than
tests/test_ops_safety.py since they're specific to this one component's
behavior, mirroring how tests/test_strategy_lab.py keeps some structural
checks next to the feature they guard).
"""
import ast
import os
import sqlite3
from datetime import date, timedelta

from automation.trading_calendar import trading_sessions_between
from db.schema import init_db
from ops.data_quality import (
    OVERALL_DEGRADED,
    OVERALL_FAILED,
    OVERALL_HEALTHY,
    OVERALL_STALE,
    STATUS_FRESH,
    STATUS_MISSING,
    STATUS_STALE,
    TickerQualityRow,
    check_ticker_quality,
    check_watchlist_quality,
    classify_overall,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A Saturday - never itself a NYSE trading session. Using a non-trading day
# as "today" throughout exercises spec §4.4's weekend edge case (freshness
# must resolve off the prior Friday, not falsely flag it stale) in every
# test here, not just a dedicated one.
ANCHOR_SATURDAY = date(2024, 6, 22)


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_price_row(conn, ticker, session_date, close=100.0, open_=100.0, high=101.0,
                      low=99.0, volume=1_000_000, source="alpaca"):
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, session_date.isoformat(), open_, high, low, close, volume, source),
    )
    conn.commit()


def recent_sessions(anchor, n):
    sessions = trading_sessions_between(anchor - timedelta(days=220), anchor)
    return sessions[-n:]


# --- fresh / stale / missing ---

def test_fresh_ticker_classified_fresh():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 5)
    for s in sessions:
        insert_price_row(conn, "AAA", s)
    row = check_ticker_quality(conn, "AAA", today=ANCHOR_SATURDAY)
    assert row.status == STATUS_FRESH
    assert row.last_date == sessions[-1].isoformat()


def test_ticker_with_no_rows_classified_missing():
    conn = make_test_db()
    row = check_ticker_quality(conn, "GHOST", today=ANCHOR_SATURDAY)
    assert row.status == STATUS_MISSING
    assert row.row_count_total == 0
    assert row.reason is not None


def test_ticker_last_row_older_than_most_recent_session_classified_stale():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 10)
    for s in sessions[:-3]:  # deliberately omit the 3 most recent sessions
        insert_price_row(conn, "AAA", s)
    row = check_ticker_quality(conn, "AAA", today=ANCHOR_SATURDAY)
    assert row.status == STATUS_STALE
    assert row.reason is not None


# --- invalid OHLCV ---

def test_invalid_ohlcv_high_less_than_low_counted_not_removed():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 5)
    insert_price_row(conn, "AAA", sessions[-1])  # keeps ticker FRESH
    insert_price_row(conn, "AAA", sessions[-2], high=10.0, low=20.0)  # high < low
    before = conn.execute("SELECT COUNT(*) as n FROM prices WHERE ticker='AAA'").fetchone()["n"]
    row = check_ticker_quality(conn, "AAA", today=ANCHOR_SATURDAY)
    after = conn.execute("SELECT COUNT(*) as n FROM prices WHERE ticker='AAA'").fetchone()["n"]
    assert row.invalid_ohlcv_rows == 1
    assert row.status == STATUS_FRESH  # invalid data doesn't change freshness classification
    assert before == after == 2  # counted, never auto-repaired/removed


def test_invalid_ohlcv_negative_volume_counted():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 5)
    insert_price_row(conn, "AAA", sessions[-1])
    insert_price_row(conn, "AAA", sessions[-2], volume=-100)
    row = check_ticker_quality(conn, "AAA", today=ANCHOR_SATURDAY)
    assert row.invalid_ohlcv_rows == 1


def test_cross_source_close_divergence_counted_as_duplicate_conflict():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 5)
    insert_price_row(conn, "AAA", sessions[-1], close=100.0, source="yfinance")
    insert_price_row(conn, "AAA", sessions[-1], close=110.0, source="alpaca")  # >1% divergence
    row = check_ticker_quality(conn, "AAA", today=ANCHOR_SATURDAY)
    assert row.duplicate_rows == 1


def test_cross_source_minor_divergence_not_counted():
    """Sanity companion (not in the checklist, but guards against an
    over-eager implementation): Alpaca/yfinance adjustment differences under
    1% must NOT be flagged - only material ones."""
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 5)
    insert_price_row(conn, "AAA", sessions[-1], close=100.0, source="yfinance")
    insert_price_row(conn, "AAA", sessions[-1], close=100.30, source="alpaca")  # 0.3% divergence
    row = check_ticker_quality(conn, "AAA", today=ANCHOR_SATURDAY)
    assert row.duplicate_rows == 0


# --- gap detection ---

def test_gap_detection_finds_missing_session_in_window():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 65)
    skipped_date = sessions[-10]  # inside the 60-session window and the 10-item display cap
    for s in sessions:
        if s == skipped_date:
            continue
        insert_price_row(conn, "AAA", s)
    row = check_ticker_quality(conn, "AAA", today=ANCHOR_SATURDAY)
    assert row.status == STATUS_FRESH  # most recent session still present
    assert skipped_date.isoformat() in row.missing_trading_days
    assert row.missing_trading_days_count >= 1


# --- overall rollup (classify_overall is a pure function - test directly) ---

def _row(ticker, status, invalid=0, dup=0, missing=None):
    missing = missing or []
    return TickerQualityRow(
        ticker=ticker, source="alpaca", status=status, last_date="2024-06-20",
        fetched_at="2024-06-20T00:00:00", row_count_total=10, row_count_last_90d=10,
        duplicate_rows=dup, invalid_ohlcv_rows=invalid, missing_trading_days=missing,
        missing_trading_days_count=len(missing),
    )


def test_overall_healthy_all_fresh_zero_findings():
    rows = [_row("AAA", STATUS_FRESH), _row("BBB", STATUS_FRESH)]
    assert classify_overall(rows) == OVERALL_HEALTHY


def test_overall_degraded_fresh_but_invalid_rows_present():
    rows = [_row("AAA", STATUS_FRESH, invalid=1), _row("BBB", STATUS_FRESH)]
    assert classify_overall(rows) == OVERALL_DEGRADED


def test_overall_stale_one_ticker_stale():
    rows = [_row("AAA", STATUS_FRESH), _row("BBB", STATUS_STALE)]
    assert classify_overall(rows) == OVERALL_STALE


def test_overall_failed_one_ticker_missing():
    rows = [_row("AAA", STATUS_FRESH), _row("BBB", STATUS_MISSING), _row("CCC", STATUS_STALE)]
    assert classify_overall(rows) == OVERALL_FAILED


# --- safety: never touches ingestion or mutates prices ---

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


def test_check_watchlist_quality_never_calls_ingestion():
    path = os.path.join(REPO_ROOT, "ops", "data_quality.py")
    imports = _module_level_import_names(path)
    forbidden = {
        i for i in imports
        if i.startswith("ingestion.alpaca_source") or i.startswith("ingestion.yfinance_source")
        or i in ("ingestion.alpaca_source", "ingestion.yfinance_source")
    }
    assert not forbidden, f"ops/data_quality.py must never import ingestion modules: {forbidden}"


def test_check_watchlist_quality_never_writes_prices_table():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 5)
    for s in sessions:
        insert_price_row(conn, "AAPL", s)
    before = conn.execute("SELECT COUNT(*) as n FROM prices").fetchone()["n"]
    check_watchlist_quality(conn, tickers=["AAPL", "MSFT", "GHOST"], today=ANCHOR_SATURDAY)
    after = conn.execute("SELECT COUNT(*) as n FROM prices").fetchone()["n"]
    assert before == after
