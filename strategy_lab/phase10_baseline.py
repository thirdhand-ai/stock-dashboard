"""Phase 10 spec item B23: compact, versioned metadata/summary artifact
alongside the full pickle cache (strategy_lab/report_phase10.py), so the
analysis version/scope/assumptions used to produce a given result are
recorded and diffable, without duplicating the full ~100k-row research
tables. No credentials.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from config.settings import BASE_DIR
from strategy_lab.execution import ASSUMPTION_SETS
from strategy_lab.phase10_experiments import ALL_VARIANTS

ANALYSIS_VERSION = "phase10-v1"
BASELINE_DIR = BASE_DIR / "data" / "research_cache"
BASELINE_FILE = BASELINE_DIR / "phase10_baseline.json"


def build_metadata_summary(results: dict) -> dict:
    variant_summary = {}
    for variant in ALL_VARIANTS:
        vr = results.get("variant_results", {}).get(variant.name, {})
        variant_summary[variant.name] = {
            friction: {
                "cumulative_return_pct": data.get("cumulative_return_pct"),
                "robustness": data.get("robustness"),
                "holding_period": data.get("holding_period"),
            }
            for friction, data in vr.items()
        }

    return {
        "analysis_version": ANALYSIS_VERSION,
        "phase": 10,
        "generated_at": results.get("generated_at"),
        "dataset": {"coverage": results.get("coverage"), "universe_size": len(results.get("tickers", []))},
        "experiment_definitions": {v.name: v.description for v in ALL_VARIANTS},
        "friction_assumptions": {k: vars(v) for k, v in ASSUMPTION_SETS.items()},
        "regime_distribution": results.get("regime_distribution").to_dict("records") if results.get("regime_distribution") is not None else None,
        "bullish_sufficiently_selective": results.get("bullish_sufficiently_selective"),
        "variant_results_summary": variant_summary,
        "bootstrap_results": results.get("bootstrap_results"),
        "production_config_unchanged": (
            results.get("production_diff_before") == {} and results.get("production_diff_after") == {}
        ),
        "frozen_at": datetime.now(timezone.utc).isoformat(),
    }


def save_baseline(results: dict) -> Path:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    summary = build_metadata_summary(results)
    with open(BASELINE_FILE, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    return BASELINE_FILE


def load_baseline() -> dict:
    if not BASELINE_FILE.exists():
        return {}
    with open(BASELINE_FILE) as f:
        return json.load(f)
