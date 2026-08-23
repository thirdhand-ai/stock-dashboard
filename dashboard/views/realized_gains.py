"""Realized Gains: per-transaction sale/partial-sale detail for real
holdings - proceeds, cost basis of shares sold, realized gain/loss, and
short/long-term classification, broken down by owner and tax year, with
a CSV export for handing to an accountant. See trading/realized_gains.py's
docstring: a transaction missing a purchase or sale date shows "Unknown"
term/tax-year rather than a guessed classification - checked what's
actually stored for the existing META sale before building this (neither
real_holdings nor its free-text note names a date).

Read-only analytics, one write path: the entry form below and its
matching delete control. Never touches real_holdings itself.
"""
import pandas as pd
import streamlit as st

from dashboard import components
from dashboard.data import (
    add_realized_sale_entry,
    delete_realized_sale_entry,
    get_realized_gains_report,
    get_real_holdings_view,
)
from trading.realized_gains import TERM_UNKNOWN, to_csv_rows, totals_by_owner, totals_by_tax_year

ALL_OWNERS = "All owners"


def _fmt_money(value):
    return f"${value:,.2f}" if value is not None else "—"


def _display_owner(owner):
    return owner if owner else "Unassigned"


def _render_entry_form(holdings):
    st.subheader("Record a sale")
    if not holdings:
        st.caption("No real holdings tracked yet - add one on the Real Holdings page first.")
        return

    holding_labels = {f"{h.ticker} — {_display_owner(h.owner)}": h for h in holdings}

    with st.form("realized_sale_entry_form", clear_on_submit=True):
        label = st.selectbox("Holding", list(holding_labels.keys()))

        col1, col2 = st.columns(2)
        with col1:
            shares_sold = st.number_input("Shares sold", min_value=0.0, value=None, step=1.0, format="%.4f")
        with col2:
            cost_basis_sold = st.number_input("Cost basis of shares sold ($)", min_value=0.0, value=None, step=0.01, format="%.2f")

        proceeds = st.number_input("Proceeds ($)", min_value=0.0, value=None, step=0.01, format="%.2f")

        st.caption("Leave a date unknown rather than guessing - it will show as \"Unknown\" and skip short/long-term classification for that field.")
        col3, col4 = st.columns(2)
        with col3:
            purchase_date_unknown = st.checkbox("Purchase date unknown", value=True)
            purchase_date = st.date_input("Purchase date", disabled=purchase_date_unknown)
        with col4:
            sale_date_unknown = st.checkbox("Sale date unknown", value=True)
            sale_date = st.date_input("Sale date", disabled=sale_date_unknown)

        note = st.text_input("Note (optional)")
        submitted = st.form_submit_button("Add sale")

    if not submitted:
        return

    if shares_sold is None or cost_basis_sold is None or proceeds is None:
        st.error("Shares sold, cost basis of shares sold, and proceeds are all required.")
        return

    holding = holding_labels[label]
    add_realized_sale_entry(
        ticker=holding.ticker, owner=holding.owner,
        purchase_date=None if purchase_date_unknown else purchase_date.isoformat(),
        sale_date=None if sale_date_unknown else sale_date.isoformat(),
        shares_sold=shares_sold, cost_basis_sold=cost_basis_sold, proceeds=proceeds, note=note or None,
    )
    st.success(f"Recorded sale of {shares_sold:g} sh {holding.ticker} ({_display_owner(holding.owner)}).")
    st.rerun()


def _render_owner_filter(rows):
    owners = sorted({r.owner or "" for r in rows})
    if len(owners) <= 1:
        return rows

    options = [ALL_OWNERS] + [_display_owner(o) for o in owners]
    selected = st.selectbox("Filter by owner", options, key="realized_gains_owner_filter")
    if selected == ALL_OWNERS:
        return rows
    return [r for r in rows if _display_owner(r.owner) == selected]


def _render_transactions_table(rows):
    st.subheader("Transactions")
    table_rows = [
        {
            "Ticker": r.ticker,
            "Owner": _display_owner(r.owner),
            "Purchase date": r.purchase_date or "Unknown",
            "Sale date": r.sale_date or "Unknown",
            "Shares sold": f"{r.shares_sold:g}",
            "Cost basis of shares sold": _fmt_money(r.cost_basis_sold),
            "Proceeds": _fmt_money(r.proceeds),
            "Realized gain/loss": _fmt_money(r.realized_gain),
            "Term": r.term,
            "Tax year": str(r.tax_year) if r.tax_year is not None else "Unknown",
        }
        for r in rows
    ]
    st.dataframe(table_rows, use_container_width=True, hide_index=True)

    unknown_term = [r for r in rows if r.term == TERM_UNKNOWN]
    if unknown_term:
        st.caption(
            "Short/long-term unknown for: " + ", ".join(f"{r.ticker} ({_display_owner(r.owner)})" for r in unknown_term) +
            " - purchase and/or sale date not tracked precisely enough. Edit via the form above once known."
        )


def _render_breakdown(title, totals_dict, key_label):
    st.subheader(title)
    rows = [
        {
            key_label: key,
            "Transactions": t["transaction_count"],
            "Total proceeds": _fmt_money(t["total_proceeds"]),
            "Total cost basis sold": _fmt_money(t["total_cost_basis_sold"]),
            "Total realized gain/loss": _fmt_money(t["total_realized_gain"]),
        }
        for key, t in totals_dict.items()
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_csv_export(rows):
    csv_rows = to_csv_rows(rows)
    csv_bytes = pd.DataFrame(csv_rows).to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV (for accountant / tax filing)",
        data=csv_bytes,
        file_name="realized_gains.csv",
        mime="text/csv",
    )


def _render_transaction_log():
    st.subheader("Delete a transaction")
    rows = get_realized_gains_report()
    if not rows:
        return

    for r in rows:
        cols = st.columns([2, 2, 2, 2, 2, 1])
        cols[0].write(r.ticker)
        cols[1].write(_display_owner(r.owner))
        cols[2].write(r.sale_date or "Unknown sale date")
        cols[3].write(f"{r.shares_sold:g} sh")
        cols[4].write(_fmt_money(r.realized_gain))
        if cols[5].button("🗑️", key=f"delete_sale_{r.id}", help="Delete this transaction"):
            delete_realized_sale_entry(r.id)
            st.rerun()


def render():
    st.title("Realized Gains")
    st.caption(
        "Per-transaction sale/partial-sale detail for real holdings - proceeds, cost basis of shares "
        "sold, realized gain/loss, and short/long-term classification based on holding period. Broken "
        "down by owner and by tax year, with a CSV export for handing to an accountant or tax filing. "
        "A transaction missing a purchase or sale date shows \"Unknown\" rather than a guessed date or "
        "classification."
    )
    components.disclaimer("Realized gain/loss reporting only - not tax advice. Confirm figures with a tax professional.")

    holdings = get_real_holdings_view()
    _render_entry_form(holdings)
    st.divider()

    rows = get_realized_gains_report()
    if not rows:
        components.empty_state(
            "No realized sales recorded yet",
            "Use the form above to record your first sale/partial-sale.",
            icon="🧾",
        )
        return

    _render_csv_export(rows)
    st.divider()
    _render_breakdown("By owner", totals_by_owner(rows), "Owner")
    st.divider()
    _render_breakdown("By tax year", totals_by_tax_year(rows), "Tax year")
    st.divider()
    filtered_rows = _render_owner_filter(rows)
    _render_transactions_table(filtered_rows)
    st.divider()
    _render_transaction_log()
