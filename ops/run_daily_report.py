"""CLI entry point: `python -m ops.run_daily_report [--date YYYY-MM-DD]`.

Builds the daily research report, prints a human-readable rendering, then
persists it. Exit code 0 always - a report that shows a FAILED production
run is still a successful report run (mirrors
`strategy_lab/run_research_job.py`'s own separation of "the job ran fine"
from "what it found").
"""
import argparse
import json
from datetime import date

from db.database import db_session
from ops.daily_report import build_daily_report, render_report_text, report_to_dict
from ops.daily_report_repository import upsert_report


def main():
    parser = argparse.ArgumentParser(description="Build and persist the Phase 12 daily research report.")
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD (defaults to today)")
    args = parser.parse_args()

    today = date.fromisoformat(args.date) if args.date else date.today()

    with db_session() as conn:
        report = build_daily_report(conn, today=today)
        print(render_report_text(report))
        upsert_report(conn, report.report_date, json.dumps(report_to_dict(report), default=str))


if __name__ == "__main__":
    main()
