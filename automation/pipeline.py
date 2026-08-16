"""Phase 6 daily automation pipeline: refresh data -> compute signals ->
evaluate alerts -> send (if enabled) -> record run history.

Reuses the existing ingestion, indicator/signal, and alert layers directly
- this module contains no market-data-fetching, indicator, or alert-rule
logic of its own, only orchestration. Each ticker is ingested and
evaluated independently so one broken ticker can never abort the rest of
the run.
"""
import logging
import traceback
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

from alerts.config import DEFAULT_ALERT_CONFIG, AlertConfig
from alerts.price_config import DEFAULT_PRICE_ALERT_CONFIG, PriceAlertConfig
from alerts.price_runner import PriceAlertRunResult, run_price_alert_cycle
from alerts.runner import AlertRunResult, run_alert_cycle
from alerts.volatility_config import DEFAULT_VOLATILITY_ALERT_CONFIG, VolatilityAlertRunConfig
from alerts.volatility_runner import VolatilityAlertRunResult, run_volatility_alert_cycle
from automation.config import DEFAULT_PIPELINE_CONFIG, PipelineConfig
from automation.trading_calendar import is_likely_trading_day
from config.settings import WATCHLIST
from db.alert_repository import get_alert_state
from db.price_alert_config_repository import list_price_alert_configs
from db.volatility_alert_config_repository import list_volatility_alert_configs
from db.run_history_repository import (
    SEND_MODE_DRY_RUN,
    SEND_MODE_REAL,
    STATUS_FAILED,
    STATUS_PARTIAL_FAILURE,
    STATUS_SKIPPED_NON_TRADING_DAY,
    STATUS_SUCCESS,
    finish_run,
    record_skipped_run,
    start_run,
)
from ingestion import alpaca_source, yfinance_source

logger = logging.getLogger(__name__)


@dataclass
class TickerOutcome:
    ticker: str
    ingest_ok: bool
    ingest_rows: int = 0
    ingest_error: Optional[str] = None
    ingest_attempts: int = 1
    alert_result: Optional[AlertRunResult] = None
    evaluation_error: Optional[str] = None
    last_known_score: Optional[float] = None
    last_known_stage: Optional[str] = None
    last_known_checked_at: Optional[str] = None
    price_alert_result: Optional[PriceAlertRunResult] = None
    price_evaluation_error: Optional[str] = None
    volatility_alert_result: Optional[VolatilityAlertRunResult] = None
    volatility_evaluation_error: Optional[str] = None

    @property
    def failed(self) -> bool:
        return (
            not self.ingest_ok
            or self.evaluation_error is not None
            or self.price_evaluation_error is not None
            or self.volatility_evaluation_error is not None
        )

    @property
    def fired_alert(self) -> bool:
        return bool(self.alert_result and self.alert_result.fired)

    @property
    def fired_price_alert(self) -> bool:
        return bool(self.price_alert_result and self.price_alert_result.fired)

    @property
    def fired_volatility_alert(self) -> bool:
        return bool(self.volatility_alert_result and self.volatility_alert_result.fired)


@dataclass
class PipelineResult:
    run_id: Optional[int]
    status: str
    send_mode: str
    outcomes: List[TickerOutcome] = field(default_factory=list)
    skipped_reason: Optional[str] = None

    @property
    def tickers_attempted(self) -> int:
        return len(self.outcomes)

    @property
    def tickers_updated(self) -> int:
        return sum(1 for o in self.outcomes if o.ingest_ok)

    @property
    def tickers_failed(self) -> int:
        return sum(1 for o in self.outcomes if o.failed)

    @property
    def alerts_generated(self) -> int:
        return sum(1 for o in self.outcomes if o.fired_alert)

    @property
    def price_alerts_generated(self) -> int:
        return sum(1 for o in self.outcomes if o.fired_price_alert)

    @property
    def volatility_alerts_generated(self) -> int:
        return sum(1 for o in self.outcomes if o.fired_volatility_alert)


def _ingest_ticker(conn, ticker: str, source: str, lookback_days: int, pipeline_config: PipelineConfig) -> TickerOutcome:
    try:
        if source == "yfinance":
            rows = yfinance_source.ingest_ticker(conn, ticker)
        else:
            rows = alpaca_source.ingest_ticker(
                conn, ticker, days=lookback_days,
                max_retries=pipeline_config.max_retries,
                retry_initial_delay_seconds=pipeline_config.retry_initial_delay_seconds,
                retry_backoff_multiplier=pipeline_config.retry_backoff_multiplier,
            )
        return TickerOutcome(ticker=ticker, ingest_ok=True, ingest_rows=rows)
    except Exception as e:
        logger.error("ingestion failed for %s: %s", ticker, e)
        outcome = TickerOutcome(ticker=ticker, ingest_ok=False, ingest_error=f"{type(e).__name__}: {e}")
        # Fail-closed: a ticker whose fresh ingestion failed is never
        # evaluated (see _evaluate_ticker_alerts) - surface whatever was
        # last durably known about it instead, clearly labeled as stored
        # (not fresh) so callers never mistake it for today's result.
        state = get_alert_state(conn, ticker)
        if state:
            outcome.last_known_score = state["last_score"]
            outcome.last_known_stage = state["last_stage"]
            outcome.last_known_checked_at = state["last_checked_at"]
        return outcome


def _evaluate_ticker_alerts(conn, outcome: TickerOutcome, alert_config: AlertConfig, send: bool) -> TickerOutcome:
    # Fail-closed: only ever evaluate (and therefore only ever mutate
    # alert_state/cooldown/alerts) a ticker whose fresh ingestion for THIS
    # run actually succeeded. A ticker whose ingestion failed keeps
    # whatever alert_state it already had - untouched, byte-identical -
    # rather than being silently re-evaluated against stale/out-of-band
    # data sitting in `prices` from some earlier, unrelated fetch.
    if not outcome.ingest_ok:
        return outcome
    try:
        results = run_alert_cycle(conn, tickers=[outcome.ticker], config=alert_config, send=send)
        outcome.alert_result = results[0]
    except Exception as e:
        logger.error("alert evaluation failed for %s: %s\n%s", outcome.ticker, e, traceback.format_exc())
        outcome.evaluation_error = f"{type(e).__name__}: {e}"
    return outcome


def _evaluate_ticker_price_alert(
    conn, outcome: TickerOutcome, price_alert_config: PriceAlertConfig, send: bool, thresholds_by_ticker: dict
) -> TickerOutcome:
    # Same fail-closed rule as _evaluate_ticker_alerts: never evaluate (and
    # therefore never mutate price_alert_state/cooldown/price_alerts) a
    # ticker whose fresh ingestion for THIS run failed. A ticker with no
    # configured PriceThreshold is simply skipped - nothing to check.
    threshold = thresholds_by_ticker.get(outcome.ticker)
    if not outcome.ingest_ok or threshold is None:
        return outcome
    try:
        results = run_price_alert_cycle(conn, thresholds=[threshold], config=price_alert_config, send=send)
        outcome.price_alert_result = results[0]
    except Exception as e:
        logger.error("price alert evaluation failed for %s: %s\n%s", outcome.ticker, e, traceback.format_exc())
        outcome.price_evaluation_error = f"{type(e).__name__}: {e}"
    return outcome


def _evaluate_ticker_volatility_alert(
    conn, outcome: TickerOutcome, volatility_alert_config: VolatilityAlertRunConfig, send: bool, volatility_configs_by_ticker: dict
) -> TickerOutcome:
    # Same fail-closed rule as _evaluate_ticker_price_alert: never evaluate
    # (and therefore never mutate volatility_alert_state/volatility_alerts)
    # a ticker whose fresh ingestion for THIS run failed. A ticker with no
    # configured VolatilityAlertConfig is simply skipped.
    volatility_config = volatility_configs_by_ticker.get(outcome.ticker)
    if not outcome.ingest_ok or volatility_config is None:
        return outcome
    try:
        results = run_volatility_alert_cycle(conn, configs=[volatility_config], config=volatility_alert_config, send=send)
        outcome.volatility_alert_result = results[0]
    except Exception as e:
        logger.error("volatility alert evaluation failed for %s: %s\n%s", outcome.ticker, e, traceback.format_exc())
        outcome.volatility_evaluation_error = f"{type(e).__name__}: {e}"
    return outcome


def run_pipeline(
    conn,
    tickers: Optional[List[str]] = None,
    send: bool = False,
    price_source: Optional[str] = None,
    pipeline_config: PipelineConfig = DEFAULT_PIPELINE_CONFIG,
    alert_config: AlertConfig = DEFAULT_ALERT_CONFIG,
    price_alert_config: PriceAlertConfig = DEFAULT_PRICE_ALERT_CONFIG,
    volatility_alert_config: VolatilityAlertRunConfig = DEFAULT_VOLATILITY_ALERT_CONFIG,
    today: Optional[date] = None,
    skip_non_trading_day_check: bool = False,
) -> PipelineResult:
    """Run one full pipeline cycle: ingest -> evaluate -> (maybe) alert -> record.

    Does NOT acquire the overlap-protection lock itself - see
    automation/run_daily.py, which wraps this call with acquire_run_lock().
    Keeping locking at the CLI layer makes this function trivially testable
    without touching the filesystem.
    """
    thresholds_by_ticker = {t.ticker: t for t in list_price_alert_configs(conn)}
    volatility_configs_by_ticker = {c.ticker: c for c in list_volatility_alert_configs(conn)}
    if tickers is None:
        # Default run: WATCHLIST plus any ticker with a configured price
        # alert threshold OR a configured volatility alert threshold, so
        # adding either from the dashboard keeps its price data fresh
        # automatically without needing to also join WATCHLIST. An explicit
        # `tickers=` argument (CLI --tickers, tests) is honored exactly as
        # passed, no union applied.
        configured_extra_tickers = set(thresholds_by_ticker) | set(volatility_configs_by_ticker)
        tickers = WATCHLIST + sorted(t for t in configured_extra_tickers if t not in WATCHLIST)
    source = price_source or pipeline_config.price_source
    send_mode = SEND_MODE_REAL if send else SEND_MODE_DRY_RUN
    check_date = today or date.today()

    if not skip_non_trading_day_check and not is_likely_trading_day(check_date):
        reason = f"{check_date.isoformat()} is not an NYSE trading day (weekend or market holiday)"
        run_id = record_skipped_run(conn, send_mode, STATUS_SKIPPED_NON_TRADING_DAY, reason, trading_date=check_date)
        return PipelineResult(run_id=run_id, status=STATUS_SKIPPED_NON_TRADING_DAY, send_mode=send_mode, skipped_reason=reason)

    run_id = start_run(conn, send_mode, trading_date=check_date)

    try:
        outcomes = []
        for ticker in tickers:
            outcome = _ingest_ticker(conn, ticker, source, pipeline_config.alpaca_lookback_days, pipeline_config)
            outcome = _evaluate_ticker_alerts(conn, outcome, alert_config, send)
            outcome = _evaluate_ticker_price_alert(conn, outcome, price_alert_config, send, thresholds_by_ticker)
            outcome = _evaluate_ticker_volatility_alert(conn, outcome, volatility_alert_config, send, volatility_configs_by_ticker)
            outcomes.append(outcome)
    except Exception as e:
        # Truly unexpected top-level failure (e.g. DB connection lost
        # mid-run) - still record the run rather than leaving it stuck at
        # status='running' forever.
        logger.error("pipeline crashed: %s\n%s", e, traceback.format_exc())
        error_summary = f"pipeline crashed: {type(e).__name__}: {e}"
        finish_run(conn, run_id, status=STATUS_FAILED, error_summary=error_summary[:2000])
        return PipelineResult(run_id=run_id, status=STATUS_FAILED, send_mode=send_mode, skipped_reason=error_summary)

    result = PipelineResult(run_id=run_id, status=STATUS_SUCCESS, send_mode=send_mode, outcomes=outcomes)

    n_failed = result.tickers_failed
    n_total = result.tickers_attempted
    if n_failed == 0:
        status = STATUS_SUCCESS
    elif n_failed < n_total:
        status = STATUS_PARTIAL_FAILURE
    else:
        status = STATUS_FAILED
    result.status = status

    error_summary = None
    failures = [o for o in outcomes if o.failed]
    if failures:
        parts = [
            f"{o.ticker}: {o.ingest_error or o.evaluation_error or o.price_evaluation_error or o.volatility_evaluation_error}"
            for o in failures
        ]
        error_summary = "; ".join(parts)[:2000]

    finish_run(
        conn, run_id, status=status,
        tickers_attempted=result.tickers_attempted,
        tickers_updated=result.tickers_updated,
        tickers_failed=result.tickers_failed,
        alerts_generated=result.alerts_generated,
        error_summary=error_summary,
    )
    return result
