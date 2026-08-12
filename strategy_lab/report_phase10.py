"""Phase 10 orchestrator: regime-gated strategy research (Part B).

Reuses the Phase 9 research cache/universe/data pipeline directly - never
redownloads the ~115k-observation dataset (B5), never touches production
config (verified via strategy_lab.production_guard before/after), and never
executes an order (structurally verified in tests/test_strategy_lab.py's
safety tests, extended for Phase 10).
"""
import logging
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path

from config.settings import BASE_DIR

from strategy_lab import analysis, phase10_events, regime_stats
from strategy_lab.backtest_universe import IDEALIZED_EXECUTION, REASONABLE_EXECUTION, equal_weight_strategy_curve
from strategy_lab.data import RESEARCH_SOURCE, coverage_summary, fetch_and_cache_universe, load_universe_prices
from strategy_lab.diagnostics import bootstrap_mean_diff_ci, drawdown_episodes, max_favorable_adverse_excursion
from strategy_lab.events import HORIZONS, build_universe_event_returns
from strategy_lab.phase10_backtest import run_universe_variant_backtests
from strategy_lab.phase10_events import detect_ticker_events_with_features
from strategy_lab.phase10_experiments import ALL_VARIANTS, BULLISH_LABEL
from strategy_lab.phase10_robustness import cross_stock_robustness_summary, holding_period_summary, year_by_year_from_curve
from strategy_lab.production_guard import diff_against_saved
from strategy_lab.regime_history import compute_historical_regime_series
from strategy_lab.report import load_cache as load_phase9_cache
from strategy_lab.universe import PRIMARY_BENCHMARK, RESEARCH_UNIVERSE, SECONDARY_BENCHMARK

logger = logging.getLogger(__name__)

CACHE_DIR = BASE_DIR / "data" / "research_cache"
CACHE_FILE = CACHE_DIR / "phase10_results.pkl"

MFE_MAE_WINDOWS = (5, 20, 60)
FAILURE_HORIZON_COL = "net_return_reasonable_20d"


def run_full_phase10_study(conn, tickers=None) -> dict:
    tickers = list(tickers or RESEARCH_UNIVERSE)
    t0 = time.time()
    timings = {}

    production_diff_before = diff_against_saved()

    # B5: reuse Phase 9 cache, only fetch genuinely missing data.
    all_tickers = tickers + [PRIMARY_BENCHMARK, SECONDARY_BENCHMARK]
    fetch_report = fetch_and_cache_universe(conn, all_tickers, years=5)
    prices_by_ticker = load_universe_prices(conn, all_tickers)
    coverage = coverage_summary(prices_by_ticker)
    phase9_baseline_cache = load_phase9_cache()

    # B3/B4: point-in-time regime series + distribution/duration/transitions.
    t = time.time()
    regime_series = compute_historical_regime_series(conn)
    dist = regime_stats.regime_distribution(regime_series)
    durations = regime_stats.regime_duration_stats(regime_series)
    matrix = regime_stats.transition_matrix(regime_series)
    freq = regime_stats.transition_frequency(regime_series)
    selective = regime_stats.is_bullish_sufficiently_selective(dist)
    timings["regime_audit"] = time.time() - t

    # B7: regime-tagged score_cross_70 event study.
    t = time.time()
    all_events, event_failures = build_universe_event_returns(conn, tickers, horizons=HORIZONS)
    score_cross = all_events[all_events["event_type"] == "score_cross_70"] if not all_events.empty else all_events
    tagged = phase10_events.tag_events_with_regime(score_cross, regime_series)
    all_ev, bullish_ev, non_bullish_ev = phase10_events.split_by_regime(tagged)

    spy_df = prices_by_ticker.get(PRIMARY_BENCHMARK)
    spy_lookup = phase10_events.spy_entry_aligned_returns(spy_df) if spy_df is not None and not spy_df.empty else None
    event_study = {}
    if spy_lookup is not None:
        for label, df in (("all", all_ev), ("bullish", bullish_ev), ("non_bullish", non_bullish_ev)):
            event_study[label] = {
                "n_events": int(len(df)),
                "idealized": phase10_events.event_stats_with_benchmark(df, spy_lookup, friction="idealized"),
                "reasonable": phase10_events.event_stats_with_benchmark(df, spy_lookup, friction="reasonable"),
            }
    timings["event_study"] = time.time() - t

    # B15: regime-transition risk on bullish entries.
    t = time.time()
    bullish_ev = bullish_ev.copy()
    bullish_ev["days_to_regime_end"] = bullish_ev["date"].apply(lambda d: phase10_events.time_to_regime_end(regime_series, d))
    ttl_bucketed = phase10_events.bucket_by_time_to_regime_end(bullish_ev)
    ttl_summary = (ttl_bucketed.groupby("ttl_bucket")[FAILURE_HORIZON_COL].agg(["count", "mean", "median"]).reset_index()
                   if not ttl_bucketed.empty else None)
    timings["transition_risk"] = time.time() - t

    # B16: failed-signal analysis (bullish-regime events only, universe-wide).
    t = time.time()
    feature_frames = []
    for tk in tickers:
        ev_feat, price_df, reason = detect_ticker_events_with_features(conn, tk)
        if reason is not None or ev_feat is None or ev_feat.empty:
            continue
        score_cross_feat = ev_feat[ev_feat["event_type"] == "score_cross_70"]
        if score_cross_feat.empty:
            continue
        from strategy_lab.events import compute_event_forward_returns
        fwd = compute_event_forward_returns(score_cross_feat, price_df, horizons=HORIZONS)
        if fwd.empty:
            continue
        fwd["regime"] = fwd["date"].map(regime_series.set_index("date")["label"])
        feature_frames.append(fwd[fwd["regime"] == BULLISH_LABEL])
    import pandas as pd
    bullish_with_features = pd.concat(feature_frames, ignore_index=True) if feature_frames else pd.DataFrame()
    failed_signal_summary = None
    if not bullish_with_features.empty:
        classified = phase10_events.classify_success_failure(bullish_with_features, horizon_col=FAILURE_HORIZON_COL)
        failed_signal_summary = {
            "outcome_counts": classified["outcome"].value_counts().to_dict(),
            "feature_comparison": phase10_events.compare_success_vs_failure_features(
                classified, phase10_events.INDICATOR_SNAPSHOT_COLS + ["score"]
            ).to_dict("records"),
        }
    timings["failed_signal"] = time.time() - t

    # B17: MFE/MAE, CONTROL-all vs bullish-regime score_cross_70 entries.
    t = time.time()
    mfe_mae_rows = []
    for group_label, df in (("all_events", all_ev), ("bullish_events", bullish_ev)):
        for window in MFE_MAE_WINDOWS:
            mfes, maes = [], []
            for tk, sub in df.groupby("ticker"):
                price_df = prices_by_ticker.get(tk)
                if price_df is None or price_df.empty:
                    continue
                price_df_sorted = price_df.sort_values("date").reset_index(drop=True)
                for _, ev in sub.iterrows():
                    entry_idx_matches = price_df_sorted.index[price_df_sorted["date"] == ev["entry_date"]]
                    if len(entry_idx_matches) == 0:
                        continue
                    entry_idx = int(entry_idx_matches[0])
                    entry_price = price_df_sorted.at[entry_idx, "open"]
                    result = max_favorable_adverse_excursion(price_df_sorted, entry_idx - 1, entry_price, window)
                    if result:
                        mfes.append(result["mfe_pct"])
                        maes.append(result["mae_pct"])
            if mfes:
                mfe_mae_rows.append({
                    "group": group_label, "window_days": window, "n": len(mfes),
                    "mean_mfe_pct": round(sum(mfes) / len(mfes), 2), "mean_mae_pct": round(sum(maes) / len(maes), 2),
                })
    mfe_mae_summary = pd.DataFrame(mfe_mae_rows)
    timings["mfe_mae"] = time.time() - t

    # B13/B14: stage & score-bucket diagnostics inside bullish regime, reusing Phase 9's pooled table.
    t = time.time()
    bullish_bucket_scores, bullish_bucket_stages = None, None
    if phase9_baseline_cache.get("pooled_signal_table") is not None:
        pooled = phase9_baseline_cache["pooled_signal_table"]
        pooled_tagged = pooled.copy()
        pooled_tagged["regime"] = pooled_tagged["date"].map(regime_series.set_index("date")["label"])
        bullish_pooled = pooled_tagged[pooled_tagged["regime"] == BULLISH_LABEL]
        bullish_bucket_scores = analysis.bucket_by_score_pooled(bullish_pooled, HORIZONS)
        bullish_bucket_stages = analysis.bucket_by_stage_pooled(bullish_pooled, HORIZONS)
    timings["bullish_bucket_stage"] = time.time() - t

    # B8-B12, B18: backtest CONTROL/A/B x idealized/reasonable, across the universe.
    t = time.time()
    variant_results = {}
    for variant in ALL_VARIANTS:
        variant_results[variant.name] = {}
        for friction_label, execution in (("idealized", IDEALIZED_EXECUTION), ("reasonable", REASONABLE_EXECUTION)):
            results, failures = run_universe_variant_backtests(conn, tickers, regime_series, variant, execution)
            curve = equal_weight_strategy_curve(results)
            variant_results[variant.name][friction_label] = {
                "results": results,
                "failures": failures,
                "curve": curve,
                "cumulative_return_pct": float((curve.iloc[-1] / curve.iloc[0] - 1) * 100) if not curve.empty else None,
                "drawdown_episodes": drawdown_episodes(curve).head(5) if not curve.empty else None,
                "robustness": cross_stock_robustness_summary(results),
                "holding_period": holding_period_summary(results),
                "year_by_year": year_by_year_from_curve(curve),
            }
    timings["backtest_variants"] = time.time() - t

    # B19: statistical robustness - paired bootstrap across tickers (CONTROL vs A vs B, reasonable friction)
    # and bullish-vs-non-bullish event-return bootstrap.
    t = time.time()
    from strategy_lab.backtest_universe import per_ticker_result_table
    control_table = per_ticker_result_table(variant_results["original_frozen_strategy"]["reasonable"]["results"]).set_index("ticker")
    a_table = per_ticker_result_table(variant_results["bullish_entry_only"]["reasonable"]["results"]).set_index("ticker")
    b_table = per_ticker_result_table(variant_results["bullish_entry_and_exit"]["reasonable"]["results"]).set_index("ticker")
    common_idx = control_table.index.intersection(a_table.index).intersection(b_table.index)
    bootstrap_results = {
        "control_vs_a": bootstrap_mean_diff_ci(a_table.loc[common_idx, "total_return_pct"], control_table.loc[common_idx, "total_return_pct"]),
        "control_vs_b": bootstrap_mean_diff_ci(b_table.loc[common_idx, "total_return_pct"], control_table.loc[common_idx, "total_return_pct"]),
        "bullish_vs_nonbullish_event_20d": bootstrap_mean_diff_ci(
            bullish_ev["net_return_reasonable_20d"], non_bullish_ev["net_return_reasonable_20d"],
        ),
    }
    timings["statistical_robustness"] = time.time() - t

    production_diff_after = diff_against_saved()

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tickers": tickers,
        "coverage": coverage,
        "fetch_report_status_counts": {s: sum(1 for r in fetch_report.values() if r["status"] == s) for s in ("cached", "fetched", "failed")},
        "regime_series": regime_series,
        "regime_distribution": dist,
        "regime_durations": durations,
        "regime_transition_matrix": matrix,
        "regime_transition_frequency": freq,
        "bullish_sufficiently_selective": selective,
        "event_study": event_study,
        "event_failures": event_failures,
        "ttl_summary": ttl_summary,
        "failed_signal_summary": failed_signal_summary,
        "mfe_mae_summary": mfe_mae_summary,
        "bullish_bucket_scores": bullish_bucket_scores,
        "bullish_bucket_stages": bullish_bucket_stages,
        "variant_results": variant_results,
        "bootstrap_results": bootstrap_results,
        "production_diff_before": production_diff_before,
        "production_diff_after": production_diff_after,
        "timings": timings,
        "total_elapsed_sec": time.time() - t0,
    }
    return results


def save_cache(results: dict) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(results, f)
    return CACHE_FILE


def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    with open(CACHE_FILE, "rb") as f:
        return pickle.load(f)
