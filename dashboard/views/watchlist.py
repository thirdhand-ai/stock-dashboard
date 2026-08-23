"""Watchlist overview: signal confidence + confirmation stage per ticker."""
import streamlit as st

from config.settings import SIGNAL_COVERAGE_TICKERS
from dashboard import components
from dashboard.data import clear_all_caches, get_watchlist_overview
from dashboard.theme import STATUS_CRITICAL, STATUS_GOOD, TEXT_MUTED
from db.run_history_repository import STATUS_FAILED, STATUS_PARTIAL_FAILURE

CARDS_PER_ROW = 3

def _render_available_card(row):
    change_color = STATUS_GOOD if (row.change or 0) >= 0 else STATUS_CRITICAL
    change_sign = "+" if (row.change or 0) >= 0 else ""
    change_text = (
        f'<span style="color:{change_color};">{change_sign}{row.change:.2f} '
        f'({change_sign}{row.change_pct:.2f}%)</span>'
        if row.change is not None
        else '<span class="dash-muted">change n/a (single day of history)</span>'
    )

    # A10: make it unmissable when the most recent scheduled run didn't
    # succeed - these signals are the last successfully STORED result, not
    # today's freshly refreshed one.
    staleness_html = (
        f'<div style="margin-top:4px; color:{STATUS_CRITICAL}; font-weight:600;">'
        f"⚠ Latest scheduled refresh {row.latest_run_status} — showing last stored data, not today's fresh analysis</div>"
        if row.is_stale else ""
    )
    fetched_text = f" &middot; fetched {row.fetched_at}" if row.fetched_at else ""

    st.markdown(
        f"""
        <div class="dash-card">
            <div style="display:flex; justify-content:space-between; align-items:baseline;">
                <span style="font-size:1.15rem; font-weight:700;">{row.ticker}</span>
                <span style="font-size:1.05rem;">${row.last_price:,.2f}</span>
            </div>
            <div style="margin:2px 0 10px 0;">{change_text}</div>
            <div style="margin-bottom:10px;">
                {components.score_badge(row.score)} &nbsp; {components.stage_badge(row.highest_confirmed_stage)}
            </div>
            <div class="dash-muted">
                RSI {row.rsi:.1f} &nbsp;&middot;&nbsp; ADX {row.adx:.1f} &nbsp;&middot;&nbsp; Vol {row.volume_ratio:.2f}x 20d avg
            </div>
            <div class="dash-muted" style="margin-top:4px;">As of {row.latest_date} &middot; {row.source}{fetched_text}</div>
            {staleness_html}
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button("View detail →", key=f"detail_{row.ticker}", use_container_width=True):
        st.session_state["selected_ticker"] = row.ticker
        from dashboard.pages_registry import PAGE_TICKER_DETAIL  # lazy: avoids circular import
        st.switch_page(PAGE_TICKER_DETAIL)


def render():
    st.title("Watchlist")
    st.caption(
        "Signal confidence and sequential confirmation state for your configured tickers. "
        "This is a research score, not a buy/sell recommendation."
    )

    left, _ = st.columns([1, 5])
    with left:
        if st.button("↻ Refresh data", help="Re-read the database and recompute indicators/scores"):
            clear_all_caches()
            st.rerun()

    rows = get_watchlist_overview(SIGNAL_COVERAGE_TICKERS)
    available = [r for r in rows if r.ok]
    unavailable = [r for r in rows if not r.ok]

    if rows and rows[0].is_stale:
        st.error(
            f"The most recent scheduled automation run status was **{rows[0].latest_run_status}** — "
            "market data could not be fully refreshed. Scores/stages below are the last successfully "
            "stored values, not today's fresh analysis. See Alert History for run details.",
            icon="⚠️",
        )

    if available:
        st.subheader(f"{len(available)} of {len(rows)} tickers with sufficient data")
        columns = st.columns(CARDS_PER_ROW)
        for i, row in enumerate(available):
            with columns[i % CARDS_PER_ROW]:
                _render_available_card(row)

    if unavailable:
        st.subheader("Insufficient data")
        st.caption("These tickers are configured in the watchlist but don't have enough stored history yet.")
        columns = st.columns(CARDS_PER_ROW)
        for i, row in enumerate(unavailable):
            with columns[i % CARDS_PER_ROW]:
                components.unavailable_card(row.ticker, row.reason)

    if not rows:
        components.empty_state("No tickers configured", "Add tickers to WATCHLIST in config/settings.py.")
