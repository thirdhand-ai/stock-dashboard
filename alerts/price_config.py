"""Threshold/config value objects for the price-alert engine (alerts/
price_engine.py). Structurally separate from alerts/config.py (score/stage
alerts) - a different signal (raw price) with a different shape of config
(per-ticker, not a single global number).

Per-ticker PriceThreshold VALUES themselves live in the price_alert_config
DB table (db/price_alerts_schema.py, db/price_alert_config_repository.py),
not here - editable from the dashboard (dashboard/views/
price_alert_config.py) so a threshold change takes effect on the next
automation run without a code deploy. This module only defines the shape
(PriceThreshold) and the cooldown config (PriceAlertConfig), which stays a
plain code constant since it's one global value, not per-ticker.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PriceThreshold:
    ticker: str
    # Alert when price crosses UP through this (previous < above <= current).
    above: Optional[float] = None
    # Alert when price crosses DOWN through this (previous > below >= current).
    below: Optional[float] = None


@dataclass(frozen=True)
class PriceAlertConfig:
    # Secondary protection only (see alerts/price_runner.py) - never a
    # substitute for correct crossing detection. 0 disables the cooldown.
    cooldown_minutes: int = 60


DEFAULT_PRICE_ALERT_CONFIG = PriceAlertConfig()
