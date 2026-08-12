"""Cross-sectional research ranking across the configured watchlist -
RESEARCH ONLY.

Answers "which tickers currently have the strongest overall research
setup" - it never answers "which stock should automatically be bought."
TickerResearchRow has no order-related field and this module has zero
dependency on trading/*, alerts/*, or automation/* (position status is
passed in by the caller as a plain set of ticker strings, not fetched here)
- see tests/test_research.py's structural-isolation check.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from indicators.technical import compute_indicators_for_ticker
from research.composite import ResearchScore, compute_research_score
from research.config import DEFAULT_RELATIVE_STRENGTH_CONFIG, DEFAULT_RESEARCH_COMPOSITE_CONFIG, ResearchCompositeConfig
from research.fundamentals import compute_fundamental_scores_cohort, load_fundamental_metrics_cohort, percentile_ranks
from research.regime import compute_market_regime
from research.relative_strength import compute_relative_strength
from research.sentiment import compute_news_sentiment_summary
from signals.engine import score_indicators

# The relative-strength window used to feed the composite's cohort-relative
# component - "3m" is a reasonable middle ground between the 1m/6m windows
# also computed and displayed. Falls back to whichever window is configured
# first if "3m" isn't present.
_COMPOSITE_RS_WINDOW_FALLBACK_ORDER = ("3m", "1m", "6m")


@dataclass
class TickerResearchRow:
    ticker: str
    technical_ok: bool
    technical_score: Optional[float]
    technical_stage: Optional[str]
    technical_reason: Optional[str]
    research_score: ResearchScore
    fundamental_score: Optional[float]
    sentiment_score: Optional[float]
    sentiment_counts: Dict[str, int]
    relative_strength_pct: Dict[str, float]
    relative_strength_percentile: Optional[float]
    regime_label: Optional[str]
    has_open_position: bool


def _composite_rs_window(config) -> Optional[str]:
    for label in _COMPOSITE_RS_WINDOW_FALLBACK_ORDER:
        if label in config.windows_trading_days:
            return label
    return next(iter(config.windows_trading_days), None)


def build_research_ranking(
    conn,
    tickers: List[str],
    position_tickers: Optional[Set[str]] = None,
    composite_config: ResearchCompositeConfig = DEFAULT_RESEARCH_COMPOSITE_CONFIG,
    relative_strength_config=DEFAULT_RELATIVE_STRENGTH_CONFIG,
) -> List[TickerResearchRow]:
    position_tickers = position_tickers or set()

    # --- technical: the existing production signal, read-only reuse ---
    technical_by_ticker = {}
    for ticker in tickers:
        indicators = compute_indicators_for_ticker(conn, ticker)
        if indicators.ok:
            score = score_indicators(indicators)
            technical_by_ticker[ticker] = (True, score.score, score.highest_confirmed_stage, None)
        else:
            technical_by_ticker[ticker] = (False, None, None, indicators.reason)

    # --- fundamentals: cohort-relative (see research/fundamentals.py) ---
    fundamental_metrics = load_fundamental_metrics_cohort(conn, tickers)
    fundamental_scores = compute_fundamental_scores_cohort(fundamental_metrics)

    # --- sentiment: per-ticker, no cohort dependency ---
    sentiment_by_ticker = {t: compute_news_sentiment_summary(conn, t) for t in tickers}

    # --- relative strength: raw values, then cohort-percentile-ranked for the composite ---
    rs_by_ticker = {t: compute_relative_strength(conn, t, relative_strength_config) for t in tickers}
    rs_window = _composite_rs_window(relative_strength_config)
    rs_window_values = {
        t: rs.relative_strength_pct[rs_window]
        for t, rs in rs_by_ticker.items() if rs.ok and rs_window and rs_window in rs.relative_strength_pct
    }
    rs_percentiles = percentile_ranks(rs_window_values, higher_is_better=True) if rs_window_values else {}

    # --- market regime: one shared macro read for every ticker this cycle ---
    regime = compute_market_regime(conn)
    regime_label = regime.label if regime.ok else None

    rows = []
    for ticker in tickers:
        tech_ok, tech_score, tech_stage, tech_reason = technical_by_ticker[ticker]
        fscore = fundamental_scores[ticker]
        ssum = sentiment_by_ticker[ticker]
        rs = rs_by_ticker[ticker]
        rs_pct = rs_percentiles.get(ticker)

        research_score = compute_research_score(
            ticker=ticker,
            technical_score=tech_score if tech_ok else None,
            fundamental_score=fscore.score if fscore.ok else None,
            sentiment_score=ssum.news_sentiment_score if ssum.ok else None,
            relative_strength_score=rs_pct,
            regime_label=regime_label,
            config=composite_config,
        )

        rows.append(TickerResearchRow(
            ticker=ticker,
            technical_ok=tech_ok, technical_score=tech_score, technical_stage=tech_stage, technical_reason=tech_reason,
            research_score=research_score,
            fundamental_score=fscore.score if fscore.ok else None,
            sentiment_score=ssum.news_sentiment_score if ssum.ok else None,
            sentiment_counts=ssum.counts if ssum.ok else {},
            relative_strength_pct=rs.relative_strength_pct if rs.ok else {},
            relative_strength_percentile=rs_pct,
            regime_label=regime_label,
            has_open_position=ticker in position_tickers,
        ))

    return rows
