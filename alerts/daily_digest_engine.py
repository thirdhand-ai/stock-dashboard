"""Daily digest content generation: one summary row per tracked ticker
(current price, day-over-day % change, distance to its configured price-
threshold and volatility-alert levels), and the pure text formatting that
turns those rows into the digest message body.

Structurally SEPARATE from alerts/price_engine.py and alerts/
volatility_engine.py: those decide WHETHER to alert (a crossing, a large
move). This module never decides anything - it reports current state for
every ticker unconditionally, whether or not either alert system would
fire. It reuses alerts/volatility_engine.py's compute_day_over_day_move_pct
for the day-over-day % change (the exact same calculation the volatility
system uses for its own move detection - no reason to recompute it twice
with different logic), and reads price history directly via
db/price_repository.py, same as alerts/volatility_engine.py does.

A ticker's threshold information is entirely optional per row: a ticker
with no PriceThreshold or VolatilityAlertConfig configured for it still
gets a price/change line, just with no threshold distance shown.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from alerts.price_config import PriceThreshold
from alerts.volatility_config import VolatilityAlertConfig
from alerts.volatility_engine import compute_day_over_day_move_pct
from db.price_repository import load_price_history, resolve_source


@dataclass
class DigestTickerRow:
    ticker: str
    ok: bool
    reason_unavailable: Optional[str] = None
    current_price: Optional[float] = None
    previous_price: Optional[float] = None
    change_pct: Optional[float] = None
    data_date: Optional[str] = None
    source: Optional[str] = None
    # Price-threshold distance (only set when a PriceThreshold is configured
    # for this ticker) - positive means the level hasn't been reached yet;
    # negative means current price is already past it.
    price_above: Optional[float] = None
    price_below: Optional[float] = None
    distance_to_above_pct: Optional[float] = None
    distance_to_below_pct: Optional[float] = None
    # Volatility-threshold distance (only set when a VolatilityAlertConfig
    # is configured) - how many more percentage points of day-over-day move
    # would be needed to reach the configured threshold. change_pct IS the
    # same day-over-day move alerts/volatility_engine.py evaluates, so this
    # is derived from it directly, never recomputed.
    volatility_threshold_percent: Optional[float] = None


def _distance_to_above_pct(current_price: float, above: float) -> float:
    return (above - current_price) / current_price * 100


def _distance_to_below_pct(current_price: float, below: float) -> float:
    return (current_price - below) / current_price * 100


def build_ticker_digest_row(
    conn,
    ticker: str,
    price_threshold: Optional[PriceThreshold] = None,
    volatility_config: Optional[VolatilityAlertConfig] = None,
) -> DigestTickerRow:
    """Read the two most recent stored closes for `ticker` and build its
    digest row. Never raises on missing/insufficient data - returns
    ok=False (no price data) or a row with change_pct=None (first-ever
    stored bar, no prior close to compare against), same fail-soft
    contract alerts/volatility_engine.py's evaluate_ticker_volatility uses."""
    price_df = load_price_history(conn, ticker)
    if price_df.empty:
        return DigestTickerRow(ticker=ticker, ok=False, reason_unavailable="no price data available")

    source = resolve_source(conn, ticker)
    latest = price_df.iloc[-1]
    current_price = float(latest["close"])
    data_date = str(latest["date"])

    previous_price = None
    change_pct = None
    if len(price_df) >= 2:
        previous = price_df.iloc[-2]
        previous_price = float(previous["close"])
        change_pct = compute_day_over_day_move_pct(previous_price, current_price)

    row = DigestTickerRow(
        ticker=ticker, ok=True, current_price=current_price, previous_price=previous_price,
        change_pct=change_pct, data_date=data_date, source=source,
    )

    if price_threshold is not None:
        row.price_above = price_threshold.above
        row.price_below = price_threshold.below
        if price_threshold.above is not None:
            row.distance_to_above_pct = _distance_to_above_pct(current_price, price_threshold.above)
        if price_threshold.below is not None:
            row.distance_to_below_pct = _distance_to_below_pct(current_price, price_threshold.below)

    if volatility_config is not None:
        row.volatility_threshold_percent = volatility_config.threshold_percent

    return row


def build_daily_digest(
    conn,
    tickers: List[str],
    price_thresholds_by_ticker: Optional[dict] = None,
    volatility_configs_by_ticker: Optional[dict] = None,
) -> List[DigestTickerRow]:
    """One DigestTickerRow per ticker, in the given order. Both threshold
    maps default to empty (a ticker simply gets no threshold distance)."""
    price_thresholds_by_ticker = price_thresholds_by_ticker or {}
    volatility_configs_by_ticker = volatility_configs_by_ticker or {}
    return [
        build_ticker_digest_row(
            conn, ticker,
            price_threshold=price_thresholds_by_ticker.get(ticker),
            volatility_config=volatility_configs_by_ticker.get(ticker),
        )
        for ticker in tickers
    ]


def format_ticker_digest_line(row: DigestTickerRow) -> str:
    """Pure text formatting, reused identically by the email body and the
    Discord embed - e.g. "AAPL: $233.10 (+0.8%), 3.2% from upper
    threshold". No network call, no DB access."""
    if not row.ok:
        return f"{row.ticker}: unavailable ({row.reason_unavailable})"

    change_text = f"{row.change_pct:+.1f}%" if row.change_pct is not None else "n/a"
    parts = [f"{row.ticker}: ${row.current_price:,.2f} ({change_text})"]

    threshold_bits = []
    if row.distance_to_above_pct is not None:
        threshold_bits.append(f"{abs(row.distance_to_above_pct):.1f}% from upper threshold")
    if row.distance_to_below_pct is not None:
        threshold_bits.append(f"{abs(row.distance_to_below_pct):.1f}% from lower threshold")
    if row.volatility_threshold_percent is not None:
        moved = abs(row.change_pct) if row.change_pct is not None else 0.0
        threshold_bits.append(f"{moved:.1f}% of {row.volatility_threshold_percent:.1f}% volatility threshold")

    if threshold_bits:
        parts.append(", ".join(threshold_bits))

    return ", ".join(parts)


def format_digest_body(rows: List[DigestTickerRow], trading_date: str) -> str:
    """Full multi-line digest body: a header line plus one
    format_ticker_digest_line per ticker. Pure function, no network call -
    reused by both the email body and dry-run reporting."""
    lines = [f"Daily Digest - {trading_date} ({len(rows)} ticker{'s' if len(rows) != 1 else ''})", ""]
    lines.extend(format_ticker_digest_line(row) for row in rows)
    return "\n".join(lines)
