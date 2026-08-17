"""Daily Digest: on/off toggle for the daily summary message covering
every tracked ticker's current price, day-over-day % change, and distance
to its configured price-threshold/volatility-alert levels. Same
dashboard-editable, "takes effect on the next run, no code deploy"
contract as dashboard/views/price_alert_config.py and dashboard/views/
volatility_alert_config.py - but a single global toggle, not a per-ticker
form, since the digest always covers every tracked ticker rather than a
configurable subset.

Distinct from the price/volatility alert systems: this never evaluates a
crossing or a large move. When on, it fires once per trading day
unconditionally (automation/run_daily.py, after the pipeline completes,
win or fail) - see alerts/daily_digest_engine.py's docstring.
"""
import streamlit as st

from dashboard import components
from dashboard.data import get_daily_digest_enabled, get_daily_digest_log, set_daily_digest_enabled


def _render_toggle(enabled: bool):
    st.subheader("Status")
    if enabled:
        st.success("Daily digest is ON — sent once per trading day after the automation pipeline completes.")
    else:
        st.info("Daily digest is OFF.")

    with st.form("daily_digest_toggle_form"):
        new_enabled = st.toggle("Send daily digest", value=enabled)
        submitted = st.form_submit_button("Save")

    if submitted and new_enabled != enabled:
        set_daily_digest_enabled(new_enabled)
        st.success("Daily digest turned " + ("ON." if new_enabled else "OFF."))
        st.rerun()


def _channel_status(delivered, error) -> str:
    if delivered:
        return "delivered"
    if error:
        return f"failed ({error})"
    return "pending"


def _digest_status_label(row) -> str:
    if row["dry_run"]:
        return "Dry-run (not sent)"
    email_status = _channel_status(row["delivered"], row["delivery_error"])
    discord_status = _channel_status(row["discord_delivered"], row["discord_delivery_error"])
    return f"Email: {email_status} | Discord: {discord_status}"


def _render_recent_digests():
    st.subheader("Recent digests")
    history = get_daily_digest_log(limit=10)
    if history.empty:
        st.caption("No digest has been sent yet.")
        return

    display = history.copy()
    display["Status"] = display.apply(_digest_status_label, axis=1)
    display = display.rename(columns={
        "trading_date": "Trading date", "sent_at": "Sent at", "ticker_count": "Tickers",
    })
    columns = ["Trading date", "Sent at", "Tickers", "Status"]
    st.dataframe(display[columns], use_container_width=True, hide_index=True)


def render():
    st.title("Daily Digest")
    st.caption(
        "Turn the daily ticker summary on or off - covers every tracked ticker's current price, "
        "day-over-day % change, and distance to its configured price-threshold/volatility-alert levels. "
        "Sent once per trading day, win or fail, regardless of whether either alert system fired "
        "(`python -m alerts.run_daily_digest`, or the daily automation pipeline)."
    )
    components.disclaimer("Daily summary only - not a stock signal, not a trade recommendation.")

    enabled = get_daily_digest_enabled()
    _render_toggle(enabled)

    st.divider()
    _render_recent_digests()
