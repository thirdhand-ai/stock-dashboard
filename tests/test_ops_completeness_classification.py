"""Tests for Phase 16 Area E: ops/completeness_classification.py
(docs/specs/phase16.md §5.6 item 1, §8 item 7).

Table-driven: all 4 states (COMPLETE/PARTIAL/LEGACY_INCOMPLETE/INVALID) for
each of the 3 row shapes (observation/event/outcome), including the real
id=1 AAPL row shape.
"""
import pytest

from ops.completeness_classification import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_INVALID,
    COMPLETENESS_LEGACY_INCOMPLETE,
    COMPLETENESS_PARTIAL,
    classify_event_row,
    classify_observation_row,
    classify_outcome_row,
)
from strategy_lab.outcome_maturation import STATUS_MATURED, STATUS_PENDING, STATUS_UNAVAILABLE
from strategy_lab.prospective_events import EVENT_CONTROL_ENTRY

# --- classify_observation_row ---

_BASE_OBS = {
    "score": 80.0, "stage": "volume", "close": 100.0, "observation_date": "2026-08-12", "ticker": "AAPL",
    "regime": "bullish_trend", "source": "alpaca_adjusted", "methodology_version": "phase11-v1",
    "config_fingerprint": "fp1",
}


@pytest.mark.parametrize("overrides,expected", [
    ({}, COMPLETENESS_COMPLETE),
    ({"score": None}, COMPLETENESS_INVALID),
    ({"stage": None}, COMPLETENESS_INVALID),
    ({"close": None}, COMPLETENESS_INVALID),
    ({"observation_date": None}, COMPLETENESS_INVALID),
    ({"observation_date": ""}, COMPLETENESS_INVALID),
    ({"ticker": None}, COMPLETENESS_INVALID),
    ({"ticker": ""}, COMPLETENESS_INVALID),
    ({"methodology_version": None}, COMPLETENESS_LEGACY_INCOMPLETE),
    ({"regime": None}, COMPLETENESS_PARTIAL),
    ({"source": None}, COMPLETENESS_PARTIAL),
    ({"config_fingerprint": None}, COMPLETENESS_PARTIAL),
    ({"regime": None, "source": None}, COMPLETENESS_PARTIAL),
])
def test_classify_observation_row_table(overrides, expected):
    row = dict(_BASE_OBS)
    row.update(overrides)
    assert classify_observation_row(row) == expected


def test_classify_observation_row_priority_invalid_wins_over_legacy_incomplete():
    """A row that is BOTH invalid (score None) AND has methodology_version=None
    must classify INVALID (first match wins), not LEGACY_INCOMPLETE."""
    row = dict(_BASE_OBS)
    row["score"] = None
    row["methodology_version"] = None
    assert classify_observation_row(row) == COMPLETENESS_INVALID


def test_classify_observation_row_real_id1_aapl_shape_is_legacy_incomplete():
    """The exact real, confirmed id=1 AAPL row shape (live DB): source=NULL,
    methodology_version=NULL, config_fingerprint=NULL, regime='bullish_trend'
    (already reconstructed out-of-band per docs/specs/phase16.md §0.1)."""
    row = {
        "score": 0.0, "stage": "none", "close": 301.36, "observation_date": "2026-08-12", "ticker": "AAPL",
        "regime": "bullish_trend", "source": None, "methodology_version": None, "config_fingerprint": None,
    }
    assert classify_observation_row(row) == COMPLETENESS_LEGACY_INCOMPLETE


# --- classify_event_row ---

_BASE_EVENT = {
    "event_type": EVENT_CONTROL_ENTRY, "event_date": "2026-08-12", "ticker": "AAPL",
    "methodology_version": "phase11-v1", "source": "alpaca_adjusted", "config_fingerprint": "fp1",
}


@pytest.mark.parametrize("overrides,expected", [
    ({}, COMPLETENESS_COMPLETE),
    ({"event_type": "not_a_real_event_type"}, COMPLETENESS_INVALID),
    ({"event_date": None}, COMPLETENESS_INVALID),
    ({"event_date": ""}, COMPLETENESS_INVALID),
    ({"ticker": None}, COMPLETENESS_INVALID),
    ({"methodology_version": None}, COMPLETENESS_LEGACY_INCOMPLETE),
    ({"source": None}, COMPLETENESS_PARTIAL),
    ({"config_fingerprint": None}, COMPLETENESS_PARTIAL),
])
def test_classify_event_row_table(overrides, expected):
    row = dict(_BASE_EVENT)
    row.update(overrides)
    assert classify_event_row(row) == expected


def test_classify_event_row_legacy_null_fingerprint_shape():
    """A legacy event (config_fingerprint=NULL) with methodology_version present
    is PARTIAL, not INVALID or COMPLETE - and event_type attribution is
    unaffected by the NULL provenance column."""
    row = dict(_BASE_EVENT)
    row["config_fingerprint"] = None
    assert classify_event_row(row) == COMPLETENESS_PARTIAL
    assert row["event_type"] == EVENT_CONTROL_ENTRY  # attribution unaffected


# --- classify_outcome_row ---

_BASE_OUTCOME_MATURED = {
    "status": STATUS_MATURED, "realized_return": 0.05, "exit_date": "2026-08-13",
    "methodology_version": "phase11-v1", "source": "alpaca_adjusted", "config_fingerprint": "fp1",
    "source_resolution_method": "explicit", "effective_price_source_used": "alpaca_adjusted",
}


@pytest.mark.parametrize("overrides,expected", [
    ({}, COMPLETENESS_COMPLETE),
    ({"status": "bogus_status"}, COMPLETENESS_INVALID),
    ({"realized_return": None}, COMPLETENESS_INVALID),
    ({"exit_date": None}, COMPLETENESS_INVALID),
    ({"methodology_version": None}, COMPLETENESS_LEGACY_INCOMPLETE),
    ({"source": None}, COMPLETENESS_PARTIAL),
    ({"config_fingerprint": None}, COMPLETENESS_PARTIAL),
    ({"source_resolution_method": None}, COMPLETENESS_PARTIAL),
    ({"effective_price_source_used": None}, COMPLETENESS_PARTIAL),
])
def test_classify_outcome_row_matured_table(overrides, expected):
    row = dict(_BASE_OUTCOME_MATURED)
    row.update(overrides)
    assert classify_outcome_row(row) == expected


def test_classify_outcome_row_pending_complete_without_resolution_fields():
    """A PENDING outcome is COMPLETE as long as methodology_version/source/
    config_fingerprint are known - source_resolution_method/
    effective_price_source_used are correctly allowed to be NULL for PENDING
    (nothing about pricing has happened yet, by design)."""
    row = {
        "status": STATUS_PENDING, "realized_return": None, "exit_date": None,
        "methodology_version": "phase11-v1", "source": "alpaca_adjusted", "config_fingerprint": "fp1",
        "source_resolution_method": None, "effective_price_source_used": None,
    }
    assert classify_outcome_row(row) == COMPLETENESS_COMPLETE


def test_classify_outcome_row_pending_legacy_incomplete():
    row = {
        "status": STATUS_PENDING, "realized_return": None, "exit_date": None,
        "methodology_version": None, "source": None, "config_fingerprint": None,
        "source_resolution_method": None, "effective_price_source_used": None,
    }
    assert classify_outcome_row(row) == COMPLETENESS_LEGACY_INCOMPLETE


def test_classify_outcome_row_unavailable_status_with_full_provenance_is_partial():
    """UNAVAILABLE status is a valid status but is not STATUS_PENDING, so the
    COMPLETE branch requires source_resolution_method/effective_price_source_used
    to both be set too."""
    row = {
        "status": STATUS_UNAVAILABLE, "realized_return": None, "exit_date": None,
        "methodology_version": "phase11-v1", "source": "alpaca_adjusted", "config_fingerprint": "fp1",
        "source_resolution_method": None, "effective_price_source_used": None,
    }
    assert classify_outcome_row(row) == COMPLETENESS_PARTIAL


def test_classify_outcome_row_unavailable_status_never_reaches_complete_since_effective_source_is_legitimately_null():
    """A genuinely UNAVAILABLE outcome (SOURCE_RESOLUTION_UNAVAILABLE means
    resolve_source() found NOTHING) correctly has effective_price_source_used=None
    - by classify_outcome_row's own exact priority order (spec Sec5.1) this
    means an UNAVAILABLE-status row can never classify COMPLETE, only PARTIAL
    at best, even with full methodology_version/source/config_fingerprint
    provenance. Documented behavior, not a test bug - flagged here so a future
    reader doesn't "fix" classify_outcome_row to special-case this."""
    row = {
        "status": STATUS_UNAVAILABLE, "realized_return": None, "exit_date": None,
        "methodology_version": "phase11-v1", "source": "alpaca_adjusted", "config_fingerprint": "fp1",
        "source_resolution_method": "unavailable", "effective_price_source_used": None,
    }
    assert classify_outcome_row(row) == COMPLETENESS_PARTIAL


def test_classify_outcome_row_matured_but_missing_realized_return_is_invalid_even_with_full_provenance():
    row = dict(_BASE_OUTCOME_MATURED)
    row["realized_return"] = None
    assert classify_outcome_row(row) == COMPLETENESS_INVALID
