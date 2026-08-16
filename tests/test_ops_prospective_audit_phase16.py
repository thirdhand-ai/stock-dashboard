"""Tests for Phase 16 Area E rollups (ops/prospective_audit.py's additive
functions), dashboard wiring, and the explicit cross-cutting safety
requirements from the tester's task brief (docs/specs/phase16.md §5.6,
§8 items 9-10).

Mirrors tests/test_ops_prospective_audit_phase15.py's convention of a
separate file importing the same ops.prospective_audit module.
"""
import ast
import contextlib
import inspect
import os
import re
import sqlite3
from datetime import date

import pytest

from db.database import db_session
from db.schema import init_db
from ops.evidence_classification import (
    EVIDENCE_EARLY_EVIDENCE,
    EVIDENCE_EVALUATION_READY,
    EVIDENCE_INSUFFICIENT_DATA,
)
from ops.prospective_audit import (
    compute_completeness_breakdown,
    compute_regime_distribution,
    long_term_monitoring_summary,
    phase16_monitoring_summary,
)
from strategy_lab.prospective import record_observation
from strategy_lab.research_automation import STATUS_SUCCESS_RESEARCH, ResearchRunResult
from strategy_lab.research_automation import _finish_run as research_finish_run
from strategy_lab.research_automation import _start_run as research_start_run
from strategy_lab.research_automation import ensure_schema as ensure_research_schema

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _record_research_run(conn, trading_date, status, observations_created=0):
    ensure_research_schema(conn)
    run_id = research_start_run(conn, date.fromisoformat(trading_date))
    result = ResearchRunResult(
        run_id=run_id, status=status, trading_date=trading_date,
        tickers_attempted=1, observations_created=observations_created, duplicates_skipped=0, errors=[],
    )
    research_finish_run(conn, run_id, result)
    return run_id


def _seed_observation(conn, ticker, obs_date, regime=None, source=None, methodology_version=None, config_fingerprint=None):
    record_observation(
        conn, observation_date=obs_date, ticker=ticker, score=80.0, stage="volume", regime=regime,
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=100.0,
        source=source, methodology_version=methodology_version, config_fingerprint=config_fingerprint,
    )


# --- §5.6 item 2: compute_regime_distribution ---


def test_compute_regime_distribution_counts_four_labels_plus_null_bucket():
    conn = make_test_db()
    _seed_observation(conn, "AAA", "2026-01-01", regime="bullish_trend")
    _seed_observation(conn, "BBB", "2026-01-01", regime="neutral_mixed")
    _seed_observation(conn, "CCC", "2026-01-01", regime="bearish_trend")
    _seed_observation(conn, "DDD", "2026-01-01", regime="elevated_volatility_risk_off")
    _seed_observation(conn, "EEE", "2026-01-01", regime=None)
    _seed_observation(conn, "FFF", "2026-01-02", regime=None)

    dist = compute_regime_distribution(conn)
    assert dist["by_label"]["bullish_trend"] == 1
    assert dist["by_label"]["neutral_mixed"] == 1
    assert dist["by_label"]["bearish_trend"] == 1
    assert dist["by_label"]["elevated_volatility_risk_off"] == 1
    assert dist["by_label"]["NULL"] == 2
    assert dist["total"] == 6


def test_compute_regime_distribution_empty_db():
    conn = make_test_db()
    dist = compute_regime_distribution(conn)
    assert dist == {"by_label": {}, "total": 0}


# --- compute_completeness_breakdown rollup ---


def test_compute_completeness_breakdown_shape_and_counts():
    conn = make_test_db()
    _seed_observation(conn, "AAA", "2026-01-01", regime="bullish_trend", source="alpaca_adjusted",
                       methodology_version="phase11-v1", config_fingerprint="fp1")
    _seed_observation(conn, "BBB", "2026-01-01", regime=None, source=None, methodology_version=None, config_fingerprint=None)

    breakdown = compute_completeness_breakdown(conn)
    assert set(breakdown.keys()) == {"observations", "events", "outcomes"}
    for section in breakdown.values():
        assert set(section.keys()) == {"COMPLETE", "PARTIAL", "LEGACY_INCOMPLETE", "INVALID", "total"}
    assert breakdown["observations"]["total"] == 2
    assert breakdown["observations"]["COMPLETE"] == 1
    assert breakdown["observations"]["LEGACY_INCOMPLETE"] == 1
    assert breakdown["events"]["total"] == 0
    assert breakdown["outcomes"]["total"] == 0


# --- §5.6 item 3: phase16_monitoring_summary is a strict superset ---


def test_phase16_monitoring_summary_is_superset_of_long_term_monitoring_summary():
    conn = make_test_db()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=1)
    base = long_term_monitoring_summary(conn, today=date(2024, 1, 10))
    extended = phase16_monitoring_summary(conn, today=date(2024, 1, 10))

    assert set(base.keys()).issubset(set(extended.keys()))
    new_keys = set(extended.keys()) - set(base.keys())
    assert new_keys == {
        "regime_distribution", "legacy_regime_gap_summary", "completeness_breakdown",
        "event_provenance_audit", "outcome_source_resolution", "evidence_sufficiency",
    }
    for k in base:
        assert extended[k] == base[k]


def test_phase16_monitoring_summary_empty_db_never_raises():
    conn = make_test_db()
    summary = phase16_monitoring_summary(conn, today=date(2024, 1, 10))
    assert "regime_distribution" in summary
    assert "evidence_sufficiency" in summary


# --- §5.6 item 6: evidence_sufficiency status vocabulary ---


def test_evidence_sufficiency_status_from_exact_vocabulary_only():
    conn = make_test_db()
    summary = phase16_monitoring_summary(conn, today=date(2024, 1, 10))
    status = summary["evidence_sufficiency"]["status"]
    assert status in {EVIDENCE_INSUFFICIENT_DATA, EVIDENCE_EARLY_EVIDENCE, EVIDENCE_EVALUATION_READY}


# --- §5.6 item 4: dashboard getters - single db_session, idempotent ---


def test_get_phase16_monitoring_summary_and_get_legacy_regime_gap_report_open_exactly_one_db_session():
    import dashboard.data as dashboard_data
    src1 = inspect.getsource(dashboard_data.get_phase16_monitoring_summary)
    src2 = inspect.getsource(dashboard_data.get_legacy_regime_gap_report)
    assert src1.count("db_session(") == 1
    assert src2.count("db_session(") == 1


def _patch_db_session(monkeypatch, conn):
    import dashboard.data as dashboard_data

    @contextlib.contextmanager
    def fake_db_session():
        yield conn
    monkeypatch.setattr(dashboard_data, "db_session", fake_db_session)


def test_get_phase16_monitoring_summary_deterministic_across_repeated_calls(monkeypatch):
    import dashboard.data as dashboard_data
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    dashboard_data.get_phase16_monitoring_summary.clear()
    _record_research_run(conn, "2024-01-02", STATUS_SUCCESS_RESEARCH, observations_created=1)

    first = dashboard_data.get_phase16_monitoring_summary()
    dashboard_data.get_phase16_monitoring_summary.clear()
    second = dashboard_data.get_phase16_monitoring_summary()
    assert first == second


def test_get_legacy_regime_gap_report_deterministic_across_repeated_calls(monkeypatch):
    import dashboard.data as dashboard_data
    conn = make_test_db()
    _patch_db_session(monkeypatch, conn)
    dashboard_data.get_legacy_regime_gap_report.clear()

    first = dashboard_data.get_legacy_regime_gap_report()
    dashboard_data.get_legacy_regime_gap_report.clear()
    second = dashboard_data.get_legacy_regime_gap_report()
    assert first.equals(second)
    assert first.empty  # no legacy rows seeded


def test_clear_all_caches_includes_phase16_getters():
    import dashboard.data as dashboard_data
    source = inspect.getsource(dashboard_data.clear_all_caches)
    assert "get_phase16_monitoring_summary.clear()" in source
    assert "get_legacy_regime_gap_report.clear()" in source


# --- §5.6 item 5 / parent task item: dashboard read-only safety (grep-based) ---


def test_render_regime_provenance_monitoring_contains_no_button_or_form():
    with open(os.path.join(REPO_ROOT, "dashboard", "views", "ops_overview.py")) as f:
        content = f.read()
    start = content.index("def _render_regime_provenance_monitoring(")
    end = content.index("\ndef _render_automation_history(")
    body = content[start:end]
    assert "st.button" not in body
    assert "st.form" not in body


def test_render_regime_provenance_detail_contains_no_button_or_form():
    with open(os.path.join(REPO_ROOT, "dashboard", "views", "strategy_lab.py")) as f:
        content = f.read()
    start = content.index("def _render_regime_provenance_detail(")
    end = content.index("\ndef render():", start)
    body = content[start:end]
    assert "st.button" not in body
    assert "st.form" not in body


# --- ================= explicit, cross-cutting Phase 16 safety tests ================= ---

PHASE16_FILES = [
    "ops/regime_reconstruction_audit.py",
    "ops/event_provenance_audit.py",
    "ops/completeness_classification.py",
    "strategy_lab/regime_history.py",
    "strategy_lab/prospective.py",
    "strategy_lab/outcome_maturation.py",
    "ops/prospective_audit.py",
    "dashboard/data.py",
    "dashboard/views/ops_overview.py",
    "dashboard/views/strategy_lab.py",
]


def _full_paths():
    return [os.path.join(REPO_ROOT, p) for p in PHASE16_FILES]


def _module_level_import_names(file_path):
    with open(file_path) as f:
        tree = ast.parse(f.read(), filename=file_path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_no_phase16_file_imports_trading_order_placing_functions():
    # NOTE: trading.client (a read-only Alpaca client wrapper used by the
    # pre-existing, out-of-Phase-16-scope Paper Portfolio dashboard view) is
    # deliberately NOT in this list - it is not order-execution capable by
    # itself and dashboard/data.py's import of it predates Phase 16 entirely
    # (see git blame). The actual order-placing modules are engine/orders/run_paper.
    forbidden_prefixes = ("trading.engine", "trading.orders", "trading.run_paper")
    offenders = {}
    for path in _full_paths():
        imports = _module_level_import_names(path)
        forbidden = {i for i in imports if i in forbidden_prefixes or any(i.startswith(p + ".") for p in forbidden_prefixes)}
        if forbidden:
            offenders[path] = forbidden
    assert not offenders, f"Phase 16 files must never import trading order-placing modules: {offenders}"


def test_no_phase16_file_imports_discord_send_functions():
    forbidden_prefixes = ("alerts.discord", "alerts.runner", "alerts.run_alerts")
    offenders = {}
    for path in _full_paths():
        imports = _module_level_import_names(path)
        forbidden = {i for i in imports if i in forbidden_prefixes or any(i.startswith(p + ".") for p in forbidden_prefixes)}
        if forbidden:
            offenders[path] = forbidden
    assert not offenders, f"Phase 16 files must never import alerts.discord/runner/run_alerts: {offenders}"


def _docstring_node_ids(tree):
    """id()s of every module/function/class DOCSTRING constant node (the
    first statement of the body, when it's a bare string expression) - so a
    file's own safety-documentation prose (e.g. "never touches ... alert_state")
    can be excluded from a real-SQL/real-write scan without excluding actual
    code-level string constants."""
    ids = set()
    candidates = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    for node in candidates:
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) \
                and isinstance(node.body[0].value.value, str):
            ids.add(id(node.body[0].value))
    return ids


def test_no_phase16_file_contains_alert_state_or_insert_into_alerts_string_literals():
    """AST ast.Constant scan, EXCLUDING docstrings (this task's own explicit
    instruction: 'outside comments/docstrings') - so a file's own safety
    documentation prose ("never touches alert_state") can't false-positive,
    while a real code-level string constant containing either literal still
    fails the build."""
    offenders = {}
    for path in _full_paths():
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
        docstring_ids = _docstring_node_ids(tree)
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_ids:
                if "alert_state" in node.value or "INSERT INTO alerts" in node.value:
                    hits.append(node.value[:120])
        if hits:
            offenders[path] = hits
    assert not offenders, f"Phase 16 files must never contain 'alert_state'/'INSERT INTO alerts' string constants: {offenders}"


def test_no_phase16_file_references_deploy_or_plist_or_subprocess_or_launchctl():
    offenders = {}
    for path in _full_paths():
        with open(path) as f:
            content = f.read()
            f.seek(0)
            tree = ast.parse(content, filename=path)

        string_hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "deploy/" in node.value or ".plist" in node.value or "launchctl" in node.value:
                    string_hits.append(node.value[:120])

        import_hits = []
        imports = _module_level_import_names(path)
        if any(i == "subprocess" or i.startswith("subprocess.") for i in imports):
            import_hits.append("imports subprocess")

        call_hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "system" and isinstance(func.value, ast.Name) and func.value.id == "os":
                    call_hits.append(("os.system call", node.lineno))

        hits = string_hits + import_hits + call_hits
        if hits:
            offenders[path] = hits
    assert not offenders, f"Phase 16 files must never reference deploy/.plist/subprocess/os.system/launchctl: {offenders}"


_MUTATION_TARGET_MODULES = {"signals.config", "backtest.config", "trading.config", "config.settings", "alerts.config"}


def test_no_phase16_file_does_setattr_or_attribute_assignment_targeting_frozen_config_modules():
    offenders = {}
    for path in _full_paths():
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)

        # import aliases actually used in this file, so we can resolve e.g.
        # `from signals import config as signals_config; signals_config.X = 1`
        alias_to_module = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    alias_to_module[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    alias_to_module[alias.asname or alias.name] = f"{node.module}.{alias.name}"

        hits = []
        for node in ast.walk(tree):
            # setattr(module_obj, "name", value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr":
                if node.args and isinstance(node.args[0], ast.Name):
                    target_module = alias_to_module.get(node.args[0].id, node.args[0].id)
                    if target_module in _MUTATION_TARGET_MODULES:
                        hits.append(("setattr", node.lineno, target_module))
            # module_obj.attr = value
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                        target_module = alias_to_module.get(target.value.id, target.value.id)
                        if target_module in _MUTATION_TARGET_MODULES:
                            hits.append(("attr-assign", node.lineno, target_module))
        if hits:
            offenders[path] = hits
    assert not offenders, f"Phase 16 files must never mutate a frozen config module: {offenders}"


def test_evidence_classification_thresholds_untouched_by_phase16():
    """ops/evidence_classification.py's thresholds must be byte-identical to
    their pre-Phase-16 values - confirmed here via a real git diff check
    (zero working-tree changes to that file) rather than trusting a
    docstring claim."""
    import subprocess
    result = subprocess.run(
        ["git", "diff", "--stat", "HEAD", "--", "ops/evidence_classification.py"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.stdout.strip() == "", f"ops/evidence_classification.py must have zero diff: {result.stdout}"

    from ops.evidence_classification import MIN_DAYS_EARLY_EVIDENCE, MIN_DAYS_EVALUATION_READY
    assert MIN_DAYS_EARLY_EVIDENCE == 20
    assert MIN_DAYS_EVALUATION_READY == 60


# --- generic ops/*.py and strategy_lab/*.py directory-wide scans already
# cover every Phase 16 file with zero test changes to those files
# themselves (docs/specs/phase16.md §6/§8 item 9) - sanity-check that the
# new/modified files are actually present on disk so that claim is
# meaningful, not vacuous. ---


def test_all_new_phase16_ops_files_exist_and_are_covered_by_generic_ops_scan_directory():
    ops_dir = os.path.join(REPO_ROOT, "ops")
    for filename in ("regime_reconstruction_audit.py", "event_provenance_audit.py", "completeness_classification.py"):
        assert os.path.isfile(os.path.join(ops_dir, filename)), f"expected new Phase 16 file {filename} in ops/"


def test_all_modified_phase16_strategy_lab_files_exist_and_are_covered_by_generic_scan_directory():
    sl_dir = os.path.join(REPO_ROOT, "strategy_lab")
    for filename in ("regime_history.py", "prospective.py", "outcome_maturation.py"):
        assert os.path.isfile(os.path.join(sl_dir, filename))


# --- frozen-file safety net: the diff must never touch a file explicitly
# declared frozen/forbidden by ANY phase, not just be a subset of one
# phase's own allowlist ---
#
# NOTE (superseded design): this test used to be
# test_git_diff_only_touches_files_listed_in_phase16_spec_section7, which
# asserted the live `git diff`/untracked-file set was an exact SUBSET of
# Phase 16's own hardcoded file list (docs/specs/phase16.md §7). That design
# is structurally broken for any later phase that legitimately touches the
# same files Phase 16 touched - e.g. Phase 17 (docs/specs/phase17.md §6)
# correctly extends strategy_lab/prospective.py, ops/prospective_audit.py,
# dashboard/data.py, dashboard/views/ops_overview.py, and
# dashboard/views/strategy_lab.py, none of which were in Phase 16's §7
# allowlist, so the old test broke on the very next phase - a false
# positive, not a real safety violation.
#
# The REAL safety property Phase 16 (and every phase since) actually cares
# about is narrower and durable across phases: the diff must never touch a
# small, explicitly-frozen set of files - strategy_lab/phase10_experiments.py,
# backtest/config.py, db/schema.py, ops/evidence_classification.py, deploy/,
# and the frozen Phase 9/10/11 artifact generators
# (strategy_lab/report.py, strategy_lab/report_phase10.py,
# strategy_lab/portfolio_simulator.py, strategy_lab/phase9_baseline.py,
# strategy_lab/phase10_baseline.py, strategy_lab/phase11_report.py) - the
# exact set docs/specs/phase16.md §7 and docs/specs/phase17.md §6 BOTH
# separately, independently declare "untouched" ("do not edit"). This test
# asserts non-intersection with that forbidden set, not subset-of-an-
# allowlist, so it stays meaningful for Phase 17, 18, 19, ... without
# needing a rewrite every single phase.

FORBIDDEN_FROZEN_FILES = {
    "strategy_lab/phase10_experiments.py",
    "backtest/config.py",
    "db/schema.py",
    "ops/evidence_classification.py",
    "strategy_lab/report.py",
    "strategy_lab/report_phase10.py",
    "strategy_lab/portfolio_simulator.py",
    "strategy_lab/phase9_baseline.py",
    "strategy_lab/phase10_baseline.py",
    "strategy_lab/phase11_report.py",
}
FORBIDDEN_FROZEN_PREFIXES = ("deploy/",)


def _current_diff_and_untracked_files():
    import subprocess
    modified = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"], capture_output=True, text=True, cwd=REPO_ROOT,
    ).stdout.splitlines()
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"], capture_output=True, text=True, cwd=REPO_ROOT,
    ).stdout.splitlines()
    return set(modified) | {u for u in untracked if not u.startswith(".claude/")}


def test_git_diff_never_touches_frozen_or_forbidden_files():
    """Durable, cross-phase safety net (see module-level note above): the
    live working-tree diff (modified + untracked, staged or not) must never
    intersect FORBIDDEN_FROZEN_FILES/FORBIDDEN_FROZEN_PREFIXES - the exact
    set both docs/specs/phase16.md §7 and docs/specs/phase17.md §6 declare
    frozen/untouched. This is a real, still-checked safety guarantee (no
    frozen artifact generator, no strategy-rule/risk-config file, no
    deploy/scheduling file is ever silently edited by an application phase)
    - it is just no longer expressed as "subset of one phase's allowlist,"
    which rotted on the very next phase."""
    all_touched = _current_diff_and_untracked_files()

    exact_hits = all_touched & FORBIDDEN_FROZEN_FILES
    prefix_hits = {f for f in all_touched for p in FORBIDDEN_FROZEN_PREFIXES if f.startswith(p)}

    hits = exact_hits | prefix_hits
    assert not hits, (
        f"working tree touches explicitly frozen/forbidden files - this must never happen: {hits}"
    )
