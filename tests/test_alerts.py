"""Tests for the Phase 5 alert engine + Discord delivery.

Two layers, tested separately:
  - alerts/engine.py's determine_alert_reasons(): pure crossing/advancement
    logic, tested directly with hand-picked values (fast, deterministic).
  - alerts/runner.py's run_alert_cycle(): DB-integration tests against a
    throwaway in-memory SQLite database. The "current" indicator snapshot
    is mocked to a fixed, known-strong IndicatorResult (same pattern as
    tests/test_signal_engine.py's make_indicators() fixture) rather than
    relying on synthetic price data happening to produce a high score -
    that turned out to be seed-fragile (RSI easily overshoots into
    overbought territory on a realistic random walk).

All Discord delivery is mocked via unittest.mock.patch on
alerts.discord.requests.post - no test in this file, or anywhere else in
the suite, ever makes a real network call to Discord.
"""
import sqlite3
from unittest.mock import Mock, patch

import pytest

from alerts.config import AlertConfig
from alerts.discord import build_discord_payload, send_discord_alert
from alerts.engine import (
    REASON_SCORE_CROSSING,
    REASON_STAGE_ADVANCE,
    determine_alert_reasons,
    evaluate_ticker,
)
from alerts.runner import run_alert_cycle
from alerts.test_notification import TEST_ALERT_TYPE, TEST_TICKER_LABEL, build_test_payload, send_test_notification
from db.alert_repository import get_alert_state, load_alert_history, upsert_alert_state
from db.schema import init_db
from indicators.technical import IndicatorResult

TEST_CONFIG = AlertConfig(score_threshold=70.0, cooldown_minutes=60)


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_placeholder_price_row(conn, ticker, source="yfinance"):
    """A single row is enough for resolve_source() to find a source name -
    the actual indicator values used by evaluate_ticker are mocked below."""
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, "2024-06-03", 149.0, 151.0, 148.0, 150.0, 2_000_000, source),
    )
    conn.commit()


def strong_indicator_result(ticker, close=150.0):
    """Deterministically produces score=100, all three stages confirmed -
    the same fixture values validated in tests/test_signal_engine.py."""
    return IndicatorResult(
        ticker=ticker, ok=True, latest_date="2024-06-03", close=close,
        rsi=60.0, macd=2.0, macd_signal=1.0, macd_hist=1.0,
        bb_lower=140.0, bb_mid=145.0, bb_upper=150.0,
        adx=30.0, sma_50=140.0, volume=2_000_000.0, volume_avg_20=1_000_000.0, volume_ratio=2.0,
    )


def evaluate_with_strong_signal(conn, ticker, config=TEST_CONFIG):
    with patch("alerts.engine.compute_indicators_for_ticker", return_value=strong_indicator_result(ticker)):
        return evaluate_ticker(conn, ticker, config)


def run_cycle_with_strong_signal(conn, ticker, config=TEST_CONFIG, send=False):
    with patch("alerts.engine.compute_indicators_for_ticker", return_value=strong_indicator_result(ticker)):
        return run_alert_cycle(conn, tickers=[ticker], config=config, send=send)


# --- determine_alert_reasons: pure crossing/advancement logic ---


def test_score_crossing_from_below_to_above_triggers():
    reasons = determine_alert_reasons(65.0, 72.0, "trend", "trend", TEST_CONFIG)
    assert REASON_SCORE_CROSSING in reasons


def test_remaining_above_threshold_does_not_retrigger():
    reasons = determine_alert_reasons(75.0, 80.0, "trend", "trend", TEST_CONFIG)
    assert REASON_SCORE_CROSSING not in reasons


def test_falling_below_threshold_does_not_trigger_upward_crossing():
    reasons = determine_alert_reasons(80.0, 60.0, "trend", "trend", TEST_CONFIG)
    assert REASON_SCORE_CROSSING not in reasons


def test_crossing_again_after_falling_below_can_trigger_new_alert():
    # Step 1: falls below - no alert, but this becomes the new baseline.
    step1 = determine_alert_reasons(80.0, 60.0, "trend", "trend", TEST_CONFIG)
    assert REASON_SCORE_CROSSING not in step1

    # Step 2: crosses back up from that new (below-threshold) baseline.
    step2 = determine_alert_reasons(60.0, 75.0, "trend", "trend", TEST_CONFIG)
    assert REASON_SCORE_CROSSING in step2


def test_stage_advancement_detected():
    reasons = determine_alert_reasons(50.0, 55.0, "trend", "momentum", TEST_CONFIG)
    assert REASON_STAGE_ADVANCE in reasons
    assert REASON_SCORE_CROSSING not in reasons  # both below threshold


def test_stage_downgrade_does_not_trigger_advancement():
    reasons = determine_alert_reasons(50.0, 45.0, "momentum", "trend", TEST_CONFIG)
    assert REASON_STAGE_ADVANCE not in reasons


def test_first_observation_never_alerts():
    assert determine_alert_reasons(None, 95.0, None, "volume", TEST_CONFIG) == []
    assert determine_alert_reasons(None, 95.0, "trend", "volume", TEST_CONFIG) == []
    assert determine_alert_reasons(60.0, 95.0, None, "volume", TEST_CONFIG) == []


def test_both_reasons_can_fire_together():
    reasons = determine_alert_reasons(65.0, 75.0, "trend", "momentum", TEST_CONFIG)
    assert REASON_SCORE_CROSSING in reasons
    assert REASON_STAGE_ADVANCE in reasons


def test_disabled_conditions_are_suppressed_via_config():
    config = AlertConfig(score_threshold=70.0, enable_score_crossing=False, enable_stage_advance=True)
    reasons = determine_alert_reasons(65.0, 75.0, "trend", "trend", config)
    assert reasons == []  # would have crossed, but score_crossing is disabled


# --- evaluate_ticker / run_alert_cycle: DB integration ---


def test_evaluate_ticker_no_data():
    conn = make_test_db()
    ev = evaluate_ticker(conn, "GHOST", TEST_CONFIG)
    assert ev.ok is False


def test_run_alert_cycle_persists_alert_and_updates_state():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "AAA")
    upsert_alert_state(conn, "AAA", score=10.0, stage="none", alerted=False)  # guaranteed below threshold

    results = run_cycle_with_strong_signal(conn, "AAA")
    assert len(results) == 1
    result = results[0]

    assert result.evaluation.should_alert is True
    assert result.fired is True
    assert result.alert_id is not None

    history = load_alert_history(conn)
    assert len(history) == 1
    row = history.iloc[0]
    assert row["ticker"] == "AAA"
    assert row["dry_run"] == 1
    assert row["delivered"] == 0
    assert row["score"] == 100.0
    assert row["previous_score"] == pytest.approx(10.0)

    state = get_alert_state(conn, "AAA")
    assert state["last_score"] == pytest.approx(100.0)


def test_run_alert_cycle_no_repeat_alert_when_state_unchanged():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "BBB")
    upsert_alert_state(conn, "BBB", score=10.0, stage="none", alerted=False)  # seed a below-threshold baseline

    first = run_cycle_with_strong_signal(conn, "BBB")  # crosses 10 -> 100, fires
    second = run_cycle_with_strong_signal(conn, "BBB")  # unchanged (100 -> 100), should not re-fire

    assert first[0].fired is True
    assert second[0].fired is False
    assert len(load_alert_history(conn)) == 1  # only the first run's alert


def test_cooldown_suppresses_duplicate_alert_within_window():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "CCC")

    upsert_alert_state(conn, "CCC", score=10.0, stage="none", alerted=False)
    first = run_cycle_with_strong_signal(conn, "CCC")
    assert first[0].fired is True

    # Re-seed a fresh below-threshold baseline (simulating a genuine new dip
    # and re-cross) but last_alert_at is still very recent -> cooldown
    # should suppress it even though the crossing condition is legitimately
    # met again.
    upsert_alert_state(conn, "CCC", score=10.0, stage="none", alerted=False)
    conn.execute("UPDATE alert_state SET last_alert_at = datetime('now') WHERE ticker = 'CCC'")
    conn.commit()
    second = run_cycle_with_strong_signal(conn, "CCC")

    assert second[0].evaluation.should_alert is True   # the condition IS met
    assert second[0].suppressed_by_cooldown is True
    assert second[0].fired is False
    assert len(load_alert_history(conn)) == 1  # only the first alert was persisted


def test_zero_cooldown_does_not_suppress():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "DDD")
    no_cooldown_config = AlertConfig(score_threshold=70.0, cooldown_minutes=0)

    upsert_alert_state(conn, "DDD", score=10.0, stage="none", alerted=False)
    run_cycle_with_strong_signal(conn, "DDD", config=no_cooldown_config)

    upsert_alert_state(conn, "DDD", score=10.0, stage="none", alerted=False)
    second = run_cycle_with_strong_signal(conn, "DDD", config=no_cooldown_config)

    assert second[0].suppressed_by_cooldown is False
    assert second[0].fired is True


# --- Discord delivery: always mocked ---


def test_dry_run_never_calls_discord():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "EEE")
    upsert_alert_state(conn, "EEE", score=10.0, stage="none", alerted=False)

    with patch("alerts.discord.requests.post") as mock_post:
        results = run_cycle_with_strong_signal(conn, "EEE", send=False)

    assert results[0].fired is True
    mock_post.assert_not_called()


def test_successful_mocked_discord_delivery_marks_delivered():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "FFF")
    upsert_alert_state(conn, "FFF", score=10.0, stage="none", alerted=False)

    fake_response = Mock(status_code=204)
    with patch("alerts.discord.requests.post", return_value=fake_response) as mock_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"):
        results = run_cycle_with_strong_signal(conn, "FFF", send=True)

    mock_post.assert_called_once()
    assert results[0].delivery.ok is True

    row = load_alert_history(conn).iloc[0]
    assert row["dry_run"] == 0
    assert row["delivered"] == 1
    assert row["delivered_at"] is not None
    assert row["delivery_error"] is None


def test_failed_mocked_discord_delivery_marks_failure_visible():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "GGG")
    upsert_alert_state(conn, "GGG", score=10.0, stage="none", alerted=False)

    fake_response = Mock(status_code=400)
    with patch("alerts.discord.requests.post", return_value=fake_response), \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"):
        results = run_cycle_with_strong_signal(conn, "GGG", send=True)

    assert results[0].delivery.ok is False

    row = load_alert_history(conn).iloc[0]
    assert row["dry_run"] == 0
    assert row["delivered"] == 0  # never pretend a failed send succeeded
    assert row["delivery_error"] is not None
    assert "400" in row["delivery_error"]


def test_send_without_webhook_configured_fails_safely():
    with patch("alerts.discord.requests.post") as mock_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", None):
        result = send_discord_alert({"embeds": []})

    assert result.ok is False
    assert "not configured" in result.error
    mock_post.assert_not_called()  # fails before ever attempting a network call


# --- secrets never leak into records or payloads ---


def test_discord_payload_never_contains_webhook_url():
    fake_webhook = "https://discord.com/api/webhooks/123456789/super-secret-token-value"
    conn = make_test_db()
    insert_placeholder_price_row(conn, "HHH")
    upsert_alert_state(conn, "HHH", score=10.0, stage="none", alerted=False)

    with patch("alerts.discord.DISCORD_WEBHOOK_URL", fake_webhook):
        ev = evaluate_with_strong_signal(conn, "HHH")
        assert ev.should_alert is True
        payload = build_discord_payload(ev)

    payload_text = str(payload)
    assert "super-secret-token-value" not in payload_text
    assert "discord.com" not in payload_text


def test_alert_record_never_contains_webhook_url():
    fake_webhook = "https://discord.com/api/webhooks/123456789/super-secret-token-value"
    conn = make_test_db()
    insert_placeholder_price_row(conn, "III")
    upsert_alert_state(conn, "III", score=10.0, stage="none", alerted=False)

    with patch("alerts.discord.DISCORD_WEBHOOK_URL", fake_webhook):
        results = run_cycle_with_strong_signal(conn, "III", send=False)

    assert results[0].fired is True
    history = load_alert_history(conn)
    dump = history.to_string()
    assert "super-secret-token-value" not in dump
    assert "discord.com" not in dump


# --- dedicated test-notification path (alerts/test_notification.py) ---


def test_build_test_payload_is_clearly_labeled_as_test():
    payload = build_test_payload()
    text = str(payload)

    assert "TEST ALERT" in text
    assert "not a real market signal" in text.lower() or "not real" in text.lower()
    assert "not an executed trade" in text.lower() or "no trade" in text.lower()
    # sanity: it must not resemble a real per-ticker alert (no AAPL/MSFT etc.)
    assert "AAPL" not in text
    assert "MSFT" not in text


def test_test_payload_never_contains_webhook_url():
    fake_webhook = "https://discord.com/api/webhooks/123456789/super-secret-token-value"
    with patch("alerts.discord.DISCORD_WEBHOOK_URL", fake_webhook):
        payload = build_test_payload()
    text = str(payload)
    assert "super-secret-token-value" not in text
    assert "discord.com" not in text


def test_send_test_notification_does_not_touch_any_real_ticker_state():
    conn = make_test_db()
    # Seed real-looking state for AAPL/MSFT to prove the test path leaves it alone.
    upsert_alert_state(conn, "AAPL", score=15.0, stage="none", alerted=False)
    upsert_alert_state(conn, "MSFT", score=50.0, stage="trend", alerted=False)

    fake_response = Mock(status_code=204)
    with patch("alerts.discord.requests.post", return_value=fake_response) as mock_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"):
        result = send_test_notification(conn=conn, persist=True)

    mock_post.assert_called_once()
    assert result.ok is True

    # AAPL/MSFT alert_state must be byte-for-byte unchanged.
    aapl_state = get_alert_state(conn, "AAPL")
    msft_state = get_alert_state(conn, "MSFT")
    assert aapl_state["last_score"] == 15.0 and aapl_state["last_stage"] == "none"
    assert msft_state["last_score"] == 50.0 and msft_state["last_stage"] == "trend"

    # No alert_state row was created for the test ticker either.
    assert get_alert_state(conn, TEST_TICKER_LABEL) is None


def test_send_test_notification_persists_under_distinct_test_type():
    conn = make_test_db()
    fake_response = Mock(status_code=204)
    with patch("alerts.discord.requests.post", return_value=fake_response), \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"):
        send_test_notification(conn=conn, persist=True)

    history = load_alert_history(conn)
    assert len(history) == 1
    row = history.iloc[0]
    assert row["ticker"] == TEST_TICKER_LABEL
    assert row["alert_type"] == TEST_ALERT_TYPE
    assert row["alert_type"] not in (REASON_SCORE_CROSSING, REASON_STAGE_ADVANCE)
    assert row["delivered"] == 1
    assert row["dry_run"] == 0


# --- operational failure notification (A11): enabled 2026-08-16, dual-channel ---


def test_operational_alerts_enabled():
    """Flipped on 2026-08-16 after review, closing the gap exposed by the
    2026-08-12 total-ingestion-failure incident (logged but never paged -
    see logs/automation.log)."""
    from alerts.ops_notifications import OPERATIONAL_ALERTS_ENABLED
    assert OPERATIONAL_ALERTS_ENABLED is True


def test_operational_notification_is_noop_when_disabled():
    from alerts.ops_notifications import send_operational_failure_notification
    from datetime import date

    conn = make_test_db()
    with patch("alerts.ops_notifications.requests.post") as mock_post, \
         patch("alerts.email.smtplib.SMTP") as mock_smtp:
        result = send_operational_failure_notification(
            conn, trading_date=date(2026, 8, 12), error_summary="DNS failure", enabled=False,
        )

    mock_post.assert_not_called()
    mock_smtp.assert_not_called()
    assert result.sent is False


def test_operational_notification_sends_when_explicitly_enabled():
    """Discord succeeds; email fails safe (unconfigured in this test) -
    overall `sent` is True because at least one channel delivered."""
    from alerts.ops_notifications import send_operational_failure_notification
    from datetime import date

    conn = make_test_db()
    fake_response = Mock(status_code=204)
    with patch("alerts.ops_notifications.requests.post", return_value=fake_response) as mock_post, \
         patch("alerts.ops_notifications.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"), \
         patch("alerts.email.SMTP_HOST", None):
        result = send_operational_failure_notification(
            conn, trading_date=date(2026, 8, 12), error_summary="DNS failure", enabled=True,
        )

    mock_post.assert_called_once()
    assert result.sent is True
    assert result.discord_sent is True
    assert result.email_sent is False


def test_operational_notification_sends_email_and_discord_independently():
    from alerts.ops_notifications import send_operational_failure_notification
    from datetime import date

    conn = make_test_db()
    mock_server = Mock()
    mock_smtp_cm = Mock()
    mock_smtp_cm.__enter__ = Mock(return_value=mock_server)
    mock_smtp_cm.__exit__ = Mock(return_value=False)
    fake_discord_response = Mock(status_code=204)
    with patch("alerts.ops_notifications.requests.post", return_value=fake_discord_response) as mock_post, \
         patch("alerts.ops_notifications.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"), \
         patch("alerts.email.smtplib.SMTP", return_value=mock_smtp_cm), \
         patch("alerts.email.SMTP_HOST", "smtp.example.com"), \
         patch("alerts.email.SMTP_USERNAME", "user@example.com"), \
         patch("alerts.email.SMTP_PASSWORD", "app-password"), \
         patch("alerts.email.ALERT_EMAIL_FROM", "user@example.com"), \
         patch("alerts.email.ALERT_EMAIL_TO", "me@example.com"):
        result = send_operational_failure_notification(
            conn, trading_date=date(2026, 8, 12), error_summary="DNS failure", enabled=True,
        )

    mock_post.assert_called_once()
    mock_server.send_message.assert_called_once()
    assert result.sent is True
    assert result.email_sent is True
    assert result.discord_sent is True


def test_operational_notification_survives_when_only_email_configured():
    """Discord unconfigured; email succeeds - one working channel is
    enough, same "independent channels" contract as the stock alerts."""
    from alerts.ops_notifications import send_operational_failure_notification
    from datetime import date

    conn = make_test_db()
    mock_server = Mock()
    mock_smtp_cm = Mock()
    mock_smtp_cm.__enter__ = Mock(return_value=mock_server)
    mock_smtp_cm.__exit__ = Mock(return_value=False)
    with patch("alerts.ops_notifications.requests.post") as mock_post, \
         patch("alerts.ops_notifications.DISCORD_WEBHOOK_URL", None), \
         patch("alerts.email.smtplib.SMTP", return_value=mock_smtp_cm), \
         patch("alerts.email.SMTP_HOST", "smtp.example.com"), \
         patch("alerts.email.SMTP_USERNAME", "user@example.com"), \
         patch("alerts.email.SMTP_PASSWORD", "app-password"), \
         patch("alerts.email.ALERT_EMAIL_FROM", "user@example.com"), \
         patch("alerts.email.ALERT_EMAIL_TO", "me@example.com"):
        result = send_operational_failure_notification(
            conn, trading_date=date(2026, 8, 12), error_summary="DNS failure", enabled=True,
        )

    mock_post.assert_not_called()
    assert result.sent is True
    assert result.email_sent is True
    assert result.discord_sent is False
    assert result.discord_error is not None


def test_operational_notification_records_attempt_even_if_both_channels_fail():
    """Neither channel configured: overall `sent` is False, but the
    trading_date is still recorded as attempted - a day that can't reach
    anyone must not be retried in an infinite loop by later failed runs."""
    from alerts.ops_notifications import send_operational_failure_notification
    from datetime import date

    conn = make_test_db()
    with patch("alerts.ops_notifications.DISCORD_WEBHOOK_URL", None), \
         patch("alerts.email.SMTP_HOST", None):
        first = send_operational_failure_notification(conn, trading_date=date(2026, 8, 12), error_summary="DNS failure", enabled=True)
        second = send_operational_failure_notification(conn, trading_date=date(2026, 8, 12), error_summary="DNS failure again", enabled=True)

    assert first.sent is False
    assert "both channels" in first.reason
    assert second.sent is False
    assert "already sent" in second.reason


def test_operational_notification_at_most_once_per_day():
    from alerts.ops_notifications import send_operational_failure_notification
    from datetime import date

    conn = make_test_db()
    fake_response = Mock(status_code=204)
    with patch("alerts.ops_notifications.requests.post", return_value=fake_response) as mock_post, \
         patch("alerts.ops_notifications.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"), \
         patch("alerts.email.SMTP_HOST", None):
        first = send_operational_failure_notification(conn, trading_date=date(2026, 8, 12), error_summary="DNS failure", enabled=True)
        second = send_operational_failure_notification(conn, trading_date=date(2026, 8, 12), error_summary="DNS failure again", enabled=True)

    assert first.sent is True
    assert second.sent is False
    assert "already sent" in second.reason
    mock_post.assert_called_once()


def test_operational_notification_sanitizes_urls_and_secrets():
    from alerts.ops_notifications import sanitize_error_summary
    raw = "failed calling https://data.alpaca.markets/v2/bars?apikey=SECRET123 token=abcxyz"
    sanitized = sanitize_error_summary(raw)
    assert "https://" not in sanitized
    assert "SECRET123" not in sanitized
    assert "abcxyz" not in sanitized


def test_operational_notification_payload_has_no_stock_content():
    from alerts.ops_notifications import build_operational_failure_payload
    payload = build_operational_failure_payload("ingestion failed for all tickers")
    text = str(payload)
    for ticker in ("AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META"):
        assert ticker not in text
    assert "score" not in text.lower()
    assert "stage" not in text.lower()


def test_operational_failure_email_has_no_stock_content():
    from alerts.ops_notifications import build_operational_failure_email_message
    message = build_operational_failure_email_message("ingestion failed for all tickers")
    text = str(message)
    for ticker in ("AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA", "META"):
        assert ticker not in text
    assert "score" not in text.lower()
    assert "stage" not in text.lower()


def test_send_test_notification_sends_exactly_one_request():
    conn = make_test_db()
    fake_response = Mock(status_code=204)
    with patch("alerts.discord.requests.post", return_value=fake_response) as mock_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"):
        send_test_notification(conn=conn, persist=True)

    assert mock_post.call_count == 1
