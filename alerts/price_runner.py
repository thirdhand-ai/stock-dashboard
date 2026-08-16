"""Orchestrates one price-alert cycle: evaluate -> cooldown check ->
persist -> (only if explicitly requested) real delivery over two
independent channels, email and Discord. Mirrors alerts/runner.py's
contract exactly, adapted to the price signal/dual-channel delivery.

Dry-run is the default and the safe path: `send=False` (the default on
every entry point in this module) never performs a real SMTP send or
Discord webhook POST - it only evaluates and persists what *would* have
fired. Sending for real requires the caller to explicitly pass send=True;
nothing here escalates to a real send just because SMTP/DISCORD_WEBHOOK_URL
happens to be configured. Email and Discord delivery are attempted
independently on every real send - one channel failing never skips or
blocks the other, and each channel's delivery status is tracked in its own
price_alerts columns (db/schema.py)."""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from alerts.discord import DeliveryResult as DiscordDeliveryResult, build_price_alert_discord_payload, send_discord_alert
from alerts.email import DeliveryResult as EmailDeliveryResult, build_email_message, send_email_alert
from alerts.price_config import DEFAULT_PRICE_ALERT_CONFIG, PriceAlertConfig, PriceThreshold
from alerts.price_engine import PriceAlertEvaluation, evaluate_price_thresholds
from db.price_alert_repository import (
    get_price_alert_state,
    mark_delivered,
    mark_delivery_failed,
    mark_discord_delivered,
    mark_discord_delivery_failed,
    record_price_alert,
    upsert_price_alert_state,
)

logger = logging.getLogger(__name__)


@dataclass
class PriceAlertRunResult:
    evaluation: PriceAlertEvaluation
    fired: bool                        # reasons detected AND not suppressed by cooldown
    suppressed_by_cooldown: bool = False
    alert_id: Optional[int] = None
    delivery: Optional[EmailDeliveryResult] = None            # email channel (unchanged field name/semantics)
    discord_delivery: Optional[DiscordDeliveryResult] = None  # Discord channel


def _within_cooldown(last_alert_at: Optional[str], cooldown_minutes: int) -> bool:
    if not last_alert_at or cooldown_minutes <= 0:
        return False
    last = datetime.fromisoformat(last_alert_at.replace(" ", "T")).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last < timedelta(minutes=cooldown_minutes)


def _build_message_text(evaluation: PriceAlertEvaluation) -> str:
    reasons = ", ".join(evaluation.reasons)
    prev_price = f"${evaluation.previous_price:.2f}" if evaluation.previous_price is not None else "n/a"
    return (
        f"{evaluation.ticker}: {reasons}. "
        f"Price ${evaluation.current_price:.2f} (was {prev_price}) "
        f"as of {evaluation.data_date} ({evaluation.source})."
    )


def run_price_alert_cycle(
    conn,
    thresholds: Optional[List[PriceThreshold]] = None,
    config: PriceAlertConfig = DEFAULT_PRICE_ALERT_CONFIG,
    send: bool = False,
    persist: bool = True,
) -> List[PriceAlertRunResult]:
    """Evaluate the configured price thresholds, persist any triggered
    alerts, and - only if send=True - attempt real delivery for each one
    over BOTH channels: email (alerts/email.py) and Discord
    (alerts/discord.py). The two channels are independent - each is
    attempted and its result recorded even if the other one failed.

    persist=False makes this a pure, read-only preview: evaluation still
    happens against current data, and the returned PriceAlertRunResults
    still say what fired/would-fire, but nothing is written - no alert row,
    no price_alert_state/cooldown update. persist=False forces send=False
    regardless of the send argument - a preview can never deliver email or
    post to Discord.
    """
    if not persist:
        send = False

    # thresholds=None defaults inside evaluate_price_thresholds itself, to
    # the DB-backed price_alert_config table (dashboard-editable) - no need
    # to duplicate that default-resolution here.
    evaluations = evaluate_price_thresholds(conn, thresholds)
    results = []

    for evaluation in evaluations:
        if not evaluation.ok:
            results.append(PriceAlertRunResult(evaluation=evaluation, fired=False))
            continue

        state = get_price_alert_state(conn, evaluation.ticker)
        last_alert_at = state["last_alert_at"] if state else None

        suppressed = evaluation.should_alert and _within_cooldown(last_alert_at, config.cooldown_minutes)
        fired = evaluation.should_alert and not suppressed

        alert_id = None
        delivery = None
        discord_delivery = None

        if fired and persist:
            reason = evaluation.reasons[0]
            threshold_value = evaluation.threshold.above if reason == "price_above" else evaluation.threshold.below
            alert_id = record_price_alert(
                conn,
                ticker=evaluation.ticker,
                alert_type="+".join(evaluation.reasons),
                price=evaluation.current_price,
                previous_price=evaluation.previous_price,
                threshold=threshold_value,
                data_date=evaluation.data_date,
                source=evaluation.source,
                message=_build_message_text(evaluation),
                dry_run=not send,
            )

            if send:
                message = build_email_message(evaluation)
                delivery = send_email_alert(message)
                if delivery.ok:
                    mark_delivered(conn, alert_id)
                else:
                    mark_delivery_failed(conn, alert_id, delivery.error or "unknown delivery error")

                discord_payload = build_price_alert_discord_payload(evaluation)
                discord_delivery = send_discord_alert(discord_payload)
                if discord_delivery.ok:
                    mark_discord_delivered(conn, alert_id)
                else:
                    mark_discord_delivery_failed(conn, alert_id, discord_delivery.error or "unknown delivery error")

        if persist:
            upsert_price_alert_state(
                conn, evaluation.ticker,
                price=evaluation.current_price,
                alerted=fired,
            )

        results.append(PriceAlertRunResult(
            evaluation=evaluation, fired=fired, suppressed_by_cooldown=suppressed,
            alert_id=alert_id, delivery=delivery, discord_delivery=discord_delivery,
        ))

    return results
