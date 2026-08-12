"""Pull daily OHLCV price data from yfinance and store it in SQLite."""
import logging

import yfinance as yf

from config.settings import YFINANCE_INTERVAL, YFINANCE_PERIOD

logger = logging.getLogger(__name__)

SOURCE_NAME = "yfinance"


def fetch_ohlcv(ticker, period=YFINANCE_PERIOD, interval=YFINANCE_INTERVAL):
    """Fetch historical OHLCV data for a single ticker. Returns a DataFrame."""
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    if df.empty:
        raise ValueError(f"yfinance returned no data for {ticker}")
    df = df.reset_index()
    df["date"] = df["Date"].dt.strftime("%Y-%m-%d")
    return df[["date", "Open", "High", "Low", "Close", "Volume"]]


def store_ohlcv(conn, ticker, df):
    """Upsert OHLCV rows for a ticker into the prices table."""
    cur = conn.cursor()
    rows = [
        (
            ticker,
            row.date,
            float(row.Open),
            float(row.High),
            float(row.Low),
            float(row.Close),
            int(row.Volume),
            SOURCE_NAME,
        )
        for row in df.itertuples(index=False)
    ]
    cur.executemany(
        """
        INSERT INTO prices (ticker, date, open, high, low, close, volume, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, date, source) DO UPDATE SET
            open=excluded.open,
            high=excluded.high,
            low=excluded.low,
            close=excluded.close,
            volume=excluded.volume,
            fetched_at=datetime('now')
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def ingest_ticker(conn, ticker):
    df = fetch_ohlcv(ticker)
    n = store_ohlcv(conn, ticker, df)
    logger.info("yfinance: stored %d rows for %s", n, ticker)
    return n
