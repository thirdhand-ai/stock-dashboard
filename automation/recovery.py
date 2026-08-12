"""Safe, idempotent recovery for a missed/failed scheduled run (Phase 6
hardening, added after the 2026-08-12 DNS outage - see automation/pipeline.py).

Two distinct operations, both read-only with respect to Discord/orders:

  - run_recovery(): idempotent catch-up. No-ops if today already has a
    fully successful fresh-data run; otherwise makes exactly ONE additional
    attempt by calling the same run_pipeline() the scheduled job uses -
    reusing its existing fail-closed/partial-failure semantics rather than
    reimplementing them. Bounded (one attempt), never aggressive polling.

  - preview_recovery(): observational-only. Refreshes real price data (safe
    and idempotent - store_bars upserts on ticker/date/source) then
    evaluates alerts with persist=False, so nothing is written to
    alert_state/alerts/cooldown. A subsequent, explicitly-approved real
    recovery run can still legitimately detect and act on any crossing this
    preview reports - the preview can never consume or mask it. Never sends
    Discord, never places an order.

Neither function activates a new scheduler; see deploy/ for that decision,
which stays manual and explicit.
"""
import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

from alerts.config import DEFAULT_ALERT_CONFIG, AlertConfig
from alerts.runner import run_alert_cycle
from automation.config import DEFAULT_PIPELINE_CONFIG, PipelineConfig
from automation.lock import LockHeldError, acquire_run_lock
from automation.pipeline import PipelineResult, run_pipeline
from config.settings import DISCORD_WEBHOOK_URL, WATCHLIST
from db.database import db_session
from db.run_history_repository import STATUS_SUCCESS, load_run_history
from ingestion import alpaca_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("automation.recovery")


@dataclass
class RecoveryResult:
    ran: bool
    reason: str
    pipeline_result: Optional[PipelineResult] = None


def _todays_successful_run_exists(conn, today: date) -> bool:
    """Idempotency check for run_recovery(): has a fresh-data run already
    fully succeeded today? Only STATUS_SUCCESS counts - a partial failure or
    total failure still leaves genuine work for recovery to do."""
    history = load_run_history(conn, limit=50)
    if history.empty:
        return False
    todays = history[history["started_at"].astype(str).str.startswith(today.isoformat())]
    return bool((todays["status"] == STATUS_SUCCESS).any())


def run_recovery(
    conn,
    tickers: Optional[List[str]] = None,
    send: bool = False,
    price_source: Optional[str] = None,
    today: Optional[date] = None,
    pipeline_config: PipelineConfig = DEFAULT_PIPELINE_CONFIG,
    alert_config: AlertConfig = DEFAULT_ALERT_CONFIG,
) -> RecoveryResult:
    today = today or date.today()
    if _todays_successful_run_exists(conn, today):
        return RecoveryResult(ran=False, reason="today's successful fresh-data run already exists; recovery is a no-op")

    result = run_pipeline(
        conn, tickers=tickers, send=send, price_source=price_source,
        pipeline_config=pipeline_config, alert_config=alert_config, today=today,
    )
    return RecoveryResult(ran=True, reason="recovery attempt executed", pipeline_result=result)


@dataclass
class ConnectivityCheck:
    ok: bool
    detail: str


def check_alpaca_connectivity() -> ConnectivityCheck:
    """Minimal read-only Alpaca reachability check (one API call, no
    credentials exposed) - reuses the existing paper-account read rather
    than adding a separate preflight framework."""
    try:
        alpaca_source.check_paper_account()
        return ConnectivityCheck(ok=True, detail="Alpaca paper account reachable")
    except Exception as e:
        return ConnectivityCheck(ok=False, detail=f"{type(e).__name__}: {e}")


@dataclass
class RecoveryPreviewTickerResult:
    ticker: str
    ingest_ok: bool
    ingest_error: Optional[str] = None
    ok: bool = False
    reason_unavailable: Optional[str] = None
    would_fire: bool = False
    reasons: List[str] = field(default_factory=list)
    current_score: Optional[float] = None
    previous_score: Optional[float] = None
    current_stage: Optional[str] = None
    previous_stage: Optional[str] = None


@dataclass
class RecoveryPreviewResult:
    connectivity: ConnectivityCheck
    ticker_results: List[RecoveryPreviewTickerResult] = field(default_factory=list)

    @property
    def any_would_fire(self) -> bool:
        return any(r.would_fire for r in self.ticker_results)


def preview_recovery(
    conn,
    tickers: Optional[List[str]] = None,
    pipeline_config: PipelineConfig = DEFAULT_PIPELINE_CONFIG,
    alert_config: AlertConfig = DEFAULT_ALERT_CONFIG,
) -> RecoveryPreviewResult:
    tickers = tickers or WATCHLIST
    connectivity = check_alpaca_connectivity()
    if not connectivity.ok:
        logger.warning("recovery preview: Alpaca connectivity unavailable (%s) - stopping, not polling", connectivity.detail)
        return RecoveryPreviewResult(connectivity=connectivity, ticker_results=[])

    ticker_results = []
    for ticker in tickers:
        try:
            alpaca_source.ingest_ticker(
                conn, ticker, days=pipeline_config.alpaca_lookback_days,
                max_retries=pipeline_config.max_retries,
                retry_initial_delay_seconds=pipeline_config.retry_initial_delay_seconds,
                retry_backoff_multiplier=pipeline_config.retry_backoff_multiplier,
            )
            ingest_ok, ingest_error = True, None
        except Exception as e:
            logger.error("recovery preview: ingestion failed for %s: %s", ticker, e)
            ingest_ok, ingest_error = False, f"{type(e).__name__}: {e}"

        tr = RecoveryPreviewTickerResult(ticker=ticker, ingest_ok=ingest_ok, ingest_error=ingest_error)
        if ingest_ok:
            # persist=False: pure observation, see run_alert_cycle's docstring.
            results = run_alert_cycle(conn, tickers=[ticker], config=alert_config, send=False, persist=False)
            ev = results[0].evaluation
            tr.ok = ev.ok
            tr.reason_unavailable = ev.reason_unavailable
            tr.would_fire = results[0].fired
            tr.reasons = ev.reasons
            tr.current_score = ev.current_score
            tr.previous_score = ev.previous_score
            tr.current_stage = ev.current_stage
            tr.previous_stage = ev.previous_stage
        ticker_results.append(tr)

    return RecoveryPreviewResult(connectivity=connectivity, ticker_results=ticker_results)


def _print_preview(preview: RecoveryPreviewResult):
    print(f"\n{'=' * 70}")
    print("RECOVERY PREVIEW (observational only - no Discord, no state mutation, no orders)")
    print(f"Alpaca connectivity: {'OK' if preview.connectivity.ok else 'UNAVAILABLE'} - {preview.connectivity.detail}")
    if not preview.connectivity.ok:
        print("Stopping - not polling. No fresh data refreshed, no evaluation performed.")
        print("=" * 70)
        return
    print(f"{'=' * 70}")
    for tr in preview.ticker_results:
        if not tr.ingest_ok:
            print(f"  {tr.ticker:6s} INGEST FAILED: {tr.ingest_error}")
            continue
        if not tr.ok:
            print(f"  {tr.ticker:6s} unavailable: {tr.reason_unavailable}")
            continue
        status = "WOULD FIRE" if tr.would_fire else "no eligible alert"
        print(
            f"  {tr.ticker:6s} score {tr.previous_score} -> {tr.current_score}  "
            f"stage {tr.previous_stage} -> {tr.current_stage}  | {status}"
            + (f" ({', '.join(tr.reasons)})" if tr.reasons else "")
        )
    print("=" * 70)
    print(f"Genuine alerts that WOULD be eligible: {sum(1 for r in preview.ticker_results if r.would_fire)}")
    print("No real Discord send performed. Approval required before any real recovery send.")


def main():
    parser = argparse.ArgumentParser(description="Recover a missed/failed scheduled automation run")
    parser.add_argument("--tickers", nargs="+", default=WATCHLIST)
    parser.add_argument(
        "--preview", action="store_true",
        help="Observational only: refresh data and show what WOULD be eligible, without touching alert_state or sending anything.",
    )
    parser.add_argument(
        "--send", action="store_true",
        help="Actually deliver eligible alerts to Discord during a real (non-preview) recovery run. Requires explicit approval.",
    )
    args = parser.parse_args()

    if args.send and not args.preview:
        if not DISCORD_WEBHOOK_URL:
            print("ERROR: --send was passed but DISCORD_WEBHOOK_URL is not configured.", file=sys.stderr)
            sys.exit(2)
        logger.info("REAL SEND MODE: a real recovery run will deliver eligible alerts to Discord.")

    try:
        with acquire_run_lock(DEFAULT_PIPELINE_CONFIG.lock_path):
            with db_session() as conn:
                if args.preview:
                    preview = preview_recovery(conn, tickers=args.tickers)
                    _print_preview(preview)
                    sys.exit(0)

                recovery = run_recovery(conn, tickers=args.tickers, send=args.send, today=date.today())
    except LockHeldError as e:
        logger.error(str(e))
        print(f"SKIPPED: {e}")
        sys.exit(3)

    print(f"\n{'=' * 70}")
    print(f"Recovery ran: {recovery.ran}  ({recovery.reason})")
    if recovery.pipeline_result:
        print(f"Pipeline status: {recovery.pipeline_result.status}  send_mode: {recovery.pipeline_result.send_mode}")
    print("=" * 70)


if __name__ == "__main__":
    main()
