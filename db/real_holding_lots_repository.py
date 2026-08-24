"""Read/write interface for real_holding_lots - see db/
real_holding_lots_schema.py's docstring for what this table is and its
relationship to real_holdings and dividend_payments.

Every function here calls ensure_real_holding_lots_schema(conn) first -
same lazy, on-first-use convention as db/real_holdings_repository.py.
"""
from dataclasses import dataclass
from typing import List, Optional

from db.real_holding_lots_schema import ensure_real_holding_lots_schema


@dataclass(frozen=True)
class RealHoldingLot:
    id: int
    ticker: str
    owner: str
    purchase_date: str
    shares: float
    cost_per_share: float
    total_cost: float
    note: Optional[str]


def _row_to_lot(row) -> RealHoldingLot:
    return RealHoldingLot(
        id=row["id"], ticker=row["ticker"], owner=row["owner"], purchase_date=row["purchase_date"],
        shares=row["shares"], cost_per_share=row["cost_per_share"], total_cost=row["total_cost"],
        note=row["note"],
    )


def list_real_holding_lots(conn, ticker: Optional[str] = None, owner: Optional[str] = None) -> List[RealHoldingLot]:
    """Lots, purchase_date-ascending (oldest first - matches how a running
    share total is naturally read, same convention as
    db/dividend_payments_repository.py::list_dividend_payments). Optionally
    narrowed to one ticker, or one (ticker, owner) pair when both are given."""
    ensure_real_holding_lots_schema(conn)
    if ticker is not None and owner is not None:
        rows = conn.execute(
            "SELECT id, ticker, owner, purchase_date, shares, cost_per_share, total_cost, note "
            "FROM real_holding_lots WHERE ticker = ? AND owner = ? ORDER BY purchase_date",
            (ticker, owner or ""),
        ).fetchall()
    elif ticker is not None:
        rows = conn.execute(
            "SELECT id, ticker, owner, purchase_date, shares, cost_per_share, total_cost, note "
            "FROM real_holding_lots WHERE ticker = ? ORDER BY purchase_date",
            (ticker,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, ticker, owner, purchase_date, shares, cost_per_share, total_cost, note "
            "FROM real_holding_lots ORDER BY purchase_date"
        ).fetchall()
    return [_row_to_lot(row) for row in rows]


def add_real_holding_lot(
    conn,
    ticker: str,
    owner: Optional[str] = None,
    purchase_date: str = None,
    shares: float = None,
    cost_per_share: float = None,
    total_cost: float = None,
    note: Optional[str] = None,
) -> int:
    """Insert one dated share-purchase lot. Never an upsert - each lot is
    its own event, same "insert-only ledger" pattern
    db/dividend_payments_repository.py::add_dividend_payment uses. Returns
    the new row's id."""
    ensure_real_holding_lots_schema(conn)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO real_holding_lots (ticker, owner, purchase_date, shares, cost_per_share, total_cost, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (ticker, owner or "", purchase_date, shares, cost_per_share, total_cost, note),
    )
    conn.commit()
    return cur.lastrowid


def delete_real_holding_lot(conn, lot_id: int) -> None:
    """Remove a single, incorrectly-entered lot by id - never a bulk
    ticker-wide delete, same convention as
    db/dividend_payments_repository.py::delete_dividend_payment."""
    ensure_real_holding_lots_schema(conn)
    conn.execute("DELETE FROM real_holding_lots WHERE id = ?", (lot_id,))
    conn.commit()
