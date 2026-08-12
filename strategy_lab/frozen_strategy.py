"""Freezes and snapshots the exact production technical strategy definition
in force at the start of Phase 9 (Phase 9 spec item 3).

Read-only: imports the live production config objects (signals/config.py,
backtest/config.py) and records their values verbatim. Phase 9 must never
alter these objects, and nothing in this module writes to them - it only
snapshots and reports. All Phase 9 signal computation reuses
signals.engine.score_indicators / backtest.scoring.compute_score_series
directly against these same frozen objects, so research and production
cannot silently drift apart.
"""
import dataclasses
import json
from datetime import datetime, timezone

from backtest.config import DEFAULT_EXECUTION, DEFAULT_RULES
from backtest.scoring import STAGE_ORDER
from signals.config import DEFAULT_THRESHOLDS, DEFAULT_WEIGHTS

SCORE_BUCKETS = [
    (0, 39, "0-39"),
    (40, 54, "40-54"),
    (55, 69, "55-69"),
    (70, 84, "70-84"),
    (85, 100, "85-100"),
]

STAGE_LABELS = ["none", "trend", "momentum", "volume"]


def score_bucket(score: float) -> str:
    for lo, hi, label in SCORE_BUCKETS:
        if lo <= score <= hi:
            return label
    return "85-100" if score > 100 else "0-39"


def snapshot() -> dict:
    """A frozen, JSON-serializable record of the exact production rules in
    force right now, captured once at the start of Phase 9 and never
    recomputed against a later, possibly-changed config."""
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "signal_thresholds": dataclasses.asdict(DEFAULT_THRESHOLDS),
        "signal_weights": dataclasses.asdict(DEFAULT_WEIGHTS),
        "backtest_rules": dataclasses.asdict(DEFAULT_RULES),
        "backtest_execution": dataclasses.asdict(DEFAULT_EXECUTION),
        "stage_order": STAGE_ORDER,
        "score_buckets": [label for _, _, label in SCORE_BUCKETS],
        "notes": (
            "Entry: highest_confirmed_stage >= entry_min_stage AND score >= entry_min_score. "
            "Exit: highest_confirmed_stage < exit_stage_floor OR score <= exit_max_score. "
            "Stage chain is sequential and independent of the raw composite score: "
            "trend requires ADX>=adx_trend_threshold AND close>SMA50; momentum requires trend AND "
            "rsi_bullish_min<RSI<rsi_overbought AND MACD>signal; volume requires momentum AND "
            "volume_ratio>=volume_ratio_min."
        ),
    }


FROZEN_STRATEGY = snapshot()


if __name__ == "__main__":
    print(json.dumps(FROZEN_STRATEGY, indent=2))
