"""Realized Gains report analytics for real_holdings - per-transaction
detail (proceeds, cost basis of shares sold, realized gain/loss) plus
short/long-term classification and tax-year bucketing, backed by
db/realized_sales_repository.py.

`realized_gain` is always DERIVED here as proceeds - cost_basis_sold,
never stored - see db/realized_sales_schema.py's docstring for why this
table doesn't have its own realized_gain column.

Short/long-term and tax-year both require dates this system may not have
(checked what's stored for the existing META sale before building this -
neither real_holdings nor its free-text note names a purchase or sale
date). A transaction missing either date gets term=TERM_UNKNOWN and
tax_year=None rather than a guessed classification or a fabricated year -
same "never fabricate" rule every other real_holdings-derived module in
this codebase follows.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from db.realized_sales_repository import RealizedSale, list_realized_sales

# IRS rule: held MORE than one year = long-term. 365 days is the same
# boundary convention trading/dividends.py's TTM_WINDOW_DAYS uses.
LONG_TERM_THRESHOLD_DAYS = 365

TERM_SHORT = "Short-term"
TERM_LONG = "Long-term"
TERM_UNKNOWN = "Unknown - dates not tracked"

UNKNOWN_TAX_YEAR = "Unknown"


@dataclass
class RealizedGainRow:
    id: int
    ticker: str
    owner: str
    purchase_date: Optional[str]
    sale_date: Optional[str]
    shares_sold: float
    cost_basis_sold: float
    proceeds: float
    realized_gain: float
    holding_period_days: Optional[int]
    term: str
    tax_year: Optional[int]


def _parse_date(value: str):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _classify(sale: RealizedSale):
    tax_year = _parse_date(sale.sale_date).year if sale.sale_date else None

    if not sale.purchase_date or not sale.sale_date:
        return None, TERM_UNKNOWN, tax_year

    holding_period_days = (_parse_date(sale.sale_date) - _parse_date(sale.purchase_date)).days
    term = TERM_LONG if holding_period_days > LONG_TERM_THRESHOLD_DAYS else TERM_SHORT
    return holding_period_days, term, tax_year


def _to_row(sale: RealizedSale) -> RealizedGainRow:
    holding_period_days, term, tax_year = _classify(sale)
    return RealizedGainRow(
        id=sale.id, ticker=sale.ticker, owner=sale.owner, purchase_date=sale.purchase_date,
        sale_date=sale.sale_date, shares_sold=sale.shares_sold, cost_basis_sold=sale.cost_basis_sold,
        proceeds=sale.proceeds, realized_gain=sale.proceeds - sale.cost_basis_sold,
        holding_period_days=holding_period_days, term=term, tax_year=tax_year,
    )


def build_realized_gains_report(conn) -> List[RealizedGainRow]:
    return [_to_row(s) for s in list_realized_sales(conn)]


def _totals(rows: List[RealizedGainRow]) -> dict:
    return {
        "total_proceeds": sum(r.proceeds for r in rows),
        "total_cost_basis_sold": sum(r.cost_basis_sold for r in rows),
        "total_realized_gain": sum(r.realized_gain for r in rows),
        "transaction_count": len(rows),
    }


def totals_by_owner(rows: List[RealizedGainRow]) -> Dict[str, dict]:
    owners = sorted({r.owner or "" for r in rows})
    return {(owner or "Unassigned"): _totals([r for r in rows if (r.owner or "") == owner]) for owner in owners}


def totals_by_tax_year(rows: List[RealizedGainRow]) -> Dict[str, dict]:
    """Keyed by tax year as a string, "Unknown" bucket last - a sale with
    no sale_date can't be assigned to a tax year, and is never guessed
    into one."""
    def year_key(r):
        return str(r.tax_year) if r.tax_year is not None else UNKNOWN_TAX_YEAR

    years = sorted({year_key(r) for r in rows}, key=lambda y: (y == UNKNOWN_TAX_YEAR, y))
    return {year: _totals([r for r in rows if year_key(r) == year]) for year in years}


def to_csv_rows(rows: List[RealizedGainRow]) -> List[dict]:
    """Flat dict per transaction, column order matching the dashboard
    table - one shared shape so the on-screen table and the CSV export
    can never drift apart."""
    return [
        {
            "Ticker": r.ticker,
            "Owner": r.owner or "Unassigned",
            "Purchase date": r.purchase_date or "Unknown",
            "Sale date": r.sale_date or "Unknown",
            "Shares sold": r.shares_sold,
            "Cost basis of shares sold": r.cost_basis_sold,
            "Proceeds": r.proceeds,
            "Realized gain/loss": r.realized_gain,
            "Term": r.term,
            "Tax year": r.tax_year if r.tax_year is not None else UNKNOWN_TAX_YEAR,
        }
        for r in rows
    ]
