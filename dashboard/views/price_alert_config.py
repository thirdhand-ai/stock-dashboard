"""Price Alert Thresholds: view/add/edit/remove the per-ticker above/below
thresholds the price-alert engine (alerts/price_engine.py) evaluates.
Previously a hardcoded PRICE_THRESHOLDS constant in alerts/price_config.py;
now stored in the price_alert_config DB table (db/price_alert_config_repository.py)
so a change here takes effect on the next automation run without a code
deploy - no restart, no redeploy.

Two modes, selected here: fixed dollar above/below (the original behavior),
or a percentage band around a baseline price (alerts/price_config.py's
resolve_percent_band resolves this to concrete above/below at save time -
this page never needs to know which mode produced them for display, except
to label the row and let the user edit the mode they originally chose).

This is the first dashboard view that mutates data outside of an explicit
"capture a snapshot" button (see dashboard/views/paper_portfolio.py's
capture-snapshot pattern, reused here) - it edits real automation config,
not just local research state. Every write is a deliberate, user-triggered
form submission; nothing here runs on page load.
"""
import streamlit as st

from alerts.price_config import MODE_FIXED, MODE_PERCENT
from dashboard import components
from dashboard.data import (
    add_or_update_price_alert_threshold,
    get_price_alert_thresholds,
    remove_price_alert_threshold,
)

FIXED_LABEL = "Fixed dollar amount"
PERCENT_LABEL = "Percentage band from baseline"


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
            "Type": (
                f"±{t.percent:.2f}% (baseline ${t.baseline_price:,.2f})"
                if t.mode == MODE_PERCENT and t.percent is not None and t.baseline_price is not None
                else "Fixed"
            ),
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
        "Choose Fixed dollar amount for literal above/below levels, or Percentage band to alert "
        "when price moves a given % away from a baseline price you specify."
    )
    mode_label = st.radio("Threshold type", [FIXED_LABEL, PERCENT_LABEL], horizontal=True, key="price_alert_mode_choice")

    with st.form("price_alert_config_add_edit_form", clear_on_submit=True):
        ticker = st.text_input("Ticker", max_chars=10, placeholder="e.g. AAPL").strip().upper()

        above = below = percent = baseline_price = None
        if mode_label == FIXED_LABEL:
            st.caption("Leave a field blank to skip that direction (e.g. only alert on a drop, not a rise).")
            col1, col2 = st.columns(2)
            with col1:
                above = st.number_input("Above (alert on upward crossing)", min_value=0.0, value=None, step=1.0, format="%.2f")
            with col2:
                below = st.number_input("Below (alert on downward crossing)", min_value=0.0, value=None, step=1.0, format="%.2f")
        else:
            st.caption("Alerts fire when price crosses either edge of the ±band around the baseline price.")
            col1, col2 = st.columns(2)
            with col1:
                baseline_price = st.number_input("Baseline price ($)", min_value=0.01, value=None, step=1.0, format="%.2f")
            with col2:
                percent = st.number_input("Band (±%)", min_value=0.1, value=None, step=0.5, format="%.2f")

        submitted = st.form_submit_button("Save threshold")

    if not submitted:
        return

    if not ticker:
        st.error("Ticker is required.")
        return

    if mode_label == FIXED_LABEL:
        if above is None and below is None:
            st.error("Set at least one of Above / Below.")
            return
        if above is not None and below is not None and below >= above:
            st.error("Below must be less than Above.")
            return
        add_or_update_price_alert_threshold(ticker, above=above, below=below, mode=MODE_FIXED)
    else:
        if baseline_price is None or percent is None:
            st.error("Set both Baseline price and Band %.")
            return
        add_or_update_price_alert_threshold(ticker, mode=MODE_PERCENT, percent=percent, baseline_price=baseline_price)

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
