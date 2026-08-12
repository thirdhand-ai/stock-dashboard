"""Alpaca trading-client access for Phase 7 - PAPER TRADING ONLY.

Reuses ingestion/alpaca_source.get_trading_client() (which already hardcodes
paper=True from config.settings.ALPACA_PAPER) rather than constructing a
second TradingClient path. This module adds one thing ingestion/alpaca_source
deliberately does not: the ability to actually submit orders - but every
write path here is gated by verify_paper_environment(), which must be called
(and pass) before any order is ever submitted (see trading/engine.py).

There is no live/production client constructor anywhere in this module, and
no parameter that could select one - "paper" is not a runtime choice here.
"""
import logging

from alpaca.common.enums import BaseURL
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from ingestion.alpaca_source import get_trading_client

logger = logging.getLogger(__name__)

# Paper account numbers issued by Alpaca are conventionally prefixed "PA";
# live account numbers are not. This is an independent, server-returned
# signal (not just our own paper=True config echoed back), used as a second,
# fail-closed check alongside the client's own configured base URL.
_PAPER_ACCOUNT_NUMBER_PREFIX = "PA"


class PaperEnvironmentUnconfirmedError(Exception):
    """Raised when the Alpaca account/environment cannot be programmatically
    confirmed as paper trading. Callers must treat this as fail-closed: no
    order may be submitted while this exception would be raised."""


def get_client():
    """A TradingClient constructed exactly as ingestion/alpaca_source does
    (paper=True, from the same credentials) - the one and only trading
    client constructor used anywhere in trading/."""
    return get_trading_client()


def verify_paper_environment(client=None):
    """Fail-closed confirmation that `client` is talking to Alpaca's paper
    endpoint against a real paper account. Raises PaperEnvironmentUnconfirmedError
    if either signal is missing or inconsistent - never assumes paper by
    default. Returns the fetched TradeAccount on success (callers that also
    need account fields can reuse it instead of fetching twice)."""
    client = client or get_client()

    base_url = getattr(client, "_base_url", None)
    if base_url != BaseURL.TRADING_PAPER:
        raise PaperEnvironmentUnconfirmedError(
            f"trading client is not configured against the paper endpoint (base_url={base_url!r})"
        )

    try:
        account = client.get_account()
    except Exception as e:
        raise PaperEnvironmentUnconfirmedError(f"could not fetch Alpaca account to confirm paper environment: {e}") from e

    account_number = str(getattr(account, "account_number", "") or "")
    if not account_number.startswith(_PAPER_ACCOUNT_NUMBER_PREFIX):
        raise PaperEnvironmentUnconfirmedError(
            f"Alpaca account number does not look like a paper account (got {account_number!r}); refusing to proceed"
        )

    status_obj = getattr(account, "status", None)
    status = getattr(status_obj, "value", str(status_obj))
    if status.upper() != "ACTIVE":
        raise PaperEnvironmentUnconfirmedError(f"Alpaca paper account is not ACTIVE (status={status!r})")

    logger.info("paper environment confirmed: account ...%s, status=%s", account_number[-4:], status)
    return account


def get_account(client=None):
    client = client or get_client()
    return client.get_account()


def get_positions(client=None):
    """All currently open Alpaca paper positions (read-only)."""
    client = client or get_client()
    return client.get_all_positions()


def get_open_orders(client=None):
    """All open/pending Alpaca paper orders (read-only) - new, accepted,
    partially_filled, and other non-terminal states Alpaca itself considers
    'open'."""
    client = client or get_client()
    return client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))


def get_order_by_id(order_id, client=None):
    client = client or get_client()
    return client.get_order_by_id(order_id)
