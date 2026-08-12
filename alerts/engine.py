"""Alert evaluation: detects meaningful score/stage transitions worth
notifying about, reusing signals.engine.score_indicators directly (the same
Phase 2 rules the dashboard and backtest already use - no separate,
inconsistent alert logic).

Two conditions, either of which can fire independently:
  - score_crossing: raw score crosses UP through config.score_threshold
    (previous < threshold <= current). Remaining above threshold, or
    falling below it, never fires this.
  - stage_advance: highest_confirmed_stage moves to a strictly higher stage
    than last observed (none -> trend -> momentum -> volume).

Neither ever fires on a ticker's first-ever evaluation - with no prior
observation there is nothing to have crossed *from*, so the first run only
establishes a baseline.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from alerts.config import DEFAULT_ALERT_CONFIG, AlertConfig
from config.settings import WATCHLIST
from db.alert_repository import get_alert_state
from db.price_repository import resolve_source
from indicators.technical import compute_indicators_for_ticker
from backtest.scoring import STAGE_ORDER
from signals.engine import score_indicators

REASON_SCORE_CROSSING = "score_crossing"
REASON_STAGE_ADVANCE = "stage_advance"


def determine_alert_reasons(
    previous_score: Optional[float],
    current_score: float,
    previous_stage: Optional[str],
    current_stage: str,
    config: AlertConfig = DEFAULT_ALERT_CONFIG,
) -> List[str]:
    """Pure decision logic, no DB/indicator dependency - the core of what
    counts as a "meaningful state change" worth alerting on."""
    if previous_score is None or previous_stage is None:
        return []  # first observation: establish baseline, never alert

    reasons = []
    if config.enable_score_crossing and previous_score < config.score_threshold <= current_score:
        reasons.append(REASON_SCORE_CROSSING)
    if config.enable_stage_advance and STAGE_ORDER[current_stage] > STAGE_ORDER[previous_stage]:
        reasons.append(REASON_STAGE_ADVANCE)
    return reasons


@dataclass
class AlertEvaluation:
    ticker: str
    ok: bool
    reason_unavailable: Optional[str] = None
    should_alert: bool = False
    reasons: List[str] = field(default_factory=list)
    current_score: Optional[float] = None
    previous_score: Optional[float] = None
    current_stage: Optional[str] = None
    previous_stage: Optional[str] = None
    price: Optional[float] = None
    data_date: Optional[str] = None
    source: Optional[str] = None
    fired_conditions: List[str] = field(default_factory=list)


def evaluate_ticker(conn, ticker: str, config: AlertConfig = DEFAULT_ALERT_CONFIG) -> AlertEvaluation:
    """Compute the current score/stage for a ticker and compare against its
    last-observed state (alert_state) to decide whether it should alert."""
    indicators = compute_indicators_for_ticker(conn, ticker)
    if not indicators.ok:
        return AlertEvaluation(ticker=ticker, ok=False, reason_unavailable=indicators.reason)

    score = score_indicators(indicators)

    state = get_alert_state(conn, ticker)
    previous_score = state["last_score"] if state else None
    previous_stage = state["last_stage"] if state else None

    reasons = determine_alert_reasons(
        previous_score, score.score, previous_stage, score.highest_confirmed_stage, config
    )
    fired_conditions = [c.name for c in score.conditions if c.fired]

    return AlertEvaluation(
        ticker=ticker,
        ok=True,
        should_alert=bool(reasons),
        reasons=reasons,
        current_score=score.score,
        previous_score=previous_score,
        current_stage=score.highest_confirmed_stage,
        previous_stage=previous_stage,
        price=indicators.close,
        data_date=indicators.latest_date,
        source=resolve_source(conn, ticker),
        fired_conditions=fired_conditions,
    )


def evaluate_watchlist(
    conn, tickers: Optional[List[str]] = None, config: AlertConfig = DEFAULT_ALERT_CONFIG
) -> List[AlertEvaluation]:
    tickers = tickers or WATCHLIST
    return [evaluate_ticker(conn, t, config) for t in tickers]
