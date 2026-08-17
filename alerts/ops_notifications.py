"""Operational-failure notification, delivered over the same two
independent channels (email + Discord) every other alert type in this
codebase uses - structurally SEPARATE from stock-signal alerts (see
alerts/discord.py, alerts/email.py, alerts/engine.py).

This module never contains stock recommendation content, never reads
scores/stages/prices. OPERATIONAL_ALERTS_ENABLED must be explicitly flipped
to True (a deliberate code change, not an env var an operator could set by
accident) before send_operational_failure_notification() will ever perform
a real network call - flipped on 2026-08-16 after review, closing the gap
exposed by the 2026-08-12 total-ingestion-failure incident (see
logs/automation.log for that day's run): the pipeline correctly logged and
recorded the failure, but nothing paged anyone, so it went unnoticed until
the next manual check.

At most one notification per failed scheduled run/day: the caller is
expected to pass the trading date, and already_sent_today() records/checks
that in operational_notifications so a flaky day generating several failed
runs still only pages once. Email and Discord are each attempted
independently - one channel being unconfigured or failing never blocks the
other, same contract alerts/price_runner.py and alerts/volatility_runner.py
use for stock alerts.
"""
import logging
import re
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from typing import Optional

import requests

from alerts.email import send_email_alert
from config.settings import ALERT_EMAIL_FROM, ALERT_EMAIL_TO, DISCORD_WEBHOOK_URL

logger = logging.getLogger(__name__)

# Flipping this requires an explicit code change and review - not something
# a config file or env var flips silently.
OPERATIONAL_ALERTS_ENABLED = True

OPERATIONAL_FAILURE_HEADLINE = "Stock dashboard automation failed — market data could not be refreshed."

_URL_RE = re.compile(r"https?://\S+")
_SECRET_RE = re.compile(r"(key|secret|token|password|webhook)[=:]\S+", re.IGNORECASE)


def sanitize_error_summary(raw: Optional[str], max_len: int = 300) -> str:
    """Strip URLs/credential-shaped substrings and cap length - never
    forward a raw stack trace or webhook value into a Discord message."""
    if not raw:
        return "unknown error"
    text = _URL_RE.sub("[url removed]", raw)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = text.replace("\n", " ").strip()
    return text[:max_len]


def build_operational_failure_payload(error_summary: str) -> dict:
    """Pure function, no network call. Structurally distinct from
    build_discord_payload() in alerts/discord.py - no ticker, score, stage,
    or price field anywhere in this payload."""
    sanitized = sanitize_error_summary(error_summary)
    return {
        "embeds": [
            {
                "title": "Stock Dashboard — Automation Failure",
                "description": OPERATIONAL_FAILURE_HEADLINE,
                "color": 0xE53935,
                "fields": [{"name": "Summary", "value": sanitized, "inline": False}],
                "footer": {"text": "Operational status only — not a stock signal, not a trade recommendation."},
            }
        ]
    }


def build_operational_failure_email_message(error_summary: str) -> EmailMessage:
    """Build the email for an operational-failure notification. Pure
    function, no network call - same content/footer convention as
    build_operational_failure_payload above, just as plain text instead of
    a Discord embed."""
    sanitized = sanitize_error_summary(error_summary)
    message = EmailMessage()
    message["Subject"] = "Stock Dashboard - Automation Failure"
    message["From"] = ALERT_EMAIL_FROM
    message["To"] = ALERT_EMAIL_TO
    message.set_content(
        f"{OPERATIONAL_FAILURE_HEADLINE}\n\nSummary: {sanitized}\n\n"
        "Operational status only - not a stock signal, not a trade recommendation."
    )
    return message


@dataclass
class OperationalNotificationResult:
    sent: bool                          # True if at least one channel delivered
    reason: str
    email_sent: bool = False
    email_error: Optional[str] = None
    discord_sent: bool = False
    discord_error: Optional[str] = None


def send_operational_failure_notification(
    conn, trading_date: date, error_summary: str, enabled: bool = OPERATIONAL_ALERTS_ENABLED,
) -> OperationalNotificationResult:
    """Send at most one operational-failure notification per trading_date,
    attempting both email and Discord independently - one channel being
    unconfigured or failing never blocks the other. No-op (and no network
    call at all) unless `enabled` is explicitly True."""
    if not enabled:
        return OperationalNotificationResult(sent=False, reason="operational alerts disabled")

    from db.ops_notification_repository import already_sent_today, record_sent

    if already_sent_today(conn, trading_date):
        return OperationalNotificationResult(sent=False, reason="already sent once today")

    email_message = build_operational_failure_email_message(error_summary)
    email_result = send_email_alert(email_message)
    email_sent = email_result.ok
    email_error = email_result.error

    discord_sent = False
    discord_error = None
    if not DISCORD_WEBHOOK_URL:
        discord_error = "DISCORD_WEBHOOK_URL not configured"
    else:
        payload = build_operational_failure_payload(error_summary)
        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10.0)
            discord_sent = 200 <= response.status_code < 300
            if not discord_sent:
                discord_error = f"Discord returned HTTP {response.status_code}"
        except requests.RequestException as e:
            logger.error("operational notification Discord delivery failed: %s", type(e).__name__)
            discord_error = type(e).__name__

    record_sent(
        conn, trading_date, sanitize_error_summary(error_summary),
        email_delivered=email_sent, email_error=email_error,
        discord_delivered=discord_sent, discord_error=discord_error,
    )

    sent = email_sent or discord_sent
    reason = "delivered" if sent else "delivery failed on both channels"
    return OperationalNotificationResult(
        sent=sent, reason=reason,
        email_sent=email_sent, email_error=email_error,
        discord_sent=discord_sent, discord_error=discord_error,
    )
