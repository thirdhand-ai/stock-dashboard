"""Phase 16 Area C: event/experiment provenance audit - read-only.

Same read-only convention as ops/correction_impact_audit.py and
ops/regime_reconstruction_audit.py: this module never calls conn.execute/
conn.executemany/conn.executescript/conn.commit, never contains an
INSERT/UPDATE/DELETE SQL string, and never imports record_observation/
record_event/mature_outcomes/_persist. See
tests/test_ops_event_provenance_audit.py's AST/source-text scan.

Confirmed finding (docs/specs/phase16.md §0.2), contradicting the task's
original working hypothesis: strategy_lab.prospective_events.ALL_EVENT_TYPES
ALREADY contains EVENT_CONTROL_ENTRY/EVENT_EXPERIMENT_A_ENTRY/
EVENT_EXPERIMENT_B_ENTRY as three distinct, independently-recorded event
types - there is no entry-side attribution gap, event_type alone
unambiguously identifies CONTROL vs Experiment A vs Experiment B for every
entry event. The real, confirmed gap is that ALL_EVENT_TYPES contains ZERO
event types with "exit" in the name, so Experiment B's sole differentiator
from Experiment A (exit_on_regime_loss=True) is structurally invisible in
event data. This is a coverage gap, not an ambiguity bug, and is flagged as
Open Question #2 in docs/specs/phase16.md §9 rather than built here.
"""
from strategy_lab.prospective_events import (
    ALL_EVENT_TYPES,
    EVENT_CONTROL_ENTRY,
    EVENT_CONTROL_EXIT,
    EVENT_EXPERIMENT_A_ENTRY,
    EVENT_EXPERIMENT_A_EXIT,
    EVENT_EXPERIMENT_B_ENTRY,
    EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS,
    EVENT_EXPERIMENT_B_EXIT_TECHNICAL,
    load_events,
)

from ops.evidence_provenance import PROVENANCE_UNKNOWN_LEGACY, label_provenance_value

# entry-side attribution is ALREADY unambiguous via event_type alone (§0.2) -
# no new column/table added for this. String literals matching
# strategy_lab.prospective_events's own constants directly, deliberately
# never coupled to strategy_lab/phase10_experiments.py's frozen
# RegimeGatedRules.name values.
EVENT_TYPE_TO_VARIANT = {
    EVENT_CONTROL_ENTRY: "CONTROL",
    EVENT_EXPERIMENT_A_ENTRY: "EXPERIMENT_A",
    EVENT_EXPERIMENT_B_ENTRY: "EXPERIMENT_B",
}

# Phase 17 §3.3: exit-side counterpart, kept SEPARATE from
# EVENT_TYPE_TO_VARIANT (entry-only) - never merged into it, to avoid
# collapsing EVENT_EXPERIMENT_B_EXIT_TECHNICAL/
# EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS into one undifferentiated
# "EXPERIMENT_B" count, which would erase exactly the information this
# phase exists to surface.
EXIT_EVENT_TYPE_TO_VARIANT = {
    EVENT_CONTROL_EXIT: "CONTROL",
    EVENT_EXPERIMENT_A_EXIT: "EXPERIMENT_A",
    EVENT_EXPERIMENT_B_EXIT_TECHNICAL: "EXPERIMENT_B",
    EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS: "EXPERIMENT_B",
}


def compute_event_type_breakdown(conn) -> dict:
    """{'entry_attributable': {...} (unchanged, 3 entry types),
    'exit_attributable': {...} (NEW, 4 exit types), 'shared_signal_detection':
    {...} (unchanged membership: the 4 non-variant-specific types),
    'total_events': n}."""
    events = load_events(conn)
    counts = events["event_type"].value_counts().to_dict() if not events.empty else {}
    entry_attributable: dict = {}
    exit_attributable: dict = {}
    shared_signal_detection: dict = {}
    for event_type in ALL_EVENT_TYPES:
        count = int(counts.get(event_type, 0))
        if event_type in EVENT_TYPE_TO_VARIANT:
            entry_attributable[event_type] = count
        elif event_type in EXIT_EVENT_TYPE_TO_VARIANT:
            exit_attributable[event_type] = count
        else:
            shared_signal_detection[event_type] = count

    return {
        "entry_attributable": entry_attributable,
        "exit_attributable": exit_attributable,
        "shared_signal_detection": shared_signal_detection,
        "total_events": int(len(events)),
    }


def compute_event_provenance_coverage(conn) -> dict:
    """{'events_with_fingerprint', 'events_unknown_legacy' (via
    label_provenance_value on config_fingerprint), 'by_variant':
    {variant_name: count}} - by_variant derived from event_type via
    EVENT_TYPE_TO_VARIANT directly (never from config_fingerprint) for
    entry-attributable rows only."""
    events = load_events(conn)
    if events.empty:
        return {
            "events_with_fingerprint": 0,
            "events_unknown_legacy": 0,
            "by_variant": {v: 0 for v in EVENT_TYPE_TO_VARIANT.values()},
        }

    fingerprint_labels = events["config_fingerprint"].apply(label_provenance_value)
    events_unknown_legacy = int((fingerprint_labels == PROVENANCE_UNKNOWN_LEGACY).sum())
    events_with_fingerprint = int(len(events) - events_unknown_legacy)

    by_variant = {v: 0 for v in EVENT_TYPE_TO_VARIANT.values()}
    for event_type, variant in EVENT_TYPE_TO_VARIANT.items():
        by_variant[variant] += int((events["event_type"] == event_type).sum())

    return {
        "events_with_fingerprint": events_with_fingerprint,
        "events_unknown_legacy": events_unknown_legacy,
        "by_variant": by_variant,
    }


def compute_exit_event_type_provenance(conn) -> dict:
    """{'by_event_type': {event_type: count for each of the 4 exit types,
    always present, 0-filled}, 'events_with_fingerprint',
    'events_unknown_legacy', 'total_exit_events'}. Counted PER EVENT TYPE
    (not per variant) deliberately - EVENT_EXPERIMENT_B_EXIT_TECHNICAL and
    EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS both map to variant 'EXPERIMENT_B'
    but must stay independently visible."""
    events = load_events(conn)
    by_event_type = {event_type: 0 for event_type in EXIT_EVENT_TYPE_TO_VARIANT}

    if events.empty:
        return {
            "by_event_type": by_event_type,
            "events_with_fingerprint": 0,
            "events_unknown_legacy": 0,
            "total_exit_events": 0,
        }

    exit_events = events[events["event_type"].isin(EXIT_EVENT_TYPE_TO_VARIANT.keys())]
    for event_type in EXIT_EVENT_TYPE_TO_VARIANT:
        by_event_type[event_type] = int((exit_events["event_type"] == event_type).sum())

    if exit_events.empty:
        events_with_fingerprint = 0
        events_unknown_legacy = 0
    else:
        fingerprint_labels = exit_events["config_fingerprint"].apply(label_provenance_value)
        events_unknown_legacy = int((fingerprint_labels == PROVENANCE_UNKNOWN_LEGACY).sum())
        events_with_fingerprint = int(len(exit_events) - events_unknown_legacy)

    return {
        "by_event_type": by_event_type,
        "events_with_fingerprint": events_with_fingerprint,
        "events_unknown_legacy": events_unknown_legacy,
        "total_exit_events": int(len(exit_events)),
    }


def compute_exit_attribution_gap_note() -> dict:
    """{'exit_event_types_exist': bool, 'exit_event_types': [...], 'note': str}.
    Dynamically inspects strategy_lab.prospective_events.ALL_EVENT_TYPES for
    any type containing 'exit' - self-correcting if a future phase adds
    exit events; never hardcodes today's absence as a permanent fact.

    Function-local import (mirrors strategy_lab/outcome_maturation.py's own
    function-local import convention): re-reads the CURRENT value of
    ALL_EVENT_TYPES off strategy_lab.prospective_events at call time, rather
    than a module-load-time binding, so the check is genuinely dynamic (a
    monkeypatch of strategy_lab.prospective_events.ALL_EVENT_TYPES is
    reflected here, not silently ignored)."""
    from strategy_lab.prospective_events import ALL_EVENT_TYPES as current_all_event_types
    exit_event_types = [t for t in current_all_event_types if "exit" in t]
    exit_event_types_exist = len(exit_event_types) > 0
    if exit_event_types_exist:
        note = (
            f"Exit-side event type(s) found: {exit_event_types}. Experiment B's "
            "exit_on_regime_loss differentiator is now observable in event data."
        )
    else:
        note = (
            "No exit-side event type exists in ALL_EVENT_TYPES today. Experiment "
            "B's sole differentiator from Experiment A (exit_on_regime_loss=True, "
            "strategy_lab/phase10_experiments.py) is structurally invisible in "
            "research_prospective_events - flagged as Open Question #2 "
            "(docs/specs/phase16.md §9), not built."
        )
    return {
        "exit_event_types_exist": exit_event_types_exist,
        "exit_event_types": exit_event_types,
        "note": note,
    }


ENTRY_AB_COOCCURRENCE_NOTE = (
    "EVENT_EXPERIMENT_A_ENTRY and EVENT_EXPERIMENT_B_ENTRY always co-occur on "
    "the same (ticker, observation_date) by design - both variants share the "
    "exact same entry rule (entry_qualifies and is_bullish) and differ only on "
    "exit (strategy_lab/phase10_experiments.py). This is not an anomaly."
)

# Phase 17 §3.3: exit-side counterpart to ENTRY_AB_COOCCURRENCE_NOTE.
EXPERIMENT_B_EXIT_COOCCURRENCE_NOTE = (
    "When both EVENT_EXPERIMENT_B_EXIT_TECHNICAL and "
    "EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS newly transition true on the same "
    "(ticker, observation_date), BOTH events are recorded independently - "
    "never merged or prioritized. This mirrors the existing "
    "EVENT_EXPERIMENT_A_ENTRY/EVENT_EXPERIMENT_B_ENTRY co-occurrence "
    "convention (docs/specs/phase16.md §0.2) of allowing multiple "
    "independently-true event types to co-occur on the same day."
)


def event_provenance_audit_summary(conn) -> dict:
    """Rollup: {'event_type_breakdown', 'event_provenance_coverage',
    'exit_attribution_gap', 'entry_ab_cooccurrence_note': a fixed string
    documenting that EVENT_EXPERIMENT_A_ENTRY/EVENT_EXPERIMENT_B_ENTRY
    always co-occur by construction (§0.2), not an anomaly,
    'exit_event_provenance', 'experiment_b_exit_cooccurrence_note'}."""
    return {
        "event_type_breakdown": compute_event_type_breakdown(conn),
        "event_provenance_coverage": compute_event_provenance_coverage(conn),
        "exit_attribution_gap": compute_exit_attribution_gap_note(),
        "entry_ab_cooccurrence_note": ENTRY_AB_COOCCURRENCE_NOTE,
        "exit_event_provenance": compute_exit_event_type_provenance(conn),
        "experiment_b_exit_cooccurrence_note": EXPERIMENT_B_EXIT_COOCCURRENCE_NOTE,
    }
