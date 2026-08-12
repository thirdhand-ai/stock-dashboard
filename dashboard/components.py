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
