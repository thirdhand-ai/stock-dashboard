"""Tests for trading/concentration.py's analytics: position weight %,
sector rollup, and threshold flagging. Pure functions over hand-built
RealHoldingView objects - no DB, no network (ensure_sector_data's Finnhub
call is exercised separately in tests/test_concentration_sector_data.py
with a mocked fetch)."""
from trading.concentration import (
    SECTOR_THRESHOLD_PCT,
    SINGLE_POSITION_THRESHOLD_PCT,
    build_concentration_report,
    build_concentration_reports,
)
from trading.real_holdings import RealHoldingView


def make_view(ticker, owner, market_value, needs_manual_entry=False):
    return RealHoldingView(
        ticker=ticker, owner=owner, shares=100.0 if market_value is not None else None,
        cost_basis_total=1000.0, cost_basis_per_share=10.0, current_price=100.0 if market_value is not None else None,
        price_as_of="2026-08-23", source="alpaca", market_value=market_value, unrealized_pl=None,
        unrealized_pl_pct=None, realized_gain=0.0, weight_pct=None, needs_manual_entry=needs_manual_entry, note=None,
    )


def test_weight_pct_sums_to_100():
    views = [make_view("AAA", "Mom", 8000.0), make_view("BBB", "Mom", 2000.0)]
    report = build_concentration_report(views, {}, "Combined")

    assert report.total_market_value == 10000.0
    weights = {p.ticker: p.weight_pct for p in report.positions}
    assert weights["AAA"] == 80.0
    assert weights["BBB"] == 20.0


def test_position_exceeding_threshold_is_flagged():
    views = [make_view("AAA", "Mom", 8000.0), make_view("BBB", "Mom", 2000.0)]
    report = build_concentration_report(views, {}, "Combined")

    assert report.flagged_positions == [p for p in report.positions if p.ticker == "AAA"]
    bbb = next(p for p in report.positions if p.ticker == "BBB")
    assert bbb.exceeds_threshold is False


def test_position_at_exactly_the_threshold_is_not_flagged():
    """Strictly greater-than, not >= - a position sitting exactly at the
    round-number threshold isn't itself "exceeding" it."""
    views = [make_view("AAA", "Mom", SINGLE_POSITION_THRESHOLD_PCT), make_view("BBB", "Mom", 100 - SINGLE_POSITION_THRESHOLD_PCT)]
    report = build_concentration_report(views, {}, "Combined")

    aaa = next(p for p in report.positions if p.ticker == "AAA")
    assert aaa.weight_pct == 20.0
    assert aaa.exceeds_threshold is False


def test_unpriced_position_excluded_from_weights_and_listed_separately():
    views = [make_view("AAA", "Mom", 5000.0), make_view("STN", "Mom", None, needs_manual_entry=True)]
    report = build_concentration_report(views, {}, "Combined")

    assert report.total_market_value == 5000.0
    assert [p.ticker for p in report.positions] == ["AAA"]
    assert report.excluded_tickers == ["STN (Mom)"]


def test_empty_portfolio_has_zero_total_and_no_positions():
    report = build_concentration_report([], {}, "Combined")
    assert report.total_market_value == 0.0
    assert report.positions == []
    assert report.sectors == []


def test_sector_rollup_aggregates_multiple_tickers_and_flags_overweight():
    views = [make_view("AAA", "Mom", 4000.0), make_view("BBB", "Mom", 4000.0), make_view("CCC", "Mom", 2000.0)]
    sectors = {"AAA": "Technology", "BBB": "Technology", "CCC": "Healthcare"}
    report = build_concentration_report(views, sectors, "Combined")

    tech = next(s for s in report.sectors if s.industry == "Technology")
    assert tech.market_value == 8000.0
    assert tech.weight_pct == 80.0
    assert tech.exceeds_threshold is True
    assert sorted(tech.tickers) == ["AAA", "BBB"]

    health = next(s for s in report.sectors if s.industry == "Healthcare")
    assert health.weight_pct == 20.0
    assert health.exceeds_threshold is False


def test_missing_sector_classification_buckets_as_unknown():
    views = [make_view("AAA", "Mom", 1000.0)]
    report = build_concentration_report(views, {}, "Combined")

    assert report.sectors[0].industry == "Unknown"


def test_build_concentration_reports_computes_each_owner_relative_to_their_own_total():
    """Mom and Tyler have very different concentration profiles - each
    owner's weight_pct must be relative to THEIR OWN total, not the
    combined total."""
    views = [
        make_view("AAA", "Mom", 90000.0),
        make_view("BBB", "Mom", 10000.0),
        make_view("CCC", "Tyler", 500.0),
    ]
    reports = build_concentration_reports(views, {})

    assert set(reports) == {"Combined", "Mom", "Tyler"}
    assert reports["Combined"].total_market_value == 100500.0
    ccc_combined = next(p for p in reports["Combined"].positions if p.ticker == "CCC")
    assert round(ccc_combined.weight_pct, 2) == round(500.0 / 100500.0 * 100, 2)

    ccc_tyler = next(p for p in reports["Tyler"].positions if p.ticker == "CCC")
    assert ccc_tyler.weight_pct == 100.0  # Tyler's ONLY position - 100% of his own portfolio
    assert ccc_tyler.exceeds_threshold is True


def test_owner_with_only_unpriced_positions_gets_empty_report():
    views = [make_view("STN", "Tyler", None, needs_manual_entry=True)]
    reports = build_concentration_reports(views, {})

    assert reports["Tyler"].total_market_value == 0.0
    assert reports["Tyler"].positions == []
    assert reports["Tyler"].excluded_tickers == ["STN (Tyler)"]
