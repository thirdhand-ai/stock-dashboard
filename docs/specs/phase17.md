# Phase 17: Benchmark Freshness, Regime Provenance, and Exit-Event
Attribution

Status: PLANNED. Builds on Phase 9-16 (repo HEAD 8dc5e85, "Add evidence
provenance and correction-impact auditing"). No implementation code in this
document - signatures/pseudocode below are illustrative specification,
matching the convention used in docs/specs/phase16.md.

This phase directly follows up on Phase 16 §9 Open Questions #1 and #2,
which Phase 16 explicitly deferred rather than built. Verified independently
against the current codebase (all four areas below were re-confirmed by
reading the actual live files, not assumed from the Phase 16 spec text).

## 0. Grounding: re-verified against the live codebase

### 0.1 Area A/B root cause (re-confirmed)

Phase 16's regime-lookup fix (`strategy_lab/regime_history.py::
regime_label_as_of`, `strategy_lab/prospective.py::build_todays_observation`)
is live and correct - confirmed by reading both files and
`tests/test_strategy_lab_regime_asof.py` in full: the point-in-time,
no-look-ahead lookup is exactly as Phase 16 specified. But nothing in
`strategy_lab/research_automation.py::run_research_job` (confirmed by
reading its full source) calls `strategy_lab.data.fetch_and_cache_universe`
for SPY/QQQ - the daily 16:45 ET research job only ever calls
`compute_historical_regime_series`, which reads whatever `RESEARCH_SOURCE=
"alpaca_adjusted"` data already happens to be cached. If that data goes
stale, `regime_label_as_of` degrades *correctly* (falls back to the most
recent available prior label, never fabricates) but *silently* - nothing
records that this happened.

Separately, `regime_label_as_of` (confirmed by reading its body) returns
only the label string - it discards the actual `date` of the row it
selected. There is no way today to see, per-observation, *which* SPY bar
date actually produced a given `regime` value, or whether that date lagged
the observation's own date.

### 0.2 Area A benchmark-constants decision (resolved)

Two independent definitions of "SPY"/"QQQ" exist:
- `research/config.py::RegimeConfig.primary_benchmark="SPY"` /
  `.secondary_benchmark="QQQ"` (the DEFAULT_REGIME_CONFIG instance) - this
  is the value `strategy_lab/regime_history.py::compute_historical_regime_series`
  ACTUALLY reads (`config.primary_benchmark`, defaulted to
  `DEFAULT_REGIME_CONFIG`) to decide which ticker drives the regime label.
- `strategy_lab/universe.py::PRIMARY_BENCHMARK="SPY"` /
  `SECONDARY_BENCHMARK="QQQ"` - a separate, Phase-9-era constant pair, used
  elsewhere in `strategy_lab/` for research-universe bookkeeping, NOT
  consumed by `compute_historical_regime_series` or `regime_label_as_of` at
  all.

**Decision: Area A's benchmark refresh must read
`research.config.DEFAULT_REGIME_CONFIG.primary_benchmark`/
`.secondary_benchmark`, never `strategy_lab.universe`'s constants.**
Justification: the two pairs are identical *today* only by coincidence -
they are independently defined with no code linking them. Using
`research/config.py`'s own values keeps Area A's fetch mechanically coupled
to the exact config object `compute_historical_regime_series` actually
reads; if `RegimeConfig.primary_benchmark` is ever changed without a
matching edit to `strategy_lab/universe.py` (a realistic drift risk since
nothing enforces they stay in sync), a `universe.py`-based refresh would
silently fetch the wrong ticker relative to what the regime computation
uses. `strategy_lab/universe.py` is left completely untouched.

**Also decided (both benchmarks, not SPY-only):** the refresh call fetches
BOTH `primary_benchmark` and `secondary_benchmark` in one
`fetch_and_cache_universe(conn, [primary, secondary])` call (one Alpaca
batch, negligible added cost - `fetch_and_cache_universe` already skips
tickers with sufficient cached history and only calls Alpaca for
genuinely-stale/missing ones, per its own docstring, re-confirmed by
reading `strategy_lab/data.py` in full). Justification: QQQ is also read by
the LIVE (non-point-in-time) `research.regime.compute_market_regime`'s
secondary context (used today by `ops/daily_report.py`'s `RegimeSection`)
and by `research/config.py::RelativeStrengthConfig`'s sector-proxy lookup -
it has the identical staleness exposure as SPY, just without gating the
regime *label* itself. Refreshing it alongside SPY closes that adjacent gap
at zero marginal risk (same function, same call, already-established Phase
14 staleness-repair mechanism underneath). **QQQ freshness is never used to
gate or classify the regime *label*'s own freshness** (§1.3) - only SPY
(`primary_benchmark`) does that, per `research/regime.py::
compute_market_regime`'s own docstring: "the label itself is always driven
by the primary benchmark only."

### 0.3 Area C decision: inline duplication vs. `trading.signals_bridge` import (resolved)

`trading/signals_bridge.py::exit_qualifies(score, rules=DEFAULT_RULES)`
already exists, is pure/stateless, and computes the exact "technical exit"
condition needed here. `strategy_lab/prospective.py::
build_todays_observation` already hand-duplicates the identical
computation for entry (`entry_qualifies`, inline, not imported from
`trading.signals_bridge.entry_qualifies` which also exists and does the
same thing) - confirmed by reading both files.

Checked `tests/test_strategy_lab.py`'s exact import-scan list (re-read in
full, §0 above): the generic scan
(`test_strategy_lab_never_imports_order_execution_or_alerting`) only
forbids `trading.engine`/`trading.orders`/`trading.run_paper`/
`alerts.discord`/`alerts.runner`/`alerts.run_alerts` - `trading.
signals_bridge` is NOT in that forbidden list, so importing it would not
break any existing test.

**Decision: inline duplication, not an import.** Justification: (1) it
matches the file's own existing, deliberate convention for the entry side -
`build_todays_observation` today has ZERO `trading.*` imports; introducing
the first one for a marginal DRY win changes this file's risk profile and
its "cannot possibly reach trading code" property from "true by
construction" to "true only because `trading.signals_bridge` itself happens
to stay side-effect-free" - a weaker guarantee. (2) The computation is four
lines, already proven copy-paste-safe by the existing `entry_qualifies`
precedent in the same function. (3) `backtest.config.DEFAULT_RULES` and
`backtest.scoring.STAGE_ORDER` (the actual source of truth both
`entry_qualifies` and `trading.signals_bridge.exit_qualifies` derive from)
are imported directly either way, so there is no duplicated *threshold* -
only a duplicated four-line boolean expression, which is the same tradeoff
Phase 16 §4.1 explicitly made and documented ("lower blast radius than
touching a file with unrelated call sites").

## 1. Area A - Benchmark freshness refresh (modified)

### 1.1 `strategy_lab/research_automation.py` (modified)

New top-level imports: `from research.config import DEFAULT_REGIME_CONFIG`,
`from strategy_lab.data import RESEARCH_SOURCE, fetch_and_cache_universe`.

New function, called once per run, immediately after `run_id = _start_run(
conn, today)` and BEFORE `_compute_config_fingerprint_safe` (regime lookups
in the per-ticker loop below depend on this; provenance capture does not
depend on it, so ordering here is deliberate but not safety-critical
either way):

```python
def _refresh_benchmark_data_safe(conn, run_id: int, trading_date: date) -> dict:
    """Best-effort SPY/QQQ RESEARCH_SOURCE refresh, run once per job run,
    BEFORE any observation is built. Mirrors _compute_config_fingerprint_safe's
    fail-open convention exactly: a failure here never fails the job, is
    recorded as a diagnostic ticker error (ticker=<primary benchmark or
    None>), and the job proceeds using whatever SPY/QQQ data is already
    cached - regime_label_as_of (Phase 16) degrades correctly regardless
    (falls back to the most recent available prior label, never fabricates).

    Returns {'primary_benchmark': str, 'primary_fetch_status': str
    ('fetched'|'cached'|'failed'|'exception'), 'secondary_benchmark':
    Optional[str], 'secondary_fetch_status': Optional[str]} - NOT persisted
    to research_run_history (no new column added to that table); purely an
    in-memory diagnostic for this call/its tests. A failure is ALSO recorded
    via the existing research_run_ticker_errors mechanism (ticker=None on a
    total exception, ticker=primary_benchmark on a reported per-ticker
    'failed' status), so historical failures remain visible through the
    existing table without a schema change."""
    primary = DEFAULT_REGIME_CONFIG.primary_benchmark
    secondary = DEFAULT_REGIME_CONFIG.secondary_benchmark
    tickers = [t for t in (primary, secondary) if t]
    try:
        report = fetch_and_cache_universe(conn, tickers)
    except Exception as e:
        logger.warning("research job: benchmark refresh raised for %s: %s", tickers, e)
        record_ticker_error(
            conn, run_id, ticker=None, source=RESEARCH_SOURCE, trading_date=trading_date.isoformat(),
            reason=f"benchmark refresh (regime freshness): {type(e).__name__}: {e}",
        )
        return {
            "primary_benchmark": primary, "primary_fetch_status": "exception",
            "secondary_benchmark": secondary, "secondary_fetch_status": "exception",
        }

    primary_status = report.get(primary, {}).get("status", "unknown")
    secondary_status = report.get(secondary, {}).get("status", "unknown") if secondary else None
    if primary_status == "failed":
        record_ticker_error(
            conn, run_id, ticker=primary, source=RESEARCH_SOURCE, trading_date=trading_date.isoformat(),
            reason=f"benchmark refresh (regime freshness): primary benchmark fetch failed: {report.get(primary, {}).get('error')}",
        )
    return {
        "primary_benchmark": primary, "primary_fetch_status": primary_status,
        "secondary_benchmark": secondary, "secondary_fetch_status": secondary_status,
    }
```

In `run_research_job`, immediately after `run_id = _start_run(conn, today)`:
```python
benchmark_refresh = _refresh_benchmark_data_safe(conn, run_id, today)
```
Add `benchmark_refresh_status: Optional[dict] = None` to `ResearchRunResult`
(new dataclass field, default `None` - backward compatible with every
existing `ResearchRunResult(...)` construction in tests) and set
`result.benchmark_refresh_status = benchmark_refresh` right after
constructing `result`. This field is NOT written to
`research_run_history` (no new column - matches `config_fingerprint`'s own
"computed once, used, not persisted to this particular table" precedent);
it exists purely so `run_research_job`'s return value is directly
inspectable/testable for this behavior without a DB round-trip.

**No change to `_todays_production_run_succeeded`, `_record_skipped`, the
non-trading-day gate, or the no-fresh-production-data gate** - the
benchmark refresh only runs on the path that already proceeds to build
observations, exactly where `_compute_config_fingerprint_safe` already
runs.

### 1.2 `strategy_lab/regime_history.py` (modified - additive/refactor-preserving)

Refactor `regime_label_as_of`'s BODY (not its signature or its public
behavior) to delegate to a new private selection helper, and add two new
public functions. This refactor must be **byte-identical in output** to
today's `regime_label_as_of` for every input - required regression: every
existing test in `tests/test_strategy_lab_regime_asof.py` must pass
unmodified.

```python
def _select_as_of_row(regime_series: pd.DataFrame, as_of_date: Optional[str] = None) -> Optional[pd.Series]:
    """Extracted, not new, selection logic - identical to regime_label_as_of's
    existing body. Returns the selected row (Series with 'date'/'label'
    among its fields) or None under the exact same conditions
    regime_label_as_of already documents (empty series; as_of_date before
    every row)."""
    if regime_series.empty:
        return None
    if as_of_date is None:
        return regime_series.iloc[-1]
    eligible = regime_series[regime_series["date"] <= as_of_date]
    if eligible.empty:
        return None
    return eligible.iloc[-1]


def regime_label_as_of(regime_series: pd.DataFrame, as_of_date: Optional[str] = None) -> Optional[str]:
    """UNCHANGED public contract (Phase 16 §1.1) - now implemented via
    _select_as_of_row so there is exactly one selection logic, not two."""
    row = _select_as_of_row(regime_series, as_of_date)
    return row["label"] if row is not None else None


def regime_label_and_date_as_of(regime_series: pd.DataFrame, as_of_date: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """New companion to regime_label_as_of (Phase 17 Area B) - same
    selection, but also returns the actual `date` of the row selected (the
    real SPY/benchmark bar date backing this label), distinct from
    as_of_date/observation_date. (None, None) under the exact same
    conditions regime_label_as_of returns None. Used ONLY by
    strategy_lab.prospective.build_todays_observation - all other existing
    callers of regime_label_as_of (there are none besides prospective.py
    today) are unaffected."""
    row = _select_as_of_row(regime_series, as_of_date)
    if row is None:
        return None, None
    return row["label"], row["date"]


REGIME_FRESHNESS_FRESH = "FRESH"
REGIME_FRESHNESS_STALE = "STALE"
REGIME_FRESHNESS_UNAVAILABLE = "UNAVAILABLE"


def classify_regime_freshness(observation_date: Optional[str], benchmark_data_date: Optional[str]) -> str:
    """Pure, deterministic, no DB access.
      UNAVAILABLE: benchmark_data_date is None (no eligible history at all
                   as of observation_date - regime_series empty, or every
                   row's date > observation_date).
      FRESH:       benchmark_data_date == observation_date (the benchmark's
                   own latest bar covers the observation's own date - the
                   normal case once Area A's refresh succeeds).
      STALE:       benchmark_data_date is not None and < observation_date
                   (a real, known label exists but it lags - whether
                   because the fetch failed, Alpaca hasn't posted the bar
                   yet, or this ticker's observation_date is itself in the
                   past relative to when this function is called)."""
    if benchmark_data_date is None:
        return REGIME_FRESHNESS_UNAVAILABLE
    if observation_date is not None and benchmark_data_date == observation_date:
        return REGIME_FRESHNESS_FRESH
    return REGIME_FRESHNESS_STALE
```

`compute_historical_regime_series` itself is untouched (Phase 16 §1.1's
constraint against modifying it still applies - it is not modified here
either).

### 1.3 Verification checklist - Area A

1. `_refresh_benchmark_data_safe`: monkeypatch `fetch_and_cache_universe`
   to (a) return `{"SPY": {"status": "fetched", ...}, "QQQ": {"status":
   "cached", ...}}` -> `primary_fetch_status="fetched"`, no ticker error
   recorded; (b) return `{"SPY": {"status": "failed", "error": "..."}}`
   -> `primary_fetch_status="failed"`, ONE ticker error recorded with
   `ticker="SPY"`; (c) raise an exception -> `primary_fetch_status=
   "exception"`, ONE ticker error recorded with `ticker=None`.
2. `_refresh_benchmark_data_safe` is called with EXACTLY
   `[DEFAULT_REGIME_CONFIG.primary_benchmark, DEFAULT_REGIME_CONFIG.
   secondary_benchmark]` (mock `fetch_and_cache_universe`, assert call
   args) - never `strategy_lab.universe.PRIMARY_BENCHMARK`/
   `SECONDARY_BENCHMARK` (assert those constants are never imported into
   `research_automation.py`).
3. `run_research_job` (integration): with a monkeypatched
   `fetch_and_cache_universe` that raises, the job still completes with
   `status=STATUS_SUCCESS_RESEARCH` (or `STATUS_PARTIAL_FAILURE_RESEARCH`
   if a per-ticker error also occurred) - a benchmark-refresh failure alone
   must never flip the overall run to `STATUS_FAILED`, and must never
   prevent observations from being built (fail-open, matching
   `_compute_config_fingerprint_safe`'s exact posture).
4. `classify_regime_freshness`: table-driven over the 3 states -
   `(None, None) -> UNAVAILABLE`; `("2026-08-14", "2026-08-14") ->
   FRESH`; `("2026-08-14", "2026-08-13") -> STALE`.
5. `regime_label_and_date_as_of`: same table-driven sweep as Phase 16 §1.4
   item 1/§8 item 2 (the exact real-world 16:45 ET scenario) plus the
   weekend/holiday-boundary tests from `tests/test_strategy_lab_regime_asof.py`
   - now additionally asserting the returned `benchmark_data_date` is the
   PRIOR available row's date, never `as_of_date` itself, in the lagging
   case.
6. `regime_label_as_of`'s full existing test suite
   (`tests/test_strategy_lab_regime_asof.py`) passes unmodified against the
   refactored implementation - zero behavior change.

## 2. Area B - Regime provenance on observations (modified, additive schema)

### 2.1 `strategy_lab/prospective.py` (modified)

Add import: `from research.config import DEFAULT_REGIME_CONFIG` (alongside
the existing `strategy_lab.regime_history` import, which now also imports
`regime_label_and_date_as_of` and `classify_regime_freshness`).

Four new columns on `research_prospective_observations` (additive
`ALTER TABLE`, exact existing `PRAGMA table_info` + guarded `ALTER TABLE`
pattern in `ensure_schema`):
```python
if "regime_benchmark" not in cols:
    conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN regime_benchmark TEXT")
if "regime_benchmark_source" not in cols:
    conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN regime_benchmark_source TEXT")
if "regime_benchmark_data_date" not in cols:
    conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN regime_benchmark_data_date TEXT")
if "regime_freshness_status" not in cols:
    conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN regime_freshness_status TEXT")
```
No existing row is ever touched - legacy rows get NULL for all four, same
as every prior additive migration in this file.

`record_observation`: add 4 new kwargs, ALL optional with `None` default
(backward-compatible with every existing call site in the test suite that
does not pass them):
```python
regime_benchmark: Optional[str] = None,
regime_benchmark_source: Optional[str] = None,
regime_benchmark_data_date: Optional[str] = None,
regime_freshness_status: Optional[str] = None,
```
Extend the `INSERT` column list/`VALUES` placeholders/tuple accordingly.
**No change to the `ON CONFLICT(ticker, observation_date) DO NOTHING`
clause** - conflict semantics are untouched.

`build_todays_observation`: replace the current
```python
regime_series = compute_historical_regime_series(conn)
regime_label = regime_label_as_of(regime_series, as_of_date)
```
with
```python
regime_series = compute_historical_regime_series(conn)
regime_label, regime_benchmark_data_date = regime_label_and_date_as_of(regime_series, as_of_date)
observation_date_value = as_of_date or indicators.latest_date
regime_freshness_status = classify_regime_freshness(observation_date_value, regime_benchmark_data_date)
```
(`observation_date_value` replaces the existing duplicated
`as_of_date or indicators.latest_date` expression at the return statement
- same value, computed once.) Add to the returned dict:
```python
"observation_date": observation_date_value,   # was: as_of_date or indicators.latest_date (identical value)
...
"regime_benchmark": DEFAULT_REGIME_CONFIG.primary_benchmark,
"regime_benchmark_source": RESEARCH_SOURCE,
"regime_benchmark_data_date": regime_benchmark_data_date,
"regime_freshness_status": regime_freshness_status,
```
No other line of `build_todays_observation` changes - `is_bullish =
regime_label == BULLISH_LABEL` stays exactly as-is, still degrades
correctly on `regime_label=None`.

### 2.2 Verification checklist - Area B

1. Schema migration: fresh DB -> `research_prospective_observations` has
   all 4 new columns; a pre-existing DB missing them gets them added via
   `ALTER TABLE`, existing rows' values for all other columns unchanged.
2. `build_todays_observation` returns `regime_benchmark="SPY"`,
   `regime_benchmark_source="alpaca_adjusted"` for every call (constants,
   not data-dependent).
3. Fresh case: SPY has a bar exactly on `as_of_date` -> `regime_benchmark_data_date
   == as_of_date`, `regime_freshness_status="FRESH"`.
4. Stale case: SPY's latest available bar is before `as_of_date` (the
   exact Phase 16 §0.1 scenario) -> `regime_benchmark_data_date` is that
   prior date, `regime_freshness_status="STALE"`, `regime` is still the
   correct (non-None) prior label - Area A/B never conflict with Phase
   16's fix, they only add visibility to it.
5. Missing case: zero SPY history -> `regime_benchmark_data_date=None`,
   `regime_freshness_status="UNAVAILABLE"`, `regime=None` (unchanged Phase
   16 behavior).
6. `record_observation` called WITHOUT the 4 new kwargs (mirrors every
   existing test call site, e.g. `tests/test_ops_correction_impact_audit.py
   ::_seed_observation`) still succeeds; the 4 new columns are NULL for
   that row.
7. Idempotency: calling `build_todays_observation` twice for the same
   `as_of_date` against unchanged data returns identical
   `regime_benchmark_data_date`/`regime_freshness_status` both times (pure
   read, no interaction with the dedup path - mirrors Phase 16 §8 item 11).

## 3. Area C - Exit-event attribution (modified, additive schema)

### 3.1 `strategy_lab/prospective.py` (modified, same file as Area B)

Four new columns on `research_prospective_observations` (same additive
`ALTER TABLE` pattern, same `ensure_schema` block as §2.1):
```python
control_exit_signal INTEGER
experiment_a_exit_signal INTEGER
experiment_b_exit_technical_signal INTEGER
experiment_b_exit_regime_loss_signal INTEGER
```

`record_observation`: 4 more new kwargs, ALL optional with `None` default
(same backward-compatibility rationale as §2.1):
```python
control_exit_signal: Optional[bool] = None,
experiment_a_exit_signal: Optional[bool] = None,
experiment_b_exit_technical_signal: Optional[bool] = None,
experiment_b_exit_regime_loss_signal: Optional[bool] = None,
```
In the `INSERT` tuple, each must be cast `int(x) if x is not None else
None` (unlike the required entry-signal fields, which are always bool and
use a plain `int(...)` - these are Optional, so a `None`-guard is
required to avoid `int(None)` raising).

`build_todays_observation`: add, alongside the existing inline
`entry_qualifies` computation (same STAGE_ORDER/DEFAULT_RULES already
imported there - §0.3's inline-duplication decision):
```python
technical_exit_qualifies = (
    STAGE_ORDER[score.highest_confirmed_stage] < STAGE_ORDER[DEFAULT_RULES.exit_stage_floor]
    or score.score <= DEFAULT_RULES.exit_max_score
)
# "regime loss" is a KNOWN condition, not "unknown regime" - a None
# regime_label (Area A/B stale-or-missing case) must NEVER be reported as
# a regime-loss exit signal, since we do not actually know the regime left
# bullish_trend when we don't know the regime at all. Fabricating a
# regime-loss claim off a data gap would misattribute an EXPERIMENT_B exit
# event to a condition that was never actually observed.
experiment_b_regime_loss = regime_label is not None and regime_label != BULLISH_LABEL
```
Add to the returned dict:
```python
"control_exit_signal": technical_exit_qualifies,
"experiment_a_exit_signal": technical_exit_qualifies,       # identical rule to CONTROL - phase10_experiments.py: A's exit is "original frozen exit only", same as CONTROL
"experiment_b_exit_technical_signal": technical_exit_qualifies,  # same shared technical rule
"experiment_b_exit_regime_loss_signal": experiment_b_regime_loss,
```
These are LEVEL fields (mirrors the existing `control_entry_signal`/
`experiment_a_entry_signal`/`experiment_b_entry_signal` convention exactly)
- "is this condition true today," not "did it just become true." Transition
detection (§3.2) is where the actual event fires, same layering as entry
signals already use.

**No change anywhere to `backtest/config.py`, `backtest.scoring`,
`strategy_lab/phase10_experiments.py`, or any real trading exit path** -
this is the SAME computation `trading.signals_bridge.exit_qualifies` uses
(duplicated, not imported - §0.3), applied read-only to already-stored
observation fields, purely for research-event visibility. It cannot affect
any real position.

### 3.2 `strategy_lab/prospective_events.py` (modified)

Four new event type constants:
```python
EVENT_CONTROL_EXIT = "control_exit"
EVENT_EXPERIMENT_A_EXIT = "experiment_a_exit"
EVENT_EXPERIMENT_B_EXIT_TECHNICAL = "experiment_b_exit_technical"
EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS = "experiment_b_exit_regime_loss"

ALL_EVENT_TYPES = (
    EVENT_SCORE_CROSSING, EVENT_TREND_ADVANCE, EVENT_MOMENTUM_ADVANCE, EVENT_VOLUME_ADVANCE,
    EVENT_CONTROL_ENTRY, EVENT_EXPERIMENT_A_ENTRY, EVENT_EXPERIMENT_B_ENTRY,
    EVENT_CONTROL_EXIT, EVENT_EXPERIMENT_A_EXIT, EVENT_EXPERIMENT_B_EXIT_TECHNICAL, EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS,
)  # 11 total (was 7)
```

New function, mirrors `_entry_transition_events` exactly:
```python
def _exit_transition_events(prev_row: Optional[dict], curr_row: dict) -> List[str]:
    """Same False->True transition convention as _entry_transition_events -
    a ticker's very first observation is never an exit event; a condition
    that stays continuously true produces exactly one event, on the day it
    first became true.

    Decision (both-true-simultaneously case): if, on the SAME
    (ticker, observation_date), BOTH experiment_b_exit_technical_signal AND
    experiment_b_exit_regime_loss_signal newly transition True (e.g. a
    ticker's score/stage crosses the technical exit floor on the exact same
    day the regime itself leaves bullish_trend), BOTH
    EVENT_EXPERIMENT_B_EXIT_TECHNICAL and EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS
    are recorded independently - never merged, never prioritized/deduped
    into one. Justification: these are two independently-defined,
    independently-meaningful conditions (not two labels for one underlying
    fact), and the existing EVENT_EXPERIMENT_A_ENTRY/EVENT_EXPERIMENT_B_ENTRY
    co-occurrence (Phase 16 §0.2 - both variants share one entry rule and
    always fire together) already establishes the precedent that multiple
    independently-true event types recorded on the same (ticker, date) is
    normal, not an anomaly requiring special-case suppression."""
    if prev_row is None:
        return []
    events = []
    for field_name, event_type in (
        ("control_exit_signal", EVENT_CONTROL_EXIT),
        ("experiment_a_exit_signal", EVENT_EXPERIMENT_A_EXIT),
        ("experiment_b_exit_technical_signal", EVENT_EXPERIMENT_B_EXIT_TECHNICAL),
        ("experiment_b_exit_regime_loss_signal", EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS),
    ):
        was = bool(prev_row.get(field_name))
        now = bool(curr_row.get(field_name))
        if now and not was:
            events.append(event_type)
    return events
```
In `detect_events_for_new_observation`, add:
```python
event_types += _exit_transition_events(prev_row, curr_row)
```
(alongside the existing `_entry_transition_events(prev_row, curr_row)`
call). `record_event`/`record_events_for_observation` are UNCHANGED -
`event_type` is just another string value from the now-11-member
`ALL_EVENT_TYPES`; the `UNIQUE(ticker, event_type, event_date)` /
`ON CONFLICT ... DO NOTHING` dedup logic already handles any event type
generically.

### 3.3 `ops/event_provenance_audit.py` (modified - additive keys/functions only)

New dict (kept SEPARATE from the existing, entry-only
`EVENT_TYPE_TO_VARIANT` - never merged into it, to avoid collapsing
`EVENT_EXPERIMENT_B_EXIT_TECHNICAL`/`EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS`
into one undifferentiated "EXPERIMENT_B" count, which would erase exactly
the information this phase exists to surface):
```python
EXIT_EVENT_TYPE_TO_VARIANT = {
    EVENT_CONTROL_EXIT: "CONTROL",
    EVENT_EXPERIMENT_A_EXIT: "EXPERIMENT_A",
    EVENT_EXPERIMENT_B_EXIT_TECHNICAL: "EXPERIMENT_B",
    EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS: "EXPERIMENT_B",
}
```

`compute_event_type_breakdown` (MODIFIED - additive key, existing keys
unchanged in shape/values): add a third bucket `exit_attributable`
alongside the existing `entry_attributable`/`shared_signal_detection`, and
change the per-type classification to a three-way branch (entry -> exit ->
shared) so the 4 new exit types no longer fall into
`shared_signal_detection` (which would otherwise mislabel them as "not
variant-specific by design" - false for exit events):
```python
def compute_event_type_breakdown(conn) -> dict:
    """{'entry_attributable': {...} (unchanged, 3 entry types),
    'exit_attributable': {...} (NEW, 4 exit types), 'shared_signal_detection':
    {...} (unchanged membership: the 4 non-variant-specific types),
    'total_events': n}."""
    events = load_events(conn)
    counts = events["event_type"].value_counts().to_dict() if not events.empty else {}
    entry_attributable, exit_attributable, shared_signal_detection = {}, {}, {}
    for event_type in ALL_EVENT_TYPES:
        count = int(counts.get(event_type, 0))
        if event_type in EVENT_TYPE_TO_VARIANT:
            entry_attributable[event_type] = count
        elif event_type in EXIT_EVENT_TYPE_TO_VARIANT:
            exit_attributable[event_type] = count
        else:
            shared_signal_detection[event_type] = count
    return {
        "entry_attributable": entry_attributable, "exit_attributable": exit_attributable,
        "shared_signal_detection": shared_signal_detection, "total_events": int(len(events)),
    }
```

New function (exit-side counterpart to `compute_event_provenance_coverage`,
kept SEPARATE - that existing function/its `by_variant` shape stays
entry-only and byte-unchanged):
```python
def compute_exit_event_type_provenance(conn) -> dict:
    """{'by_event_type': {event_type: count for each of the 4 exit types,
    always present, 0-filled}, 'events_with_fingerprint',
    'events_unknown_legacy', 'total_exit_events'}. Counted PER EVENT TYPE
    (not per variant) deliberately - EVENT_EXPERIMENT_B_EXIT_TECHNICAL and
    EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS both map to variant 'EXPERIMENT_B'
    but must stay independently visible."""
```

`compute_exit_attribution_gap_note()`: **UNCHANGED** - it was deliberately
built (Phase 16) to dynamically re-inspect `ALL_EVENT_TYPES` at call time.
Once Phase 17's 4 exit types land in `ALL_EVENT_TYPES`, this function
AUTOMATICALLY returns `exit_event_types_exist=True` with the 4 real exit
type names and a "found" note - no code change needed, this is the
anticipated future event Phase 16 explicitly built this for.

New fixed-string constant, mirrors `ENTRY_AB_COOCCURRENCE_NOTE`:
```python
EXPERIMENT_B_EXIT_COOCCURRENCE_NOTE = (
    "When both EVENT_EXPERIMENT_B_EXIT_TECHNICAL and "
    "EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS newly transition true on the same "
    "(ticker, observation_date), BOTH events are recorded independently - "
    "never merged or prioritized. This mirrors the existing "
    "EVENT_EXPERIMENT_A_ENTRY/EVENT_EXPERIMENT_B_ENTRY co-occurrence "
    "convention (docs/specs/phase16.md §0.2) of allowing multiple "
    "independently-true event types to co-occur on the same day."
)
```

`event_provenance_audit_summary` (MODIFIED - 2 new top-level keys added,
existing 4 keys unchanged):
```python
def event_provenance_audit_summary(conn) -> dict:
    return {
        "event_type_breakdown": compute_event_type_breakdown(conn),
        "event_provenance_coverage": compute_event_provenance_coverage(conn),
        "exit_attribution_gap": compute_exit_attribution_gap_note(),
        "entry_ab_cooccurrence_note": ENTRY_AB_COOCCURRENCE_NOTE,
        "exit_event_provenance": compute_exit_event_type_provenance(conn),          # NEW
        "experiment_b_exit_cooccurrence_note": EXPERIMENT_B_EXIT_COOCCURRENCE_NOTE, # NEW
    }
```

**Pre-existing test updates required** (documented here explicitly - these
are expected, anticipated consequences of building what Phase 16 designed
these checks to detect, not accidental breakage) in
`tests/test_ops_event_provenance_audit.py`:
- `test_exit_attribution_gap_note_real_all_event_types_is_false` must be
  renamed/updated: the REAL (unpatched) `ALL_EVENT_TYPES` now genuinely
  contains exit types, so `compute_exit_attribution_gap_note()`'s real
  output is now `exit_event_types_exist=True` with the 4 real exit type
  names and a "found" note. Rename to
  `test_exit_attribution_gap_note_real_all_event_types_is_true_post_phase17`
  and update assertions accordingly.
- `test_event_provenance_audit_summary_rollup_shape`'s exact
  `set(summary.keys()) == {...}` assertion must be widened from 4 to 6
  keys (add `"exit_event_provenance"`, `"experiment_b_exit_cooccurrence_note"`).

### 3.4 Verification checklist - Area C

1. Schema migration for the 4 new observation columns - same pattern/proof
   as §2.2 item 1.
2. `build_todays_observation`: technical exit condition table-driven over
   stage/score combinations that do/don't cross `DEFAULT_RULES.
   exit_stage_floor`/`exit_max_score` - `control_exit_signal`,
   `experiment_a_exit_signal`, `experiment_b_exit_technical_signal` are
   always identical to each other for the same input (shared rule).
3. `experiment_b_exit_regime_loss_signal`: `regime_label="bearish_trend"`
   -> `True`; `regime_label="bullish_trend"` -> `False`; `regime_label=
   None` -> `False` (the data-gap-is-not-a-known-loss edge case - explicit
   regression test).
4. `_exit_transition_events`: a ticker's first-ever observation (prev_row=
   None) produces zero exit events, even if all 4 level fields are True on
   that first observation (mirrors the existing entry-side "no baseline, no
   event" convention).
5. A level field that stays continuously True across 3+ consecutive
   observations produces exactly ONE event, on the day it first became
   True (not one per day it continues to hold).
6. **Experiment B regime-loss-alone case**: seed prev_row with
   `experiment_b_exit_technical_signal=False, experiment_b_exit_regime_loss_signal=False`
   and curr_row with `experiment_b_exit_technical_signal=False,
   experiment_b_exit_regime_loss_signal=True` -> exactly
   `[EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS]` fires, `EVENT_EXPERIMENT_B_EXIT_TECHNICAL`
   does NOT.
7. **Both-true-simultaneously case**: seed prev_row with both False, curr_row
   with both True -> BOTH `EVENT_EXPERIMENT_B_EXIT_TECHNICAL` and
   `EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS` fire (both present in the returned
   list) - proves the documented decision (§3.2), never silently drops one.
8. `ALL_EVENT_TYPES` has exactly 11 members, exact string values as listed
   in §3.2.
9. `record_events_for_observation`/`record_event`'s existing
   `ON CONFLICT(ticker, event_type, event_date) DO NOTHING` dedup still
   applies generically to all 4 new event types (retry-safe, matches Phase
   16 §8 item 11's exact idempotency pattern).
10. `compute_event_type_breakdown`: seed one of each of the 4 new exit
    types -> all land under `exit_attributable`, none under
    `shared_signal_detection` or `entry_attributable`; the 4 pre-existing
    shared types are unaffected (regression against Phase 16's own seeded
    test in `test_event_type_breakdown_buckets_entry_attributable_vs_shared`).
11. `compute_exit_event_type_provenance`: `EVENT_EXPERIMENT_B_EXIT_TECHNICAL`
    and `EVENT_EXPERIMENT_B_EXIT_REGIME_LOSS` counted SEPARATELY in
    `by_event_type` even though both map to "EXPERIMENT_B" - never summed
    together anywhere in this function's output.
12. The 2 required pre-existing test updates (§3.3) are made and the full
    file still passes.
13. Zero-writes proof for `compute_exit_event_type_provenance` and the
    modified `compute_event_type_breakdown`/`event_provenance_audit_summary`
    - same before/after row-count snapshot pattern as Phase 16 §3.1 item 6.

## 4. Area D - Monitoring/reporting (additive only)

### 4.1 `ops/daily_report.py` (modified - additive dataclass fields/sections)

New import: `from strategy_lab.prospective import load_observations`
(alongside the existing `strategy_lab.research_automation` import - same
established `ops -> strategy_lab` read-only dependency direction).

`RegimeSection` (MODIFIED - 3 new fields, all with defaults so existing
positional/keyword construction sites remain valid):
```python
@dataclass
class RegimeSection:
    ok: bool
    label: Optional[str]
    reason: Optional[str]
    as_of_date: Optional[str] = None          # NEW: the LIVE compute_market_regime's own result.as_of_date, previously discarded
    benchmark: Optional[str] = None           # NEW: result.benchmark
    is_stale_vs_report_date: Optional[bool] = None  # NEW: True iff as_of_date != report_date (best-effort calendar-day compare, NOT NYSE-session-aware - see caption text)
```
`_build_market_regime(conn, today: date)` (signature gains `today`; its one
call site in `build_daily_report` already has `today` in scope):
```python
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
```

New section dataclass (kept EXPLICITLY SEPARATE from `RegimeSection` -
this is the PROSPECTIVE, point-in-time regime lookup used by today's
research observations, never the LIVE `compute_market_regime` read above;
labeled distinctly in both the dataclass docstring and the rendered text so
the two are never conflated, per the task's explicit "keep retrospective
and prospective evidence clearly separated" requirement):
```python
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
```

`DailyReport` (MODIFIED - one new required field, always populated by
`build_daily_report`):
```python
@dataclass
class DailyReport:
    ...
    market_regime: RegimeSection
    prospective_regime_freshness: ProspectiveRegimeFreshnessSection   # NEW
```
`build_daily_report`: `market_regime=_build_market_regime(conn, today)`
(now passes `today`), add
`prospective_regime_freshness=_build_prospective_regime_freshness(conn, today)`.

`report_to_dict` (unchanged - `dataclasses.asdict` picks up new fields
automatically, recursively, including the new nested dataclass).

`render_report_text`: EXTEND the existing "Market Regime" section and add a
new section, so both are actually visible via the text-rendering path (not
just the JSON):
```python
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
```

### 4.2 `ops/prospective_audit.py` (extended - additive functions only)

New import: reuse `strategy_lab.regime_history.REGIME_FRESHNESS_FRESH/
STALE/UNAVAILABLE` (§1.2).

```python
def compute_regime_freshness_distribution(conn) -> dict:
    """{'by_status': {FRESH:n, STALE:n, UNAVAILABLE:n, 'NULL':n (rows
    created before this Phase 17 migration - column not yet populated for
    that row, disambiguated from a genuine UNAVAILABLE outcome)}, 'total'}
    - a pure rollup of load_observations(conn)['regime_freshness_status'],
    mirroring compute_regime_distribution's (Phase 16) own literal 'NULL'
    bucket convention exactly."""

def phase17_monitoring_summary(conn, today: Optional[date] = None) -> dict:
    """Additive rollup: returns phase16_monitoring_summary(conn, today)'s
    existing dict (UNCHANGED keys - the exit-event provenance additions
    from Area C flow through automatically via the pre-existing
    'event_provenance_audit' key, since event_provenance_audit_summary's
    OWN return dict grew additively, §3.3 - no key rename needed here) with
    ONE new top-level key: 'regime_freshness_distribution'. Mirrors Phase
    16's own phase16_monitoring_summary-wraps-long_term_monitoring_summary
    layering exactly - never mutates phase16_monitoring_summary itself."""
    today = today or date.today()
    summary = dict(phase16_monitoring_summary(conn, today=today))
    summary["regime_freshness_distribution"] = compute_regime_freshness_distribution(conn)
    return summary
```

### 4.3 `dashboard/data.py` (additive getter)

```python
@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_phase17_monitoring_summary() -> dict:
    from ops.prospective_audit import phase17_monitoring_summary
    with db_session() as conn:
        return phase17_monitoring_summary(conn)
```
Add to `clear_all_caches()`.

### 4.4 `dashboard/views/ops_overview.py` (modified - additive within existing functions)

`_render_system_health(report)`: after the existing `mr = report[
"market_regime"]` line and its 4-column metrics, add a caption for the new
LIVE-regime staleness flag and a small block for the PROSPECTIVE freshness
section, explicitly labeled to distinguish the two:
```python
if mr.get("is_stale_vs_report_date"):
    st.caption(f"⚠️ Live regime benchmark data as-of {mr.get('as_of_date')} lags report date {report['report_date']}.")

prf = report.get("prospective_regime_freshness") or {}
st.caption(
    f"Prospective (point-in-time) regime freshness: "
    f"{prf.get('regime_freshness_status', 'n/a') if prf.get('ok') else 'unavailable'} "
    f"— distinct from the live regime above."
)
```

`_render_regime_provenance_monitoring()` (Phase 16, EXTENDED additively -
justified as the natural fit: this function already renders
`event_provenance_audit`'s entry-attributable/shared breakdown and the
`regime_distribution` table, so exit-event coverage and regime-freshness
distribution belong alongside them, not in a new disconnected section):
- Switch its data source from `get_phase16_monitoring_summary()` to
  `get_phase17_monitoring_summary()` (a strict superset - regression test
  §4.6 item 3 proves every Phase 16 key/value is unchanged).
- Add a "Regime freshness distribution" table (FRESH/STALE/UNAVAILABLE/NULL
  counts), from `summary["regime_freshness_distribution"]`.
- Extend the existing "Event type breakdown" block with a third column
  ("Exit-attributable") reading `breakdown.get("exit_attributable", {})`,
  alongside the existing entry/shared columns.
- Add an "Exit event provenance" block from
  `summary["event_provenance_audit"]["exit_event_provenance"]` (per-event-type
  counts) and display both `exit_attribution_gap`'s `note` (now describing
  the real exit types, per §3.3) and the new
  `experiment_b_exit_cooccurrence_note`.

### 4.5 `dashboard/views/strategy_lab.py` (modified - additive within existing function)

`_render_regime_provenance_detail()` (Phase 16, EXTENDED additively, same
justification as §4.4): switch to `get_phase17_monitoring_summary()`, add
the exit-event-provenance detail and `experiment_b_exit_cooccurrence_note`
alongside the existing exit-attribution-gap note display.

### 4.6 Verification checklist - Area D

1. `build_daily_report`: `market_regime.as_of_date`/`.benchmark` populated
   from a real `compute_market_regime` call; `is_stale_vs_report_date`
   table-driven over `as_of_date == today` / `< today` / regime unavailable
   (`None`, not `False`).
2. `_build_prospective_regime_freshness`: no observations at all -> `ok=
   False`; observations exist but none for `today` -> `ok=False` with the
   date-specific reason; a real observation for `today` -> `ok=True` with
   the 3 fields correctly read from that row.
3. `phase17_monitoring_summary` is a strict superset of
   `phase16_monitoring_summary`'s keys/values for identical inputs
   (mirrors Phase 16 §5.6 item 3's exact pattern).
4. `render_report_text` includes both the "Market Regime (live)" and
   "Prospective Regime Freshness" sections with the new fields visible as
   plain text (not just present in the JSON/dict form).
5. `report_to_dict`/JSON round-trip (extends the existing
   `test_report_to_dict_json_serializable_and_round_trips_through_repository`
   test) still passes with the new nested dataclass field present.
6. Dashboard read-only safety: grep the 2 extended render functions for
   `st.button`/`st.form` -> none found (extends Phase 15/16's existing
   pattern).
7. `get_phase17_monitoring_summary`: idempotent, opens exactly one
   `db_session()`, present in `clear_all_caches()`.
8. Zero-writes proof for `compute_regime_freshness_distribution` and
   `phase17_monitoring_summary` (same snapshot pattern as §3.4 item 13).

## 5. Hard constraints - restated per area, verified against this design

- **No change to strategy rules, signal weights/thresholds, CONTROL/A/B
  methodology, risk config, watchlist, alerts/cooldown, Discord config,
  evidence thresholds, or production behavior.** `strategy_lab/
  phase10_experiments.py`, `backtest/config.py`, `db/schema.py`,
  `deploy/`, `ops/evidence_classification.py` are NOT edited anywhere in
  this spec - confirmed no area requires touching any of them. Area C's
  exit-signal computation reuses `backtest.config.DEFAULT_RULES`/
  `backtest.scoring.STAGE_ORDER` READ-ONLY (imported, never mutated),
  exactly as the pre-existing `entry_qualifies` already does in the same
  file.
- **No orders placed/canceled/modified anywhere in Phase 17 code.** Zero
  import of `trading.engine`/`trading.orders`/`trading.run_paper` in any
  new/modified file. `ops/daily_report.py`'s pre-existing
  `trading.client`/`trading.portfolio`/`trading.signals_bridge` imports are
  unchanged (read-only, pre-existing, not touched by this phase's edits to
  that file). `strategy_lab/prospective.py` gains NO `trading.*` import at
  all (§0.3 decision).
- **No Discord messages.** Zero import of `alerts.discord`/`alerts.runner`/
  `alerts.run_alerts` anywhere in Phase 17 code.
- **No LaunchAgent/schedule changes, no new scheduled jobs, `deploy/`
  untouched.** Area A's change is entirely inside `run_research_job`'s own
  function body (one new call, one new helper function, one new
  dataclass field) - the SAME job the LaunchAgent already invokes daily,
  doing one more thing. Nothing under `deploy/` is read, referenced, or
  modified.
- **No historical prospective records rewritten, no automatic backfill.**
  All 8 new `research_prospective_observations` columns (§2.1, §3.1) and
  the 4 new event types are additive/`ADD COLUMN`-only; every existing row
  keeps its exact current values, new columns are NULL for legacy rows.
  `ops/regime_reconstruction_audit.py` (Phase 16, legacy NULL-regime
  backfill-adjacent reporting) is NOT modified - Area B/C are forward-only,
  exactly like Phase 16 Area A/D.
- **No optimization/new trading filters.** Area C's exit-signal fields are
  computed and stored for OBSERVATION/EVENT purposes only; nothing in this
  spec reads them back into any entry/exit decision, `trading/` module, or
  automation pipeline. `strategy_lab/phase10_experiments.py`'s frozen
  `RegimeGatedRules` are read about (for documentation/justification) but
  never imported by any Phase 17 code path.

## 6. Files: full list

**New:**
- `tests/test_strategy_lab_regime_freshness_phase17.py` (Area A)
- `tests/test_strategy_lab_regime_provenance_phase17.py` (Area B)
- `tests/test_strategy_lab_exit_events_phase17.py` (Area C)
- `tests/test_ops_prospective_audit_phase17.py` (Area D, mirrors Phase
  16's own `test_ops_prospective_audit_phase16.py` convention of a
  separate file importing the same `ops.prospective_audit` module)

**Modified:**
- `strategy_lab/research_automation.py` (§1.1)
- `strategy_lab/regime_history.py` (§1.2, refactor-preserving + additive)
- `strategy_lab/prospective.py` (§2.1, §3.1 - same file, both areas'
  schema/function changes land together since both touch
  `build_todays_observation`/`record_observation`/`ensure_schema`)
- `strategy_lab/prospective_events.py` (§3.2)
- `ops/event_provenance_audit.py` (§3.3)
- `ops/daily_report.py` (§4.1)
- `ops/prospective_audit.py` (§4.2)
- `dashboard/data.py` (§4.3)
- `dashboard/views/ops_overview.py` (§4.4)
- `dashboard/views/strategy_lab.py` (§4.5)
- `tests/test_ops_event_provenance_audit.py` (§3.3 - 2 specific,
  documented, expected test updates; new tests added for the 2 new
  functions/2 new rollup keys)
- `tests/test_ops_daily_report.py` (extend with §4.6 items 1, 2, 4, 5)

**Untouched (confirm, do not edit):**
- `strategy_lab/phase10_experiments.py`, `backtest/config.py`,
  `db/schema.py`, `deploy/`, `ops/evidence_classification.py` (hard
  constraints, §5).
- `strategy_lab/universe.py` (§0.2 - PRIMARY_BENCHMARK/SECONDARY_BENCHMARK
  constants deliberately not reused).
- `trading/signals_bridge.py` (read about, never imported - §0.3).
- `strategy_lab/report.py`, `strategy_lab/report_phase10.py`,
  `strategy_lab/portfolio_simulator.py`, `strategy_lab/phase9_baseline.py`,
  `strategy_lab/phase10_baseline.py`, `strategy_lab/phase11_report.py` -
  frozen artifact generators (Phase 16 §1.1's constraint still applies;
  `compute_historical_regime_series` is not modified, only
  `regime_label_as_of`'s internals, which these files do not import).
- `ops/regime_reconstruction_audit.py`, `ops/completeness_classification.py`,
  `ops/correction_impact_audit.py`, `ops/evidence_provenance.py` (Phase
  15/16, imported from only where needed, never modified).
- `strategy_lab/cache_integrity.py`, `strategy_lab/data.py` (imported
  from only - `fetch_and_cache_universe` is called, not modified).
- `research/config.py`, `research/regime.py` (read from only -
  `DEFAULT_REGIME_CONFIG`/`RegimeResult.as_of_date`/`.benchmark` are
  read, never redefined).

## 7. Testing section (tester agent - full checklist)

In addition to each area's own §1.3/§2.2/§3.4/§4.6 checklist above:

1. **Fresh/stale/missing benchmark data - all 3 states, not just 2**:
   §1.3 item 4, §2.2 items 3-5 (covers `classify_regime_freshness` in
   isolation AND its wiring through `build_todays_observation`).
2. **No-look-ahead behavior, table-driven sweep**: §1.3 item 5, extending
   Phase 16's exact `tests/test_strategy_lab_regime_asof.py` sweep pattern
   (`test_as_of_date_sweep_inside_and_outside_range`) to
   `regime_label_and_date_as_of`'s date return value too, not just the
   label.
3. **Regime provenance field correctness**: §2.2 items 2-5.
4. **CONTROL/A/B entry+exit event detection - all 11 event types**: §3.4
   items 2, 5, 8, 10 (3 entry unchanged from Phase 16 + 4 shared unchanged
   + 4 new exit = 11 total, exact names listed in §3.2).
5. **Experiment B regime-loss attribution specifically**: §3.4 items 6-7
   (regime-loss-alone case AND both-true-simultaneously case, the two
   cases explicitly required by the task).
6. **Immutability/idempotency**: `research_prospective_observations`/
   `research_prospective_events`' existing `INSERT ... ON CONFLICT DO
   NOTHING` patterns are unchanged in kind (only column lists grew) -
   regression via `grep -n "conn.execute\|cur.execute" strategy_lab/
   prospective.py strategy_lab/prospective_events.py` showing every
   execute call is still part of `CREATE TABLE`/`ALTER TABLE`/the single
   pre-existing `INSERT ... ON CONFLICT DO NOTHING` statement per table,
   never a bare `UPDATE`/`DELETE` - mirrors Phase 16's exact
   `test_no_new_write_path_added_to_prospective_or_prospective_events_by_phase16`
   test, extended (or a new sibling test) to also assert this for Phase 17.
7. **Explicit safety tests - zero trading/Discord/alert-state/LaunchAgent
   side effects:**
   - Import-direction scans: the existing generic
     `tests/test_strategy_lab.py::test_strategy_lab_never_imports_order_execution_or_alerting`
     and `tests/test_ops_safety.py`'s generic `ops/*.py` scan already
     directory-wide cover every modified file in `strategy_lab/`/`ops/`
     automatically - zero changes needed to those two scans themselves.
   - New assertion (in `tests/test_strategy_lab_regime_provenance_phase17.py`
     or `tests/test_strategy_lab_exit_events_phase17.py`): `strategy_lab/
     prospective.py` imports ZERO names from any `trading.*` module (AST
     scan for any `ImportFrom` node whose `module` starts with `"trading"`)
     - proves the §0.3 inline-duplication decision was actually followed,
       not just planned.
   - Extend `tests/test_ops_event_provenance_audit.py`'s existing 4-part
     read-only AST/source-text scan pattern (already directory-generic
     over the whole file, so the new `EXIT_EVENT_TYPE_TO_VARIANT`/
     `compute_exit_event_type_provenance` additions are automatically
     covered with zero new scan code needed) - just confirm the existing
     scans still pass post-edit.
   - Explicit assertion that no Phase 17 file references `deploy/` or any
     `.plist` path as a string literal, and none imports `subprocess`/
     `os.system`/`launchctl`-adjacent calls (same grep+AST pattern as
     Phase 16 §8 item 9).
   - Explicit assertion that `research_automation.py`'s new
     `_refresh_benchmark_data_safe` function never calls anything from
     `alerts.*` and never writes to the `alerts`/`alert_state` tables (grep
     for the literal strings `"alert_state"`/`"INSERT INTO alerts"` across
     the modified file, zero matches outside comments).
8. **Timezone/NYSE-calendar correctness**: §1.3 item 5's weekend/holiday
   sweep, extending Phase 16's exact existing test cases
   (`test_no_lookahead_across_weekend_boundary`/
   `test_no_lookahead_across_holiday_boundary`) to also assert the correct
   `benchmark_data_date` return value.
9. **Duplicate/retry idempotency**: running `run_research_job()` twice for
   the same `today` post-fix produces identical `regime_benchmark_data_date`/
   `regime_freshness_status`/all 4 exit-signal values on the (unchanged,
   deduped) first-call row - mirrors Phase 16 §8 item 11 exactly, extended
   to the new fields.
10. Full `pytest tests/` green; `git diff` touches only files listed in §6.

## 8. Open questions (explicit - NOT decided here, for human/orchestrator confirmation before build)

1. **STALE threshold granularity.** `classify_regime_freshness` (§1.2)
   currently treats ANY lag (`benchmark_data_date < observation_date`,
   even by exactly one trading session - the expected transient state
   right after a fetch failure, before the next day's retry) as `STALE`,
   with no separate "mildly stale (1 session)" vs. "badly stale (multiple
   sessions)" tier. Given Area A's refresh runs every job execution, a
   1-session lag should self-heal the next trading day if the underlying
   cause was transient (e.g. Alpaca hadn't posted the bar yet at 16:45 ET).
   **Question for the user/orchestrator:** is a flat FRESH/STALE/UNAVAILABLE
   3-state classification sufficient for now, or should `STALE` be split
   further (e.g. `STALE_1_SESSION` vs. `STALE_MULTI_SESSION`) for sharper
   dashboard alerting? Not decided here - the 3-state version is the
   minimum viable, and is what §1.3/§2.2's tests are written against;
   splitting it further is a straightforward additive refinement if
   requested, not a redesign.
2. **Whether `ProspectiveRegimeFreshnessSection`'s "most recent
   observation for `report_date`, any ticker" sourcing is representative
   enough.** Since Area B's 4 new columns are computed ONCE per research
   job run and are identical across every ticker's observation that day
   (§2.1 - `regime_benchmark_data_date`/`regime_freshness_status` don't
   vary by ticker), reading any single row for that date is sufficient by
   construction TODAY. If a future phase ever made these values
   ticker-dependent, this section's "any ticker" sourcing would become
   incorrect. Flagged here only so this coupling assumption isn't silently
   forgotten; not a blocking concern for this phase.
3. **Secondary-benchmark (QQQ) freshness visibility.** §0.2 decided to
   refresh QQQ alongside SPY (cheap, low-risk), but this spec does NOT add
   a QQQ-specific freshness column/status to observations or the daily
   report (only SPY/`primary_benchmark` gates the regime label and gets
   full provenance tracking, per §0.2's own reasoning). If QQQ-specific
   staleness visibility (e.g. for the live `RegimeSection.secondary`
   context, or `RelativeStrengthConfig`'s sector-proxy use) is wanted
   later, that is a natural, small, additive follow-up - not built here,
   since nothing in the original task description asked for it and adding
   it now would be scope creep beyond the two explicitly-deferred Phase 16
   open questions this phase addresses.

## 9. Manual/integration verification before moving on from Phase 17

1. `pytest tests/` - full suite green, including all new Phase 17 test
   files and the 2 documented pre-existing test updates in §6.
2. Real, read-only verification against the live DB (safe - the ONLY
   write path touched by this phase is the pre-existing, additive
   `record_observation`/`fetch_and_cache_universe` machinery, unchanged in
   kind): run `phase17_monitoring_summary(conn)` against the actual repo
   DB and confirm it reports a real (likely all-NULL, since no rows exist
   yet under the new columns) `regime_freshness_distribution` without
   raising.
3. After deploying, monitor the NEXT scheduled research job run (16:45 ET)
   and confirm (read-only query) that: (a) `research_run_ticker_errors`
   shows no NEW spurious errors attributable to the benchmark-refresh step
   under normal conditions; (b) the day's new
   `research_prospective_observations` rows have `regime_benchmark_data_date`
   equal to that same trading date (i.e. `regime_freshness_status=
   "FRESH"`) whenever Alpaca's SPY bar was available by job time - this is
   the actual fix for Phase 16 Open Question #1.
4. Confirm (read-only query) that the SAME day's newly-created
   `research_prospective_events` table can, going forward, contain a
   `experiment_b_exit_regime_loss` row the next time the market regime
   genuinely leaves `bullish_trend` for a ticker that was previously
   entry-eligible under Experiment B - this is the actual fix for Phase 16
   Open Question #2 (cannot be verified synthetically-only; requires
   waiting for a real regime transition, but the mechanism itself is fully
   unit-tested per §3.4).
5. `streamlit run dashboard/app.py` - load both the Operations and
   Strategy Lab pages, confirm the extended Phase 16 sections (now
   Phase-16-plus-17) render without exception against a DB with zero new
   Phase 17 data yet (empty-state paths for `regime_freshness_distribution`
   all-NULL/all-zero, `exit_event_provenance` all-zero).
6. `git diff` touches only files listed in §6.
