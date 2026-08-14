"""Phase 11 spec B3: a separate, read-only-with-respect-to-predictions
outcome maturation process for prospective observations
(strategy_lab/prospective.py).

The original prediction row in research_prospective_observations is NEVER
edited - maturation results are written to a SEPARATE table,
research_prospective_outcomes, keyed by (observation_id, horizon_days).
This is a genuinely distinct table+writer, not a convention layered onto
the same row, so "never edit the prediction row" is structurally true, not
just documented.

Status is computed from real elapsed NYSE trading sessions (see
automation/trading_calendar.py's trading_sessions_elapsed()), never from
calendar-day arithmetic and never from "does price data happen to already
exist that far forward" - a horizon is only ever matured once it has
genuinely, chronologically elapsed as of `today`.
"""
from dataclasses import dataclass
from datetime import date
from typing import Optional, Tuple

import pandas as pd

from automation.trading_calendar import trading_sessions_elapsed
from db.price_repository import load_price_history
from strategy_lab.prospective import load_observations

OUTCOME_TABLE_NAME = "research_prospective_outcomes"
HORIZONS: Tuple[int, ...] = (1, 5, 20, 60)

STATUS_PENDING = "pending"
STATUS_MATURED = "matured"
STATUS_UNAVAILABLE = "unavailable"

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {OUTCOME_TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    status TEXT NOT NULL,
    realized_return REAL,
    exit_date TEXT,
    matured_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(observation_id, horizon_days)
)
"""


def ensure_schema(conn) -> None:
    conn.execute(_CREATE_TABLE_SQL)
    # Phase 15 §1.3: additive migration for `source`/`methodology_version`/
    # `config_fingerprint` - denormalized copies from the parent observation,
    # matching the existing `ticker`/`observation_date` convention on this
    # same table. Mirrors the exact PRAGMA table_info + guarded ALTER TABLE
    # pattern used elsewhere.
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({OUTCOME_TABLE_NAME})").fetchall()}
    if "source" not in cols:
        conn.execute(f"ALTER TABLE {OUTCOME_TABLE_NAME} ADD COLUMN source TEXT")
    if "methodology_version" not in cols:
        conn.execute(f"ALTER TABLE {OUTCOME_TABLE_NAME} ADD COLUMN methodology_version TEXT")
    if "config_fingerprint" not in cols:
        conn.execute(f"ALTER TABLE {OUTCOME_TABLE_NAME} ADD COLUMN config_fingerprint TEXT")
    conn.commit()


@dataclass
class MaturationOutcome:
    observation_id: int
    ticker: str
    observation_date: str
    horizon_days: int
    status: str
    realized_return: Optional[float] = None
    exit_date: Optional[str] = None
    source: Optional[str] = None
    methodology_version: Optional[str] = None
    config_fingerprint: Optional[str] = None


def _compute_one(conn, obs_row, horizon_days: int, today: date, price_cache: dict) -> MaturationOutcome:
    obs_date = date.fromisoformat(obs_row["observation_date"])
    elapsed = trading_sessions_elapsed(obs_date, today)

    # Phase 15 §1.3: denormalized provenance, copied verbatim from the
    # observation row's OWN stored values - never recomputed at maturation
    # time. A legacy observation (source/methodology_version/
    # config_fingerprint all NULL, e.g. the real id=1 AAPL row) simply
    # propagates NULL onto every outcome it matures, never fabricated.
    prov_source = obs_row["source"] if "source" in obs_row and obs_row["source"] else None
    prov_methodology_version = (
        obs_row["methodology_version"] if "methodology_version" in obs_row and obs_row["methodology_version"] else None
    )
    prov_config_fingerprint = (
        obs_row["config_fingerprint"] if "config_fingerprint" in obs_row and obs_row["config_fingerprint"] else None
    )

    if elapsed < horizon_days:
        return MaturationOutcome(
            observation_id=int(obs_row["id"]), ticker=obs_row["ticker"],
            observation_date=obs_row["observation_date"], horizon_days=horizon_days, status=STATUS_PENDING,
            source=prov_source, methodology_version=prov_methodology_version,
            config_fingerprint=prov_config_fingerprint,
        )

    ticker = obs_row["ticker"]
    source = prov_source
    cache_key = (ticker, source)
    if cache_key not in price_cache:
        price_cache[cache_key] = load_price_history(conn, ticker, source=source).sort_values("date").reset_index(drop=True)
    price_df = price_cache[cache_key]

    if price_df.empty:
        return MaturationOutcome(
            observation_id=int(obs_row["id"]), ticker=ticker, observation_date=obs_row["observation_date"],
            horizon_days=horizon_days, status=STATUS_UNAVAILABLE,
            source=prov_source, methodology_version=prov_methodology_version,
            config_fingerprint=prov_config_fingerprint,
        )

    date_index = {d: i for i, d in enumerate(price_df["date"])}
    idx = date_index.get(obs_row["observation_date"])
    exit_idx = idx + horizon_days if idx is not None else None

    if idx is None or exit_idx >= len(price_df):
        # The horizon has genuinely elapsed in real time, but this ticker's
        # stored price history doesn't reach that far (a data gap) - never
        # fabricate a return, mark unavailable rather than silently pending
        # forever.
        return MaturationOutcome(
            observation_id=int(obs_row["id"]), ticker=ticker, observation_date=obs_row["observation_date"],
            horizon_days=horizon_days, status=STATUS_UNAVAILABLE,
            source=prov_source, methodology_version=prov_methodology_version,
            config_fingerprint=prov_config_fingerprint,
        )

    entry_price = float(price_df.at[idx, "close"])
    exit_price = float(price_df.at[exit_idx, "close"])
    realized_return = exit_price / entry_price - 1.0 if entry_price else None

    return MaturationOutcome(
        observation_id=int(obs_row["id"]), ticker=ticker, observation_date=obs_row["observation_date"],
        horizon_days=horizon_days, status=STATUS_MATURED,
        realized_return=realized_return, exit_date=str(price_df.at[exit_idx, "date"]),
        source=prov_source, methodology_version=prov_methodology_version,
        config_fingerprint=prov_config_fingerprint,
    )


def compute_outcome_for_horizon(
    conn, obs_row, horizon_days: int, today: date, price_cache: Optional[dict] = None,
) -> MaturationOutcome:
    """Public alias for _compute_one - pure read+compute, no
    conn.execute/commit anywhere in this function or anything it calls
    (load_price_history is itself read-only). Exists so
    ops/correction_impact_audit.py can deterministically recompute a single
    outcome without duplicating this logic and without importing _persist or
    mature_outcomes."""
    return _compute_one(conn, obs_row, horizon_days, today, price_cache if price_cache is not None else {})


def _persist(conn, outcome: MaturationOutcome) -> None:
    """Upsert into the SEPARATE outcomes table only - never touches
    research_prospective_observations. A pending outcome can be revisited
    (re-upserted) as time passes and it matures; a matured outcome, once
    computed from immutable price history, is stable and re-upserting it
    is idempotent (same inputs, same output).

    Phase 15 §1.3: also (re-)persists `source`/`methodology_version`/
    `config_fingerprint`, always re-derived from the same immutable
    observation row on every call - safe/idempotent in DO UPDATE."""
    conn.execute(
        f"""
        INSERT INTO {OUTCOME_TABLE_NAME} (
            observation_id, ticker, observation_date, horizon_days, status, realized_return, exit_date,
            source, methodology_version, config_fingerprint
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(observation_id, horizon_days) DO UPDATE SET
            status=excluded.status, realized_return=excluded.realized_return,
            exit_date=excluded.exit_date, matured_at=datetime('now'),
            source=excluded.source, methodology_version=excluded.methodology_version,
            config_fingerprint=excluded.config_fingerprint
        """,
        (outcome.observation_id, outcome.ticker, outcome.observation_date, outcome.horizon_days,
         outcome.status, outcome.realized_return, outcome.exit_date,
         outcome.source, outcome.methodology_version, outcome.config_fingerprint),
    )


def mature_outcomes(conn, today: Optional[date] = None, horizons: Tuple[int, ...] = HORIZONS) -> pd.DataFrame:
    """Compute/refresh maturation status for every stored observation x
    horizon pair. Returns a DataFrame of what was written this call. Safe
    to call repeatedly (idempotent) - a horizon that already matured
    re-computes to the same value from the same immutable price history."""
    ensure_schema(conn)
    today = today or date.today()
    observations = load_observations(conn)
    if observations.empty:
        return pd.DataFrame()

    price_cache: dict = {}
    results = []
    for _, obs_row in observations.iterrows():
        for h in horizons:
            outcome = _compute_one(conn, obs_row, h, today, price_cache)
            _persist(conn, outcome)
            results.append(outcome)
    conn.commit()
    return pd.DataFrame([r.__dict__ for r in results])


def load_outcomes(conn, ticker: Optional[str] = None) -> pd.DataFrame:
    ensure_schema(conn)
    query = f"SELECT * FROM {OUTCOME_TABLE_NAME}"
    params = ()
    if ticker:
        query += " WHERE ticker = ?"
        params = (ticker,)
    return pd.read_sql_query(query, conn, params=params)


def maturation_summary(conn) -> dict:
    """Counts by status - used by Strategy Lab's Prospective Validation
    section (B15) and the sample-size guardrail labels (B16)."""
    outcomes = load_outcomes(conn)
    if outcomes.empty:
        return {"pending": 0, "matured": 0, "unavailable": 0, "total": 0}
    counts = outcomes["status"].value_counts().to_dict()
    return {
        "pending": int(counts.get(STATUS_PENDING, 0)),
        "matured": int(counts.get(STATUS_MATURED, 0)),
        "unavailable": int(counts.get(STATUS_UNAVAILABLE, 0)),
        "total": int(len(outcomes)),
    }
