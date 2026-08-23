"""Portfolio Performance Over Time: historical value of real holdings -
sum(shares held × close price) at each date, across whatever price
history is already stored per ticker, combined and per-owner, plus a
relative-performance overlay against a benchmark (SPY). See
trading/portfolio_history.py's docstring for the core limitation this
page is built around: no dated purchase/DRIP/sale events exist anywhere
in this system precisely enough to reconstruct a holding's share count
changing over time, so the current share count is applied across a
ticker's full stored history as the best available estimate - any
holding known to have changed at an undated point (DRIP growth, an
undated partial sale) is flagged rather than presented as precise, and a
holding with no shares on record at all is excluded entirely rather than
estimated.

Benchmark overlay reuses dashboard/views/paper_portfolio.py's exact
SPY-comparison pattern (dashboard.charts.index_to_100 +
build_portfolio_equity_chart, dashboard.data.get_paper_benchmark_series) -
same infrastructure, no second implementation.
"""
import pandas as pd
import streamlit as st

from dashboard import components
from dashboard.charts import build_portfolio_equity_chart, index_to_100
from dashboard.data import get_paper_benchmark_series, get_portfolio_value_series_by_scope
from trading.portfolio_history import to_series

BENCHMARK_TICKER = "SPY"


def _display_owner(owner):
    return owner if owner else "Unassigned"


def _render_caveats(series):
    if series.excluded_tickers:
        st.warning(
            "Excluded from this chart (shares not confirmed, cannot compute any historical value): "
            + ", ".join(series.excluded_tickers),
            icon="⚠️",
        )
    if series.stale_tickers:
        details = ", ".join(f"{s['ticker']} (last priced {s['last_priced_date']})" for s in series.stale_tickers)
        st.warning(
            f"Latest-date total excludes: {details} — its stored price data lags the rest of the "
            f"portfolio (latest combined date: {series.stale_tickers[0]['portfolio_latest_date']}). "
            "Usually this is the split-adjusted price source this chart prefers (for consistency across "
            "a stock's history) not yet having as recent a refresh as the ticker's raw, unadjusted "
            "source - see trading/portfolio_history.py's ADJUSTED_SOURCE preference.",
            icon="⚠️",
        )
    for r in series.approximate:
        st.info(f"**{r.ticker} ({_display_owner(r.owner)})** — {r.caveat}", icon="ℹ️")


def _render_value_chart(series):
    st.subheader(f"{series.scope_label} portfolio value over time")
    if not series.dates:
        components.empty_state(
            f"No priced history for {series.scope_label}",
            "Every position in this scope is missing shares (needs manual entry), so there's nothing "
            "to compute historical value from yet.",
            icon="📈",
        )
        return

    equity_series = to_series(series)
    st.plotly_chart(
        build_portfolio_equity_chart(equity_series),
        use_container_width=True, config={"displaylogo": False},
    )
    st.caption(
        f"Latest stored value: ${series.values[-1]:,.2f} as of {series.dates[-1]}. Earlier dates reflect "
        "only the tickers that already had stored price history at that time - some holdings' price "
        "history starts later than others, so a total from before a newer ticker's history began "
        "represents a smaller, earlier subset of today's holdings, not the full current portfolio."
    )


def _render_benchmark_chart(series):
    st.subheader(f"{series.scope_label} vs. {BENCHMARK_TICKER} (relative performance)")
    if not series.dates:
        return

    equity_series = to_series(series)
    benchmark_df = get_paper_benchmark_series(BENCHMARK_TICKER)
    if benchmark_df is None or benchmark_df.empty:
        st.caption(f"{BENCHMARK_TICKER} benchmark unavailable — no price history stored locally yet.")
        return

    bench = benchmark_df.copy()
    bench["date"] = pd.to_datetime(bench["date"])
    bench = bench.set_index("date")["close"]

    common_index = equity_series.index.intersection(bench.index).sort_values()
    if len(common_index) < 2:
        st.caption(f"Not enough overlapping dates with {BENCHMARK_TICKER} to compare yet.")
        return

    equity_common = equity_series.loc[common_index]
    bench_common = bench.loc[common_index]

    st.plotly_chart(
        build_portfolio_equity_chart(
            index_to_100(equity_common), index_to_100(bench_common), benchmark_label=BENCHMARK_TICKER,
        ),
        use_container_width=True, config={"displaylogo": False},
    )
    st.caption(
        f"Both series indexed to 100 at {common_index.min().strftime('%Y-%m-%d')} (the earliest date "
        f"both this portfolio's value and {BENCHMARK_TICKER} have stored prices) — shows relative "
        "growth, not absolute dollars."
    )


def render():
    st.title("Portfolio Performance Over Time")
    st.caption(
        "Historical value of real holdings - sum(shares held × close price) at each date, from "
        "whatever price history is already stored per ticker. A holding's current share count is "
        "applied across its full stored history (the best available estimate, since no dated "
        "purchase/DRIP/sale events are tracked precisely enough to reconstruct changes over time) - "
        "any holding known to have changed at an undated point is flagged below, and a holding with no "
        "shares on record at all is excluded entirely rather than estimated."
    )
    components.disclaimer("Portfolio value tracking only - not a stock signal, not a trade recommendation.")

    by_scope = get_portfolio_value_series_by_scope()
    if not by_scope:
        components.empty_state("No real holdings tracked yet", "Add one on the Real Holdings page first.", icon="📈")
        return

    scope = st.radio("Portfolio", list(by_scope.keys()), horizontal=True, key="portfolio_performance_scope")
    series = by_scope[scope]

    _render_caveats(series)
    st.divider()
    _render_value_chart(series)
    st.divider()
    _render_benchmark_chart(series)
