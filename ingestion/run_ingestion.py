"""CLI entry point to test/run data ingestion from all Phase 1 sources.

Usage:
    python -m ingestion.run_ingestion --source all
    python -m ingestion.run_ingestion --source yfinance --tickers AAPL MSFT
    python -m ingestion.run_ingestion --source alpaca --days 400
    python -m ingestion.run_ingestion --source finnhub
"""
import argparse
import logging
import sys

from config.settings import WATCHLIST
from db.database import db_session
from ingestion import alpaca_source, finnhub_source, yfinance_source

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("run_ingestion")


def run_yfinance(conn, tickers):
    results = {}
    for ticker in tickers:
        try:
            n = yfinance_source.ingest_ticker(conn, ticker)
            results[ticker] = {"ok": True, "rows": n}
        except Exception as e:
            results[ticker] = {"ok": False, "error": str(e)}
            logger.error("yfinance failed for %s: %s", ticker, e)
    return results


def run_alpaca(conn, tickers, days=30):
    results = {"_account_check": None}
    try:
        results["_account_check"] = {"ok": True, **alpaca_source.check_paper_account()}
    except Exception as e:
        results["_account_check"] = {"ok": False, "error": str(e)}
        logger.error("alpaca account check failed: %s", e)
        return results

    for ticker in tickers:
        try:
            n = alpaca_source.ingest_ticker(conn, ticker, days=days)
            results[ticker] = {"ok": True, "rows": n}
        except Exception as e:
            results[ticker] = {"ok": False, "error": str(e)}
            logger.error("alpaca failed for %s: %s", ticker, e)
    return results


def run_finnhub(conn, tickers):
    results = {}
    for ticker in tickers:
        try:
            news_n, fund_n = finnhub_source.ingest_ticker(conn, ticker)
            results[ticker] = {"ok": True, "news_rows": news_n, "fundamentals_rows": fund_n}
        except Exception as e:
            results[ticker] = {"ok": False, "error": str(e)}
            logger.error("finnhub failed for %s: %s", ticker, e)
    return results


def print_summary(source, results):
    print(f"\n=== {source} ===")
    for key, val in results.items():
        status = "OK" if val and val.get("ok") else "FAILED"
        detail = {k: v for k, v in val.items() if k != "ok"} if val else {}
        print(f"  {key}: {status} {detail}")


def main():
    parser = argparse.ArgumentParser(description="Run Phase 1 data ingestion")
    parser.add_argument(
        "--source",
        choices=["yfinance", "alpaca", "finnhub", "all"],
        default="all",
    )
    parser.add_argument("--tickers", nargs="+", default=WATCHLIST[:3])
    parser.add_argument(
        "--days", type=int, default=30, help="Lookback window in calendar days for Alpaca bars"
    )
    args = parser.parse_args()

    any_failure = False

    with db_session() as conn:
        if args.source in ("yfinance", "all"):
            results = run_yfinance(conn, args.tickers)
            print_summary("yfinance", results)
            any_failure = any_failure or any(not v["ok"] for v in results.values())

        if args.source in ("alpaca", "all"):
            results = run_alpaca(conn, args.tickers, days=args.days)
            print_summary("alpaca (paper)", results)
            any_failure = any_failure or any(not v["ok"] for v in results.values())

        if args.source in ("finnhub", "all"):
            results = run_finnhub(conn, args.tickers)
            print_summary("finnhub", results)
            any_failure = any_failure or any(not v["ok"] for v in results.values())

    sys.exit(1 if any_failure else 0)


if __name__ == "__main__":
    main()
