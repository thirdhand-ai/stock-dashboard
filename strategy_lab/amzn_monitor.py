"""Phase 10 spec item B21: READ-ONLY monitoring of the existing AMZN paper
position. Never sells, adds to, cancels, replaces, or submits any order -
this module has no import of trading.orders/trading.engine/trading.run_paper
(see tests/test_strategy_lab.py's structural safety tests).

Displays whether the existing frozen production exit condition
(backtest.config.DEFAULT_RULES: stage < trend OR score <= 40) is currently
met - informational only. If met, the caller must display the exit
condition prominently and NOT act on it; a human decides and executes
manually via `python -m trading.run_paper --paper-send`, never this module.
"""
from dataclasses import dataclass
from typing import Optional

from backtest.config import DEFAULT_RULES
from backtest.scoring import STAGE_ORDER
from indicators.technical import compute_indicators_for_ticker
from research.regime import compute_market_regime
from signals.engine import score_indicators
from trading.client import get_account, get_client, get_positions, verify_paper_environment

TICKER = "AMZN"


@dataclass
class AmznStatus:
    ok: bool
    reason: Optional[str] = None
    qty: Optional[float] = None
    avg_entry_price: Optional[float] = None
    current_price: Optional[float] = None
    market_value: Optional[float] = None
    unrealized_pl: Optional[float] = None
    unrealized_pl_pct: Optional[float] = None
    account_equity: Optional[float] = None
    portfolio_exposure_pct: Optional[float] = None
    score: Optional[float] = None
    stage: Optional[str] = None
    regime: Optional[str] = None
    exit_condition_met: Optional[bool] = None
    exit_condition_detail: Optional[str] = None


def get_amzn_status(conn, client=None) -> AmznStatus:
    client = client or get_client()
    try:
        verify_paper_environment(client)
    except Exception as e:
        return AmznStatus(ok=False, reason=f"Alpaca paper environment unavailable: {e}")

    positions = {p.symbol: p for p in get_positions(client)}
    position = positions.get(TICKER)
    if position is None:
        return AmznStatus(ok=False, reason="No open AMZN position found in the Alpaca paper account.")

    account = get_account(client)
    equity = float(account.equity)
    market_value = float(position.market_value)

    indicators = compute_indicators_for_ticker(conn, TICKER)
    score_result = score_indicators(indicators) if indicators.ok else None

    regime_result = compute_market_regime(conn)
    regime_label = regime_result.label if regime_result.ok else None

    exit_met, exit_detail = None, "Signal data unavailable - cannot evaluate exit condition."
    if score_result is not None:
        stage_rank = STAGE_ORDER[score_result.highest_confirmed_stage]
        floor_rank = STAGE_ORDER[DEFAULT_RULES.exit_stage_floor]
        stage_below_floor = stage_rank < floor_rank
        score_at_or_below_exit = score_result.score <= DEFAULT_RULES.exit_max_score
        exit_met = stage_below_floor or score_at_or_below_exit
        exit_detail = (
            f"stage={score_result.highest_confirmed_stage} ({'<' if stage_below_floor else '>='} {DEFAULT_RULES.exit_stage_floor}) "
            f"OR score={score_result.score:.1f} ({'<=' if score_at_or_below_exit else '>'} {DEFAULT_RULES.exit_max_score})"
        )

    return AmznStatus(
        ok=True,
        qty=float(position.qty),
        avg_entry_price=float(position.avg_entry_price),
        current_price=float(position.current_price),
        market_value=market_value,
        unrealized_pl=float(position.unrealized_pl),
        unrealized_pl_pct=float(position.unrealized_plpc) * 100,
        account_equity=equity,
        portfolio_exposure_pct=(market_value / equity * 100) if equity else None,
        score=score_result.score if score_result else None,
        stage=score_result.highest_confirmed_stage if score_result else None,
        regime=regime_label,
        exit_condition_met=exit_met,
        exit_condition_detail=exit_detail,
    )
