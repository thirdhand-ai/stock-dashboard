"""Phase 15 Component C/D: tiny shared provenance-label helper.

Kept as its own file (rather than duplicated in ops/prospective_audit.py and
dashboard/views/strategy_lab.py) so the "legacy/unknown" label is defined
exactly once. Pure, read-only - no schema, no table, no DB access at all.
"""
from typing import Optional

PROVENANCE_UNKNOWN_LEGACY = "UNKNOWN_LEGACY"


def label_provenance_value(value: Optional[str]) -> str:
    """value if non-NULL else PROVENANCE_UNKNOWN_LEGACY. Never guesses."""
    return value if value else PROVENANCE_UNKNOWN_LEGACY
