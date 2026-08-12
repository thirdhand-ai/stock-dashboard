"""One-off CLI to send a SINGLE controlled Discord TEST notification -
verifies webhook delivery end-to-end without touching any real ticker's
alert state or market data. Isolated from alerts/run_alerts.py entirely.

Requires --confirm to reduce the chance of accidental invocation; this
performs one real network request to Discord when run without --dry-run.

Usage:
    python -m alerts.send_test_notification --confirm            # real send
    python -m alerts.send_test_notification --confirm --dry-run  # build/print only, no network call
"""
import argparse
import sys

from alerts.test_notification import build_test_payload, send_test_notification
from config.settings import DISCORD_WEBHOOK_URL
from db.database import db_session


def main():
    parser = argparse.ArgumentParser(description="Send one controlled Discord test notification")
    parser.add_argument(
        "--confirm", action="store_true", required=True,
        help="Required. Without --dry-run, this sends one real Discord message.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Build and print the payload only - no network call")
    parser.add_argument("--no-persist", action="store_true", help="Don't write a record to the alerts table")
    args = parser.parse_args()

    if args.dry_run:
        import json
        print("DRY RUN: payload that would be sent (no network call made):\n")
        print(json.dumps(build_test_payload(), indent=2))
        sys.exit(0)

    if not DISCORD_WEBHOOK_URL:
        print("ERROR: DISCORD_WEBHOOK_URL is not configured in the environment.", file=sys.stderr)
        sys.exit(1)

    print("Sending ONE real Discord test notification...")
    if args.no_persist:
        result = send_test_notification(conn=None, persist=False)
    else:
        with db_session() as conn:
            result = send_test_notification(conn=conn, persist=True)

    if result.ok:
        print(f"SUCCESS: Discord accepted the request (HTTP {result.status_code}).")
        sys.exit(0)
    else:
        print(f"FAILED: {result.error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
