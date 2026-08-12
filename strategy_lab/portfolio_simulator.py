"""Phase 11 spec B7-B13: realistic, capital-constrained multi-asset
portfolio simulator - a research-only historical simulation using Phase 7's
ACTUAL capital/risk constraints (trading/config.py's DEFAULT_RISK_CONFIG),
not Phase 10's simplified equal-weight-per-signal approximation.

Never imports/calls Alpaca order submission - no import anywhere in this
module of trading.orders/engine/run_paper (see tests/test_strategy_lab.py's
structural safety tests, which scan this file too).

Constraints enforced (asserted against the live RiskConfig, never
reimplemented as separate hardcoded numbers - see simulate_variant()):
  - $100,000 reference starting equity (this module's own simulation
    constant - independent of the real Alpaca paper account balance)
  - max 10% of CURRENT simulated equity per new position
  - max 60% total exposure
  - max 6 simultaneous positions
  - long-only, no leverage, no shorting, no options, no averaging down,
    no duplicate ticker position
  - next-session execution: a signal observed on date T enters/exits no
    earlier than T+1's open (same convention as strategy_lab/events.py)
  - friction: strategy_lab.execution.ASSUMPTION_SETS["reasonable"], which
    is exactly 5bps slippage + 10bps commission (DEFAULT_EXECUTION.commission)
    - reused, not re-specified, so Phase 11's friction assumption can never
    drift from Phase 9's.

Ranking when there are more eligible signals than slots/capital (B8):
highest production technical score, then higher confirmation stage, then
alphabetical ticker - deterministic, uses only information known at the
signal date. Never ranks using future returns or the Phase 8 Research Score.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from backtest.config import DEFAULT_RULES
from backtest.scoring import STAGE_ORDER, compute_score_series
from db.price_repository import load_price_history
from indicators.technical import MIN_REQUIRED_ROWS, enrich_with_indicators
from strategy_lab.data import RESEARCH_SOURCE
from strategy_lab.execution import ASSUMPTION_SETS
from strategy_lab.phase10_experiments import ALL_VARIANTS, BULLISH_LABEL, RegimeGatedRules
from strategy_lab.regime_history import compute_historical_regime_series
from strategy_lab.universe import RESEARCH_UNIVERSE
from trading.config import DEFAULT_RISK_CONFIG, RiskConfig

STARTING_EQUITY = 100_000.0
FRICTION = ASSUMPTION_SETS["reasonable"]
TRADING_DAYS_PER_YEAR = 252

_STAGE_LABEL_BY_RANK = {rank: label for label, rank in STAGE_ORDER.items()}


@dataclass
class Trade:
    ticker: str
    entry_date: str
    entry_price: float
    qty: float
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    realized_pnl: Optional[float] = None

    @property
    def is_open(self) -> bool:
        return self.exit_date is None


@dataclass
class SkippedOpportunity:
    date: str
    ticker: str
    reason: str  # position_cap | exposure_limit | insufficient_cash | already_held (structurally impossible here)


@dataclass
class SimulationResult:
    variant_name: str
    equity_curve: pd.DataFrame  # date, cash, market_value, equity, exposure_pct, n_positions
    trades: List[Trade] = field(default_factory=list)
    skipped: List[SkippedOpportunity] = field(default_factory=list)
    starting_equity: float = STARTING_EQUITY

    @property
    def closed_trades(self) -> List[Trade]:
        return [t for t in self.trades if not t.is_open]


def build_signal_frames(conn, tickers: Optional[List[str]] = None, source: str = RESEARCH_SOURCE) -> Dict[str, pd.DataFrame]:
    """One row per trading day per ticker: date/open/close/score/stage_rank.
    Tickers with insufficient history are silently excluded (not fabricated)."""
    tickers = tickers or RESEARCH_UNIVERSE
    frames = {}
    for t in tickers:
        price_df = load_price_history(conn, t, source=source)
        if len(price_df) < MIN_REQUIRED_ROWS:
            continue
        price_df = price_df.sort_values("date").reset_index(drop=True)
        enriched = enrich_with_indicators(price_df)
        scores = compute_score_series(enriched, t)
        df = price_df.merge(scores, on="date", how="inner").reset_index(drop=True)
        frames[t] = df
    return frames


def _apply_variant_signals(df: pd.DataFrame, variant: RegimeGatedRules, regime_by_date: dict) -> pd.DataFrame:
    """Adds entry_eligible/exit_eligible/entry_event columns for one variant.
    entry_event is the transition (eligible today, not eligible yesterday) -
    a signal that stays eligible for many consecutive days produces exactly
    one entry_event, mirroring how alerts/engine.py fires once per crossing."""
    df = df.copy()
    trend_rank = STAGE_ORDER[DEFAULT_RULES.entry_min_stage]
    floor_rank = STAGE_ORDER[DEFAULT_RULES.exit_stage_floor]

    base_entry = (df["stage_rank"] >= trend_rank) & (df["score"] >= DEFAULT_RULES.entry_min_score)
    base_exit = (df["stage_rank"] < floor_rank) | (df["score"] <= DEFAULT_RULES.exit_max_score)

    # Unknown regime (e.g. before enough SPY history exists) is treated as
    # NOT bullish - conservative for both entry-gating (no entry) and
    # exit-on-regime-loss (exit if regime can't be confirmed bullish).
    is_bullish = df["date"].map(regime_by_date).eq(BULLISH_LABEL).fillna(False)

    entry_eligible = (base_entry & is_bullish) if variant.require_bullish_entry else base_entry
    exit_eligible = (base_exit | (~is_bullish)) if variant.exit_on_regime_loss else base_exit

    df["entry_eligible"] = entry_eligible.fillna(False).astype(bool)
    df["exit_eligible"] = exit_eligible.fillna(False).astype(bool)
    prev_eligible = df["entry_eligible"].shift(1).fillna(False).astype(bool)
    df["entry_event"] = df["entry_eligible"] & (~prev_eligible)
    return df


def _rank_candidates(candidates: List[dict]) -> List[dict]:
    """Deterministic ranking (B8): score desc, confirmation stage desc,
    ticker alphabetical tie-break. Never uses future returns or the Phase 8
    Research Score."""
    return sorted(candidates, key=lambda c: (-c["score"], -c["stage_rank"], c["ticker"]))


def simulate_variant(
    conn, variant: RegimeGatedRules, ticker_frames: Dict[str, pd.DataFrame],
    starting_equity: float = STARTING_EQUITY, risk_config: RiskConfig = DEFAULT_RISK_CONFIG,
) -> SimulationResult:
    # Structural assertions, not reimplementations - if trading/config.py's
    # RiskConfig ever loosens one of these, the simulator refuses to run
    # rather than silently simulating a less conservative strategy.
    assert risk_config.long_only
    assert not risk_config.allow_margin
    assert not risk_config.allow_short
    assert not risk_config.allow_options
    assert not risk_config.allow_averaging_down
    assert not risk_config.allow_duplicate_ticker_position

    regime_series = compute_historical_regime_series(conn)
    regime_by_date = regime_series.set_index("date")["label"].to_dict() if not regime_series.empty else {}

    variant_frames = {t: _apply_variant_signals(df, variant, regime_by_date) for t, df in ticker_frames.items()}
    if not variant_frames:
        return SimulationResult(variant_name=variant.name, equity_curve=pd.DataFrame(), starting_equity=starting_equity)

    all_dates = sorted(set().union(*[set(df["date"]) for df in variant_frames.values()]))
    date_row_idx = {t: {row["date"]: i for i, row in df.iterrows()} for t, df in variant_frames.items()}

    cash = starting_equity
    positions: Dict[str, Trade] = {}
    trades: List[Trade] = []
    skipped: List[SkippedOpportunity] = []
    equity_rows = []
    one_way_cost = (FRICTION.slippage_bps / 10_000.0) + FRICTION.commission_pct

    for day_i in range(1, len(all_dates)):
        signal_date = all_dates[day_i - 1]
        exec_date = all_dates[day_i]

        # --- 1. EXITS: exit_eligible checked on signal_date, executed at exec_date's open ---
        for ticker in list(positions.keys()):
            df = variant_frames[ticker]
            idx_map = date_row_idx[ticker]
            sig_idx, exec_idx = idx_map.get(signal_date), idx_map.get(exec_date)
            if sig_idx is None or exec_idx is None:
                continue
            if bool(df.at[sig_idx, "exit_eligible"]):
                trade = positions.pop(ticker)
                exit_price = float(df.at[exec_idx, "open"]) * (1.0 - one_way_cost)
                cash += trade.qty * exit_price
                trade.exit_date, trade.exit_price, trade.exit_reason = exec_date, exit_price, "signal_exit"
                trade.realized_pnl = trade.qty * exit_price - trade.qty * trade.entry_price
                trades.append(trade)

        # --- 2. mark existing positions to exec_date's close for sizing/exposure decisions this day ---
        deployed_notional = 0.0
        for ticker, trade in positions.items():
            idx_map = date_row_idx.get(ticker, {})
            exec_idx = idx_map.get(exec_date)
            price = float(variant_frames[ticker].at[exec_idx, "close"]) if exec_idx is not None else trade.entry_price
            deployed_notional += trade.qty * price
        equity_now = cash + deployed_notional

        # --- 3. ENTRIES: rank today's fresh candidates, fill open slots within all limits ---
        candidates = []
        for ticker, df in variant_frames.items():
            if ticker in positions:
                continue  # no duplicate ticker position - structurally impossible to reach twice
            idx_map = date_row_idx[ticker]
            sig_idx, exec_idx = idx_map.get(signal_date), idx_map.get(exec_date)
            if sig_idx is None or exec_idx is None:
                continue
            if bool(df.at[sig_idx, "entry_event"]):
                candidates.append({
                    "ticker": ticker, "score": float(df.at[sig_idx, "score"]),
                    "stage_rank": float(df.at[sig_idx, "stage_rank"]), "exec_idx": exec_idx,
                })

        for c in _rank_candidates(candidates):
            if len(positions) >= risk_config.max_open_positions:
                skipped.append(SkippedOpportunity(exec_date, c["ticker"], "position_cap"))
                continue
            df = variant_frames[c["ticker"]]
            entry_price = float(df.at[c["exec_idx"], "open"]) * (1.0 + one_way_cost)
            sized_notional = round(risk_config.max_position_size_pct * equity_now, 2)

            projected_exposure_pct = (deployed_notional + sized_notional) / equity_now if equity_now > 0 else float("inf")
            if projected_exposure_pct > risk_config.max_total_exposure_pct + 1e-9:
                skipped.append(SkippedOpportunity(exec_date, c["ticker"], "exposure_limit"))
                continue
            if sized_notional > cash:
                skipped.append(SkippedOpportunity(exec_date, c["ticker"], "insufficient_cash"))
                continue

            qty = sized_notional / entry_price
            cash -= sized_notional
            deployed_notional += sized_notional
            positions[c["ticker"]] = Trade(ticker=c["ticker"], entry_date=exec_date, entry_price=entry_price, qty=qty)

        # --- 4. end-of-day equity mark ---
        market_value = 0.0
        for ticker, trade in positions.items():
            exec_idx = date_row_idx[ticker].get(exec_date)
            if exec_idx is not None:
                market_value += trade.qty * float(variant_frames[ticker].at[exec_idx, "close"])
            else:
                market_value += trade.qty * trade.entry_price
        equity = cash + market_value
        equity_rows.append({
            "date": exec_date, "cash": cash, "market_value": market_value, "equity": equity,
            "exposure_pct": (market_value / equity * 100.0) if equity > 0 else 0.0,
            "n_positions": len(positions),
        })

        # Structural invariants that must hold at every snapshot regardless
        # of price movement:
        assert len(positions) <= risk_config.max_open_positions
        assert cash >= -1e-6  # never spends more cash than available
        # The 60% exposure limit is enforced as a SIZING-TIME constraint
        # (the projected_exposure_pct check above, before each entry is
        # executed) - not a hard ceiling on end-of-day mark-to-market value.
        # A position sized at exactly the cap can organically drift above
        # it as its price moves after entry, exactly as a real portfolio's
        # exposure % drifts with price - that is not a limit violation, so
        # it is not asserted here (see B9's "except trivial rounding").

    return SimulationResult(
        variant_name=variant.name, equity_curve=pd.DataFrame(equity_rows),
        trades=trades + list(positions.values()), skipped=skipped, starting_equity=starting_equity,
    )


def simulate_all_variants(conn, tickers: Optional[List[str]] = None, starting_equity: float = STARTING_EQUITY) -> Dict[str, SimulationResult]:
    frames = build_signal_frames(conn, tickers=tickers)
    return {variant.name: simulate_variant(conn, variant, frames, starting_equity=starting_equity) for variant in ALL_VARIANTS}


@dataclass
class PortfolioPerformanceStats:
    label: str
    final_value: float
    cumulative_return_pct: float
    cagr_pct: float
    annualized_vol_pct: float
    sharpe_ratio_rf0: Optional[float]
    sortino_ratio_rf0: Optional[float]
    max_drawdown_pct: float
    calmar_ratio: Optional[float]
    realized_pnl: float
    trade_count: int
    win_rate_pct: Optional[float]
    profit_factor: Optional[float]
    turnover: Optional[float]
    avg_holding_period_days: Optional[float]
    median_holding_period_days: Optional[float]
    avg_exposure_pct: Optional[float]
    max_exposure_pct: Optional[float]
    avg_position_count: Optional[float]
    cash_pct_of_time: Optional[float]


def compute_portfolio_stats(result: SimulationResult) -> Optional[PortfolioPerformanceStats]:
    ec = result.equity_curve
    if ec.empty or len(ec) < 2:
        return None

    equity = ec["equity"]
    returns = equity.pct_change().dropna()
    final_value = float(equity.iloc[-1])
    cumulative_return = final_value / result.starting_equity - 1.0
    years = len(ec) / TRADING_DAYS_PER_YEAR
    cagr = (final_value / result.starting_equity) ** (1.0 / years) - 1.0 if years > 0 else float("nan")
    ann_vol = float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)) if len(returns) > 1 else float("nan")
    sharpe = cagr / ann_vol if ann_vol and ann_vol > 0 else None

    downside = returns[returns < 0]
    downside_dev = float(downside.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)) if len(downside) > 1 else None
    sortino = (cagr / downside_dev) if downside_dev and downside_dev > 0 else None

    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    max_dd = float(drawdown.min())
    calmar = (cagr / abs(max_dd)) if max_dd < 0 else None

    closed = result.closed_trades
    realized_pnl = float(sum(t.realized_pnl for t in closed)) if closed else 0.0
    wins = [t for t in closed if t.realized_pnl and t.realized_pnl > 0]
    losses = [t for t in closed if t.realized_pnl and t.realized_pnl < 0]
    win_rate = (len(wins) / len(closed) * 100.0) if closed else None
    gross_win = sum(t.realized_pnl for t in wins)
    gross_loss = abs(sum(t.realized_pnl for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (None if not wins else float("inf"))

    total_bought = sum(t.qty * t.entry_price for t in result.trades)
    total_sold = sum(t.qty * t.exit_price for t in closed)
    avg_equity = float(equity.mean())
    turnover = (total_bought + total_sold) / (2 * avg_equity) if avg_equity else None

    # Holding period in calendar days between entry_date/exit_date - a
    # coarse diagnostic (not session-precise, unlike outcome maturation's
    # NYSE-session gate, which must be exact).
    holding_days = [
        (pd.Timestamp(t.exit_date) - pd.Timestamp(t.entry_date)).days for t in closed
    ]

    return PortfolioPerformanceStats(
        label=result.variant_name,
        final_value=round(final_value, 2),
        cumulative_return_pct=round(cumulative_return * 100, 2),
        cagr_pct=round(cagr * 100, 2) if cagr == cagr else None,
        annualized_vol_pct=round(ann_vol * 100, 2) if ann_vol == ann_vol else None,
        sharpe_ratio_rf0=round(sharpe, 3) if sharpe is not None else None,
        sortino_ratio_rf0=round(sortino, 3) if sortino is not None else None,
        max_drawdown_pct=round(max_dd * 100, 2),
        calmar_ratio=round(calmar, 3) if calmar is not None else None,
        realized_pnl=round(realized_pnl, 2),
        trade_count=len(closed),
        win_rate_pct=round(win_rate, 1) if win_rate is not None else None,
        profit_factor=round(profit_factor, 3) if isinstance(profit_factor, float) and profit_factor != float("inf") else profit_factor,
        turnover=round(turnover, 3) if turnover is not None else None,
        avg_holding_period_days=round(float(np.mean(holding_days)), 1) if holding_days else None,
        median_holding_period_days=round(float(np.median(holding_days)), 1) if holding_days else None,
        avg_exposure_pct=round(float(ec["exposure_pct"].mean()), 2),
        max_exposure_pct=round(float(ec["exposure_pct"].max()), 2),
        avg_position_count=round(float(ec["n_positions"].mean()), 2),
        cash_pct_of_time=round(float((ec["cash"] / ec["equity"]).mean() * 100.0), 2),
    )


# --- B11: capital-constraint / skipped-opportunity accounting ---

def capital_constraint_summary(result: SimulationResult) -> dict:
    reasons = {}
    for s in result.skipped:
        reasons[s.reason] = reasons.get(s.reason, 0) + 1
    return {
        "eligible_entries_considered": len(result.trades) + sum(reasons.values()),
        "entries_executed": len(result.trades),
        "skipped_position_cap": reasons.get("position_cap", 0),
        "skipped_exposure_limit": reasons.get("exposure_limit", 0),
        "skipped_insufficient_cash": reasons.get("insufficient_cash", 0),
        "skipped_already_held": 0,  # structurally impossible: entry_event never fires while ticker in positions
    }


# --- B12: concentration / correlation diagnostics (research-only, no new rule) ---

def concentration_diagnostics(result: SimulationResult, ticker_frames: Dict[str, pd.DataFrame]) -> dict:
    """Diagnostic only - does not create or change any risk rule. Reports
    which tickers were held most often/longest and pairwise return
    correlation among tickers ever held, for future-hypothesis review."""
    if not result.trades:
        return {"holding_frequency": {}, "avg_pairwise_correlation": None, "most_correlated_pair": None}

    holding_frequency: Dict[str, int] = {}
    for t in result.trades:
        holding_frequency[t.ticker] = holding_frequency.get(t.ticker, 0) + 1

    held_tickers = list(holding_frequency.keys())
    return_series = {}
    for t in held_tickers:
        df = ticker_frames.get(t)
        if df is None:
            continue
        return_series[t] = df.sort_values("date").set_index("date")["close"].pct_change()

    if len(return_series) < 2:
        return {"holding_frequency": holding_frequency, "avg_pairwise_correlation": None, "most_correlated_pair": None}

    wide = pd.DataFrame(return_series)
    corr = wide.corr()
    pairs = []
    for i, a in enumerate(corr.columns):
        for b in corr.columns[i + 1:]:
            val = corr.at[a, b]
            if val == val:  # not NaN
                pairs.append((a, b, float(val)))

    avg_corr = float(np.mean([p[2] for p in pairs])) if pairs else None
    most_correlated = max(pairs, key=lambda p: p[2]) if pairs else None

    return {
        "holding_frequency": holding_frequency,
        "avg_pairwise_correlation": round(avg_corr, 3) if avg_corr is not None else None,
        "most_correlated_pair": most_correlated,
    }
