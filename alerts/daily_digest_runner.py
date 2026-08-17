"""Orchestrates one daily-digest cycle: build content -> de-dupe (at most
once per trading_date) -> persist -> (only if explicitly requested) real
delivery over email + Discord, independently. Mirrors alerts/price_runner.py
and alerts/volatility_runner.py's dry-run-by-default contract, but with no
crossing/threshold decision at all - the digest always "fires" (subject
only to the enabled toggle and the once-per-day de-dupe), covering every
given ticker unconditionally.
"""
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

from alerts.daily_digest_engine import DigestTickerRow, build_daily_digest, format_digest_body
from alerts.discord import DeliveryResult as DiscordDeliveryResult, build_daily_digest_discord_payload, send_discord_alert
from alerts.email import DeliveryResult as EmailDeliveryResult, build_daily_digest_email_message, send_email_alert
from db.daily_digest_repository import (
    already_sent_today,
    mark_delivered,
    mark_delivery_failed,
    mark_discord_delivered,
    mark_discord_delivery_failed,
    record_digest_sent,
)

logger = logging.getLogger(__name__)


@dataclass
class DailyDigestRunResult:
    rows: List[DigestTickerRow] = field(default_factory=list)
    sent: bool = False               # digest content was actually delivered/persisted this call
    reason: str = ""
    digest_id: Optional[int] = None
    delivery: Optional[EmailDeliveryResult] = None
    discord_delivery: Optional[DiscordDeliveryResult] = None


def run_daily_digest(
    conn,
    tickers: List[str],
    price_thresholds_by_ticker: Optional[dict] = None,
    volatility_configs_by_ticker: Optional[dict] = None,
    trading_date: Optional[date] = None,
    send: bool = False,
    persist: bool = True,
) -> DailyDigestRunResult:
    """Build the digest for `tickers` and, if not already sent today,
    persist the send-log record and (only if send=True) actually deliver
    over email + Discord independently.

    persist=False makes this a pure, read-only preview - forces send=False
    regardless of the send argument, same contract run_price_alert_cycle/
    run_volatility_alert_cycle use, and never touches daily_digest_log (so
    a preview never counts against the once-per-day de-dupe)."""
    if not persist:
        send = False

    trading_date_str = (trading_date or date.today()).isoformat()
    rows = build_daily_digest(conn, tickers, price_thresholds_by_ticker, volatility_configs_by_ticker)

    if persist and already_sent_today(conn, trading_date_str):
        return DailyDigestRunResult(rows=rows, sent=False, reason="already sent once today")

    digest_id = None
    delivery = None
    discord_delivery = None

    if persist:
        digest_id = record_digest_sent(conn, trading_date_str, ticker_count=len(rows), dry_run=not send)

        if send:
            body = format_digest_body(rows, trading_date_str)
            email_message = build_daily_digest_email_message(body)
            delivery = send_email_alert(email_message)
            if delivery.ok:
                mark_delivered(conn, digest_id)
            else:
                mark_delivery_failed(conn, digest_id, delivery.error or "unknown delivery error")

            discord_payload = build_daily_digest_discord_payload(rows, trading_date_str)
            discord_delivery = send_discord_alert(discord_payload)
            if discord_delivery.ok:
                mark_discord_delivered(conn, digest_id)
            else:
                mark_discord_delivery_failed(conn, digest_id, discord_delivery.error or "unknown delivery error")

    return DailyDigestRunResult(
        rows=rows, sent=True, reason="delivered" if persist else "preview (not persisted)",
        digest_id=digest_id, delivery=delivery, discord_delivery=discord_delivery,
    )
