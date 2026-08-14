"""Trading-day-based prospective evidence classification (Phase 12,
shared by Components D and E).

Distinct from Phase 11's `strategy_lab.prospective_events.evidence_label`
(event-count based). This is the trading-DAY-based classification, with
its own mandated vocabulary - the two are shown side by side in the
dashboard, clearly labeled as measuring different things, never merged or
substituted for each other.

No optimization/backfill using today's prospective outcomes: every
function here is a pure function of already-recorded, already-immutable
data.
"""
from strategy_lab.prospective import load_observations

EVIDENCE_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
EVIDENCE_EARLY_EVIDENCE = "EARLY_EVIDENCE"
EVIDENCE_EVALUATION_READY = "EVALUATION_READY"

# Documented placeholder thresholds for REPORTING classification only, not
# trading-readiness decisions - scaled down from Phase 11's existing 30/100
# EVENT thresholds to a DAY-count basis (a day typically yields several
# events).
MIN_DAYS_EARLY_EVIDENCE = 20     # ~1 trading month
MIN_DAYS_EVALUATION_READY = 60   # ~1 trading quarter


def count_prospective_trading_days(conn) -> int:
    """Distinct observation_date values in research_prospective_observations
    (strategy_lab.prospective.load_observations, read-only). Genuinely
    prospective by construction: that table only ever receives rows going
    forward from when strategy_lab.research_automation started running
    (insert-only, no backfill path anywhere in strategy_lab/prospective.py)."""
    observations = load_observations(conn)
    if observations.empty:
        return 0
    return int(observations["observation_date"].nunique())


def classify_evidence(n_days: int) -> str:
    """Pure, deterministic."""
    if n_days < MIN_DAYS_EARLY_EVIDENCE:
        return EVIDENCE_INSUFFICIENT_DATA
    if n_days < MIN_DAYS_EVALUATION_READY:
        return EVIDENCE_EARLY_EVIDENCE
    return EVIDENCE_EVALUATION_READY
