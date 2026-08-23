"""Back up data/stock_dashboard.db - the manual "run this before any risky
change" entrypoint, and also what backup/com.stockdashboard.backup.plist.example
schedules to run daily, unattended (see backup/README.md for install
steps - this template lives here rather than under deploy/ since deploy/
is a frozen directory this project never edits from an application phase).
Same script both ways - no separate "scheduled" code path to drift out of
sync with a manual run.

Usage:
    python -m backup.run_backup                        # backup now, prune anything older than 30 days
    python -m backup.run_backup --keep-days 60          # keep a longer rolling window
    python -m backup.run_backup --backup-dir /some/path # write elsewhere (e.g. an external drive)
    python -m backup.run_backup --list                  # show existing backups, then exit

Backups land in data/backups/ by default, named
stock_dashboard_YYYYMMDD_HHMMSS.db - see backup/db_backup.py for the
SQLite Online Backup API call that makes this safe to run even while the
dashboard or a scheduled automation run has the live file open.

To restore: stop the dashboard/any running automation, then
    cp data/backups/stock_dashboard_<timestamp>.db data/stock_dashboard.db
(or point --db-path at wherever you want the restored copy written).
"""
import argparse
import logging
from datetime import datetime
from pathlib import Path

from backup.db_backup import DEFAULT_RETENTION_DAYS, create_backup, list_backups, prune_old_backups
from config.settings import DB_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("run_backup")

DEFAULT_BACKUP_DIR = DB_PATH.parent / "backups"


def main():
    parser = argparse.ArgumentParser(description="Back up data/stock_dashboard.db via SQLite's Online Backup API")
    parser.add_argument("--db-path", type=Path, default=DB_PATH, help=f"Source database (default: {DB_PATH})")
    parser.add_argument(
        "--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR,
        help=f"Backup destination directory (default: {DEFAULT_BACKUP_DIR})",
    )
    parser.add_argument(
        "--keep-days", type=int, default=DEFAULT_RETENTION_DAYS,
        help=f"Delete backups older than this many days (default: {DEFAULT_RETENTION_DAYS})",
    )
    parser.add_argument("--list", action="store_true", help="List existing backups and exit - no new backup taken")
    args = parser.parse_args()

    if args.list:
        backups = list_backups(args.backup_dir)
        if not backups:
            print(f"No backups found in {args.backup_dir}")
            return
        for path in backups:
            size_kb = path.stat().st_size / 1024
            print(f"{path}  ({size_kb:.1f} KB)")
        return

    if not args.db_path.exists():
        logger.error("no database found at %s - nothing to back up", args.db_path)
        raise SystemExit(1)

    now = datetime.now()
    result = create_backup(args.db_path, args.backup_dir, timestamp=now)
    logger.info("backup written: %s (%.1f KB)", result.backup_path, result.size_bytes / 1024)

    deleted = prune_old_backups(args.backup_dir, keep_days=args.keep_days, now=now)
    if deleted:
        logger.info("pruned %d backup(s) older than %d day(s): %s", len(deleted), args.keep_days, ", ".join(p.name for p in deleted))


if __name__ == "__main__":
    main()
