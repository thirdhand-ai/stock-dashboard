"""CLI entry point for the Phase 11 research-only automation job:
`python -m strategy_lab.run_research_job`.

This is what a SEPARATE research LaunchAgent would invoke, roughly 10-15
minutes after the production job (~16:30 ET) - see the Phase 11 final
report for the exact (unexecuted) LaunchAgent config this would require,
which needs explicit approval before being activated. This script takes no
trade/send arguments because strategy_lab.research_automation has no code
path that could place an order or send Discord.
"""
import logging
from datetime import date

from db.database import db_session
from strategy_lab.research_automation import run_research_job

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    with db_session() as conn:
        result = run_research_job(conn, today=date.today())

    print(f"\n{'=' * 70}")
    print(f"Research job run_id={result.run_id}  status={result.status}  trading_date={result.trading_date}")
    if result.skip_reason:
        print(f"Skip reason: {result.skip_reason}")
    print(f"Tickers attempted: {result.tickers_attempted}")
    print(f"Observations created: {result.observations_created}  Duplicates skipped: {result.duplicates_skipped}")
    print(f"Events created: {result.events_created}")
    print(f"Outcomes matured this run: {result.outcomes_matured}")
    if result.errors:
        print(f"Errors ({len(result.errors)}): {result.errors}")
    print("=" * 70)


if __name__ == "__main__":
    main()
