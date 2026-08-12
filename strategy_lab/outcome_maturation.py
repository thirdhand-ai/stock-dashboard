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


def _compute_one(conn, obs_row, horizon_days: int, today: date, price_cache: dict) -> MaturationOutcome:
    obs_date = date.fromisoformat(obs_row["observation_date"])
    elapsed = trading_sessions_elapsed(obs_date, today)

    if elapsed < horizon_days:
        return MaturationOutcome(
            observation_id=int(obs_row["id"]), ticker=obs_row["ticker"],
            observation_date=obs_row["observation_date"], horizon_days=horizon_days, status=STATUS_PENDING,
        )

    ticker = obs_row["ticker"]
    source = obs_row["source"] if "source" in obs_row and obs_row["source"] else None
    cache_key = (ticker, source)
    if cache_key not in price_cache:
        price_cache[cache_key] = load_price_history(conn, ticker, source=source).sort_values("date").reset_index(drop=True)
    price_df = price_cache[cache_key]

    if price_df.empty:
        return MaturationOutcome(
            observation_id=int(obs_row["id"]), ticker=ticker, observation_date=obs_row["observation_date"],
            horizon_days=horizon_days, status=STATUS_UNAVAILABLE,
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
        )

    entry_price = float(price_df.at[idx, "close"])
    exit_price = float(price_df.at[exit_idx, "close"])
    realized_return = exit_price / entry_price - 1.0 if entry_price else None

    return MaturationOutcome(
        observation_id=int(obs_row["id"]), ticker=ticker, observation_date=obs_row["observation_date"],
        horizon_days=horizon_days, status=STATUS_MATURED,
        realized_return=realized_return, exit_date=str(price_df.at[exit_idx, "date"]),
    )


def _persist(conn, outcome: MaturationOutcome) -> None:
    """Upsert into the SEPARATE outcomes table only - never touches
    research_prospective_observations. A pending outcome can be revisited
    (re-upserted) as time passes and it matures; a matured outcome, once
    computed from immutable price history, is stable and re-upserting it
    is idempotent (same inputs, same output)."""
    conn.execute(
        f"""
        INSERT INTO {OUTCOME_TABLE_NAME} (observation_id, ticker, observation_date, horizon_days, status, realized_return, exit_date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(observation_id, horizon_days) DO UPDATE SET
            status=excluded.status, realized_return=excluded.realized_return,
            exit_date=excluded.exit_date, matured_at=datetime('now')
        """,
        (outcome.observation_id, outcome.ticker, outcome.observation_date, outcome.horizon_days,
         outcome.status, outcome.realized_return, outcome.exit_date),
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
