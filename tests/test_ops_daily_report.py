"""Tests for Phase 12 Component A: ops/daily_report.py (+ persistence in
ops/daily_report_repository.py).

build_daily_report is required to be READ-ONLY and to never raise, even
when Alpaca is unreachable / unconfigured or no prior automation/research
runs exist. All Alpaca access here is mocked - no real network call.
"""
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from db.schema import init_db
from ops.daily_report import (
    _build_market_regime,
    _build_prospective_regime_freshness,
    build_daily_report,
    render_report_text,
    report_to_dict,
)
from ops.daily_report_repository import ensure_schema as ensure_report_schema
from ops.daily_report_repository import load_latest_report, load_report, upsert_report
from strategy_lab.prospective import record_observation


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _raise_get_client(*_a, **_kw):
    raise RuntimeError("no test Alpaca credentials configured")


# --- determinism ---

def test_build_daily_report_deterministic_given_same_db_state(monkeypatch):
    conn = make_test_db()
    monkeypatch.setattr("trading.client.get_client", _raise_get_client)

    today = date(2024, 6, 20)
    report1 = build_daily_report(conn, today=today)
    report2 = build_daily_report(conn, today=today)

    d1 = report_to_dict(report1)
    d2 = report_to_dict(report2)
    d1.pop("generated_at")
    d2.pop("generated_at")
    assert d1 == d2


# --- exit-condition display flag, never acted on ---

def test_build_daily_report_flags_exit_condition_without_acting(monkeypatch):
    conn = make_test_db()

    fake_position = SimpleNamespace(
        symbol="AMZN", qty=5.0, avg_entry_price=100.0, current_price=110.0,
        market_value=550.0, unrealized_pl=50.0, unrealized_plpc=0.10,
    )
    fake_account = SimpleNamespace(equity=10_000.0, cash=5_000.0, buying_power=5_000.0)

    client = MagicMock()
    client.get_account.return_value = fake_account
    client.get_all_positions.return_value = [fake_position]

    monkeypatch.setattr("trading.client.get_client", lambda: client)
    monkeypatch.setattr("trading.client.verify_paper_environment", lambda c=None: None)

    # Force a deterministic, exit-qualifying signal state (stage below the
    # exit floor AND score at/below the exit threshold) without depending
    # on real indicator computation over synthetic price history.
    from signals.engine import SignalScore
    fake_score = SignalScore(ticker="AMZN", ok=True, score=20.0, highest_confirmed_stage="none")
    monkeypatch.setattr("trading.portfolio.compute_indicators_for_ticker", lambda conn, ticker: SimpleNamespace(ok=True))
    monkeypatch.setattr("trading.portfolio.score_indicators", lambda indicators: fake_score)

    report = build_daily_report(conn, today=date(2024, 6, 20))

    assert report.paper_portfolio.ok is True
    assert len(report.paper_portfolio.positions) == 1
    pos = report.paper_portfolio.positions[0]
    assert pos.ticker == "AMZN"
    assert pos.exit_condition_met is True
    assert pos.exit_condition_detail  # non-empty explanatory string

    # The report only ever sets a display flag - it must never act on it.
    client.submit_order.assert_not_called()
    client.cancel_order_by_id.assert_not_called()
    client.replace_order_by_id.assert_not_called()
    client.close_position.assert_not_called()
    client.close_all_positions.assert_not_called()


# --- edge cases: no prior history, Alpaca unreachable ---

def test_build_daily_report_handles_no_automation_runs_yet(monkeypatch):
    conn = make_test_db()
    monkeypatch.setattr("trading.client.get_client", _raise_get_client)

    report = build_daily_report(conn, today=date(2024, 6, 20))

    assert report.production_health.status is None
    assert report.production_health.tickers_failed == 0
    assert report.production_health.history == []
    assert report.research_job.latest_status is None


def test_build_daily_report_handles_alpaca_unreachable(monkeypatch):
    conn = make_test_db()
    monkeypatch.setattr("trading.client.get_client", _raise_get_client)

    report = build_daily_report(conn, today=date(2024, 6, 20))  # must not raise

    assert report.paper_portfolio.ok is False
    assert report.paper_portfolio.reason is not None
    assert "no test Alpaca credentials configured" in report.paper_portfolio.reason
    assert report.paper_portfolio.positions == []
    assert report.paper_portfolio.equity is None


# --- report_to_dict / persistence round trip (Component A repository) ---

def test_report_to_dict_json_serializable_and_round_trips_through_repository(monkeypatch):
    import json
    conn = make_test_db()
    monkeypatch.setattr("trading.client.get_client", _raise_get_client)

    report = build_daily_report(conn, today=date(2024, 6, 20))
    payload = json.dumps(report_to_dict(report), default=str)

    row_id = upsert_report(conn, report.report_date, payload)
    assert row_id is not None
    loaded = load_report(conn, report.report_date)
    assert loaded["report_date"] == report.report_date
    assert load_latest_report(conn)["report_date"] == report.report_date


def test_upsert_report_same_day_rerun_overwrites_only_that_day(monkeypatch):
    conn = make_test_db()
    ensure_report_schema(conn)
    upsert_report(conn, "2024-06-19", '{"report_date": "2024-06-19", "v": 1}')
    upsert_report(conn, "2024-06-20", '{"report_date": "2024-06-20", "v": 1}')
    upsert_report(conn, "2024-06-20", '{"report_date": "2024-06-20", "v": 2}')  # re-run same day

    assert load_report(conn, "2024-06-19")["v"] == 1  # untouched
    assert load_report(conn, "2024-06-20")["v"] == 2  # overwritten in place
    count = conn.execute("SELECT COUNT(*) as n FROM ops_daily_reports").fetchone()["n"]
    assert count == 2  # no extra row created for the re-run


# =================== Phase 17 §4.6 item 1: market_regime.as_of_date/.benchmark
# populated from a real compute_market_regime call; is_stale_vs_report_date
# table-driven over as_of_date == today / < today / regime unavailable ===================


def _seed_spy_series(conn, start, n, base=300.0, step=0.5):
    d0 = date.fromisoformat(start)
    price = base
    for i in range(n):
        price += step
        d = (d0 + timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
            ("SPY", d, price, price, price, price, 1000, "alpaca"),
        )
    conn.commit()


def test_market_regime_section_as_of_date_and_benchmark_populated_and_not_stale_when_current():
    conn = make_test_db()
    today = date(2026, 8, 14)
    # 220 daily rows ending exactly on `today` - real compute_market_regime call.
    _seed_spy_series(conn, (today - timedelta(days=219)).isoformat(), 220)

    section = _build_market_regime(conn, today)
    assert section.ok is True
    assert section.benchmark == "SPY"
    assert section.as_of_date == today.isoformat()
    assert section.is_stale_vs_report_date is False


def test_market_regime_section_is_stale_true_when_as_of_date_lags_report_date():
    conn = make_test_db()
    latest_spy_date = date(2026, 8, 10)
    report_date = date(2026, 8, 14)  # 4 calendar days after SPY's latest bar
    _seed_spy_series(conn, (latest_spy_date - timedelta(days=219)).isoformat(), 220)

    section = _build_market_regime(conn, report_date)
    assert section.ok is True
    assert section.as_of_date == latest_spy_date.isoformat()
    assert section.is_stale_vs_report_date is True


def test_market_regime_section_is_stale_none_not_false_when_regime_unavailable():
    conn = make_test_db()
    today = date(2026, 8, 14)
    # Zero SPY history -> compute_market_regime returns ok=False.
    section = _build_market_regime(conn, today)
    assert section.ok is False
    assert section.is_stale_vs_report_date is None  # explicitly None, never fabricated False


# =================== Phase 17 §4.6 item 2: _build_prospective_regime_freshness ===================


def test_build_prospective_regime_freshness_no_observations_at_all():
    conn = make_test_db()
    section = _build_prospective_regime_freshness(conn, date(2026, 8, 14))
    assert section.ok is False
    assert "no prospective observations recorded yet" in section.reason


def test_build_prospective_regime_freshness_observations_exist_but_none_for_today():
    conn = make_test_db()
    record_observation(
        conn, observation_date="2026-08-13", ticker="AAA", score=80.0, stage="volume", regime="bullish_trend",
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=100.0,
        source="alpaca_adjusted", regime_benchmark="SPY", regime_benchmark_source="alpaca_adjusted",
        regime_benchmark_data_date="2026-08-13", regime_freshness_status="FRESH",
    )
    section = _build_prospective_regime_freshness(conn, date(2026, 8, 14))
    assert section.ok is False
    assert "2026-08-14" in section.reason


def test_build_prospective_regime_freshness_real_observation_for_today_ok_true_fields_correct():
    conn = make_test_db()
    today = date(2026, 8, 14)
    record_observation(
        conn, observation_date=today.isoformat(), ticker="AAA", score=80.0, stage="volume", regime="bearish_trend",
        control_entry_signal=False, experiment_a_entry_signal=False, experiment_b_entry_signal=False,
        adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=100.0,
        source="alpaca_adjusted", regime_benchmark="SPY", regime_benchmark_source="alpaca_adjusted",
        regime_benchmark_data_date="2026-08-13", regime_freshness_status="STALE",
    )
    section = _build_prospective_regime_freshness(conn, today)
    assert section.ok is True
    assert section.reason is None
    assert section.regime_benchmark == "SPY"
    assert section.regime_benchmark_data_date == "2026-08-13"
    assert section.regime_freshness_status == "STALE"


# =================== Phase 17 §4.6 item 4: render_report_text includes both new sections ===================


def test_render_report_text_includes_market_regime_live_and_prospective_freshness_sections(monkeypatch):
    conn = make_test_db()
    monkeypatch.setattr("trading.client.get_client", _raise_get_client)
    today = date(2026, 8, 14)
    _seed_spy_series(conn, (today - timedelta(days=219)).isoformat(), 220)
    record_observation(
        conn, observation_date=today.isoformat(), ticker="AAA", score=80.0, stage="volume", regime="bullish_trend",
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=100.0,
        source="alpaca_adjusted", regime_benchmark="SPY", regime_benchmark_source="alpaca_adjusted",
        regime_benchmark_data_date=today.isoformat(), regime_freshness_status="FRESH",
    )

    report = build_daily_report(conn, today=today)
    text = render_report_text(report)

    assert "Market Regime (live)" in text
    assert "Prospective Regime Freshness" in text
    assert "Benchmark: SPY" in text
    assert "Status: FRESH" in text


def test_render_report_text_prospective_freshness_unavailable_path_renders_reason(monkeypatch):
    conn = make_test_db()
    monkeypatch.setattr("trading.client.get_client", _raise_get_client)
    report = build_daily_report(conn, today=date(2026, 8, 14))
    text = render_report_text(report)
    assert "Prospective Regime Freshness" in text
    assert "Unavailable:" in text


# =================== Phase 17 §4.6 item 5: report_to_dict includes the new nested field ===================


def test_report_to_dict_includes_prospective_regime_freshness_nested_field(monkeypatch):
    conn = make_test_db()
    monkeypatch.setattr("trading.client.get_client", _raise_get_client)
    today = date(2026, 8, 14)
    record_observation(
        conn, observation_date=today.isoformat(), ticker="AAA", score=80.0, stage="volume", regime="bullish_trend",
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=100.0,
        source="alpaca_adjusted", regime_benchmark="SPY", regime_benchmark_source="alpaca_adjusted",
        regime_benchmark_data_date=today.isoformat(), regime_freshness_status="FRESH",
    )
    report = build_daily_report(conn, today=today)
    d = report_to_dict(report)
    assert "prospective_regime_freshness" in d
    assert d["prospective_regime_freshness"]["ok"] is True
    assert d["prospective_regime_freshness"]["regime_benchmark"] == "SPY"
    assert d["prospective_regime_freshness"]["regime_freshness_status"] == "FRESH"
    assert "as_of_date" in d["market_regime"]
    assert "benchmark" in d["market_regime"]
    assert "is_stale_vs_report_date" in d["market_regime"]

    import json
    json.dumps(d, default=str)  # must still be JSON serializable end-to-end
