"""Tests for the Phase 6 automation pipeline (automation/pipeline.py,
automation/lock.py, automation/trading_calendar.py) and run-history
persistence (db/run_history_repository.py).

Discord delivery is always mocked - no test here (or anywhere in the
suite) ever makes a real network call or places a trade. Ingestion sources
are patched with fakes so these tests never call a real market-data API.
"""
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from automation.lock import LockHeldError, acquire_run_lock
from automation.pipeline import run_pipeline
from automation.trading_calendar import is_likely_trading_day
from db.price_alert_config_repository import upsert_price_alert_config
from db.real_holdings_repository import upsert_real_holding
from db.volatility_alert_config_repository import upsert_volatility_alert_config
from db.run_history_repository import (
    STATUS_FAILED,
    STATUS_PARTIAL_FAILURE,
    STATUS_SKIPPED_NON_TRADING_DAY,
    STATUS_SUCCESS,
    load_run_history,
)
from db.schema import init_db
from indicators.technical import IndicatorResult


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_placeholder_price_row(conn, ticker, source="alpaca"):
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, "2024-06-03", 149.0, 151.0, 148.0, 150.0, 2_000_000, source),
    )
    conn.commit()


def strong_indicator_result(ticker):
    return IndicatorResult(
        ticker=ticker, ok=True, latest_date="2024-06-03", close=150.0,
        rsi=60.0, macd=2.0, macd_signal=1.0, macd_hist=1.0,
        bb_lower=140.0, bb_mid=145.0, bb_upper=150.0,
        adx=30.0, sma_50=140.0, volume=2_000_000.0, volume_avg_20=1_000_000.0, volume_ratio=2.0,
    )


def fake_ingest_ticker_factory(rows_by_ticker=None, fail_tickers=None):
    """A fake alpaca_source.ingest_ticker that never touches the network -
    just writes one placeholder price row so downstream evaluation has
    something to read, or raises for tickers named in fail_tickers."""
    fail_tickers = fail_tickers or set()

    def fake_ingest(conn, ticker, days=5, **kwargs):
        if ticker in fail_tickers:
            raise ConnectionError(f"simulated network failure for {ticker}")
        insert_placeholder_price_row(conn, ticker)
        return 1

    return fake_ingest


# --- trading_calendar: backed by pandas_market_calendars (real NYSE calendar) ---


def test_normal_weekdays_are_trading_days():
    assert is_likely_trading_day(date(2026, 8, 10)) is True   # Monday
    assert is_likely_trading_day(date(2026, 8, 11)) is True   # Tuesday
    assert is_likely_trading_day(date(2026, 8, 14)) is True   # Friday


def test_weekend_is_not_trading_day():
    assert is_likely_trading_day(date(2026, 8, 8)) is False   # Saturday
    assert is_likely_trading_day(date(2026, 8, 9)) is False   # Sunday


@pytest.mark.parametrize("holiday_name,holiday_date", [
    ("New Year's Day 2026", date(2026, 1, 1)),
    ("MLK Day 2026", date(2026, 1, 19)),
    ("Presidents Day 2026", date(2026, 2, 16)),
    ("Good Friday 2026", date(2026, 4, 3)),          # not a federal holiday - a real
    ("Memorial Day 2026", date(2026, 5, 25)),          # differentiator vs. a weekday-only
    ("Juneteenth 2026", date(2026, 6, 19)),            # or federal-holiday-based check
    ("Independence Day (observed) 2026", date(2026, 7, 3)),  # July 4 falls on a Sat in 2026
    ("Labor Day 2026", date(2026, 9, 7)),
    ("Thanksgiving 2026", date(2026, 11, 26)),
    ("Christmas 2026", date(2026, 12, 25)),
])
def test_major_nyse_holidays_are_not_trading_days(holiday_name, holiday_date):
    assert is_likely_trading_day(holiday_date) is False, f"{holiday_name} ({holiday_date}) should be a market closure"


def test_day_before_and_after_a_holiday_are_still_trading_days():
    # Thanksgiving 2026 is Nov 26 (Thursday); surrounding weekdays trade normally.
    assert is_likely_trading_day(date(2026, 11, 25)) is True
    assert is_likely_trading_day(date(2026, 11, 27)) is True   # day-after-Thanksgiving is a half day, still open


# --- lock ---


def test_lock_blocks_concurrent_acquisition():
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = str(Path(tmp) / "test.lock")
        with acquire_run_lock(lock_path):
            with pytest.raises(LockHeldError):
                with acquire_run_lock(lock_path):
                    pass  # pragma: no cover


def test_lock_is_reacquirable_after_release():
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = str(Path(tmp) / "test.lock")
        with acquire_run_lock(lock_path):
            pass
        with acquire_run_lock(lock_path):
            pass  # should not raise


# --- run_pipeline: orchestration ---


def test_non_trading_day_is_skipped_and_recorded():
    conn = make_test_db()
    result = run_pipeline(conn, tickers=["AAA"], today=date(2026, 8, 8))  # Saturday

    assert result.status == STATUS_SKIPPED_NON_TRADING_DAY
    assert result.tickers_attempted == 0

    history = load_run_history(conn)
    assert len(history) == 1
    assert history.iloc[0]["status"] == STATUS_SKIPPED_NON_TRADING_DAY


def test_force_run_bypasses_weekend_check():
    conn = make_test_db()
    fake_ingest = fake_ingest_ticker_factory()
    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("automation.pipeline._evaluate_ticker_alerts", side_effect=lambda conn, o, cfg, send: o):
        result = run_pipeline(conn, tickers=["AAA"], today=date(2026, 8, 8), skip_non_trading_day_check=True)

    assert result.status != STATUS_SKIPPED_NON_TRADING_DAY
    assert result.tickers_attempted == 1


def test_force_run_bypasses_holiday_check():
    # Without force-run, a real NYSE holiday is correctly skipped...
    conn = make_test_db()
    thanksgiving = date(2026, 11, 26)
    skipped = run_pipeline(conn, tickers=["AAA"], today=thanksgiving)
    assert skipped.status == STATUS_SKIPPED_NON_TRADING_DAY

    # ...but --force-run (skip_non_trading_day_check=True) runs it anyway.
    fake_ingest = fake_ingest_ticker_factory()
    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("automation.pipeline._evaluate_ticker_alerts", side_effect=lambda conn, o, cfg, send: o):
        forced = run_pipeline(conn, tickers=["AAA"], today=thanksgiving, skip_non_trading_day_check=True)

    assert forced.status != STATUS_SKIPPED_NON_TRADING_DAY
    assert forced.tickers_attempted == 1


def test_successful_multi_ticker_run():
    conn = make_test_db()
    fake_ingest = fake_ingest_ticker_factory()

    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)), \
         patch("alerts.discord.requests.post") as mock_post:
        result = run_pipeline(conn, tickers=["AAA", "BBB", "CCC"], today=date(2026, 8, 10), send=False)

    assert result.status == STATUS_SUCCESS
    assert result.tickers_attempted == 3
    assert result.tickers_updated == 3
    assert result.tickers_failed == 0
    mock_post.assert_not_called()  # dry-run: no Discord calls even though data refreshed


def test_default_run_ingests_watchlist_plus_configured_price_alert_tickers():
    """A ticker with a price-alert threshold but no WATCHLIST membership
    (e.g. added from the dashboard's Price Alert Thresholds page, see
    dashboard/views/price_alert_config.py) must still get ingested on a
    default (no explicit --tickers) automation run, or its price data goes
    stale and the only way to refresh it is a manual, non-WATCHLIST
    ingestion call - exactly what caused the SOURCE_PRIORITY collision on
    NOW earlier. An explicit `tickers=` argument must NOT be unioned - it's
    honored exactly as passed (see test_successful_multi_ticker_run etc.)."""
    conn = make_test_db()
    upsert_price_alert_config(conn, "ZZZ", above=200.0, below=None)  # not in WATCHLIST
    fake_ingest = fake_ingest_ticker_factory()

    with patch("automation.pipeline.WATCHLIST", ["AAA", "BBB"]), \
         patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)), \
         patch("alerts.price_engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)):
        result = run_pipeline(conn, today=date(2026, 8, 10))  # tickers=None -> default union

    assert [o.ticker for o in result.outcomes] == ["AAA", "BBB", "ZZZ"]

    outcomes_by_ticker = {o.ticker: o for o in result.outcomes}
    zzz = outcomes_by_ticker["ZZZ"]
    assert zzz.ingest_ok is True
    assert zzz.ingest_rows == 1
    assert zzz.price_alert_result is not None  # threshold was actually evaluated, not skipped


def test_default_run_ingests_watchlist_plus_configured_volatility_alert_tickers():
    """Same contract as test_default_run_ingests_watchlist_plus_configured_price_alert_tickers,
    for a ticker whose only configuration is a volatility (day-over-day %
    move) threshold - see dashboard/views/volatility_alert_config.py."""
    conn = make_test_db()
    upsert_volatility_alert_config(conn, "YYY", threshold_percent=5.0)  # not in WATCHLIST
    fake_ingest = fake_ingest_ticker_factory()

    with patch("automation.pipeline.WATCHLIST", ["AAA", "BBB"]), \
         patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)):
        result = run_pipeline(conn, today=date(2026, 8, 10))  # tickers=None -> default union

    assert [o.ticker for o in result.outcomes] == ["AAA", "BBB", "YYY"]

    outcomes_by_ticker = {o.ticker: o for o in result.outcomes}
    yyy = outcomes_by_ticker["YYY"]
    assert yyy.ingest_ok is True
    assert yyy.volatility_alert_result is not None  # threshold was actually evaluated, not skipped


def test_default_run_ingests_watchlist_plus_real_holdings_tickers():
    """Same contract as test_default_run_ingests_watchlist_plus_configured_price_alert_tickers,
    for a ticker tracked only via real_holdings (dashboard/views/
    real_holdings.py) - a real position with no alert configured must
    still get its price data refreshed on a default run, or "current
    price" on that page goes stale exactly the way NOW's did before the
    original union fix."""
    conn = make_test_db()
    upsert_real_holding(conn, "WWW", shares=10, cost_basis_total=1000.0)  # not in WATCHLIST
    fake_ingest = fake_ingest_ticker_factory()

    with patch("automation.pipeline.WATCHLIST", ["AAA", "BBB"]), \
         patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)):
        result = run_pipeline(conn, today=date(2026, 8, 10))  # tickers=None -> default union

    assert [o.ticker for o in result.outcomes] == ["AAA", "BBB", "WWW"]

    outcomes_by_ticker = {o.ticker: o for o in result.outcomes}
    www = outcomes_by_ticker["WWW"]
    assert www.ingest_ok is True


def test_volatility_alert_evaluated_alongside_price_alert_for_same_ticker():
    """A ticker can be configured for both alert types at once - both must
    be evaluated independently on the same run."""
    conn = make_test_db()
    upsert_price_alert_config(conn, "ZZZ", above=200.0, below=None)
    upsert_volatility_alert_config(conn, "ZZZ", threshold_percent=5.0)
    fake_ingest = fake_ingest_ticker_factory()

    with patch("automation.pipeline.WATCHLIST", []), \
         patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)), \
         patch("alerts.price_engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)):
        result = run_pipeline(conn, today=date(2026, 8, 10))

    zzz = {o.ticker: o for o in result.outcomes}["ZZZ"]
    assert zzz.price_alert_result is not None
    assert zzz.volatility_alert_result is not None


def test_one_ticker_ingest_failure_does_not_abort_others():
    conn = make_test_db()
    fake_ingest = fake_ingest_ticker_factory(fail_tickers={"BBB"})

    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)):
        result = run_pipeline(conn, tickers=["AAA", "BBB", "CCC"], today=date(2026, 8, 10))

    assert result.status == STATUS_PARTIAL_FAILURE
    assert result.tickers_attempted == 3
    assert result.tickers_updated == 2
    assert result.tickers_failed == 1

    outcomes_by_ticker = {o.ticker: o for o in result.outcomes}
    assert outcomes_by_ticker["BBB"].ingest_ok is False
    assert "simulated network failure" in outcomes_by_ticker["BBB"].ingest_error
    # AAA and CCC still got a real evaluation despite BBB's failure.
    assert outcomes_by_ticker["AAA"].alert_result is not None
    assert outcomes_by_ticker["CCC"].alert_result is not None


def test_all_tickers_failing_marks_run_as_failed():
    conn = make_test_db()
    fake_ingest = fake_ingest_ticker_factory(fail_tickers={"AAA", "BBB"})

    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest):
        result = run_pipeline(conn, tickers=["AAA", "BBB"], today=date(2026, 8, 10))

    assert result.status == STATUS_FAILED
    assert result.tickers_failed == 2

    history = load_run_history(conn)
    assert history.iloc[0]["status"] == STATUS_FAILED
    assert history.iloc[0]["error_summary"] is not None


def test_ticker_evaluation_exception_isolated_from_others():
    conn = make_test_db()
    fake_ingest = fake_ingest_ticker_factory()

    call_count = {"n": 0}

    def flaky_indicators(conn, ticker):
        call_count["n"] += 1
        if ticker == "BBB":
            raise RuntimeError("simulated evaluation crash")
        return strong_indicator_result(ticker)

    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=flaky_indicators):
        result = run_pipeline(conn, tickers=["AAA", "BBB", "CCC"], today=date(2026, 8, 10))

    outcomes_by_ticker = {o.ticker: o for o in result.outcomes}
    assert outcomes_by_ticker["BBB"].evaluation_error is not None
    assert outcomes_by_ticker["BBB"].ingest_ok is True  # ingestion itself succeeded
    # AAA and CCC were still evaluated normally.
    assert outcomes_by_ticker["AAA"].alert_result is not None
    assert outcomes_by_ticker["CCC"].alert_result is not None
    assert result.status == STATUS_PARTIAL_FAILURE


def test_dry_run_never_calls_discord_across_full_pipeline():
    conn = make_test_db()
    fake_ingest = fake_ingest_ticker_factory()

    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)), \
         patch("alerts.discord.requests.post") as mock_post:
        run_pipeline(conn, tickers=["AAA"], today=date(2026, 8, 10), send=False)

    mock_post.assert_not_called()


def test_send_true_still_requires_explicit_flag_and_delivers():
    conn = make_test_db()
    fake_ingest = fake_ingest_ticker_factory()
    fake_response = Mock(status_code=204)

    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)), \
         patch("alerts.discord.requests.post", return_value=fake_response) as mock_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"):
        result = run_pipeline(conn, tickers=["AAA"], today=date(2026, 8, 10), send=True)

    # First observation never alerts (no baseline) - so still no network
    # call on this run, proving send=True alone doesn't force a send;
    # a real crossing is still required.
    mock_post.assert_not_called()
    assert result.alerts_generated == 0


def test_idempotent_rerun_same_day_does_not_duplicate_alerts():
    conn = make_test_db()
    fake_ingest = fake_ingest_ticker_factory()

    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)):
        run_pipeline(conn, tickers=["AAA"], today=date(2026, 8, 10))  # baseline
        run_pipeline(conn, tickers=["AAA"], today=date(2026, 8, 10))  # rerun, unchanged data
        result3 = run_pipeline(conn, tickers=["AAA"], today=date(2026, 8, 10))  # rerun again

    assert result3.alerts_generated == 0
    alerts = pd.read_sql_query("SELECT * FROM alerts", conn)
    assert len(alerts) == 0  # never crossed (score constant across reruns), never duplicated


def test_run_history_persists_start_and_finish_fields():
    conn = make_test_db()
    fake_ingest = fake_ingest_ticker_factory()

    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)):
        run_pipeline(conn, tickers=["AAA", "BBB"], today=date(2026, 8, 10))

    history = load_run_history(conn)
    assert len(history) == 1
    row = history.iloc[0]
    assert row["started_at"] is not None
    assert row["finished_at"] is not None
    assert row["status"] == STATUS_SUCCESS
    assert row["send_mode"] == "dry_run"
    assert row["tickers_attempted"] == 2
    assert row["tickers_updated"] == 2


def test_total_ingestion_failure_evaluates_zero_signals():
    conn = make_test_db()
    fake_ingest = fake_ingest_ticker_factory(fail_tickers={"AAA", "BBB"})

    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest):
        result = run_pipeline(conn, tickers=["AAA", "BBB"], today=date(2026, 8, 10))

    assert all(o.alert_result is None for o in result.outcomes)


def test_total_ingestion_failure_updates_zero_alert_states():
    conn = make_test_db()
    from db.alert_repository import upsert_alert_state, get_alert_state
    upsert_alert_state(conn, "AAA", score=42.0, stage="trend", alerted=False)
    before = dict(get_alert_state(conn, "AAA"))

    fake_ingest = fake_ingest_ticker_factory(fail_tickers={"AAA", "BBB"})
    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest):
        run_pipeline(conn, tickers=["AAA", "BBB"], today=date(2026, 8, 10))

    after = dict(get_alert_state(conn, "AAA"))
    assert after == before  # byte-identical: not even last_checked_at moved


def test_total_ingestion_failure_sends_zero_discord_calls():
    conn = make_test_db()
    fake_ingest = fake_ingest_ticker_factory(fail_tickers={"AAA"})

    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.discord.requests.post") as mock_post:
        run_pipeline(conn, tickers=["AAA"], today=date(2026, 8, 10), send=True)

    mock_post.assert_not_called()


def test_partial_failure_evaluates_only_successful_tickers():
    conn = make_test_db()
    fake_ingest = fake_ingest_ticker_factory(fail_tickers={"BBB"})

    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)):
        result = run_pipeline(conn, tickers=["AAA", "BBB", "CCC"], today=date(2026, 8, 10))

    outcomes_by_ticker = {o.ticker: o for o in result.outcomes}
    assert outcomes_by_ticker["AAA"].alert_result is not None
    assert outcomes_by_ticker["CCC"].alert_result is not None
    assert outcomes_by_ticker["BBB"].alert_result is None  # never evaluated


def test_failed_ticker_alert_state_remains_byte_identical():
    """Reproduces the 2026-08-12 incident: a ticker whose ingestion fails
    must not have its alert_state touched, even though `prices` may still
    hold an older or out-of-band row for it that evaluation could run
    against if it were (wrongly) allowed to."""
    conn = make_test_db()
    from db.alert_repository import upsert_alert_state, get_alert_state
    insert_placeholder_price_row(conn, "AAA")  # data present, but ingestion for THIS run still fails
    upsert_alert_state(conn, "AAA", score=70.0, stage="momentum", alerted=False)
    before = dict(get_alert_state(conn, "AAA"))

    fake_ingest = fake_ingest_ticker_factory(fail_tickers={"AAA"})
    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)):
        result = run_pipeline(conn, tickers=["AAA"], today=date(2026, 8, 10))

    after = dict(get_alert_state(conn, "AAA"))
    assert after == before
    assert result.outcomes[0].alert_result is None
    assert result.status == STATUS_FAILED


def test_failed_ticker_reports_last_known_stored_signal_not_fresh():
    conn = make_test_db()
    from db.alert_repository import upsert_alert_state
    upsert_alert_state(conn, "AAA", score=55.0, stage="trend", alerted=False)

    fake_ingest = fake_ingest_ticker_factory(fail_tickers={"AAA"})
    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest):
        result = run_pipeline(conn, tickers=["AAA"], today=date(2026, 8, 10))

    outcome = result.outcomes[0]
    assert outcome.alert_result is None  # never freshly evaluated
    assert outcome.last_known_score == 55.0
    assert outcome.last_known_stage == "trend"


# --- retry behavior (A6) ---


def test_transient_dns_error_retries_within_configured_bound():
    import requests
    from ingestion.alpaca_source import retry_request

    call_count = {"n": 0}

    def flaky():
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise requests.exceptions.ConnectionError("simulated DNS failure")
        return "ok"

    with patch("time.sleep"):
        result = retry_request(flaky, ticker="AAA", max_retries=2, initial_delay_seconds=0.01)

    assert result == "ok"
    assert call_count["n"] == 3  # 1 initial attempt + 2 retries


def test_retries_stop_after_configured_maximum():
    import requests
    from ingestion.alpaca_source import retry_request

    call_count = {"n": 0}

    def always_fails():
        call_count["n"] += 1
        raise requests.exceptions.ConnectionError("persistent DNS failure")

    with patch("time.sleep"), pytest.raises(requests.exceptions.ConnectionError):
        retry_request(always_fails, ticker="AAA", max_retries=2, initial_delay_seconds=0.01)

    assert call_count["n"] == 3  # 1 initial attempt + 2 retries, then raises


def test_non_retryable_errors_do_not_loop():
    from ingestion.alpaca_source import retry_request

    call_count = {"n": 0}

    def bad_data():
        call_count["n"] += 1
        raise ValueError("no bar data returned")

    with pytest.raises(ValueError):
        retry_request(bad_data, ticker="AAA", max_retries=2, initial_delay_seconds=0.01)

    assert call_count["n"] == 1  # never retried


def test_successful_retry_proceeds_normally():
    import requests
    from ingestion.alpaca_source import retry_request

    call_count = {"n": 0}

    def fails_once_then_ok():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise requests.exceptions.ConnectionError("transient")
        return {"data": "ok"}

    with patch("time.sleep"):
        result = retry_request(fails_once_then_ok, ticker="AAA", max_retries=2, initial_delay_seconds=0.01)

    assert result == {"data": "ok"}
    assert call_count["n"] == 2


# --- recovery (A8/A9) ---


def test_recovery_is_idempotent_and_does_nothing_if_success_already_exists():
    # automation_runs.started_at is always real wall-clock time (datetime('now')
    # in db/run_history_repository.py), independent of run_pipeline's `today`
    # override (which only governs the trading-day check) - so the
    # idempotency check and the establishing run must agree on a real date.
    from automation.recovery import run_recovery

    conn = make_test_db()
    real_today = date.today()
    fake_ingest = fake_ingest_ticker_factory()
    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)):
        run_pipeline(conn, tickers=["AAA"], today=real_today, skip_non_trading_day_check=True)  # establishes a successful run

    recovery = run_recovery(conn, tickers=["AAA"], today=real_today)
    assert recovery.ran is False
    assert "already exists" in recovery.reason


def test_recovery_retries_when_todays_run_failed():
    from automation.recovery import run_recovery

    conn = make_test_db()
    real_today = date.today()
    fake_ingest = fake_ingest_ticker_factory(fail_tickers={"AAA"})
    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest):
        run_pipeline(conn, tickers=["AAA"], today=real_today, skip_non_trading_day_check=True)  # fails

    good_ingest = fake_ingest_ticker_factory()
    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=good_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)):
        recovery = run_recovery(conn, tickers=["AAA"], today=real_today)

    assert recovery.ran is True
    assert recovery.pipeline_result.status == STATUS_SUCCESS


def test_dry_run_recovery_preview_cannot_consume_or_mask_a_future_real_alert():
    """The core A9 requirement: previewing what WOULD fire must not itself
    consume the crossing, so a subsequent real send can still fire for real."""
    from alerts.runner import run_alert_cycle
    from db.alert_repository import get_alert_state, upsert_alert_state

    conn = make_test_db()
    insert_placeholder_price_row(conn, "AAA")
    upsert_alert_state(conn, "AAA", score=10.0, stage="none", alerted=False)  # below-threshold baseline
    before = dict(get_alert_state(conn, "AAA"))

    with patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)):
        preview_results = run_alert_cycle(conn, tickers=["AAA"], send=False, persist=False)

    assert preview_results[0].evaluation.should_alert is True
    assert preview_results[0].fired is True  # preview correctly reports it WOULD fire

    after_preview = dict(get_alert_state(conn, "AAA"))
    assert after_preview == before  # but nothing was actually written
    assert len(pd.read_sql_query("SELECT * FROM alerts", conn)) == 0  # no alert row persisted

    # A subsequent REAL run against the same unchanged baseline still fires for real.
    with patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)):
        real_results = run_alert_cycle(conn, tickers=["AAA"], send=False, persist=True)

    assert real_results[0].fired is True
    assert len(pd.read_sql_query("SELECT * FROM alerts", conn)) == 1


def test_no_credentials_in_run_history_or_alert_records():
    conn = make_test_db()
    fake_webhook = "https://discord.com/api/webhooks/999/top-secret-value"
    fake_ingest = fake_ingest_ticker_factory()

    with patch("automation.pipeline.alpaca_source.ingest_ticker", side_effect=fake_ingest), \
         patch("alerts.engine.compute_indicators_for_ticker", side_effect=lambda conn, t: strong_indicator_result(t)), \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", fake_webhook):
        run_pipeline(conn, tickers=["AAA"], today=date(2026, 8, 10))

    history_dump = load_run_history(conn).to_string()
    alerts_dump = pd.read_sql_query("SELECT * FROM alerts", conn).to_string()
    assert "top-secret-value" not in history_dump
    assert "top-secret-value" not in alerts_dump
