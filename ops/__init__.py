"""Phase 12 — Research operations, daily reporting, data quality, and
experiment governance.

Read-only/reporting/governance code lives here, never in `strategy_lab/`
(research-only) or `trading/` (execution-capable). Every module in this
package is importable and runnable without a Streamlit process, calls at
most the read-only Alpaca methods (`get_account`, `get_all_positions`,
`get_orders` status-filtered, `get_order_by_id`), and never imports
`trading.engine`/`trading.orders`/`trading.run_paper` or
`alerts.discord`/`alerts.runner`/`alerts.run_alerts` - see
tests/test_ops_safety.py.
"""
