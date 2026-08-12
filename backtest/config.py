"""Explicit, configurable rules for Phase 3 backtesting and walk-forward validation.

Every threshold, window size, and execution assumption lives here, by name,
so backtest/strategy.py and backtest/walkforward.py never contain an
unexplained constant.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class BacktestRules:
    """Entry/exit rules for the signal-score-driven backtest strategy.

    Reuses signals/engine.py's two decoupled Phase 2 outputs directly rather
    than inventing a separate rule set:
      - highest_confirmed_stage: the sequential trend -> momentum -> volume
        qualification chain (a structural gate).
      - score: the raw 0-100 composite confidence score (a numeric gate).

    Entry requires BOTH gates to pass. Exit uses a lower score threshold
    than entry (hysteresis) so a position isn't opened and closed on every
    small wiggle right at the boundary.
    """

    entry_min_stage: str = "trend"     # highest_confirmed_stage must be >= this to open a position
    entry_min_score: float = 70.0      # AND raw composite score must be >= this
    exit_stage_floor: str = "trend"    # close the position once stage falls below this...
    exit_max_score: float = 40.0       # ...or once raw score falls to/below this (whichever comes first)


@dataclass(frozen=True)
class BacktestExecutionConfig:
    initial_cash: float = 10_000.0
    commission: float = 0.001   # 0.1% per trade, a reasonable retail-brokerage approximation


@dataclass(frozen=True)
class WalkForwardConfig:
    """Chronological, non-overlapping train/test windows.

    train_window_days is NOT used to fit/optimize anything in this phase
    (Phase 2's thresholds/weights are kept fixed) - it exists so each test
    window has enough preceding history for indicators to warm up, and so
    the code structure has a slot for a future optimizer to plug into
    without changing the validation methodology. See
    backtest/walkforward.py:default_parameter_selector.
    """

    train_window_days: int = 252   # ~1 trading year, reserved for future optimization
    test_window_days: int = 63     # ~1 trading quarter, evaluated out-of-sample
    step_days: int = 63            # advance by one full test window each split -> no test/test overlap


DEFAULT_RULES = BacktestRules()
DEFAULT_EXECUTION = BacktestExecutionConfig()
DEFAULT_WF_CONFIG = WalkForwardConfig()
