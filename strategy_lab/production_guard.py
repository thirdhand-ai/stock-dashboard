"""Part A5: fingerprints the production configuration Phase 10 research must
never alter, so any accidental change is provably detectable rather than
just "we didn't mean to."

Covers both structured values (read from the live config objects, so it
reflects what the code actually does, not just file bytes) and raw file
hashes (to also catch changes that don't alter the specific fields read
here, e.g. a new field added to a dataclass). Read-only: this module never
imports or calls anything that could itself change these files.
"""
import dataclasses
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

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


def _hash_file(rel_path: str):
    path = BASE_DIR / rel_path
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
