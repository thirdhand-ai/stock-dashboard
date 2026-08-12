"""Explicit, configurable automation schedule and pipeline settings.

ScheduleConfig documents *when* the production automation is meant to run.
It is not itself a scheduler - nothing in this codebase installs a cron
entry, systemd timer, or cloud scheduled job on its own. Activating a real
recurring schedule is a deliberate deployment step the operator performs
explicitly (see deploy/), never something triggered by running this code.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduleConfig:
    # Once per trading day, after the daily bar has finalized. The US
    # market closes at 16:00 ET; 16:30 gives data providers a buffer to
    # settle the day's final bar before ingestion runs.
    run_time_local: str = "16:30"
    timezone: str = "America/New_York"
    # Documents intent; the actual check lives in automation/trading_calendar.py
    # (a real NYSE calendar, not just a weekday check) and is applied in
    # automation/pipeline.py unless --force-run is passed.
    skip_non_trading_days: bool = True


@dataclass(frozen=True)
class PipelineConfig:
    # "alpaca" is the default and only source used for scheduled runs -
    # it has been reliable throughout this project, unlike yfinance (see
    # ingestion/yfinance_source.py's history of Yahoo rate-limiting). A
    # manual run can still pass --source yfinance if Yahoo's limit clears.
    price_source: str = "alpaca"
    alpaca_lookback_days: int = 5  # daily incremental refresh, not a full backfill
    lock_path: str = "data/.automation.lock"
    # Bounded retry for transient network/DNS failures reaching Alpaca (see
    # ingestion/alpaca_source.py's retry_request()). Small and contained on
    # purpose - not a general retry framework. Non-retryable errors (auth,
    # bad request, no data) are never retried regardless of this count.
    max_retries: int = 2
    retry_initial_delay_seconds: float = 1.0
    retry_backoff_multiplier: float = 2.0


DEFAULT_SCHEDULE = ScheduleConfig()
DEFAULT_PIPELINE_CONFIG = PipelineConfig()
