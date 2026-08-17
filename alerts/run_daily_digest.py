"""CLI to build and (optionally) send the daily digest - a single summary
message covering every tracked ticker's current price, day-over-day %
change, and distance to its configured price-threshold/volatility-alert
levels. Mirrors alerts/run_price_alerts.py's contract: dry-run by default,
--send required for real delivery.

Unlike alerts/run_price_alerts.py and alerts/run_volatility_alerts.py,
this always "fires" for every given ticker - there is no crossing/
threshold condition to satisfy, only the once-per-trading_date de-dupe
(db/daily_digest_repository.py) and, when run via automation/run_daily.py,
the dashboard-configured on/off toggle (db/daily_digest_config_repository.py).
This CLI does not check that toggle - it is a manual/testing entry point,
independent of whether the daily automation would have sent one.

Usage:
    python -m alerts.run_daily_digest                       # dry-run, WATCHLIST + configured tickers
    python -m alerts.run_daily_digest --tickers AAPL MSFT    # dry-run, specific tickers
    python -m alerts.run_daily_digest --send                 # REAL email + Discord delivery - use deliberately
"""
import argparse
import logging
import sys

from alerts.daily_digest_engine import format_ticker_digest_line
from alerts.daily_digest_runner import run_daily_digest
from config.settings import (
    ALERT_EMAIL_FROM,
    ALERT_EMAIL_TO,
    DISCORD_WEBHOOK_URL,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_USERNAME,
    WATCHLIST,
)
from db.database import db_session
from db.price_alert_config_repository import list_price_alert_configs
from db.volatility_alert_config_repository import list_volatility_alert_configs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("run_daily_digest")

SMTP_CONFIGURED = bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and ALERT_EMAIL_FROM and ALERT_EMAIL_TO)
DISCORD_CONFIGURED = bool(DISCORD_WEBHOOK_URL)


def print_summary(result):
    print(f"\n{'=' * 70}")
    for row in result.rows:
        print(format_ticker_digest_line(row))
    print(f"{'=' * 70}")

    if not result.sent:
        print(f"NOT SENT - {result.reason}")
        return

    if result.digest_id is None:
        print("PREVIEW (not persisted)")
        return

    email_mode = "SENT" if result.delivery and result.delivery.ok else ("DRY-RUN" if result.delivery is None else "SEND FAILED")
    discord_mode = (
        "SENT" if result.discord_delivery and result.discord_delivery.ok
        else ("DRY-RUN" if result.discord_delivery is None else "SEND FAILED")
    )
    print(f"Digest #{result.digest_id} [email={email_mode} discord={discord_mode}] covering {len(result.rows)} ticker(s).")
    if result.delivery and not result.delivery.ok:
        print(f"  email delivery error: {result.delivery.error}")
    if result.discord_delivery and not result.discord_delivery.ok:
        print(f"  discord delivery error: {result.discord_delivery.error}")


def main():
    with db_session() as conn:
        price_thresholds_by_ticker = {t.ticker: t for t in list_price_alert_configs(conn)}
        volatility_configs_by_ticker = {c.ticker: c for c in list_volatility_alert_configs(conn)}

    default_tickers = WATCHLIST + sorted(
        (set(price_thresholds_by_ticker) | set(volatility_configs_by_ticker)) - set(WATCHLIST)
    )

    parser = argparse.ArgumentParser(description="Build and optionally send the daily ticker digest")
    parser.add_argument("--tickers", nargs="+", default=default_tickers)
    parser.add_argument(
        "--send", action="store_true",
        help="Actually deliver via email and Discord. Without this flag, runs in dry-run mode: "
             "builds and persists the digest-log record but never sends email or contacts Discord.",
    )
    args = parser.parse_args()

    if args.send:
        if not (SMTP_CONFIGURED or DISCORD_CONFIGURED):
            print("ERROR: --send was passed but neither SMTP/email nor DISCORD_WEBHOOK_URL is configured in the environment.", file=sys.stderr)
            sys.exit(1)
        print("REAL SEND MODE: the digest will be delivered by email and/or Discord.")
    else:
        print("DRY-RUN MODE (default): no email or Discord messages will be sent.")

    with db_session() as conn:
        result = run_daily_digest(
            conn, tickers=args.tickers,
            price_thresholds_by_ticker=price_thresholds_by_ticker,
            volatility_configs_by_ticker=volatility_configs_by_ticker,
            send=args.send,
        )

    print_summary(result)
    sys.exit(0)


if __name__ == "__main__":
    main()
