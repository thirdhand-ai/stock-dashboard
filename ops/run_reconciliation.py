"""CLI entry point: `python -m ops.run_reconciliation`.

Builds a paper-portfolio reconciliation report (local `paper_orders` vs.
Alpaca's authoritative paper account state), prints a summary, and
persists it. Read-only against Alpaca; never submits/cancels/replaces/
closes anything.
"""
from db.database import db_session
from ops.reconciliation import build_reconciliation_report, record_check


def main():
    with db_session() as conn:
        report = build_reconciliation_report(conn)
        record_check(conn, report)

    print(f"\n{'=' * 78}")
    print(f"Paper portfolio reconciliation — overall status: {report.overall_status}")
    print(f"Checked at: {report.checked_at}")
    if report.alpaca_unreachable:
        print(f"Alpaca unreachable: {report.alpaca_error}")
    for row in report.tickers:
        print(
            f"  {row.ticker}: {row.status}  local_qty={row.local_qty} alpaca_qty={row.alpaca_qty}"
            f"  reasons={row.reasons}"
        )
    if report.amzn is not None:
        print(f"AMZN (designated live verification ticker): {report.amzn.status}")
    else:
        print("AMZN: no local or Alpaca state to reconcile")
    print("=" * 78)


if __name__ == "__main__":
    main()
