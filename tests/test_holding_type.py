"""Tests for dashboard/holding_type.py - pure classification/grouping logic,
no DB, no Streamlit runtime. Monkeypatches the module's internal watchlist/
exploratory sets so these tests stay correct regardless of what
config.settings.WATCHLIST/EXPLORATORY_WATCHLIST actually contain.
"""
from dashboard import holding_type as ht


def _isolated(monkeypatch, watchlist, exploratory):
    monkeypatch.setattr(ht, "_WATCHLIST_SET", set(watchlist))
    monkeypatch.setattr(ht, "_EXPLORATORY_SET", set(exploratory))


def test_classify_ticker_real_holdings_takes_precedence_over_watchlist(monkeypatch):
    _isolated(monkeypatch, watchlist=["AAPL"], exploratory=[])
    assert ht.classify_ticker("AAPL", real_holding_tickers={"AAPL"}) == ht.HOLDING_TYPE_REAL


def test_classify_ticker_watchlist_membership(monkeypatch):
    _isolated(monkeypatch, watchlist=["MSFT"], exploratory=[])
    assert ht.classify_ticker("MSFT", real_holding_tickers=set()) == ht.HOLDING_TYPE_WATCHLIST


def test_classify_ticker_exploratory_membership(monkeypatch):
    _isolated(monkeypatch, watchlist=[], exploratory=["PLTR"])
    assert ht.classify_ticker("PLTR", real_holding_tickers=set()) == ht.HOLDING_TYPE_EXPLORATORY


def test_classify_ticker_unclassified_is_other(monkeypatch):
    _isolated(monkeypatch, watchlist=["MSFT"], exploratory=["PLTR"])
    assert ht.classify_ticker("SPY", real_holding_tickers=set()) == ht.HOLDING_TYPE_OTHER


def test_classify_ticker_real_holding_with_no_watchlist_or_exploratory_membership(monkeypatch):
    _isolated(monkeypatch, watchlist=[], exploratory=[])
    assert ht.classify_ticker("NOW", real_holding_tickers={"NOW"}) == ht.HOLDING_TYPE_REAL


def test_build_holding_type_map_covers_every_ticker_across_all_three_sources(monkeypatch):
    _isolated(monkeypatch, watchlist=["MSFT", "AAPL"], exploratory=["PLTR"])
    result = ht.build_holding_type_map(real_holding_tickers={"NOW", "AAPL"})
    assert result == {
        "NOW": ht.HOLDING_TYPE_REAL,
        "AAPL": ht.HOLDING_TYPE_REAL,   # real holding wins even though AAPL is also on the watchlist
        "MSFT": ht.HOLDING_TYPE_WATCHLIST,
        "PLTR": ht.HOLDING_TYPE_EXPLORATORY,
    }


def test_holding_type_for_no_ticker_returns_none():
    assert ht.holding_type_for(None, type_map={"AAPL": ht.HOLDING_TYPE_WATCHLIST}) is None


def test_holding_type_for_unmapped_ticker_falls_back_to_other():
    assert ht.holding_type_for("SPY", type_map={"AAPL": ht.HOLDING_TYPE_WATCHLIST}) == ht.HOLDING_TYPE_OTHER


def test_holding_type_for_mapped_ticker_returns_its_type():
    assert ht.holding_type_for("AAPL", type_map={"AAPL": ht.HOLDING_TYPE_REAL}) == ht.HOLDING_TYPE_REAL


def test_holding_type_label_includes_icon_for_each_known_type():
    for t in ht.HOLDING_TYPE_ORDER:
        label = ht.holding_type_label(t)
        assert t in label
        assert label != t  # an icon prefix was actually added


def test_holding_type_label_none_is_em_dash():
    assert ht.holding_type_label(None) == "—"
