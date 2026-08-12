"""Read/write interface for automation run history (Phase 6).

One row per pipeline execution, created at start and completed at finish -
so a crashed run is still visible as a row stuck in status='running'
rather than vanishing silently. Never stores credentials or webhook values.
"""
import pandas as pd

RUN_COLUMNS = [
    "id", "started_at", "finished_at", "status", "send_mode",
    "tickers_attempted", "tickers_updated", "tickers_failed",
    "alerts_generated", "error_summary",
]

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_PARTIAL_FAILURE = "partial_failure"
STATUS_FAILED = "failed"
STATUS_SKIPPED_NON_TRADING_DAY = "skipped_non_trading_day"
STATUS_SKIPPED_OVERLAP = "skipped_overlap"

SEND_MODE_DRY_RUN = "dry_run"
SEND_MODE_REAL = "real"


def start_run(conn, send_mode: str) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO automation_runs (status, send_mode) VALUES (?, ?)",
        (STATUS_RUNNING, send_mode),
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


def record_skipped_run(conn, send_mode: str, status: str, reason: str) -> int:
    """A run that never got to the ticker loop at all (non-trading day,
    lock held by another process). Recorded as start+finish immediately so
    it's visible in history without a lingering 'running' row."""
    run_id = start_run(conn, send_mode)
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
