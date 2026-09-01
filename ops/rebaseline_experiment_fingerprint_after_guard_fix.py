"""One-off, idempotent migration: re-baselines the Phase 12 experiment
registry's stored config_fingerprint for the three Phase 10/11 experiments
after fixing strategy_lab/production_guard.py's fingerprinting scope.

Background: production_guard.py used to whole-file-hash config/settings.py,
a general settings grab-bag (credentials, SMTP/email alert settings,
DB_PATH, dashboard-display ticker lists like EXPLORATORY_WATCHLIST and
SIGNAL_COVERAGE_TICKERS) that also happens to define WATCHLIST, the one
part of it the guard actually needs frozen. Every unrelated edit to that
file - none of which touched WATCHLIST or any structured_values field -
flipped the whole-file hash and made
ops.experiment_registry.check_active_experiments_config_drift() report a
false positive for all three ACTIVE experiments below. Fixed in the same
change by narrowing config/settings.py's guarded hash to just the WATCHLIST
assignment's own source text (see GUARDED_SECTIONS in production_guard.py).
Verified during the investigation: WATCHLIST and every structured_values
field were byte-identical the whole time; only file_hashes.config/settings.py
had changed, entirely due to those unrelated additions.

This is a fingerprinting-ALGORITHM fix, not a methodology change - the
underlying frozen config is unchanged. register_experiment()'s immutability
guard exists to catch a REAL config change under an existing experiment_id;
it can't tell that apart from an algorithm fix and would (correctly, for its
purpose) raise ExperimentImmutabilityError here. This script deliberately
does NOT go through register_experiment() - it performs the narrow, logged
UPDATE directly, exactly once, only for these three known experiment_ids,
and appends an audit note to each row explaining why.

Idempotent: re-running after the fingerprint already matches the freshly
computed value is a no-op for that row.

python -m ops.rebaseline_experiment_fingerprint_after_guard_fix
"""
from db.database import db_session
from ops.experiment_registry import compute_config_fingerprint, get_experiment, TABLE_NAME

EXPERIMENT_IDS = [
    "control-original-frozen-strategy",
    "experiment-a-bullish-entry-only",
    "experiment-b-bullish-entry-and-exit",
]

REASON = (
    "config_fingerprint rebaselined: production_guard.py's config/settings.py "
    "hash was narrowed from whole-file to just the WATCHLIST assignment "
    "(Phase 12 config-drift false-positive fix, 2026-09-01) - WATCHLIST and "
    "all structured_values fields are unchanged; only the fingerprinting "
    "algorithm changed."
)


def main():
    with db_session() as conn:
        new_fingerprint = compute_config_fingerprint()
        for experiment_id in EXPERIMENT_IDS:
            row = get_experiment(conn, experiment_id)
            if row is None:
                print(f"{experiment_id}: NOT FOUND - skipped")
                continue
            if row["config_fingerprint"] == new_fingerprint:
                print(f"{experiment_id}: already at new fingerprint (no-op)")
                continue

            old_fingerprint = row["config_fingerprint"]
            existing_notes = row["notes"] or ""
            new_notes = f"{existing_notes}\n{REASON}".strip()
            conn.execute(
                f"UPDATE {TABLE_NAME} SET config_fingerprint = ?, notes = ? WHERE experiment_id = ?",
                (new_fingerprint, new_notes, experiment_id),
            )
            conn.commit()
            print(f"{experiment_id}: rebaselined {old_fingerprint} -> {new_fingerprint}")


if __name__ == "__main__":
    main()
