"""Volatility Alert Thresholds: view/add/edit/remove the per-ticker
day-over-day % move thresholds the volatility-alert engine
(alerts/volatility_engine.py) evaluates. Mirrors dashboard/views/
price_alert_config.py's structure - a ticker with a row here is "enabled"
for volatility alerts; adding/removing a row is the enable/disable toggle,
and editing threshold_percent is the reconfigure path. Distinct from Price
Alert Thresholds: a volatility threshold is a single percent (either
direction), compared against yesterday's close fresh every evaluation, not
a static above/below dollar level (see alerts/volatility_config.py's
docstring for why this is a separate concept, not a third PriceThreshold
mode).

Same "changes take effect on the next automation run, no code deploy"
contract as the price-alert config page - every write here is a deliberate,
user-triggered form submission, nothing runs on page load.
"""
import streamlit as st

from dashboard import components
from dashboard.data import (
    add_or_update_volatility_alert_threshold,
    get_volatility_alert_configs,
    remove_volatility_alert_threshold,
    send_volatility_alert_test_notification,
)


def _render_current_thresholds(configs):
    st.subheader("Currently enabled tickers")
    if not configs:
        components.empty_state(
            "No volatility alerts configured",
            "Add a ticker below. A ticker with no volatility threshold here is never evaluated for a "
            "day-over-day move alert.",
            icon="📉",
        )
        return

    rows = [{"Ticker": c.ticker, "Threshold": f"±{c.threshold_percent:.2f}% in a day"} for c in configs]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_add_edit_form():
    st.subheader("Enable or update a ticker's volatility alert")
    st.caption(
        "Enter an existing ticker to update its threshold, or a new ticker to enable volatility alerts for it. "
        "Fires when a ticker's close moves more than the given percentage from the prior trading day's close, "
        "in either direction."
    )
    with st.form("volatility_alert_config_add_edit_form", clear_on_submit=True):
        ticker = st.text_input("Ticker", max_chars=10, placeholder="e.g. AAPL").strip().upper()
        threshold_percent = st.number_input(
            "Day-over-day move threshold (±%)", min_value=0.1, value=None, step=0.5, format="%.2f",
        )
        submitted = st.form_submit_button("Save threshold")

    if not submitted:
        return

    if not ticker:
        st.error("Ticker is required.")
        return
    if threshold_percent is None:
        st.error("Set a threshold percentage.")
        return

    add_or_update_volatility_alert_threshold(ticker, threshold_percent=threshold_percent)
    st.success(f"Enabled volatility alerts for {ticker} at ±{threshold_percent:.2f}%.")
    st.rerun()


def _render_remove_form(configs):
    if not configs:
        return
    st.subheader("Disable a ticker's volatility alert")
    st.caption("Disabling stops future volatility evaluation for that ticker - it does not delete its alert history.")
    with st.form("volatility_alert_config_remove_form"):
        ticker_to_remove = st.selectbox("Ticker", [c.ticker for c in configs])
        submitted = st.form_submit_button("Disable", type="secondary")

    if submitted:
        remove_volatility_alert_threshold(ticker_to_remove)
        st.success(f"Disabled volatility alerts for {ticker_to_remove}.")
        st.rerun()


def _render_test_alert_button():
    st.subheader("Verify delivery")
    st.caption(
        "Sends a real, clearly [TEST]-labeled sample volatility alert over email + Discord using "
        "fixed placeholder data. Never touches a real threshold, volatility_alert_state, or "
        "crossing-detection logic - this only proves your SMTP/Discord config can deliver."
    )
    if st.button("Send Test Alert", key="volatility_alert_test_send_button"):
        with st.spinner("Sending test alert..."):
            result = send_volatility_alert_test_notification()
        components.render_test_send_result(result)


def render():
    st.title("Volatility Alert Thresholds")
    st.caption(
        "Manage per-ticker day-over-day % move thresholds for the volatility-alert engine "
        "(`python -m alerts.run_volatility_alerts`, or the daily automation pipeline). "
        "Distinct from Price Alert Thresholds - this fires on a large single-day move, not a crossing of a "
        "fixed price level. Changes here take effect on the next run - no code deploy needed."
    )
    components.disclaimer("Signal-monitoring alert configuration only - not an executed trade, not financial advice.")

    _render_test_alert_button()

    st.divider()
    configs = get_volatility_alert_configs()
    _render_current_thresholds(configs)

    st.divider()
    _render_add_edit_form()

    st.divider()
    _render_remove_form(configs)
