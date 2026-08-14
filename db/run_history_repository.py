"""Read/write interface for automation run history (Phase 6).

One row per pipeline execution, created at start and completed at finish -
so a crashed run is still visible as a row stuck in status='running'
rather than vanishing silently. Never stores credentials or webhook values.

Phase 13 §5.2: `trading_date` is an additive, nullable column (see
db/schema.py::_migrate_add_trading_date_to_automation_runs) that lets a
caller ("was there a successful run FOR trading day X") match on an exact
NYSE trading date instead of string-matching a UTC timestamp against a
local calendar date.
"""
from datetime import date
from typing import Optional

import pandas as pd

RUN_COLUMNS = [
    "id", "started_at", "finished_at", "status", "send_mode",
    "tickers_attempted", "tickers_updated", "tickers_failed",
    "alerts_generated", "error_summary", "trading_date",
]

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_PARTIAL_FAILURE = "partial_failure"
STATUS_FAILED = "failed"
STATUS_SKIPPED_NON_TRADING_DAY = "skipped_non_trading_day"
STATUS_SKIPPED_OVERLAP = "skipped_overlap"

SEND_MODE_DRY_RUN = "dry_run"
SEND_MODE_REAL = "real"


def start_run(conn, send_mode: str, trading_date: Optional[date] = None, started_at: Optional[str] = None) -> int:
    """`started_at` override exists ONLY to make deterministic tests possible
    without needing the real wall clock - production code never passes it.
    When `started_at` IS explicitly passed (a test simulating a specific
    historical row), `trading_date` is stored exactly as given, including
    None/NULL, so tests can still construct legacy pre-migration-shaped rows.

    On a normal call (no `started_at` override - the real production path,
    and any direct caller like a test that doesn't override the clock),
    `trading_date` defaults to today's real local calendar date rather than
    NULL when not explicitly passed - this is the actual trading day the
    run pertains to (matches automation/pipeline.py's own `check_date`
    convention), so any caller of start_run() gets correct exact-match
    trading_date semantics without needing to thread the value through
    manually."""
    if started_at is not None:
        trading_date_str = trading_date.isoformat() if trading_date is not None else None
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO automation_runs (status, send_mode, trading_date, started_at) VALUES (?, ?, ?, ?)",
            (STATUS_RUNNING, send_mode, trading_date_str, started_at),
        )
        conn.commit()
        return cur.lastrowid
    trading_date_str = trading_date.isoformat() if trading_date is not None else date.today().isoformat()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO automation_runs (status, send_mode, trading_date) VALUES (?, ?, ?)",
        (STATUS_RUNNING, send_mode, trading_date_str),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(
    conn,
    run_id: int,
    status: str,
    tickers_attempted: int = 0,
    tickers_updated: int = 0,
    tickers_failed: int = 0,
    alerts_generated: int = 0,
    error_summary: str = None,
):
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE automation_runs SET
            finished_at = datetime('now'),
            status = ?,
            tickers_attempted = ?,
            tickers_updated = ?,
            tickers_failed = ?,
            alerts_generated = ?,
            error_summary = ?
        WHERE id = ?
        """,
        (status, tickers_attempted, tickers_updated, tickers_failed, alerts_generated, error_summary, run_id),
    )
    conn.commit()


def record_skipped_run(conn, send_mode: str, status: str, reason: str, trading_date: Optional[date] = None) -> int:
    """A run that never got to the ticker loop at all (non-trading day,
    lock held by another process). Recorded as start+finish immediately so
    it's visible in history without a lingering 'running' row."""
    run_id = start_run(conn, send_mode, trading_date=trading_date)
    finish_run(conn, run_id, status=status, error_summary=reason)
    return run_id


def load_run_history(conn, limit: int = 50) -> pd.DataFrame:
    columns_sql = ", ".join(RUN_COLUMNS)
    return pd.read_sql_query(
        f"SELECT {columns_sql} FROM automation_runs ORDER BY started_at DESC LIMIT ?",
        conn,
        params=(limit,),
    )


def get_latest_run(conn):
    return conn.execute(
        "SELECT * FROM automation_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
