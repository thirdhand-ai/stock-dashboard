"""Portfolio Concentration: diversification view for real_holdings -
position size as % of portfolio value, sector/industry allocation, and
purely descriptive flags for any position or sector past a concentration
threshold. Never a buy/sell signal or investment advice - see
trading/concentration.py's docstring for the same "surfacing the numbers"
contract every other real_holdings-derived page carries.

Shown both combined and per-owner (Tyler vs. Mom currently have very
different concentration profiles - Tyler's tiny, unconfirmed lot would
otherwise be invisible against Mom's much larger, more diversified one).
"""
import streamlit as st

from dashboard import components
from dashboard.data import get_concentration_reports
from trading.concentration import SECTOR_THRESHOLD_PCT, SINGLE_POSITION_THRESHOLD_PCT


def _fmt_money(value):
    return f"${value:,.2f}" if value is not None else "—"


def _fmt_pct(value):
    return f"{value:.1f}%"


def _render_flag_summary(report):
    if not report.flagged_positions and not report.flagged_sectors:
        st.success(
            f"No position exceeds {SINGLE_POSITION_THRESHOLD_PCT:.0f}% of this portfolio, and no sector "
            f"exceeds {SECTOR_THRESHOLD_PCT:.0f}%.",
            icon="✅",
        )
        return

    if report.flagged_positions:
        tickers = ", ".join(f"{p.ticker} ({p.weight_pct:.1f}%)" for p in report.flagged_positions)
        st.warning(f"Position(s) over {SINGLE_POSITION_THRESHOLD_PCT:.0f}% of portfolio value: {tickers}", icon="⚠️")

    if report.flagged_sectors:
        sectors = ", ".join(f"{s.industry} ({s.weight_pct:.1f}%)" for s in report.flagged_sectors)
        st.warning(f"Sector(s) over {SECTOR_THRESHOLD_PCT:.0f}% of portfolio value: {sectors}", icon="⚠️")


def _render_positions_table(report):
    st.subheader("Position size")
    rows = [
        {
            "Ticker": p.ticker,
            "Owner": p.owner or "Unassigned",
            "Industry": p.industry,
            "Market value": _fmt_money(p.market_value),
            "% of portfolio": _fmt_pct(p.weight_pct),
            "Status": "⚠️ Over threshold" if p.exceeds_threshold else "OK",
        }
        for p in report.positions
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_sector_table(report):
    st.subheader("Sector / industry allocation")
    rows = [
        {
            "Industry": s.industry,
            "Market value": _fmt_money(s.market_value),
            "% of portfolio": _fmt_pct(s.weight_pct),
            "Tickers": ", ".join(s.tickers),
            "Status": "⚠️ Over threshold" if s.exceeds_threshold else "OK",
        }
        for s in report.sectors
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_report(report):
    if report.total_market_value == 0:
        components.empty_state(
            f"No priced positions for {report.scope_label}",
            "Every position in this scope is missing shares/cost basis (needs manual entry), "
            "so there's nothing to compute a concentration % against yet."
            if report.excluded_tickers else "No real holdings in this scope.",
            icon="📊",
        )
        return

    st.metric(f"{report.scope_label} total market value", _fmt_money(report.total_market_value))
    _render_flag_summary(report)
    st.divider()
    _render_positions_table(report)
    st.divider()
    _render_sector_table(report)

    if report.excluded_tickers:
        st.caption(
            "Excluded from these percentages (needs manual entry, no market value yet): "
            + ", ".join(report.excluded_tickers)
        )


def render():
    st.title("Portfolio Concentration")
    st.caption(
        "Position size and sector/industry allocation for real holdings - purely descriptive, not "
        "investment advice. Flags a position over "
        f"{SINGLE_POSITION_THRESHOLD_PCT:.0f}% of portfolio value or a sector over "
        f"{SECTOR_THRESHOLD_PCT:.0f}%, just surfacing the numbers for you to notice, same as every "
        "other real_holdings-derived page in this dashboard."
    )
    components.disclaimer("Descriptive concentration flagging only - not a stock signal, not a trade recommendation.")

    reports = get_concentration_reports()
    if not reports or reports.get("Combined") is None:
        components.empty_state("No real holdings tracked yet", "Add one on the Real Holdings page first.", icon="📊")
        return

    scope_options = ["Combined"] + [label for label in reports if label != "Combined"]
    scope = st.radio("Portfolio", scope_options, horizontal=True, key="concentration_scope")

    _render_report(reports[scope])
