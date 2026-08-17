"""Manual "Send Test Alert" buttons - one on each of dashboard/views/
price_alert_config.py, volatility_alert_config.py, and daily_digest_config.py.
Mirrors alerts/test_notification.py's isolation contract (a dedicated,
non-evaluation path used only to verify delivery end-to-end), extended to
these three newer alert types and made STRICTER: no function in this
module accepts or opens a database connection, so there is nothing for it
to write. It never imports alerts/price_engine.py, alerts/volatility_engine.py,
alerts/daily_digest_engine.py, or any db/*_repository module - a click here
cannot reach price_alert_state, volatility_alert_state, or
daily_digest_log (the once-per-day dedupe) even by accident, because the
code path never touches them at all.

Every payload/message here is built from FIXED, clearly "[TEST]"-labeled
placeholder data - never derived from any real ticker, price, or
threshold. It reuses alerts/email.py's send_email_alert() and alerts/
discord.py's send_discord_alert() directly - the exact same delivery
functions every real alert type uses - so a successful test proves the
real SMTP/Discord config actually works, without reusing any
crossing-detection, distance-to-threshold, or digest-building logic that
could contaminate a real evaluation.
"""
from dataclasses import dataclass
from email.message import EmailMessage

from alerts.discord import DeliveryResult as DiscordDeliveryResult, send_discord_alert
from alerts.email import DeliveryResult as EmailDeliveryResult, send_email_alert
from config.settings import ALERT_EMAIL_FROM, ALERT_EMAIL_TO

# Neutral gray - deliberately distinct from any real alert type's color,
# same convention alerts/test_notification.py's build_test_payload() uses.
TEST_COLOR = 0x898781


@dataclass
class TestSendResult:
    email: EmailDeliveryResult
    discord: DiscordDeliveryResult

    @property
    def any_ok(self) -> bool:
        return self.email.ok or self.discord.ok


def _send_test(subject: str, body: str, discord_payload: dict) -> TestSendResult:
    """Shared delivery step for all three test-send functions below - both
    channels are attempted independently, same "one failing never blocks
    the other" contract every real alert type uses."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = ALERT_EMAIL_FROM
    message["To"] = ALERT_EMAIL_TO
    message.set_content(body)
    email_result = send_email_alert(message)

    discord_result = send_discord_alert(discord_payload)

    return TestSendResult(email=email_result, discord=discord_result)


def send_price_alert_test() -> TestSendResult:
    """Sample price-threshold alert - the "AAPL crossed above $250.00"
    shape alerts/email.py's build_email_message()/alerts/discord.py's
    build_price_alert_discord_payload() produce, with realistic but fixed
    placeholder numbers. Never reads db/price_alert_config_repository.py -
    the $250.00/$251.30 values below are not a real configured threshold."""
    subject = "[TEST] AAPL crossed above $250.00"
    body = (
        "[TEST] This is a sample price-threshold alert - not a real market signal.\n\n"
        "AAPL crossed above your configured threshold of $250.00.\n\n"
        "Current price: $251.30 (was $248.90)\n"
        "As of: 2026-01-02 (sample data)\n\n"
        "Sent manually from the Price Alert Thresholds dashboard page to verify email/Discord "
        "delivery. Not derived from any real ticker, threshold, or price - no threshold, state, "
        "or crossing-detection logic was touched to produce this message."
    )
    payload = {
        "embeds": [
            {
                "title": "[TEST] AAPL crossed above $250.00",
                "description": (
                    "Sample price-threshold alert used to verify Discord delivery only. "
                    "Not a real market signal."
                ),
                "color": TEST_COLOR,
                "fields": [
                    {"name": "Sample price", "value": "$251.30 (was $248.90)", "inline": True},
                    {"name": "Sample threshold", "value": "$250.00 (above)", "inline": True},
                    {"name": "As of", "value": "2026-01-02 (sample data)", "inline": True},
                ],
                "footer": {"text": "TEST ALERT — Price Alert Thresholds delivery check only. Not a real signal."},
            }
        ]
    }
    return _send_test(subject, body, payload)


def send_volatility_alert_test() -> TestSendResult:
    """Sample volatility alert - the "AAPL jumped +6.00% today" shape
    alerts/email.py's build_volatility_email_message()/alerts/discord.py's
    build_volatility_alert_discord_payload() produce. Never reads db/
    volatility_alert_config_repository.py - the 5.00% threshold and 6.00%
    move below are not a real configured value or a real day-over-day
    move."""
    subject = "[TEST] AAPL jumped +6.00% today"
    body = (
        "[TEST] This is a sample volatility alert - not a real market signal.\n\n"
        "AAPL moved 6.00% day-over-day, beyond a sample 5.00% volatility threshold.\n\n"
        "Close: $251.30 (was $237.08)\n"
        "As of: 2026-01-02 (prev 2026-01-01, sample data)\n\n"
        "Sent manually from the Volatility Alert Thresholds dashboard page to verify email/Discord "
        "delivery. Not derived from any real ticker, threshold, or price move - no threshold, "
        "state, or crossing-detection logic was touched to produce this message."
    )
    payload = {
        "embeds": [
            {
                "title": "[TEST] AAPL jumped +6.00% today",
                "description": (
                    "Sample volatility alert used to verify Discord delivery only. "
                    "Not a real market signal."
                ),
                "color": TEST_COLOR,
                "fields": [
                    {"name": "Sample close", "value": "$251.30 (was $237.08)", "inline": True},
                    {"name": "Sample threshold", "value": "±5.00% daily move", "inline": True},
                    {"name": "As of", "value": "2026-01-02 (prev 2026-01-01, sample data)", "inline": True},
                ],
                "footer": {"text": "TEST ALERT — Volatility Alert Thresholds delivery check only. Not a real signal."},
            }
        ]
    }
    return _send_test(subject, body, payload)


def send_daily_digest_test() -> TestSendResult:
    """Sample daily digest - the multi-ticker summary shape alerts/
    daily_digest_engine.py's format_digest_body()/alerts/discord.py's
    build_daily_digest_discord_payload() produce, with two fixed
    placeholder tickers. Never reads db/price_repository.py or either
    threshold repository - no real ticker's price, change, or threshold
    distance is computed, and this never touches db/daily_digest_repository.py's
    daily_digest_log, so a test click never counts against the real
    once-per-day dedupe."""
    subject = "[TEST] Stock Dashboard - Daily Digest"
    body = (
        "[TEST] Daily Digest - sample content, not real market data.\n\n"
        "AAPL: $251.30 (+1.7%), 4.0% from upper threshold\n"
        "MSFT: $495.40 (-0.3%), 6.0% from upper threshold, 0.3% of 3.0% volatility threshold\n\n"
        "Sent manually from the Daily Digest dashboard page to verify email/Discord delivery. "
        "Not derived from any real ticker price, change, or threshold distance, and does not "
        "count against the once-per-day digest send limit."
    )
    payload = {
        "embeds": [
            {
                "title": "[TEST] Daily Digest",
                "description": "Sample daily digest used to verify Discord delivery only. Not real market data.",
                "color": TEST_COLOR,
                "fields": [
                    {"name": "AAPL", "value": "$251.30 (+1.7%), 4.0% from upper threshold", "inline": False},
                    {
                        "name": "MSFT",
                        "value": "$495.40 (-0.3%), 6.0% from upper threshold, 0.3% of 3.0% volatility threshold",
                        "inline": False,
                    },
                ],
                "footer": {"text": "TEST ALERT — Daily Digest delivery check only. Not real market data."},
            }
        ]
    }
    return _send_test(subject, body, payload)
