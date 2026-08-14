"""Tests for ops/evidence_classification.py: the trading-day-based
prospective evidence vocabulary (INSUFFICIENT_DATA / EARLY_EVIDENCE /
EVALUATION_READY), distinct from Phase 11's event-count-based
strategy_lab.prospective_events.evidence_label. Pure-function determinism
and boundary tests, plus the distinct-dates-not-rows counting rule.
"""
import sqlite3

from db.schema import init_db
from ops.evidence_classification import (
    EVIDENCE_EARLY_EVIDENCE,
    EVIDENCE_EVALUATION_READY,
    EVIDENCE_INSUFFICIENT_DATA,
    MIN_DAYS_EARLY_EVIDENCE,
    MIN_DAYS_EVALUATION_READY,
    classify_evidence,
    count_prospective_trading_days,
)


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _sample_observation(ticker="AAA", date="2024-01-02", score=75.0):
    return dict(
        observation_date=date, ticker=ticker, score=score, stage="momentum", regime="bullish_trend",
        control_entry_signal=True, experiment_a_entry_signal=True, experiment_b_entry_signal=True,
        adx=30.0, rsi=60.0, macd=1.0, macd_signal=0.5, volume_ratio=1.5, close=100.0,
    )


# --- classify_evidence boundaries ---

def test_classify_evidence_boundaries():
    assert MIN_DAYS_EARLY_EVIDENCE == 20
    assert MIN_DAYS_EVALUATION_READY == 60
    assert classify_evidence(19) == EVIDENCE_INSUFFICIENT_DATA
    assert classify_evidence(20) == EVIDENCE_EARLY_EVIDENCE
    assert classify_evidence(59) == EVIDENCE_EARLY_EVIDENCE
    assert classify_evidence(60) == EVIDENCE_EVALUATION_READY


def test_classify_evidence_zero_is_insufficient():
    assert classify_evidence(0) == EVIDENCE_INSUFFICIENT_DATA


def test_classify_evidence_large_value_is_evaluation_ready():
    assert classify_evidence(1000) == EVIDENCE_EVALUATION_READY


# --- count_prospective_trading_days: distinct dates, not rows ---

def test_count_prospective_trading_days_empty_table_is_zero():
    conn = make_test_db()
    assert count_prospective_trading_days(conn) == 0


def test_count_prospective_trading_days_counts_distinct_dates_not_rows():
    from strategy_lab import prospective
    conn = make_test_db()
    # Two different tickers observed on the SAME date must count as 1 day.
    prospective.record_observation(conn, **_sample_observation(ticker="AAA", date="2024-01-02"))
    prospective.record_observation(conn, **_sample_observation(ticker="BBB", date="2024-01-02"))
    assert count_prospective_trading_days(conn) == 1

    # A genuinely new date increments the count.
    prospective.record_observation(conn, **_sample_observation(ticker="AAA", date="2024-01-03"))
    assert count_prospective_trading_days(conn) == 2
