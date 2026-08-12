"""Tests for the Phase 8 research/analysis layer (research/*.py,
dashboard's research data helpers).

Everything here is synthetic/deterministic - no test makes a real network
call, and (per Phase 8's mandate) no test in this file may place a paper or
live order or send a Discord notification. See
test_research_package_never_imports_trading_or_automation /
test_research_center_view_never_imports_order_execution_modules for the
explicit structural check that this research layer cannot reach order
execution at all.
"""
import ast
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from db.schema import init_db
from research.composite import compute_research_score
from research.config import RegimeConfig, RelativeStrengthConfig, SentimentConfig
from research.fundamentals import (
    FundamentalMetrics,
    compute_fundamental_scores_cohort,
    load_fundamental_metrics,
    percentile_ranks,
)
from research.historical_quality import bucket_by_score, build_score_return_table, compute_forward_returns
from research.regime import compute_market_regime
from research.relative_strength import compute_relative_strength
from research.sentiment import classify_text, compute_news_sentiment_summary

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_price_rows(conn, ticker, closes, start="2023-01-02", source="test"):
    dates = pd.bdate_range(start, periods=len(closes))
    rows = [
        (ticker, d.strftime("%Y-%m-%d"), c, c + 0.5, c - 0.5, c, 1_000_000, source)
        for d, c in zip(dates, closes)
    ]
    conn.executemany(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()


def insert_fundamental_rows(conn, ticker, metrics: dict, as_of="2026-08-10"):
    rows = [(ticker, k, float(v), as_of) for k, v in metrics.items()]
    conn.executemany("INSERT INTO fundamentals (ticker, metric, value, as_of) VALUES (?,?,?,?)", rows)
    conn.commit()


def insert_news_row(conn, ticker, headline, summary="", published_at=None, source="test-source"):
    published_at = published_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO news (ticker, headline, summary, source, url, published_at) VALUES (?,?,?,?,?,?)",
        (ticker, headline, summary, source, f"https://example.test/{ticker}/{hash(headline)}", published_at),
    )
    conn.commit()


# --- structural isolation: the research layer must never reach order execution ---


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


def test_research_package_never_imports_trading_or_automation():
    """research/*.py must have ZERO dependency on trading/* or automation/*
    - position status is passed into research/ranking.py as a plain set of
    strings by the caller, never fetched by the research layer itself."""
    research_dir = os.path.join(REPO_ROOT, "research")
    offenders = {}
    for filename in os.listdir(research_dir):
        if not filename.endswith(".py"):
            continue
        imports = _module_level_import_names(os.path.join(research_dir, filename))
        forbidden = {i for i in imports if i == "trading" or i.startswith("trading.") or i == "automation" or i.startswith("automation.")}
        if forbidden:
            offenders[filename] = forbidden
    assert not offenders, f"research/*.py must never import trading/automation: {offenders}"


def test_research_center_view_never_imports_order_execution_modules():
    """The dashboard view may read trading.client for read-only position
    display (like paper_portfolio.py does) but must never import the
    order-execution modules themselves."""
    path = os.path.join(REPO_ROOT, "dashboard", "views", "research_center.py")
    imports = _module_level_import_names(path)
    forbidden = {
        i for i in imports
        if i in ("trading.engine", "trading.orders", "trading.reconcile") or i.startswith("automation.")
    }
    assert not forbidden, f"research_center.py must never import order-execution modules: {forbidden}"


def test_research_engine_module_has_no_run_cycle_reference():
    """Belt-and-suspenders: confirm the actual submitted symbols research/
    ranking.py resolves at import time don't include trading.engine.run_cycle."""
    import research.ranking as ranking_module
    assert not hasattr(ranking_module, "run_cycle")
    assert "trading" not in ranking_module.__dict__


# --- fundamentals: cohort-relative scoring ---


def test_fundamental_scoring_ranks_stronger_cohort_member_higher():
    metrics = {
        "STRONG": FundamentalMetrics(ticker="STRONG", ok=True, raw={
            "peTTM": 15.0, "psTTM": 3.0, "pb": 4.0,
            "epsGrowthTTMYoy": 30.0, "revenueGrowthTTMYoy": 25.0,
            "netProfitMarginTTM": 35.0, "operatingMarginTTM": 40.0, "roeTTM": 45.0,
            "totalDebt/totalEquityQuarterly": 0.2,
        }),
        "WEAK": FundamentalMetrics(ticker="WEAK", ok=True, raw={
            "peTTM": 60.0, "psTTM": 20.0, "pb": 25.0,
            "epsGrowthTTMYoy": -10.0, "revenueGrowthTTMYoy": -5.0,
            "netProfitMarginTTM": 2.0, "operatingMarginTTM": 3.0, "roeTTM": 4.0,
            "totalDebt/totalEquityQuarterly": 3.5,
        }),
    }
    scores = compute_fundamental_scores_cohort(metrics)
    assert scores["STRONG"].ok and scores["WEAK"].ok
    assert scores["STRONG"].score > scores["WEAK"].score


def test_fundamental_scoring_handles_missing_data_without_fabrication():
    metrics = {
        "NODATA": FundamentalMetrics(ticker="NODATA", ok=False, reason="no fundamentals data stored for this ticker yet"),
        "PARTIAL": FundamentalMetrics(ticker="PARTIAL", ok=True, raw={"netProfitMarginTTM": 30.0, "operatingMarginTTM": 30.0, "roeTTM": 30.0}),
    }
    scores = compute_fundamental_scores_cohort(metrics)

    assert scores["NODATA"].ok is False
    assert scores["NODATA"].score is None

    # PARTIAL has only profitability data - score must be computed from
    # that category alone (100% of the weight), not fabricated for the
    # missing valuation/growth/leverage categories.
    assert scores["PARTIAL"].ok is True
    assert scores["PARTIAL"].category_scores["valuation"] is None
    assert scores["PARTIAL"].category_scores["growth"] is None
    assert scores["PARTIAL"].category_scores["leverage"] is None
    assert scores["PARTIAL"].category_scores["profitability"] is not None
    assert scores["PARTIAL"].category_weights_applied == {"profitability": 100.0}


def test_percentile_ranks_single_ticker_cohort_is_neutral():
    assert percentile_ranks({"ONLY": 12.3}, higher_is_better=True) == {"ONLY": 50.0}


def test_load_fundamental_metrics_includes_earnings_date():
    conn = make_test_db()
    epoch = datetime(2026, 10, 28, tzinfo=timezone.utc).timestamp()
    insert_fundamental_rows(conn, "AAPL", {"peTTM": 30.0, "nextEarningsDateEpoch": epoch})
    metrics = load_fundamental_metrics(conn, "AAPL")
    assert metrics.ok
    assert metrics.earnings_date == "2026-10-28"
    assert metrics.days_until_earnings is not None


def test_load_fundamental_metrics_missing_ticker_is_not_ok():
    conn = make_test_db()
    metrics = load_fundamental_metrics(conn, "NOPE")
    assert metrics.ok is False


# --- sentiment: deterministic local classification ---


def test_classify_text_positive_and_major_flag():
    result = classify_text("Company beats estimates and raises guidance after record revenue quarter")
    assert result.label == "positive"
    assert result.major_positive is True
    assert result.major_negative is False


def test_classify_text_negative_and_major_flag():
    result = classify_text("Company faces SEC investigation and lawsuit after cuts guidance")
    assert result.label == "negative"
    assert result.major_negative is True


def test_classify_text_neutral_when_no_keywords():
    result = classify_text("Company will host its annual investor day next month")
    assert result.label == "neutral"
    assert result.net_score == 0


def test_classify_text_does_not_match_substrings_inside_other_words():
    # "miss" must not match inside "mississippi"/"dismiss" style false positives
    result = classify_text("The Mississippi river flows past the new plant")
    assert result.label == "neutral"


def test_news_sentiment_summary_counts_and_score():
    conn = make_test_db()
    insert_news_row(conn, "AAPL", "Apple beats estimates, raises guidance")
    insert_news_row(conn, "AAPL", "Apple faces lawsuit over patent dispute")
    insert_news_row(conn, "AAPL", "Apple hosts developer conference")

    summary = compute_news_sentiment_summary(conn, "AAPL")
    assert summary.ok
    assert summary.counts["positive"] == 1
    assert summary.counts["negative"] == 1
    assert summary.counts["neutral"] == 1
    assert 0.0 <= summary.news_sentiment_score <= 100.0


def test_news_sentiment_summary_respects_lookback_window():
    conn = make_test_db()
    old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
    insert_news_row(conn, "AAPL", "Old headline beats estimates", published_at=old_date)
    summary = compute_news_sentiment_summary(conn, "AAPL", config=SentimentConfig(lookback_days=14))
    assert summary.ok is False


def test_news_sentiment_summary_no_news_is_not_ok():
    conn = make_test_db()
    summary = compute_news_sentiment_summary(conn, "NODATA")
    assert summary.ok is False


# --- market regime ---


def test_regime_classifies_bullish_trend():
    conn = make_test_db()
    closes = 100 + np.cumsum(np.full(260, 0.3))  # smooth, low-vol uptrend
    insert_price_rows(conn, "SPY", closes)
    regime = compute_market_regime(conn, RegimeConfig(secondary_benchmark=None))
    assert regime.ok
    assert regime.label == "bullish_trend"


def test_regime_classifies_bearish_trend():
    conn = make_test_db()
    closes = 300 - np.cumsum(np.full(260, 0.3))
    insert_price_rows(conn, "SPY", closes)
    regime = compute_market_regime(conn, RegimeConfig(secondary_benchmark=None))
    assert regime.ok
    assert regime.label == "bearish_trend"


def test_regime_classifies_elevated_volatility():
    conn = make_test_db()
    rng = np.random.default_rng(7)
    # Multiplicative daily returns (never lets price go non-positive) with a
    # large sigma -> ~80% annualized realized vol, comfortably over the
    # default 25% threshold.
    daily_returns = rng.normal(0, 0.05, 260)
    closes = 100 * np.cumprod(1 + daily_returns)
    insert_price_rows(conn, "SPY", closes)
    regime = compute_market_regime(conn, RegimeConfig(secondary_benchmark=None))
    assert regime.ok
    assert regime.label == "elevated_volatility_risk_off"


def test_regime_insufficient_history_is_not_ok():
    conn = make_test_db()
    insert_price_rows(conn, "SPY", [100.0] * 50)
    regime = compute_market_regime(conn, RegimeConfig(secondary_benchmark=None))
    assert regime.ok is False


# --- relative strength ---


def test_relative_strength_outperformance_is_positive():
    conn = make_test_db()
    flat = [100.0] * 130
    insert_price_rows(conn, "SPY", flat)
    outperformer = list(100 + np.cumsum(np.full(130, 0.5)))
    insert_price_rows(conn, "HOT", outperformer)

    result = compute_relative_strength(conn, "HOT", RelativeStrengthConfig(sector_map={}, sector_benchmark_proxy={}))
    assert result.ok
    assert result.relative_strength_pct["1m"] > 0
    assert result.relative_strength_pct["3m"] > 0


def test_relative_strength_uses_sector_proxy_when_mapped():
    conn = make_test_db()
    insert_price_rows(conn, "SPY", [100.0] * 130)
    insert_price_rows(conn, "QQQ", [200.0] * 130)
    insert_price_rows(conn, "TECHCO", list(50 + np.cumsum(np.full(130, 0.2))))

    config = RelativeStrengthConfig(sector_map={"TECHCO": "Technology"}, sector_benchmark_proxy={"Technology": "QQQ"})
    result = compute_relative_strength(conn, "TECHCO", config)
    assert result.ok
    assert result.sector == "Technology"
    assert result.sector_benchmark == "QQQ"
    assert "1m" in result.relative_strength_vs_sector_pct


def test_relative_strength_no_price_history_is_not_ok():
    conn = make_test_db()
    insert_price_rows(conn, "SPY", [100.0] * 130)
    result = compute_relative_strength(conn, "NODATA")
    assert result.ok is False


# --- research composite ---


def test_research_composite_weighted_combination_matches_manual_calc():
    score = compute_research_score(
        ticker="X", technical_score=80.0, fundamental_score=60.0, sentiment_score=50.0,
        relative_strength_score=70.0, regime_label="bullish_trend",
    )
    assert score.ok
    # All 5 components present -> full configured weights apply.
    assert sum(score.weights_applied_pct.values()) == pytest.approx(100.0)
    expected = 80.0 * 0.35 + 60.0 * 0.25 + 50.0 * 0.10 + 70.0 * 0.20 + 100.0 * 0.10
    assert score.score == pytest.approx(round(expected, 1), abs=0.1)


def test_research_composite_renormalizes_when_components_missing():
    score = compute_research_score(
        ticker="X", technical_score=90.0, fundamental_score=None, sentiment_score=None,
        relative_strength_score=None, regime_label=None,
    )
    assert score.ok
    assert score.components == {"technical": 90.0}
    assert score.weights_applied_pct == {"technical": 100.0}
    assert score.score == pytest.approx(90.0)


def test_research_composite_no_components_is_not_ok():
    score = compute_research_score(
        ticker="X", technical_score=None, fundamental_score=None, sentiment_score=None,
        relative_strength_score=None, regime_label=None,
    )
    assert score.ok is False


# --- historical signal quality: forward returns, no-lookahead, bucketing ---


def test_compute_forward_returns_correctness_and_tail_nan():
    df = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=10).strftime("%Y-%m-%d"), "close": [100 + i for i in range(10)]})
    fwd = compute_forward_returns(df, horizons_days=(1, 5))

    # row 0: fwd_return_1d = close[1]/close[0] - 1
    assert fwd["fwd_return_1d"].iloc[0] == pytest.approx(101 / 100 - 1)
    assert fwd["fwd_return_5d"].iloc[0] == pytest.approx(105 / 100 - 1)

    # last row can never have a forward return - no future data exists yet.
    assert pd.isna(fwd["fwd_return_1d"].iloc[-1])
    assert pd.isna(fwd["fwd_return_5d"].iloc[-5])  # not enough days left for a 5d-forward return


def test_historical_quality_no_lookahead_score_matches_truncated_series():
    """The score computed for date t must be identical whether it's
    computed against the full price history or a history truncated to end
    exactly at t - proof that nothing after t leaks into the signal."""
    conn = make_test_db()
    rng = np.random.default_rng(3)
    closes = 100 + np.cumsum(rng.normal(0.05, 1.0, 150))
    closes = np.maximum(closes, 1.0)
    insert_price_rows(conn, "AAPL", closes)

    full = build_score_return_table(conn, "AAPL")
    assert full.ok

    conn_truncated = make_test_db()
    cutoff_index = 120
    insert_price_rows(conn_truncated, "AAPL", closes[: cutoff_index + 1])
    truncated = build_score_return_table(conn_truncated, "AAPL")
    assert truncated.ok

    cutoff_date = truncated.table["date"].iloc[-1]
    full_row = full.table[full.table["date"] == cutoff_date]
    truncated_row = truncated.table[truncated.table["date"] == cutoff_date]
    assert not full_row.empty and not truncated_row.empty
    assert full_row["score"].iloc[0] == pytest.approx(truncated_row["score"].iloc[0])
    assert full_row["stage_rank"].iloc[0] == truncated_row["stage_rank"].iloc[0]


def test_bucket_by_score_withholds_stats_below_min_sample_size():
    df = pd.DataFrame({
        "score": [10.0, 15.0, 90.0],
        "stage_label": ["none", "none", "volume"],
        "fwd_return_1d": [0.01, -0.02, 0.03],
    })
    from research.config import HistoricalQualityConfig
    config = HistoricalQualityConfig(
        forward_return_horizons_days=(1,),
        score_buckets=((0.0, 39.999, "0-39"), (85.0, 100.0, "85-100")),
        min_sample_size_for_stats=5,
    )
    result = bucket_by_score(df, config)

    low_bucket = result[result["bucket"] == "0-39"].iloc[0]
    assert low_bucket["count"] == 2
    assert bool(low_bucket["sufficient_sample"]) is False
    assert low_bucket["mean_return_pct"] is None  # withheld - thin sample

    high_bucket = result[result["bucket"] == "85-100"].iloc[0]
    assert high_bucket["count"] == 1
    assert bool(high_bucket["sufficient_sample"]) is False


def test_build_score_return_table_insufficient_history_is_not_ok():
    conn = make_test_db()
    insert_price_rows(conn, "AAPL", [100.0] * 30)
    result = build_score_return_table(conn, "AAPL")
    assert result.ok is False


# --- dashboard data helper: read-only position lookup ---


def test_load_open_position_tickers_degrades_gracefully_on_failure():
    from dashboard.data import _load_open_position_tickers

    with patch("trading.client.get_client", side_effect=RuntimeError("no credentials")):
        result = _load_open_position_tickers()
    assert result == set()


def test_load_open_position_tickers_returns_symbols_on_success():
    from dashboard.data import _load_open_position_tickers

    class FakePosition:
        def __init__(self, symbol):
            self.symbol = symbol

    class FakeClient:
        pass

    with patch("trading.client.get_client", return_value=FakeClient()), \
         patch("trading.client.verify_paper_environment", return_value=None), \
         patch("trading.client.get_positions", return_value=[FakePosition("AAPL"), FakePosition("MSFT")]):
        result = _load_open_position_tickers()
    assert result == {"AAPL", "MSFT"}


# --- ranking: light end-to-end integration ---


def test_build_research_ranking_end_to_end_with_position_flag():
    from research.ranking import build_research_ranking

    conn = make_test_db()
    rng = np.random.default_rng(1)
    for ticker in ["AAPL", "MSFT"]:
        closes = 100 + np.cumsum(rng.normal(0.05, 1.0, 150))
        insert_price_rows(conn, ticker, np.maximum(closes, 1.0))
        insert_fundamental_rows(conn, ticker, {"peTTM": 20.0, "roeTTM": 25.0})
        insert_news_row(conn, ticker, f"{ticker} beats estimates")
    insert_price_rows(conn, "SPY", 100 + np.cumsum(rng.normal(0.02, 1.0, 150)))

    rows = build_research_ranking(conn, ["AAPL", "MSFT"], position_tickers={"AAPL"})
    by_ticker = {r.ticker: r for r in rows}
    assert by_ticker["AAPL"].has_open_position is True
    assert by_ticker["MSFT"].has_open_position is False
    assert by_ticker["AAPL"].research_score.ok
