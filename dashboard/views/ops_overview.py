"""Operations: Phase 12 read-only operational dashboard - production
health, data freshness, paper-portfolio reconciliation, and prospective
research evidence progress in one place.

Strictly read-only: every section here reads through dashboard/data.py's
ops getters (backed by the `ops/` package). This page can never trigger
ingestion, never submits/modifies/cancels/closes an order, and never sends
a real Discord message.
"""
import pandas as pd
import streamlit as st

from dashboard import components
from dashboard.data import (
    get_data_quality_report,
    get_ops_daily_report,
    get_ops_research_run_history,
    get_prospective_evidence_status,
    get_reconciliation_report,
    get_run_history,
)


def _render_system_health(report):
    st.header("System health")

    if report is None:
        components.empty_state(
            "Daily report unavailable",
            "Run `python -m ops.run_daily_report` from the project root to generate today's report.",
            icon="⚠️",
        )
    else:
        ph = report["production_health"]
        mr = report["market_regime"]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Latest production run", ph["status"] or "no runs yet")
        c2.metric("Started", ph["started_at"] or "n/a")
        c3.metric("Tickers failed", ph["tickers_failed"])
        c4.metric("Market regime", mr["label"] if mr["ok"] else "n/a")

        if ph["status"] in ("failed", "partial_failure"):
            st.error(
                f"⚠️ Latest scheduled production run was **{ph['status']}**. {ph.get('error_summary') or ''}",
                icon="⚠️",
            )
        elif ph["status"] == "success":
            st.success("Latest scheduled production run succeeded.", icon="✅")
        else:
            st.info("No production run history yet.", icon="ℹ️")

    try:
        dq = get_data_quality_report()
    except Exception as e:
        components.empty_state("Data quality report unavailable", str(e), icon="⚠️")
        return

    overall = dq["overall_status"]
    if overall == "HEALTHY":
        st.success(f"Data quality: **{overall}**", icon="✅")
    elif overall == "DEGRADED":
        st.warning(f"Data quality: **{overall}**", icon="⚠️")
    elif overall == "STALE":
        st.warning(f"Data quality: **{overall}**", icon="🕒")
    else:
        st.error(f"Data quality: **{overall}**", icon="🚨")
    st.caption(f"Checked at {dq['checked_at']}")

    _render_provider_reconciliation(dq)


def _render_provider_reconciliation(dq: dict):
    with st.expander("Provider reconciliation & provenance detail (Phase 13)"):
        st.caption(
            "How production ('alpaca', raw, single-source-per-ticker) and "
            "research ('alpaca_adjusted', split/dividend-adjusted) price data "
            "compare. EXPECTED_ADJUSTMENT_DIFFERENCE is normal and does not "
            "indicate a problem. STALE_SOURCE flags a non-authoritative "
            "source's most recent row as a likely incomplete/preliminary "
            "snapshot — informational, never auto-repaired."
        )
        overall_v2 = dq.get("overall_status_provenance_aware")
        st.metric("Provenance-aware overall status", overall_v2)
        rows = []
        for t in dq["tickers"]:
            for f in t.get("provider_findings", []):
                rows.append({
                    "Ticker": f["ticker"], "Date": f["date"],
                    "Source A": f["source_a"], "Source B": f["source_b"],
                    "Classification": f["classification"],
                    "Price ratio": f["price_ratio"], "Volume ratio": f["volume_ratio"],
                    "Detail": f["detail"],
                })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.caption("No cross-source findings.")
        notes = [n for t in dq["tickers"] for n in t.get("non_authoritative_source_notes", [])]
        if notes:
            st.caption("Non-authoritative source notes: " + "; ".join(notes))


def _render_today_signals(report):
    st.header("Today's signals")
    if report is None:
        components.empty_state(
            "No daily report available",
            "Run `python -m ops.run_daily_report` to generate one.",
            icon="📋",
        )
        return

    rows = []
    for row in report["ticker_signals"]:
        if row["ok"]:
            rows.append({
                "Ticker": row["ticker"], "Score": row["score"], "Stage": row["stage"],
                "Close": row["close"], "Date": row["latest_date"], "Source": row["source"],
            })
        else:
            rows.append({
                "Ticker": row["ticker"], "Score": None, "Stage": None,
                "Close": None, "Date": None, "Source": row["reason_unavailable"],
            })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _render_paper_portfolio_summary(report):
    st.header("Paper portfolio (summary)")
    st.caption("Summary view only — see the Paper Portfolio page for the full detailed view.")
    if report is None:
        return

    pp = report["paper_portfolio"]
    if not pp["ok"]:
        components.empty_state("Paper portfolio unavailable", pp["reason"], icon="⚠️")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Equity", f"${pp['equity']:,.2f}")
    c2.metric("Cash", f"${pp['cash']:,.2f}")
    c3.metric("Unrealized P&L", f"${pp['unrealized_pl']:,.2f}")
    c4.metric("Open positions", pp["open_position_count"])

    if not pp["positions"]:
        st.caption("No open paper positions.")
        return

    rows = [{
        "Ticker": pos["ticker"], "Qty": pos["qty"], "Unrealized P&L": pos["unrealized_pl"],
        "Unrealized P&L %": pos["unrealized_pl_pct"], "Score": pos["score"], "Stage": pos["stage"],
        "Exit condition met": pos["exit_condition_met"],
    } for pos in pp["positions"]]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    flagged = [p["ticker"] for p in pp["positions"] if p["exit_condition_met"]]
    if flagged:
        st.warning(
            f"⚠️ Exit condition met (informational only, never auto-acted-on) for: {flagged}", icon="🔔",
        )


def _render_prospective_evidence():
    st.header("Prospective research status")
    st.caption(
        "Trading-day-based evidence progress (ops/evidence_classification.py) - distinct from the event-count "
        "based evidence label shown on the Strategy Lab page."
    )
    try:
        status = get_prospective_evidence_status()
    except Exception as e:
        components.empty_state("Prospective evidence status unavailable", str(e), icon="⚠️")
        return

    st.metric("Prospective trading days observed", status["n_prospective_trading_days"])
    label = status["status"]
    if label == "INSUFFICIENT_DATA":
        st.warning(f"**{label}**", icon="🔬")
    elif label == "EARLY_EVIDENCE":
        st.info(f"**{label}**", icon="🔬")
    else:
        st.success(f"**{label}**", icon="🔬")


def _render_automation_history():
    st.header("Recent automation history")
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Production runs")
        history = get_run_history(limit=10)
        if history.empty:
            st.caption("No production run history yet.")
        else:
            st.dataframe(history, use_container_width=True, hide_index=True)

    with col2:
        st.subheader("Research job runs")
        try:
            research_history = get_ops_research_run_history(limit=10)
        except Exception as e:
            components.empty_state("Research run history unavailable", str(e), icon="⚠️")
            return
        if research_history.empty:
            st.caption("No research job runs recorded yet.")
        else:
            st.dataframe(research_history, use_container_width=True, hide_index=True)


def _render_reconciliation():
    st.header("Reconciliation summary")
    st.caption(
        "Local paper_orders vs. Alpaca's authoritative paper account state — read-only comparison, never a "
        "mutation. AMZN is the designated live verification case."
    )
    try:
        recon = get_reconciliation_report()
    except Exception as e:
        components.empty_state("Reconciliation report unavailable", str(e), icon="⚠️")
        return

    if recon["alpaca_unreachable"]:
        st.error(f"Alpaca unreachable: {recon['alpaca_error']}", icon="🚨")
        return

    overall = recon["overall_status"]
    if overall == "MATCHED":
        st.success(f"Overall: **{overall}**", icon="✅")
    elif overall == "WARNING":
        st.warning(f"Overall: **{overall}**", icon="⚠️")
    else:
        st.error(f"Overall: **{overall}**", icon="🚨")

    amzn = recon.get("amzn")
    if amzn is not None:
        st.markdown(f"**AMZN (designated live verification ticker): {amzn['status']}**")
        if amzn["reasons"]:
            st.caption("; ".join(amzn["reasons"]))
    else:
        st.caption("AMZN: no local or Alpaca state to reconcile.")

    if recon["tickers"]:
        st.dataframe(pd.DataFrame(recon["tickers"]), use_container_width=True, hide_index=True)


def render():
    st.title("🛠️ Operations")
    components.disclaimer(
        "Phase 12 operational dashboard — production health, data freshness, paper-portfolio reconciliation, "
        "and prospective research evidence progress. Strictly read-only: this page can never trigger "
        "ingestion, submit/cancel/replace/close an order, or send a real Discord message."
    )

    try:
        report = get_ops_daily_report()
    except Exception as e:
        components.empty_state("Daily report unavailable", str(e), icon="⚠️")
        report = None

    _render_system_health(report)
    st.divider()
    _render_today_signals(report)
    st.divider()
    _render_paper_portfolio_summary(report)
    st.divider()
    _render_prospective_evidence()
    st.divider()
    _render_automation_history()
    st.divider()
    _render_reconciliation()
