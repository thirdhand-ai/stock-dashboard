"""Deterministic client-order identifiers for Phase 7 paper orders.

One client_order_id per ticker+intent+day: a repeated evaluation of
unchanged data on the same day always recomputes the same id, so the
paper_orders.client_order_id UNIQUE constraint (db/trading_repository.py)
turns "run this twice" into an update-in-place rather than a duplicate
order - this is the core of the idempotency guarantee, not just a naming
convention.
"""
from datetime import date as date_cls
from typing import Optional

from trading.config import DEFAULT_EXECUTION_CONFIG, ExecutionConfig


def build_client_order_id(
    ticker: str,
    intent: str,
    as_of: Optional[date_cls] = None,
    execution_config: ExecutionConfig = DEFAULT_EXECUTION_CONFIG,
) -> str:
    as_of = as_of or date_cls.today()
    return f"{execution_config.client_order_id_prefix}-{ticker.lower()}-{intent}-{as_of.isoformat()}"
