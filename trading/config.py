"""Explicit, configurable rules for Phase 7 paper trading.

Every limit lives here, by name, so trading/risk.py and trading/engine.py
never contain an unexplained constant - the same convention as
signals/config.py, backtest/config.py, alerts/config.py, and
automation/config.py.

Several "rules" below (long_only, allow_margin, allow_short, allow_options,
allow_averaging_down, allow_duplicate_ticker_position) are not merely
config toggles that risk.py happens to check - there is no code path
anywhere in trading/ that constructs a sell-to-open, margin, options, or
averaging-down order. They are recorded here as explicit, auditable
documentation of that structural guarantee, and risk.py still asserts them
so a future change to this file cannot silently loosen behavior without a
corresponding, visible check failing.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskConfig:
    # Position sizing - fraction of *current* Alpaca paper account equity,
    # not a fixed dollar amount, so limits scale with the real account.
    max_position_size_pct: float = 0.10
    """Max notional for a single new entry, as a fraction of current equity."""

    max_total_exposure_pct: float = 0.60
    """Max total long market value (existing positions + proposed entry), as a fraction of current equity."""

    max_open_positions: int = 6
    """Max number of simultaneously open strategy positions."""

    long_only: bool = True
    allow_margin: bool = False
    allow_short: bool = False
    allow_options: bool = False
    allow_averaging_down: bool = False
    """No automatic scale-in: an entry is only ever considered when the ticker has no open position (see trading/signals_bridge.py)."""
    allow_duplicate_ticker_position: bool = False


@dataclass(frozen=True)
class ExecutionConfig:
    """Order-construction settings. No live/production endpoint option
    exists anywhere in this dataclass or its consumers - see
    trading/client.py's fail-closed paper verification."""

    order_type: str = "market"
    time_in_force: str = "day"

    use_notional_entries: bool = True
    """Entries are sized as a dollar notional (10% of equity) rather than a
    share count. Alpaca's paper API supports fractional-share notional
    market orders, which makes this *simpler and safer* than hand-rounding
    a share quantity: the position size is exactly the risk-config
    percentage with no leftover-cash slop, and no separate "how many whole
    shares fit in $X" logic is needed. Exits still close the exact
    Alpaca-reported position quantity (see trading/orders.py), fractional
    or not, so nothing is ever partially unwound."""

    client_order_id_prefix: str = "sb"
    """"sb" = signal-bridge. See trading/idempotency.py for the full scheme."""


DEFAULT_RISK_CONFIG = RiskConfig()
DEFAULT_EXECUTION_CONFIG = ExecutionConfig()
