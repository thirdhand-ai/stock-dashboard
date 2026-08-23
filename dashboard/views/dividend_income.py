"""Dividend Income: manually-entered dividend payment history for real
holdings, trailing-12-month yield on cost per holding, and a running
total of dividend income received across the portfolio. Manual entry was
a deliberate choice - this dashboard has no existing data source for
dividend history (see db/dividend_payments_schema.py's docstring); an
automated source is a separate future decision, not built here.

Read-only analytics, one write path: the entry form below and its
matching delete control. Never touches real_holdings, prices, or any
other table.
"""
import streamlit as st

from dashboard import components
from dashboard.data import (
    add_dividend_payment_entry,
    delete_dividend_payment_entry,
    get_dividend_payments,
    get_dividend_summary,
    get_real_holdings_view,
)
from trading.dividends import dividend_totals_by_owner, portfolio_dividend_totals

ALL_OWNERS = "All owners"


def _fmt_money(value):
    return f"${value:,.2f}" if value is not None else "—"


def _fmt_pct(value):
    return f"{value:.2f}%" if value is not None else "—"


def _display_owner(owner):
    return owner if owner else "Unassigned"


def _render_entry_form(holdings):
    st.subheader("Record a dividend payment")
    if not holdings:
        st.caption("No real holdings tracked yet - add one on the Real Holdings page first.")
        return

    holding_labels = {f"{h.ticker} — {_display_owner(h.owner)}": h for h in holdings}

    with st.form("dividend_payment_entry_form", clear_on_submit=True):
        label = st.selectbox("Holding", list(holding_labels.keys()))
        col1, col2 = st.columns(2)
        with col1:
            pay_date = st.date_input("Payment date")
        with col2:
            amount_per_share = st.number_input("Amount per share ($)", min_value=0.0, value=None, step=0.01, format="%.4f")

        col3, col4 = st.columns(2)
        with col3:
            total_received = st.number_input("Total received ($)", min_value=0.0, value=None, step=0.01, format="%.2f")
        with col4:
            reinvested = st.checkbox("Reinvested via DRIP")

        note = st.text_input("Note (optional)")
        submitted = st.form_submit_button("Add payment")

    if not submitted:
        return

    if amount_per_share is None or total_received is None:
        st.error("Amount per share and total received are both required.")
        return

    holding = holding_labels[label]
    add_dividend_payment_entry(
        ticker=holding.ticker, owner=holding.owner, pay_date=pay_date.isoformat(),
        amount_per_share=amount_per_share, total_received=total_received,
        reinvested=reinvested, note=note or None,
    )
    st.success(f"Recorded {_fmt_money(total_received)} for {holding.ticker} ({_display_owner(holding.owner)}) on {pay_date.isoformat()}.")
    st.rerun()


def _render_aggregate(summaries):
    totals = portfolio_dividend_totals(summaries)
    c1, c2 = st.columns(2)
    c1.metric("Trailing 12-month dividend income", _fmt_money(totals["total_ttm_received"]))
    c2.metric("Total dividend income received (all-time)", _fmt_money(totals["total_all_time_received"]))


def _render_owner_breakdown(summaries):
    by_owner = dividend_totals_by_owner(summaries)
    if len(by_owner) <= 1:
        return

    st.subheader("By owner")
    rows = [
        {
            "Owner": _display_owner(owner),
            "TTM dividend income": _fmt_money(t["total_ttm_received"]),
            "All-time dividend income": _fmt_money(t["total_all_time_received"]),
        }
        for owner, t in by_owner.items()
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_owner_filter(summaries):
    owners = sorted({s.owner or "" for s in summaries})
    if len(owners) <= 1:
        return summaries

    options = [ALL_OWNERS] + [_display_owner(o) for o in owners]
    selected = st.selectbox("Filter by owner", options, key="dividend_owner_filter")
    if selected == ALL_OWNERS:
        return summaries
    return [s for s in summaries if _display_owner(s.owner) == selected]


def _render_per_holding_table(summaries):
    st.subheader("Per holding")
    rows = [
        {
            "Ticker": s.ticker,
            "Owner": _display_owner(s.owner),
            "TTM received": _fmt_money(s.ttm_received),
            "TTM yield on cost": _fmt_pct(s.ttm_yield_on_cost_pct),
            "All-time received": _fmt_money(s.all_time_received),
            "Payments": str(s.payment_count),
            "Last payment": s.last_payment_date or "—",
        }
        for s in summaries
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)

    no_cost_basis = [s for s in summaries if s.cost_basis_total is None]
    if no_cost_basis:
        st.caption(
            "Yield on cost is unavailable for " + ", ".join(s.ticker for s in no_cost_basis) +
            " - cost basis not yet confirmed for this holding."
        )


def _render_payment_log():
    st.subheader("Payment log")
    payments = get_dividend_payments()
    if not payments:
        return

    for p in reversed(payments):  # most-recent-first for the log view
        cols = st.columns([2, 2, 2, 2, 2, 1])
        cols[0].write(p.ticker)
        cols[1].write(_display_owner(p.owner))
        cols[2].write(p.pay_date)
        cols[3].write(_fmt_money(p.total_received))
        cols[4].write("DRIP" if p.reinvested else "Cash")
        if cols[5].button("🗑️", key=f"delete_dividend_{p.id}", help="Delete this payment"):
            delete_dividend_payment_entry(p.id)
            st.rerun()


def render():
    st.title("Dividend Income")
    st.caption(
        "Manually-entered dividend payment history for real holdings - date, amount per share, total "
        "received, and whether it was reinvested via DRIP. Trailing-12-month yield on cost is computed "
        "against each holding's known cost basis; a holding with unconfirmed cost basis shows no yield "
        "rather than a guessed figure."
    )
    components.disclaimer("Portfolio income tracking only - not a stock signal, not a trade recommendation.")

    holdings = get_real_holdings_view()
    _render_entry_form(holdings)
    st.divider()

    summaries = get_dividend_summary()
    if not summaries:
        components.empty_state(
            "No dividend payments recorded yet",
            "Use the form above to record your first payment.",
            icon="💰",
        )
        return

    _render_aggregate(summaries)
    st.divider()
    _render_owner_breakdown(summaries)
    st.divider()
    filtered_summaries = _render_owner_filter(summaries)
    _render_per_holding_table(filtered_summaries)
    st.divider()
    _render_payment_log()
