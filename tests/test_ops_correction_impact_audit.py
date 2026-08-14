"""Tests for Phase 15 Component B: ops/correction_impact_audit.py
(docs/specs/phase15.md §2, §8 items 8-15).

This is THE safety-critical module of Phase 15: it must have zero write
capability at all, not merely "shouldn't be called that way" - the Phase 14
incident (a live batch-correction command run before checking frozen-artifact
impact) is structurally prevented here because this module simply cannot
mutate anything.

Everything here is synthetic/deterministic and uses an in-memory SQLite DB -
no real network call, no order/alert of any kind.
"""
import ast
import re
import sqlite3
import tokenize
from datetime import date

import pytest

from db.schema import init_db
from ops.correction_impact_audit import (
    AffectedOutcome,
    RecomputationResult,
    audit_all_corrections,
    audit_correction,
    find_outcomes_affected_by_correction,
    load_corrections,
    recompute_outcome_deterministically,
)
from strategy_lab.cache_integrity import CORRECTIONS_TABLE
from strategy_lab.cache_integrity import ensure_schema as ensure_corrections_schema
from strategy_lab.outcome_maturation import STATUS_MATURED, STATUS_PENDING, load_outcomes, mature_outcomes
from strategy_lab.prospective import load_observations, record_observation


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _seed_price(conn, ticker, d, close, source, volume=1000):
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, d, close, close, close, close, volume, source),
    )
    conn.commit()


def _seed_observation(conn, ticker, obs_date, source=None, methodology_version=None, config_fingerprint=None):
    record_observation(
        conn, observation_date=obs_date, ticker=ticker, score=90.0, stage="volume", regime="bullish_trend",
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=100.0,
        source=source, methodology_version=methodology_version, config_fingerprint=config_fingerprint,
    )


def _seed_correction(conn, ticker, date_str, source, old_close=100.0, new_close=101.0):
    ensure_corrections_schema(conn)
    conn.execute(
        f"""
        INSERT INTO {CORRECTIONS_TABLE} (
            ticker, date, source, old_close, new_close, old_volume, new_volume,
            old_fetched_at, new_fetched_at, reason, affects_frozen_artifacts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ticker, date_str, source, old_close, new_close, 1000, 1000, None, None, "test correction", "[]"),
    )
    conn.commit()
    row = conn.execute(
        f"SELECT * FROM {CORRECTIONS_TABLE} WHERE ticker = ? AND date = ? ORDER BY id DESC LIMIT 1",
        (ticker, date_str),
    ).fetchone()
    return dict(row)


# --- #8: structural read-only guarantees ---


def test_module_never_calls_execute_executemany_executescript_or_commit():
    """No line matching \\.execute\\(|\\.executemany\\(|\\.executescript\\(|\\.commit\\( anywhere -
    checked as raw source lines (the module's own docstring only ever
    mentions these as bare identifiers, never with a following '(', so this
    cannot false-positive on prose)."""
    with open("ops/correction_impact_audit.py") as f:
        lines = f.readlines()
    pattern = re.compile(r"\.execute\(|\.executemany\(|\.executescript\(|\.commit\(")
    offenders = [(i, l.strip()) for i, l in enumerate(lines, start=1) if pattern.search(l)]
    assert not offenders, f"ops/correction_impact_audit.py must never call a DB write method: {offenders}"


def test_module_contains_no_insert_update_delete_sql_string_constants():
    """AST-based: scans actual STRING CONSTANTS (not prose/docstrings that
    merely mention the words) for real SQL statement shapes."""
    with open("ops/correction_impact_audit.py") as f:
        tree = ast.parse(f.read(), filename="ops/correction_impact_audit.py")
    sql_pattern = re.compile(r"\bINSERT\s+INTO\b|\bUPDATE\s+\S+\s+SET\b|\bDELETE\s+FROM\b", re.IGNORECASE)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if sql_pattern.search(node.value):
                offenders.append(node.value[:120])
    assert not offenders, f"ops/correction_impact_audit.py must contain no real SQL mutation string: {offenders}"


def test_module_imports_no_write_capable_function_by_name():
    with open("ops/correction_impact_audit.py") as f:
        tree = ast.parse(f.read(), filename="ops/correction_impact_audit.py")
    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.extend(alias.name for alias in node.names)

    forbidden_exact = {"_persist", "_record_correction", "_store_adjusted_bars", "refresh_incomplete_latest_bars"}
    forbidden_prefixes = ("record_", "mature_", "save_")

    offenders = [
        n for n in imported_names
        if n in forbidden_exact or n.startswith(forbidden_prefixes)
    ]
    assert not offenders, f"ops/correction_impact_audit.py must never import a write-capable function: {offenders}"


def test_module_never_calls_a_write_function_by_name_in_source():
    """Belt-and-suspenders on top of the import-name scan: an AST walk over
    every actual ast.Call node (never docstrings/comments, which are not
    Call nodes) confirms no write-shaped function name is ever CALLED
    anywhere in the module - the module's own safety docstring mentions
    "mature_outcomes()" in prose, which this AST-based scan correctly
    ignores (a naive text/regex scan over raw lines would false-positive on
    that exact prose, which is why this is AST-based, not line-based)."""
    with open("ops/correction_impact_audit.py") as f:
        tree = ast.parse(f.read(), filename="ops/correction_impact_audit.py")
    forbidden_prefixes = ("record_", "mature_", "save_")
    forbidden_exact = {"_persist", "_record_correction", "_store_adjusted_bars", "refresh_incomplete_latest_bars"}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name and (name in forbidden_exact or name.startswith(forbidden_prefixes)):
            offenders.append((node.lineno, name))
    assert not offenders, f"ops/correction_impact_audit.py must never CALL a write function: {offenders}"


def test_module_has_no_docstring_or_comment_false_positives_in_scans():
    """Sanity check on the scans above: the module DOES mention execute/
    commit/INSERT/UPDATE/DELETE in prose (its own safety docstring) - confirm
    the file is non-trivial (not accidentally empty) so a green result above
    is meaningful, not just an artifact of an empty/broken file."""
    with open("ops/correction_impact_audit.py") as f:
        content = f.read()
    assert "conn.execute" in content  # present as prose/docstring text
    assert len(content) > 2000  # a real, substantial module


# --- fixtures shared by the AAPL-shaped tests (#9-#12) ---


def test_find_outcomes_affected_aapl_shaped_legacy_source_none_source_matched_false():
    """Mirrors the REAL, CONFIRMED AAPL case (docs/specs/phase15.md §0.2):
    observation.source=NULL (legacy row) -> resolved to production 'alpaca'
    at maturation time -> a 'alpaca_adjusted'-scoped correction on the same
    (ticker, date) is a structural entry-leg match but source_matched=False."""
    conn = make_test_db()
    obs_date = "2026-08-12"
    exit_date = "2026-08-13"
    _seed_price(conn, "AAPL", obs_date, 100.0, "alpaca")
    _seed_price(conn, "AAPL", exit_date, 101.0, "alpaca")
    _seed_observation(conn, "AAPL", obs_date, source=None)  # legacy shape - matches real id=1 row

    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))

    corr_row = _seed_correction(conn, "AAPL", obs_date, "alpaca_adjusted", old_close=99.0, new_close=100.5)
    affected = find_outcomes_affected_by_correction(conn, corr_row)

    assert len(affected) == 1
    a = affected[0]
    assert isinstance(a, AffectedOutcome)
    assert a.horizon_days == 1
    assert a.affected_leg == "entry"
    assert a.source_matched is False, "real AAPL case: source is NULL -> production alpaca, not alpaca_adjusted"
    assert a.status == STATUS_MATURED
    assert a.stored_realized_return is not None


def test_find_outcomes_affected_post_phase15_source_present_source_matched_true():
    """Same shape as the real case, but with source populated (a
    post-Phase-15 observation) - proves the join correctly detects a genuine
    match when one exists, not just the negative case."""
    conn = make_test_db()
    obs_date = "2026-08-12"
    exit_date = "2026-08-13"
    _seed_price(conn, "AAPL", obs_date, 100.0, "alpaca_adjusted")
    _seed_price(conn, "AAPL", exit_date, 101.0, "alpaca_adjusted")
    _seed_observation(conn, "AAPL", obs_date, source="alpaca_adjusted")

    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))

    corr_row = _seed_correction(conn, "AAPL", obs_date, "alpaca_adjusted")
    affected = find_outcomes_affected_by_correction(conn, corr_row)

    assert len(affected) == 1
    assert affected[0].affected_leg == "entry"
    assert affected[0].source_matched is True


def test_find_outcomes_affected_exit_leg_match():
    """An EARLIER observation whose exit_date lands exactly on the corrected
    date - the correction must be attributed to the exit leg, not the entry
    leg (no observation exists dated exactly on the corrected date here)."""
    conn = make_test_db()
    earlier_obs_date = "2026-08-11"
    corrected_date = "2026-08-12"  # exactly earlier_obs_date + 1 trading session
    _seed_price(conn, "AAPL", earlier_obs_date, 90.0, "alpaca")
    _seed_price(conn, "AAPL", corrected_date, 95.0, "alpaca")
    _seed_observation(conn, "AAPL", earlier_obs_date, source="alpaca")

    mature_outcomes(conn, today=date(2026, 8, 12), horizons=(1,))

    corr_row = _seed_correction(conn, "AAPL", corrected_date, "alpaca")
    affected = find_outcomes_affected_by_correction(conn, corr_row)

    assert len(affected) == 1
    assert affected[0].affected_leg == "exit"
    assert affected[0].observation_date == earlier_obs_date
    assert affected[0].exit_date == corrected_date


def test_find_outcomes_affected_no_match_returns_empty_list_never_raises():
    conn = make_test_db()
    obs_date = "2026-08-12"
    exit_date = "2026-08-13"
    _seed_price(conn, "AAPL", obs_date, 100.0, "alpaca")
    _seed_price(conn, "AAPL", exit_date, 101.0, "alpaca")
    _seed_observation(conn, "AAPL", obs_date, source="alpaca")
    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))

    # A correction on a completely unrelated date - no entry or exit leg
    # of any stored outcome touches it.
    corr_row = _seed_correction(conn, "AAPL", "2026-09-01", "alpaca")
    affected = find_outcomes_affected_by_correction(conn, corr_row)
    assert affected == []


def test_load_corrections_returns_empty_dataframe_on_fresh_db_never_raises():
    conn = make_test_db()
    df = load_corrections(conn)
    assert df.empty


# --- #13: recompute_outcome_deterministically - mismatch detection + zero persistence ---


def test_recompute_deterministically_detects_mismatch_and_never_persists():
    conn = make_test_db()
    obs_date = "2026-08-12"
    exit_date = "2026-08-13"
    _seed_price(conn, "AAPL", obs_date, 100.0, "alpaca")
    _seed_price(conn, "AAPL", exit_date, 110.0, "alpaca")
    _seed_observation(conn, "AAPL", obs_date, source="alpaca")
    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))

    outcomes_before = load_outcomes(conn, ticker="AAPL")
    outcome_row = outcomes_before[outcomes_before["horizon_days"] == 1].iloc[0].to_dict()
    assert outcome_row["status"] == STATUS_MATURED
    matured_at_before = outcome_row["matured_at"]
    realized_return_before = outcome_row["realized_return"]

    # Simulate a correction having been applied directly to `prices` (the
    # correction workflow itself is out of scope here - we're only testing
    # the read-only recomputation against whatever `prices` currently holds).
    conn.execute(
        "UPDATE prices SET close = ? WHERE ticker = ? AND date = ? AND source = ?",
        (150.0, "AAPL", exit_date, "alpaca"),
    )
    conn.commit()

    observation_row = load_observations(conn, ticker="AAPL").iloc[0].to_dict()

    result1 = recompute_outcome_deterministically(conn, outcome_row, observation_row, today=date(2026, 8, 13))
    assert isinstance(result1, RecomputationResult)
    assert result1.mismatch is True
    assert result1.stored_realized_return is not None
    assert result1.recomputed_realized_return is not None
    assert result1.stored_realized_return != result1.recomputed_realized_return

    # Calling it again must be perfectly deterministic.
    result2 = recompute_outcome_deterministically(conn, outcome_row, observation_row, today=date(2026, 8, 13))
    assert result1 == result2

    # And it must have written ABSOLUTELY NOTHING back to the outcomes table.
    outcomes_after = load_outcomes(conn, ticker="AAPL")
    row_after = outcomes_after[outcomes_after["horizon_days"] == 1].iloc[0]
    assert row_after["realized_return"] == realized_return_before
    assert row_after["matured_at"] == matured_at_before


def test_recompute_deterministically_no_mismatch_when_prices_unchanged():
    conn = make_test_db()
    obs_date = "2026-08-12"
    exit_date = "2026-08-13"
    _seed_price(conn, "AAPL", obs_date, 100.0, "alpaca")
    _seed_price(conn, "AAPL", exit_date, 110.0, "alpaca")
    _seed_observation(conn, "AAPL", obs_date, source="alpaca")
    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))

    outcomes = load_outcomes(conn, ticker="AAPL")
    outcome_row = outcomes[outcomes["horizon_days"] == 1].iloc[0].to_dict()
    observation_row = load_observations(conn, ticker="AAPL").iloc[0].to_dict()

    result = recompute_outcome_deterministically(conn, outcome_row, observation_row, today=date(2026, 8, 13))
    assert result.mismatch is False
    assert result.stored_realized_return == pytest.approx(result.recomputed_realized_return)


# --- #14: audit_correction on a PENDING outcome - recomputation skipped, not fabricated ---


def test_audit_correction_pending_outcome_skips_recomputation():
    conn = make_test_db()
    obs_date = date(2026, 8, 13)
    _seed_price(conn, "AAPL", obs_date.isoformat(), 100.0, "alpaca")
    _seed_observation(conn, "AAPL", obs_date.isoformat(), source="alpaca")

    mature_outcomes(conn, today=obs_date, horizons=(1,))  # elapsed=0 -> PENDING
    outcomes = load_outcomes(conn, ticker="AAPL")
    pending_row = outcomes[outcomes["horizon_days"] == 1].iloc[0]
    assert pending_row["status"] == STATUS_PENDING

    corr_row = _seed_correction(conn, "AAPL", obs_date.isoformat(), "alpaca")
    result = audit_correction(conn, int(corr_row["id"]), today=obs_date)

    assert len(result["affected_outcomes"]) == 1
    assert result["affected_outcomes"][0].status == STATUS_PENDING
    assert result["recomputations"] == [], "a PENDING outcome has nothing stored to compare against - never fabricated"


# --- #15: unknown correction_id raises ValueError ---


def test_audit_correction_unknown_id_raises_value_error():
    conn = make_test_db()
    with pytest.raises(ValueError):
        audit_correction(conn, correction_id=99999)


def test_audit_correction_unknown_id_raises_value_error_even_with_other_corrections_present():
    conn = make_test_db()
    obs_date = "2026-08-12"
    exit_date = "2026-08-13"
    _seed_price(conn, "AAPL", obs_date, 100.0, "alpaca")
    _seed_price(conn, "AAPL", exit_date, 101.0, "alpaca")
    _seed_observation(conn, "AAPL", obs_date, source="alpaca")
    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))
    _seed_correction(conn, "AAPL", obs_date, "alpaca_adjusted")

    with pytest.raises(ValueError):
        audit_correction(conn, correction_id=99999)


# --- audit_all_corrections: flattening + empty-DB behavior ---


def test_audit_all_corrections_empty_dataframe_when_no_corrections():
    conn = make_test_db()
    df = audit_all_corrections(conn)
    assert df.empty


def test_audit_all_corrections_flattens_one_row_per_correction_affected_outcome_pair():
    conn = make_test_db()
    obs_date = "2026-08-12"
    exit_date = "2026-08-13"
    _seed_price(conn, "AAPL", obs_date, 100.0, "alpaca")
    _seed_price(conn, "AAPL", exit_date, 101.0, "alpaca")
    _seed_observation(conn, "AAPL", obs_date, source=None)
    mature_outcomes(conn, today=date(2026, 8, 13), horizons=(1,))
    _seed_correction(conn, "AAPL", obs_date, "alpaca_adjusted")

    df = audit_all_corrections(conn, today=date(2026, 8, 13))
    assert len(df) == 1
    assert df.iloc[0]["source_matched"] == False  # noqa: E712 - real AAPL case
    assert df.iloc[0]["affected_leg"] == "entry"
