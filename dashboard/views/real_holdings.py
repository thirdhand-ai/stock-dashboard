"""Real Holdings: actual, real-money positions (some held by Tyler's
mother) - current price, gain/loss vs. cost basis, and % of portfolio.
Entirely separate from the Paper Portfolio page (dashboard/views/
paper_portfolio.py), which is the simulated Alpaca paper-trading account -
see trading/real_holdings.py's docstring for why these two systems never
share a table.

Read-only: no order execution, no Alpaca calls. Editing a holding's
shares/cost-basis/manual-entry flag is not yet exposed in this UI - use
db/real_holdings_repository.py::upsert_real_holding directly for now.
"""
import streamlit as st

from dashboard import components
from dashboard.data import get_real_holdings_view
from trading.real_holdings import portfolio_totals, subtotals_by_owner

ALL_OWNERS = "All owners"


def _fmt_money(value):
    return f"${value:,.2f}" if value is not None else "—"


def _fmt_pct(value):
    return f"{value:+.2f}%" if value is not None else "—"


def _display_owner(owner):
    return owner if owner else "Unassigned"


def _render_summary(views):
    totals = portfolio_totals(views)
    c1, c2, c3 = st.columns(3)
    c1.metric("Total market value", _fmt_money(totals["total_market_value"]))
    c2.metric("Total cost basis", _fmt_money(totals["total_cost_basis"]))
    c3.metric("Total unrealized P&L", _fmt_money(totals["total_unrealized_pl"]))

    c4, c5 = st.columns(2)
    c4.metric("Total realized gain", _fmt_money(totals["total_realized_gain"]))
    c5.metric("Positions needing manual entry", str(totals["positions_needing_manual_entry"]))


def _render_owner_breakdown(views):
    """Always shows every owner's subtotal, independent of the table's
    owner filter below - this is "the combined total, broken down", not
    itself filterable."""
    subtotals = subtotals_by_owner(views)
    if len(subtotals) <= 1:
        return  # nothing to break down - everything belongs to one owner (or none assigned)

    st.subheader("By owner")
    rows = [
        {
            "Owner": _display_owner(owner),
            "Market value": _fmt_money(t["total_market_value"]),
            "Cost basis": _fmt_money(t["total_cost_basis"]),
            "Unrealized P&L": _fmt_money(t["total_unrealized_pl"]),
            "Realized gain": _fmt_money(t["total_realized_gain"]) if t["total_realized_gain"] else "—",
            "Needs manual entry": str(t["positions_needing_manual_entry"]),
        }
        for owner, t in subtotals.items()
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_owner_filter(views):
    owners = sorted({v.owner or "" for v in views})
    if len(owners) <= 1:
        return views  # only one owner (or none assigned) - no filter needed

    options = [ALL_OWNERS] + [_display_owner(o) for o in owners]
    selected = st.selectbox("Filter by owner", options, key="real_holdings_owner_filter")
    if selected == ALL_OWNERS:
        return views
    return [v for v in views if _display_owner(v.owner) == selected]


def _render_table(views):
    st.subheader("Positions")
    rows = []
    for v in views:
        rows.append({
            "Ticker": v.ticker + (" ⚠️" if v.needs_manual_entry else ""),
            "Owner": _display_owner(v.owner),
            "Shares": f"{v.shares:g}" if v.shares is not None else "—",
            "Cost basis": _fmt_money(v.cost_basis_total),
            "Cost/share": _fmt_money(v.cost_basis_per_share),
            "Current price": _fmt_money(v.current_price),
            "As of": v.price_as_of or "—",
            "Market value": _fmt_money(v.market_value),
            "Unrealized P&L": (
                f"{_fmt_money(v.unrealized_pl)} ({_fmt_pct(v.unrealized_pl_pct)})"
                if v.unrealized_pl is not None else "—"
            ),
            "Realized gain": _fmt_money(v.realized_gain) if v.realized_gain else "—",
            "% of portfolio": f"{v.weight_pct:.1f}%" if v.weight_pct is not None else "—",
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

    manual_entry = [v for v in views if v.needs_manual_entry]
    if manual_entry:
        st.warning(
            "⚠️ Needs manual entry: " + ", ".join(v.ticker for v in manual_entry) +
            " — shares/cost basis not yet confirmed, so market value and gain/loss are not computed "
            "for these. Edit via db/real_holdings_repository.py::upsert_real_holding."
        )

    for v in views:
        if v.note:
            st.caption(f"**{v.ticker}** ({_display_owner(v.owner)}): {v.note}")


def render():
    st.title("Real Holdings")
    st.caption(
        "Actual held positions - not paper-trading simulations. Some of these belong to Tyler's mother. "
        "Priced against this system's own stored price history (same data every other page reads); "
        "cost basis and share counts are entered manually, not derived from any brokerage feed."
    )
    components.disclaimer("Portfolio tracking only - not a stock signal, not a trade recommendation.")

    views = get_real_holdings_view()
    if not views:
        components.empty_state(
            "No real holdings tracked yet",
            "Add one via db/real_holdings_repository.py::upsert_real_holding.",
            icon="💼",
        )
        return

    _render_summary(views)
    st.divider()
    _render_owner_breakdown(views)
    st.divider()
    filtered_views = _render_owner_filter(views)
    _render_table(filtered_views)
