"""Tests for Phase 16 Area A: strategy_lab/regime_history.py::regime_label_as_of
and its wiring into strategy_lab/prospective.py::build_todays_observation
(docs/specs/phase16.md §0.1, §1, §8 items 1-2, 4, 10, 11, 12).

This is THE root-cause fix module: before it, `regime` was NULL for
essentially every automated prospective observation because of an
EXACT-match lookup against a regime series whose latest available date is
structurally always behind the observation's own date at 16:45 ET.
"""
import ast
import re
import sqlite3
import subprocess
from datetime import date, timedelta

import pandas as pd
import pytest

from db.schema import init_db
from research.config import DEFAULT_REGIME_CONFIG
from strategy_lab.regime_history import compute_historical_regime_series, regime_label_as_of

LABEL_BULLISH = DEFAULT_REGIME_CONFIG.LABEL_BULLISH
LABEL_NEUTRAL = DEFAULT_REGIME_CONFIG.LABEL_NEUTRAL
LABEL_BEARISH = DEFAULT_REGIME_CONFIG.LABEL_BEARISH
LABEL_ELEVATED_VOL = DEFAULT_REGIME_CONFIG.LABEL_ELEVATED_VOL


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _series(dates_labels):
    return pd.DataFrame({"date": [d for d, _ in dates_labels], "label": [l for _, l in dates_labels]})


# --- exact string values (defensive: catches a future accidental rename) ---


def test_regime_config_label_strings_are_exactly_as_documented():
    assert LABEL_BULLISH == "bullish_trend"
    assert LABEL_NEUTRAL == "neutral_mixed"
    assert LABEL_BEARISH == "bearish_trend"
    assert LABEL_ELEVATED_VOL == "elevated_volatility_risk_off"


# --- §1.4 item 1 / §8 item 2: the exact real-world 16:45 ET scenario ---


def test_no_lookahead_missing_todays_bar_falls_back_to_yesterdays_label():
    series = _series([("2026-08-10", LABEL_BULLISH), ("2026-08-11", LABEL_NEUTRAL), ("2026-08-12", LABEL_BEARISH)])
    # D = 2026-08-13 has no row (today's SPY bar not fetched yet at 16:45 ET)
    result = regime_label_as_of(series, "2026-08-13")
    assert result == LABEL_BEARISH  # D-1's label, never None, never a fabricated D value
    assert result is not None


# --- §1.4 item 2 / §8 item 1: all four labels round-trip ---


@pytest.mark.parametrize("label", [LABEL_BULLISH, LABEL_NEUTRAL, LABEL_BEARISH, LABEL_ELEVATED_VOL])
def test_all_four_labels_round_trip(label):
    series = _series([("2026-01-01", "other_label_1"), ("2026-01-02", label), ("2026-01-03", "other_label_2")])
    assert regime_label_as_of(series, "2026-01-02") == label


# --- table-driven sweep of as_of_date values inside/outside the range ---


@pytest.mark.parametrize("as_of_date,expected", [
    ("2026-01-01", LABEL_BULLISH),          # exact match on first row
    ("2026-01-02", LABEL_BULLISH),          # between rows -> most recent <=
    ("2026-01-03", LABEL_NEUTRAL),          # exact match on second row
    ("2026-01-04", LABEL_NEUTRAL),          # between rows -> most recent <=
    ("2026-01-05", LABEL_BEARISH),          # exact match on last row
    ("2026-06-01", LABEL_BEARISH),          # far beyond last row -> most recent <=
    ("2025-12-31", None),                    # before every row -> None
])
def test_as_of_date_sweep_inside_and_outside_range(as_of_date, expected):
    series = _series([
        ("2026-01-01", LABEL_BULLISH), ("2026-01-03", LABEL_NEUTRAL), ("2026-01-05", LABEL_BEARISH),
    ])
    assert regime_label_as_of(series, as_of_date) == expected


# --- §1.4 item 3: as_of_date earlier than every row -> None ---


def test_as_of_date_before_every_row_returns_none():
    series = _series([("2026-05-01", LABEL_BULLISH), ("2026-05-02", LABEL_NEUTRAL)])
    assert regime_label_as_of(series, "2026-04-01") is None


# --- §1.4 item 4 / §8 item 3: empty series -> None regardless of as_of_date ---


def test_empty_series_returns_none_for_any_as_of_date():
    empty = pd.DataFrame(columns=["date", "label"])
    assert regime_label_as_of(empty, None) is None
    assert regime_label_as_of(empty, "2026-01-01") is None


def test_empty_series_from_real_compute_historical_regime_series_returns_none():
    """Insufficient SPY history (fresh DB, zero price rows) -> compute_historical_regime_series
    returns an empty frame -> regime_label_as_of must return None, never raise, never fabricate."""
    conn = make_test_db()
    series = compute_historical_regime_series(conn)
    assert series.empty
    assert regime_label_as_of(series, None) is None
    assert regime_label_as_of(series, "2026-01-01") is None


# --- §1.4 item 5: as_of_date=None behavior is byte-identical to pre-fix behavior ---


def test_as_of_date_none_returns_single_most_recent_label_like_pre_fix_behavior():
    series = _series([("2026-01-01", LABEL_BULLISH), ("2026-01-02", LABEL_NEUTRAL), ("2026-01-03", LABEL_BEARISH)])
    # Pre-fix behavior (docs/specs/phase16.md §0.1): regime_series.iloc[-1]["label"]
    assert regime_label_as_of(series, None) == series.iloc[-1]["label"] == LABEL_BEARISH


def test_as_of_date_none_on_empty_series_is_none_not_a_crash():
    empty = pd.DataFrame(columns=["date", "label"])
    assert regime_label_as_of(empty, None) is None


# --- §8 item 10: timezone/NYSE-calendar edge case across a weekend/holiday boundary ---


def test_no_lookahead_across_weekend_boundary():
    """Friday 2026-08-14's regime is the most recent available bar; Monday
    2026-08-17 (no SPY bar recorded yet, e.g. before the day's own close is
    fetched) must fall back to Friday's label, never None, never a
    fabricated Monday value."""
    series = _series([
        ("2026-08-12", LABEL_NEUTRAL), ("2026-08-13", LABEL_NEUTRAL), ("2026-08-14", LABEL_BULLISH),
        # 2026-08-15 (Sat), 2026-08-16 (Sun) never appear - no trading
    ])
    assert regime_label_as_of(series, "2026-08-17") == LABEL_BULLISH


def test_no_lookahead_across_holiday_boundary():
    """2026-09-07 is Labor Day (NYSE closed) - no SPY bar for that date.
    An as_of_date of the holiday itself, or the day after, must fall back to
    the most recent PRIOR trading day's label."""
    series = _series([("2026-09-03", LABEL_BEARISH), ("2026-09-04", LABEL_ELEVATED_VOL)])
    assert regime_label_as_of(series, "2026-09-07") == LABEL_ELEVATED_VOL  # Labor Day itself
    assert regime_label_as_of(series, "2026-09-08") == LABEL_ELEVATED_VOL  # day after, still no new bar


# --- §1.4 item 6: report.py/portfolio_simulator.py/report_phase10.py untouched ---


def test_frozen_artifact_generators_never_import_regime_label_as_of():
    for path in ("strategy_lab/report.py", "strategy_lab/portfolio_simulator.py", "strategy_lab/report_phase10.py"):
        with open(path) as f:
            tree = ast.parse(f.read(), filename=path)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_names.update(alias.name for alias in node.names)
        assert "regime_label_as_of" not in imported_names, (
            f"{path} must not import regime_label_as_of - it keeps its own exact-match "
            "compute_historical_regime_series usage unchanged (docs/specs/phase16.md §1.1)"
        )


def test_frozen_artifact_generators_have_zero_git_diff_from_phase16():
    """These three files are explicitly listed as 'untouched' by
    docs/specs/phase16.md §7. Confirm git shows no working-tree changes to
    them (a real diff would mean Phase 16 silently touched a frozen
    artifact generator)."""
    result = subprocess.run(
        ["git", "diff", "--stat", "HEAD", "--",
         "strategy_lab/report.py", "strategy_lab/portfolio_simulator.py", "strategy_lab/report_phase10.py"],
        capture_output=True, text=True, cwd=".",
    )
    assert result.stdout.strip() == "", f"frozen artifact generators must have zero diff: {result.stdout}"


# --- §8 item 4: observation/event immutability - only pre-existing writes ---


def test_prospective_py_contains_only_the_pre_existing_insert_on_conflict_do_nothing_statement():
    with open("strategy_lab/prospective.py") as f:
        content = f.read()
    execute_lines = [l.strip() for l in content.splitlines() if re.search(r"conn\.execute\(|cur\.execute\(", l)]
    assert len(execute_lines) >= 1
    # No UPDATE/DELETE SQL string constant anywhere in the file.
    with open("strategy_lab/prospective.py") as f:
        tree = ast.parse(f.read(), filename="strategy_lab/prospective.py")
    sql_mutation_pattern = re.compile(r"\bUPDATE\s+\S+\s+SET\b|\bDELETE\s+FROM\b", re.IGNORECASE)
    offenders = [
        node.value[:120] for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and sql_mutation_pattern.search(node.value)
    ]
    assert not offenders, f"strategy_lab/prospective.py must contain no UPDATE/DELETE SQL string: {offenders}"
    assert "ON CONFLICT(ticker, observation_date) DO NOTHING" in content


def test_prospective_events_py_contains_only_the_pre_existing_insert_on_conflict_do_nothing_statement():
    with open("strategy_lab/prospective_events.py") as f:
        content = f.read()
    with open("strategy_lab/prospective_events.py") as f:
        tree = ast.parse(f.read(), filename="strategy_lab/prospective_events.py")
    sql_mutation_pattern = re.compile(r"\bUPDATE\s+\S+\s+SET\b|\bDELETE\s+FROM\b", re.IGNORECASE)
    offenders = [
        node.value[:120] for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and sql_mutation_pattern.search(node.value)
    ]
    assert not offenders, f"strategy_lab/prospective_events.py must contain no UPDATE/DELETE SQL string: {offenders}"
    assert "ON CONFLICT(ticker, event_type, event_date) DO NOTHING" in content


def test_no_new_write_path_added_to_prospective_or_prospective_events_by_phase16():
    """grep -n "conn.execute" strategy_lab/prospective.py strategy_lab/prospective_events.py
    (docs/specs/phase16.md §8 item 4) - every conn.execute/cur.execute call in
    both files must be part of the CREATE TABLE / ALTER TABLE / the single
    INSERT...ON CONFLICT DO NOTHING statement, never a bare UPDATE/DELETE."""
    for path in ("strategy_lab/prospective.py", "strategy_lab/prospective_events.py"):
        result = subprocess.run(["grep", "-n", "conn.execute\\|cur.execute", path], capture_output=True, text=True)
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        assert lines, f"expected at least one conn.execute in {path}"
        for line in lines:
            assert not re.search(r"UPDATE\s+\S+\s+SET|DELETE\s+FROM", line, re.IGNORECASE), (
                f"unexpected mutation statement in {path}: {line}"
            )


# --- degrades correctly: None regime never fabricates a bullish entry signal ---


def _seed_price(conn, ticker, d, close, source, volume=1000):
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, d, close, close, close, close, volume, source),
    )
    conn.commit()


def _seed_price_series(conn, ticker, source, start, n, base=100.0, step=0.5):
    d0 = date.fromisoformat(start)
    price = base
    for i in range(n):
        price += step
        _seed_price(conn, ticker, (d0 + timedelta(days=i)).isoformat(), price, source)


def _seed_indicator_inputs(conn, ticker="AAPL", source="alpaca"):
    """Enough price history for signals.engine.score_indicators to produce a
    non-error indicators.ok=True result, independent of the SPY regime
    series entirely."""
    _seed_price_series(conn, ticker, source, "2025-01-01", 260, base=100.0, step=0.2)


def test_build_todays_observation_none_regime_never_fabricates_bullish_entry_signal():
    """When SPY has zero rows (regime_series empty -> regime_label None),
    is_bullish must be False (None == LABEL_BULLISH is False), never
    fabricating a bullish experiment entry signal from missing regime data."""
    conn = make_test_db()
    _seed_indicator_inputs(conn, ticker="AAPL", source="alpaca")
    # No SPY rows at all -> compute_historical_regime_series returns empty -> regime None.
    from strategy_lab.prospective import build_todays_observation
    obs = build_todays_observation(conn, "AAPL", as_of_date="2025-12-31")
    assert obs is not None
    assert obs["regime"] is None
    assert obs["experiment_a_entry_signal"] in (False, 0)
    assert obs["experiment_b_entry_signal"] in (False, 0)


def test_build_todays_observation_uses_point_in_time_regime_not_exact_match():
    """The actual reported incident: SPY's regime series lags the
    observation's own as_of_date by at least one day. build_todays_observation
    must still return a non-None regime (the most recent available, prior
    day's label) rather than None."""
    conn = make_test_db()
    _seed_indicator_inputs(conn, ticker="AAPL", source="alpaca")
    _seed_price_series(conn, "SPY", "alpaca_adjusted", "2025-01-01", 260, base=300.0, step=0.5)

    from strategy_lab.prospective import build_todays_observation
    regime_series = compute_historical_regime_series(conn)
    assert not regime_series.empty
    last_available_date = regime_series.iloc[-1]["date"]
    # as_of_date is one calendar day AFTER the latest available SPY bar -
    # the exact real-world 16:45 ET scenario (today's own bar not fetched yet).
    as_of_date = (date.fromisoformat(last_available_date) + timedelta(days=1)).isoformat()

    obs = build_todays_observation(conn, "AAPL", as_of_date=as_of_date)
    assert obs is not None
    assert obs["regime"] is not None
    assert obs["regime"] == regime_series.iloc[-1]["label"]


# --- §8 item 11 / §8 item 12: duplicate/retry idempotency post-fix ---


def test_build_and_record_twice_for_same_date_produces_identical_regime_and_dedupes():
    conn = make_test_db()
    _seed_indicator_inputs(conn, ticker="AAPL", source="alpaca")
    _seed_price_series(conn, "SPY", "alpaca_adjusted", "2025-01-01", 260, base=300.0, step=0.5)

    from strategy_lab.prospective import build_todays_observation, load_observations, record_observation

    as_of_date = "2025-09-15"
    obs1 = build_todays_observation(conn, "AAPL", as_of_date=as_of_date)
    inserted1 = record_observation(
        conn, observation_date=obs1["observation_date"], ticker=obs1["ticker"], score=obs1["score"],
        stage=obs1["stage"], regime=obs1["regime"], control_entry_signal=obs1["control_entry_signal"],
        experiment_a_entry_signal=obs1["experiment_a_entry_signal"], experiment_b_entry_signal=obs1["experiment_b_entry_signal"],
        adx=obs1["adx"], rsi=obs1["rsi"], macd=obs1["macd"], macd_signal=obs1["macd_signal"],
        volume_ratio=obs1["volume_ratio"], close=obs1["close"], source=obs1["source"],
    )
    assert inserted1 is True

    obs2 = build_todays_observation(conn, "AAPL", as_of_date=as_of_date)
    assert obs2["regime"] == obs1["regime"]  # Area A fix is a pure read - no interaction with dedup
    inserted2 = record_observation(
        conn, observation_date=obs2["observation_date"], ticker=obs2["ticker"], score=obs2["score"],
        stage=obs2["stage"], regime=obs2["regime"], control_entry_signal=obs2["control_entry_signal"],
        experiment_a_entry_signal=obs2["experiment_a_entry_signal"], experiment_b_entry_signal=obs2["experiment_b_entry_signal"],
        adx=obs2["adx"], rsi=obs2["rsi"], macd=obs2["macd"], macd_signal=obs2["macd_signal"],
        volume_ratio=obs2["volume_ratio"], close=obs2["close"], source=obs2["source"],
    )
    assert inserted2 is False, "ON CONFLICT DO NOTHING must dedupe the retry"

    stored = load_observations(conn, ticker="AAPL")
    assert len(stored) == 1
    assert stored.iloc[0]["regime"] == obs1["regime"]  # stored regime unchanged by the retry
