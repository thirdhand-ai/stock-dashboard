"""Order construction and (only when trading/engine.py is run in
--paper-send mode) submission to Alpaca's PAPER endpoint.

This module has no concept of "dry-run" - the mode gate lives entirely in
trading/engine.py as a single, auditable `if mode == MODE_PAPER_SEND:`
branch that is the only place submit_order() is ever called. Keeping the
gate out of this module means there is no flag here that could accidentally
widen what --dry-run does.
"""
import logging
from typing import Optional

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from trading.config import DEFAULT_EXECUTION_CONFIG, ExecutionConfig
from trading.signals_bridge import INTENT_ENTRY, TradeCandidate

logger = logging.getLogger(__name__)

_TIME_IN_FORCE = {"day": TimeInForce.DAY}


def build_order_request(
    candidate: TradeCandidate,
    client_order_id: str,
    sized_notional: Optional[float] = None,
    sized_qty: Optional[float] = None,
    execution_config: ExecutionConfig = DEFAULT_EXECUTION_CONFIG,
) -> MarketOrderRequest:
    """Entries are notional (dollar-sized) market orders; exits are qty
    market orders that close the exact Alpaca-reported position size (see
    trading/config.py's ExecutionConfig docstring for why)."""
    side = OrderSide.BUY if candidate.side == "buy" else OrderSide.SELL
    tif = _TIME_IN_FORCE.get(execution_config.time_in_force, TimeInForce.DAY)

    if candidate.intent == INTENT_ENTRY:
        if not sized_notional or sized_notional <= 0:
            raise ValueError(f"entry order for {candidate.ticker} requires a positive sized_notional")
        return MarketOrderRequest(
            symbol=candidate.ticker,
            notional=round(sized_notional, 2),
            side=side,
            time_in_force=tif,
            client_order_id=client_order_id,
        )

    if not sized_qty or sized_qty <= 0:
        raise ValueError(f"exit order for {candidate.ticker} requires a positive sized_qty")
    return MarketOrderRequest(
        symbol=candidate.ticker,
        qty=sized_qty,
        side=side,
        time_in_force=tif,
        client_order_id=client_order_id,
    )


def submit_order(client, order_request: MarketOrderRequest):
    """Submit one order to Alpaca's PAPER endpoint."""
    logger.info("submitting PAPER order: %s", order_request)
    return client.submit_order(order_data=order_request)
