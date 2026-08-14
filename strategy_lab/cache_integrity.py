"""Phase 14 Component A: research-cache lifecycle - detects and repairs a
stale/incomplete single latest RESEARCH_SOURCE ('alpaca_adjusted') bar left
behind by an intraday `fetch_and_cache_universe` run (see
docs/specs/phase14.md §0.1/§1).

The bug this fixes: `strategy_lab/data.py::_needs_fetch` only ever checks
row COUNT for a ticker, never the freshness/completeness of the single most
recent row. If acquisition ran intraday (before NYSE close), Alpaca's
`Adjustment.ALL` bar for the still-open session is preliminary; once that
session genuinely closes, `_needs_fetch` still returns False (row count is
already sufficient) - the preliminary bar is permanently frozen as if final.
`ops/provider_reconciliation.py` already DETECTS this pattern
(CLASS_STALE_SOURCE, via `_is_intraday_fetch`) but is read-only reporting;
this module adds the repair, reusing the same NYSE-close-time semantics and
naive-UTC `fetched_at` convention.

Hard safety properties:
  - NEVER touches source="alpaca" (production) - every write here goes
    through strategy_lab.data._store_adjusted_bars, which is hardcoded to
    RESEARCH_SOURCE.
  - NEVER evaluates/rewrites any row other than a ticker's single MAX(date)
    row - all historical rows are left completely untouched.
  - NEVER writes/deletes any .pkl/.json research artifact - a correction
    that might affect a frozen Phase 9-13 artifact is only ever DETECTED and
    logged (append-only, research_cache_corrections); regenerating an
    artifact remains a deliberate, separate, manually-invoked CLI action
    (python -m strategy_lab.run_study / run_phase10_study / run_phase11_study).
  - Never imports trading.*/alerts.* (structural safety test, §8).
"""
import json
import logging
import pickle
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from alpaca.data.enums import Adjustment
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from automation.trading_calendar import is_likely_trading_day
from ingestion.alpaca_source import get_data_client
from strategy_lab import phase9_baseline, phase10_baseline, phase11_report, report, report_phase10
from strategy_lab.data import RESEARCH_SOURCE, _store_adjusted_bars

logger = logging.getLogger(__name__)

# Local copy - matches the existing, twice-duplicated convention
# (ops/data_quality.py, ops/provider_reconciliation.py, §0.1 of the spec)
# rather than a shared refactor of those two private helpers.
NYSE_TZ = ZoneInfo("America/New_York")
NYSE_CLOSE_TIME = time(16, 0)

CORRECTIONS_TABLE = "research_cache_corrections"

_CREATE_CORRECTIONS_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {CORRECTIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corrected_at TEXT NOT NULL DEFAULT (datetime('now')),
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    source TEXT NOT NULL,
    old_close REAL, new_close REAL,
    old_volume INTEGER, new_volume INTEGER,
    old_fetched_at TEXT, new_fetched_at TEXT,
    reason TEXT NOT NULL,
    affects_frozen_artifacts TEXT NOT NULL
)
"""


def ensure_schema(conn) -> None:
    conn.execute(_CREATE_CORRECTIONS_TABLE_SQL)
    conn.commit()


def session_close_utc(session_date: date) -> datetime:
    """session_date's 16:00 America/New_York close, converted to a naive UTC
    datetime (matching db/schema.py's `datetime('now')` naive-UTC
    convention, and the exact parsing approach in
    ops/provider_reconciliation.py::_is_intraday_fetch). Correctly DST-aware
    via zoneinfo (no hardcoded UTC offset)."""
    local_close = datetime.combine(session_date, NYSE_CLOSE_TIME, tzinfo=NYSE_TZ)
    return local_close.astimezone(timezone.utc).replace(tzinfo=None)


def latest_cached_row(conn, ticker: str, source: str = RESEARCH_SOURCE) -> Optional[dict]:
    row = conn.execute(
        "SELECT date, close, volume, fetched_at FROM prices WHERE ticker = ? AND source = ? "
        "ORDER BY date DESC LIMIT 1",
        (ticker, source),
    ).fetchone()
    return dict(row) if row is not None else None


def _parse_naive_utc_fetched_at(fetched_at: Optional[str]) -> Optional[datetime]:
    """Mirrors ops/provider_reconciliation.py::_is_intraday_fetch's exact
    parsing approach - `fetched_at` is a naive-UTC timestamp string per
    db/schema.py's `datetime('now')` convention. Returns None (never raises)
    on anything unparseable/missing."""
    if not fetched_at:
        return None
    try:
        return datetime.strptime(fetched_at[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def latest_row_is_incomplete(conn, ticker: str, today: Optional[date] = None) -> bool:
    """True iff:
      1. a RESEARCH_SOURCE row exists for `ticker`,
      2. its `date` is itself a valid NYSE session (defensive; False if not
         - never treats a corrupted/malformed date as actionable),
      3. that session's close has genuinely passed
         (session_close_utc(row_date) <= now_utc), AND
      4. `fetched_at` is missing, unparseable, OR < that session's close.

    FAILS CLOSED on ambiguity: an unparseable/missing `fetched_at` returns
    True (treat as incomplete / worth reverifying) rather than silently
    trusting an ambiguous row forever - mirrors outcome_maturation.py's
    "never fabricate, mark unavailable/uncertain rather than assume OK"
    posture. NEVER evaluates any row other than the single MAX(date) row for
    (ticker, RESEARCH_SOURCE) - historical rows are never re-examined here.

    `today` (a calendar date, defaulting to date.today()) lets a caller
    deterministically establish that a row's session is unambiguously in the
    past (row_date < today) without needing to mock the wall clock; when
    row_date == today, the real current UTC time is used to decide whether
    that (still-possibly-open) session has genuinely closed yet."""
    row = latest_cached_row(conn, ticker)
    if row is None:
        return False

    try:
        row_date = date.fromisoformat(row["date"])
    except (TypeError, ValueError):
        return False

    if not is_likely_trading_day(row_date):
        return False

    today = today or date.today()
    close_utc = session_close_utc(row_date)
    if row_date < today:
        session_has_closed = True
    elif row_date > today:
        session_has_closed = False
    else:
        session_has_closed = close_utc <= datetime.now(timezone.utc).replace(tzinfo=None)

    if not session_has_closed:
        return False

    fetched_at = _parse_naive_utc_fetched_at(row.get("fetched_at"))
    if fetched_at is None:
        return True
    return fetched_at < close_utc


# --- Frozen-artifact detection (§1.4) - read-only, best-effort, never
# raises, never writes to any of these files. ---

FROZEN_ARTIFACT_REGISTRY: List[dict] = [
    {"name": "phase9_baseline", "kind": "json", "path": phase9_baseline.BASELINE_FILE,
     "date_range_keys": ("dataset", "coverage", "date_range")},
    {"name": "phase10_baseline", "kind": "json", "path": phase10_baseline.BASELINE_FILE,
     "date_range_keys": ("dataset", "coverage", "date_range")},
    {"name": "phase9_results", "kind": "pickle", "path": report.CACHE_FILE,
     "date_range_keys": ("coverage", "date_range")},
    {"name": "phase10_results", "kind": "pickle", "path": report_phase10.CACHE_FILE,
     "date_range_keys": ("coverage", "date_range")},
    {"name": "phase11_results", "kind": "pickle", "path": phase11_report.CACHE_FILE,
     "date_range_keys": ("coverage", "date_range")},
]


def _load_artifact_max_date(entry: dict) -> Optional[str]:
    """Best-effort, read-only, never raises. Returns None on any failure
    (file missing, corrupt, unexpected shape) - detection degrades to "no
    finding" rather than blocking a refetch."""
    try:
        path = entry["path"]
        if not path.exists():
            return None
        if entry["kind"] == "json":
            with open(path) as f:
                data = json.load(f)
        else:
            with open(path, "rb") as f:
                data = pickle.load(f)

        node = data
        for key in entry["date_range_keys"][:-1]:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
            if node is None:
                return None
        if not isinstance(node, dict):
            return None
        date_range = node.get(entry["date_range_keys"][-1])
        if not date_range or len(date_range) < 2:
            return None
        return str(date_range[1])
    except Exception as e:
        logger.warning("cache_integrity: could not inspect frozen artifact %s: %s", entry.get("name"), e)
        return None


def check_correction_affects_frozen_artifacts(corrected_date: str) -> List[str]:
    """Best-effort, never raises. Returns FROZEN_ARTIFACT_REGISTRY entry
    `name`s whose recorded coverage max-date >= corrected_date. A missing/
    unreadable artifact file is silently skipped (not an error)."""
    affected = []
    for entry in FROZEN_ARTIFACT_REGISTRY:
        try:
            max_date = _load_artifact_max_date(entry)
            if max_date is not None and max_date >= corrected_date:
                affected.append(entry["name"])
        except Exception as e:
            logger.warning("cache_integrity: frozen-artifact check failed for %s: %s", entry.get("name"), e)
            continue
    return affected


def _record_correction(
    conn, *, ticker: str, date_str: str, source: str,
    old_close: float, new_close: float, old_volume: int, new_volume: int,
    old_fetched_at: Optional[str], new_fetched_at: Optional[str],
    reason: str, affects_frozen_artifacts: List[str],
) -> None:
    """Append-only - no UPDATE/DELETE path anywhere for this table."""
    ensure_schema(conn)
    conn.execute(
        f"""
        INSERT INTO {CORRECTIONS_TABLE} (
            ticker, date, source, old_close, new_close, old_volume, new_volume,
            old_fetched_at, new_fetched_at, reason, affects_frozen_artifacts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker, date_str, source, old_close, new_close, old_volume, new_volume,
            old_fetched_at, new_fetched_at, reason, json.dumps(affects_frozen_artifacts),
        ),
    )
    conn.commit()


def refresh_incomplete_latest_bars(
    conn, tickers: List[str], today: Optional[date] = None,
) -> Dict[str, dict]:
    """For each ticker where latest_row_is_incomplete(conn, ticker, today)
    is True: fetch a small tail window (StockBarsRequest, that ticker only,
    start=row_date - 7 calendar days, timeframe=Day, adjustment=Adjustment.ALL
    - never the full 5-year backfill), find the bar matching row_date exactly.

    - If the Alpaca call raises: leave the existing row completely untouched,
      report {"status": "fetch_failed", "detail": str(e)}. Never partially
      writes.
    - If no bar for that exact date is returned: leave untouched, report
      {"status": "no_data_returned"}.
    - If a bar IS returned: compare its close/volume against the currently
      cached row (exact equality). If unchanged, upsert anyway (refreshes
      fetched_at to now, so this ticker is never re-flagged incomplete every
      single run) and report {"status": "refreshed_unchanged"}. If changed,
      call _record_correction(...) FIRST (append-only audit row - detection
      happens before the value is overwritten), then upsert via the existing
      strategy_lab.data._store_adjusted_bars, report
      {"status": "refreshed_changed", "old_close": ..., "new_close": ...,
      "affects_frozen_artifacts": [...]}.

    NEVER touches any row other than that ticker's single MAX(date) row.
    NEVER writes source="alpaca". NEVER writes/deletes any .pkl/.json file.
    Ticker not flagged incomplete -> {"status": "no_action_needed"} (no API
    call made for that ticker)."""
    ensure_schema(conn)
    report_out: Dict[str, dict] = {}
    client = None

    for ticker in tickers:
        if not latest_row_is_incomplete(conn, ticker, today=today):
            report_out[ticker] = {"status": "no_action_needed"}
            continue

        row = latest_cached_row(conn, ticker)
        row_date_str = row["date"]
        row_date = date.fromisoformat(row_date_str)
        start = datetime.combine(row_date - timedelta(days=7), time.min, tzinfo=timezone.utc)

        try:
            if client is None:
                client = get_data_client()
            request = StockBarsRequest(
                symbol_or_symbols=[ticker], timeframe=TimeFrame.Day, start=start, adjustment=Adjustment.ALL,
            )
            barset = client.get_stock_bars(request)
            df = barset.df.reset_index()
        except Exception as e:
            logger.error("cache_integrity: completeness refresh fetch failed for %s: %s", ticker, e)
            report_out[ticker] = {"status": "fetch_failed", "detail": str(e)}
            continue

        if df.empty:
            report_out[ticker] = {"status": "no_data_returned"}
            continue

        df = df.copy()
        df["date"] = df["timestamp"].dt.strftime("%Y-%m-%d")
        match = df[df["date"] == row_date_str]
        if match.empty:
            report_out[ticker] = {"status": "no_data_returned"}
            continue

        new_bar = match.iloc[0]
        new_close = float(new_bar["close"])
        new_volume = int(new_bar["volume"])
        old_close = float(row["close"])
        old_volume = int(row["volume"])
        old_fetched_at = row.get("fetched_at")

        bars_df = match[["date", "open", "high", "low", "close", "volume"]]

        if new_close == old_close and new_volume == old_volume:
            _store_adjusted_bars(conn, ticker, bars_df)
            report_out[ticker] = {"status": "refreshed_unchanged"}
            continue

        affected = check_correction_affects_frozen_artifacts(row_date_str)
        new_fetched_at_approx = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        _record_correction(
            conn, ticker=ticker, date_str=row_date_str, source=RESEARCH_SOURCE,
            old_close=old_close, new_close=new_close, old_volume=old_volume, new_volume=new_volume,
            old_fetched_at=old_fetched_at, new_fetched_at=new_fetched_at_approx,
            reason=(
                "latest cached bar was refetched after its NYSE session close and its value changed "
                "vs. the preliminary intraday snapshot"
            ),
            affects_frozen_artifacts=affected,
        )
        _store_adjusted_bars(conn, ticker, bars_df)
        report_out[ticker] = {
            "status": "refreshed_changed", "old_close": old_close, "new_close": new_close,
            "affects_frozen_artifacts": affected,
        }

    return report_out
