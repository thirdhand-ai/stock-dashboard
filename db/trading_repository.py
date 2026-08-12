"""Read/write interface for Phase 7 paper-trading order intents
(`paper_orders`) and portfolio snapshots (`portfolio_snapshots`).

`paper_orders` is keyed by a deterministic `client_order_id` (see
trading/idempotency.py) - upsert_order() does an INSERT ... ON CONFLICT
UPDATE against that key, which is what makes repeated evaluations on
unchanged data idempotent at the storage layer rather than relying solely on
in-memory duplicate detection. Never stores API keys/secrets.
"""
import json
from typing import Optional

import pandas as pd

ORDER_COLUMNS = [
    "id", "ticker", "side", "intent", "qty", "notional", "signal_score",
    "confirmed_stage", "reason", "reference_price", "client_order_id",
    "alpaca_order_id", "status", "risk_checks", "rejection_reason",
    "environment", "submitted_at", "filled_at", "filled_qty",
    "filled_avg_price", "created_at", "updated_at",
]

# Statuses that mean this client_order_id has actually been sent to
# Alpaca - re-evaluating the same candidate while one of these is on file
# must never submit again, since Alpaca already has (or might have) this
# order (see trading/engine.py's duplicate check).
#
# "proposed" is deliberately NOT in this set: it means a --dry-run cycle
# computed and risk-checked this candidate but made zero calls to Alpaca.
# Treating "proposed" as equivalent to "submitted" would permanently block
# a later --paper-send run from ever promoting an approved dry-run
# proposal into a real order under its own client_order_id - a real bug
# this project once had. A "proposed" (or rejected/canceled/error) row is
# safe to re-evaluate and, if it still qualifies, upsert-in-place (same
# client_order_id, same row - never a second logical order) all the way to
# an actual submission.
ALREADY_SUBMITTED_STATUSES = {"submitted", "accepted", "partially_filled", "filled"}

# Statuses safe to retry (a fresh evaluation may re-propose/re-submit under
# this same deterministic client_order_id, updating the existing row rather
# than inserting a duplicate).
RETRYABLE_STATUSES = {"proposed", "rejected_by_risk", "rejected", "canceled", "error"}


def get_order_by_client_id(conn, client_order_id: str):
    return conn.execute(
        "SELECT * FROM paper_orders WHERE client_order_id = ?", (client_order_id,)
    ).fetchone()


def upsert_order(
    conn,
    ticker: str,
    side: str,
    intent: str,
    reason: str,
    client_order_id: str,
    status: str,
    qty: Optional[float] = None,
    notional: Optional[float] = None,
    signal_score: Optional[float] = None,
    confirmed_stage: Optional[str] = None,
    reference_price: Optional[float] = None,
    alpaca_order_id: Optional[str] = None,
    risk_checks: Optional[dict] = None,
    rejection_reason: Optional[str] = None,
    environment: str = "paper",
) -> int:
    """Insert a new order-intent row, or update the existing row for this
    client_order_id in place (idempotent re-evaluation - never duplicates)."""
    risk_checks_json = json.dumps(risk_checks) if risk_checks is not None else None
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO paper_orders (
            ticker, side, intent, qty, notional, signal_score, confirmed_stage,
            reason, reference_price, client_order_id, alpaca_order_id, status,
            risk_checks, rejection_reason, environment
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(client_order_id) DO UPDATE SET
            qty=excluded.qty,
            notional=excluded.notional,
            signal_score=excluded.signal_score,
            confirmed_stage=excluded.confirmed_stage,
            reason=excluded.reason,
            reference_price=excluded.reference_price,
            alpaca_order_id=excluded.alpaca_order_id,
            status=excluded.status,
            risk_checks=excluded.risk_checks,
            rejection_reason=excluded.rejection_reason,
            updated_at=datetime('now')
        """,
        (
            ticker, side, intent, qty, notional, signal_score, confirmed_stage,
            reason, reference_price, client_order_id, alpaca_order_id, status,
            risk_checks_json, rejection_reason, environment,
        ),
    )
    conn.commit()
    row = get_order_by_client_id(conn, client_order_id)
    return row["id"]


def update_order_status(
    conn,
    order_id: int,
    status: str,
    alpaca_order_id: Optional[str] = None,
    submitted_at: Optional[str] = None,
    filled_at: Optional[str] = None,
    filled_qty: Optional[float] = None,
    filled_avg_price: Optional[float] = None,
    rejection_reason: Optional[str] = None,
):
    """Update an order's lifecycle status - used both right after submission
    and by reconciliation against Alpaca's authoritative order state."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE paper_orders SET
            status = ?,
            alpaca_order_id = COALESCE(?, alpaca_order_id),
            submitted_at = COALESCE(?, submitted_at),
            filled_at = COALESCE(?, filled_at),
            filled_qty = COALESCE(?, filled_qty),
            filled_avg_price = COALESCE(?, filled_avg_price),
            rejection_reason = COALESCE(?, rejection_reason),
            updated_at = datetime('now')
        WHERE id = ?
        """,
        (status, alpaca_order_id, submitted_at, filled_at, filled_qty,
         filled_avg_price, rejection_reason, order_id),
    )
    conn.commit()


def get_orders_in_nonterminal_state(conn):
    """Local orders that were actually submitted to Alpaca and might still
    change state (submitted/accepted/partially_filled) - the reconciliation
    worklist. 'proposed'/'rejected_by_risk' never reached Alpaca, so they're
    excluded; 'filled'/'canceled'/'rejected'/'error' are already terminal."""
    return conn.execute(
        "SELECT * FROM paper_orders WHERE status IN ('submitted', 'accepted', 'partially_filled')"
    ).fetchall()


def load_order_history(conn, ticker=None, limit=500) -> pd.DataFrame:
    columns_sql = ", ".join(ORDER_COLUMNS)
    if ticker:
        query = f"SELECT {columns_sql} FROM paper_orders WHERE ticker = ? ORDER BY created_at DESC LIMIT ?"
        params = (ticker, limit)
    else:
        query = f"SELECT {columns_sql} FROM paper_orders ORDER BY created_at DESC LIMIT ?"
        params = (limit,)
    return pd.read_sql_query(query, conn, params=params)


def load_filled_orders_for_ticker(conn, ticker: str) -> pd.DataFrame:
    """Chronological filled orders for one ticker - the basis for matching
    entry/exit pairs into realized P&L (see trading/portfolio.py)."""
    return pd.read_sql_query(
        """
        SELECT * FROM paper_orders
        WHERE ticker = ? AND status = 'filled'
        ORDER BY filled_at ASC, created_at ASC
        """,
        conn,
        params=(ticker,),
    )


def insert_portfolio_snapshot(
    conn,
    equity: float,
    cash: float,
    buying_power: float,
    long_market_value: float,
    invested_exposure_pct: float,
    unrealized_pl: float,
    open_position_count: int,
    environment: str = "paper",
) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO portfolio_snapshots (
            equity, cash, buying_power, long_market_value, invested_exposure_pct,
            unrealized_pl, open_position_count, environment
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (equity, cash, buying_power, long_market_value, invested_exposure_pct,
         unrealized_pl, open_position_count, environment),
    )
    conn.commit()
    return cur.lastrowid


def load_portfolio_snapshots(conn, limit: int = 500) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM portfolio_snapshots ORDER BY captured_at ASC LIMIT ?",
        conn,
        params=(limit,),
    )
