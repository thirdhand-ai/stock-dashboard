"""Read/write interface for real_holdings - see db/real_holdings_schema.py's
docstring for what this table is and why it's separate from the Alpaca
paper-trading tables.

Every function here calls ensure_real_holdings_schema(conn) first - same
lazy, on-first-use convention as db/price_alert_repository.py.
"""
from dataclasses import dataclass
from typing import List, Optional

from db.real_holdings_schema import ensure_real_holdings_schema


@dataclass(frozen=True)
class RealHolding:
    ticker: str
    owner: Optional[str]
    shares: Optional[float]
    cost_basis_total: Optional[float]
    realized_gain: float
    needs_manual_entry: bool
    note: Optional[str]


def _row_to_holding(row) -> RealHolding:
    return RealHolding(
        ticker=row["ticker"], owner=row["owner"], shares=row["shares"],
        cost_basis_total=row["cost_basis_total"], realized_gain=row["realized_gain"],
        needs_manual_entry=bool(row["needs_manual_entry"]), note=row["note"],
    )


def list_real_holdings(conn) -> List[RealHolding]:
    """All tracked real holdings, ticker-ascending. Includes positions
    flagged needs_manual_entry (shares/cost_basis_total NULL) - callers
    decide how to display those, this just returns what's stored."""
    ensure_real_holdings_schema(conn)
    rows = conn.execute(
        "SELECT ticker, owner, shares, cost_basis_total, realized_gain, needs_manual_entry, note "
        "FROM real_holdings ORDER BY ticker"
    ).fetchall()
    return [_row_to_holding(row) for row in rows]


def get_real_holding(conn, ticker: str) -> Optional[RealHolding]:
    ensure_real_holdings_schema(conn)
    row = conn.execute(
        "SELECT ticker, owner, shares, cost_basis_total, realized_gain, needs_manual_entry, note "
        "FROM real_holdings WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    return _row_to_holding(row) if row is not None else None


def upsert_real_holding(
    conn,
    ticker: str,
    owner: Optional[str] = None,
    shares: Optional[float] = None,
    cost_basis_total: Optional[float] = None,
    realized_gain: float = 0.0,
    needs_manual_entry: bool = False,
    note: Optional[str] = None,
) -> None:
    """Add a new ticker's holding, or overwrite an existing one's - single-
    row-per-ticker, so add and edit are the same operation, same pattern
    db/price_alert_config_repository.py::upsert_price_alert_config uses."""
    ensure_real_holdings_schema(conn)
    conn.execute(
        """
        INSERT INTO real_holdings (ticker, owner, shares, cost_basis_total, realized_gain, needs_manual_entry, note, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(ticker) DO UPDATE SET
            owner = excluded.owner, shares = excluded.shares, cost_basis_total = excluded.cost_basis_total,
            realized_gain = excluded.realized_gain, needs_manual_entry = excluded.needs_manual_entry,
            note = excluded.note, updated_at = excluded.updated_at
        """,
        (ticker, owner, shares, cost_basis_total, realized_gain, int(needs_manual_entry), note),
    )
    conn.commit()


def delete_real_holding(conn, ticker: str) -> None:
    ensure_real_holdings_schema(conn)
    conn.execute("DELETE FROM real_holdings WHERE ticker = ?", (ticker,))
    conn.commit()
