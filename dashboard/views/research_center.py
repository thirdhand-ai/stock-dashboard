"""Research Center: Phase 8's experimental, RESEARCH-ONLY analysis layer.

Combines technical signal, fundamentals, news sentiment, market regime, and
relative strength into a "Research Score" that is deliberately separate
from the production `signal score`. This view never submits, modifies, or
cancels an order - it only reads trading.client's read-only position list
for display (has_open_position), the same read-only pattern
dashboard/views/paper_portfolio.py already uses. See research/*.py module
docstrings and tests/test_research.py's structural-isolation check for the
guarantee that this layer cannot reach order execution.
"""
import pandas as pd
import streamlit as st

from config.settings import WATCHLIST
from dashboard import components
from dashboard.data import (
    clear_all_caches,
    get_historical_quality,
    get_market_regime,
    get_research_ranking,
    get_ticker_fundamentals,
    get_ticker_relative_strength,
    get_ticker_sentiment,
)
from dashboard.theme import STATUS_CRITICAL, STATUS_GOOD, STATUS_WARNING, TEXT_MUTED, badge_html
from research.config import DEFAULT_REGIME_CONFIG, DEFAULT_SENTIMENT_CONFIG

REGIME_LABELS = {
    "bullish_trend": "Bullish trend",
    "neutral_mixed": "Neutral / mixed",
    "bearish_trend": "Bearish trend",
    "elevated_volatility_risk_off": "Elevated volatility (risk-off)",
}
REGIME_COLORS = {
    "bullish_trend": STATUS_GOOD,
    "neutral_mixed": STATUS_WARNING,
    "bearish_trend": STATUS_CRITICAL,
    "elevated_volatility_risk_off": STATUS_CRITICAL,
}
SENTIMENT_COLORS = {"positive": STATUS_GOOD, "neutral": TEXT_MUTED, "negative": STATUS_CRITICAL}

FUNDAMENTAL_DISPLAY_FIELDS = [
    ("Market cap", "market_cap_millions", "${:,.0f}M"),
    ("P/E (trailing)", "pe_trailing", "{:.1f}x"),
    ("P/E (forward)", "pe_forward", "{:.1f}x"),
    ("P/S", "price_to_sales", "{:.1f}x"),
    ("P/B", "price_to_book", "{:.1f}x"),
    ("EPS (TTM)", "eps_ttm", "${:.2f}"),
    ("EPS growth YoY", "eps_growth_ttm_yoy_pct", "{:+.1f}%"),
    ("Revenue growth YoY", "revenue_growth_ttm_yoy_pct", "{:+.1f}%"),
    ("Net margin", "net_profit_margin_pct", "{:.1f}%"),
    ("Operating margin", "operating_margin_pct", "{:.1f}%"),
    ("ROE", "roe_pct", "{:.1f}%"),
    ("Debt/Equity", "debt_to_equity", "{:.2f}"),
    ("Price/FCF", "price_to_fcf", "{:.1f}x"),
]


def _regime_badge(label):
    if not label:
        return badge_html("n/a", TEXT_MUTED)
    return badge_html(REGIME_LABELS.get(label, label), REGIME_COLORS.get(label, TEXT_MUTED))


def _render_market_overview():
    st.subheader("A. Market overview")
    regime = get_market_regime()
    if not regime.ok:
        components.empty_state("Market regime unavailable", regime.reason, icon="⚠️")
        return

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(f"**{regime.benchmark} regime**")
        st.markdown(_regime_badge(regime.label), unsafe_allow_html=True)
    c2.metric(f"{regime.benchmark} close", f"${regime.close:,.2f}")
    c3.metric(
        "Realized vol (ann.)",
        f"{regime.realized_vol_annualized * 100:.1f}%" if regime.realized_vol_annualized is not None else "n/a",
    )
    c4.metric(
        f"Drawdown ({DEFAULT_REGIME_CONFIG.drawdown_window_days}d)",
        f"{regime.drawdown_pct:.2f}%" if regime.drawdown_pct is not None else "n/a",
    )

    # Two "$..." amounts on one markdown line would otherwise be misread as
    # inline LaTeX math delimiters by Streamlit's markdown renderer.
    st.caption(f"50-day MA \\${regime.sma_short:,.2f} · 200-day MA \\${regime.sma_long:,.2f} · as of {regime.as_of_date}")
    if regime.secondary:
        st.caption(
            f"Secondary context — {regime.secondary.benchmark}: "
            f"{REGIME_LABELS.get(regime.secondary.label, regime.secondary.label)} (close ${regime.secondary.close:,.2f})"
        )

    components.disclaimer(
        "Regime labeling uses simple trailing indicators (moving averages, realized volatility, drawdown) with "
        "configurable thresholds (research/config.py) — descriptive context, not a trading signal. It never alters "
        "production trade execution."
    )


def _render_ranking():
    st.subheader("B. Stock ranking")
    st.caption(
        "Click a column header to sort. **Research Score is EXPERIMENTAL** and structurally separate from the "
        "production technical signal — it never affects paper-order eligibility. This answers \"which stocks have "
        "the strongest overall research setup,\" not \"which stock should be bought.\""
    )

    rows = get_research_ranking(WATCHLIST)
    table_rows = []
    for r in rows:
        table_rows.append({
            "Ticker": r.ticker,
            "Research Score": r.research_score.score if r.research_score.ok else None,
            "Technical Score": r.technical_score,
            "Stage": r.technical_stage or "n/a",
            "Fundamental": r.fundamental_score,
            "Sentiment": r.sentiment_score,
            "Rel. Strength %ile": round(r.relative_strength_percentile, 1) if r.relative_strength_percentile is not None else None,
            "Regime": REGIME_LABELS.get(r.regime_label, r.regime_label or "n/a"),
            "Open Position": "Yes" if r.has_open_position else "No",
        })
    df = pd.DataFrame(table_rows).sort_values("Research Score", ascending=False, na_position="last")
    st.dataframe(df, use_container_width=True, hide_index=True)


def _render_ticker_research():
    st.subheader("C. Ticker research")
    if st.session_state.get("research_selected_ticker") not in WATCHLIST:
        st.session_state["research_selected_ticker"] = WATCHLIST[0]
    ticker = st.selectbox("Ticker", WATCHLIST, key="research_selected_ticker")

    rows = {r.ticker: r for r in get_research_ranking(WATCHLIST)}
    row = rows.get(ticker)

    if row:
        if row.technical_ok:
            st.markdown(
                f"**Technical (production signal):** {components.score_badge(row.technical_score)} "
                f"{components.stage_badge(row.technical_stage)}",
                unsafe_allow_html=True,
            )
        else:
            st.caption(f"Technical signal unavailable — {row.technical_reason}")

        if row.research_score.ok:
            st.markdown(f"**Research Score (experimental): {row.research_score.score:.1f}/100**")
            weights_str = ", ".join(f"{k}: {v:.0f}%" for k, v in row.research_score.weights_applied_pct.items())
            components_str = ", ".join(f"{k}: {v:.1f}" for k, v in row.research_score.components.items())
            st.caption(f"Weights applied — {weights_str}")
            st.caption(f"Component scores (0-100) — {components_str}")
        else:
            st.caption(f"Research Score unavailable — {row.research_score.reason}")

    st.markdown("#### Fundamentals")
    fm = get_ticker_fundamentals(ticker)
    if not fm.ok:
        st.caption(f"No fundamentals data — {fm.reason}")
    else:
        cols = st.columns(4)
        for i, (label, field_name, fmt) in enumerate(FUNDAMENTAL_DISPLAY_FIELDS):
            value = fm.values.get(field_name)
            with cols[i % 4]:
                st.metric(label, fmt.format(value) if value is not None else "n/a")

        if fm.earnings_date:
            earnings_caption = f"Next earnings: {fm.earnings_date}"
            if fm.days_until_earnings is not None:
                earnings_caption += f" ({fm.days_until_earnings} days)"
            st.caption(earnings_caption)
        else:
            st.caption("Next earnings date: not available")
        st.caption(f"Analyst price target: unavailable — {fm.analyst_target_unavailable_reason}")
        st.caption(f"As of {fm.as_of}")

    st.markdown("#### Relative strength")
    rs = get_ticker_relative_strength(ticker)
    if not rs.ok:
        st.caption(f"Unavailable — {rs.reason}")
    else:
        caption = f"Sector: {rs.sector or 'n/a'}"
        if rs.sector_benchmark:
            caption += f" · sector proxy: {rs.sector_benchmark} (approximation, not a true sector ETF)"
        st.caption(caption)

        cols = st.columns(3)
        for i, window in enumerate(["1m", "3m", "6m"]):
            with cols[i]:
                value = rs.relative_strength_pct.get(window)
                st.metric(f"{window} vs SPY", f"{value:+.2f}%" if value is not None else "n/a")

        if rs.relative_strength_vs_sector_pct:
            st.caption("vs sector proxy: " + ", ".join(f"{w}: {v:+.2f}%" for w, v in rs.relative_strength_vs_sector_pct.items()))
        if rs.realized_vol_annualized is not None:
            st.caption(f"Realized vol (ann.): {rs.realized_vol_annualized * 100:.1f}% · Drawdown: {rs.drawdown_pct:.2f}%")

    st.markdown("#### News & sentiment")
    ssum = get_ticker_sentiment(ticker)
    if not ssum.ok:
        st.caption(f"No recent news — {ssum.reason}")
    else:
        st.caption(
            f"news_sentiment_score (experimental, local keyword heuristic — NOT financial analysis): "
            f"{ssum.news_sentiment_score:.0f}/100. {ssum.counts['positive']} positive · {ssum.counts['neutral']} "
            f"neutral · {ssum.counts['negative']} negative in the last {DEFAULT_SENTIMENT_CONFIG.lookback_days} days."
        )
        if ssum.major_positive_count or ssum.major_negative_count:
            st.caption(
                f"Major-news keyword flags: {ssum.major_positive_count} positive, {ssum.major_negative_count} negative"
            )
        for article in ssum.articles[:10]:
            flag = " 🚩" if (article.major_positive or article.major_negative) else ""
            st.markdown(
                f"{badge_html(article.label, SENTIMENT_COLORS[article.label])}{flag} **{article.headline}**  \n"
                f"<span class='dash-muted'>{article.source or 'unknown source'} · {article.published_at}</span>",
                unsafe_allow_html=True,
            )


def _render_historical_quality():
    st.subheader("D. Historical signal analysis")
    st.caption(
        "How the EXISTING production technical signal has historically behaved — measurement only. Thresholds are "
        "not changed based on these results. Forward returns use future prices only as the measured outcome, never "
        "as an input to the signal at the time it was computed."
    )

    if st.session_state.get("research_hist_ticker") not in WATCHLIST:
        st.session_state["research_hist_ticker"] = WATCHLIST[0]
    ticker = st.selectbox("Ticker", WATCHLIST, key="research_hist_ticker")

    data = get_historical_quality(ticker)
    if not data.ok:
        components.empty_state("Not enough history", data.reason, icon="⚠️")
        return

    st.markdown("**By score bucket**")
    bucket_df = data.score_bucket_summary.copy()
    bucket_df["sufficient_sample"] = bucket_df["sufficient_sample"].map({True: "yes", False: "no (thin sample)"})
    st.dataframe(bucket_df, use_container_width=True, hide_index=True)

    st.markdown("**By confirmation stage**")
    stage_df = data.stage_summary.copy()
    stage_df["sufficient_sample"] = stage_df["sufficient_sample"].map({True: "yes", False: "no (thin sample)"})
    st.dataframe(stage_df, use_container_width=True, hide_index=True)

    st.caption(f"{len(data.table)} total scored historical observations for {ticker}.")


def render():
    st.title("Research Center")
    st.caption(
        "Technical + fundamentals + news sentiment + market regime + relative strength + historical signal "
        "quality — an experimental research layer, structurally separate from the production trading signal."
    )
    components.disclaimer(
        "EXPERIMENTAL / RESEARCH-ONLY. The Research Score and every component on this page are not connected to "
        "trading.engine, alerts, or automation.run_daily, and do not change entry/exit eligibility. Nothing here "
        "is financial advice."
    )

    left, _ = st.columns([1, 5])
    with left:
        if st.button("↻ Refresh research data", help="Re-read the database and recompute research analytics"):
            clear_all_caches()
            st.rerun()

    _render_market_overview()
    st.divider()
    _render_ranking()
    st.divider()
    _render_ticker_research()
    st.divider()
    _render_historical_quality()
