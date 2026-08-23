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
    share_history_caveat: Optional[str]


def _normalize_owner(owner: Optional[str]) -> str:
    """'' is this table's "not yet assigned" value, never NULL - see
    db/real_holdings_schema.py's docstring for why (SQLite treats every
    NULL as distinct from every other NULL in a UNIQUE index, which would
    silently defeat (ticker, owner) uniqueness). Every read/write path in
    this module goes through this so the convention can't drift."""
    return owner or ""


def _row_to_holding(row) -> RealHolding:
    return RealHolding(
        ticker=row["ticker"], owner=row["owner"], shares=row["shares"],
        cost_basis_total=row["cost_basis_total"], realized_gain=row["realized_gain"],
        needs_manual_entry=bool(row["needs_manual_entry"]), note=row["note"],
        share_history_caveat=row["share_history_caveat"],
    )


SELECT_COLUMNS = "ticker, owner, shares, cost_basis_total, realized_gain, needs_manual_entry, note, share_history_caveat"


def list_real_holdings(conn) -> List[RealHolding]:
    """All tracked real holdings, ticker-ascending (then owner, so a
    multi-owner ticker's rows sit together in a stable order). Includes
    positions flagged needs_manual_entry (shares/cost_basis_total NULL) -
    callers decide how to display those, this just returns what's stored."""
    ensure_real_holdings_schema(conn)
    rows = conn.execute(
        f"SELECT {SELECT_COLUMNS} FROM real_holdings ORDER BY ticker, owner"
    ).fetchall()
    return [_row_to_holding(row) for row in rows]


def get_real_holding(conn, ticker: str, owner: Optional[str] = None) -> Optional[RealHolding]:
    """A single holding by ticker, optionally narrowed to one owner. Pass
    `owner` whenever the ticker might have more than one row (e.g. NOW) -
    without it, this returns whichever matching row SQLite happens to
    return first, which is only safe for a ticker known to have exactly
    one row."""
    ensure_real_holdings_schema(conn)
    if owner is not None:
        row = conn.execute(
            f"SELECT {SELECT_COLUMNS} FROM real_holdings WHERE ticker = ? AND owner = ?",
            (ticker, _normalize_owner(owner)),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT {SELECT_COLUMNS} FROM real_holdings WHERE ticker = ?",
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
    share_history_caveat: Optional[str] = None,
) -> None:
    """Add a new (ticker, owner) holding, or overwrite an existing one's -
    the same operation handles both add and edit, same pattern
    db/price_alert_config_repository.py::upsert_price_alert_config uses.
    The same ticker can have more than one row as long as `owner` differs
    (e.g. NOW: separate rows for Tyler's mother's lot and Tyler's own) -
    omitting `owner` (or passing None) targets the '' "not yet assigned"
    row for that ticker, same as always calling this without an owner.

    Full-overwrite semantics, same as every other field here (e.g. `note`):
    omitting `share_history_caveat` on a later upsert clears it, it does
    not preserve whatever was set before - pass it explicitly every time
    you want it to stick."""
    ensure_real_holdings_schema(conn)
    owner = _normalize_owner(owner)
    conn.execute(
        """
        INSERT INTO real_holdings (ticker, owner, shares, cost_basis_total, realized_gain, needs_manual_entry, note, share_history_caveat, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(ticker, owner) DO UPDATE SET
            shares = excluded.shares, cost_basis_total = excluded.cost_basis_total,
            realized_gain = excluded.realized_gain, needs_manual_entry = excluded.needs_manual_entry,
            note = excluded.note, share_history_caveat = excluded.share_history_caveat,
            updated_at = excluded.updated_at
        """,
        (ticker, owner, shares, cost_basis_total, realized_gain, int(needs_manual_entry), note, share_history_caveat),
    )
    conn.commit()


def delete_real_holding(conn, ticker: str, owner: Optional[str] = None) -> None:
    """Remove a holding. Pass `owner` to remove just that one row; omit it
    to remove every row for that ticker (all owners) - a deliberate
    "remove this ticker entirely" operation, not the default path for a
    ticker known to have more than one owner."""
    ensure_real_holdings_schema(conn)
    if owner is not None:
        conn.execute(
            "DELETE FROM real_holdings WHERE ticker = ? AND owner = ?",
            (ticker, _normalize_owner(owner)),
        )
    else:
        conn.execute("DELETE FROM real_holdings WHERE ticker = ?", (ticker,))
    conn.commit()
