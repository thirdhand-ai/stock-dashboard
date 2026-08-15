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
    EVENT_EXPERIMENT_A_ENTRY,
    EVENT_EXPERIMENT_B_ENTRY,
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


def compute_event_type_breakdown(conn) -> dict:
    """{'entry_attributable': {event_type: count}, 'shared_signal_detection':
    {event_type: count}, 'total_events': n}. 'shared_signal_detection' =
    the 4 event types NOT in EVENT_TYPE_TO_VARIANT
    (score_crossing_70/trend_advance/momentum_advance/volume_advance) -
    reported as 'not variant-specific by design', never as a gap."""
    events = load_events(conn)
    entry_attributable: dict = {}
    shared_signal_detection: dict = {}
    if not events.empty:
        counts = events["event_type"].value_counts().to_dict()
        for event_type in ALL_EVENT_TYPES:
            count = int(counts.get(event_type, 0))
            if event_type in EVENT_TYPE_TO_VARIANT:
                entry_attributable[event_type] = count
            else:
                shared_signal_detection[event_type] = count
    else:
        for event_type in ALL_EVENT_TYPES:
            if event_type in EVENT_TYPE_TO_VARIANT:
                entry_attributable[event_type] = 0
            else:
                shared_signal_detection[event_type] = 0

    return {
        "entry_attributable": entry_attributable,
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


def event_provenance_audit_summary(conn) -> dict:
    """Rollup: {'event_type_breakdown', 'event_provenance_coverage',
    'exit_attribution_gap', 'entry_ab_cooccurrence_note': a fixed string
    documenting that EVENT_EXPERIMENT_A_ENTRY/EVENT_EXPERIMENT_B_ENTRY
    always co-occur by construction (§0.2), not an anomaly}."""
    return {
        "event_type_breakdown": compute_event_type_breakdown(conn),
        "event_provenance_coverage": compute_event_provenance_coverage(conn),
        "exit_attribution_gap": compute_exit_attribution_gap_note(),
        "entry_ab_cooccurrence_note": ENTRY_AB_COOCCURRENCE_NOTE,
    }
