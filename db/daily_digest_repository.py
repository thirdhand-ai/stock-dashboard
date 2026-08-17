"""Read/write interface for daily_digest_log - the send-dedup + delivery-
outcome record for the daily digest. At most one digest per trading_date:
already_sent_today()/record_sent() gate a re-run (e.g. a manual CLI retry
the same day) from sending a duplicate, same convention as
db/ops_notification_repository.py, extended here with per-channel delivery
tracking like db/price_alert_repository.py/db/volatility_alert_repository.py.
"""
import pandas as pd

from db.daily_digest_schema import ensure_daily_digest_schema

DAILY_DIGEST_LOG_COLUMNS = [
    "id", "trading_date", "sent_at", "ticker_count", "dry_run",
    "delivered", "delivered_at", "delivery_error",
    "discord_delivered", "discord_delivered_at", "discord_delivery_error",
]


def already_sent_today(conn, trading_date: str) -> bool:
    ensure_daily_digest_schema(conn)
    row = conn.execute(
        "SELECT 1 FROM daily_digest_log WHERE trading_date = ?", (trading_date,)
    ).fetchone()
    return row is not None


def record_digest_sent(conn, trading_date: str, ticker_count: int, dry_run: bool = True) -> int:
    """Insert the digest-log row for this trading_date. Called once per
    day (already_sent_today gates repeats) whether or not delivery
    ultimately succeeds - the dedup record and the delivery outcome are
    tracked independently, same as price_alerts/volatility_alerts."""
    ensure_daily_digest_schema(conn)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO daily_digest_log (trading_date, ticker_count, dry_run) VALUES (?, ?, ?)",
        (trading_date, ticker_count, int(dry_run)),
    )
    conn.commit()
    return cur.lastrowid


def mark_delivered(conn, digest_id: int) -> None:
    ensure_daily_digest_schema(conn)
    conn.execute(
        "UPDATE daily_digest_log SET delivered = 1, delivered_at = datetime('now'), delivery_error = NULL WHERE id = ?",
        (digest_id,),
    )
    conn.commit()


def mark_delivery_failed(conn, digest_id: int, error: str) -> None:
    ensure_daily_digest_schema(conn)
    conn.execute(
        "UPDATE daily_digest_log SET delivered = 0, delivery_error = ? WHERE id = ?",
        (str(error), digest_id),
    )
    conn.commit()


def mark_discord_delivered(conn, digest_id: int) -> None:
    ensure_daily_digest_schema(conn)
    conn.execute(
        "UPDATE daily_digest_log SET discord_delivered = 1, discord_delivered_at = datetime('now'), discord_delivery_error = NULL WHERE id = ?",
        (digest_id,),
    )
    conn.commit()


def mark_discord_delivery_failed(conn, digest_id: int, error: str) -> None:
    ensure_daily_digest_schema(conn)
    conn.execute(
        "UPDATE daily_digest_log SET discord_delivered = 0, discord_delivery_error = ? WHERE id = ?",
        (str(error), digest_id),
    )
    conn.commit()


def load_digest_log(conn, limit: int = 50) -> pd.DataFrame:
    ensure_daily_digest_schema(conn)
    columns_sql = ", ".join(DAILY_DIGEST_LOG_COLUMNS)
    return pd.read_sql_query(
        f"SELECT {columns_sql} FROM daily_digest_log ORDER BY trading_date DESC LIMIT ?", conn, params=(limit,),
    )
