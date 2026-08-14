"""CLI entry point: `python -m ops.generate_daily_summary [--date YYYY-MM-DD]`.

Builds the daily research report, builds the Discord-shaped summary
payload, and prints the resulting JSON to stdout ONLY. Never POSTs
anywhere, regardless of ops.discord_summary.DISCORD_SUMMARY_ENABLED's
value - that flag isn't even read by this CLI.
"""
import argparse
import json
from datetime import date

from db.database import db_session
from ops.daily_report import build_daily_report
from ops.discord_summary import build_daily_summary_payload


def main():
    parser = argparse.ArgumentParser(
        description="Generate the Phase 12 Discord-shaped daily summary payload (stdout only, no network call)."
    )
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (defaults to today)")
    args = parser.parse_args()

    today = date.fromisoformat(args.date) if args.date else date.today()

    with db_session() as conn:
        report = build_daily_report(conn, today=today)

    payload = build_daily_summary_payload(report)
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
