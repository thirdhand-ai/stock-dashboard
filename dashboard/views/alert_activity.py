"""Alert Activity: a single, read-only, chronological feed combining every
notification-history table in the system - price-threshold alerts,
volatility alerts, daily digests, operational-failure notifications, and
manual test sends. Purely a display page: it only ever calls
dashboard.data's get_alert_activity_feed(), which only ever SELECTs from
five existing tables (see dashboard/data.py::_load_alert_activity_feed) -
no write path is added here or anywhere reachable from this page.

Each row is also labeled with its holding type (Real Holdings / Watchlist /
Exploratory - see dashboard/holding_type.py) and filterable by it, so
real-money alerts stay easy to isolate from exploratory-ticker noise.
"""
import streamlit as st

from dashboard import components
from dashboard.data import get_alert_activity_feed, get_all_active_snoozes, get_holding_type_map
from dashboard.holding_type import HOLDING_TYPE_ORDER, holding_type_for, holding_type_label


def _render_currently_snoozed():
    st.subheader("Currently Snoozed")
    snoozes = get_all_active_snoozes()
    if snoozes.empty:
        st.caption("Nothing is currently snoozed.")
        return

    from datetime import datetime

    from alerts.snooze import format_snoozed_until_local

    local_tz = datetime.now().astimezone().tzinfo
    display = [
        {
            "Alert type": row["type"],
            "Ticker": row["ticker"],
            "Snoozed until (local)": format_snoozed_until_local(row["snoozed_until"], local_tz),
        }
        for _, row in snoozes.iterrows()
    ]
    st.dataframe(display, use_container_width=True, hide_index=True)
    st.caption(
        "Manage snoozes from the Price Alert Thresholds / Volatility Alert Thresholds pages. "
        "A snooze past its end time stops suppressing on the next evaluation automatically - no action needed."
    )


def render():
    st.title("Alert Activity")
    st.caption(
        "A single, read-only feed of every alert-related notification this system has sent or attempted: "
        "price-threshold alerts, volatility alerts, daily digests, operational-failure notifications, and "
        "manual test sends. Purely a display of existing history - this page never evaluates a condition "
        "and never sends anything."
    )
    components.disclaimer("Signal-monitoring activity log only - not a stock signal, not a trade recommendation.")

    _render_currently_snoozed()
    st.divider()

    feed = get_alert_activity_feed()
    if feed.empty:
        components.empty_state(
            "No alert activity recorded yet",
            "This view reads from price_alerts, volatility_alerts, daily_digest_log, "
            "operational_notifications, and alert_test_log. None have any rows yet.",
            icon="📋",
        )
        return

    type_map = get_holding_type_map()
    feed = feed.copy()
    feed["holding_type"] = feed["ticker"].apply(lambda t: holding_type_for(t, type_map))

    types_available = sorted(feed["type"].unique())
    holding_types_available = [t for t in HOLDING_TYPE_ORDER if t in set(feed["holding_type"].dropna())]
    min_date, max_date = feed["timestamp"].min().date(), feed["timestamp"].max().date()

    col1, col2, col3, col4 = st.columns([2, 2, 1, 1])
    with col1:
        selected_types = st.multiselect("Filter by type", types_available, default=types_available)
    with col2:
        selected_holding_types = st.multiselect(
            "Filter by holding type",
            holding_types_available,
            default=holding_types_available,
            format_func=holding_type_label,
            help="🏦 Real Holdings = actually owned - the alerts that matter for real money. "
                 "👁️ Watchlist / 🧪 Exploratory = no ownership. Rows with no associated ticker "
                 "(Daily Digest, Operational Failure, test sends) are always shown.",
        )
    with col3:
        start_date = st.date_input("From", value=min_date, min_value=min_date, max_value=max_date)
    with col4:
        end_date = st.date_input("To", value=max_date, min_value=min_date, max_value=max_date)

    filtered = feed[feed["type"].isin(selected_types)]
    filtered = filtered[filtered["holding_type"].isin(selected_holding_types) | filtered["holding_type"].isna()]
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
    display["Holding Type"] = display["holding_type"].map(holding_type_label)
    display = display.rename(columns={
        "type": "Type", "description": "Description", "email_status": "Email", "discord_status": "Discord",
    })
    columns = ["Timestamp", "Type", "Ticker", "Holding Type", "Description", "Email", "Discord"]
    st.dataframe(display[columns], use_container_width=True, hide_index=True)
