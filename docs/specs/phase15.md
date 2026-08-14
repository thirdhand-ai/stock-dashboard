# Phase 15: Evidence Provenance, Reproducibility, and Long-Term Research Monitoring

Status: PLANNED. Builds on Phase 9-14 (repo HEAD e8ad4d5). No implementation
code in this document.

## 0. Grounding: what already exists (do not rebuild)

### 0.1 Provenance mechanisms that already exist

- `ops/experiment_registry.py`: `experiment_registry` table
  (`ops/experiment_registry.py:24-36`) with `methodology_version`,
  `hypothesis`, `config_fingerprint`, `status` (ACTIVE/SUPERSEDED/RETIRED,
  `:17-19`), immutable except `status`/`superseded_by`
  (`register_experiment`, `:89-123`). `compute_config_fingerprint()`
  (`:62-78`) is a sha256 of `strategy_lab.production_guard.compute_fingerprint()`
  with `computed_at` stripped — **this is the one function Phase 15 must
  reuse verbatim, never reimplement**.
- `strategy_lab/production_guard.py::compute_fingerprint()` (`:56-61`)
  hashes guarded files (`signals/config.py`, `backtest/config.py`,
  `trading/config.py`, `config/settings.py`, `alerts/config.py`, the
  LaunchAgent plist example) plus structured dataclass values. This is a
  **whole-production-config** fingerprint, not per-methodology-variant.
- `ops/register_phase10_11_experiments.py:38-52`: registers
  `control-original-frozen-strategy`, `experiment-a-bullish-entry-only`,
  `experiment-b-bullish-entry-and-exit` — **all three with the exact same
  `compute_config_fingerprint()` call result**, since the fingerprint
  captures shared infra config, not per-variant hypothesis differences.
  **A single observation's `config_fingerprint` value can therefore
  legitimately match multiple `experiment_id`s simultaneously** — there is
  no 1:1 row-to-experiment mapping today and Phase 15 must not invent one.
- `strategy_lab/prospective.py` — `research_prospective_observations`
  schema (`:51-73`): already has `source TEXT` and `methodology_version
  TEXT` (added via a guarded `ALTER TABLE` migration at `:78-87`). **Has no
  `config_fingerprint` column today.** `source` is populated from
  `db.price_repository.resolve_source(conn, ticker)`
  (`build_todays_observation`, `:152,165`) — this is the **production**
  source (`SOURCE_PRIORITY = ["yfinance", "alpaca"]`,
  `db/price_repository.py:5`), **not** `strategy_lab.data.RESEARCH_SOURCE`
  (`"alpaca_adjusted"`). Insert-only, `ON CONFLICT DO NOTHING`
  (`record_observation`, `:90-120`) — no `update_observation` exists.
- `strategy_lab/prospective_events.py` — `research_prospective_events`
  schema (`:38-51`): has `methodology_version` but **no `source` and no
  `config_fingerprint` column**. Insert-only, `ON CONFLICT DO NOTHING`
  (`record_event`, `:115-130`).
- `strategy_lab/outcome_maturation.py` — `research_prospective_outcomes`
  schema (`:35-48`): `id, observation_id, ticker, observation_date,
  horizon_days, status, realized_return, exit_date, matured_at`. **Zero
  provenance columns today.** `ticker` and `observation_date` are already
  denormalized copies from the parent observation — Phase 15's new
  provenance columns follow this same convention. This table has an
  `UPSERT` path (`_persist`, `:115-131`, `ON CONFLICT DO UPDATE`) — that is
  pre-existing and fine; the hard constraint "no UPDATE path ever" applies
  only to `research_prospective_observations` and `research_prospective_events`.
- `strategy_lab/cache_integrity.py` — `research_cache_corrections`
  (Phase 14, `:56-69`): `id, corrected_at, ticker, date, source, old_close,
  new_close, old_volume, new_volume, old_fetched_at, new_fetched_at,
  reason, affects_frozen_artifacts`. Append-only. **`source` is always
  `RESEARCH_SOURCE` ("alpaca_adjusted")** — `refresh_incomplete_latest_bars`
  only ever writes through `strategy_lab.data._store_adjusted_bars`,
  hardcoded to `RESEARCH_SOURCE`, and never touches `source="alpaca"`.

### 0.2 The correction-impact join, traced exactly

`strategy_lab/outcome_maturation.py::_compute_one` (`:67-112`):
```
obs_date = date.fromisoformat(obs_row["observation_date"])
...
source = obs_row["source"] if "source" in obs_row and obs_row["source"] else None
price_df = load_price_history(conn, ticker, source=source)   # :78-82
date_index = {d: i for i, d in enumerate(price_df["date"])}
idx = date_index.get(obs_row["observation_date"])            # :91 — ENTRY row index
exit_idx = idx + horizon_days if idx is not None else None   # :92 — EXIT row index
entry_price = float(price_df.at[idx, "close"])                # :104
exit_price  = float(price_df.at[exit_idx, "close"])            # :105
exit_date   = str(price_df.at[exit_idx, "date"])                # :111
```

Two structural findings that determine Component B's design:

1. **The ENTRY price for a given observation is always the bar dated
   `observation_date`, for every `horizon_days` of that observation**
   (idx is shared across horizons `:91`). A correction to a ticker's
   `observation_date` bar therefore affects the entry leg of **all**
   outcome rows (all horizons) for that one `(ticker, observation_date)`
   pair, not just `horizon_days=1`. Separately, the EXIT price for a
   *different, earlier* observation can independently land on that same
   corrected date if `earlier_observation_date + horizon_days` trading
   sessions == the corrected date. **Both join predicates must be
   checked** (`outcome.observation_date == corrected.date` for entry-leg
   matches, `outcome.exit_date == corrected.date` for exit-leg matches) —
   they are never simultaneously true for the same row.

2. **Source must match, and — CONFIRMED against the real live DB — it does
   not for the actual AAPL case.** `_compute_one` loads price history
   using `obs_row["source"]` (line `:78`): when that column is `None`,
   `source` resolves to `None`, and `load_price_history(conn, ticker,
   source=None)` falls through to `resolve_source()` (production
   `SOURCE_PRIORITY = ["yfinance","alpaca"]`). **A direct query of the
   real DB confirms the actual AAPL, `observation_date=2026-08-12` row
   (`research_prospective_observations.id=1`, the system's very first
   recorded observation) has `source=NULL` and `methodology_version=NULL`**
   — a legacy row created before `build_todays_observation` began
   populating those columns, even though the column itself already
   existed via an earlier migration. Its `horizon_days=1` outcome (`id=1`,
   `status='matured'`, `realized_return=0.009958643507030684`,
   `exit_date='2026-08-13'`) was therefore matured against the
   **production `alpaca` source, not `alpaca_adjusted`**. `research_cache_corrections`
   rows are exclusively `source="alpaca_adjusted"`. **Conclusion,
   confirmed not guessed: the Phase 14 correction to AAPL's `alpaca_adjusted`
   2026-08-12 bar did NOT actually affect this outcome's stored
   `realized_return` — its `source_matched` value under Component B's join
   is `False`.** This corrects the orchestrating session's earlier,
   speculative Phase 14 final-report framing (which assumed an effect
   without checking the source column) and is exactly the kind of fact
   Component B exists to establish precisely rather than assume.
   Component B's join implements the strict source-match unconditionally,
   and never silently drops a candidate match just because sources
   differ — it reports `source_matched=False` so this exact scenario is
   visible, not hidden.

### 0.3 Long-term monitoring: what's computed today vs. what's missing

- `ops/evidence_classification.py`: `EVIDENCE_INSUFFICIENT_DATA` /
  `EARLY_EVIDENCE` / `EVALUATION_READY` (`:16-18`), thresholds
  `MIN_DAYS_EARLY_EVIDENCE=20` / `MIN_DAYS_EVALUATION_READY=60` (`:24-25`).
  **Untouched by Phase 15** — measures evidence *sufficiency*, not
  operational health.
- `ops/prospective_audit.py`: `build_prospective_day_ledger()` (`:159-196`)
  classifies each trading day into EXPECTED/CAPTURED/CAPTURED_PARTIAL/
  DUPLICATE_SKIPPED/UNAVAILABLE/MISSED (`:50-55`).
  `prospective_evidence_audit_summary()` (`:199-228`) rolls this up into
  `counts_by_status` plus `maturation_by_horizon` (pending/matured/
  unavailable counts only — **no lag, no rate, no coverage**). This module
  is explicitly read-only by construction — only imports `load_*`
  functions, never `record_*`/`mature_*`. **Missing today:** capture rate,
  consecutive-missed-day streaks, maturity lag, research-job
  success/partial/failure *rates*, cache-correction counts,
  provenance/experiment coverage, and any HEALTHY/DEGRADED/… operational label.
- `db/run_history_repository.py`: production `automation_runs` table,
  statuses `running/success/partial_failure/failed/skipped_non_trading_day/
  skipped_overlap` (`:24-29`). `load_run_history()` is a plain read, no
  rate computed.
- `strategy_lab/research_automation.py`: `research_run_history` table,
  statuses `STATUS_SUCCESS_RESEARCH="success"`,
  `STATUS_SKIPPED_NON_TRADING_DAY`, `STATUS_SKIPPED_NO_FRESH_DATA`,
  `STATUS_FAILED`, `STATUS_PARTIAL_FAILURE_RESEARCH="partial_failure"`,
  plus the "crashed" convention (`status='running'` never finished). No
  rate computed anywhere.
- `ops/data_quality.py`: `OVERALL_HEALTHY`/`OVERALL_DEGRADED`/
  `OVERALL_STALE`/`OVERALL_FAILED` (`:47-50`) — this is the **operational
  health vocabulary that already exists** for price-data freshness. It is
  a *different domain* (per-ticker price freshness, not research-job
  reliability), so Component C must not call `classify_overall()` itself,
  but **must import and reuse these exact four string constants** for its
  own, independently-computed research-operational-health label, rather
  than inventing new spellings — two vocabularies, not three.

### 0.4 Dashboard insertion points

- `dashboard/views/ops_overview.py::render()`: sections run system health →
  today's signals → paper portfolio → prospective evidence (trading-day) →
  prospective day-ledger → research cache completeness → automation
  history → reconciliation. **Insertion point:** a new
  `_render_evidence_provenance_audit()` call, wrapped in the same
  `try/except: components.empty_state(...)` pattern as its neighbors,
  inserted immediately after the research-cache-completeness block and
  before `_render_automation_history()`.
- `dashboard/views/strategy_lab.py::render()`: the last two Phase 12
  sections are `_render_experiment_registry()` and
  `_render_prospective_evidence_progress()`, each independently
  try/except-wrapped. **Insertion point:** a new
  `_render_evidence_provenance_detail()` function, called last, in its own
  try/except block, after the existing final block.
- `dashboard/data.py` getters follow one fixed convention:
  `@st.cache_data(ttl=...)`, thin function that opens `db_session()` and
  calls exactly one `ops.*`/`strategy_lab.*` read function. New getters
  for Phase 15 must follow this exact shape.

### 0.5 Orchestrator-resolved open questions (confirmed before implementation)

1. **Real AAPL source-match question — RESOLVED with confirmed evidence**
   (§0.2 finding 2): the real observation's `source` is `NULL` → resolved
   to production `alpaca` at maturation time → the Phase 14 correction
   (scoped to `alpaca_adjusted` only) did NOT actually affect this
   outcome's stored value. `source_matched=False` for this specific real
   row. Component B's join must still implement the strict match generally
   (this is one confirmed instance, not a reason to loosen the join logic
   — a future row with `source="alpaca_adjusted"` populated would
   correctly show `source_matched=True`).
2. **Whether `outcome_maturation.py` should eventually price outcomes off
   `RESEARCH_SOURCE` instead of the production source: confirmed OUT OF
   SCOPE for Phase 15.** Not changed here. This is a separate, real design
   question for a future phase, not silently fixed by loosening Component
   B's join or by changing `_compute_one`'s source resolution.
3. **The new `strategy_lab → ops` import edge** (function-local import in
   `research_automation.py` for `compute_config_fingerprint()`): APPROVED.
   It mirrors `ops.experiment_registry.compute_config_fingerprint()`'s own
   existing convention of a local, function-scoped import to cross this
   exact package boundary, avoids widening `research_automation.py`'s
   static import surface, and is the only way to reuse the fingerprint
   mechanism verbatim (per the hard constraint against reimplementing it).
   Confirmed non-circular (`ops.experiment_registry` only imports
   `strategy_lab.production_guard`, which imports nothing from `ops`).
4. **Fail-open provenance capture** (a fingerprinting failure must never
   break the research job itself — logs a warning, stores
   `config_fingerprint=NULL`, records a `research_run_ticker_errors` row
   with `ticker=None` for visibility, does NOT fail the job status):
   APPROVED. Provenance capture is diagnostic metadata; it must never be
   able to block a genuine observation from being recorded.

## 1. Component A — Evidence provenance (additive schema + insert-time capture)

### 1.1 `strategy_lab/prospective.py`

- `ensure_schema(conn)` (`:76-87`): extend the existing guarded-migration
  block with one more check:
  ```python
  if "config_fingerprint" not in cols:
      conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN config_fingerprint TEXT")
  ```
  (mirrors the exact existing pattern for `source`/`methodology_version`).
- `record_observation(conn, *, ..., source: Optional[str] = None,
  methodology_version: str = METHODOLOGY_VERSION,
  config_fingerprint: Optional[str] = None) -> bool`: add
  `config_fingerprint` to the INSERT column list and values tuple. Default
  `None` — a caller that doesn't pass it gets `NULL`, never a guessed value.
- `build_todays_observation(conn, ticker: str, as_of_date: Optional[str] =
  None, config_fingerprint: Optional[str] = None) -> Optional[dict]`: add
  the parameter, include `"config_fingerprint": config_fingerprint` in the
  returned dict (alongside the existing `"source": resolve_source(...)`).

### 1.2 `strategy_lab/prospective_events.py`

- `ensure_schema(conn)`: add the same `PRAGMA table_info` + guarded
  `ALTER TABLE` pattern for two new columns on `research_prospective_events`:
  `source TEXT`, `config_fingerprint TEXT`.
- `record_event(conn, *, event_date, ticker, event_type, score, stage,
  regime, methodology_version=METHODOLOGY_VERSION, source: Optional[str] =
  None, config_fingerprint: Optional[str] = None) -> bool`: add both new
  kwargs to the INSERT.
- `record_events_for_observation(conn, curr_row: dict) -> Tuple[List[str],
  List[str]]`: when calling `record_event(...)`, additionally pass
  `source=curr_row.get("source"), config_fingerprint=curr_row.get("config_fingerprint")`
  — `curr_row` is the same dict `build_todays_observation` produced, which
  now carries both fields.

### 1.3 `strategy_lab/outcome_maturation.py`

- `ensure_schema(conn)`: add guarded `ALTER TABLE` for three new columns on
  `research_prospective_outcomes`: `source TEXT`, `methodology_version TEXT`,
  `config_fingerprint TEXT` (denormalized, matching the existing
  `ticker`/`observation_date` convention on this same table).
- `MaturationOutcome` dataclass: add fields `source: Optional[str] = None`,
  `methodology_version: Optional[str] = None`,
  `config_fingerprint: Optional[str] = None`.
- `_compute_one(...)`: populate the three new fields on every returned
  `MaturationOutcome` (including PENDING/UNAVAILABLE branches, not just
  MATURED) from the **observation row's own stored values** —
  `obs_row.get("source")`, `obs_row.get("methodology_version")`,
  `obs_row.get("config_fingerprint")`. **Never recompute a fresh
  fingerprint at maturation time** — provenance must reflect what
  methodology/config produced the *original prediction*, not whatever
  config is live when maturation runs later.
- `_persist(...)`: extend the INSERT/`ON CONFLICT DO UPDATE` to include
  `source`, `methodology_version`, `config_fingerprint` in both the column
  list and the `SET` clause. Safe/idempotent in `DO UPDATE` because these
  three values are always re-derived from the same immutable observation
  row on every call.
- Add one new **public, read-only** function, exposed specifically so
  Component B can reuse the exact maturation math without importing
  anything write-capable:
  ```python
  def compute_outcome_for_horizon(
      conn, obs_row, horizon_days: int, today: date,
      price_cache: Optional[dict] = None,
  ) -> MaturationOutcome:
      """Public alias for _compute_one — pure read+compute, no
      conn.execute/commit anywhere in this function or anything it calls
      (load_price_history is itself read-only). Exists so
      ops/correction_impact_audit.py can deterministically recompute a
      single outcome without duplicating this logic and without importing
      _persist or mature_outcomes."""
      return _compute_one(conn, obs_row, horizon_days, today, price_cache if price_cache is not None else {})
  ```

### 1.4 `strategy_lab/research_automation.py` — threading the fingerprint through one job run

- Add a local helper (fail-open, per §0.5 item 4):
  ```python
  def _compute_config_fingerprint_safe(conn, run_id: int, trading_date: date) -> Optional[str]:
      """Best-effort provenance capture — a fingerprinting failure must
      never break the research job itself. Local import (mirrors
      ops.experiment_registry.compute_config_fingerprint's own local-import
      convention for crossing this exact package boundary) — the one
      deliberate strategy_lab -> ops dependency edge in this codebase,
      approved per §0.5 item 3."""
      try:
          from ops.experiment_registry import compute_config_fingerprint
          return compute_config_fingerprint()
      except Exception as e:
          logger.warning("research job: could not compute config_fingerprint for provenance: %s", e)
          record_ticker_error(
              conn, run_id, ticker=None, source=None, trading_date=trading_date.isoformat(),
              reason=f"provenance: config_fingerprint computation failed: {type(e).__name__}: {e}",
          )
          return None
  ```
- In `run_research_job(...)`: call this **once**, immediately after
  `run_id = _start_run(conn, today)` and before the ticker loop, so every
  observation/event in this one run shares an identical fingerprint value.
  Pass `config_fingerprint=config_fingerprint` into
  `build_todays_observation(conn, ticker, as_of_date=today.isoformat(),
  config_fingerprint=config_fingerprint)`. No change needed to the
  `mature_outcomes(conn, today=today)` call — since `load_observations()`
  already does `SELECT *`, the new `config_fingerprint` column on
  observations is automatically visible to `_compute_one` with zero extra
  plumbing.

### 1.5 Legacy-row semantics (must be tested explicitly)

- Any `research_prospective_observations`/`research_prospective_events`
  row created **before** this migration ships: `config_fingerprint` is
  `NULL` forever — never backfilled, never guessed. **Confirmed real
  case:** the live DB's `id=1` AAPL observation already has `source=NULL`
  and `methodology_version=NULL` (§0.2 finding 2) — this is exactly the
  legacy scenario this component must handle correctly, not hypothetically.
- Any `research_prospective_outcomes` row that already existed before this
  migration: the **very next** `mature_outcomes()` call after deployment
  re-persists every observation×horizon pair (existing behavior, always
  has) and will therefore retroactively populate `source`/
  `methodology_version` on old outcome rows from the parent observation's
  **already-known, immutable** `source`/`methodology_version` columns —
  this is a factual copy of pre-existing data onto a sibling denormalized
  column, **not** a backfill/guess. `config_fingerprint` on those same
  outcome rows will **only** become non-NULL if the parent observation
  itself has a non-NULL `config_fingerprint` — for the real `id=1` AAPL
  row (and any pre-Phase-15 observation), `config_fingerprint` stays
  `NULL` on its outcomes forever, correctly propagating "unknown" rather
  than fabricating a value.

## 2. Component B — `ops/correction_impact_audit.py` (new file, read-only, zero write path)

Structurally read-only, not just conventionally: this module never calls
`conn.execute`/`conn.executemany`/`conn.executescript`/`conn.commit`
directly, never contains an `INSERT`/`UPDATE`/`DELETE` SQL string, and
never imports `record_observation`, `record_event`, `mature_outcomes`,
`_persist`, `refresh_incomplete_latest_bars`, or `_record_correction`.
Only imports: `pandas.read_sql_query` (via `conn`), `CORRECTIONS_TABLE`
(a string constant) from `strategy_lab.cache_integrity`,
`load_observations` from `strategy_lab.prospective`, `load_outcomes` +
`STATUS_MATURED`/`STATUS_PENDING`/`STATUS_UNAVAILABLE` +
`compute_outcome_for_horizon` (§1.3) from `strategy_lab.outcome_maturation`.

```python
def load_corrections(conn, ticker: Optional[str] = None) -> pd.DataFrame:
    """Pure SELECT over research_cache_corrections (Phase 14). Returns an
    empty DataFrame (not a raised exception) if the table doesn't exist yet
    on a fresh DB with zero corrections ever recorded."""

@dataclass
class AffectedOutcome:
    outcome_id: int
    observation_id: int
    ticker: str
    observation_date: str
    horizon_days: int
    exit_date: Optional[str]
    status: str
    affected_leg: str        # "entry" | "exit"
    source_matched: bool     # True only if outcome's source == correction's source
    stored_realized_return: Optional[float]
    correction_id: int

def find_outcomes_affected_by_correction(conn, correction_row: dict) -> List[AffectedOutcome]:
    """Loads research_prospective_outcomes (load_outcomes), matches:
      entry leg: outcome.observation_date == correction_row['date']
                  AND outcome.ticker == correction_row['ticker']
      exit leg:  outcome.exit_date == correction_row['date']
                  AND outcome.ticker == correction_row['ticker']
    For each candidate match, resolves the effective source used at
    maturation: outcome.source if non-NULL (post-Phase-15 rows), else
    (legacy NULL) falls back to a join against research_prospective_observations
    via observation_id to read that row's source column (same value
    _compute_one would have read at maturation time). `source_matched =
    (effective_source == correction_row['source'])`. Rows where
    source_matched is False are still RETURNED (never silently dropped —
    confirmed necessary by the real AAPL case, §0.2) but flagged so
    callers/dashboard can distinguish "not actually affected" from
    "affected." Read-only: SELECT-only, filtered in pandas. Never calls
    mature_outcomes()."""

@dataclass
class RecomputationResult:
    outcome_id: int
    observation_id: int
    ticker: str
    observation_date: str
    horizon_days: int
    stored_status: str
    stored_realized_return: Optional[float]
    stored_exit_date: Optional[str]
    recomputed_status: str
    recomputed_realized_return: Optional[float]
    recomputed_exit_date: Optional[str]
    mismatch: bool
    detail: str

def recompute_outcome_deterministically(
    conn, outcome_row: dict, observation_row: dict, today: Optional[date] = None,
) -> RecomputationResult:
    """Calls strategy_lab.outcome_maturation.compute_outcome_for_horizon
    (§1.3's new public read-only alias for _compute_one) against the
    CURRENT contents of `prices` — post any correction — WITHOUT calling
    mature_outcomes() and WITHOUT persisting anything. mismatch=True iff
    |stored_realized_return - recomputed_realized_return| > 1e-9 (treating
    None vs a float, or vice versa, as a mismatch), OR stored_status !=
    recomputed_status, OR stored_exit_date != recomputed_exit_date. Never
    writes stored_* anywhere — recomputed_* is returned to the caller for
    display/human review only."""

def audit_correction(conn, correction_id: int, today: Optional[date] = None) -> dict:
    """Primary entry point. Returns:
      {'correction': {...}, 'affected_outcomes': [AffectedOutcome...],
       'recomputations': [RecomputationResult...]}   # one per affected
                                                        # outcome whose
                                                        # stored status ==
                                                        # STATUS_MATURED
                                                        # only — a still-
                                                        # PENDING outcome has
                                                        # nothing stored yet
                                                        # to compare against,
                                                        # recomputation is
                                                        # skipped, not
                                                        # fabricated.
    Raises ValueError if correction_id not found. Zero writes."""

def audit_all_corrections(conn, today: Optional[date] = None) -> pd.DataFrame:
    """Flattens audit_correction() over every research_cache_corrections
    row into one row per (correction, affected_outcome) pair — used by
    Component C's correction-count rollup and Component D's dashboard
    table. Empty DataFrame if there are no corrections."""
```

**Answering the AAPL case with these functions** (documented, not a
dedicated AAPL-named function): `load_corrections(conn, ticker="AAPL")`
→ find the row with `date == "2026-08-12"` → pass that row's dict into
`find_outcomes_affected_by_correction` → filter the result for
`horizon_days == 1` → per the confirmed real evidence (§0.2 finding 2),
`affected_leg` will read `"entry"` but `source_matched` will read `False`
— the correction is a candidate match structurally (same ticker/date) but
was NOT actually consequential to this outcome's stored value, because the
real observation's `source` is `NULL` (resolved to production `alpaca` at
maturation time, never `alpaca_adjusted`).

## 3. Component C — extend `ops/prospective_audit.py` (additive functions, same file, existing functions untouched)

New file: `ops/evidence_provenance.py` (small, shared, read-only) — used
by both C and D so the "legacy/unknown" label is defined exactly once:

```python
PROVENANCE_UNKNOWN_LEGACY = "UNKNOWN_LEGACY"

def label_provenance_value(value: Optional[str]) -> str:
    """value if non-NULL else PROVENANCE_UNKNOWN_LEGACY. Never guesses."""
    return value if value else PROVENANCE_UNKNOWN_LEGACY
```

New functions **added to** `ops/prospective_audit.py` (imports at top gain
`from ops.data_quality import OVERALL_HEALTHY, OVERALL_DEGRADED,
OVERALL_STALE, OVERALL_FAILED`, `from ops.experiment_registry import
list_experiments`, `from ops.correction_impact_audit import
load_corrections`, `from ops.evidence_provenance import
PROVENANCE_UNKNOWN_LEGACY, label_provenance_value`):

```python
def compute_capture_rate(conn, today: Optional[date] = None, window_sessions: int = 60) -> dict:
    """Built on build_prospective_day_ledger() (reused, not re-queried).
    Over the ledger days within the trailing window_sessions NYSE sessions
    (never extending before eligibility_start_date(conn)):
    capture_rate_pct = 100 * (CAPTURED + CAPTURED_PARTIAL + DUPLICATE_SKIPPED)
                        / (all ledger days in window EXCEPT status==EXPECTED)
    EXPECTED (today, job hasn't run yet) is excluded from both numerator
    and denominator. Returns {'window_sessions_considered', 'captured',
    'missed', 'unavailable', 'capture_rate_pct' (None if considered==0)}."""

def compute_consecutive_missed_days(conn, today: Optional[date] = None) -> int:
    """Trailing run-length of PROSPECTIVE_DAY_MISSED, counting backward
    from the most recent non-EXPECTED ledger day. 0 if that day isn't MISSED."""

def compute_maturity_lag(conn) -> dict:
    """Per horizon_days, over MATURED outcomes only: mean/median CALENDAR-
    day lag (date.fromisoformat(exit_date) - date.fromisoformat(observation_date)).days.
    Session-day lag is NOT separately reported — it always equals
    horizon_days by construction, so a second number would just restate
    horizon_days. Also reports mean 'pipeline notice lag' — calendar days
    between the NYSE session on which the horizon first genuinely elapsed
    and matured_at's calendar date — 0 for the expected/common case, >0
    only if a research job was MISSED on the day maturity first became
    available (cross-referenced against build_prospective_day_ledger()).
    Returns {horizon_days: {'n_matured', 'mean_calendar_day_lag',
    'median_calendar_day_lag', 'mean_pipeline_notice_lag_days'}}."""

def compute_research_job_rates(conn, today: Optional[date] = None, window_sessions: int = 60) -> dict:
    """Over load_research_run_history(), collapsed to one row per
    trading_date (reusing the same latest-row-per-date logic
    build_prospective_day_ledger() already applies, imported not
    reimplemented) within the trailing window_sessions:
    success_rate_pct / partial_failure_rate_pct / failure_rate_pct /
    crashed_rate_pct (STATUS_RUNNING left stuck), denominator excludes
    STATUS_SKIPPED_NON_TRADING_DAY."""

def compute_correction_counts(conn, since: Optional[str] = None) -> dict:
    """Reuses ops.correction_impact_audit.load_corrections() (imported,
    not reimplemented). {'total', 'affecting_frozen_artifacts', 'since'}."""

def compute_provenance_coverage(conn) -> dict:
    """{'observations_with_fingerprint', 'observations_unknown_legacy',
    'by_experiment_id': {experiment_id: n}, 'unregistered_fingerprint_count'}.
    For rows with non-NULL config_fingerprint: cross-references
    ops.experiment_registry.list_experiments()'s config_fingerprint column
    — one observation's fingerprint can match MULTIPLE experiment_ids
    (§0.1, CONTROL/A/B share one fingerprint today), so by_experiment_id
    counts are NOT mutually exclusive. A fingerprint matching ZERO
    currently-registered experiment_id goes to 'unregistered_fingerprint_count'
    — distinct from observations_unknown_legacy (NULL column)."""

def classify_research_operational_health(
    capture_rate_pct: Optional[float], consecutive_missed_days: int,
    job_failure_rate_pct: Optional[float],
) -> str:
    """Pure, deterministic. Returns one of ops.data_quality.OVERALL_HEALTHY/
    DEGRADED/STALE/FAILED — imported and reused verbatim. A SEPARATE
    classification function from ops.data_quality.classify_overall
    (different input domain) sharing the same 4-value vocabulary.
      FAILED:   consecutive_missed_days >= 5, OR capture_rate_pct is None.
      STALE:    consecutive_missed_days in [2, 4].
      DEGRADED: consecutive_missed_days in [0, 1] AND
                 (capture_rate_pct < 90 OR (job_failure_rate_pct or 0) > 10).
      HEALTHY:  otherwise.
    Explicitly NOT ops/evidence_classification.py's
    INSUFFICIENT_DATA/EARLY_EVIDENCE/EVALUATION_READY vocabulary (untouched
    — that measures evidence SUFFICIENCY, this measures pipeline
    RELIABILITY); the two must always be shown side by side, never merged."""

def long_term_monitoring_summary(conn, today: Optional[date] = None) -> dict:
    """Additive rollup: returns prospective_evidence_audit_summary(conn, today)'s
    existing dict (UNCHANGED keys) with new keys merged in: 'capture_rate',
    'consecutive_missed_days', 'maturity_lag_by_horizon',
    'research_job_rates', 'correction_counts', 'provenance_coverage',
    'operational_health'. This is the one function Component D's dashboard
    getter calls — never call the individual compute_* functions directly
    from a dashboard view."""
```

## 4. Component D — Dashboard: "Evidence Provenance / Audit" section (read-only, no new write action anywhere)

### 4.1 `dashboard/data.py` — new getters (mirror existing `@st.cache_data` shape)

```python
@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_long_term_monitoring_summary() -> dict:
    from ops.prospective_audit import long_term_monitoring_summary
    with db_session() as conn:
        return long_term_monitoring_summary(conn)

@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_correction_audit_report(limit: int = 50) -> pd.DataFrame:
    from ops.correction_impact_audit import audit_all_corrections
    with db_session() as conn:
        df = audit_all_corrections(conn)
    return df.tail(limit) if not df.empty else df
```

### 4.2 `dashboard/views/ops_overview.py` — new section

```python
def _render_evidence_provenance_audit():
    st.header("Evidence provenance / audit (Phase 15)")
    st.caption(
        "Methodology/config fingerprint coverage, correction-impact trail, "
        "and long-term capture/maturity monitoring — read-only, distinct "
        "from the operational System health section above and the "
        "trading-day evidence progress section above it."
    )
    summary = get_long_term_monitoring_summary()
    st.metric("Operational health", summary["operational_health"])
    # capture rate, consecutive missed days, research job rates, maturity
    # lag by horizon, provenance coverage (with UNKNOWN_LEGACY counts
    # visually distinct — st.caption, never silently omitted), correction
    # counts — each rendered as st.metric/st.dataframe, same visual idiom
    # as the existing Phase 13/14 expander blocks.
    corrections = get_correction_audit_report()
    if not corrections.empty:
        st.dataframe(corrections, use_container_width=True, hide_index=True)
    else:
        st.caption("No corrections recorded — no correction-impact trail to show.")
```
Called in `render()` right after the existing research-cache-completeness
block and before `_render_automation_history()`, wrapped in the same
try/except pattern as every other section in this file.

### 4.3 `dashboard/views/strategy_lab.py` — new section

`_render_evidence_provenance_detail()`: methodology version + fingerprint
per observed "era" (group `research_prospective_observations` by
`config_fingerprint`, min/max `observation_date` per group, label NULL
group as `UNKNOWN_LEGACY` via `ops.evidence_provenance.label_provenance_value`),
provenance coverage table (`compute_provenance_coverage`'s
`by_experiment_id` map, with the many-to-one caveat rendered as an
`st.caption`, not hidden), correction history + affected-outcomes list
(reuse `get_correction_audit_report()`, same getter as D2 — no duplicate
query), current evidence status **shown, but never merged with**
`_render_prospective_evidence_progress()`'s own INSUFFICIENT_DATA/
EARLY_EVIDENCE/EVALUATION_READY block just above it. Called last in
`render()`, in its own try/except, after the existing final block.

**No new write action anywhere in D** — every element here is `st.metric`/
`st.dataframe`/`st.caption`/`st.warning`/`st.error` reading an already-
cached getter; no `st.button`, no form, no session-state mutation that
triggers a backend call.

## 5. Hard constraints (restated per component — all verified against the design above)

- No changes to signal weights/thresholds, entry/exit rules,
  CONTROL/A/B methodology, risk limits, watchlist, alert logic/cooldown,
  Discord config, either LaunchAgent/schedule. Components A-D touch only
  `strategy_lab/prospective*.py`, `strategy_lab/outcome_maturation.py`,
  `strategy_lab/research_automation.py`, new `ops/*.py`, and dashboard
  view files — zero touches to `signals/`, `backtest/config.py`,
  `trading/`, `alerts/`, `config/settings.py`, or `deploy/`.
- No new trading hypothesis/optimization; no paper/live orders; no Discord
  sends; no new scheduled jobs. No new file under `automation/` or
  `deploy/`; existing structural safety tests already scan every file in
  `strategy_lab/` for `trading.*`/`alerts.*` imports — Phase 15's
  new/changed files there are automatically covered with zero spec changes
  needed to that test.
- No provider auto-switching, no retrospective backfill of missed
  prospective predictions. Component A never populates `config_fingerprint`
  on a pre-existing row (§1.5); Component B/C never call
  `record_observation`/`record_event`.
- Existing prospective predictions remain immutable — **no UPDATE path
  added to `research_prospective_observations` or
  `research_prospective_events`, ever.** §1.1/§1.2 only add
  `ALTER TABLE ADD COLUMN` + extend existing insert-only functions' column
  lists; no new `UPDATE ... SET` statement anywhere in either file — the
  existing `_has_update_sql`-style test in `tests/test_ops_prospective_audit.py`
  must keep passing unmodified.
- Do not regenerate frozen Phase 9-11 artifacts; do not modify historical
  evidence to make results look cleaner. No import of
  `strategy_lab.run_study`/`run_phase10_study`/`run_phase11_study`/`report`/
  `report_phase10`/`phase11_report`/`phase9_baseline`/`phase10_baseline`
  anywhere in Components A-D.
- Any ambiguous correction/evidence mutation must be flagged for explicit
  human approval before any code executes it, never auto-applied.
  Component B has zero write path at all (see below); D has zero write action.
- **The Phase 14 incident** (a live batch-correction command run before
  checking frozen-artifact impact) is structurally prevented here because
  Component B's module contains **no mutation capability in the code at
  all** — not "callers are trusted." Confirmed by an AST/source-text scan
  test that fails the build if a future edit ever adds one.

## 6. Files: full list

**New:**
- `ops/correction_impact_audit.py` (Component B)
- `ops/evidence_provenance.py` (Component C/D shared label helper)
- `tests/test_ops_correction_impact_audit.py`
- `tests/test_ops_evidence_provenance.py`
- `tests/test_ops_prospective_audit_phase15.py` (extends Component C's
  testing; kept separate from `tests/test_ops_prospective_audit.py` — but
  imports the SAME module, `ops.prospective_audit`)

**Modified:**
- `strategy_lab/prospective.py` (§1.1)
- `strategy_lab/prospective_events.py` (§1.2)
- `strategy_lab/outcome_maturation.py` (§1.3, adds `compute_outcome_for_horizon`)
- `strategy_lab/research_automation.py` (§1.4)
- `ops/prospective_audit.py` (Component C, additive functions only)
- `dashboard/data.py` (§4.1)
- `dashboard/views/ops_overview.py` (§4.2)
- `dashboard/views/strategy_lab.py` (§4.3)

**Untouched (confirm, do not edit):**
- `db/schema.py` — no `ALL_STATEMENTS` change; Phase 15's tables are all
  already owned by their respective modules' own `ensure_schema()`.
- `ops/evidence_classification.py`, `ops/data_quality.py`,
  `ops/provider_reconciliation.py`, `ops/experiment_registry.py`,
  `strategy_lab/production_guard.py`, `strategy_lab/cache_integrity.py` —
  read/imported from, never modified.

## 7. Migration syntax (exact, mirroring `strategy_lab/prospective.py:78-87`)

```python
def ensure_schema(conn) -> None:
    conn.execute(_CREATE_TABLE_SQL)
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()}
    if "config_fingerprint" not in cols:
        conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN config_fingerprint TEXT")
    conn.commit()
```
Applied per-table as spec'd in §1.1 (observations: +1 column), §1.2
(events: +2 columns), §1.3 (outcomes: +3 columns). Never edit
`db/schema.py`'s `CREATE_*_TABLE_SQL` strings — none of these three tables
are defined there.

## 8. Testing checklist

**Component A — provenance on new rows / legacy labeling:**
1. `record_observation(..., config_fingerprint="abc123")` then
   `load_observations()` round-trips the value exactly.
2. A pre-migration-shaped row has `config_fingerprint IS NULL` after
   `ensure_schema()` runs — never silently defaulted to anything else.
3. `build_todays_observation(..., config_fingerprint=None)` (the default)
   produces a dict with `"config_fingerprint": None`.
4. Two tickers processed within the **same** `run_research_job()` call
   receive byte-identical `config_fingerprint` values (computed once,
   threaded through) — test by monkeypatching
   `ops.experiment_registry.compute_config_fingerprint` to return a
   sequence of different values on successive calls and asserting it was
   only actually called once per job run.
5. `_compute_config_fingerprint_safe` raising results in
   `config_fingerprint=None` on every observation from that run, a
   `research_run_ticker_errors` row with `ticker=None` and a `reason`
   mentioning "config_fingerprint", AND `result.status` still reaches
   `STATUS_SUCCESS_RESEARCH` (or `PARTIAL_FAILURE_RESEARCH` only if a
   *different*, unrelated error also occurred).
6. **Legacy regression test, mirroring the real confirmed AAPL case:** seed
   a `research_prospective_observations` row with `source=NULL,
   methodology_version=NULL, config_fingerprint=NULL` (exactly matching
   the real `id=1` AAPL row's shape) and a matching
   `research_prospective_outcomes` row, then call `mature_outcomes()`
   again post-migration and assert: `source`/`methodology_version` on the
   outcome row STAY NULL too (since the parent observation's own values
   are NULL — nothing to copy), `config_fingerprint` remains NULL on both
   rows (never fabricated).
7. `compute_outcome_for_horizon` produces byte-identical results to an
   equivalent hand-constructed call into the still-private `_compute_one`
   for the same inputs — confirms the public alias didn't fork the logic.

**Component B — correction-impact audit:**
8. Structural: (a) AST/source-text scan confirms `ops/correction_impact_audit.py`
   contains no `INSERT`/`UPDATE`/`DELETE` SQL keyword and no line matching
   `\.execute\(|\.executemany\(|\.executescript\(|\.commit\(` outside
   comments; (b) AST `ImportFrom` scan asserts no imported name starts
   with `record_`, `mature_`, `save_`, or equals `_persist`,
   `_record_correction`, `_store_adjusted_bars`, `refresh_incomplete_latest_bars`.
9. `find_outcomes_affected_by_correction` on a **safe, synthetic fixture
   mirroring the real, CONFIRMED AAPL case** (not the live DB): seed one
   observation `(ticker="AAPL", observation_date="2026-08-12",
   source=None)` [matching the real row's actual NULL source, per §0.2],
   mature a `horizon_days=1` outcome against a synthetic 2-day
   `alpaca`-sourced price series, then seed a `research_cache_corrections`
   row `(ticker="AAPL", date="2026-08-12", source="alpaca_adjusted",
   old_close=X, new_close=Y)` — assert exactly one `AffectedOutcome`
   returned, `affected_leg="entry"`, **`source_matched=False`** (this is
   the real-world outcome, confirmed against actual data, not a
   hypothetical).
10. Same fixture but with the observation's `source="alpaca_adjusted"`
    (matching what a POST-Phase-15 observation would look like) — assert
    `source_matched=True`, proving the join correctly detects a genuine
    match when one exists.
11. Exit-leg match: an earlier observation (`observation_date` = N trading
    sessions before the corrected date) with `horizon_days=N` whose
    `exit_date` lands exactly on the corrected date — asserts
    `affected_leg="exit"`.
12. A correction with no matching `(ticker, date)` in any observation
    returns an empty list — never raises.
13. `recompute_outcome_deterministically`: seed a MATURED outcome, then
    directly mutate the underlying `prices` row's `close` value (simulating
    a correction having been applied), call the function, assert
    `mismatch=True` and both `stored_realized_return` and
    `recomputed_realized_return` are populated and differ; assert calling
    it twice in a row produces identical `RecomputationResult`s and the
    underlying `research_prospective_outcomes` row's
    `realized_return`/`matured_at` are provably unchanged in the DB.
14. `audit_correction` on a PENDING outcome: `recomputations` list is
    empty for that outcome.
15. `audit_correction(conn, correction_id=99999)` raises `ValueError`.

**Component C — long-term monitoring:**
16. `compute_capture_rate`: a ledger with `[CAPTURED, CAPTURED, MISSED,
    EXPECTED]` → `capture_rate_pct` computed over 3 days (EXPECTED
    excluded), not 4.
17. `compute_consecutive_missed_days`: `[..., MISSED, MISSED, EXPECTED]`
    (today) → counts the two MISSED days, skipping over EXPECTED.
18. `compute_maturity_lag`: synthetic MATURED outcomes across weekends —
    assert calendar-day lag correctly exceeds `horizon_days` trading-day
    lag around a weekend/holiday.
19. `compute_research_job_rates`: multiple `research_run_history` rows on
    the SAME `trading_date` (retry-after-failure scenario) collapse to the
    latest row only — assert a failed-then-succeeded-on-retry day counts
    as success, not double counted.
20. `compute_provenance_coverage`: seed observations with 3 different
    `config_fingerprint` values, register 2 experiments sharing ONE of
    those fingerprints (mirroring the real CONTROL/A/B pattern) and leave
    the third fingerprint unregistered — assert `by_experiment_id` has
    that one fingerprint's observation count under BOTH experiment_ids,
    the third fingerprint's rows appear under `unregistered_fingerprint_count`,
    and NULL-fingerprint rows appear under `observations_unknown_legacy`.
21. `classify_research_operational_health`: table-driven test for every
    boundary (0/1/2/4/5 consecutive missed days; 89%/90%/91% capture rate
    at the DEGRADED boundary; `capture_rate_pct=None` forces FAILED).
22. Vocabulary-separation test: assert
    `ops.prospective_audit.classify_research_operational_health` output
    values are drawn from the exact same string set as
    `ops.data_quality.OVERALL_HEALTHY/DEGRADED/STALE/FAILED`, AND assert
    none of those four strings ever equal any of
    `ops.evidence_classification.EVIDENCE_INSUFFICIENT_DATA/EARLY_EVIDENCE/
    EVALUATION_READY`.
23. `long_term_monitoring_summary` is a strict superset of
    `prospective_evidence_audit_summary`'s keys for identical inputs.
24. Idempotency: calling `compute_capture_rate`/`compute_research_job_rates`
    twice in a row against unchanged data returns identical results.

**No-look-ahead (cross-cutting, Components A-C):**
25. `compute_maturity_lag` and `compute_capture_rate` never consider a
    ledger day or outcome dated after the `today` parameter passed in.

**Timezone / NYSE-calendar boundary tests:**
26. Extend the existing DST pattern from
    `tests/test_strategy_lab_cache_integrity.py` for any new date-arithmetic
    in Component C.

**Dashboard read-only safety:**
27. Extend the existing structural pattern (grep for `st.button`/`st.form`)
    to assert the two new render functions contain none.
28. `get_long_term_monitoring_summary` / `get_correction_audit_report`:
    assert calling them twice against an unchanged DB returns identical
    results, and that neither function ever opens more than one
    `db_session()`.

**Duplicate/retry idempotency:**
29. Running `run_research_job()` twice for the same `today` produces
    `duplicates_skipped` observations on the second call (existing
    behavior) AND identical `config_fingerprint` values on the (unchanged)
    first-call rows — the second call must never touch `config_fingerprint`
    on the already-inserted row.

## 9. Manual/integration verification before moving on from Phase 15

1. `pytest tests/` — full suite green.
2. Real, read-only verification of the AAPL case against the live DB:
   confirm `audit_correction` for the real AAPL 2026-08-12 correction
   reproduces `source_matched=False` as documented in §0.2 — this is
   read-only (Component B has no write path at all), safe to run directly
   against the real DB with zero risk.
3. `streamlit run dashboard/app.py` — load both pages, confirm the new
   sections render without exception.
4. `git diff` touches only files listed in §6.
