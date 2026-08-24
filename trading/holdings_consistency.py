"""Data-consistency checks for real_holdings positions that also have a
lot/dividend transaction ledger (real_holding_lots, dividend_payments) - a
DIFFERENT class of problem than trading/data_completeness.py's gaps. That
module flags data that's simply MISSING (needs_manual_entry, an unknown
share/cost/date, an undated DRIP caveat) - "this value doesn't exist yet".
This module flags data that EXISTS on both sides but has drifted out of
sync with itself: real_holdings.shares/cost_basis_total are manually-
maintained aggregate fields (db/real_holdings_repository.py), and for a
holding that also has a transaction-level ledger, those aggregates should
always equal what the ledger sums to. If they don't, either the aggregate
wasn't updated after a lot/dividend edit, or a lot/dividend row itself has
a typo - "this value exists on both sides, but they disagree".

Only checks (ticker, owner) pairs with at least one real_holding_lots row
or at least one dividend_payments row - a holding with neither (most of
them: a single manually-entered aggregate with no underlying ledger, e.g.
NVDA/MSFT/XLV/STN/NOW today) has nothing to reconcile against and is
silently skipped, never flagged as a mismatch.
"""
from dataclasses import dataclass
from typing import List

from db.dividend_payments_repository import DividendPayment, list_dividend_payments
from db.real_holding_lots_repository import RealHoldingLot, list_real_holding_lots
from db.real_holdings_repository import RealHolding, list_real_holdings

# Both sides being compared are sums of many float additions (a
# reinvestment's shares are total_received / amount_per_share, computed
# fresh here rather than stored) - a hundredth of a share or a cent of
# pure float-accumulation drift is not a real mismatch worth flagging.
SHARE_TOLERANCE = 0.01
COST_TOLERANCE = 0.01


@dataclass(frozen=True)
class ConsistencyMismatch:
    ticker: str
    owner: str
    field: str  # "shares" or "cost_basis_total"
    recorded: float
    reconstructed: float
    detail: str


def _reconstructed_shares(lots: List[RealHoldingLot], payments: List[DividendPayment]) -> float:
    total = sum(lot.shares for lot in lots)
    total += sum(
        p.total_received / p.amount_per_share for p in payments if p.reinvested and p.amount_per_share
    )
    return total


def _reconstructed_cost_basis(lots: List[RealHoldingLot], payments: List[DividendPayment]) -> float:
    """Lot costs plus every reinvested dividend's dollar amount - a
    reinvestment IS a cost-basis-increasing event (it bought new shares
    with that money), same accounting this session used to set HPI/KMI's
    cost_basis_total in the first place. A dividend paid in cash
    (reinvested=False) never bought shares, so it never contributes here."""
    total = sum(lot.total_cost for lot in lots)
    total += sum(p.total_received for p in payments if p.reinvested)
    return total


def check_holding_consistency(conn, holding: RealHolding) -> List[ConsistencyMismatch]:
    """[] for a holding with no lot/dividend ledger to check against, or
    whose ledger fully agrees with real_holdings' aggregate fields (within
    tolerance); one ConsistencyMismatch per disagreeing field otherwise.
    shares/cost_basis_total being None (needs_manual_entry) skips that
    field's check - there's nothing recorded to compare the ledger against."""
    lots = list_real_holding_lots(conn, ticker=holding.ticker, owner=holding.owner)
    payments = list_dividend_payments(conn, ticker=holding.ticker, owner=holding.owner)
    if not lots and not payments:
        return []

    mismatches: List[ConsistencyMismatch] = []

    if holding.shares is not None:
        reconstructed_shares = _reconstructed_shares(lots, payments)
        if abs(reconstructed_shares - holding.shares) > SHARE_TOLERANCE:
            mismatches.append(ConsistencyMismatch(
                ticker=holding.ticker, owner=holding.owner, field="shares",
                recorded=holding.shares, reconstructed=reconstructed_shares,
                detail=(
                    f"Lots + reinvested dividend shares sum to {reconstructed_shares:g}, but "
                    f"real_holdings.shares is {holding.shares:g}."
                ),
            ))

    if holding.cost_basis_total is not None:
        reconstructed_cost = _reconstructed_cost_basis(lots, payments)
        if abs(reconstructed_cost - holding.cost_basis_total) > COST_TOLERANCE:
            mismatches.append(ConsistencyMismatch(
                ticker=holding.ticker, owner=holding.owner, field="cost_basis_total",
                recorded=holding.cost_basis_total, reconstructed=reconstructed_cost,
                detail=(
                    f"Lot costs + reinvested dividend dollars sum to ${reconstructed_cost:,.2f}, but "
                    f"real_holdings.cost_basis_total is ${holding.cost_basis_total:,.2f}."
                ),
            ))

    return mismatches


def check_all_holdings_consistency(conn) -> List[ConsistencyMismatch]:
    """Every mismatch across every tracked real_holdings position -
    ticker-ascending (then owner), same iteration order list_real_holdings
    already returns."""
    mismatches: List[ConsistencyMismatch] = []
    for holding in list_real_holdings(conn):
        mismatches.extend(check_holding_consistency(conn, holding))
    return mismatches
