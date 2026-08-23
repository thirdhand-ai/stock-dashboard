"""Data Completeness: a single, read-only list of every gap already
flagged elsewhere in this system - real_holdings.needs_manual_entry,
missing shares/cost basis, real_holdings.share_history_caveat (DRIP
growth with no dated history to reconstruct from), and realized_sales
rows missing a purchase/sale date. See trading/data_completeness.py's
docstring: this invents no new flag types, it only re-reads the exact
same nullable columns/flags Real Holdings, Portfolio Performance, and
Realized Gains already surface individually, collected in one place with
a pointer to which page actually lets you go fix each one.

Strictly read-only: no write path anywhere on this page.
"""
import streamlit as st

from dashboard import components
from dashboard.data import get_data_completeness_report


def render():
    st.title("Data Completeness")
    st.caption(
        "Every gap already flagged elsewhere in this dashboard - needs_manual_entry holdings, unknown "
        "share counts or cost bases, approximate (DRIP) share history, and realized sales missing a "
        "purchase or sale date - collected in one place. This surfaces existing flags only; it never "
        "guesses at a missing value or invents a new kind of gap."
    )

    report = get_data_completeness_report()

    if not report.gaps:
        components.empty_state(
            "No gaps currently flagged",
            "Every real holding, dividend record, and realized-gains transaction currently has complete "
            "data - nothing here is marked needs_manual_entry, approximate, or missing a date.",
            icon="✅",
        )
        return

    st.metric("Total gaps flagged", len(report.gaps))

    st.subheader("By category")
    category_rows = [
        {"Category": category, "Count": len(gaps)}
        for category, gaps in report.by_category.items()
    ]
    st.dataframe(category_rows, use_container_width=True, hide_index=True)

    st.subheader("All gaps")
    gap_rows = [
        {
            "Category": g.category,
            "Ticker": g.ticker,
            "Owner": g.owner,
            "What's missing": g.detail,
            "Go fix on": g.fix_page,
        }
        for g in report.gaps
    ]
    st.dataframe(gap_rows, use_container_width=True, hide_index=True)

    st.caption(
        "Dividend records were also scanned - every field in that table is required at entry time, so "
        "no dividend_payments row can currently be in an incomplete state, and none is listed above."
    )
