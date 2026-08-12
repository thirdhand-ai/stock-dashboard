"""Production automation CLI: one complete daily cycle -
ingest -> compute signals -> evaluate alerts -> (optionally) send -> log.

Dry-run is the default and safe: it refreshes data, evaluates alert
conditions, and persists what *would* have fired, but never contacts
Discord. Real delivery requires the explicit --send flag - exactly the
same gate alerts/run_alerts.py uses, reused here rather than re-invented.

Usage:
    python -m automation.run_daily                      # dry-run, full watchlist
    python -m automation.run_daily --tickers AAPL MSFT   # dry-run, specific tickers
    python -m automation.run_daily --send                # REAL Discord delivery - use deliberately
    python -m automation.run_daily --force-run            # bypass the NYSE trading-day check

Exit codes:
    0  success (including a skipped non-trading-day run)
    1  partial failure - some tickers failed, others succeeded
    2  total failure - every ticker failed, or an unhandled pipeline error
    3  skipped - another run already holds the lock (see automation/lock.py)
"""
import argparse
import logging
import sys
from datetime import date

from automation.config import DEFAULT_PIPELINE_CONFIG
from automation.lock import LockHeldError, acquire_run_lock
from automation.pipeline import run_pipeline
from config.settings import DISCORD_WEBHOOK_URL, WATCHLIST
from db.database import db_session
from db.run_history_repository import (
    STATUS_FAILED,
    STATUS_PARTIAL_FAILURE,
    STATUS_SKIPPED_NON_TRADING_DAY,
    STATUS_SUCCESS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("run_daily")

EXIT_SUCCESS = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_TOTAL_FAILURE = 2
EXIT_LOCK_HELD = 3


def print_summary(result):
    print(f"\n{'=' * 70}")
    print(f"Run status: {result.status}   send_mode: {result.send_mode}")
    if result.skipped_reason:
        print(f"Reason: {result.skipped_reason}")
    for o in result.outcomes:
        ingest_state = f"ingested {o.ingest_rows} rows" if o.ingest_ok else f"INGEST FAILED: {o.ingest_error}"
        if not o.ingest_ok:
            # Fail-closed (see automation/pipeline.py): a ticker whose fresh
            # ingestion failed this run is never evaluated. Show the last
            # DURABLY STORED signal only, clearly labeled as stored (not
            # today's fresh result) - never presented as this run's output.
            if o.last_known_score is not None:
                alert_state = (
                    f"INGESTION FAILED — SIGNAL NOT EVALUATED "
                    f"(last known stored signal: score={o.last_known_score:.0f} stage={o.last_known_stage} "
                    f"as of {o.last_known_checked_at})"
                )
            else:
                alert_state = "INGESTION FAILED — SIGNAL NOT EVALUATED (no prior stored signal)"
        elif o.evaluation_error:
            alert_state = f"EVAL FAILED: {o.evaluation_error}"
        elif o.alert_result is None:
            alert_state = "not evaluated"
        elif not o.alert_result.evaluation.ok:
            alert_state = f"unavailable: {o.alert_result.evaluation.reason_unavailable}"
        elif o.alert_result.fired:
            alert_state = f"ALERT FIRED: {o.alert_result.evaluation.reasons}"
        elif o.alert_result.suppressed_by_cooldown:
            alert_state = "suppressed (cooldown)"
        elif o.alert_result.evaluation.previous_score is None:
            alert_state = f"baseline established (score={o.alert_result.evaluation.current_score})"
        else:
            alert_state = f"no change (score={o.alert_result.evaluation.current_score})"
        print(f"  {o.ticker:6s} {ingest_state:32s} | {alert_state}")
    print(f"{'=' * 70}")
    print(f"Attempted: {result.tickers_attempted}  Updated: {result.tickers_updated}  "
          f"Failed: {result.tickers_failed}  Alerts generated: {result.alerts_generated}")


def main():
    parser = argparse.ArgumentParser(description="Run one complete automation cycle")
    parser.add_argument("--tickers", nargs="+", default=WATCHLIST)
    parser.add_argument(
        "--send", action="store_true",
        help="Actually deliver eligible alerts to Discord. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--source", default=None, choices=["alpaca", "yfinance"],
        help="Override the configured price data source for this run (default: config's price_source, 'alpaca').",
    )
    parser.add_argument(
        "--force-run", action="store_true",
        help="Run even if today is not an NYSE trading day (weekend or market holiday). Does not bypass the lock.",
    )
    args = parser.parse_args()

    if args.send:
        if not DISCORD_WEBHOOK_URL:
            print("ERROR: --send was passed but DISCORD_WEBHOOK_URL is not configured in the environment.", file=sys.stderr)
            sys.exit(EXIT_TOTAL_FAILURE)
        logger.info("REAL SEND MODE: eligible alerts will be delivered to Discord.")
    else:
        logger.info("DRY-RUN MODE (default): no Discord messages will be sent.")

    try:
        with acquire_run_lock(DEFAULT_PIPELINE_CONFIG.lock_path):
            with db_session() as conn:
                result = run_pipeline(
                    conn,
                    tickers=args.tickers,
                    send=args.send,
                    price_source=args.source,
                    today=date.today(),
                    skip_non_trading_day_check=args.force_run,
                )
    except LockHeldError as e:
        logger.error(str(e))
        print(f"SKIPPED: {e}")
        sys.exit(EXIT_LOCK_HELD)

    print_summary(result)

    if result.status in (STATUS_FAILED, STATUS_PARTIAL_FAILURE):
        # Disabled by default (see alerts/ops_notifications.py) - this call
        # is a documented no-op until OPERATIONAL_ALERTS_ENABLED is
        # explicitly flipped on after separate approval.
        from alerts.ops_notifications import send_operational_failure_notification
        failure_summary = "; ".join(f"{o.ticker}: {o.ingest_error}" for o in result.outcomes if not o.ingest_ok)
        with db_session() as conn:
            send_operational_failure_notification(conn, trading_date=date.today(), error_summary=failure_summary)

    if result.status in (STATUS_SUCCESS, STATUS_SKIPPED_NON_TRADING_DAY):
        sys.exit(EXIT_SUCCESS)
    elif result.status == STATUS_PARTIAL_FAILURE:
        sys.exit(EXIT_PARTIAL_FAILURE)
    else:
        sys.exit(EXIT_TOTAL_FAILURE)


if __name__ == "__main__":
    main()
