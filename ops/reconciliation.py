"""Phase 12 Component C: paper portfolio reconciliation (read-only).

A distinct, read-only comparison between locally tracked paper-trading
state (`paper_orders`) and Alpaca's authoritative paper account state -
never a wrapper around `trading/reconcile.py`'s mutating helpers
(`reconcile_order`/`reconcile_open_orders`), which write to `paper_orders`.
This module never imports either of those two functions; it only ever
reads Alpaca via `get_account`/`get_all_positions`/`get_orders`(open)/
`get_order_by_id` and only ever reads local state via existing read-only
SELECT helpers.

`ops/reconciliation_repository.py` (§5.2 of the spec) is folded into this
file rather than kept separate, per the spec's explicit note.
"""
import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from db.trading_repository import get_orders_in_nonterminal_state, load_filled_orders_for_ticker
from trading import client as trading_client

STATUS_MATCHED = "MATCHED"
STATUS_WARNING = "WARNING"
STATUS_MISMATCH = "MISMATCH"

_STATUS_RANK = {STATUS_MATCHED: 0, STATUS_WARNING: 1, STATUS_MISMATCH: 2}

# Fractional-share (`use_notional_entries=True`) tolerant qty comparison -
# never exact float equality.
QTY_TOLERANCE = 0.0001
AVG_PRICE_DIVERGENCE_PCT = 0.02

AMZN_TICKER = "AMZN"

TABLE_NAME = "ops_reconciliation_checks"

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    overall_status TEXT NOT NULL,
    report_json TEXT NOT NULL
)
"""


@dataclass
class TickerReconciliation:
    ticker: str
    status: str
    reasons: List[str] = field(default_factory=list)
    local_qty: Optional[float] = None
    alpaca_qty: Optional[float] = None
    local_avg_price: Optional[float] = None
    alpaca_avg_price: Optional[float] = None
    local_open_order_count: int = 0
    alpaca_open_order_count: int = 0


@dataclass
class ReconciliationReport:
    checked_at: str
    overall_status: str
    tickers: List[TickerReconciliation] = field(default_factory=list)
    amzn: Optional[TickerReconciliation] = None
    alpaca_unreachable: bool = False
    alpaca_error: Optional[str] = None


def derive_local_open_positions(conn) -> Dict[str, dict]:
    """ticker -> {"qty": float, "avg_price": float}, computed ONLY from
    paper_orders WHERE status='filled', using the exact same sequential
    entry/exit pairing convention as trading.portfolio.compute_closed_trades
    (long-only, no averaging down, no duplicate positions per
    trading/config.py's RiskConfig - an entry with no subsequent matching
    exit is the "currently open" position). Read-only SELECT."""
    tickers = [
        row["ticker"] for row in
        conn.execute("SELECT DISTINCT ticker FROM paper_orders WHERE status = 'filled'").fetchall()
    ]

    open_positions: Dict[str, dict] = {}
    for ticker in tickers:
        filled = load_filled_orders_for_ticker(conn, ticker)
        pending_entry = None
        for _, row in filled.iterrows():
            if row["intent"] == "entry":
                pending_entry = row
            elif row["intent"] == "exit" and pending_entry is not None:
                pending_entry = None

        if pending_entry is not None and pending_entry["filled_qty"] is not None:
            open_positions[ticker] = {
                "qty": float(pending_entry["filled_qty"]),
                "avg_price": (
                    float(pending_entry["filled_avg_price"])
                    if pending_entry["filled_avg_price"] is not None else None
                ),
            }

    return open_positions


def _worst_status(statuses: List[str]) -> str:
    return max(statuses, key=lambda s: _STATUS_RANK[s])


def build_reconciliation_report(conn, client=None) -> ReconciliationReport:
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        client = client or trading_client.get_client()
        trading_client.verify_paper_environment(client)
    except Exception as e:
        return ReconciliationReport(
            checked_at=checked_at, overall_status=STATUS_MISMATCH, tickers=[], amzn=None,
            alpaca_unreachable=True, alpaca_error=str(e),
        )

    try:
        local_positions = derive_local_open_positions(conn)
        alpaca_positions = {p.symbol: p for p in trading_client.get_positions(client)}

        local_open_order_counts: Dict[str, int] = {}
        for row in get_orders_in_nonterminal_state(conn):
            local_open_order_counts[row["ticker"]] = local_open_order_counts.get(row["ticker"], 0) + 1

        alpaca_open_order_counts: Dict[str, int] = {}
        for o in trading_client.get_open_orders(client):
            alpaca_open_order_counts[o.symbol] = alpaca_open_order_counts.get(o.symbol, 0) + 1
    except Exception as e:
        return ReconciliationReport(
            checked_at=checked_at, overall_status=STATUS_MISMATCH, tickers=[], amzn=None,
            alpaca_unreachable=True, alpaca_error=str(e),
        )

    all_tickers = (
        set(local_positions) | set(alpaca_positions)
        | set(local_open_order_counts) | set(alpaca_open_order_counts)
    )

    ticker_results: List[TickerReconciliation] = []
    for ticker in sorted(all_tickers):
        reasons: List[str] = []
        local_pos = local_positions.get(ticker)
        alpaca_pos = alpaca_positions.get(ticker)
        local_qty = local_pos["qty"] if local_pos else None
        local_avg_price = local_pos["avg_price"] if local_pos else None
        alpaca_qty = float(alpaca_pos.qty) if alpaca_pos is not None else None
        alpaca_avg_price = float(alpaca_pos.avg_entry_price) if alpaca_pos is not None else None
        local_open_order_count = local_open_order_counts.get(ticker, 0)
        alpaca_open_order_count = alpaca_open_order_counts.get(ticker, 0)

        status = STATUS_MATCHED

        if local_pos is not None and alpaca_pos is None:
            status = STATUS_MISMATCH
            reasons.append("local position open, no matching Alpaca position")
        elif local_pos is None and alpaca_pos is not None:
            status = STATUS_MISMATCH
            reasons.append("Alpaca position with no local order history")
        elif local_pos is not None and alpaca_pos is not None:
            if abs(local_qty - alpaca_qty) > QTY_TOLERANCE:
                status = STATUS_MISMATCH
                reasons.append(f"quantity divergence: local={local_qty} alpaca={alpaca_qty}")
            else:
                if local_open_order_count != alpaca_open_order_count:
                    status = STATUS_WARNING
                    reasons.append(
                        f"open order count divergence: local={local_open_order_count} "
                        f"alpaca={alpaca_open_order_count} - run trading/reconcile.py "
                        "reconciliation to resolve"
                    )
                if (
                    local_avg_price is not None and alpaca_avg_price
                    and abs(local_avg_price - alpaca_avg_price) / alpaca_avg_price > AVG_PRICE_DIVERGENCE_PCT
                ):
                    status = STATUS_WARNING
                    reasons.append(f"avg price divergence: local={local_avg_price} alpaca={alpaca_avg_price}")

        ticker_results.append(TickerReconciliation(
            ticker=ticker, status=status, reasons=reasons,
            local_qty=local_qty, alpaca_qty=alpaca_qty,
            local_avg_price=local_avg_price, alpaca_avg_price=alpaca_avg_price,
            local_open_order_count=local_open_order_count, alpaca_open_order_count=alpaca_open_order_count,
        ))

    overall_status = _worst_status([r.status for r in ticker_results]) if ticker_results else STATUS_MATCHED
    amzn = next((r for r in ticker_results if r.ticker == AMZN_TICKER), None)

    return ReconciliationReport(
        checked_at=checked_at, overall_status=overall_status, tickers=ticker_results, amzn=amzn,
        alpaca_unreachable=False, alpaca_error=None,
    )


def ensure_schema(conn) -> None:
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()


def record_check(conn, report: ReconciliationReport) -> int:
    ensure_schema(conn)
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO {TABLE_NAME} (checked_at, overall_status, report_json) VALUES (?, ?, ?)",
        (report.checked_at, report.overall_status, json.dumps(dataclasses.asdict(report), default=str)),
    )
    conn.commit()
    return cur.lastrowid


def load_latest_check(conn) -> Optional[dict]:
    ensure_schema(conn)
    row = conn.execute(
        f"SELECT report_json FROM {TABLE_NAME} ORDER BY checked_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["report_json"])
