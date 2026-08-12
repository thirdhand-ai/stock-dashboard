"""Optional operational-failure Discord notification - structurally
SEPARATE from stock-signal alerts (see alerts/discord.py, alerts/engine.py).

This module never contains stock recommendation content, never reads
scores/stages/prices, and is disabled by default: OPERATIONAL_ALERTS_ENABLED
must be explicitly flipped to True (a deliberate code change, not an env
var an operator could set by accident) before send_operational_failure_notification()
will ever perform a real network call. Until that happens this is inert -
prepared for future use per the 2026-08-12 incident review, not activated.

At most one notification per failed scheduled run/day: the caller is
expected to pass the trading date, and already_sent_today() records/checks
that in operational_notifications so a flaky day generating several failed
runs still only pages once.
"""
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

import requests

from config.settings import DISCORD_WEBHOOK_URL

logger = logging.getLogger(__name__)

# Deliberately hardcoded off. Flipping this requires an explicit code
# change and review - not something a config file or env var flips
# silently. Do not wire a scheduler/cron to assume this is on.
OPERATIONAL_ALERTS_ENABLED = False

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


@dataclass
class OperationalNotificationResult:
    sent: bool
    reason: str


def send_operational_failure_notification(
    conn, trading_date: date, error_summary: str, enabled: bool = OPERATIONAL_ALERTS_ENABLED,
) -> OperationalNotificationResult:
    """Send at most one operational-failure notification per trading_date.
    No-op (and no network call at all) unless BOTH `enabled` is explicitly
    True and DISCORD_WEBHOOK_URL is configured."""
    if not enabled:
        return OperationalNotificationResult(sent=False, reason="operational alerts disabled")

    from db.ops_notification_repository import already_sent_today, record_sent

    if already_sent_today(conn, trading_date):
        return OperationalNotificationResult(sent=False, reason="already sent once today")

    if not DISCORD_WEBHOOK_URL:
        return OperationalNotificationResult(sent=False, reason="DISCORD_WEBHOOK_URL not configured")

    payload = build_operational_failure_payload(error_summary)
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10.0)
        ok = 200 <= response.status_code < 300
    except requests.RequestException as e:
        logger.error("operational notification delivery failed: %s", type(e).__name__)
        ok = False

    record_sent(conn, trading_date, sanitize_error_summary(error_summary))
    return OperationalNotificationResult(sent=ok, reason="delivered" if ok else "delivery failed")
