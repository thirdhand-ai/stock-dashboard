"""Orchestrates one alert cycle: evaluate -> cooldown check -> persist ->
(only if explicitly requested) real Discord delivery.

Dry-run is the default and the safe path: `send=False` (the default on
every entry point in this module) never imports a live network call path
for delivery - it only evaluates and persists what *would* have fired.
Sending for real requires the caller to explicitly pass send=True; nothing
here escalates to a real send just because DISCORD_WEBHOOK_URL happens to
be set.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from alerts.config import DEFAULT_ALERT_CONFIG, AlertConfig
from alerts.discord import DeliveryResult, build_discord_payload, send_discord_alert
from alerts.engine import AlertEvaluation, evaluate_watchlist
from db.alert_repository import get_alert_state, mark_delivered, mark_delivery_failed, record_alert, upsert_alert_state

logger = logging.getLogger(__name__)


@dataclass
class AlertRunResult:
    evaluation: AlertEvaluation
    fired: bool                        # reasons detected AND not suppressed by cooldown
    suppressed_by_cooldown: bool = False
    alert_id: Optional[int] = None
    delivery: Optional[DeliveryResult] = None


def _within_cooldown(last_alert_at: Optional[str], cooldown_minutes: int) -> bool:
    if not last_alert_at or cooldown_minutes <= 0:
        return False
    last = datetime.fromisoformat(last_alert_at.replace(" ", "T")).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last < timedelta(minutes=cooldown_minutes)


def _build_message_text(evaluation: AlertEvaluation) -> str:
    reasons = ", ".join(evaluation.reasons)
    prev_score = f"{evaluation.previous_score:.0f}" if evaluation.previous_score is not None else "n/a"
    return (
        f"{evaluation.ticker}: {reasons}. "
        f"Score {evaluation.current_score:.0f}/100 (was {prev_score}). "
        f"Stage {evaluation.current_stage} (was {evaluation.previous_stage}). "
        f"Price ${evaluation.price:.2f} as of {evaluation.data_date} ({evaluation.source})."
    )


def run_alert_cycle(
    conn,
    tickers: Optional[List[str]] = None,
    config: AlertConfig = DEFAULT_ALERT_CONFIG,
    send: bool = False,
    persist: bool = True,
) -> List[AlertRunResult]:
    """Evaluate the watchlist, persist any triggered alerts, and - only if
    send=True - attempt real Discord delivery for each one.

    persist=False makes this a pure, read-only preview: evaluation still
    happens against current data, and the returned AlertRunResults still
    say what fired/would-fire, but nothing is written - no alert row, no
    alert_state/cooldown update. This exists for recovery-preview use
    (see automation/recovery.py): a preview must never consume/mask a
    genuine crossing that a subsequent real (persist=True) run needs to
    still see and act on. persist=False forces send=False regardless of
    the send argument - a preview can never deliver to Discord.
    """
    if not persist:
        send = False

    evaluations = evaluate_watchlist(conn, tickers, config)
    results = []

    for evaluation in evaluations:
        if not evaluation.ok:
            results.append(AlertRunResult(evaluation=evaluation, fired=False))
            continue

        state = get_alert_state(conn, evaluation.ticker)
        last_alert_at = state["last_alert_at"] if state else None

        suppressed = evaluation.should_alert and _within_cooldown(last_alert_at, config.cooldown_minutes)
        fired = evaluation.should_alert and not suppressed

        alert_id = None
        delivery = None

        if fired and persist:
            alert_id = record_alert(
                conn,
                ticker=evaluation.ticker,
                alert_type="+".join(evaluation.reasons),
                score=evaluation.current_score,
                previous_score=evaluation.previous_score,
                highest_confirmed_stage=evaluation.current_stage,
                previous_stage=evaluation.previous_stage,
                data_date=evaluation.data_date,
                source=evaluation.source,
                message=_build_message_text(evaluation),
                dry_run=not send,
            )

            if send:
                payload = build_discord_payload(evaluation)
                delivery = send_discord_alert(payload)
                if delivery.ok:
                    mark_delivered(conn, alert_id)
                else:
                    mark_delivery_failed(conn, alert_id, delivery.error or "unknown delivery error")

        if persist:
            upsert_alert_state(
                conn, evaluation.ticker,
                score=evaluation.current_score, stage=evaluation.current_stage,
                alerted=fired,
            )

        results.append(AlertRunResult(
            evaluation=evaluation, fired=fired, suppressed_by_cooldown=suppressed,
            alert_id=alert_id, delivery=delivery,
        ))

    return results
