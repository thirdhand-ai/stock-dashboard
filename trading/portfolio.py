"""Portfolio analytics for Phase 7 - equity/cash/exposure/P&L/position
weights, built from the real Alpaca paper account (read-only) plus locally
persisted paper_orders / portfolio_snapshots.

Never fabricates a number: realized P&L only reflects orders this system
actually saw filled and reconciled (see trading/reconcile.py), and the
equity chart only ever shows snapshots that were actually captured via
capture_snapshot() - there is no backfill/interpolation of history before
snapshots exist.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd

from db.price_repository import load_price_history
from db.trading_repository import insert_portfolio_snapshot, load_filled_orders_for_ticker, load_portfolio_snapshots
from indicators.technical import compute_indicators_for_ticker
from signals.engine import score_indicators


@dataclass
class AccountSummary:
    equity: float
    cash: float
    buying_power: float
    long_market_value: float
    invested_exposure_pct: float
    unrealized_pl: float
    open_position_count: int


@dataclass
class PositionView:
    ticker: str
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    weight_pct: float
    unrealized_pl: float
    unrealized_pl_pct: float
    signal_score: Optional[float]
    signal_stage: Optional[str]


@dataclass
class ClosedTrade:
    ticker: str
    entry_filled_at: Optional[str]
    exit_filled_at: Optional[str]
    entry_price: float
    exit_price: float
    qty: float
    realized_pnl: float
    realized_pnl_pct: float


def build_portfolio_view(conn, client) -> Tuple[AccountSummary, List[PositionView]]:
    """One combined Alpaca read: account + all open positions, plus the
    current signal score/stage for each held ticker (same computation the
    watchlist view uses)."""
    account = client.get_account()
    positions = client.get_all_positions()

    equity = float(account.equity)
    long_market_value = sum(float(p.market_value) for p in positions)
    unrealized_pl = sum(float(p.unrealized_pl) for p in positions)

    summary = AccountSummary(
        equity=equity,
        cash=float(account.cash),
        buying_power=float(account.buying_power),
        long_market_value=long_market_value,
        invested_exposure_pct=(long_market_value / equity) if equity else 0.0,
        unrealized_pl=unrealized_pl,
        open_position_count=len(positions),
    )

    position_views = []
    for p in positions:
        market_value = float(p.market_value)
        signal_score = None
        signal_stage = None
        indicators = compute_indicators_for_ticker(conn, p.symbol)
        if indicators.ok:
            score = score_indicators(indicators)
            signal_score = score.score
            signal_stage = score.highest_confirmed_stage

        position_views.append(PositionView(
            ticker=p.symbol,
            qty=float(p.qty),
            avg_entry_price=float(p.avg_entry_price),
            current_price=float(p.current_price),
            market_value=market_value,
            weight_pct=(market_value / equity) if equity else 0.0,
            unrealized_pl=float(p.unrealized_pl),
            unrealized_pl_pct=float(p.unrealized_plpc) * 100,
            signal_score=signal_score,
            signal_stage=signal_stage,
        ))

    return summary, position_views


def compute_closed_trades(conn) -> List[ClosedTrade]:
    """Match filled entry/exit orders per ticker into closed round-trip
    trades. Long-only, no averaging down, no duplicate positions (see
    trading/config.py's RiskConfig) means each ticker has at most one open
    position at a time locally, so a simple sequential entry->exit pairing
    is exact - no FIFO/LIFO ambiguity to resolve."""
    tickers = [
        row["ticker"] for row in
        conn.execute("SELECT DISTINCT ticker FROM paper_orders WHERE status = 'filled'").fetchall()
    ]

    trades: List[ClosedTrade] = []
    for ticker in tickers:
        filled = load_filled_orders_for_ticker(conn, ticker)
        pending_entry = None
        for _, row in filled.iterrows():
            if row["intent"] == "entry":
                pending_entry = row
            elif row["intent"] == "exit" and pending_entry is not None:
                if row["filled_avg_price"] is None or pending_entry["filled_avg_price"] is None:
                    pending_entry = None
                    continue
                entry_price = float(pending_entry["filled_avg_price"])
                exit_price = float(row["filled_avg_price"])
                qty = float(row["filled_qty"]) if row["filled_qty"] is not None else 0.0
                pnl = (exit_price - entry_price) * qty
                pnl_pct = ((exit_price / entry_price) - 1.0) * 100 if entry_price else 0.0
                trades.append(ClosedTrade(
                    ticker=ticker,
                    entry_filled_at=pending_entry["filled_at"],
                    exit_filled_at=row["filled_at"],
                    entry_price=entry_price,
                    exit_price=exit_price,
                    qty=qty,
                    realized_pnl=pnl,
                    realized_pnl_pct=pnl_pct,
                ))
                pending_entry = None

    return trades


def compute_realized_pnl(conn) -> float:
    return sum(t.realized_pnl for t in compute_closed_trades(conn))


def compute_win_loss_summary(conn) -> Dict[str, int]:
    trades = compute_closed_trades(conn)
    return {
        "completed_trades": len(trades),
        "wins": sum(1 for t in trades if t.realized_pnl > 0),
        "losses": sum(1 for t in trades if t.realized_pnl < 0),
        "flat": sum(1 for t in trades if t.realized_pnl == 0),
    }


def capture_snapshot(conn, client) -> int:
    """Persist one point-in-time portfolio snapshot from the real Alpaca
    paper account. Call explicitly (CLI/scheduled) - never implicit from
    just reading the dashboard."""
    summary, _ = build_portfolio_view(conn, client)
    return insert_portfolio_snapshot(
        conn,
        equity=summary.equity,
        cash=summary.cash,
        buying_power=summary.buying_power,
        long_market_value=summary.long_market_value,
        invested_exposure_pct=summary.invested_exposure_pct,
        unrealized_pl=summary.unrealized_pl,
        open_position_count=summary.open_position_count,
    )


def get_equity_curve(conn) -> pd.DataFrame:
    """Locally captured portfolio_snapshots only - never fabricated/backfilled."""
    return load_portfolio_snapshots(conn)


def get_benchmark_series(conn, ticker: str = "SPY") -> Optional[pd.DataFrame]:
    """SPY close price history from the local prices table, if any has been
    ingested. Returns None (not an empty/fabricated frame) when unavailable,
    so callers can show an explicit "insufficient data" message rather than
    a misleading flat/empty chart."""
    df = load_price_history(conn, ticker)
    if df.empty:
        return None
    return df[["date", "close"]]
