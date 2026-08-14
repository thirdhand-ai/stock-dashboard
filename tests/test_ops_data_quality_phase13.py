"""Tests for Phase 13 Component B: the additive extension of ops/data_quality.py
(provenance ledger + new TickerQualityRow/DataQualityReport fields +
provenance-aware rollup). tests/test_ops_data_quality.py (Phase 12) is
untouched by this phase and must keep passing byte-for-byte unmodified.
"""
import sqlite3
from datetime import date, timedelta

from automation.trading_calendar import trading_sessions_between
from db.schema import init_db
from ops.data_quality import (
    OVERALL_DEGRADED,
    OVERALL_FAILED,
    OVERALL_HEALTHY,
    STATUS_FRESH,
    TickerQualityRow,
    check_ticker_quality,
    check_watchlist_quality,
    classify_overall_provenance_aware,
    ensure_provenance_schema,
    load_provenance_history,
    record_source_provenance,
)
from ops.provider_reconciliation import CLASS_EXPECTED_ADJUSTMENT_DIFFERENCE

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


def _row(ticker, status=STATUS_FRESH, invalid=0, unexplained=0, stale_source=0,
         authoritative_stale=False, missing=None):
    missing = missing or []
    return TickerQualityRow(
        ticker=ticker, source="alpaca", status=status, last_date="2024-06-20",
        fetched_at="2024-06-20T00:00:00", row_count_total=10, row_count_last_90d=10,
        duplicate_rows=0, invalid_ohlcv_rows=invalid, missing_trading_days=missing,
        missing_trading_days_count=len(missing),
        unexplained_divergence_count=unexplained, stale_source_count=stale_source,
        authoritative_source_stale=authoritative_stale,
    )


# --- existing Phase 12 fields/behavior, exercised through the new code path ---


def test_existing_phase12_fields_and_behavior_unchanged():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 5)
    for s in sessions:
        insert_price_row(conn, "AAA", s)
    row = check_ticker_quality(conn, "AAA", today=ANCHOR_SATURDAY)
    assert row.status == STATUS_FRESH
    assert row.last_date == sessions[-1].isoformat()
    assert row.row_count_total == 5
    assert row.duplicate_rows == 0
    assert row.invalid_ohlcv_rows == 0


def test_new_fields_default_safely_on_direct_construction():
    row = TickerQualityRow(
        ticker="AAA", source="alpaca", status=STATUS_FRESH, last_date="2024-06-20",
        fetched_at="2024-06-20T00:00:00", row_count_total=10, row_count_last_90d=10,
        duplicate_rows=0, invalid_ohlcv_rows=0,
    )
    assert row.provider_findings == []
    assert row.unexplained_divergence_count == 0
    assert row.stale_source_count == 0
    assert row.authoritative_source_stale is False
    assert row.non_authoritative_source_notes == []


# --- provenance-aware rollup ---


def test_provenance_aware_rollup_ignores_expected_adjustment_difference():
    rows = [
        _row("AAA", status=STATUS_FRESH, unexplained=0, stale_source=0, authoritative_stale=False),
    ]
    rows[0].provider_findings = [{"classification": CLASS_EXPECTED_ADJUSTMENT_DIFFERENCE}]
    assert classify_overall_provenance_aware(rows) == OVERALL_HEALTHY


def test_provenance_aware_rollup_degraded_by_stale_source_finding():
    rows = [_row("AAA", status=STATUS_FRESH, stale_source=1)]
    assert classify_overall_provenance_aware(rows) == OVERALL_DEGRADED


def test_provenance_aware_rollup_failed_when_authoritative_source_stale():
    rows = [_row("AAA", status=STATUS_FRESH, authoritative_stale=True)]
    assert classify_overall_provenance_aware(rows) == OVERALL_FAILED


def test_provenance_aware_rollup_not_degraded_by_stale_non_authoritative_freshness_alone():
    """A non-authoritative source being stale (freshness-wise) alone, with
    no STALE_SOURCE finding and no unexplained divergence recorded on the
    TickerQualityRow, must not degrade the rollup - only the DERIVED
    counts (stale_source_count/unexplained_divergence_count/
    authoritative_source_stale) feed classify_overall_provenance_aware."""
    rows = [_row("AAA", status=STATUS_FRESH, stale_source=0, unexplained=0, authoritative_stale=False)]
    assert classify_overall_provenance_aware(rows) == OVERALL_HEALTHY


# --- provenance ledger ---


def test_record_source_provenance_inserts_one_row_per_ticker_source_pair():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 5)
    for s in sessions:
        insert_price_row(conn, "AAA", s, source="alpaca")
        insert_price_row(conn, "AAA", s, source="alpaca_adjusted")
    for s in sessions:
        insert_price_row(conn, "BBB", s, source="alpaca")

    ensure_provenance_schema(conn)
    inserted = record_source_provenance(conn, check_id=1, tickers=["AAA", "BBB"])
    assert inserted == 3

    history_aaa_alpaca = load_provenance_history(conn, "AAA", "alpaca")
    history_aaa_adj = load_provenance_history(conn, "AAA", "alpaca_adjusted")
    history_bbb = load_provenance_history(conn, "BBB", "alpaca")
    assert len(history_aaa_alpaca) == 1
    assert len(history_aaa_adj) == 1
    assert len(history_bbb) == 1
    assert history_aaa_alpaca.iloc[0]["row_count"] == 5


def test_record_source_provenance_fingerprint_changes_when_underlying_rows_change():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 5)
    for s in sessions:
        insert_price_row(conn, "AAA", s, close=100.0, source="alpaca")

    ensure_provenance_schema(conn)
    record_source_provenance(conn, check_id=1, tickers=["AAA"])
    fp1 = load_provenance_history(conn, "AAA", "alpaca").iloc[0]["fingerprint"]

    conn.execute("UPDATE prices SET close = 999.0 WHERE ticker='AAA' AND source='alpaca' AND date=?",
                 (sessions[0].isoformat(),))
    conn.commit()
    record_source_provenance(conn, check_id=2, tickers=["AAA"])
    fp2 = load_provenance_history(conn, "AAA", "alpaca", limit=1).iloc[0]["fingerprint"]

    assert fp1 != fp2


def test_record_source_provenance_never_writes_prices_table():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 5)
    for s in sessions:
        insert_price_row(conn, "AAA", s, source="alpaca")
    before = conn.execute("SELECT COUNT(*) as n FROM prices").fetchone()["n"]
    ensure_provenance_schema(conn)
    record_source_provenance(conn, check_id=1, tickers=["AAA"])
    after = conn.execute("SELECT COUNT(*) as n FROM prices").fetchone()["n"]
    assert before == after


def test_check_watchlist_quality_still_never_writes_prices_table_with_new_fields():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 5)
    for s in sessions:
        insert_price_row(conn, "AAPL", s)
    before = conn.execute("SELECT COUNT(*) as n FROM prices").fetchone()["n"]
    report = check_watchlist_quality(conn, tickers=["AAPL", "MSFT", "GHOST"], today=ANCHOR_SATURDAY)
    after = conn.execute("SELECT COUNT(*) as n FROM prices").fetchone()["n"]
    assert before == after
    assert hasattr(report, "overall_status_provenance_aware")
    for row in report.tickers:
        assert hasattr(row, "provider_findings")
        assert hasattr(row, "unexplained_divergence_count")
