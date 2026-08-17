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
