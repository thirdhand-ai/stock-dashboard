"""CLI entry point for Phase 9: `python -m strategy_lab.run_study`.

Runs the full large-sample research study (strategy_lab/report.py) and
writes its results to data/research_cache/phase9_results.pkl for the
Strategy Lab dashboard page to read. Research-only: never touches Alpaca
order submission, Discord, or any production trading/alert state (see
tests/test_strategy_lab_safety.py).
"""
import logging

from db.database import db_session
from strategy_lab.report import run_full_study, save_cache

logging.basicConfig(level=logging.INFO)


def main():
    with db_session() as conn:
        results = run_full_study(conn)
    path = save_cache(results)
    print(f"Phase 9 study complete in {results['total_elapsed_sec']:.1f}s. Cache written to {path}")


if __name__ == "__main__":
    main()
