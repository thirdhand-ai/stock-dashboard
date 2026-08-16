"""Tests for the price-threshold alert engine + dual-channel delivery
(email, Discord). Mirrors tests/test_alerts.py's structure exactly, adapted
to the price signal/two-channel delivery.

Two layers, tested separately:
  - alerts/price_engine.py's determine_price_alert_reasons(): pure crossing
    logic, tested directly with hand-picked values (fast, deterministic).
  - alerts/price_runner.py's run_price_alert_cycle(): DB-integration tests
    against a throwaway in-memory SQLite database. The "current" indicator
    snapshot is mocked to a fixed, known close price (same pattern
    tests/test_alerts.py uses) rather than relying on synthetic price data.

All email delivery is mocked via unittest.mock.patch on
alerts.email.smtplib.SMTP; all Discord delivery is mocked via
unittest.mock.patch on alerts.discord.requests.post - no test in this file
ever opens a real SMTP connection or makes a real network call to Discord.
"""
import sqlite3
from unittest.mock import MagicMock, Mock, patch

import pytest

from alerts.discord import build_price_alert_discord_payload, send_discord_alert
from alerts.email import send_email_alert
from alerts.price_config import PriceAlertConfig, PriceThreshold
from alerts.price_engine import (
    REASON_PRICE_ABOVE,
    REASON_PRICE_BELOW,
    determine_price_alert_reasons,
    evaluate_ticker_price,
)
from alerts.price_runner import run_price_alert_cycle
from db.price_alert_config_repository import upsert_price_alert_config
from db.price_alert_repository import get_price_alert_state, load_price_alert_history, upsert_price_alert_state
from db.schema import init_db
from indicators.technical import IndicatorResult

TEST_CONFIG = PriceAlertConfig(cooldown_minutes=60)


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_placeholder_price_row(conn, ticker, source="yfinance"):
    """A single row is enough for resolve_source() to find a source name -
    the actual close value used by evaluate_ticker_price is mocked below."""
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, "2024-06-03", 149.0, 151.0, 148.0, 150.0, 2_000_000, source),
    )
    conn.commit()


def indicator_result_at_close(ticker, close):
    return IndicatorResult(ticker=ticker, ok=True, latest_date="2024-06-03", close=close)


def evaluate_at_price(conn, ticker, threshold, close):
    with patch("alerts.price_engine.compute_indicators_for_ticker", return_value=indicator_result_at_close(ticker, close)):
        return evaluate_ticker_price(conn, ticker, threshold)


def run_cycle_at_price(conn, ticker, threshold, close, config=TEST_CONFIG, send=False):
    with patch("alerts.price_engine.compute_indicators_for_ticker", return_value=indicator_result_at_close(ticker, close)):
        return run_price_alert_cycle(conn, thresholds=[threshold], config=config, send=send)


# --- determine_price_alert_reasons: pure crossing logic ---


def test_crossing_above_threshold_triggers():
    threshold = PriceThreshold(ticker="AAA", above=200.0)
    reasons = determine_price_alert_reasons(195.0, 205.0, threshold)
    assert REASON_PRICE_ABOVE in reasons


def test_remaining_above_threshold_does_not_retrigger():
    threshold = PriceThreshold(ticker="AAA", above=200.0)
    reasons = determine_price_alert_reasons(210.0, 215.0, threshold)
    assert REASON_PRICE_ABOVE not in reasons


def test_falling_below_upper_threshold_does_not_trigger_upward_crossing():
    threshold = PriceThreshold(ticker="AAA", above=200.0)
    reasons = determine_price_alert_reasons(215.0, 190.0, threshold)
    assert REASON_PRICE_ABOVE not in reasons


def test_crossing_below_threshold_triggers():
    threshold = PriceThreshold(ticker="AAA", below=180.0)
    reasons = determine_price_alert_reasons(185.0, 175.0, threshold)
    assert REASON_PRICE_BELOW in reasons


def test_remaining_below_threshold_does_not_retrigger():
    threshold = PriceThreshold(ticker="AAA", below=180.0)
    reasons = determine_price_alert_reasons(170.0, 165.0, threshold)
    assert REASON_PRICE_BELOW not in reasons


def test_both_directions_configured_independently():
    threshold = PriceThreshold(ticker="AAA", above=200.0, below=180.0)
    up_reasons = determine_price_alert_reasons(195.0, 205.0, threshold)
    down_reasons = determine_price_alert_reasons(185.0, 175.0, threshold)
    assert up_reasons == [REASON_PRICE_ABOVE]
    assert down_reasons == [REASON_PRICE_BELOW]


def test_first_observation_never_alerts():
    threshold = PriceThreshold(ticker="AAA", above=200.0, below=180.0)
    assert determine_price_alert_reasons(None, 250.0, threshold) == []
    assert determine_price_alert_reasons(None, 100.0, threshold) == []


def test_crossing_again_after_falling_below_can_trigger_new_alert():
    threshold = PriceThreshold(ticker="AAA", above=200.0)
    step1 = determine_price_alert_reasons(210.0, 190.0, threshold)  # falls below - new baseline, no alert
    assert REASON_PRICE_ABOVE not in step1
    step2 = determine_price_alert_reasons(190.0, 205.0, threshold)  # crosses back up
    assert REASON_PRICE_ABOVE in step2


# --- evaluate_ticker_price / run_price_alert_cycle: DB integration ---


def test_evaluate_ticker_price_no_data():
    conn = make_test_db()
    threshold = PriceThreshold(ticker="GHOST", above=200.0)
    ev = evaluate_ticker_price(conn, "GHOST", threshold)
    assert ev.ok is False


def test_run_price_alert_cycle_persists_alert_and_updates_state():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "AAA")
    threshold = PriceThreshold(ticker="AAA", above=200.0)
    upsert_price_alert_state(conn, "AAA", price=150.0, alerted=False)  # below threshold baseline

    results = run_cycle_at_price(conn, "AAA", threshold, close=210.0)
    assert len(results) == 1
    result = results[0]

    assert result.evaluation.should_alert is True
    assert result.fired is True
    assert result.alert_id is not None

    history = load_price_alert_history(conn)
    assert len(history) == 1
    row = history.iloc[0]
    assert row["ticker"] == "AAA"
    assert row["dry_run"] == 1
    assert row["delivered"] == 0
    assert row["price"] == pytest.approx(210.0)
    assert row["previous_price"] == pytest.approx(150.0)
    assert row["threshold"] == pytest.approx(200.0)

    state = get_price_alert_state(conn, "AAA")
    assert state["last_price"] == pytest.approx(210.0)


def test_run_price_alert_cycle_no_repeat_alert_when_state_unchanged():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "BBB")
    threshold = PriceThreshold(ticker="BBB", above=200.0)
    upsert_price_alert_state(conn, "BBB", price=150.0, alerted=False)

    first = run_cycle_at_price(conn, "BBB", threshold, close=210.0)   # crosses, fires
    second = run_cycle_at_price(conn, "BBB", threshold, close=210.0)  # unchanged, should not re-fire

    assert first[0].fired is True
    assert second[0].fired is False
    assert len(load_price_alert_history(conn)) == 1


def test_cooldown_suppresses_duplicate_alert_within_window():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "CCC")
    threshold = PriceThreshold(ticker="CCC", above=200.0)

    upsert_price_alert_state(conn, "CCC", price=150.0, alerted=False)
    first = run_cycle_at_price(conn, "CCC", threshold, close=210.0)
    assert first[0].fired is True

    upsert_price_alert_state(conn, "CCC", price=150.0, alerted=False)
    conn.execute("UPDATE price_alert_state SET last_alert_at = datetime('now') WHERE ticker = 'CCC'")
    conn.commit()
    second = run_cycle_at_price(conn, "CCC", threshold, close=210.0)

    assert second[0].evaluation.should_alert is True
    assert second[0].suppressed_by_cooldown is True
    assert second[0].fired is False
    assert len(load_price_alert_history(conn)) == 1


def test_zero_cooldown_does_not_suppress():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "DDD")
    threshold = PriceThreshold(ticker="DDD", above=200.0)
    no_cooldown_config = PriceAlertConfig(cooldown_minutes=0)

    upsert_price_alert_state(conn, "DDD", price=150.0, alerted=False)
    run_cycle_at_price(conn, "DDD", threshold, close=210.0, config=no_cooldown_config)

    upsert_price_alert_state(conn, "DDD", price=150.0, alerted=False)
    second = run_cycle_at_price(conn, "DDD", threshold, close=210.0, config=no_cooldown_config)

    assert second[0].suppressed_by_cooldown is False
    assert second[0].fired is True


def test_ticker_with_no_configured_threshold_is_never_evaluated():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "EEE")
    results = run_price_alert_cycle(conn, thresholds=[], send=False)
    assert results == []


# --- Email delivery: always mocked ---


def test_dry_run_never_calls_smtp():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "FFF")
    threshold = PriceThreshold(ticker="FFF", above=200.0)
    upsert_price_alert_state(conn, "FFF", price=150.0, alerted=False)

    with patch("alerts.email.smtplib.SMTP") as mock_smtp:
        results = run_cycle_at_price(conn, "FFF", threshold, close=210.0, send=False)

    assert results[0].fired is True
    mock_smtp.assert_not_called()


def test_successful_mocked_email_delivery_marks_delivered():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "GGG")
    threshold = PriceThreshold(ticker="GGG", above=200.0)
    upsert_price_alert_state(conn, "GGG", price=150.0, alerted=False)

    mock_server = MagicMock()
    mock_smtp_cm = MagicMock()
    mock_smtp_cm.__enter__.return_value = mock_server
    with patch("alerts.email.smtplib.SMTP", return_value=mock_smtp_cm), \
         patch("alerts.email.SMTP_HOST", "smtp.example.com"), \
         patch("alerts.email.SMTP_USERNAME", "user@example.com"), \
         patch("alerts.email.SMTP_PASSWORD", "app-password"), \
         patch("alerts.email.ALERT_EMAIL_FROM", "user@example.com"), \
         patch("alerts.email.ALERT_EMAIL_TO", "me@example.com"), \
         patch("alerts.discord.requests.post") as mock_discord_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", None):  # this test is email-only; Discord must never be hit
        results = run_cycle_at_price(conn, "GGG", threshold, close=210.0, send=True)

    mock_server.send_message.assert_called_once()
    mock_discord_post.assert_not_called()
    assert results[0].delivery.ok is True

    row = load_price_alert_history(conn).iloc[0]
    assert row["dry_run"] == 0
    assert row["delivered"] == 1
    assert row["delivered_at"] is not None
    assert row["delivery_error"] is None


def test_failed_mocked_email_delivery_marks_failure_visible():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "HHH")
    threshold = PriceThreshold(ticker="HHH", above=200.0)
    upsert_price_alert_state(conn, "HHH", price=150.0, alerted=False)

    import smtplib as smtplib_module

    with patch("alerts.email.smtplib.SMTP", side_effect=smtplib_module.SMTPException("boom")), \
         patch("alerts.email.SMTP_HOST", "smtp.example.com"), \
         patch("alerts.email.SMTP_USERNAME", "user@example.com"), \
         patch("alerts.email.SMTP_PASSWORD", "app-password"), \
         patch("alerts.email.ALERT_EMAIL_FROM", "user@example.com"), \
         patch("alerts.email.ALERT_EMAIL_TO", "me@example.com"), \
         patch("alerts.discord.requests.post") as mock_discord_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", None):  # this test is email-only; Discord must never be hit
        results = run_cycle_at_price(conn, "HHH", threshold, close=210.0, send=True)

    mock_discord_post.assert_not_called()
    assert results[0].delivery.ok is False

    row = load_price_alert_history(conn).iloc[0]
    assert row["dry_run"] == 0
    assert row["delivered"] == 0  # never pretend a failed send succeeded
    assert row["delivery_error"] is not None


def test_send_without_smtp_configured_fails_safely():
    with patch("alerts.email.smtplib.SMTP") as mock_smtp, \
         patch("alerts.email.SMTP_HOST", None):
        from email.message import EmailMessage
        result = send_email_alert(EmailMessage())

    assert result.ok is False
    assert "not" in result.error.lower() or "configured" in result.error.lower()
    mock_smtp.assert_not_called()  # fails before ever attempting a network call


# --- Discord delivery: always mocked ---


def test_dry_run_never_calls_discord_webhook():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "JJJ")
    threshold = PriceThreshold(ticker="JJJ", above=200.0)
    upsert_price_alert_state(conn, "JJJ", price=150.0, alerted=False)

    with patch("alerts.discord.requests.post") as mock_post:
        results = run_cycle_at_price(conn, "JJJ", threshold, close=210.0, send=False)

    assert results[0].fired is True
    mock_post.assert_not_called()


def test_successful_mocked_discord_delivery_marks_delivered():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "KKK")
    threshold = PriceThreshold(ticker="KKK", above=200.0)
    upsert_price_alert_state(conn, "KKK", price=150.0, alerted=False)

    fake_response = Mock(status_code=204)
    with patch("alerts.discord.requests.post", return_value=fake_response) as mock_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"), \
         patch("alerts.email.smtplib.SMTP"):
        results = run_cycle_at_price(conn, "KKK", threshold, close=210.0, send=True)

    mock_post.assert_called_once()
    assert results[0].discord_delivery.ok is True

    row = load_price_alert_history(conn).iloc[0]
    assert row["dry_run"] == 0
    assert row["discord_delivered"] == 1
    assert row["discord_delivered_at"] is not None
    assert row["discord_delivery_error"] is None


def test_failed_mocked_discord_delivery_marks_failure_visible():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "LLL")
    threshold = PriceThreshold(ticker="LLL", above=200.0)
    upsert_price_alert_state(conn, "LLL", price=150.0, alerted=False)

    fake_response = Mock(status_code=400)
    with patch("alerts.discord.requests.post", return_value=fake_response), \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"), \
         patch("alerts.email.smtplib.SMTP"):
        results = run_cycle_at_price(conn, "LLL", threshold, close=210.0, send=True)

    assert results[0].discord_delivery.ok is False

    row = load_price_alert_history(conn).iloc[0]
    assert row["dry_run"] == 0
    assert row["discord_delivered"] == 0  # never pretend a failed post succeeded
    assert row["discord_delivery_error"] is not None
    assert "400" in row["discord_delivery_error"]


def test_send_without_webhook_configured_fails_safely():
    with patch("alerts.discord.requests.post") as mock_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", None):
        result = send_discord_alert({"embeds": []})

    assert result.ok is False
    assert "not configured" in result.error
    mock_post.assert_not_called()  # fails before ever attempting a network call


def test_email_and_discord_deliver_independently_on_the_same_alert():
    """Both channels are attempted on every real send, and one channel's
    failure never blocks or is masked by the other's outcome."""
    conn = make_test_db()
    insert_placeholder_price_row(conn, "MMM")
    threshold = PriceThreshold(ticker="MMM", above=200.0)
    upsert_price_alert_state(conn, "MMM", price=150.0, alerted=False)

    mock_server = MagicMock()
    mock_smtp_cm = MagicMock()
    mock_smtp_cm.__enter__.return_value = mock_server
    fake_discord_response = Mock(status_code=400)  # Discord fails...
    with patch("alerts.email.smtplib.SMTP", return_value=mock_smtp_cm), \
         patch("alerts.email.SMTP_HOST", "smtp.example.com"), \
         patch("alerts.email.SMTP_USERNAME", "user@example.com"), \
         patch("alerts.email.SMTP_PASSWORD", "app-password"), \
         patch("alerts.email.ALERT_EMAIL_FROM", "user@example.com"), \
         patch("alerts.email.ALERT_EMAIL_TO", "me@example.com"), \
         patch("alerts.discord.requests.post", return_value=fake_discord_response), \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"):
        results = run_cycle_at_price(conn, "MMM", threshold, close=210.0, send=True)

    mock_server.send_message.assert_called_once()  # ...email still attempted regardless
    assert results[0].delivery.ok is True
    assert results[0].discord_delivery.ok is False

    row = load_price_alert_history(conn).iloc[0]
    assert row["delivered"] == 1
    assert row["discord_delivered"] == 0
    assert row["discord_delivery_error"] is not None


# --- secrets never leak into records or payloads ---


def test_alert_record_never_contains_smtp_password():
    conn = make_test_db()
    insert_placeholder_price_row(conn, "III")
    threshold = PriceThreshold(ticker="III", above=200.0)
    upsert_price_alert_state(conn, "III", price=150.0, alerted=False)

    with patch("alerts.email.SMTP_PASSWORD", "super-secret-password-value"):
        results = run_cycle_at_price(conn, "III", threshold, close=210.0, send=False)

    assert results[0].fired is True
    history = load_price_alert_history(conn)
    dump = history.to_string()
    assert "super-secret-password-value" not in dump


def test_price_alert_discord_payload_never_contains_webhook_url():
    fake_webhook = "https://discord.com/api/webhooks/123456789/super-secret-token-value"
    conn = make_test_db()
    insert_placeholder_price_row(conn, "NNN")
    threshold = PriceThreshold(ticker="NNN", above=200.0)
    upsert_price_alert_state(conn, "NNN", price=150.0, alerted=False)

    with patch("alerts.discord.DISCORD_WEBHOOK_URL", fake_webhook):
        ev = evaluate_at_price(conn, "NNN", threshold, close=210.0)
        assert ev.should_alert is True
        payload = build_price_alert_discord_payload(ev)

    payload_text = str(payload)
    assert "super-secret-token-value" not in payload_text
    assert "discord.com" not in payload_text


def test_price_alert_record_never_contains_discord_webhook_url():
    fake_webhook = "https://discord.com/api/webhooks/123456789/super-secret-token-value"
    conn = make_test_db()
    insert_placeholder_price_row(conn, "OOO")
    threshold = PriceThreshold(ticker="OOO", above=200.0)
    upsert_price_alert_state(conn, "OOO", price=150.0, alerted=False)

    with patch("alerts.discord.DISCORD_WEBHOOK_URL", fake_webhook):
        results = run_cycle_at_price(conn, "OOO", threshold, close=210.0, send=False)

    assert results[0].fired is True
    history = load_price_alert_history(conn)
    dump = history.to_string()
    assert "super-secret-token-value" not in dump
    assert "discord.com" not in dump
