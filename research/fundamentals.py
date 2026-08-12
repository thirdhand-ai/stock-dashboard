"""Fundamental analysis layer - RESEARCH ONLY.

Reads fundamentals already stored by ingestion/finnhub_source.py's plain
metric/value table (no new data source, no live API calls from this
module - it only reads SQLite, the same pattern signals/backtest/dashboard
already follow). Never fabricates a missing metric: a ticker with no data
for a given metric is simply excluded from that metric's contribution to
the score, and a ticker with no fundamentals data at all gets `ok=False`
rather than a fabricated score.

Scoring is COHORT-RELATIVE (percentile rank within the tickers actually
passed to compute_fundamental_scores_cohort), not fixed magic-number
cutoffs - see research/config.py's FundamentalScoreConfig docstring for why.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from research.config import DEFAULT_FUNDAMENTAL_CONFIG, FundamentalScoreConfig

# Display-facing fields pulled from Finnhub's metric dict, in fallback
# preference order (first present key wins). Values are Finnhub's native
# units (ratios as plain numbers, growth/margins as percents, market cap in
# millions of dollars).
DISPLAY_METRIC_FALLBACKS: Dict[str, Tuple[str, ...]] = {
    "market_cap_millions": ("marketCapitalization",),
    "pe_trailing": ("peTTM", "peExclExtraTTM", "peBasicExclExtraTTM", "peAnnual"),
    "pe_forward": ("forwardPE",),
    "price_to_sales": ("psTTM", "psAnnual"),
    "price_to_book": ("pb", "pbAnnual", "pbQuarterly"),
    "eps_ttm": ("epsTTM", "epsAnnual"),
    "eps_growth_ttm_yoy_pct": ("epsGrowthTTMYoy", "epsGrowth3Y", "epsGrowth5Y"),
    "revenue_growth_ttm_yoy_pct": ("revenueGrowthTTMYoy", "revenueGrowth3Y", "revenueGrowth5Y"),
    "net_profit_margin_pct": ("netProfitMarginTTM", "netProfitMarginAnnual"),
    "operating_margin_pct": ("operatingMarginTTM", "operatingMarginAnnual"),
    "roe_pct": ("roeTTM", "roeRfy", "roe5Y"),
    "debt_to_equity": ("totalDebt/totalEquityQuarterly", "totalDebt/totalEquityAnnual"),
    # Finnhub's free "basic financials" tier has no raw free-cash-flow
    # dollar figure - price-to-FCF-per-share is the closest available,
    # non-fabricated FCF-related multiple.
    "price_to_fcf": ("pfcfShareTTM", "pfcfShareAnnual"),
}

# analyst price targets require a Finnhub endpoint (price_target) that
# returned HTTP 403 (premium-only) on this project's Finnhub plan when
# checked during Phase 8 - documented as a known limitation rather than
# spending API calls on a endpoint already confirmed unavailable.
ANALYST_TARGET_UNAVAILABLE_REASON = "requires a Finnhub plan tier not available on this account (price_target endpoint returns 403)"


@dataclass
class FundamentalMetrics:
    ticker: str
    ok: bool
    reason: Optional[str] = None
    as_of: Optional[str] = None
    values: Dict[str, float] = field(default_factory=dict)   # display fields actually available
    raw: Dict[str, float] = field(default_factory=dict)       # every stored Finnhub metric
    earnings_date: Optional[str] = None                        # 'YYYY-MM-DD', if known
    days_until_earnings: Optional[int] = None
    analyst_target: Optional[float] = None
    analyst_target_unavailable_reason: Optional[str] = ANALYST_TARGET_UNAVAILABLE_REASON


def load_fundamental_metrics(conn, ticker: str) -> FundamentalMetrics:
    rows = conn.execute("SELECT metric, value, as_of FROM fundamentals WHERE ticker = ?", (ticker,)).fetchall()
    if not rows:
        return FundamentalMetrics(ticker=ticker, ok=False, reason="no fundamentals data stored for this ticker yet")

    raw = {r["metric"]: r["value"] for r in rows}
    as_of = max((r["as_of"] for r in rows if r["as_of"]), default=None)

    values = {}
    for display_field, candidates in DISPLAY_METRIC_FALLBACKS.items():
        for key in candidates:
            if key in raw:
                values[display_field] = raw[key]
                break

    earnings_date = None
    days_until_earnings = None
    epoch = raw.get("nextEarningsDateEpoch")
    if epoch is not None:
        earnings_dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        earnings_date = earnings_dt.strftime("%Y-%m-%d")
        days_until_earnings = (earnings_dt.date() - datetime.now(timezone.utc).date()).days

    return FundamentalMetrics(
        ticker=ticker, ok=True, as_of=as_of, values=values, raw=raw,
        earnings_date=earnings_date, days_until_earnings=days_until_earnings,
    )


def load_fundamental_metrics_cohort(conn, tickers: List[str]) -> Dict[str, FundamentalMetrics]:
    return {ticker: load_fundamental_metrics(conn, ticker) for ticker in tickers}


@dataclass
class FundamentalScore:
    ticker: str
    ok: bool
    reason: Optional[str] = None
    score: Optional[float] = None
    category_scores: Dict[str, Optional[float]] = field(default_factory=dict)
    category_weights_applied: Dict[str, float] = field(default_factory=dict)


def percentile_ranks(values_by_ticker: Dict[str, float], higher_is_better: bool) -> Dict[str, float]:
    """0-100 percentile rank of each ticker's value among the given cohort.
    A single-ticker cohort ranks itself as neutral (50) - there is nothing
    to rank it relative to. Ties share the same (average) rank."""
    tickers = list(values_by_ticker.keys())
    if len(tickers) <= 1:
        return {t: 50.0 for t in tickers}

    ordered = sorted(values_by_ticker.items(), key=lambda kv: kv[1])
    ranks: Dict[str, float] = {}
    i = 0
    n = len(ordered)
    while i < n:
        j = i
        while j < n and ordered[j][1] == ordered[i][1]:
            j += 1
        avg_rank = (i + j - 1) / 2.0
        pct = avg_rank / (n - 1) * 100.0
        for k in range(i, j):
            ranks[ordered[k][0]] = pct
        i = j

    if not higher_is_better:
        ranks = {t: 100.0 - pct for t, pct in ranks.items()}
    return ranks


def _category_score(
    metrics_by_ticker: Dict[str, FundamentalMetrics],
    metric_groups: Tuple[Tuple[str, ...], ...],
    higher_is_better: bool,
) -> Dict[str, Optional[float]]:
    """Average, per ticker, of the percentile ranks across every metric
    group in this category for which that ticker has data. A ticker with no
    data in ANY group of this category gets None (excluded from the
    category, not scored as 0)."""
    per_ticker_group_scores: Dict[str, List[float]] = {t: [] for t in metrics_by_ticker}

    for group in metric_groups:
        values: Dict[str, float] = {}
        for ticker, metrics in metrics_by_ticker.items():
            if not metrics.ok:
                continue
            for key in group:
                if key in metrics.raw:
                    values[ticker] = metrics.raw[key]
                    break
        if not values:
            continue
        ranks = percentile_ranks(values, higher_is_better)
        for ticker, pct in ranks.items():
            per_ticker_group_scores[ticker].append(pct)

    return {
        ticker: (sum(scores) / len(scores) if scores else None)
        for ticker, scores in per_ticker_group_scores.items()
    }


def compute_fundamental_scores_cohort(
    metrics_by_ticker: Dict[str, FundamentalMetrics],
    config: FundamentalScoreConfig = DEFAULT_FUNDAMENTAL_CONFIG,
) -> Dict[str, FundamentalScore]:
    """Cohort-relative 0-100 fundamental score for every ticker in
    `metrics_by_ticker`. A ticker with no fundamentals at all gets
    ok=False. A ticker with partial data gets a score computed only from
    the categories it has data for, with weights renormalized across those
    categories (never fabricates a value for a missing category)."""
    valuation = _category_score(metrics_by_ticker, config.valuation_metrics, higher_is_better=False)
    growth = _category_score(metrics_by_ticker, config.growth_metrics, higher_is_better=True)
    profitability = _category_score(metrics_by_ticker, config.profitability_metrics, higher_is_better=True)
    leverage = _category_score(metrics_by_ticker, config.leverage_metrics, higher_is_better=False)

    categories = {
        "valuation": (valuation, config.valuation_weight),
        "growth": (growth, config.growth_weight),
        "profitability": (profitability, config.profitability_weight),
        "leverage": (leverage, config.leverage_weight),
    }

    results: Dict[str, FundamentalScore] = {}
    for ticker, metrics in metrics_by_ticker.items():
        if not metrics.ok:
            results[ticker] = FundamentalScore(ticker=ticker, ok=False, reason=metrics.reason)
            continue

        category_scores = {name: scores.get(ticker) for name, (scores, _weight) in categories.items()}
        available = {name: (category_scores[name], weight) for name, (_scores, weight) in categories.items() if category_scores[name] is not None}

        if not available:
            results[ticker] = FundamentalScore(
                ticker=ticker, ok=False, reason="no scoreable fundamental metrics available for this ticker",
                category_scores=category_scores,
            )
            continue

        total_weight = sum(weight for _score, weight in available.values())
        weighted = sum(score * weight for score, weight in available.values()) / total_weight
        weights_applied = {name: weight / total_weight * 100.0 for name, (_score, weight) in available.items()}

        results[ticker] = FundamentalScore(
            ticker=ticker, ok=True, score=round(weighted, 1),
            category_scores=category_scores, category_weights_applied=weights_applied,
        )

    return results
