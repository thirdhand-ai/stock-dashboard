"""Explicit, configurable rules for Phase 8's research/analysis layer.

Every threshold and weight used by research/*.py lives here, by name - the
same convention as signals/config.py, backtest/config.py, and
trading/config.py. Nothing in this package is connected to order
submission, alerts, or automation - see research/__init__.py's absence of
any `trading`/`automation` import anywhere in this package (enforced by
tests/test_research.py's structural-isolation check).

Everything here is RESEARCH-ONLY. No value defined in this module feeds
signals/config.py, backtest/config.py, trading/config.py, or any live
entry/exit decision.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class FundamentalScoreConfig:
    """Fundamental scoring is COHORT-RELATIVE, not based on fixed magic-number
    cutoffs (e.g. "P/E < 15 is cheap") - a single valuation multiple isn't
    universally good or bad across sectors/rate environments. Each metric is
    percentile-ranked against the other tickers actually being scored in the
    same run (the current watchlist), then combined into category scores.

    Category weights (must sum to 100). Profitability is weighted highest
    because margin/ROE data is the most consistently reported and least
    noisy category across this watchlist's mega-cap tickers; valuation and
    growth are weighted second; leverage lowest since debt/equity is a
    single, coarser signal. Adjust freely - there is nothing sacred about
    these numbers, they are a documented starting point.
    """

    valuation_weight: float = 25.0
    growth_weight: float = 25.0
    profitability_weight: float = 30.0
    leverage_weight: float = 20.0

    # Finnhub metric keys used for each category, in fallback preference
    # order (first present value wins). "lower_is_better" categories are
    # inverted before percentile-ranking so a high category score always
    # means "favorable."
    valuation_metrics: Tuple[Tuple[str, ...], ...] = (
        ("peTTM", "peExclExtraTTM", "peBasicExclExtraTTM", "peAnnual"),
        ("psTTM", "psAnnual"),
        ("pb", "pbAnnual", "pbQuarterly"),
    )
    growth_metrics: Tuple[Tuple[str, ...], ...] = (
        ("epsGrowthTTMYoy", "epsGrowth3Y", "epsGrowth5Y"),
        ("revenueGrowthTTMYoy", "revenueGrowth3Y", "revenueGrowth5Y"),
    )
    profitability_metrics: Tuple[Tuple[str, ...], ...] = (
        ("netProfitMarginTTM", "netProfitMarginAnnual"),
        ("operatingMarginTTM", "operatingMarginAnnual"),
        ("roeTTM", "roeRfy", "roe5Y"),
    )
    leverage_metrics: Tuple[Tuple[str, ...], ...] = (
        ("totalDebt/totalEquityQuarterly", "totalDebt/totalEquityAnnual"),
    )

    def total_weight(self) -> float:
        return self.valuation_weight + self.growth_weight + self.profitability_weight + self.leverage_weight


@dataclass(frozen=True)
class SentimentConfig:
    """Deterministic, local keyword-lexicon sentiment - not a call to any
    external AI/NLP API. This is a coarse heuristic (bag-of-words polarity),
    not true financial-NLP sentiment; treat it as a rough, cheap signal.
    """

    lookback_days: int = 14
    max_articles: int = 30

    # Net score (positive hits - negative hits) at/above this -> "positive";
    # at/below the negated value -> "negative"; otherwise "neutral". A small
    # deadband around zero avoids single-keyword headlines flip-flopping
    # the label.
    positive_threshold: int = 1
    negative_threshold: int = -1

    # news_sentiment_score = clamp(score_midpoint + avg_net_score_per_article
    # * score_scale_per_net_point, 0, 100). An average net score of 0 (equal
    # positive/negative keyword hits, or no hits) sits at the neutral
    # midpoint; +/-1 average net score per article moves the score by
    # score_scale_per_net_point points. The scale is a documented starting
    # point, not a calibrated statistical mapping.
    score_midpoint: float = 50.0
    score_scale_per_net_point: float = 15.0

    positive_words: Tuple[str, ...] = (
        "beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies",
        "upgrade", "upgraded", "outperform", "record", "growth", "strong",
        "profit", "profits", "gain", "gains", "buyback", "expansion",
        "bullish", "positive", "raises guidance", "raise guidance",
        "exceeds", "exceeded", "top estimates", "beats estimates",
    )
    negative_words: Tuple[str, ...] = (
        "miss", "misses", "missed", "plunge", "plunges", "slump", "slumps",
        "downgrade", "downgraded", "underperform", "decline", "declines",
        "weak", "loss", "losses", "layoffs", "lawsuit", "investigation",
        "probe", "recall", "cuts guidance", "cut guidance", "bearish",
        "negative", "warning", "delay", "delays", "fraud", "breach",
    )

    # Keyword flags surfaced separately from the pos/neg polarity count -
    # "this headline is probably a big deal," independent of tone.
    major_positive_keywords: Tuple[str, ...] = (
        "acquisition", "acquires", "merger", "record revenue", "stock split",
        "beats estimates", "raises guidance",
    )
    major_negative_keywords: Tuple[str, ...] = (
        "investigation", "lawsuit", "recall", "data breach", "sec probe",
        "earnings miss", "cuts guidance", "resigns", "bankruptcy",
    )


@dataclass(frozen=True)
class RegimeConfig:
    """Market-regime labeling from trailing, objective indicators only -
    price vs moving averages, realized volatility, drawdown. Priority order
    when labeling (see research/regime.py): elevated volatility overrides
    trend labels, since a choppy/risk-off tape matters regardless of moving
    average position.
    """

    primary_benchmark: str = "SPY"
    secondary_benchmark: Optional[str] = "QQQ"

    sma_short_days: int = 50
    sma_long_days: int = 200

    realized_vol_window_days: int = 20
    """Trailing window for realized volatility (annualized stdev of daily returns)."""
    elevated_vol_annualized_threshold: float = 0.25
    """Above this annualized realized vol -> "elevated_volatility_risk_off" regardless of trend.
    SPY's long-run realized vol is roughly 12-20%; 25% is a documented starting
    threshold for "meaningfully elevated," not a validated statistical cutoff."""

    drawdown_window_days: int = 60
    """Trailing window used to compute drawdown-from-high for regime context."""

    LABEL_BULLISH = "bullish_trend"
    LABEL_NEUTRAL = "neutral_mixed"
    LABEL_BEARISH = "bearish_trend"
    LABEL_ELEVATED_VOL = "elevated_volatility_risk_off"


@dataclass(frozen=True)
class RelativeStrengthConfig:
    windows_trading_days: Dict[str, int] = field(default_factory=lambda: {"1m": 21, "3m": 63, "6m": 126})
    volatility_window_days: int = 20
    drawdown_window_days: int = 126
    primary_benchmark: str = "SPY"

    # Sector metadata kept as a small static map rather than a new API
    # integration - "avoid adding many new external dependencies or APIs
    # just for sector metadata" (Phase 8 spec). Covers exactly the
    # configured watchlist; extend by hand if the watchlist changes.
    sector_map: Dict[str, str] = field(default_factory=lambda: {
        "AAPL": "Technology",
        "MSFT": "Technology",
        "GOOGL": "Communication Services",
        "AMZN": "Consumer Discretionary",
        "NVDA": "Technology",
        "TSLA": "Consumer Discretionary",
        "META": "Communication Services",
    })

    # No true sector-ETF price data is ingested (would require a new data
    # source). QQQ (Nasdaq-100, tech-heavy) is used as an approximate
    # sector proxy for "Technology" only - a documented simplification, not
    # a real sector benchmark. Other sectors fall back to SPY-only.
    sector_benchmark_proxy: Dict[str, str] = field(default_factory=lambda: {"Technology": "QQQ"})


@dataclass(frozen=True)
class ResearchCompositeConfig:
    """Weights for the RESEARCH-ONLY composite score. Not used by
    signals/backtest/trading in any way. Missing components are excluded and
    the remaining weights renormalized (see research/composite.py) rather
    than fabricating a value for a component with no data.
    """

    technical_weight: float = 35.0
    fundamental_weight: float = 25.0
    sentiment_weight: float = 10.0
    relative_strength_weight: float = 20.0
    regime_weight: float = 10.0

    # Regime label -> a flat, ticker-independent 0-100 "how favorable is
    # the macro backdrop" contribution to the composite.
    regime_component_scores: Dict[str, float] = field(default_factory=lambda: {
        RegimeConfig.LABEL_BULLISH: 100.0,
        RegimeConfig.LABEL_NEUTRAL: 50.0,
        RegimeConfig.LABEL_BEARISH: 0.0,
        RegimeConfig.LABEL_ELEVATED_VOL: 25.0,
    })

    def total_weight(self) -> float:
        return (
            self.technical_weight + self.fundamental_weight + self.sentiment_weight
            + self.relative_strength_weight + self.regime_weight
        )


@dataclass(frozen=True)
class HistoricalQualityConfig:
    """Buckets and horizons for the historical-signal-quality analysis
    (research/historical_quality.py). This module measures the EXISTING
    production signal's historical behavior - it never adjusts
    signals/config.py or backtest/config.py based on what it finds."""

    forward_return_horizons_days: Tuple[int, ...] = (1, 5, 20)
    score_buckets: Tuple[Tuple[float, float, str], ...] = (
        (0.0, 39.999, "0-39"),
        (40.0, 54.999, "40-54"),
        (55.0, 69.999, "55-69"),
        (70.0, 84.999, "70-84"),
        (85.0, 100.0, "85-100"),
    )
    min_sample_size_for_stats: int = 5
    """Below this observation count, mean/median/%positive are withheld -
    only the (small) count is shown, so a thin sample never looks authoritative."""


DEFAULT_FUNDAMENTAL_CONFIG = FundamentalScoreConfig()
DEFAULT_SENTIMENT_CONFIG = SentimentConfig()
DEFAULT_REGIME_CONFIG = RegimeConfig()
DEFAULT_RELATIVE_STRENGTH_CONFIG = RelativeStrengthConfig()
DEFAULT_RESEARCH_COMPOSITE_CONFIG = ResearchCompositeConfig()
DEFAULT_HISTORICAL_QUALITY_CONFIG = HistoricalQualityConfig()

assert abs(DEFAULT_FUNDAMENTAL_CONFIG.total_weight() - 100.0) < 1e-9, "FundamentalScoreConfig weights must sum to 100"
assert abs(DEFAULT_RESEARCH_COMPOSITE_CONFIG.total_weight() - 100.0) < 1e-9, "ResearchCompositeConfig weights must sum to 100"
