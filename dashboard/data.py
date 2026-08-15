"""Dashboard data-access layer.

Every function here reuses the existing Phase 1-3 modules (db, indicators,
signals, backtest) rather than re-deriving anything. Functions are split
into:
  - `_load_*(conn, ...)`: plain, synchronous, DB-connection-in / dict-or-
    DataFrame-out. No Streamlit dependency, so these are unit-testable with
    a throwaway SQLite connection.
  - `get_*(...)`: thin `st.cache_data`-wrapped callers that open their own
    short-lived DB connection and delegate to the `_load_*` function.

No API credentials are read or exposed here - all data comes from the local
SQLite database populated by the existing ingestion layer.
"""
import dataclasses
import logging
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd
import streamlit as st

from backtest.config import DEFAULT_EXECUTION, DEFAULT_RULES, DEFAULT_WF_CONFIG
from backtest.runner import BacktestResult, run_backtest
from backtest.walkforward import WalkForwardResult, run_walk_forward
from config.settings import WATCHLIST
from db.alert_repository import load_alert_history
from db.database import db_session
from db.price_repository import get_latest_fetched_at, load_price_history, resolve_source
from db.run_history_repository import (
    STATUS_FAILED,
    STATUS_PARTIAL_FAILURE,
    load_run_history,
)
from db.trading_repository import load_order_history
from indicators.technical import IndicatorResult, MIN_REQUIRED_ROWS, compute_indicators, enrich_with_indicators
from signals.engine import SignalScore, score_indicators
from research.fundamentals import FundamentalMetrics, load_fundamental_metrics
from research.historical_quality import HistoricalQualityData, build_score_return_table
from research.ranking import TickerResearchRow, build_research_ranking
from research.regime import RegimeResult, compute_market_regime
from research.relative_strength import RelativeStrengthResult, compute_relative_strength
from research.sentiment import NewsSentimentSummary, compute_news_sentiment_summary
from trading import client as trading_client
from trading import portfolio as trading_portfolio

logger = logging.getLogger(__name__)

WATCHLIST_TTL_SECONDS = 300      # price/indicator/score data - refresh every 5 min at most
BACKTEST_TTL_SECONDS = 3600      # backtests only change when new price data is ingested
ALERTS_TTL_SECONDS = 60
PAPER_PORTFOLIO_TTL_SECONDS = 60  # live Alpaca account/positions - short TTL, but capped to limit API calls
RESEARCH_TTL_SECONDS = 900       # fundamentals/news/regime change slowly - cache generously to limit API/CPU use
STRATEGY_LAB_TTL_SECONDS = 3600  # Phase 9 research cache - refreshed by an explicit CLI run, not page load


@dataclass
class WatchlistRow:
    ticker: str
    ok: bool
    reason: Optional[str] = None
    last_price: Optional[float] = None
    change: Optional[float] = None
    change_pct: Optional[float] = None
    score: Optional[float] = None
    highest_confirmed_stage: Optional[str] = None
    rsi: Optional[float] = None
    adx: Optional[float] = None
    volume_ratio: Optional[float] = None
    latest_date: Optional[str] = None
    source: Optional[str] = None
    fetched_at: Optional[str] = None            # A10: when this row was actually written, not the market date
    latest_run_status: Optional[str] = None      # A10: status of the most recent scheduled automation run
    is_stale: bool = False                       # A10: True when the most recent scheduled run did not succeed


@dataclass
class PaperPortfolioData:
    """Phase 7: a snapshot of the real Alpaca paper account + local order
    history, for the read-only Paper Portfolio dashboard view. This dataclass
    (and every function that builds it below) never calls anything that
    could submit, modify, or cancel an order - see trading/client.py, whose
    read helpers (get_account/get_positions/get_open_orders) are the only
    Alpaca calls made here."""

    ok: bool
    reason: Optional[str] = None
    summary: Optional[trading_portfolio.AccountSummary] = None
    positions: List[trading_portfolio.PositionView] = field(default_factory=list)
    realized_pnl: float = 0.0
    win_loss: dict = field(default_factory=dict)


@dataclass
class TickerDetail:
    ticker: str
    ok: bool
    reason: Optional[str] = None
    source: Optional[str] = None
    enriched_history: Optional[pd.DataFrame] = None   # full per-row indicator series, for charting
    indicators: Optional[IndicatorResult] = None        # latest-row snapshot
    score: Optional[SignalScore] = None


# --- plain, testable loaders (no Streamlit dependency) ---


def _latest_scheduled_run_status(conn) -> Optional[str]:
    """A10: status of the most recent scheduled automation run - used to
    tell the dashboard whether today's displayed signals came from a run
    that actually refreshed data, without the dashboard ever triggering
    ingestion itself (see get_run_history's docstring)."""
    history = load_run_history(conn, limit=1)
    if history.empty:
        return None
    return history.iloc[0]["status"]


def _load_watchlist_row(conn, ticker: str, latest_run_status: Optional[str] = None) -> WatchlistRow:
    price_df = load_price_history(conn, ticker)
    is_stale = latest_run_status in (STATUS_FAILED, STATUS_PARTIAL_FAILURE)
    if price_df.empty:
        return WatchlistRow(ticker=ticker, ok=False, reason="no price data available")

    source = resolve_source(conn, ticker)
    fetched_at = get_latest_fetched_at(conn, ticker, source=source)
    latest = price_df.iloc[-1]
    prev = price_df.iloc[-2] if len(price_df) >= 2 else None
    change = float(latest["close"] - prev["close"]) if prev is not None else None
    change_pct = float(change / prev["close"] * 100) if prev is not None and prev["close"] else None

    indicators = compute_indicators(price_df, ticker)
    if not indicators.ok:
        return WatchlistRow(
            ticker=ticker,
            ok=False,
            reason=indicators.reason,
            last_price=float(latest["close"]),
            change=change,
            change_pct=change_pct,
            latest_date=str(latest["date"]),
            source=source,
            fetched_at=fetched_at,
            latest_run_status=latest_run_status,
            is_stale=is_stale,
        )

    score = score_indicators(indicators)
    return WatchlistRow(
        ticker=ticker,
        ok=True,
        last_price=indicators.close,
        change=change,
        change_pct=change_pct,
        score=score.score,
        highest_confirmed_stage=score.highest_confirmed_stage,
        rsi=indicators.rsi,
        adx=indicators.adx,
        volume_ratio=indicators.volume_ratio,
        latest_date=indicators.latest_date,
        source=source,
        fetched_at=fetched_at,
        latest_run_status=latest_run_status,
        is_stale=is_stale,
    )


def _load_ticker_detail(conn, ticker: str) -> TickerDetail:
    price_df = load_price_history(conn, ticker)
    if price_df.empty:
        return TickerDetail(ticker=ticker, ok=False, reason="no price data available")

    source = resolve_source(conn, ticker)

    if len(price_df) < MIN_REQUIRED_ROWS:
        return TickerDetail(
            ticker=ticker,
            ok=False,
            reason=f"insufficient history: {len(price_df)} rows, need >= {MIN_REQUIRED_ROWS}",
            source=source,
        )

    enriched = enrich_with_indicators(price_df)
    indicators = compute_indicators(price_df, ticker)
    score = score_indicators(indicators) if indicators.ok else None

    return TickerDetail(
        ticker=ticker,
        ok=True,
        source=source,
        enriched_history=enriched,
        indicators=indicators,
        score=score,
    )


def _load_backtest_report(conn, ticker: str):
    price_df = load_price_history(conn, ticker)
    source = resolve_source(conn, ticker)

    if len(price_df) < MIN_REQUIRED_ROWS:
        return {
            "ticker": ticker,
            "ok": False,
            "reason": f"insufficient history: {len(price_df)} rows, need >= {MIN_REQUIRED_ROWS}",
            "source": source,
        }

    backtest_result = run_backtest(price_df, ticker, source=source or "unknown")
    walk_forward_result = run_walk_forward(price_df, ticker, source=source or "unknown")

    close_prices = price_df.copy()
    close_prices["date"] = pd.to_datetime(close_prices["date"])
    close_prices = close_prices.set_index("date")["close"]

    return {
        "ticker": ticker,
        "ok": True,
        "source": source,
        "n_rows": len(price_df),
        "date_range": (price_df["date"].iloc[0], price_df["date"].iloc[-1]),
        "backtest": backtest_result,
        "walk_forward": walk_forward_result,
        "close_prices": close_prices,
    }


def _load_paper_portfolio(conn) -> PaperPortfolioData:
    try:
        alpaca_client = trading_client.get_client()
        trading_client.verify_paper_environment(alpaca_client)
    except Exception as e:
        return PaperPortfolioData(ok=False, reason=f"Alpaca paper environment unavailable: {e}")

    try:
        summary, positions = trading_portfolio.build_portfolio_view(conn, alpaca_client)
    except Exception as e:
        return PaperPortfolioData(ok=False, reason=f"could not fetch Alpaca account/positions: {e}")

    return PaperPortfolioData(
        ok=True,
        summary=summary,
        positions=positions,
        realized_pnl=trading_portfolio.compute_realized_pnl(conn),
        win_loss=trading_portfolio.compute_win_loss_summary(conn),
    )


# --- cached wrappers (Streamlit-facing) ---


@st.cache_data(ttl=WATCHLIST_TTL_SECONDS, show_spinner=False)
def get_watchlist_overview(tickers: Optional[List[str]] = None) -> List[WatchlistRow]:
    tickers = tickers or WATCHLIST
    with db_session() as conn:
        latest_run_status = _latest_scheduled_run_status(conn)
        return [_load_watchlist_row(conn, t, latest_run_status=latest_run_status) for t in tickers]


@st.cache_data(ttl=WATCHLIST_TTL_SECONDS, show_spinner=False)
def get_ticker_detail(ticker: str) -> TickerDetail:
    with db_session() as conn:
        return _load_ticker_detail(conn, ticker)


@st.cache_data(ttl=BACKTEST_TTL_SECONDS, show_spinner="Running backtest and walk-forward validation...")
def get_backtest_report(ticker: str) -> dict:
    with db_session() as conn:
        return _load_backtest_report(conn, ticker)


@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_alert_history(limit: int = 200) -> pd.DataFrame:
    with db_session() as conn:
        return load_alert_history(conn, limit=limit)


@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_run_history(limit: int = 20) -> pd.DataFrame:
    """Read-only: shows what automation/run_daily.py has done. This
    dashboard never runs the pipeline itself - see dashboard/app.py's
    docstring and the absence of any `import automation` here."""
    with db_session() as conn:
        return load_run_history(conn, limit=limit)


@st.cache_data(ttl=PAPER_PORTFOLIO_TTL_SECONDS, show_spinner=False)
def get_paper_portfolio() -> PaperPortfolioData:
    """Read-only Alpaca paper account + position snapshot. Never places,
    modifies, or cancels an order - see PaperPortfolioData's docstring."""
    with db_session() as conn:
        return _load_paper_portfolio(conn)


@st.cache_data(ttl=PAPER_PORTFOLIO_TTL_SECONDS, show_spinner=False)
def get_paper_order_history(limit: int = 300) -> pd.DataFrame:
    """Read-only: shows what trading/run_paper.py has proposed/submitted.
    This dashboard never runs the paper-trading engine itself - see the
    absence of any `import trading.engine` here."""
    with db_session() as conn:
        return load_order_history(conn, limit=limit)


@st.cache_data(ttl=PAPER_PORTFOLIO_TTL_SECONDS, show_spinner=False)
def get_paper_equity_curve() -> pd.DataFrame:
    with db_session() as conn:
        return trading_portfolio.get_equity_curve(conn)


@st.cache_data(ttl=PAPER_PORTFOLIO_TTL_SECONDS, show_spinner=False)
def get_paper_benchmark_series(ticker: str = "SPY") -> Optional[pd.DataFrame]:
    with db_session() as conn:
        return trading_portfolio.get_benchmark_series(conn, ticker)


def capture_paper_portfolio_snapshot() -> int:
    """Explicit, user-triggered persistence of one portfolio_snapshots row -
    not a trade, just a data capture (see trading/portfolio.py). Called only
    from a button click in dashboard/views/paper_portfolio.py, never on
    page load."""
    with db_session() as conn:
        alpaca_client = trading_client.get_client()
        trading_client.verify_paper_environment(alpaca_client)
        snapshot_id = trading_portfolio.capture_snapshot(conn, alpaca_client)
    get_paper_equity_curve.clear()
    return snapshot_id


def _load_open_position_tickers() -> set:
    """Read-only Alpaca position symbols, for research/ranking.py's
    has_open_position display field only - never used to decide anything
    research computes. Degrades to an empty set (not an error) if Alpaca is
    unavailable, since the research ranking should still render without it."""
    try:
        alpaca_client = trading_client.get_client()
        trading_client.verify_paper_environment(alpaca_client)
        positions = trading_client.get_positions(alpaca_client)
        return {p.symbol for p in positions}
    except Exception as e:
        logger.warning("research: could not fetch open positions for display: %s", e)
        return set()


@st.cache_data(ttl=RESEARCH_TTL_SECONDS, show_spinner="Building research ranking...")
def get_research_ranking(tickers: Optional[List[str]] = None) -> List[TickerResearchRow]:
    tickers = tickers or WATCHLIST
    position_tickers = _load_open_position_tickers()
    with db_session() as conn:
        return build_research_ranking(conn, tickers, position_tickers=position_tickers)


@st.cache_data(ttl=RESEARCH_TTL_SECONDS, show_spinner=False)
def get_market_regime() -> RegimeResult:
    with db_session() as conn:
        return compute_market_regime(conn)


@st.cache_data(ttl=RESEARCH_TTL_SECONDS, show_spinner=False)
def get_ticker_fundamentals(ticker: str) -> FundamentalMetrics:
    with db_session() as conn:
        return load_fundamental_metrics(conn, ticker)


@st.cache_data(ttl=RESEARCH_TTL_SECONDS, show_spinner=False)
def get_ticker_sentiment(ticker: str) -> NewsSentimentSummary:
    with db_session() as conn:
        return compute_news_sentiment_summary(conn, ticker)


@st.cache_data(ttl=RESEARCH_TTL_SECONDS, show_spinner=False)
def get_ticker_relative_strength(ticker: str) -> RelativeStrengthResult:
    with db_session() as conn:
        return compute_relative_strength(conn, ticker)


@st.cache_data(ttl=BACKTEST_TTL_SECONDS, show_spinner="Computing historical signal quality...")
def get_historical_quality(ticker: str) -> HistoricalQualityData:
    with db_session() as conn:
        return build_score_return_table(conn, ticker)


@st.cache_data(ttl=STRATEGY_LAB_TTL_SECONDS, show_spinner=False)
def get_strategy_lab_results() -> dict:
    """Read-only: loads the precomputed Phase 9 research cache from disk
    (see strategy_lab/report.py). Never runs the (multi-minute) large-sample
    study itself - refreshing it is a deliberate, explicit
    `python -m strategy_lab.run_study` action, not something a page load
    should trigger. Returns {} if the study has never been run."""
    from strategy_lab.report import load_cache
    return load_cache()


@st.cache_data(ttl=STRATEGY_LAB_TTL_SECONDS, show_spinner=False)
def get_phase10_results() -> dict:
    """Read-only: loads the precomputed Phase 10 (regime-gated strategy
    research) cache. Refreshed via `python -m strategy_lab.run_phase10_study`,
    never on page load. Returns {} if Phase 10 has never been run."""
    from strategy_lab.report_phase10 import load_cache
    return load_cache()


@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_production_health() -> dict:
    """Phase 11 Strategy Lab B15: latest production automation run status/
    freshness - a plain read of the same run history the Watchlist page's
    staleness banner uses (dashboard/views/watchlist.py). Never triggers
    ingestion."""
    with db_session() as conn:
        history = load_run_history(conn, limit=5)
    if history.empty:
        return {"status": None, "history": history}
    latest = history.iloc[0]
    return {
        "status": latest["status"],
        "started_at": latest["started_at"],
        "finished_at": latest["finished_at"],
        "send_mode": latest["send_mode"],
        "tickers_failed": latest["tickers_failed"],
        "error_summary": latest["error_summary"],
        "history": history,
    }


@st.cache_data(ttl=STRATEGY_LAB_TTL_SECONDS, show_spinner=False)
def get_prospective_summary() -> dict:
    """Phase 11: observation/event/outcome counts for Strategy Lab's
    Prospective Validation section. Read-only against strategy_lab's own
    tables; never recomputes anything."""
    from strategy_lab.outcome_maturation import maturation_summary
    from strategy_lab.prospective import load_observations
    from strategy_lab.prospective_events import load_events

    with db_session() as conn:
        obs = load_observations(conn)
        events = load_events(conn)
        maturation = maturation_summary(conn)

    return {
        "observation_count": len(obs),
        "first_date": obs["observation_date"].min() if not obs.empty else None,
        "last_date": obs["observation_date"].max() if not obs.empty else None,
        "event_count": len(events),
        "event_counts_by_type": events["event_type"].value_counts().to_dict() if not events.empty else {},
        "maturation": maturation,
    }


@st.cache_data(ttl=STRATEGY_LAB_TTL_SECONDS, show_spinner=False)
def get_research_run_history(limit: int = 20) -> pd.DataFrame:
    """Phase 11: research-only automation run history, separate from
    db.run_history_repository's production automation_runs table."""
    from strategy_lab.research_automation import load_research_run_history
    with db_session() as conn:
        return load_research_run_history(conn, limit=limit)


@st.cache_data(ttl=STRATEGY_LAB_TTL_SECONDS, show_spinner=False)
def get_phase11_results() -> dict:
    """Read-only: loads the precomputed Phase 11 (realistic portfolio
    simulation) research cache. Refreshed via
    `python -m strategy_lab.run_phase11_study`, never on page load."""
    from strategy_lab.phase11_report import load_cache
    return load_cache()


def get_amzn_monitor_status():
    """Read-only Alpaca paper-account check for the existing AMZN position -
    never places, modifies, or cancels an order (see strategy_lab.amzn_monitor's
    docstring). Not st.cache_data-wrapped - this should always reflect the
    latest account state when the page loads, like paper_portfolio.py's
    Alpaca reads."""
    from strategy_lab.amzn_monitor import get_amzn_status
    with db_session() as conn:
        return get_amzn_status(conn)


# --- Phase 12: ops (daily report / data quality / reconciliation /
# experiment governance) getters. Same thin-caller-delegates-to-plain-
# function convention as every getter above. ---


@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_ops_daily_report() -> dict:
    """Read-only: builds today's Phase 12 daily research report. Never
    persists it (that's ops/run_daily_report.py's job) - a page load must
    never write to ops_daily_reports."""
    with db_session() as conn:
        from ops.daily_report import build_daily_report, report_to_dict
        return report_to_dict(build_daily_report(conn))


@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_data_quality_report() -> dict:
    """Read-only: checks watchlist price-data freshness/validity directly
    against the `prices` table. Never calls ingestion, never mutates."""
    with db_session() as conn:
        from ops.data_quality import check_watchlist_quality
        report = check_watchlist_quality(conn)
    return dataclasses.asdict(report)


@st.cache_data(ttl=PAPER_PORTFOLIO_TTL_SECONDS, show_spinner=False)
def get_reconciliation_report() -> dict:
    """Read-only comparison of local paper-trading state vs. Alpaca's
    authoritative paper account state. Never submits/cancels/replaces/
    closes anything."""
    from ops.reconciliation import build_reconciliation_report
    with db_session() as conn:
        report = build_reconciliation_report(conn)
    return dataclasses.asdict(report)


@st.cache_data(ttl=STRATEGY_LAB_TTL_SECONDS, show_spinner=False)
def get_experiment_registry() -> pd.DataFrame:
    from ops.experiment_registry import list_experiments
    with db_session() as conn:
        return list_experiments(conn)


@st.cache_data(ttl=STRATEGY_LAB_TTL_SECONDS, show_spinner=False)
def get_experiment_registry_drift() -> dict:
    """Read-only: for every ACTIVE experiment, whether its stored config
    fingerprint still matches the live production config. Never writes
    anything, never auto-retires an experiment."""
    from ops.experiment_registry import check_active_experiments_config_drift
    with db_session() as conn:
        return check_active_experiments_config_drift(conn)


@st.cache_data(ttl=STRATEGY_LAB_TTL_SECONDS, show_spinner=False)
def get_prospective_evidence_status() -> dict:
    """Trading-day-based prospective evidence classification (Phase 12) -
    distinct from get_prospective_summary()'s event-count-based label."""
    from ops.evidence_classification import classify_evidence, count_prospective_trading_days
    with db_session() as conn:
        n_days = count_prospective_trading_days(conn)
    return {"n_prospective_trading_days": n_days, "status": classify_evidence(n_days)}


@st.cache_data(ttl=STRATEGY_LAB_TTL_SECONDS, show_spinner=False)
def get_ops_research_run_history(limit: int = 20) -> pd.DataFrame:
    """Thin re-export identical to get_research_run_history() - kept
    separate so dashboard/views/ops_overview.py doesn't need to import
    strategy_lab.research_automation directly, matching this file's
    existing "dashboard imports, views don't" convention."""
    from strategy_lab.research_automation import load_research_run_history
    with db_session() as conn:
        return load_research_run_history(conn, limit=limit)


@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_prospective_day_ledger(limit_days: int = 90) -> pd.DataFrame:
    """ops.prospective_audit.build_prospective_day_ledger, most recent
    limit_days rows, as a DataFrame for st.dataframe. Read-only; never
    fabricates or backfills a missed day's prediction."""
    from ops.prospective_audit import build_prospective_day_ledger
    with db_session() as conn:
        ledger = build_prospective_day_ledger(conn)
    df = pd.DataFrame([dataclasses.asdict(day) for day in ledger])
    if df.empty:
        return df
    return df.tail(limit_days).reset_index(drop=True)


@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_prospective_audit_summary() -> dict:
    """ops.prospective_audit.prospective_evidence_audit_summary."""
    from ops.prospective_audit import prospective_evidence_audit_summary
    with db_session() as conn:
        return prospective_evidence_audit_summary(conn)


@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_research_cache_completeness_report() -> dict:
    """READ-ONLY snapshot: for each RESEARCH_UNIVERSE ticker, whether
    strategy_lab.cache_integrity.latest_row_is_incomplete(conn, ticker) is
    True RIGHT NOW (a pure SQL check - no Alpaca call), plus the most recent
    rows of research_cache_corrections. NEVER calls
    refresh_incomplete_latest_bars (which makes live Alpaca calls) from a
    dashboard page load."""
    from strategy_lab.cache_integrity import CORRECTIONS_TABLE, ensure_schema, latest_row_is_incomplete
    from strategy_lab.universe import RESEARCH_UNIVERSE

    with db_session() as conn:
        ensure_schema(conn)
        flagged = [t for t in RESEARCH_UNIVERSE if latest_row_is_incomplete(conn, t)]
        corrections_df = pd.read_sql_query(
            f"SELECT * FROM {CORRECTIONS_TABLE} ORDER BY corrected_at DESC, id DESC LIMIT 20", conn,
        )
    return {
        "currently_flagged_incomplete": flagged,
        "recent_corrections": corrections_df.to_dict("records"),
    }


@st.cache_data(ttl=STRATEGY_LAB_TTL_SECONDS, show_spinner=False)
def get_research_run_ticker_errors(run_id: Optional[int] = None, limit: int = 100) -> pd.DataFrame:
    """strategy_lab.research_automation.load_ticker_errors_for_run for the
    given run_id, or the latest research_run_history run if None."""
    from strategy_lab.research_automation import load_research_run_history, load_ticker_errors_for_run

    with db_session() as conn:
        if run_id is None:
            history = load_research_run_history(conn, limit=1)
            if history.empty:
                return pd.DataFrame()
            run_id = int(history.iloc[0]["id"])
        errors = load_ticker_errors_for_run(conn, run_id)
    return errors.head(limit)


@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_long_term_monitoring_summary() -> dict:
    """Phase 15: capture rate, maturity lag, research-job rates, correction
    counts, provenance coverage, and an operational-health label - the one
    getter Component D's dashboard sections read (never call the individual
    ops.prospective_audit.compute_* functions directly from a view)."""
    from ops.prospective_audit import long_term_monitoring_summary
    with db_session() as conn:
        return long_term_monitoring_summary(conn)


@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_correction_audit_report(limit: int = 50) -> pd.DataFrame:
    """Phase 15: one row per (correction, affected_outcome) pair - read-only,
    ops.correction_impact_audit has zero write path at all."""
    from ops.correction_impact_audit import audit_all_corrections
    with db_session() as conn:
        df = audit_all_corrections(conn)
    return df.tail(limit) if not df.empty else df


@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_phase16_monitoring_summary() -> dict:
    """Phase 16: regime distribution/legacy-gap summary, completeness
    breakdown, event-provenance audit, and outcome source-resolution
    breakdown - a strict superset of get_long_term_monitoring_summary()'s
    keys. Read-only; never triggers ingestion, mutation, or backfill."""
    from ops.prospective_audit import phase16_monitoring_summary
    with db_session() as conn:
        return phase16_monitoring_summary(conn)


@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_legacy_regime_gap_report(limit: int = 200) -> pd.DataFrame:
    """Phase 16: per-legacy-observation regime reconstruction detail
    (ops.regime_reconstruction_audit) - informational only, no repair/write
    path anywhere in that module."""
    from ops.regime_reconstruction_audit import audit_legacy_regime_gaps
    with db_session() as conn:
        rows = audit_legacy_regime_gaps(conn)
    df = pd.DataFrame([r.__dict__ for r in rows])
    return df.tail(limit) if not df.empty else df


def clear_all_caches():
    get_watchlist_overview.clear()
    get_ticker_detail.clear()
    get_backtest_report.clear()
    get_alert_history.clear()
    get_run_history.clear()
    get_paper_portfolio.clear()
    get_paper_order_history.clear()
    get_paper_equity_curve.clear()
    get_paper_benchmark_series.clear()
    get_research_ranking.clear()
    get_market_regime.clear()
    get_ticker_fundamentals.clear()
    get_ticker_sentiment.clear()
    get_ticker_relative_strength.clear()
    get_historical_quality.clear()
    get_strategy_lab_results.clear()
    get_phase10_results.clear()
    get_production_health.clear()
    get_prospective_summary.clear()
    get_research_run_history.clear()
    get_phase11_results.clear()
    get_ops_daily_report.clear()
    get_data_quality_report.clear()
    get_reconciliation_report.clear()
    get_experiment_registry.clear()
    get_experiment_registry_drift.clear()
    get_prospective_evidence_status.clear()
    get_ops_research_run_history.clear()
    get_prospective_day_ledger.clear()
    get_prospective_audit_summary.clear()
    get_research_cache_completeness_report.clear()
    get_research_run_ticker_errors.clear()
    get_long_term_monitoring_summary.clear()
    get_correction_audit_report.clear()
    get_phase16_monitoring_summary.clear()
    get_legacy_regime_gap_report.clear()
