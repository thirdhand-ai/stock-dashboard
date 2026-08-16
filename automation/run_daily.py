"""Production automation CLI: one complete daily cycle -
ingest -> compute signals -> evaluate alerts -> (optionally) send -> log.

Dry-run is the default and safe: it refreshes data, evaluates alert
conditions, and persists what *would* have fired, but never contacts
Discord. Real delivery requires the explicit --send flag - exactly the
same gate alerts/run_alerts.py uses, reused here rather than re-invented.

Usage:
    python -m automation.run_daily                      # dry-run, WATCHLIST + configured price-alert tickers
    python -m automation.run_daily --tickers AAPL MSFT   # dry-run, specific tickers (exact list, no union)
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
from config.settings import (
    ALERT_EMAIL_FROM,
    ALERT_EMAIL_TO,
    DISCORD_WEBHOOK_URL,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_USERNAME,
)
from db.database import db_session
from db.price_alert_config_repository import list_price_alert_configs
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

        if o.ingest_ok and o.price_alert_result is not None:
            pr = o.price_alert_result
            if not pr.evaluation.ok:
                price_state = f"price alert unavailable: {pr.evaluation.reason_unavailable}"
            elif pr.fired:
                price_state = f"PRICE ALERT FIRED: {pr.evaluation.reasons}"
            elif pr.suppressed_by_cooldown:
                price_state = "price alert suppressed (cooldown)"
            elif pr.evaluation.previous_price is None:
                price_state = f"price baseline established (price={pr.evaluation.current_price})"
            else:
                price_state = f"price: no change (price={pr.evaluation.current_price})"
            print(f"  {' ' * 6} {' ' * 32} | {price_state}")
        elif o.price_evaluation_error:
            print(f"  {' ' * 6} {' ' * 32} | PRICE EVAL FAILED: {o.price_evaluation_error}")

        if o.ingest_ok and o.volatility_alert_result is not None:
            vr = o.volatility_alert_result
            if not vr.evaluation.ok:
                volatility_state = f"volatility alert unavailable: {vr.evaluation.reason_unavailable}"
            elif vr.fired:
                volatility_state = f"VOLATILITY ALERT FIRED: {vr.evaluation.reasons} ({vr.evaluation.move_pct:+.2f}%)"
            elif vr.suppressed_already_alerted_today:
                volatility_state = "volatility alert suppressed (already alerted for this trading day)"
            elif vr.suppressed_by_cooldown:
                volatility_state = "volatility alert suppressed (cooldown)"
            elif vr.evaluation.previous_price is None:
                volatility_state = f"volatility baseline established (close={vr.evaluation.current_price})"
            else:
                volatility_state = f"volatility: no significant move (move={vr.evaluation.move_pct:+.2f}%)"
            print(f"  {' ' * 6} {' ' * 32} | {volatility_state}")
        elif o.volatility_evaluation_error:
            print(f"  {' ' * 6} {' ' * 32} | VOLATILITY EVAL FAILED: {o.volatility_evaluation_error}")
    print(f"{'=' * 70}")
    print(f"Attempted: {result.tickers_attempted}  Updated: {result.tickers_updated}  "
          f"Failed: {result.tickers_failed}  Alerts generated: {result.alerts_generated}  "
          f"Price alerts generated: {result.price_alerts_generated}  "
          f"Volatility alerts generated: {result.volatility_alerts_generated}")


def main():
    parser = argparse.ArgumentParser(description="Run one complete automation cycle")
    parser.add_argument(
        "--tickers", nargs="+", default=None,
        help="Tickers to run. Default: WATCHLIST plus any ticker with a configured price alert threshold.",
    )
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
        smtp_configured = bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and ALERT_EMAIL_FROM and ALERT_EMAIL_TO)
        with db_session() as conn:
            has_price_thresholds = bool(list_price_alert_configs(conn))
        if has_price_thresholds and not smtp_configured:
            print("ERROR: --send was passed with price thresholds configured, but SMTP/email settings "
                  "are not fully configured in the environment.", file=sys.stderr)
            sys.exit(EXIT_TOTAL_FAILURE)
        logger.info("REAL SEND MODE: eligible alerts will be delivered to Discord/email.")
    else:
        logger.info("DRY-RUN MODE (default): no Discord messages or emails will be sent.")

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
        # Enabled as of 2026-08-16 (see alerts/ops_notifications.py's
        # docstring) - delivers over email + Discord independently, at most
        # once per trading_date.
        from alerts.ops_notifications import send_operational_failure_notification
        failure_summary = "; ".join(f"{o.ticker}: {o.ingest_error}" for o in result.outcomes if not o.ingest_ok)
        with db_session() as conn:
            notification_result = send_operational_failure_notification(conn, trading_date=date.today(), error_summary=failure_summary)
        logger.info(
            "operational failure notification: sent=%s reason=%s email_sent=%s email_error=%s discord_sent=%s discord_error=%s",
            notification_result.sent, notification_result.reason,
            notification_result.email_sent, notification_result.email_error,
            notification_result.discord_sent, notification_result.discord_error,
        )
        print(f"Operational failure notification: {notification_result.reason} "
              f"(email_sent={notification_result.email_sent}, discord_sent={notification_result.discord_sent})")

    if result.status in (STATUS_SUCCESS, STATUS_SKIPPED_NON_TRADING_DAY):
        sys.exit(EXIT_SUCCESS)
    elif result.status == STATUS_PARTIAL_FAILURE:
        sys.exit(EXIT_PARTIAL_FAILURE)
    else:
        sys.exit(EXIT_TOTAL_FAILURE)


if __name__ == "__main__":
    main()
