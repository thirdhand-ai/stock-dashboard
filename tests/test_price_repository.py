"""Tests for db/price_repository.py's resolve_source() source-selection logic.

Covers the MIN_TRUST_RATIO floor added to fix the SOURCE_PRIORITY
collision: a SOURCE_PRIORITY source (yfinance/alpaca) must only shadow a
larger non-priority source (e.g. alpaca_adjusted) when its row count is
reasonably close to the best available count - not whenever it merely
exists, which is what let a handful of stray `alpaca` rows outrank NOW's
much larger `alpaca_adjusted` series.
"""
import sqlite3

from db.price_repository import MIN_TRUST_RATIO, SOURCE_PRIORITY, resolve_source
from db.schema import init_db


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_price_rows(conn, ticker, source, count, start_day=1):
    """Insert `count` distinct-dated rows for (ticker, source)."""
    for i in range(count):
        day = start_day + i
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ticker, f"2024-01-{day:02d}", 100.0, 101.0, 99.0, 100.0, 1_000_000, source),
        )
    conn.commit()


def test_single_source_still_resolves_with_no_other_data():
    """Baseline: only one source exists, so it's trivially both the
    priority match and the largest source - the ratio floor never
    excludes it (best_count == its own count, ratio == 1.0)."""
    conn = make_test_db()
    insert_price_rows(conn, "AAA", "alpaca", count=5)

    assert resolve_source(conn, "AAA") == "alpaca"


def test_thin_but_growing_priority_source_is_still_preferred():
    """A newly onboarded, legitimately thin production series: `alpaca`
    has fewer rows than `alpaca_adjusted` but is well within 2x (ratio
    0.8 >= MIN_TRUST_RATIO), the normal case when both sources are
    ingested on the same day-to-day cadence. SOURCE_PRIORITY should still
    win here - this must not regress just because we added a floor."""
    conn = make_test_db()
    insert_price_rows(conn, "ZZZ", "alpaca_adjusted", count=10)
    insert_price_rows(conn, "ZZZ", "alpaca", count=8)

    assert resolve_source(conn, "ZZZ") == "alpaca"


def test_now_style_collision_falls_through_to_largest_source():
    """The actual NOW bug: a handful of stray `alpaca` rows (e.g. from a
    manual/test ingestion) against a much larger, legitimately-backfilled
    `alpaca_adjusted` series. 3/1254 is far below MIN_TRUST_RATIO, so
    resolve_source must fall through to alpaca_adjusted instead of
    letting the thin priority source shadow it."""
    conn = make_test_db()
    insert_price_rows(conn, "NOW", "alpaca_adjusted", count=1254)
    insert_price_rows(conn, "NOW", "alpaca", count=3, start_day=1)

    assert resolve_source(conn, "NOW") == "alpaca_adjusted"


def test_priority_source_exactly_at_ratio_floor_is_trusted():
    """Boundary case: priority source count is exactly MIN_TRUST_RATIO of
    the best count - should still be trusted (the check is >=, not >)."""
    conn = make_test_db()
    insert_price_rows(conn, "BBB", "alpaca_adjusted", count=10)
    insert_price_rows(conn, "BBB", "alpaca", count=int(10 * MIN_TRUST_RATIO))

    assert resolve_source(conn, "BBB") == "alpaca"


def test_first_priority_source_beats_second_even_when_second_is_bigger():
    """SOURCE_PRIORITY order (yfinance before alpaca) is still respected
    when both priority sources individually clear the ratio floor."""
    conn = make_test_db()
    assert SOURCE_PRIORITY[0] == "yfinance"
    insert_price_rows(conn, "CCC", "alpaca", count=20)
    insert_price_rows(conn, "CCC", "yfinance", count=15)

    assert resolve_source(conn, "CCC") == "yfinance"


def test_no_price_data_returns_none():
    conn = make_test_db()
    assert resolve_source(conn, "NOPE") is None
