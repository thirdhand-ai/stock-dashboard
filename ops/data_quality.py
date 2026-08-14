"""Phase 12 Component B: data quality / freshness monitor (read-only).

Derives freshness/gap/validity findings independently FROM the `prices`
table itself - the point is that it must be able to catch staleness even
if `automation_runs` claims success but `prices` didn't actually update.
Never calls any ingestion function, never mutates `db.price_repository`'s
source preference, never deletes/repairs a row - findings are counted and
reported, not auto-corrected.

`ops/data_quality_repository.py` (§4.2 of the spec) is folded into this
file rather than kept separate, per the spec's explicit note.
"""
import dataclasses
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import pandas as pd

from automation.trading_calendar import is_likely_trading_day, trading_sessions_between
from config.settings import WATCHLIST
from db.price_repository import get_latest_fetched_at, resolve_source

logger = logging.getLogger(__name__)

STATUS_FRESH = "FRESH"
STATUS_STALE = "STALE"
STATUS_MISSING = "MISSING"

OVERALL_HEALTHY = "HEALTHY"
OVERALL_DEGRADED = "DEGRADED"
OVERALL_STALE = "STALE"
OVERALL_FAILED = "FAILED"

# How many NYSE trading sessions the gap-detection window looks back over.
GAP_DETECTION_WINDOW_SESSIONS = 60
# How many missing session dates are surfaced in the report itself (the
# full count is always reported separately in missing_trading_days_count).
MISSING_DAYS_DISPLAY_CAP = 10
# Cross-source close-price divergence threshold flagged as a conflict.
CROSS_SOURCE_CLOSE_DIVERGENCE_PCT = 0.01

TABLE_NAME = "ops_data_quality_checks"

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    overall_status TEXT NOT NULL,
    report_json TEXT NOT NULL
)
"""


@dataclass
class TickerQualityRow:
    ticker: str
    source: Optional[str]
    status: str
    last_date: Optional[str]
    fetched_at: Optional[str]
    row_count_total: int
    row_count_last_90d: int
    duplicate_rows: int
    invalid_ohlcv_rows: int
    missing_trading_days: List[str] = field(default_factory=list)
    # Full count of missing sessions in the gap-detection window - a
    # separate field from missing_trading_days because that list is capped
    # at MISSING_DAYS_DISPLAY_CAP for report brevity (spec §4.1).
    missing_trading_days_count: int = 0
    reason: Optional[str] = None


@dataclass
class DataQualityReport:
    checked_at: str
    tickers: List[TickerQualityRow] = field(default_factory=list)
    overall_status: str = OVERALL_HEALTHY


def _most_recent_completed_session(today: date) -> date:
    """The latest NYSE trading day <= today. Walks backward a small,
    bounded number of days - NYSE holidays never cluster more than a
    handful of days together, so this always terminates quickly."""
    d = today
    for _ in range(14):
        if is_likely_trading_day(d):
            return d
        d = d - timedelta(days=1)
    return d


def _count_invalid_ohlcv(conn, ticker: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) as n FROM prices
        WHERE ticker = ? AND (
            high < low OR close > high OR close < low OR open > high OR open < low
            OR volume < 0
            OR open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
            OR open != open OR high != high OR low != low OR close != close
            OR close <= 0
        )
        """,
        (ticker,),
    ).fetchone()
    return row["n"]


def _count_cross_source_conflicts(conn, ticker: str) -> int:
    """Rows for the same (ticker, date) recorded under >1 `source` with
    materially different close prices (see CROSS_SOURCE_CLOSE_DIVERGENCE_PCT)
    - a cross-source disagreement flag, not a hard error (Alpaca/yfinance
    can legitimately show minor adjustment differences). Exact structural
    duplicates are already impossible via prices' UNIQUE(ticker, date,
    source) constraint."""
    rows = conn.execute(
        """
        SELECT a.date as date, a.close as close_a, b.close as close_b
        FROM prices a
        JOIN prices b ON a.ticker = b.ticker AND a.date = b.date AND a.source < b.source
        WHERE a.ticker = ?
        """,
        (ticker,),
    ).fetchall()
    count = 0
    for r in rows:
        close_a = r["close_a"]
        close_b = r["close_b"]
        if close_a and abs(close_a - close_b) / abs(close_a) > CROSS_SOURCE_CLOSE_DIVERGENCE_PCT:
            count += 1
    return count


def _gap_detection(conn, ticker: str, source: Optional[str], today: date, most_recent_session: date):
    """Last GAP_DETECTION_WINDOW_SESSIONS NYSE sessions ending at
    most_recent_session, excluding today, checked against stored (ticker,
    source) rows. Returns (missing_days_capped, missing_days_count)."""
    if source is None:
        return [], 0

    window_start = most_recent_session - timedelta(days=int(GAP_DETECTION_WINDOW_SESSIONS * 2.5) + 10)
    sessions = trading_sessions_between(window_start, most_recent_session)
    sessions = [s for s in sessions if s != today][-GAP_DETECTION_WINDOW_SESSIONS:]
    if not sessions:
        return [], 0

    session_strs = [s.isoformat() for s in sessions]
    placeholders = ",".join("?" for _ in session_strs)
    rows = conn.execute(
        f"SELECT DISTINCT date FROM prices WHERE ticker = ? AND source = ? AND date IN ({placeholders})",
        (ticker, source, *session_strs),
    ).fetchall()
    present = {r["date"] for r in rows}
    missing = [s for s in session_strs if s not in present]
    return missing[-MISSING_DAYS_DISPLAY_CAP:], len(missing)


def check_ticker_quality(conn, ticker: str, today: Optional[date] = None) -> TickerQualityRow:
    today = today or date.today()
    source = resolve_source(conn, ticker)

    row_count_total = conn.execute(
        "SELECT COUNT(*) as n FROM prices WHERE ticker = ?", (ticker,)
    ).fetchone()["n"]

    if row_count_total == 0:
        return TickerQualityRow(
            ticker=ticker, source=source, status=STATUS_MISSING, last_date=None, fetched_at=None,
            row_count_total=0, row_count_last_90d=0, duplicate_rows=0, invalid_ohlcv_rows=0,
            missing_trading_days=[], missing_trading_days_count=0,
            reason="no price rows for this ticker",
        )

    last_date = conn.execute(
        "SELECT MAX(date) as d FROM prices WHERE ticker = ?", (ticker,)
    ).fetchone()["d"]
    fetched_at = get_latest_fetched_at(conn, ticker, source=source)

    cutoff_90d = (today - timedelta(days=90)).isoformat()
    row_count_last_90d = conn.execute(
        "SELECT COUNT(*) as n FROM prices WHERE ticker = ? AND date >= ?", (ticker, cutoff_90d)
    ).fetchone()["n"]

    most_recent_session = _most_recent_completed_session(today)
    reason = None
    if last_date < most_recent_session.isoformat():
        status = STATUS_STALE
        reason = (
            f"last stored date {last_date} is before the most recent completed NYSE session "
            f"{most_recent_session.isoformat()}"
        )
    else:
        status = STATUS_FRESH

    duplicate_rows = _count_cross_source_conflicts(conn, ticker)
    invalid_ohlcv_rows = _count_invalid_ohlcv(conn, ticker)
    missing_days, missing_days_count = _gap_detection(conn, ticker, source, today, most_recent_session)

    return TickerQualityRow(
        ticker=ticker, source=source, status=status, last_date=last_date, fetched_at=fetched_at,
        row_count_total=row_count_total, row_count_last_90d=row_count_last_90d,
        duplicate_rows=duplicate_rows, invalid_ohlcv_rows=invalid_ohlcv_rows,
        missing_trading_days=missing_days, missing_trading_days_count=missing_days_count,
        reason=reason,
    )


def classify_overall(rows: List[TickerQualityRow]) -> str:
    if any(r.status == STATUS_MISSING for r in rows):
        return OVERALL_FAILED
    if any(r.status == STATUS_STALE for r in rows):
        return OVERALL_STALE
    if any(r.invalid_ohlcv_rows > 0 or r.duplicate_rows > 0 or r.missing_trading_days for r in rows):
        return OVERALL_DEGRADED
    return OVERALL_HEALTHY


def check_watchlist_quality(
    conn, tickers: Optional[List[str]] = None, today: Optional[date] = None
) -> DataQualityReport:
    tickers = tickers if tickers is not None else WATCHLIST
    today = today or date.today()
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        rows = [check_ticker_quality(conn, t, today=today) for t in tickers]
        overall = classify_overall(rows)
    except Exception as e:
        logger.error("data quality check failed: %s", e)
        return DataQualityReport(checked_at=checked_at, tickers=[], overall_status=OVERALL_FAILED)

    return DataQualityReport(checked_at=checked_at, tickers=rows, overall_status=overall)


def ensure_schema(conn) -> None:
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()


def record_check(conn, report: DataQualityReport) -> int:
    ensure_schema(conn)
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO {TABLE_NAME} (checked_at, overall_status, report_json) VALUES (?, ?, ?)",
        (report.checked_at, report.overall_status, json.dumps(dataclasses.asdict(report), default=str)),
    )
    conn.commit()
    return cur.lastrowid


def load_latest_check(conn) -> Optional[dict]:
    ensure_schema(conn)
    row = conn.execute(
        f"SELECT report_json FROM {TABLE_NAME} ORDER BY checked_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["report_json"])


def load_check_history(conn, limit: int = 30) -> pd.DataFrame:
    ensure_schema(conn)
    return pd.read_sql_query(
        f"SELECT id, checked_at, overall_status FROM {TABLE_NAME} ORDER BY checked_at DESC, id DESC LIMIT ?",
        conn,
        params=(limit,),
    )
