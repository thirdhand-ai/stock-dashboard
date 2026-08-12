"""Reconcile locally persisted paper_orders against Alpaca's authoritative
order state. Never invents fill information - every status/filled_qty/
filled_avg_price value written here comes directly from Alpaca's own order
record, never assumed from the fact that an order was merely submitted.

Read-only against Alpaca (get_order_by_id), so this is always safe to run,
including at the start of a --dry-run cycle - it lets a dry-run reflect
fills that happened since the last --paper-send run.
"""
import logging

from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus

from db.trading_repository import get_orders_in_nonterminal_state, update_order_status

logger = logging.getLogger(__name__)

STATUS_PROPOSED = "proposed"
STATUS_REJECTED_BY_RISK = "rejected_by_risk"
STATUS_SUBMITTED = "submitted"
STATUS_ACCEPTED = "accepted"
STATUS_PARTIALLY_FILLED = "partially_filled"
STATUS_FILLED = "filled"
STATUS_CANCELED = "canceled"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"

# Alpaca has more granular statuses than we track locally; several distinct
# Alpaca states collapse onto the same local status (see Phase 7 spec's
# required status list) without losing meaning for this system's purposes.
_ALPACA_STATUS_MAP = {
    AlpacaOrderStatus.NEW: STATUS_ACCEPTED,
    AlpacaOrderStatus.PENDING_NEW: STATUS_ACCEPTED,
    AlpacaOrderStatus.ACCEPTED: STATUS_ACCEPTED,
    AlpacaOrderStatus.ACCEPTED_FOR_BIDDING: STATUS_ACCEPTED,
    AlpacaOrderStatus.PENDING_REVIEW: STATUS_ACCEPTED,
    AlpacaOrderStatus.CALCULATED: STATUS_ACCEPTED,
    AlpacaOrderStatus.HELD: STATUS_ACCEPTED,
    AlpacaOrderStatus.PENDING_CANCEL: STATUS_ACCEPTED,
    AlpacaOrderStatus.PENDING_REPLACE: STATUS_ACCEPTED,
    AlpacaOrderStatus.PARTIALLY_FILLED: STATUS_PARTIALLY_FILLED,
    AlpacaOrderStatus.FILLED: STATUS_FILLED,
    AlpacaOrderStatus.DONE_FOR_DAY: STATUS_CANCELED,
    AlpacaOrderStatus.CANCELED: STATUS_CANCELED,
    AlpacaOrderStatus.EXPIRED: STATUS_CANCELED,
    AlpacaOrderStatus.REPLACED: STATUS_CANCELED,
    AlpacaOrderStatus.STOPPED: STATUS_CANCELED,
    AlpacaOrderStatus.SUSPENDED: STATUS_CANCELED,
    AlpacaOrderStatus.REJECTED: STATUS_REJECTED,
}


def map_alpaca_status(alpaca_status) -> str:
    if alpaca_status in _ALPACA_STATUS_MAP:
        return _ALPACA_STATUS_MAP[alpaca_status]
    return str(getattr(alpaca_status, "value", alpaca_status))


def reconcile_order(conn, client, order_row):
    """Refresh one local order row from Alpaca's current order state.
    No-op if the order was never actually submitted (alpaca_order_id unset -
    e.g. a dry-run proposal or a risk rejection)."""
    alpaca_order_id = order_row["alpaca_order_id"]
    if not alpaca_order_id:
        return None

    try:
        alpaca_order = client.get_order_by_id(alpaca_order_id)
    except Exception as e:
        logger.error(
            "reconciliation failed for local order id=%s (alpaca order %s): %s",
            order_row["id"], alpaca_order_id, e,
        )
        return None

    status = map_alpaca_status(alpaca_order.status)
    filled_qty = float(alpaca_order.filled_qty) if alpaca_order.filled_qty is not None else None
    filled_avg_price = float(alpaca_order.filled_avg_price) if alpaca_order.filled_avg_price is not None else None
    filled_at = str(alpaca_order.filled_at) if alpaca_order.filled_at else None

    update_order_status(
        conn, order_row["id"], status=status,
        filled_qty=filled_qty, filled_avg_price=filled_avg_price, filled_at=filled_at,
    )
    return status


def reconcile_open_orders(conn, client):
    """Reconcile every locally tracked order still in a non-terminal state
    (submitted/accepted/partially_filled). Always read-only against Alpaca."""
    rows = get_orders_in_nonterminal_state(conn)
    results = {}
    for row in rows:
        results[row["client_order_id"]] = reconcile_order(conn, client, row)
    return results
