"""Phase 10 spec item B1: the three experiment definitions, frozen exactly
as specified before any result was examined. Never mutated after definition
- if a variant here needs a different rule, that is a NEW experiment with a
new name, not an edit to CONTROL/A/B.

CONTROL is the exact Phase 9 frozen strategy (backtest.config.DEFAULT_RULES)
- reused directly, not re-derived, so CONTROL cannot silently drift from
Phase 9's own baseline.

No score/weight/threshold value is redefined here - only the ADDITIONAL
regime-gating condition on entry (A, B) and exit (B only) is new.
"""
from dataclasses import dataclass

from backtest.config import DEFAULT_RULES

BULLISH_LABEL = "bullish_trend"


@dataclass(frozen=True)
class RegimeGatedRules:
    name: str
    description: str
    require_bullish_entry: bool
    exit_on_regime_loss: bool


CONTROL = RegimeGatedRules(
    name="original_frozen_strategy",
    description=(
        f"Phase 9 CONTROL, unmodified: entry stage>={DEFAULT_RULES.entry_min_stage} AND "
        f"score>={DEFAULT_RULES.entry_min_score}; exit stage<{DEFAULT_RULES.exit_stage_floor} OR "
        f"score<={DEFAULT_RULES.exit_max_score}. No regime condition."
    ),
    require_bullish_entry=False,
    exit_on_regime_loss=False,
)

EXPERIMENT_A = RegimeGatedRules(
    name="bullish_entry_only",
    description=(
        "Original frozen entry AND market regime == bullish_trend at entry. "
        "Exit: original frozen exit only (regime not part of exit)."
    ),
    require_bullish_entry=True,
    exit_on_regime_loss=False,
)

EXPERIMENT_B = RegimeGatedRules(
    name="bullish_entry_and_exit",
    description=(
        "Original frozen entry AND market regime == bullish_trend at entry. "
        "Exit: original frozen exit OR regime leaves bullish_trend."
    ),
    require_bullish_entry=True,
    exit_on_regime_loss=True,
)

ALL_VARIANTS = (CONTROL, EXPERIMENT_A, EXPERIMENT_B)

# Hard rule (Phase 10 spec B2): no parameter in DEFAULT_RULES, signals/config.py,
# or research/config.py's RegimeConfig is touched or optimized anywhere in
# Phase 10. Anything that looks like a promising modification during
# analysis must be recorded as a "Future hypothesis - not tested in Phase 10"
# in the final report, never implemented here.
FUTURE_HYPOTHESES: list = []


def record_future_hypothesis(text: str) -> None:
    """Append-only log of ideas noticed during Phase 10 analysis that were
    deliberately NOT implemented (per B2's hard rule against parameter
    optimization/searching). Read by the final report; never fed back into
    any strategy definition."""
    FUTURE_HYPOTHESES.append(text)
