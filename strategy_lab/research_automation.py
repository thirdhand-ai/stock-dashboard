"""Phase 11 spec B5/B6: research-only automation orchestration.

Workflow (called by strategy_lab/run_research_job.py, the CLI entry point
a SEPARATE research LaunchAgent would invoke - never automation/run_daily.py,
never wired into the production job):
  1. verify NYSE trading day
  2. confirm today's PRODUCTION automation run actually succeeded (reads
     db.run_history_repository only - a plain DB read, never imports
     automation.pipeline/run_daily) - if it didn't, skip immutably rather
     than building prospective observations on stale prices. This is
     exactly the gate that would have caught the 2026-08-12 DNS outage.
  3. create immutable prospective observations/events for the research
     universe (strategy_lab.prospective / strategy_lab.prospective_events)
  4. mature legitimately available old outcomes (strategy_lab.outcome_maturation)
  5. persist research-run history (this module's own table, separate from
     db.run_history_repository's production automation_runs)
  6. exit

Structurally incapable of trading or alerting: no import anywhere in this
module of trading.orders/engine/run_paper or alerts.discord/runner/
run_alerts, and no call anywhere that writes to alert_state or paper_orders
(see tests/test_strategy_lab.py's structural safety tests, which scan this
file too).
"""
import logging
import traceback
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

from automation.trading_calendar import is_likely_trading_day
from db.run_history_repository import STATUS_SUCCESS, load_run_history
from strategy_lab.outcome_maturation import mature_outcomes
from strategy_lab.prospective import build_todays_observation, record_observation
from strategy_lab.prospective_events import record_events_for_observation
from strategy_lab.universe import RESEARCH_UNIVERSE

logger = logging.getLogger(__name__)

RUN_HISTORY_TABLE_NAME = "research_run_history"

STATUS_SUCCESS_RESEARCH = "success"
STATUS_SKIPPED_NON_TRADING_DAY = "skipped_non_trading_day"
STATUS_SKIPPED_NO_FRESH_DATA = "skipped_no_fresh_market_data"
STATUS_FAILED = "failed"

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {RUN_HISTORY_TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    trading_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    tickers_attempted INTEGER NOT NULL DEFAULT 0,
    observations_created INTEGER NOT NULL DEFAULT 0,
    duplicates_skipped INTEGER NOT NULL DEFAULT 0,
    events_created INTEGER NOT NULL DEFAULT 0,
    outcomes_matured INTEGER NOT NULL DEFAULT 0,
    errors TEXT,
    skip_reason TEXT
)
"""


def ensure_schema(conn) -> None:
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()


@dataclass
class ResearchRunResult:
    run_id: Optional[int]
    status: str
    trading_date: str
    tickers_attempted: int = 0
    observations_created: int = 0
    duplicates_skipped: int = 0
    events_created: int = 0
    outcomes_matured: int = 0
    errors: List[str] = field(default_factory=list)
    skip_reason: Optional[str] = None


def _todays_production_run_succeeded(conn, today: date) -> bool:
    """The B5 freshness gate: today's fresh production automation run must
    have actually SUCCEEDED (not partial_failure, not failed, not merely
    "ran") before the research job will build any prospective observation
    for today - directly prevents a repeat of the 2026-08-12 failure mode,
    where evaluation silently proceeded against stale/unverified data."""
    history = load_run_history(conn, limit=50)
    if history.empty:
        return False
    todays = history[history["started_at"].astype(str).str.startswith(today.isoformat())]
    return bool((todays["status"] == STATUS_SUCCESS).any())


def _start_run(conn, trading_date: date) -> int:
    ensure_schema(conn)
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO {RUN_HISTORY_TABLE_NAME} (trading_date, status) VALUES (?, 'running')",
        (trading_date.isoformat(),),
    )
    conn.commit()
    return cur.lastrowid


def _finish_run(conn, run_id: int, result: ResearchRunResult) -> None:
    conn.execute(
        f"""
        UPDATE {RUN_HISTORY_TABLE_NAME} SET
            finished_at = datetime('now'), status = ?, tickers_attempted = ?,
            observations_created = ?, duplicates_skipped = ?, events_created = ?,
            outcomes_matured = ?, errors = ?, skip_reason = ?
        WHERE id = ?
        """,
        (
            result.status, result.tickers_attempted, result.observations_created, result.duplicates_skipped,
            result.events_created, result.outcomes_matured,
            "; ".join(result.errors)[:2000] if result.errors else None, result.skip_reason, run_id,
        ),
    )
    conn.commit()


def _record_skipped(conn, trading_date: date, status: str, reason: str) -> ResearchRunResult:
    ensure_schema(conn)
    run_id = _start_run(conn, trading_date)
    result = ResearchRunResult(run_id=run_id, status=status, trading_date=trading_date.isoformat(), skip_reason=reason)
    _finish_run(conn, run_id, result)
    return result


def run_research_job(conn, today: Optional[date] = None, tickers: Optional[List[str]] = None) -> ResearchRunResult:
    today = today or date.today()
    tickers = tickers or RESEARCH_UNIVERSE

    if not is_likely_trading_day(today):
        return _record_skipped(conn, today, STATUS_SKIPPED_NON_TRADING_DAY, f"{today.isoformat()} is not an NYSE trading day")

    if not _todays_production_run_succeeded(conn, today):
        return _record_skipped(
            conn, today, STATUS_SKIPPED_NO_FRESH_DATA,
            "today's production automation run did not succeed - refusing to build prospective observations from stale/unverified prices",
        )

    run_id = _start_run(conn, today)
    result = ResearchRunResult(run_id=run_id, status=STATUS_SUCCESS_RESEARCH, trading_date=today.isoformat(), tickers_attempted=len(tickers))

    for ticker in tickers:
        try:
            obs = build_todays_observation(conn, ticker, as_of_date=today.isoformat())
            if obs is None:
                continue
            inserted = record_observation(conn, **obs)
            if inserted:
                result.observations_created += 1
                event_types = record_events_for_observation(conn, obs)
                result.events_created += len(event_types)
            else:
                result.duplicates_skipped += 1
        except Exception as e:
            logger.error("research job: observation failed for %s: %s\n%s", ticker, e, traceback.format_exc())
            result.errors.append(f"{ticker}: {type(e).__name__}: {e}")

    try:
        matured = mature_outcomes(conn, today=today)
        result.outcomes_matured = int((matured["status"] == "matured").sum()) if not matured.empty else 0
    except Exception as e:
        logger.error("research job: outcome maturation failed: %s\n%s", e, traceback.format_exc())
        result.errors.append(f"maturation: {type(e).__name__}: {e}")

    if result.errors and result.observations_created == 0 and result.duplicates_skipped == 0:
        result.status = STATUS_FAILED

    _finish_run(conn, run_id, result)
    return result


def load_research_run_history(conn, limit: int = 20):
    import pandas as pd
    ensure_schema(conn)
    return pd.read_sql_query(
        f"SELECT * FROM {RUN_HISTORY_TABLE_NAME} ORDER BY started_at DESC LIMIT ?", conn, params=(limit,),
    )
