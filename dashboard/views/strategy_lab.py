"""Strategy Lab: Phase 9 large-sample research findings - HISTORICAL
RESEARCH ONLY, not a live signal or a guarantee of future returns.

Strictly read-only and disconnected from execution: this page only reads a
precomputed cache (data/research_cache/phase9_results.pkl, written by
`python -m strategy_lab.run_study`) via dashboard/data.py's
get_strategy_lab_results(). It never recomputes the study on page load,
never calls Alpaca, never touches paper_orders/alert_state, and nothing on
this page can submit, modify, or cancel an order - see
tests/test_strategy_lab_safety.py.
"""
import pandas as pd
import streamlit as st

from dashboard import components
from dashboard.charts import build_bar_chart, build_drawdown_chart, build_multi_line_chart
from dashboard.data import (
    get_amzn_monitor_status,
    get_experiment_registry,
    get_experiment_registry_drift,
    get_phase10_results,
    get_phase11_results,
    get_production_health,
    get_prospective_evidence_status,
    get_prospective_summary,
    get_research_run_history,
    get_strategy_lab_results,
)
from dashboard.theme import SLOT_AQUA, SLOT_BLUE, SLOT_ORANGE, SLOT_VIOLET, STATUS_CRITICAL, STATUS_GOOD

HORIZON_OPTIONS = [1, 5, 20, 60]
VARIANT_LABELS = {
    "original_frozen_strategy": "CONTROL (original)",
    "bullish_entry_only": "Experiment A (bullish entry only)",
    "bullish_entry_and_exit": "Experiment B (bullish entry + exit)",
}


def _horizon_picker(key: str) -> int:
    return st.select_slider("Forward-return horizon (trading days)", options=HORIZON_OPTIONS, value=20, key=key)


def _render_header(results: dict):
    st.title("🧪 Strategy Lab")
    components.disclaimer(
        "This page shows HISTORICAL RESEARCH results only - a large-sample backtest of the existing production "
        "technical signal across ~90 liquid large-cap stocks over ~5 years. It is not a live signal, not connected "
        "to paper or live trading, and past patterns are not a guarantee or prediction of future returns. "
        "See the Paper Portfolio page for actual account state."
    )
    generated_at = results.get("generated_at", "unknown")
    st.caption(f"Research snapshot generated: {generated_at}. Refresh with `python -m strategy_lab.run_study`.")


def _render_coverage(results: dict):
    cov = results.get("coverage", {})
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Research universe", f"{cov.get('n_tickers', 0)} tickers")
    date_range = cov.get("date_range")
    c2.metric("Date range", f"{date_range[0]} → {date_range[1]}" if date_range else "n/a")
    c3.metric("Ticker-day observations", f"{cov.get('total_observations', 0):,}")
    c4.metric("Failed to load", f"{cov.get('n_failed', 0)}")

    with st.expander("Frozen production strategy definition (unchanged by this research)"):
        snap = results.get("strategy_snapshot", {})
        st.json(snap)


def _render_bucket_returns(results: dict):
    st.subheader("Forward return by signal-score bucket")
    st.caption(
        "Does a higher composite technical score correspond to a better subsequent return? Daily observations, "
        "no look-ahead (score at date t only uses data through t; forward return is the outcome being measured)."
    )
    horizon = _horizon_picker("bucket_horizon")
    df = results.get("bucket_scores")
    if df is None or df.empty:
        components.empty_state("No bucket data", "Run `python -m strategy_lab.run_study` to generate the research cache.")
        return

    sub = df[df["horizon_days"] == horizon]
    col1, col2 = st.columns(2)
    with col1:
        st.plotly_chart(build_bar_chart(sub["bucket"], sub["mean_return_pct"], y_title="Mean forward return (%)", color=SLOT_BLUE),
                         use_container_width=True, config={"displaylogo": False})
    with col2:
        st.plotly_chart(build_bar_chart(sub["bucket"], sub["n"], y_title="Sample size (n)", color=SLOT_VIOLET),
                         use_container_width=True, config={"displaylogo": False})
    st.dataframe(sub[["bucket", "n", "mean_return_pct", "median_return_pct", "std_pct", "pct_positive",
                       "ci95_low_pct", "ci95_high_pct", "excess_mean_return_pct", "overlapping_observations"]],
                 use_container_width=True, hide_index=True)
    if horizon > 1:
        st.caption(
            f"⚠️ Horizon={horizon}d forward returns are computed from overlapping windows, so consecutive "
            "observations are not independent - the standard errors/CIs above are naive and understate true "
            "uncertainty. Treat as descriptive, not a rigorous hypothesis test."
        )


def _render_stage_returns(results: dict):
    st.subheader("Forward return by confirmation stage")
    st.caption("Does advancing further through the trend → momentum → volume confirmation chain improve outcomes?")
    horizon = _horizon_picker("stage_horizon")
    df = results.get("bucket_stages")
    if df is None or df.empty:
        components.empty_state("No stage data", "Run the study to populate this section.")
        return
    sub = df[df["horizon_days"] == horizon]
    order = ["none", "trend", "momentum", "volume"]
    sub = sub.set_index("stage").reindex(order).reset_index()
    st.plotly_chart(build_bar_chart(sub["stage"], sub["mean_return_pct"], y_title="Mean forward return (%)", color=SLOT_AQUA),
                     use_container_width=True, config={"displaylogo": False})
    st.dataframe(sub[["stage", "n", "mean_return_pct", "median_return_pct", "pct_positive", "excess_mean_return_pct"]],
                 use_container_width=True, hide_index=True)


def _render_events(results: dict):
    st.subheader("Signal-event analysis (transitions, not daily observations)")
    st.caption(
        "Approximates how the alert/trading engine actually fires: once on a crossing/advancement, not on every "
        "day a threshold happens to still be met. Entry is simulated at the NEXT session's open (never same-close)."
    )
    ev = results.get("event_returns")
    if ev is None or ev.empty:
        components.empty_state("No event data", "Run the study to populate this section.")
        return

    counts = ev["event_type"].value_counts()
    c1, c2, c3 = st.columns(3)
    c1.metric("score_cross_70 events", int(counts.get("score_cross_70", 0)))
    c2.metric("momentum_advance events", int(counts.get("momentum_advance", 0)))
    c3.metric("volume_advance events", int(counts.get("volume_advance", 0)))

    horizon = _horizon_picker("event_horizon")
    idealized_col, reasonable_col = f"net_return_idealized_{horizon}d", f"net_return_reasonable_{horizon}d"
    summary = ev.groupby("event_type")[[idealized_col, reasonable_col]].mean().reset_index()
    summary = summary.rename(columns={idealized_col: "idealized_mean_return_pct", reasonable_col: "reasonable_friction_mean_return_pct"})
    for c in ("idealized_mean_return_pct", "reasonable_friction_mean_return_pct"):
        summary[c] = summary[c] * 100

    st.markdown("**Idealized (no friction) vs reasonable friction (5bps slippage + 0.10% commission, next-session entry)**")
    melted = summary.melt(id_vars="event_type", value_vars=["idealized_mean_return_pct", "reasonable_friction_mean_return_pct"],
                           var_name="assumption", value_name="mean_return_pct")
    import plotly.graph_objects as go
    from dashboard.theme import GRIDLINE, SURFACE, TEXT_SECONDARY
    fig = go.Figure()
    for assumption, color in [("idealized_mean_return_pct", SLOT_BLUE), ("reasonable_friction_mean_return_pct", SLOT_ORANGE)]:
        sub = melted[melted["assumption"] == assumption]
        fig.add_trace(go.Bar(x=sub["event_type"], y=sub["mean_return_pct"], name=assumption.replace("_mean_return_pct", ""), marker_color=color))
    fig.update_layout(barmode="group", paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
                       font=dict(color=TEXT_SECONDARY), yaxis_title="Mean return (%)", height=340,
                       legend=dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=1.1))
    fig.update_xaxes(gridcolor=GRIDLINE)
    fig.update_yaxes(gridcolor=GRIDLINE)
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False})
    st.dataframe(summary, use_container_width=True, hide_index=True)


def _render_strategy_vs_benchmark(results: dict):
    st.subheader("Strategy vs. SPY / equal-weight buy-and-hold")
    st.caption(
        "Equal-weight combination of per-ticker whole-history backtests using the exact frozen production "
        "entry/exit rules (Backtesting.py), indexed to 100 at the start of the sample."
    )
    series = {}
    if results.get("spy_equity_curve") is not None:
        s = results["spy_equity_curve"].copy(); s.index = pd.to_datetime(s.index); series["SPY buy & hold"] = s
    if results.get("equal_weight_equity_curve") is not None:
        s = results["equal_weight_equity_curve"].copy(); s.index = pd.to_datetime(s.index); series["Equal-weight universe buy & hold"] = s
    if results.get("strategy_curve_reasonable") is not None and not results["strategy_curve_reasonable"].empty:
        s = results["strategy_curve_reasonable"] * 100.0; s.index = pd.to_datetime(s.index); series["Strategy (reasonable friction)"] = s
    if results.get("strategy_curve_idealized") is not None and not results["strategy_curve_idealized"].empty:
        s = results["strategy_curve_idealized"] * 100.0; s.index = pd.to_datetime(s.index); series["Strategy (idealized, no friction)"] = s

    if not series:
        components.empty_state("No benchmark data", "Run the study to populate this section.")
        return

    st.plotly_chart(build_multi_line_chart(series, y_title="Indexed to 100 at start"), use_container_width=True, config={"displaylogo": False})

    spy_stats, ew_stats = results.get("spy_stats"), results.get("equal_weight_stats")
    rows = []
    for label, s in (("SPY buy-and-hold", spy_stats), ("Equal-weight universe buy-and-hold", ew_stats)):
        if s is not None:
            rows.append({"label": s.label, "cumulative_return_pct": s.cumulative_return_pct, "annualized_return_pct": s.annualized_return_pct,
                          "annualized_vol_pct": s.annualized_vol_pct, "sharpe_rf0": s.sharpe_ratio_rf0, "max_drawdown_pct": s.max_drawdown_pct})
    pt_r = results.get("per_ticker_reasonable")
    pt_i = results.get("per_ticker_idealized")
    if pt_r is not None and not pt_r.empty:
        rows.append({"label": "Strategy equal-weight (reasonable friction)", "cumulative_return_pct": round((series.get("Strategy (reasonable friction)", pd.Series([100,100])).iloc[-1] / 100 - 1) * 100, 2),
                      "annualized_return_pct": None, "annualized_vol_pct": None, "sharpe_rf0": round(float(pt_r["sharpe_ratio"].mean()), 3), "max_drawdown_pct": round(float(pt_r["max_drawdown_pct"].median()), 2)})
    if pt_i is not None and not pt_i.empty:
        rows.append({"label": "Strategy equal-weight (idealized)", "cumulative_return_pct": round((series.get("Strategy (idealized, no friction)", pd.Series([100,100])).iloc[-1] / 100 - 1) * 100, 2),
                      "annualized_return_pct": None, "annualized_vol_pct": None, "sharpe_rf0": round(float(pt_i["sharpe_ratio"].mean()), 3), "max_drawdown_pct": round(float(pt_i["max_drawdown_pct"].median()), 2)})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    if "Strategy (reasonable friction)" in series:
        st.plotly_chart(build_drawdown_chart(series["Strategy (reasonable friction)"] / 100.0), use_container_width=True, config={"displaylogo": False})

    if pt_r is not None and not pt_r.empty:
        pct_beat = (pt_r["excess_vs_own_buy_hold_pct"] > 0).mean() * 100
        st.caption(f"Only {pct_beat:.1f}% of individual tickers had the strategy beat that ticker's own buy-and-hold over the sample period.")


def _render_walk_forward(results: dict):
    st.subheader("Walk-forward / out-of-sample performance by year")
    st.caption("Chronological, non-overlapping train(252d)/test(63d) windows across the universe - the frozen rules are never fit on train data.")
    wf = results.get("wf_summary", {})
    if wf.get("n_windows_total", 0) == 0:
        components.empty_state("No walk-forward data", "Run the study to populate this section.")
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total OOS windows", wf.get("n_windows_total"))
    c2.metric("Mean window return", f"{wf.get('mean_window_return_pct')}%")
    c3.metric("% windows profitable", f"{wf.get('pct_windows_profitable')}%")
    c4.metric("Pooled trade win rate", f"{wf.get('pooled_trade_win_rate_pct')}%")

    by_year = results.get("wf_by_year")
    if by_year is not None and not by_year.empty:
        st.plotly_chart(build_bar_chart(by_year["year"], by_year["mean_return_pct"], y_title="Mean OOS window return (%)", color="pnl"),
                         use_container_width=True, config={"displaylogo": False})
        st.dataframe(by_year, use_container_width=True, hide_index=True)


def _render_regime(results: dict):
    st.subheader("Performance by market regime")
    st.caption("Regime classified from trailing SPY price/vol/drawdown (same framework as the Research Center page).")
    regime_results = results.get("regime_bucket_results", {})
    if not regime_results:
        components.empty_state("No regime data", "Run the study to populate this section.")
        return
    horizon = _horizon_picker("regime_horizon")
    order = ["bullish_trend", "neutral_mixed", "bearish_trend", "elevated_volatility_risk_off"]
    rows = []
    for label in order:
        df = regime_results.get(label)
        if df is None:
            continue
        sub = df[(df["horizon_days"] == horizon) & (df["bucket"] == "70-84")]
        if not sub.empty:
            rows.append({"regime": label, "n": int(sub["n"].iloc[0]), "mean_return_pct": sub["mean_return_pct"].iloc[0]})
    if not rows:
        st.caption("Insufficient sample in the 70-84 score bucket for one or more regimes at this horizon.")
        return
    rdf = pd.DataFrame(rows)
    st.caption("Shown for the 70-84 score bucket (the production entry-qualifying range).")
    st.plotly_chart(build_bar_chart(rdf["regime"], rdf["mean_return_pct"], y_title="Mean forward return (%)", color="pnl"),
                     use_container_width=True, config={"displaylogo": False})
    st.dataframe(rdf, use_container_width=True, hide_index=True)


def _render_robustness(results: dict):
    st.subheader("Cross-sectional (ticker-level) robustness")
    st.caption("Is apparent performance broad-based across the universe, or concentrated in a few tickers?")
    rr = results.get("robustness_results", {})
    bucket_key = st.selectbox("Score bucket", options=list(rr.keys()), index=0 if rr else None)
    if not bucket_key:
        components.empty_state("No robustness data", "Run the study to populate this section.")
        return
    r = rr[bucket_key]
    c1, c2, c3 = st.columns(3)
    c1.metric("Tickers with data", r.get("n_tickers_with_data", 0))
    c2.metric("% tickers positive", f"{r.get('pct_tickers_positive')}%")
    c3.metric("Top-5 share of positive return", f"{r.get('top5_share_of_positive_return_pct')}%")
    if r.get("concentration_flag"):
        st.warning("⚠️ Performance in this bucket is concentrated in a handful of tickers (top 5 ≥ 60% of positive return).")

    best = pd.DataFrame(r.get("best_5", []))
    worst = pd.DataFrame(r.get("worst_5", []))
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Best performers**")
        st.dataframe(best, use_container_width=True, hide_index=True)
    with col2:
        st.markdown("**Worst performers**")
        st.dataframe(worst, use_container_width=True, hide_index=True)


def _render_factors(results: dict):
    st.subheader("Phase 8 research-factor validation")
    st.caption(
        "Relative strength is validated historically (price-derived, full universe coverage). Fundamentals and "
        "news sentiment are NOT validated here - the local database only stores a current snapshot for the 7 "
        "production watchlist tickers, not point-in-time history across the research universe, so sample "
        "availability is inadequate for a historical study (see Phase 9 report)."
    )
    rs = results.get("rs_quantile_returns")
    if rs is None or rs.empty:
        components.empty_state("No factor data", "Run the study to populate this section.")
        return
    st.markdown("**Relative strength quartile → 20-day forward return** (Q1 = weakest relative strength, Q4 = strongest)")
    st.plotly_chart(build_bar_chart(rs["rs_quartile"], rs["mean_return_pct"], y_title="Mean 20d forward return (%)", color=SLOT_VIOLET),
                     use_container_width=True, config={"displaylogo": False})
    st.dataframe(rs, use_container_width=True, hide_index=True)


def _render_amzn_monitor():
    st.subheader("AMZN paper position — read-only monitor")
    st.caption("Live Alpaca paper-account read. This section can never place, modify, or cancel an order.")
    try:
        status = get_amzn_monitor_status()
    except Exception as e:
        components.empty_state("AMZN status unavailable", str(e), icon="⚠️")
        return
    if not status.ok:
        components.empty_state("AMZN status unavailable", status.reason, icon="⚠️")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Qty", f"{status.qty:g}")
    c2.metric("Avg entry", f"${status.avg_entry_price:,.2f}")
    c3.metric("Current price", f"${status.current_price:,.2f}")
    c4.metric("Market value", f"${status.market_value:,.2f}")
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Unrealized P&L", f"${status.unrealized_pl:,.2f} ({status.unrealized_pl_pct:+.2f}%)")
    c6.metric("Account equity", f"${status.account_equity:,.2f}")
    c7.metric("Portfolio exposure", f"{status.portfolio_exposure_pct:.1f}%" if status.portfolio_exposure_pct is not None else "n/a")
    c8.metric("Regime", status.regime or "n/a")
    st.caption(f"Current signal: score {status.score:.0f}/100 · stage {status.stage or 'n/a'}" if status.score is not None else "Signal data unavailable")

    if status.exit_condition_met:
        st.error(f"🔴 **PAPER EXIT CONDITION MET — MANUAL ACTION REQUIRED** ({status.exit_condition_detail})", icon="🚨")
    else:
        st.success(f"Paper exit condition not currently met. ({status.exit_condition_detail})", icon="✅")
    st.caption("Informational only — this page never executes an exit. Use `python -m trading.run_paper --paper-send` manually if you choose to act.")


def _phase10_disclaimer():
    components.disclaimer(
        "Historical research only — not a prediction, recommendation, or guarantee of future performance. "
        "The 2021–2026 sample was already examined in Phase 9 to discover this regime-gating hypothesis, so it is "
        "NOT a pristine out-of-sample test — see the OOS/data-snooping note below."
    )


def _render_phase10_regime(p10: dict):
    st.subheader("Regime distribution, duration & transitions")
    dist = p10.get("regime_distribution")
    if dist is not None and not dist.empty:
        st.plotly_chart(build_bar_chart(dist["label"], dist["pct_of_sample"], y_title="% of sample", color=SLOT_VIOLET),
                         use_container_width=True, config={"displaylogo": False})
        st.dataframe(dist, use_container_width=True, hide_index=True)
    st.caption(f"Bullish sufficiently selective (≤70% of sample): {p10.get('bullish_sufficiently_selective')}")
    durations = p10.get("regime_durations")
    if durations is not None and not durations.empty:
        st.markdown("**Regime episode durations**")
        st.dataframe(durations, use_container_width=True, hide_index=True)
    matrix = p10.get("regime_transition_matrix")
    if matrix is not None:
        st.markdown("**Day-to-day transition matrix** (rows = from, columns = to)")
        st.dataframe(matrix, use_container_width=True)
    freq = p10.get("regime_transition_frequency")
    if freq:
        st.caption(f"{freq.get('n_transitions')} regime changes over {freq.get('n_days')} days ({freq.get('transition_rate_pct')}% of days).")


def _render_phase10_variant_comparison(p10: dict):
    st.subheader("CONTROL vs Experiment A vs Experiment B")
    st.caption("Equal-weight combination of per-ticker whole-history backtests, identical execution assumptions across all three.")
    vr = p10.get("variant_results", {})
    friction = st.radio("Friction assumption", ["reasonable", "idealized"], horizontal=True, key="p10_friction")

    series = {}
    rows = []
    for name, label in VARIANT_LABELS.items():
        data = vr.get(name, {}).get(friction)
        if not data:
            continue
        curve = data.get("curve")
        if curve is not None and not curve.empty:
            s = curve * 100.0
            s.index = pd.to_datetime(s.index)
            series[label] = s
        rob = data.get("robustness", {})
        rows.append({
            "Variant": label, "Cumulative return %": round(data.get("cumulative_return_pct") or 0, 2),
            "Mean ticker return %": rob.get("mean_return_pct"), "Median ticker return %": rob.get("median_return_pct"),
            "% profitable": rob.get("pct_profitable"), "% beat own buy-hold": rob.get("pct_beating_own_buy_hold"),
        })

    if series:
        st.plotly_chart(build_multi_line_chart(series, y_title="Indexed to 100 at start"), use_container_width=True, config={"displaylogo": False})
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    bootstrap = p10.get("bootstrap_results", {})
    if bootstrap:
        st.markdown("**Paired bootstrap CI, per-ticker total return (A/B minus CONTROL)** — includes zero ⇒ not statistically distinguishable")
        st.dataframe(pd.DataFrame([
            {"comparison": "A − CONTROL", **bootstrap.get("control_vs_a", {})},
            {"comparison": "B − CONTROL", **bootstrap.get("control_vs_b", {})},
        ]), use_container_width=True, hide_index=True)


def _render_phase10_drawdown(p10: dict):
    st.subheader("Drawdown / downside comparison")
    vr = p10.get("variant_results", {})
    for name, label in VARIANT_LABELS.items():
        data = vr.get(name, {}).get("reasonable")
        if not data:
            continue
        curve = data.get("curve")
        with st.expander(label, expanded=(name == "original_frozen_strategy")):
            if curve is not None and not curve.empty:
                s = curve.copy()
                s.index = pd.to_datetime(s.index)
                st.plotly_chart(build_drawdown_chart(s), use_container_width=True, config={"displaylogo": False})
            eps = data.get("drawdown_episodes")
            if eps is not None and not eps.empty:
                st.markdown("Worst episodes:")
                st.dataframe(eps, use_container_width=True, hide_index=True)


def _render_phase10_event_study(p10: dict):
    st.subheader("Forward returns: all events vs bullish-regime vs non-bullish-regime")
    st.caption("score_cross_70 transition events only (not every day above threshold). Overlapping horizons >1d are not independent observations.")
    event_study = p10.get("event_study", {})
    friction = st.radio("Friction", ["reasonable", "idealized"], horizontal=True, key="p10_event_friction")
    horizon = st.select_slider("Horizon (days)", options=HORIZON_OPTIONS, value=20, key="p10_event_horizon")

    rows = []
    for group, label in (("all", "All events"), ("bullish", "Bullish-regime events"), ("non_bullish", "Non-bullish-regime events")):
        d = event_study.get(group)
        if not d:
            continue
        sub = d[friction]
        r = sub[sub["horizon_days"] == horizon]
        if not r.empty:
            row = r.iloc[0].to_dict()
            row["group"] = label
            row["n_events"] = d["n_events"]
            rows.append(row)
    if rows:
        df = pd.DataFrame(rows)
        st.plotly_chart(build_bar_chart(df["group"], df["mean_return_pct"], y_title="Mean return (%)", color="pnl"),
                         use_container_width=True, config={"displaylogo": False})
        st.dataframe(df[["group", "n_events", "n", "mean_return_pct", "median_return_pct", "pct_positive", "spy_relative_mean_return_pct"]],
                     use_container_width=True, hide_index=True)


def _render_phase10_bullish_bucket_stage(p10: dict):
    st.subheader("Score buckets & confirmation stages — inside bullish_trend only")
    st.caption("Does conditioning on regime restore a sensible score/stage → return relationship? (Existing thresholds only — nothing searched.)")
    horizon = st.select_slider("Horizon (days)", options=HORIZON_OPTIONS, value=20, key="p10_bucket_horizon")
    col1, col2 = st.columns(2)
    bs = p10.get("bullish_bucket_scores")
    with col1:
        if bs is not None and not bs.empty:
            sub = bs[bs["horizon_days"] == horizon]
            st.plotly_chart(build_bar_chart(sub["bucket"], sub["mean_return_pct"], y_title="Mean return (%)", color=SLOT_BLUE),
                             use_container_width=True, config={"displaylogo": False})
    bt = p10.get("bullish_bucket_stages")
    with col2:
        if bt is not None and not bt.empty:
            sub = bt[bt["horizon_days"] == horizon].set_index("stage").reindex(["none", "trend", "momentum", "volume"]).reset_index()
            st.plotly_chart(build_bar_chart(sub["stage"], sub["mean_return_pct"], y_title="Mean return (%)", color=SLOT_AQUA),
                             use_container_width=True, config={"displaylogo": False})


def _render_phase10_year_by_year(p10: dict):
    st.subheader("Year-by-year")
    vr = p10.get("variant_results", {})
    frames = []
    for name, label in VARIANT_LABELS.items():
        yby = vr.get(name, {}).get("reasonable", {}).get("year_by_year")
        if yby is not None and not yby.empty:
            t = yby.set_index("year")["return_pct"].rename(label)
            frames.append(t)
    if frames:
        combined = pd.concat(frames, axis=1).reset_index()
        st.dataframe(combined, use_container_width=True, hide_index=True)
        st.caption("2021/2022 near-zero rows for A/B partly reflect the 200-day regime warm-up window, not necessarily genuine avoidance skill.")


def _render_phase10_robustness(p10: dict):
    st.subheader("Cross-stock robustness by variant")
    vr = p10.get("variant_results", {})
    for name, label in VARIANT_LABELS.items():
        rob = vr.get(name, {}).get("reasonable", {}).get("robustness", {})
        if not rob:
            continue
        with st.expander(label):
            c1, c2, c3 = st.columns(3)
            c1.metric("Tickers profitable", f"{rob.get('pct_profitable')}%")
            c2.metric("Beat own buy-hold", f"{rob.get('pct_beating_own_buy_hold')}%")
            c3.metric("Top-5 share of + return", f"{rob.get('top5_share_of_positive_return_pct')}%")
            if rob.get("concentration_flag"):
                st.warning("⚠️ Concentrated in a handful of tickers.")
            col1, col2 = st.columns(2)
            col1.markdown("**Best**")
            col1.dataframe(pd.DataFrame(rob.get("best_5", [])), use_container_width=True, hide_index=True)
            col2.markdown("**Worst**")
            col2.dataframe(pd.DataFrame(rob.get("worst_5", [])), use_container_width=True, hide_index=True)


def _render_phase10_diagnostics(p10: dict):
    st.subheader("Regime-transition risk, failed-signal, MFE/MAE, exposure")

    ttl = p10.get("ttl_summary")
    if ttl is not None and not ttl.empty:
        st.markdown("**Forward return by time-remaining-in-bullish-regime at entry** (diagnostic only, not a new filter)")
        st.plotly_chart(build_bar_chart(ttl["ttl_bucket"], ttl["mean"] * 100, y_title="Mean 20d return (%)", color="pnl"),
                         use_container_width=True, config={"displaylogo": False})
        st.dataframe(ttl, use_container_width=True, hide_index=True)

    failed = p10.get("failed_signal_summary")
    if failed:
        st.markdown("**Succeeded vs failed bullish-regime events — entry-time feature comparison**")
        st.caption(f"Outcome counts: {failed.get('outcome_counts')}")
        st.dataframe(pd.DataFrame(failed.get("feature_comparison", [])), use_container_width=True, hide_index=True)

    mfe = p10.get("mfe_mae_summary")
    if mfe is not None and not mfe.empty:
        st.markdown("**Maximum favorable / adverse excursion**")
        st.dataframe(mfe, use_container_width=True, hide_index=True)

    st.markdown("**Exposure / holding period by variant**")
    vr = p10.get("variant_results", {})
    hp_rows = []
    for name, label in VARIANT_LABELS.items():
        hp = vr.get(name, {}).get("reasonable", {}).get("holding_period", {})
        hp_rows.append({"Variant": label, **hp})
    st.dataframe(pd.DataFrame(hp_rows), use_container_width=True, hide_index=True)


def _render_phase10_prospective_status():
    st.subheader("Prospective validation status (legacy summary)")
    st.caption("See the full Phase 11 — Prospective Validation section below for observation/event/outcome detail.")
    try:
        from db.database import db_session
        from strategy_lab.prospective import load_observations
        with db_session() as conn:
            obs = load_observations(conn)
        if obs.empty:
            st.caption("No prospective observations recorded yet.")
        else:
            st.metric("Observations recorded", len(obs))
            st.caption(f"Date range: {obs['observation_date'].min()} → {obs['observation_date'].max()}")
    except Exception as e:
        st.caption(f"Prospective observation table unavailable: {e}")


# --- Phase 11 ---


def _render_production_health():
    st.header("Phase 11 — Production Health")
    st.caption("Read-only view of the production automation run history. This section never triggers ingestion.")
    health = get_production_health()
    if not health.get("status"):
        components.empty_state("No production run history yet", "Runs will appear here once automation/run_daily.py has executed.")
        return

    status = health["status"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Latest run status", status)
    c2.metric("Started", health.get("started_at") or "n/a")
    c3.metric("Send mode", health.get("send_mode") or "n/a")
    c4.metric("Tickers failed", health.get("tickers_failed", 0))

    if status in ("failed", "partial_failure"):
        st.error(
            f"⚠️ Latest scheduled production run was **{status}** — displayed signals elsewhere in this app may be "
            f"based on the last successfully stored data, not today's fresh analysis. {health.get('error_summary') or ''}",
            icon="⚠️",
        )
    elif status == "success":
        st.success("Latest scheduled production run succeeded.", icon="✅")

    with st.expander("Recent run history"):
        st.dataframe(health.get("history"), use_container_width=True, hide_index=True)


def _render_prospective_validation():
    from strategy_lab.prospective_events import evidence_label

    st.header("Phase 11 — Prospective Validation")
    st.caption(
        "Genuinely unseen forward evidence collected AFTER this system began recording — immutable, insert-only "
        "observations (strategy_lab/prospective.py). NOT scheduled yet; see the research LaunchAgent proposal "
        "below. Display/research labels only — never automatically upgrades trading readiness."
    )
    summary = get_prospective_summary()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Observations", summary["observation_count"])
    c2.metric("First → last date", f"{summary['first_date']} → {summary['last_date']}" if summary["first_date"] else "n/a")
    c3.metric("Events recorded", summary["event_count"])
    maturation = summary["maturation"]
    c4.metric("Outcomes matured", maturation.get("matured", 0))

    label = evidence_label(summary["event_count"])
    if label == "INSUFFICIENT EVIDENCE":
        st.warning(f"**{label}** — fewer than 30 qualifying prospective events recorded so far.", icon="🔬")
    elif label == "PRELIMINARY":
        st.info(f"**{label}** — 30–99 qualifying events. Directional only, not yet a robust sample.", icon="🔬")
    else:
        st.success(f"**{label}** — 100+ qualifying events recorded.", icon="🔬")

    if summary["event_counts_by_type"]:
        st.markdown("**Events by type**")
        st.dataframe(pd.DataFrame(list(summary["event_counts_by_type"].items()), columns=["event_type", "count"]),
                     use_container_width=True, hide_index=True)

    st.markdown("**Outcome maturation**")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Pending", maturation.get("pending", 0))
    m2.metric("Matured", maturation.get("matured", 0))
    m3.metric("Unavailable", maturation.get("unavailable", 0))
    m4.metric("Total tracked", maturation.get("total", 0))

    st.markdown("**Research job run history**")
    run_history = get_research_run_history()
    if run_history is not None and not run_history.empty:
        st.dataframe(run_history, use_container_width=True, hide_index=True)
    else:
        st.caption("No research job runs recorded yet.")


def _render_realistic_portfolio():
    st.header("Phase 11 — Realistic Portfolio Simulation")
    st.caption(
        "Research-only historical simulation using Phase 7's ACTUAL capital/risk constraints ($100,000 reference "
        "equity, max 10% per position, max 60% exposure, max 6 positions, long-only, next-session execution, "
        "5bps slippage + 10bps commission). Never imports/calls Alpaca order submission."
    )
    p11 = get_phase11_results()
    if not p11:
        components.empty_state(
            "No Phase 11 research cache found",
            "Run `python -m strategy_lab.run_phase11_study` from the project root to generate the Phase 11 snapshot.",
            icon="💼",
        )
        return

    st.caption(f"Phase 11 snapshot generated: {p11.get('generated_at', 'unknown')}.")
    stats = p11.get("portfolio_stats", {})
    benchmarks = p11.get("benchmarks", {})

    rows = []
    for name, label in VARIANT_LABELS.items():
        s = stats.get(name)
        if s is None:
            continue
        rows.append({
            "Variant": label, "Final value": s.final_value, "Cumulative return %": s.cumulative_return_pct,
            "CAGR %": s.cagr_pct, "Ann. vol %": s.annualized_vol_pct, "Sharpe (rf0)": s.sharpe_ratio_rf0,
            "Sortino (rf0)": s.sortino_ratio_rf0, "Max DD %": s.max_drawdown_pct, "Calmar": s.calmar_ratio,
            "Realized P&L": s.realized_pnl, "Trades": s.trade_count, "Win rate %": s.win_rate_pct,
            "Profit factor": s.profit_factor, "Turnover": s.turnover, "Avg exposure %": s.avg_exposure_pct,
            "Max exposure %": s.max_exposure_pct, "Avg positions": s.avg_position_count,
        })
    for label, bs in (("SPY buy-and-hold", benchmarks.get("spy_buy_hold")), ("Equal-weight universe buy-and-hold", benchmarks.get("equal_weight_universe"))):
        if bs is not None:
            rows.append({
                "Variant": label, "Final value": None, "Cumulative return %": bs.cumulative_return_pct,
                "CAGR %": bs.annualized_return_pct, "Ann. vol %": bs.annualized_vol_pct, "Sharpe (rf0)": bs.sharpe_ratio_rf0,
                "Max DD %": bs.max_drawdown_pct,
            })
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    sims = p11.get("simulations", {})
    series = {}
    for name, label in VARIANT_LABELS.items():
        result = sims.get(name)
        if result is not None and not result.equity_curve.empty:
            s = result.equity_curve.set_index("date")["equity"]
            s.index = pd.to_datetime(s.index)
            series[label] = s
    if series:
        st.plotly_chart(build_multi_line_chart(series, y_title="Equity ($)"), use_container_width=True, config={"displaylogo": False})

    st.markdown("**Capital-constraint / skipped-opportunity accounting**")
    cc = p11.get("capital_constraint_summary", {})
    cc_rows = [{"Variant": VARIANT_LABELS.get(name, name), **summary} for name, summary in cc.items()]
    if cc_rows:
        st.dataframe(pd.DataFrame(cc_rows), use_container_width=True, hide_index=True)

    st.markdown("**Concentration / correlation diagnostics** (research-only — does not create a new risk rule)")
    conc = p11.get("concentration_diagnostics", {})
    conc_rows = []
    for name, label in VARIANT_LABELS.items():
        c = conc.get(name)
        if c:
            conc_rows.append({
                "Variant": label, "Tickers held": len(c.get("holding_frequency", {})),
                "Avg pairwise correlation": c.get("avg_pairwise_correlation"),
                "Most correlated pair": c.get("most_correlated_pair"),
            })
    if conc_rows:
        st.dataframe(pd.DataFrame(conc_rows), use_container_width=True, hide_index=True)

    comparison = p11.get("phase10_equal_weight_comparison")
    if comparison:
        st.markdown("**B13: realistic vs. Phase 10 equal-weight approximation**")
        comp_rows = []
        for name, label in VARIANT_LABELS.items():
            p10_val = comparison.get(name, {})
            realistic_return = stats.get(name).cumulative_return_pct if stats.get(name) else None
            comp_rows.append({
                "Variant": label,
                "Phase 10 equal-weight return %": p10_val.get("cumulative_return_pct"),
                "Phase 11 realistic return %": realistic_return,
            })
        st.dataframe(pd.DataFrame(comp_rows), use_container_width=True, hide_index=True)
        st.caption(comparison.get("source", ""))


# --- Phase 12 ---


def _render_experiment_registry():
    st.header("Phase 12 — Experiment Governance Registry")
    st.caption("Immutable methodology metadata. A real change to hypothesis, "
               "methodology, or frozen config always requires a NEW experiment_id "
               "— this table can never be edited in place for those fields.")
    df = get_experiment_registry()
    if df.empty:
        components.empty_state("No experiments registered",
            "Run `python -m ops.register_phase10_11_experiments`.", icon="📋")
        return
    st.dataframe(df, use_container_width=True, hide_index=True)
    drift = get_experiment_registry_drift()   # new getter wrapping check_active_experiments_config_drift
    drifted = {k: v for k, v in drift.items() if v["drifted"]}
    if drifted:
        st.error(f"⚠️ Config drift detected for ACTIVE experiment(s): {list(drifted.keys())}", icon="🚨")


def _render_prospective_evidence_progress():
    st.header("Phase 12 — Prospective Evidence Progress (trading-day based)")
    st.caption(
        "Distinct from the event-count evidence label in the Prospective "
        "Validation section above (strategy_lab.prospective_events.evidence_label). "
        "This measures distinct genuinely-forward-observed trading days."
    )
    status = get_prospective_evidence_status()
    n = status["n_prospective_trading_days"]
    label = status["status"]
    st.metric("Prospective trading days observed", n)
    if label == "INSUFFICIENT_DATA":
        st.warning("**INSUFFICIENT_DATA**", icon="🔬")
    elif label == "EARLY_EVIDENCE":
        st.info("**EARLY_EVIDENCE**", icon="🔬")
    else:
        st.success("**EVALUATION_READY**", icon="🔬")

    st.subheader("Prospective vs. retrospective comparison")
    if label == "INSUFFICIENT_DATA":
        st.warning("INSUFFICIENT PROSPECTIVE EVIDENCE", icon="⚠️")
        return   # no chart, no table — nothing further rendered

    # EARLY_EVIDENCE or EVALUATION_READY: render the comparison, using only
    # already-existing, already-computed pure reads (strategy_lab.prospective's
    # own realized-returns join, and the Phase 9 research cache's bucket
    # returns) - never a new computation of retrospective performance.
    if label == "EARLY_EVIDENCE":
        st.caption("⚠️ Small sample (EARLY_EVIDENCE) — directional only, not yet a robust comparison.")

    try:
        from db.database import db_session
        from strategy_lab.prospective import compute_realized_returns
        with db_session() as conn:
            realized = compute_realized_returns(conn)
    except Exception as e:
        st.caption(f"Prospective realized-return comparison unavailable: {e}")
        return

    if realized.empty:
        st.caption("No matured prospective observations old enough yet to compute realized forward returns.")
        return

    horizon = _horizon_picker("prospective_comparison_horizon")
    return_col = f"realized_return_{horizon}d"
    if return_col not in realized.columns:
        st.caption(f"No {horizon}-day realized returns available yet.")
        return

    control_rows = realized[(realized["control_entry_signal"] == 1) & realized[return_col].notna()]
    if control_rows.empty:
        st.caption("No matured CONTROL entry-signal observations at this horizon yet.")
        return

    prospective_mean_pct = round(float(control_rows[return_col].mean()) * 100, 2)
    comp_rows = [{
        "Source": "Prospective (forward-observed, CONTROL entry signal)",
        "n": len(control_rows), "Mean return %": prospective_mean_pct,
    }]

    p9 = get_strategy_lab_results()
    bucket_scores = p9.get("bucket_scores") if p9 else None
    if bucket_scores is not None and not bucket_scores.empty:
        sub = bucket_scores[(bucket_scores["horizon_days"] == horizon) & (bucket_scores["bucket"] == "70-84")]
        if not sub.empty:
            comp_rows.append({
                "Source": "Retrospective (historical research, 70-84 score bucket)",
                "n": int(sub["n"].iloc[0]), "Mean return %": sub["mean_return_pct"].iloc[0],
            })

    st.dataframe(pd.DataFrame(comp_rows), use_container_width=True, hide_index=True)


def render():
    results = get_strategy_lab_results()
    _render_header(results if results else {})
    if not results:
        components.empty_state(
            "No Phase 9 research cache found",
            "Run `python -m strategy_lab.run_study` from the project root to generate the research snapshot this page reads.",
            icon="🧪",
        )
        return

    _render_coverage(results)
    st.divider()
    _render_bucket_returns(results)
    st.divider()
    _render_stage_returns(results)
    st.divider()
    _render_events(results)
    st.divider()
    _render_strategy_vs_benchmark(results)
    st.divider()
    _render_walk_forward(results)
    st.divider()
    _render_regime(results)
    st.divider()
    _render_robustness(results)
    st.divider()
    _render_factors(results)

    st.divider()
    components.disclaimer(
        "Statistical caution: horizons > 1 day use overlapping observations (not independent draws); confidence "
        "intervals shown are naive and understate true uncertainty. Cumulative return figures are sensitive to a "
        "handful of large-winner tickers (e.g. NVDA/MU-style outliers) - median ticker results are shown alongside "
        "means wherever possible. This is exploratory research evidence, not a statistically proven trading edge."
    )

    st.divider()
    st.header("Phase 10 — Regime-gated strategy research")
    _phase10_disclaimer()
    p10 = get_phase10_results()

    _render_amzn_monitor()
    st.divider()

    if not p10:
        components.empty_state(
            "No Phase 10 research cache found",
            "Run `python -m strategy_lab.run_phase10_study` from the project root to generate the Phase 10 snapshot.",
            icon="🧫",
        )
        return

    st.caption(f"Phase 10 snapshot generated: {p10.get('generated_at', 'unknown')}.")
    _render_phase10_regime(p10)
    st.divider()
    _render_phase10_variant_comparison(p10)
    st.divider()
    _render_phase10_drawdown(p10)
    st.divider()
    _render_phase10_event_study(p10)
    st.divider()
    _render_phase10_bullish_bucket_stage(p10)
    st.divider()
    _render_phase10_year_by_year(p10)
    st.divider()
    _render_phase10_robustness(p10)
    st.divider()
    _render_phase10_diagnostics(p10)
    st.divider()
    _render_phase10_prospective_status()

    st.divider()
    _render_production_health()
    st.divider()
    _render_prospective_validation()
    st.divider()
    _render_realistic_portfolio()

    st.divider()
    try:
        _render_experiment_registry()
    except Exception as e:
        components.empty_state("Experiment registry unavailable", str(e), icon="⚠️")

    st.divider()
    try:
        _render_prospective_evidence_progress()
    except Exception as e:
        components.empty_state("Prospective evidence progress unavailable", str(e), icon="⚠️")
