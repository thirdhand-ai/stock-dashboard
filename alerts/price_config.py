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

Two ways to configure a ticker's threshold, both resolving to the same
absolute above/below shape:
  - MODE_FIXED (default, pre-existing behavior): above/below are literal
    dollar levels the user entered directly.
  - MODE_PERCENT: above/below are derived once, at write time, from a
    symmetric percent band around a fixed baseline_price (see
    resolve_percent_band) - never re-anchored as the price moves. Storing
    the resolved dollar levels (not the raw percent) means alerts/
    price_engine.py's crossing detection needs zero percent-specific
    branching: it only ever compares against above/below, exactly as
    before.
"""
from dataclasses import dataclass
from typing import Optional, Tuple

MODE_FIXED = "fixed"
MODE_PERCENT = "percent"


@dataclass(frozen=True)
class PriceThreshold:
    ticker: str
    # Alert when price crosses UP through this (previous < above <= current).
    above: Optional[float] = None
    # Alert when price crosses DOWN through this (previous > below >= current).
    below: Optional[float] = None
    # "fixed" (default) or "percent" - informational/display only. Crossing
    # detection always compares against the resolved above/below values
    # above, regardless of mode.
    mode: str = MODE_FIXED
    # Only set when mode == MODE_PERCENT: the band width in percent (e.g.
    # 6.0 for a +/-6% band). above/below are pre-resolved from this plus
    # baseline_price - never recomputed from percent at evaluation time.
    percent: Optional[float] = None
    # Only set when mode == MODE_PERCENT: the reference price the band was
    # anchored to when saved. Fixed at save time, not a trailing/moving
    # baseline.
    baseline_price: Optional[float] = None


def resolve_percent_band(baseline_price: float, percent: float) -> Tuple[float, float]:
    """Convert a symmetric percent band (percent=6.0 -> +/-6%) around
    baseline_price into absolute (above, below) levels - the same shape a
    fixed-dollar threshold already uses, so nothing downstream of this needs
    to know percent mode exists at all."""
    if baseline_price <= 0:
        raise ValueError("baseline_price must be greater than zero")
    if percent <= 0:
        raise ValueError("percent must be greater than zero")
    above = baseline_price * (1 + percent / 100)
    below = baseline_price * (1 - percent / 100)
    return above, below


@dataclass(frozen=True)
class PriceAlertConfig:
    # Secondary protection only (see alerts/price_runner.py) - never a
    # substitute for correct crossing detection. 0 disables the cooldown.
    cooldown_minutes: int = 60


DEFAULT_PRICE_ALERT_CONFIG = PriceAlertConfig()
