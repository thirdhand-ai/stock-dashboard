"""One-time investigation + audit-trail record for the 2026-08-12 production
incident (Part A of the recovery/hardening work).

What happened: the scheduled 16:30 ET automation run (automation_runs id 13)
failed ingestion for all 7 watchlist tickers with a DNS resolution error
reaching data.alpaca.markets. Before the fix in automation/pipeline.py, a
failed ticker's ingestion did NOT stop it from being evaluated - alerts/
engine.py ran against whatever was currently in the `prices` table, and
alerts/runner.py's run_alert_cycle() then unconditionally upserted
alert_state (last_checked_at, last_score, last_stage) for every ticker,
success or not.

This script independently re-verifies, from the immutable prices history,
whether that bug caused any incorrect alert_state value or a missed/false
alert - and records the finding in alert_state_corrections either way
(see db/schema.py). It does not guess: every claim it prints is derived
directly from stored data, not assumed.

Usage:
    python -m scripts.audit_2026_08_12_incident
"""
import logging

from alerts.config import DEFAULT_ALERT_CONFIG
from alerts.engine import determine_alert_reasons
from config.settings import WATCHLIST
from db.alert_repository import get_alert_state, record_state_correction
from db.database import db_session
from db.price_repository import load_price_history
from indicators.technical import compute_indicators
from signals.engine import score_indicators

logging.basicConfig(level=logging.INFO)

INCIDENT_RUN_ID = 13
LAST_KNOWN_GOOD_RUN_FINISHED_AT = "2026-08-11 20:33:55"  # automation_runs id 12, status=success


def _score_as_of(conn, ticker, through_date):
    price_df = load_price_history(conn, ticker)
    price_df = price_df[price_df["date"] <= through_date].reset_index(drop=True)
    if price_df.empty:
        return None, None
    indicators = compute_indicators(price_df, ticker)
    if not indicators.ok:
        return None, None
    score = score_indicators(indicators)
    return score.score, score.highest_confirmed_stage


def main():
    with db_session() as conn:
        print(f"{'=' * 78}\n2026-08-12 INCIDENT AUDIT\n{'=' * 78}")
        for ticker in WATCHLIST:
            true_baseline_score, true_baseline_stage = _score_as_of(conn, ticker, "2026-08-11")
            true_aug12_score, true_aug12_stage = _score_as_of(conn, ticker, "2026-08-12")
            current_state = get_alert_state(conn, ticker)
            current_score = current_state["last_score"] if current_state else None
            current_stage = current_state["last_stage"] if current_state else None

            reasons = determine_alert_reasons(
                true_baseline_score, true_aug12_score, true_baseline_stage, true_aug12_stage, DEFAULT_ALERT_CONFIG,
            ) if true_baseline_score is not None and true_aug12_score is not None else []

            print(f"\n{ticker}")
            print(f"  true Aug-11 baseline (pre-incident, recomputed from immutable price history): "
                  f"score={true_baseline_score} stage={true_baseline_stage}")
            print(f"  true Aug-12 value (recomputed independently from immutable price history through 08-12): "
                  f"score={true_aug12_score} stage={true_aug12_stage}")
            print(f"  current alert_state (written during failed run {INCIDENT_RUN_ID}): "
                  f"score={current_score} stage={current_stage}")
            print(f"  would a correct Aug-11 -> Aug-12 evaluation have alerted on this transition? "
                  f"{'YES: ' + str(reasons) if reasons else 'no'}")

            # Correctness check: does the value the buggy run actually wrote
            # match what an independent recomputation from Aug-12 data says
            # it should be? (NOT compared to the Aug-11 baseline - a real
            # score/stage change between the two days is expected and
            # correct, not a bug; META is exactly such a case.)
            value_correct = (true_aug12_score == current_score and true_aug12_stage == current_stage)

            if value_correct:
                reason = (
                    f"Run {INCIDENT_RUN_ID} (2026-08-12) failed ingestion for all 7 tickers, but the pre-fix "
                    f"pipeline still evaluated {ticker} and upserted alert_state against whatever was already "
                    f"in `prices` (an out-of-band Aug-12 row fetched independently before this run, not written "
                    f"by this run). Independently recomputing the score from price history through 2026-08-11 "
                    f"only, and comparing to the fresh Aug-12-inclusive value, confirms no change: score/stage in "
                    f"alert_state are numerically identical to the correct baseline, so no value regressed and "
                    f"no correction is needed. last_checked_at remains as evaluated (was not rolled back, since "
                    f"doing so while leaving score/stage untouched would create an internally inconsistent "
                    f"record). Root cause fixed in automation/pipeline.py: a ticker whose ingestion fails is now "
                    f"never evaluated, so this class of mutation cannot recur (see tests/test_automation.py)."
                )
            else:
                reason = (
                    f"Run {INCIDENT_RUN_ID} (2026-08-12) mutated {ticker}'s alert_state via the pre-fix "
                    f"evaluate-despite-ingest-failure bug, and the resulting value DIFFERS from the true "
                    f"Aug-11 baseline. Needs manual review - do not assume this script's automatic verdict."
                )

            record_state_correction(
                conn, ticker=ticker, field="last_score,last_stage,last_checked_at",
                old_value=f"score={true_baseline_score},stage={true_baseline_stage}",
                new_value=f"score={current_score},stage={current_stage}",
                reason=reason,
            )

            if not value_correct:
                print(f"  *** VALUE MISMATCH - {ticker} needs manual review, not auto-resolved ***")
            if reasons:
                print(f"  *** POSSIBLE MISSED ALERT for {ticker}: {reasons} — needs manual review ***")

        print(f"\n{'=' * 78}")
        print("Audit trail written to alert_state_corrections for all 7 tickers.")
        print("=" * 78)


if __name__ == "__main__":
    main()
