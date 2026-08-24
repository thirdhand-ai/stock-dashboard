"""Data Completeness: a single, read-only list of every gap already
flagged elsewhere in this system - real_holdings.needs_manual_entry,
missing shares/cost basis, real_holdings.share_history_caveat (DRIP
growth with no dated history to reconstruct from), and realized_sales
rows missing a purchase/sale date. See trading/data_completeness.py's
docstring: this invents no new flag types, it only re-reads the exact
same nullable columns/flags Real Holdings, Portfolio Performance, and
Realized Gains already surface individually, collected in one place with
a pointer to which page actually lets you go fix each one.

Also renders a second, separate section for a DIFFERENT class of problem:
trading/holdings_consistency.py's mismatches, where a holding's lot/
dividend ledger DOES exist but doesn't sum to real_holdings' recorded
shares/cost_basis_total - "the data disagrees with itself", not "the data
is missing". Kept visually and structurally separate from the gaps
section below (its own subheader, its own table, rendered first) rather
than folded in as just another gap category, so the two problem classes
are never mistaken for each other.

Strictly read-only: no write path anywhere on this page.
"""
import streamlit as st

from dashboard import components
from dashboard.data import get_data_completeness_report, get_holdings_consistency_mismatches


def _render_consistency_mismatches(mismatches):
    st.subheader("⚠️ Data consistency mismatches")
    st.caption(
        "A different class of problem from the gaps below: these positions HAVE a full lot/dividend "
        "transaction ledger (real_holding_lots + dividend_payments), but real_holdings' recorded shares "
        "or cost basis don't match what that ledger actually sums to - the data exists on both sides, it "
        "just disagrees, rather than being missing."
    )
    mismatch_rows = [
        {
            "Ticker": m.ticker, "Owner": m.owner or "Unassigned", "Field": m.field,
            "Recorded": round(m.recorded, 4), "Ledger sums to": round(m.reconstructed, 4),
            "Detail": m.detail,
        }
        for m in mismatches
    ]
    st.dataframe(mismatch_rows, use_container_width=True, hide_index=True)


def render():
    st.title("Data Completeness")
    st.caption(
        "Every gap already flagged elsewhere in this dashboard - needs_manual_entry holdings, unknown "
        "share counts or cost bases, approximate (DRIP) share history, and realized sales missing a "
        "purchase or sale date - collected in one place. This surfaces existing flags only; it never "
        "guesses at a missing value or invents a new kind of gap. A separate section below covers a "
        "different problem: recorded data that doesn't add up, not data that's missing."
    )

    report = get_data_completeness_report()
    mismatches = get_holdings_consistency_mismatches()

    if not report.gaps and not mismatches:
        components.empty_state(
            "No gaps or mismatches currently flagged",
            "Every real holding, dividend record, and realized-gains transaction currently has complete "
            "data - nothing here is marked needs_manual_entry, approximate, or missing a date, and every "
            "lot/dividend ledger sums to its holding's recorded shares and cost basis.",
            icon="✅",
        )
        return

    if mismatches:
        _render_consistency_mismatches(mismatches)
        st.divider()

    if not report.gaps:
        st.success("No missing-data gaps currently flagged.", icon="✅")
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
