"""Phase 7 pre-trade risk checks - the single gate every proposed order must
pass before it can be submitted (or, in --dry-run, before it would be
submitted). Every limit is read from trading/config.py's RiskConfig; nothing
here invents its own threshold.

Checks are always evaluated in full (never short-circuited) so the audit
trail persisted to paper_orders.risk_checks always shows every check's
outcome, not just the first failure. `reason` on a failing result is the
first failure in the priority order specified for Phase 7:
paper account confirmed -> buying power -> existing position -> exposure ->
position-size limit -> max open positions -> watchlist membership -> signal
still qualifies.

`ticker_in_watchlist` is only ever checked for ENTRY candidates. An open
position must remain exitable by its normal signal-driven exit even after
its ticker is later removed from the configured watchlist - gating exits on
current watchlist membership would strand that position with no
systematic way out. New entries are still strictly limited to the
configured watchlist (trading/signals_bridge.py's build_candidates also
enforces this at candidate-generation time, before risk evaluation ever
runs, so a non-watchlist ticker never even produces an entry candidate).
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from config.settings import WATCHLIST
from trading.config import DEFAULT_RISK_CONFIG, RiskConfig
from trading.signals_bridge import (
    INTENT_ENTRY,
    INTENT_EXIT,
    TradeCandidate,
    entry_qualifies,
    evaluate_ticker,
    exit_qualifies,
)

# Priority order used to pick the single `reason` from possibly several
# failing checks - matches the order Phase 7 specifies for validation.
_CHECK_PRIORITY = [
    "paper_account_confirmed",
    "buying_power_sufficient",
    "no_existing_position",
    "has_existing_position",
    "exposure_limit",
    "position_size_limit",
    "max_open_positions",
    "no_pending_order",
    "ticker_in_watchlist",
    "signal_still_qualifies",
]


@dataclass
class RiskCheckResult:
    passed: bool
    reason: Optional[str]
    checks: Dict[str, bool] = field(default_factory=dict)
    detail: Dict[str, float] = field(default_factory=dict)
    sized_qty: Optional[float] = None
    sized_notional: Optional[float] = None


def _first_failure_reason(checks: Dict[str, bool], detail: Dict[str, float]) -> Optional[str]:
    for name in _CHECK_PRIORITY:
        if name in checks and not checks[name]:
            return f"{name} failed ({detail.get(name, '')})".strip()
    failing = [k for k, v in checks.items() if not v]
    return f"{failing[0]} failed" if failing else None


def evaluate_risk(
    conn,
    candidate: TradeCandidate,
    account,
    position_tickers: Set[str],
    open_order_tickers: Set[str],
    long_market_value: float,
    open_position_count: int,
    paper_account_confirmed: bool,
    watchlist: Optional[List[str]] = None,
    risk_config: RiskConfig = DEFAULT_RISK_CONFIG,
) -> RiskCheckResult:
    """Validate one proposed order against every configured risk rule.

    `account` is the Alpaca TradeAccount fetched this cycle (equity/cash/
    buying_power are read live from it, per Phase 7's requirement to use the
    real paper account as the reference portfolio - never a hardcoded
    starting balance). `position_tickers`/`open_order_tickers`/
    `long_market_value`/`open_position_count` are the fresh Alpaca snapshot
    taken this cycle by trading/engine.py.
    """
    watchlist = watchlist if watchlist is not None else WATCHLIST
    equity = float(account.equity)
    buying_power = float(account.buying_power)

    checks: Dict[str, bool] = {}
    detail: Dict[str, float] = {}

    checks["paper_account_confirmed"] = bool(paper_account_confirmed)
    detail["paper_account_confirmed"] = paper_account_confirmed

    if candidate.intent == INTENT_ENTRY:
        # Not checked for exits - see this module's docstring: an existing
        # position must stay exitable even after its ticker leaves the
        # configured watchlist.
        checks["ticker_in_watchlist"] = candidate.ticker in watchlist
        detail["ticker_in_watchlist"] = candidate.ticker in watchlist

    checks["no_pending_order"] = candidate.ticker not in open_order_tickers
    detail["no_pending_order"] = candidate.ticker not in open_order_tickers

    # Re-check the signal fresh, right now, rather than trusting the
    # candidate built moments earlier in this same cycle - guards against
    # staleness if evaluation takes a while or is reused across a batch.
    fresh_signal = evaluate_ticker(conn, candidate.ticker)
    if candidate.intent == INTENT_ENTRY:
        signal_ok = fresh_signal.ok and entry_qualifies(fresh_signal.score)
    else:
        signal_ok = fresh_signal.ok and exit_qualifies(fresh_signal.score)
    checks["signal_still_qualifies"] = signal_ok
    detail["signal_still_qualifies"] = signal_ok

    sized_qty = None
    sized_notional = None

    if candidate.intent == INTENT_ENTRY:
        checks["no_existing_position"] = candidate.ticker not in position_tickers
        detail["no_existing_position"] = candidate.ticker not in position_tickers

        checks["max_open_positions"] = open_position_count < risk_config.max_open_positions
        detail["max_open_positions"] = open_position_count

        sized_notional = round(risk_config.max_position_size_pct * equity, 2)
        checks["position_size_limit"] = sized_notional <= risk_config.max_position_size_pct * equity + 1e-6
        detail["position_size_limit"] = sized_notional

        projected_exposure = (long_market_value + sized_notional) / equity if equity > 0 else float("inf")
        checks["exposure_limit"] = projected_exposure <= risk_config.max_total_exposure_pct + 1e-6
        detail["exposure_limit"] = projected_exposure

        checks["buying_power_sufficient"] = sized_notional <= buying_power
        detail["buying_power_sufficient"] = buying_power

    else:  # INTENT_EXIT
        checks["has_existing_position"] = candidate.ticker in position_tickers
        detail["has_existing_position"] = candidate.ticker in position_tickers
        sized_qty = candidate.existing_qty

    passed = all(checks.values())
    reason = None if passed else _first_failure_reason(checks, detail)

    return RiskCheckResult(
        passed=passed,
        reason=reason,
        checks=checks,
        detail={k: (v if isinstance(v, (int, float)) else bool(v)) for k, v in detail.items()},
        sized_qty=sized_qty,
        sized_notional=sized_notional,
    )
