"""Phase 16 Area E: shared, pure row-completeness classification - mirrors
ops/evidence_provenance.py's precedent (tiny, self-contained, no DB access,
no schema, exactly one definition, importable by Areas B/D/E and the
dashboard alike).

Distinct from ops/evidence_classification.py (trading-DAY evidence
SUFFICIENCY, untouched by Phase 16) and ops/evidence_provenance.py (a single
UNKNOWN_LEGACY label for one column). This module classifies whether a
SINGLE row's own provenance/required fields are complete, partial, a known
pre-provenance legacy row, or structurally invalid - for observations,
events, and outcomes respectively.
"""
from typing import Optional

from strategy_lab.outcome_maturation import STATUS_MATURED, STATUS_PENDING, STATUS_UNAVAILABLE
from strategy_lab.prospective_events import ALL_EVENT_TYPES

COMPLETENESS_COMPLETE = "COMPLETE"
COMPLETENESS_PARTIAL = "PARTIAL"
COMPLETENESS_LEGACY_INCOMPLETE = "LEGACY_INCOMPLETE"
COMPLETENESS_INVALID = "INVALID"

_VALID_OUTCOME_STATUSES = {STATUS_PENDING, STATUS_MATURED, STATUS_UNAVAILABLE}


def classify_observation_row(row: dict) -> str:
    """Checked in this exact priority order (first match wins):
    INVALID:            score is None or stage is None or close is None
                         or not row.get("observation_date") or not row.get("ticker")
    LEGACY_INCOMPLETE:   row.get("methodology_version") is None
    COMPLETE:            regime is not None and source is not None
                          and methodology_version is not None
                          and config_fingerprint is not None
    PARTIAL:             everything else (methodology_version known, but
                          regime/source/config_fingerprint has >=1 gap -
                          e.g. a modern row from a run whose
                          config_fingerprint capture failed fail-open,
                          Phase 15 §0.5 item 4)."""
    if (
        row.get("score") is None or row.get("stage") is None or row.get("close") is None
        or not row.get("observation_date") or not row.get("ticker")
    ):
        return COMPLETENESS_INVALID
    if row.get("methodology_version") is None:
        return COMPLETENESS_LEGACY_INCOMPLETE
    if (
        row.get("regime") is not None and row.get("source") is not None
        and row.get("methodology_version") is not None and row.get("config_fingerprint") is not None
    ):
        return COMPLETENESS_COMPLETE
    return COMPLETENESS_PARTIAL


def classify_event_row(row: dict) -> str:
    """INVALID: event_type not in strategy_lab.prospective_events.ALL_EVENT_TYPES
                or not row.get("event_date") or not row.get("ticker")
    LEGACY_INCOMPLETE: methodology_version is None
    COMPLETE: methodology_version, source, config_fingerprint all not None
    PARTIAL: otherwise."""
    if (
        row.get("event_type") not in ALL_EVENT_TYPES
        or not row.get("event_date") or not row.get("ticker")
    ):
        return COMPLETENESS_INVALID
    if row.get("methodology_version") is None:
        return COMPLETENESS_LEGACY_INCOMPLETE
    if (
        row.get("methodology_version") is not None and row.get("source") is not None
        and row.get("config_fingerprint") is not None
    ):
        return COMPLETENESS_COMPLETE
    return COMPLETENESS_PARTIAL


def classify_outcome_row(row: dict) -> str:
    """INVALID: status not in {STATUS_PENDING, STATUS_MATURED, STATUS_UNAVAILABLE}
                or (status == STATUS_MATURED and (realized_return is None or exit_date is None))
    LEGACY_INCOMPLETE: methodology_version is None
    COMPLETE: methodology_version, source, config_fingerprint all not None
              AND (status == STATUS_PENDING OR
                   (source_resolution_method is not None and effective_price_source_used is not None))
    PARTIAL: otherwise."""
    status = row.get("status")
    if status not in _VALID_OUTCOME_STATUSES:
        return COMPLETENESS_INVALID
    if status == STATUS_MATURED and (row.get("realized_return") is None or row.get("exit_date") is None):
        return COMPLETENESS_INVALID
    if row.get("methodology_version") is None:
        return COMPLETENESS_LEGACY_INCOMPLETE
    if (
        row.get("methodology_version") is not None and row.get("source") is not None
        and row.get("config_fingerprint") is not None
        and (
            status == STATUS_PENDING
            or (row.get("source_resolution_method") is not None and row.get("effective_price_source_used") is not None)
        )
    ):
        return COMPLETENESS_COMPLETE
    return COMPLETENESS_PARTIAL
