# Phase 14 — Research Data Lifecycle, Prospective Evidence Integrity, and Unattended Reliability

Status: PLANNED. No implementation in this document. Coder implements exactly
this; tester validates exactly this; reviewer checks against exactly this.

## 0. Verified current state (2026-08-14, HEAD 28fb8db)

### 0.1 The Phase 13 stale-bar bug — confirmed at `strategy_lab/data.py:61-63`

```python
def _needs_fetch(conn, ticker: str, min_rows: int) -> bool:
    existing = load_price_history(conn, ticker, source=RESEARCH_SOURCE)
    return len(existing) < min_rows
```

`fetch_and_cache_universe` (`strategy_lab/data.py:66-111`) only calls Alpaca
for a ticker when its **total row count** is below `min_rows` (a ~5-year
floor, `strategy_lab/data.py:73`). Once a ticker has enough rows, it is
`"cached"` (line 76-78) and **never refetched again, regardless of how stale
or preliminary its single most recent row is.** If acquisition runs
intraday (before NYSE close), Alpaca's `Adjustment.ALL` bar for the
still-open session is preliminary; `_store_adjusted_bars` (`strategy_lab/data.py:41-58`)
upserts it with `fetched_at=datetime('now')`. After that session genuinely
closes, `_needs_fetch` still returns `False` for that ticker (row count is
already sufficient) — the preliminary bar is permanently frozen as if final.

`ops/provider_reconciliation.py` already **detects** this pattern
(`CLASS_STALE_SOURCE`, check #4, `ops/provider_reconciliation.py:223-234`),
via `_is_intraday_fetch` (`ops/provider_reconciliation.py:141-157`, naive-UTC
`fetched_at` parsed and compared in `America/New_York` against
`NYSE_CLOSE_TIME = time(16, 0)`, `ops/provider_reconciliation.py:79-80`) —
but it is read-only reporting; nothing repairs it. Phase 14 Component A adds
the repair, reusing the same NYSE-close-time semantics and naive-UTC
`fetched_at` convention.

`automation/trading_calendar.py` provides `is_likely_trading_day`,
`trading_sessions_elapsed`, `trading_sessions_between` (real
`pandas_market_calendars`-backed NYSE sessions) but **no session-close-time
helper** — every module that needs one defines its own local
`NYSE_TZ`/`NYSE_CLOSE_TIME` constants rather than a shared helper
(`ops/data_quality.py:107-116` and `ops/provider_reconciliation.py:325-335`
both keep a private `_most_recent_completed_session` "to avoid a circular
import"). Component A follows this established, twice-duplicated
convention: a third small, local, non-exported helper — not a refactor of
the existing two.

`prices` has `UNIQUE(ticker, date, source)` (`db/schema.py:5-19`) and
`_store_adjusted_bars` upserts via `ON CONFLICT(...) DO UPDATE SET ...,
fetched_at=datetime('now')` (`strategy_lab/data.py:47-57`) — a corrected
refetch is naturally idempotent and self-terminating (the row's `fetched_at`
becomes post-close on the write that fixes it, so it will never be flagged
incomplete again).

`db.price_repository.SOURCE_PRIORITY = ["yfinance", "alpaca"]`
(`db/price_repository.py:5`) — `"alpaca_adjusted"` (i.e. `RESEARCH_SOURCE`)
is never in this list and never resolved by `resolve_source`/
`load_price_history` when `source` is omitted. Component A cannot touch
production reads even by construction, since it only ever writes rows tagged
`source="alpaca_adjusted"`.

### 0.2 The full prospective lifecycle — traced end to end

Production success → research observation → event → outcome maturation:

1. `automation/pipeline.py` writes `automation_runs.trading_date` +
   `status` (`db/run_history_repository.py:35-66`, `STATUS_SUCCESS = "success"`).
2. `strategy_lab/research_automation.py::run_research_job` gates on
   `_todays_production_run_succeeded` (`strategy_lab/research_automation.py:84-110`,
   exact `trading_date` match, Phase 13 fix) before building anything.
3. Per ticker: `build_todays_observation` (pure compute,
   `strategy_lab/prospective.py:130-167`) → `record_observation`
   (`strategy_lab/prospective.py:90-120`) — **INSERT ... ON CONFLICT(ticker,
   observation_date) DO NOTHING**, no `UPDATE` anywhere in the module. The
   table has **no forward-return columns at all** (`strategy_lab/prospective.py:51-73`)
   — structurally impossible to record an outcome at observation time.
4. `record_events_for_observation` (`strategy_lab/prospective_events.py:133-143`)
   → `record_event` (`strategy_lab/prospective_events.py:115-130`) — **INSERT
   ... ON CONFLICT(ticker, event_type, event_date) DO NOTHING**, no `UPDATE`
   anywhere in the module.
5. `mature_outcomes` (`strategy_lab/outcome_maturation.py:134-153`) computes
   from `automation.trading_calendar.trading_sessions_elapsed` (never
   calendar-day arithmetic, never "does a row happen to exist") and
   `_persist` (`strategy_lab/outcome_maturation.py:115-131`) — **UPSERT into
   the SEPARATE `research_prospective_outcomes` table**, keyed
   `(observation_id, horizon_days)`, `ON CONFLICT DO UPDATE`. This is the
   **one** legitimate mutation path in the whole lifecycle, and it never
   touches the observation row.

**Immutability: solid for observations/events (insert-only, no `UPDATE`
statement exists in either module). Duplicate prevention: solid at the DB
level** (unique constraints + `DO NOTHING`) **but has one real gap in the
retry/crash path**, found in `run_research_job` itself (§0.3 below) — not in
`prospective.py`/`prospective_events.py`.

**Outcome upsert re-derivation risk (ties Component A to Component B):**
`mature_outcomes` recomputes `realized_return` from
`load_price_history(conn, ticker, source=obs_row["source"])`
(`strategy_lab/outcome_maturation.py:81`) every time it runs, and re-upserts.
Its own docstring calls this "idempotent (same inputs, same output)" — only
true if the underlying `prices` rows never change after being read once.
Before Phase 14, that was true by omission. **After Component A ships, it is
possible for a `horizon_days=1` outcome's `exit_date` row to be exactly the
single "latest row" Component A's completeness check targets.** This is
real, not hypothetical, and is addressed explicitly in Component A's spec
(§1.6) rather than left as a silent side-effect.

**No existing per-trading-day status vocabulary.** `ops/evidence_classification.py`
(`ops/evidence_classification.py:14-46`) only counts `nunique(observation_date)`
against two fixed thresholds (`MIN_DAYS_EARLY_EVIDENCE=20`,
`MIN_DAYS_EVALUATION_READY=60`) — it has no concept of "a day that should
have had an observation but didn't." Nothing in the codebase currently
cross-references `automation_runs`, `research_run_history`, and
`research_prospective_observations` by calendar day. This is exactly
Component B's gap to fill (reporting only).

### 0.3 Research-job resilience gaps — confirmed at `strategy_lab/research_automation.py`

**Bug 1 — false "success" status on partial failure**
(`strategy_lab/research_automation.py:166-193`):

```python
for ticker in tickers:
    try:
        obs = build_todays_observation(...)
        if obs is None:
            continue
        inserted = record_observation(conn, **obs)
        if inserted:
            result.observations_created += 1
            event_types = record_events_for_observation(conn, obs)
            result.events_created += len(event_types)
        else:
            result.duplicates_skipped += 1
    except Exception as e:
        result.errors.append(f"{ticker}: {type(e).__name__}: {e}")   # <-- error recorded...

try:
    matured = mature_outcomes(conn, today=today)
    result.outcomes_matured = ...
except Exception as e:
    result.errors.append(f"maturation: {type(e).__name__}: {e}")     # <-- ...and here

if result.errors and result.observations_created == 0 and result.duplicates_skipped == 0:
    result.status = STATUS_FAILED
# else: result.status stays STATUS_SUCCESS_RESEARCH = "success", EVEN IF result.errors is non-empty
```

Any run where **at least one ticker succeeds** (or was a duplicate) keeps
`status="success"` no matter how many other tickers errored, and no matter
whether outcome maturation itself threw. `research_run_history.status`
becomes silently unreliable exactly when it matters most.

**Bug 2 — event creation is not idempotent under retry after a mid-loop
failure** (`strategy_lab/research_automation.py:171-176`, same block above):
`record_events_for_observation` is only called **inside** `if inserted:`.
`record_observation` is itself correctly idempotent
(`ON CONFLICT...DO NOTHING`), but that means on any date after the very
first successful `INSERT` for a `(ticker, date)`, every subsequent call
(including a deliberate retry specifically because event-building threw the
first time) returns `inserted=False` → falls into the `else:
duplicates_skipped += 1` branch → **`record_events_for_observation` is never
called again for that ticker/day, permanently**, even though nothing about
its own dedup logic (`ON CONFLICT(ticker, event_type, event_date) DO
NOTHING`) would have prevented a safe re-attempt. This is the actual gap
behind "ensure retries are fully idempotent (including events/outcomes, not
just observations)" — outcomes are already fine (`mature_outcomes` always
runs, unconditionally, once per job call); events are not.

**Diagnostics:** `research_run_history` (`strategy_lab/research_automation.py:47-62`)
has one flat `errors` TEXT column — no structured per-ticker/source/reason
breakdown; a caller must string-parse to find out which ticker(s) failed and why.

**Crash mid-run:** `_start_run` inserts `status='running'`
(`strategy_lab/research_automation.py:113-121`); if the process is killed
before `_finish_run` runs, the row is permanently stuck at `status='running'`
— this is the **same accepted, documented convention** production's
`automation_runs` already uses. Phase 14 does not change this convention;
Component B must simply classify a stuck `'running'` row for a past day as
`MISSED` in its audit (§2), and Component C must ensure diagnostics make it
visible (§3.5).

### 0.4 Dashboard extension points confirmed — no rebuild needed

- `dashboard/views/ops_overview.py`: `_render_prospective_evidence()`
  (`dashboard/views/ops_overview.py:163-183`), `_render_automation_history()`
  (`dashboard/views/ops_overview.py:185-207`), `render()`
  (`dashboard/views/ops_overview.py:246-271`).
- `dashboard/views/strategy_lab.py`: `_render_prospective_validation()`
  (`dashboard/views/strategy_lab.py:574-618`), `_render_prospective_evidence_progress()`
  (`dashboard/views/strategy_lab.py:730-800`, already the trading-day-based
  section — direct extension point), `render()`
  (`dashboard/views/strategy_lab.py:803-893`).
- `dashboard/data.py`: existing `get_*`/`_load_*` pairing convention,
  existing `get_prospective_evidence_status`, `get_ops_research_run_history`,
  `get_research_run_history`, `get_data_quality_report`,
  `clear_all_caches()` (`dashboard/data.py:551-579`) to extend.

### 0.5 Orchestrator-resolved open questions (confirmed before implementation)

Three decisions the planner explicitly flagged rather than guessing:

1. **`MATURED` as a day-level status:** NOT added to the six-state day
   ledger (§2.2/§2.3). Confirmed correct: maturation is inherently
   per-observation-per-horizon (1d matures almost immediately, 60d takes
   ~3 months), so "day D is MATURED" has no single well-defined truth
   value. `MATURED` stays exactly as it exists today
   (`research_prospective_outcomes.status`), reported as a separate
   maturation-by-horizon rollup (§2.1's `prospective_evidence_audit_summary`).
2. **Component A's completeness-refresh pass runs automatically**, every
   time `fetch_and_cache_universe` executes (§1.3) — no opt-in flag.
   Confirmed correct: this directly serves the phase's stated goal ("run
   unattended for months... safely refresh incomplete/stale research rows
   once authoritative completed-session data is available") and the cost is
   bounded (one cheap SQL check per already-cached ticker; an Alpaca call
   only for tickers genuinely flagged incomplete, capped to a small tail
   window, never the full 5-year backfill).
3. **Research LaunchAgent remains unscheduled** (unchanged hard constraint).
   Acknowledged, not actionable: Component B's day ledger will honestly show
   most historical trading days as `MISSED` until the job is run more
   regularly — this is the ledger correctly reflecting reality, not a defect.

## 1. Component A — Research cache lifecycle (incomplete-bar repair)

### 1.1 New module: `strategy_lab/cache_integrity.py`

Imports only `strategy_lab.data.RESEARCH_SOURCE` and
`ingestion.alpaca_source.get_data_client` (same Alpaca client
`strategy_lab/data.py` already uses) plus `automation.trading_calendar.is_likely_trading_day`.
Never imports `trading.*`/`alerts.*` (structural safety test, §8).

```python
NYSE_TZ = ZoneInfo("America/New_York")      # local copy — matches the
NYSE_CLOSE_TIME = time(16, 0)               # existing duplicated-locally
                                             # convention (§0.1)

CORRECTIONS_TABLE = "research_cache_corrections"

def ensure_schema(conn) -> None: ...

def session_close_utc(session_date: date) -> datetime:
    """session_date's 16:00 America/New_York close, converted to a naive
    UTC datetime (matching db/schema.py's `datetime('now')` naive-UTC
    convention, and the exact parsing approach in
    ops/provider_reconciliation.py::_is_intraday_fetch). Correctly DST-aware
    via zoneinfo (no hardcoded UTC offset)."""

def latest_cached_row(conn, ticker: str, source: str = RESEARCH_SOURCE) -> Optional[dict]:
    """SELECT date, close, volume, fetched_at FROM prices WHERE ticker=? AND
    source=? ORDER BY date DESC LIMIT 1. None if no rows."""

def latest_row_is_incomplete(conn, ticker: str, today: Optional[date] = None) -> bool:
    """True iff:
      1. a RESEARCH_SOURCE row exists for `ticker`,
      2. its `date` is itself a valid NYSE session (defensive; False if not
         - never treats a corrupted/malformed date as actionable),
      3. that session's close has genuinely passed
         (session_close_utc(row_date) <= now_utc), AND
      4. `fetched_at` is missing, unparseable, OR < that session's close.

    FAILS CLOSED on ambiguity: an unparseable/missing `fetched_at` returns
    True (treat as incomplete / worth reverifying) rather than silently
    trusting an ambiguous row forever - mirrors outcome_maturation.py's
    "never fabricate, mark unavailable/uncertain rather than assume OK"
    posture. NEVER evaluates any row other than the single MAX(date) row for
    (ticker, RESEARCH_SOURCE) - historical rows are never re-examined here."""

FROZEN_ARTIFACT_REGISTRY: List[dict]   # see §1.4

def check_correction_affects_frozen_artifacts(corrected_date: str) -> List[str]:
    """Best-effort, never raises. Returns FROZEN_ARTIFACT_REGISTRY entry
    `name`s whose recorded coverage max-date >= corrected_date. A missing/
    unreadable artifact file is silently skipped (not an error) - see §1.4."""

def refresh_incomplete_latest_bars(
    conn, tickers: List[str], today: Optional[date] = None,
) -> Dict[str, dict]:
    """For each ticker where latest_row_is_incomplete(conn, ticker, today)
    is True: fetch a small tail window (StockBarsRequest, that ticker only,
    start=row_date - 7 calendar days, timeframe=Day, adjustment=Adjustment.ALL
    - never the full 5-year backfill), find the bar matching row_date exactly.

    - If the Alpaca call raises: leave the existing row completely untouched,
      report {"status": "fetch_failed", "detail": str(e)}. Never partially
      writes.
    - If no bar for that exact date is returned: leave untouched, report
      {"status": "no_data_returned"}.
    - If a bar IS returned: compare its close/volume against the currently
      cached row (exact equality - same provider, same adjustment mode, not
      a cross-source tolerance comparison). If unchanged, upsert anyway
      (refreshes fetched_at to now, so this ticker is never re-flagged
      incomplete every single run) and report
      {"status": "refreshed_unchanged"}. If changed, call
      _record_correction(...) FIRST (§1.4, append-only audit row - detection
      happens before the value is overwritten), then upsert via the existing
      strategy_lab.data._store_adjusted_bars, report
      {"status": "refreshed_changed", "old_close": ..., "new_close": ...,
      "affects_frozen_artifacts": [...]}.

    NEVER touches any row other than that ticker's single MAX(date) row.
    NEVER writes source="alpaca". NEVER writes/deletes any .pkl/.json file.
    Ticker not flagged incomplete -> {"status": "no_action_needed"} (no API
    call made for that ticker)."""
```

### 1.2 New table: `research_cache_corrections` (additive, append-only)

```sql
CREATE TABLE IF NOT EXISTS research_cache_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corrected_at TEXT NOT NULL DEFAULT (datetime('now')),
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    source TEXT NOT NULL,
    old_close REAL, new_close REAL,
    old_volume INTEGER, new_volume INTEGER,
    old_fetched_at TEXT, new_fetched_at TEXT,
    reason TEXT NOT NULL,
    affects_frozen_artifacts TEXT NOT NULL   -- JSON list, "[]" if none
)
```
No `UPDATE`/`DELETE` path anywhere for this table — append-only. A
correction with `old_close == new_close` documents a "verified, no change
needed" finding (the `refreshed_unchanged` case, §1.1, does NOT write a row
here — only `refreshed_changed` does; this table exists strictly to log
genuine value changes, not routine fetched_at refreshes).

### 1.3 Modified: `strategy_lab/data.py::fetch_and_cache_universe`

Minimal, additive change — the existing full-backfill path
(`_needs_fetch`/the `to_fetch` loop, lines 74, 86-111) is **completely
unchanged**. A new pass is inserted for tickers that were **not** flagged
for backfill (i.e. already have enough rows) but whose single latest row
may be incomplete:

```python
def fetch_and_cache_universe(conn, tickers, years=DEFAULT_YEARS) -> Dict[str, dict]:
    min_rows = int(years * 365 * 0.68)
    to_fetch = [t for t in tickers if _needs_fetch(conn, t, min_rows)]
    already_cached = [t for t in tickers if t not in to_fetch]
    report = {t: {"status": "cached", "rows": len(load_price_history(conn, t, source=RESEARCH_SOURCE)),
                   "error": None} for t in already_cached}

    from strategy_lab.cache_integrity import refresh_incomplete_latest_bars
    completeness = refresh_incomplete_latest_bars(conn, already_cached)
    for t, r in completeness.items():
        if r["status"] != "no_action_needed":
            report[t]["completeness_refresh"] = r

    if not to_fetch:
        return report
    # ... existing unchanged full-backfill loop ...
```

`_needs_fetch` itself is **not modified** — it remains exactly the "do we
have enough rows at all" check it always was; completeness is a genuinely
separate, independently-composed concern, confirmed to run automatically
per §0.5 item 2.

### 1.4 Frozen-artifact detection (never auto-regenerates anything)

```python
# strategy_lab/cache_integrity.py
FROZEN_ARTIFACT_REGISTRY = [
    {"name": "phase9_baseline",  "kind": "json",   "path": phase9_baseline.BASELINE_FILE,
     "date_range_keys": ("dataset", "coverage", "date_range")},
    {"name": "phase10_baseline", "kind": "json",   "path": phase10_baseline.BASELINE_FILE,
     "date_range_keys": ("dataset", "coverage", "date_range")},
    {"name": "phase9_results",   "kind": "pickle", "path": report.CACHE_FILE,
     "date_range_keys": ("coverage", "date_range")},
    {"name": "phase10_results",  "kind": "pickle", "path": report_phase10.CACHE_FILE,
     "date_range_keys": ("coverage", "date_range")},
    {"name": "phase11_results",  "kind": "pickle", "path": phase11_report.CACHE_FILE,
     "date_range_keys": ("coverage", "date_range")},
]
```
Confirmed real key paths: `strategy_lab/phase9_baseline.py:56-60`
(`"dataset": {"coverage": results.get("coverage"), ...}`),
`strategy_lab/phase10_baseline.py:33-37` (identical shape),
`strategy_lab/report.py:47-49` (`coverage = coverage_summary(...)`, stored
top-level in the pickle's `results` dict — same for `report_phase10.py:49-51`
and `phase11_report.py:69-71`). `coverage_summary`
(`strategy_lab/data.py:120-134`) returns `date_range=(min_date, max_date)` —
JSON round-trips this as a 2-element list; pickle preserves the tuple.

Loading any entry is wrapped in try/except returning `None` on any failure
(file missing, corrupt, unexpected shape) — detection **degrades to "no
finding," never raises, never blocks a refetch.** This is read-only against
these files; nothing here ever opens them for writing.

**What "detect and report" means concretely:** when `refresh_incomplete_latest_bars`
finds a changed value (§1.1) at `date_str` for `ticker`, it calls
`check_correction_affects_frozen_artifacts(date_str)`. If the returned list
is non-empty, the `research_cache_corrections` row's
`affects_frozen_artifacts` column records exactly which artifacts *may* now
be stale relative to corrected source data. **No code path regenerates or
rewrites any `.pkl`/`.json` artifact as a result of this finding** — that
remains a deliberate, explicit `python -m strategy_lab.run_study` /
`run_phase10_study` / `run_phase11_study` invocation, exactly as today.
Component D (§4) surfaces this list on the dashboard so a human notices it.

### 1.5 New CLI: `strategy_lab/run_cache_completeness_check.py`

`python -m strategy_lab.run_cache_completeness_check [--tickers ...]` —
mirrors `ops/run_data_quality_check.py`'s convention. Calls
`refresh_incomplete_latest_bars(conn, tickers or RESEARCH_UNIVERSE)`, prints
a summary table, exits 0. Lets an operator/tester run just this narrow check
without the multi-minute full study.

### 1.6 Interaction with outcome maturation (cross-reference §0.3)

Documented, not additionally mutated: `mature_outcomes` (`strategy_lab/outcome_maturation.py`)
is **not modified** by Component A. Its existing upsert-on-recompute
behavior already means that if Component A corrects a price row an outcome
was matured against, the **next scheduled `run_research_job` call**
(Component C, §3) will naturally recompute and correctly update that
outcome's `realized_return` via the existing, already-safe
`_persist`/`ON CONFLICT DO UPDATE` path — this is a **pre-existing,
already-idempotent recompute**, not a new mutation path. Component A's only
added obligation here: `_record_correction` (§1.1/§1.4) logs the correction
so a human reviewing `research_cache_corrections` understands why a
previously-displayed `realized_return` may have changed between two
dashboard loads — genuinely informational, never suppressed.

## 2. Component B — Prospective evidence integrity (auditable per-day status)

### 2.1 New module: `ops/prospective_audit.py`

Read-only. Imports `db.run_history_repository.load_run_history`,
`strategy_lab.research_automation.load_research_run_history`,
`strategy_lab.prospective.load_observations`,
`strategy_lab.outcome_maturation.load_outcomes`,
`automation.trading_calendar.{is_likely_trading_day, trading_sessions_between}`.
**Never imports anything that writes** — no `record_observation`, no
`record_event`, no `mature_outcomes` call.

```python
PROSPECTIVE_DAY_EXPECTED = "EXPECTED"                  # today, job hasn't run yet
PROSPECTIVE_DAY_CAPTURED = "CAPTURED"                   # all attempted tickers got observations
PROSPECTIVE_DAY_CAPTURED_PARTIAL = "CAPTURED_PARTIAL"   # some tickers succeeded, some errored
PROSPECTIVE_DAY_DUPLICATE_SKIPPED = "DUPLICATE_SKIPPED" # retry found everything already captured
PROSPECTIVE_DAY_UNAVAILABLE = "UNAVAILABLE"             # job ran, but no ticker had a scoreable observation
PROSPECTIVE_DAY_MISSED = "MISSED"                       # no successful capture recorded for this day at all

@dataclass
class ProspectiveDayStatus:
    trading_date: str
    status: str
    reason: str
    production_run_status: Optional[str]
    research_run_status: Optional[str]
    observations_created: int
    duplicates_skipped: int
    n_distinct_tickers_observed: int

def eligibility_start_date(conn) -> Optional[date]:
    """MIN(trading_date) across research_run_history - the first day the
    research job was EVER run. Returns None if research_run_history is
    empty. Days before this date are never evaluated or reported as MISSED
    - the system did not exist yet for them (no retroactive fabrication)."""

def build_prospective_day_ledger(conn, today: Optional[date] = None) -> List[ProspectiveDayStatus]:
    """For every NYSE trading session in
    [eligibility_start_date(conn), today] (inclusive; empty list if
    eligibility_start_date is None): classify per §2.2. Pure read - one
    bounded query each against load_run_history(conn, limit=750),
    load_research_run_history(conn, limit=750), and
    load_observations(conn), filtered/grouped client-side in pandas by
    trading_date."""

def prospective_evidence_audit_summary(conn, today: Optional[date] = None) -> dict:
    """{'counts_by_status': {...}, 'ledger_days': N, 'eligibility_start':
    ..., 'maturation_by_horizon': {horizon: {'pending':n,'matured':n,
    'unavailable':n}} - a pure rollup of strategy_lab.outcome_maturation.load_outcomes,
    grouped by horizon_days, reusing that table's existing status vocabulary
    verbatim rather than inventing a rival one (§0.5 item 1)."""
```

### 2.2 Per-day classification logic (first match wins)

For trading session `D` in `[eligibility_start_date, today]`:

1. `D == today` and no `research_run_history` row exists yet for `D` →
   `EXPECTED` ("today's research job has not run yet").
2. `D < today` (or `D == today` with a row present) and **no**
   `research_run_history` row exists for `D` at all → `MISSED`, reason
   `"no research job run recorded for this trading day"`.
3. Latest `research_run_history` row for `D` has
   `status in (STATUS_SKIPPED_NO_FRESH_DATA,)` OR
   `status == 'running'` (stuck/crashed, per §0.3) → `MISSED`, reason =
   that row's `skip_reason` or `"run left in status=running (crashed or
   interrupted) - never completed"`.
4. Latest row has `status == STATUS_FAILED` (Component C's hardened
   semantics, §3) and `observations_created == 0` → `MISSED`, reason =
   that row's `errors`.
5. Latest row has `observations_created > 0` and
   `status in (STATUS_SUCCESS_RESEARCH, STATUS_PARTIAL_FAILURE_RESEARCH)` →
   `CAPTURED` if `status == STATUS_SUCCESS_RESEARCH` (no errors),
   `CAPTURED_PARTIAL` if `STATUS_PARTIAL_FAILURE_RESEARCH` (§3).
6. Latest row has `observations_created == 0`, `duplicates_skipped > 0`,
   no errors → `DUPLICATE_SKIPPED` ("already fully captured by an earlier
   run this day; this run/retry did no new work").
7. Latest row has `observations_created == 0`, `duplicates_skipped == 0`,
   no errors, `status == STATUS_SUCCESS_RESEARCH` → `UNAVAILABLE`
   ("job ran cleanly but no ticker produced a scoreable observation, e.g.
   insufficient indicator history for the whole universe that day" —
   reuses `outcome_maturation.STATUS_UNAVAILABLE`'s existing naming
   convention for a consistent "never fabricate, mark unavailable"
   vocabulary rather than inventing an unrelated term).
8. Anything not matched above → `MISSED` with a defensive
   `reason="unclassifiable research_run_history row - needs manual review"`
   (fail closed: an unrecognized state is never silently treated as
   evidence of success).

`production_run_status` is attached from `load_run_history` filtered to
`trading_date == D` (may be `None` if no production row exists for `D`
either — itself informative, shown as-is, never inferred).

### 2.3 `MATURED` — resolved per §0.5 item 1

Kept as the existing per-observation-per-horizon concept
(`research_prospective_outcomes.status`), reported as a **separate rollup**
in `prospective_evidence_audit_summary`, not folded into the six-state day
ledger.

### 2.4 Immutability hardening — tests, not new mutation paths

No new `UPDATE`/`DELETE` code is added anywhere by Component B. New tests
(`tests/test_ops_prospective_audit.py`) assert:
- A static source scan (`ast`-based) confirms `strategy_lab/prospective.py`
  and `strategy_lab/prospective_events.py` contain no `UPDATE` SQL keyword.
- Calling `record_observation` twice for the same `(ticker, observation_date)`
  with different field values leaves the original row's values unchanged.
- Calling `record_event` twice for the same
  `(ticker, event_type, event_date)` leaves the original row unchanged.
- `ops/prospective_audit.py`'s functions never call any function whose name
  starts with `record_`/`mature_`/`save_` (grep-based structural check).

## 3. Component C — Research-job resilience

### 3.1 New status constant, `strategy_lab/research_automation.py`

```python
STATUS_PARTIAL_FAILURE_RESEARCH = "partial_failure"   # new
```
(String value intentionally matches `db.run_history_repository.STATUS_PARTIAL_FAILURE`
for human-readability consistency across the two separate tables — this is a
new local constant in `research_run_history`'s own module, not an import;
same pattern `STATUS_SUCCESS_RESEARCH` already uses.)

### 3.2 Fix Bug 1 (false success) — `run_research_job`, replace lines 189-190

```python
# before:
if result.errors and result.observations_created == 0 and result.duplicates_skipped == 0:
    result.status = STATUS_FAILED

# after:
if result.errors:
    if result.observations_created > 0 or result.duplicates_skipped > 0:
        result.status = STATUS_PARTIAL_FAILURE_RESEARCH
    else:
        result.status = STATUS_FAILED
```
Any non-empty `result.errors` — whether from a per-ticker exception or the
maturation `try/except` — now **always** moves `status` off
`STATUS_SUCCESS_RESEARCH`. A clean run (empty `errors`) is unaffected.

### 3.3 Fix Bug 2 (event orphaning on retry) — decouple event-building from
first-insert, lines 166-180

```python
# after:
for ticker in tickers:
    try:
        obs = build_todays_observation(conn, ticker, as_of_date=today.isoformat())
        if obs is None:
            continue
        inserted = record_observation(conn, **obs)
        if inserted:
            result.observations_created += 1
        else:
            result.duplicates_skipped += 1
        # ALWAYS attempt event-building, whether this call inserted a new
        # row or found an existing one - detect_events_for_new_observation
        # compares against the PRIOR day's stored observation (strictly <
        # today's date), so this is already correct/idempotent to call on a
        # duplicate-day retry; record_event's own ON CONFLICT DO NOTHING
        # makes re-recording an already-existing event a genuine no-op.
        # This is what makes a retry after an event-building failure
        # actually able to recover, instead of being permanently gated
        # behind `if inserted:`.
        detected, newly_recorded = record_events_for_observation(conn, obs)
        result.events_created += len(newly_recorded)
    except Exception as e:
        result.errors.append(f"{ticker}: {type(e).__name__}: {e}")
        record_ticker_error(conn, run_id, ticker=ticker,
                             source=obs.get("source") if obs is not None else None,
                             trading_date=today.isoformat(), reason=f"{type(e).__name__}: {e}")
```

**Implementation-time correction:** the loop must reset `obs = None` at the
top of each iteration (before the `try:`), and the except-block's guard
must be `obs is not None`, not `'obs' in dir()`. `dir()` sees names bound
anywhere in the enclosing scope, not just this iteration — if ticker N's
`build_todays_observation` raises before `obs` is (re)assigned, `'obs' in
dir()` is still `True` from ticker N-1's successful iteration, silently
misattributing the error row's `source` column to the wrong ticker. Caught
by the coder via a live test; fixed directly since it's diagnostic-only
(never gates status/control-flow/money-movement) and unambiguous.

### 3.4 Fix events_created double-counting — `strategy_lab/prospective_events.py::record_events_for_observation`

Return type changes from `List[str]` to `Tuple[List[str], List[str]]`:

```python
def record_events_for_observation(conn, curr_row: dict) -> Tuple[List[str], List[str]]:
    """Returns (detected_event_types, newly_recorded_event_types). detected
    is every event_type the transition logic identified this call (useful
    for logging/debugging, including on a duplicate-day retry where they'll
    all already exist). newly_recorded is the subset that record_event()
    actually inserted (rowcount > 0) THIS call - what run_research_job's
    events_created counter must use, so a retry never inflates the metric
    for events that already existed."""
    event_types = detect_events_for_new_observation(conn, curr_row)
    newly_recorded = []
    for event_type in event_types:
        was_new = record_event(conn, event_date=curr_row["observation_date"], ticker=curr_row["ticker"],
                                event_type=event_type, score=curr_row["score"], stage=curr_row["stage"],
                                regime=curr_row.get("regime"))
        if was_new:
            newly_recorded.append(event_type)
    return event_types, newly_recorded
```
`record_event` already returns `cur.rowcount > 0` — this only changes how
the caller uses that existing return value; `record_event` itself is
unchanged. **Coder must update the one other caller** of
`record_events_for_observation` if any exists outside `research_automation.py`
(confirmed via grep: none currently — only `run_research_job` calls it) and
any test asserting on its old `List[str]` return shape
(`tests/test_phase11.py:163` `test_control_entry_event_fires_once_per_streak`
— verify and update if it inspects the return value directly).

### 3.5 New table: `research_run_ticker_errors` (additive, structured diagnostics)

```sql
CREATE TABLE IF NOT EXISTS research_run_ticker_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    trading_date TEXT NOT NULL,
    ticker TEXT,              -- NULL for a maturation-phase (not per-ticker) error
    source TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
```
```python
def record_ticker_error(conn, run_id: int, trading_date: str, reason: str,
                         ticker: Optional[str] = None, source: Optional[str] = None) -> None:
    """Append-only. Called at BOTH existing exception sites in
    run_research_job (per-ticker, and the maturation try/except - ticker=None
    for the latter)."""

def load_ticker_errors_for_run(conn, run_id: int) -> pd.DataFrame: ...
```
The existing flat `errors` TEXT column on `research_run_history` is
**kept unchanged** (backward-compatible human-readable summary); this table
is a strictly additive, queryable companion.

### 3.6 Idempotency of retries — what must now hold, and how it's tested

- Re-running `run_research_job(conn, today=X)` after a fully clean prior run
  for `X`: `observations_created=0`, `duplicates_skipped=len(tickers with an
  obs)`, `events_created=0` (fixed by §3.4 — previously this over-counted).
- Re-running after a run where ticker T's event-building threw once: the
  retry (with the transient cause fixed) must now produce T's event(s) via
  the §3.3 fix — previously permanently impossible.
- `mature_outcomes` is already unconditionally called once per
  `run_research_job` invocation regardless of per-ticker success/failure
  (existing behavior, unchanged) — confirmed already correct.

## 4. Component D — Evidence-quality dashboard (extend, not rebuild)

### 4.1 New `dashboard/data.py` getters (same thin-caller convention as
existing getters)

```python
@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_prospective_day_ledger(limit_days: int = 90) -> pd.DataFrame:
    """ops.prospective_audit.build_prospective_day_ledger, most recent
    limit_days rows, as a DataFrame for st.dataframe."""

@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_prospective_audit_summary() -> dict:
    """ops.prospective_audit.prospective_evidence_audit_summary."""

@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_research_cache_completeness_report() -> dict:
    """READ-ONLY snapshot: for each RESEARCH_UNIVERSE ticker, whether
    strategy_lab.cache_integrity.latest_row_is_incomplete(conn, ticker) is
    True RIGHT NOW (a pure SQL check - no Alpaca call), plus the most recent
    N rows of research_cache_corrections. NEVER calls
    refresh_incomplete_latest_bars (which makes live Alpaca calls) from a
    dashboard page load."""

@st.cache_data(ttl=STRATEGY_LAB_TTL_SECONDS, show_spinner=False)
def get_research_run_ticker_errors(run_id: Optional[int] = None, limit: int = 100) -> pd.DataFrame:
    """strategy_lab.research_automation.load_ticker_errors_for_run for the
    given run_id, or the latest research_run_history run if None."""
```
All four added to `clear_all_caches()` (`dashboard/data.py:551-579`).

### 4.2 `dashboard/views/ops_overview.py` extension

New section added after `_render_prospective_evidence()`
(`dashboard/views/ops_overview.py:163-183`) and before
`_render_automation_history()`:

```python
def _render_prospective_day_ledger():
    st.header("Prospective evidence audit (per-trading-day)")
    st.caption(
        "Cross-references production run history, research job history, and "
        "recorded observations - reporting only, never fabricates or "
        "backfills a missed day's prediction."
    )
    summary = get_prospective_audit_summary()
    counts = summary["counts_by_status"]
    cols = st.columns(len(counts) or 1)
    for col, (status, n) in zip(cols, counts.items()):
        col.metric(status, n)
    ledger = get_prospective_day_ledger()
    st.dataframe(ledger, use_container_width=True, hide_index=True)
    st.markdown("**Outcome maturation by horizon**")
    st.dataframe(pd.DataFrame(summary["maturation_by_horizon"]).T, use_container_width=True)


def _render_research_cache_completeness():
    st.header("Research cache completeness / anomaly surfacing")
    report = get_research_cache_completeness_report()
    flagged = report["currently_flagged_incomplete"]
    if flagged:
        st.warning(f"⚠️ {len(flagged)} ticker(s) have a possibly-incomplete latest cached bar, "
                    f"pending the next scheduled cache-completeness pass: {flagged}", icon="🕒")
    else:
        st.success("No tickers currently flagged as having an incomplete latest bar.", icon="✅")
    corrections = report["recent_corrections"]
    if corrections:
        st.markdown("**Recent cache corrections (preliminary → final bar replacements)**")
        st.dataframe(pd.DataFrame(corrections), use_container_width=True, hide_index=True)
        affecting_frozen = [c for c in corrections if c.get("affects_frozen_artifacts")]
        if affecting_frozen:
            st.error(
                "⚠️ One or more corrections may affect data already reflected in a frozen "
                "Phase 9-13 research artifact. Regenerating that artifact requires an explicit, "
                f"human-approved `python -m strategy_lab.run_study` (etc.) run: {affecting_frozen}",
                icon="🚨",
            )
```
Wired into `render()` (`dashboard/views/ops_overview.py:246-271`) right
after the existing `_render_prospective_evidence()` call, each wrapped in
the same `try/except -> components.empty_state` pattern every other section
in this file already uses.

### 4.3 `dashboard/views/strategy_lab.py` extension

New `_render_research_job_diagnostics()` called immediately after
`_render_production_health()` (`dashboard/views/strategy_lab.py:546-571`):
shows the latest research run's status (now meaningfully distinguishing
`success`/`partial_failure`/`failed`, §3.2) and, if not a clean success,
`get_research_run_ticker_errors()` as a table (ticker/source/reason). The
existing `_render_prospective_evidence_progress()`
(`dashboard/views/strategy_lab.py:730-800`) gets one new subsection appended
— the day-ledger summary (`get_prospective_audit_summary()` metrics) — using
the exact same `EVIDENCE_INSUFFICIENT_DATA`/`EARLY_EVIDENCE`/
`EVALUATION_READY` labels already rendered there (unchanged,
`ops/evidence_classification.py` is not touched by this phase).

### 4.4 Hard constraint restated for Component D

No change to `ops/evidence_classification.py`'s thresholds or vocabulary.
No new `st.button`/write action added anywhere in either view file — every
new function here is a `get_*`-backed read, exactly like every existing
section.

## 5. Hard constraints (restated per component)

No changes to signal weights/thresholds, entry/exit rules, CONTROL/A/B
methodology, risk limits, watchlist, alert logic/cooldown, or Discord
config. No strategy optimization. No paper/live order code touched anywhere
(`trading/*`, `alerts/*` are not imported by any new/modified file in this
phase — verified against the same structural-safety-test pattern already in
`tests/test_strategy_lab.py`). No Discord sends. No LaunchAgent/schedule
changes (`strategy_lab/run_research_job.py` remains manually invoked). No
new scheduled jobs. No automatic provider switching
(`db.price_repository.SOURCE_PRIORITY` is not touched; Component A never
writes `source="alpaca"`). No retrospective creation/backfill of missed
prospective predictions (Component B is audit-only; `eligibility_start_date`
explicitly prevents retroactively judging pre-system days). Never overwrites
an existing prospective prediction (`record_observation`/`record_event` are
untouched; only `research_prospective_outcomes`'s pre-existing, documented
upsert continues to exist). Does not broadly regenerate Phase 9-13 research
results just because cache data changed (§1.4: detection writes an audit
row; no code path calls `save_cache`/`save_baseline`). Any historical-data
correction that would alter frozen research evidence is detected/reported
(§1.4) and requires explicit human action (a manually-run study CLI) —
never silently applied.

**Money/live-order confirmation:** none of the four components touch
`trading/*`, `alerts/discord*`, `alerts/runner.py`, or any code path that
can submit/modify/cancel/close an order or send a Discord message.

## 6. New/modified files (summary)

**New:**
- `strategy_lab/cache_integrity.py` (Component A)
- `strategy_lab/run_cache_completeness_check.py` (Component A CLI)
- `ops/prospective_audit.py` (Component B)
- `tests/test_ops_prospective_audit.py` (Component B)
- `tests/test_strategy_lab_cache_integrity.py` (Component A)
- `tests/test_research_automation_resilience.py` (Component C)
- `tests/test_ops_prospective_dashboard.py` (Component D)

**Modified:**
- `strategy_lab/data.py` (`fetch_and_cache_universe`, additive pass only;
  `_needs_fetch` unchanged)
- `strategy_lab/research_automation.py` (§3.1-3.3, 3.5: new status
  constant, bug fixes, new `record_ticker_error` calls; new table)
- `strategy_lab/prospective_events.py` (`record_events_for_observation`
  return-type change, §3.4)
- `db/schema.py` — **not modified**; both new tables (`research_cache_corrections`,
  `research_run_ticker_errors`) get their own `ensure_schema(conn)` in their
  owning module, matching the existing convention every `strategy_lab/*`
  and `ops/*` table already uses — never added to `db/schema.py::ALL_STATEMENTS`.
- `dashboard/data.py` (4 new getters, §4.1)
- `dashboard/views/ops_overview.py` (2 new render functions, §4.2)
- `dashboard/views/strategy_lab.py` (1 new render function + 1 extension,
  §4.3)
- `tests/test_phase11.py` (verify/update `test_control_entry_event_fires_once_per_streak`
  against §3.4's return-type change if it inspects the return value directly)

## 7. Testing checklist

**Component A:**
- Incomplete-bar-then-refresh: seed a row with `fetched_at` before that
  session's `session_close_utc`, assert `latest_row_is_incomplete` True,
  assert `refresh_incomplete_latest_bars` (mocked Alpaca response) upserts
  and updates `fetched_at`.
- Completed-bar-stays-stable/idempotent: seed a row with `fetched_at` after
  close; assert `latest_row_is_incomplete` False and no Alpaca call is made
  (mock call-count assertion).
- Weekend/holiday boundary: `session_close_utc` on a non-trading date is
  never evaluated (`latest_row_is_incomplete` returns False defensively).
- DST boundary: `session_close_utc` for a January date vs a July date
  differs by exactly one hour in UTC (21:00 vs 20:00) — confirms zoneinfo,
  not a hardcoded offset.
- Fail-closed on ambiguous `fetched_at`: malformed/missing string →
  `latest_row_is_incomplete` returns True.
- Production/research source isolation: after any `refresh_incomplete_latest_bars`
  call, `SELECT COUNT(*) FROM prices WHERE source='alpaca'` is unchanged.
- Never touches historical rows: seed 2 rows (older + latest) for a ticker,
  only the latest row's `fetched_at`/values may change after a refresh call.
- Frozen-artifact detection: build a fake `phase9_baseline.json` with a
  known `date_range`, assert `check_correction_affects_frozen_artifacts`
  correctly includes/excludes it based on the corrected date; assert the
  file's bytes are unchanged after the check.
- Alpaca failure during refresh: mocked exception → existing row untouched,
  `status="fetch_failed"` reported, no `research_cache_corrections` row
  written.

**Component B:**
- Missed-day detection without backfill: a trading day with zero
  `research_run_history` rows classifies `MISSED`; assert no observation/
  event row is created as a side effect of running the audit.
- Eligibility window: a trading day before `eligibility_start_date` never
  appears in the ledger at all (not `MISSED`, simply absent).
- `DUPLICATE_SKIPPED` classification for a genuine retry-of-already-captured
  day.
- `UNAVAILABLE` vs `MISSED` distinction (job ran with zero errors/zero
  progress vs. job never ran / genuinely failed).
- Stuck `status='running'` row for a past day classifies `MISSED`.
- Observation/event immutability tests (§2.4).

**Component C:**
- Partial-failure handling: mock one ticker's `build_todays_observation` to
  raise; assert final `status == STATUS_PARTIAL_FAILURE_RESEARCH` (not
  `"success"`), and `research_run_ticker_errors` has exactly one row for
  that ticker.
- Maturation-only failure: mock `mature_outcomes` to raise with all tickers
  otherwise succeeding; assert status is `partial_failure`, not `success`.
- Total failure (zero progress + errors): status is `failed`.
- Duplicate-rerun idempotency: run twice cleanly, assert second run's
  `events_created == 0` (§3.4 fix) and no duplicate rows in any table.
- Crash/retry recovery for events (§3.3 fix): first call raises inside
  event-building for one ticker (mocked); assert the ticker's observation
  IS committed but events are NOT recorded and an error is logged; second
  call (mock removed) must now successfully record that ticker's events —
  this is the core regression test for Bug 2.
- Existing `test_research_job_recovery_is_idempotent_on_duplicate_call`
  (`tests/test_phase11.py:268`) must still pass unmodified.

**Component D:**
- Dashboard/evidence-count determinism: `get_prospective_audit_summary()`
  called twice against unchanged DB state returns identical output.
- Safety-boundary tests: static scan confirms no new `st.button`/write call
  in any modified dashboard file; confirms `get_research_cache_completeness_report`
  never imports/calls `refresh_incomplete_latest_bars` or any Alpaca client
  constructor.
- Zero trade/Discord/alert-state mutation: confirms no row is ever written
  to `paper_orders`, `alert_state`, or `alerts` by any Component D code path.

## 8. Manual/integration verification before moving on from Phase 14

1. `pytest tests/` — full suite green.
2. `python -m strategy_lab.run_cache_completeness_check` against the real
   local DB — confirm whether the Phase 13-discovered stale AAPL/MSFT/NVDA
   (and other watchlist) `alpaca_adjusted` rows get refreshed, and whether
   any correction is flagged as potentially affecting a frozen artifact.
   If so, STOP and report — do not proceed to regenerate anything.
3. `streamlit run dashboard/app.py` — load Operations and Strategy Lab
   pages, confirm the new sections render without exception.
4. `git diff` touches only files listed in §6. No `signals/config.py`,
   `backtest/config.py`, `trading/config.py`, `alerts/config.py`,
   `config/settings.py`, `db/schema.py`, or `deploy/*` file appears in the
   diff.
