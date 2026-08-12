"""Backtesting.py Strategy driven by the precomputed signal score + sequential
confirmation stage (see backtest/scoring.py and signals/engine.py).

Score and stage are computed once, up front, on the full price history
(indicators/technical.enrich_with_indicators + backtest/scoring.py) and
passed into Backtest() as extra data columns. This class contains no
indicator math of its own - it only reads the precomputed Score/StageRank
columns bar by bar and applies the entry/exit rules from backtest/config.py.
"""
import numpy as np
from backtesting import Strategy

from backtest.config import DEFAULT_RULES
from backtest.scoring import STAGE_ORDER


class SignalScoreStrategy(Strategy):
    # Class-level defaults, overridable per-run via Backtest.run(**kwargs) -
    # this is how backtest/runner.py plugs in a specific BacktestRules.
    entry_min_stage_rank = STAGE_ORDER[DEFAULT_RULES.entry_min_stage]
    entry_min_score = DEFAULT_RULES.entry_min_score
    exit_stage_floor_rank = STAGE_ORDER[DEFAULT_RULES.exit_stage_floor]
    exit_max_score = DEFAULT_RULES.exit_max_score

    def init(self):
        pass  # Score/StageRank are already fully precomputed data columns

    def next(self):
        score = self.data.Score[-1]
        stage_rank = self.data.StageRank[-1]

        if np.isnan(score) or np.isnan(stage_rank):
            return  # still inside the indicator warm-up window - do nothing

        if not self.position:
            if stage_rank >= self.entry_min_stage_rank and score >= self.entry_min_score:
                self.buy()
        else:
            if stage_rank < self.exit_stage_floor_rank or score <= self.exit_max_score:
                self.position.close()


def run_kwargs_for_rules(rules) -> dict:
    """Translate a BacktestRules into the kwargs Backtest.run() needs to
    override SignalScoreStrategy's class defaults for this run."""
    return {
        "entry_min_stage_rank": STAGE_ORDER[rules.entry_min_stage],
        "entry_min_score": rules.entry_min_score,
        "exit_stage_floor_rank": STAGE_ORDER[rules.exit_stage_floor],
        "exit_max_score": rules.exit_max_score,
    }
