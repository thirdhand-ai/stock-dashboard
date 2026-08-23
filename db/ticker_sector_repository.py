"""Read/write interface for ticker_sector - see db/ticker_sector_schema.py's
docstring for what this table is and where the data comes from.

Every function here calls ensure_ticker_sector_schema(conn) first - same
lazy, on-first-use convention as db/real_holdings_repository.py.
"""
from dataclasses import dataclass
from typing import List, Optional

from db.ticker_sector_schema import ensure_ticker_sector_schema


@dataclass(frozen=True)
class TickerSector:
    ticker: str
    industry: Optional[str]


def _row_to_sector(row) -> TickerSector:
    return TickerSector(ticker=row["ticker"], industry=row["industry"])


def list_ticker_sectors(conn) -> List[TickerSector]:
    ensure_ticker_sector_schema(conn)
    rows = conn.execute("SELECT ticker, industry FROM ticker_sector ORDER BY ticker").fetchall()
    return [_row_to_sector(row) for row in rows]


def get_ticker_sector(conn, ticker: str) -> Optional[TickerSector]:
    ensure_ticker_sector_schema(conn)
    row = conn.execute("SELECT ticker, industry FROM ticker_sector WHERE ticker = ?", (ticker,)).fetchone()
    return _row_to_sector(row) if row is not None else None


def upsert_ticker_sector(conn, ticker: str, industry: Optional[str]) -> None:
    """Add or overwrite one ticker's cached industry classification -
    single-row-per-ticker, same pattern as every other config table in
    this codebase."""
    ensure_ticker_sector_schema(conn)
    conn.execute(
        """
        INSERT INTO ticker_sector (ticker, industry, fetched_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(ticker) DO UPDATE SET industry = excluded.industry, fetched_at = excluded.fetched_at
        """,
        (ticker, industry),
    )
    conn.commit()
