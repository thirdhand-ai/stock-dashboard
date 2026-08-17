"""Small reusable UI building blocks shared across dashboard views."""
import streamlit as st

from dashboard.theme import badge_html, score_status, stage_color, stage_label


def score_badge(score) -> str:
    if score is None:
        return badge_html("n/a", "#898781")
    label, color = score_status(score)
    return badge_html(f"{score:.0f}/100 &middot; {label}", color)


def stage_badge(stage) -> str:
    if stage is None:
        return badge_html("n/a", "#898781")
    return badge_html(stage_label(stage), stage_color(stage))


def unavailable_card(ticker: str, reason: str):
    st.markdown(
        f"""
        <div class="dash-card">
            <strong>{ticker}</strong>
            <div class="dash-muted">Data unavailable &mdash; {reason}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def disclaimer(text: str):
    st.markdown(f'<div class="dash-disclaimer">{text}</div>', unsafe_allow_html=True)


def empty_state(title: str, body: str, icon: str = "ℹ️"):
    st.info(f"**{title}**\n\n{body}", icon=icon)


def render_test_send_result(result):
    """Render an alerts/alert_test_notifications.py TestSendResult as a
    success/failure confirmation showing which channels delivered. Shared
    by every alert-config page's "Send Test Alert" button (Price Alert
    Thresholds, Volatility Alert Thresholds, Daily Digest)."""
    email_status = "delivered" if result.email.ok else f"failed ({result.email.error})"
    discord_status = "delivered" if result.discord.ok else f"failed ({result.discord.error})"
    if result.any_ok:
        st.success(f"Test alert sent. Email: {email_status} | Discord: {discord_status}")
    else:
        st.error(f"Test alert failed on both channels. Email: {email_status} | Discord: {discord_status}")


def render_snooze_controls(tickers, active_snoozes_df, snooze_fn, unsnooze_fn, key_prefix: str):
    """Shared snooze/unsnooze UI - a table of currently-active snoozes plus
    a form to add a new one. Identical between dashboard/views/
    price_alert_config.py and volatility_alert_config.py (see
    db/alert_snooze_schema.py's docstring for why both alert types share
    one snooze mechanism) - only the tickers list and the snooze_fn/
    unsnooze_fn callables differ per page.

    snooze_fn(ticker, preset=None, custom_date=None, custom_time=None) and
    unsnooze_fn(snooze_id) are dashboard/data.py wrappers - this function
    has no DB access of its own, same separation every other view in this
    codebase keeps from dashboard/data.py.
    """
    from datetime import datetime

    from alerts.snooze import SNOOZE_PRESETS, format_snoozed_until_local

    CUSTOM_LABEL = "Custom date & time"

    st.subheader("Snooze")
    st.caption(
        "Temporarily mute email/Discord delivery for a ticker (or all tickers) until a chosen time. "
        "A snoozed ticker is still evaluated and logged as normal - only delivery is suppressed, and it "
        "shows as 'suppressed (snoozed ...)' in Alert History and Alert Activity rather than being "
        "silently dropped. Snoozes expire automatically - no need to come back and unsnooze."
    )

    local_tz = datetime.now().astimezone().tzinfo
    if active_snoozes_df.empty:
        st.caption("Nothing is currently snoozed.")
    else:
        display_rows, options = [], {}
        for _, row in active_snoozes_df.iterrows():
            scope = row["ticker"] if row["ticker"] else "All tickers"
            until_local = format_snoozed_until_local(row["snoozed_until"], local_tz)
            display_rows.append({"Scope": scope, "Snoozed until (local)": until_local})
            options[f"{scope} (until {until_local})"] = row["id"]
        st.dataframe(display_rows, use_container_width=True, hide_index=True)

        with st.form(f"{key_prefix}_unsnooze_form"):
            choice = st.selectbox("Unsnooze", list(options.keys()), key=f"{key_prefix}_unsnooze_choice")
            submitted = st.form_submit_button("Unsnooze now", type="secondary")
        if submitted:
            unsnooze_fn(options[choice])
            st.success("Unsnoozed.")
            st.rerun()

    scope_choice = st.selectbox("Ticker to snooze", ["All tickers"] + tickers, key=f"{key_prefix}_snooze_scope")
    duration_choice = st.radio(
        "Duration", list(SNOOZE_PRESETS.keys()) + [CUSTOM_LABEL], horizontal=True, key=f"{key_prefix}_snooze_duration",
    )

    with st.form(f"{key_prefix}_snooze_form", clear_on_submit=True):
        custom_date = custom_time = None
        if duration_choice == CUSTOM_LABEL:
            col1, col2 = st.columns(2)
            with col1:
                custom_date = st.date_input("Until date (local time)", key=f"{key_prefix}_snooze_custom_date")
            with col2:
                custom_time = st.time_input("Until time (local time)", key=f"{key_prefix}_snooze_custom_time")
        submitted = st.form_submit_button("Snooze")

    if not submitted:
        return

    ticker = None if scope_choice == "All tickers" else scope_choice
    try:
        if duration_choice == CUSTOM_LABEL:
            snooze_fn(ticker, custom_date=custom_date, custom_time=custom_time)
        else:
            snooze_fn(ticker, preset=duration_choice)
    except ValueError as e:
        st.error(str(e))
        return

    st.success(f"Snoozed {scope_choice}.")
    st.rerun()
