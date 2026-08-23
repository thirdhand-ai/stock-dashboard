"""Read/write interface for realized_sales - see db/realized_sales_schema.py's
docstring for what this table is and its relationship to real_holdings.

Every function here calls ensure_realized_sales_schema(conn) first - same
lazy, on-first-use convention as db/real_holdings_repository.py.
"""
from dataclasses import dataclass
from typing import List, Optional

from db.realized_sales_schema import ensure_realized_sales_schema


@dataclass(frozen=True)
class RealizedSale:
    id: int
    ticker: str
    owner: str
    purchase_date: Optional[str]
    sale_date: Optional[str]
    shares_sold: float
    cost_basis_sold: float
    proceeds: float
    note: Optional[str]


def _row_to_sale(row) -> RealizedSale:
    return RealizedSale(
        id=row["id"], ticker=row["ticker"], owner=row["owner"], purchase_date=row["purchase_date"],
        sale_date=row["sale_date"], shares_sold=row["shares_sold"], cost_basis_sold=row["cost_basis_sold"],
        proceeds=row["proceeds"], note=row["note"],
    )


def list_realized_sales(conn, ticker: Optional[str] = None, owner: Optional[str] = None) -> List[RealizedSale]:
    """Sale transactions - most-recent-sale-date-first when sale_date is
    known (NULLs sort last in SQLite's default ASC, so DESC puts them
    first; explicit ordering below avoids relying on that). Optionally
    narrowed to one ticker, or one (ticker, owner) pair when both given."""
    ensure_realized_sales_schema(conn)
    order_by = "ORDER BY (sale_date IS NULL), sale_date DESC, id"
    if ticker is not None and owner is not None:
        rows = conn.execute(
            f"SELECT id, ticker, owner, purchase_date, sale_date, shares_sold, cost_basis_sold, proceeds, note "
            f"FROM realized_sales WHERE ticker = ? AND owner = ? {order_by}",
            (ticker, owner or ""),
        ).fetchall()
    elif ticker is not None:
        rows = conn.execute(
            f"SELECT id, ticker, owner, purchase_date, sale_date, shares_sold, cost_basis_sold, proceeds, note "
            f"FROM realized_sales WHERE ticker = ? {order_by}",
            (ticker,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT id, ticker, owner, purchase_date, sale_date, shares_sold, cost_basis_sold, proceeds, note "
            f"FROM realized_sales {order_by}"
        ).fetchall()
    return [_row_to_sale(row) for row in rows]


def add_realized_sale(
    conn,
    ticker: str,
    owner: Optional[str] = None,
    purchase_date: Optional[str] = None,
    sale_date: Optional[str] = None,
    shares_sold: float = None,
    cost_basis_sold: float = None,
    proceeds: float = None,
    note: Optional[str] = None,
) -> int:
    """Insert one sale/partial-sale transaction. Never an upsert - each
    sale is its own event. purchase_date/sale_date are left NULL rather
    than guessed when not known - see db/realized_sales_schema.py's
    docstring. Returns the new row's id."""
    ensure_realized_sales_schema(conn)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO realized_sales (ticker, owner, purchase_date, sale_date, shares_sold, cost_basis_sold, proceeds, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ticker, owner or "", purchase_date, sale_date, shares_sold, cost_basis_sold, proceeds, note),
    )
    conn.commit()
    return cur.lastrowid


def delete_realized_sale(conn, sale_id: int) -> None:
    """Remove a single, incorrectly-entered sale by id - never a bulk
    ticker-wide delete, same convention as db/dividend_payments_repository.py."""
    ensure_realized_sales_schema(conn)
    conn.execute("DELETE FROM realized_sales WHERE id = ?", (sale_id,))
    conn.commit()
