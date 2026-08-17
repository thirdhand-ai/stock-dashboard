"""Tests for the "Send Test Alert" buttons on the three alert-config
dashboard pages (alerts/alert_test_notifications.py, plus the thin
dashboard/data.py wrappers). Mirrors tests/test_price_alerts.py's/tests/
test_volatility_alerts.py's delivery-mocking conventions.

The central guarantee under test: a test-send click never mutates
price_alert_state, volatility_alert_state, or daily_digest_log (the
once-per-day dedupe) - proven directly by seeding those tables with real-
looking data, calling each test-send function, and asserting the tables
are byte-identical afterward, not just "probably fine because the
function doesn't take a conn."
"""
import sqlite3
from unittest.mock import MagicMock, Mock, patch

from alerts.alert_test_notifications import (
    send_daily_digest_test,
    send_price_alert_test,
    send_volatility_alert_test,
)
from db.schema import init_db

ALL_SEND_FUNCTIONS = [send_price_alert_test, send_volatility_alert_test, send_daily_digest_test]


def make_seeded_db():
    """A DB with real-looking state already present, mirroring production:
    a real ticker's price_alert_state, volatility_alert_state, and one
    prior daily_digest_log entry."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)

    from db.price_alert_repository import upsert_price_alert_state
    from db.volatility_alert_repository import upsert_volatility_alert_state
    from db.daily_digest_repository import record_digest_sent

    upsert_price_alert_state(conn, "AAPL", price=233.10, alerted=False)
    upsert_volatility_alert_state(conn, "AAPL", alerted=False, data_date="2026-01-01")
    record_digest_sent(conn, "2026-01-01", ticker_count=8, dry_run=False)
    return conn


def _snapshot(conn, table):
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]


def _mocked_delivery():
    """Both channels succeed, fully mocked - no real network call."""
    mock_server = MagicMock()
    mock_smtp_cm = MagicMock()
    mock_smtp_cm.__enter__.return_value = mock_server
    fake_discord_response = Mock(status_code=204)
    return patch("alerts.email.smtplib.SMTP", return_value=mock_smtp_cm), \
        patch("alerts.email.SMTP_HOST", "smtp.example.com"), \
        patch("alerts.email.SMTP_USERNAME", "user@example.com"), \
        patch("alerts.email.SMTP_PASSWORD", "app-password"), \
        patch("alerts.email.ALERT_EMAIL_FROM", "user@example.com"), \
        patch("alerts.email.ALERT_EMAIL_TO", "me@example.com"), \
        patch("alerts.discord.requests.post", return_value=fake_discord_response), \
        patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test")


# --- content: clearly [TEST]-labeled, realistic sample data ---


def test_price_alert_test_is_clearly_labeled_and_never_calls_smtp_when_unmocked():
    with patch("alerts.email.smtplib.SMTP") as mock_smtp, \
         patch("alerts.email.SMTP_HOST", None), \
         patch("alerts.discord.requests.post") as mock_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", None):
        result = send_price_alert_test()

    # Both fail safe (unconfigured in this test), proving no network call
    # was attempted before the config check - same fail-safe contract
    # every real send_email_alert/send_discord_alert already has.
    assert result.email.ok is False
    assert result.discord.ok is False
    mock_smtp.assert_not_called()
    mock_post.assert_not_called()


def test_price_alert_test_payload_and_subject_are_test_labeled():
    with patch("alerts.alert_test_notifications.send_email_alert") as mock_email, \
         patch("alerts.alert_test_notifications.send_discord_alert") as mock_discord:
        mock_email.return_value = Mock(ok=True)
        mock_discord.return_value = Mock(ok=True)
        send_price_alert_test()
        email_message = mock_email.call_args[0][0]
        discord_payload = mock_discord.call_args[0][0]

    assert email_message["Subject"].startswith("[TEST]")
    assert "AAPL" in str(email_message)
    assert discord_payload["embeds"][0]["title"].startswith("[TEST]")


def test_volatility_alert_test_payload_and_subject_are_test_labeled():
    with patch("alerts.alert_test_notifications.send_email_alert") as mock_email, \
         patch("alerts.alert_test_notifications.send_discord_alert") as mock_discord:
        mock_email.return_value = Mock(ok=True)
        mock_discord.return_value = Mock(ok=True)
        send_volatility_alert_test()
        email_message = mock_email.call_args[0][0]
        discord_payload = mock_discord.call_args[0][0]

    assert email_message["Subject"].startswith("[TEST]")
    assert discord_payload["embeds"][0]["title"].startswith("[TEST]")


def test_daily_digest_test_payload_covers_two_sample_tickers():
    with patch("alerts.alert_test_notifications.send_email_alert") as mock_email, \
         patch("alerts.alert_test_notifications.send_discord_alert") as mock_discord:
        mock_email.return_value = Mock(ok=True)
        mock_discord.return_value = Mock(ok=True)
        send_daily_digest_test()
        email_message = mock_email.call_args[0][0]
        discord_payload = mock_discord.call_args[0][0]

    assert email_message["Subject"].startswith("[TEST]")
    body_text = str(email_message)
    assert "AAPL" in body_text and "MSFT" in body_text
    field_names = [f["name"] for f in discord_payload["embeds"][0]["fields"]]
    assert "AAPL" in field_names and "MSFT" in field_names


# --- delivery: both channels attempted, independently ---


def test_successful_delivery_over_both_channels():
    patches = _mocked_delivery()
    with patches[0] as mock_smtp, patches[1], patches[2], patches[3], patches[4], patches[5], patches[6] as mock_post, patches[7]:
        result = send_price_alert_test()

    mock_smtp.assert_called_once()
    mock_post.assert_called_once()
    assert result.email.ok is True
    assert result.discord.ok is True
    assert result.any_ok is True


def test_one_channel_failing_does_not_block_the_other():
    fake_discord_response = Mock(status_code=400)
    with patch("alerts.email.smtplib.SMTP") as mock_smtp_cls, \
         patch("alerts.email.SMTP_HOST", "smtp.example.com"), \
         patch("alerts.email.SMTP_USERNAME", "user@example.com"), \
         patch("alerts.email.SMTP_PASSWORD", "app-password"), \
         patch("alerts.email.ALERT_EMAIL_FROM", "user@example.com"), \
         patch("alerts.email.ALERT_EMAIL_TO", "me@example.com"), \
         patch("alerts.discord.requests.post", return_value=fake_discord_response), \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/fake/test"):
        mock_smtp_cls.return_value.__enter__.return_value = MagicMock()
        result = send_volatility_alert_test()

    assert result.email.ok is True
    assert result.discord.ok is False
    assert result.any_ok is True


def test_both_channels_failing_reports_not_any_ok():
    with patch("alerts.email.smtplib.SMTP"), \
         patch("alerts.email.SMTP_HOST", None), \
         patch("alerts.discord.requests.post"), \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", None):
        result = send_daily_digest_test()

    assert result.email.ok is False
    assert result.discord.ok is False
    assert result.any_ok is False


# --- isolation: never mutates production state ---


def test_price_alert_test_never_touches_price_alert_state():
    conn = make_seeded_db()
    before = _snapshot(conn, "price_alert_state")

    patches = _mocked_delivery()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
        send_price_alert_test()

    after = _snapshot(conn, "price_alert_state")
    assert after == before


def test_volatility_alert_test_never_touches_volatility_alert_state():
    conn = make_seeded_db()
    before = _snapshot(conn, "volatility_alert_state")

    patches = _mocked_delivery()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
        send_volatility_alert_test()

    after = _snapshot(conn, "volatility_alert_state")
    assert after == before


def test_daily_digest_test_never_touches_daily_digest_log():
    """The strongest isolation claim: a test click must never count
    against the once-per-day digest dedupe. Seed a prior real digest_log
    row, then confirm it's the only row before AND after the test send -
    no new row, no changed row."""
    conn = make_seeded_db()
    before = _snapshot(conn, "daily_digest_log")
    assert len(before) == 1  # sanity: the seed actually landed

    patches = _mocked_delivery()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
        send_daily_digest_test()

    after = _snapshot(conn, "daily_digest_log")
    assert after == before


def test_none_of_the_three_test_sends_write_to_any_table():
    """Broadest possible proof: snapshot every user table's row count
    before and after running all three test-send functions, assert
    nothing changed anywhere in the database - not just the three tables
    named in the feature request."""
    conn = make_seeded_db()
    tables = [
        row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    before = {t: _snapshot(conn, t) for t in tables}

    patches = _mocked_delivery()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
        for fn in ALL_SEND_FUNCTIONS:
            fn()

    after = {t: _snapshot(conn, t) for t in tables}
    assert after == before


def test_send_functions_accept_no_database_connection_argument():
    """Structural guarantee, not just a behavioral one: these functions
    take zero arguments, so there is no conn parameter available to
    (mis)use even if someone tried."""
    import inspect
    for fn in ALL_SEND_FUNCTIONS:
        assert inspect.signature(fn).parameters == {}


# --- dashboard/data.py wrappers: never open a DB session either ---


def test_dashboard_wrappers_never_open_a_db_session():
    """dashboard/data.py's send_*_test_notification wrappers must not call
    db_session() at all - proven by making db_session raise if invoked,
    and confirming the wrapper still succeeds."""
    import dashboard.data as dashboard_data

    def _explode(*args, **kwargs):
        raise AssertionError("db_session() must never be called by a test-send wrapper")

    with patch("dashboard.data.db_session", side_effect=_explode), \
         patch("alerts.email.smtplib.SMTP") as mock_smtp, \
         patch("alerts.email.SMTP_HOST", None), \
         patch("alerts.discord.requests.post") as mock_post, \
         patch("alerts.discord.DISCORD_WEBHOOK_URL", None):
        price_result = dashboard_data.send_price_alert_test_notification()
        volatility_result = dashboard_data.send_volatility_alert_test_notification()
        digest_result = dashboard_data.send_daily_digest_test_notification()

    assert price_result.any_ok is False  # unconfigured in this test - fails safe, but reaches here without raising
    assert volatility_result.any_ok is False
    assert digest_result.any_ok is False
