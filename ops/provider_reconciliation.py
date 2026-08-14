"""Phase 13 Components A + B: provider reconciliation and source-of-truth
policy documentation (read-only).

Compares the two source tags actually present in `prices` for a ticker
(currently `"alpaca"` — raw production data — and `"alpaca_adjusted"` —
split/dividend-adjusted research data, see `strategy_lab/data.py`) and
classifies every overlapping (ticker, date) pair into one of a small,
documented vocabulary. Never reads `ingestion.*`, never mutates
`db.price_repository.SOURCE_PRIORITY`, never writes/deletes a `prices` row -
pure read + classify + report, same as `ops/data_quality.py`.

See docs/specs/phase13.md §0.1 for the three confirmed real divergence
patterns this classifier distinguishes, and §3.1 for the exact check order
(first match wins) that this module implements verbatim.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from itertools import combinations
from typing import List, Optional
from zoneinfo import ZoneInfo

from automation.trading_calendar import is_likely_trading_day, trading_sessions_elapsed
from db.price_repository import resolve_source

logger = logging.getLogger(__name__)

# Static, documented registry - NOT inferred per-row. Confirmed against
# actual ingestion code: ingestion/alpaca_source.py (no `adjustment` param
# -> Alpaca default RAW), strategy_lab/data.py (adjustment=Adjustment.ALL).
# ingestion/yfinance_source.py exists (auto_adjust=True -> would be ADJUSTED)
# but has never ingested a row into this DB - kept in the registry for when
# it does, not because it's currently active.
ADJUSTMENT_RAW = "RAW"
ADJUSTMENT_ADJUSTED = "ADJUSTED"
ADJUSTMENT_UNKNOWN = "UNKNOWN"

SOURCE_ADJUSTMENT_MODE = {
    "alpaca": ADJUSTMENT_RAW,
    "yfinance": ADJUSTMENT_ADJUSTED,
    "alpaca_adjusted": ADJUSTMENT_ADJUSTED,
}

# Documentation/reporting only - never consulted to pick a source at read
# time (db.price_repository / strategy_lab.data already do that, unmodified).
PRODUCTION_SOURCE_POLICY = (
    "db.price_repository.resolve_source() — SOURCE_PRIORITY=['yfinance','alpaca'], "
    "single source per ticker, never blended"
)
RESEARCH_SOURCE_POLICY = (
    "strategy_lab.data.RESEARCH_SOURCE ('alpaca_adjusted') — always explicit, never "
    "falls back to 'alpaca'/'yfinance'"
)

CLASS_MATCHED = "MATCHED"
CLASS_EXPECTED_ADJUSTMENT_DIFFERENCE = "EXPECTED_ADJUSTMENT_DIFFERENCE"
CLASS_STALE_SOURCE = "STALE_SOURCE"
CLASS_SESSION_ALIGNMENT_DIFFERENCE = "SESSION_ALIGNMENT_DIFFERENCE"
CLASS_MISSING_SOURCE = "MISSING_SOURCE"
CLASS_UNEXPLAINED_DIVERGENCE = "UNEXPLAINED_DIVERGENCE"

# Configurable tolerances - no exact float equality anywhere in this file.
CLOSE_MATCH_TOLERANCE_PCT = 0.0015          # 0.15%
VOLUME_MATCH_TOLERANCE_PCT = 0.02           # 2% - dividend-only adjustment
                                             # must leave volume unchanged;
                                             # this tolerance just absorbs
                                             # float/rounding noise, not a
                                             # real "adjustment" of volume
KNOWN_SPLIT_RATIOS = (2, 3, 4, 5, 7, 10, 20)  # confirmed real case in this
                                               # DB: NVDA 10:1 (June 2024,
                                               # ratio measured at 10.017);
                                               # GOOGL 20:1 (2022, cited in
                                               # strategy_lab/data.py docstring)
SPLIT_RATIO_TOLERANCE_PCT = 0.02            # 2% band around a known ratio (or its inverse)
STALE_SOURCE_SESSION_THRESHOLD = 5          # a source's OWN last_date more than this many
                                             # NYSE sessions behind "most recent completed
                                             # session" is STALE_SOURCE for that source

NYSE_TZ = ZoneInfo("America/New_York")
NYSE_CLOSE_TIME = time(16, 0)


@dataclass
class DivergenceFinding:
    ticker: str
    date: str
    source_a: str
    source_b: str
    adjustment_mode_a: str
    adjustment_mode_b: str
    close_a: float
    close_b: float
    volume_a: int
    volume_b: int
    price_ratio: Optional[float]    # close_a / close_b, None if close_b == 0
    volume_ratio: Optional[float]   # volume_a / volume_b, None if volume_b == 0
    classification: str
    detail: str                     # human-readable, cites the specific signal that decided it


def _safe_ratio(a: float, b: float) -> Optional[float]:
    if not b:
        return None
    return a / b


def _within_relative_tolerance(ratio: Optional[float], tol: float) -> bool:
    return ratio is not None and abs(ratio - 1.0) <= tol


def _adjustment_modes_differ(mode_a: str, mode_b: str) -> bool:
    """A missing/UNKNOWN tag is never treated as equal to anything,
    including another UNKNOWN - implemented via a sentinel comparison, not
    literal string equality, so it never falls through silently (§3.2)."""
    if mode_a == ADJUSTMENT_UNKNOWN or mode_b == ADJUSTMENT_UNKNOWN:
        return True
    return mode_a != mode_b


def _find_split_match(price_ratio: Optional[float], volume_ratio: Optional[float]) -> Optional[float]:
    """Returns the KNOWN_SPLIT_RATIOS value both price_ratio and volume_ratio
    (or either's inverse) agree on within SPLIT_RATIO_TOLERANCE_PCT, or None."""
    if price_ratio is None or volume_ratio is None or price_ratio == 0 or volume_ratio == 0:
        return None
    price_candidates = [price_ratio, 1.0 / price_ratio]
    volume_candidates = [volume_ratio, 1.0 / volume_ratio]
    for k in KNOWN_SPLIT_RATIOS:
        price_match = any(abs(c - k) / k <= SPLIT_RATIO_TOLERANCE_PCT for c in price_candidates)
        volume_match = any(abs(c - k) / k <= SPLIT_RATIO_TOLERANCE_PCT for c in volume_candidates)
        if price_match and volume_match:
            return float(k)
    return None


def _format_fetched_at_utc(fetched_at: Optional[str]) -> str:
    if not fetched_at:
        return "unknown"
    return fetched_at.replace(" ", "T") + ("Z" if not fetched_at.endswith("Z") else "")


def _is_intraday_fetch(fetched_at: Optional[str], date_str: str) -> bool:
    """True iff `fetched_at` (stored as a naive UTC timestamp string, per
    db/schema.py's `datetime('now')` convention) falls on the same calendar
    date as `date_str` in America/New_York AND before NYSE_CLOSE_TIME -
    i.e. this row was very likely fetched intraday, before the session it
    describes had actually closed (§3.1 check #4, confirmed real pattern
    (c))."""
    if not fetched_at:
        return False
    try:
        dt_utc = datetime.strptime(fetched_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    dt_et = dt_utc.astimezone(NYSE_TZ)
    if dt_et.date().isoformat() != date_str:
        return False
    return dt_et.time() < NYSE_CLOSE_TIME


def classify_pair(
    ticker: str, date_str: str,
    source_a: str, close_a: float, volume_a: int, fetched_at_a: Optional[str],
    source_b: str, close_b: float, volume_b: int, fetched_at_b: Optional[str],
    is_latest_date_for_source_a: bool = False,
    is_latest_date_for_source_b: bool = False,
    neighbor_close_b_prev: Optional[float] = None,
    neighbor_close_b_next: Optional[float] = None,
) -> DivergenceFinding:
    """Pure, deterministic classification of ONE (ticker,date) pair already
    known to be present under two sources (MISSING_SOURCE is handled by the
    caller before this is invoked, not here).

    Order of checks (first match wins) — see docs/specs/phase13.md §3.1 for
    full rationale: volume consistency and same-session-fetch-time are
    checked BEFORE accepting an adjustment-mode mismatch as
    EXPECTED_ADJUSTMENT_DIFFERENCE, so a stale/incomplete intraday bar
    (confirmed real pattern (c)) is never silently absorbed into a normal
    adjustment-difference classification just because its price delta alone
    looks adjustment-sized.
    """
    mode_a = SOURCE_ADJUSTMENT_MODE.get(source_a, ADJUSTMENT_UNKNOWN)
    mode_b = SOURCE_ADJUSTMENT_MODE.get(source_b, ADJUSTMENT_UNKNOWN)
    price_ratio = _safe_ratio(close_a, close_b)
    volume_ratio = _safe_ratio(volume_a, volume_b)

    def _finding(classification: str, detail: str) -> DivergenceFinding:
        return DivergenceFinding(
            ticker=ticker, date=date_str, source_a=source_a, source_b=source_b,
            adjustment_mode_a=mode_a, adjustment_mode_b=mode_b,
            close_a=close_a, close_b=close_b, volume_a=volume_a, volume_b=volume_b,
            price_ratio=price_ratio, volume_ratio=volume_ratio,
            classification=classification, detail=detail,
        )

    # 1. MATCHED
    if _within_relative_tolerance(price_ratio, CLOSE_MATCH_TOLERANCE_PCT) and _within_relative_tolerance(
        volume_ratio, VOLUME_MATCH_TOLERANCE_PCT
    ):
        return _finding(
            CLASS_MATCHED,
            f"close and volume both within tolerance (price_ratio={price_ratio:.6f}, "
            f"volume_ratio={volume_ratio:.6f})",
        )

    # 2. Split-ratio, confirmed via matching price AND volume scaling
    split_ratio = _find_split_match(price_ratio, volume_ratio)
    if split_ratio is not None:
        return _finding(
            CLASS_EXPECTED_ADJUSTMENT_DIFFERENCE,
            f"price and volume both scale by ~{split_ratio:g}x, consistent with a {split_ratio:g}:1 "
            f"(or 1:{split_ratio:g}) stock split (price_ratio={price_ratio:.4f}, volume_ratio={volume_ratio:.4f})",
        )

    # 3. Adjustment-mode mismatch, confirmed via unchanged volume (dividend-only adjustment)
    if _adjustment_modes_differ(mode_a, mode_b) and _within_relative_tolerance(volume_ratio, VOLUME_MATCH_TOLERANCE_PCT):
        return _finding(
            CLASS_EXPECTED_ADJUSTMENT_DIFFERENCE,
            f"{source_a} ({mode_a}) vs {source_b} ({mode_b}): volume unchanged (volume_ratio="
            f"{volume_ratio:.6f}), price delta consistent with an adjustment-mode difference "
            f"(price_ratio={price_ratio if price_ratio is None else round(price_ratio, 6)})",
        )

    # 4. Intraday-fetch staleness on whichever source's latest row this is
    stale_source, stale_fetched_at = None, None
    if is_latest_date_for_source_a and _is_intraday_fetch(fetched_at_a, date_str):
        stale_source, stale_fetched_at = source_a, fetched_at_a
    elif is_latest_date_for_source_b and _is_intraday_fetch(fetched_at_b, date_str):
        stale_source, stale_fetched_at = source_b, fetched_at_b
    if stale_source is not None:
        return _finding(
            CLASS_STALE_SOURCE,
            f"{stale_source}: latest row ({date_str}) appears to be a stale/incomplete intraday "
            f"snapshot fetched {_format_fetched_at_utc(stale_fetched_at)}, before session close",
        )

    # 5. Session-alignment: close_a matches a neighboring date's close_b
    if neighbor_close_b_prev is not None and _within_relative_tolerance(
        _safe_ratio(close_a, neighbor_close_b_prev), CLOSE_MATCH_TOLERANCE_PCT
    ):
        return _finding(
            CLASS_SESSION_ALIGNMENT_DIFFERENCE,
            f"{source_a} close on {date_str} matches {source_b}'s PREVIOUS available date's close "
            f"within tolerance (neighbor_close_b_prev={neighbor_close_b_prev}) — likely a session/"
            f"timezone boundary misalignment, not a genuine price divergence",
        )
    if neighbor_close_b_next is not None and _within_relative_tolerance(
        _safe_ratio(close_a, neighbor_close_b_next), CLOSE_MATCH_TOLERANCE_PCT
    ):
        return _finding(
            CLASS_SESSION_ALIGNMENT_DIFFERENCE,
            f"{source_a} close on {date_str} matches {source_b}'s NEXT available date's close "
            f"within tolerance (neighbor_close_b_next={neighbor_close_b_next}) — likely a session/"
            f"timezone boundary misalignment, not a genuine price divergence",
        )

    # 6. No explanation matched
    return _finding(
        CLASS_UNEXPLAINED_DIVERGENCE,
        f"no known pattern explains this divergence (price_ratio="
        f"{price_ratio if price_ratio is None else round(price_ratio, 6)}, volume_ratio="
        f"{volume_ratio if volume_ratio is None else round(volume_ratio, 6)}, adjustment_modes="
        f"({mode_a}, {mode_b}))",
    )


def find_divergences_for_ticker(conn, ticker: str) -> List[DivergenceFinding]:
    """For every (ticker,date) present under >1 source, fetch fetched_at and
    neighbor rows (date-1/date+1 for source_b) via indexed queries, compute
    is_latest_date_for_source_x by comparing against MAX(date) per source,
    then classify_pair(...) each. Read-only SELECTs only."""
    source_rows = conn.execute(
        "SELECT DISTINCT source FROM prices WHERE ticker = ? ORDER BY source", (ticker,)
    ).fetchall()
    sources = [r["source"] for r in source_rows]
    if len(sources) < 2:
        return []

    findings: List[DivergenceFinding] = []
    for source_a, source_b in combinations(sources, 2):
        max_date_a = conn.execute(
            "SELECT MAX(date) as d FROM prices WHERE ticker = ? AND source = ?", (ticker, source_a)
        ).fetchone()["d"]
        max_date_b = conn.execute(
            "SELECT MAX(date) as d FROM prices WHERE ticker = ? AND source = ?", (ticker, source_b)
        ).fetchone()["d"]

        pair_rows = conn.execute(
            """
            SELECT a.date as date,
                   a.close as close_a, a.volume as volume_a, a.fetched_at as fetched_at_a,
                   b.close as close_b, b.volume as volume_b, b.fetched_at as fetched_at_b
            FROM prices a
            JOIN prices b ON a.ticker = b.ticker AND a.date = b.date
            WHERE a.ticker = ? AND a.source = ? AND b.source = ?
            ORDER BY a.date ASC
            """,
            (ticker, source_a, source_b),
        ).fetchall()

        for r in pair_rows:
            date_str = r["date"]
            neighbor_prev_row = conn.execute(
                "SELECT close FROM prices WHERE ticker = ? AND source = ? AND date < ? ORDER BY date DESC LIMIT 1",
                (ticker, source_b, date_str),
            ).fetchone()
            neighbor_next_row = conn.execute(
                "SELECT close FROM prices WHERE ticker = ? AND source = ? AND date > ? ORDER BY date ASC LIMIT 1",
                (ticker, source_b, date_str),
            ).fetchone()

            finding = classify_pair(
                ticker, date_str,
                source_a, r["close_a"], r["volume_a"], r["fetched_at_a"],
                source_b, r["close_b"], r["volume_b"], r["fetched_at_b"],
                is_latest_date_for_source_a=(date_str == max_date_a),
                is_latest_date_for_source_b=(date_str == max_date_b),
                neighbor_close_b_prev=neighbor_prev_row["close"] if neighbor_prev_row else None,
                neighbor_close_b_next=neighbor_next_row["close"] if neighbor_next_row else None,
            )
            findings.append(finding)

    return findings


def _most_recent_completed_session(today: date) -> date:
    """The latest NYSE trading day <= today. Mirrors
    ops/data_quality.py::_most_recent_completed_session (kept as a private
    local copy rather than imported, to avoid a circular import since
    ops/data_quality.py imports this module)."""
    d = today
    for _ in range(14):
        if is_likely_trading_day(d):
            return d
        d = d - timedelta(days=1)
    return d


@dataclass
class SourceFreshness:
    ticker: str
    source: str
    adjustment_mode: str
    is_authoritative_production: bool   # True iff source == resolve_source(conn, ticker)
    last_date: Optional[str]
    sessions_behind: Optional[int]      # None if no rows at all
    stale: bool                         # sessions_behind > STALE_SOURCE_SESSION_THRESHOLD


def check_all_sources_freshness(conn, ticker: str, today: Optional[date] = None) -> List[SourceFreshness]:
    """Unlike ops.data_quality.check_ticker_quality (which only evaluates
    the ONE resolved/authoritative source), this evaluates EVERY distinct
    source present for the ticker independently, so a stale secondary is
    visible but tagged is_authoritative_production=False - the rollup
    (§4.2) uses this flag to never let a stale non-authoritative source
    degrade overall health the way a stale authoritative source must."""
    today = today or date.today()
    authoritative = resolve_source(conn, ticker)
    if authoritative is None:
        return []

    most_recent_session = _most_recent_completed_session(today)

    source_rows = conn.execute(
        "SELECT DISTINCT source FROM prices WHERE ticker = ? ORDER BY source", (ticker,)
    ).fetchall()
    sources = [r["source"] for r in source_rows]

    result: List[SourceFreshness] = []
    for source in sources:
        last_date = conn.execute(
            "SELECT MAX(date) as d FROM prices WHERE ticker = ? AND source = ?", (ticker, source)
        ).fetchone()["d"]
        adjustment_mode = SOURCE_ADJUSTMENT_MODE.get(source, ADJUSTMENT_UNKNOWN)
        if last_date is None:
            sessions_behind: Optional[int] = None
            stale = False
        else:
            sessions_behind = trading_sessions_elapsed(date.fromisoformat(last_date), most_recent_session)
            stale = sessions_behind > STALE_SOURCE_SESSION_THRESHOLD
        result.append(
            SourceFreshness(
                ticker=ticker, source=source, adjustment_mode=adjustment_mode,
                is_authoritative_production=(source == authoritative),
                last_date=last_date, sessions_behind=sessions_behind, stale=stale,
            )
        )
    return result


@dataclass
class ProviderReconciliationSummary:
    ticker: str
    findings: List[DivergenceFinding]
    source_freshness: List[SourceFreshness]
    unexplained_count: int
    stale_source_count: int
    authoritative_stale: bool


def build_ticker_summary(conn, ticker: str, today: Optional[date] = None) -> ProviderReconciliationSummary:
    findings = find_divergences_for_ticker(conn, ticker)
    source_freshness = check_all_sources_freshness(conn, ticker, today=today)

    unexplained_count = len([f for f in findings if f.classification == CLASS_UNEXPLAINED_DIVERGENCE])
    stale_source_count = len([f for f in findings if f.classification == CLASS_STALE_SOURCE])
    authoritative_stale = any(sf.stale for sf in source_freshness if sf.is_authoritative_production)

    for f in findings:
        if f.classification in (CLASS_UNEXPLAINED_DIVERGENCE, CLASS_STALE_SOURCE):
            logger.warning(
                "provider reconciliation: %s %s %s source_a=%s(%s) source_b=%s(%s) price_ratio=%s volume_ratio=%s",
                f.classification, f.ticker, f.date, f.source_a, f.adjustment_mode_a,
                f.source_b, f.adjustment_mode_b, f.price_ratio, f.volume_ratio,
            )

    logger.info(
        "provider reconciliation: %s findings=%d unexplained=%d stale=%d authoritative_stale=%s",
        ticker, len(findings), unexplained_count, stale_source_count, authoritative_stale,
    )

    return ProviderReconciliationSummary(
        ticker=ticker, findings=findings, source_freshness=source_freshness,
        unexplained_count=unexplained_count, stale_source_count=stale_source_count,
        authoritative_stale=authoritative_stale,
    )
