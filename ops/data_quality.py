"""Phase 12 Component B: data quality / freshness monitor (read-only).

Derives freshness/gap/validity findings independently FROM the `prices`
table itself - the point is that it must be able to catch staleness even
if `automation_runs` claims success but `prices` didn't actually update.
Never calls any ingestion function, never mutates `db.price_repository`'s
source preference, never deletes/repairs a row - findings are counted and
reported, not auto-corrected.

`ops/data_quality_repository.py` (§4.2 of the spec) is folded into this
file rather than kept separate, per the spec's explicit note.

Phase 13 additions (all additive - see docs/specs/phase13.md §4): a
provenance ledger (`ops_source_provenance`) that fingerprints each
(ticker, source) pair over time to detect silent same-source drift, plus
cross-source divergence/staleness findings from
`ops.provider_reconciliation` folded onto `TickerQualityRow`/
`DataQualityReport`, and a provenance-aware overall-status rollup.
"""
import dataclasses
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import pandas as pd

from automation.trading_calendar import is_likely_trading_day, trading_sessions_between
from config.settings import WATCHLIST
from db.price_repository import get_latest_fetched_at, resolve_source
from ops.provider_reconciliation import (
    CLASS_STALE_SOURCE,
    CLASS_UNEXPLAINED_DIVERGENCE,
    SOURCE_ADJUSTMENT_MODE,
    ADJUSTMENT_UNKNOWN,
    build_ticker_summary,
)

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
    # Phase 13 additions - all default safely so existing direct
    # construction (tests/test_ops_data_quality.py) is unaffected.
    provider_findings: List[dict] = field(default_factory=list)        # [DivergenceFinding asdict, ...]
    unexplained_divergence_count: int = 0
    stale_source_count: int = 0
    authoritative_source_stale: bool = False   # redundant with status==STATUS_STALE today,
                                                # but explicit for the new rollup's clarity
    non_authoritative_source_notes: List[str] = field(default_factory=list)


@dataclass
class DataQualityReport:
    checked_at: str
    tickers: List[TickerQualityRow] = field(default_factory=list)
    overall_status: str = OVERALL_HEALTHY
    overall_status_provenance_aware: str = OVERALL_HEALTHY


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


def _provider_reconciliation_fields(conn, ticker: str, today: date):
    """Phase 13 addition: call ops.provider_reconciliation.build_ticker_summary
    and translate its output into the new TickerQualityRow fields (§4.3).
    Never raises - a reconciliation failure must never break the base
    freshness/validity check this function augments."""
    try:
        summary = build_ticker_summary(conn, ticker, today=today)
    except Exception as e:
        logger.error("provider reconciliation failed for ticker=%s: %s", ticker, e)
        return [], 0, 0, False, []

    provider_findings = [dataclasses.asdict(f) for f in summary.findings]
    non_authoritative_source_notes = [
        f.detail for f in summary.findings if f.classification == CLASS_STALE_SOURCE
    ]
    return (
        provider_findings,
        summary.unexplained_count,
        summary.stale_source_count,
        summary.authoritative_stale,
        non_authoritative_source_notes,
    )


def check_ticker_quality(conn, ticker: str, today: Optional[date] = None) -> TickerQualityRow:
    today = today or date.today()
    source = resolve_source(conn, ticker)

    row_count_total = conn.execute(
        "SELECT COUNT(*) as n FROM prices WHERE ticker = ?", (ticker,)
    ).fetchone()["n"]

    if row_count_total == 0:
        (
            provider_findings, unexplained_divergence_count, stale_source_count,
            authoritative_source_stale, non_authoritative_source_notes,
        ) = _provider_reconciliation_fields(conn, ticker, today)
        return TickerQualityRow(
            ticker=ticker, source=source, status=STATUS_MISSING, last_date=None, fetched_at=None,
            row_count_total=0, row_count_last_90d=0, duplicate_rows=0, invalid_ohlcv_rows=0,
            missing_trading_days=[], missing_trading_days_count=0,
            reason="no price rows for this ticker",
            provider_findings=provider_findings,
            unexplained_divergence_count=unexplained_divergence_count,
            stale_source_count=stale_source_count,
            authoritative_source_stale=authoritative_source_stale,
            non_authoritative_source_notes=non_authoritative_source_notes,
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

    (
        provider_findings, unexplained_divergence_count, stale_source_count,
        authoritative_source_stale, non_authoritative_source_notes,
    ) = _provider_reconciliation_fields(conn, ticker, today)
    # Redundant-but-explicit: a STALE authoritative source is exactly what
    # status==STATUS_STALE already means; keep them in agreement rather than
    # relying on two independently-derived signals to coincidentally match.
    authoritative_source_stale = authoritative_source_stale or (status == STATUS_STALE)

    return TickerQualityRow(
        ticker=ticker, source=source, status=status, last_date=last_date, fetched_at=fetched_at,
        row_count_total=row_count_total, row_count_last_90d=row_count_last_90d,
        duplicate_rows=duplicate_rows, invalid_ohlcv_rows=invalid_ohlcv_rows,
        missing_trading_days=missing_days, missing_trading_days_count=missing_days_count,
        reason=reason,
        provider_findings=provider_findings,
        unexplained_divergence_count=unexplained_divergence_count,
        stale_source_count=stale_source_count,
        authoritative_source_stale=authoritative_source_stale,
        non_authoritative_source_notes=non_authoritative_source_notes,
    )


def classify_overall(rows: List[TickerQualityRow]) -> str:
    if any(r.status == STATUS_MISSING for r in rows):
        return OVERALL_FAILED
    if any(r.status == STATUS_STALE for r in rows):
        return OVERALL_STALE
    if any(r.invalid_ohlcv_rows > 0 or r.duplicate_rows > 0 or r.missing_trading_days for r in rows):
        return OVERALL_DEGRADED
    return OVERALL_HEALTHY


def classify_overall_provenance_aware(rows: List[TickerQualityRow]) -> str:
    """Same OVERALL_* vocabulary as classify_overall, but:
      - FAILED: any ticker STATUS_MISSING, or authoritative_source_stale True
        for any ticker.
      - STALE: no MISSING/authoritative-stale, but >=1 ticker STATUS_STALE
        (existing field, kept as fallback signal).
      - DEGRADED: all tickers FRESH and no authoritative staleness, but >=1
        ticker has invalid_ohlcv_rows > 0 OR unexplained_divergence_count > 0
        OR stale_source_count > 0 (non-authoritative) OR non-empty
        missing_trading_days. Critically: MATCHED / EXPECTED_ADJUSTMENT_DIFFERENCE
        / SESSION_ALIGNMENT_DIFFERENCE findings never degrade this on their
        own; only UNEXPLAINED_DIVERGENCE and STALE_SOURCE do (both indicate
        something an operator should look at, even if not authoritative-breaking).
      - HEALTHY: otherwise.
    This is the rollup the dashboard (§6) should treat as authoritative; the
    OLD classify_overall/overall_status fields are kept for Phase 12
    backward compatibility, not removed.
    """
    if any(r.status == STATUS_MISSING for r in rows) or any(r.authoritative_source_stale for r in rows):
        return OVERALL_FAILED
    if any(r.status == STATUS_STALE for r in rows):
        return OVERALL_STALE
    if any(
        r.invalid_ohlcv_rows > 0
        or r.unexplained_divergence_count > 0
        or r.stale_source_count > 0
        or r.missing_trading_days
        for r in rows
    ):
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
        overall_provenance_aware = classify_overall_provenance_aware(rows)
    except Exception as e:
        logger.error("data quality check failed for tickers=%s: %s", tickers, e)
        return DataQualityReport(
            checked_at=checked_at, tickers=[], overall_status=OVERALL_FAILED,
            overall_status_provenance_aware=OVERALL_FAILED,
        )

    return DataQualityReport(
        checked_at=checked_at, tickers=rows, overall_status=overall,
        overall_status_provenance_aware=overall_provenance_aware,
    )


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


# --- Phase 13 §4.5: provenance ledger (append-only, own ensure_schema) ---

PROVENANCE_TABLE_NAME = "ops_source_provenance"

_CREATE_PROVENANCE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {PROVENANCE_TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id INTEGER NOT NULL,
    checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    ticker TEXT NOT NULL,
    source TEXT NOT NULL,
    adjustment_mode TEXT NOT NULL,
    min_date TEXT,
    max_date TEXT,
    row_count INTEGER NOT NULL,
    fingerprint TEXT NOT NULL
)
"""


def ensure_provenance_schema(conn) -> None:
    conn.execute(_CREATE_PROVENANCE_TABLE_SQL)
    conn.commit()


def _fingerprint_rows(conn, ticker: str, source: str) -> str:
    """sha256 of the ordered (date,open,high,low,close,volume) tuples for
    this (ticker,source) - detects silent same-source drift across checks
    over time (e.g. a row's values changing on re-fetch), NOT cross-source
    difference (that's provider_findings, §4.1)."""
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM prices WHERE ticker = ? AND source = ? ORDER BY date ASC",
        (ticker, source),
    ).fetchall()
    hasher = hashlib.sha256()
    for r in rows:
        hasher.update(f"{r['date']}|{r['open']}|{r['high']}|{r['low']}|{r['close']}|{r['volume']}".encode("utf-8"))
    return hasher.hexdigest()


def record_source_provenance(conn, check_id: int, tickers: List[str]) -> int:
    """For every (ticker, source) pair present in `prices` for `tickers`,
    INSERT one row: adjustment_mode from provider_reconciliation.SOURCE_ADJUSTMENT_MODE,
    min/max(date), COUNT(*), and fingerprint = sha256 of the ordered
    (date,open,high,low,close,volume) tuples for that (ticker,source) -
    detects silent same-source drift across checks over time, NOT
    cross-source difference (that's provider_findings, §4.1). Append-only.
    Returns rows inserted."""
    ensure_provenance_schema(conn)
    inserted = 0
    for ticker in tickers:
        source_rows = conn.execute(
            "SELECT DISTINCT source FROM prices WHERE ticker = ?", (ticker,)
        ).fetchall()
        for r in source_rows:
            source = r["source"]
            stats = conn.execute(
                "SELECT MIN(date) as min_d, MAX(date) as max_d, COUNT(*) as n FROM prices WHERE ticker = ? AND source = ?",
                (ticker, source),
            ).fetchone()
            if not stats or stats["n"] == 0:
                continue
            adjustment_mode = SOURCE_ADJUSTMENT_MODE.get(source, ADJUSTMENT_UNKNOWN)
            fingerprint = _fingerprint_rows(conn, ticker, source)
            conn.execute(
                f"""
                INSERT INTO {PROVENANCE_TABLE_NAME}
                    (check_id, ticker, source, adjustment_mode, min_date, max_date, row_count, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (check_id, ticker, source, adjustment_mode, stats["min_d"], stats["max_d"], stats["n"], fingerprint),
            )
            inserted += 1
    conn.commit()
    return inserted


def load_provenance_history(conn, ticker: str, source: str, limit: int = 30) -> pd.DataFrame:
    ensure_provenance_schema(conn)
    return pd.read_sql_query(
        f"""
        SELECT id, check_id, checked_at, ticker, source, adjustment_mode, min_date, max_date, row_count, fingerprint
        FROM {PROVENANCE_TABLE_NAME}
        WHERE ticker = ? AND source = ?
        ORDER BY checked_at DESC, id DESC LIMIT ?
        """,
        conn,
        params=(ticker, source, limit),
    )
