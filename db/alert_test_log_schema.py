"""Schema for alert_test_log - a dedicated, isolated audit trail for
"Send Test Alert" button clicks (dashboard/views/price_alert_config.py,
volatility_alert_config.py, daily_digest_config.py), so they're visible in
the Alert Activity feed (dashboard/views/alert_activity.py).

Deliberately SEPARATE from price_alert_state/volatility_alert_state/
daily_digest_log: alerts/alert_test_notifications.py's delivery functions
themselves remain completely database-free (see that module's docstring -
they take zero arguments and never open a connection, a structural
guarantee that a test click cannot touch real state or the digest dedupe).
This table is populated one layer up, by dashboard/data.py's wrapper
functions, only AFTER delivery completes - the delivery path itself is
unchanged and unaware this table exists.

Kept OUT of db/schema.py's init_db() (which is frozen - see
tests/test_ops_prospective_audit_phase16.py::
test_git_diff_never_touches_frozen_or_forbidden_files), lazily created
here, same convention as every other alert table in this codebase.
"""
CREATE_ALERT_TEST_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS alert_test_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    alert_type TEXT NOT NULL,
    email_delivered INTEGER NOT NULL DEFAULT 0,
    email_error TEXT,
    discord_delivered INTEGER NOT NULL DEFAULT 0,
    discord_error TEXT
);
"""


def ensure_alert_test_log_schema(conn) -> None:
    conn.execute(CREATE_ALERT_TEST_LOG_TABLE)
    conn.commit()
