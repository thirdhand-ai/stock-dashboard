"""Cross-sectional robustness (Phase 9 spec item 10) - is apparent strategy
performance broad-based or concentrated in a handful of tickers?
"""
from typing import Dict

import pandas as pd

from strategy_lab.analysis import per_ticker_bucket_return


def robustness_summary(pooled_signal_table: pd.DataFrame, bucket_label: str, min_score: float, max_score: float, horizon: int, min_obs_per_ticker: int = 5) -> dict:
    per_ticker = per_ticker_bucket_return(pooled_signal_table, bucket_label, horizon, min_score, max_score)
    per_ticker = per_ticker[per_ticker["n"] >= min_obs_per_ticker]
    if per_ticker.empty:
        return {"n_tickers_with_data": 0}

    positive = per_ticker[per_ticker["mean_return_pct"] > 0]
    total_positive_return_sum = per_ticker.loc[per_ticker["mean_return_pct"] > 0, "mean_return_pct"].sum()
    top5_share = None
    if total_positive_return_sum > 0:
        top5_sum = per_ticker.sort_values("mean_return_pct", ascending=False).head(5)["mean_return_pct"].clip(lower=0).sum()
        top5_share = round(float(top5_sum / total_positive_return_sum) * 100, 1)

    return {
        "n_tickers_with_data": int(len(per_ticker)),
        "median_ticker_mean_return_pct": round(float(per_ticker["mean_return_pct"].median()), 3),
        "pct_tickers_positive": round(float((per_ticker["mean_return_pct"] > 0).mean() * 100), 1),
        "best_5": per_ticker.head(5)[["ticker", "n", "mean_return_pct"]].to_dict("records"),
        "worst_5": per_ticker.tail(5)[["ticker", "n", "mean_return_pct"]].to_dict("records"),
        "top5_share_of_positive_return_pct": top5_share,
        "concentration_flag": bool(top5_share is not None and top5_share >= 60.0),
    }
