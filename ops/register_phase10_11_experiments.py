"""One-off, idempotent seed script: registers the CURRENT frozen Phase
10/11 methodology (CONTROL / Experiment A / Experiment B) into the Phase 12
experiment governance registry. No new rule is invented here - hypotheses
are pulled verbatim from strategy_lab/phase10_experiments.py's existing
docstrings/descriptions.

Safe to re-run: register_experiment() no-ops on an identical
re-registration. If the frozen config has genuinely changed under an
unchanged experiment_id, register_experiment() raises
ExperimentImmutabilityError - this script does NOT catch that and silently
continue; it prints the error and exits non-zero, because a config change
under an unchanged experiment_id is exactly the scenario this component
exists to catch.

python -m ops.register_phase10_11_experiments
"""
import sys

from db.database import db_session
from ops.experiment_registry import (
    STATUS_ACTIVE,
    ExperimentImmutabilityError,
    compute_config_fingerprint,
    register_experiment,
)
from strategy_lab.phase10_experiments import CONTROL, EXPERIMENT_A, EXPERIMENT_B
from strategy_lab.prospective import METHODOLOGY_VERSION

EXPERIMENTS = [
    ("control-original-frozen-strategy", CONTROL.description),
    ("experiment-a-bullish-entry-only", EXPERIMENT_A.description),
    ("experiment-b-bullish-entry-and-exit", EXPERIMENT_B.description),
]


def main():
    with db_session() as conn:
        fingerprint = compute_config_fingerprint()
        for experiment_id, hypothesis in EXPERIMENTS:
            try:
                inserted = register_experiment(
                    conn,
                    experiment_id=experiment_id,
                    methodology_version=METHODOLOGY_VERSION,
                    hypothesis=hypothesis,
                    config_fingerprint=fingerprint,
                    status=STATUS_ACTIVE,
                )
            except ExperimentImmutabilityError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                sys.exit(1)
            print(f"{experiment_id}: {'inserted' if inserted else 'already registered (no-op)'}")


if __name__ == "__main__":
    main()
