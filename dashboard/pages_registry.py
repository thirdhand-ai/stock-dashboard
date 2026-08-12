"""Central registry of st.Page objects.

Imported lazily (inside callbacks, not at view-module load time) by views
that need to st.switch_page() to another view - importing this eagerly from
a view module would create a circular import, since this module imports
every view to build its Page objects.
"""
import streamlit as st

from dashboard.views import alert_history, backtest_report, paper_portfolio, research_center, strategy_lab, ticker_detail, watchlist

PAGE_WATCHLIST = st.Page(watchlist.render, title="Watchlist", icon="📋", url_path="watchlist", default=True)
PAGE_TICKER_DETAIL = st.Page(ticker_detail.render, title="Ticker Detail", icon="📈", url_path="detail")
PAGE_PAPER_PORTFOLIO = st.Page(paper_portfolio.render, title="Paper Portfolio", icon="💼", url_path="paper-portfolio")
PAGE_RESEARCH_CENTER = st.Page(research_center.render, title="Research Center", icon="🔬", url_path="research-center")
PAGE_BACKTEST = st.Page(backtest_report.render, title="Backtest Report", icon="🧪", url_path="backtest")
PAGE_ALERTS = st.Page(alert_history.render, title="Alert History", icon="🔔", url_path="alerts")
PAGE_STRATEGY_LAB = st.Page(strategy_lab.render, title="Strategy Lab", icon="🧫", url_path="strategy-lab")

NAV_STRUCTURE = {
    "Overview": [PAGE_WATCHLIST, PAGE_TICKER_DETAIL, PAGE_PAPER_PORTFOLIO],
    "Research": [PAGE_RESEARCH_CENTER, PAGE_BACKTEST, PAGE_ALERTS, PAGE_STRATEGY_LAB],
}
