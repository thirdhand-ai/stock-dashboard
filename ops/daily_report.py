"""Phase 12 Component A: daily research report (read-only).

Every field is a direct read/transform of already-stored data or a pure
boolean/threshold comparison against frozen config
(`backtest.config.DEFAULT_RULES`) - no LLM call, no free-text generation of
market commentary anywhere in this module.

`build_daily_report` performs zero writes - persisting the result is a
separate step (`ops/daily_report_repository.py::upsert_report`), called
only by the CLI (`ops/run_daily_report.py`), never from inside this
function, so tests can call `build_daily_report` against an in-memory DB
with zero side effects.
"""
import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import List, Optional

from backtest.config import DEFAULT_RULES
from backtest.scoring import STAGE_ORDER
from config.settings import WATCHLIST
from db.run_history_repository import load_run_history
from research.regime import compute_market_regime
from signals.engine import SignalScore
from strategy_lab.prospective import load_observations
from strategy_lab.research_automation import load_research_run_history
from trading import client as trading_client
from trading import portfolio as trading_portfolio
from trading.signals_bridge import evaluate_ticker, exit_qualifies


@dataclass
class ProductionHealthSection:
    status: Optional[str]
    started_at: Optional[str]
    tickers_failed: int
    error_summary: Optional[str]
    history: List[dict] = field(default_factory=list)


@dataclass
class TickerSignalRow:
    ticker: str
    ok: bool
    reason_unavailable: Optional[str]
    score: Optional[float]
    stage: Optional[str]
    close: Optional[float]
    latest_date: Optional[str]
    source: Optional[str]


@dataclass
class PaperPositionFlag:
    ticker: str
    qty: float
    unrealized_pl: float
    unrealized_pl_pct: float
    score: Optional[float]
    stage: Optional[str]
    exit_condition_met: bool
    exit_condition_detail: str


@dataclass
class PaperPortfolioSection:
    ok: bool
    reason: Optional[str]
    equity: Optional[float]
    cash: Optional[float]
    invested_exposure_pct: Optional[float]
    unrealized_pl: Optional[float]
    open_position_count: Optional[int]
    positions: List[PaperPositionFlag] = field(default_factory=list)
    realized_pnl: float = 0.0
    win_loss: dict = field(default_factory=dict)


@dataclass
class ResearchJobSection:
    latest_status: Optional[str]
    trading_date: Optional[str]
    observations_created: int
    events_created: int
    outcomes_matured: int
    skip_reason: Optional[str]


@dataclass
class RegimeSection:
    ok: bool
    label: Optional[str]
    reason: Optional[str]
    as_of_date: Optional[str] = None          # NEW: the LIVE compute_market_regime's own result.as_of_date, previously discarded
    benchmark: Optional[str] = None           # NEW: result.benchmark
    is_stale_vs_report_date: Optional[bool] = None  # NEW: True iff as_of_date != report_date (best-effort calendar-day compare, NOT NYSE-session-aware - see caption text)


@dataclass
class ProspectiveRegimeFreshnessSection:
    """Freshness of the POINT-IN-TIME regime lookup used by TODAY's
    prospective research observations (strategy_lab/prospective.py::
    build_todays_observation, Area A/B of Phase 17) - DISTINCT from
    `market_regime` above (the LIVE, non-point-in-time
    research.regime.compute_market_regime read). Sourced from the most
    recent research_prospective_observations row for report_date (any
    ticker - these fields are shared across every ticker observed the same
    day, since they describe one shared benchmark lookup), never
    recomputed here."""
    ok: bool
    reason: Optional[str]
    regime_benchmark: Optional[str] = None
    regime_benchmark_data_date: Optional[str] = None
    regime_freshness_status: Optional[str] = None


@dataclass
class DailyReport:
    report_date: str
    generated_at: str
    production_health: ProductionHealthSection
    ticker_signals: List[TickerSignalRow]
    paper_portfolio: PaperPortfolioSection
    research_job: ResearchJobSection
    market_regime: RegimeSection
    prospective_regime_freshness: ProspectiveRegimeFreshnessSection   # NEW


def _build_production_health(conn) -> ProductionHealthSection:
    history = load_run_history(conn, limit=5)
    if history.empty:
        return ProductionHealthSection(status=None, started_at=None, tickers_failed=0, error_summary=None, history=[])
    latest = history.iloc[0]
    tickers_failed = latest["tickers_failed"]
    return ProductionHealthSection(
        status=latest["status"],
        started_at=latest["started_at"],
        tickers_failed=int(tickers_failed) if tickers_failed is not None else 0,
        error_summary=latest["error_summary"],
        history=history.to_dict("records"),
    )


def _build_ticker_signal_row(ticker: str, signal) -> TickerSignalRow:
    return TickerSignalRow(
        ticker=ticker,
        ok=signal.ok,
        reason_unavailable=signal.reason_unavailable,
        score=signal.score.score if signal.ok and signal.score is not None else None,
        stage=signal.score.highest_confirmed_stage if signal.ok and signal.score is not None else None,
        close=signal.close,
        latest_date=signal.latest_date,
        source=signal.source,
    )


def _build_ticker_signals(conn) -> List[TickerSignalRow]:
    rows = []
    for ticker in WATCHLIST:
        signal = evaluate_ticker(conn, ticker)
        rows.append(_build_ticker_signal_row(ticker, signal))
    return rows


def _empty_paper_portfolio_section(reason: str) -> PaperPortfolioSection:
    return PaperPortfolioSection(
        ok=False, reason=reason, equity=None, cash=None, invested_exposure_pct=None,
        unrealized_pl=None, open_position_count=None, positions=[], realized_pnl=0.0, win_loss={},
    )


def _position_exit_flag(position) -> PaperPositionFlag:
    """A boolean-only, display-purposes exit-condition read for one held
    position, via `trading.signals_bridge.exit_qualifies` - never
    `build_candidates`, and never anything in `trading/orders.py` or
    `trading/engine.py`. Setting this flag never acts on it."""
    exit_met = False
    exit_detail = "signal data unavailable - cannot evaluate exit condition"
    if position.signal_score is not None and position.signal_stage is not None:
        score_shape = SignalScore(
            ticker=position.ticker, ok=True, score=position.signal_score,
            highest_confirmed_stage=position.signal_stage,
        )
        exit_met = exit_qualifies(score_shape, DEFAULT_RULES)
        stage_below_floor = STAGE_ORDER[position.signal_stage] < STAGE_ORDER[DEFAULT_RULES.exit_stage_floor]
        score_at_or_below_exit = position.signal_score <= DEFAULT_RULES.exit_max_score
        exit_detail = (
            f"stage={position.signal_stage} ({'<' if stage_below_floor else '>='} {DEFAULT_RULES.exit_stage_floor}) "
            f"OR score={position.signal_score:.1f} ({'<=' if score_at_or_below_exit else '>'} {DEFAULT_RULES.exit_max_score})"
        )
    return PaperPositionFlag(
        ticker=position.ticker,
        qty=position.qty,
        unrealized_pl=position.unrealized_pl,
        unrealized_pl_pct=position.unrealized_pl_pct,
        score=position.signal_score,
        stage=position.signal_stage,
        exit_condition_met=exit_met,
        exit_condition_detail=exit_detail,
    )


def _build_paper_portfolio(conn) -> PaperPortfolioSection:
    try:
        client = trading_client.get_client()
        trading_client.verify_paper_environment(client)
    except Exception as e:
        return _empty_paper_portfolio_section(f"Alpaca paper environment unavailable: {e}")

    try:
        summary, positions = trading_portfolio.build_portfolio_view(conn, client)
        realized_pnl = trading_portfolio.compute_realized_pnl(conn)
        win_loss = trading_portfolio.compute_win_loss_summary(conn)
    except Exception as e:
        return _empty_paper_portfolio_section(f"could not fetch Alpaca account/positions: {e}")

    return PaperPortfolioSection(
        ok=True,
        reason=None,
        equity=summary.equity,
        cash=summary.cash,
        invested_exposure_pct=summary.invested_exposure_pct,
        unrealized_pl=summary.unrealized_pl,
        open_position_count=summary.open_position_count,
        positions=[_position_exit_flag(p) for p in positions],
        realized_pnl=realized_pnl,
        win_loss=win_loss,
    )


def _build_research_job(conn) -> ResearchJobSection:
    history = load_research_run_history(conn, limit=1)
    if history.empty:
        return ResearchJobSection(
            latest_status=None, trading_date=None, observations_created=0,
            events_created=0, outcomes_matured=0, skip_reason=None,
        )
    latest = history.iloc[0]
    return ResearchJobSection(
        latest_status=latest["status"],
        trading_date=latest["trading_date"],
        observations_created=int(latest["observations_created"] or 0),
        events_created=int(latest["events_created"] or 0),
        outcomes_matured=int(latest["outcomes_matured"] or 0),
        skip_reason=latest["skip_reason"],
    )


def _build_market_regime(conn, today: date) -> RegimeSection:
    try:
        result = compute_market_regime(conn)
    except Exception as e:
        return RegimeSection(ok=False, label=None, reason=str(e))
    is_stale = (result.as_of_date is not None and result.as_of_date != today.isoformat()) if result.ok else None
    return RegimeSection(
        ok=result.ok, label=result.label, reason=result.reason,
        as_of_date=result.as_of_date, benchmark=result.benchmark, is_stale_vs_report_date=is_stale,
    )


def _build_prospective_regime_freshness(conn, today: date) -> ProspectiveRegimeFreshnessSection:
    observations = load_observations(conn)
    if observations.empty:
        return ProspectiveRegimeFreshnessSection(ok=False, reason="no prospective observations recorded yet")
    day_obs = observations[observations["observation_date"] == today.isoformat()]
    if day_obs.empty:
        return ProspectiveRegimeFreshnessSection(ok=False, reason=f"no prospective observation recorded for {today.isoformat()} yet")
    row = day_obs.iloc[0]
    return ProspectiveRegimeFreshnessSection(
        ok=True, reason=None,
        regime_benchmark=row.get("regime_benchmark"),
        regime_benchmark_data_date=row.get("regime_benchmark_data_date"),
        regime_freshness_status=row.get("regime_freshness_status"),
    )


def build_daily_report(conn, today: Optional[date] = None) -> DailyReport:
    today = today or date.today()
    return DailyReport(
        report_date=today.isoformat(),
        generated_at=datetime.now(timezone.utc).isoformat(),
        production_health=_build_production_health(conn),
        ticker_signals=_build_ticker_signals(conn),
        paper_portfolio=_build_paper_portfolio(conn),
        research_job=_build_research_job(conn),
        market_regime=_build_market_regime(conn, today),
        prospective_regime_freshness=_build_prospective_regime_freshness(conn, today),
    )


def report_to_dict(report: DailyReport) -> dict:
    return dataclasses.asdict(report)


def render_report_text(report: DailyReport) -> str:
    lines = []
    lines.append(f"Daily Research Report — {report.report_date}")
    lines.append(f"Generated at: {report.generated_at}")
    lines.append("")

    lines.append("-- Production Health --")
    ph = report.production_health
    lines.append(f"  Status: {ph.status or 'no runs yet'}")
    if ph.started_at:
        lines.append(f"  Started: {ph.started_at}")
    lines.append(f"  Tickers failed: {ph.tickers_failed}")
    if ph.error_summary:
        lines.append(f"  Error summary: {ph.error_summary}")
    lines.append("")

    lines.append("-- Ticker Signals --")
    for row in report.ticker_signals:
        if row.ok:
            lines.append(
                f"  {row.ticker}: score={row.score} stage={row.stage} close={row.close} "
                f"date={row.latest_date} source={row.source}"
            )
        else:
            lines.append(f"  {row.ticker}: unavailable ({row.reason_unavailable})")
    lines.append("")

    lines.append("-- Paper Portfolio --")
    pp = report.paper_portfolio
    if not pp.ok:
        lines.append(f"  unavailable: {pp.reason}")
    else:
        lines.append(f"  Equity: {pp.equity}  Cash: {pp.cash}  Exposure: {pp.invested_exposure_pct}")
        lines.append(f"  Unrealized P&L: {pp.unrealized_pl}  Open positions: {pp.open_position_count}")
        lines.append(f"  Realized P&L: {pp.realized_pnl}  Win/Loss: {pp.win_loss}")
        for pos in pp.positions:
            flag = "  [EXIT CONDITION MET — informational only]" if pos.exit_condition_met else ""
            lines.append(
                f"    {pos.ticker}: qty={pos.qty} unrealized_pl={pos.unrealized_pl:.2f} "
                f"({pos.unrealized_pl_pct:+.2f}%){flag}"
            )
    lines.append("")

    lines.append("-- Research Job --")
    rj = report.research_job
    lines.append(f"  Latest status: {rj.latest_status or 'no runs yet'}  trading_date={rj.trading_date}")
    lines.append(
        f"  Observations: {rj.observations_created}  Events: {rj.events_created}  "
        f"Outcomes matured: {rj.outcomes_matured}"
    )
    if rj.skip_reason:
        lines.append(f"  Skip reason: {rj.skip_reason}")
    lines.append("")

    lines.append("-- Market Regime (live) --")
    mr = report.market_regime
    if mr.ok:
        lines.append(f"  Label: {mr.label}  Benchmark: {mr.benchmark}  As-of date: {mr.as_of_date}")
        if mr.is_stale_vs_report_date:
            lines.append(f"  NOTE: live regime benchmark data lags report_date ({report.report_date}) - real, not fabricated.")
    else:
        lines.append(f"  Unavailable: {mr.reason}")
    lines.append("")

    lines.append("-- Prospective Regime Freshness (research observations) --")
    prf = report.prospective_regime_freshness
    if prf.ok:
        lines.append(
            f"  Benchmark: {prf.regime_benchmark}  Data date: {prf.regime_benchmark_data_date}  "
            f"Status: {prf.regime_freshness_status}"
        )
    else:
        lines.append(f"  Unavailable: {prf.reason}")

    return "\n".join(lines)
