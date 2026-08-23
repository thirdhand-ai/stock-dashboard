"""Data Completeness aggregation - a single, read-only summary of every
gap ALREADY flagged elsewhere in this system: real_holdings.needs_manual_entry,
missing shares/cost_basis_total, real_holdings.share_history_caveat, and
realized_sales rows with an unknown purchase_date/sale_date. Invents no
new flag types - this module only re-reads the exact same nullable
columns/flags each source table already defines and that other pages
(Real Holdings, Portfolio Performance, Realized Gains) already surface
individually; it just collects them in one place.

dividend_payments has no nullable "unknown" field at all (pay_date,
amount_per_share, total_received are all NOT NULL - see db/
dividend_payments_schema.py) - scan_dividend_gaps always returns []
today. Kept as its own function (not omitted) so this page's three-table
scope stays honest even when one table currently has nothing to report -
a future schema change there would only need to fill in this one
function, not restructure the page.
"""
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List

from db.real_holdings_repository import list_real_holdings
from db.realized_sales_repository import list_realized_sales

REAL_HOLDINGS_PAGE = "Real Holdings"
PORTFOLIO_PERFORMANCE_PAGE = "Portfolio Performance"
REALIZED_GAINS_PAGE = "Realized Gains"


@dataclass(frozen=True)
class CompletenessGap:
    category: str
    ticker: str
    owner: str
    detail: str
    fix_page: str


def _display_owner(owner) -> str:
    return owner if owner else "Unassigned"


def scan_real_holdings_gaps(conn) -> List[CompletenessGap]:
    gaps = []
    for h in list_real_holdings(conn):
        owner = _display_owner(h.owner)

        if h.needs_manual_entry:
            gaps.append(CompletenessGap(
                category="Needs manual entry", ticker=h.ticker, owner=owner,
                detail="Shares and cost basis are not confirmed for this holding (needs_manual_entry).",
                fix_page=REAL_HOLDINGS_PAGE,
            ))
        else:
            # Independently nullable in the schema even when
            # needs_manual_entry is False - checked separately so a
            # partial gap (only one of the two missing) is never masked.
            if h.shares is None:
                gaps.append(CompletenessGap(
                    category="Unknown share count", ticker=h.ticker, owner=owner,
                    detail="Share count is not recorded for this holding.", fix_page=REAL_HOLDINGS_PAGE,
                ))
            if h.cost_basis_total is None:
                gaps.append(CompletenessGap(
                    category="Unknown cost basis", ticker=h.ticker, owner=owner,
                    detail="Cost basis is not recorded for this holding.", fix_page=REAL_HOLDINGS_PAGE,
                ))

        if h.share_history_caveat:
            gaps.append(CompletenessGap(
                category="Approximate share history (DRIP)", ticker=h.ticker, owner=owner,
                detail=h.share_history_caveat, fix_page=PORTFOLIO_PERFORMANCE_PAGE,
            ))
    return gaps


def scan_realized_sales_gaps(conn) -> List[CompletenessGap]:
    gaps = []
    for s in list_realized_sales(conn):
        missing = []
        if s.purchase_date is None:
            missing.append("purchase date")
        if s.sale_date is None:
            missing.append("sale date")
        if not missing:
            continue
        gaps.append(CompletenessGap(
            category="Missing sale date(s)", ticker=s.ticker, owner=_display_owner(s.owner),
            detail=(
                f"{' and '.join(missing).capitalize()} not tracked for this {s.shares_sold:g}-share "
                "sale - short/long-term classification and tax-year bucketing are unavailable for it."
            ),
            fix_page=REALIZED_GAINS_PAGE,
        ))
    return gaps


def scan_dividend_gaps(conn) -> List[CompletenessGap]:
    """Always [] today - see this module's docstring."""
    return []


@dataclass(frozen=True)
class DataCompletenessReport:
    gaps: List[CompletenessGap] = field(default_factory=list)

    @property
    def by_category(self) -> Dict[str, List[CompletenessGap]]:
        grouped: "OrderedDict[str, List[CompletenessGap]]" = OrderedDict()
        for g in self.gaps:
            grouped.setdefault(g.category, []).append(g)
        return grouped


def build_data_completeness_report(conn) -> DataCompletenessReport:
    gaps = scan_real_holdings_gaps(conn) + scan_realized_sales_gaps(conn) + scan_dividend_gaps(conn)
    return DataCompletenessReport(gaps=gaps)
