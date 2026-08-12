"""Phase 7 CLI: evaluate the watchlist's signals against current Alpaca
paper positions/orders, apply risk checks, and either just report what
would happen (--dry-run, the default) or submit qualifying orders to
Alpaca's PAPER endpoint (--paper-send).

Usage:
    python -m trading.run_paper                      # dry-run (default), full watchlist
    python -m trading.run_paper --dry-run             # same, explicit
    python -m trading.run_paper --tickers AAPL MSFT   # dry-run, specific tickers
    python -m trading.run_paper --paper-send          # submits qualifying PAPER orders - use deliberately

There is no --live flag and no way to point this at a live-money endpoint -
see trading/client.py's verify_paper_environment(), which this CLI calls
before doing anything else (via trading/engine.py) and which fails closed
(raises, exits nonzero) if the paper environment cannot be confirmed.

Exit codes:
    0  success
    2  paper environment could not be confirmed, or an unhandled error occurred
"""
import argparse
import logging
import sys

from config.settings import WATCHLIST
from db.database import db_session
from trading.client import PaperEnvironmentUnconfirmedError
from trading.engine import MODE_DRY_RUN, MODE_PAPER_SEND, run_cycle

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("run_paper")

EXIT_SUCCESS = 0
EXIT_ERROR = 2


def print_summary(result):
    print(f"\n{'=' * 78}")
    print(f"Mode: {result.mode}   paper_account_confirmed: {result.paper_account_confirmed}")
    print(
        f"Equity: ${result.account_equity:,.2f}   Cash: ${result.account_cash:,.2f}   "
        f"Buying power: ${result.account_buying_power:,.2f}"
    )
    print(f"{'=' * 78}")
    if not result.actions:
        print("No entry/exit candidates this cycle.")
    for a in result.actions:
        c = a.candidate
        print(
            f"  {c.ticker:6s} {c.intent:6s} {c.side:4s} outcome={a.outcome:18s} "
            f"score={c.score:.1f} stage={c.stage:9s} {a.detail or ''}"
        )
    print(f"{'=' * 78}")
    print(f"Proposed: {result.proposed_count}  Submitted: {result.submitted_count}  Rejected: {result.rejected_count}")


def main():
    parser = argparse.ArgumentParser(description="Evaluate signals and (optionally) submit Alpaca PAPER orders")
    parser.add_argument("--tickers", nargs="+", default=WATCHLIST)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run", action="store_true",
        help="Calculate intended orders but submit nothing (default behavior even without this flag).",
    )
    mode_group.add_argument(
        "--paper-send", action="store_true",
        help="Submit qualifying orders to Alpaca's PAPER endpoint. Never touches a live account - see trading/client.py.",
    )
    args = parser.parse_args()

    mode = MODE_PAPER_SEND if args.paper_send else MODE_DRY_RUN
    logger.info("%s MODE", "PAPER-SEND" if mode == MODE_PAPER_SEND else "DRY-RUN (default)")

    try:
        with db_session() as conn:
            result = run_cycle(conn, tickers=args.tickers, mode=mode)
    except PaperEnvironmentUnconfirmedError as e:
        logger.error("paper environment could not be confirmed: %s", e)
        print(f"ABORTED: {e}", file=sys.stderr)
        sys.exit(EXIT_ERROR)
    except Exception as e:
        logger.error("run_paper crashed: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(EXIT_ERROR)

    print_summary(result)
    sys.exit(EXIT_SUCCESS)


if __name__ == "__main__":
    main()
