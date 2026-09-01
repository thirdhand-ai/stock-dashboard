"""Part A5: fingerprints the production configuration Phase 10 research must
never alter, so any accidental change is provably detectable rather than
just "we didn't mean to."

Covers both structured values (read from the live config objects, so it
reflects what the code actually does, not just file bytes) and raw file
hashes (to also catch changes that don't alter the specific fields read
here, e.g. a new field added to a dataclass). Read-only: this module never
imports or calls anything that could itself change these files.

GUARDED_SECTIONS (Phase 12 config-drift fix, 2026-09-01): config/settings.py
is a general settings grab-bag - credentials, SMTP/email alert settings,
DB_PATH, dashboard-display ticker lists (EXPLORATORY_WATCHLIST,
SIGNAL_COVERAGE_TICKERS) - that also happens to define WATCHLIST, the one
part of it this guard actually needs frozen. Whole-file hashing it (as every
other GUARDED_FILES entry still is - each of those is dedicated trading
config top to bottom, so hashing them whole is correct) meant any unrelated
edit anywhere in the file - an appended research ticker, a new SMTP setting -
flipped the fingerprint and falsely flagged every ACTIVE experiment in
ops/experiment_registry.py as config-drifted, even though WATCHLIST and
every structured_values field were provably unchanged (confirmed via git
history + diff_against_saved() during the investigation this fixes). For
config/settings.py only, _hash_file now hashes just the named top-level
assignment's own source text via _extract_assignment_source, not the whole
file.
"""
import ast
import dataclasses
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.settings import BASE_DIR, WATCHLIST

FINGERPRINT_DIR = BASE_DIR / "data" / "research_cache"
FINGERPRINT_FILE = FINGERPRINT_DIR / "production_fingerprint.json"

GUARDED_FILES = [
    "signals/config.py",
    "backtest/config.py",
    "trading/config.py",
    "config/settings.py",
    "alerts/config.py",
    "deploy/com.stockdashboard.dailyrun.plist.example",
]

# Maps a GUARDED_FILES entry to the single top-level assignment name that
# actually needs freezing in it - see this module's docstring. A file with
# no entry here is still whole-file hashed, unchanged from before.
GUARDED_SECTIONS = {
    "config/settings.py": "WATCHLIST",
}


def _extract_assignment_source(source: str, name: str) -> Optional[str]:
    """Returns the exact source text of the top-level `name = ...`
    assignment statement in `source` (module level only, never inside a
    function/class), or None if no such assignment exists. Insensitive to
    everything else in the file - other assignments, comments, blank lines -
    only this statement's own tokens count, so it changes if and only if
    this specific assignment's value or formatting changes."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.get_source_segment(source, node)
    return None


def _hash_file(rel_path: str):
    path = BASE_DIR / rel_path
    if not path.exists():
        return None

    section_name = GUARDED_SECTIONS.get(rel_path)
    if section_name is None:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    segment = _extract_assignment_source(path.read_text(), section_name)
    if segment is None:
        # The guarded assignment itself is gone/renamed - that IS a real
        # config change (not a false positive), so this must still produce
        # a distinct, stable value rather than silently matching nothing.
        return f"MISSING:{section_name}"
    return hashlib.sha256(segment.encode("utf-8")).hexdigest()


def _structured_values() -> dict:
    from backtest.config import DEFAULT_EXECUTION, DEFAULT_RULES
    from signals.config import DEFAULT_THRESHOLDS, DEFAULT_WEIGHTS
    from trading.config import DEFAULT_RISK_CONFIG
    from alerts.config import DEFAULT_ALERT_CONFIG

    return {
        "signal_thresholds": dataclasses.asdict(DEFAULT_THRESHOLDS),
        "signal_weights": dataclasses.asdict(DEFAULT_WEIGHTS),
        "backtest_rules": dataclasses.asdict(DEFAULT_RULES),
        "backtest_execution": dataclasses.asdict(DEFAULT_EXECUTION),
        "risk_config": dataclasses.asdict(DEFAULT_RISK_CONFIG),
        "alert_rules": dataclasses.asdict(DEFAULT_ALERT_CONFIG),
        "watchlist": list(WATCHLIST),
    }


def compute_fingerprint() -> dict:
    return {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "file_hashes": {f: _hash_file(f) for f in GUARDED_FILES},
        "structured_values": _structured_values(),
    }


def save_fingerprint() -> Path:
    FINGERPRINT_DIR.mkdir(parents=True, exist_ok=True)
    fp = compute_fingerprint()
    with open(FINGERPRINT_FILE, "w") as f:
        json.dump(fp, f, indent=2, default=str)
    return FINGERPRINT_FILE


def load_fingerprint() -> dict:
    if not FINGERPRINT_FILE.exists():
        return {}
    with open(FINGERPRINT_FILE) as f:
        return json.load(f)


def diff_against_saved() -> dict:
    """Compares the CURRENT live config against the saved fingerprint.
    Returns {} if identical (ignoring the computed_at timestamp), otherwise
    a dict of {field: (old, new)} for every field that changed."""
    saved = load_fingerprint()
    if not saved:
        return {"error": "no saved fingerprint to compare against"}
    current = compute_fingerprint()

    diffs = {}
    for f, h in saved.get("file_hashes", {}).items():
        if current["file_hashes"].get(f) != h:
            diffs[f"file_hashes.{f}"] = (h, current["file_hashes"].get(f))
    for k, v in saved.get("structured_values", {}).items():
        if current["structured_values"].get(k) != v:
            diffs[f"structured_values.{k}"] = (v, current["structured_values"].get(k))
    return diffs
