"""Phase 7 orchestration: one full paper-trading cycle -
verify paper environment -> reconcile existing orders -> build candidates
from current signals -> risk-check each -> persist the decision -> (only in
--paper-send mode) submit qualifying orders to Alpaca's PAPER endpoint.

Reuses every existing layer directly: signals.engine (via
trading/signals_bridge.py) for signal state, backtest.config.DEFAULT_RULES
for entry/exit gates, trading/risk.py for limits, trading/client.py for the
one paper-only TradingClient path. This module contains no trading-rule
logic of its own, only sequencing.
"""
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from config.settings import WATCHLIST
from db.trading_repository import (
    ALREADY_SUBMITTED_STATUSES,
    get_order_by_client_id,
    update_order_status,
    upsert_order,
)
from trading import client as trading_client
from trading.config import DEFAULT_EXECUTION_CONFIG, DEFAULT_RISK_CONFIG, ExecutionConfig, RiskConfig
from trading.idempotency import build_client_order_id
from trading.orders import build_order_request, submit_order
from trading.reconcile import (
    STATUS_ERROR,
    STATUS_PROPOSED,
    STATUS_REJECTED_BY_RISK,
    map_alpaca_status,
    reconcile_open_orders,
)
from trading.risk import RiskCheckResult, evaluate_risk
from trading.signals_bridge import TradeCandidate, build_candidates

logger = logging.getLogger(__name__)

MODE_DRY_RUN = "dry_run"
MODE_PAPER_SEND = "paper_send"


@dataclass
class ProposedAction:
    candidate: TradeCandidate
    client_order_id: str
    outcome: str                      # "duplicate_skipped" | "rejected_by_risk" | "proposed" | "submitted" | "error"
    risk: Optional[RiskCheckResult] = None
    local_order_id: Optional[int] = None
    alpaca_order_id: Optional[str] = None
    detail: Optional[str] = None


@dataclass
class PaperRunResult:
    mode: str
    paper_account_confirmed: bool
    actions: List[ProposedAction] = field(default_factory=list)
    account_equity: Optional[float] = None
    account_cash: Optional[float] = None
    account_buying_power: Optional[float] = None

    @property
    def submitted_count(self) -> int:
        return sum(1 for a in self.actions if a.outcome == "submitted")

    @property
    def proposed_count(self) -> int:
        return sum(1 for a in self.actions if a.outcome == "proposed")

    @property
    def rejected_count(self) -> int:
        return sum(1 for a in self.actions if a.outcome == "rejected_by_risk")


def run_cycle(
    conn,
    client=None,
    tickers: Optional[List[str]] = None,
    mode: str = MODE_DRY_RUN,
    risk_config: RiskConfig = DEFAULT_RISK_CONFIG,
    execution_config: ExecutionConfig = DEFAULT_EXECUTION_CONFIG,
) -> PaperRunResult:
    """Run one full evaluate -> risk-check -> (maybe) submit cycle.

    mode=MODE_DRY_RUN (the default) never calls trading.orders.submit_order -
    the call below is the only place that can happen anywhere in this
    module, and it is gated on `mode == MODE_PAPER_SEND` alone.
    """
    if mode not in (MODE_DRY_RUN, MODE_PAPER_SEND):
        raise ValueError(f"unknown mode {mode!r}; must be {MODE_DRY_RUN!r} or {MODE_PAPER_SEND!r}")

    client = client or trading_client.get_client()
    tickers = tickers or WATCHLIST

    account = trading_client.verify_paper_environment(client)  # fail-closed; raises if unconfirmed
    paper_account_confirmed = True

    reconcile_open_orders(conn, client)  # read-only; reflects fills since the last run before proposing anything new

    positions = trading_client.get_positions(client)
    position_tickers = {p.symbol for p in positions}
    position_qty_by_ticker = {p.symbol: float(p.qty) for p in positions}
    long_market_value = sum(float(p.market_value) for p in positions)
    open_position_count = len(positions)

    open_orders = trading_client.get_open_orders(client)
    open_order_tickers = {o.symbol for o in open_orders}

    # Evaluate the configured watchlist plus any ticker with an open
    # position, even if that ticker has since been removed from the
    # watchlist - an existing position must remain eligible for its normal
    # signal-driven exit regardless. New entries stay gated to `tickers`
    # (the actual watchlist) via entry_watchlist - see
    # trading/signals_bridge.py's build_candidates docstring.
    evaluation_tickers = list(dict.fromkeys(list(tickers) + [t for t in position_tickers if t not in tickers]))

    candidates = build_candidates(
        conn, evaluation_tickers, position_tickers, open_order_tickers, position_qty_by_ticker,
        entry_watchlist=set(tickers),
    )

    result = PaperRunResult(
        mode=mode,
        paper_account_confirmed=paper_account_confirmed,
        account_equity=float(account.equity),
        account_cash=float(account.cash),
        account_buying_power=float(account.buying_power),
    )

    for candidate in candidates:
        client_order_id = build_client_order_id(
            candidate.ticker, candidate.intent, execution_config=execution_config,
        )
        existing = get_order_by_client_id(conn, client_order_id)
        if existing is not None and existing["status"] in ALREADY_SUBMITTED_STATUSES:
            result.actions.append(ProposedAction(
                candidate=candidate, client_order_id=client_order_id,
                outcome="duplicate_skipped", local_order_id=existing["id"],
                alpaca_order_id=existing["alpaca_order_id"],
                detail=f"an order intent was already sent to Alpaca with status={existing['status']!r} for {client_order_id}",
            ))
            continue

        risk_result = evaluate_risk(
            conn, candidate, account, position_tickers, open_order_tickers,
            long_market_value, open_position_count, paper_account_confirmed,
            watchlist=tickers, risk_config=risk_config,
        )

        if not risk_result.passed:
            local_order_id = upsert_order(
                conn, ticker=candidate.ticker, side=candidate.side, intent=candidate.intent,
                reason=candidate.reason, client_order_id=client_order_id,
                status=STATUS_REJECTED_BY_RISK, signal_score=candidate.score,
                confirmed_stage=candidate.stage, reference_price=candidate.reference_price,
                risk_checks=risk_result.checks, rejection_reason=risk_result.reason,
            )
            result.actions.append(ProposedAction(
                candidate=candidate, client_order_id=client_order_id,
                outcome="rejected_by_risk", risk=risk_result, local_order_id=local_order_id,
                detail=risk_result.reason,
            ))
            continue

        local_order_id = upsert_order(
            conn, ticker=candidate.ticker, side=candidate.side, intent=candidate.intent,
            reason=candidate.reason, client_order_id=client_order_id,
            status=STATUS_PROPOSED, qty=risk_result.sized_qty, notional=risk_result.sized_notional,
            signal_score=candidate.score, confirmed_stage=candidate.stage,
            reference_price=candidate.reference_price, risk_checks=risk_result.checks,
        )

        if mode == MODE_PAPER_SEND:
            try:
                order_request = build_order_request(
                    candidate, client_order_id,
                    sized_notional=risk_result.sized_notional, sized_qty=risk_result.sized_qty,
                    execution_config=execution_config,
                )
                alpaca_order = submit_order(client, order_request)
                status = map_alpaca_status(alpaca_order.status)
                update_order_status(
                    conn, local_order_id, status=status,
                    alpaca_order_id=str(alpaca_order.id),
                    submitted_at=str(alpaca_order.submitted_at) if alpaca_order.submitted_at else None,
                )
                result.actions.append(ProposedAction(
                    candidate=candidate, client_order_id=client_order_id,
                    outcome="submitted", risk=risk_result, local_order_id=local_order_id,
                    alpaca_order_id=str(alpaca_order.id), detail=f"submitted, status={status}",
                ))
            except Exception as e:
                logger.error("order submission failed for %s: %s", candidate.ticker, e)
                update_order_status(conn, local_order_id, status=STATUS_ERROR, rejection_reason=str(e))
                result.actions.append(ProposedAction(
                    candidate=candidate, client_order_id=client_order_id,
                    outcome="error", risk=risk_result, local_order_id=local_order_id, detail=str(e),
                ))
        else:
            result.actions.append(ProposedAction(
                candidate=candidate, client_order_id=client_order_id,
                outcome="proposed", risk=risk_result, local_order_id=local_order_id,
                detail="dry-run: no order submitted",
            ))

    return result
