"""Dedicated, isolated path for sending a single controlled Discord TEST
notification - used to verify webhook delivery end-to-end.

This module is intentionally NOT part of normal alert evaluation:
  - it never calls evaluate_ticker/evaluate_watchlist and never touches
    AAPL/MSFT (or any real ticker's) alert_state
  - it never reads or manipulates real market/indicator/score data
  - if persisted, it writes under ticker="TEST-NOTIFICATION" and
    alert_type="test_notification" - values that can never collide with or
    be mistaken for a real "score_crossing"/"stage_advance" alert on an
    actual watchlist ticker

It reuses alerts.discord.send_discord_alert() - the exact same delivery
function real alerts use - so a successful test proves the real delivery
path works, without reusing any evaluation/state logic that could
contaminate real ticker data.
"""
from datetime import datetime, timezone

from alerts.discord import DeliveryResult, send_discord_alert
from db.alert_repository import mark_delivered, mark_delivery_failed, record_alert

TEST_TICKER_LABEL = "TEST-NOTIFICATION"
TEST_ALERT_TYPE = "test_notification"


def build_test_payload() -> dict:
    """Obviously-labeled sample payload - every value here is a fixed
    placeholder, never derived from any real ticker or market data."""
    return {
        "embeds": [
            {
                "title": "\U0001f9ea TEST ALERT — Notification Delivery Check",
                "description": (
                    "This is a **TEST ALERT** confirming Discord delivery is working correctly. "
                    "It is **not a real market signal and not an executed trade**. "
                    "Every value below is sample/placeholder data, not derived from any actual "
                    "ticker or current market conditions."
                ),
                "color": 0x898781,  # neutral gray - deliberately distinct from a real alert's blue
                "fields": [
                    {"name": "Sample ticker", "value": "TEST (placeholder, not a real symbol)", "inline": True},
                    {"name": "Sample score", "value": "N/A — test data", "inline": True},
                    {"name": "Sample stage", "value": "N/A — test data", "inline": True},
                    {
                        "name": "Purpose",
                        "value": "One-time manual check that the alert engine can deliver to this Discord webhook.",
                        "inline": False,
                    },
                ],
                "footer": {
                    "text": "TEST ALERT — signal-monitoring notification test only. "
                            "Not financial advice. No trade was placed.",
                },
            }
        ]
    }


def send_test_notification(conn=None, persist: bool = True) -> DeliveryResult:
    """Send exactly one real Discord test notification via the same
    send_discord_alert() real alerts use.

    Does NOT touch alert_state for any ticker (real or otherwise) - it
    never calls upsert_alert_state. If persist=True and a connection is
    given, records exactly one row in `alerts` under the dedicated
    TEST_TICKER_LABEL/TEST_ALERT_TYPE so it is unambiguous in Alert History.
    """
    payload = build_test_payload()
    result = send_discord_alert(payload)

    if persist and conn is not None:
        alert_id = record_alert(
            conn,
            ticker=TEST_TICKER_LABEL,
            alert_type=TEST_ALERT_TYPE,
            score=0.0,
            previous_score=None,
            highest_confirmed_stage="none",
            previous_stage=None,
            data_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            source="manual_test",
            message=(
                "TEST ALERT: manual Discord delivery verification, triggered deliberately by the "
                "operator. Not a real market signal, not derived from any ticker's data, no trade placed."
            ),
            dry_run=False,  # this is a genuine real-send attempt, not a dry run
        )
        if result.ok:
            mark_delivered(conn, alert_id)
        else:
            mark_delivery_failed(conn, alert_id, result.error or "unknown delivery error")

    return result
