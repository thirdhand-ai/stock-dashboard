"""Tests for Phase 13 Component A: ops/provider_reconciliation.py.

Synthetic/in-memory SQLite only, no network. Covers the classification
matrix from docs/specs/phase13.md §3.1/§8.1, including the exact confirmed
real-data patterns from §0.1 (AAPL/MSFT/NVDA divergence investigation).
"""
import ast
import os
import sqlite3
from datetime import date, timedelta

from automation.trading_calendar import trading_sessions_between
from db.schema import init_db
from ops.provider_reconciliation import (
    CLASS_EXPECTED_ADJUSTMENT_DIFFERENCE,
    CLASS_MATCHED,
    CLASS_SESSION_ALIGNMENT_DIFFERENCE,
    CLASS_STALE_SOURCE,
    CLASS_UNEXPLAINED_DIVERGENCE,
    build_ticker_summary,
    check_all_sources_freshness,
    classify_pair,
    find_divergences_for_ticker,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ANCHOR_SATURDAY = date(2024, 6, 22)  # never itself a NYSE session


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_price_row(conn, ticker, session_date, close=100.0, open_=100.0, high=101.0,
                      low=99.0, volume=1_000_000, source="alpaca", fetched_at=None):
    if fetched_at is not None:
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ticker, session_date.isoformat(), open_, high, low, close, volume, source, fetched_at),
        )
    else:
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            (ticker, session_date.isoformat(), open_, high, low, close, volume, source),
        )
    conn.commit()


def recent_sessions(anchor, n):
    sessions = trading_sessions_between(anchor - timedelta(days=220), anchor)
    return sessions[-n:]


# --- classify_pair: check order matrix ---


def test_matched_within_tolerance():
    finding = classify_pair(
        "AAA", "2026-08-07",
        "alpaca", 100.10, 1_000_000, "2026-08-08 20:00:00",
        "alpaca_adjusted", 100.00, 1_000_000, "2026-08-08 19:00:00",
    )
    assert finding.classification == CLASS_MATCHED


def test_matched_when_real_dividend_delta_is_within_tolerance():
    """The real, confirmed AAPL 2026-08-07 row (313.33 vs 313.06, a 0.086%
    delta) falls UNDER CLOSE_MATCH_TOLERANCE_PCT (0.15%), so it correctly
    classifies MATCHED - not EXPECTED_ADJUSTMENT_DIFFERENCE. MATCHED already
    means "no material divergence, volume-consistent," which is the correct
    signal for a delta this small (spec §0.1(a) implementation-time
    correction)."""
    finding = classify_pair(
        "AAPL", "2026-08-07",
        "alpaca", 313.33, 41_000_000, "2026-08-08 20:30:07",
        "alpaca_adjusted", 313.06, 41_000_000, "2026-08-08 19:00:00",
    )
    assert finding.classification == CLASS_MATCHED


def test_expected_adjustment_difference_dividend_only_price_diff_volume_identical():
    """Synthetic delta EXCEEDING CLOSE_MATCH_TOLERANCE_PCT, same volume,
    differing adjustment-mode tags - exercises the adjustment-mode-mismatch
    branch (check #3) rather than a real captured row, since no real
    dividend-only delta in this DB happens to exceed the match tolerance
    (spec §0.1(a) implementation-time correction)."""
    finding = classify_pair(
        "AAPL", "2026-08-07",
        "alpaca", 313.33, 41_000_000, "2026-08-08 20:30:07",
        "alpaca_adjusted", 312.00, 41_000_000, "2026-08-08 19:00:00",
    )
    assert finding.classification == CLASS_EXPECTED_ADJUSTMENT_DIFFERENCE
    assert "alpaca" in finding.detail and "adjustment" in finding.detail.lower()


def test_expected_adjustment_difference_split_price_and_volume_both_scale_by_same_ratio():
    """Reproduces confirmed pattern (b): NVDA-shaped, ratio ~10 on both price and volume."""
    finding = classify_pair(
        "NVDA", "2024-06-07",
        "alpaca", 1208.88, 412_385_800, "2026-08-13 20:30:07",
        "alpaca_adjusted", 120.68, 41_238_580, "2026-08-12 18:54:58",
    )
    assert finding.classification == CLASS_EXPECTED_ADJUSTMENT_DIFFERENCE
    assert "split" in finding.detail.lower() or "10" in finding.detail


def test_stale_source_when_price_close_but_volume_diverges_and_fetched_intraday():
    """Reproduces confirmed pattern (c): small price ratio, large unexplained
    volume ratio, fetched_at same-day pre-close on the source's latest row -
    must classify STALE_SOURCE, NOT EXPECTED_ADJUSTMENT_DIFFERENCE."""
    finding = classify_pair(
        "AAPL", "2026-08-12",
        "alpaca", 302.25, 41_873_071, "2026-08-13 20:30:07",
        "alpaca_adjusted", 301.52, 25_652_286, "2026-08-12 18:54:58",
        is_latest_date_for_source_a=False,
        is_latest_date_for_source_b=True,
    )
    assert finding.classification == CLASS_STALE_SOURCE
    assert finding.classification != CLASS_EXPECTED_ADJUSTMENT_DIFFERENCE


def test_unexplained_divergence_when_no_explanation_matches_and_not_latest_date():
    finding = classify_pair(
        "AAA", "2026-08-07",
        "alpaca", 150.0, 1_000_000, "2026-08-08 20:00:00",
        "alpaca_adjusted", 100.0, 1_300_000, "2026-08-08 19:00:00",
        is_latest_date_for_source_a=False,
        is_latest_date_for_source_b=False,
    )
    assert finding.classification == CLASS_UNEXPLAINED_DIVERGENCE


def test_session_alignment_difference_matches_neighbor_date():
    finding = classify_pair(
        "AAA", "2026-08-07",
        "alpaca", 105.0, 1000, "2026-08-08 20:00:00",
        "yfinance", 100.0, 2000, "2026-08-08 19:00:00",
        is_latest_date_for_source_a=False,
        is_latest_date_for_source_b=False,
        neighbor_close_b_prev=105.0,
    )
    assert finding.classification == CLASS_SESSION_ALIGNMENT_DIFFERENCE


def test_zero_close_or_volume_never_raises_zerodivisionerror():
    finding = classify_pair(
        "AAA", "2026-08-07",
        "alpaca", 100.0, 100, "2026-08-08 20:00:00",
        "alpaca_adjusted", 0.0, 0, "2026-08-08 19:00:00",
    )
    assert finding.price_ratio is None
    assert finding.volume_ratio is None
    assert finding.classification == CLASS_UNEXPLAINED_DIVERGENCE


def test_unknown_source_tag_never_silently_matched():
    """Two sources with an unregistered/UNKNOWN adjustment-mode tag must
    never be treated as 'same adjustment mode' just because both are
    unknown - UNKNOWN != UNKNOWN is deliberately True (§3.2)."""
    finding = classify_pair(
        "AAA", "2026-08-07",
        "future_source_x", 100.30, 1_000_000, "2026-08-08 20:00:00",
        "future_source_y", 100.00, 1_000_000, "2026-08-08 19:00:00",
    )
    assert finding.adjustment_mode_a == "UNKNOWN"
    assert finding.adjustment_mode_b == "UNKNOWN"
    # Volume matches within tolerance and modes are considered "differ" ->
    # classified as an adjustment-mode difference (check #3), NOT silently
    # collapsed into MATCHED just because both tags are unknown.
    assert finding.classification == CLASS_EXPECTED_ADJUSTMENT_DIFFERENCE


# --- check_all_sources_freshness / build_ticker_summary ---


def test_check_all_sources_freshness_flags_non_authoritative_stale_without_flagging_authoritative():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 20)
    for s in sessions[-5:]:
        insert_price_row(conn, "AAA", s, source="alpaca")
    for s in sessions[:2]:  # far in the past -> definitely >5 sessions behind
        insert_price_row(conn, "AAA", s, source="alpaca_adjusted")

    result = check_all_sources_freshness(conn, "AAA", today=ANCHOR_SATURDAY)
    by_source = {r.source: r for r in result}

    assert by_source["alpaca"].is_authoritative_production is True
    assert by_source["alpaca"].stale is False
    assert by_source["alpaca_adjusted"].is_authoritative_production is False
    assert by_source["alpaca_adjusted"].stale is True


def test_check_all_sources_freshness_authoritative_stale_true_when_resolved_source_itself_stale():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 20)
    for s in sessions[:2]:  # only stale rows, no fresh source at all
        insert_price_row(conn, "AAA", s, source="alpaca")

    summary = build_ticker_summary(conn, "AAA", today=ANCHOR_SATURDAY)
    assert summary.authoritative_stale is True


def test_find_divergences_for_ticker_read_only():
    conn = make_test_db()
    sessions = recent_sessions(ANCHOR_SATURDAY, 5)
    for s in sessions:
        insert_price_row(conn, "AAA", s, close=100.0, source="alpaca")
        insert_price_row(conn, "AAA", s, close=100.05, source="alpaca_adjusted")
    before = conn.execute("SELECT COUNT(*) as n FROM prices").fetchone()["n"]
    find_divergences_for_ticker(conn, "AAA")
    after = conn.execute("SELECT COUNT(*) as n FROM prices").fetchone()["n"]
    assert before == after


def test_provider_reconciliation_never_imports_ingestion():
    path = os.path.join(REPO_ROOT, "ops", "provider_reconciliation.py")
    with open(path) as f:
        tree = ast.parse(f.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    forbidden = {i for i in names if i == "ingestion" or i.startswith("ingestion.")}
    assert not forbidden, f"ops/provider_reconciliation.py must never import ingestion modules: {forbidden}"


def test_real_aapl_msft_nvda_pattern_reproduction():
    """Seed rows with the exact confirmed values from spec §0.1 and assert
    the three expected classifications exactly."""
    conn = make_test_db()

    # (a) AAPL 2026-08-07: dividend-only difference, same volume.
    insert_price_row(conn, "AAPL", date(2026, 8, 7), close=313.33, volume=41_000_000, source="alpaca")
    insert_price_row(conn, "AAPL", date(2026, 8, 7), close=313.06, volume=41_000_000, source="alpaca_adjusted")

    # (b) NVDA 2024-06-07: real 10-for-1 split, both price and volume scale.
    insert_price_row(conn, "NVDA", date(2024, 6, 7), close=1208.88, volume=412_385_800, source="alpaca")
    insert_price_row(conn, "NVDA", date(2024, 6, 7), close=120.68, volume=41_238_580, source="alpaca_adjusted",
                      fetched_at="2026-08-13 20:30:07")

    # (c) AAPL 2026-08-12: stale intraday-fetched incomplete bar.
    insert_price_row(conn, "AAPL", date(2026, 8, 12), close=302.25, volume=41_873_071, source="alpaca",
                      fetched_at="2026-08-13 20:30:07")
    insert_price_row(conn, "AAPL", date(2026, 8, 12), close=301.52, volume=25_652_286, source="alpaca_adjusted",
                      fetched_at="2026-08-12 18:54:58")

    aapl_findings = {f.date: f for f in find_divergences_for_ticker(conn, "AAPL")}
    nvda_findings = {f.date: f for f in find_divergences_for_ticker(conn, "NVDA")}

    # (a) real delta (0.086%) is under CLOSE_MATCH_TOLERANCE_PCT (0.15%) -
    # correctly MATCHED, not EXPECTED_ADJUSTMENT_DIFFERENCE (spec §0.1(a)
    # implementation-time correction: MATCHED already means "no material
    # divergence, volume-consistent," which is right for a delta this small).
    assert aapl_findings["2026-08-07"].classification == CLASS_MATCHED
    assert nvda_findings["2024-06-07"].classification == CLASS_EXPECTED_ADJUSTMENT_DIFFERENCE
    assert aapl_findings["2026-08-12"].classification == CLASS_STALE_SOURCE
