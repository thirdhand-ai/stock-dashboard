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
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from automation.trading_calendar import is_likely_trading_day, trading_sessions_between
from db.run_history_repository import load_run_history
from ops.correction_impact_audit import load_corrections
from ops.data_quality import OVERALL_DEGRADED, OVERALL_FAILED, OVERALL_HEALTHY, OVERALL_STALE
from ops.evidence_provenance import PROVENANCE_UNKNOWN_LEGACY, label_provenance_value
from ops.experiment_registry import list_experiments
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
    STATUS_SKIPPED_NON_TRADING_DAY,
    STATUS_SUCCESS_RESEARCH,
    load_research_run_history,
)

# --- Phase 16 additive imports (§5.2) ---
from ops.completeness_classification import (
    classify_event_row,
    classify_observation_row,
    classify_outcome_row,
)
from ops.event_provenance_audit import event_provenance_audit_summary
from ops.evidence_classification import (
    EVIDENCE_INSUFFICIENT_DATA,
    classify_evidence,
    count_prospective_trading_days,
)
from ops.regime_reconstruction_audit import legacy_regime_gap_summary
from strategy_lab.outcome_maturation import source_resolution_summary
from strategy_lab.prospective_events import load_events

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


# --- Phase 15 Component C: long-term operational monitoring (additive).
# Everything below reuses build_prospective_day_ledger()/
# eligibility_start_date()/_latest_row_for_date() above rather than
# re-querying - still read-only, still no retrospective backfill. ---

_CAPTURED_STATUSES = (
    PROSPECTIVE_DAY_CAPTURED, PROSPECTIVE_DAY_CAPTURED_PARTIAL, PROSPECTIVE_DAY_DUPLICATE_SKIPPED,
)


def compute_capture_rate(conn, today: Optional[date] = None, window_sessions: int = 60) -> dict:
    """Built on build_prospective_day_ledger() (reused, not re-queried).
    Over the ledger days within the trailing window_sessions NYSE sessions
    (never extending before eligibility_start_date(conn)):
    capture_rate_pct = 100 * (CAPTURED + CAPTURED_PARTIAL + DUPLICATE_SKIPPED)
                        / (all ledger days in window EXCEPT status==EXPECTED)
    EXPECTED (today, job hasn't run yet) is excluded from both numerator
    and denominator. Returns {'window_sessions_considered', 'captured',
    'missed', 'unavailable', 'capture_rate_pct' (None if considered==0)}."""
    today = today or date.today()
    ledger = build_prospective_day_ledger(conn, today=today)
    window = ledger[-window_sessions:] if window_sessions else ledger
    considered = [d for d in window if d.status != PROSPECTIVE_DAY_EXPECTED]

    captured = sum(1 for d in considered if d.status in _CAPTURED_STATUSES)
    missed = sum(1 for d in considered if d.status == PROSPECTIVE_DAY_MISSED)
    unavailable = sum(1 for d in considered if d.status == PROSPECTIVE_DAY_UNAVAILABLE)
    n = len(considered)

    return {
        "window_sessions_considered": n,
        "captured": captured,
        "missed": missed,
        "unavailable": unavailable,
        "capture_rate_pct": round(100.0 * captured / n, 2) if n > 0 else None,
    }


def compute_consecutive_missed_days(conn, today: Optional[date] = None) -> int:
    """Trailing run-length of PROSPECTIVE_DAY_MISSED, counting backward from
    the most recent non-EXPECTED ledger day. 0 if that day isn't MISSED."""
    today = today or date.today()
    ledger = build_prospective_day_ledger(conn, today=today)
    non_expected = [d for d in ledger if d.status != PROSPECTIVE_DAY_EXPECTED]

    count = 0
    for day in reversed(non_expected):
        if day.status != PROSPECTIVE_DAY_MISSED:
            break
        count += 1
    return count


def _parse_matured_at_date(matured_at: Optional[str]) -> Optional[date]:
    """matured_at is a naive-UTC `datetime('now')` string
    (db/schema.py convention) - returns just its calendar date, or None on
    anything unparseable/missing (never raises)."""
    if not matured_at:
        return None
    try:
        return datetime.strptime(str(matured_at)[:19], "%Y-%m-%d %H:%M:%S").date()
    except ValueError:
        return None


def _horizon_elapsed_session(obs_date: date, horizon_days: int) -> Optional[date]:
    """The NYSE trading session on which `horizon_days` real trading
    sessions have elapsed since obs_date - derived directly from the NYSE
    calendar (automation.trading_calendar), not from a possibly-gappy
    stored price series. trading_sessions_between(obs_date, end) is
    inclusive of obs_date itself at index 0, so the session `horizon_days`
    positions later (index `horizon_days`) is the one on which the horizon
    first genuinely elapsed."""
    end = obs_date + timedelta(days=horizon_days * 3 + 30)
    sessions = trading_sessions_between(obs_date, end)
    if len(sessions) <= horizon_days:
        end = obs_date + timedelta(days=horizon_days * 5 + 60)
        sessions = trading_sessions_between(obs_date, end)
    if len(sessions) <= horizon_days:
        return None
    return sessions[horizon_days]


def compute_maturity_lag(conn) -> dict:
    """Per horizon_days, over MATURED outcomes only: mean/median CALENDAR-
    day lag (date.fromisoformat(exit_date) - date.fromisoformat(observation_date)).days.
    Session-day lag is NOT separately reported - it always equals
    horizon_days by construction, so a second number would just restate
    horizon_days. Also reports mean 'pipeline notice lag' - calendar days
    between the NYSE session on which the horizon first genuinely elapsed
    and matured_at's calendar date - 0 for the expected/common case, >0
    only if a research job was MISSED on the day maturity first became
    available. Returns {horizon_days: {'n_matured', 'mean_calendar_day_lag',
    'median_calendar_day_lag', 'mean_pipeline_notice_lag_days'}}."""
    outcomes = load_outcomes(conn)
    if outcomes.empty:
        return {}
    matured = outcomes[outcomes["status"] == STATUS_MATURED]
    if matured.empty:
        return {}

    result: Dict[int, dict] = {}
    for horizon, group in matured.groupby("horizon_days"):
        calendar_lags: List[int] = []
        notice_lags: List[int] = []
        for _, row in group.iterrows():
            try:
                obs_date = date.fromisoformat(row["observation_date"])
                exit_date = date.fromisoformat(row["exit_date"])
            except (TypeError, ValueError):
                continue
            calendar_lags.append((exit_date - obs_date).days)

            matured_at_date = _parse_matured_at_date(row.get("matured_at"))
            horizon_session = _horizon_elapsed_session(obs_date, int(horizon))
            if matured_at_date is not None and horizon_session is not None:
                notice_lags.append(max(0, (matured_at_date - horizon_session).days))

        result[int(horizon)] = {
            "n_matured": len(group),
            "mean_calendar_day_lag": round(statistics.mean(calendar_lags), 2) if calendar_lags else None,
            "median_calendar_day_lag": statistics.median(calendar_lags) if calendar_lags else None,
            "mean_pipeline_notice_lag_days": round(statistics.mean(notice_lags), 2) if notice_lags else None,
        }
    return result


def compute_research_job_rates(conn, today: Optional[date] = None, window_sessions: int = 60) -> dict:
    """Over load_research_run_history(), collapsed to one row per
    trading_date (reusing the same latest-row-per-date logic
    build_prospective_day_ledger() already applies, via _latest_row_for_date,
    imported not reimplemented) within the trailing window_sessions:
    success_rate_pct / partial_failure_rate_pct / failure_rate_pct /
    crashed_rate_pct (STATUS_RUNNING left stuck), denominator excludes
    STATUS_SKIPPED_NON_TRADING_DAY. A trading session with NO
    research_run_history row at all (job never even attempted) is excluded
    from the denominator too - there is no run-status to rate."""
    today = today or date.today()
    start = eligibility_start_date(conn)
    if start is None:
        return {
            "sessions_considered": 0, "success_rate_pct": None, "partial_failure_rate_pct": None,
            "failure_rate_pct": None, "crashed_rate_pct": None,
        }

    history = load_research_run_history(conn, limit=1_000_000)
    sessions = [d for d in trading_sessions_between(start, today) if is_likely_trading_day(d)]
    if window_sessions:
        sessions = sessions[-window_sessions:]

    statuses: List[str] = []
    for d in sessions:
        row = _latest_row_for_date(history, "trading_date", d.isoformat())
        if row is None:
            continue
        statuses.append(row["status"])

    considered = [s for s in statuses if s != STATUS_SKIPPED_NON_TRADING_DAY]
    n = len(considered)

    def _pct(count: int) -> Optional[float]:
        return round(100.0 * count / n, 2) if n > 0 else None

    return {
        "sessions_considered": n,
        "success_rate_pct": _pct(sum(1 for s in considered if s == STATUS_SUCCESS_RESEARCH)),
        "partial_failure_rate_pct": _pct(sum(1 for s in considered if s == STATUS_PARTIAL_FAILURE_RESEARCH)),
        "failure_rate_pct": _pct(sum(1 for s in considered if s == STATUS_FAILED)),
        "crashed_rate_pct": _pct(sum(1 for s in considered if s == _RESEARCH_STATUS_RUNNING)),
    }


def compute_correction_counts(conn, since: Optional[str] = None) -> dict:
    """Reuses ops.correction_impact_audit.load_corrections() (imported, not
    reimplemented). {'total', 'affecting_frozen_artifacts', 'since'}."""
    corrections = load_corrections(conn)
    if since and not corrections.empty:
        corrections = corrections[corrections["corrected_at"] >= since]

    total = len(corrections)
    affecting = 0
    if not corrections.empty:
        affecting = int(corrections["affects_frozen_artifacts"].apply(
            lambda v: v is not None and str(v) not in ("", "[]", "null")
        ).sum())

    return {"total": total, "affecting_frozen_artifacts": affecting, "since": since}


def _has_provenance_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and pd.isna(value):
        return False
    return value != ""


def compute_provenance_coverage(conn) -> dict:
    """{'observations_with_fingerprint', 'observations_unknown_legacy',
    'by_experiment_id': {experiment_id: n}, 'unregistered_fingerprint_count'}.
    For rows with non-NULL config_fingerprint: cross-references
    ops.experiment_registry.list_experiments()'s config_fingerprint column
    - one observation's fingerprint can match MULTIPLE experiment_ids
    (§0.1, CONTROL/A/B share one fingerprint today), so by_experiment_id
    counts are NOT mutually exclusive. A fingerprint matching ZERO
    currently-registered experiment_id goes to 'unregistered_fingerprint_count'
    - distinct from observations_unknown_legacy (NULL column)."""
    observations = load_observations(conn)
    if observations.empty:
        return {
            "observations_with_fingerprint": 0, "observations_unknown_legacy": 0,
            "by_experiment_id": {}, "unregistered_fingerprint_count": 0,
        }

    has_fp = observations["config_fingerprint"].apply(_has_provenance_value)
    observations_with_fingerprint = int(has_fp.sum())
    observations_unknown_legacy = int((~has_fp).sum())
    by_fp_count = observations.loc[has_fp, "config_fingerprint"].value_counts().to_dict()

    experiments = list_experiments(conn)
    fp_to_experiment_ids: Dict[str, List[str]] = {}
    if not experiments.empty:
        for _, exp in experiments.iterrows():
            fp_to_experiment_ids.setdefault(exp["config_fingerprint"], []).append(exp["experiment_id"])

    by_experiment_id: Dict[str, int] = {}
    unregistered_fingerprint_count = 0
    for fp, count in by_fp_count.items():
        exp_ids = fp_to_experiment_ids.get(fp, [])
        if not exp_ids:
            unregistered_fingerprint_count += int(count)
        for exp_id in exp_ids:
            by_experiment_id[exp_id] = by_experiment_id.get(exp_id, 0) + int(count)

    return {
        "observations_with_fingerprint": observations_with_fingerprint,
        "observations_unknown_legacy": observations_unknown_legacy,
        "by_experiment_id": by_experiment_id,
        "unregistered_fingerprint_count": unregistered_fingerprint_count,
    }


def classify_research_operational_health(
    capture_rate_pct: Optional[float], consecutive_missed_days: int,
    job_failure_rate_pct: Optional[float],
) -> str:
    """Pure, deterministic. Returns one of ops.data_quality.OVERALL_HEALTHY/
    DEGRADED/STALE/FAILED - imported and reused verbatim. A SEPARATE
    classification function from ops.data_quality.classify_overall
    (different input domain) sharing the same 4-value vocabulary.
      FAILED:   consecutive_missed_days >= 5, OR capture_rate_pct is None.
      STALE:    consecutive_missed_days in [2, 4].
      DEGRADED: consecutive_missed_days in [0, 1] AND
                 (capture_rate_pct < 90 OR (job_failure_rate_pct or 0) > 10).
      HEALTHY:  otherwise.
    Explicitly NOT ops/evidence_classification.py's
    INSUFFICIENT_DATA/EARLY_EVIDENCE/EVALUATION_READY vocabulary (untouched
    - that measures evidence SUFFICIENCY, this measures pipeline
    RELIABILITY); the two must always be shown side by side, never merged."""
    if consecutive_missed_days >= 5 or capture_rate_pct is None:
        return OVERALL_FAILED
    if 2 <= consecutive_missed_days <= 4:
        return OVERALL_STALE
    if consecutive_missed_days in (0, 1) and (capture_rate_pct < 90 or (job_failure_rate_pct or 0) > 10):
        return OVERALL_DEGRADED
    return OVERALL_HEALTHY


def long_term_monitoring_summary(conn, today: Optional[date] = None) -> dict:
    """Additive rollup: returns prospective_evidence_audit_summary(conn, today)'s
    existing dict (UNCHANGED keys) with new keys merged in: 'capture_rate',
    'consecutive_missed_days', 'maturity_lag_by_horizon',
    'research_job_rates', 'correction_counts', 'provenance_coverage',
    'operational_health'. This is the one function Component D's dashboard
    getter calls - never call the individual compute_* functions directly
    from a dashboard view."""
    today = today or date.today()
    summary = dict(prospective_evidence_audit_summary(conn, today=today))

    capture_rate = compute_capture_rate(conn, today=today)
    consecutive_missed_days = compute_consecutive_missed_days(conn, today=today)
    research_job_rates = compute_research_job_rates(conn, today=today)

    summary.update({
        "capture_rate": capture_rate,
        "consecutive_missed_days": consecutive_missed_days,
        "maturity_lag_by_horizon": compute_maturity_lag(conn),
        "research_job_rates": research_job_rates,
        "correction_counts": compute_correction_counts(conn),
        "provenance_coverage": compute_provenance_coverage(conn),
        "operational_health": classify_research_operational_health(
            capture_rate.get("capture_rate_pct"), consecutive_missed_days,
            research_job_rates.get("failure_rate_pct"),
        ),
    })
    return summary


# --- Phase 16 §5.2: additive monitoring/reporting functions. Everything
# below reuses existing load_* functions and Areas B/C/D/E helpers rather
# than re-deriving anything - still read-only, still no retrospective
# backfill. ---


def compute_regime_distribution(conn) -> dict:
    """{'by_label': {label_or_'NULL': count}, 'total'} - groups
    load_observations(conn)['regime'] including a literal 'NULL' bucket
    key for None values (a REPORTING bucket only - never a stored DB
    value; NULL rows may be either pre-Area-A-fix legacy rows or genuine
    insufficient-history rows, disambiguated via legacy_regime_gap_summary
    below, not conflated here)."""
    observations = load_observations(conn)
    if observations.empty:
        return {"by_label": {}, "total": 0}

    by_label: Dict[str, int] = {}
    for value in observations["regime"]:
        label = "NULL" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)
        by_label[label] = by_label.get(label, 0) + 1

    return {"by_label": by_label, "total": int(len(observations))}


def compute_completeness_breakdown(conn) -> dict:
    """{'observations': {COMPLETE:n, PARTIAL:n, LEGACY_INCOMPLETE:n, INVALID:n, 'total':n},
    'events': {...same 4 keys...}, 'outcomes': {...same 4 keys...}} - one
    classify_*_row() call per row via load_observations/load_events/load_outcomes."""

    def _rollup(df: pd.DataFrame, classify_fn) -> dict:
        counts = {
            "COMPLETE": 0, "PARTIAL": 0, "LEGACY_INCOMPLETE": 0, "INVALID": 0, "total": 0,
        }
        if df.empty:
            return counts
        for _, row in df.iterrows():
            label = classify_fn(row.to_dict())
            counts[label] = counts.get(label, 0) + 1
            counts["total"] += 1
        return counts

    observations = load_observations(conn)
    events = load_events(conn)
    outcomes = load_outcomes(conn)

    return {
        "observations": _rollup(observations, classify_observation_row),
        "events": _rollup(events, classify_event_row),
        "outcomes": _rollup(outcomes, classify_outcome_row),
    }


def phase16_monitoring_summary(conn, today: Optional[date] = None) -> dict:
    """Additive rollup: returns long_term_monitoring_summary(conn, today)'s
    existing dict (UNCHANGED keys, Phase 15) with new keys merged in:
    'regime_distribution', 'legacy_regime_gap_summary',
    'completeness_breakdown', 'event_provenance_audit',
    'outcome_source_resolution' (source_resolution_summary(conn)),
    'evidence_sufficiency': {'n_prospective_trading_days':
    count_prospective_trading_days(conn), 'status': classify_evidence(n)} -
    the SAME existing vocabulary/thresholds (ops/evidence_classification.py,
    untouched), reused not reimplemented. This is the one function
    Phase 16's dashboard getter calls."""
    today = today or date.today()
    summary = dict(long_term_monitoring_summary(conn, today=today))

    n_prospective_trading_days = count_prospective_trading_days(conn)
    summary.update({
        "regime_distribution": compute_regime_distribution(conn),
        "legacy_regime_gap_summary": legacy_regime_gap_summary(conn),
        "completeness_breakdown": compute_completeness_breakdown(conn),
        "event_provenance_audit": event_provenance_audit_summary(conn),
        "outcome_source_resolution": source_resolution_summary(conn),
        "evidence_sufficiency": {
            "n_prospective_trading_days": n_prospective_trading_days,
            "status": classify_evidence(n_prospective_trading_days),
        },
    })
    return summary
