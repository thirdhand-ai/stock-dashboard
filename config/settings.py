"""Central configuration: env vars, watchlist, and shared constants."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# --- Credentials (read-only access from environment; never hardcode) ---
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
FMP_API_KEY = os.getenv("FMP_API_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# --- Email (price-threshold alerts) ---
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO")

# Alpaca must always run in paper trading mode for this project.
ALPACA_PAPER = True
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"

# --- Storage ---
DB_PATH = BASE_DIR / "data" / "stock_dashboard.db"

# --- Watchlist (configurable) ---
WATCHLIST = [
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "NVDA",
    "TSLA",
    "META",
]

# Confirmed real holdings (db/real_holdings_repository.py) that get the same
# technical-indicator/score-stage signal coverage as WATCHLIST tickers on the
# dashboard (Watchlist page, Ticker Detail page), without joining WATCHLIST
# itself - WATCHLIST must stay exactly equal to strategy_lab/universe.py's
# PRODUCTION_WATCHLIST_OVERLAP (see that module's assert_isolated_from_watchlist).
REAL_HOLDINGS_WITH_SIGNAL_COVERAGE = ["XLV", "NCLH", "KMI", "HPI"]

# Exploratory/research tracking tickers - NOT real holdings and NOT part of
# the production WATCHLIST (same isolation-from-WATCHLIST reasoning as
# REAL_HOLDINGS_WITH_SIGNAL_COVERAGE above). These get the same score/stage
# signal-engine coverage as WATCHLIST tickers on the dashboard purely for
# research/monitoring purposes - no position is held in any of them. GOOGL
# and AMZN were requested alongside this list but are already core WATCHLIST
# tickers with full coverage, so they're intentionally left out here rather
# than duplicated.
EXPLORATORY_WATCHLIST = [
    "AVGO", "TSM", "AMD", "MU", "ALAB", "ANET", "ASML", "PLTR",
    "SNOW", "NXPI", "SNPS", "IBM", "APP",
]

# Tickers shown with score/stage signal indicators across the dashboard.
SIGNAL_COVERAGE_TICKERS = WATCHLIST + REAL_HOLDINGS_WITH_SIGNAL_COVERAGE + EXPLORATORY_WATCHLIST

# --- Data ingestion defaults ---
YFINANCE_PERIOD = "2y"
YFINANCE_INTERVAL = "1d"
NEWS_LOOKBACK_DAYS = 7
