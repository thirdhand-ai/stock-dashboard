"""CLI entry point for Phase 10's prospective-validation system:
`python -m strategy_lab.run_prospective_observation`.

Records one immutable observation per research-universe ticker for today's
signal/regime state (strategy_lab/prospective.py). Read-only against Alpaca
market DATA (via the same cached-price path every other strategy_lab module
uses) - never constructs a trading client, never calls submit_order, never
sends a Discord message.

NOT currently scheduled anywhere - see the Phase 10 final report for the
exact (unexecuted) LaunchAgent change this would require, which needs
explicit approval before being made.
"""
import logging

from db.database import db_session
from strategy_lab.prospective import build_todays_observation, record_observation
from strategy_lab.universe import RESEARCH_UNIVERSE

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    recorded, skipped = 0, 0
    with db_session() as conn:
        for ticker in RESEARCH_UNIVERSE:
            obs = build_todays_observation(conn, ticker)
            if obs is None:
                skipped += 1
                continue
            record_observation(conn, **obs)
            recorded += 1
    print(f"Prospective observation pass complete: {recorded} tickers recorded/deduped, {skipped} skipped (insufficient data).")


if __name__ == "__main__":
    main()
