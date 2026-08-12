"""Translate current signal state into candidate paper-trading actions.

Reuses signals.engine.score_indicators and backtest.config.DEFAULT_RULES /
backtest.scoring.STAGE_ORDER directly - the exact same entry/exit gate
backtest/strategy.py's SignalScoreStrategy.next() applies bar-by-bar in
backtesting:

    entry: highest_confirmed_stage >= rules.entry_min_stage AND score >= rules.entry_min_score
    exit:  highest_confirmed_stage <  rules.exit_stage_floor  OR  score <= rules.exit_max_score

No separate signal strategy is invented here, and no thresholds are
duplicated - a change to backtest/config.py's DEFAULT_RULES automatically
applies here too.
"""
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set

from backtest.config import DEFAULT_RULES, BacktestRules
from backtest.scoring import STAGE_ORDER
from db.price_repository import resolve_source
from indicators.technical import compute_indicators_for_ticker
from signals.engine import SignalScore, score_indicators

INTENT_ENTRY = "entry"
INTENT_EXIT = "exit"
SIDE_BUY = "buy"
SIDE_SELL = "sell"


@dataclass
class TradeCandidate:
    ticker: str
    intent: str                     # "entry" | "exit"
    side: str                       # "buy" | "sell"
    score: float
    stage: str
    reference_price: float
    data_date: str
    source: Optional[str]
    reason: str
    existing_qty: Optional[float] = None   # exits only: the Alpaca-reported open quantity to close


@dataclass
class TickerSignal:
    ticker: str
    ok: bool
    reason_unavailable: Optional[str] = None
    score: Optional[SignalScore] = None
    close: Optional[float] = None
    latest_date: Optional[str] = None
    source: Optional[str] = None


def evaluate_ticker(conn, ticker: str) -> TickerSignal:
    """Current signal score/stage plus reference price for one ticker - the
    same computation alerts/engine.py and dashboard/data.py use, reused
    rather than re-derived."""
    indicators = compute_indicators_for_ticker(conn, ticker)
    if not indicators.ok:
        return TickerSignal(ticker=ticker, ok=False, reason_unavailable=indicators.reason)

    score = score_indicators(indicators)
    return TickerSignal(
        ticker=ticker,
        ok=True,
        score=score,
        close=indicators.close,
        latest_date=indicators.latest_date,
        source=resolve_source(conn, ticker),
    )


def entry_qualifies(score: SignalScore, rules: BacktestRules = DEFAULT_RULES) -> bool:
    return (
        STAGE_ORDER[score.highest_confirmed_stage] >= STAGE_ORDER[rules.entry_min_stage]
        and score.score >= rules.entry_min_score
    )


def exit_qualifies(score: SignalScore, rules: BacktestRules = DEFAULT_RULES) -> bool:
    return (
        STAGE_ORDER[score.highest_confirmed_stage] < STAGE_ORDER[rules.exit_stage_floor]
        or score.score <= rules.exit_max_score
    )


def build_candidates(
    conn,
    tickers: Iterable[str],
    position_tickers: Set[str],
    open_order_tickers: Set[str],
    position_qty_by_ticker: Dict[str, float],
    rules: BacktestRules = DEFAULT_RULES,
    entry_watchlist: Optional[Set[str]] = None,
) -> List[TradeCandidate]:
    """At most one candidate per ticker: an entry if flat, the ticker is
    entry-eligible, and the entry gate qualifies; or an exit if holding a
    position and the exit gate qualifies. A ticker with any open/pending
    Alpaca order is skipped entirely - the existing order must resolve
    (fill/cancel/reject) before a new candidate is considered, which is half
    of the duplicate-order protection (the other half is the local
    client_order_id uniqueness constraint - see db/trading_repository.py).

    `tickers` is the full set to evaluate this cycle - trading/engine.py
    folds in any ticker with an open position even if it has since been
    removed from the configured watchlist, so that position's signal-driven
    exit is never orphaned. `entry_watchlist` (defaults to `tickers` itself)
    is the separate, smaller set actually eligible for NEW entries: removing
    a ticker from the configured watchlist stops new entries into it
    immediately, without blocking the exit of a position that already
    exists there.
    """
    entry_watchlist = set(entry_watchlist) if entry_watchlist is not None else set(tickers)
    candidates: List[TradeCandidate] = []

    for ticker in tickers:
        if ticker in open_order_tickers:
            continue

        signal = evaluate_ticker(conn, ticker)
        if not signal.ok:
            continue

        has_position = ticker in position_tickers

        if not has_position and ticker in entry_watchlist and entry_qualifies(signal.score, rules):
            candidates.append(TradeCandidate(
                ticker=ticker,
                intent=INTENT_ENTRY,
                side=SIDE_BUY,
                score=signal.score.score,
                stage=signal.score.highest_confirmed_stage,
                reference_price=signal.close,
                data_date=signal.latest_date,
                source=signal.source,
                reason=(
                    f"stage={signal.score.highest_confirmed_stage} (>= {rules.entry_min_stage}) and "
                    f"score={signal.score.score:.1f} (>= {rules.entry_min_score}): entry gate qualifies"
                ),
            ))
        elif has_position and exit_qualifies(signal.score, rules):
            candidates.append(TradeCandidate(
                ticker=ticker,
                intent=INTENT_EXIT,
                side=SIDE_SELL,
                score=signal.score.score,
                stage=signal.score.highest_confirmed_stage,
                reference_price=signal.close,
                data_date=signal.latest_date,
                source=signal.source,
                reason=(
                    f"stage={signal.score.highest_confirmed_stage} (< {rules.exit_stage_floor}) or "
                    f"score={signal.score.score:.1f} (<= {rules.exit_max_score}): exit gate qualifies"
                ),
                existing_qty=position_qty_by_ticker.get(ticker),
            ))

    return candidates
