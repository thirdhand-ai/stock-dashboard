"""Analytics for real_holdings - current price, market value, unrealized
gain/loss, and % of portfolio for actual (non-simulated) positions. Reuses
db/price_repository.py's load_price_history/resolve_source directly, the
same price data every other view in this codebase reads - no separate
price-fetch logic.

Never fabricates a number (same rule trading/portfolio.py documents): a
holding with unknown shares or cost_basis_total (needs_manual_entry) gets
None for every derived field rather than a guessed value, and a ticker
with no stored price history gets current_price=None rather than a stale
or zero placeholder.
"""
from dataclasses import dataclass
from typing import List, Optional

from db.price_repository import load_price_history, resolve_source
from db.real_holdings_repository import RealHolding, list_real_holdings


@dataclass
class RealHoldingView:
    ticker: str
    owner: Optional[str]
    shares: Optional[float]
    cost_basis_total: Optional[float]
    cost_basis_per_share: Optional[float]
    current_price: Optional[float]
    price_as_of: Optional[str]
    source: Optional[str]
    market_value: Optional[float]
    unrealized_pl: Optional[float]
    unrealized_pl_pct: Optional[float]
    realized_gain: float
    weight_pct: Optional[float]
    needs_manual_entry: bool
    note: Optional[str]


def _price_holding(conn, h: RealHolding) -> RealHoldingView:
    current_price = price_as_of = source = None
    df = load_price_history(conn, h.ticker)
    if not df.empty:
        latest = df.iloc[-1]
        current_price = float(latest["close"])
        price_as_of = str(latest["date"])
        source = resolve_source(conn, h.ticker)

    market_value = None
    if current_price is not None and h.shares is not None:
        market_value = current_price * h.shares

    cost_basis_per_share = None
    if h.cost_basis_total is not None and h.shares:
        cost_basis_per_share = h.cost_basis_total / h.shares

    unrealized_pl = unrealized_pl_pct = None
    if market_value is not None and h.cost_basis_total is not None:
        unrealized_pl = market_value - h.cost_basis_total
        if h.cost_basis_total:
            unrealized_pl_pct = (unrealized_pl / h.cost_basis_total) * 100

    return RealHoldingView(
        ticker=h.ticker, owner=h.owner, shares=h.shares, cost_basis_total=h.cost_basis_total,
        cost_basis_per_share=cost_basis_per_share, current_price=current_price, price_as_of=price_as_of,
        source=source, market_value=market_value, unrealized_pl=unrealized_pl,
        unrealized_pl_pct=unrealized_pl_pct, realized_gain=h.realized_gain, weight_pct=None,
        needs_manual_entry=h.needs_manual_entry, note=h.note,
    )


def build_real_holdings_view(conn) -> List[RealHoldingView]:
    """One row per tracked ticker, priced against the local `prices` table,
    with weight_pct filled in as a second pass once every position's
    market_value is known (needs the portfolio total first)."""
    holdings = list_real_holdings(conn)
    views = [_price_holding(conn, h) for h in holdings]

    total_market_value = sum(v.market_value for v in views if v.market_value is not None)
    if total_market_value:
        for v in views:
            if v.market_value is not None:
                v.weight_pct = (v.market_value / total_market_value) * 100

    return views


def portfolio_totals(views: List[RealHoldingView]) -> dict:
    """Sums across every priced position - positions missing a
    market_value/cost_basis (needs_manual_entry) simply don't contribute,
    same as they're excluded from weight_pct above."""
    return {
        "total_market_value": sum(v.market_value for v in views if v.market_value is not None),
        "total_cost_basis": sum(v.cost_basis_total for v in views if v.cost_basis_total is not None),
        "total_unrealized_pl": sum(v.unrealized_pl for v in views if v.unrealized_pl is not None),
        "total_realized_gain": sum(v.realized_gain for v in views),
        "positions_needing_manual_entry": sum(1 for v in views if v.needs_manual_entry),
    }
