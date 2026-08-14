# Phase 13 — Data Integrity, Provider Reconciliation, and Reliability Hardening

Status: PLANNED. No implementation in this document. Coder implements exactly
this; tester validates exactly this; reviewer checks against exactly this.

## 0. Verified current state (2026-08-14, HEAD af5bdec)

### 0.1 Provider divergence — CONFIRMED against real DB rows (not guessed)

**Correction to the task's framing:** the live DB currently contains **only
two source tags: `"alpaca"` (6,406 rows) and `"alpaca_adjusted"` (115,368
rows). There are zero `"yfinance"` rows anywhere in `prices`** (confirmed via
`SELECT source, COUNT(*) FROM prices GROUP BY source`). `ingestion/yfinance_source.py`
exists as code and is in `SOURCE_PRIORITY = ["yfinance","alpaca"]`, but has
never actually ingested data into this DB — it's a configured fallback that
has never fired, not an active second provider. So "the AAPL/MSFT/NVDA
divergence" is actually **`alpaca` (raw, production, ~30-day window) vs.
`alpaca_adjusted` (split+dividend adjusted, `strategy_lab/data.py`'s isolated
research source, ~5-year window)** — not an Alpaca-vs-yfinance issue. This
spec's classifier and tests are written against the real pairing.

Direct query of `prices` for AAPL/MSFT/NVDA (both sources, `date >= 2026-08-01`
plus NVDA's full history) revealed **three distinct, confirmed real
phenomena**, not one:

**(a) Genuine small dividend-adjustment differences (the common case).**
For most overlapping dates (e.g. AAPL 2026-08-07: `alpaca` close=313.33 vs
`alpaca_adjusted` close=313.06, a 0.086% difference; same pattern on
2026-08-03 through 2026-08-10 for AAPL/MSFT/NVDA, consistently ~0.086%–0.13%),
**close price differs by a small, source-consistent amount while volume is
byte-identical between the two sources.** This is exactly what a
dividend-only adjustment looks like (dividends rescale historical closes,
never volume).

**Implementation-time correction (verified against §3.1's own mandated
check order):** all of AAPL/MSFT's real measured dividend-only deltas
(~0.086%–0.13%) fall UNDER `CLOSE_MATCH_TOLERANCE_PCT` (0.15%), so per
check #1 ("first match wins," close AND volume both within tolerance),
these real rows correctly classify as `MATCHED`, not
`EXPECTED_ADJUSTMENT_DIFFERENCE` — this is intentional, not a bug: `MATCHED`
already means "no material divergence, volume-consistent," which IS the
correct signal for a dividend delta this small. `EXPECTED_ADJUSTMENT_DIFFERENCE`
is reserved for a same-volume, differing-adjustment-mode pair whose price
delta EXCEEDS the match tolerance (e.g. a larger dividend, or multiple
dividends since the research source was last refreshed) — no real captured
row in this DB happens to be large enough to land in that bucket via the
adjustment-mode path alone (NVDA's split, pattern (b), is the only real
`EXPECTED_ADJUSTMENT_DIFFERENCE` example currently in the DB). Both
outcomes are non-degrading in `classify_overall_provenance_aware` (§4.2),
so this doesn't change the phase's safety properties — it only changes
which specific classification label a given real row receives. §8.1's
test for this pattern uses a synthetic delta exceeding tolerance (still
same-volume, differing adjustment mode) to exercise the
`EXPECTED_ADJUSTMENT_DIFFERENCE` branch; the real 313.33/313.06 row is
used to test the `MATCHED` branch instead.

**(b) A genuine, confirmed historical stock split — NVDA, June 2024.**
Querying the full NVDA overlap for rows where volume differs turned up a
large, sustained block: for every date from 2024-05-28 through 2024-06-07
(and earlier), `alpaca` (raw) shows closes around $1,100–$1,225 with volume
V, while `alpaca_adjusted` shows closes around $110–$122 (**ratio ≈ 10.00,
e.g. 1208.88/120.68 = 10.017**) with **volume scaled by the same ~10x**
(e.g. 412,385,800 vs 41,238,580). This is NVDA's real, well-known 10-for-1
stock split (June 2024) — raw data correctly shows pre-split price levels
for pre-split dates, `alpaca_adjusted` correctly back-adjusts them.
Classify as `EXPECTED_ADJUSTMENT_DIFFERENCE` via ratio-based split detection
— **both price AND volume scale by the same ratio**, which is the
distinguishing signature vs. case (c) below.

**(c) A real, previously-undiagnosed data-quality issue — NOT an
adjustment effect.** On exactly one date per ticker — **the single most
recent date present in `alpaca_adjusted`** (2026-08-12 for all three
tickers at investigation time) — both close AND volume diverge, but by
amounts that **do not share a consistent ratio** and do **not** match any
known split (AAPL: close ratio 0.998 but volume ratio 0.613; MSFT: close
ratio 1.0006 but volume ratio 0.535; NVDA: close ratio 0.999 but volume
ratio 0.703). Cross-referencing `fetched_at`: `alpaca_adjusted`'s row for
2026-08-12 has `fetched_at = '2026-08-12 18:54:58'` — **18:54:58 UTC =
14:54:58 ET, i.e. fetched intraday, before that day's 16:00 ET close**,
while `alpaca`'s row for the same date has `fetched_at = '2026-08-13
20:30:07'` (fetched the next day, after close, via the normal production
pipeline). **Root cause: `strategy_lab/data.py::fetch_and_cache_universe`
was run intraday on 2026-08-12 and Alpaca returned a preliminary/incomplete
bar for the still-open trading day; `_needs_fetch`'s row-count-only check
means that ticker was never re-fetched afterward, so the incomplete bar is
now permanently frozen as `alpaca_adjusted`'s "final" row for that date.**
This is a real, generic bug pattern (not specific to these 3 tickers) that
the classifier must NOT silently absorb into `EXPECTED_ADJUSTMENT_DIFFERENCE`
just because the price delta alone looks adjustment-sized — volume
divergence is the tell, and §3.1 below adds a volume-consistency check
specifically because of this finding. Classify as `STALE_SOURCE` (the
non-authoritative source's most recent row is a stale/incomplete snapshot,
not a finalized session).

**Implication for the classifier design:** price-ratio-only comparison
(the original design) would have silently misclassified case (c) as a
normal adjustment difference. §3.1's `classify_pair` therefore checks
volume consistency alongside price before accepting an adjustment-mode
mismatch as `EXPECTED_ADJUSTMENT_DIFFERENCE`, and adds an explicit
same-session-intraday-fetch heuristic for `STALE_SOURCE`.

### 0.2 Date-sensitive test bug — CONFIRMED by direct code read

Verified directly (not inferred):
- `automation/recovery.py:51-59` (`_todays_successful_run_exists`) and
  `strategy_lab/research_automation.py:84-94` (`_todays_production_run_succeeded`)
  both execute exactly:
  ```python
  history = load_run_history(conn, limit=50)
  todays = history[history["started_at"].astype(str).str.startswith(today.isoformat())]
  ```
- `db/schema.py:96`: `started_at TEXT NOT NULL DEFAULT (datetime('now'))` —
  raw SQLite `datetime('now')`, which is **UTC**.
- `db/database.py:1-13`: plain `sqlite3.connect(DB_PATH)`, no timezone
  pragma, no `'localtime'` modifier anywhere.
- `today` in both functions and in the 3 failing tests is `date.today()` —
  the **local** system calendar date.
- Whenever local date ≠ UTC date (roughly 20:00–23:59 in America/New_York,
  which is the timezone `deploy/*.plist.example` assumes the host runs in),
  the UTC-timestamp-string-prefix-vs-local-date comparison silently fails
  to match a row inserted moments earlier. The scheduled production path
  (16:30/16:45 ET) never hits this window; ad-hoc/test/recovery runs at
  arbitrary times do.
- **Confirmed: genuine production-code bug (bad "today" derivation), not a
  stale test fixture.** `automation_runs` has no explicit trading-date
  column to match against instead — that's the structural gap. (Contrast:
  `research_run_history` already stores its own explicit `trading_date`
  column — it's only the shared read of `automation_runs` that lacks one.)

### 0.3 Other confirmed current-state facts

- Production source-of-truth: `signals/engine.py::score_ticker` →
  `indicators/technical.py::compute_indicators_for_ticker(source=None)` →
  `db/price_repository.py::load_price_history(source=None)` →
  `resolve_source()` (`SOURCE_PRIORITY = ["yfinance","alpaca"]`, currently
  always resolves to `"alpaca"` in practice since no `yfinance` rows exist).
  Single source per ticker, never blended.
- Research/backtest source-of-truth: every `strategy_lab/*`/`backtest/*`
  caller passes `source=strategy_lab.data.RESEARCH_SOURCE` (`"alpaca_adjusted"`)
  explicitly. This is the existing isolated adjusted research source — not
  reinvented here.
- `ops/data_quality.py` (Phase 12) already derives freshness/gaps/invalid-OHLCV
  from the *resolved* (authoritative) source only — correct, unchanged by
  this phase. Its cross-source check (`_count_cross_source_conflicts`) is a
  blunt ">1% close difference between any two sources, same date" counter
  with no adjustment-mode or volume awareness — this is the component this
  phase fixes.
- `db/schema.py::init_db` already has precedent for an additive,
  `PRAGMA table_info`-guarded migration (`_migrate_pre_phase5_alerts_table`)
  — this phase's schema fix follows that exact pattern.
- Dashboard extension point: `dashboard/views/ops_overview.py::_render_system_health`,
  reading `dashboard/data.py::get_data_quality_report()` (already
  `dataclasses.asdict(report)` — new dataclass fields flow through with
  zero change to that getter).
- `production_fingerprint.json`'s local `computed_at`-only modification
  (noted in Phase 12) is caused by `tests/test_strategy_lab.py:179` calling
  `save_fingerprint()` against the real cache file during test runs — a
  pre-existing Phase 11 test-hygiene issue, unrelated to this phase, not
  fixed here (out of scope; flagged for a future phase).

## 1. Global hard constraints (apply to every component below)

1. No changes to signal weights/thresholds, entry/exit rules, CONTROL/A/B
   experiment definitions (`strategy_lab/phase10_experiments.py`), risk
   limits (`trading/config.py`), the production watchlist
   (`config/settings.py::WATCHLIST`), alert logic/cooldown (`alerts/engine.py`,
   `alerts/config.py`, `alerts/runner.py` — confirmed via repo-wide grep
   these contain no `date.today()`/`datetime.now()` calls at all, so this
   phase's date fix structurally cannot touch them), the Discord webhook
   config/client (`alerts/discord.py`), the prospective methodology
   (`strategy_lab/prospective.py`'s `METHODOLOGY_VERSION`,
   `build_todays_observation`), or either LaunchAgent/schedule
   (`deploy/*.plist.example`, `deploy/README.md`).
2. No order placement of any kind. No new module may call any Alpaca method
   beyond `get_account`, `get_all_positions`, `get_orders` (status filter
   only), `get_order_by_id` — same allowlist as Phase 12. No
   `submit_order`/`cancel_order_by_id`/`replace_order_by_id`/
   `close_position`/`close_all_positions` anywhere in this phase's code.
3. No Discord sends. No new module imports `alerts.discord`, `alerts.runner`,
   `alerts.run_alerts`, or references `DISCORD_WEBHOOK_URL`.
4. No new scheduled jobs: no `launchctl` call, no `.plist` write/edit, no
   cron edit, anywhere in this phase's code.
5. No strategy optimization: no component in this phase reads prospective
   outcomes to tune anything. New/extended modules are pure read+classify+report.
6. No automatic provider switching or data repair: `db.price_repository.SOURCE_PRIORITY`
   is read, never mutated; no `prices` row is ever updated/deleted by any
   function in this phase (existing Phase 12 test
   `test_check_watchlist_quality_never_writes_prices_table` must keep
   passing unmodified; this phase adds an equivalent test for the new module).
7. No overwriting immutable prospective observations
   (`research_prospective_observations`) or frozen research history
   (`data/research_cache/phase9_results.pkl`, `phase10_results.pkl`,
   `phase11_results.pkl`, `*_baseline.json`) — no function in this phase
   opens any of those paths for writing.
8. Every schema change is additive-only: new nullable columns via a guarded
   `ALTER TABLE ... ADD COLUMN` migration (never touching an existing
   `CREATE TABLE` string in `db/schema.py::ALL_STATEMENTS`), or entirely
   new tables with their own `ensure_schema()`, matching Phase 12's convention.

## 2. New/modified files — overview

```
ops/
  provider_reconciliation.py        # NEW — Components A + B classification engine
  data_quality.py                   # MODIFIED (additive) — provenance ledger + new
                                     #   fields on TickerQualityRow/DataQualityReport +
                                     #   provenance-aware rollup + logging improvement
  run_data_quality_check.py         # MODIFIED (additive) — calls record_source_provenance

dashboard/
  views/ops_overview.py             # MODIFIED (additive) — new expander section

automation/
  pipeline.py                       # MODIFIED (specific fix) — pass trading_date through
  recovery.py                       # MODIFIED (specific bug fix) —
                                     #   _todays_successful_run_exists rewritten

strategy_lab/
  research_automation.py            # MODIFIED (specific bug fix) —
                                     #   _todays_production_run_succeeded rewritten

db/
  schema.py                         # MODIFIED (additive migration) — automation_runs
                                     #   gains nullable trading_date column
  run_history_repository.py         # MODIFIED (additive) — start_run/record_skipped_run
                                     #   gain optional trading_date/started_at params;
                                     #   RUN_COLUMNS gains "trading_date"

tests/
  test_ops_provider_reconciliation.py   # NEW
  test_ops_data_quality_phase13.py      # NEW (existing test_ops_data_quality.py untouched)
  test_ops_date_reliability.py          # NEW
  test_ops_safety_phase13.py            # NEW
```

No file outside this list should appear in the implementation diff.
`db/schema.py`'s existing `CREATE_AUTOMATION_RUNS_TABLE` string is NOT
edited (a new migration function is added instead). `tests/test_ops_data_quality.py`,
`tests/test_automation.py`, `tests/test_phase11.py` are not edited beyond
what naturally starts passing once the production bug is fixed — no
existing assertion in those files needs to change.

## 3. Component A — Provider reconciliation + source-of-truth policy

### 3.1 `ops/provider_reconciliation.py` (new module)

```python
# Static, documented registry - NOT inferred per-row. Confirmed against
# actual ingestion code: ingestion/alpaca_source.py (no `adjustment` param
# -> Alpaca default RAW), strategy_lab/data.py (adjustment=Adjustment.ALL).
# ingestion/yfinance_source.py exists (auto_adjust=True -> would be ADJUSTED)
# but has never ingested a row into this DB - kept in the registry for when
# it does, not because it's currently active.
ADJUSTMENT_RAW = "RAW"
ADJUSTMENT_ADJUSTED = "ADJUSTED"
ADJUSTMENT_UNKNOWN = "UNKNOWN"

SOURCE_ADJUSTMENT_MODE = {
    "alpaca": ADJUSTMENT_RAW,
    "yfinance": ADJUSTMENT_ADJUSTED,
    "alpaca_adjusted": ADJUSTMENT_ADJUSTED,
}

# Documentation/reporting only - never consulted to pick a source at read
# time (db.price_repository / strategy_lab.data already do that, unmodified).
PRODUCTION_SOURCE_POLICY = "db.price_repository.resolve_source() — SOURCE_PRIORITY=['yfinance','alpaca'], single source per ticker, never blended"
RESEARCH_SOURCE_POLICY = "strategy_lab.data.RESEARCH_SOURCE ('alpaca_adjusted') — always explicit, never falls back to 'alpaca'/'yfinance'"

CLASS_MATCHED = "MATCHED"
CLASS_EXPECTED_ADJUSTMENT_DIFFERENCE = "EXPECTED_ADJUSTMENT_DIFFERENCE"
CLASS_STALE_SOURCE = "STALE_SOURCE"
CLASS_SESSION_ALIGNMENT_DIFFERENCE = "SESSION_ALIGNMENT_DIFFERENCE"
CLASS_MISSING_SOURCE = "MISSING_SOURCE"
CLASS_UNEXPLAINED_DIVERGENCE = "UNEXPLAINED_DIVERGENCE"

# Configurable tolerances - no exact float equality anywhere in this file.
CLOSE_MATCH_TOLERANCE_PCT = 0.0015          # 0.15%
VOLUME_MATCH_TOLERANCE_PCT = 0.02           # 2% - dividend-only adjustment
                                             # must leave volume unchanged;
                                             # this tolerance just absorbs
                                             # float/rounding noise, not a
                                             # real "adjustment" of volume
KNOWN_SPLIT_RATIOS = (2, 3, 4, 5, 7, 10, 20)  # confirmed real case in this
                                               # DB: NVDA 10:1 (June 2024,
                                               # ratio measured at 10.017);
                                               # GOOGL 20:1 (2022, cited in
                                               # strategy_lab/data.py docstring)
SPLIT_RATIO_TOLERANCE_PCT = 0.02            # 2% band around a known ratio (or its inverse)
STALE_SOURCE_SESSION_THRESHOLD = 5          # a source's OWN last_date more than this many
                                             # NYSE sessions behind "most recent completed
                                             # session" is STALE_SOURCE for that source

@dataclass
class DivergenceFinding:
    ticker: str
    date: str
    source_a: str
    source_b: str
    adjustment_mode_a: str
    adjustment_mode_b: str
    close_a: float
    close_b: float
    volume_a: int
    volume_b: int
    price_ratio: float           # close_a / close_b
    volume_ratio: Optional[float]  # volume_a / volume_b, None if volume_b == 0
    classification: str
    detail: str                  # human-readable, cites the specific signal that decided it

def classify_pair(
    ticker: str, date_str: str,
    source_a: str, close_a: float, volume_a: int, fetched_at_a: Optional[str],
    source_b: str, close_b: float, volume_b: int, fetched_at_b: Optional[str],
    is_latest_date_for_source_a: bool = False,
    is_latest_date_for_source_b: bool = False,
    neighbor_close_b_prev: Optional[float] = None,
    neighbor_close_b_next: Optional[float] = None,
) -> DivergenceFinding:
    """Pure, deterministic classification of ONE (ticker,date) pair already
    known to be present under two sources (MISSING_SOURCE is handled by the
    caller before this is invoked, not here).

    Order of checks (first match wins) — refined against 3 confirmed real
    patterns in this DB (see spec §0.1): a pure price-ratio check alone
    would misclassify a stale/incomplete intraday bar (real pattern (c),
    price AND volume both off with no clean ratio) as a normal adjustment
    difference, since its price delta alone looks adjustment-sized. Volume
    consistency and same-session-fetch-time are checked BEFORE accepting an
    adjustment-mode mismatch as EXPECTED_ADJUSTMENT_DIFFERENCE:

      1. close matches within CLOSE_MATCH_TOLERANCE_PCT AND volume matches
         within VOLUME_MATCH_TOLERANCE_PCT -> MATCHED

      2. price_ratio (or 1/price_ratio) within SPLIT_RATIO_TOLERANCE_PCT of
         a KNOWN_SPLIT_RATIOS value, AND volume_ratio (or 1/volume_ratio) is
         within SPLIT_RATIO_TOLERANCE_PCT of THAT SAME ratio (confirmed
         signature of a real split — pattern (b): both price and volume
         scale by the same factor) -> EXPECTED_ADJUSTMENT_DIFFERENCE,
         detail names the matched ratio

      3. SOURCE_ADJUSTMENT_MODE.get(source_a) != SOURCE_ADJUSTMENT_MODE.get(source_b)
         (treating a missing/UNKNOWN tag as never equal to anything,
         including another UNKNOWN) AND volume matches within
         VOLUME_MATCH_TOLERANCE_PCT (confirmed signature of pattern (a) —
         genuine dividend-only adjustment never moves volume)
         -> EXPECTED_ADJUSTMENT_DIFFERENCE, detail names the adjustment-mode pair

      4. (is_latest_date_for_source_a or is_latest_date_for_source_b) AND
         the source whose latest date this is has fetched_at on the SAME
         calendar date as `date_str` at a wall-clock time before the NYSE
         session's typical close (America/New_York 16:00) — i.e. fetched
         intraday, likely capturing a preliminary/incomplete bar that was
         never refreshed after session close (confirmed real pattern (c))
         -> STALE_SOURCE, detail states which source/date and the fetch
         timestamp that triggered it

      5. neighbor_close_b_prev/next provided and close_a matches either
         within CLOSE_MATCH_TOLERANCE_PCT -> SESSION_ALIGNMENT_DIFFERENCE

      6. else -> UNEXPLAINED_DIVERGENCE
    """

def find_divergences_for_ticker(conn, ticker: str) -> List[DivergenceFinding]:
    """For every (ticker,date) present under >1 source, fetch fetched_at and
    neighbor rows (date-1/date+1 for source_b) via indexed queries, compute
    is_latest_date_for_source_x by comparing against MAX(date) per source,
    then classify_pair(...) each. Read-only SELECTs only."""

@dataclass
class SourceFreshness:
    ticker: str
    source: str
    adjustment_mode: str
    is_authoritative_production: bool   # True iff source == resolve_source(conn, ticker)
    last_date: Optional[str]
    sessions_behind: Optional[int]      # None if no rows at all
    stale: bool                         # sessions_behind > STALE_SOURCE_SESSION_THRESHOLD

def check_all_sources_freshness(conn, ticker: str, today: Optional[date] = None) -> List[SourceFreshness]:
    """Unlike ops.data_quality.check_ticker_quality (which only evaluates
    the ONE resolved/authoritative source), this evaluates EVERY distinct
    source present for the ticker independently, so a stale secondary is
    visible but tagged is_authoritative_production=False - the rollup
    (§4.2) uses this flag to never let a stale non-authoritative source
    degrade overall health the way a stale authoritative source must."""

@dataclass
class ProviderReconciliationSummary:
    ticker: str
    findings: List[DivergenceFinding]
    source_freshness: List[SourceFreshness]
    unexplained_count: int              # len([f for f in findings if f.classification == CLASS_UNEXPLAINED_DIVERGENCE])
    stale_source_count: int             # len([f for f in findings if f.classification == CLASS_STALE_SOURCE])
    authoritative_stale: bool           # any(sf.stale for sf in source_freshness if sf.is_authoritative_production)

def build_ticker_summary(conn, ticker: str, today: Optional[date] = None) -> ProviderReconciliationSummary
```

### 3.2 Edge cases

- Ticker with only one source present entirely → `find_divergences_for_ticker`
  returns `[]`; `check_all_sources_freshness` returns a single-element list.
- `close_b == 0` or `volume_b == 0` → never raises `ZeroDivisionError`;
  `price_ratio`/`volume_ratio` set to `None` when the denominator is zero,
  `classify_pair` treats a `None` ratio as failing every ratio-based check
  and falls through toward `UNEXPLAINED_DIVERGENCE`.
- A 4th, future source tag not in `SOURCE_ADJUSTMENT_MODE` → treated as
  `ADJUSTMENT_UNKNOWN`; two `UNKNOWN` sources are never treated as
  "same adjustment mode" just because both are unknown (check #3 requires
  the modes to differ, and `UNKNOWN != UNKNOWN` is deliberately `True` here
  — implemented via a sentinel comparison, not literal string equality, so
  it never falls through silently).
- `neighbor_close_b_prev`/`_next` unavailable (start/end of history) →
  `None`, check #5 is skipped.
- `resolve_source(conn, ticker)` returns `None` (zero rows at all) →
  `check_all_sources_freshness` returns `[]`, `authoritative_stale=False`
  (Phase 12's `STATUS_MISSING` already covers "no data at all").
- Two sources both flagged `is_latest_date_for_source_x=True` on the SAME
  date (e.g. both happened to be fetched same-day) → check #4 still
  evaluates each source's own `fetched_at` independently; if BOTH were
  fetched intraday, classify based on whichever fetch is earlier relative
  to close (both fetched pre-close is still `STALE_SOURCE`, not `MATCHED`,
  since neither is a finalized bar).

### 3.3 First implementation step (real-data validation, already partially done)

The orchestrating session already ran a real, read-only spot-check against
the live DB confirming patterns (a)/(b)/(c) in §0.1 — the coder does not
need to repeat that discovery, but MUST re-run `find_divergences_for_ticker`
for AAPL/MSFT/NVDA against the real local DB once implemented and confirm
the classifier reproduces these three exact classifications (dividend-only
→ `EXPECTED_ADJUSTMENT_DIFFERENCE`, NVDA June-2024 block →
`EXPECTED_ADJUSTMENT_DIFFERENCE`, the single latest `alpaca_adjusted` date
per ticker → `STALE_SOURCE`) before the reviewer signs off. This is a
verification step, not committed code.

## 4. Component B — Data integrity + provenance (extends `ops/data_quality.py`)

**All changes below are additive.** `TickerQualityRow`, `DataQualityReport`,
`check_ticker_quality`, `check_watchlist_quality`, `classify_overall`, and
`_count_cross_source_conflicts` keep their EXACT current signatures/bodies —
`tests/test_ops_data_quality.py` must pass completely unmodified after this phase.

### 4.1 New dataclass fields (appended, all with defaults)

```python
@dataclass
class TickerQualityRow:
    # ... all existing fields, unchanged ...
    provider_findings: List[dict] = field(default_factory=list)        # [DivergenceFinding asdict, ...]
    unexplained_divergence_count: int = 0
    stale_source_count: int = 0
    authoritative_source_stale: bool = False   # redundant with status==STATUS_STALE today,
                                                # but explicit for the new rollup's clarity
    non_authoritative_source_notes: List[str] = field(default_factory=list)  # e.g.
        # ["alpaca_adjusted: latest row (2026-08-12) appears to be a stale/incomplete
        #   intraday snapshot fetched 2026-08-12T18:54:58Z, before session close"]

@dataclass
class DataQualityReport:
    # ... all existing fields, unchanged ...
    overall_status_provenance_aware: str = OVERALL_HEALTHY
```

### 4.2 New rollup function (additive, does not replace `classify_overall`)

```python
def classify_overall_provenance_aware(rows: List[TickerQualityRow]) -> str:
    """Same OVERALL_* vocabulary as classify_overall, but:
      - FAILED: any ticker STATUS_MISSING, or authoritative_source_stale True
        for any ticker.
      - STALE: no MISSING/authoritative-stale, but >=1 ticker STATUS_STALE
        (existing field, kept as fallback signal).
      - DEGRADED: all tickers FRESH and no authoritative staleness, but >=1
        ticker has invalid_ohlcv_rows > 0 OR unexplained_divergence_count > 0
        OR stale_source_count > 0 (non-authoritative) OR non-empty
        missing_trading_days. Critically: MATCHED / EXPECTED_ADJUSTMENT_DIFFERENCE
        / SESSION_ALIGNMENT_DIFFERENCE findings never degrade this on their
        own; only UNEXPLAINED_DIVERGENCE and STALE_SOURCE do (both indicate
        something an operator should look at, even if not authoritative-breaking).
      - HEALTHY: otherwise.
    This is the rollup the dashboard (§6) should treat as authoritative; the
    OLD classify_overall/overall_status fields are kept for Phase 12
    backward compatibility, not removed.
    """
```

### 4.3 `check_ticker_quality` extension (additive body changes only)

After the existing logic (unchanged), add a call to
`ops.provider_reconciliation.build_ticker_summary(conn, ticker, today=today)`
and populate the new `TickerQualityRow` fields from its `findings`/
`source_freshness`/`unexplained_count`/`stale_source_count`/
`authoritative_stale`. The existing `status`/`duplicate_rows`/etc. fields
are computed exactly as before, byte-for-byte.

### 4.4 `check_watchlist_quality` extension

After computing `rows` and the existing `overall = classify_overall(rows)`
(unchanged), add `overall_provenance_aware = classify_overall_provenance_aware(rows)`
and populate `DataQualityReport.overall_status_provenance_aware`.

**Logging improvement:** the existing `except Exception as e: logger.error("data
quality check failed: %s", e)` is extended to `logger.error("data quality
check failed for tickers=%s: %s", tickers, e)` — names which tickers were
being checked; still never dumps a traceback to any caller (still catches,
still returns `OVERALL_FAILED`, never re-raises).

### 4.5 Provenance ledger — new table, own `ensure_schema` in `ops/data_quality.py`

```python
PROVENANCE_TABLE_NAME = "ops_source_provenance"

CREATE TABLE IF NOT EXISTS ops_source_provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id INTEGER NOT NULL,
    checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    ticker TEXT NOT NULL,
    source TEXT NOT NULL,
    adjustment_mode TEXT NOT NULL,
    min_date TEXT,
    max_date TEXT,
    row_count INTEGER NOT NULL,
    fingerprint TEXT NOT NULL
);

def ensure_provenance_schema(conn) -> None
def record_source_provenance(conn, check_id: int, tickers: List[str]) -> int
    """For every (ticker, source) pair present in `prices` for `tickers`,
    INSERT one row: adjustment_mode from provider_reconciliation.SOURCE_ADJUSTMENT_MODE,
    min/max(date), COUNT(*), and fingerprint = sha256 of the ordered
    (date,open,high,low,close,volume) tuples for that (ticker,source) -
    detects silent same-source drift across checks over time, NOT
    cross-source difference (that's provider_findings, §4.1). Append-only.
    Returns rows inserted."""
def load_provenance_history(conn, ticker: str, source: str, limit: int = 30) -> pd.DataFrame
```

`ops/run_data_quality_check.py` calls `record_source_provenance(conn,
check_id, tickers)` immediately after `record_check(conn, report)` (using
its returned `id` as `check_id`), additively.

### 4.6 Edge cases

- A ticker present under only the authoritative source → all new fields
  empty/zero/False; `overall_status_provenance_aware` unaffected.
- `resolve_source` changes which source is authoritative between two
  consecutive checks → each check independently computed; no state carried
  between runs beyond the append-only provenance ledger.
- True corporate-actions/dividends table doesn't exist in this repo and is
  NOT invented here (out of scope — would need a new ingestion source);
  `KNOWN_SPLIT_RATIOS` ratio-based detection is the generic substitute,
  documented as an approximation.
- `record_source_provenance` called with a ticker that has zero `prices`
  rows → skipped for that ticker, never raises.

## 5. Component C — Deterministic date/time reliability

### 5.1 Schema fix — `db/schema.py` (additive migration, mirrors `_migrate_pre_phase5_alerts_table`)

```python
def _migrate_add_trading_date_to_automation_runs(conn):
    """Additive: automation_runs gets a nullable trading_date column so
    'was there a successful run FOR trading day X' can be an exact match
    instead of string-matching a UTC timestamp against a local calendar
    date. Pre-existing rows get trading_date=NULL - never backfilled/guessed."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(automation_runs)").fetchall()}
    if "trading_date" not in cols:
        conn.execute("ALTER TABLE automation_runs ADD COLUMN trading_date TEXT")
        conn.commit()
```

Called from `init_db()` alongside the existing
`_migrate_pre_phase5_alerts_table(conn)` call. `CREATE_AUTOMATION_RUNS_TABLE`
itself is NOT edited.

### 5.2 `db/run_history_repository.py` (additive params, backward compatible)

```python
RUN_COLUMNS = [
    "id", "started_at", "finished_at", "status", "send_mode",
    "tickers_attempted", "tickers_updated", "tickers_failed",
    "alerts_generated", "error_summary", "trading_date",   # appended
]

def start_run(conn, send_mode: str, trading_date: Optional[date] = None,
              started_at: Optional[str] = None) -> int:
    """AMENDED DURING IMPLEMENTATION (see §0.4 below): when `started_at` is
    NOT overridden (the normal/real-time call path), `trading_date` defaults
    to today's real local calendar date rather than NULL, so any direct
    caller of start_run() — not just automation/pipeline.py — gets correct
    exact-match trading_date semantics without threading the value through
    manually. `started_at` override exists ONLY to make deterministic tests
    possible without the real wall clock; when it IS passed, `trading_date`
    is stored exactly as given (including None), so tests can still
    construct legacy pre-migration-shaped rows with trading_date IS NULL."""

def record_skipped_run(conn, send_mode: str, status: str, reason: str,
                        trading_date: Optional[date] = None) -> int
```

`finish_run` is unchanged.

### 5.3 `automation/pipeline.py` (specific fix — 2 call sites pass `trading_date=check_date`)

`check_date` is the existing local variable (`check_date = today or
date.today()`) — the NYSE trading date this run pertains to. Nothing else
in this file changes.

### 5.4 `automation/recovery.py::_todays_successful_run_exists` (specific bug fix)

```python
def _todays_successful_run_exists(conn, today: date) -> bool:
    history = load_run_history(conn, limit=50)
    if history.empty:
        return False
    if "trading_date" in history.columns:
        exact = history[history["trading_date"] == today.isoformat()]
        if not exact.empty:
            return bool((exact["status"] == STATUS_SUCCESS).any())
    # Legacy fallback for rows recorded before this migration (trading_date
    # IS NULL) - the OLD, fragile UTC-vs-local-date string-prefix comparison,
    # kept ONLY so pre-migration history isn't silently invisible. New rows
    # always populate trading_date and never reach this branch.
    todays = history[history["started_at"].astype(str).str.startswith(today.isoformat())]
    return bool((todays["status"] == STATUS_SUCCESS).any())
```

### 5.5 `strategy_lab/research_automation.py::_todays_production_run_succeeded` (identical fix)

Same rewrite pattern as §5.4 — reads the same `automation_runs` table via
`load_run_history`.

### 5.6 Calendar/timezone audit — files/functions reviewed, findings

| File:function | Finding |
|---|---|
| `automation/pipeline.py` (`check_date = today or date.today()`) | OK. Caller-injectable. No change beyond §5.3. |
| `automation/run_daily.py` (`date.today()`) | OK, CLI entry point matches LaunchAgent's local-TZ trigger design. No change. |
| `automation/recovery.py::_todays_successful_run_exists` | **Bug, fixed** — §5.4. |
| `strategy_lab/research_automation.py::_todays_production_run_succeeded` | **Bug, fixed** — §5.5. |
| `strategy_lab/run_research_job.py` (`date.today()`) | OK, same local-TZ assumption, scheduled 16:45 local. No change. |
| `strategy_lab/outcome_maturation.py` + `automation/trading_calendar.py::trading_sessions_elapsed` | No bug found — `.date()` truncation of `pandas_market_calendars`'s `valid_days` is DST-safe (session identity is a calendar date regardless of intraday UTC offset). No existing test locks this in across a DST boundary — add regression coverage (§8.3). |
| `ops/data_quality.py` (`today = today or date.today()` + `_most_recent_completed_session`) | OK, already fully overridable/tested (Phase 12's Saturday-anchor tests). No change. |
| `strategy_lab/prospective.py::build_todays_observation` | OK, `as_of_date` always caller-supplied, no internal `date.today()`. No change. |
| `alerts/runner.py`, `alerts/engine.py`, `alerts/config.py` | Confirmed via repo-wide grep: NO `date.today()`/`datetime.now()` calls at all. Structurally cannot be affected. Out of scope per §1.1 regardless. |
| `db/run_history_repository.py`, `db/schema.py` | **Fixed** — §5.1/§5.2. |

### 0.4 Implementation-time amendment (discovered during coding, real evidence)

The coder implemented §5.2's original literal spec (trading_date defaults
to `None` when not passed) and re-ran the 3 target tests against the real
DB: `tests/test_automation.py::test_recovery_is_idempotent_and_does_nothing_if_success_already_exists`
started passing (it goes through `automation/pipeline.py`, which explicitly
passes `trading_date=check_date`), but
`tests/test_phase11.py::test_research_job_creates_observations_when_production_run_succeeded`
and `tests/test_phase11.py::test_research_job_recovery_is_idempotent_on_duplicate_call`
still failed intermittently by real wall-clock time — because both tests
call `db.run_history_repository.start_run(conn, "real")` **directly**,
bypassing `automation/pipeline.py` entirely, so `trading_date` stayed NULL
and both tests fell through to the legacy UTC-string-prefix fallback (§5.4),
reproducing the exact original bug.

**Fix applied (verified against the real failing tests, confirmed passing
in the actual UTC/local-boundary window that originally reproduced the
bug):** `start_run`'s default was changed so that when `started_at` is NOT
overridden, `trading_date` defaults to `date.today()` (today's real local
calendar date) rather than `None`. This makes ANY direct caller of
`start_run()` — not only `automation/pipeline.py` — get correct
exact-match `trading_date` semantics for free, which is what actually
closes the gap in both `test_phase11.py` tests without editing either test
file (in scope per §1: no existing assertion in those files changes). The
`started_at`-override test path (used only by deterministic tests
simulating a specific historical row) still allows an explicit
`trading_date=None` to be passed through unchanged, so a test can still
construct a legacy pre-migration-shaped NULL-trading_date row on purpose
(see the renamed test in §8.3 below).

**This supersedes §5.2's original code listing above and §8.3's
`test_start_run_trading_date_defaults_to_none_when_not_passed` test
name/assertion, which is now `test_start_run_trading_date_defaults_to_today_when_not_passed`**
(defaults to today's real date, not None, on the normal call path; defaults
to None only when `started_at` is also explicitly overridden). Verified:
all 3 originally-failing tests now pass deterministically, confirmed via
direct pytest run, and the full regression suite is 329/329 passing.

## 6. Component D — Operations/dashboard diagnostics

### 6.1 `dashboard/views/ops_overview.py` (additive)

No change needed to `dashboard/data.py::get_data_quality_report()` — already
does `dataclasses.asdict(report)`, so new §4.1 fields flow through automatically.

Add, inside/immediately after `_render_system_health`:

```python
def _render_provider_reconciliation(dq: dict):
    with st.expander("Provider reconciliation & provenance detail (Phase 13)"):
        st.caption(
            "How production ('alpaca', raw, single-source-per-ticker) and "
            "research ('alpaca_adjusted', split/dividend-adjusted) price data "
            "compare. EXPECTED_ADJUSTMENT_DIFFERENCE is normal and does not "
            "indicate a problem. STALE_SOURCE flags a non-authoritative "
            "source's most recent row as a likely incomplete/preliminary "
            "snapshot — informational, never auto-repaired."
        )
        overall_v2 = dq.get("overall_status_provenance_aware")
        st.metric("Provenance-aware overall status", overall_v2)
        rows = []
        for t in dq["tickers"]:
            for f in t.get("provider_findings", []):
                rows.append({
                    "Ticker": f["ticker"], "Date": f["date"],
                    "Source A": f["source_a"], "Source B": f["source_b"],
                    "Classification": f["classification"],
                    "Price ratio": f["price_ratio"], "Volume ratio": f["volume_ratio"],
                    "Detail": f["detail"],
                })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.caption("No cross-source findings.")
        notes = [n for t in dq["tickers"] for n in t.get("non_authoritative_source_notes", [])]
        if notes:
            st.caption("Non-authoritative source notes: " + "; ".join(notes))
```

Called from within `_render_system_health`'s existing `try/except` around
`get_data_quality_report()` — no new exception path.

### 6.2 Logging call sites — specific, named

- `ops/data_quality.py::check_watchlist_quality` except-block: extended to
  include `tickers` (§4.4).
- `ops/provider_reconciliation.py::build_ticker_summary` (new): on
  completion, `logger.info("provider reconciliation: %s findings=%d unexplained=%d stale=%d authoritative_stale=%s", ...)`.
  For each `CLASS_UNEXPLAINED_DIVERGENCE` or `CLASS_STALE_SOURCE` finding:
  `logger.warning("provider reconciliation: %s %s %s source_a=%s(%s) source_b=%s(%s) price_ratio=%s volume_ratio=%s", classification, ticker, date, source_a, mode_a, source_b, mode_b, price_ratio, volume_ratio)`
  — always names ticker/date/both sources/both adjustment modes/both
  ratios; never logs a raw exception object or stack trace.
- No component in this phase ever logs `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
  or `DISCORD_WEBHOOK_URL` — trivially true (pure DB reads); enforced by an
  explicit test.

### 6.3 Edge cases

- `dq.get("overall_status_provenance_aware")` missing (stale Streamlit
  cache from before this phase) → `.get(...)` returns `None`, `st.metric`
  renders gracefully.
- Zero findings across the whole watchlist → "No cross-source findings"
  caption, not an empty/broken table.

## 7. Open questions (explicit — do not guess further)

1. `CLOSE_MATCH_TOLERANCE_PCT=0.0015`, `VOLUME_MATCH_TOLERANCE_PCT=0.02`,
   `STALE_SOURCE_SESSION_THRESHOLD=5` are placeholders — reasonable given
   the confirmed real data (dividend deltas measured ~0.09–0.6%, volume
   otherwise byte-identical when genuinely matched) but not yet
   operator-confirmed as final policy.
2. `KNOWN_SPLIT_RATIOS`/`SPLIT_RATIO_TOLERANCE_PCT` is a generic heuristic
   in the absence of a real corporate-actions data source — confirmed none
   exists in this repo. Acceptable as an approximation; a future phase
   could ingest real corporate-action data instead.
3. The intraday-fetch STALE_SOURCE heuristic (§3.1 check #4) assumes NYSE
   close ≈ 16:00 America/New_York and treats any same-calendar-day fetch
   before that as "possibly incomplete." This is a heuristic, not a
   guarantee (Alpaca's intraday bar for the current session could
   theoretically already be final moments before close) — acceptable given
   it only produces an informational `STALE_SOURCE` classification, never
   blocks anything or triggers a repair.
4. No open question on live/real-money — nothing in this spec touches live
   trading; §1.2 restates Phase 12's exact read-only Alpaca allowlist.

## 8. Testing checklist

### 8.1 `tests/test_ops_provider_reconciliation.py`

- `test_matched_within_tolerance`
- `test_expected_adjustment_difference_dividend_only_price_diff_volume_identical`
  (reproduces confirmed pattern (a): small price delta, byte-identical volume)
- `test_expected_adjustment_difference_split_price_and_volume_both_scale_by_same_ratio`
  (reproduces confirmed pattern (b): NVDA-shaped, ratio ≈10 on both price and volume)
- `test_stale_source_when_price_close_but_volume_diverges_and_fetched_intraday`
  (reproduces confirmed pattern (c): small price ratio, large unexplained
  volume ratio, `fetched_at` same-day pre-close on the source's latest row
  — must classify `STALE_SOURCE`, NOT `EXPECTED_ADJUSTMENT_DIFFERENCE`)
- `test_unexplained_divergence_when_no_explanation_matches_and_not_latest_date`
  (large price delta, not the source's latest date, no split ratio match)
- `test_session_alignment_difference_matches_neighbor_date`
- `test_zero_close_or_volume_never_raises_zerodivisionerror`
- `test_unknown_source_tag_never_silently_matched`
- `test_check_all_sources_freshness_flags_non_authoritative_stale_without_flagging_authoritative`
- `test_check_all_sources_freshness_authoritative_stale_true_when_resolved_source_itself_stale`
- `test_find_divergences_for_ticker_read_only` (row-count diff on `prices` == 0)
- `test_provider_reconciliation_never_imports_ingestion` (AST-scan)
- `test_real_aapl_msft_nvda_pattern_reproduction` (optional but recommended:
  seed rows with the EXACT confirmed values from §0.1 — AAPL 2026-08-07
  313.33/313.06 same volume; NVDA 2024-06-07 1208.88/120.68 with 10x volume
  scale; AAPL 2026-08-12 302.25/301.52 with divergent volume 41873071/25652286
  and `fetched_at`='2026-08-12 18:54:58' — assert the three expected
  classifications exactly)

### 8.2 `tests/test_ops_data_quality_phase13.py` (existing `test_ops_data_quality.py` untouched)

- `test_existing_phase12_fields_and_behavior_unchanged`
- `test_new_fields_default_safely_on_direct_construction`
- `test_provenance_aware_rollup_ignores_expected_adjustment_difference`
- `test_provenance_aware_rollup_degraded_by_stale_source_finding`
- `test_provenance_aware_rollup_failed_when_authoritative_source_stale`
- `test_provenance_aware_rollup_not_degraded_by_stale_non_authoritative_freshness_alone`
- `test_record_source_provenance_inserts_one_row_per_ticker_source_pair`
- `test_record_source_provenance_fingerprint_changes_when_underlying_rows_change`
- `test_record_source_provenance_never_writes_prices_table`
- `test_check_watchlist_quality_still_never_writes_prices_table_with_new_fields`

### 8.3 `tests/test_ops_date_reliability.py`

Deterministic — no test in this file calls `date.today()` or depends on the real wall clock.

- `test_todays_successful_run_exists_true_via_exact_trading_date_match`
- `test_todays_successful_run_exists_false_when_trading_date_differs`
- `test_todays_successful_run_exists_legacy_fallback_for_null_trading_date`
  (directly INSERT a raw row with `trading_date=NULL` and an explicit
  UTC-next-day `started_at`, confirm the legacy fallback path still finds it)
- `test_research_job_gate_uses_exact_trading_date_not_utc_string_match`
  (same shape, against `_todays_production_run_succeeded`)
- `test_start_run_trading_date_defaults_to_today_when_not_passed` (normal call
  path with no `started_at` override — defaults to real `date.today()`, per §0.4)
- `test_start_run_trading_date_stays_none_when_started_at_override_and_no_trading_date_passed`
  (the deterministic-test path — explicit `started_at` with no `trading_date`
  still stores NULL, so legacy-shaped rows remain constructible, per §0.4)
- `test_record_skipped_run_accepts_trading_date`
- `test_pipeline_persists_trading_date_matching_check_date`
- `test_trading_sessions_elapsed_across_dst_spring_forward_2026` (explicit
  dates spanning the March 2026 US DST transition)
- `test_trading_sessions_elapsed_across_dst_fall_back_2026` (November 2026)
- `test_trading_sessions_between_weekend_and_holiday_boundaries` (reuse the
  existing holiday table from `tests/test_automation.py`, don't duplicate)
- `test_outcome_maturation_deterministic_across_explicit_dates_not_wall_clock`

### 8.4 `tests/test_ops_safety_phase13.py`

- `test_provider_reconciliation_module_never_imports_order_execution_or_alerting`
- `test_provider_reconciliation_never_calls_alpaca_mutating_endpoints`
- `test_phase13_changes_never_write_to_prices_alert_state_paper_orders_or_prospective_tables`
  (seed DB, run `check_watchlist_quality`, `build_ticker_summary`,
  `record_source_provenance`, `run_pipeline` with mocked ingest; assert
  `prices`/`alert_state`/`alerts`/`paper_orders`/`research_prospective_*`
  row counts unchanged; `automation_runs`/`research_run_history` may grow
  via `run_pipeline`/`run_research_job` themselves)
- `test_no_credentials_logged_by_phase13_modules`
- `test_migration_add_trading_date_column_idempotent`
- `test_migration_never_alters_existing_automation_runs_rows`
- `test_phase13_never_edits_deploy_or_launchagent_files`

### 8.5 Manual/integration verification before moving on from Phase 13

1. `pytest tests/` — full suite green, including the 3 previously-failing
   tests now passing deterministically, plus all new files.
2. `python -m ops.run_data_quality_check` against the real local DB —
   confirm it exits 0, confirms the 3 real classifications from §0.1
   reproduce, confirms `ops_source_provenance` gets rows, confirms no
   protected table's row count/values change unexpectedly.
3. `streamlit run dashboard/app.py` — load the Operations page, expand the
   new "Provider reconciliation & provenance detail" section, confirm no
   exception and confirm it shows the real AAPL/MSFT/NVDA findings
   correctly classified (2 as `EXPECTED_ADJUSTMENT_DIFFERENCE`, 1 as
   `STALE_SOURCE` per ticker, per §0.1).
4. `git diff` for this phase touches only the files listed in §2.
