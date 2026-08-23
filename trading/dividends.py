"""Analytics for dividend_payments - trailing-12-month yield on cost and
running totals per real_holdings position. Reuses db/real_holdings_repository.py's
list_real_holdings directly (cost basis lives there, not duplicated here),
same no-separate-source-of-truth convention trading/real_holdings.py
follows for price data.

Never fabricates a number: a holding with unknown cost_basis_total
(needs_manual_entry) gets None for ttm_yield_on_cost_pct rather than a
guessed figure, same rule trading/real_holdings.py documents for
unrealized P&L.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from db.dividend_payments_repository import DividendPayment, list_dividend_payments
from db.real_holdings_repository import RealHolding, list_real_holdings

TTM_WINDOW_DAYS = 365


@dataclass
class DividendHoldingSummary:
    ticker: str
    owner: str
    cost_basis_total: Optional[float]
    ttm_received: float
    ttm_yield_on_cost_pct: Optional[float]
    all_time_received: float
    payment_count: int
    last_payment_date: Optional[str]


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _summarize_one(holding: RealHolding, payments: List[DividendPayment], as_of: date) -> DividendHoldingSummary:
    cutoff = as_of - timedelta(days=TTM_WINDOW_DAYS)
    ttm_payments = [p for p in payments if cutoff <= _parse_date(p.pay_date) <= as_of]

    ttm_received = sum(p.total_received for p in ttm_payments)
    all_time_received = sum(p.total_received for p in payments)

    ttm_yield_on_cost_pct = None
    if holding.cost_basis_total:
        ttm_yield_on_cost_pct = (ttm_received / holding.cost_basis_total) * 100

    last_payment_date = max((p.pay_date for p in payments), default=None)

    return DividendHoldingSummary(
        ticker=holding.ticker, owner=holding.owner, cost_basis_total=holding.cost_basis_total,
        ttm_received=ttm_received, ttm_yield_on_cost_pct=ttm_yield_on_cost_pct,
        all_time_received=all_time_received, payment_count=len(payments), last_payment_date=last_payment_date,
    )


def build_dividend_summary(conn, as_of: Optional[date] = None) -> List[DividendHoldingSummary]:
    """One row per real_holdings position (ticker, owner) that has at least
    one recorded dividend payment - a holding that has never paid (or
    hasn't had a payment entered yet) simply doesn't appear, same "absence
    means not evaluated" contract price_alert_config uses. `as_of` is
    injectable for tests; defaults to the real current date."""
    as_of = as_of or date.today()
    holdings = list_real_holdings(conn)
    summaries = []
    for h in holdings:
        payments = list_dividend_payments(conn, ticker=h.ticker, owner=h.owner)
        if not payments:
            continue
        summaries.append(_summarize_one(h, payments, as_of))
    return summaries


def portfolio_dividend_totals(summaries: List[DividendHoldingSummary]) -> dict:
    """Aggregate dividend income across every summarized holding."""
    return {
        "total_ttm_received": sum(s.ttm_received for s in summaries),
        "total_all_time_received": sum(s.all_time_received for s in summaries),
    }


def dividend_totals_by_owner(summaries: List[DividendHoldingSummary]) -> Dict[str, dict]:
    """portfolio_dividend_totals, grouped by owner - same pattern as
    trading/real_holdings.py::subtotals_by_owner."""
    owners = sorted({s.owner or "" for s in summaries})
    return {owner: portfolio_dividend_totals([s for s in summaries if (s.owner or "") == owner]) for owner in owners}
