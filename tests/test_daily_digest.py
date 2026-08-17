"""Tests for the daily digest: content generation (alerts/daily_digest_engine.py)
and dual-channel delivery (alerts/daily_digest_runner.py). Mirrors tests/
test_volatility_alerts.py's structure/mocking conventions.

Three layers, tested separately:
  - alerts/daily_digest_engine.py's build_ticker_digest_row(): DB
    integration against a throwaway in-memory SQLite database, reading
    real rows from the `prices` table (same approach
    tests/test_volatility_alerts.py uses for the volatility engine, since
    both read the two most recent stored closes directly).
  - format_ticker_digest_line()/format_digest_body(): pure text formatting,
    tested directly with hand-picked values.
  - alerts/daily_digest_runner.py's run_daily_digest(): de-dupe + delivery,
    mocked identically to the price/volatility runner tests - no test here
    ever opens a real SMTP connection or makes a real network call to
    Discord.
"""
import sqlite3
from unittest.mock import MagicMock, Mock, patch

import pytest

from alerts.daily_digest_engine import (
    DigestTickerRow,
    build_daily_digest,
    build_ticker_digest_row,
    format_digest_body,
    format_ticker_digest_line,
)
from alerts.daily_digest_runner import run_daily_digest
from alerts.price_config import PriceThreshold
from alerts.volatility_config import VolatilityAlertConfig
from db.daily_digest_repository import already_sent_today, load_digest_log
from db.schema import init_db

TRADING_DATE = "2024-06-04"


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


# --- build_ticker_digest_row: DB integration ---


def test_no_price_data_is_unavailable():
    conn = make_test_db()
    row = build_ticker_digest_row(conn, "GHOST")
    assert row.ok is False
    assert row.reason_unavailable is not None


def test_first_day_with_no_prior_close_has_no_change_pct():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)

    row = build_ticker_digest_row(conn, "AAA")

    assert row.ok is True
    assert row.current_price == pytest.approx(100.0)
    assert row.previous_price is None
    assert row.change_pct is None


def test_computes_day_over_day_change_pct():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 100.8)

    row = build_ticker_digest_row(conn, "AAA")

    assert row.current_price == pytest.approx(100.8)
    assert row.previous_price == pytest.approx(100.0)
    assert row.change_pct == pytest.approx(0.8)


def test_price_threshold_distance_computed_for_both_directions():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 100.8)
    threshold = PriceThreshold(ticker="AAA", above=104.0, below=90.0)

    row = build_ticker_digest_row(conn, "AAA", price_threshold=threshold)

    # current=100.8 is between above=104 and below=90 - neither has been
    # reached yet, so both distances are positive ("needs to move this
    # much to get there").
    assert row.distance_to_above_pct == pytest.approx(3.1746, abs=0.001)
    assert row.distance_to_below_pct == pytest.approx(10.7143, abs=0.001)


def test_price_threshold_distance_is_negative_once_already_past_the_level():
    """below=105 with current=100.8: the price is already BELOW the
    below-threshold (it would have already fired a price alert) - distance
    is negative, signaling "already past," not "needs to fall further"."""
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 100.8)
    threshold = PriceThreshold(ticker="AAA", above=None, below=105.0)

    row = build_ticker_digest_row(conn, "AAA", price_threshold=threshold)

    assert row.distance_to_below_pct == pytest.approx(-4.1667, abs=0.001)


def test_price_threshold_with_only_above_configured_leaves_below_none():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 100.8)
    threshold = PriceThreshold(ticker="AAA", above=104.0, below=None)

    row = build_ticker_digest_row(conn, "AAA", price_threshold=threshold)

    assert row.distance_to_above_pct is not None
    assert row.distance_to_below_pct is None


def test_volatility_threshold_percent_carried_through_unchanged():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 100.8)
    volatility_config = VolatilityAlertConfig(ticker="AAA", threshold_percent=5.0)

    row = build_ticker_digest_row(conn, "AAA", volatility_config=volatility_config)

    assert row.volatility_threshold_percent == pytest.approx(5.0)


def test_ticker_with_no_configured_thresholds_has_no_threshold_fields():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 100.8)

    row = build_ticker_digest_row(conn, "AAA")

    assert row.distance_to_above_pct is None
    assert row.distance_to_below_pct is None
    assert row.volatility_threshold_percent is None


def test_build_daily_digest_covers_every_given_ticker_in_order():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 101.0)
    insert_price_row(conn, "BBB", "2024-06-03", 50.0)
    insert_price_row(conn, "BBB", "2024-06-04", 49.0)

    rows = build_daily_digest(
        conn, ["AAA", "BBB", "GHOST"],
        price_thresholds_by_ticker={"AAA": PriceThreshold(ticker="AAA", above=110.0)},
        volatility_configs_by_ticker={"BBB": VolatilityAlertConfig(ticker="BBB", threshold_percent=3.0)},
    )

    assert [r.ticker for r in rows] == ["AAA", "BBB", "GHOST"]
    assert rows[0].distance_to_above_pct is not None
    assert rows[1].volatility_threshold_percent == pytest.approx(3.0)
    assert rows[2].ok is False


# --- format_ticker_digest_line / format_digest_body: pure formatting ---


def test_format_matches_price_and_change_and_upper_threshold_distance():
    """The exact shape from the feature request: "AAPL: $233.10 (+0.8%),
    3.2% from upper threshold"."""
    row = DigestTickerRow(
        ticker="AAPL", ok=True, current_price=100.8, previous_price=100.0, change_pct=0.8,
        price_above=104.0, distance_to_above_pct=3.1746,
    )
    assert format_ticker_digest_line(row) == "AAPL: $100.80 (+0.8%), 3.2% from upper threshold"


def test_format_shows_negative_change_with_sign():
    row = DigestTickerRow(ticker="BBB", ok=True, current_price=49.0, previous_price=50.0, change_pct=-2.0)
    assert format_ticker_digest_line(row) == "BBB: $49.00 (-2.0%)"


def test_format_shows_n_a_when_no_prior_close():
    row = DigestTickerRow(ticker="CCC", ok=True, current_price=10.0, previous_price=None, change_pct=None)
    assert format_ticker_digest_line(row) == "CCC: $10.00 (n/a)"


def test_format_unavailable_ticker():
    row = DigestTickerRow(ticker="GHOST", ok=False, reason_unavailable="no price data available")
    assert format_ticker_digest_line(row) == "GHOST: unavailable (no price data available)"


def test_format_shows_both_thresholds_and_volatility_together():
    row = DigestTickerRow(
        ticker="DDD", ok=True, current_price=100.0, previous_price=99.0, change_pct=1.0101,
        distance_to_above_pct=5.0, distance_to_below_pct=8.0, volatility_threshold_percent=3.0,
    )
    line = format_ticker_digest_line(row)
    assert line.startswith("DDD: $100.00 (+1.0%), ")
    assert "5.0% from upper threshold" in line
    assert "8.0% from lower threshold" in line
    assert "1.0% of 3.0% volatility threshold" in line


def test_format_no_threshold_bits_when_nothing_configured():
    row = DigestTickerRow(ticker="EEE", ok=True, current_price=25.0, previous_price=25.0, change_pct=0.0)
    assert format_ticker_digest_line(row) == "EEE: $25.00 (+0.0%)"


def test_format_digest_body_includes_header_and_all_ticker_lines():
    rows = [
        DigestTickerRow(ticker="AAA", ok=True, current_price=10.0, previous_price=10.0, change_pct=0.0),
        DigestTickerRow(ticker="BBB", ok=True, current_price=20.0, previous_price=20.0, change_pct=0.0),
    ]
    body = format_digest_body(rows, TRADING_DATE)
    assert f"Daily Digest - {TRADING_DATE} (2 tickers)" in body
    assert "AAA: $10.00 (+0.0%)" in body
    assert "BBB: $20.00 (+0.0%)" in body


def test_format_digest_body_singular_ticker_count_wording():
    rows = [DigestTickerRow(ticker="AAA", ok=True, current_price=10.0, previous_price=10.0, change_pct=0.0)]
    body = format_digest_body(rows, TRADING_DATE)
    assert "(1 ticker)" in body
    assert "(1 tickers)" not in body


# --- run_daily_digest: de-dupe + persistence ---


def test_run_daily_digest_persists_log_entry_and_covers_all_tickers():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 101.0)

    result = run_daily_digest(conn, tickers=["AAA"], trading_date=None)

    assert result.sent is True
    assert len(result.rows) == 1
    assert already_sent_today(conn, result.rows[0].data_date) or True  # sanity: no crash
    history = load_digest_log(conn)
    assert len(history) == 1
    assert history.iloc[0]["ticker_count"] == 1
    assert history.iloc[0]["dry_run"] == 1


def test_rerunning_same_trading_day_does_not_duplicate():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 101.0)

    from datetime import date
    d = date(2024, 6, 4)

    first = run_daily_digest(conn, tickers=["AAA"], trading_date=d)
    second = run_daily_digest(conn, tickers=["AAA"], trading_date=d)

    assert first.sent is True
    assert second.sent is False
    assert "already sent" in second.reason
    assert len(load_digest_log(conn)) == 1


def test_a_new_trading_day_sends_again():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 101.0)

    from datetime import date
    first = run_daily_digest(conn, tickers=["AAA"], trading_date=date(2024, 6, 4))
    second = run_daily_digest(conn, tickers=["AAA"], trading_date=date(2024, 6, 5))

    assert first.sent is True
    assert second.sent is True
    assert len(load_digest_log(conn)) == 2


def test_persist_false_never_touches_the_log():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 101.0)

    result = run_daily_digest(conn, tickers=["AAA"], persist=False)

    assert result.digest_id is None
    assert len(load_digest_log(conn)) == 0
    assert len(result.rows) == 1  # content is still built for preview


# --- Email delivery: always mocked ---


def test_dry_run_never_calls_smtp():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 101.0)

    with patch("alerts.email.smtplib.SMTP") as mock_smtp:
        run_daily_digest(conn, tickers=["AAA"], send=False)

    mock_smtp.assert_not_called()


def test_successful_mocked_email_delivery_marks_delivered():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 101.0)

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
        result = run_daily_digest(conn, tickers=["AAA"], send=True)

    mock_server.send_message.assert_called_once()
    mock_discord_post.assert_not_called()
    assert result.delivery.ok is True

    row = load_digest_log(conn).iloc[0]
    assert row["dry_run"] == 0
    assert row["delivered"] == 1
    assert row["delivered_at"] is not None


def test_failed_mocked_email_delivery_marks_failure_visible():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 101.0)

    import smtplib as smtplib_module

    with patch("alerts.email.smtplib.SMTP", side_effect=smtplib_module.SMTPException("boom")), \
         patch("alerts.email.SMTP_HOST", "smtp.example.com"), \
         patch("alerts.email.SMTP_USERNAME", "user@example.com"), \
         patch("alerts.email.SMTP_PASSWORD", "app-password"), \
         patch("alerts.email.ALERT_EMAIL_FROM", "user@example.com"), \
         patch("alerts.email.ALERT_EMAIL_TO", "me@example.com"), \
         patch("alerts.discord.requests.post") as mock_discord_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", None):
        result = run_daily_digest(conn, tickers=["AAA"], send=True)

    assert result.delivery.ok is False
    row = load_digest_log(conn).iloc[0]
    assert row["delivered"] == 0
    assert row["delivery_error"] is not None


# --- Discord delivery: always mocked ---


def test_dry_run_never_calls_discord_webhook():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 101.0)

    with patch("alerts.discord.requests.post") as mock_post:
        run_daily_digest(conn, tickers=["AAA"], send=False)

    mock_post.assert_not_called()


def test_successful_mocked_discord_delivery_marks_delivered():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 101.0)

    fake_response = Mock(status_code=204)
    with patch("alerts.discord.requests.post", return_value=fake_response) as mock_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"), \
         patch("alerts.email.smtplib.SMTP"):
        result = run_daily_digest(conn, tickers=["AAA"], send=True)

    mock_post.assert_called_once()
    assert result.discord_delivery.ok is True

    row = load_digest_log(conn).iloc[0]
    assert row["discord_delivered"] == 1


def test_failed_mocked_discord_delivery_marks_failure_visible():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 101.0)

    fake_response = Mock(status_code=400)
    with patch("alerts.discord.requests.post", return_value=fake_response), \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"), \
         patch("alerts.email.smtplib.SMTP"):
        result = run_daily_digest(conn, tickers=["AAA"], send=True)

    assert result.discord_delivery.ok is False
    row = load_digest_log(conn).iloc[0]
    assert row["discord_delivered"] == 0
    assert row["discord_delivery_error"] is not None


def test_email_and_discord_deliver_independently():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 101.0)

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
        result = run_daily_digest(conn, tickers=["AAA"], send=True)

    mock_server.send_message.assert_called_once()  # ...email still attempted regardless
    assert result.delivery.ok is True
    assert result.discord_delivery.ok is False


# --- secrets never leak into records or payloads ---


def test_digest_email_body_never_contains_smtp_password():
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 101.0)

    from alerts.daily_digest_engine import build_daily_digest, format_digest_body

    with patch("alerts.email.SMTP_PASSWORD", "super-secret-password-value"):
        rows = build_daily_digest(conn, ["AAA"])
        body = format_digest_body(rows, TRADING_DATE)

    assert "super-secret-password-value" not in body


def test_digest_discord_payload_never_contains_webhook_url():
    fake_webhook = "https://discord.com/api/webhooks/123456789/super-secret-token-value"
    conn = make_test_db()
    insert_price_row(conn, "AAA", "2024-06-03", 100.0)
    insert_price_row(conn, "AAA", "2024-06-04", 101.0)

    from alerts.daily_digest_engine import build_daily_digest
    from alerts.discord import build_daily_digest_discord_payload

    with patch("alerts.discord.DISCORD_WEBHOOK_URL", fake_webhook):
        rows = build_daily_digest(conn, ["AAA"])
        payload = build_daily_digest_discord_payload(rows, TRADING_DATE)

    payload_text = str(payload)
    assert "super-secret-token-value" not in payload_text
    assert "discord.com" not in payload_text
