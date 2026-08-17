"""Tests for db/daily_digest_config_repository.py (the on/off toggle) and
db/daily_digest_repository.py's send-log/de-dupe CRUD. Mirrors tests/
test_price_alert_config_repository.py's structure.
"""
import sqlite3

from db.daily_digest_config_repository import get_digest_enabled, set_digest_enabled
from db.daily_digest_repository import already_sent_today, load_digest_log, record_digest_sent
from db.schema import init_db


def make_test_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


# --- daily_digest_config: on/off toggle ---


def test_digest_disabled_by_default():
    conn = make_test_db()
    assert get_digest_enabled(conn) is False


def test_set_digest_enabled_true_persists():
    conn = make_test_db()
    set_digest_enabled(conn, True)
    assert get_digest_enabled(conn) is True


def test_set_digest_enabled_can_be_toggled_back_off():
    conn = make_test_db()
    set_digest_enabled(conn, True)
    set_digest_enabled(conn, False)
    assert get_digest_enabled(conn) is False


def test_set_digest_enabled_is_a_singleton_not_multiple_rows():
    conn = make_test_db()
    set_digest_enabled(conn, True)
    set_digest_enabled(conn, False)
    set_digest_enabled(conn, True)

    rows = conn.execute("SELECT COUNT(*) n FROM daily_digest_config").fetchone()
    assert rows["n"] == 1


# --- daily_digest_log: send-dedup + history ---


def test_already_sent_today_false_when_no_row():
    conn = make_test_db()
    assert already_sent_today(conn, "2024-06-04") is False


def test_record_digest_sent_marks_the_trading_date_as_sent():
    conn = make_test_db()
    record_digest_sent(conn, "2024-06-04", ticker_count=8, dry_run=True)
    assert already_sent_today(conn, "2024-06-04") is True
    assert already_sent_today(conn, "2024-06-05") is False


def test_record_digest_sent_twice_for_the_same_date_raises_on_unique_constraint():
    """The UNIQUE(trading_date) constraint is the actual de-dupe mechanism
    - alerts/daily_digest_runner.py checks already_sent_today() first so
    this path is never hit in practice, but the constraint itself must be
    the backstop."""
    import sqlite3 as sqlite3_module

    conn = make_test_db()
    record_digest_sent(conn, "2024-06-04", ticker_count=8, dry_run=True)
    try:
        record_digest_sent(conn, "2024-06-04", ticker_count=8, dry_run=True)
        assert False, "expected a UNIQUE constraint violation"
    except sqlite3_module.IntegrityError:
        pass


def test_load_digest_log_most_recent_trading_date_first():
    conn = make_test_db()
    record_digest_sent(conn, "2024-06-03", ticker_count=8, dry_run=True)
    record_digest_sent(conn, "2024-06-04", ticker_count=8, dry_run=True)

    history = load_digest_log(conn)
    assert list(history["trading_date"]) == ["2024-06-04", "2024-06-03"]
