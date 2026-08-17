"""Schema for alert_snooze - temporary delivery suppression for the
price-threshold and volatility alert systems. Kept OUT of db/schema.py's
init_db() (frozen - see tests/test_ops_prospective_audit_phase16.py::
test_git_diff_never_touches_frozen_or_forbidden_files), lazily created
here, same convention as every other alert table in this codebase.

Deliberately ONE shared table for both alert types (`alert_type` column
distinguishes them) rather than a price_alert_snooze/volatility_alert_snooze
pair - unlike price_alert_config/volatility_alert_config, the snooze/expiry
mechanics here are identical for both systems (a ticker or an entire alert
type, muted until a UTC timestamp), so splitting it would just be
copy-paste drift with nothing type-specific to justify it.

`ticker IS NULL` means "every ticker for this alert_type" (a global
snooze) - see db/alert_snooze_repository.py's get_active_snooze for how a
ticker-specific row takes precedence over a global one covering the same
alert_type. There is no `active`/`cancelled` flag: a snooze is either
still in its window (`snoozed_until` in the future) or it isn't - manual
unsnooze deletes the row outright (db/alert_snooze_repository.py's
delete_snooze), and natural expiry needs no action at all, since every
read filters on `snoozed_until > datetime('now')`.
"""
CREATE_ALERT_SNOOZE_TABLE = """
CREATE TABLE IF NOT EXISTS alert_snooze (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,
    ticker TEXT,
    snoozed_until TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_ALERT_SNOOZE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_alert_snooze_type_ticker ON alert_snooze(alert_type, ticker);
"""


def ensure_alert_snooze_schema(conn) -> None:
    """Idempotent - safe to call on every read/write, same convention as
    db/price_alerts_schema.py::ensure_price_alerts_schema."""
    conn.execute(CREATE_ALERT_SNOOZE_TABLE)
    conn.execute(CREATE_ALERT_SNOOZE_INDEX)
    conn.commit()
