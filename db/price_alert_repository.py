"""Read/write interface for the price-threshold alert log and per-ticker
price state. Structurally separate from db/alert_repository.py - see
db/price_alerts_schema.py's price_alerts / price_alert_state tables (kept
out of db/schema.py - see that module's docstring).

`price_alerts` is an event log (one row per triggered alert, dry-run or
real). `price_alert_state` is a single row per ticker holding the last-
observed price - updated on every evaluation, which is what lets
alerts/price_engine.py detect a genuine crossing instead of just "is
currently above/below".

Every function here calls ensure_price_alerts_schema(conn) first - same
lazy, on-first-use convention as strategy_lab/prospective.py's
ensure_schema(), since these tables are never created by db/schema.py's
init_db()/db/database.py's db_session().
"""
import pandas as pd

from db.price_alerts_schema import ensure_price_alerts_schema

PRICE_ALERT_COLUMNS = [
    "id", "ticker", "triggered_at", "alert_type", "price", "previous_price",
    "threshold", "data_date", "source", "message", "dry_run", "delivered",
    "delivered_at", "delivery_error", "discord_delivered", "discord_delivered_at",
    "discord_delivery_error", "suppressed_reason",
]


def load_price_alert_history(conn, ticker=None, limit=200):
    """Most-recent-first price-alert log, optionally filtered to one ticker."""
    ensure_price_alerts_schema(conn)
    columns_sql = ", ".join(PRICE_ALERT_COLUMNS)
    if ticker:
        query = f"SELECT {columns_sql} FROM price_alerts WHERE ticker = ? ORDER BY triggered_at DESC LIMIT ?"
        params = (ticker, limit)
    else:
        query = f"SELECT {columns_sql} FROM price_alerts ORDER BY triggered_at DESC LIMIT ?"
        params = (limit,)
    return pd.read_sql_query(query, conn, params=params)


def record_price_alert(
    conn,
    ticker,
    alert_type,
    price,
    previous_price,
    threshold,
    data_date,
    source,
    message,
    dry_run=True,
):
    """Insert a new price-alert event. Called for both dry-run and real-send
    cycles - dry_run marks which one this was. Never pass SMTP credential
    values here; nothing in this signature accepts them."""
    ensure_price_alerts_schema(conn)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO price_alerts (
            ticker, alert_type, price, previous_price, threshold,
            data_date, source, message, dry_run
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker, alert_type, price, previous_price, threshold,
            data_date, source, message, int(dry_run),
        ),
    )
    conn.commit()
    return cur.lastrowid


def mark_delivered(conn, alert_id):
    """Mark a price alert as successfully delivered by email."""
    ensure_price_alerts_schema(conn)
    cur = conn.cursor()
    cur.execute(
        "UPDATE price_alerts SET delivered = 1, delivered_at = datetime('now'), delivery_error = NULL WHERE id = ?",
        (alert_id,),
    )
    conn.commit()


def mark_delivery_failed(conn, alert_id, error):
    """Mark a price-alert delivery attempt as failed - visible as a failure
    in Alert History, not silently dropped or shown as delivered."""
    ensure_price_alerts_schema(conn)
    cur = conn.cursor()
    cur.execute(
        "UPDATE price_alerts SET delivered = 0, delivery_error = ? WHERE id = ?",
        (str(error), alert_id),
    )
    conn.commit()


def mark_discord_delivered(conn, alert_id):
    """Mark a price alert's Discord delivery as successful. Tracked in its
    own discord_* columns, independent of mark_delivered's email columns -
    the two channels fire independently and either can succeed/fail on its
    own."""
    ensure_price_alerts_schema(conn)
    cur = conn.cursor()
    cur.execute(
        "UPDATE price_alerts SET discord_delivered = 1, discord_delivered_at = datetime('now'), discord_delivery_error = NULL WHERE id = ?",
        (alert_id,),
    )
    conn.commit()


def mark_discord_delivery_failed(conn, alert_id, error):
    """Mark a price-alert Discord delivery attempt as failed - visible as a
    failure in Alert History, not silently dropped or shown as delivered."""
    ensure_price_alerts_schema(conn)
    cur = conn.cursor()
    cur.execute(
        "UPDATE price_alerts SET discord_delivered = 0, discord_delivery_error = ? WHERE id = ?",
        (str(error), alert_id),
    )
    conn.commit()


def mark_suppressed_by_snooze(conn, alert_id, snoozed_until):
    """Mark a fired price alert as delivery-suppressed because its ticker
    (or all tickers) was snoozed at evaluation time - both channels are
    marked undelivered with a shared, human-readable suppressed_reason
    rather than either being left looking like a plain dry-run or a
    delivery failure. See alerts/price_runner.py: this is only ever called
    in place of the send_email_alert/send_discord_alert calls, never after
    them - a snoozed alert never actually attempts real delivery."""
    ensure_price_alerts_schema(conn)
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE price_alerts SET
            delivered = 0, delivered_at = NULL, delivery_error = NULL,
            discord_delivered = 0, discord_delivered_at = NULL, discord_delivery_error = NULL,
            suppressed_reason = ?
        WHERE id = ?
        """,
        (f"snoozed until {snoozed_until}", alert_id),
    )
    conn.commit()


def get_price_alert_state(conn, ticker):
    """The last-observed price for this ticker, or None if it has never
    been evaluated before."""
    ensure_price_alerts_schema(conn)
    row = conn.execute(
        "SELECT ticker, last_price, last_checked_at, last_alert_at FROM price_alert_state WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    return row


def upsert_price_alert_state(conn, ticker, price, alerted=False):
    """Record the latest observed price for a ticker. Always called after
    an evaluation, whether or not an alert fired - this is the baseline the
    *next* evaluation compares against. `alerted=True` also stamps
    last_alert_at, for cooldown tracking."""
    ensure_price_alerts_schema(conn)
    cur = conn.cursor()
    if alerted:
        cur.execute(
            """
            INSERT INTO price_alert_state (ticker, last_price, last_checked_at, last_alert_at)
            VALUES (?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(ticker) DO UPDATE SET
                last_price=excluded.last_price,
                last_checked_at=excluded.last_checked_at,
                last_alert_at=excluded.last_alert_at
            """,
            (ticker, price),
        )
    else:
        cur.execute(
            """
            INSERT INTO price_alert_state (ticker, last_price, last_checked_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(ticker) DO UPDATE SET
                last_price=excluded.last_price,
                last_checked_at=excluded.last_checked_at
            """,
            (ticker, price),
        )
    conn.commit()
