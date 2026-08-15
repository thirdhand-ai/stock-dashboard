# Phase 16: Regime Capture Fix, Legacy Regime Audit, Event/Outcome
Provenance, and Monitoring

Status: PLANNED. Builds on Phase 9-15 (repo HEAD 8dc5e85). No
implementation code in this document — signatures/pseudocode below are
illustrative specification, matching the convention already used in
docs/specs/phase15.md §1-§4.

## 0. Grounding: root causes confirmed independently

### 0.1 Area A root cause (verified, not just trusted)

`strategy_lab/prospective.py::build_todays_observation` (`:141-181`):
```python
regime_series = compute_historical_regime_series(conn)
regime_label = None
if not regime_series.empty:
    regime_label = regime_series.iloc[-1]["label"] if as_of_date is None else (
        regime_series.set_index("date")["label"].get(as_of_date)
    )
```
`strategy_lab/research_automation.py::run_research_job` (`:251-253`) always
calls this with `as_of_date=today.isoformat()` — `today` being the
observation's own trading date. `strategy_lab/regime_history.py::
compute_historical_regime_series` (`:21-59`) derives `regime_series` purely
from `load_price_history(conn, config.primary_benchmark="SPY",
source=RESEARCH_SOURCE="alpaca_adjusted")` and drops every row that doesn't
have a full trailing `sma_long_days=200`-row window
(`.dropna(subset=["sma_long"])`, `:59`). The research LaunchAgent
(`deploy/com.stockdashboard.researchjob.plist.example:53-57`) fires at
**16:45 America/New_York, Mon-Fri** — after the 16:00 NYSE close but with
no guarantee that SAME session's SPY `alpaca_adjusted` bar has been
fetched/stored yet (nothing in `run_research_job`'s daily path calls
`strategy_lab.data.fetch_and_cache_universe` — that function is only
invoked by the separate, manually-run `run_study`/`run_phase10_study`/
`run_phase11_study` CLIs). A direct query of the live DB
(`SELECT source, MAX(date) FROM prices WHERE ticker='SPY'`) confirms both
`alpaca` and `alpaca_adjusted` cap out at `2026-08-12` as of `2026-08-14`.

**Confirmed mechanism:** `regime_series.set_index("date")["label"].get(as_of_date)`
is an EXACT-match dict lookup. Since `regime_series`'s latest available
date is structurally always `<= as_of_date - 1` (or further behind) at
16:45 ET on `as_of_date` itself, this `.get()` call returns `None` on
every single run, permanently — not a backlog, an ongoing daily miss. The
one exception, `id=1` (AAPL, `observation_date=2026-08-12`,
`created_at=2026-08-12T21:06:40Z`), was created out-of-band later that
evening (not by the 16:45 scheduled job) — by 21:06 UTC (~17:06 ET) it's
plausible 2026-08-12's own bar existed, which is consistent with, not
proof beyond, this mechanism; the exact-match structural flaw is the
actual root cause regardless.

**Confirmed NOT a look-ahead bug** (`strategy_lab/regime_history.py`'s
own docstring, `:1-11`, and `research/config.py::RegimeConfig`, `:118-146`,
whose 4 labels are trailing-indicator-only by construction): the fix must
preserve "trailing information available as of that date" — using the most
recent **available** (already-stored) SPY bar at or before the observation
date is still genuinely point-in-time (it is what a real-time observer
would have known at that moment), never inventing a same-day bar that
doesn't exist yet.

### 0.2 Area C finding (contradicts the task's working hypothesis — verified)

`strategy_lab/prospective_events.py::ALL_EVENT_TYPES` (`:33-36`) ALREADY
contains `EVENT_CONTROL_ENTRY="control_entry"`,
`EVENT_EXPERIMENT_A_ENTRY="experiment_a_entry"`,
`EVENT_EXPERIMENT_B_ENTRY="experiment_b_entry"` as three distinct,
independently-recorded event types (`_entry_transition_events`, `:80-98`,
loops over exactly these three signal fields). **There is no entry-side
attribution gap** — `event_type` alone already unambiguously identifies
CONTROL vs Experiment A vs Experiment B for every entry event, with no
need to resolve `config_fingerprint`'s many-to-one ambiguity (Phase 15
§0.1) for this purpose.

**The real, confirmed gap:** `ALL_EVENT_TYPES` contains **zero** event
types with "exit" in the name. `strategy_lab/phase10_experiments.py`
(`:39-57`) defines Experiment B's *sole* differentiator from Experiment A
as `exit_on_regime_loss=True` (a different EXIT rule) — but
`research_prospective_events` only ever records entry/trend/momentum/
volume-crossing events, never an exit decision for any variant. Experiment
B's differentiating behavior is therefore structurally invisible in event
data. This is a coverage gap, not an ambiguity bug, and per the hard
constraints below it is **flagged as Open Question #2, not built** in
Phase 16.

Also confirmed: `experiment_a_entry_signal` and `experiment_b_entry_signal`
are computed identically in `build_todays_observation` (`:174-175`,
`entry_qualifies and is_bullish` for both) — `EVENT_EXPERIMENT_A_ENTRY`
and `EVENT_EXPERIMENT_B_ENTRY` therefore always co-occur on the same
`(ticker, observation_date)`. This is by design (documented in
`phase10_experiments.py`, "differ only on exit"), not an anomaly to fix.

### 0.3 Area D finding (verified)

`strategy_lab/outcome_maturation.py::_compute_one` (`:82-148`) sets
`source = prov_source = obs_row["source"] if ... else None` (`:91`), then
`load_price_history(conn, ticker, source=source)` (`:111`).
`db/price_repository.py::load_price_history` (`:10-30`): when `source is
None`, it calls `resolve_source(conn, ticker)` (`:49-64`), which picks
"the source with the most stored rows for this ticker, preferring
`SOURCE_PRIORITY=["yfinance","alpaca"]`" — i.e. the **production** source,
never `RESEARCH_SOURCE`. For the real `id=1` AAPL row (`source=NULL`,
confirmed live), this fallback is silent: nothing in `_compute_one` or the
persisted `MaturationOutcome` records *which* source was actually used to
price that outcome, or that a fallback occurred at all. `_persist`'s
`ON CONFLICT DO UPDATE` (`:163-189`) re-derives `source`/
`methodology_version`/`config_fingerprint` from the observation row on
every call — confirmed idempotent, and confirmed that a row **with** a
real `source` can never reach the fallback branch (`load_price_history`
only calls `resolve_source` when its `source` arg is falsy, and
`_compute_one` only passes a falsy `source` when `prov_source` is falsy —
structurally impossible for a provenanced row to fall back).

## 1. Area A — Regime capture fix

### 1.1 `strategy_lab/regime_history.py` (modified — additive function only)

Add one new function. **Do not modify `compute_historical_regime_series`**
— `strategy_lab/report.py:157-158`, `strategy_lab/portfolio_simulator.py`
(via `report_phase10.py`), and `strategy_lab/report.py:108-109` (Phase 9/10
frozen-artifact generators) all consume its exact return shape via
`.set_index("date")["label"]` lookups against dates that are always
already present in the series (retrospective backtests iterate historical
price dates that, by construction, are already in `regime_series`) — those
call sites are untouched and must remain byte-identical in behavior.

```python
def regime_label_as_of(regime_series: pd.DataFrame, as_of_date: Optional[str] = None) -> Optional[str]:
    """Point-in-time regime lookup used ONLY by
    strategy_lab.prospective.build_todays_observation (never by report.py/
    portfolio_simulator.py/report_phase10.py, which keep their own exact-
    match usage unchanged).

    as_of_date=None: returns the single most recent available label
    (regime_series.iloc[-1]["label"]) — equivalent to today's existing
    behavior for the ad-hoc/no-date CLI path
    (strategy_lab/run_prospective_observation.py:28 calls
    build_todays_observation(conn, ticker) with no as_of_date).

    as_of_date=a date string: returns the label of the most recent row in
    regime_series with date <= as_of_date (regime_series is guaranteed
    sorted ascending by compute_historical_regime_series's own
    .sort_values("date").reset_index(drop=True), :29). NEVER considers a
    date > as_of_date — no look-ahead. Returns None (never fabricates a
    label) if regime_series is empty, or if every row's date is >
    as_of_date (insufficient trailing history existed as of that date —
    a real 'no signal' outcome)."""
    if regime_series.empty:
        return None
    if as_of_date is None:
        return regime_series.iloc[-1]["label"]
    eligible = regime_series[regime_series["date"] <= as_of_date]
    if eligible.empty:
        return None
    return eligible.iloc[-1]["label"]
```

### 1.2 `strategy_lab/prospective.py` (modified)

Replace `build_todays_observation`'s regime block (`:153-158`):
```python
regime_series = compute_historical_regime_series(conn)
regime_label = regime_label_as_of(regime_series, as_of_date)
```
Add `from strategy_lab.regime_history import compute_historical_regime_series, regime_label_as_of`
(extends the existing import, was `compute_historical_regime_series` only).
No other change to this function — `is_bullish = regime_label == BULLISH_LABEL`
(`:163`) is untouched and degrades correctly (`None == "bullish_trend"` is
`False`, never fabricating a bullish entry signal from a missing regime).

### 1.3 No schema change

`research_prospective_observations.regime` already exists (`:59`). Area A
is pure logic-fix, zero `ALTER TABLE`.

### 1.4 Verification checklist — Area A

1. `regime_label_as_of` with a synthetic series covering dates
   `[D-3, D-2, D-1]` (no `D` row) and `as_of_date=D.isoformat()` returns
   `D-1`'s label, never `None` and never a fabricated `D` value — this is
   the exact real-world 16:45-ET scenario.
2. All four labels (`RegimeConfig.LABEL_BULLISH="bullish_trend"`,
   `LABEL_NEUTRAL="neutral_mixed"`, `LABEL_BEARISH="bearish_trend"`,
   `LABEL_ELEVATED_VOL="elevated_volatility_risk_off"` — exact strings
   from `research/config.py:143-146`) round-trip correctly through
   `regime_label_as_of` for a series containing each.
3. `as_of_date` earlier than every row in `regime_series` → `None`.
4. `regime_series` empty (insufficient SPY history) → `None`, for both
   `as_of_date=None` and any `as_of_date` value.
5. `as_of_date=None` behavior is byte-identical to pre-fix behavior
   (regression test against `strategy_lab/run_prospective_observation.py`'s
   no-date call path).
6. `strategy_lab/report.py`, `strategy_lab/portfolio_simulator.py`,
   `strategy_lab/report_phase10.py` are NOT imported, NOT modified, and
   their own `compute_historical_regime_series(conn)` + `.set_index(...)`
   usage is asserted unchanged (grep/diff check — Phase 9/10 frozen
   artifacts must not regenerate differently).
7. Live/manual sanity check (read-only): after deploying, run
   `build_todays_observation` for a real ticker with `as_of_date=today`
   and confirm `regime` is non-`None` whenever `alpaca_adjusted` SPY has
   at least one row `<= today`.

## 2. Area B — Legacy regime audit (new, read-only, additive)

New file: **`ops/regime_reconstruction_audit.py`**. Structurally read-only
(same convention as `ops/correction_impact_audit.py`): no
`conn.execute`/`executemany`/`executescript`/`commit`, no
`INSERT`/`UPDATE`/`DELETE` SQL string, no import of `record_observation`/
`record_event`/`mature_outcomes`/`_persist`. Only imports `load_observations`
(`strategy_lab.prospective`), `compute_historical_regime_series` +
`regime_label_as_of` (`strategy_lab.regime_history`, §1.1), `load_corrections`
(`ops.correction_impact_audit`), `RESEARCH_SOURCE` (`strategy_lab.data`),
`DEFAULT_REGIME_CONFIG` (`research.config`).

```python
REGIME_LOOKBACK_TRADING_DAYS = max(
    DEFAULT_REGIME_CONFIG.sma_long_days,          # 200
    DEFAULT_REGIME_CONFIG.drawdown_window_days,    # 60
    DEFAULT_REGIME_CONFIG.realized_vol_window_days,# 20
)  # imported, never hardcoded a second time

REASON_RECONSTRUCTABLE = "reconstructable"
REASON_NO_SPY_HISTORY_AT_OR_BEFORE_DATE = "no_spy_history_at_or_before_date"

@dataclass
class LegacyRegimeGapRow:
    observation_id: int
    ticker: str
    observation_date: str
    reconstructable: bool
    reason: str                              # REASON_* constants above
    reconstructed_label: Optional[str]       # None unless reconstructable
    corrections_in_window_count: int
    would_change_if_corrections_applied: bool  # True iff corrections_in_window_count > 0

def audit_legacy_regime_gaps(conn) -> List[LegacyRegimeGapRow]:
    """For every research_prospective_observations row with regime IS NULL
    (load_observations(conn), filter regime.isna()): compute
    regime_label_as_of(compute_historical_regime_series(conn),
    observation_date) using CURRENT SPY data. reconstructable = result is
    not None. would_change_if_corrections_applied: window_start =
    observation_date - timedelta(days=int(REGIME_LOOKBACK_TRADING_DAYS * 1.6) + 10)
    (a deliberately generous, over-inclusive calendar-day buffer over the
    200-trading-day window, matching ops/prospective_audit.py's own
    _horizon_elapsed_session buffer-multiplier convention — over-flagging a
    candidate is the SAFE direction for an informational audit, never
    under-flagging). load_corrections(conn, ticker='SPY') filtered to
    source == RESEARCH_SOURCE and window_start <= date <= observation_date;
    would_change_if_corrections_applied = count > 0. This is a FAIRNESS
    flag, not a proof of change: a correction inside the window MIGHT have
    altered the reconstructed label; it is not recomputed pre/post
    correction here (that would require reconstructing the regime series
    AS OF the correction time, out of scope for an informational report)."""

def legacy_regime_gap_summary(conn) -> dict:
    """{'total_legacy_null_regime', 'reconstructable_count',
    'not_reconstructable_count', 'correction_sensitive_count'} — a pure
    rollup of audit_legacy_regime_gaps()."""
```

**No backfill, no write, anywhere.** Any real repair (writing a
reconstructed `regime` value into an existing row) is explicitly OUT OF
SCOPE: `research_prospective_observations` has no `UPDATE` path today by
design (Phase 11 spec, immutability by construction) and Phase 16 must not
add one. If a repair is ever approved by a human, it requires a separate,
future, explicitly-specced decision (e.g. a new companion table, never an
`UPDATE` of the immutable row) — not decided or built here.

### 2.1 Verification checklist — Area B

1. Structural AST/source-text scan (mirrors
   `tests/test_ops_correction_impact_audit.py`'s three scans exactly,
   applied to `ops/regime_reconstruction_audit.py`): no write method call,
   no mutation SQL string constant, no import of a `record_*`/`mature_*`/
   `save_*`/`_persist` name.
2. A synthetic legacy row (`regime=None`, `observation_date` within
   current SPY history's reconstructable range) → `reconstructable=True`,
   `reconstructed_label` matches a direct `regime_label_as_of` call.
3. A synthetic legacy row dated before SPY ever had `sma_long_days` rows
   → `reconstructable=False`, `reason=REASON_NO_SPY_HISTORY_AT_OR_BEFORE_DATE`.
4. A synthetic correction on SPY/`alpaca_adjusted` dated inside the
   window → `would_change_if_corrections_applied=True`,
   `corrections_in_window_count >= 1`.
5. A synthetic correction on SPY dated OUTSIDE the window (e.g.
   `observation_date + 5` or far before `window_start`) →
   `would_change_if_corrections_applied=False`.
6. A correction with `source != RESEARCH_SOURCE` (e.g. plain `"alpaca"`)
   is never counted (regime always computes off `RESEARCH_SOURCE` only).
7. Zero-writes proof: run `audit_legacy_regime_gaps`/`legacy_regime_gap_summary`
   against a seeded DB, then assert every table's row count and every
   `research_prospective_observations.regime` value is byte-identical
   before/after the call.
8. Real, read-only verification against the live DB: run
   `legacy_regime_gap_summary(conn)` against the actual repo DB and confirm
   it reports the real 180 NULL rows without raising and without any write
   (safe to run directly, zero write path).

## 3. Area C — Event/experiment provenance audit (new, read-only, additive)

New file: **`ops/event_provenance_audit.py`**. Same read-only convention.
Only imports `load_events`, `ALL_EVENT_TYPES`, `EVENT_CONTROL_ENTRY`,
`EVENT_EXPERIMENT_A_ENTRY`, `EVENT_EXPERIMENT_B_ENTRY` from
`strategy_lab.prospective_events`, and `PROVENANCE_UNKNOWN_LEGACY`/
`label_provenance_value` from `ops.evidence_provenance` (Phase 15, reused
verbatim).

```python
EVENT_TYPE_TO_VARIANT = {
    EVENT_CONTROL_ENTRY: "CONTROL",
    EVENT_EXPERIMENT_A_ENTRY: "EXPERIMENT_A",
    EVENT_EXPERIMENT_B_ENTRY: "EXPERIMENT_B",
}  # entry-side attribution is ALREADY unambiguous via event_type alone
   # (§0.2) — no new column/table added for this.

def compute_event_type_breakdown(conn) -> dict:
    """{'entry_attributable': {event_type: count}, 'shared_signal_detection':
    {event_type: count}, 'total_events': n}. 'shared_signal_detection' =
    the 4 event types NOT in EVENT_TYPE_TO_VARIANT
    (score_crossing_70/trend_advance/momentum_advance/volume_advance) —
    reported as 'not variant-specific by design', never as a gap."""

def compute_event_provenance_coverage(conn) -> dict:
    """{'events_with_fingerprint', 'events_unknown_legacy' (via
    label_provenance_value on config_fingerprint), 'by_variant':
    {variant_name: count}} — by_variant derived from event_type via
    EVENT_TYPE_TO_VARIANT directly (never from config_fingerprint) for
    entry-attributable rows only."""

def compute_exit_attribution_gap_note() -> dict:
    """{'exit_event_types_exist': bool, 'exit_event_types': [...], 'note': str}.
    Dynamically inspects strategy_lab.prospective_events.ALL_EVENT_TYPES for
    any type containing 'exit' — self-correcting if a future phase adds
    exit events; never hardcodes today's absence as a permanent fact."""

def event_provenance_audit_summary(conn) -> dict:
    """Rollup: {'event_type_breakdown', 'event_provenance_coverage',
    'exit_attribution_gap', 'entry_ab_cooccurrence_note': a fixed string
    documenting that EVENT_EXPERIMENT_A_ENTRY/EVENT_EXPERIMENT_B_ENTRY
    always co-occur by construction (§0.2), not an anomaly}."""
```

**No schema change in Area C** — confirmed no genuine entry-attribution
gap exists; adding a column/table for an already-solved problem would be
unjustified scope creep. The one real gap (exit-side event types) is
explicitly NOT built here — see Open Question #2.

### 3.1 Verification checklist — Area C

1. AST/source-text read-only scans (same 3-part pattern as §2.1 item 1),
   applied to `ops/event_provenance_audit.py`.
2. Seed one `control_entry`, one `experiment_a_entry`, one
   `experiment_b_entry`, one `score_crossing_70` event →
   `compute_event_type_breakdown` places the first three under
   `entry_attributable` and the fourth under `shared_signal_detection`,
   counts correct.
3. `compute_event_provenance_coverage`'s `by_variant` sums the three
   entry-type counts under `"CONTROL"`/`"EXPERIMENT_A"`/`"EXPERIMENT_B"`
   respectively — never double-counted, never merged.
4. A `config_fingerprint=NULL` event → counted under `events_unknown_legacy`.
5. `compute_exit_attribution_gap_note()` on the real, current
   `ALL_EVENT_TYPES` returns `exit_event_types_exist=False` (documents the
   real, confirmed gap) — and a monkeypatched `ALL_EVENT_TYPES` that
   *does* include an `"experiment_b_exit_regime_loss"`-shaped string
   flips it to `True` (proves the check is dynamic, not hardcoded).
6. Zero-writes proof, same pattern as §2.1 item 7.

## 4. Area D — Outcome maturation provenance (modified, additive schema)

### 4.1 `strategy_lab/outcome_maturation.py`

Two new columns on `research_prospective_outcomes` (additive
`ALTER TABLE`, exact existing pattern):
```python
if "source_resolution_method" not in cols:
    conn.execute(f"ALTER TABLE {OUTCOME_TABLE_NAME} ADD COLUMN source_resolution_method TEXT")
if "effective_price_source_used" not in cols:
    conn.execute(f"ALTER TABLE {OUTCOME_TABLE_NAME} ADD COLUMN effective_price_source_used TEXT")
```

New constants:
```python
SOURCE_RESOLUTION_EXPLICIT = "explicit"          # prov_source was non-NULL
SOURCE_RESOLUTION_FALLBACK = "resolved_fallback"  # prov_source NULL, resolve_source() picked one
SOURCE_RESOLUTION_UNAVAILABLE = "unavailable"     # prov_source NULL AND resolve_source() found nothing
```

`MaturationOutcome`: add `source_resolution_method: Optional[str] = None`,
`effective_price_source_used: Optional[str] = None`.

`_compute_one`: **do not touch the existing `source`/`methodology_version`/
`config_fingerprint` propagation (`:91-97`) — those columns keep meaning
"the observation's own stored provenance," staying `NULL` for a legacy row,
exactly as today.** The two new fields are a SEPARATE, additional pair of
columns describing what maturation actually did, computed only at the
point price data is actually loaded (never on the early `STATUS_PENDING`
return, `:99-105` — `source_resolution_method`/`effective_price_source_used`
stay `None` there; nothing about pricing has happened yet to describe):

```python
# immediately before the existing `cache_key = (ticker, source)` line (:109)
from db.price_repository import resolve_source  # function-local import, mirrors this file's existing style
if prov_source:
    effective_source = prov_source
    source_resolution_method = SOURCE_RESOLUTION_EXPLICIT
else:
    effective_source = resolve_source(conn, ticker)
    source_resolution_method = (
        SOURCE_RESOLUTION_FALLBACK if effective_source else SOURCE_RESOLUTION_UNAVAILABLE
    )
```
Attach `source_resolution_method=source_resolution_method,
effective_price_source_used=effective_source` to every `MaturationOutcome`
returned from this point forward (both `STATUS_UNAVAILABLE` branches and
`STATUS_MATURED`). **The actual `load_price_history(conn, ticker,
source=source)` call (`:111`) is unchanged** — `source` is still exactly
`prov_source` (possibly `None`); this deliberately duplicates the cheap,
pure `resolve_source` read rather than changing `db/price_repository.py`'s
shared, widely-used public contract — accepted, documented tradeoff (lower
blast radius than touching a file with unrelated call sites).

`_persist`: extend the `INSERT`/`ON CONFLICT DO UPDATE` column list and
`SET` clause with `source_resolution_method`, `effective_price_source_used`
— same idempotent pattern as the three Phase 15 fields.

New pure read function (mirrors `maturation_summary`, same file):
```python
def source_resolution_summary(conn) -> dict:
    """{'explicit', 'resolved_fallback', 'unavailable', 'not_yet_resolved'
    (STATUS_PENDING rows, both fields NULL), 'total'} — counts over
    load_outcomes(conn)."""
```

### 4.2 Explicit safeguard against silent fallback on a provenanced row

Structurally guaranteed, not just tested: the `if prov_source:` branch
above means a row with a real `source` **can never** reach
`resolve_source()` — there is no code path for it to. Required regression
test (§4.3 item 4) proves this by mocking `resolve_source` to return a
*different* value than the stored `source` and asserting it is never
called when `prov_source` is truthy.

### 4.3 Verification checklist — Area D

1. A synthetic observation with `source="alpaca_adjusted"` (explicit) →
   matured outcome has `source_resolution_method="explicit"`,
   `effective_price_source_used="alpaca_adjusted"`.
2. **Real-shape regression, mirroring the confirmed AAPL row exactly:** an
   observation with `source=None`, priced data only under `source="alpaca"`
   → matured outcome has `source_resolution_method="resolved_fallback"`,
   `effective_price_source_used="alpaca"`, **and** `source` (the existing
   Phase 15 column) stays `NULL` — the fallback is now visible via the new
   fields without changing the existing field's "unknown provenance"
   semantics.
3. An observation with `source=None` and zero price data anywhere for that
   ticker → `source_resolution_method="unavailable"`,
   `effective_price_source_used=None`, `status=STATUS_UNAVAILABLE`
   (unchanged existing behavior).
4. **Non-fallback safeguard proof:** monkeypatch
   `db.price_repository.resolve_source` to return a sentinel value
   different from a seeded observation's real `source="alpaca_adjusted"`;
   mature it; assert `effective_price_source_used="alpaca_adjusted"` (the
   explicit value, never the sentinel) AND assert (via mock call-count)
   the patched `resolve_source` was never invoked for this row.
5. `STATUS_PENDING` outcome → `source_resolution_method=None`,
   `effective_price_source_used=None` (not yet resolved, by design — not a
   gap).
6. Idempotency: calling `mature_outcomes()` twice on unchanged data
   produces byte-identical `source_resolution_method`/
   `effective_price_source_used` on the second call.
7. **Legacy real-row regression:** seed the exact real `id=1` AAPL row
   shape (`source=NULL`, `methodology_version=NULL`,
   `config_fingerprint=NULL`), mature it, and assert `realized_return`/
   `exit_date`/`status` are UNCHANGED from what Phase 15's own test (§8
   item 6 of docs/specs/phase15.md) already established — proving the new
   fields add visibility without altering the actual computed value.
8. `source_resolution_summary(conn)` counts sum correctly across a mixed
   fixture (some explicit, some fallback, some unavailable, some pending).

## 5. Area E — Monitoring/reporting (additive only)

### 5.1 `ops/completeness_classification.py` (new, small, pure — mirrors `ops/evidence_provenance.py`)

```python
COMPLETENESS_COMPLETE = "COMPLETE"
COMPLETENESS_PARTIAL = "PARTIAL"
COMPLETENESS_LEGACY_INCOMPLETE = "LEGACY_INCOMPLETE"
COMPLETENESS_INVALID = "INVALID"

def classify_observation_row(row: dict) -> str:
    """Checked in this exact priority order (first match wins):
    INVALID:            score is None or stage is None or close is None
                         or not row.get("observation_date") or not row.get("ticker")
    LEGACY_INCOMPLETE:   row.get("methodology_version") is None
    COMPLETE:            regime is not None and source is not None
                          and methodology_version is not None
                          and config_fingerprint is not None
    PARTIAL:             everything else (methodology_version known, but
                          regime/source/config_fingerprint has >=1 gap —
                          e.g. a modern row from a run whose
                          config_fingerprint capture failed fail-open,
                          Phase 15 §0.5 item 4)."""

def classify_event_row(row: dict) -> str:
    """INVALID: event_type not in strategy_lab.prospective_events.ALL_EVENT_TYPES
                or not row.get("event_date") or not row.get("ticker")
    LEGACY_INCOMPLETE: methodology_version is None
    COMPLETE: methodology_version, source, config_fingerprint all not None
    PARTIAL: otherwise."""

def classify_outcome_row(row: dict) -> str:
    """INVALID: status not in {STATUS_PENDING, STATUS_MATURED, STATUS_UNAVAILABLE}
                or (status == STATUS_MATURED and (realized_return is None or exit_date is None))
    LEGACY_INCOMPLETE: methodology_version is None
    COMPLETE: methodology_version, source, config_fingerprint all not None
              AND (status == STATUS_PENDING OR
                   (source_resolution_method is not None and effective_price_source_used is not None))
    PARTIAL: otherwise."""
```
Pure functions, no DB access, no schema, importable by Areas B/D/E and the
dashboard alike — exactly one definition, matching the
`ops/evidence_provenance.py` precedent.

### 5.2 `ops/prospective_audit.py` (extended — additive functions only, same file)

New imports at top: `regime_reconstruction_audit.legacy_regime_gap_summary`,
`event_provenance_audit.event_provenance_audit_summary`,
`completeness_classification.*`, `strategy_lab.outcome_maturation.
source_resolution_summary`, `strategy_lab.prospective_events.load_events`,
`ops.evidence_classification.{classify_evidence, count_prospective_trading_days,
EVIDENCE_INSUFFICIENT_DATA}` (reused verbatim, thresholds untouched).

```python
def compute_regime_distribution(conn) -> dict:
    """{'by_label': {label_or_'NULL': count}, 'total'} — groups
    load_observations(conn)['regime'] including a literal 'NULL' bucket
    key for None values (a REPORTING bucket only — never a stored DB
    value; NULL rows may be either pre-Area-A-fix legacy rows or genuine
    insufficient-history rows, disambiguated via legacy_regime_gap_summary
    below, not conflated here)."""

def compute_completeness_breakdown(conn) -> dict:
    """{'observations': {COMPLETE:n, PARTIAL:n, LEGACY_INCOMPLETE:n, INVALID:n, 'total':n},
    'events': {...same 4 keys...}, 'outcomes': {...same 4 keys...}} — one
    classify_*_row() call per row via load_observations/load_events/load_outcomes."""

def phase16_monitoring_summary(conn, today: Optional[date] = None) -> dict:
    """Additive rollup: returns long_term_monitoring_summary(conn, today)'s
    existing dict (UNCHANGED keys, Phase 15) with new keys merged in:
    'regime_distribution', 'legacy_regime_gap_summary',
    'completeness_breakdown', 'event_provenance_audit',
    'outcome_source_resolution' (source_resolution_summary(conn)),
    'evidence_sufficiency': {'n_prospective_trading_days':
    count_prospective_trading_days(conn), 'status': classify_evidence(n)} —
    the SAME existing vocabulary/thresholds (ops/evidence_classification.py,
    untouched), reused not reimplemented. This is the one function
    Phase 16's dashboard getter calls."""
```

### 5.3 `dashboard/data.py` (additive getters, same `@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)` shape)

```python
@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_phase16_monitoring_summary() -> dict:
    from ops.prospective_audit import phase16_monitoring_summary
    with db_session() as conn:
        return phase16_monitoring_summary(conn)

@st.cache_data(ttl=ALERTS_TTL_SECONDS, show_spinner=False)
def get_legacy_regime_gap_report(limit: int = 200) -> pd.DataFrame:
    from ops.regime_reconstruction_audit import audit_legacy_regime_gaps
    with db_session() as conn:
        rows = audit_legacy_regime_gaps(conn)
    df = pd.DataFrame([r.__dict__ for r in rows])
    return df.tail(limit) if not df.empty else df
```
Add both to `clear_all_caches()`.

### 5.4 `dashboard/views/ops_overview.py` (additive section, same file — already in `PHASE13_FILES`'s import-safety scan list, `tests/test_ops_safety_phase13.py:21`, automatically covered)

New function `_render_regime_provenance_monitoring()`, inserted (in
`render()`) immediately after the existing
`_render_evidence_provenance_audit()` try/except block and before
`_render_automation_history()`, same try/except wrapper pattern:
- Evidence-sufficiency caption at top (reused `EVIDENCE_INSUFFICIENT_DATA`
  flag, never a new rival label).
- Regime distribution table/metrics + legacy-gap summary metrics
  (`reconstructable_count` / `not_reconstructable_count` /
  `correction_sensitive_count`).
- Completeness breakdown: 3 small tables (observations/events/outcomes),
  each showing the 4-state counts.
- Event type breakdown (entry_attributable vs shared_signal_detection) +
  exit-attribution-gap note (`st.caption`, always visible, never hidden).
- Outcome source-resolution breakdown (`explicit`/`resolved_fallback`/
  `unavailable`/`not_yet_resolved`).

All `st.metric`/`st.dataframe`/`st.caption` — no `st.button`, no form.

### 5.5 `dashboard/views/strategy_lab.py` (additive section, same file — already scanned by `test_strategy_lab_dashboard_view_never_imports_order_execution`)

New function `_render_regime_provenance_detail()`, called last in
`render()`, own try/except, after the existing
`_render_evidence_provenance_detail()` block. Shows the detailed
`get_legacy_regime_gap_report()` DataFrame with an explicit caption:
*"Informational only — no repair is performed by this system. Any real
correction to a legacy row requires explicit human approval and a
separately-specced mechanism (this table has no write path)."* Also shows
`event_provenance_audit`'s exit-attribution-gap note explicitly, and the
outcome source-resolution breakdown.

### 5.6 Verification checklist — Area E

1. `classify_observation_row`/`classify_event_row`/`classify_outcome_row`:
   table-driven test covering all 4 states for each of the 3 row shapes,
   including the exact real `id=1` AAPL shape → `LEGACY_INCOMPLETE`.
2. `compute_regime_distribution`: seeded rows with each of the 4 real
   labels plus 2 NULL rows → `by_label['NULL'] == 2`, others correct.
3. `phase16_monitoring_summary` is a strict superset of
   `long_term_monitoring_summary`'s keys for identical inputs (mirrors
   Phase 15 §8 item 23's pattern).
4. `get_phase16_monitoring_summary`/`get_legacy_regime_gap_report`:
   idempotent (two calls, unchanged DB, identical results), each opens
   exactly one `db_session()`.
5. Dashboard read-only safety: grep both new render functions for
   `st.button`/`st.form` → none found (extends the existing pattern noted
   in Phase 15 §8 item 27).
6. `evidence_sufficiency`'s `status` value is drawn from exactly
   `{EVIDENCE_INSUFFICIENT_DATA, EVIDENCE_EARLY_EVIDENCE, EVIDENCE_EVALUATION_READY}`
   — never a new string.

## 6. Hard constraints — restated per area, verified against this design

- **No trading/Discord dependency anywhere in Phase 16 code.** Every new
  file (`ops/regime_reconstruction_audit.py`, `ops/event_provenance_audit.py`,
  `ops/completeness_classification.py`) lives under `ops/`, automatically
  covered by the existing generic scan
  `tests/test_ops_safety.py::test_ops_never_imports_order_execution_or_alerting`
  (scans every `ops/*.py` for `trading.engine`/`trading.orders`/
  `trading.run_paper`/`alerts.discord`/`alerts.runner`/`alerts.run_alerts`)
  — zero spec changes needed to that test. Every modified `strategy_lab/`
  file is covered the same way by
  `tests/test_strategy_lab.py::test_strategy_lab_never_imports_order_execution_or_alerting`.
  `dashboard/views/ops_overview.py` is already in
  `tests/test_ops_safety_phase13.py::PHASE13_FILES`;
  `dashboard/views/strategy_lab.py` is covered by
  `test_strategy_lab_dashboard_view_never_imports_order_execution`.
- **No write path in Areas B/C** (`ops/regime_reconstruction_audit.py`,
  `ops/event_provenance_audit.py`) — proven by the same AST/source-text
  scan pattern as `ops/correction_impact_audit.py`
  (`tests/test_ops_correction_impact_audit.py`'s three scans), applied
  verbatim to both new files in new test files (§8).
- **No rewrite of any existing row.** Area A changes only *future*
  lookups (a pure function change in the read path used before a value is
  ever written); Areas D's two new columns are `ADD COLUMN` only, and the
  existing `_persist` `ON CONFLICT DO UPDATE` path is unchanged in kind
  (already pre-existing per Phase 15 §0.1, applies only to
  `research_prospective_outcomes`, never to `research_prospective_observations`
  or `research_prospective_events`). No `UPDATE ... SET` statement is
  added to `strategy_lab/prospective.py` or `strategy_lab/prospective_events.py`
  anywhere in Phase 16.
- **No LaunchAgent/schedule changes.** Nothing in `deploy/` is read,
  referenced, or modified by any Phase 16 code path (Area A's mechanism
  understanding references the plist only as background documentation in
  this spec, §0.1 — no file under `deploy/` is touched).
- **No new trading hypothesis/filter/optimization.** Area C explicitly
  does NOT add exit-event detection (the one place this could have
  drifted toward new signal logic) — flagged as Open Question #2 instead.
  `strategy_lab/phase10_experiments.py` is imported nowhere in Phase 16
  code except read-only in `ops/event_provenance_audit.py`'s docstring
  reference (no import at all, actually — `EVENT_TYPE_TO_VARIANT` uses
  string literals matching `strategy_lab.prospective_events`'s own
  constants, not `phase10_experiments.py`'s `RegimeGatedRules.name` values,
  to avoid any coupling to the frozen experiment-definition module).
- **No automatic repair/backfill of legacy evidence.** Areas B and D are
  reporting only — stated explicitly in both sections above and in each
  dashboard caption.

## 7. Files: full list

**New:**
- `ops/regime_reconstruction_audit.py` (Area B)
- `ops/event_provenance_audit.py` (Area C)
- `ops/completeness_classification.py` (Area E, shared pure helper)
- `tests/test_strategy_lab_regime_asof.py` (Area A)
- `tests/test_ops_regime_reconstruction_audit.py` (Area B)
- `tests/test_ops_event_provenance_audit.py` (Area C)
- `tests/test_strategy_lab_outcome_maturation_phase16.py` (Area D)
- `tests/test_ops_completeness_classification.py` (Area E)
- `tests/test_ops_prospective_audit_phase16.py` (Area E, mirrors
  `test_ops_prospective_audit_phase15.py`'s convention of a separate file
  importing the same `ops.prospective_audit` module)

**Modified:**
- `strategy_lab/regime_history.py` (§1.1, additive function)
- `strategy_lab/prospective.py` (§1.2)
- `strategy_lab/outcome_maturation.py` (§4.1)
- `ops/prospective_audit.py` (§5.2, additive functions only)
- `dashboard/data.py` (§5.3)
- `dashboard/views/ops_overview.py` (§5.4)
- `dashboard/views/strategy_lab.py` (§5.5)

**Untouched (confirm, do not edit):**
- `strategy_lab/report.py`, `strategy_lab/report_phase10.py`,
  `strategy_lab/portfolio_simulator.py`, `strategy_lab/phase9_baseline.py`,
  `strategy_lab/phase10_baseline.py`, `strategy_lab/phase11_report.py` —
  frozen artifact generators, zero import from Phase 16 code, zero
  behavior change (§1.1's "do not modify
  `compute_historical_regime_series`" constraint).
- `strategy_lab/phase10_experiments.py` — read about, never imported by
  Phase 16 code (§6).
- `db/schema.py`, `db/price_repository.py` (§4.1's deliberate
  no-signature-change decision), `ops/evidence_classification.py`
  (thresholds untouched), `ops/data_quality.py`, `ops/experiment_registry.py`,
  `ops/correction_impact_audit.py` (Phase 15, imported from only),
  `ops/evidence_provenance.py` (Phase 15, imported from only),
  `strategy_lab/production_guard.py`, `strategy_lab/cache_integrity.py`,
  `deploy/`.

## 8. Testing section (tester agent — full checklist)

In addition to each area's own §1.4/§2.1/§3.1/§4.3/§5.6 checklist above:

1. **All four regime labels correctly assigned** — §1.4 item 2.
2. **No-look-ahead/truncation behavior** — §1.4 item 1 (the exact
   real-world scenario), plus a table-driven sweep of `as_of_date` values
   both inside and outside `regime_series`'s date range.
3. **Insufficient-history handling** — §1.4 item 4; §2.1 item 3.
4. **Observation/event/outcome immutability preserved**: re-run the
   EXISTING Phase 11/15 tests asserting
   `INSERT...ON CONFLICT DO NOTHING` on `research_prospective_observations`/
   `research_prospective_events` still holds (no new code path added by
   Phase 16 touches either table's write functions at all — confirm via
   `grep -n "conn.execute" strategy_lab/prospective.py
   strategy_lab/prospective_events.py` showing only the pre-existing
   `INSERT ... ON CONFLICT DO NOTHING` statements, byte-identical to
   pre-Phase-16).
5. **Read-only-ness of the legacy audit (Area B)** — §2.1 items 1, 7.
6. **Event attribution immutability for legacy rows**: a legacy event row
   (`config_fingerprint=NULL`) is still classified `events_unknown_legacy`
   and its `event_type`→variant mapping (if an entry type) is unaffected
   by provenance-column NULL-ness — attribution via `event_type` never
   depends on `config_fingerprint` being populated.
7. **Completeness classification correctness for each of the 4 states** —
   §5.6 item 1, for all three row shapes.
8. **Maturation source/idempotency/no-silent-fallback-for-provenanced-rows** —
   §4.3 items 1, 4, 6.
9. **Explicit safety tests — cannot trade, cannot send Discord, cannot
   mutate `alerts`/`alert_state` tables, cannot alter frozen strategy
   config, cannot touch `deploy`/LaunchAgent files:**
   - Import-direction scans (§6, first bullet) — extend
     `tests/test_ops_safety.py`'s existing generic `ops/*.py` scan and
     `tests/test_strategy_lab.py`'s generic `strategy_lab/*.py` scan
     automatically cover all new/modified files with **zero** test-file
     changes needed to those two scans themselves (already directory-wide).
   - New, Phase-16-specific AST/source-text scans (mirroring
     `tests/test_ops_correction_impact_audit.py`'s exact pattern, reused
     verbatim per this task's own instruction) in
     `tests/test_ops_regime_reconstruction_audit.py` and
     `tests/test_ops_event_provenance_audit.py`: no `.execute(`/
     `.executemany(`/`.executescript(`/`.commit(`, no
     `INSERT INTO`/`UPDATE ... SET`/`DELETE FROM` string constant, no
     import/call of any `record_*`/`mature_*`/`save_*`/`_persist`-named
     function.
   - Explicit assertion (new test, any Phase 16 test file) that no
     Phase 16 file imports or calls anything from `alerts.config`'s
     `alert_state`-writing functions or `db.database`'s `alerts` table
     write helpers — a grep-based scan for the literal strings
     `"alert_state"` and `"INSERT INTO alerts"` across all 3 new `ops/`
     files and the 3 modified `strategy_lab/` files, asserting zero
     matches outside docstrings/comments (AST `ast.Constant` scan, same
     technique as the SQL-string scan).
   - Explicit assertion that no Phase 16 file references
     `deploy/` or any `.plist` path as a string literal, and none imports
     `subprocess`/`os.system`/`launchctl`-adjacent calls (grep + AST scan,
     new test).
   - Explicit assertion that no Phase 16 file imports
     `signals.config`/`backtest.config`/`trading.config`/`config.settings`/
     `alerts.config` for anything other than a read of an already-defined
     constant (i.e., no `ALTER`/mutation of those modules' module-level
     values) — a static-analysis scan confirming no `setattr(` /
     module-attribute-assignment targeting those modules anywhere in the
     new/modified files.
10. **Timezone/NYSE-calendar correctness**: Area A's `regime_label_as_of`
    and Area B's window-buffer calendar-day arithmetic tested across a
    weekend/holiday boundary (extends the existing DST test pattern noted
    in Phase 15 §8 item 26).
11. **Duplicate/retry idempotency**: running `run_research_job()` twice for
    the same `today` (post-Area-A-fix) produces identical `regime` values
    on the (unchanged, deduped) first-call row — Area A's fix doesn't
    interact with the existing `ON CONFLICT DO NOTHING` dedup path at all.
12. Full `pytest tests/` green; `git diff` touches only files listed in §7.

## 9. Open questions (explicit — NOT decided here, for human/orchestrator confirmation before build)

1. **Upstream SPY (`alpaca_adjusted`) freshness cadence.** Area A's
   point-in-time fix (§1.1) is correct and no-look-ahead-safe regardless
   of how stale `RESEARCH_SOURCE` SPY data is — but if nothing refreshes
   it regularly, `regime_label_as_of` could keep returning a label that's
   several trading days behind (better than permanently `NULL`, but not
   ideal). Confirmed: nothing in `run_research_job`'s daily path calls
   `strategy_lab.data.fetch_and_cache_universe` for SPY today — it's
   fetched only via manually-invoked `run_study`/`run_phase10_study`/
   `run_phase11_study` CLIs. Per the hard constraint against new scheduled
   jobs/`deploy/` changes, Phase 16 does **not** add a daily SPY refresh
   call to `run_research_job`. **Question for the user/orchestrator:** is
   the current manual refresh cadence acceptable, or should a future phase
   add a lightweight, existing-job-embedded SPY-only refresh (not a new
   LaunchAgent, but a code change to `run_research_job` itself)? Not
   decided or built in Phase 16.
2. **Exit-side event attribution gap (Area C, §0.2).** Experiment B's only
   differentiator from Experiment A (`exit_on_regime_loss`) is invisible
   in `research_prospective_events` — no exit event type exists for any
   variant. Building exit-event detection would require new,
   hypothesis-adjacent signal-observation logic that risks touching the
   "no new trading hypothesis/filter" and "never touch frozen experiment
   definitions" hard constraints even if purely observational/read-only.
   **Question for the user:** should a future phase add read-only,
   observational exit-event recording for CONTROL/A/B (never affecting
   real positions), or is entry-only event attribution considered
   sufficient for the eventual Phase 10 hypothesis evaluation? Not decided
   or built in Phase 16.
3. **Area D's new visibility columns — material-change determination.**
   My assessment: `source_resolution_method`/`effective_price_source_used`
   (§4.1) do **not** constitute a material methodology/comparability
   change — they change zero existing `realized_return`/`status`/
   `exit_date`/`source` values for any row (proven by §4.3 item 7's
   regression test), they only make an already-happening, already-silent
   fallback visible. Per this task's explicit instruction, this
   determination is flagged here for human confirmation rather than
   silently acted on — **please confirm before the coder implements §4.1**,
   or veto/amend if this reasoning is judged insufficient.
4. **Area B's "would change if corrections applied" buffer.** The
   `REGIME_LOOKBACK_TRADING_DAYS * 1.6 + 10` calendar-day buffer (§2) is a
   deliberately generous, over-inclusive heuristic (documented as safe
   because over-flagging is the conservative direction for an
   informational-only audit). If a tighter, NYSE-calendar-exact window is
   preferred instead of this buffer heuristic, that's a straightforward
   implementation refinement or the coder — flagged here only so the
   choice of "buffer heuristic vs. exact calendar walk" isn't silently
   assumed to be the only acceptable approach.

## 10. Manual/integration verification before moving on from Phase 16

1. `pytest tests/` — full suite green, including all new Phase 16 test
   files listed in §7.
2. Real, read-only verification against the live DB (safe — zero write
   path in any Area B/C code): `legacy_regime_gap_summary(conn)` reports
   a real breakdown of the actual 180 legacy-NULL rows;
   `event_provenance_audit_summary(conn)` runs cleanly against the real
   `research_prospective_events` table.
3. After deploying Area A, monitor the NEXT scheduled research job run
   (16:45 ET) and confirm (read-only query) that newly-created
   observations for that trading date have a non-`NULL` `regime` value
   whenever `alpaca_adjusted` SPY has at least one row `<=` that date —
   this is the actual fix for the reported incident.
4. `streamlit run dashboard/app.py` — load both the Operations and
   Strategy Lab pages, confirm the two new sections render without
   exception, including against a DB with zero legacy gaps and zero
   corrections (empty-state paths).
5. `git diff` touches only files listed in §7.
