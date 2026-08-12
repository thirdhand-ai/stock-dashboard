"""Relative-strength / sector context - RESEARCH ONLY.

Compares each ticker's trailing returns against SPY and (for tickers mapped
to "Technology") QQQ as a documented sector-proxy - see research/config.py's
RelativeStrengthConfig for the sector map and the honest caveat that QQQ is
an approximation, not a real sector ETF. No new external data source is
added; everything here reads price history already stored by the existing
ingestion layer (db.price_repository.load_price_history).
"""
from dataclasses import dataclass, field
from typing import Dict, Optional

import pandas as pd

from db.price_repository import load_price_history
from research.config import DEFAULT_RELATIVE_STRENGTH_CONFIG, RelativeStrengthConfig

TRADING_DAYS_PER_YEAR = 252


@dataclass
class RelativeStrengthResult:
    ticker: str
    ok: bool
    reason: Optional[str] = None
    sector: Optional[str] = None
    returns_pct: Dict[str, float] = field(default_factory=dict)
    benchmark_returns_pct: Dict[str, float] = field(default_factory=dict)
    relative_strength_pct: Dict[str, float] = field(default_factory=dict)
    sector_benchmark: Optional[str] = None
    sector_benchmark_returns_pct: Dict[str, float] = field(default_factory=dict)
    relative_strength_vs_sector_pct: Dict[str, float] = field(default_factory=dict)
    realized_vol_annualized: Optional[float] = None
    drawdown_pct: Optional[float] = None


def _load_close_series(conn, ticker: str) -> Optional[pd.Series]:
    df = load_price_history(conn, ticker)
    if df.empty:
        return None
    df = df.sort_values("date").reset_index(drop=True)
    return df.set_index("date")["close"]


def _trailing_return_pct(close: pd.Series, trading_days: int) -> Optional[float]:
    if len(close) <= trading_days:
        return None
    return float((close.iloc[-1] / close.iloc[-1 - trading_days] - 1.0) * 100.0)


def compute_relative_strength(
    conn, ticker: str, config: RelativeStrengthConfig = DEFAULT_RELATIVE_STRENGTH_CONFIG,
) -> RelativeStrengthResult:
    ticker_close = _load_close_series(conn, ticker)
    if ticker_close is None or len(ticker_close) < 2:
        return RelativeStrengthResult(ticker=ticker, ok=False, reason="no price history for this ticker")

    benchmark_close = _load_close_series(conn, config.primary_benchmark)
    if benchmark_close is None:
        return RelativeStrengthResult(
            ticker=ticker, ok=False,
            reason=f"no price history stored for benchmark {config.primary_benchmark}",
        )

    sector = config.sector_map.get(ticker)
    sector_benchmark_ticker = config.sector_benchmark_proxy.get(sector) if sector else None
    sector_close = _load_close_series(conn, sector_benchmark_ticker) if sector_benchmark_ticker else None

    returns_pct, benchmark_returns_pct, relative_strength_pct = {}, {}, {}
    sector_benchmark_returns_pct, relative_strength_vs_sector_pct = {}, {}

    for label, trading_days in config.windows_trading_days.items():
        t_ret = _trailing_return_pct(ticker_close, trading_days)
        b_ret = _trailing_return_pct(benchmark_close, trading_days)
        if t_ret is not None:
            returns_pct[label] = round(t_ret, 2)
        if b_ret is not None:
            benchmark_returns_pct[label] = round(b_ret, 2)
        if t_ret is not None and b_ret is not None:
            relative_strength_pct[label] = round(t_ret - b_ret, 2)

        if sector_close is not None:
            s_ret = _trailing_return_pct(sector_close, trading_days)
            if s_ret is not None:
                sector_benchmark_returns_pct[label] = round(s_ret, 2)
            if t_ret is not None and s_ret is not None:
                relative_strength_vs_sector_pct[label] = round(t_ret - s_ret, 2)

    vol_window_returns = ticker_close.pct_change().tail(config.volatility_window_days).dropna()
    realized_vol_annualized = (
        float(vol_window_returns.std() * (TRADING_DAYS_PER_YEAR ** 0.5)) if len(vol_window_returns) >= 2 else None
    )

    dd_window = ticker_close.tail(config.drawdown_window_days)
    running_max = dd_window.cummax()
    drawdown_pct = float((dd_window.iloc[-1] / running_max.iloc[-1] - 1.0) * 100.0) if len(dd_window) else None

    return RelativeStrengthResult(
        ticker=ticker, ok=True, sector=sector, returns_pct=returns_pct,
        benchmark_returns_pct=benchmark_returns_pct, relative_strength_pct=relative_strength_pct,
        sector_benchmark=sector_benchmark_ticker, sector_benchmark_returns_pct=sector_benchmark_returns_pct,
        relative_strength_vs_sector_pct=relative_strength_vs_sector_pct,
        realized_vol_annualized=realized_vol_annualized, drawdown_pct=drawdown_pct,
    )
