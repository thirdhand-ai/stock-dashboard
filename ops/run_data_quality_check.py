"""CLI entry point: `python -m ops.run_data_quality_check [--tickers ...]`.

Builds a data-quality/freshness report, prints a summary table, and
persists it via `record_check`. Never calls any ingestion function, never
switches source preference, never deletes/repairs a row.
"""
import argparse

from db.database import db_session
from ops.data_quality import check_watchlist_quality, record_check


def main():
    parser = argparse.ArgumentParser(description="Run the Phase 12 data-quality/freshness check.")
    parser.add_argument(
        "--tickers", nargs="*", default=None,
        help="Tickers to check (defaults to the production watchlist)",
    )
    args = parser.parse_args()

    with db_session() as conn:
        report = check_watchlist_quality(conn, tickers=args.tickers)
        record_check(conn, report)

    print(f"\n{'=' * 78}")
    print(f"Data quality check — overall status: {report.overall_status}")
    print(f"Checked at: {report.checked_at}")
    print(f"{'ticker':<8}{'status':<10}{'source':<10}{'last_date':<12}{'rows':<8}{'invalid':<9}{'dup':<6}{'gaps':<6}")
    for row in report.tickers:
        print(
            f"{row.ticker:<8}{row.status:<10}{(row.source or 'n/a'):<10}{(row.last_date or 'n/a'):<12}"
            f"{row.row_count_total:<8}{row.invalid_ohlcv_rows:<9}{row.duplicate_rows:<6}"
            f"{row.missing_trading_days_count:<6}"
        )
    print("=" * 78)


if __name__ == "__main__":
    main()
