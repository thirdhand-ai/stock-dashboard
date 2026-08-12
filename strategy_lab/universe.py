"""Research universe for Phase 9 large-sample validation.

Deliberately a hardcoded, version-controlled list rather than a live scrape
of index membership - reproducible across runs (see Phase 9 spec item 1).
Curated as of 2026-08-12 as a stable set of large-cap, highly liquid U.S.
common stocks broadly representative of S&P 100-style mega/large-cap names
across sectors. Not a live/authoritative index membership feed; if used for
anything beyond this research study, re-verify current constituents.

This is intentionally a *separate* list from config.settings.WATCHLIST (the
production trading watchlist). The two overlap on the 7 production tickers
(they are legitimately part of this universe too) but nothing here mutates
or reads from WATCHLIST, and nothing in production code imports this module -
see tests/test_strategy_lab_safety.py for the isolation check.
"""

# Production watchlist tickers (config.settings.WATCHLIST) - included here
# because they are themselves liquid large-caps and should be covered by the
# same large-sample study, not excluded from it.
PRODUCTION_WATCHLIST_OVERLAP = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META"]

_ADDITIONAL_UNIVERSE = [
    # Technology / communication services
    "AVGO", "ORCL", "CRM", "ADBE", "CSCO", "ACN", "IBM", "INTC", "AMD", "QCOM",
    "TXN", "INTU", "NOW", "AMAT", "MU", "ADI", "LRCX", "PANW", "NFLX", "CMCSA",
    "T", "VZ", "TMUS", "DIS",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "AXP", "BLK", "SCHW", "SPGI", "USB", "PNC", "COF",
    # Healthcare
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "BMY",
    "AMGN", "GILD", "MDT", "CVS", "CI", "ELV",
    # Consumer
    "WMT", "PG", "KO", "PEP", "COST", "HD", "MCD", "NKE", "SBUX", "TGT", "LOW", "BKNG", "TJX",
    # Industrials
    "BA", "CAT", "GE", "HON", "UPS", "RTX", "LMT", "DE", "MMM", "UNP",
    # Energy
    "XOM", "CVX", "COP", "SLB",
    # Payments / other mega-cap
    "V", "MA", "PYPL",
]

# The full research universe: production-overlap tickers first (stable
# ordering matters for reproducibility of any downstream indexing/reporting).
RESEARCH_UNIVERSE = list(dict.fromkeys(PRODUCTION_WATCHLIST_OVERLAP + _ADDITIONAL_UNIVERSE))

# Benchmarks used throughout Phase 9 - not part of the "tested universe" for
# cross-sectional robustness purposes.
PRIMARY_BENCHMARK = "SPY"
SECONDARY_BENCHMARK = "QQQ"


def assert_isolated_from_watchlist():
    """Documents (and lets a test assert) that this module never imports or
    mutates config.settings.WATCHLIST - the two lists are maintained
    independently by design."""
    from config.settings import WATCHLIST
    return WATCHLIST == PRODUCTION_WATCHLIST_OVERLAP
