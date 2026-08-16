"""Tests for Phase 17 Area C: exit-event attribution
(docs/specs/phase17.md §3, §3.4, §7 items 4-7).

Covers:
  - strategy_lab/prospective.py::build_todays_observation - technical exit
    signal computation and experiment_b_exit_regime_loss_signal's three-way
    behavior (bearish/bullish/None).
  - strategy_lab/prospective_events.py - ALL_EVENT_TYPES (11 total),
    _exit_transition_events (first-observation/continuously-true/
    regime-loss-alone/both-true-simultaneously cases).
  - Immutability/safety: no bare UPDATE/DELETE, no trading.* import from
    prospective.py, dedup idempotency on the 4 new event types.
"""
import ast
import re
import sqlite3
from datetime import date, timedelta

import pytest

from backtest.config import DEFAULT_RULES
from backtest.scoring import STAGE_ORDER
from db.schema import init_db
from strategy_lab.phase10_experiments import BULLISH_LABEL
from strategy_lab.prospective import build_todays_observation, load_observations, record_observation
from strategy_lab.prospective_events import (
    ALL_EVENT_TYPES,
    EVENT_CONTROL_ENTRY,
    EVENT_CONTROL_EXIT,
    EVENT_EXPERIMENT_A_ENTRY,
    EVENT_EXPERIMENT_A_EXIT,
    EVENT_EXPERIMENT_B_ENTRY,
    EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS,
    EVENT_EXPERIMENT_B_EXIT_TECHNICAL,
    EVENT_MOMENTUM_ADVANCE,
    EVENT_SCORE_CROSSING,
    EVENT_TREND_ADVANCE,
    EVENT_VOLUME_ADVANCE,
    _exit_transition_events,
    load_events,
    record_events_for_observation,
)


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


# =================== §3.4 item 8: exactly 11 event types, exact strings ===================


def test_all_event_types_exactly_11_members_exact_strings():
    assert len(ALL_EVENT_TYPES) == 11
    assert ALL_EVENT_TYPES == (
        EVENT_SCORE_CROSSING, EVENT_TREND_ADVANCE, EVENT_MOMENTUM_ADVANCE, EVENT_VOLUME_ADVANCE,
        EVENT_CONTROL_ENTRY, EVENT_EXPERIMENT_A_ENTRY, EVENT_EXPERIMENT_B_ENTRY,
        EVENT_CONTROL_EXIT, EVENT_EXPERIMENT_A_EXIT, EVENT_EXPERIMENT_B_EXIT_TECHNICAL, EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS,
    )
    assert EVENT_CONTROL_EXIT == "control_exit"
    assert EVENT_EXPERIMENT_A_EXIT == "experiment_a_exit"
    assert EVENT_EXPERIMENT_B_EXIT_TECHNICAL == "experiment_b_exit_technical"
    assert EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS == "experiment_b_exit_regime_loss"


# =================== §3.4 item 2: technical exit computation, table-driven ===================


def _seed_price_series(conn, ticker, source, start, n, base=100.0, step=0.2):
    d0 = date.fromisoformat(start)
    price = base
    for i in range(n):
        price += step
        d = (d0 + timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            (ticker, d, price, price, price, price, 1000, source),
        )
    conn.commit()


def _canned_score(stage, score_value):
    from signals.engine import SignalScore
    return SignalScore(ticker="AAPL", ok=True, score=score_value, highest_confirmed_stage=stage)


@pytest.mark.parametrize("stage,score_value,expected_technical_exit", [
    ("volume", 90.0, False),     # rank 3 >= trend floor(1), score well above exit_max_score(40)
    ("none", 90.0, True),        # rank 0 < 1 -> exit, regardless of score
    ("trend", DEFAULT_RULES.exit_max_score, True),        # score == floor -> exit (<=)
    ("trend", DEFAULT_RULES.exit_max_score + 1, False),   # score just above floor, stage exactly at floor -> no exit
    ("momentum", 100.0, False),  # rank 2 >= 1, score high -> no exit
    ("none", 0.0, True),         # both conditions true
])
def test_technical_exit_signal_table_driven_all_three_variants_identical(
    monkeypatch, stage, score_value, expected_technical_exit,
):
    conn = make_test_db()
    _seed_price_series(conn, "AAPL", "alpaca", "2025-01-01", 260)

    monkeypatch.setattr(
        "strategy_lab.prospective.score_indicators", lambda indicators: _canned_score(stage, score_value),
    )

    obs = build_todays_observation(conn, "AAPL", as_of_date="2025-12-31")
    assert obs is not None
    assert obs["control_exit_signal"] == expected_technical_exit
    assert obs["experiment_a_exit_signal"] == expected_technical_exit
    assert obs["experiment_b_exit_technical_signal"] == expected_technical_exit
    # All three are always identical to each other for the same input (shared rule).
    assert obs["control_exit_signal"] == obs["experiment_a_exit_signal"] == obs["experiment_b_exit_technical_signal"]


# =================== §3.4 item 3: experiment_b_exit_regime_loss_signal three-way ===================


@pytest.mark.parametrize("regime_label,expected_regime_loss", [
    ("bearish_trend", True),
    ("bullish_trend", False),
    (None, False),  # a data gap must NEVER be misreported as a known regime loss
])
def test_experiment_b_exit_regime_loss_signal_three_way(monkeypatch, regime_label, expected_regime_loss):
    conn = make_test_db()
    _seed_price_series(conn, "AAPL", "alpaca", "2025-01-01", 260)

    monkeypatch.setattr(
        "strategy_lab.prospective.regime_label_and_date_as_of",
        lambda regime_series, as_of_date=None: (regime_label, "2025-12-30" if regime_label is not None else None),
    )

    obs = build_todays_observation(conn, "AAPL", as_of_date="2025-12-31")
    assert obs is not None
    assert obs["regime"] == regime_label
    assert obs["experiment_b_exit_regime_loss_signal"] is expected_regime_loss


def test_experiment_b_exit_regime_loss_signal_never_true_when_regime_is_bullish_via_real_computation():
    conn = make_test_db()
    _seed_price_series(conn, "AAPL", "alpaca", "2025-01-01", 260)
    # No SPY rows -> regime None -> regime loss must be False, not fabricated.
    obs = build_todays_observation(conn, "AAPL", as_of_date="2025-12-31")
    assert obs["regime"] is None
    assert obs["experiment_b_exit_regime_loss_signal"] is False


# =================== §3.4 items 4-5: transition semantics ===================


def _row(control=False, exp_a=False, exp_b_tech=False, exp_b_regime=False):
    return {
        "control_exit_signal": control, "experiment_a_exit_signal": exp_a,
        "experiment_b_exit_technical_signal": exp_b_tech, "experiment_b_exit_regime_loss_signal": exp_b_regime,
    }


def test_exit_transition_events_first_observation_never_fires_even_if_all_true():
    events = _exit_transition_events(None, _row(True, True, True, True))
    assert events == []


def test_exit_transition_events_continuously_true_fires_exactly_once():
    row_true = _row(True, False, False, False)
    # Day 1 -> Day 2: False -> True, fires.
    assert _exit_transition_events(_row(False), row_true) == [EVENT_CONTROL_EXIT]
    # Day 2 -> Day 3: stays True, does not fire again.
    assert _exit_transition_events(row_true, row_true) == []
    # Day 3 -> Day 4: still True, still does not fire.
    assert _exit_transition_events(row_true, row_true) == []


# =================== §3.4 items 6-7: Experiment B regime-loss attribution ===================


def test_experiment_b_regime_loss_alone_fires_only_regime_loss_event():
    prev = _row(exp_b_tech=False, exp_b_regime=False)
    curr = _row(exp_b_tech=False, exp_b_regime=True)
    events = _exit_transition_events(prev, curr)
    assert events == [EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS]
    assert EVENT_EXPERIMENT_B_EXIT_TECHNICAL not in events


def test_experiment_b_both_true_simultaneously_fires_both_events_independently():
    prev = _row(exp_b_tech=False, exp_b_regime=False)
    curr = _row(exp_b_tech=True, exp_b_regime=True)
    events = _exit_transition_events(prev, curr)
    assert EVENT_EXPERIMENT_B_EXIT_TECHNICAL in events
    assert EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS in events
    assert len(events) == 2  # neither dropped, neither merged/prioritized


# =================== §3.4 item 9 / §7 item 6: dedup idempotency for new event types ===================


def _seed_obs_row(conn, ticker, obs_date, **overrides):
    base = dict(
        observation_date=obs_date, ticker=ticker, score=50.0, stage="none", regime="bearish_trend",
        control_entry_signal=False, experiment_a_entry_signal=False, experiment_b_entry_signal=False,
        adx=20.0, rsi=40.0, macd=-0.5, macd_signal=-0.2, volume_ratio=1.0, close=90.0, source="alpaca_adjusted",
        control_exit_signal=True, experiment_a_exit_signal=True,
        experiment_b_exit_technical_signal=False, experiment_b_exit_regime_loss_signal=True,
    )
    base.update(overrides)
    record_observation(conn, **base)
    return build_todays_observation


def test_record_events_for_observation_dedups_new_exit_event_types_on_retry():
    conn = make_test_db()
    # Prior day: all exit signals False.
    _seed_obs_row(
        conn, "AAA", "2026-01-01",
        control_exit_signal=False, experiment_a_exit_signal=False,
        experiment_b_exit_technical_signal=False, experiment_b_exit_regime_loss_signal=False,
    )
    curr_row = {
        "observation_date": "2026-01-02", "ticker": "AAA", "score": 30.0, "stage": "none", "regime": "bearish_trend",
        "control_entry_signal": False, "experiment_a_entry_signal": False, "experiment_b_entry_signal": False,
        "control_exit_signal": True, "experiment_a_exit_signal": True,
        "experiment_b_exit_technical_signal": True, "experiment_b_exit_regime_loss_signal": True,
        "source": "alpaca_adjusted", "config_fingerprint": None,
    }
    record_observation(
        conn, observation_date=curr_row["observation_date"], ticker=curr_row["ticker"], score=curr_row["score"],
        stage=curr_row["stage"], regime=curr_row["regime"], control_entry_signal=False,
        experiment_a_entry_signal=False, experiment_b_entry_signal=False, adx=20.0, rsi=40.0, macd=-0.5,
        macd_signal=-0.2, volume_ratio=1.0, close=90.0, source="alpaca_adjusted",
        control_exit_signal=True, experiment_a_exit_signal=True,
        experiment_b_exit_technical_signal=True, experiment_b_exit_regime_loss_signal=True,
    )

    detected1, newly1 = record_events_for_observation(conn, curr_row)
    assert set(newly1) == {
        EVENT_CONTROL_EXIT, EVENT_EXPERIMENT_A_EXIT, EVENT_EXPERIMENT_B_EXIT_TECHNICAL, EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS,
    }

    # Retry: same curr_row again - detection still fires (compares against
    # prior stored obs), but record_event's ON CONFLICT DO NOTHING dedupes.
    detected2, newly2 = record_events_for_observation(conn, curr_row)
    assert set(detected2) == set(detected1)
    assert newly2 == []

    events = load_events(conn, ticker="AAA")
    for et in (EVENT_CONTROL_EXIT, EVENT_EXPERIMENT_A_EXIT, EVENT_EXPERIMENT_B_EXIT_TECHNICAL, EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS):
        assert (events["event_type"] == et).sum() == 1, f"{et} must not be duplicated on retry"


# =================== §7 item 6: immutability - only pre-existing write patterns ===================


def test_prospective_and_prospective_events_contain_no_bare_update_or_delete():
    for path in ("strategy_lab/prospective.py", "strategy_lab/prospective_events.py"):
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
        sql_mutation_pattern = re.compile(r"\bUPDATE\s+\S+\s+SET\b|\bDELETE\s+FROM\b", re.IGNORECASE)
        offenders = [
            node.value[:120] for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and sql_mutation_pattern.search(node.value)
        ]
        assert not offenders, f"{path} must contain no UPDATE/DELETE SQL string post-Phase17: {offenders}"


def test_prospective_execute_calls_are_only_create_alter_or_the_single_insert_on_conflict():
    import subprocess
    for path in ("strategy_lab/prospective.py", "strategy_lab/prospective_events.py"):
        result = subprocess.run(["grep", "-n", "conn.execute\\|cur.execute", path], capture_output=True, text=True)
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        assert lines, f"expected at least one conn.execute in {path}"
        for line in lines:
            assert not re.search(r"UPDATE\s+\S+\s+SET|DELETE\s+FROM", line, re.IGNORECASE), (
                f"unexpected mutation statement in {path} post-Phase17: {line}"
            )


# =================== §7 item 7: prospective.py imports ZERO names from trading.* ===================


def test_prospective_py_imports_zero_names_from_trading_module():
    with open("strategy_lab/prospective.py") as f:
        tree = ast.parse(f.read(), filename="strategy_lab/prospective.py")
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (
            node.module == "trading" or node.module.startswith("trading.")
        ):
            offenders.append(node.module)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "trading" or alias.name.startswith("trading."):
                    offenders.append(alias.name)
    assert not offenders, (
        f"strategy_lab/prospective.py must import ZERO names from any trading.* module "
        f"(docs/specs/phase17.md §0.3 inline-duplication decision): {offenders}"
    )


# =================== §7 item 7: research_automation.py never touches alerts/alert_state ===================


def test_research_automation_never_calls_alerts_or_writes_alert_state():
    with open("strategy_lab/research_automation.py") as f:
        content = f.read()
        f.seek(0)
        tree = ast.parse(content, filename="strategy_lab/research_automation.py")

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (
            node.module == "alerts" or node.module.startswith("alerts.")
        ):
            pytest.fail(f"strategy_lab/research_automation.py must never import from alerts.*: {node.module}")

    docstring_ids = set()
    candidates = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    for node in candidates:
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) \
                and isinstance(node.body[0].value.value, str):
            docstring_ids.add(id(node.body[0].value))

    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_ids:
            if "alert_state" in node.value or "INSERT INTO alerts" in node.value:
                hits.append(node.value[:120])
    assert not hits, f"strategy_lab/research_automation.py must never contain 'alert_state'/'INSERT INTO alerts' string constants: {hits}"


# =================== §7 item 7: no deploy/.plist/subprocess/os.system/launchctl in Phase 17 files ===================


PHASE17_MODIFIED_FILES = [
    "strategy_lab/research_automation.py", "strategy_lab/regime_history.py", "strategy_lab/prospective.py",
    "strategy_lab/prospective_events.py", "ops/event_provenance_audit.py", "ops/daily_report.py",
    "ops/prospective_audit.py", "dashboard/data.py", "dashboard/views/ops_overview.py",
    "dashboard/views/strategy_lab.py",
]


def test_no_phase17_file_references_deploy_plist_subprocess_or_launchctl():
    offenders = {}
    for path in PHASE17_MODIFIED_FILES:
        with open(path) as f:
            content = f.read()
            f.seek(0)
            tree = ast.parse(content, filename=path)

        string_hits = [
            node.value[:120] for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and ("deploy/" in node.value or ".plist" in node.value or "launchctl" in node.value)
        ]

        import_hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "subprocess" or alias.name.startswith("subprocess."):
                        import_hits.append("imports subprocess")
            if isinstance(node, ast.ImportFrom) and node.module and (
                node.module == "subprocess" or node.module.startswith("subprocess.")
            ):
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
    assert not offenders, f"Phase 17 files must never reference deploy/.plist/subprocess/os.system/launchctl: {offenders}"


# =================== §7 item 7 (cross-check): existing generic scans already cover Phase 17 files ===================


def test_generic_strategy_lab_import_scan_still_passes_and_covers_phase17_files():
    """docs/specs/phase17.md §7 item 7: the existing generic
    tests/test_strategy_lab.py::test_strategy_lab_never_imports_order_execution_or_alerting
    scan is directory-wide and automatically covers every modified
    strategy_lab/*.py file - confirm it still passes (no changes needed to
    the scan itself). Run via a real pytest subprocess so fixture-taking
    tests in that module are still collected/injected correctly."""
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         "tests/test_strategy_lab.py::test_strategy_lab_never_imports_order_execution_or_alerting"],
        capture_output=True, text=True, cwd=".",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_generic_ops_safety_scan_still_passes_and_covers_phase17_files():
    """§7 item 7: tests/test_ops_safety.py's generic ops/*.py scan(s) are
    directory-wide, so they automatically cover the modified ops/*.py files
    with zero changes to the scan itself - confirm the whole file still
    passes via a real pytest subprocess."""
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_ops_safety.py"],
        capture_output=True, text=True, cwd=".",
    )
    assert result.returncode == 0, result.stdout + result.stderr
