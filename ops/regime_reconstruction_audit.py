"""Phase 16 Area B: legacy regime audit - read-only.

Structurally read-only (same convention as ops/correction_impact_audit.py):
this module never calls conn.execute/conn.executemany/conn.executescript/
conn.commit, never contains an INSERT/UPDATE/DELETE SQL string, and never
imports record_observation/record_event/mature_outcomes/_persist. See
tests/test_ops_regime_reconstruction_audit.py's AST/source-text scan, which
fails the build if a future edit ever adds a mutation path here.

Answers docs/specs/phase16.md's Area A root-cause finding (§0.1): before the
Area A fix (strategy_lab/regime_history.py::regime_label_as_of), every
research_prospective_observations row's `regime` column was computed via an
EXACT-match lookup against a regime series whose latest available date is
structurally always behind the observation's own date at 16:45 ET - so
`regime` is NULL for essentially every automated observation prior to the
fix. This module reports, for every such legacy NULL-regime row, whether the
regime is now reconstructable from CURRENT SPY data (using the same
regime_label_as_of point-in-time lookup the fix uses going forward), and
whether any cache correction landed inside the reconstruction window (a
fairness flag only, never a recomputed pre/post value).

No backfill, no write, anywhere. research_prospective_observations has no
UPDATE path today by design (Phase 11 spec, immutability by construction)
and this module must not add one. Any real repair of a legacy row is
explicitly out of scope - see docs/specs/phase16.md §2.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional

from ops.correction_impact_audit import load_corrections
from research.config import DEFAULT_REGIME_CONFIG
from strategy_lab.data import RESEARCH_SOURCE
from strategy_lab.prospective import load_observations
from strategy_lab.regime_history import compute_historical_regime_series, regime_label_as_of

# Imported, never hardcoded a second time.
REGIME_LOOKBACK_TRADING_DAYS = max(
    DEFAULT_REGIME_CONFIG.sma_long_days,           # 200
    DEFAULT_REGIME_CONFIG.drawdown_window_days,     # 60
    DEFAULT_REGIME_CONFIG.realized_vol_window_days,  # 20
)

REASON_RECONSTRUCTABLE = "reconstructable"
REASON_NO_SPY_HISTORY_AT_OR_BEFORE_DATE = "no_spy_history_at_or_before_date"


@dataclass
class LegacyRegimeGapRow:
    observation_id: int
    ticker: str
    observation_date: str
    reconstructable: bool
    reason: str                                # REASON_* constants above
    reconstructed_label: Optional[str]         # None unless reconstructable
    corrections_in_window_count: int
    would_change_if_corrections_applied: bool  # True iff corrections_in_window_count > 0


def _window_start(observation_date: str) -> str:
    """A deliberately generous, over-inclusive calendar-day buffer over the
    200-trading-day window, matching ops/prospective_audit.py's own
    _horizon_elapsed_session buffer-multiplier convention - over-flagging a
    candidate is the SAFE direction for an informational audit, never
    under-flagging."""
    obs_date = date.fromisoformat(observation_date)
    start = obs_date - timedelta(days=int(REGIME_LOOKBACK_TRADING_DAYS * 1.6) + 10)
    return start.isoformat()


def audit_legacy_regime_gaps(conn) -> List[LegacyRegimeGapRow]:
    """For every research_prospective_observations row with regime IS NULL
    (load_observations(conn), filter regime.isna()): compute
    regime_label_as_of(compute_historical_regime_series(conn),
    observation_date) using CURRENT SPY data. reconstructable = result is
    not None. would_change_if_corrections_applied: window_start =
    observation_date - timedelta(days=int(REGIME_LOOKBACK_TRADING_DAYS * 1.6)
    + 10) (see _window_start). load_corrections(conn, ticker='SPY') filtered
    to source == RESEARCH_SOURCE and window_start <= date <= observation_date;
    would_change_if_corrections_applied = count > 0. This is a FAIRNESS
    flag, not a proof of change: a correction inside the window MIGHT have
    altered the reconstructed label; it is not recomputed pre/post
    correction here (that would require reconstructing the regime series
    AS OF the correction time, out of scope for an informational report)."""
    observations = load_observations(conn)
    if observations.empty:
        return []

    legacy = observations[observations["regime"].isna()]
    if legacy.empty:
        return []

    regime_series = compute_historical_regime_series(conn)

    spy_corrections = load_corrections(conn, ticker="SPY")
    if not spy_corrections.empty:
        spy_corrections = spy_corrections[spy_corrections["source"] == RESEARCH_SOURCE]

    rows: List[LegacyRegimeGapRow] = []
    for _, obs in legacy.iterrows():
        observation_date = obs["observation_date"]
        reconstructed_label = regime_label_as_of(regime_series, observation_date)
        reconstructable = reconstructed_label is not None
        reason = REASON_RECONSTRUCTABLE if reconstructable else REASON_NO_SPY_HISTORY_AT_OR_BEFORE_DATE

        corrections_in_window_count = 0
        if not spy_corrections.empty:
            window_start = _window_start(observation_date)
            in_window = spy_corrections[
                (spy_corrections["date"] >= window_start) & (spy_corrections["date"] <= observation_date)
            ]
            corrections_in_window_count = int(len(in_window))

        rows.append(LegacyRegimeGapRow(
            observation_id=int(obs["id"]),
            ticker=obs["ticker"],
            observation_date=observation_date,
            reconstructable=reconstructable,
            reason=reason,
            reconstructed_label=reconstructed_label,
            corrections_in_window_count=corrections_in_window_count,
            would_change_if_corrections_applied=corrections_in_window_count > 0,
        ))
    return rows


def legacy_regime_gap_summary(conn) -> dict:
    """{'total_legacy_null_regime', 'reconstructable_count',
    'not_reconstructable_count', 'correction_sensitive_count'} - a pure
    rollup of audit_legacy_regime_gaps()."""
    rows = audit_legacy_regime_gaps(conn)
    return {
        "total_legacy_null_regime": len(rows),
        "reconstructable_count": sum(1 for r in rows if r.reconstructable),
        "not_reconstructable_count": sum(1 for r in rows if not r.reconstructable),
        "correction_sensitive_count": sum(1 for r in rows if r.would_change_if_corrections_applied),
    }
