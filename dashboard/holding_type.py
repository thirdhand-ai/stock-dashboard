"""Holding-type classification: labels every ticker touched anywhere on the
dashboard as Real Holdings (an actual owned position - db/
real_holdings_repository.py), Watchlist (config.settings.WATCHLIST - no
ownership), or Exploratory (config.settings.EXPLORATORY_WATCHLIST -
speculative research/AI tickers, no ownership). Purely a display/grouping
label reusing existing ticker lists and the real_holdings table - computes
no new signal or alert condition, and changes no ticker's membership in
any of those three sources.

Precedence when a ticker sits in more than one source (e.g. META, MSFT, and
NVDA are both real holdings AND core WATCHLIST tickers - see
config/settings.py): Real Holdings wins. Ownership is what makes an alert
real-money-relevant, so a ticker that's actually held is always labeled
Real Holdings even when it's also on the watchlist.
"""
from typing import Dict, Iterable, Optional, Set

from config.settings import EXPLORATORY_WATCHLIST, WATCHLIST

HOLDING_TYPE_REAL = "Real Holdings"
HOLDING_TYPE_WATCHLIST = "Watchlist"
HOLDING_TYPE_EXPLORATORY = "Exploratory"
HOLDING_TYPE_OTHER = "Other"

# Display order used everywhere this shows up as a filter/grouping option.
HOLDING_TYPE_ORDER = [HOLDING_TYPE_REAL, HOLDING_TYPE_WATCHLIST, HOLDING_TYPE_EXPLORATORY, HOLDING_TYPE_OTHER]

_HOLDING_TYPE_ICONS = {
    HOLDING_TYPE_REAL: "🏦",
    HOLDING_TYPE_WATCHLIST: "👁️",
    HOLDING_TYPE_EXPLORATORY: "🧪",
    HOLDING_TYPE_OTHER: "❔",
}

_WATCHLIST_SET = set(WATCHLIST)
_EXPLORATORY_SET = set(EXPLORATORY_WATCHLIST)


def classify_ticker(ticker: str, real_holding_tickers: Set[str]) -> str:
    """A ticker's holding type. `real_holding_tickers` must be the live set
    from db/real_holdings_repository.py (not config.settings.
    REAL_HOLDINGS_WITH_SIGNAL_COVERAGE, which is only the subset that also
    gets signal-engine coverage) - a real position with no signal coverage
    (e.g. NOW, STN) must still classify as Real Holdings."""
    if ticker in real_holding_tickers:
        return HOLDING_TYPE_REAL
    if ticker in _WATCHLIST_SET:
        return HOLDING_TYPE_WATCHLIST
    if ticker in _EXPLORATORY_SET:
        return HOLDING_TYPE_EXPLORATORY
    return HOLDING_TYPE_OTHER


def build_holding_type_map(real_holding_tickers: Iterable[str]) -> Dict[str, str]:
    """{ticker: holding_type} for every ticker across all three sources."""
    real_set = set(real_holding_tickers)
    all_tickers = real_set | _WATCHLIST_SET | _EXPLORATORY_SET
    return {ticker: classify_ticker(ticker, real_set) for ticker in all_tickers}


def holding_type_for(ticker: Optional[str], type_map: Dict[str, str]) -> Optional[str]:
    """Look up a ticker's holding type from a prebuilt map (see
    build_holding_type_map / dashboard.data.get_holding_type_map). A None
    ticker (an activity row with no associated ticker at all, e.g. a Daily
    Digest or Operational Failure entry) stays None. A ticker present but
    NOT in the map (an alert configured on something outside all three
    tracked lists, e.g. SPY) falls back to Other rather than being
    mistaken for a ticker-less row."""
    if ticker is None:
        return None
    return type_map.get(ticker, HOLDING_TYPE_OTHER)


def holding_type_label(holding_type: Optional[str]) -> str:
    """Icon-prefixed display label, e.g. '🏦 Real Holdings'. None (an
    activity row with no associated ticker, like a Daily Digest or
    Operational Failure entry) renders as a plain em dash."""
    if holding_type is None:
        return "—"
    icon = _HOLDING_TYPE_ICONS.get(holding_type, "")
    return f"{icon} {holding_type}".strip()
