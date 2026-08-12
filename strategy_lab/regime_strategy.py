"""Phase 10 spec item B8: a Backtesting.py Strategy that can additionally
gate entry/exit on market regime, WITHOUT modifying backtest/strategy.py's
production SignalScoreStrategy at all - that class is untouched and still
what CONTROL's numbers should match.

When require_bullish_entry=False and exit_on_regime_loss=False, this class's
logic is mathematically identical to SignalScoreStrategy (same base
entry/exit gate, reading the same DEFAULT_RULES) - used to run CONTROL
through the exact same code path as Experiments A/B for a clean apples-to-
apples comparison, while backtest_universe.py's Phase-9-baseline CONTROL
numbers (via the real SignalScoreStrategy) remain the authoritative
unmodified Phase 9 baseline.
"""
import numpy as np
from backtesting import Strategy

from backtest.config import DEFAULT_RULES
from backtest.scoring import STAGE_ORDER


class RegimeGatedStrategy(Strategy):
    entry_min_stage_rank = STAGE_ORDER[DEFAULT_RULES.entry_min_stage]
    entry_min_score = DEFAULT_RULES.entry_min_score
    exit_stage_floor_rank = STAGE_ORDER[DEFAULT_RULES.exit_stage_floor]
    exit_max_score = DEFAULT_RULES.exit_max_score
    require_bullish_entry = False
    exit_on_regime_loss = False

    def init(self):
        pass  # Score/StageRank/RegimeBullish are precomputed data columns

    def next(self):
        score = self.data.Score[-1]
        stage_rank = self.data.StageRank[-1]

        if np.isnan(score) or np.isnan(stage_rank):
            return

        is_bullish = bool(self.data.RegimeBullish[-1])

        if not self.position:
            base_entry = stage_rank >= self.entry_min_stage_rank and score >= self.entry_min_score
            if self.require_bullish_entry and not is_bullish:
                return
            if base_entry:
                self.buy()
        else:
            base_exit = stage_rank < self.exit_stage_floor_rank or score <= self.exit_max_score
            regime_exit = self.exit_on_regime_loss and not is_bullish
            if base_exit or regime_exit:
                self.position.close()


def run_kwargs_for_variant(variant) -> dict:
    return {"require_bullish_entry": variant.require_bullish_entry, "exit_on_regime_loss": variant.exit_on_regime_loss}
