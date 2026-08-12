"""B12/B18: cross-stock robustness and holding-period/exposure summaries for
a set of per-ticker BacktestResult objects (one variant's universe run).
"""
from typing import Dict

import pandas as pd

from strategy_lab.backtest_universe import per_ticker_result_table


def cross_stock_robustness_summary(results: Dict[str, "BacktestResult"]) -> dict:
    table = per_ticker_result_table(results)
    if table.empty:
        return {"n_tickers": 0}

    positive = table[table["total_return_pct"] > 0]
    pct_beating_bh = float((table["excess_vs_own_buy_hold_pct"] > 0).mean() * 100)
    total_positive_sum = positive["total_return_pct"].sum()
    top5_share = round(float(table.sort_values("total_return_pct", ascending=False).head(5)["total_return_pct"].clip(lower=0).sum() / total_positive_sum) * 100, 1) if total_positive_sum > 0 else None
    negative = table[table["total_return_pct"] < 0]
    total_negative_sum = negative["total_return_pct"].sum()
    bottom5_share = round(float(table.sort_values("total_return_pct").head(5)["total_return_pct"].clip(upper=0).sum() / total_negative_sum) * 100, 1) if total_negative_sum < 0 else None

    return {
        "n_tickers": int(len(table)),
        "pct_profitable": round(float((table["total_return_pct"] > 0).mean() * 100), 1),
        "mean_return_pct": round(float(table["total_return_pct"].mean()), 2),
        "median_return_pct": round(float(table["total_return_pct"].median()), 2),
        "std_return_pct": round(float(table["total_return_pct"].std()), 2),
        "pct_beating_own_buy_hold": round(pct_beating_bh, 1),
        "best_5": table.head(5)[["ticker", "total_return_pct", "num_trades"]].to_dict("records"),
        "worst_5": table.tail(5)[["ticker", "total_return_pct", "num_trades"]].to_dict("records"),
        "top5_share_of_positive_return_pct": top5_share,
        "bottom5_share_of_negative_return_pct": bottom5_share,
        "concentration_flag": bool(top5_share is not None and top5_share >= 60.0),
    }


def holding_period_summary(results: Dict[str, "BacktestResult"]) -> dict:
    exposures, avg_durations, trade_counts = [], [], []
    for r in results.values():
        stats = r.raw_stats
        if stats is None:
            continue
        exposure = stats.get("Exposure Time [%]")
        if exposure == exposure:  # not NaN
            exposures.append(float(exposure))
        duration = stats.get("Avg. Trade Duration")
        if duration is not None and pd.notna(duration):
            try:
                avg_durations.append(float(pd.Timedelta(duration).days))
            except Exception:
                pass
        trade_counts.append(r.num_trades)

    return {
        "mean_exposure_time_pct": round(sum(exposures) / len(exposures), 1) if exposures else None,
        "mean_avg_trade_duration_days": round(sum(avg_durations) / len(avg_durations), 1) if avg_durations else None,
        "mean_trades_per_ticker": round(sum(trade_counts) / len(trade_counts), 1) if trade_counts else None,
        "total_trades": int(sum(trade_counts)),
    }


def year_by_year_from_curve(curve: pd.Series) -> pd.DataFrame:
    """Calendar-year return of an equity curve indexed by date (e.g. from
    strategy_lab.backtest_universe.equal_weight_strategy_curve)."""
    if curve is None or curve.empty:
        return pd.DataFrame(columns=["year", "return_pct"])
    s = curve.copy()
    s.index = pd.to_datetime(s.index)
    rows = []
    for year, group in s.groupby(s.index.year):
        if len(group) < 2:
            continue
        ret = float(group.iloc[-1] / group.iloc[0] - 1.0) * 100
        rows.append({"year": str(year), "return_pct": round(ret, 2), "n_days": len(group)})
    return pd.DataFrame(rows)
