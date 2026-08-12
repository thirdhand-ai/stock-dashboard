"""RESEARCH-ONLY composite: the "Research Score."

Deliberately named differently from, and structurally unconnected to, the
production `signal score` computed by signals/engine.py. Nothing in this
module is imported by trading/engine.py, alerts/*, or automation/run_daily.py
- see tests/test_research.py's structural-isolation check. Changing this
module's weights has zero effect on entry/exit eligibility.

Missing components are excluded (never fabricated) and the remaining
weights are renormalized - the same pattern used in
research/fundamentals.py's category scoring.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional

from research.config import DEFAULT_RESEARCH_COMPOSITE_CONFIG, ResearchCompositeConfig


@dataclass
class ResearchScore:
    ticker: str
    ok: bool
    reason: Optional[str] = None
    score: Optional[float] = None
    components: Dict[str, float] = field(default_factory=dict)          # sub-scores actually used, 0-100 each
    weights_applied_pct: Dict[str, float] = field(default_factory=dict)  # renormalized weight actually applied, sums to 100


def compute_research_score(
    ticker: str,
    technical_score: Optional[float],
    fundamental_score: Optional[float],
    sentiment_score: Optional[float],
    relative_strength_score: Optional[float],
    regime_label: Optional[str],
    config: ResearchCompositeConfig = DEFAULT_RESEARCH_COMPOSITE_CONFIG,
) -> ResearchScore:
    """`relative_strength_score` and `regime_label` are pre-computed by the
    caller (research/ranking.py) - relative strength needs cohort context
    (percentile rank vs the rest of the watchlist) that this function
    deliberately doesn't own, keeping this module a pure combiner."""
    regime_component = config.regime_component_scores.get(regime_label) if regime_label else None

    candidates = {
        "technical": (technical_score, config.technical_weight),
        "fundamental": (fundamental_score, config.fundamental_weight),
        "sentiment": (sentiment_score, config.sentiment_weight),
        "relative_strength": (relative_strength_score, config.relative_strength_weight),
        "regime": (regime_component, config.regime_weight),
    }
    available = {name: (score, weight) for name, (score, weight) in candidates.items() if score is not None}

    if not available:
        return ResearchScore(ticker=ticker, ok=False, reason="no research components available for this ticker")

    total_weight = sum(weight for _score, weight in available.values())
    weighted = sum(score * weight for score, weight in available.values()) / total_weight

    return ResearchScore(
        ticker=ticker, ok=True, score=round(weighted, 1),
        components={name: score for name, (score, _w) in available.items()},
        weights_applied_pct={name: round(weight / total_weight * 100.0, 1) for name, (_s, weight) in available.items()},
    )
