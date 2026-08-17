# Alert System

Built the night of 2026-08-16. If you're reading this cold: this covers
everything that pages you or emails you from this project, the two
data-integrity bugs that were hiding under it, and the one dashboard page
that shows all of it at once. Code is the source of truth if this drifts —
every claim below was checked against the actual files, not the commit
messages, as of commit `7465a97`.

## The five things that can notify you

All of them share the same two-channel contract: **email (SMTP) and Discord
(webhook) are each attempted independently** — one being unconfigured or
failing never blocks the other. All of them are `dry_run` by default; real
delivery requires an explicit `--send` flag on whatever CLI runs them (or
the pipeline being invoked with `--send`).

### 1. Price-threshold alerts

**What**: per-ticker fixed-dollar or percent-band above/below levels.
"AAPL crossed above $250."

- **Configure**: dashboard → *Price Alert Thresholds* page
  (`dashboard/views/price_alert_config.py`). Two modes:
  - **Fixed**: enter above/below dollar values directly.
  - **Percent**: enter a baseline price + a symmetric `±X%` band.
    `resolve_percent_band()` (`alerts/price_config.py`) resolves this to
    concrete above/below dollars **once, at save time** — the band is
    anchored to the baseline you entered, it never re-anchors as price
    moves. Crossing-detection (`alerts/price_engine.py`) never knows the
    difference; it only ever compares against resolved above/below.
  - A ticker with no row in `price_alert_config` is simply never evaluated.
- **Trigger**: evaluated every automation run against the ticker's latest
  close. Never fires on a ticker's first-ever observation (that just
  establishes a baseline). Two suppression layers: crossing must actually
  happen (price moved from one side of the threshold to the other, not
  just "currently above"), and a `cooldown_minutes` (default 60) wall-clock
  backstop in `alerts/price_runner.py`.
- **Delivered by**: `alerts/run_price_alerts.py`, or wired into
  `automation/run_daily.py`'s daily pipeline.
- **Log table**: `price_alerts` (event log, one row per fire, with
  independent `delivered`/`discord_delivered` columns) +
  `price_alert_state` (last-observed price per ticker, for crossing
  detection).

### 2. Volatility alerts (day-over-day % move)

**What**: "AAPL moved 6% today" — a single per-ticker `threshold_percent`
that fires on a big move in **either** direction (unlike price-threshold's
separate above/below fields).

- **Configure**: dashboard → *Volatility Alert Thresholds* page.
- **Trigger**: `alerts/volatility_engine.py` reads the ticker's two most
  recent rows **directly from the `prices` table** (not
  `price_alert_state` — that table belongs to the price-threshold system
  and can be stale relative to yesterday's actual close). Fires when
  `|current - previous| / previous * 100 >= threshold_percent`. Two
  suppression layers, same file as price alerts:
  primary — a given trading day fires at most once; secondary —
  `cooldown_minutes` (default 60) wall-clock backstop.
  - **Known, deliberate limitation**: if a day was missed in ingestion, the
    "previous" row is whatever the last *stored* row is, not literally
    yesterday. A 2-day gap gets measured as if it were a 1-day move. This is
    intentionally not special-cased (see the module docstring and tests) —
    it is not the same thing as the ingestion-staleness fix below, which
    prevents the gap from happening at all for *configured* tickers.
- **Delivered by**: `alerts/run_volatility_alerts.py`, or the daily
  pipeline.
- **Log table**: `volatility_alerts` + `volatility_alert_state`
  (`volatility_alert_config` holds the per-ticker threshold).

### 3. Daily digest

**What**: unconditional, once-per-trading-day summary of *every* tracked
ticker — current price, day-over-day %, distance to its configured
price-threshold and volatility levels. Format:
`"AAPL: $233.10 (+0.8%), 3.2% from upper threshold"`. This one **never
decides anything** — it's not conditional like the two above, it just
reports state, win or fail, whether or not either alert type fired.

- **Configure**: dashboard → *Daily Digest* page. A single global on/off
  toggle (`daily_digest_config`), not per-ticker — the digest always
  covers every tracked ticker by definition.
- **Trigger**: wired into `automation/run_daily.py` as a post-pipeline
  step (needs the full resolved ticker set, so it runs after ingestion,
  not inside the per-ticker loop). Fires on any pipeline outcome it
  actually attempted (success, partial, or total failure) — but not on a
  skipped non-trading day. De-duped to at most one send per `trading_date`
  via `UNIQUE(trading_date)` on `daily_digest_log`.
- **Delivered by**: `alerts/run_daily_digest.py`, or the daily pipeline.
- **Log table**: `daily_digest_log`.

### 4. Operational-failure notifications

**What**: pages you when the pipeline itself breaks — market data
couldn't be refreshed — as opposed to a stock-signal alert. This exists
because of a real incident: on 2026-08-12 a total ingestion failure was
correctly logged but nothing paged anyone; it went unnoticed until a
manual check. `alerts/ops_notifications.py`'s `OPERATIONAL_ALERTS_ENABLED`
was sitting hardcoded `False` ("prepared for future use") until this was
reviewed and flipped to `True` on 2026-08-16. It's a deliberate code
change, not an env var — not something that can be silently toggled.

- **Configure**: nothing dashboard-side; it's on unconditionally once
  `OPERATIONAL_ALERTS_ENABLED = True` in `alerts/ops_notifications.py`.
- **Trigger**: `automation/run_daily.py` checks `result.status` after
  every pipeline run; if it's `STATUS_FAILED` or `STATUS_PARTIAL_FAILURE`,
  calls `send_operational_failure_notification()` with a summary of which
  tickers failed and why. At most one per `trading_date` (dedup via
  `operational_notifications` table, checked with `already_sent_today()`).
  Error text is sanitized first (`sanitize_error_summary()` strips URLs
  and anything credential-shaped before it ever reaches Discord/email).
- **Delivered by**: `automation/run_daily.py` only — there's no standalone
  CLI for this one, it only makes sense in the context of a real pipeline
  run failing.
- **Log table**: `operational_notifications`.

### 5. Manual test sends ("Send Test Alert")

**What**: a button on each of the *Price Alert Thresholds*, *Volatility
Alert Thresholds*, and *Daily Digest* config pages that sends a fixed,
clearly `[TEST]`-labeled sample message over both channels — proves your
SMTP/Discord config actually works without touching any real state.

- **Isolation guarantee** (`alerts/alert_test_notifications.py`): these
  functions take **zero arguments and never open a DB connection or
  import any engine module** (`price_engine.py`, `volatility_engine.py`,
  `daily_digest_engine.py`). A test click structurally cannot touch
  `price_alert_state`, `volatility_alert_state`, or the digest's
  once-per-day dedupe, even by accident — the code path never reaches
  those modules at all. All content is hardcoded placeholder data (e.g.
  "AAPL crossed above $250.00 ... sample data").
- **Trigger**: manual button click only, from
  `dashboard/views/{price_alert_config,volatility_alert_config,daily_digest_config}.py`.
- **Log table**: `alert_test_log` — populated one layer up, by
  `dashboard/data.py`'s wrapper functions, *after* delivery completes.
  The delivery path in `alert_test_notifications.py` itself is unaware
  this table exists.

## The two data-integrity fixes

### SOURCE_PRIORITY minimum-row floor (commit `1e77aad`)

`db/price_repository.py`'s `resolve_source()` used to blindly trust a
`SOURCE_PRIORITY` source (yfinance/alpaca) over the largest available
source *whenever it existed at all*, regardless of row count. In
practice: 3 stray/test `alpaca` rows for a ticker silently shadowed
`alpaca_adjusted`'s 1,254 legitimately-backfilled rows — stale/wrong
prices with no visible error, because a priority source technically
existed.

Fix: `MIN_TRUST_RATIO` — a priority source only wins if its row count is
at least half the best available source's row count; otherwise resolution
falls through to whichever source actually has the most rows. This is
foundational — every alert type reads through `resolve_source()`/
`load_price_history()`, so a wrong source resolution silently corrupts
every alert type at once.

### Ingestion staleness gap (from the price-threshold and volatility commits)

Before tonight, the daily pipeline only ingested `WATCHLIST` tickers. If
you configured a price-threshold or volatility alert on a ticker that
wasn't in `WATCHLIST`, that ticker's price data would just never refresh
— its alert would silently evaluate against increasingly stale data
forever, no error, no signal that anything was wrong.

Fix, in `automation/pipeline.py`:
```python
configured_extra_tickers = set(thresholds_by_ticker) | set(volatility_configs_by_ticker)
tickers = WATCHLIST + sorted(t for t in configured_extra_tickers if t not in WATCHLIST)
```
Any ticker with a configured price-threshold or volatility alert now
automatically joins the default ingestion set — configuring an alert on a
new ticker is enough on its own, no need to also add it to `WATCHLIST`.
(Passing `--tickers` explicitly on the CLI bypasses this union entirely —
exact list, no auto-add, on purpose.)

## The frozen `db/schema.py` constraint, and the lazy-schema workaround

`db/schema.py` is in `FORBIDDEN_FROZEN_FILES`
(`tests/test_ops_prospective_audit_phase16.py`), along with a handful of
other core files (`backtest/config.py`, `strategy_lab/phase*.py`, etc.) and
everything under `deploy/`. A test —
`test_git_diff_never_touches_frozen_or_forbidden_files` — asserts the live
git working-tree diff (staged or not, modified or untracked) never
intersects that set. It's a hard gate: if you touch `db/schema.py`, the
test suite fails, on purpose, with no allowlist exception to reach for.

Every alert table (`price_alerts`/`price_alert_state`/`price_alert_config`,
the volatility equivalents, `daily_digest_config`/`daily_digest_log`,
`operational_notifications`, `alert_test_log`) was therefore built as its
own schema module *outside* `db/schema.py`'s `init_db()`, following the
convention `strategy_lab/prospective.py` already established:

```python
def ensure_<thing>_schema(conn) -> None:
    conn.execute(CREATE_TABLE_IF_NOT_EXISTS_...)
    conn.commit()
    _migrate_add_whatever_columns(conn)  # additive migrations, same pattern
```

Each table's repository module (`db/price_alert_repository.py`, etc.)
calls its own `ensure_*_schema()` at the top of every read/write function
— idempotent, safe to call every time, never referenced from
`db/schema.py` or `db/database.py`. This is also how additive schema
changes get made after the fact without a real migration system: e.g.
percent-mode columns were added to `price_alert_config` via
`_migrate_add_percent_mode_columns_to_price_alert_config()`, which checks
`PRAGMA table_info` and only runs `ALTER TABLE` if the column is actually
missing — pre-existing rows back-fill to sane defaults
(`mode='fixed'`), existing thresholds keep evaluating exactly as before.

**If you need a new alert-adjacent table**: don't touch `db/schema.py`.
Add a new `db/<thing>_schema.py` with its own `ensure_*_schema()`, and
call it from the top of whatever repository functions read/write that
table.

## launchd scheduling

Nothing is actually scheduled yet as of this writing — `deploy/` (frozen,
see above) contains a **template only**:
`deploy/com.stockdashboard.dailyrun.plist.example`. Turning it on is a
manual step (`deploy/README.md` has the full walkthrough):

```bash
cp deploy/com.stockdashboard.dailyrun.plist.example ~/Library/LaunchAgents/com.stockdashboard.dailyrun.plist
# edit the copied file: fix paths if the repo isn't at /Users/tylertyson/stock-dashboard
launchctl load ~/Library/LaunchAgents/com.stockdashboard.dailyrun.plist
```

Check it's registered: `launchctl list | grep stockdashboard`.
Stop it: `launchctl unload ~/Library/LaunchAgents/com.stockdashboard.dailyrun.plist`.

**Logs**: `logs/automation.log` (the raw log every run writes to), and the
dashboard's *Alert History* page / `python -m automation.run_daily` stdout
for a formatted summary of a given run.

**Sleep/wake behavior** (this bit me before, worth remembering): launchd's
`StartCalendarInterval` is not cron.
- Mac asleep at the scheduled time → job runs as soon as it wakes
  (multiple missed firings while asleep coalesce into one catch-up run,
  not one per miss).
- Mac fully powered off at the scheduled time → that day's run simply
  doesn't happen, and is **not** retroactively replayed on next boot (the
  NYSE-trading-day check is based on *today's* date at run time).
- The template runs in dry-run mode by default. Real Discord/email
  delivery requires manually adding `--send` to the plist's
  `ProgramArguments` — a deliberately separate decision from "the
  scheduler runs at all."

## Alert Activity — the one page that shows everything

`dashboard/views/alert_activity.py`, URL path `alert-activity`, title
"Alert Activity" in the sidebar. **Purely read-only** — it only calls
`dashboard.data.get_alert_activity_feed()`, which only `SELECT`s from five
existing tables and merges them into one chronological feed:

| Source table | Alert type shown |
|---|---|
| `price_alerts` | Price Alert |
| `volatility_alerts` | Volatility Alert |
| `daily_digest_log` | Daily Digest |
| `operational_notifications` | Operational Failure |
| `alert_test_log` | (manual test sends) |

No write path exists anywhere reachable from this page — if you're
debugging "did X actually get delivered," this is the first place to
look, before digging into individual tables. Filterable by type and date
range in the UI; shows per-channel (email/Discord) delivery status per
row, so a partial delivery (one channel sent, one failed) is visible at a
glance rather than needing to cross-reference two tables.

## Env vars this all depends on

From `config/settings.py`, read server-side only (never exposed to the
Streamlit frontend):

```
DISCORD_WEBHOOK_URL
SMTP_HOST, SMTP_PORT (default 587), SMTP_USERNAME, SMTP_PASSWORD
ALERT_EMAIL_FROM, ALERT_EMAIL_TO
```

`automation/run_daily.py` refuses to run with `--send` if price thresholds
are configured but SMTP isn't fully set — fails loud (`EXIT_TOTAL_FAILURE`)
rather than silently skipping email delivery.
