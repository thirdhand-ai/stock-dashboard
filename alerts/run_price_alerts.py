"""CLI to evaluate configured price thresholds and generate eligible alerts,
delivered over two independent channels (email + Discord). Mirrors
alerts/run_alerts.py's contract exactly.

Dry-run is the default and safe: it evaluates conditions, persists what
*would* have fired (marked dry_run=1 in the price_alerts table), and never
sends email or contacts Discord. A real send requires the explicit --send
flag - it is never triggered just because SMTP/DISCORD_WEBHOOK_URL happens
to be configured. Each channel fails safe on its own if unconfigured (see
alerts/email.py, alerts/discord.py) - --send only refuses to run at all if
NEITHER channel is configured, since that would silently deliver nothing.

Usage:
    python -m alerts.run_price_alerts                       # dry-run, all configured thresholds
    python -m alerts.run_price_alerts --tickers AAPL MSFT   # dry-run, specific tickers
    python -m alerts.run_price_alerts --send                # REAL email + Discord delivery - use deliberately
"""
import argparse
import logging
import sys

from alerts.price_config import DEFAULT_PRICE_ALERT_CONFIG
from alerts.price_runner import run_price_alert_cycle
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("run_price_alerts")

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
            print(f"{e.ticker:6s} ALERT [email={email_mode} discord={discord_mode}] reasons={e.reasons} "
                  f"price={e.current_price:.2f} (prev={e.previous_price})")
            if r.delivery and not r.delivery.ok:
                print(f"         email delivery error: {r.delivery.error}")
            if r.discord_delivery and not r.discord_delivery.ok:
                print(f"         discord delivery error: {r.discord_delivery.error}")
        elif r.suppressed_by_cooldown:
            print(f"{e.ticker:6s} suppressed (cooldown active) - reasons would have been {e.reasons}")
        elif e.previous_price is None:
            print(f"{e.ticker:6s} baseline established (price={e.current_price:.2f}) - no prior state to compare against")
        else:
            print(f"{e.ticker:6s} no change - price={e.current_price:.2f}")
    print(f"{'=' * 70}")
    print(f"{sum(1 for r in results if r.fired)} alert(s) fired out of {len(results)} ticker(s) evaluated.")


def main():
    with db_session() as conn:
        configured_thresholds = list_price_alert_configs(conn)

    parser = argparse.ArgumentParser(description="Evaluate configured price thresholds and generate email + Discord alerts")
    configured_tickers = [t.ticker for t in configured_thresholds]
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

    thresholds = [t for t in configured_thresholds if t.ticker in args.tickers]

    with db_session() as conn:
        results = run_price_alert_cycle(conn, thresholds=thresholds, config=DEFAULT_PRICE_ALERT_CONFIG, send=args.send)

    print_summary(results)
    sys.exit(0)


if __name__ == "__main__":
    main()
