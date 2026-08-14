"""Historical OHLCV acquisition for Phase 9, isolated from production
ingestion (ingestion/run_ingestion.py, automation/pipeline.py) - this module
is never imported by either.

Stores into the same shared `prices` table as production ingestion, but
under its OWN source tag (`RESEARCH_SOURCE = "alpaca_adjusted"`) rather than
reusing "alpaca" - deliberately, for two reasons:

1. Split/dividend adjustment: Alpaca's bars endpoint defaults to raw
   (unadjusted) prices. Production ingestion (ingestion/alpaca_source.py)
   fetches raw bars, which is fine for its ~30-day recent-bar use case, but
   is wrong for a 5-year backtest that spans corporate actions - e.g. GOOGL's
   2022 20-for-1 split shows up as a fabricated ~-90% overnight price crash
   in raw data. Phase 9 fetches with adjustment=ALL (splits + dividends)
   instead.
2. Isolation: writing under a distinct source means this never overwrites or
   mixes with the raw rows production code already relies on for the 7
   watchlist tickers - db.price_repository.load_price_history(ticker) with
   no explicit source still resolves to "alpaca" (SOURCE_PRIORITY), so
   production reads are completely unaffected by anything this module does.
   Every strategy_lab module explicitly passes source=RESEARCH_SOURCE.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from alpaca.data.enums import Adjustment
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from db.price_repository import load_price_history
from ingestion.alpaca_source import get_data_client

logger = logging.getLogger(__name__)

RESEARCH_SOURCE = "alpaca_adjusted"
DEFAULT_YEARS = 5
CHUNK_SIZE = 30  # tickers per Alpaca request, to keep individual calls small


def _store_adjusted_bars(conn, ticker: str, df) -> int:
    cur = conn.cursor()
    rows = [
        (ticker, row.date, float(row.open), float(row.high), float(row.low), float(row.close), int(row.volume), RESEARCH_SOURCE)
        for row in df.itertuples(index=False)
    ]
    cur.executemany(
        """
        INSERT INTO prices (ticker, date, open, high, low, close, volume, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, date, source) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume, fetched_at=datetime('now')
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def _needs_fetch(conn, ticker: str, min_rows: int) -> bool:
    existing = load_price_history(conn, ticker, source=RESEARCH_SOURCE)
    return len(existing) < min_rows


def fetch_and_cache_universe(conn, tickers: List[str], years: int = DEFAULT_YEARS) -> Dict[str, dict]:
    """Ensure `years` of split/dividend-adjusted daily OHLCV is cached
    (source=RESEARCH_SOURCE) for every ticker. Skips tickers that already
    have sufficient cached history, so repeated runs don't redownload
    (Phase 9 spec item 2). Never fabricates data for a ticker that fails -
    failures are reported, not silently dropped or interpolated.
    """
    min_rows = int(years * 365 * 0.68)  # ~252 trading days/year, generous floor
    to_fetch = [t for t in tickers if _needs_fetch(conn, t, min_rows)]
    already_cached = [t for t in tickers if t not in to_fetch]
    report: Dict[str, dict] = {
        t: {"status": "cached", "rows": len(load_price_history(conn, t, source=RESEARCH_SOURCE)), "error": None}
        for t in already_cached
    }

    # Phase 14 Component A (additive, §1.3): tickers that already have
    # enough total rows (so were never in `to_fetch`) may still have a
    # stale/incomplete SINGLE latest bar (fetched intraday, before that
    # session's NYSE close, and never refreshed since - see
    # strategy_lab/cache_integrity.py's docstring). This pass runs
    # automatically every call, no opt-in flag (§0.5 item 2); the cost is
    # bounded - one cheap SQL check per already-cached ticker, an Alpaca call
    # only for tickers genuinely flagged incomplete, and never the full
    # 5-year backfill. `_needs_fetch` itself and the full-backfill loop below
    # are completely unchanged.
    from strategy_lab.cache_integrity import refresh_incomplete_latest_bars
    completeness = refresh_incomplete_latest_bars(conn, already_cached)
    for t, r in completeness.items():
        if r["status"] != "no_action_needed":
            report[t]["completeness_refresh"] = r

    if not to_fetch:
        return report

    client = get_data_client()
    start = datetime.now(timezone.utc) - timedelta(days=int(years * 365.25))

    for i in range(0, len(to_fetch), CHUNK_SIZE):
        chunk = to_fetch[i:i + CHUNK_SIZE]
        try:
            request = StockBarsRequest(
                symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start, adjustment=Adjustment.ALL,
            )
            barset = client.get_stock_bars(request)
            df = barset.df.reset_index()
        except Exception as e:
            logger.error("strategy_lab data fetch failed for chunk %s: %s", chunk, e)
            for t in chunk:
                report[t] = {"status": "failed", "rows": 0, "error": str(e)}
            continue

        returned_symbols = set(df["symbol"].unique()) if not df.empty else set()
        for t in chunk:
            sub = df[df["symbol"] == t] if t in returned_symbols else None
            if sub is None or sub.empty:
                report[t] = {"status": "failed", "rows": 0, "error": "no data returned by Alpaca"}
                continue
            sub = sub.copy()
            sub["date"] = sub["timestamp"].dt.strftime("%Y-%m-%d")
            n = _store_adjusted_bars(conn, t, sub[["date", "open", "high", "low", "close", "volume"]])
            report[t] = {"status": "fetched", "rows": n, "error": None}

    return report


def load_universe_prices(conn, tickers: List[str]) -> Dict[str, "pd.DataFrame"]:
    """Load cached, split/dividend-adjusted price history for each ticker
    (empty DataFrame, never an exception, for tickers with no data)."""
    return {t: load_price_history(conn, t, source=RESEARCH_SOURCE) for t in tickers}


def coverage_summary(prices_by_ticker: Dict[str, "pd.DataFrame"]) -> dict:
    loaded = {t: df for t, df in prices_by_ticker.items() if not df.empty}
    failed = [t for t, df in prices_by_ticker.items() if df.empty]
    if not loaded:
        return {"n_tickers": 0, "n_failed": len(failed), "failed": failed, "date_range": None, "total_observations": 0}
    min_date = min(df["date"].iloc[0] for df in loaded.values())
    max_date = max(df["date"].iloc[-1] for df in loaded.values())
    total_obs = sum(len(df) for df in loaded.values())
    return {
        "n_tickers": len(loaded),
        "n_failed": len(failed),
        "failed": failed,
        "date_range": (min_date, max_date),
        "total_observations": total_obs,
    }
