"""Price Alert Thresholds: view/add/edit/remove the per-ticker above/below
thresholds the price-alert engine (alerts/price_engine.py) evaluates.
Previously a hardcoded PRICE_THRESHOLDS constant in alerts/price_config.py;
now stored in the price_alert_config DB table (db/price_alert_config_repository.py)
so a change here takes effect on the next automation run without a code
deploy - no restart, no redeploy.

This is the first dashboard view that mutates data outside of an explicit
"capture a snapshot" button (see dashboard/views/paper_portfolio.py's
capture-snapshot pattern, reused here) - it edits real automation config,
not just local research state. Every write is a deliberate, user-triggered
form submission; nothing here runs on page load.
"""
import streamlit as st

from dashboard import components
from dashboard.data import (
    add_or_update_price_alert_threshold,
    get_price_alert_thresholds,
    remove_price_alert_threshold,
)


def _render_current_thresholds(thresholds):
    st.subheader("Current thresholds")
    if not thresholds:
        components.empty_state(
            "No price-alert thresholds configured",
            "Add one below. A ticker with no threshold here is never evaluated for a price alert.",
            icon="🔔",
        )
        return

    rows = [
        {
            "Ticker": t.ticker,
            "Above": f"${t.above:,.2f}" if t.above is not None else "—",
            "Below": f"${t.below:,.2f}" if t.below is not None else "—",
        }
        for t in thresholds
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_add_edit_form():
    st.subheader("Add or update a threshold")
    st.caption(
        "Enter an existing ticker to update its thresholds, or a new ticker to add one. "
        "Leave a field blank to skip that direction (e.g. only alert on a drop, not a rise)."
    )

    with st.form("price_alert_config_add_edit_form", clear_on_submit=True):
        ticker = st.text_input("Ticker", max_chars=10, placeholder="e.g. AAPL").strip().upper()
        col1, col2 = st.columns(2)
        with col1:
            above = st.number_input("Above (alert on upward crossing)", min_value=0.0, value=None, step=1.0, format="%.2f")
        with col2:
            below = st.number_input("Below (alert on downward crossing)", min_value=0.0, value=None, step=1.0, format="%.2f")
        submitted = st.form_submit_button("Save threshold")

    if not submitted:
        return

    if not ticker:
        st.error("Ticker is required.")
        return
    if above is None and below is None:
        st.error("Set at least one of Above / Below.")
        return
    if above is not None and below is not None and below >= above:
        st.error("Below must be less than Above.")
        return

    add_or_update_price_alert_threshold(ticker, above=above, below=below)
    st.success(f"Saved threshold for {ticker}.")
    st.rerun()


def _render_remove_form(thresholds):
    if not thresholds:
        return
    st.subheader("Remove a threshold")
    st.caption("Removing a threshold stops future evaluation for that ticker - it does not delete its alert history.")
    with st.form("price_alert_config_remove_form"):
        ticker_to_remove = st.selectbox("Ticker", [t.ticker for t in thresholds])
        submitted = st.form_submit_button("Remove", type="secondary")

    if submitted:
        remove_price_alert_threshold(ticker_to_remove)
        st.success(f"Removed threshold for {ticker_to_remove}.")
        st.rerun()


def render():
    st.title("Price Alert Thresholds")
    st.caption(
        "Manage per-ticker price-crossing thresholds for the price-alert engine "
        "(`python -m alerts.run_price_alerts`, or the daily automation pipeline). "
        "Changes here take effect on the next run - no code deploy needed."
    )
    components.disclaimer("Signal-monitoring alert configuration only - not an executed trade, not financial advice.")

    thresholds = get_price_alert_thresholds()
    _render_current_thresholds(thresholds)

    st.divider()
    _render_add_edit_form()

    st.divider()
    _render_remove_form(thresholds)
