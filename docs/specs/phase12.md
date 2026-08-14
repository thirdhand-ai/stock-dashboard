# Phase 12 — Research Operations, Daily Reporting, Data Quality, Experiment Governance

Status: PLANNED. No implementation in this document. Coder implements exactly
this; tester validates exactly this; reviewer checks against exactly this.

## 0. Verified current state (2026-08-13, HEAD afba7e3)

- Production automation: `automation/run_daily.py` → `automation/pipeline.py`
  → `db.run_history_repository` (`automation_runs` table). Fail-closed on
  ingestion failure (`automation/pipeline.py:112-127`). Discord gated by
  `--send`; `alerts/discord.py` never logs `DISCORD_WEBHOOK_URL`.
- Research automation: `strategy_lab/run_research_job.py` →
  `strategy_lab/research_automation.py` (`research_run_history` table, its
  own `ensure_schema`, NOT in `db/schema.py`). Gate: today's PRODUCTION run
  must have `status == 'success'` before any observation is built
  (`_todays_production_run_succeeded`). No import anywhere in
  `strategy_lab/*` of `trading.engine/orders/run_paper` or
  `alerts.discord/runner/run_alerts` — enforced today by
  `tests/test_strategy_lab.py::test_strategy_lab_never_imports_order_execution_or_alerting`.
- Prospective observations (`strategy_lab/prospective.py`,
  `research_prospective_observations`): schema has NO outcome column;
  `record_observation` is `INSERT ... ON CONFLICT DO NOTHING`; no
  `update_observation` exists anywhere.
- Prospective events (`strategy_lab/prospective_events.py`,
  `research_prospective_events`): existing `evidence_label()` classifies by
  **qualifying event count** (< 30 → `"INSUFFICIENT EVIDENCE"`, < 100 →
  `"PRELIMINARY"`, else `"REQUIRES FULL STATISTICAL REVIEW"`). **This
  function and its vocabulary are Phase 11's and must not be edited or
  reused for Phase 12's required vocabulary** (see §6.2 — different
  vocabulary, different unit of measure: trading days, not events).
- Outcome maturation (`strategy_lab/outcome_maturation.py`,
  `research_prospective_outcomes`): separate table, keyed by
  `(observation_id, horizon_days)`; upserts only this table, never the
  observations table.
- Config-drift fingerprinting: `strategy_lab/production_guard.py` already
  hashes `signals/config.py`, `backtest/config.py`, `trading/config.py`,
  `config/settings.py`, `alerts/config.py`,
  `deploy/com.stockdashboard.dailyrun.plist.example` plus structured
  dataclass values (thresholds/weights/rules/risk/alert config/watchlist),
  saved to `data/research_cache/production_fingerprint.json`. **Phase 12
  reuses this via `compute_fingerprint()`/`diff_against_saved()` — it is
  not re-implemented.**
- `trading/client.py` exposes only read calls: `get_account`,
  `get_positions`, `get_open_orders`, `get_order_by_id`,
  `verify_paper_environment`. **No submit/cancel/replace/close function
  exists anywhere in `trading/client.py`.** Order mutation lives only in
  `trading/orders.py` / `trading/engine.py` (out of scope — never imported
  by anything in this phase).
- `trading/reconcile.py::reconcile_order` / `reconcile_open_orders` READ
  Alpaca (`get_order_by_id`) but WRITE to local `paper_orders` via
  `update_order_status`. Phase 12's reconciliation report must **not**
  call these — it needs a pure comparison, not a mutation (§4).
- `strategy_lab/amzn_monitor.py::get_amzn_status` already does a read-only
  AMZN position + exit-condition check; Phase 12's reconciliation (§4)
  reuses AMZN as its verification ticker but is a distinct, broader
  local-vs-Alpaca comparison, not a duplicate of this module.
- Dashboard: `dashboard/pages_registry.py` has two nav groups ("Overview",
  "Research") and 7 `st.Page`s. `dashboard/data.py` is the only place
  Streamlit-cached data loaders live; every loader delegates to an
  un-cached `_load_*(conn, ...)` function with no Streamlit dependency —
  Phase 12 follows this exact split.
- No `docs/` directory exists yet in this repo; no `ops/` package exists.
- `data/research_cache/production_fingerprint.json` is currently showing as
  locally modified (`git status`) from a prior session run of
  `save_fingerprint()`. Not part of Phase 12 — flagged in §9 as a pre-flight
  check, not something this phase touches or explains away.

## 1. Global hard constraints (apply to every component below)

1. No edits to: `signals/config.py`, `backtest/config.py`,
   `trading/config.py`, `alerts/config.py`, `config/settings.py`
   (`WATCHLIST`), `alerts/engine.py`, `alerts/discord.py`,
   `alerts/runner.py`, `trading/engine.py`, `trading/orders.py`,
   `trading/run_paper.py`, any `deploy/*` file, any `db/schema.py`
   `ALL_STATEMENTS` entry.
2. Every new module in this phase is **importable and runnable without a
   Streamlit process** (CLI-first, dashboard second) — mirrors
   `strategy_lab/research_automation.py` / `run_research_job.py`.
3. No new module may import `trading.engine`, `trading.orders`,
   `trading.run_paper`, `alerts.discord`, `alerts.runner`,
   `alerts.run_alerts`. Enforced by AST-scan tests (§8).
4. No new module may call any Alpaca method other than: `get_account`,
   `get_all_positions`, `get_orders` (status filter only),
   `get_order_by_id`. No `submit_order`, `cancel_order_by_id`,
   `replace_order_by_id`, `close_position`, `close_all_positions` call
   anywhere in this phase's code.
5. No new module may write to `alert_state`, `alerts`, `paper_orders`
   (except the pre-existing, unmodified `trading/reconcile.py`, which this
   phase does not call), `research_prospective_observations`,
   `research_prospective_events`, `research_prospective_outcomes`,
   `automation_runs`, `research_run_history`. Every new table this phase
   creates is a **new** table.
6. No new module activates a scheduler: no `launchctl`, no `.plist`
   write/edit, no cron edit. CLI entry points are manual-run only, exactly
   like `strategy_lab/run_research_job.py` today.
7. No optimization/backfill using today's prospective outcomes — every
   evidence/threshold function in this phase takes only already-recorded,
   already-immutable data as input and is a pure function of it.
8. Evidence classification vocabulary for prospective-trading-day progress
   is **exactly** `INSUFFICIENT_DATA` / `EARLY_EVIDENCE` /
   `EVALUATION_READY` (§6.2) — distinct from and does not replace Phase
   11's `evidence_label()` (event-count based); both are shown in the
   dashboard, clearly labeled as measuring different things.

## 2. New package: `ops/`

Top-level package, sibling to `trading/`, `strategy_lab/`, `alerts/`,
`automation/`. Read-only/reporting/governance code lives here, never in
`strategy_lab/` (research-only) or `trading/` (execution-capable).

```
ops/
  __init__.py
  daily_report.py              # A
  daily_report_repository.py   # A (own ensure_schema, own table)
  data_quality.py              # B
  reconciliation.py            # C
  experiment_registry.py       # D
  register_phase10_11_experiments.py   # D (one-off idempotent seed script)
  evidence_classification.py   # shared by D/E — trading-day evidence vocab
  discord_summary.py           # F (optional, generator only)
  run_daily_report.py          # CLI: python -m ops.run_daily_report
  run_data_quality_check.py    # CLI: python -m ops.run_data_quality_check
  run_reconciliation.py        # CLI: python -m ops.run_reconciliation
  generate_daily_summary.py    # CLI: python -m ops.generate_daily_summary (F)
```

Dashboard additions live in the existing `dashboard/` tree (§6), not `ops/`.

## 3. Component A — Daily research report (read-only)

### 3.1 `ops/daily_report_repository.py`

```python
TABLE_NAME = "ops_daily_reports"

CREATE TABLE IF NOT EXISTS ops_daily_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL UNIQUE,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    report_json TEXT NOT NULL
);

def ensure_schema(conn) -> None
def upsert_report(conn, report_date: str, report_json: str) -> int
    # INSERT ... ON CONFLICT(report_date) DO UPDATE SET report_json=excluded.report_json,
    # generated_at=datetime('now') — re-running the SAME day's report is
    # idempotent overwrite of that day's cached rendering only (never touches
    # any other day's row, never touches any Phase 11 table).
def load_report(conn, report_date: str) -> Optional[dict]   # json.loads(report_json) or None
def load_latest_report(conn) -> Optional[dict]
def load_report_history(conn, limit: int = 20) -> pd.DataFrame
```

### 3.2 `ops/daily_report.py`

```python
@dataclass
class ProductionHealthSection:
    status: Optional[str]           # from db.run_history_repository latest row
    started_at: Optional[str]
    tickers_failed: int
    error_summary: Optional[str]
    history: List[dict]             # last 5 rows, serializable

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
    positions: List[PaperPositionFlag]
    realized_pnl: float
    win_loss: dict

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

@dataclass
class DailyReport:
    report_date: str
    generated_at: str
    production_health: ProductionHealthSection
    ticker_signals: List[TickerSignalRow]
    paper_portfolio: PaperPortfolioSection
    research_job: ResearchJobSection
    market_regime: RegimeSection

def build_daily_report(conn, today: Optional[date] = None) -> DailyReport
def report_to_dict(report: DailyReport) -> dict     # for json.dumps
def render_report_text(report: DailyReport) -> str  # human-readable CLI/print output, pure function
```

`build_daily_report` composition (every call READ-ONLY, no writes except
the final `upsert_report` call made by the CLI, never inside
`build_daily_report` itself — building and persisting are separate
functions so tests can call `build_daily_report` against an in-memory DB
with zero side effects):

- `production_health`: `db.run_history_repository.load_run_history(conn, limit=5)`.
- `ticker_signals`: for each `t` in `config.settings.WATCHLIST`, call
  `trading.signals_bridge.evaluate_ticker(conn, t)` (existing pure
  function — already used by `trading/engine.py`, reused not
  re-derived).
- `paper_portfolio`: `trading.client.get_client()` +
  `trading.client.verify_paper_environment(client)` (read-only); on
  success, `trading.portfolio.build_portfolio_view(conn, client)` +
  `trading.portfolio.compute_realized_pnl(conn)` +
  `trading.portfolio.compute_win_loss_summary(conn)`. For each
  `PositionView`, exit condition computed via
  `trading.signals_bridge.exit_qualifies(SignalScore-shaped-from-position, DEFAULT_RULES)`
  — **never** `trading.signals_bridge.build_candidates` (that function is
  entry+exit candidate generation feeding real execution; this report
  only needs the boolean exit check for display). If `exit_qualifies` is
  `True`, set `exit_condition_met=True` and include the detail string —
  the report NEVER calls anything in `trading/orders.py` or
  `trading/engine.py` as a result, only sets a display flag.
- `research_job`: `strategy_lab.research_automation.load_research_run_history(conn, limit=1)`.
- `market_regime`: `research.regime.compute_market_regime(conn)` (existing
  function reused from `dashboard/data.py`'s own usage).

Deterministic: every field above is a direct read/transform of stored
data or a pure boolean/threshold comparison against frozen config
(`backtest.config.DEFAULT_RULES`). No LLM call, no free-text generation
of market commentary anywhere in `ops/daily_report.py`.

### 3.3 `ops/run_daily_report.py`

```
python -m ops.run_daily_report [--date YYYY-MM-DD]
```
Builds via `build_daily_report`, prints via `render_report_text`, then
calls `ops.daily_report_repository.upsert_report(conn, report_date, json.dumps(report_to_dict(report), default=str))`.
Exit code 0 always (a report that shows a FAILED production run is still
a successful report run — mirrors `strategy_lab/run_research_job.py`'s
own separation of "the job ran fine" from "what it found").

### 3.4 Edge cases (must be handled, tests required — see §8)

- Alpaca unreachable / paper env unconfirmed →
  `PaperPortfolioSection(ok=False, reason=...)`, never raises out of
  `build_daily_report`.
- No `automation_runs` rows yet → `ProductionHealthSection(status=None, ...)`.
- No `research_run_history` rows yet → `ResearchJobSection(latest_status=None, ...)`.
- A watchlist ticker with insufficient history (`< MIN_REQUIRED_ROWS`) →
  `TickerSignalRow(ok=False, reason_unavailable=...)` via
  `evaluate_ticker`'s existing handling — not re-implemented.
- Division-by-zero: `weight_pct`/`invested_exposure_pct` already guarded
  in `trading/portfolio.py` (`if equity else 0.0`) — reused, not
  reimplemented; `build_daily_report` must not add a second unguarded
  division anywhere (e.g. no manual `x / equity` in `ops/daily_report.py`).
- Weekend/holiday run (`--date` a non-trading day, or cron accidentally
  invoked): report still builds (it's a read of whatever is stored); no
  special-case skip logic is needed here since this module never
  ingests/mutates.

## 4. Component B — Data quality / freshness monitor

### 4.1 `ops/data_quality.py`

```python
STATUS_FRESH = "FRESH"
STATUS_STALE = "STALE"
STATUS_MISSING = "MISSING"          # zero rows at all for this ticker/source

OVERALL_HEALTHY = "HEALTHY"
OVERALL_DEGRADED = "DEGRADED"
OVERALL_STALE = "STALE"
OVERALL_FAILED = "FAILED"

@dataclass
class TickerQualityRow:
    ticker: str
    source: Optional[str]           # db.price_repository.resolve_source result
    status: str                     # FRESH | STALE | MISSING
    last_date: Optional[str]
    fetched_at: Optional[str]
    row_count_total: int
    row_count_last_90d: int
    duplicate_rows: int             # (ticker,date) pairs appearing under >1 fetched_at with differing OHLCV — see below
    invalid_ohlcv_rows: int
    missing_trading_days: List[str] # NYSE trading days in [last_60_sessions] with no row, excluding today
    reason: Optional[str]           # human string for MISSING/STALE

@dataclass
class DataQualityReport:
    checked_at: str
    tickers: List[TickerQualityRow]
    overall_status: str

def check_ticker_quality(conn, ticker: str, today: Optional[date] = None) -> TickerQualityRow
def check_watchlist_quality(conn, tickers: Optional[List[str]] = None, today: Optional[date] = None) -> DataQualityReport
def classify_overall(rows: List[TickerQualityRow]) -> str   # pure, deterministic
```

**Freshness rule** (deterministic, uses
`automation.trading_calendar.is_likely_trading_day` — reused, not
reimplemented): compute `most_recent_completed_session` = the latest
NYSE trading day strictly before `today` if today is itself a trading day
before 16:30 ET context is irrelevant here (this is a daily batch check,
not intraday) — simplify to: the latest NYSE trading day `<= today`.
- `STATUS_MISSING`: `row_count_total == 0`.
- `STATUS_STALE`: `last_date < most_recent_completed_session` (the row
  set doesn't include the most recent trading day that should already be
  populated as of when this check runs — i.e., mirrors exactly what
  `dashboard/data.py::_latest_scheduled_run_status` + `is_stale` already
  infer from `automation_runs`, but this component derives it
  independently **from the `prices` table itself**, which is the point:
  it must be able to catch staleness even if `automation_runs` itself
  claims success but `prices` didn't actually update).
- `STATUS_FRESH`: otherwise.

**Duplicate detection**: the `prices` table has `UNIQUE(ticker, date,
source)`, so exact duplicates are already structurally impossible via
`db/schema.py`. `duplicate_rows` here means: more than one row for the
same `(ticker, date)` across *different* `source` values with
**materially different close prices** (`abs(close_a - close_b) / close_a
> 0.01` — flag as a cross-source disagreement, not a hard error, since
Alpaca/yfinance can legitimately show minor adjustment differences).

**Invalid OHLCV rule** (query, not a full table scan client-side — use
SQL `WHERE` on `prices`): a row is invalid if any of: `high < low`,
`close > high`, `close < low`, `open > high`, `open < low`, `volume <
0`, any of `open/high/low/close` is NULL/NaN, `close <= 0`. Counted, not
auto-repaired — the row stays in `prices` untouched; only the count is
reported.

**Gap detection**: last 60 NYSE trading sessions (via
`automation.trading_calendar` — needs a new small helper,
`trading_sessions_between(start, end) -> List[date]`, since
`trading_sessions_elapsed` only returns a count; add this as a pure,
additive function in `automation/trading_calendar.py` — the **one**
exception to "no edits to automation/" allowed in this phase, because it
is a pure, additive, read-only calendar helper with no behavior change
to any existing caller, not a schedule/config/threshold change).
Sessions with no `prices` row for that `(ticker, source)` are listed in
`missing_trading_days` (capped at last 10 for report brevity, full count
in a separate `missing_trading_days_count` field).

**Overall rollup** (`classify_overall`, pure function over
`List[TickerQualityRow]`):
- `OVERALL_FAILED`: any ticker `STATUS_MISSING`, or the DB read itself
  raised (caller wraps and reports `OVERALL_FAILED` with the exception
  message, never propagates).
- `OVERALL_STALE`: no `MISSING`, but `>= 1` ticker `STATUS_STALE`.
- `OVERALL_DEGRADED`: all tickers `FRESH`, but `>=1` ticker has
  `invalid_ohlcv_rows > 0` or `duplicate_rows > 0` or non-empty
  `missing_trading_days` within the 60-session window.
- `OVERALL_HEALTHY`: all `FRESH`, zero invalid/duplicate/gap findings.

### 4.2 `ops/data_quality_repository.py` *(fold into `ops/data_quality.py`, no separate file needed)*

```python
TABLE_NAME = "ops_data_quality_checks"
CREATE TABLE IF NOT EXISTS ops_data_quality_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    overall_status TEXT NOT NULL,
    report_json TEXT NOT NULL
);
def ensure_schema(conn) -> None
def record_check(conn, report: DataQualityReport) -> int      # plain INSERT, append-only log (no UNIQUE — can run multiple times/day)
def load_latest_check(conn) -> Optional[dict]
def load_check_history(conn, limit: int = 30) -> pd.DataFrame
```

### 4.3 `ops/run_data_quality_check.py`

```
python -m ops.run_data_quality_check [--tickers ...]
```
Builds report, prints a summary table, calls `record_check`. Never calls
any ingestion function (`ingestion.alpaca_source` / `ingestion.yfinance_source`
must not appear as an import anywhere in `ops/data_quality.py`), never
switches source preference (`db.price_repository.SOURCE_PRIORITY` is read,
never mutated), never deletes/repairs rows.

### 4.4 Edge cases

- Ticker with fewer than `MIN_REQUIRED_ROWS` total → still gets a
  `TickerQualityRow` (freshness/gap logic doesn't require indicator
  warm-up length — that's a signals concern, not a data-quality one);
  `row_count_total` simply reflects the small number.
- Non-trading-day run (weekend) → `most_recent_completed_session` still
  resolves correctly (last Friday), so a Saturday check doesn't falsely
  flag Friday's already-fresh data as stale.
- Ticker present in `prices` under a source not in
  `SOURCE_PRIORITY` (unexpected 3rd source) → `resolve_source` already
  falls back to `max(counts, key=counts.get)`; `check_ticker_quality`
  reports whatever `resolve_source` returns, doesn't special-case it.
- Empty `prices` table entirely (fresh clone before first ingestion) →
  every ticker `MISSING`, `overall_status=FAILED`, no exception raised.

## 5. Component C — Paper portfolio reconciliation

### 5.1 `ops/reconciliation.py`

```python
STATUS_MATCHED = "MATCHED"
STATUS_WARNING = "WARNING"
STATUS_MISMATCH = "MISMATCH"

@dataclass
class TickerReconciliation:
    ticker: str
    status: str                      # MATCHED | WARNING | MISMATCH
    reasons: List[str]
    local_qty: Optional[float]
    alpaca_qty: Optional[float]
    local_avg_price: Optional[float]
    alpaca_avg_price: Optional[float]
    local_open_order_count: int
    alpaca_open_order_count: int

@dataclass
class ReconciliationReport:
    checked_at: str
    overall_status: str              # worst-of across tickers, same 3-value scale
    tickers: List[TickerReconciliation]
    amzn: Optional[TickerReconciliation]   # explicit pointer to the AMZN row, the designated live verification case
    alpaca_unreachable: bool
    alpaca_error: Optional[str]

def derive_local_open_positions(conn) -> Dict[str, dict]
    # ticker -> {"qty": float, "avg_price": float}, computed ONLY from
    # paper_orders WHERE status='filled', using the exact same sequential
    # entry/exit pairing convention as trading.portfolio.compute_closed_trades
    # (long-only, no averaging down, no duplicate positions per
    # trading/config.py's RiskConfig — an entry with no subsequent
    # matching exit is the "currently open" position). Read-only SELECT.

def build_reconciliation_report(conn, client=None) -> ReconciliationReport
```

`build_reconciliation_report`:
1. `client = client or trading.client.get_client()`;
   `trading.client.verify_paper_environment(client)` — on failure, return
   `ReconciliationReport(alpaca_unreachable=True, alpaca_error=..., overall_status=STATUS_MISMATCH, tickers=[], amzn=None)`
   (fail loud, not silently "everything's fine").
2. `local_positions = derive_local_open_positions(conn)`.
3. `alpaca_positions = {p.symbol: p for p in trading.client.get_positions(client)}` (read-only).
4. `local_open_order_tickers` via
   `db.trading_repository.get_orders_in_nonterminal_state(conn)` (existing
   read-only SELECT, no mutation call).
5. `alpaca_open_orders = trading.client.get_open_orders(client)` (read-only).
6. For the union of tickers appearing in any of the four sets, build a
   `TickerReconciliation`:
   - `MISMATCH` if exactly one side has a position (`local` has a
     ticker `alpaca` doesn't, or vice versa), or both have a position
     but `abs(local_qty - alpaca_qty) > 0.0001`.
   - `WARNING` if both sides agree on position but
     `local_open_order_count != alpaca_open_order_count` (a locally
     non-terminal order Alpaca no longer shows as open — this is exactly
     what `trading/reconcile.py::reconcile_open_orders` would fix, but
     THIS report only flags it, with a reason string instructing the
     operator to run reconciliation, never running it itself), or price
     divergence `abs(local_avg_price - alpaca_avg_price)/alpaca_avg_price > 0.02`.
   - `MATCHED` otherwise.
7. `overall_status` = worst status across all `TickerReconciliation` rows
   (`MISMATCH` > `WARNING` > `MATCHED`), or `MISMATCH` if
   `alpaca_unreachable`.
8. `amzn` = the `TickerReconciliation` for `"AMZN"` if present in the
   union, else `None` with a note (handled by caller/dashboard, not a
   crash).

**This module never imports `trading.reconcile`** — it is a distinct,
read-only comparison, not a wrapper around the mutating reconciliation
helper. (It MAY import `trading.reconcile.map_alpaca_status`, a pure
string-mapping function with no side effects, purely for consistent
status vocabulary display — not `reconcile_order`/`reconcile_open_orders`.)

### 5.2 `ops/reconciliation_repository.py` *(fold into `ops/reconciliation.py`)*

```python
TABLE_NAME = "ops_reconciliation_checks"
CREATE TABLE IF NOT EXISTS ops_reconciliation_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    overall_status TEXT NOT NULL,
    report_json TEXT NOT NULL
);
def ensure_schema(conn) -> None
def record_check(conn, report: ReconciliationReport) -> int
def load_latest_check(conn) -> Optional[dict]
```

### 5.3 `ops/run_reconciliation.py`

```
python -m ops.run_reconciliation
```

### 5.4 Edge cases

- Local position exists, Alpaca shows none (e.g. manually closed outside
  this system, or a fill event never reconciled) → `MISMATCH`, reason
  `"local position open, no matching Alpaca position"`.
- Alpaca shows a position with no local `filled` entry order at all
  (e.g. a position that predates this system's order tracking) →
  `MISMATCH`, reason `"Alpaca position with no local order history"`.
- Fractional share qty (`use_notional_entries=True` per
  `trading/config.py`) → qty comparison uses a tolerance
  (`0.0001`), never exact float equality.
- Alpaca API rate limit / transient network error mid-call → caught,
  `alpaca_unreachable=True`, report still returns (never raises), overall
  `MISMATCH` (fail loud rather than silently reporting stale-looking
  `MATCHED`).

## 6. Component D — Experiment governance registry

### 6.1 `ops/experiment_registry.py`

```python
STATUS_ACTIVE = "ACTIVE"
STATUS_SUPERSEDED = "SUPERSEDED"
STATUS_RETIRED = "RETIRED"

class ExperimentImmutabilityError(Exception):
    """Raised when register_experiment() is called with an existing
    experiment_id but a hypothesis/methodology_version/config_fingerprint
    that differs from what's already stored."""

TABLE_NAME = "experiment_registry"
HISTORY_TABLE_NAME = "experiment_registry_status_history"

CREATE TABLE IF NOT EXISTS experiment_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL UNIQUE,
    methodology_version TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    superseded_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    notes TEXT
);

CREATE TABLE IF NOT EXISTS experiment_registry_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (datetime('now')),
    reason TEXT NOT NULL
);

def ensure_schema(conn) -> None

def compute_config_fingerprint() -> str
    # sha256(json.dumps(strategy_lab.production_guard.compute_fingerprint(),
    # sort_keys=True, default=str)).hexdigest() — reuses A5's existing
    # fingerprint mechanism verbatim, never re-implements config hashing.

def register_experiment(
    conn, *, experiment_id: str, methodology_version: str, hypothesis: str,
    config_fingerprint: str, status: str = STATUS_ACTIVE, notes: Optional[str] = None,
) -> bool:
    """Returns True if a new row was inserted. Returns False (no-op) if
    experiment_id already exists AND methodology_version, hypothesis, and
    config_fingerprint are all byte-identical to the stored row (safe to
    call repeatedly, e.g. from a re-run seed script). Raises
    ExperimentImmutabilityError if experiment_id already exists and ANY of
    those three fields differ — this is the enforcement point: a real
    methodology change MUST use a new experiment_id, it cannot silently
    overwrite an existing one."""

def update_status(conn, experiment_id: str, new_status: str, reason: str) -> None
    """The ONLY mutable field on an existing row. UPDATE statement touches
    status/superseded_by columns ONLY — never hypothesis, methodology_version,
    or config_fingerprint (see test_update_status_never_touches_frozen_columns,
    §8, which greps this function's SQL for those column names in a SET
    clause). Appends a row to experiment_registry_status_history first,
    unconditionally, before the UPDATE (audit trail is append-only and
    cannot be skipped by a caller)."""

def mark_superseded_by(conn, experiment_id: str, superseded_by: str, reason: str) -> None
    """Requires superseded_by to already exist in experiment_registry (raises
    ValueError if not) — calls update_status(..., STATUS_SUPERSEDED, reason)
    then sets superseded_by column via the same guarded path."""

def get_experiment(conn, experiment_id: str) -> Optional[dict]
def list_experiments(conn) -> pd.DataFrame
def load_status_history(conn, experiment_id: Optional[str] = None) -> pd.DataFrame

def check_active_experiments_config_drift(conn) -> Dict[str, dict]
    """Read-only drift detector: for every experiment_id with status=ACTIVE,
    compares its STORED config_fingerprint against
    compute_config_fingerprint() computed fresh right now. Returns
    {experiment_id: {"stored": ..., "current": ..., "drifted": bool}} for
    every ACTIVE experiment. Never writes anything - a drift is purely
    reported (surfaced in the daily report / dashboard, §3/§6.3), never
    auto-corrected and never auto-retires the experiment."""
```

### 6.2 `ops/evidence_classification.py`

Distinct from Phase 11's `strategy_lab.prospective_events.evidence_label`
(event-count based). This is the **trading-day**-based classification the
task requires, with the exact mandated vocabulary.

```python
EVIDENCE_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
EVIDENCE_EARLY_EVIDENCE = "EARLY_EVIDENCE"
EVIDENCE_EVALUATION_READY = "EVALUATION_READY"

# Placeholder defaults — flagged as an explicit open question in §9;
# scaled down from Phase 11's existing 30/100 EVENT thresholds to a
# DAY-count basis (a day typically yields several events).
MIN_DAYS_EARLY_EVIDENCE = 20   # ~1 trading month
MIN_DAYS_EVALUATION_READY = 60  # ~1 trading quarter

def count_prospective_trading_days(conn) -> int:
    """Distinct observation_date values in research_prospective_observations
    (strategy_lab.prospective.load_observations(conn), read-only). Genuinely
    prospective by construction: that table only ever receives rows going
    forward from when strategy_lab.research_automation started running
    (insert-only, no backfill path anywhere in strategy_lab/prospective.py)."""

def classify_evidence(n_days: int) -> str:
    """Pure, deterministic. n_days < MIN_DAYS_EARLY_EVIDENCE -> INSUFFICIENT_DATA;
    < MIN_DAYS_EVALUATION_READY -> EARLY_EVIDENCE; else EVALUATION_READY."""
```

### 6.3 `ops/register_phase10_11_experiments.py`

One-off, idempotent (safe to re-run — `register_experiment` no-ops on an
identical re-registration) seed script registering the CURRENT
methodology as-is, using the exact names/definitions already frozen in
`strategy_lab/phase10_experiments.py` — no new rule is invented here:

```python
python -m ops.register_phase10_11_experiments
```
Registers three rows, `methodology_version = strategy_lab.prospective.METHODOLOGY_VERSION`
("phase11-v1"), `config_fingerprint = ops.experiment_registry.compute_config_fingerprint()`,
`status = ACTIVE`:

| experiment_id | hypothesis (pulled verbatim from existing docstrings) |
|---|---|
| `control-original-frozen-strategy` | `strategy_lab.phase10_experiments.CONTROL.description` |
| `experiment-a-bullish-entry-only` | `strategy_lab.phase10_experiments.EXPERIMENT_A.description` |
| `experiment-b-bullish-entry-and-exit` | `strategy_lab.phase10_experiments.EXPERIMENT_B.description` |

### 6.4 Edge cases

- Re-running `register_phase10_11_experiments` after `signals/config.py`
  or `backtest/config.py` genuinely changed → `compute_config_fingerprint()`
  differs from the stored value → `register_experiment` raises
  `ExperimentImmutabilityError` → the seed script must **not** catch this
  and silently continue; it must print the error and exit non-zero,
  because a config change under an unchanged experiment_id is exactly the
  scenario this component exists to catch.
- `update_status` called with an `experiment_id` that doesn't exist →
  raises `ValueError`, no history row written.
- `mark_superseded_by` where `superseded_by` doesn't exist yet → raises
  `ValueError` before touching either row.
- Empty registry (`register_phase10_11_experiments` never run) →
  `list_experiments` returns an empty DataFrame, `check_active_experiments_config_drift`
  returns `{}`, dashboard shows an empty-state, not an error.

## 7. Component E — Dashboard additions

### 7.1 New page: `dashboard/views/ops_overview.py`

```python
def render() -> None
```
Registered in `dashboard/pages_registry.py`:
```python
from dashboard.views import ops_overview
PAGE_OPS_OVERVIEW = st.Page(ops_overview.render, title="Operations", icon="🛠️", url_path="ops")
NAV_STRUCTURE = {
    "Overview": [PAGE_WATCHLIST, PAGE_TICKER_DETAIL, PAGE_PAPER_PORTFOLIO, PAGE_OPS_OVERVIEW],
    "Research": [PAGE_RESEARCH_CENTER, PAGE_BACKTEST, PAGE_ALERTS, PAGE_STRATEGY_LAB],
}
```
(Additive edit only — no existing `st.Page`/nav entry removed or
reordered.)

Sections, each reading via new `dashboard/data.py` getters (§7.2), same
`st.cache_data`-wrapped-thin-caller pattern as every existing getter:
1. System health: `get_ops_daily_report()` production_health +
   `get_data_quality_report()` overall_status, rendered as
   `st.metric`/`st.error`/`st.success` exactly like
   `_render_production_health()` in `dashboard/views/strategy_lab.py`
   today.
2. Today's signals: table from `get_ops_daily_report()`'s `ticker_signals`.
3. Paper portfolio: `get_ops_daily_report()`'s `paper_portfolio` section
   (equity/positions/flags) — a summary view; the existing Paper
   Portfolio page remains the detailed one, this is not a duplicate full
   rebuild.
4. Prospective research status: `get_prospective_evidence_status()` (new
   getter, §7.2) showing `n_prospective_trading_days` and the
   `INSUFFICIENT_DATA`/`EARLY_EVIDENCE`/`EVALUATION_READY` label.
5. Recent automation history: two tables side by side —
   `dashboard.data.get_run_history()` (existing, unmodified) and
   `get_ops_research_run_history()` reusing
   `strategy_lab.research_automation.load_research_run_history` exactly
   as `dashboard/views/strategy_lab.py::_render_prospective_validation`
   already does.
6. Reconciliation summary: `get_reconciliation_report()`, AMZN row
   highlighted explicitly (per the task's designated live verification
   case).

### 7.2 `dashboard/data.py` additions (additive only — no existing function edited)

```python
@st.cache_data(ttl=300, show_spinner=False)
def get_ops_daily_report() -> dict:
    with db_session() as conn:
        from ops.daily_report import build_daily_report, report_to_dict
        return report_to_dict(build_daily_report(conn))

@st.cache_data(ttl=300, show_spinner=False)
def get_data_quality_report() -> dict:
    with db_session() as conn:
        from ops.data_quality import check_watchlist_quality
        report = check_watchlist_quality(conn)
    return dataclasses.asdict(report)

@st.cache_data(ttl=300, show_spinner=False)
def get_reconciliation_report() -> dict:
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
def get_prospective_evidence_status() -> dict:
    from ops.evidence_classification import count_prospective_trading_days, classify_evidence
    with db_session() as conn:
        n_days = count_prospective_trading_days(conn)
    return {"n_prospective_trading_days": n_days, "status": classify_evidence(n_days)}

@st.cache_data(ttl=STRATEGY_LAB_TTL_SECONDS, show_spinner=False)
def get_ops_research_run_history(limit: int = 20) -> pd.DataFrame:
    # thin re-export identical to get_research_run_history — kept separate
    # so ops_overview.py doesn't need to import strategy_lab.research_automation
    # directly, matching this file's existing "dashboard imports, views don't" convention.
```
Add all six `.clear()` calls to `clear_all_caches()`.

### 7.3 Strategy Lab view additions (`dashboard/views/strategy_lab.py`, additive)

Two new private render functions, called at the end of `render()`, after
the existing `_render_realistic_portfolio()` call (new `st.divider()`
before each, existing sections untouched):

```python
def _render_experiment_registry():
    st.header("Phase 12 — Experiment Governance Registry")
    st.caption("Immutable methodology metadata. A real change to hypothesis, "
               "methodology, or frozen config always requires a NEW experiment_id "
               "— this table can never be edited in place for those fields.")
    df = get_experiment_registry()
    if df.empty:
        components.empty_state("No experiments registered",
            "Run `python -m ops.register_phase10_11_experiments`.", icon="📋")
        return
    st.dataframe(df, use_container_width=True, hide_index=True)
    drift = get_experiment_registry_drift()   # new getter wrapping check_active_experiments_config_drift
    drifted = {k: v for k, v in drift.items() if v["drifted"]}
    if drifted:
        st.error(f"⚠️ Config drift detected for ACTIVE experiment(s): {list(drifted.keys())}", icon="🚨")


def _render_prospective_evidence_progress():
    st.header("Phase 12 — Prospective Evidence Progress (trading-day based)")
    st.caption(
        "Distinct from the event-count evidence label in the Prospective "
        "Validation section above (strategy_lab.prospective_events.evidence_label). "
        "This measures distinct genuinely-forward-observed trading days."
    )
    status = get_prospective_evidence_status()
    n = status["n_prospective_trading_days"]
    label = status["status"]
    st.metric("Prospective trading days observed", n)
    if label == "INSUFFICIENT_DATA":
        st.warning("**INSUFFICIENT_DATA**", icon="🔬")
    elif label == "EARLY_EVIDENCE":
        st.info("**EARLY_EVIDENCE**", icon="🔬")
    else:
        st.success("**EVALUATION_READY**", icon="🔬")

    st.subheader("Prospective vs. retrospective comparison")
    if label == "INSUFFICIENT_DATA":
        st.warning("INSUFFICIENT PROSPECTIVE EVIDENCE", icon="⚠️")
        return   # no chart, no table — nothing further rendered
    # else (EARLY_EVIDENCE or EVALUATION_READY): render the comparison,
    # with an explicit small-sample caption when label == EARLY_EVIDENCE.
    ...
```

### 7.4 Edge cases

- `get_ops_daily_report()` / `get_reconciliation_report()` raising (e.g.
  Alpaca down) must not crash the page — each render function wraps its
  data call in `try/except` and shows `components.empty_state(...)`,
  matching the existing `_render_amzn_monitor()` pattern in the same file.
- Empty `experiment_registry` before the seed script has ever run → shown
  as an explicit empty-state with the exact CLI command to run, not a
  stack trace.

## 8. Optional: Discord summary payload generator (`ops/discord_summary.py`)

In scope — fits cleanly isolated.

```python
def build_daily_summary_payload(report: DailyReport) -> dict:
    """Pure function: DailyReport -> Discord-embed-shaped dict. No network
    call. Does NOT import requests, alerts.discord, or
    config.settings.DISCORD_WEBHOOK_URL anywhere in this module — total
    isolation from the real webhook path, enforced by an AST-scan test."""

DISCORD_SUMMARY_ENABLED = False   # hardcoded off, same convention as
                                   # alerts/ops_notifications.OPERATIONAL_ALERTS_ENABLED
```

`ops/generate_daily_summary.py`:
```
python -m ops.generate_daily_summary [--date YYYY-MM-DD]
```
Builds the report, builds the payload, **prints the JSON to stdout only**
(or optionally writes to `data/research_cache/daily_summary_preview.json`
for manual copy/paste) — never POSTs anywhere, regardless of
`DISCORD_SUMMARY_ENABLED`'s value (that flag isn't even read by this CLI;
it exists purely as a documented "this is what would gate a future real
send," matching `alerts/ops_notifications.py`'s convention, and is not
wired to anything in this phase).

## 9. Open questions (explicit — do not guess further, confirm before/while implementing)

1. **Evidence thresholds** (`MIN_DAYS_EARLY_EVIDENCE=20`,
   `MIN_DAYS_EVALUATION_READY=60`, §6.2) are a placeholder scaled from
   Phase 11's existing 30/100 event thresholds. Confirm these day-counts
   before treating them as final policy — this determines when the
   dashboard starts showing prospective-vs-retrospective comparisons at
   all, which is a real trading-readiness-adjacent decision even though
   the vocabulary is deliberately non-committal.
2. **`production_fingerprint.json` is currently locally modified**
   (uncommitted) relative to `git log` HEAD. This is unrelated to Phase 12
   but is directly load-bearing for §6's `compute_config_fingerprint()` —
   confirm the fingerprint file's current state is intentional/expected
   before running `ops.register_phase10_11_experiments` for real, since
   that script freezes whatever fingerprint is live at seed time.
3. **`automation/trading_calendar.py` gets one small additive function**
   (`trading_sessions_between`, §4.1) to support gap detection. This is
   the one touch to an existing "automation/" file in this phase — flagged
   explicitly since §1 otherwise forbids editing automation/*; confirm
   this is acceptable, or the reviewer may prefer it live as a private
   helper inside `ops/data_quality.py` instead (duplicating a few lines of
   NYSE-calendar logic rather than touching `automation/`).
4. **Real money / live orders**: nothing in this spec touches live
   trading, and no component here reads or writes anything about a
   non-paper Alpaca environment — `trading/client.py` has no live-mode
   constructor to call in the first place. No open question here beyond
   what's already covered by the hard constraints in §1.
5. **Discord summary generator (§8) default output location**: printing to
   stdout only vs. also writing a preview file to
   `data/research_cache/`. Either is safe (no network, no webhook config
   touched); confirm preference before implementation.

## 10. Testing checklist

### 10.1 New test files (mirrors existing `tests/test_strategy_lab.py` conventions — synthetic/in-memory SQLite, no network, no LLM)

- `tests/test_ops_daily_report.py`
- `tests/test_ops_data_quality.py`
- `tests/test_ops_reconciliation.py`
- `tests/test_ops_experiment_registry.py`
- `tests/test_ops_evidence_classification.py`
- `tests/test_ops_dashboard.py` (thin `_load_*`-equivalent tests for the
  new `dashboard/data.py` getters, following `tests/test_dashboard_data.py`'s
  existing pattern of testing the un-cached layer)
- `tests/test_ops_safety.py` (structural/AST-scan tests, own file — new,
  not appended to `test_strategy_lab.py`, since `ops/` is a new package)

### 10.2 Component B (data quality) — fresh/stale/missing/invalid/gap matrix

- `test_fresh_ticker_classified_fresh`
- `test_ticker_with_no_rows_classified_missing`
- `test_ticker_last_row_older_than_most_recent_session_classified_stale`
- `test_invalid_ohlcv_high_less_than_low_counted_not_removed`
- `test_invalid_ohlcv_negative_volume_counted`
- `test_cross_source_close_divergence_counted_as_duplicate_conflict`
- `test_gap_detection_finds_missing_session_in_window`
- `test_overall_healthy_all_fresh_zero_findings`
- `test_overall_degraded_fresh_but_invalid_rows_present`
- `test_overall_stale_one_ticker_stale`
- `test_overall_failed_one_ticker_missing`
- `test_check_watchlist_quality_never_calls_ingestion` (AST-scan: no
  `ingestion.alpaca_source`/`ingestion.yfinance_source` import in
  `ops/data_quality.py`)
- `test_check_watchlist_quality_never_writes_prices_table` (before/after
  row-count diff on `prices` == 0 after running the full check against a
  seeded in-memory DB)

### 10.3 Component C (reconciliation) — MATCHED/WARNING/MISMATCH states

- `test_matched_when_local_and_alpaca_positions_agree`
- `test_mismatch_when_local_position_missing_from_alpaca`
- `test_mismatch_when_alpaca_position_missing_locally`
- `test_mismatch_on_qty_divergence_beyond_tolerance`
- `test_warning_on_open_order_count_divergence`
- `test_warning_on_avg_price_divergence_beyond_tolerance`
- `test_alpaca_unreachable_yields_mismatch_not_silent_matched`
- `test_amzn_row_always_present_when_amzn_has_any_local_or_alpaca_state`
- `test_reconciliation_never_calls_submit_cancel_replace_close` (mock the
  Alpaca client with a `MagicMock` asserting `submit_order`/
  `cancel_order_by_id`/`replace_order_by_id`/`close_position`/
  `close_all_positions` are never called on it during
  `build_reconciliation_report`)
- `test_reconciliation_never_calls_trading_reconcile_module` (AST-scan:
  `trading.reconcile.reconcile_order`/`reconcile_open_orders` never
  imported by name in `ops/reconciliation.py`)

### 10.4 Component D (experiment governance) — immutability, explicit per the task

- `test_register_new_experiment_inserts_row`
- `test_register_identical_duplicate_is_idempotent_noop` (returns
  `False`, row count unchanged, stored row byte-identical before/after)
- `test_register_same_id_changed_hypothesis_raises_immutability_error`
- `test_register_same_id_changed_config_fingerprint_raises_immutability_error`
- `test_register_same_id_changed_methodology_version_raises_immutability_error`
- `test_register_same_id_changed_notes_does_not_raise` (`notes` is
  explicitly NOT a frozen field — confirm this is intentional per §9 or
  make it frozen too; spec currently treats only
  hypothesis/methodology_version/config_fingerprint as frozen)
- `test_update_status_appends_history_row_before_updating_status`
- `test_update_status_never_modifies_hypothesis_methodology_or_fingerprint`
  (fetch full row before/after `update_status`, assert those three fields
  byte-identical)
- `test_update_status_sql_source_never_contains_frozen_column_in_set_clause`
  (source-text scan of `ops/experiment_registry.py`: the `UPDATE ...
  SET` string literal for `update_status` must not contain
  `hypothesis=`/`methodology_version=`/`config_fingerprint=`)
- `test_update_status_unknown_experiment_id_raises_value_error`
- `test_mark_superseded_by_requires_target_to_exist`
- `test_check_active_experiments_config_drift_detects_changed_config`
  (mutate a copy of the fingerprint inputs in-memory / monkeypatch
  `compute_config_fingerprint`, confirm `drifted=True` reported without
  any write)
- `test_check_active_experiments_config_drift_is_read_only` (row-count
  diff on `experiment_registry` == 0 after calling it)
- `test_register_phase10_11_experiments_seed_script_idempotent` (run
  twice against the same in-memory DB, assert exactly 3 rows, no error)

### 10.5 Component A (daily report) & evidence classification — determinism

- `test_build_daily_report_deterministic_given_same_db_state` (call
  twice, assert `report_to_dict` output identical except `generated_at`)
- `test_build_daily_report_flags_exit_condition_without_acting` (seed an
  Alpaca-mock position whose signal qualifies for exit; assert
  `exit_condition_met=True` in output AND assert the trading-client mock's
  order-mutation methods were never called)
- `test_build_daily_report_handles_no_automation_runs_yet`
- `test_build_daily_report_handles_alpaca_unreachable`
- `test_classify_evidence_boundaries` (`19→INSUFFICIENT_DATA`,
  `20→EARLY_EVIDENCE`, `59→EARLY_EVIDENCE`, `60→EVALUATION_READY`,
  deterministic pure-function table test)
- `test_count_prospective_trading_days_counts_distinct_dates_not_rows`
  (multiple tickers on the same observation_date count as 1 day)

### 10.6 Safety-boundary tests (`tests/test_ops_safety.py`) — explicit per the task

- `test_ops_never_imports_order_execution_or_alerting` (AST-scan every
  `.py` in `ops/` for `trading.engine`, `trading.orders`,
  `trading.run_paper`, `alerts.discord`, `alerts.runner`,
  `alerts.run_alerts` — mirrors
  `test_strategy_lab_never_imports_order_execution_or_alerting`)
- `test_trading_automation_alerts_never_import_ops` (reverse-direction
  isolation, mirrors `test_trading_and_automation_never_import_strategy_lab`)
- `test_ops_never_calls_alpaca_mutating_endpoints` (source-text scan of
  every `ops/*.py` for the literal substrings `submit_order(`,
  `cancel_order_by_id(`, `replace_order_by_id(`, `close_position(`,
  `close_all_positions(` — must find zero occurrences outside comments;
  simplest robust check: none of these substrings appear anywhere in the
  file at all, matching the existing `amzn_monitor.py` test's
  `"submit_order(" not in source` pattern)
- `test_ops_never_writes_alert_state_or_paper_orders_or_prospective_tables`
  (integration-style: seed an in-memory DB, run
  `build_daily_report`, `check_watchlist_quality`,
  `build_reconciliation_report` with mocked Alpaca client, assert
  row-counts for `alert_state`, `alerts`, `paper_orders`,
  `research_prospective_observations`, `research_prospective_events`,
  `research_prospective_outcomes`, `automation_runs`,
  `research_run_history` are all unchanged before/after)
- `test_discord_summary_module_never_imports_requests_or_webhook_config`
  (AST-scan `ops/discord_summary.py`: no `requests` import, no
  `config.settings` import, no reference to `DISCORD_WEBHOOK_URL`)
- `test_generate_daily_summary_cli_never_calls_network` (mock/patch
  `requests.post` globally during the test and assert it's never invoked
  when running `ops.generate_daily_summary.main()`)
- `test_no_ops_module_writes_or_edits_launchagent_or_deploy_files`
  (mirrors `test_no_strategy_lab_module_writes_to_launchagent_plist`)
- `test_no_ops_module_imports_or_edits_signals_backtest_trading_alerts_config`
  (AST-scan: no `ops/*.py` imports `signals.config`, `backtest.config`,
  `trading.config`, or `alerts.config` for write access — read-only
  imports of these config modules ARE expected in
  `ops/experiment_registry.py` via `production_guard.compute_fingerprint()`,
  so this test asserts no `open(..., "w")` / no `.write(` call anywhere
  targeting those file paths, not that the modules are never imported)

### 10.7 Manual/integration verification before moving on from Phase 12

1. `pytest tests/` — full suite green, including all new files above,
   with **zero** new test relying on a real network call or real Discord
   webhook.
2. Run each new CLI once against the real local DB in a read-only smoke
   test: `python -m ops.run_daily_report`, `python -m ops.run_data_quality_check`,
   `python -m ops.run_reconciliation`, `python -m ops.register_phase10_11_experiments`,
   `python -m ops.generate_daily_summary`. Confirm each exits 0 and
   confirm via `sqlite3 data/stock_dashboard.db` row counts that only the
   new `ops_*`/`experiment_registry*` tables changed row count — `prices`,
   `alerts`, `alert_state`, `paper_orders`, `automation_runs`,
   `research_prospective_*`, `research_run_history` row counts are
   identical before/after.
3. `streamlit run dashboard/app.py` — manually load the new Operations
   page and the updated Strategy Lab page; confirm no exception, confirm
   existing pages/sections still render unchanged.
4. Run `python -c "from strategy_lab.production_guard import diff_against_saved; print(diff_against_saved())"`
   before and after implementing this phase — must be identical both
   times (Phase 12 must never cause `signals/config.py`,
   `backtest/config.py`, `trading/config.py`, `config/settings.py`,
   `alerts/config.py`, or the LaunchAgent plist to actually change).
5. Confirm `git diff` for this phase touches only: new files under
   `ops/`, new files under `tests/`, `dashboard/pages_registry.py`
   (additive), `dashboard/data.py` (additive), `dashboard/views/strategy_lab.py`
   (additive), new `dashboard/views/ops_overview.py`, and (pending §9 item
   3) one additive function in `automation/trading_calendar.py`. No other
   file in the repo should appear in the diff.
