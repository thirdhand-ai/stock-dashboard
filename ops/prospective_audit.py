"""Phase 14 Component B: prospective evidence integrity - a read-only,
per-trading-day audit that cross-references production run history, research
job history, and recorded observations by calendar day.

Read-only by construction: only ever imports load_* functions -
`db.run_history_repository.load_run_history`,
`strategy_lab.research_automation.load_research_run_history`,
`strategy_lab.prospective.load_observations`,
`strategy_lab.outcome_maturation.load_outcomes` - never `record_observation`,
`record_event`, or `mature_outcomes`. No new UPDATE/DELETE code is added
anywhere by this module.

No retrospective creation/backfill of missed prospective predictions:
`eligibility_start_date` explicitly prevents retroactively judging days
before the research job existed - a day before that date simply never
appears in the ledger (not reported as MISSED).
"""
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

from automation.trading_calendar import is_likely_trading_day, trading_sessions_between
from db.run_history_repository import load_run_history
from strategy_lab.outcome_maturation import (
    STATUS_MATURED,
    STATUS_PENDING,
    STATUS_UNAVAILABLE,
    load_outcomes,
)
from strategy_lab.prospective import load_observations
from strategy_lab.research_automation import (
    STATUS_FAILED,
    STATUS_PARTIAL_FAILURE_RESEARCH,
    STATUS_SKIPPED_NO_FRESH_DATA,
    STATUS_SUCCESS_RESEARCH,
    load_research_run_history,
)

# Internal string literal for the "crashed mid-run" convention documented in
# docs/specs/phase14.md §0.3 - research_run_history's own DEFAULT 'running'
# (see strategy_lab/research_automation.py's _CREATE_TABLE_SQL). Not
# exported as a constant from research_automation.py itself (no such
# constant exists there today; this mirrors the same bare-string convention
# db/run_history_repository.py's STATUS_RUNNING = "running" documents for
# the production table).
_RESEARCH_STATUS_RUNNING = "running"

PROSPECTIVE_DAY_EXPECTED = "EXPECTED"                    # today, job hasn't run yet
PROSPECTIVE_DAY_CAPTURED = "CAPTURED"                     # all attempted tickers got observations
PROSPECTIVE_DAY_CAPTURED_PARTIAL = "CAPTURED_PARTIAL"     # some tickers succeeded, some errored
PROSPECTIVE_DAY_DUPLICATE_SKIPPED = "DUPLICATE_SKIPPED"   # retry found everything already captured
PROSPECTIVE_DAY_UNAVAILABLE = "UNAVAILABLE"               # job ran, but no ticker had a scoreable observation
PROSPECTIVE_DAY_MISSED = "MISSED"                         # no successful capture recorded for this day at all


@dataclass
class ProspectiveDayStatus:
    trading_date: str
    status: str
    reason: str
    production_run_status: Optional[str]
    research_run_status: Optional[str]
    observations_created: int
    duplicates_skipped: int
    n_distinct_tickers_observed: int


def eligibility_start_date(conn) -> Optional[date]:
    """MIN(trading_date) across research_run_history - the first day the
    research job was EVER run. Returns None if research_run_history is
    empty. Days before this date are never evaluated or reported as MISSED
    - the system did not exist yet for them (no retroactive fabrication)."""
    history = load_research_run_history(conn, limit=1_000_000)
    if history.empty or "trading_date" not in history.columns:
        return None
    dates = history["trading_date"].dropna()
    if dates.empty:
        return None
    return date.fromisoformat(str(dates.min()))


def _latest_row_for_date(history: pd.DataFrame, date_col: str, d_str: str) -> Optional[pd.Series]:
    if history.empty or date_col not in history.columns:
        return None
    day_rows = history[history[date_col] == d_str]
    if day_rows.empty:
        return None
    sort_col = "started_at" if "started_at" in day_rows.columns else date_col
    return day_rows.sort_values(sort_col).iloc[-1]


def _n_distinct_tickers_observed(observations: pd.DataFrame, d_str: str) -> int:
    if observations.empty:
        return 0
    day_obs = observations[observations["observation_date"] == d_str]
    if day_obs.empty:
        return 0
    return int(day_obs["ticker"].nunique())


def _classify_day(d: date, today: date, research_row: Optional[pd.Series]):
    """Per-day classification logic (first match wins) - docs/specs/phase14.md §2.2."""
    if research_row is None:
        if d == today:
            return PROSPECTIVE_DAY_EXPECTED, "today's research job has not run yet", None, 0, 0
        return PROSPECTIVE_DAY_MISSED, "no research job run recorded for this trading day", None, 0, 0

    research_run_status = research_row["status"]
    observations_created = int(research_row["observations_created"] or 0)
    duplicates_skipped = int(research_row["duplicates_skipped"] or 0)
    errors = research_row["errors"]
    skip_reason = research_row["skip_reason"]
    has_errors = bool(errors)

    if research_run_status == STATUS_SKIPPED_NO_FRESH_DATA or research_run_status == _RESEARCH_STATUS_RUNNING:
        reason = skip_reason or "run left in status=running (crashed or interrupted) - never completed"
        return PROSPECTIVE_DAY_MISSED, reason, research_run_status, observations_created, duplicates_skipped

    if research_run_status == STATUS_FAILED and observations_created == 0:
        return (
            PROSPECTIVE_DAY_MISSED, errors or "research job failed", research_run_status,
            observations_created, duplicates_skipped,
        )

    if observations_created > 0 and research_run_status in (STATUS_SUCCESS_RESEARCH, STATUS_PARTIAL_FAILURE_RESEARCH):
        if research_run_status == STATUS_SUCCESS_RESEARCH:
            return (
                PROSPECTIVE_DAY_CAPTURED, "all attempted tickers got observations", research_run_status,
                observations_created, duplicates_skipped,
            )
        return (
            PROSPECTIVE_DAY_CAPTURED_PARTIAL, errors or "some tickers errored", research_run_status,
            observations_created, duplicates_skipped,
        )

    if observations_created == 0 and duplicates_skipped > 0 and not has_errors:
        return (
            PROSPECTIVE_DAY_DUPLICATE_SKIPPED,
            "already fully captured by an earlier run this day; this run/retry did no new work",
            research_run_status, observations_created, duplicates_skipped,
        )

    if observations_created == 0 and duplicates_skipped == 0 and not has_errors and research_run_status == STATUS_SUCCESS_RESEARCH:
        return (
            PROSPECTIVE_DAY_UNAVAILABLE,
            "job ran cleanly but no ticker produced a scoreable observation, e.g. insufficient indicator "
            "history for the whole universe that day",
            research_run_status, observations_created, duplicates_skipped,
        )

    return (
        PROSPECTIVE_DAY_MISSED, "unclassifiable research_run_history row - needs manual review",
        research_run_status, observations_created, duplicates_skipped,
    )


def build_prospective_day_ledger(conn, today: Optional[date] = None) -> List[ProspectiveDayStatus]:
    """For every NYSE trading session in
    [eligibility_start_date(conn), today] (inclusive; empty list if
    eligibility_start_date is None): classify per §2.2. Pure read - one
    bounded query each against load_run_history(conn, limit=750),
    load_research_run_history(conn, limit=750), and load_observations(conn),
    filtered/grouped client-side in pandas by trading_date."""
    today = today or date.today()
    start = eligibility_start_date(conn)
    if start is None:
        return []

    production_history = load_run_history(conn, limit=750)
    research_history = load_research_run_history(conn, limit=750)
    observations = load_observations(conn)

    sessions = [d for d in trading_sessions_between(start, today) if is_likely_trading_day(d)]

    ledger: List[ProspectiveDayStatus] = []
    for d in sessions:
        d_str = d.isoformat()
        prod_row = _latest_row_for_date(production_history, "trading_date", d_str)
        production_run_status = prod_row["status"] if prod_row is not None else None

        research_row = _latest_row_for_date(research_history, "trading_date", d_str)
        n_observed = _n_distinct_tickers_observed(observations, d_str)

        status, reason, research_run_status, observations_created, duplicates_skipped = _classify_day(
            d, today, research_row,
        )
        ledger.append(ProspectiveDayStatus(
            trading_date=d_str, status=status, reason=reason,
            production_run_status=production_run_status,
            research_run_status=research_run_status,
            observations_created=observations_created, duplicates_skipped=duplicates_skipped,
            n_distinct_tickers_observed=n_observed,
        ))
    return ledger


def prospective_evidence_audit_summary(conn, today: Optional[date] = None) -> dict:
    """{'counts_by_status': {...}, 'ledger_days': N, 'eligibility_start':
    ..., 'maturation_by_horizon': {horizon: {'pending':n,'matured':n,
    'unavailable':n}} - a pure rollup of
    strategy_lab.outcome_maturation.load_outcomes, grouped by horizon_days,
    reusing that table's existing status vocabulary verbatim rather than
    inventing a rival one (§0.5 item 1)."""
    ledger = build_prospective_day_ledger(conn, today=today)
    counts: Dict[str, int] = {}
    for day in ledger:
        counts[day.status] = counts.get(day.status, 0) + 1

    outcomes = load_outcomes(conn)
    maturation_by_horizon: Dict[int, dict] = {}
    if not outcomes.empty:
        for horizon, group in outcomes.groupby("horizon_days"):
            status_counts = group["status"].value_counts().to_dict()
            maturation_by_horizon[int(horizon)] = {
                "pending": int(status_counts.get(STATUS_PENDING, 0)),
                "matured": int(status_counts.get(STATUS_MATURED, 0)),
                "unavailable": int(status_counts.get(STATUS_UNAVAILABLE, 0)),
            }

    start = eligibility_start_date(conn)
    return {
        "counts_by_status": counts,
        "ledger_days": len(ledger),
        "eligibility_start": start.isoformat() if start else None,
        "maturation_by_horizon": maturation_by_horizon,
    }
