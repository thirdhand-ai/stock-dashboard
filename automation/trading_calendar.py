"""NYSE trading-day check, backed by pandas_market_calendars.

Uses the maintained NYSE calendar rather than a hardcoded holiday list or a
weekday-only approximation - it correctly accounts for fixed and floating
U.S. market holidays (including ones with no fixed calendar-date rule, like
Good Friday, which isn't a federal holiday and so wouldn't be caught by a
federal-holiday-based check) and holiday observance shifts (e.g. a holiday
landing on a Saturday is observed the preceding Friday).

Running the pipeline on an unhandled/misclassified date is still safe, not
just "probably fine": ingestion upserts are idempotent
(db/schema.py's UNIQUE(ticker, date, source) constraint) and
alerts/engine.py only fires on a genuine score/stage change, so a run
against unchanged data is a harmless no-op - it costs an extra API call and
a run_history row, never a duplicate alert or corrupted state.
"""
from datetime import date
from functools import lru_cache
from typing import List

import pandas_market_calendars as mcal

NYSE_CALENDAR_NAME = "NYSE"


@lru_cache(maxsize=1)
def _get_calendar():
    # Built once per process (the automation CLI runs once and exits, so
    # this is a single construction per invocation, not a hot path).
    return mcal.get_calendar(NYSE_CALENDAR_NAME)


def is_likely_trading_day(check_date: date) -> bool:
    calendar = _get_calendar()
    valid_days = calendar.valid_days(start_date=check_date.isoformat(), end_date=check_date.isoformat())
    return len(valid_days) > 0


def trading_sessions_elapsed(start_date: date, through_date: date) -> int:
    """Count of real NYSE trading sessions strictly AFTER start_date, up to
    and including through_date. Used by strategy_lab/outcome_maturation.py
    (Phase 11 spec B3) to gate forward-return maturation on genuinely
    elapsed trading time rather than calendar-day arithmetic (which
    over/under-counts around weekends/holidays) or on whatever price rows
    happen to already be stored (which a seeded/backfilled dataset could
    make misleadingly "available" before the horizon has really passed)."""
    if through_date <= start_date:
        return 0
    calendar = _get_calendar()
    valid_days = calendar.valid_days(start_date=start_date.isoformat(), end_date=through_date.isoformat())
    valid_dates = {d.date() for d in valid_days}
    return sum(1 for d in valid_dates if d > start_date)


def trading_sessions_between(start_date: date, end_date: date) -> List[date]:
    """All NYSE trading sessions in [start_date, end_date], inclusive,
    ascending. Pure, additive, read-only calendar helper (Phase 12 spec
    §4.1) - a small companion to `trading_sessions_elapsed`, which only
    returns a count; this returns the actual session dates so a caller
    (e.g. ops/data_quality.py's gap detection) can check which specific
    sessions have no corresponding stored data. No behavior change to any
    existing caller of this module."""
    if end_date < start_date:
        return []
    calendar = _get_calendar()
    valid_days = calendar.valid_days(start_date=start_date.isoformat(), end_date=end_date.isoformat())
    return sorted({d.date() for d in valid_days})
