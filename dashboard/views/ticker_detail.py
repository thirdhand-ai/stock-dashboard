"""Ticker Detail: candlestick + indicator panels, and the full signal breakdown."""
import pandas as pd
import streamlit as st

from config.settings import WATCHLIST
from dashboard import components
from dashboard.charts import build_ticker_chart
from dashboard.data import get_ticker_detail

LAYER_LABELS = {"trend": "1 · Trend filter", "momentum": "2 · Momentum trigger", "volume": "3 · Volume confirmation"}


def _format_values(values: dict) -> str:
    return ", ".join(f"{k}={v:,.2f}" for k, v in values.items())


def render():
    st.title("Ticker Detail")

    if st.session_state.get("selected_ticker") not in WATCHLIST:
        st.session_state["selected_ticker"] = WATCHLIST[0]
    ticker = st.selectbox("Ticker", WATCHLIST, key="selected_ticker")

    detail = get_ticker_detail(ticker)
    if not detail.ok:
        components.empty_state(f"{ticker}: data unavailable", detail.reason, icon="⚠️")
        return

    overlay_col1, overlay_col2, _ = st.columns([1, 1, 3])
    show_ma50 = overlay_col1.checkbox("50-day MA", value=True)
    show_bbands = overlay_col2.checkbox("Bollinger Bands", value=True)

    fig = build_ticker_chart(detail.enriched_history, show_ma50=show_ma50, show_bbands=show_bbands)
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False})
    st.caption(f"Source: {detail.source} · {len(detail.enriched_history)} observations")

    st.divider()
    st.subheader("Current signal")

    if detail.score is None:
        st.warning("Latest indicator values are still within the warm-up window — no score available yet.")
        return

    score = detail.score

    m1, m2 = st.columns([1, 2])
    with m1:
        st.markdown(
            f"""
            <div class="dash-card">
                <div class="dash-muted">Raw composite score</div>
                <div style="font-size:2.2rem; font-weight:700;">{score.score:.0f}<span style="font-size:1.1rem; color:#898781;"> / 100</span></div>
                {components.score_badge(score.score)}
            </div>
            """,
            unsafe_allow_html=True,
        )
    with m2:
        st.markdown(
            f"""
            <div class="dash-card">
                <div class="dash-muted">Sequential confirmation state</div>
                <div style="margin:6px 0;">Highest confirmed stage: {components.stage_badge(score.highest_confirmed_stage)}</div>
                <div class="dash-muted" style="margin-top:8px;">
                    trend_confirmed = <strong>{score.trend_confirmed}</strong> &nbsp;·&nbsp;
                    momentum_confirmed = <strong>{score.momentum_confirmed}</strong> &nbsp;·&nbsp;
                    volume_confirmed = <strong>{score.volume_confirmed}</strong>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    st.caption(
        "The raw score counts every condition that fires, independent of the others. "
        "The confirmation state requires trend → momentum → volume to confirm *in order* — "
        "a ticker can score highly without ever reaching a late-stage confirmation, and the table below shows exactly why."
    )

    rows = []
    for c in score.conditions:
        rows.append({
            "Layer": LAYER_LABELS.get(c.layer, c.layer),
            "Condition": c.name.replace("_", " "),
            "Threshold": c.description,
            "Actual value(s)": _format_values(c.values),
            "Fired": "Yes" if c.fired else "No",
            "Points": f"{c.points_awarded:.0f} / {c.points_available:.0f}",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    components.disclaimer(
        "This score and confirmation state are research indicators computed from historical price data. "
        "They are not a recommendation to buy or sell any security, and past patterns are not a guarantee of future results."
    )
