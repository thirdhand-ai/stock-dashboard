"""Demonstrate Phase 2 (indicators + signal engine) against real historical
data already pulled into SQLite by the Phase 1 ingestion layer.

Usage:
    python -m scripts.verify_phase2
    python -m scripts.verify_phase2 --tickers AAPL MSFT NVDA
"""
import argparse
import logging

from config.settings import WATCHLIST
from db.database import db_session
from indicators.technical import compute_indicators_for_ticker
from signals.engine import score_indicators

logging.basicConfig(level=logging.WARNING)


def fmt(v, digits=2):
    return "n/a" if v is None else f"{v:.{digits}f}"


def print_ticker_report(ticker, conn):
    print(f"\n{'=' * 60}\n{ticker}\n{'=' * 60}")

    indicators = compute_indicators_for_ticker(conn, ticker)
    if not indicators.ok:
        print(f"  INDICATORS: not available - {indicators.reason}")
        return

    print(f"  As of {indicators.latest_date} | Close: {fmt(indicators.close)}")
    print("  --- Indicators ---")
    print(f"  RSI(14):          {fmt(indicators.rsi)}")
    print(f"  MACD/Signal/Hist: {fmt(indicators.macd)} / {fmt(indicators.macd_signal)} / {fmt(indicators.macd_hist)}")
    print(f"  Bollinger L/M/U:  {fmt(indicators.bb_lower)} / {fmt(indicators.bb_mid)} / {fmt(indicators.bb_upper)}")
    print(f"  ADX(14):          {fmt(indicators.adx)}")
    print(f"  50-day MA:        {fmt(indicators.sma_50)}")
    print(f"  Volume / 20d avg: {int(indicators.volume):,} / {int(indicators.volume_avg_20):,} (ratio {fmt(indicators.volume_ratio)}x)")

    score = score_indicators(indicators)
    print("\n  --- Raw composite score (unconditional - counts every fired condition) ---")
    print(f"  SCORE: {score.score}/100")
    print("\n  --- Sequential qualification chain ---")
    print(f"  trend_confirmed={score.trend_confirmed}  momentum_confirmed={score.momentum_confirmed}  volume_confirmed={score.volume_confirmed}")
    print(f"  highest_confirmed_stage={score.highest_confirmed_stage}")
    print("  Breakdown:")
    for c in score.conditions:
        state = "FIRED" if c.fired else "no"
        print(
            f"    [{c.layer:9s}] {c.name:28s} {state:5s} "
            f"points={c.points_awarded:>5.1f}/{c.points_available:<5.1f}  values={c.values}"
        )


def main():
    parser = argparse.ArgumentParser(description="Verify Phase 2 indicators + signal engine on real data")
    parser.add_argument("--tickers", nargs="+", default=WATCHLIST[:2])
    args = parser.parse_args()

    with db_session() as conn:
        for ticker in args.tickers:
            print_ticker_report(ticker, conn)


if __name__ == "__main__":
    main()
