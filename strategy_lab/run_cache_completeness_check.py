"""CLI entry point: `python -m strategy_lab.run_cache_completeness_check [--tickers ...]`.

Mirrors ops/run_data_quality_check.py's convention. Lets an operator/tester
run just the narrow Phase 14 Component A completeness-refresh pass
(strategy_lab.cache_integrity.refresh_incomplete_latest_bars) against the
research universe without running the multi-minute full study. Never
regenerates a frozen Phase 9-13 research artifact - a detected correction
that may affect one is only ever logged (research_cache_corrections);
regenerating remains a deliberate, separate `python -m strategy_lab.run_study`
(etc.) invocation.
"""
import argparse

from db.database import db_session
from strategy_lab.cache_integrity import refresh_incomplete_latest_bars
from strategy_lab.universe import RESEARCH_UNIVERSE


def main():
    parser = argparse.ArgumentParser(
        description="Check (and, where needed, refresh) incomplete latest research-cache bars.",
    )
    parser.add_argument(
        "--tickers", nargs="*", default=None,
        help="Tickers to check (defaults to the full research universe)",
    )
    args = parser.parse_args()

    tickers = args.tickers or RESEARCH_UNIVERSE

    with db_session() as conn:
        report = refresh_incomplete_latest_bars(conn, tickers)

    print(f"\n{'=' * 90}")
    print("Research cache completeness check")
    print(f"{'ticker':<8}{'status':<22}{'detail'}")
    n_flagged = 0
    n_affects_frozen = 0
    for ticker, r in report.items():
        status = r.get("status", "")
        if status != "no_action_needed":
            n_flagged += 1
        detail = ""
        if status == "refreshed_changed":
            detail = f"close {r.get('old_close')} -> {r.get('new_close')}"
            affected = r.get("affects_frozen_artifacts") or []
            if affected:
                n_affects_frozen += 1
                detail += f"  *** AFFECTS FROZEN ARTIFACTS: {affected} ***"
        elif status == "fetch_failed":
            detail = r.get("detail", "")
        print(f"{ticker:<8}{status:<22}{detail}")
    print("=" * 90)
    print(f"{n_flagged} ticker(s) had an incomplete latest bar; {n_affects_frozen} correction(s) may affect a frozen research artifact.")
    if n_affects_frozen:
        print(
            "STOP: at least one correction may affect a frozen Phase 9-13 research artifact. "
            "Do not regenerate anything automatically - review research_cache_corrections and decide "
            "whether a manual `python -m strategy_lab.run_study` (etc.) re-run is warranted."
        )
    print("=" * 90)


if __name__ == "__main__":
    main()
