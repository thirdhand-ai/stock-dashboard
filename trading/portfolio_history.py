"""Historical portfolio value for real_holdings - sum(shares held × close
price) at each date, across whatever price history is already stored per
ticker (db/price_repository.py::load_price_history - the same price data
every other view in this codebase reads).

For most holdings, no dated share-count-change events exist anywhere in
this system (dividend_payments has zero rows for them, so no DRIP
reinvestment has a recorded date; realized_sales rows are often undated
too). For those, the best available estimate is each holding's CURRENT
share count applied across its full stored price history - but for a
holding KNOWN to have grown or shrunk at some point (DRIP reinvestment, a
partial sale) without dated records, applying today's count to years-old
prices can overstate (or understate) what was actually held then. Every
such ticker is flagged as approximate rather than silently presented as
precise - see _ticker_reliability. A holding with NO shares on record at
all (needs_manual_entry) contributes nothing and is listed separately as
excluded, never estimated.

Some holdings (currently HPI and KMI) DO have a full dated history now:
an original cash-purchase lot in real_holding_lots plus every subsequent
DRIP reinvestment as a dated, reinvested=1 row in dividend_payments (see
_reconstruct_share_timeline). When those dated events sum to exactly the
holding's current recorded share count, this module reconstructs a
precise piecewise share count - 0 shares before the first lot, stepping
up at each event's date - instead of applying a constant count. A holding
whose dated events DON'T fully account for its current share count (e.g.
no lots recorded at all, or only partial DRIP history) falls back to the
flat, current-share-count approximation, flagged via share_history_caveat
same as before - this is auto-detected per (ticker, owner) from what's
actually in the database, not hardcoded to any specific ticker, so any
future holding with a full dated history gets the same precise treatment
automatically.

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
below prefers 'alpaca_adjusted' for a ticker only when it's actually
CURRENT - i.e. its latest date is not behind that same ticker's raw
'alpaca' history - falling back to the normal resolved source otherwise
(e.g. XLV/NCLH/KMI/HPI/STN, which only ever had 'alpaca'; and
META/MSFT/NOW/NVDA once 'alpaca_adjusted' stopped advancing past
2026-08-12 while 'alpaca' kept ingesting daily - see strategy_lab/
data.py's docstring: that source is a research/backtest cache, populated
by a one-off backfill plus a single-latest-row completeness patch, not a
daily-incrementing feed, and is deliberately left untouched by this fix).
This is scoped to this module only - db/price_repository.py's shared
resolve_source and strategy_lab/data.py's cache are both unchanged, so no
other page's or job's behavior is affected.
"""
import bisect
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from db.dividend_payments_repository import list_dividend_payments
from db.price_repository import load_price_history
from db.real_holding_lots_repository import list_real_holding_lots
from db.real_holdings_repository import RealHolding, list_real_holdings
from db.realized_sales_repository import list_realized_sales

# Tolerance for comparing a reconstructed dated-event share total against
# real_holdings.shares - both are sums of many float additions (dividend
# shares are total_received / amount_per_share), so they won't always
# match to the last bit; anything within a hundredth of a share is the
# same position, not a partial/incomplete reconstruction.
RECONSTRUCTION_TOLERANCE = 0.01


def _reconstruct_share_timeline(conn, ticker: str, owner: str) -> List[Tuple[str, float]]:
    """Every dated share-count-increasing event for (ticker, owner), sorted
    ascending by date, as (date, cumulative_shares_as_of_that_date) pairs -
    a cash-purchase lot from real_holding_lots (db/
    real_holding_lots_schema.py) contributes its `shares` directly; a
    reinvested dividend_payments row (db/dividend_payments_schema.py)
    contributes total_received / amount_per_share, the shares that
    specific reinvestment bought. [] if this (ticker, owner) has no dated
    events at all recorded in either table - the caller falls back to the
    flat, current-share-count approximation in that case (see
    _is_fully_reconstructed)."""
    lots = list_real_holding_lots(conn, ticker=ticker, owner=owner)
    payments = list_dividend_payments(conn, ticker=ticker, owner=owner)
    events: List[Tuple[str, float]] = [(lot.purchase_date, lot.shares) for lot in lots]
    events += [
        (p.pay_date, p.total_received / p.amount_per_share)
        for p in payments if p.reinvested and p.amount_per_share
    ]
    if not events:
        return []

    events.sort(key=lambda e: e[0])
    timeline: List[Tuple[str, float]] = []
    running = 0.0
    for date, delta in events:
        running += delta
        timeline.append((date, running))
    return timeline


def _is_fully_reconstructed(timeline: List[Tuple[str, float]], shares: Optional[float]) -> bool:
    """True when `timeline` (from _reconstruct_share_timeline) accounts for
    the ENTIRE current share count, not just some of it - the reconstructed
    running total after the last event must land within
    RECONSTRUCTION_TOLERANCE of real_holdings.shares. A holding with only
    partial dated history (e.g. some but not all DRIP payments recorded)
    would otherwise silently understate early-history value without ever
    being flagged - this check is what keeps a partial reconstruction from
    masquerading as a complete one."""
    return bool(timeline) and shares is not None and abs(timeline[-1][1] - shares) <= RECONSTRUCTION_TOLERANCE


def _shares_as_of(timeline: List[Tuple[str, float]], date: str) -> float:
    """Cumulative shares held as of `date`, from a `timeline` already
    sorted ascending by date (as returned by _reconstruct_share_timeline).
    0.0 for any date before the first event - the position didn't exist
    yet, so it never contributed to portfolio value that early. ISO date
    strings ("YYYY-MM-DD") compare correctly with plain string ordering,
    so bisect works directly on them without parsing."""
    dates = [d for d, _ in timeline]
    idx = bisect.bisect_right(dates, date) - 1
    return timeline[idx][1] if idx >= 0 else 0.0


@dataclass(frozen=True)
class TickerReliability:
    ticker: str
    owner: str
    caveat: str


def _ticker_reliability(holding: RealHolding, conn, timeline: List[Tuple[str, float]]) -> Optional[TickerReliability]:
    """None if this holding's value-over-time contribution is believed
    precise (either a full dated reconstruction exists - see
    _is_fully_reconstructed - or there's no reason to doubt the flat
    current-share-count approximation); a TickerReliability explaining why
    otherwise. Checked in priority order: an explicit share_history_caveat
    (DRIP growth with no full dated reconstruction - see db/
    real_holdings_schema.py's docstring) UNLESS `timeline` already fully
    accounts for the current share count (in which case the caveat is
    stale and skipped - see trading/real_holdings.py's real_holdings row
    for HPI/KMI, whose share_history_caveat was cleared once their dated
    reconstruction landed), then any realized_sales row for this holding
    with an unknown sale_date (a sale happened, but we don't know when, so
    we don't know which historical dates the current, post-sale count
    actually applies to - independent of whether the growth side of the
    history is dated)."""
    if not _is_fully_reconstructed(timeline, holding.shares) and holding.share_history_caveat:
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
RAW_SOURCE = "alpaca"


def _load_price_history_for_value_chart(conn, ticker: str) -> pd.DataFrame:
    """Prefer ADJUSTED_SOURCE for this ticker only when it's actually
    CURRENT - see this module's docstring for why the adjusted series is
    preferred at all when it qualifies (split-adjustment consistency
    matters here in a way it doesn't for a single "current price" lookup
    elsewhere). "Current" means: RAW_SOURCE has no row dated later than
    ADJUSTED_SOURCE's latest row for this same ticker - i.e. the adjusted
    series isn't lagging the raw one. A ticker whose ADJUSTED_SOURCE cache
    has stalled while RAW_SOURCE kept ingesting (e.g. a frozen research
    backfill - see strategy_lab/data.py) falls back to the RAW_SOURCE
    series directly - NOT to load_price_history(conn, ticker)'s generic
    resolve_source() pick, which would just re-apply MIN_TRUST_RATIO and
    hand ADJUSTED_SOURCE right back for a ticker like NOW, whose thin
    RAW_SOURCE row count (production ingestion only recently started)
    can't clear that floor against ADJUSTED_SOURCE's much larger backfill
    count. A ticker with no adjusted series at all still uses the normal
    resolved source (e.g. XLV/NCLH/KMI/HPI/STN, which only ever had
    RAW_SOURCE, so no such shadowing is possible)."""
    adjusted = load_price_history(conn, ticker, source=ADJUSTED_SOURCE)
    if adjusted.empty:
        return load_price_history(conn, ticker)

    raw = load_price_history(conn, ticker, source=RAW_SOURCE)
    adjusted_is_stale = not raw.empty and str(raw["date"].iloc[-1]) > str(adjusted["date"].iloc[-1])
    if adjusted_is_stale:
        return raw

    return adjusted


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

        timeline = _reconstruct_share_timeline(conn, h.ticker, h.owner)
        reliability = _ticker_reliability(h, conn, timeline)
        if reliability is not None:
            approximate.append(reliability)
        fully_reconstructed = _is_fully_reconstructed(timeline, h.shares)

        df = _load_price_history_for_value_chart(conn, h.ticker)
        if df.empty:
            continue
        last_date_per_ticker[h.ticker] = str(df["date"].iloc[-1])
        for date, close in zip(df["date"], df["close"]):
            shares_as_of = _shares_as_of(timeline, str(date)) if fully_reconstructed else h.shares
            per_date_total[str(date)] = per_date_total.get(str(date), 0.0) + shares_as_of * float(close)

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
