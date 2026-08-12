"""Paper Portfolio: Phase 7 read-only view of the real Alpaca paper account,
persisted order history, and portfolio analytics.

Strictly read-only for order execution - this module never imports
trading.engine or trading.orders, so opening, clicking, or refreshing this
page can never submit, modify, or cancel a trade. The only Alpaca calls made
(via dashboard/data.py -> trading/client.py) are account/positions/orders
reads. The one write path on this page - the "Capture snapshot" button -
only inserts a portfolio_snapshots row (a data capture, not a trade).
"""
import pandas as pd
import streamlit as st

from dashboard import components
from dashboard.charts import build_drawdown_chart, build_portfolio_equity_chart, index_to_100
from dashboard.data import (
    capture_paper_portfolio_snapshot,
    clear_all_caches,
    get_paper_benchmark_series,
    get_paper_equity_curve,
    get_paper_order_history,
    get_paper_portfolio,
)
from dashboard.theme import STATUS_CRITICAL, STATUS_GOOD, TEXT_MUTED
from trading.config import DEFAULT_RISK_CONFIG

MIN_SNAPSHOTS_FOR_DRAWDOWN = 5


def _pnl_color(value: float) -> str:
    return STATUS_GOOD if (value or 0) >= 0 else STATUS_CRITICAL


def _render_summary(portfolio):
    s = portfolio.summary
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Equity", f"${s.equity:,.2f}")
    c2.metric("Cash", f"${s.cash:,.2f}")
    c3.metric("Buying power", f"${s.buying_power:,.2f}")
    c4.metric("Invested exposure", f"{s.invested_exposure_pct * 100:.1f}%")

    c5, c6, c7 = st.columns(3)
    c5.metric("Unrealized P&L", f"${s.unrealized_pl:,.2f}")
    c6.metric("Realized P&L", f"${portfolio.realized_pnl:,.2f}")
    c7.metric("Open positions", f"{s.open_position_count}")

    wl = portfolio.win_loss
    if wl.get("completed_trades", 0) > 0:
        st.caption(
            f"Completed trades: {wl['completed_trades']} · Wins: {wl['wins']} · "
            f"Losses: {wl['losses']} · Flat: {wl['flat']}"
        )
    else:
        st.caption("No completed round-trip trades yet (no entry has been both filled and later exited).")


def _render_positions(portfolio):
    st.subheader("Open positions")
    if not portfolio.positions:
        st.caption("No open paper positions.")
        return

    rows = []
    for p in portfolio.positions:
        rows.append({
            "Ticker": p.ticker,
            "Qty": f"{p.qty:g}",
            "Avg entry": f"${p.avg_entry_price:,.2f}",
            "Current price": f"${p.current_price:,.2f}",
            "Market value": f"${p.market_value:,.2f}",
            "Weight": f"{p.weight_pct * 100:.1f}%",
            "Unrealized P&L": f"${p.unrealized_pl:,.2f} ({p.unrealized_pl_pct:+.2f}%)",
            "Score": f"{p.signal_score:.0f}/100" if p.signal_score is not None else "n/a",
            "Stage": p.signal_stage or "n/a",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _render_order_history():
    st.subheader("Trade / order history")
    st.caption(
        "Every entry/exit decision this system has made, dry-run or paper-send, with the reason and signal state "
        "at decision time. `environment` is always 'paper' - this table never reflects live-money activity."
    )

    orders = get_paper_order_history(limit=300)
    if orders.empty:
        components.empty_state(
            "No paper-trading orders recorded yet",
            "Run `python -m trading.run_paper --dry-run` to evaluate the watchlist against the current Alpaca "
            "paper account (submits nothing). `python -m trading.run_paper --paper-send` submits qualifying PAPER orders.",
            icon="🧾",
        )
        return

    display = orders.copy()
    display["Signal at decision"] = display.apply(
        lambda r: f"{r['signal_score']:.0f}/100 · {r['confirmed_stage']}" if r["signal_score"] is not None else "n/a",
        axis=1,
    )
    display["Fill"] = display.apply(
        lambda r: f"{r['filled_qty']:g} @ ${r['filled_avg_price']:,.2f}" if r["filled_qty"] else "—",
        axis=1,
    )
    display = display.rename(columns={
        "ticker": "Ticker", "side": "Side", "intent": "Intent", "status": "Status",
        "reason": "Reason", "environment": "Env", "created_at": "Created",
        "submitted_at": "Submitted", "filled_at": "Filled", "rejection_reason": "Rejection reason",
    })
    columns = [
        "Ticker", "Intent", "Side", "Status", "Signal at decision", "Reason",
        "Fill", "Env", "Created", "Submitted", "Filled", "Rejection reason",
    ]
    st.dataframe(display[columns], use_container_width=True, hide_index=True)


def _render_performance():
    st.subheader("Performance")
    st.caption(
        "Built only from portfolio snapshots actually captured with the 'Capture snapshot' button or "
        "`python -m trading.run_paper` - never backfilled or estimated."
    )

    snapshots = get_paper_equity_curve()
    if snapshots.empty or len(snapshots) < 2:
        components.empty_state(
            "Not enough snapshot history yet",
            f"{len(snapshots)} snapshot(s) captured so far. Capture at least 2 (ideally across different days) "
            "to see an equity chart.",
            icon="📈",
        )
        return

    equity_series = snapshots.set_index(pd.to_datetime(snapshots["captured_at"]))["equity"]

    benchmark_df = get_paper_benchmark_series("SPY")
    benchmark_indexed = None
    if benchmark_df is not None and not benchmark_df.empty:
        bench = benchmark_df.copy()
        bench["date"] = pd.to_datetime(bench["date"])
        bench = bench.set_index("date")["close"]
        # SPY's daily bars are date-only (midnight); a snapshot captured
        # later the same day would otherwise be excluded by a naive
        # timestamp comparison - normalize to the snapshot's calendar date.
        bench = bench[bench.index >= equity_series.index.min().normalize()]
        if len(bench) >= 2:
            benchmark_indexed = index_to_100(bench)

    equity_indexed = index_to_100(equity_series) if benchmark_indexed is not None else equity_series
    st.plotly_chart(
        build_portfolio_equity_chart(equity_indexed, benchmark_indexed, benchmark_label="SPY"),
        use_container_width=True, config={"displaylogo": False},
    )
    if benchmark_indexed is None:
        st.caption("SPY benchmark unavailable — no SPY price history stored locally yet for the snapshot period.")

    if len(snapshots) >= MIN_SNAPSHOTS_FOR_DRAWDOWN:
        st.plotly_chart(build_drawdown_chart(equity_series), use_container_width=True, config={"displaylogo": False})
    else:
        st.caption(f"Drawdown chart needs at least {MIN_SNAPSHOTS_FOR_DRAWDOWN} snapshots ({len(snapshots)} so far).")


def _render_risk(portfolio):
    st.subheader("Risk")
    s = portfolio.summary
    rc = DEFAULT_RISK_CONFIG

    exposure_pct = s.invested_exposure_pct * 100
    exposure_limit_pct = rc.max_total_exposure_pct * 100
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**Exposure:** {exposure_pct:.1f}% of equity invested (limit {exposure_limit_pct:.0f}%)")
        st.progress(min(exposure_pct / exposure_limit_pct, 1.0) if exposure_limit_pct else 0.0)
    with c2:
        st.markdown(f"**Open positions:** {s.open_position_count} (limit {rc.max_open_positions})")
        st.progress(min(s.open_position_count / rc.max_open_positions, 1.0) if rc.max_open_positions else 0.0)

    st.caption(
        f"Max new position size: {rc.max_position_size_pct * 100:.0f}% of equity · "
        f"Long-only: {rc.long_only} · Margin: {rc.allow_margin} · Short selling: {rc.allow_short} · "
        f"Options: {rc.allow_options} · Averaging down: {rc.allow_averaging_down} · "
        f"Duplicate ticker positions: {rc.allow_duplicate_ticker_position}"
    )


def render():
    st.title("Paper Portfolio")
    st.caption(
        "Alpaca PAPER account only — never live money. This page is read-only for trade execution; it never "
        "submits, modifies, or cancels an order. Use `python -m trading.run_paper` from the command line to "
        "evaluate signals and (with --paper-send) submit qualifying paper orders."
    )
    components.disclaimer(
        "This is a research/paper-trading system, not financial advice. All figures reflect a simulated "
        "Alpaca PAPER account."
    )

    left, _ = st.columns([1, 5])
    with left:
        if st.button("↻ Refresh", help="Re-fetch the Alpaca paper account and re-read the local database"):
            clear_all_caches()
            st.rerun()

    portfolio = get_paper_portfolio()
    if not portfolio.ok:
        components.empty_state("Paper account unavailable", portfolio.reason, icon="⚠️")
        return

    _render_summary(portfolio)
    st.divider()
    _render_positions(portfolio)
    st.divider()
    _render_order_history()
    st.divider()
    _render_performance()
    st.divider()
    _render_risk(portfolio)

    st.divider()
    st.caption("Capturing a snapshot only records the current equity/cash/exposure locally — it never places a trade.")
    if st.button("📸 Capture snapshot now"):
        try:
            capture_paper_portfolio_snapshot()
            st.success("Snapshot captured.")
            st.rerun()
        except Exception as e:
            st.error(f"Could not capture snapshot: {e}")
