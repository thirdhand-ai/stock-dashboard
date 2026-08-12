"""Backtest Report: reuses the Phase 3 backtesting + walk-forward layer."""
import pandas as pd
import streamlit as st

from backtest.config import DEFAULT_EXECUTION, DEFAULT_RULES, DEFAULT_WF_CONFIG
from config.settings import WATCHLIST
from dashboard import components
from dashboard.charts import build_equity_curve_chart, index_to_100
from dashboard.data import get_backtest_report


def _metric_row(result):
    c1, c2, c3 = st.columns(3)
    c1.metric("Total return", f"{result.total_return_pct:+.2f}%")
    c2.metric("Buy & hold return", f"{result.buy_hold_return_pct:+.2f}%")
    c3.metric("Sharpe ratio", f"{result.sharpe_ratio:.2f}")

    c4, c5, c6 = st.columns(3)
    c4.metric("Max drawdown", f"{result.max_drawdown_pct:.2f}%")
    c5.metric("Win rate", f"{result.win_rate_pct:.1f}%" if result.num_trades else "n/a")
    c6.metric("Number of trades", f"{result.num_trades}")

    c7, c8, c9 = st.columns(3)
    c7.metric("Avg trade return", f"{result.avg_trade_pct:+.2f}%" if result.num_trades else "n/a")
    c8.metric("Best trade", f"{result.best_trade_pct:+.2f}%" if result.num_trades else "n/a")
    c9.metric("Worst trade", f"{result.worst_trade_pct:+.2f}%" if result.num_trades else "n/a")


def render():
    st.title("Backtest Report")
    st.caption("Validates the Phase 2 signal rules against historical data — research only, no trades are placed here.")

    if st.session_state.get("selected_ticker") not in WATCHLIST:
        st.session_state["selected_ticker"] = WATCHLIST[0]
    ticker = st.selectbox("Ticker", WATCHLIST, key="selected_ticker")

    with st.expander("Entry/exit rules and execution assumptions in effect", expanded=False):
        st.markdown(
            f"""
            - **Entry:** highest confirmed stage ≥ `{DEFAULT_RULES.entry_min_stage}` **and** raw score ≥ `{DEFAULT_RULES.entry_min_score}`
            - **Exit:** highest confirmed stage < `{DEFAULT_RULES.exit_stage_floor}` **or** raw score ≤ `{DEFAULT_RULES.exit_max_score}`
            - **Execution:** ${DEFAULT_EXECUTION.initial_cash:,.0f} starting cash, {DEFAULT_EXECUTION.commission:.2%} commission per trade, single position at a time
            - **Walk-forward:** {DEFAULT_WF_CONFIG.train_window_days}-day train window (reserved for future optimization, unused this phase) · {DEFAULT_WF_CONFIG.test_window_days}-day out-of-sample test window · {DEFAULT_WF_CONFIG.step_days}-day step
            """
        )

    report = get_backtest_report(ticker)
    if not report["ok"]:
        components.empty_state(f"{ticker}: backtest not available", report["reason"], icon="⚠️")
        return

    st.caption(
        f"Data source: **{report['source']}** · {report['n_rows']} observations · "
        f"{report['date_range'][0]} to {report['date_range'][1]}"
    )

    result = report["backtest"]

    st.subheader("Whole-history backtest (in-sample)")
    st.caption("The full stored history evaluated as one continuous backtest — useful as a sanity check, but not a substitute for the out-of-sample results below.")
    _metric_row(result)

    strategy_equity = index_to_100(result.equity_curve)
    buy_hold_prices = report["close_prices"].reindex(result.equity_curve.index).ffill()
    buy_hold_equity = index_to_100(buy_hold_prices)
    st.plotly_chart(
        build_equity_curve_chart(strategy_equity, buy_hold_equity),
        use_container_width=True, config={"displaylogo": False},
    )
    if result.total_return_pct < result.buy_hold_return_pct:
        st.caption(
            f"⚠️ The strategy underperformed buy-and-hold by "
            f"{result.buy_hold_return_pct - result.total_return_pct:.2f} percentage points over this period. "
            "This is shown as-is — see the Backtest Report section of the project notes for interpretation."
        )

    st.divider()
    st.subheader("Walk-forward validation (out-of-sample)")
    st.caption(
        "Chronological, non-overlapping train/test windows. Only test-window performance is shown here — "
        "the strategy never sees test-window data before trading it."
    )

    wf = report["walk_forward"]
    summary = wf.summary()

    if summary["n_windows"] == 0:
        components.empty_state(
            "Not enough history for walk-forward validation",
            f"Need at least {DEFAULT_WF_CONFIG.train_window_days + DEFAULT_WF_CONFIG.test_window_days} rows "
            f"({DEFAULT_WF_CONFIG.train_window_days} train + {DEFAULT_WF_CONFIG.test_window_days} test), "
            f"have {report['n_rows']}.",
            icon="⚠️",
        )
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("Windows evaluated", f"{summary['n_windows']}")
    c2.metric("Mean OOS return", f"{summary['mean_return_pct']:+.2f}%")
    c3.metric("Mean Sharpe (traded)", f"{summary['mean_sharpe']:.2f}" if summary["mean_sharpe"] == summary["mean_sharpe"] else "n/a")

    c4, c5, c6 = st.columns(3)
    c4.metric("Windows profitable", f"{summary['pct_windows_profitable']:.0f}%")
    c5.metric("Total trades (OOS)", f"{summary['total_trades']}")
    c6.metric("Pooled win rate", f"{summary['pooled_win_rate_pct']:.1f}%" if summary['pooled_win_rate_pct'] == summary['pooled_win_rate_pct'] else "n/a")

    st.metric("Zero-trade windows", f"{summary['zero_trade_windows']} / {summary['n_windows']}")

    window_rows = []
    for wr in wf.windows:
        if wr.result is None:
            window_rows.append({
                "Window": wr.window.index, "Test period": f"{wr.window.test_start} → {wr.window.test_end}",
                "Return": "skipped", "Sharpe": "—", "Trades": 0, "Max DD": "—",
            })
        else:
            r = wr.result
            window_rows.append({
                "Window": wr.window.index,
                "Test period": f"{wr.window.test_start} → {wr.window.test_end}",
                "Return": f"{r.total_return_pct:+.2f}%",
                "Sharpe": f"{r.sharpe_ratio:.2f}" if r.sharpe_ratio == r.sharpe_ratio else "n/a",
                "Trades": r.num_trades,
                "Max DD": f"{r.max_drawdown_pct:.2f}%",
            })
    st.dataframe(pd.DataFrame(window_rows), use_container_width=True, hide_index=True)

    components.disclaimer(
        "Backtests are historical research on past data, not financial advice and not a guarantee of future "
        "performance. Small trade counts here mean these Sharpe/win-rate figures are illustrative, not statistically reliable."
    )
