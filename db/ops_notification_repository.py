"""Dedupe store for the (disabled-by-default) operational-failure Discord
notification - see alerts/ops_notifications.py. Structurally separate from
db/alert_repository.py (stock-signal alerts)."""
from datetime import date


def already_sent_today(conn, trading_date: date) -> bool:
    row = conn.execute(
        "SELECT 1 FROM operational_notifications WHERE trading_date = ?",
        (trading_date.isoformat(),),
    ).fetchone()
    return row is not None


def record_sent(conn, trading_date: date, error_summary: str):
    conn.execute(
        "INSERT OR IGNORE INTO operational_notifications (trading_date, error_summary) VALUES (?, ?)",
        (trading_date.isoformat(), error_summary),
    )
    conn.commit()
