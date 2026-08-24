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
#
# Batch 2 (2026-08-24, the 26 tickers starting at "INTC" below - the
# request said 25, but listed 26 distinct tickers; all 26 were kept rather
# than guessing which to drop) deliberately has NO price-threshold or
# volatility-alert config, unlike batch 1 above (which originally did, until
# those were removed the same day for generating digest/alert-activity
# noise). Consequently, batch 2 is NOT part of automation/pipeline.py's
# default daily ticker union either - that union is driven entirely by
# WATCHLIST + configured alert thresholds + real_holdings, never by this
# list. These tickers got a one-time historical backfill instead (via
# ingestion/alpaca_source.py, run manually - see git history for this
# commit) and will NOT refresh automatically going forward; re-run that
# backfill by hand when the data gets too stale. The alternative - adding
# them to the daily ticker union to stay fresh - would also expose them to
# alerts/engine.py's score-crossing/stage-advance Discord alert, which has
# no per-ticker opt-out (unlike price/volatility alerts, it evaluates every
# ticker the pipeline touches unconditionally). Staleness was judged the
# safer tradeoff for tickers meant to be purely observational.
EXPLORATORY_WATCHLIST = [
    "AVGO", "TSM", "AMD", "MU", "ALAB", "ANET", "ASML", "PLTR",
    "SNOW", "NXPI", "SNPS", "IBM", "APP",
    "INTC", "MRVL", "WDC", "STX", "SNDK", "COHR", "SMCI", "BABA", "JD", "FUTU",
    "HOOD", "SOFI", "VIRT", "BGC", "MP", "SCCO", "ERO", "UUUU", "DK", "CVI",
    "PARR", "IREN", "CRWV", "FCFS", "TEM", "MRNA",
]

# Tickers shown with score/stage signal indicators across the dashboard.
SIGNAL_COVERAGE_TICKERS = WATCHLIST + REAL_HOLDINGS_WITH_SIGNAL_COVERAGE + EXPLORATORY_WATCHLIST

# --- Data ingestion defaults ---
YFINANCE_PERIOD = "2y"
YFINANCE_INTERVAL = "1d"
NEWS_LOOKBACK_DAYS = 7
