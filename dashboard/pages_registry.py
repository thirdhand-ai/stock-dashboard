"""Central registry of st.Page objects.

Imported lazily (inside callbacks, not at view-module load time) by views
that need to st.switch_page() to another view - importing this eagerly from
a view module would create a circular import, since this module imports
every view to build its Page objects.
"""
import streamlit as st

from dashboard.views import alert_history, backtest_report, ops_overview, paper_portfolio, price_alert_config, research_center, strategy_lab, ticker_detail, volatility_alert_config, watchlist

PAGE_WATCHLIST = st.Page(watchlist.render, title="Watchlist", icon="📋", url_path="watchlist", default=True)
PAGE_TICKER_DETAIL = st.Page(ticker_detail.render, title="Ticker Detail", icon="📈", url_path="detail")
PAGE_PAPER_PORTFOLIO = st.Page(paper_portfolio.render, title="Paper Portfolio", icon="💼", url_path="paper-portfolio")
PAGE_RESEARCH_CENTER = st.Page(research_center.render, title="Research Center", icon="🔬", url_path="research-center")
PAGE_BACKTEST = st.Page(backtest_report.render, title="Backtest Report", icon="🧪", url_path="backtest")
PAGE_ALERTS = st.Page(alert_history.render, title="Alert History", icon="🔔", url_path="alerts")
PAGE_PRICE_ALERT_CONFIG = st.Page(price_alert_config.render, title="Price Alert Thresholds", icon="⚙️", url_path="price-alert-config")
PAGE_VOLATILITY_ALERT_CONFIG = st.Page(volatility_alert_config.render, title="Volatility Alert Thresholds", icon="📉", url_path="volatility-alert-config")
PAGE_STRATEGY_LAB = st.Page(strategy_lab.render, title="Strategy Lab", icon="🧫", url_path="strategy-lab")
PAGE_OPS_OVERVIEW = st.Page(ops_overview.render, title="Operations", icon="🛠️", url_path="ops")

NAV_STRUCTURE = {
    "Overview": [PAGE_WATCHLIST, PAGE_TICKER_DETAIL, PAGE_PAPER_PORTFOLIO, PAGE_OPS_OVERVIEW],
    "Research": [PAGE_RESEARCH_CENTER, PAGE_BACKTEST, PAGE_ALERTS, PAGE_PRICE_ALERT_CONFIG, PAGE_VOLATILITY_ALERT_CONFIG, PAGE_STRATEGY_LAB],
}
