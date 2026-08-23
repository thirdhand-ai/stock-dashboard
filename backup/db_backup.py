"""Safe, consistent SQLite backups for data/stock_dashboard.db - the only
copy of real financial data (cost basis, holdings, alert configs) this
project stores; the file is deliberately gitignored (`data/*.db`), so
there is no other copy anywhere until this module runs.

Uses sqlite3.Connection.backup() - the SQLite Online Backup API - rather
than a plain file copy. A plain `shutil.copy` while the dashboard or a
scheduled automation run holds the file open mid-write could capture a
torn, partially-written snapshot (a page written, journal not yet
committed). The backup API takes a proper page-level snapshot under
SQLite's own locking and is safe to run at any time, even while the
source file is actively being read or written elsewhere.

Used both by backup/run_backup.py (the manual "backup now" entrypoint)
and, unattended, by whatever schedules it (see
backup/com.stockdashboard.backup.plist.example) - one code path, no
separate "scheduled" logic to drift out of sync with the manual one.
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

BACKUP_FILENAME_FORMAT = "stock_dashboard_%Y%m%d_%H%M%S.db"
DEFAULT_RETENTION_DAYS = 30


@dataclass(frozen=True)
class BackupResult:
    backup_path: Path
    size_bytes: int


def create_backup(db_path: Path, backup_dir: Path, timestamp: Optional[datetime] = None) -> BackupResult:
    """Snapshot db_path into backup_dir via SQLite's own backup API - safe
    to call while db_path is open elsewhere. `timestamp` is injectable for
    tests; defaults to the real current time."""
    timestamp = timestamp or datetime.now()
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / timestamp.strftime(BACKUP_FILENAME_FORMAT)

    source = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(backup_path))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    return BackupResult(backup_path=backup_path, size_bytes=backup_path.stat().st_size)


def prune_old_backups(backup_dir: Path, keep_days: int = DEFAULT_RETENTION_DAYS, now: Optional[datetime] = None) -> List[Path]:
    """Delete backups older than keep_days, keeping a rolling window rather
    than growing forever. Only ever touches files matching this module's
    own naming pattern (stock_dashboard_*.db) - never deletes anything
    else a user might also keep in backup_dir. `now` is injectable for
    tests; defaults to the real current time."""
    if not backup_dir.exists():
        return []

    now = now or datetime.now()
    cutoff = now - timedelta(days=keep_days)
    stem_format = BACKUP_FILENAME_FORMAT[: -len(".db")]

    deleted = []
    for path in sorted(backup_dir.glob("stock_dashboard_*.db")):
        try:
            stamp = datetime.strptime(path.stem, stem_format)
        except ValueError:
            continue  # not one of ours - never touch a file we didn't create
        if stamp < cutoff:
            path.unlink()
            deleted.append(path)
    return deleted


def list_backups(backup_dir: Path) -> List[Path]:
    """All recognized backups in backup_dir, oldest first - used by
    backup/run_backup.py's summary output and available for a future
    dashboard "restore" UI."""
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob("stock_dashboard_*.db"))
