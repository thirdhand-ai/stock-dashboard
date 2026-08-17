"""Read/write interface for the volatility-alert log and per-ticker
volatility-alert state. Structurally mirrors db/price_alert_repository.py,
adapted to the day-over-day move signal (see db/volatility_alerts_schema.py's
docstring for why this is a separate table family from price_alerts/
price_alert_state rather than a shared one).

`volatility_alerts` is an event log (one row per triggered alert, dry-run or
real). `volatility_alert_state` is a single row per ticker holding
`last_alert_data_date` (the trading day whose move last fired an alert - the
primary de-dupe: a given day's move only ever fires once, however many times
the evaluation is re-run) and `last_alert_at` (wall-clock time, the same
secondary cooldown protection price_alert_state uses).

Every function here calls ensure_volatility_alerts_schema(conn) first - same
lazy, on-first-use convention as db/price_alert_repository.py.
"""
import pandas as pd

from db.volatility_alerts_schema import ensure_volatility_alerts_schema

VOLATILITY_ALERT_COLUMNS = [
    "id", "ticker", "triggered_at", "move_pct", "threshold_percent", "previous_close",
    "current_close", "previous_date", "data_date", "source", "message", "dry_run",
    "delivered", "delivered_at", "delivery_error",
    "discord_delivered", "discord_delivered_at", "discord_delivery_error", "suppressed_reason",
]


def load_volatility_alert_history(conn, ticker=None, limit=200):
    """Most-recent-first volatility-alert log, optionally filtered to one ticker."""
    ensure_volatility_alerts_schema(conn)
    columns_sql = ", ".join(VOLATILITY_ALERT_COLUMNS)
    if ticker:
        query = f"SELECT {columns_sql} FROM volatility_alerts WHERE ticker = ? ORDER BY triggered_at DESC LIMIT ?"
        params = (ticker, limit)
    else:
        query = f"SELECT {columns_sql} FROM volatility_alerts ORDER BY triggered_at DESC LIMIT ?"
        params = (limit,)
    return pd.read_sql_query(query, conn, params=params)


def record_volatility_alert(
    conn,
    ticker,
    move_pct,
    threshold_percent,
    previous_close,
    current_close,
    previous_date,
    data_date,
    source,
    message,
    dry_run=True,
):
    """Insert a new volatility-alert event. Called for both dry-run and
    real-send cycles - dry_run marks which one this was."""
    ensure_volatility_alerts_schema(conn)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO volatility_alerts (
            ticker, move_pct, threshold_percent, previous_close, current_close,
            previous_date, data_date, source, message, dry_run
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker, move_pct, threshold_percent, previous_close, current_close,
            previous_date, data_date, source, message, int(dry_run),
        ),
    )
    conn.commit()
    return cur.lastrowid


def mark_delivered(conn, alert_id):
    """Mark a volatility alert as successfully delivered by email."""
    ensure_volatility_alerts_schema(conn)
    cur = conn.cursor()
    cur.execute(
        "UPDATE volatility_alerts SET delivered = 1, delivered_at = datetime('now'), delivery_error = NULL WHERE id = ?",
        (alert_id,),
    )
    conn.commit()


def mark_delivery_failed(conn, alert_id, error):
    """Mark a volatility-alert email delivery attempt as failed - visible as
    a failure in Alert History, not silently dropped."""
    ensure_volatility_alerts_schema(conn)
    cur = conn.cursor()
    cur.execute(
        "UPDATE volatility_alerts SET delivered = 0, delivery_error = ? WHERE id = ?",
        (str(error), alert_id),
    )
    conn.commit()


def mark_discord_delivered(conn, alert_id):
    """Mark a volatility alert's Discord delivery as successful. Tracked in
    its own discord_* columns, independent of mark_delivered's email
    columns."""
    ensure_volatility_alerts_schema(conn)
    cur = conn.cursor()
    cur.execute(
        "UPDATE volatility_alerts SET discord_delivered = 1, discord_delivered_at = datetime('now'), discord_delivery_error = NULL WHERE id = ?",
        (alert_id,),
    )
    conn.commit()


def mark_discord_delivery_failed(conn, alert_id, error):
    """Mark a volatility-alert Discord delivery attempt as failed - visible
    as a failure in Alert History, not silently dropped."""
    ensure_volatility_alerts_schema(conn)
    cur = conn.cursor()
    cur.execute(
        "UPDATE volatility_alerts SET discord_delivered = 0, discord_delivery_error = ? WHERE id = ?",
        (str(error), alert_id),
    )
    conn.commit()


def mark_suppressed_by_snooze(conn, alert_id, snoozed_until):
    """Mark a fired volatility alert as delivery-suppressed because its
    ticker (or all tickers) was snoozed at evaluation time - same
    convention as db/price_alert_repository.py::mark_suppressed_by_snooze.
    Only ever called in place of the send_email_alert/send_discord_alert
    calls in alerts/volatility_runner.py, never after them."""
    ensure_volatility_alerts_schema(conn)
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE volatility_alerts SET
            delivered = 0, delivered_at = NULL, delivery_error = NULL,
            discord_delivered = 0, discord_delivered_at = NULL, discord_delivery_error = NULL,
            suppressed_reason = ?
        WHERE id = ?
        """,
        (f"snoozed until {snoozed_until}", alert_id),
    )
    conn.commit()


def get_volatility_alert_state(conn, ticker):
    """The last-alert bookkeeping for this ticker, or None if it has never
    fired a volatility alert (or never been evaluated) before."""
    ensure_volatility_alerts_schema(conn)
    row = conn.execute(
        "SELECT ticker, last_checked_at, last_alert_at, last_alert_data_date FROM volatility_alert_state WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    return row


def upsert_volatility_alert_state(conn, ticker, alerted=False, data_date=None):
    """Record that this ticker was evaluated. Always called after an
    evaluation, whether or not an alert fired. `alerted=True` also stamps
    last_alert_at (wall-clock cooldown) and last_alert_data_date (the
    trading day being de-duplicated against - see this module's docstring)."""
    ensure_volatility_alerts_schema(conn)
    cur = conn.cursor()
    if alerted:
        cur.execute(
            """
            INSERT INTO volatility_alert_state (ticker, last_checked_at, last_alert_at, last_alert_data_date)
            VALUES (?, datetime('now'), datetime('now'), ?)
            ON CONFLICT(ticker) DO UPDATE SET
                last_checked_at=excluded.last_checked_at,
                last_alert_at=excluded.last_alert_at,
                last_alert_data_date=excluded.last_alert_data_date
            """,
            (ticker, data_date),
        )
    else:
        cur.execute(
            """
            INSERT INTO volatility_alert_state (ticker, last_checked_at)
            VALUES (?, datetime('now'))
            ON CONFLICT(ticker) DO UPDATE SET last_checked_at=excluded.last_checked_at
            """,
            (ticker,),
        )
    conn.commit()
