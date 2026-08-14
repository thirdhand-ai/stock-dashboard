"""Tests for Phase 12 Component D: ops/experiment_registry.py.

Focus: immutability enforcement (a real methodology change must use a new
experiment_id, never silently overwrite an existing row's frozen fields),
the append-only status-history audit trail, and the read-only config-drift
detector. Synthetic in-memory SQLite only.
"""
import contextlib
import inspect
import sqlite3

import pytest

from db.schema import init_db
from ops import experiment_registry
from ops.experiment_registry import (
    STATUS_ACTIVE,
    STATUS_SUPERSEDED,
    TABLE_NAME,
    ExperimentImmutabilityError,
    get_experiment,
    load_status_history,
    mark_superseded_by,
    register_experiment,
    update_status,
)


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _register(conn, experiment_id="exp-1", methodology_version="v1", hypothesis="h1",
              config_fingerprint="fp1", notes=None):
    return register_experiment(
        conn, experiment_id=experiment_id, methodology_version=methodology_version,
        hypothesis=hypothesis, config_fingerprint=config_fingerprint, notes=notes,
    )


# --- register_experiment / immutability ---

def test_register_new_experiment_inserts_row():
    conn = make_test_db()
    inserted = _register(conn)
    assert inserted is True
    row = get_experiment(conn, "exp-1")
    assert row is not None
    assert row["status"] == STATUS_ACTIVE
    assert row["superseded_by"] is None


def test_register_identical_duplicate_is_idempotent_noop():
    conn = make_test_db()
    _register(conn)
    before = dict(get_experiment(conn, "exp-1"))
    inserted_again = _register(conn)
    after = dict(get_experiment(conn, "exp-1"))
    assert inserted_again is False
    assert before == after
    count = conn.execute(f"SELECT COUNT(*) as n FROM {TABLE_NAME}").fetchone()["n"]
    assert count == 1


def test_register_same_id_changed_hypothesis_raises_immutability_error():
    conn = make_test_db()
    _register(conn, hypothesis="original hypothesis")
    with pytest.raises(ExperimentImmutabilityError):
        _register(conn, hypothesis="a genuinely different hypothesis")


def test_register_same_id_changed_config_fingerprint_raises_immutability_error():
    conn = make_test_db()
    _register(conn, config_fingerprint="fp-original")
    with pytest.raises(ExperimentImmutabilityError):
        _register(conn, config_fingerprint="fp-changed")


def test_register_same_id_changed_methodology_version_raises_immutability_error():
    conn = make_test_db()
    _register(conn, methodology_version="v1")
    with pytest.raises(ExperimentImmutabilityError):
        _register(conn, methodology_version="v2")


def test_register_same_id_changed_notes_does_not_raise():
    conn = make_test_db()
    _register(conn, notes="first note")
    inserted = _register(conn, notes="a totally different note")  # notes not frozen
    assert inserted is False  # still a no-op, no exception
    count = conn.execute(f"SELECT COUNT(*) as n FROM {TABLE_NAME}").fetchone()["n"]
    assert count == 1


# --- update_status / audit trail ---

def test_update_status_appends_history_row_before_updating_status():
    conn = make_test_db()
    _register(conn)
    update_status(conn, "exp-1", STATUS_SUPERSEDED, reason="test supersede")
    history = load_status_history(conn, "exp-1")
    assert len(history) == 1
    assert history.iloc[0]["old_status"] == STATUS_ACTIVE
    assert history.iloc[0]["new_status"] == STATUS_SUPERSEDED
    assert history.iloc[0]["reason"] == "test supersede"
    row = get_experiment(conn, "exp-1")
    assert row["status"] == STATUS_SUPERSEDED


def test_update_status_never_modifies_hypothesis_methodology_or_fingerprint():
    conn = make_test_db()
    _register(conn, methodology_version="v1", hypothesis="h1", config_fingerprint="fp1")
    before = dict(get_experiment(conn, "exp-1"))
    update_status(conn, "exp-1", STATUS_SUPERSEDED, reason="x")
    after = dict(get_experiment(conn, "exp-1"))
    assert before["hypothesis"] == after["hypothesis"]
    assert before["methodology_version"] == after["methodology_version"]
    assert before["config_fingerprint"] == after["config_fingerprint"]
    assert before["status"] != after["status"]


def test_update_status_sql_source_never_contains_frozen_column_in_set_clause():
    source = inspect.getsource(experiment_registry.update_status)
    collapsed = source.replace(" ", "").replace("\n", "")
    assert "hypothesis=" not in collapsed
    assert "methodology_version=" not in collapsed
    assert "config_fingerprint=" not in collapsed


def test_update_status_unknown_experiment_id_raises_value_error():
    conn = make_test_db()
    with pytest.raises(ValueError):
        update_status(conn, "does-not-exist", STATUS_SUPERSEDED, reason="x")
    history = load_status_history(conn)
    assert history.empty


def test_mark_superseded_by_requires_target_to_exist():
    conn = make_test_db()
    _register(conn, experiment_id="exp-old")
    with pytest.raises(ValueError):
        mark_superseded_by(conn, "exp-old", "exp-new-does-not-exist", reason="x")
    row = get_experiment(conn, "exp-old")
    assert row["status"] == STATUS_ACTIVE  # untouched
    history = load_status_history(conn, "exp-old")
    assert history.empty  # no history row written either


def test_mark_superseded_by_succeeds_when_target_exists():
    conn = make_test_db()
    _register(conn, experiment_id="exp-old")
    _register(conn, experiment_id="exp-new", hypothesis="h2", config_fingerprint="fp2")
    mark_superseded_by(conn, "exp-old", "exp-new", reason="methodology revised")
    row = get_experiment(conn, "exp-old")
    assert row["status"] == STATUS_SUPERSEDED
    assert row["superseded_by"] == "exp-new"


# --- config drift detection ---

def test_check_active_experiments_config_drift_detects_changed_config(monkeypatch):
    conn = make_test_db()
    _register(conn, experiment_id="exp-1", config_fingerprint="fp-original")
    monkeypatch.setattr(experiment_registry, "compute_config_fingerprint", lambda: "fp-changed")

    drift = experiment_registry.check_active_experiments_config_drift(conn)
    assert drift["exp-1"]["stored"] == "fp-original"
    assert drift["exp-1"]["current"] == "fp-changed"
    assert drift["exp-1"]["drifted"] is True


def test_check_active_experiments_config_drift_no_drift_when_unchanged(monkeypatch):
    conn = make_test_db()
    _register(conn, experiment_id="exp-1", config_fingerprint="fp-same")
    monkeypatch.setattr(experiment_registry, "compute_config_fingerprint", lambda: "fp-same")

    drift = experiment_registry.check_active_experiments_config_drift(conn)
    assert drift["exp-1"]["drifted"] is False


def test_check_active_experiments_config_drift_is_read_only(monkeypatch):
    conn = make_test_db()
    _register(conn, experiment_id="exp-1", config_fingerprint="fp-original")
    monkeypatch.setattr(experiment_registry, "compute_config_fingerprint", lambda: "fp-changed")

    before = conn.execute(f"SELECT COUNT(*) as n FROM {TABLE_NAME}").fetchone()["n"]
    experiment_registry.check_active_experiments_config_drift(conn)
    after = conn.execute(f"SELECT COUNT(*) as n FROM {TABLE_NAME}").fetchone()["n"]
    assert before == after


def test_check_active_experiments_config_drift_empty_registry_returns_empty_dict():
    conn = make_test_db()
    assert experiment_registry.check_active_experiments_config_drift(conn) == {}


# --- seed script ---

def test_register_phase10_11_experiments_seed_script_idempotent(monkeypatch):
    conn = make_test_db()

    @contextlib.contextmanager
    def fake_db_session():
        yield conn

    import ops.register_phase10_11_experiments as seed_module
    monkeypatch.setattr(seed_module, "db_session", fake_db_session)

    seed_module.main()
    seed_module.main()  # must be safe to re-run

    count = conn.execute(f"SELECT COUNT(*) as n FROM {TABLE_NAME}").fetchone()["n"]
    assert count == 3
    for experiment_id in (
        "control-original-frozen-strategy",
        "experiment-a-bullish-entry-only",
        "experiment-b-bullish-entry-and-exit",
    ):
        row = get_experiment(conn, experiment_id)
        assert row is not None
        assert row["status"] == STATUS_ACTIVE


def test_register_phase10_11_experiments_seed_script_exits_nonzero_on_config_change(monkeypatch):
    """§6.4 edge case: if the frozen config genuinely changed under an
    unchanged experiment_id, the seed script must print the error and exit
    non-zero, NOT catch ExperimentImmutabilityError and continue."""
    conn = make_test_db()

    @contextlib.contextmanager
    def fake_db_session():
        yield conn

    import ops.register_phase10_11_experiments as seed_module
    monkeypatch.setattr(seed_module, "db_session", fake_db_session)

    seed_module.main()  # first run seeds all 3 rows under fingerprint A

    # Simulate a genuine config change: fingerprint B on the second run.
    monkeypatch.setattr(seed_module, "compute_config_fingerprint", lambda: "fp-changed-simulated")

    with pytest.raises(SystemExit) as exc_info:
        seed_module.main()
    assert exc_info.value.code != 0
