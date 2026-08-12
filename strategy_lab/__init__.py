"""Phase 9: large-sample strategy validation / research backtesting.

Structurally isolated from trading/automation/alerts, the same way research/
(Phase 8) is: nothing in this package places, modifies, or cancels an Alpaca
order, calls Discord, or writes to alert_state/paper_orders. See
tests/test_strategy_lab_safety.py for the enforcement tests.

This package answers one question - "does the existing production technical
signal actually predict forward returns across a large sample?" - without
changing the production strategy (signals/, backtest/config.py's
DEFAULT_RULES) or connecting research findings to trade execution.
"""
