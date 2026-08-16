"""Orchestrates one volatility-alert cycle: evaluate -> de-dupe/cooldown ->
persist -> (only if explicitly requested) real delivery over the same two
independent channels alerts/price_runner.py uses, email and Discord. Mirrors
alerts/price_runner.py's contract exactly, adapted to the day-over-day move
signal.

Two layers of repeat-alert suppression, same "cooldown is secondary
protection, never a substitute for correct detection" split alerts/
price_runner.py documents:
  - primary: a given trading day's move (evaluation.data_date) fires at
    most once - re-running the evaluation later the same day (no new price
    bar yet) must not re-alert just because the same move is still >=
    threshold.
  - secondary: config.cooldown_minutes, a generic wall-clock backstop.

Dry-run is the default and the safe path: `send=False` (the default on
every entry point in this module) never performs a real SMTP send or
Discord webhook POST.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from alerts.discord import DeliveryResult as DiscordDeliveryResult, build_volatility_alert_discord_payload, send_discord_alert
from alerts.email import DeliveryResult as EmailDeliveryResult, build_volatility_email_message, send_email_alert
from alerts.volatility_config import DEFAULT_VOLATILITY_ALERT_CONFIG, VolatilityAlertConfig, VolatilityAlertRunConfig
from alerts.volatility_engine import VolatilityAlertEvaluation, evaluate_volatility_alerts
from db.volatility_alert_repository import (
    get_volatility_alert_state,
    mark_delivered,
    mark_delivery_failed,
    mark_discord_delivered,
    mark_discord_delivery_failed,
    record_volatility_alert,
    upsert_volatility_alert_state,
)

logger = logging.getLogger(__name__)


@dataclass
class VolatilityAlertRunResult:
    evaluation: VolatilityAlertEvaluation
    fired: bool                        # threshold exceeded AND not suppressed (already-alerted-today or cooldown)
    suppressed_already_alerted_today: bool = False
    suppressed_by_cooldown: bool = False
    alert_id: Optional[int] = None
    delivery: Optional[EmailDeliveryResult] = None
    discord_delivery: Optional[DiscordDeliveryResult] = None


def _within_cooldown(last_alert_at: Optional[str], cooldown_minutes: int) -> bool:
    if not last_alert_at or cooldown_minutes <= 0:
        return False
    last = datetime.fromisoformat(last_alert_at.replace(" ", "T")).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last < timedelta(minutes=cooldown_minutes)


def _build_message_text(evaluation: VolatilityAlertEvaluation) -> str:
    move_pct = evaluation.move_pct if evaluation.move_pct is not None else 0.0
    return (
        f"{evaluation.ticker}: moved {move_pct:+.2f}% (threshold {evaluation.threshold_percent:.2f}%). "
        f"Close ${evaluation.current_price:.2f} (was ${evaluation.previous_price:.2f}) "
        f"as of {evaluation.data_date} ({evaluation.source})."
    )


def run_volatility_alert_cycle(
    conn,
    configs: Optional[List[VolatilityAlertConfig]] = None,
    config: VolatilityAlertRunConfig = DEFAULT_VOLATILITY_ALERT_CONFIG,
    send: bool = False,
    persist: bool = True,
) -> List[VolatilityAlertRunResult]:
    """Evaluate the configured volatility thresholds, persist any triggered
    alerts, and - only if send=True - attempt real delivery for each one
    over both email and Discord, independently.

    persist=False makes this a pure, read-only preview - forces send=False
    regardless of the send argument, same contract run_price_alert_cycle
    uses."""
    if not persist:
        send = False

    evaluations = evaluate_volatility_alerts(conn, configs)
    results = []

    for evaluation in evaluations:
        if not evaluation.ok:
            results.append(VolatilityAlertRunResult(evaluation=evaluation, fired=False))
            continue

        state = get_volatility_alert_state(conn, evaluation.ticker)
        last_alert_at = state["last_alert_at"] if state else None
        last_alert_data_date = state["last_alert_data_date"] if state else None

        already_alerted_today = (
            evaluation.should_alert
            and last_alert_data_date is not None
            and last_alert_data_date == evaluation.data_date
        )
        cooldown_active = evaluation.should_alert and _within_cooldown(last_alert_at, config.cooldown_minutes)
        suppressed = already_alerted_today or cooldown_active
        fired = evaluation.should_alert and not suppressed

        alert_id = None
        delivery = None
        discord_delivery = None

        if fired and persist:
            alert_id = record_volatility_alert(
                conn,
                ticker=evaluation.ticker,
                move_pct=evaluation.move_pct,
                threshold_percent=evaluation.threshold_percent,
                previous_close=evaluation.previous_price,
                current_close=evaluation.current_price,
                previous_date=evaluation.previous_date,
                data_date=evaluation.data_date,
                source=evaluation.source,
                message=_build_message_text(evaluation),
                dry_run=not send,
            )

            if send:
                message = build_volatility_email_message(evaluation)
                delivery = send_email_alert(message)
                if delivery.ok:
                    mark_delivered(conn, alert_id)
                else:
                    mark_delivery_failed(conn, alert_id, delivery.error or "unknown delivery error")

                discord_payload = build_volatility_alert_discord_payload(evaluation)
                discord_delivery = send_discord_alert(discord_payload)
                if discord_delivery.ok:
                    mark_discord_delivered(conn, alert_id)
                else:
                    mark_discord_delivery_failed(conn, alert_id, discord_delivery.error or "unknown delivery error")

        if persist:
            upsert_volatility_alert_state(
                conn, evaluation.ticker,
                alerted=fired,
                data_date=evaluation.data_date if fired else last_alert_data_date,
            )

        results.append(VolatilityAlertRunResult(
            evaluation=evaluation, fired=fired,
            suppressed_already_alerted_today=already_alerted_today,
            suppressed_by_cooldown=cooldown_active,
            alert_id=alert_id, delivery=delivery, discord_delivery=discord_delivery,
        ))

    return results
