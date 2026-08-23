"""Historical portfolio value for real_holdings - sum(shares held × close
price) at each date, across whatever price history is already stored per
ticker (db/price_repository.py::load_price_history - the same price data
every other view in this codebase reads).

No dated share-count-change events exist anywhere in this system today
(checked before building this: dividend_payments has zero rows, so no
DRIP reinvestment has a recorded date; the one realized_sales row - META
- has both purchase_date and sale_date NULL). Given that, the best
available estimate is each holding's CURRENT share count applied across
its full stored price history - but for a holding KNOWN to have grown or
shrunk at some point (DRIP reinvestment, a partial sale), applying
today's count to years-old prices can overstate (or understate) what was
actually held then. Every such ticker is flagged as approximate rather
than silently presented as precise - see _ticker_reliability. A holding
with NO shares on record at all (needs_manual_entry) contributes nothing
and is listed separately as excluded, never estimated.

If dated share-count events are added later (e.g. real purchase/sale
dates filled into realized_sales, or a future dividend_payments schema
that tracks shares-acquired-per-DRIP-payment with dates), this module can
be extended to reconstruct a precise piecewise share count instead of a
constant one - that reconstruction isn't built yet because no dated data
exists to reconstruct from.

Price source: deliberately does NOT use db/price_repository.py::
load_price_history's default source resolution unmodified - found while
building this that NVDA's default-resolved source ('alpaca', chosen over
'alpaca_adjusted' because it clears resolve_source's MIN_TRUST_RATIO
floor) is NOT split-adjusted, and NVDA's real 10-for-1 split on
2024-06-10 shows as a ~10x overnight cliff in that source (confirmed by
querying prices directly: 'alpaca' jumps $1224→$121 on the split date;
'alpaca_adjusted' is smooth, ~$122→$121). A single "current price"
lookup elsewhere in this codebase never crosses a split boundary, so this
never surfaced there - a multi-year value-over-time chart applying a
constant share count is uniquely exposed to it. _load_price_history_for_value_chart
below prefers 'alpaca_adjusted' whenever it has any data for a ticker,
falling back to the normal resolved source otherwise (e.g. XLV/NCLH/KMI/
HPI/STN, which only have 'alpaca'). This is scoped to this module only -
db/price_repository.py's shared resolve_source is unchanged, so no other
page's behavior is affected by this fix.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from db.price_repository import load_price_history
from db.real_holdings_repository import RealHolding, list_real_holdings
from db.realized_sales_repository import list_realized_sales


@dataclass(frozen=True)
class TickerReliability:
    ticker: str
    owner: str
    caveat: str


def _ticker_reliability(conn, holding: RealHolding) -> Optional[TickerReliability]:
    """None if this holding's current share count is believed to apply
    across its whole stored price history; a TickerReliability explaining
    why otherwise. Checked in priority order: an explicit
    share_history_caveat (DRIP growth - see db/real_holdings_schema.py's
    docstring) first, then any realized_sales row for this holding with
    an unknown sale_date (a sale happened, but we don't know when, so we
    don't know which historical dates the current, post-sale count
    actually applies to)."""
    if holding.share_history_caveat:
        return TickerReliability(holding.ticker, holding.owner, holding.share_history_caveat)

    sales = list_realized_sales(conn, ticker=holding.ticker, owner=holding.owner)
    undated_sale = next((s for s in sales if s.sale_date is None), None)
    if undated_sale is not None:
        return TickerReliability(
            holding.ticker, holding.owner,
            f"A {undated_sale.shares_sold:g}-share sale is recorded for this holding, but its date "
            "isn't tracked - the current share count may not have applied to earlier dates shown here.",
        )
    return None


ADJUSTED_SOURCE = "alpaca_adjusted"


def _load_price_history_for_value_chart(conn, ticker: str) -> pd.DataFrame:
    """Prefer ADJUSTED_SOURCE whenever it has any data for this ticker -
    see this module's docstring for why (split-adjustment consistency
    matters here in a way it doesn't for a single "current price" lookup
    elsewhere). Falls back to the normal resolved source for a ticker
    with no adjusted series at all."""
    adjusted = load_price_history(conn, ticker, source=ADJUSTED_SOURCE)
    if not adjusted.empty:
        return adjusted
    return load_price_history(conn, ticker)


@dataclass
class PortfolioValueSeries:
    scope_label: str
    dates: List[str] = field(default_factory=list)
    values: List[float] = field(default_factory=list)
    excluded_tickers: List[str] = field(default_factory=list)
    approximate: List[TickerReliability] = field(default_factory=list)
    stale_tickers: List[Dict[str, str]] = field(default_factory=list)


def build_portfolio_value_series(conn, holdings: List[RealHolding], scope_label: str) -> PortfolioValueSeries:
    excluded = []
    approximate = []
    stale = []
    per_date_total: Dict[str, float] = {}
    last_date_per_ticker: Dict[str, str] = {}

    for h in holdings:
        if h.shares is None:
            excluded.append(h.ticker)
            continue

        reliability = _ticker_reliability(conn, h)
        if reliability is not None:
            approximate.append(reliability)

        df = _load_price_history_for_value_chart(conn, h.ticker)
        if df.empty:
            continue
        last_date_per_ticker[h.ticker] = str(df["date"].iloc[-1])
        for date, close in zip(df["date"], df["close"]):
            per_date_total[str(date)] = per_date_total.get(str(date), 0.0) + h.shares * float(close)

    all_dates = sorted(per_date_total)

    # Truncate the TAIL at the last date every currently-priced ticker
    # actually has data for (not the union of all dates) - a ticker whose
    # own price source lags the rest would otherwise silently drop out of
    # only the most recent day(s), making the total look like it crashed
    # right at the end of the chart when really one ticker's data just
    # hasn't caught up yet. This is the "gap" option (not "flag"): the
    # early history is left alone (a ticker's price history genuinely
    # starting later is real portfolio composition history, not an
    # ingestion artifact - see this module's docstring), only the
    # currently-lagging tail is cut. stale_tickers below still reports
    # exactly what was cut and why.
    full_coverage_end = min(last_date_per_ticker.values()) if last_date_per_ticker else None
    dates = [d for d in all_dates if full_coverage_end is None or d <= full_coverage_end]
    values = [per_date_total[d] for d in dates]

    # The laggard(s) forcing the cutoff are whichever ticker(s) sit AT the
    # truncation boundary - everyone else has data past that point that
    # got cut off waiting for these to catch up.
    if full_coverage_end is not None and all_dates and all_dates[-1] > full_coverage_end:
        for ticker, last_date in last_date_per_ticker.items():
            if last_date == full_coverage_end:
                stale.append({"ticker": ticker, "last_priced_date": last_date, "portfolio_latest_date": all_dates[-1]})

    return PortfolioValueSeries(
        scope_label=scope_label, dates=dates, values=values, excluded_tickers=excluded,
        approximate=approximate, stale_tickers=stale,
    )


def build_portfolio_value_series_by_scope(conn) -> Dict[str, PortfolioValueSeries]:
    """One series for the combined portfolio plus one per distinct owner -
    same "each scope self-contained" pattern as trading/concentration.py's
    build_concentration_reports."""
    holdings = list_real_holdings(conn)
    series = {"Combined": build_portfolio_value_series(conn, holdings, "Combined")}
    owners = sorted({h.owner or "" for h in holdings})
    for owner in owners:
        label = owner if owner else "Unassigned"
        owner_holdings = [h for h in holdings if (h.owner or "") == owner]
        series[label] = build_portfolio_value_series(conn, owner_holdings, label)
    return series


def to_series(value_series: PortfolioValueSeries) -> pd.Series:
    """Convert to a pandas Series indexed by date, for reuse with the
    existing dashboard.charts.index_to_100 / build_portfolio_equity_chart
    infrastructure (dashboard/views/paper_portfolio.py's SPY-overlay chart
    already uses exactly this pattern - no need for a second one)."""
    return pd.Series(value_series.values, index=pd.to_datetime(value_series.dates))
