"""Tests for the volatility (day-over-day % move) alert engine + dual-
channel delivery (email, Discord). Mirrors tests/test_price_alerts.py's
structure, adapted to the day-over-day move signal.

Three layers, tested separately:
  - alerts/volatility_engine.py's compute_day_over_day_move_pct() /
    determine_volatility_alert_reasons(): pure move-calculation and
    crossing-magnitude logic, tested directly with hand-picked values.
  - alerts/volatility_engine.py's evaluate_ticker_volatility(): DB
    integration against a throwaway in-memory SQLite database, reading real
    rows from the `prices` table (unlike the price-threshold engine, this
    reads price history directly rather than mocking an indicator snapshot,
    since the day-over-day comparison IS the last two stored closes).
  - alerts/volatility_runner.py's run_volatility_alert_cycle(): de-dupe/
    cooldown suppression + persistence + delivery.

All email delivery is mocked via unittest.mock.patch on
alerts.email.smtplib.SMTP; all Discord delivery is mocked via
unittest.mock.patch on alerts.discord.requests.post - no test in this file
ever opens a real SMTP connection or makes a real network call to Discord.
"""
import sqlite3
from unittest.mock import MagicMock, Mock, patch

import pytest

from alerts.discord import build_volatility_alert_discord_payload
from alerts.email import send_email_alert
from alerts.volatility_config import VolatilityAlertConfig, VolatilityAlertRunConfig
from alerts.volatility_engine import (
    REASON_VOLATILITY_DOWN,
    REASON_VOLATILITY_UP,
    compute_day_over_day_move_pct,
    determine_volatility_alert_reasons,
    evaluate_ticker_volatility,
    evaluate_volatility_alerts,
)
from alerts.volatility_runner import run_volatility_alert_cycle
from db.schema import init_db
from db.volatility_alert_config_repository import upsert_volatility_alert_config
from db.volatility_alert_repository import get_volatility_alert_state, load_volatility_alert_history

TEST_CONFIG = VolatilityAlertRunConfig(cooldown_minutes=60)


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def insert_price_row(conn, ticker, date, close, source="yfinance"):
    conn.execute(
        "INSERT INTO prices (ticker, date, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
        (ticker, date, close, close, close, close, 1_000_000, source),
    )
    conn.commit()


# --- compute_day_over_day_move_pct: pure move calculation ---


def test_computes_positive_move_pct():
    assert compute_day_over_day_move_pct(100.0, 106.0) == pytest.approx(6.0)


def test_computes_negative_move_pct():
    assert compute_day_over_day_move_pct(100.0, 94.0) == pytest.approx(-6.0)


def test_computes_zero_move_pct_for_unchanged_close():
    assert compute_day_over_day_move_pct(100.0, 100.0) == pytest.approx(0.0)


def test_scales_with_baseline_close():
    assert compute_day_over_day_move_pct(1000.0, 1060.0) == pytest.approx(6.0)


def test_returns_none_when_previous_close_is_none():
    assert compute_day_over_day_move_pct(None, 100.0) is None


def test_returns_none_when_previous_close_is_zero_or_negative():
    assert compute_day_over_day_move_pct(0.0, 100.0) is None
    assert compute_day_over_day_move_pct(-10.0, 100.0) is None


# --- determine_volatility_alert_reasons: pure threshold/direction logic ---


def test_upward_move_beyond_threshold_triggers_up_reason():
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)
    reasons = determine_volatility_alert_reasons(100.0, 110.0, config)
    assert reasons == [REASON_VOLATILITY_UP]


def test_downward_move_beyond_threshold_triggers_down_reason():
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)
    reasons = determine_volatility_alert_reasons(100.0, 90.0, config)
    assert reasons == [REASON_VOLATILITY_DOWN]


def test_move_below_threshold_never_triggers():
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)
    assert determine_volatility_alert_reasons(100.0, 104.9, config) == []
    assert determine_volatility_alert_reasons(100.0, 95.1, config) == []


def test_move_exactly_at_threshold_triggers():
    """The boundary is inclusive (>=), matching alerts/price_engine.py's
    crossing convention."""
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)
    up = determine_volatility_alert_reasons(100.0, 105.0, config)
    down = determine_volatility_alert_reasons(100.0, 95.0, config)
    assert up == [REASON_VOLATILITY_UP]
    assert down == [REASON_VOLATILITY_DOWN]


def test_first_day_with_no_prior_close_never_triggers():
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)
    assert determine_volatility_alert_reasons(None, 250.0, config) == []


def test_zero_previous_close_never_triggers():
    """A % move against a non-positive baseline is undefined - must fail
    safe (no alert) rather than raise or divide by zero."""
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)
    assert determine_volatility_alert_reasons(0.0, 50.0, config) == []


# --- evaluate_ticker_volatility: DB integration, reads `prices` directly ---


def test_evaluate_ticker_volatility_no_price_data():
    conn = make_test_db()
    config = VolatilityAlertConfig(ticker="GHOST", threshold_percent=5.0)
    ev = evaluate_ticker_volatility(conn, "GHOST", config)
    assert ev.ok is False
    assert ev.reason_unavailable is not None


def test_first_day_with_no_prior_close_establishes_baseline_only():
    """Edge case: a ticker's very first stored price row. There is nothing
    to compare against, so evaluation must succeed (ok=True) but never
    alert, with previous_price=None to make the "no prior close" state
    explicit."""
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 150.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    ev = evaluate_ticker_volatility(conn, "AAA", config)

    assert ev.ok is True
    assert ev.should_alert is False
    assert ev.previous_price is None
    assert ev.current_price == pytest.approx(150.0)
    assert ev.data_date == "2024-06-03"


def test_evaluate_ticker_volatility_fires_on_large_up_move():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    ev = evaluate_ticker_volatility(conn, "AAA", config)

    assert ev.ok is True
    assert ev.should_alert is True
    assert ev.reasons == [REASON_VOLATILITY_UP]
    assert ev.move_pct == pytest.approx(10.0)
    assert ev.previous_price == pytest.approx(100.0)
    assert ev.current_price == pytest.approx(110.0)
    assert ev.previous_date == "2024-06-03"
    assert ev.data_date == "2024-06-04"


def test_evaluate_ticker_volatility_fires_on_large_down_move():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 88.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    ev = evaluate_ticker_volatility(conn, "AAA", config)

    assert ev.should_alert is True
    assert ev.reasons == [REASON_VOLATILITY_DOWN]
    assert ev.move_pct == pytest.approx(-12.0)


def test_evaluate_ticker_volatility_does_not_fire_below_threshold():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 102.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    ev = evaluate_ticker_volatility(conn, "AAA", config)

    assert ev.should_alert is False
    assert ev.move_pct == pytest.approx(2.0)


def test_evaluate_ticker_volatility_exactly_at_threshold_fires():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 105.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    ev = evaluate_ticker_volatility(conn, "AAA", config)

    assert ev.should_alert is True
    assert ev.move_pct == pytest.approx(5.0)


def test_evaluate_ticker_volatility_across_a_data_gap_uses_adjacent_stored_rows():
    """Edge case: a gap in stored data (e.g. a missed ingestion day) between
    the two most recent rows. evaluate_ticker_volatility does not attempt to
    reconstruct the trading calendar - it compares whatever two rows are
    actually adjacent in storage, so a multi-day gap is measured as a single
    move. This is documented, deliberate behavior (see
    alerts/volatility_engine.py's docstring), not a bug: a 30% move across a
    4-day gap still surfaces as an actionable, real move worth flagging."""
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-10", 130.0)  # gap: no rows for 06-04..06-09
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    ev = evaluate_ticker_volatility(conn, "AAA", config)

    assert ev.should_alert is True
    assert ev.move_pct == pytest.approx(30.0)
    assert ev.previous_date == "2024-06-03"
    assert ev.data_date == "2024-06-10"


def test_evaluate_volatility_alerts_only_evaluates_configured_tickers():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    insert_price_row(conn, "BBB", "2024-06-03", 50.0)
    insert_price_row(conn, "BBB", "2024-06-04", 51.0)

    configs = [VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)]
    evaluations = evaluate_volatility_alerts(conn, configs)

    assert len(evaluations) == 1
    assert evaluations[0].ticker == "AAA"


def test_evaluate_volatility_alerts_defaults_to_db_backed_config():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    upsert_volatility_alert_config(conn, "AAA", threshold_percent=5.0)

    evaluations = evaluate_volatility_alerts(conn)  # configs=None -> DB default

    assert len(evaluations) == 1
    assert evaluations[0].ticker == "AAA"
    assert evaluations[0].should_alert is True


def test_ticker_with_no_configured_threshold_is_never_evaluated():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    results = run_volatility_alert_cycle(conn, configs=[], send=False)
    assert results == []


# --- run_volatility_alert_cycle: persistence + de-dupe/cooldown ---


def test_run_volatility_alert_cycle_persists_alert_and_updates_state():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    results = run_volatility_alert_cycle(conn, configs=[config], config=TEST_CONFIG)
    assert len(results) == 1
    result = results[0]

    assert result.evaluation.should_alert is True
    assert result.fired is True
    assert result.alert_id is not None

    history = load_volatility_alert_history(conn)
    assert len(history) == 1
    row = history.iloc[0]
    assert row["ticker"] == "AAA"
    assert row["dry_run"] == 1
    assert row["delivered"] == 0
    assert row["move_pct"] == pytest.approx(10.0)
    assert row["previous_close"] == pytest.approx(100.0)
    assert row["current_close"] == pytest.approx(110.0)

    state = get_volatility_alert_state(conn, "AAA")
    assert state["last_alert_data_date"] == "2024-06-04"


def test_rerunning_same_trading_day_does_not_duplicate_alert():
    """Primary de-dupe: re-evaluating the same data_date (no new price bar
    yet) must not re-fire, even with cooldown disabled - the day-over-day
    move itself hasn't changed."""
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)
    no_cooldown = VolatilityAlertRunConfig(cooldown_minutes=0)

    first = run_volatility_alert_cycle(conn, configs=[config], config=no_cooldown)
    second = run_volatility_alert_cycle(conn, configs=[config], config=no_cooldown)

    assert first[0].fired is True
    assert second[0].fired is False
    assert second[0].suppressed_already_alerted_today is True
    assert len(load_volatility_alert_history(conn)) == 1


def test_a_new_trading_days_move_fires_again_after_a_prior_alert():
    """Once a new price bar arrives (a genuinely new data_date), the
    per-day de-dupe must not block it - cooldown is disabled here so only
    the date-based de-dupe is under test."""
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)
    no_cooldown = VolatilityAlertRunConfig(cooldown_minutes=0)

    first = run_volatility_alert_cycle(conn, configs=[config], config=no_cooldown)
    assert first[0].fired is True

    insert_price_row(conn, "AAA", "2024-06-05", 121.0)  # a new, independent >=5% move
    second = run_volatility_alert_cycle(conn, configs=[config], config=no_cooldown)

    assert second[0].fired is True
    assert second[0].suppressed_already_alerted_today is False
    assert len(load_volatility_alert_history(conn)) == 2


def test_cooldown_suppresses_duplicate_alert_within_window():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    first = run_volatility_alert_cycle(conn, configs=[config], config=TEST_CONFIG)
    assert first[0].fired is True

    # Simulate a new trading day's move (so the date-based de-dupe alone
    # would allow a re-fire), but within the wall-clock cooldown window -
    # the cooldown must still suppress it as a secondary protection.
    insert_price_row(conn, "AAA", "2024-06-05", 121.0)
    second = run_volatility_alert_cycle(conn, configs=[config], config=TEST_CONFIG)

    assert second[0].evaluation.should_alert is True
    assert second[0].suppressed_by_cooldown is True
    assert second[0].fired is False
    assert len(load_volatility_alert_history(conn)) == 1


def test_zero_cooldown_does_not_suppress_a_new_trading_days_move():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)
    no_cooldown = VolatilityAlertRunConfig(cooldown_minutes=0)

    run_volatility_alert_cycle(conn, configs=[config], config=no_cooldown)
    insert_price_row(conn, "AAA", "2024-06-05", 121.0)
    second = run_volatility_alert_cycle(conn, configs=[config], config=no_cooldown)

    assert second[0].suppressed_by_cooldown is False
    assert second[0].fired is True


def test_unavailable_ticker_never_fires_and_is_not_persisted():
    conn = make_test_db()
    config = VolatilityAlertConfig(ticker="GHOST", threshold_percent=5.0)

    results = run_volatility_alert_cycle(conn, configs=[config])

    assert results[0].fired is False
    assert results[0].evaluation.ok is False
    assert len(load_volatility_alert_history(conn)) == 0


# --- Email delivery: always mocked ---


def test_dry_run_never_calls_smtp():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    with patch("alerts.email.smtplib.SMTP") as mock_smtp:
        results = run_volatility_alert_cycle(conn, configs=[config], config=TEST_CONFIG, send=False)

    assert results[0].fired is True
    mock_smtp.assert_not_called()


def test_successful_mocked_email_delivery_marks_delivered():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

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
         patch("alerts.discord.DISCORD_WEBHOOK_URL", None):
        results = run_volatility_alert_cycle(conn, configs=[config], config=TEST_CONFIG, send=True)

    mock_server.send_message.assert_called_once()
    mock_discord_post.assert_not_called()
    assert results[0].delivery.ok is True

    row = load_volatility_alert_history(conn).iloc[0]
    assert row["dry_run"] == 0
    assert row["delivered"] == 1
    assert row["delivered_at"] is not None
    assert row["delivery_error"] is None


def test_failed_mocked_email_delivery_marks_failure_visible():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    import smtplib as smtplib_module

    with patch("alerts.email.smtplib.SMTP", side_effect=smtplib_module.SMTPException("boom")), \
         patch("alerts.email.SMTP_HOST", "smtp.example.com"), \
         patch("alerts.email.SMTP_USERNAME", "user@example.com"), \
         patch("alerts.email.SMTP_PASSWORD", "app-password"), \
         patch("alerts.email.ALERT_EMAIL_FROM", "user@example.com"), \
         patch("alerts.email.ALERT_EMAIL_TO", "me@example.com"), \
         patch("alerts.discord.requests.post") as mock_discord_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", None):
        results = run_volatility_alert_cycle(conn, configs=[config], config=TEST_CONFIG, send=True)

    mock_discord_post.assert_not_called()
    assert results[0].delivery.ok is False

    row = load_volatility_alert_history(conn).iloc[0]
    assert row["dry_run"] == 0
    assert row["delivered"] == 0
    assert row["delivery_error"] is not None


def test_send_without_smtp_configured_fails_safely():
    with patch("alerts.email.smtplib.SMTP") as mock_smtp, \
         patch("alerts.email.SMTP_HOST", None):
        from email.message import EmailMessage
        result = send_email_alert(EmailMessage())

    assert result.ok is False
    mock_smtp.assert_not_called()


# --- Discord delivery: always mocked ---


def test_dry_run_never_calls_discord_webhook():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    with patch("alerts.discord.requests.post") as mock_post:
        results = run_volatility_alert_cycle(conn, configs=[config], config=TEST_CONFIG, send=False)

    assert results[0].fired is True
    mock_post.assert_not_called()


def test_successful_mocked_discord_delivery_marks_delivered():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    fake_response = Mock(status_code=204)
    with patch("alerts.discord.requests.post", return_value=fake_response) as mock_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"), \
         patch("alerts.email.smtplib.SMTP"):
        results = run_volatility_alert_cycle(conn, configs=[config], config=TEST_CONFIG, send=True)

    mock_post.assert_called_once()
    assert results[0].discord_delivery.ok is True

    row = load_volatility_alert_history(conn).iloc[0]
    assert row["discord_delivered"] == 1
    assert row["discord_delivered_at"] is not None
    assert row["discord_delivery_error"] is None


def test_failed_mocked_discord_delivery_marks_failure_visible():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    fake_response = Mock(status_code=400)
    with patch("alerts.discord.requests.post", return_value=fake_response), \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"), \
         patch("alerts.email.smtplib.SMTP"):
        results = run_volatility_alert_cycle(conn, configs=[config], config=TEST_CONFIG, send=True)

    assert results[0].discord_delivery.ok is False

    row = load_volatility_alert_history(conn).iloc[0]
    assert row["discord_delivered"] == 0
    assert row["discord_delivery_error"] is not None
    assert "400" in row["discord_delivery_error"]


def test_email_and_discord_deliver_independently_on_the_same_alert():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

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
        results = run_volatility_alert_cycle(conn, configs=[config], config=TEST_CONFIG, send=True)

    mock_server.send_message.assert_called_once()  # ...email still attempted regardless
    assert results[0].delivery.ok is True
    assert results[0].discord_delivery.ok is False

    row = load_volatility_alert_history(conn).iloc[0]
    assert row["delivered"] == 1
    assert row["discord_delivered"] == 0
    assert row["discord_delivery_error"] is not None


# --- secrets never leak into records or payloads ---


def test_alert_record_never_contains_smtp_password():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    with patch("alerts.email.SMTP_PASSWORD", "super-secret-password-value"):
        results = run_volatility_alert_cycle(conn, configs=[config], config=TEST_CONFIG, send=False)

    assert results[0].fired is True
    history = load_volatility_alert_history(conn)
    dump = history.to_string()
    assert "super-secret-password-value" not in dump


def test_volatility_discord_payload_never_contains_webhook_url():
    fake_webhook = "https://discord.com/api/webhooks/123456789/super-secret-token-value"
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    with patch("alerts.discord.DISCORD_WEBHOOK_URL", fake_webhook):
        ev = evaluate_ticker_volatility(conn, "AAA", config)
        assert ev.should_alert is True
        payload = build_volatility_alert_discord_payload(ev)

    payload_text = str(payload)
    assert "super-secret-token-value" not in payload_text
    assert "discord.com" not in payload_text


def test_volatility_alert_record_never_contains_discord_webhook_url():
    fake_webhook = "https://discord.com/api/webhooks/123456789/super-secret-token-value"
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 110.0)
    config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    with patch("alerts.discord.DISCORD_WEBHOOK_URL", fake_webhook):
        results = run_volatility_alert_cycle(conn, configs=[config], config=TEST_CONFIG, send=False)

    assert results[0].fired is True
    history = load_volatility_alert_history(conn)
    dump = history.to_string()
    assert "super-secret-token-value" not in dump
    assert "discord.com" not in dump
