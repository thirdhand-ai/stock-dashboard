"""Phase 11 spec B4: immutable prospective research events - one row per
genuine state TRANSITION (never one row per consecutive day a condition
merely continues to hold), reusing the exact same crossing/advancement
logic alerts/engine.py uses for production alerts
(determine_alert_reasons) so "what counts as an event" can never drift
into a different definition than the live system's.

No event here can submit a trade or send Discord - this module has no
import of trading.orders/engine/run_paper or alerts.discord/runner/
run_alerts (see tests/test_strategy_lab.py's structural safety tests,
which scan every file in this package for exactly those imports).
"""
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import pandas as pd

from alerts.config import DEFAULT_ALERT_CONFIG
from alerts.engine import determine_alert_reasons
from backtest.scoring import STAGE_ORDER
from strategy_lab.prospective import METHODOLOGY_VERSION, load_observations

EVENT_TABLE_NAME = "research_prospective_events"

EVENT_SCORE_CROSSING = "score_crossing_70"
EVENT_TREND_ADVANCE = "trend_advance"
EVENT_MOMENTUM_ADVANCE = "momentum_advance"
EVENT_VOLUME_ADVANCE = "volume_advance"
EVENT_CONTROL_ENTRY = "control_entry"
EVENT_EXPERIMENT_A_ENTRY = "experiment_a_entry"
EVENT_EXPERIMENT_B_ENTRY = "experiment_b_entry"

ALL_EVENT_TYPES = (
    EVENT_SCORE_CROSSING, EVENT_TREND_ADVANCE, EVENT_MOMENTUM_ADVANCE, EVENT_VOLUME_ADVANCE,
    EVENT_CONTROL_ENTRY, EVENT_EXPERIMENT_A_ENTRY, EVENT_EXPERIMENT_B_ENTRY,
)

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {EVENT_TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    event_type TEXT NOT NULL,
    score REAL,
    stage TEXT,
    regime TEXT,
    methodology_version TEXT,
    UNIQUE(ticker, event_type, event_date)
)
"""


def ensure_schema(conn) -> None:
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()


def _stage_advance_events(prev_stage: Optional[str], curr_stage: Optional[str]) -> List[str]:
    if prev_stage is None or curr_stage is None:
        return []
    events = []
    for stage_name, event_type in (
        ("trend", EVENT_TREND_ADVANCE), ("momentum", EVENT_MOMENTUM_ADVANCE), ("volume", EVENT_VOLUME_ADVANCE),
    ):
        threshold_rank = STAGE_ORDER[stage_name]
        if STAGE_ORDER[curr_stage] >= threshold_rank > STAGE_ORDER[prev_stage]:
            events.append(event_type)
    return events


def _entry_transition_events(prev_row: Optional[dict], curr_row: dict) -> List[str]:
    """Entry-signal events fire only on a genuine False->True transition.
    A ticker's very first observation is never an event, matching
    alerts/engine.py's "no baseline, no alert" convention - and a signal
    that stays continuously eligible for many days produces exactly one
    event, on the day it first became eligible."""
    if prev_row is None:
        return []
    events = []
    for field_name, event_type in (
        ("control_entry_signal", EVENT_CONTROL_ENTRY),
        ("experiment_a_entry_signal", EVENT_EXPERIMENT_A_ENTRY),
        ("experiment_b_entry_signal", EVENT_EXPERIMENT_B_ENTRY),
    ):
        was = bool(prev_row.get(field_name))
        now = bool(curr_row.get(field_name))
        if now and not was:
            events.append(event_type)
    return events


def detect_events_for_new_observation(conn, curr_row: dict) -> List[str]:
    """curr_row: an observation dict/row (see strategy_lab.prospective.
    build_todays_observation), already or about-to-be recorded. Compares
    against the most recent PRIOR stored observation for this ticker
    (strictly before curr_row's observation_date) to detect genuine
    transitions - never fires on a level that merely continues to hold."""
    history = load_observations(conn, ticker=curr_row["ticker"])
    prior = history[history["observation_date"] < curr_row["observation_date"]]
    prev_row = prior.iloc[-1].to_dict() if not prior.empty else None

    prev_score = prev_row["score"] if prev_row else None
    prev_stage = prev_row["stage"] if prev_row else None

    reasons = determine_alert_reasons(prev_score, curr_row["score"], prev_stage, curr_row["stage"], DEFAULT_ALERT_CONFIG)
    event_types = []
    if "score_crossing" in reasons:
        event_types.append(EVENT_SCORE_CROSSING)
    event_types += _stage_advance_events(prev_stage, curr_row["stage"])
    event_types += _entry_transition_events(prev_row, curr_row)
    return event_types


def record_event(conn, *, event_date: str, ticker: str, event_type: str, score, stage, regime,
                  methodology_version: str = METHODOLOGY_VERSION) -> bool:
    """Insert-only, deduped on (ticker, event_type, event_date) - a repeat
    call for the same transition on the same date is a silent no-op."""
    ensure_schema(conn)
    cur = conn.cursor()
    cur.execute(
        f"""
        INSERT INTO {EVENT_TABLE_NAME} (created_at, event_date, ticker, event_type, score, stage, regime, methodology_version)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker, event_type, event_date) DO NOTHING
        """,
        (datetime.now(timezone.utc).isoformat(), event_date, ticker, event_type, score, stage, regime, methodology_version),
    )
    conn.commit()
    return cur.rowcount > 0


def record_events_for_observation(conn, curr_row: dict) -> Tuple[List[str], List[str]]:
    """Detect + persist all events implied by this observation, comparing
    against the prior stored observation for the same ticker.

    Returns (detected_event_types, newly_recorded_event_types) - Phase 14
    §3.4. `detected` is every event_type the transition logic identified
    this call (useful for logging/debugging, including on a duplicate-day
    retry where they'll all already exist). `newly_recorded` is the subset
    that record_event() actually inserted (rowcount > 0) THIS call - what
    run_research_job's events_created counter must use, so a retry never
    inflates the metric for events that already existed."""
    event_types = detect_events_for_new_observation(conn, curr_row)
    newly_recorded = []
    for event_type in event_types:
        was_new = record_event(
            conn, event_date=curr_row["observation_date"], ticker=curr_row["ticker"], event_type=event_type,
            score=curr_row["score"], stage=curr_row["stage"], regime=curr_row.get("regime"),
        )
        if was_new:
            newly_recorded.append(event_type)
    return event_types, newly_recorded


LABEL_INSUFFICIENT = "INSUFFICIENT EVIDENCE"
LABEL_PRELIMINARY = "PRELIMINARY"
LABEL_FULL_REVIEW = "REQUIRES FULL STATISTICAL REVIEW"


def evidence_label(n_qualifying_events: int) -> str:
    """Phase 11 spec B16: a DISPLAY/RESEARCH label only - never
    automatically upgrades trading readiness (see Phase 10's final
    readiness classification, which this label does not touch)."""
    if n_qualifying_events < 30:
        return LABEL_INSUFFICIENT
    if n_qualifying_events < 100:
        return LABEL_PRELIMINARY
    return LABEL_FULL_REVIEW


def load_events(conn, ticker: Optional[str] = None) -> pd.DataFrame:
    ensure_schema(conn)
    query = f"SELECT * FROM {EVENT_TABLE_NAME}"
    params = ()
    if ticker:
        query += " WHERE ticker = ?"
        params = (ticker,)
    query += " ORDER BY event_date"
    return pd.read_sql_query(query, conn, params=params)
