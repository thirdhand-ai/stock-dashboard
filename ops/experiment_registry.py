"""Phase 12 Component D: experiment governance registry.

Immutable methodology metadata: a real change to hypothesis, methodology
version, or the frozen production config fingerprint always requires a NEW
experiment_id - this table can never be edited in place for those fields.
`status` (plus `superseded_by`) is the only mutable state on an existing
row, and every status change is recorded first, unconditionally, in an
append-only history table before the row itself is updated - the audit
trail cannot be skipped by a caller.
"""
import hashlib
import json
from typing import Dict, Optional

import pandas as pd

STATUS_ACTIVE = "ACTIVE"
STATUS_SUPERSEDED = "SUPERSEDED"
STATUS_RETIRED = "RETIRED"

TABLE_NAME = "experiment_registry"
HISTORY_TABLE_NAME = "experiment_registry_status_history"

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL UNIQUE,
    methodology_version TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    superseded_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    notes TEXT
)
"""

_CREATE_HISTORY_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {HISTORY_TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (datetime('now')),
    reason TEXT NOT NULL
)
"""


class ExperimentImmutabilityError(Exception):
    """Raised when register_experiment() is called with an existing
    experiment_id but a hypothesis/methodology_version/config_fingerprint
    that differs from what's already stored."""


def ensure_schema(conn) -> None:
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(_CREATE_HISTORY_TABLE_SQL)
    conn.commit()


def compute_config_fingerprint() -> str:
    """sha256 of the existing Phase 10 production-config fingerprint
    mechanism (strategy_lab.production_guard.compute_fingerprint) - reused
    verbatim, never re-implemented. `computed_at` (a per-call timestamp,
    not part of the actual configuration) is excluded before hashing,
    exactly as production_guard's own diff_against_saved() already ignores
    it when comparing two fingerprints - without this, a fresh call here
    would never be deterministic given unchanged config, defeating both
    register_experiment()'s idempotent-no-op check and
    check_active_experiments_config_drift()'s drift detection."""
    from strategy_lab.production_guard import compute_fingerprint

    fingerprint = compute_fingerprint()
    stable = {k: v for k, v in fingerprint.items() if k != "computed_at"}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def get_experiment(conn, experiment_id: str) -> Optional[dict]:
    ensure_schema(conn)
    row = conn.execute(
        f"SELECT * FROM {TABLE_NAME} WHERE experiment_id = ?", (experiment_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def register_experiment(
    conn, *, experiment_id: str, methodology_version: str, hypothesis: str,
    config_fingerprint: str, status: str = STATUS_ACTIVE, notes: Optional[str] = None,
) -> bool:
    """Returns True if a new row was inserted. Returns False (no-op) if
    experiment_id already exists AND methodology_version, hypothesis, and
    config_fingerprint are all byte-identical to the stored row. Raises
    ExperimentImmutabilityError if experiment_id already exists and ANY of
    those three fields differ. `notes` is explicitly NOT a frozen field."""
    ensure_schema(conn)
    existing = get_experiment(conn, experiment_id)
    if existing is not None:
        if (
            existing["methodology_version"] == methodology_version
            and existing["hypothesis"] == hypothesis
            and existing["config_fingerprint"] == config_fingerprint
        ):
            return False
        raise ExperimentImmutabilityError(
            f"experiment_id={experiment_id!r} already exists with a different "
            "hypothesis/methodology_version/config_fingerprint - a real methodology "
            "change must register under a NEW experiment_id, never overwrite an "
            "existing one"
        )

    conn.execute(
        f"""
        INSERT INTO {TABLE_NAME}
            (experiment_id, methodology_version, hypothesis, config_fingerprint, status, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (experiment_id, methodology_version, hypothesis, config_fingerprint, status, notes),
    )
    conn.commit()
    return True


def update_status(conn, experiment_id: str, new_status: str, reason: str) -> None:
    """The ONLY mutable field on an existing row (besides superseded_by).
    Appends a row to experiment_registry_status_history first,
    unconditionally, before the UPDATE."""
    ensure_schema(conn)
    existing = get_experiment(conn, experiment_id)
    if existing is None:
        raise ValueError(f"unknown experiment_id: {experiment_id!r}")

    old_status = existing["status"]
    conn.execute(
        f"""
        INSERT INTO {HISTORY_TABLE_NAME} (experiment_id, old_status, new_status, reason)
        VALUES (?, ?, ?, ?)
        """,
        (experiment_id, old_status, new_status, reason),
    )
    conn.execute(
        f"UPDATE {TABLE_NAME} SET status = ? WHERE experiment_id = ?",
        (new_status, experiment_id),
    )
    conn.commit()


def mark_superseded_by(conn, experiment_id: str, superseded_by: str, reason: str) -> None:
    """Requires superseded_by to already exist in experiment_registry
    (raises ValueError before touching either row if not). Calls
    update_status(..., STATUS_SUPERSEDED, reason), then sets the
    superseded_by column via a similarly existence-guarded UPDATE."""
    ensure_schema(conn)
    target = get_experiment(conn, superseded_by)
    if target is None:
        raise ValueError(
            f"mark_superseded_by target {superseded_by!r} does not exist in {TABLE_NAME}"
        )

    update_status(conn, experiment_id, STATUS_SUPERSEDED, reason)
    conn.execute(
        f"UPDATE {TABLE_NAME} SET superseded_by = ? WHERE experiment_id = ?",
        (superseded_by, experiment_id),
    )
    conn.commit()


def list_experiments(conn) -> pd.DataFrame:
    ensure_schema(conn)
    return pd.read_sql_query(f"SELECT * FROM {TABLE_NAME} ORDER BY created_at", conn)


def load_status_history(conn, experiment_id: Optional[str] = None) -> pd.DataFrame:
    ensure_schema(conn)
    if experiment_id:
        return pd.read_sql_query(
            f"SELECT * FROM {HISTORY_TABLE_NAME} WHERE experiment_id = ? ORDER BY changed_at",
            conn, params=(experiment_id,),
        )
    return pd.read_sql_query(f"SELECT * FROM {HISTORY_TABLE_NAME} ORDER BY changed_at", conn)


def check_active_experiments_config_drift(conn) -> Dict[str, dict]:
    """Read-only drift detector: for every experiment_id with status=ACTIVE,
    compares its STORED config_fingerprint against a freshly computed
    compute_config_fingerprint(). Never writes anything - a drift is purely
    reported, never auto-corrected and never auto-retires the experiment."""
    ensure_schema(conn)
    current = compute_config_fingerprint()
    rows = conn.execute(
        f"SELECT experiment_id, config_fingerprint FROM {TABLE_NAME} WHERE status = ?",
        (STATUS_ACTIVE,),
    ).fetchall()
    return {
        row["experiment_id"]: {
            "stored": row["config_fingerprint"],
            "current": current,
            "drifted": row["config_fingerprint"] != current,
        }
        for row in rows
    }
