"""CLI to evaluate configured volatility (day-over-day % move) thresholds
and generate eligible alerts, delivered over two independent channels
(email + Discord). Mirrors alerts/run_price_alerts.py's contract exactly.

Dry-run is the default and safe: it evaluates conditions, persists what
*would* have fired (marked dry_run=1 in the volatility_alerts table), and
never sends email or contacts Discord. A real send requires the explicit
--send flag.

Usage:
    python -m alerts.run_volatility_alerts                       # dry-run, all configured thresholds
    python -m alerts.run_volatility_alerts --tickers AAPL MSFT   # dry-run, specific tickers
    python -m alerts.run_volatility_alerts --send                # REAL email + Discord delivery - use deliberately
"""
import argparse
import logging
import sys

from alerts.volatility_config import DEFAULT_VOLATILITY_ALERT_CONFIG
from alerts.volatility_runner import run_volatility_alert_cycle
from config.settings import (
    ALERT_EMAIL_FROM,
    ALERT_EMAIL_TO,
    DISCORD_WEBHOOK_URL,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_USERNAME,
)
from db.database import db_session
from db.volatility_alert_config_repository import list_volatility_alert_configs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("run_volatility_alerts")

SMTP_CONFIGURED = bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and ALERT_EMAIL_FROM and ALERT_EMAIL_TO)
DISCORD_CONFIGURED = bool(DISCORD_WEBHOOK_URL)


def print_summary(results):
    print(f"\n{'=' * 70}")
    for r in results:
        e = r.evaluation
        if not e.ok:
            print(f"{e.ticker:6s} SKIPPED - {e.reason_unavailable}")
            continue

        if r.fired:
            email_mode = "SENT" if r.delivery and r.delivery.ok else ("DRY-RUN" if r.delivery is None else "SEND FAILED")
            discord_mode = (
                "SENT" if r.discord_delivery and r.discord_delivery.ok
                else ("DRY-RUN" if r.discord_delivery is None else "SEND FAILED")
            )
            move_pct = e.move_pct if e.move_pct is not None else 0.0
            print(f"{e.ticker:6s} ALERT [email={email_mode} discord={discord_mode}] move={move_pct:+.2f}% "
                  f"(threshold {e.threshold_percent:.2f}%) close={e.current_price:.2f} (prev={e.previous_price})")
            if r.delivery and not r.delivery.ok:
                print(f"         email delivery error: {r.delivery.error}")
            if r.discord_delivery and not r.discord_delivery.ok:
                print(f"         discord delivery error: {r.discord_delivery.error}")
        elif r.suppressed_already_alerted_today:
            print(f"{e.ticker:6s} suppressed (already alerted for {e.data_date}) - move was {e.move_pct:+.2f}%")
        elif r.suppressed_by_cooldown:
            print(f"{e.ticker:6s} suppressed (cooldown active) - move was {e.move_pct:+.2f}%")
        elif e.previous_price is None:
            print(f"{e.ticker:6s} baseline established (close={e.current_price:.2f}) - no prior close to compare against")
        else:
            print(f"{e.ticker:6s} no significant move - move={e.move_pct:+.2f}% (threshold {e.threshold_percent:.2f}%)")
    print(f"{'=' * 70}")
    print(f"{sum(1 for r in results if r.fired)} alert(s) fired out of {len(results)} ticker(s) evaluated.")


def main():
    with db_session() as conn:
        configured = list_volatility_alert_configs(conn)

    parser = argparse.ArgumentParser(description="Evaluate configured volatility thresholds and generate email + Discord alerts")
    configured_tickers = [c.ticker for c in configured]
    parser.add_argument("--tickers", nargs="+", default=configured_tickers)
    parser.add_argument(
        "--send", action="store_true",
        help="Actually deliver via email and Discord. Without this flag, runs in dry-run mode: "
             "evaluates and persists alerts but never sends email or contacts Discord.",
    )
    args = parser.parse_args()

    if args.send:
        if not (SMTP_CONFIGURED or DISCORD_CONFIGURED):
            print("ERROR: --send was passed but neither SMTP/email nor DISCORD_WEBHOOK_URL is configured in the environment.", file=sys.stderr)
            sys.exit(1)
        print("REAL SEND MODE: eligible alerts will be delivered by email and/or Discord.")
        if not SMTP_CONFIGURED:
            print("  NOTE: SMTP/email settings are not fully configured - email delivery will fail safe for every alert.")
        if not DISCORD_CONFIGURED:
            print("  NOTE: DISCORD_WEBHOOK_URL is not configured - Discord delivery will fail safe for every alert.")
    else:
        print("DRY-RUN MODE (default): no email or Discord messages will be sent.")

    configs = [c for c in configured if c.ticker in args.tickers]

    with db_session() as conn:
        results = run_volatility_alert_cycle(conn, configs=configs, config=DEFAULT_VOLATILITY_ALERT_CONFIG, send=args.send)

    print_summary(results)
    sys.exit(0)


if __name__ == "__main__":
    main()
