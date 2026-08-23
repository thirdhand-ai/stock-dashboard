"""Read/write interface for dividend_payments - see db/
dividend_payments_schema.py's docstring for what this table is and its
relationship to real_holdings.

Every function here calls ensure_dividend_payments_schema(conn) first -
same lazy, on-first-use convention as db/real_holdings_repository.py.
"""
from dataclasses import dataclass
from typing import List, Optional

from db.dividend_payments_schema import ensure_dividend_payments_schema


@dataclass(frozen=True)
class DividendPayment:
    id: int
    ticker: str
    owner: str
    pay_date: str
    amount_per_share: float
    total_received: float
    reinvested: bool
    note: Optional[str]


def _row_to_payment(row) -> DividendPayment:
    return DividendPayment(
        id=row["id"], ticker=row["ticker"], owner=row["owner"], pay_date=row["pay_date"],
        amount_per_share=row["amount_per_share"], total_received=row["total_received"],
        reinvested=bool(row["reinvested"]), note=row["note"],
    )


def list_dividend_payments(conn, ticker: Optional[str] = None, owner: Optional[str] = None) -> List[DividendPayment]:
    """Payments, pay_date-ascending (oldest first - matches how a running
    total is naturally read). Optionally narrowed to one ticker, or one
    (ticker, owner) pair when both are given."""
    ensure_dividend_payments_schema(conn)
    if ticker is not None and owner is not None:
        rows = conn.execute(
            "SELECT id, ticker, owner, pay_date, amount_per_share, total_received, reinvested, note "
            "FROM dividend_payments WHERE ticker = ? AND owner = ? ORDER BY pay_date",
            (ticker, owner or ""),
        ).fetchall()
    elif ticker is not None:
        rows = conn.execute(
            "SELECT id, ticker, owner, pay_date, amount_per_share, total_received, reinvested, note "
            "FROM dividend_payments WHERE ticker = ? ORDER BY pay_date",
            (ticker,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, ticker, owner, pay_date, amount_per_share, total_received, reinvested, note "
            "FROM dividend_payments ORDER BY pay_date"
        ).fetchall()
    return [_row_to_payment(row) for row in rows]


def add_dividend_payment(
    conn,
    ticker: str,
    owner: Optional[str] = None,
    pay_date: str = None,
    amount_per_share: float = None,
    total_received: float = None,
    reinvested: bool = False,
    note: Optional[str] = None,
) -> int:
    """Insert one manually-entered dividend payment. Never an upsert - each
    payment is its own event, distinct from real_holdings' one-row-per-
    holding model. Returns the new row's id (dashboard/views/
    dividend_income.py's delete control needs it)."""
    ensure_dividend_payments_schema(conn)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO dividend_payments (ticker, owner, pay_date, amount_per_share, total_received, reinvested, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (ticker, owner or "", pay_date, amount_per_share, total_received, int(reinvested), note),
    )
    conn.commit()
    return cur.lastrowid


def delete_dividend_payment(conn, payment_id: int) -> None:
    """Remove a single, incorrectly-entered payment by id - never a bulk
    ticker-wide delete, since each row is an independent, real event a
    user typed in themselves."""
    ensure_dividend_payments_schema(conn)
    conn.execute("DELETE FROM dividend_payments WHERE id = ?", (payment_id,))
    conn.commit()
