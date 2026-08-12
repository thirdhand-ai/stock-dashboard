"""Explicit, configurable rules for the alert engine.

score_threshold reuses backtest.config.DEFAULT_RULES.entry_min_score - the
same number the dashboard's score badges and the Phase 3 backtest strategy
already use - so alert/dashboard/backtest thresholds never silently diverge.
"""
from dataclasses import dataclass

from backtest.config import DEFAULT_RULES


@dataclass(frozen=True)
class AlertConfig:
    # Primary trigger: score must cross UP through this threshold (previous
    # < threshold <= current), not merely be at/above it.
    score_threshold: float = DEFAULT_RULES.entry_min_score

    enable_score_crossing: bool = True
    enable_stage_advance: bool = True

    # Secondary protection only (see alerts/engine.py) - never a substitute
    # for correct crossing/state-change detection. 0 disables the cooldown.
    cooldown_minutes: int = 60


DEFAULT_ALERT_CONFIG = AlertConfig()
