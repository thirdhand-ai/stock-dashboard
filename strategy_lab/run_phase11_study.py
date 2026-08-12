"""CLI entry point for Phase 11: `python -m strategy_lab.run_phase11_study`.

Runs the realistic capital-constrained portfolio simulation
(strategy_lab/phase11_report.py) and writes results to
data/research_cache/phase11_results.pkl for the Strategy Lab dashboard.
Research-only: never touches Alpaca order submission, Discord, or any
production trading/alert state - see tests/test_strategy_lab.py's
structural safety tests, which scan this file too.
"""
import logging

from db.database import db_session
from strategy_lab.phase11_report import run_full_phase11_study, save_cache

logging.basicConfig(level=logging.INFO)


def main():
    with db_session() as conn:
        results = run_full_phase11_study(conn)
    path = save_cache(results)
    print(f"Phase 11 study complete in {results['elapsed_seconds']}s. Cache: {path}")


if __name__ == "__main__":
    main()
