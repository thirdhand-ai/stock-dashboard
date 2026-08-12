"""Configurable, explicitly-labeled research execution assumptions for
event-based backtests (Phase 9 spec item 7).

This is research-only friction modeling for measuring historical signal
quality - it has no relationship to and never touches trading/orders.py or
trading/engine.py's actual paper-order construction/submission.

Reuses backtest.config.DEFAULT_EXECUTION.commission (0.1%) as the reasonable
-friction commission assumption rather than inventing a new number, since
that's already the project's documented friction assumption for
backtest/runner.py's whole-history backtests.
"""
from dataclasses import dataclass

from backtest.config import DEFAULT_EXECUTION


@dataclass(frozen=True)
class ResearchExecutionAssumptions:
    label: str
    slippage_bps: float          # one-way, applied to both entry and exit
    commission_pct: float        # one-way, applied to both entry and exit
    next_session_entry: bool = True  # a signal from date T's completed bar enters no earlier than T+1


IDEALIZED = ResearchExecutionAssumptions(
    label="idealized (no friction)", slippage_bps=0.0, commission_pct=0.0, next_session_entry=True,
)
REASONABLE = ResearchExecutionAssumptions(
    label="reasonable friction", slippage_bps=5.0, commission_pct=DEFAULT_EXECUTION.commission, next_session_entry=True,
)
ASSUMPTION_SETS = {"idealized": IDEALIZED, "reasonable": REASONABLE}


def round_trip_net_return(gross_return: float, assumptions: ResearchExecutionAssumptions) -> float:
    """Apply one-way slippage+commission at both entry and exit to a gross
    price return. Multiplicative, matching how a real fill would compound
    cost on both legs of the trade."""
    one_way_cost = (assumptions.slippage_bps / 10_000.0) + assumptions.commission_pct
    entry_price_factor = 1.0 + one_way_cost   # you pay slightly more to buy
    exit_price_factor = 1.0 - one_way_cost    # you receive slightly less to sell
    gross_price_factor = 1.0 + gross_return
    net_price_factor = (gross_price_factor * exit_price_factor) / entry_price_factor
    return net_price_factor - 1.0
