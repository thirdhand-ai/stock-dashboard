"""Tests for alerts/snooze.py's pure preset/custom resolution logic - no
DB, no Streamlit. See tests/test_alert_snooze_repository.py for the
DB-integration side (create/delete/expiry/precedence)."""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from alerts.snooze import (
    SNOOZE_PRESETS,
    format_snoozed_until_local,
    is_in_the_past,
    resolve_custom_snoozed_until,
    resolve_preset_snoozed_until,
)


def test_resolve_preset_snoozed_until_adds_the_preset_duration():
    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    result = resolve_preset_snoozed_until("1 hour", now=now)
    assert result == "2026-08-17 13:00:00"


def test_resolve_preset_snoozed_until_supports_every_preset_label():
    now = datetime(2026, 8, 17, 0, 0, 0, tzinfo=timezone.utc)
    for label, delta in SNOOZE_PRESETS.items():
        result = resolve_preset_snoozed_until(label, now=now)
        assert result == (now + delta).strftime("%Y-%m-%d %H:%M:%S")


def test_resolve_preset_snoozed_until_rejects_unknown_label():
    with pytest.raises(ValueError):
        resolve_preset_snoozed_until("2 fortnights")


def test_resolve_custom_snoozed_until_converts_local_to_utc():
    # 2pm America/New_York in mid-August is EDT (UTC-4) -> 18:00 UTC.
    ny = ZoneInfo("America/New_York")
    result = resolve_custom_snoozed_until(date(2026, 8, 20), time(14, 0, 0), ny)
    assert result == "2026-08-20 18:00:00"


def test_is_in_the_past_true_for_a_past_timestamp():
    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert is_in_the_past("2026-08-17 11:59:59", now=now) is True


def test_is_in_the_past_false_for_a_future_timestamp():
    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert is_in_the_past("2026-08-17 12:00:01", now=now) is False


def test_is_in_the_past_true_when_exactly_equal_to_now():
    # A snooze ending exactly "now" would suppress nothing on the very next
    # evaluation - treated as already past, same boundary choice
    # alerts/volatility_runner.py's already_alerted_today uses (>=, not >).
    now = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert is_in_the_past("2026-08-17 12:00:00", now=now) is True


def test_format_snoozed_until_local_converts_utc_back_to_local():
    ny = ZoneInfo("America/New_York")
    result = format_snoozed_until_local("2026-08-20 18:00:00", ny)
    assert result.startswith("2026-08-20 14:00")
