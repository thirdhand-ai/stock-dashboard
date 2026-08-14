"""Persistence for the Phase 12 daily research report (Component A).

One row per report_date, storing the rendered report as JSON. Re-running
the SAME day's report is an idempotent overwrite of that day's cached
rendering only (ON CONFLICT(report_date) DO UPDATE) - it never touches any
other day's row, and never touches any Phase 11 table.
"""
import json
from typing import Optional

import pandas as pd

TABLE_NAME = "ops_daily_reports"

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL UNIQUE,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    report_json TEXT NOT NULL
)
"""


def ensure_schema(conn) -> None:
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()


def upsert_report(conn, report_date: str, report_json: str) -> int:
    ensure_schema(conn)
    cur = conn.cursor()
    cur.execute(
        f"""
        INSERT INTO {TABLE_NAME} (report_date, report_json)
        VALUES (?, ?)
        ON CONFLICT(report_date) DO UPDATE SET
            report_json = excluded.report_json,
            generated_at = datetime('now')
        """,
        (report_date, report_json),
    )
    conn.commit()
    row = conn.execute(f"SELECT id FROM {TABLE_NAME} WHERE report_date = ?", (report_date,)).fetchone()
    return row["id"]


def load_report(conn, report_date: str) -> Optional[dict]:
    ensure_schema(conn)
    row = conn.execute(f"SELECT report_json FROM {TABLE_NAME} WHERE report_date = ?", (report_date,)).fetchone()
    if row is None:
        return None
    return json.loads(row["report_json"])


def load_latest_report(conn) -> Optional[dict]:
    ensure_schema(conn)
    row = conn.execute(f"SELECT report_json FROM {TABLE_NAME} ORDER BY report_date DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return json.loads(row["report_json"])


def load_report_history(conn, limit: int = 20) -> pd.DataFrame:
    ensure_schema(conn)
    return pd.read_sql_query(
        f"SELECT id, report_date, generated_at FROM {TABLE_NAME} ORDER BY report_date DESC LIMIT ?",
        conn,
        params=(limit,),
    )
