"""CLI entry point for Phase 10: `python -m strategy_lab.run_phase10_study`.

Runs the full regime-gated strategy study (strategy_lab/report_phase10.py)
and writes results to data/research_cache/phase10_results.pkl (+ a compact
metadata summary in phase10_baseline.json) for the Strategy Lab dashboard.
Research-only: never touches Alpaca order submission, Discord, or any
production trading/alert state.
"""
import logging

from db.database import db_session
from strategy_lab.phase10_baseline import save_baseline as save_phase10_metadata
from strategy_lab.report_phase10 import run_full_phase10_study, save_cache

logging.basicConfig(level=logging.INFO)


def main():
    with db_session() as conn:
        results = run_full_phase10_study(conn)
    path = save_cache(results)
    meta_path = save_phase10_metadata(results)
    print(f"Phase 10 study complete in {results['total_elapsed_sec']:.1f}s. Cache: {path}, metadata: {meta_path}")


if __name__ == "__main__":
    main()
