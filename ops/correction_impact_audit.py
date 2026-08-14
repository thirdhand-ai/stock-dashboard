"""Phase 15 Component B: correction-impact audit.

Structurally read-only, not just conventionally: this module never calls
`conn.execute`/`conn.executemany`/`conn.executescript`/`conn.commit`
directly, never contains an `INSERT`/`UPDATE`/`DELETE` SQL string, and never
imports `record_observation`, `record_event`, `mature_outcomes`, `_persist`,
`refresh_incomplete_latest_bars`, or `_record_correction`. Every read below
goes through `pandas.read_sql_query` (via the caller's `conn`) or an
existing `load_*` function - see tests/test_ops_correction_impact_audit.py's
AST/source-text scan, which fails the build if a future edit ever adds a
mutation path here. This is the structural fix for the Phase 14 incident
(a live batch-correction command run before checking frozen-artifact
impact): Component B has no mutation capability in the code at all, so it
can never repeat that mistake even if misused.

Answers the real, confirmed AAPL question (docs/specs/phase15.md §0.2):
`load_corrections(conn, ticker="AAPL")` -> find the row with
`date == "2026-08-12"` -> `find_outcomes_affected_by_correction(...)` ->
filter for `horizon_days == 1` -> `affected_leg == "entry"` but
`source_matched == False`, because the real observation's `source` is NULL
(resolved to production `alpaca` at maturation time, never
`alpaca_adjusted`) - the Phase 14 correction (scoped to `alpaca_adjusted`
only) did NOT actually affect this outcome's stored value. The join never
silently drops a same-ticker/same-date candidate just because sources
differ; it reports `source_matched=False` so this is visible, not hidden.
"""
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

import pandas as pd

from strategy_lab.cache_integrity import CORRECTIONS_TABLE
from strategy_lab.outcome_maturation import STATUS_MATURED, compute_outcome_for_horizon, load_outcomes
from strategy_lab.prospective import load_observations


def load_corrections(conn, ticker: Optional[str] = None) -> pd.DataFrame:
    """Pure SELECT over research_cache_corrections (Phase 14). Returns an
    empty DataFrame (not a raised exception) if the table doesn't exist yet
    on a fresh DB with zero corrections ever recorded."""
    query = f"SELECT * FROM {CORRECTIONS_TABLE}"
    params: tuple = ()
    if ticker:
        query += " WHERE ticker = ?"
        params = (ticker,)
    query += " ORDER BY corrected_at ASC, id ASC"
    try:
        return pd.read_sql_query(query, conn, params=params)
    except Exception:
        # Fresh DB, table never created (strategy_lab.cache_integrity's own
        # ensure_schema has never run) - no corrections to report, not an
        # error condition for a read-only audit.
        return pd.DataFrame()


def _clean(value):
    """None/NaN/empty-string -> None; anything else returned unchanged.
    Small local helper so a NULL column read back through pandas (which can
    surface as None OR NaN depending on dtype) is normalized once, rather
    than re-deriving this at every call site."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if value == "":
        return None
    return value


def _clean_float(value) -> Optional[float]:
    value = _clean(value)
    return float(value) if value is not None else None


@dataclass
class AffectedOutcome:
    outcome_id: int
    observation_id: int
    ticker: str
    observation_date: str
    horizon_days: int
    exit_date: Optional[str]
    status: str
    affected_leg: str        # "entry" | "exit"
    source_matched: bool     # True only if outcome's source == correction's source
    stored_realized_return: Optional[float]
    correction_id: int


def find_outcomes_affected_by_correction(conn, correction_row: dict) -> List[AffectedOutcome]:
    """Loads research_prospective_outcomes (load_outcomes), matches:
      entry leg: outcome.observation_date == correction_row['date']
                  AND outcome.ticker == correction_row['ticker']
      exit leg:  outcome.exit_date == correction_row['date']
                  AND outcome.ticker == correction_row['ticker']
    For each candidate match, resolves the effective source used at
    maturation: outcome.source if non-NULL (post-Phase-15 rows), else
    (legacy NULL) falls back to a join against research_prospective_observations
    via observation_id to read that row's source column (same value
    _compute_one would have read at maturation time). `source_matched =
    (effective_source == correction_row['source'])`. Rows where
    source_matched is False are still RETURNED (never silently dropped -
    confirmed necessary by the real AAPL case, §0.2) but flagged so
    callers/dashboard can distinguish "not actually affected" from
    "affected." Read-only: SELECT-only, filtered in pandas. Never calls
    mature_outcomes()."""
    correction_ticker = correction_row["ticker"]
    correction_date = correction_row["date"]
    correction_source = correction_row["source"]
    correction_id = int(correction_row["id"])

    outcomes = load_outcomes(conn, ticker=correction_ticker)
    if outcomes.empty:
        return []

    observations = load_observations(conn, ticker=correction_ticker)
    obs_source_by_id = {}
    if not observations.empty:
        for _, obs_row in observations.iterrows():
            obs_source_by_id[int(obs_row["id"])] = _clean(obs_row.get("source"))

    results: List[AffectedOutcome] = []
    for _, row in outcomes.iterrows():
        row_ticker = row["ticker"]
        entry_match = row_ticker == correction_ticker and row["observation_date"] == correction_date
        exit_match = row_ticker == correction_ticker and _clean(row.get("exit_date")) == correction_date
        if not entry_match and not exit_match:
            continue
        # §0.2 finding 1: never simultaneously true for the same row.
        affected_leg = "entry" if entry_match else "exit"

        observation_id = int(row["observation_id"])
        effective_source = _clean(row.get("source"))
        if effective_source is None:
            effective_source = obs_source_by_id.get(observation_id)
        source_matched = effective_source == correction_source

        results.append(AffectedOutcome(
            outcome_id=int(row["id"]),
            observation_id=observation_id,
            ticker=row_ticker,
            observation_date=row["observation_date"],
            horizon_days=int(row["horizon_days"]),
            exit_date=_clean(row.get("exit_date")),
            status=row["status"],
            affected_leg=affected_leg,
            source_matched=source_matched,
            stored_realized_return=_clean_float(row.get("realized_return")),
            correction_id=correction_id,
        ))
    return results


@dataclass
class RecomputationResult:
    outcome_id: int
    observation_id: int
    ticker: str
    observation_date: str
    horizon_days: int
    stored_status: str
    stored_realized_return: Optional[float]
    stored_exit_date: Optional[str]
    recomputed_status: str
    recomputed_realized_return: Optional[float]
    recomputed_exit_date: Optional[str]
    mismatch: bool
    detail: str


def _returns_differ(stored: Optional[float], recomputed: Optional[float]) -> bool:
    """None vs a float (or vice versa) counts as a mismatch. Two floats
    mismatch iff their absolute difference exceeds 1e-9."""
    if stored is None and recomputed is None:
        return False
    if stored is None or recomputed is None:
        return True
    return abs(stored - recomputed) > 1e-9


def recompute_outcome_deterministically(
    conn, outcome_row: dict, observation_row: dict, today: Optional[date] = None,
) -> RecomputationResult:
    """Calls strategy_lab.outcome_maturation.compute_outcome_for_horizon
    (§1.3's new public read-only alias for _compute_one) against the CURRENT
    contents of `prices` - post any correction - WITHOUT calling
    mature_outcomes() and WITHOUT persisting anything. mismatch=True iff
    |stored_realized_return - recomputed_realized_return| > 1e-9 (treating
    None vs a float, or vice versa, as a mismatch), OR stored_status !=
    recomputed_status, OR stored_exit_date != recomputed_exit_date. Never
    writes stored_* anywhere - recomputed_* is returned to the caller for
    display/human review only."""
    today = today or date.today()
    horizon_days = int(outcome_row["horizon_days"])

    recomputed = compute_outcome_for_horizon(conn, observation_row, horizon_days, today)

    stored_status = outcome_row["status"]
    stored_realized_return = _clean_float(outcome_row.get("realized_return"))
    stored_exit_date = _clean(outcome_row.get("exit_date"))

    recomputed_status = recomputed.status
    recomputed_realized_return = recomputed.realized_return
    recomputed_exit_date = recomputed.exit_date

    status_mismatch = stored_status != recomputed_status
    return_mismatch = _returns_differ(stored_realized_return, recomputed_realized_return)
    exit_date_mismatch = stored_exit_date != recomputed_exit_date
    mismatch = status_mismatch or return_mismatch or exit_date_mismatch

    detail_parts = []
    if status_mismatch:
        detail_parts.append(f"status: stored={stored_status!r} recomputed={recomputed_status!r}")
    if return_mismatch:
        detail_parts.append(
            f"realized_return: stored={stored_realized_return!r} recomputed={recomputed_realized_return!r}"
        )
    if exit_date_mismatch:
        detail_parts.append(f"exit_date: stored={stored_exit_date!r} recomputed={recomputed_exit_date!r}")
    detail = "; ".join(detail_parts) if detail_parts else "recomputation matches stored value"

    return RecomputationResult(
        outcome_id=int(outcome_row["id"]),
        observation_id=int(outcome_row["observation_id"]),
        ticker=outcome_row["ticker"],
        observation_date=outcome_row["observation_date"],
        horizon_days=horizon_days,
        stored_status=stored_status,
        stored_realized_return=stored_realized_return,
        stored_exit_date=stored_exit_date,
        recomputed_status=recomputed_status,
        recomputed_realized_return=recomputed_realized_return,
        recomputed_exit_date=recomputed_exit_date,
        mismatch=mismatch,
        detail=detail,
    )


def audit_correction(conn, correction_id: int, today: Optional[date] = None) -> dict:
    """Primary entry point. Returns:
      {'correction': {...}, 'affected_outcomes': [AffectedOutcome...],
       'recomputations': [RecomputationResult...]}   # one per affected
                                                        # outcome whose
                                                        # stored status ==
                                                        # STATUS_MATURED
                                                        # only - a still-
                                                        # PENDING outcome has
                                                        # nothing stored yet
                                                        # to compare against,
                                                        # recomputation is
                                                        # skipped, not
                                                        # fabricated.
    Raises ValueError if correction_id not found. Zero writes."""
    corrections = load_corrections(conn)
    if corrections.empty:
        raise ValueError(f"correction_id={correction_id!r} not found in {CORRECTIONS_TABLE}")
    match = corrections[corrections["id"] == correction_id]
    if match.empty:
        raise ValueError(f"correction_id={correction_id!r} not found in {CORRECTIONS_TABLE}")
    correction_row = match.iloc[0].to_dict()

    affected = find_outcomes_affected_by_correction(conn, correction_row)

    observations = load_observations(conn, ticker=correction_row["ticker"])
    obs_by_id = {}
    if not observations.empty:
        for _, obs_row in observations.iterrows():
            obs_by_id[int(obs_row["id"])] = obs_row.to_dict()

    recomputations: List[RecomputationResult] = []
    for a in affected:
        if a.status != STATUS_MATURED:
            continue
        observation_row = obs_by_id.get(a.observation_id)
        if observation_row is None:
            continue
        outcome_row = {
            "id": a.outcome_id, "observation_id": a.observation_id, "ticker": a.ticker,
            "observation_date": a.observation_date, "horizon_days": a.horizon_days,
            "status": a.status, "realized_return": a.stored_realized_return, "exit_date": a.exit_date,
        }
        recomputations.append(
            recompute_outcome_deterministically(conn, outcome_row, observation_row, today=today)
        )

    return {"correction": correction_row, "affected_outcomes": affected, "recomputations": recomputations}


def audit_all_corrections(conn, today: Optional[date] = None) -> pd.DataFrame:
    """Flattens audit_correction() over every research_cache_corrections row
    into one row per (correction, affected_outcome) pair - used by
    Component C's correction-count rollup and Component D's dashboard
    table. Empty DataFrame if there are no corrections."""
    corrections = load_corrections(conn)
    if corrections.empty:
        return pd.DataFrame()

    rows = []
    for _, corr in corrections.iterrows():
        result = audit_correction(conn, int(corr["id"]), today=today)
        recomp_by_outcome_id = {r.outcome_id: r for r in result["recomputations"]}
        for affected in result["affected_outcomes"]:
            recomp = recomp_by_outcome_id.get(affected.outcome_id)
            rows.append({
                "correction_id": affected.correction_id,
                "correction_ticker": result["correction"]["ticker"],
                "correction_date": result["correction"]["date"],
                "correction_source": result["correction"]["source"],
                "outcome_id": affected.outcome_id,
                "observation_id": affected.observation_id,
                "ticker": affected.ticker,
                "observation_date": affected.observation_date,
                "horizon_days": affected.horizon_days,
                "exit_date": affected.exit_date,
                "status": affected.status,
                "affected_leg": affected.affected_leg,
                "source_matched": affected.source_matched,
                "stored_realized_return": affected.stored_realized_return,
                "recomputed_status": recomp.recomputed_status if recomp else None,
                "recomputed_realized_return": recomp.recomputed_realized_return if recomp else None,
                "recomputed_exit_date": recomp.recomputed_exit_date if recomp else None,
                "recomputation_mismatch": recomp.mismatch if recomp else None,
            })
    return pd.DataFrame(rows)
