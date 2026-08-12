"""Pull company news and basic fundamentals from Finnhub and store in SQLite."""
import logging
from datetime import datetime, timedelta, timezone

import finnhub

from config.settings import FINNHUB_API_KEY, NEWS_LOOKBACK_DAYS

logger = logging.getLogger(__name__)


def _require_credentials():
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY is not set in the environment")


def get_client():
    _require_credentials()
    return finnhub.Client(api_key=FINNHUB_API_KEY)


def fetch_news(ticker, lookback_days=NEWS_LOOKBACK_DAYS):
    client = get_client()
    to_date = datetime.utcnow().date()
    from_date = to_date - timedelta(days=lookback_days)
    articles = client.company_news(ticker, _from=str(from_date), to=str(to_date))
    return articles or []


def store_news(conn, ticker, articles):
    cur = conn.cursor()
    rows = [
        (
            ticker,
            a.get("headline", ""),
            a.get("summary", ""),
            a.get("source", ""),
            a.get("url", ""),
            datetime.utcfromtimestamp(a["datetime"]).strftime("%Y-%m-%d %H:%M:%S")
            if a.get("datetime")
            else datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        )
        for a in articles
        if a.get("headline") and a.get("url")
    ]
    cur.executemany(
        """
        INSERT OR IGNORE INTO news (ticker, headline, summary, source, url, published_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def fetch_fundamentals(ticker):
    client = get_client()
    data = client.company_basic_financials(ticker, "all")
    return (data or {}).get("metric", {})


def fetch_next_earnings_date(ticker, lookahead_days=120):
    """Nearest upcoming earnings date within the lookahead window, or None
    if Finnhub has nothing scheduled. Used by research/fundamentals.py
    (Phase 8) - this is the only consumer; it is never read by the trading
    or automation pipelines."""
    client = get_client()
    today = datetime.utcnow().date()
    to_date = today + timedelta(days=lookahead_days)
    calendar = client.earnings_calendar(_from=str(today), to=str(to_date), symbol=ticker)
    events = (calendar or {}).get("earningsCalendar") or []
    dated_events = sorted((e.get("date") for e in events if e.get("date")))
    return dated_events[0] if dated_events else None


def store_next_earnings_date(conn, ticker, date_str):
    """Stored in the existing generic `fundamentals` table (value is REAL,
    so the date is encoded as a UTC midnight epoch timestamp under metric
    name 'nextEarningsDateEpoch') rather than adding a new table for one field."""
    if not date_str:
        return 0
    epoch = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    as_of = datetime.utcnow().strftime("%Y-%m-%d")
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO fundamentals (ticker, metric, value, as_of) VALUES (?, 'nextEarningsDateEpoch', ?, ?)",
        (ticker, epoch, as_of),
    )
    conn.commit()
    return cur.rowcount


def store_fundamentals(conn, ticker, metrics):
    cur = conn.cursor()
    as_of = datetime.utcnow().strftime("%Y-%m-%d")
    rows = [
        (ticker, key, float(value), as_of)
        for key, value in metrics.items()
        if isinstance(value, (int, float))
    ]
    cur.executemany(
        """
        INSERT OR IGNORE INTO fundamentals (ticker, metric, value, as_of)
        VALUES (?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def ingest_ticker(conn, ticker):
    articles = fetch_news(ticker)
    news_count = store_news(conn, ticker, articles)

    metrics = fetch_fundamentals(ticker)
    fundamentals_count = store_fundamentals(conn, ticker, metrics)

    try:
        earnings_date = fetch_next_earnings_date(ticker)
        fundamentals_count += store_next_earnings_date(conn, ticker, earnings_date)
    except Exception as e:
        # Non-fatal and optional (Phase 8 research only) - never let an
        # earnings-calendar hiccup fail the news/fundamentals ingestion.
        logger.warning("finnhub: earnings calendar unavailable for %s: %s", ticker, e)

    logger.info(
        "finnhub: stored %d news rows and %d fundamentals rows for %s",
        news_count,
        fundamentals_count,
        ticker,
    )
    return news_count, fundamentals_count
