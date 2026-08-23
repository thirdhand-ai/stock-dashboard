# Database backups

`data/stock_dashboard.db` is gitignored and holds real financial data
(cost basis, holdings, alert configs) with no other copy anywhere. This
directory backs it up safely - via SQLite's own Online Backup API, not a
raw file copy, so a backup is never a torn/mid-write snapshot even if the
dashboard or a scheduled automation run has the file open at the same
moment.

This lives in `backup/` rather than `deploy/` deliberately: `deploy/` is a
durably frozen directory this project never edits from an application
phase (see `tests/test_ops_prospective_audit_phase16.py`'s frozen/
forbidden-path guard) - so the launchd template below sits next to the
code it schedules instead.

## Run it manually any time

```bash
python -m backup.run_backup
```

This writes a timestamped snapshot to `data/backups/stock_dashboard_<YYYYMMDD_HHMMSS>.db`.
Backups older than 30 days are deleted automatically each run.

```bash
python -m backup.run_backup --keep-days 60          # keep a longer rolling window
python -m backup.run_backup --backup-dir /some/path # write elsewhere (e.g. an external drive)
python -m backup.run_backup --list                  # show existing backups, then exit
```

## Schedule it to run daily

Same launchd pattern as `deploy/com.stockdashboard.dailyrun.plist.example`:

```bash
cp backup/com.stockdashboard.backup.plist.example ~/Library/LaunchAgents/com.stockdashboard.backup.plist
# edit ~/Library/LaunchAgents/com.stockdashboard.backup.plist:
#   replace /Users/tylertyson/stock-dashboard with your actual path (if different)
launchctl load ~/Library/LaunchAgents/com.stockdashboard.backup.plist
```

This is the actual "turn on the scheduler" step — nothing in this repo
runs it for you. Check it's registered with `launchctl list | grep stockdashboard`;
stop it later with `launchctl unload ~/Library/LaunchAgents/com.stockdashboard.backup.plist`.
Logs land in `logs/backup.log`.

## Restore from a backup

1. Stop the dashboard and make sure no scheduled automation run is
   currently in progress (`launchctl list | grep stockdashboard` to check).
2. Pick a snapshot: `python -m backup.run_backup --list`
3. Copy it over the live database (this overwrites your current
   `data/stock_dashboard.db` - if you want to keep it, move it aside
   first):
   ```bash
   cp data/backups/stock_dashboard_<timestamp>.db data/stock_dashboard.db
   ```
4. Restart the dashboard / re-enable the scheduled jobs.
