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

# --- Data ingestion defaults ---
YFINANCE_PERIOD = "2y"
YFINANCE_INTERVAL = "1d"
NEWS_LOOKBACK_DAYS = 7
