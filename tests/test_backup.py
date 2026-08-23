"""Tests for backup/db_backup.py - safe SQLite snapshotting and rolling-
window pruning for data/stock_dashboard.db backups. Uses only synthetic,
temp-directory SQLite files - never touches the real project database.
"""
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backup.db_backup import create_backup, list_backups, prune_old_backups


def make_source_db(path: Path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE real_holdings (ticker TEXT, shares REAL)")
    conn.execute("INSERT INTO real_holdings VALUES ('AAPL', 10)")
    conn.commit()
    conn.close()


@pytest.fixture
def tmp_env():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "stock_dashboard.db"
        backup_dir = tmp_path / "backups"
        make_source_db(db_path)
        yield db_path, backup_dir


def test_create_backup_writes_a_full_readable_copy(tmp_env):
    db_path, backup_dir = tmp_env
    timestamp = datetime(2026, 8, 22, 3, 0, 0)

    result = create_backup(db_path, backup_dir, timestamp=timestamp)

    assert result.backup_path == backup_dir / "stock_dashboard_20260822_030000.db"
    assert result.backup_path.exists()
    assert result.size_bytes > 0

    conn = sqlite3.connect(str(result.backup_path))
    rows = conn.execute("SELECT ticker, shares FROM real_holdings").fetchall()
    conn.close()
    assert rows == [("AAPL", 10)]


def test_create_backup_does_not_mutate_the_source_file(tmp_env):
    db_path, backup_dir = tmp_env
    original_bytes = db_path.read_bytes()

    create_backup(db_path, backup_dir, timestamp=datetime(2026, 8, 22))

    assert db_path.read_bytes() == original_bytes


def test_prune_old_backups_deletes_only_files_past_the_retention_window(tmp_env):
    db_path, backup_dir = tmp_env
    now = datetime(2026, 8, 22, 12, 0, 0)

    old = create_backup(db_path, backup_dir, timestamp=now - timedelta(days=40)).backup_path
    recent = create_backup(db_path, backup_dir, timestamp=now - timedelta(days=5)).backup_path

    deleted = prune_old_backups(backup_dir, keep_days=30, now=now)

    assert deleted == [old]
    assert not old.exists()
    assert recent.exists()


def test_prune_old_backups_never_touches_unrecognized_files(tmp_env):
    db_path, backup_dir = tmp_env
    backup_dir.mkdir(parents=True)
    unrelated = backup_dir / "notes.txt"
    unrelated.write_text("keep me")

    deleted = prune_old_backups(backup_dir, keep_days=0, now=datetime(2026, 8, 22))

    assert unrelated.exists()
    assert unrelated not in deleted


def test_prune_old_backups_on_missing_directory_returns_empty(tmp_env):
    _, backup_dir = tmp_env
    assert prune_old_backups(backup_dir, keep_days=30, now=datetime(2026, 8, 22)) == []


def test_list_backups_returns_oldest_first(tmp_env):
    db_path, backup_dir = tmp_env
    now = datetime(2026, 8, 22)

    newer = create_backup(db_path, backup_dir, timestamp=now).backup_path
    older = create_backup(db_path, backup_dir, timestamp=now - timedelta(days=1)).backup_path

    assert list_backups(backup_dir) == [older, newer]


def test_list_backups_on_missing_directory_returns_empty(tmp_env):
    _, backup_dir = tmp_env
    assert list_backups(backup_dir) == []
