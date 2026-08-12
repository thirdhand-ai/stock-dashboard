"""CLI to evaluate the configured watchlist and generate eligible alerts.

Dry-run is the default and safe: it evaluates conditions, persists what
*would* have fired (marked dry_run=1 in the alerts table), and never
contacts Discord. A real send requires the explicit --send flag - it is
never triggered just because DISCORD_WEBHOOK_URL happens to be set.

Usage:
    python -m alerts.run_alerts                       # dry-run, full watchlist
    python -m alerts.run_alerts --tickers AAPL MSFT    # dry-run, specific tickers
    python -m alerts.run_alerts --send                 # REAL Discord delivery - use deliberately
"""
import argparse
import logging
import sys

from alerts.config import DEFAULT_ALERT_CONFIG
from alerts.runner import run_alert_cycle
from config.settings import DISCORD_WEBHOOK_URL, WATCHLIST
from db.database import db_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("run_alerts")


def print_summary(results):
    print(f"\n{'=' * 70}")
    for r in results:
        e = r.evaluation
        if not e.ok:
            print(f"{e.ticker:6s} SKIPPED - {e.reason_unavailable}")
            continue

        if r.fired:
            mode = "SENT" if r.delivery and r.delivery.ok else ("DRY-RUN" if r.delivery is None else "SEND FAILED")
            print(f"{e.ticker:6s} ALERT [{mode}] reasons={e.reasons} score={e.current_score:.0f} "
                  f"(prev={e.previous_score}) stage={e.current_stage} (prev={e.previous_stage})")
            if r.delivery and not r.delivery.ok:
                print(f"         delivery error: {r.delivery.error}")
        elif r.suppressed_by_cooldown:
            print(f"{e.ticker:6s} suppressed (cooldown active) - reasons would have been {e.reasons}")
        elif e.previous_score is None:
            print(f"{e.ticker:6s} baseline established (score={e.current_score:.0f}, stage={e.current_stage}) - no prior state to compare against")
        else:
            print(f"{e.ticker:6s} no change - score={e.current_score:.0f} stage={e.current_stage}")
    print(f"{'=' * 70}")
    print(f"{sum(1 for r in results if r.fired)} alert(s) fired out of {len(results)} ticker(s) evaluated.")


def main():
    parser = argparse.ArgumentParser(description="Evaluate the watchlist and generate signal alerts")
    parser.add_argument("--tickers", nargs="+", default=WATCHLIST)
    parser.add_argument(
        "--send", action="store_true",
        help="Actually deliver to Discord. Without this flag, runs in dry-run mode: "
             "evaluates and persists alerts but never contacts Discord.",
    )
    args = parser.parse_args()

    if args.send:
        if not DISCORD_WEBHOOK_URL:
            print("ERROR: --send was passed but DISCORD_WEBHOOK_URL is not configured in the environment.", file=sys.stderr)
            sys.exit(1)
        print("REAL SEND MODE: eligible alerts will be delivered to Discord.")
    else:
        print("DRY-RUN MODE (default): no Discord messages will be sent.")

    with db_session() as conn:
        results = run_alert_cycle(conn, tickers=args.tickers, config=DEFAULT_ALERT_CONFIG, send=args.send)

    print_summary(results)
    sys.exit(0)


if __name__ == "__main__":
    main()
