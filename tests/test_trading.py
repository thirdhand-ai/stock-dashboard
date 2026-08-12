"""Tests for the Phase 7 paper-trading layer (trading/*, db/trading_repository.py,
dashboard's paper-portfolio data helpers).

Every Alpaca call is a fake/mock object defined in this file - no test here
ever makes a real network call, and no test ever places a real paper or live
order. trading/orders.submit_order() is only ever invoked against
FakeTradingClient.submit_order(), which just records the call and returns a
synthetic Order - there is no code path in this file that reaches Alpaca's
actual API.
"""
import sqlite3
from unittest.mock import patch

import pytest
from alpaca.common.enums import BaseURL
from alpaca.trading.enums import OrderStatus

from db.schema import init_db
from db.trading_repository import (
    get_order_by_client_id,
    load_order_history,
    load_portfolio_snapshots,
    upsert_order,
)
from indicators.technical import IndicatorResult
from trading import portfolio as trading_portfolio
from trading import risk as trading_risk
from trading.client import PaperEnvironmentUnconfirmedError, verify_paper_environment
from trading.config import RiskConfig
from trading.engine import MODE_DRY_RUN, MODE_PAPER_SEND, run_cycle
from trading.idempotency import build_client_order_id
from trading.reconcile import reconcile_open_orders
from trading.signals_bridge import INTENT_ENTRY, INTENT_EXIT, TradeCandidate, build_candidates


# --- fakes: no test in this file ever touches the real Alpaca API ---


class FakeAccount:
    def __init__(
        self, equity=100_000.0, cash=50_000.0, buying_power=50_000.0,
        account_number="PA3XXXXXXX", status="ACTIVE", long_market_value=0.0, last_equity=100_000.0,
    ):
        self.equity = equity
        self.cash = cash
        self.buying_power = buying_power
        self.account_number = account_number
        self.status = status
        self.long_market_value = long_market_value
        self.last_equity = last_equity


class FakePosition:
    def __init__(self, symbol, qty, avg_entry_price, current_price, market_value, unrealized_pl, unrealized_plpc):
        self.symbol = symbol
        self.qty = qty
        self.avg_entry_price = avg_entry_price
        self.current_price = current_price
        self.market_value = market_value
        self.unrealized_pl = unrealized_pl
        self.unrealized_plpc = unrealized_plpc


class FakeOrder:
    def __init__(self, id, symbol, status, filled_qty=None, filled_avg_price=None, submitted_at=None, filled_at=None):
        self.id = id
        self.symbol = symbol
        self.status = status
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.submitted_at = submitted_at
        self.filled_at = filled_at


class FakeTradingClient:
    """Stands in for alpaca.trading.client.TradingClient. Records every
    submit_order() call so tests can assert dry-run makes none."""

    def __init__(self, account=None, positions=None, open_orders=None, base_url=BaseURL.TRADING_PAPER):
        self._base_url = base_url
        self._account = account or FakeAccount()
        self._positions = positions or []
        self._open_orders = open_orders or []
        self._orders_by_id = {}
        self.submitted_orders = []

    def get_account(self):
        return self._account

    def get_all_positions(self):
        return self._positions

    def get_orders(self, filter=None):
        return self._open_orders

    def submit_order(self, order_data):
        self.submitted_orders.append(order_data)
        order_id = f"fake-order-{len(self.submitted_orders)}"
        order = FakeOrder(
            id=order_id, symbol=order_data.symbol, status=OrderStatus.ACCEPTED,
            submitted_at="2026-08-10T14:30:00Z",
        )
        self._orders_by_id[order_id] = order
        return order

    def get_order_by_id(self, order_id):
        return self._orders_by_id[order_id]

    def register_order(self, order_id, order):
        self._orders_by_id[order_id] = order


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def strong_indicator_result(ticker, close=150.0):
    """Trend+momentum+volume all confirmed -> stage=volume, score=100.
    Clearly satisfies the Phase 3 entry gate (DEFAULT_RULES: stage>=trend, score>=70)."""
    return IndicatorResult(
        ticker=ticker, ok=True, latest_date="2026-08-07", close=close,
        rsi=60.0, macd=2.0, macd_signal=1.0, macd_hist=1.0,
        bb_lower=140.0, bb_mid=145.0, bb_upper=150.0,
        adx=30.0, sma_50=close - 10, volume=2_000_000.0, volume_avg_20=1_000_000.0, volume_ratio=2.0,
    )


def weak_indicator_result(ticker, close=100.0):
    """Nothing confirmed -> stage=none, score=0. Clearly satisfies the
    Phase 3 exit gate (DEFAULT_RULES: stage<trend or score<=40)."""
    return IndicatorResult(
        ticker=ticker, ok=True, latest_date="2026-08-07", close=close,
        rsi=30.0, macd=0.5, macd_signal=1.0, macd_hist=-0.5,
        bb_lower=90.0, bb_mid=95.0, bb_upper=100.0,
        adx=10.0, sma_50=close + 10, volume=500_000.0, volume_avg_20=1_000_000.0, volume_ratio=0.5,
    )


def _fake_compute_indicators(strong, weak):
    def _fake(conn, ticker, source=None):
        if ticker in strong:
            return strong_indicator_result(ticker)
        if ticker in weak:
            return weak_indicator_result(ticker)
        return IndicatorResult(ticker=ticker, ok=False, reason="no test data configured")

    return _fake


def patch_indicators(strong=(), weak=()):
    """trading/signals_bridge.py imports compute_indicators_for_ticker into
    its own module namespace (`from indicators.technical import ...`), so
    patching indicators.technical directly wouldn't affect the already-bound
    name there - patch signals_bridge's copy."""
    strong, weak = set(strong), set(weak)
    return patch("trading.signals_bridge.compute_indicators_for_ticker", side_effect=_fake_compute_indicators(strong, weak))


def patch_portfolio_indicators(strong=(), weak=()):
    """Same as patch_indicators, but for trading/portfolio.py's own
    independently-bound import (used when building per-position signal
    score/stage for the dashboard)."""
    strong, weak = set(strong), set(weak)
    return patch("trading.portfolio.compute_indicators_for_ticker", side_effect=_fake_compute_indicators(strong, weak))


# --- paper environment verification (fail-closed) ---


def test_verify_paper_environment_passes_for_paper_account():
    client = FakeTradingClient(account=FakeAccount(account_number="PA3ABCDEFG", status="ACTIVE"))
    account = verify_paper_environment(client)
    assert account.account_number == "PA3ABCDEFG"


def test_verify_paper_environment_fails_closed_for_non_paper_base_url():
    client = FakeTradingClient(base_url=BaseURL.TRADING_LIVE)
    with pytest.raises(PaperEnvironmentUnconfirmedError):
        verify_paper_environment(client)


def test_verify_paper_environment_fails_closed_for_non_paper_account_number():
    client = FakeTradingClient(account=FakeAccount(account_number="12345678"))  # no "PA" prefix
    with pytest.raises(PaperEnvironmentUnconfirmedError):
        verify_paper_environment(client)


def test_verify_paper_environment_fails_closed_when_account_fetch_errors():
    client = FakeTradingClient()

    def _boom():
        raise ConnectionError("simulated network failure")

    client.get_account = _boom
    with pytest.raises(PaperEnvironmentUnconfirmedError):
        verify_paper_environment(client)


# --- candidate building (entry/exit gates) ---


def test_qualifying_entry_produces_candidate():
    conn = make_test_db()
    with patch_indicators(strong=["AAPL"]):
        candidates = build_candidates(conn, ["AAPL"], position_tickers=set(), open_order_tickers=set(), position_qty_by_ticker={})
    assert len(candidates) == 1
    assert candidates[0].intent == INTENT_ENTRY
    assert candidates[0].side == "buy"


def test_qualifying_exit_produces_candidate():
    conn = make_test_db()
    with patch_indicators(weak=["AAPL"]):
        candidates = build_candidates(
            conn, ["AAPL"], position_tickers={"AAPL"}, open_order_tickers=set(),
            position_qty_by_ticker={"AAPL": 10.0},
        )
    assert len(candidates) == 1
    assert candidates[0].intent == INTENT_EXIT
    assert candidates[0].side == "sell"
    assert candidates[0].existing_qty == 10.0


def test_non_watchlist_ticker_cannot_create_entry_candidate():
    """A ticker outside entry_watchlist never produces an entry candidate,
    even with a strongly-qualifying signal and no existing position."""
    conn = make_test_db()
    with patch_indicators(strong=["ZZZZ"]):
        candidates = build_candidates(
            conn, ["ZZZZ"], position_tickers=set(), open_order_tickers=set(),
            position_qty_by_ticker={}, entry_watchlist={"AAPL", "MSFT"},
        )
    assert candidates == []


def test_existing_position_in_delisted_ticker_can_still_exit():
    """A ticker no longer in entry_watchlist, but still held (e.g. removed
    from the configured watchlist after the position was opened), still
    produces its normal signal-driven exit candidate."""
    conn = make_test_db()
    with patch_indicators(weak=["ZZZZ"]):
        candidates = build_candidates(
            conn, ["ZZZZ"], position_tickers={"ZZZZ"}, open_order_tickers=set(),
            position_qty_by_ticker={"ZZZZ": 7.0}, entry_watchlist={"AAPL", "MSFT"},
        )
    assert len(candidates) == 1
    assert candidates[0].intent == INTENT_EXIT
    assert candidates[0].ticker == "ZZZZ"
    assert candidates[0].existing_qty == 7.0


def test_evaluate_risk_does_not_gate_exit_on_watchlist_membership():
    """trading/risk.py's ticker_in_watchlist check must only ever apply to
    entries - an exit for a ticker outside `watchlist` should still pass
    (assuming it holds a position) rather than being rejected."""
    conn = make_test_db()
    candidate = TradeCandidate(
        ticker="ZZZZ", intent=INTENT_EXIT, side="sell", score=0.0, stage="none",
        reference_price=10.0, data_date="2026-08-07", source="alpaca", reason="test",
        existing_qty=7.0,
    )
    with patch_indicators(weak=["ZZZZ"]):
        result = trading_risk.evaluate_risk(
            conn, candidate, _risk_inputs(), position_tickers={"ZZZZ"}, open_order_tickers=set(),
            long_market_value=700.0, open_position_count=1, paper_account_confirmed=True,
            watchlist=["AAPL", "MSFT"],  # "ZZZZ" deliberately absent
        )
    assert result.passed
    assert "ticker_in_watchlist" not in result.checks


def test_engine_still_proposes_exit_for_position_in_ticker_removed_from_watchlist():
    """End-to-end: run_cycle is called with a configured watchlist that no
    longer includes "ZZZZ", but the paper account still holds an open ZZZZ
    position with a signal that now qualifies for exit. The exit must still
    be proposed - the position must not be orphaned."""
    conn = make_test_db()
    positions = [FakePosition("ZZZZ", qty=7.0, avg_entry_price=120.0, current_price=100.0,
                               market_value=700.0, unrealized_pl=-140.0, unrealized_plpc=-0.1667)]
    client = FakeTradingClient(account=FakeAccount(equity=100_000.0, long_market_value=700.0), positions=positions)

    with patch_indicators(weak=["ZZZZ"]):
        result = run_cycle(conn, client=client, tickers=["AAPL", "MSFT"], mode=MODE_DRY_RUN)

    assert result.proposed_count == 1
    action = result.actions[0]
    assert action.candidate.ticker == "ZZZZ"
    assert action.candidate.intent == INTENT_EXIT
    assert action.outcome == "proposed"
    assert action.risk.passed


def test_no_duplicate_entry_candidate_with_existing_position():
    """A ticker already held never produces a second entry candidate, even
    if its signal still qualifies for entry - the position-existence check
    happens before the entry gate is even considered."""
    conn = make_test_db()
    with patch_indicators(strong=["AAPL"]):
        candidates = build_candidates(
            conn, ["AAPL"], position_tickers={"AAPL"}, open_order_tickers=set(),
            position_qty_by_ticker={"AAPL": 5.0},
        )
    assert all(c.intent != INTENT_ENTRY for c in candidates)


def test_no_candidate_for_ticker_with_pending_order():
    conn = make_test_db()
    with patch_indicators(strong=["AAPL"], weak=["MSFT"]):
        candidates = build_candidates(
            conn, ["AAPL", "MSFT"], position_tickers={"MSFT"}, open_order_tickers={"AAPL", "MSFT"},
            position_qty_by_ticker={"MSFT": 5.0},
        )
    assert candidates == []


# --- risk checks ---


def _risk_inputs(equity=100_000.0, buying_power=50_000.0, long_market_value=0.0):
    account = FakeAccount(equity=equity, buying_power=buying_power, long_market_value=long_market_value)
    return account


def test_position_sizing_is_ten_percent_of_equity():
    conn = make_test_db()
    candidate = TradeCandidate(
        ticker="AAPL", intent=INTENT_ENTRY, side="buy", score=100.0, stage="volume",
        reference_price=150.0, data_date="2026-08-07", source="alpaca", reason="test",
    )
    with patch_indicators(strong=["AAPL"]):
        result = trading_risk.evaluate_risk(
            conn, candidate, _risk_inputs(equity=100_000.0), position_tickers=set(),
            open_order_tickers=set(), long_market_value=0.0, open_position_count=0,
            paper_account_confirmed=True, watchlist=["AAPL"],
        )
    assert result.passed
    assert result.sized_notional == pytest.approx(10_000.0)


def test_max_exposure_rejects_when_new_position_would_breach_limit():
    conn = make_test_db()
    candidate = TradeCandidate(
        ticker="AAPL", intent=INTENT_ENTRY, side="buy", score=100.0, stage="volume",
        reference_price=150.0, data_date="2026-08-07", source="alpaca", reason="test",
    )
    # 55% already invested; +10% would push to 65%, over the 60% cap.
    with patch_indicators(strong=["AAPL"]):
        result = trading_risk.evaluate_risk(
            conn, candidate, _risk_inputs(equity=100_000.0, long_market_value=55_000.0),
            position_tickers=set(), open_order_tickers=set(), long_market_value=55_000.0,
            open_position_count=3, paper_account_confirmed=True, watchlist=["AAPL"],
        )
    assert not result.passed
    assert result.checks["exposure_limit"] is False
    assert "exposure_limit" in result.reason


def test_max_open_positions_rejects_new_entry_at_cap():
    conn = make_test_db()
    candidate = TradeCandidate(
        ticker="AAPL", intent=INTENT_ENTRY, side="buy", score=100.0, stage="volume",
        reference_price=150.0, data_date="2026-08-07", source="alpaca", reason="test",
    )
    risk_config = RiskConfig(max_open_positions=6)
    with patch_indicators(strong=["AAPL"]):
        result = trading_risk.evaluate_risk(
            conn, candidate, _risk_inputs(equity=100_000.0), position_tickers=set(),
            open_order_tickers=set(), long_market_value=0.0, open_position_count=6,
            paper_account_confirmed=True, watchlist=["AAPL"], risk_config=risk_config,
        )
    assert not result.passed
    assert result.checks["max_open_positions"] is False


def test_insufficient_buying_power_rejects_entry():
    conn = make_test_db()
    candidate = TradeCandidate(
        ticker="AAPL", intent=INTENT_ENTRY, side="buy", score=100.0, stage="volume",
        reference_price=150.0, data_date="2026-08-07", source="alpaca", reason="test",
    )
    with patch_indicators(strong=["AAPL"]):
        result = trading_risk.evaluate_risk(
            conn, candidate, _risk_inputs(equity=100_000.0, buying_power=100.0),
            position_tickers=set(), open_order_tickers=set(), long_market_value=0.0,
            open_position_count=0, paper_account_confirmed=True, watchlist=["AAPL"],
        )
    assert not result.passed
    assert result.checks["buying_power_sufficient"] is False


def test_non_watchlist_ticker_is_rejected():
    conn = make_test_db()
    candidate = TradeCandidate(
        ticker="ZZZZ", intent=INTENT_ENTRY, side="buy", score=100.0, stage="volume",
        reference_price=10.0, data_date="2026-08-07", source="alpaca", reason="test",
    )
    with patch_indicators(strong=["ZZZZ"]):
        result = trading_risk.evaluate_risk(
            conn, candidate, _risk_inputs(), position_tickers=set(), open_order_tickers=set(),
            long_market_value=0.0, open_position_count=0, paper_account_confirmed=True,
            watchlist=["AAPL", "MSFT"],
        )
    assert not result.passed
    assert result.checks["ticker_in_watchlist"] is False


# --- engine: end-to-end cycles ---


def test_dry_run_makes_zero_order_submissions():
    conn = make_test_db()
    client = FakeTradingClient()
    with patch_indicators(strong=["AAPL"]):
        result = run_cycle(conn, client=client, tickers=["AAPL"], mode=MODE_DRY_RUN)
    assert client.submitted_orders == []
    assert result.proposed_count == 1
    assert result.submitted_count == 0


def test_paper_send_submits_qualifying_order_and_persists_it():
    conn = make_test_db()
    client = FakeTradingClient()
    with patch_indicators(strong=["AAPL"]):
        result = run_cycle(conn, client=client, tickers=["AAPL"], mode=MODE_PAPER_SEND)

    assert len(client.submitted_orders) == 1
    assert client.submitted_orders[0].symbol == "AAPL"
    assert result.submitted_count == 1

    client_order_id = build_client_order_id("AAPL", INTENT_ENTRY)
    row = get_order_by_client_id(conn, client_order_id)
    assert row is not None
    assert row["status"] == "accepted"
    assert row["alpaca_order_id"] is not None
    assert row["notional"] == pytest.approx(10_000.0)
    assert row["environment"] == "paper"

    history = load_order_history(conn)
    assert len(history) == 1


def test_idempotent_repeated_evaluation_does_not_resubmit():
    conn = make_test_db()
    client = FakeTradingClient()
    with patch_indicators(strong=["AAPL"]):
        run_cycle(conn, client=client, tickers=["AAPL"], mode=MODE_PAPER_SEND)
        second = run_cycle(conn, client=client, tickers=["AAPL"], mode=MODE_PAPER_SEND)

    assert len(client.submitted_orders) == 1  # not 2
    assert second.actions[0].outcome == "duplicate_skipped"
    assert len(load_order_history(conn)) == 1


def test_dry_run_proposal_can_be_promoted_to_paper_send_without_duplicate():
    """A --dry-run cycle never sends anything to Alpaca, so its 'proposed'
    row must not permanently block a later --paper-send run from actually
    submitting that same candidate. Promotion must update the existing row
    in place (same client_order_id) rather than create a second one."""
    conn = make_test_db()
    client = FakeTradingClient()

    with patch_indicators(strong=["AAPL"]):
        dry_run_result = run_cycle(conn, client=client, tickers=["AAPL"], mode=MODE_DRY_RUN)
        assert dry_run_result.actions[0].outcome == "proposed"
        assert client.submitted_orders == []

        dry_run_client_order_id = build_client_order_id("AAPL", INTENT_ENTRY)
        proposed_row = get_order_by_client_id(conn, dry_run_client_order_id)
        assert proposed_row["status"] == "proposed"
        assert proposed_row["alpaca_order_id"] is None

        paper_send_result = run_cycle(conn, client=client, tickers=["AAPL"], mode=MODE_PAPER_SEND)

    assert paper_send_result.actions[0].outcome == "submitted"
    assert len(client.submitted_orders) == 1

    promoted_row = get_order_by_client_id(conn, dry_run_client_order_id)
    assert promoted_row["id"] == proposed_row["id"]                 # same logical order, not a new row
    assert promoted_row["client_order_id"] == dry_run_client_order_id
    assert promoted_row["status"] == "accepted"
    assert promoted_row["alpaca_order_id"] is not None

    # Exactly one row for this ticker/day - promotion updated in place.
    history = load_order_history(conn, ticker="AAPL")
    assert len(history) == 1


def test_already_submitted_order_is_never_resubmitted_after_reconciliation():
    """Once a client_order_id has genuinely reached Alpaca (submitted, then
    reconciled to filled), no later run - dry-run or paper-send - may ever
    submit it again."""
    conn = make_test_db()
    client = FakeTradingClient()

    with patch_indicators(strong=["AAPL"]):
        run_cycle(conn, client=client, tickers=["AAPL"], mode=MODE_PAPER_SEND)
        client_order_id = build_client_order_id("AAPL", INTENT_ENTRY)
        alpaca_order_id = get_order_by_client_id(conn, client_order_id)["alpaca_order_id"]

        client.register_order(alpaca_order_id, FakeOrder(
            id=alpaca_order_id, symbol="AAPL", status=OrderStatus.FILLED,
            filled_qty=35.9, filled_avg_price=278.09, filled_at="2026-08-10T14:32:00Z",
        ))

        again_dry_run = run_cycle(conn, client=client, tickers=["AAPL"], mode=MODE_DRY_RUN)
        again_paper_send = run_cycle(conn, client=client, tickers=["AAPL"], mode=MODE_PAPER_SEND)

    assert len(client.submitted_orders) == 1  # still just the original submission
    assert again_dry_run.actions[0].outcome == "duplicate_skipped"
    assert again_paper_send.actions[0].outcome == "duplicate_skipped"
    assert get_order_by_client_id(conn, client_order_id)["status"] == "filled"


def test_live_endpoint_fails_closed_in_run_cycle():
    conn = make_test_db()
    client = FakeTradingClient(base_url=BaseURL.TRADING_LIVE)
    with pytest.raises(PaperEnvironmentUnconfirmedError):
        run_cycle(conn, client=client, tickers=["AAPL"], mode=MODE_DRY_RUN)
    assert client.submitted_orders == []


# --- reconciliation ---


def test_reconciliation_updates_local_status_from_alpaca_fill():
    conn = make_test_db()
    client_order_id = build_client_order_id("AAPL", INTENT_ENTRY)
    order_id = upsert_order(
        conn, ticker="AAPL", side="buy", intent=INTENT_ENTRY, reason="test",
        client_order_id=client_order_id, status="submitted", notional=10_000.0,
        alpaca_order_id="alpaca-1",
    )
    client = FakeTradingClient()
    client.register_order("alpaca-1", FakeOrder(
        id="alpaca-1", symbol="AAPL", status=OrderStatus.FILLED,
        filled_qty=66.5, filled_avg_price=150.32, filled_at="2026-08-10T14:31:00Z",
    ))

    reconcile_open_orders(conn, client)

    row = get_order_by_client_id(conn, client_order_id)
    assert row["status"] == "filled"
    assert row["filled_qty"] == pytest.approx(66.5)
    assert row["filled_avg_price"] == pytest.approx(150.32)


def test_reconciliation_maps_rejected_and_canceled_statuses():
    conn = make_test_db()
    rejected_client_order_id = build_client_order_id("AAPL", INTENT_ENTRY)
    canceled_client_order_id = build_client_order_id("MSFT", INTENT_ENTRY)
    upsert_order(conn, ticker="AAPL", side="buy", intent=INTENT_ENTRY, reason="test",
                 client_order_id=rejected_client_order_id, status="submitted", alpaca_order_id="a-rejected")
    upsert_order(conn, ticker="MSFT", side="buy", intent=INTENT_ENTRY, reason="test",
                 client_order_id=canceled_client_order_id, status="submitted", alpaca_order_id="a-canceled")

    client = FakeTradingClient()
    client.register_order("a-rejected", FakeOrder(id="a-rejected", symbol="AAPL", status=OrderStatus.REJECTED))
    client.register_order("a-canceled", FakeOrder(id="a-canceled", symbol="MSFT", status=OrderStatus.CANCELED))

    reconcile_open_orders(conn, client)

    assert get_order_by_client_id(conn, rejected_client_order_id)["status"] == "rejected"
    assert get_order_by_client_id(conn, canceled_client_order_id)["status"] == "canceled"


# --- portfolio analytics / snapshots ---


def test_portfolio_snapshot_persistence_and_calculations():
    conn = make_test_db()
    positions = [FakePosition("AAPL", qty=10, avg_entry_price=140.0, current_price=150.0,
                               market_value=1500.0, unrealized_pl=100.0, unrealized_plpc=0.0714)]
    client = FakeTradingClient(account=FakeAccount(equity=10_000.0, cash=8_500.0, buying_power=8_500.0),
                                positions=positions)

    snapshot_id = trading_portfolio.capture_snapshot(conn, client)
    assert snapshot_id is not None

    snapshots = load_portfolio_snapshots(conn)
    assert len(snapshots) == 1
    row = snapshots.iloc[0]
    assert row["equity"] == pytest.approx(10_000.0)
    assert row["long_market_value"] == pytest.approx(1500.0)
    assert row["invested_exposure_pct"] == pytest.approx(0.15)
    assert row["unrealized_pl"] == pytest.approx(100.0)
    assert row["open_position_count"] == 1
    assert row["environment"] == "paper"


def test_realized_pnl_and_win_loss_from_matched_filled_orders():
    conn = make_test_db()
    # AAPL: entry filled at 100, exit filled at 110, qty 10 -> +100 realized, a win.
    upsert_order(conn, ticker="AAPL", side="buy", intent=INTENT_ENTRY, reason="t",
                 client_order_id="aapl-entry", status="submitted")
    from db.trading_repository import update_order_status
    entry_row = get_order_by_client_id(conn, "aapl-entry")
    update_order_status(conn, entry_row["id"], status="filled", filled_qty=10.0, filled_avg_price=100.0, filled_at="2026-08-01T14:30:00")

    upsert_order(conn, ticker="AAPL", side="sell", intent=INTENT_EXIT, reason="t",
                 client_order_id="aapl-exit", status="submitted")
    exit_row = get_order_by_client_id(conn, "aapl-exit")
    update_order_status(conn, exit_row["id"], status="filled", filled_qty=10.0, filled_avg_price=110.0, filled_at="2026-08-05T14:30:00")

    trades = trading_portfolio.compute_closed_trades(conn)
    assert len(trades) == 1
    assert trades[0].realized_pnl == pytest.approx(100.0)

    assert trading_portfolio.compute_realized_pnl(conn) == pytest.approx(100.0)
    win_loss = trading_portfolio.compute_win_loss_summary(conn)
    assert win_loss == {"completed_trades": 1, "wins": 1, "losses": 0, "flat": 0}


# --- dashboard data helpers ---


def test_dashboard_paper_portfolio_helper_reports_unavailable_on_verification_failure():
    from dashboard.data import _load_paper_portfolio

    conn = make_test_db()
    with patch("trading.client.get_client", return_value=FakeTradingClient(base_url=BaseURL.TRADING_LIVE)):
        result = _load_paper_portfolio(conn)
    assert result.ok is False
    assert "paper" in result.reason.lower() or "confirm" in result.reason.lower()


def test_dashboard_paper_portfolio_helper_returns_summary_when_available():
    from dashboard.data import _load_paper_portfolio

    conn = make_test_db()
    positions = [FakePosition("AAPL", qty=5, avg_entry_price=100.0, current_price=110.0,
                               market_value=550.0, unrealized_pl=50.0, unrealized_plpc=0.10)]
    client = FakeTradingClient(account=FakeAccount(equity=10_000.0), positions=positions)

    with patch("trading.client.get_client", return_value=client), patch_portfolio_indicators(strong=["AAPL"]):
        result = _load_paper_portfolio(conn)

    assert result.ok is True
    assert result.summary.equity == pytest.approx(10_000.0)
    assert len(result.positions) == 1
    assert result.positions[0].ticker == "AAPL"
    assert result.positions[0].signal_score == pytest.approx(100.0)
