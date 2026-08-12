"""Discord webhook delivery.

DISCORD_WEBHOOK_URL is read once from config.settings (which loads it from
the environment via python-dotenv - see config/settings.py). It is never
included in a payload, a DeliveryResult, a log line, or an exception
message anywhere in this module.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import requests

from config.settings import DISCORD_WEBHOOK_URL

logger = logging.getLogger(__name__)

STAGE_EMOJI = {"none": "⚪", "trend": "🔵", "momentum": "🟣", "volume": "🟢"}
REASON_LABELS = {
    "score_crossing": "Score crossed above entry threshold",
    "stage_advance": "Confirmation stage advanced",
}
ALERT_COLOR = 0x3987E5  # matches the dashboard's primary categorical slot


@dataclass
class DeliveryResult:
    ok: bool
    dry_run: bool
    status_code: Optional[int] = None
    error: Optional[str] = None


def build_discord_payload(evaluation) -> dict:
    """Build the Discord webhook JSON payload for one triggered
    AlertEvaluation. Pure function, no network call - reused by both a real
    send and by dry-run reporting so what you preview is what would be sent.
    """
    reasons_text = "; ".join(REASON_LABELS.get(r, r) for r in evaluation.reasons)
    prev_score_text = f" (was {evaluation.previous_score:.0f})" if evaluation.previous_score is not None else ""
    prev_stage_text = f" (was {evaluation.previous_stage})" if evaluation.previous_stage else ""
    stage_emoji = STAGE_EMOJI.get(evaluation.current_stage, "")

    fields = [
        {"name": "Price", "value": f"${evaluation.price:,.2f}", "inline": True},
        {"name": "Score", "value": f"{evaluation.current_score:.0f} / 100{prev_score_text}", "inline": True},
        {"name": "Stage", "value": f"{stage_emoji} {evaluation.current_stage}{prev_stage_text}", "inline": True},
        {
            "name": "Conditions fired",
            "value": ", ".join(evaluation.fired_conditions) if evaluation.fired_conditions else "none",
            "inline": False,
        },
        {"name": "As of", "value": f"{evaluation.data_date} ({evaluation.source})", "inline": True},
    ]

    return {
        "embeds": [
            {
                "title": f"{evaluation.ticker} — Signal Alert",
                "description": reasons_text,
                "color": ALERT_COLOR,
                "fields": fields,
                "footer": {
                    "text": "Signal-monitoring alert only — not an executed trade, not financial advice."
                },
            }
        ]
    }


def send_discord_alert(payload: dict, timeout: float = 10.0) -> DeliveryResult:
    """Real network POST to the configured webhook. Fails safely (returns
    ok=False) rather than raising if the webhook isn't configured - the
    caller decides how to record that."""
    if not DISCORD_WEBHOOK_URL:
        return DeliveryResult(ok=False, dry_run=False, error="DISCORD_WEBHOOK_URL is not configured")

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=timeout)
        if 200 <= response.status_code < 300:
            return DeliveryResult(ok=True, dry_run=False, status_code=response.status_code)
        return DeliveryResult(
            ok=False, dry_run=False, status_code=response.status_code,
            error=f"Discord returned HTTP {response.status_code}",
        )
    except requests.RequestException as e:
        return DeliveryResult(ok=False, dry_run=False, error=type(e).__name__)
