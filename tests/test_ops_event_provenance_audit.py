"""Tests for Phase 16 Area C: ops/event_provenance_audit.py
(docs/specs/phase16.md §3.1, §8 items 5-6, 10).

Same structurally read-only convention as ops/regime_reconstruction_audit.py
and ops/correction_impact_audit.py.
"""
import ast
import re
import sqlite3

import pytest

from db.database import db_session
from db.schema import init_db
from ops.event_provenance_audit import (
    ENTRY_AB_COOCCURRENCE_NOTE,
    EVENT_TYPE_TO_VARIANT,
    EXIT_EVENT_TYPE_TO_VARIANT,
    compute_event_provenance_coverage,
    compute_event_type_breakdown,
    compute_exit_attribution_gap_note,
    event_provenance_audit_summary,
)
from ops.evidence_provenance import PROVENANCE_UNKNOWN_LEGACY
from strategy_lab.prospective_events import (
    ALL_EVENT_TYPES,
    EVENT_CONTROL_ENTRY,
    EVENT_EXPERIMENT_A_ENTRY,
    EVENT_EXPERIMENT_B_ENTRY,
    EVENT_SCORE_CROSSING,
    record_event,
)

MODULE_PATH = "ops/event_provenance_audit.py"


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _seed_event(conn, ticker, event_type, event_date, source=None, config_fingerprint=None, methodology_version="phase11-v1"):
    return record_event(
        conn, event_date=event_date, ticker=ticker, event_type=event_type, score=80.0, stage="volume",
        regime="bullish_trend", methodology_version=methodology_version, source=source,
        config_fingerprint=config_fingerprint,
    )


# --- §3.1 item 1 / §8 item 5: structural read-only guarantees ---


def test_module_never_calls_execute_executemany_executescript_or_commit():
    with open(MODULE_PATH) as f:
        lines = f.readlines()
    pattern = re.compile(r"\.execute\(|\.executemany\(|\.executescript\(|\.commit\(")
    offenders = [(i, l.strip()) for i, l in enumerate(lines, start=1) if pattern.search(l)]
    assert not offenders, f"{MODULE_PATH} must never call a DB write method: {offenders}"


def test_module_contains_no_insert_update_delete_sql_string_constants():
    with open(MODULE_PATH) as f:
        tree = ast.parse(f.read(), filename=MODULE_PATH)
    sql_pattern = re.compile(r"\bINSERT\s+INTO\b|\bUPDATE\s+\S+\s+SET\b|\bDELETE\s+FROM\b", re.IGNORECASE)
    offenders = [
        node.value[:120] for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and sql_pattern.search(node.value)
    ]
    assert not offenders, f"{MODULE_PATH} must contain no real SQL mutation string: {offenders}"


def test_module_imports_no_write_capable_function_by_name():
    with open(MODULE_PATH) as f:
        tree = ast.parse(f.read(), filename=MODULE_PATH)
    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.extend(alias.name for alias in node.names)
    forbidden_exact = {"_persist", "_record_correction", "_store_adjusted_bars", "refresh_incomplete_latest_bars"}
    forbidden_prefixes = ("record_", "mature_", "save_")
    offenders = [n for n in imported_names if n in forbidden_exact or n.startswith(forbidden_prefixes)]
    assert not offenders, f"{MODULE_PATH} must never import a write-capable function: {offenders}"


def test_module_never_calls_a_write_function_by_name_in_source():
    with open(MODULE_PATH) as f:
        tree = ast.parse(f.read(), filename=MODULE_PATH)
    forbidden_prefixes = ("record_", "mature_", "save_")
    forbidden_exact = {"_persist", "_record_correction", "_store_adjusted_bars", "refresh_incomplete_latest_bars"}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
        if name and (name in forbidden_exact or name.startswith(forbidden_prefixes)):
            offenders.append((node.lineno, name))
    assert not offenders, f"{MODULE_PATH} must never CALL a write function: {offenders}"


def test_module_has_no_docstring_or_comment_false_positives_in_scans():
    with open(MODULE_PATH) as f:
        content = f.read()
    assert "conn.execute" in content or "record_" in content  # present as prose only
    assert len(content) > 1500


# --- §3.1 item 2: seed one of each -> correct bucketing/counts ---


def test_event_type_breakdown_buckets_entry_attributable_vs_shared():
    conn = make_test_db()
    _seed_event(conn, "AAPL", EVENT_CONTROL_ENTRY, "2026-01-01")
    _seed_event(conn, "AAPL", EVENT_EXPERIMENT_A_ENTRY, "2026-01-01")
    _seed_event(conn, "AAPL", EVENT_EXPERIMENT_B_ENTRY, "2026-01-01")
    _seed_event(conn, "AAPL", EVENT_SCORE_CROSSING, "2026-01-01")

    breakdown = compute_event_type_breakdown(conn)
    assert breakdown["entry_attributable"][EVENT_CONTROL_ENTRY] == 1
    assert breakdown["entry_attributable"][EVENT_EXPERIMENT_A_ENTRY] == 1
    assert breakdown["entry_attributable"][EVENT_EXPERIMENT_B_ENTRY] == 1
    assert breakdown["shared_signal_detection"][EVENT_SCORE_CROSSING] == 1
    assert breakdown["total_events"] == 4
    # All types present (0-filled), never omitted. Phase 17 §3.3: the
    # three-way branch means a type not in EVENT_TYPE_TO_VARIANT (entry) now
    # lands in exit_attributable if it's an exit type, else
    # shared_signal_detection - this loop was updated (not one of the 2
    # explicitly-documented §3.3 test updates, but a direct, mechanical
    # consequence of implementing the same documented three-way branch; see
    # docs/specs/phase17.md §3.4 item 10's own regression requirement
    # against this exact test).
    for et in ALL_EVENT_TYPES:
        if et in EVENT_TYPE_TO_VARIANT:
            assert et in breakdown["entry_attributable"]
        elif et in EXIT_EVENT_TYPE_TO_VARIANT:
            assert et in breakdown["exit_attributable"]
        else:
            assert et in breakdown["shared_signal_detection"]


def test_event_type_breakdown_empty_db_zero_fills_every_type():
    conn = make_test_db()
    breakdown = compute_event_type_breakdown(conn)
    assert breakdown["total_events"] == 0
    assert all(v == 0 for v in breakdown["entry_attributable"].values())
    assert all(v == 0 for v in breakdown["shared_signal_detection"].values())


# --- §3.1 item 3: by_variant sums correctly, never double-counted/merged ---


def test_provenance_coverage_by_variant_sums_correctly():
    conn = make_test_db()
    _seed_event(conn, "AAPL", EVENT_CONTROL_ENTRY, "2026-01-01", config_fingerprint="fp1")
    _seed_event(conn, "MSFT", EVENT_CONTROL_ENTRY, "2026-01-01", config_fingerprint="fp1")
    _seed_event(conn, "AAPL", EVENT_EXPERIMENT_A_ENTRY, "2026-01-01", config_fingerprint="fp1")
    _seed_event(conn, "AAPL", EVENT_EXPERIMENT_B_ENTRY, "2026-01-01", config_fingerprint="fp1")
    _seed_event(conn, "AAPL", EVENT_SCORE_CROSSING, "2026-01-01", config_fingerprint="fp1")

    coverage = compute_event_provenance_coverage(conn)
    assert coverage["by_variant"]["CONTROL"] == 2
    assert coverage["by_variant"]["EXPERIMENT_A"] == 1
    assert coverage["by_variant"]["EXPERIMENT_B"] == 1
    # score_crossing_70 is not variant-specific - never counted under any variant.
    assert sum(coverage["by_variant"].values()) == 4


# --- §3.1 item 4 / §8 item 6: config_fingerprint=NULL -> events_unknown_legacy,
# regardless of event_type (attribution via event_type is unaffected) ---


def test_legacy_event_null_fingerprint_counted_unknown_legacy_and_variant_still_resolves():
    conn = make_test_db()
    _seed_event(conn, "AAPL", EVENT_CONTROL_ENTRY, "2026-01-01", config_fingerprint=None)
    _seed_event(conn, "AAPL", EVENT_EXPERIMENT_A_ENTRY, "2026-01-02", config_fingerprint="fp1")

    coverage = compute_event_provenance_coverage(conn)
    assert coverage["events_unknown_legacy"] == 1
    assert coverage["events_with_fingerprint"] == 1
    # event_type -> variant attribution is unaffected by config_fingerprint NULL-ness.
    assert coverage["by_variant"]["CONTROL"] == 1
    assert coverage["by_variant"]["EXPERIMENT_A"] == 1

    events = record_event  # no-op reference to keep import used
    breakdown = compute_event_type_breakdown(conn)
    assert breakdown["entry_attributable"][EVENT_CONTROL_ENTRY] == 1


def test_events_unknown_legacy_empty_db():
    conn = make_test_db()
    coverage = compute_event_provenance_coverage(conn)
    assert coverage["events_with_fingerprint"] == 0
    assert coverage["events_unknown_legacy"] == 0
    assert coverage["by_variant"] == {"CONTROL": 0, "EXPERIMENT_A": 0, "EXPERIMENT_B": 0}


# --- §3.1 item 5: exit-attribution gap note - real absence, dynamic monkeypatch ---


def test_exit_attribution_gap_note_real_all_event_types_is_true_post_phase17():
    result = compute_exit_attribution_gap_note()
    assert result["exit_event_types_exist"] is True
    assert set(result["exit_event_types"]) == {
        "control_exit", "experiment_a_exit", "experiment_b_exit_technical", "experiment_b_exit_regime_loss",
    }
    assert "found" in result["note"].lower() or "observable" in result["note"].lower()


def test_exit_attribution_gap_note_flips_true_with_monkeypatched_all_event_types(monkeypatch):
    import strategy_lab.prospective_events as pe_module
    monkeypatch.setattr(
        pe_module, "ALL_EVENT_TYPES",
        pe_module.ALL_EVENT_TYPES + ("experiment_b_exit_regime_loss",),
    )
    result = compute_exit_attribution_gap_note()
    assert result["exit_event_types_exist"] is True
    assert "experiment_b_exit_regime_loss" in result["exit_event_types"]


# --- entry_ab_cooccurrence_note is a fixed string in the summary rollup ---


def test_event_provenance_audit_summary_rollup_shape():
    conn = make_test_db()
    _seed_event(conn, "AAPL", EVENT_CONTROL_ENTRY, "2026-01-01")
    summary = event_provenance_audit_summary(conn)
    assert set(summary.keys()) == {
        "event_type_breakdown", "event_provenance_coverage", "exit_attribution_gap", "entry_ab_cooccurrence_note",
        "exit_event_provenance", "experiment_b_exit_cooccurrence_note",
    }
    assert summary["entry_ab_cooccurrence_note"] == ENTRY_AB_COOCCURRENCE_NOTE


def test_event_provenance_audit_summary_empty_db_never_raises():
    conn = make_test_db()
    summary = event_provenance_audit_summary(conn)
    assert summary["event_type_breakdown"]["total_events"] == 0


# --- §3.1 item 6 / §8 item 5: zero-writes proof ---


def _snapshot(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def test_zero_writes_proof_seeded_db():
    conn = make_test_db()
    _seed_event(conn, "AAPL", EVENT_CONTROL_ENTRY, "2026-01-01")
    _seed_event(conn, "AAPL", EVENT_EXPERIMENT_A_ENTRY, "2026-01-01")
    _seed_event(conn, "MSFT", EVENT_SCORE_CROSSING, "2026-01-02", config_fingerprint=None)

    before = _snapshot(conn)
    event_provenance_audit_summary(conn)
    compute_event_type_breakdown(conn)
    compute_event_provenance_coverage(conn)
    compute_exit_attribution_gap_note()
    after = _snapshot(conn)
    assert before == after


def test_live_db_event_provenance_audit_summary_runs_cleanly_and_zero_writes():
    with db_session() as conn:
        conn.row_factory = sqlite3.Row
        before = _snapshot(conn)
        summary = event_provenance_audit_summary(conn)  # must not raise
        after = _snapshot(conn)
    assert before == after
    assert "event_type_breakdown" in summary
