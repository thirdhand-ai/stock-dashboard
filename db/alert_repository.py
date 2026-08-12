"""Read/write interface for the local alert log and per-ticker alert state.

`alerts` is an event log (one row per triggered alert, dry-run or real).
`alert_state` is a single row per ticker holding the last-observed score/
stage - updated on every evaluation, which is what lets alerts/engine.py
detect a genuine crossing/advancement instead of just "is currently above".
"""
import pandas as pd

ALERT_COLUMNS = [
    "id", "ticker", "triggered_at", "alert_type", "score", "previous_score",
    "highest_confirmed_stage", "previous_stage", "data_date", "source",
    "message", "dry_run", "delivered", "delivered_at", "delivery_error",
]


def load_alert_history(conn, ticker=None, limit=200):
    """Most-recent-first alert log, optionally filtered to one ticker."""
    columns_sql = ", ".join(ALERT_COLUMNS)
    if ticker:
        query = f"SELECT {columns_sql} FROM alerts WHERE ticker = ? ORDER BY triggered_at DESC LIMIT ?"
        params = (ticker, limit)
    else:
        query = f"SELECT {columns_sql} FROM alerts ORDER BY triggered_at DESC LIMIT ?"
        params = (limit,)
    return pd.read_sql_query(query, conn, params=params)


def record_alert(
    conn,
    ticker,
    alert_type,
    score,
    previous_score,
    highest_confirmed_stage,
    previous_stage,
    data_date,
    source,
    message,
    dry_run=True,
):
    """Insert a new alert event. Called for both dry-run and real-send
    cycles - dry_run marks which one this was. Never pass webhook/credential
    values here; nothing in this signature accepts them."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO alerts (
            ticker, alert_type, score, previous_score, highest_confirmed_stage,
            previous_stage, data_date, source, message, dry_run
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker, alert_type, score, previous_score, highest_confirmed_stage,
            previous_stage, data_date, source, message, int(dry_run),
        ),
    )
    conn.commit()
    return cur.lastrowid


def record_state_correction(conn, ticker, field, old_value, new_value, reason):
    """Append-only audit trail entry for a manual/scripted alert_state
    investigation or correction. old_value == new_value documents a
    "verified, no change needed" finding, not a no-op call - always record
    the investigation outcome, whichever way it goes."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO alert_state_corrections (ticker, field, old_value, new_value, reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ticker, field, str(old_value) if old_value is not None else None,
         str(new_value) if new_value is not None else None, reason),
    )
    conn.commit()
    return cur.lastrowid


def load_state_corrections(conn, ticker=None):
    query = "SELECT id, corrected_at, ticker, field, old_value, new_value, reason FROM alert_state_corrections"
    params = ()
    if ticker:
        query += " WHERE ticker = ?"
        params = (ticker,)
    query += " ORDER BY corrected_at DESC"
    return pd.read_sql_query(query, conn, params=params)


def mark_delivered(conn, alert_id):
    """Mark an alert as successfully delivered to Discord."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE alerts SET delivered = 1, delivered_at = datetime('now'), delivery_error = NULL WHERE id = ?",
        (alert_id,),
    )
    conn.commit()


def mark_delivery_failed(conn, alert_id, error):
    """Mark an alert delivery attempt as failed - visible as a failure in
    Alert History, not silently dropped or shown as delivered."""
    cur = conn.cursor()
    cur.execute(
        "UPDATE alerts SET delivered = 0, delivery_error = ? WHERE id = ?",
        (str(error), alert_id),
    )
    conn.commit()


def get_alert_state(conn, ticker):
    """The last-observed score/stage for this ticker, or None if it has
    never been evaluated before."""
    row = conn.execute(
        "SELECT ticker, last_score, last_stage, last_checked_at, last_alert_at FROM alert_state WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    return row


def upsert_alert_state(conn, ticker, score, stage, alerted=False):
    """Record the latest observed score/stage for a ticker. Always called
    after an evaluation, whether or not an alert fired - this is the
    baseline the *next* evaluation compares against. `alerted=True` also
    stamps last_alert_at, for cooldown tracking."""
    cur = conn.cursor()
    if alerted:
        cur.execute(
            """
            INSERT INTO alert_state (ticker, last_score, last_stage, last_checked_at, last_alert_at)
            VALUES (?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(ticker) DO UPDATE SET
                last_score=excluded.last_score,
                last_stage=excluded.last_stage,
                last_checked_at=excluded.last_checked_at,
                last_alert_at=excluded.last_alert_at
            """,
            (ticker, score, stage),
        )
    else:
        cur.execute(
            """
            INSERT INTO alert_state (ticker, last_score, last_stage, last_checked_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(ticker) DO UPDATE SET
                last_score=excluded.last_score,
                last_stage=excluded.last_stage,
                last_checked_at=excluded.last_checked_at
            """,
            (ticker, score, stage),
        )
    conn.commit()
