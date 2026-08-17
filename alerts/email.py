"""SMTP email delivery for price-threshold alerts. Mirrors alerts/discord.py's
contract: build_email_message() is pure (no network call, reused by both a
real send and dry-run reporting), send_email_alert() does the actual
network call and fails safe (returns ok=False) rather than raising if SMTP
isn't configured.

SMTP_* / ALERT_EMAIL_* are read once from config.settings (loaded from the
environment via python-dotenv - see config/settings.py). The password is
never included in a message, a DeliveryResult, a log line, or an exception
message anywhere in this module.
"""
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional

from config.settings import ALERT_EMAIL_FROM, ALERT_EMAIL_TO, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_USERNAME

logger = logging.getLogger(__name__)

REASON_LABELS = {
    "price_above": "crossed above",
    "price_below": "crossed below",
}

VOLATILITY_REASON_LABELS = {
    "volatility_up": "jumped",
    "volatility_down": "dropped",
}


@dataclass
class DeliveryResult:
    ok: bool
    dry_run: bool
    error: Optional[str] = None


def build_email_message(evaluation) -> EmailMessage:
    """Build the email for one triggered PriceAlertEvaluation. Pure
    function, no network call - reused by both a real send and by dry-run
    reporting so what you preview is what would be sent."""
    reason = evaluation.reasons[0] if evaluation.reasons else ""
    direction = REASON_LABELS.get(reason, reason)
    threshold_value = evaluation.threshold.above if reason == "price_above" else evaluation.threshold.below
    prev_price_text = f" (was ${evaluation.previous_price:,.2f})" if evaluation.previous_price is not None else ""

    subject = f"{evaluation.ticker} {direction} ${threshold_value:,.2f}"
    body = (
        f"{evaluation.ticker} {direction} your configured threshold of ${threshold_value:,.2f}.\n\n"
        f"Current price: ${evaluation.current_price:,.2f}{prev_price_text}\n"
        f"As of: {evaluation.data_date} ({evaluation.source})\n\n"
        "Signal-monitoring alert only - not an executed trade, not financial advice."
    )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = ALERT_EMAIL_FROM
    message["To"] = ALERT_EMAIL_TO
    message.set_content(body)
    return message


def build_volatility_email_message(evaluation) -> EmailMessage:
    """Build the email for one triggered VolatilityAlertEvaluation. Pure
    function, no network call - reused by both a real send and by dry-run
    reporting, same convention as build_email_message above."""
    reason = evaluation.reasons[0] if evaluation.reasons else ""
    direction = VOLATILITY_REASON_LABELS.get(reason, reason)
    move_pct = evaluation.move_pct if evaluation.move_pct is not None else 0.0

    subject = f"{evaluation.ticker} {direction} {move_pct:+.2f}% today"
    body = (
        f"{evaluation.ticker} moved {abs(move_pct):.2f}% day-over-day, beyond your configured "
        f"{evaluation.threshold_percent:.2f}% volatility threshold.\n\n"
        f"Close: ${evaluation.current_price:,.2f} (was ${evaluation.previous_price:,.2f})\n"
        f"As of: {evaluation.data_date} (prev {evaluation.previous_date}, {evaluation.source})\n\n"
        "Signal-monitoring alert only - not an executed trade, not financial advice."
    )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = ALERT_EMAIL_FROM
    message["To"] = ALERT_EMAIL_TO
    message.set_content(body)
    return message


def build_daily_digest_email_message(body: str) -> EmailMessage:
    """Build the email for the daily digest. Pure function, no network
    call - the body text is already fully formatted by
    alerts/daily_digest_engine.py::format_digest_body, so this just wraps
    it in a subject/from/to envelope, same convention as
    build_email_message above."""
    message = EmailMessage()
    message["Subject"] = "Stock Dashboard - Daily Digest"
    message["From"] = ALERT_EMAIL_FROM
    message["To"] = ALERT_EMAIL_TO
    message.set_content(body)
    return message


def send_email_alert(message: EmailMessage, timeout: float = 10.0) -> DeliveryResult:
    """Real SMTP send. Fails safely (returns ok=False) rather than raising
    if SMTP isn't configured - the caller decides how to record that."""
    if not (SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and ALERT_EMAIL_FROM and ALERT_EMAIL_TO):
        return DeliveryResult(ok=False, dry_run=False, error="SMTP/email settings are not fully configured")

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=timeout) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(message)
        return DeliveryResult(ok=True, dry_run=False)
    except (smtplib.SMTPException, OSError) as e:
        return DeliveryResult(ok=False, dry_run=False, error=type(e).__name__)
