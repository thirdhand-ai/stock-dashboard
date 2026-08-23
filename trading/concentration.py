"""Portfolio diversification/concentration analytics for real_holdings -
position size as % of portfolio value, sector/industry allocation, and
purely descriptive over-threshold flags. Never a buy/sell signal or
investment advice - just surfacing the numbers (same disclaimer every
other real_holdings/paper_portfolio page carries).

Reuses trading/real_holdings.py's RealHoldingView directly (market value
already computed there against stored price history) - no separate
pricing logic. A position with unknown market_value (needs_manual_entry)
is excluded from every percentage here, same "never fabricate" rule
trading/real_holdings.py's weight_pct already follows - it would make
every other position's % of total misleading if included as an unpriced
zero.

Sector/industry classification comes from db/ticker_sector_repository.py
(cached Finnhub data, see that module's schema docstring) - ensure_sector_data
below fetches-and-caches only what's missing, since this is static
reference data that doesn't need refreshing on a schedule.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from db.ticker_sector_repository import list_ticker_sectors, upsert_ticker_sector
from trading.real_holdings import RealHoldingView

logger = logging.getLogger(__name__)

UNKNOWN_INDUSTRY = "Unknown"

# Thresholds are descriptive defaults, not a rule this module enforces -
# the dashboard just flags positions/sectors past these for the viewer to
# notice, same "surfacing the numbers, not advice" contract as every
# other real_holdings-derived page.
SINGLE_POSITION_THRESHOLD_PCT = 20.0  # given directly: ">20% of total value in one ticker"
SECTOR_THRESHOLD_PCT = 30.0  # "heavily weighted in one sector" - a materially higher bar than
# the single-position threshold, since a sector naturally aggregates multiple positions


def ensure_sector_data(conn, tickers: List[str]) -> None:
    """Fetch and cache industry classification for any ticker not already
    in ticker_sector. One-time lookup per ticker - never re-fetched once
    stored (see db/ticker_sector_schema.py's docstring for why). A failed
    lookup for one ticker never blocks the others; that ticker's industry
    is simply left unset (shown as Unknown), never guessed."""
    from ingestion.finnhub_source import fetch_company_industry

    existing = {s.ticker for s in list_ticker_sectors(conn)}
    for ticker in sorted(set(tickers) - existing):
        try:
            industry = fetch_company_industry(ticker)
        except Exception as e:
            logger.warning("concentration: failed to fetch industry for %s: %s", ticker, e)
            industry = None
        upsert_ticker_sector(conn, ticker, industry)


@dataclass
class ConcentrationPosition:
    ticker: str
    owner: str
    industry: str
    market_value: float
    weight_pct: float
    exceeds_threshold: bool


@dataclass
class SectorAllocation:
    industry: str
    market_value: float
    weight_pct: float
    tickers: List[str]
    exceeds_threshold: bool


@dataclass
class ConcentrationReport:
    scope_label: str
    total_market_value: float
    positions: List[ConcentrationPosition] = field(default_factory=list)
    sectors: List[SectorAllocation] = field(default_factory=list)
    excluded_tickers: List[str] = field(default_factory=list)  # needs_manual_entry / no market value

    @property
    def flagged_positions(self) -> List[ConcentrationPosition]:
        return [p for p in self.positions if p.exceeds_threshold]

    @property
    def flagged_sectors(self) -> List[SectorAllocation]:
        return [s for s in self.sectors if s.exceeds_threshold]


def build_concentration_report(
    views: List[RealHoldingView],
    sector_by_ticker: Dict[str, Optional[str]],
    scope_label: str,
    position_threshold_pct: float = SINGLE_POSITION_THRESHOLD_PCT,
    sector_threshold_pct: float = SECTOR_THRESHOLD_PCT,
) -> ConcentrationReport:
    priced = [v for v in views if v.market_value is not None]
    # Labeled with owner, not bare tickers - in the Combined scope, a
    # ticker held by more than one owner (e.g. NOW) can be a priced
    # position for one owner and excluded for another at the same time;
    # a bare ticker in this list would look like a contradiction against
    # that same ticker's row in the priced positions table above it.
    excluded = [f"{v.ticker} ({v.owner or 'Unassigned'})" for v in views if v.market_value is None]
    total = sum(v.market_value for v in priced)

    if not total:
        return ConcentrationReport(scope_label=scope_label, total_market_value=0.0, excluded_tickers=excluded)

    positions = []
    for v in priced:
        weight_pct = (v.market_value / total) * 100
        industry = sector_by_ticker.get(v.ticker) or UNKNOWN_INDUSTRY
        positions.append(ConcentrationPosition(
            ticker=v.ticker, owner=v.owner or "", industry=industry, market_value=v.market_value,
            weight_pct=weight_pct, exceeds_threshold=weight_pct > position_threshold_pct,
        ))
    positions.sort(key=lambda p: p.weight_pct, reverse=True)

    sector_totals: Dict[str, float] = {}
    sector_tickers: Dict[str, List[str]] = {}
    for p in positions:
        sector_totals[p.industry] = sector_totals.get(p.industry, 0.0) + p.market_value
        sector_tickers.setdefault(p.industry, []).append(p.ticker)

    sectors = []
    for industry, market_value in sector_totals.items():
        weight_pct = (market_value / total) * 100
        sectors.append(SectorAllocation(
            industry=industry, market_value=market_value, weight_pct=weight_pct,
            tickers=sorted(sector_tickers[industry]), exceeds_threshold=weight_pct > sector_threshold_pct,
        ))
    sectors.sort(key=lambda s: s.weight_pct, reverse=True)

    return ConcentrationReport(
        scope_label=scope_label, total_market_value=total, positions=positions,
        sectors=sectors, excluded_tickers=excluded,
    )


def build_concentration_reports(views: List[RealHoldingView], sector_by_ticker: Dict[str, Optional[str]]) -> Dict[str, ConcentrationReport]:
    """One report for the combined portfolio plus one per distinct owner -
    each owner's weight_pct is relative to THAT owner's own total, not the
    combined total, so Mom's and Tyler's concentration profiles are each
    self-contained (a position isn't "20% of the combined portfolio" vs.
    "90% of Tyler's tiny portfolio" conflated into one number)."""
    reports = {"Combined": build_concentration_report(views, sector_by_ticker, "Combined")}
    owners = sorted({v.owner or "" for v in views})
    for owner in owners:
        label = owner if owner else "Unassigned"
        owner_views = [v for v in views if (v.owner or "") == owner]
        reports[label] = build_concentration_report(owner_views, sector_by_ticker, label)
    return reports
