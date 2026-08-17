"""Alert Activity: a single, read-only, chronological feed combining every
notification-history table in the system - price-threshold alerts,
volatility alerts, daily digests, operational-failure notifications, and
manual test sends. Purely a display page: it only ever calls
dashboard.data's get_alert_activity_feed(), which only ever SELECTs from
five existing tables (see dashboard/data.py::_load_alert_activity_feed) -
no write path is added here or anywhere reachable from this page.
"""
import streamlit as st

from dashboard import components
from dashboard.data import get_alert_activity_feed


def render():
    st.title("Alert Activity")
    st.caption(
        "A single, read-only feed of every alert-related notification this system has sent or attempted: "
        "price-threshold alerts, volatility alerts, daily digests, operational-failure notifications, and "
        "manual test sends. Purely a display of existing history - this page never evaluates a condition "
        "and never sends anything."
    )
    components.disclaimer("Signal-monitoring activity log only - not a stock signal, not a trade recommendation.")

    feed = get_alert_activity_feed()
    if feed.empty:
        components.empty_state(
            "No alert activity recorded yet",
            "This view reads from price_alerts, volatility_alerts, daily_digest_log, "
            "operational_notifications, and alert_test_log. None have any rows yet.",
            icon="📋",
        )
        return

    types_available = sorted(feed["type"].unique())
    min_date, max_date = feed["timestamp"].min().date(), feed["timestamp"].max().date()

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        selected_types = st.multiselect("Filter by type", types_available, default=types_available)
    with col2:
        start_date = st.date_input("From", value=min_date, min_value=min_date, max_value=max_date)
    with col3:
        end_date = st.date_input("To", value=max_date, min_value=min_date, max_value=max_date)

    filtered = feed[feed["type"].isin(selected_types)]
    filtered = filtered[
        (filtered["timestamp"].dt.date >= start_date) & (filtered["timestamp"].dt.date <= end_date)
    ]

    st.caption(f"{len(filtered)} of {len(feed)} total activity row(s) shown.")

    if filtered.empty:
        st.caption("No activity matches the current filters.")
        return

    display = filtered.copy()
    display["Timestamp"] = display["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    display["Ticker"] = display["ticker"].fillna("—")
    display = display.rename(columns={
        "type": "Type", "description": "Description", "email_status": "Email", "discord_status": "Discord",
    })
    columns = ["Timestamp", "Type", "Ticker", "Description", "Email", "Discord"]
    st.dataframe(display[columns], use_container_width=True, hide_index=True)
