"""Phase 10 spec item B20: a lightweight, research-only mechanism for
collecting genuinely unseen future evidence about CONTROL/Experiment A/
Experiment B entry signals.

Immutability by construction, not just convention:
  - The table schema has NO forward-return/outcome columns at all - it is
    structurally impossible to record an outcome at observation-creation
    time, because there is nowhere to put it.
  - record_observation() uses INSERT ... ON CONFLICT(ticker, observation_date)
    DO NOTHING - a duplicate call for the same ticker/date is a silent no-op,
    never an overwrite. There is no update_observation() function anywhere
    in this module.
  - Realized returns are computed later by compute_realized_returns(), which
    only READS observations + price history and returns a DataFrame - it
    never writes back into research_prospective_observations.

This table lives in the same shared SQLite file as the rest of the app (the
existing research-data pattern - see strategy_lab/data.py's docstring) but
in its own dedicated table, created here, never referenced by
db/schema.py's init_db() or any trading/alerts code - see
tests/test_strategy_lab.py's structural isolation tests.

NOT wired into any scheduler/LaunchAgent - see strategy_lab/run_prospective_observation.py
for the CLI entry point that WOULD be scheduled, and the Phase 10 final
report for the exact (unexecuted) activation step this requires.
"""
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from backtest.scoring import compute_score_series
from db.price_repository import load_price_history
from indicators.technical import MIN_REQUIRED_ROWS, enrich_with_indicators
from research.config import DEFAULT_REGIME_CONFIG
from signals.engine import score_indicators
from indicators.technical import compute_indicators_for_ticker
from strategy_lab.data import RESEARCH_SOURCE
from strategy_lab.phase10_experiments import ALL_VARIANTS, BULLISH_LABEL
from strategy_lab.regime_history import (
    classify_regime_freshness,
    compute_historical_regime_series,
    regime_label_and_date_as_of,
)

TABLE_NAME = "research_prospective_observations"

# Bumped only if the observation-construction methodology itself changes
# (e.g. a different indicator snapshot, a different regime definition).
# Stored on every row (Phase 11 spec B1/B2) so a methodology change is
# always visible in the data rather than silently blending two eras of
# observations together, and so no future rewrite can misrepresent an old
# observation as having been produced by a newer methodology.
METHODOLOGY_VERSION = "phase11-v1"

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    score REAL,
    stage TEXT,
    regime TEXT,
    control_entry_signal INTEGER,
    experiment_a_entry_signal INTEGER,
    experiment_b_entry_signal INTEGER,
    adx REAL,
    rsi REAL,
    macd REAL,
    macd_signal REAL,
    volume_ratio REAL,
    close REAL,
    source TEXT,
    methodology_version TEXT,
    UNIQUE(ticker, observation_date)
)
"""


def ensure_schema(conn) -> None:
    conn.execute(_CREATE_TABLE_SQL)
    # Additive migration for a DB created before `source`/`methodology_version`
    # existed (see db/schema.py's _migrate_pre_phase5_alerts_table for the
    # same pattern) - ADD COLUMN, never DROP/recreate, so no existing
    # immutable observation row is ever touched.
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()}
    if "source" not in cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN source TEXT")
    if "methodology_version" not in cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN methodology_version TEXT")
    # Phase 15 §1.1: additive migration for a DB created before
    # `config_fingerprint` existed - same ADD COLUMN pattern, no existing
    # row is ever touched.
    if "config_fingerprint" not in cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN config_fingerprint TEXT")
    # Phase 17 §2.1 (Area B): additive migration for regime-benchmark
    # provenance/freshness columns. No existing row is ever touched.
    if "regime_benchmark" not in cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN regime_benchmark TEXT")
    if "regime_benchmark_source" not in cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN regime_benchmark_source TEXT")
    if "regime_benchmark_data_date" not in cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN regime_benchmark_data_date TEXT")
    if "regime_freshness_status" not in cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN regime_freshness_status TEXT")
    # Phase 17 §3.1 (Area C): additive migration for exit-signal columns.
    # No existing row is ever touched.
    if "control_exit_signal" not in cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN control_exit_signal INTEGER")
    if "experiment_a_exit_signal" not in cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN experiment_a_exit_signal INTEGER")
    if "experiment_b_exit_technical_signal" not in cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN experiment_b_exit_technical_signal INTEGER")
    if "experiment_b_exit_regime_loss_signal" not in cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN experiment_b_exit_regime_loss_signal INTEGER")
    conn.commit()


def record_observation(
    conn, *, observation_date: str, ticker: str, score: Optional[float], stage: Optional[str],
    regime: Optional[str], control_entry_signal: bool, experiment_a_entry_signal: bool,
    experiment_b_entry_signal: bool, adx: Optional[float], rsi: Optional[float], macd: Optional[float],
    macd_signal: Optional[float], volume_ratio: Optional[float], close: Optional[float],
    source: Optional[str] = None, methodology_version: str = METHODOLOGY_VERSION,
    config_fingerprint: Optional[str] = None,
    regime_benchmark: Optional[str] = None,
    regime_benchmark_source: Optional[str] = None,
    regime_benchmark_data_date: Optional[str] = None,
    regime_freshness_status: Optional[str] = None,
    control_exit_signal: Optional[bool] = None,
    experiment_a_exit_signal: Optional[bool] = None,
    experiment_b_exit_technical_signal: Optional[bool] = None,
    experiment_b_exit_regime_loss_signal: Optional[bool] = None,
) -> bool:
    """Insert-only. A pre-existing (ticker, observation_date) row is left
    completely untouched (DO NOTHING) - this function can never overwrite an
    observation once created, which is what makes it a genuine prospective
    record rather than something that could be quietly rewritten after the
    outcome is known.

    `config_fingerprint` (Phase 15 §1.1): defaults to None - a caller that
    doesn't pass it gets NULL, never a guessed value.

    `regime_benchmark`/`regime_benchmark_source`/`regime_benchmark_data_date`/
    `regime_freshness_status` (Phase 17 §2.1, Area B) and
    `control_exit_signal`/`experiment_a_exit_signal`/
    `experiment_b_exit_technical_signal`/`experiment_b_exit_regime_loss_signal`
    (Phase 17 §3.1, Area C): all default to None - a caller that doesn't
    pass them gets NULL, never a guessed value."""
    ensure_schema(conn)
    cur = conn.cursor()
    cur.execute(
        f"""
        INSERT INTO {TABLE_NAME} (
            created_at, observation_date, ticker, score, stage, regime,
            control_entry_signal, experiment_a_entry_signal, experiment_b_entry_signal,
            adx, rsi, macd, macd_signal, volume_ratio, close, source, methodology_version,
            config_fingerprint, regime_benchmark, regime_benchmark_source, regime_benchmark_data_date,
            regime_freshness_status, control_exit_signal, experiment_a_exit_signal,
            experiment_b_exit_technical_signal, experiment_b_exit_regime_loss_signal
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker, observation_date) DO NOTHING
        """,
        (
            datetime.now(timezone.utc).isoformat(), observation_date, ticker, score, stage, regime,
            int(control_entry_signal), int(experiment_a_entry_signal), int(experiment_b_entry_signal),
            adx, rsi, macd, macd_signal, volume_ratio, close, source, methodology_version,
            config_fingerprint, regime_benchmark, regime_benchmark_source, regime_benchmark_data_date,
            regime_freshness_status,
            int(control_exit_signal) if control_exit_signal is not None else None,
            int(experiment_a_exit_signal) if experiment_a_exit_signal is not None else None,
            int(experiment_b_exit_technical_signal) if experiment_b_exit_technical_signal is not None else None,
            int(experiment_b_exit_regime_loss_signal) if experiment_b_exit_regime_loss_signal is not None else None,
        ),
    )
    conn.commit()
    return cur.rowcount > 0  # True if a new row was inserted, False if it was a duplicate (deduped)


def load_observations(conn, ticker: Optional[str] = None) -> pd.DataFrame:
    ensure_schema(conn)
    if ticker:
        return pd.read_sql_query(f"SELECT * FROM {TABLE_NAME} WHERE ticker = ? ORDER BY observation_date", conn, params=(ticker,))
    return pd.read_sql_query(f"SELECT * FROM {TABLE_NAME} ORDER BY observation_date", conn)


def build_todays_observation(
    conn, ticker: str, as_of_date: Optional[str] = None, config_fingerprint: Optional[str] = None,
) -> Optional[dict]:
    """Computes (but does not record) one ticker's current signal/regime
    state, using only the production scoring engine (signals.engine.score_indicators)
    and the point-in-time regime series - the same computation the live
    system would see, never a forward-looking one."""
    indicators = compute_indicators_for_ticker(conn, ticker)
    if not indicators.ok:
        return None
    score = score_indicators(indicators)

    regime_series = compute_historical_regime_series(conn)
    regime_label, regime_benchmark_data_date = regime_label_and_date_as_of(regime_series, as_of_date)
    observation_date_value = as_of_date or indicators.latest_date
    regime_freshness_status = classify_regime_freshness(observation_date_value, regime_benchmark_data_date)

    from backtest.config import DEFAULT_RULES
    from backtest.scoring import STAGE_ORDER
    entry_qualifies = STAGE_ORDER[score.highest_confirmed_stage] >= STAGE_ORDER[DEFAULT_RULES.entry_min_stage] and score.score >= DEFAULT_RULES.entry_min_score
    is_bullish = regime_label == BULLISH_LABEL

    # Phase 17 §3.1 (Area C): the same "technical exit" computation
    # trading.signals_bridge.exit_qualifies uses, hand-duplicated inline
    # (§0.3 decision) rather than imported - mirrors the existing
    # entry_qualifies inline pattern immediately above.
    technical_exit_qualifies = (
        STAGE_ORDER[score.highest_confirmed_stage] < STAGE_ORDER[DEFAULT_RULES.exit_stage_floor]
        or score.score <= DEFAULT_RULES.exit_max_score
    )
    # "regime loss" is a KNOWN condition, not "unknown regime" - a None
    # regime_label (Area A/B stale-or-missing case) must NEVER be reported as
    # a regime-loss exit signal, since we do not actually know the regime left
    # bullish_trend when we don't know the regime at all. Fabricating a
    # regime-loss claim off a data gap would misattribute an EXPERIMENT_B exit
    # event to a condition that was never actually observed.
    experiment_b_regime_loss = regime_label is not None and regime_label != BULLISH_LABEL

    from db.price_repository import resolve_source

    return {
        "observation_date": observation_date_value,
        "ticker": ticker,
        "score": score.score,
        "stage": score.highest_confirmed_stage,
        "regime": regime_label,
        "control_entry_signal": entry_qualifies,
        "experiment_a_entry_signal": entry_qualifies and is_bullish,
        "experiment_b_entry_signal": entry_qualifies and is_bullish,  # same entry rule as A; B differs only on exit
        "adx": indicators.adx, "rsi": indicators.rsi, "macd": indicators.macd,
        "macd_signal": indicators.macd_signal, "volume_ratio": indicators.volume_ratio, "close": indicators.close,
        "source": resolve_source(conn, ticker),
        "methodology_version": METHODOLOGY_VERSION,
        "config_fingerprint": config_fingerprint,
        "regime_benchmark": DEFAULT_REGIME_CONFIG.primary_benchmark,
        "regime_benchmark_source": RESEARCH_SOURCE,
        "regime_benchmark_data_date": regime_benchmark_data_date,
        "regime_freshness_status": regime_freshness_status,
        "control_exit_signal": technical_exit_qualifies,
        "experiment_a_exit_signal": technical_exit_qualifies,       # identical rule to CONTROL - phase10_experiments.py: A's exit is "original frozen exit only", same as CONTROL
        "experiment_b_exit_technical_signal": technical_exit_qualifies,  # same shared technical rule
        "experiment_b_exit_regime_loss_signal": experiment_b_regime_loss,
    }


def compute_realized_returns(conn, horizons=(1, 5, 20, 60), source: str = RESEARCH_SOURCE) -> pd.DataFrame:
    """READ-ONLY: joins immutable observations against price history to
    compute realized forward returns for observations old enough to have
    them. Never writes back into research_prospective_observations - the
    result is returned to the caller (e.g. for display or a separate,
    explicitly-named results table), never persisted into the immutable
    table itself."""
    observations = load_observations(conn)
    if observations.empty:
        return pd.DataFrame()

    rows = []
    for ticker, group in observations.groupby("ticker"):
        price_df = load_price_history(conn, ticker, source=source).sort_values("date").reset_index(drop=True)
        if price_df.empty:
            continue
        date_index = {d: i for i, d in enumerate(price_df["date"])}
        for _, obs in group.iterrows():
            idx = date_index.get(obs["observation_date"])
            if idx is None:
                continue
            row = obs.to_dict()
            for h in horizons:
                exit_idx = idx + h
                row[f"realized_return_{h}d"] = (
                    float(price_df.at[exit_idx, "close"] / price_df.at[idx, "close"] - 1.0) if exit_idx < len(price_df) else None
                )
            rows.append(row)
    return pd.DataFrame(rows)
