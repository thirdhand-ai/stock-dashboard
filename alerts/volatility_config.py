"""Value objects for the volatility-alert engine (alerts/volatility_engine.py).
Structurally separate from alerts/price_config.py's PriceThreshold - see
db/volatility_alerts_schema.py's docstring for why a day-over-day % move
threshold is its own concept rather than a third PriceThreshold mode: it
never resolves to a static above/below dollar level the way a
baseline-percent threshold does, since the "previous" side of the comparison
is always yesterday's close, recomputed fresh on every evaluation.

Per-ticker VolatilityAlertConfig VALUES live in the volatility_alert_config
DB table (db/volatility_alerts_schema.py, db/volatility_alert_config_repository.py),
not here - editable from the dashboard (dashboard/views/volatility_alert_config.py)
so a threshold change takes effect on the next automation run without a code
deploy. This module only defines the shape (VolatilityAlertConfig) and the
cooldown config (VolatilityAlertRunConfig), which stays a plain code constant
since it's one global value, not per-ticker - same split alerts/price_config.py
uses for PriceThreshold vs. PriceAlertConfig.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class VolatilityAlertConfig:
    ticker: str
    # Alert when |day-over-day % move| >= this value (e.g. 5.0 = +/-5%).
    threshold_percent: float


@dataclass(frozen=True)
class VolatilityAlertRunConfig:
    # Secondary protection only (see alerts/volatility_runner.py) - the
    # primary de-dupe is per-trading-day (a given day's move fires at most
    # once, however many times the evaluation is re-run). 0 disables the
    # cooldown.
    cooldown_minutes: int = 60


DEFAULT_VOLATILITY_ALERT_CONFIG = VolatilityAlertRunConfig()
