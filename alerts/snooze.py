"""Pure snooze-duration resolution logic - no DB, no Streamlit. Kept
separate from db/alert_snooze_repository.py so "what UTC timestamp does
this preset/custom pick resolve to" is unit-testable without a database
connection, same separation alerts/price_config.py's resolve_percent_band
keeps from db/price_alert_config_repository.py.

Every other timestamp column in this codebase (triggered_at, last_alert_at,
sent_at, ...) is written via SQLite's own datetime('now'), which is always
UTC regardless of the host machine's timezone. snoozed_until has to be
stored in that same UTC format to compare correctly against it - so a
dashboard date/time picker's input (implicitly the operator's local wall
clock - see deploy/README.md's note that this project assumes
America/New_York) must be converted to UTC here before it's ever written,
not left naive.
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# Ordered (dict preserves insertion order) so dashboard selectboxes list
# shortest-to-longest without needing a separate sort.
SNOOZE_PRESETS = {
    "1 hour": timedelta(hours=1),
    "4 hours": timedelta(hours=4),
    "24 hours": timedelta(hours=24),
    "3 days": timedelta(days=3),
    "1 week": timedelta(days=7),
}


def resolve_preset_snoozed_until(preset_label: str, now: Optional[datetime] = None) -> str:
    """now defaults to the real current UTC time; a caller-supplied now= is
    for tests only, so preset resolution is deterministic without freezing
    the system clock (same pattern alerts/price_runner.py's
    _within_cooldown takes a real datetime.now(timezone.utc) call, just
    made injectable here for testability)."""
    if preset_label not in SNOOZE_PRESETS:
        raise ValueError(f"unknown snooze preset: {preset_label!r}")
    base = now if now is not None else datetime.now(timezone.utc)
    return (base + SNOOZE_PRESETS[preset_label]).strftime(TIMESTAMP_FORMAT)


def resolve_custom_snoozed_until(local_date: date, local_time: time, local_tzinfo) -> str:
    """Interprets local_date/local_time as wall-clock time in local_tzinfo
    (the dashboard passes the host machine's local timezone) and converts
    to the UTC string format every timestamp in this codebase's SQLite
    tables uses."""
    local_dt = datetime.combine(local_date, local_time, tzinfo=local_tzinfo)
    return local_dt.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def is_in_the_past(snoozed_until: str, now: Optional[datetime] = None) -> bool:
    """Validation helper: a snooze whose end time is already in the past
    would never suppress anything - dashboard views reject this before
    ever calling create_snooze, rather than silently accepting a no-op
    snooze."""
    base = now if now is not None else datetime.now(timezone.utc)
    until = datetime.strptime(snoozed_until, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    return until <= base


def format_snoozed_until_local(snoozed_until: str, local_tzinfo) -> str:
    """Display helper: converts a stored UTC snoozed_until back to the
    host machine's local time, for showing on the config pages/Alert
    Activity - operators think in wall-clock time, not UTC."""
    until_utc = datetime.strptime(snoozed_until, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    local = until_utc.astimezone(local_tzinfo)
    return local.strftime("%Y-%m-%d %H:%M %Z")
