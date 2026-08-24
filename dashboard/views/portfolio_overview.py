"""Portfolio Overview: the landing page. A read-only aggregation of what's
already computed elsewhere in this dashboard - a tracked-tickers-by-type
breakdown (see dashboard/holding_type.py), total portfolio value and
gain/loss (combined + per-owner, from Real Holdings), active concentration
flags (from Portfolio Concentration), pending data-completeness gaps (from
Data Completeness), the 5 most recent Alert Activity entries (optionally
filtered to Real Holdings only), and a 30-day snapshot of the combined
portfolio-value chart (from Portfolio Performance).

Every number here comes from the same dashboard/data.py getters those pages
already call - this module computes nothing new and adds no second
implementation of any of it. Strictly read-only, same as every page it
pulls from.
"""
import streamlit as st

from dashboard import components
from dashboard.charts import build_portfolio_equity_chart
from dashboard.data import (
    get_alert_activity_feed,
    get_concentration_reports,
    get_data_completeness_report,
    get_holding_type_map,
    get_portfolio_value_series_by_scope,
    get_real_holdings_view,
)
from dashboard.holding_type import HOLDING_TYPE_ORDER, HOLDING_TYPE_REAL, holding_type_for, holding_type_label
from trading.portfolio_history import to_series
from trading.real_holdings import portfolio_totals, subtotals_by_owner

RECENT_ALERT_COUNT = 5
CHART_LOOKBACK_DAYS = 30


def _fmt_money(value):
    return f"${value:,.2f}" if value is not None else "—"


def _display_owner(owner):
    return owner if owner else "Unassigned"


def _page_link(page_attr: str, label: str):
    """Lazy import of pages_registry, same reason dashboard/views/
    watchlist.py imports it lazily inside a callback rather than at module
    load - pages_registry imports every view module to build its Page
    objects, so importing it eagerly here would be circular."""
    from dashboard import pages_registry
    st.page_link(getattr(pages_registry, page_attr), label=label)


def _render_holdings_breakdown():
    st.subheader("Tracked tickers by type")
    st.caption(
        "🏦 Real Holdings = actually owned. 👁️ Watchlist = original tracked tickers, no ownership. "
        "🧪 Exploratory = speculative research/AI tickers, no ownership. A ticker that's both owned "
        "and on the watchlist is always counted as Real Holdings."
    )
    type_map = get_holding_type_map()
    counts = {t: 0 for t in HOLDING_TYPE_ORDER}
    for holding_type in type_map.values():
        counts[holding_type] = counts.get(holding_type, 0) + 1

    cols = st.columns(len([t for t in HOLDING_TYPE_ORDER if counts.get(t, 0) > 0]) or 1)
    i = 0
    for holding_type in HOLDING_TYPE_ORDER:
        if counts.get(holding_type, 0) == 0:
            continue
        cols[i].metric(holding_type_label(holding_type), counts[holding_type])
        i += 1


def _render_value_summary():
    st.subheader("Portfolio value")
    views = get_real_holdings_view()
    if not views:
        components.empty_state("No real holdings tracked yet", "Add one on the Real Holdings page first.", icon="💼")
        _page_link("PAGE_REAL_HOLDINGS", "Go to Real Holdings →")
        return

    totals = portfolio_totals(views)
    c1, c2, c3 = st.columns(3)
    c1.metric("Total market value", _fmt_money(totals["total_market_value"]))
    c2.metric("Total unrealized P&L", _fmt_money(totals["total_unrealized_pl"]))
    c3.metric("Total realized gain", _fmt_money(totals["total_realized_gain"]))

    subtotals = subtotals_by_owner(views)
    if len(subtotals) > 1:
        rows = [
            {
                "Owner": _display_owner(owner),
                "Market value": _fmt_money(t["total_market_value"]),
                "Unrealized P&L": _fmt_money(t["total_unrealized_pl"]),
                "Realized gain": _fmt_money(t["total_realized_gain"]) if t["total_realized_gain"] else "—",
            }
            for owner, t in subtotals.items()
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)

    _page_link("PAGE_REAL_HOLDINGS", "Go to Real Holdings →")


def _render_concentration_flags():
    st.subheader("Concentration flags")
    reports = get_concentration_reports()
    if not reports or reports.get("Combined") is None:
        st.caption("No real holdings tracked yet - nothing to flag.")
        return

    any_flags = False
    for scope_label, report in reports.items():
        for p in report.flagged_positions:
            any_flags = True
            st.warning(f"**{scope_label}**: {p.ticker} is {p.weight_pct:.1f}% of portfolio value", icon="⚠️")
        for s in report.flagged_sectors:
            any_flags = True
            st.warning(f"**{scope_label}**: {s.industry} sector is {s.weight_pct:.1f}% of portfolio value", icon="⚠️")

    if not any_flags:
        st.success("No position or sector concentration flags currently active.", icon="✅")

    _page_link("PAGE_CONCENTRATION", "Go to Portfolio Concentration →")


def _render_completeness_gaps():
    st.subheader("Data completeness")
    report = get_data_completeness_report()
    if not report.gaps:
        st.success("No data-completeness gaps currently flagged.", icon="✅")
    else:
        st.metric("Total gaps flagged", len(report.gaps))
        category_rows = [
            {"Category": category, "Count": len(gaps)}
            for category, gaps in report.by_category.items()
        ]
        st.dataframe(category_rows, use_container_width=True, hide_index=True)

    _page_link("PAGE_DATA_COMPLETENESS", "Go to Data Completeness →")


def _render_recent_alerts():
    st.subheader("Recent alert activity")
    feed = get_alert_activity_feed()
    if feed.empty:
        st.caption("No alert activity recorded yet.")
        _page_link("PAGE_ALERT_ACTIVITY", "Go to Alert Activity →")
        return

    type_map = get_holding_type_map()
    feed = feed.copy()
    feed["holding_type"] = feed["ticker"].apply(lambda t: holding_type_for(t, type_map))

    real_only = st.checkbox(
        "Show only Real Holdings alerts",
        value=False,
        help="Hide alert activity for Watchlist/Exploratory tickers - isolate what actually touches "
             "Mom/Dad/Tyler's real money.",
    )
    if real_only:
        feed = feed[feed["holding_type"] == HOLDING_TYPE_REAL]
        if feed.empty:
            st.caption("No Real Holdings alert activity recorded yet.")
            _page_link("PAGE_ALERT_ACTIVITY", "Go to Alert Activity →")
            return

    recent = feed.head(RECENT_ALERT_COUNT).copy()
    recent["Timestamp"] = recent["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    recent["Ticker"] = recent["ticker"].fillna("—")
    recent["Holding Type"] = recent["holding_type"].map(holding_type_label)
    recent = recent.rename(columns={"type": "Type", "description": "Description"})
    st.dataframe(
        recent[["Timestamp", "Type", "Ticker", "Holding Type", "Description"]],
        use_container_width=True, hide_index=True,
    )

    _page_link("PAGE_ALERT_ACTIVITY", "Go to Alert Activity →")


def _render_performance_snapshot():
    st.subheader("Performance (last 30 days, combined)")
    by_scope = get_portfolio_value_series_by_scope()
    series = by_scope.get("Combined") if by_scope else None
    if series is None or not series.dates:
        st.caption("No priced history for the combined portfolio yet.")
        _page_link("PAGE_PORTFOLIO_PERFORMANCE", "Go to Portfolio Performance →")
        return

    equity_series = to_series(series).tail(CHART_LOOKBACK_DAYS)
    st.plotly_chart(
        build_portfolio_equity_chart(equity_series),
        use_container_width=True, config={"displaylogo": False},
    )
    st.caption(f"Latest stored value: {_fmt_money(series.values[-1])} as of {series.dates[-1]}.")

    _page_link("PAGE_PORTFOLIO_PERFORMANCE", "Go to Portfolio Performance →")


def render():
    st.title("Portfolio Overview")
    st.caption(
        "A single-glance summary pulling together what's already computed elsewhere in this "
        "dashboard - nothing here is recalculated, every number links back to its source page."
    )
    components.disclaimer("Portfolio summary only - not a stock signal, not a trade recommendation.")

    _render_holdings_breakdown()
    st.divider()
    _render_value_summary()
    st.divider()
    _render_concentration_flags()
    st.divider()
    _render_completeness_gaps()
    st.divider()
    _render_recent_alerts()
    st.divider()
    _render_performance_snapshot()
