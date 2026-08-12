# Deployment guide

Nothing in this directory is active. These are prepared configuration
templates and instructions — no scheduler has been installed, no cloud
resource has been created, and nothing has been made publicly accessible.
Every step that requires you to log into a platform, create an account,
or spend money is marked explicitly below.

## Why this matters: SQLite and persistent disk

The whole project (`prices`, `alerts`, `alert_state`, `automation_runs`)
lives in one SQLite file at `data/stock_dashboard.db`. SQLite is a great
fit for a single-machine portfolio project, but it has one hard
requirement: **the dashboard and the scheduled automation job must both
read/write the same file on the same persistent disk.**

This rules out, or at least complicates, several popular "easy" hosting
options:

- **Streamlit Community Cloud** (free) — great for hosting the dashboard
  itself, but its filesystem is ephemeral (wiped on every redeploy/restart)
  and it has no scheduler feature at all. It cannot run `automation/run_daily.py`,
  and a SQLite file written there would not survive a restart.
- **GitHub Actions scheduled workflows** — free, cron-capable, but each run
  gets a brand-new ephemeral filesystem. You'd have to commit the database
  back to the repo after every run (noisy, and briefly puts your data in
  git history) or push it somewhere else — real friction for what SQLite
  is supposed to make simple.
- Most serverless/functions platforms have the same ephemeral-disk problem.

**I have not migrated the project off SQLite, and I won't without asking
you first** — that's an explicit instruction I'm following, not just a
default. If you want the "dashboard on a free public URL + scheduler
somewhere else" architecture, the clean way to do it is a small hosted
Postgres (Supabase/Neon/Railway all have free tiers) that both services
reach over the network — but that's a real architecture change to
`db/database.py` and every module that opens a `sqlite3.connect`, not a
config tweak. **Ask me first if you want to go that route; I'll scope it
properly rather than doing it silently.**

## Recommended architecture

**Run both the dashboard and the scheduler on one machine with persistent
local disk, sharing the one SQLite file.** For a portfolio project this is
simpler, cheaper, and has zero migration risk compared to splitting
services across platforms with different storage models.

Two ways to do that, in order of how much it costs you:

### Option A — your own Mac (recommended to start; $0, works today)

Everything is already set up here. You'd just:
- Run the dashboard manually (or via a login item) when you want to look at it.
- Schedule `automation/run_daily.py` via macOS's native scheduler, **launchd**,
  using the template at `deploy/com.stockdashboard.dailyrun.plist.example`.

No new accounts, no cost, no data migration. The tradeoff: the dashboard
is only reachable while your Mac is on and only from your own network
(fine for a portfolio project you're the primary user of; not a public URL).

### Option B — a small persistent VPS ($4-6/mo; DigitalOcean, Linode, Hetzner, etc.)

Same architecture, just on a small always-on Linux box instead of your Mac,
using systemd instead of launchd (templates in `deploy/systemd/`). This
gets you a public URL for the dashboard (behind whatever access control you
set up — I'd suggest at minimum putting it behind a reverse proxy with
basic auth, since it displays your Alpaca paper account activity even
though it can't place trades). This is a real, small recurring cost and
requires you to create an account with a hosting provider — **I won't do
this for you; see the step-by-step below for exactly what to do.**

### Not recommended (without a further conversation): Streamlit Community Cloud

Free and easy for the dashboard alone, but per the SQLite section above it
can't run the scheduler or persist data written there. If you want a free
public dashboard URL specifically, the least-bad path is Option A/B for
the automation + database, with the dashboard *also* deployed to Streamlit
Cloud reading a periodically-synced copy of the SQLite file — but that
introduces staleness and sync complexity. Happy to design that properly if
you want it; flagging now rather than guessing.

---

## What's already done locally (in this repo, right now)

- `automation/run_daily.py` — the pipeline, dry-run safe by default.
- `deploy/run_dashboard.sh` — production-safe dashboard launch script
  (reads `$PORT` if set, binds `0.0.0.0`, otherwise identical to local dev).
- `deploy/com.stockdashboard.dailyrun.plist.example` — macOS launchd template.
- `deploy/crontab.example` — Linux cron template.
- `deploy/systemd/*.service`, `*.timer` — Linux systemd templates.
- `.env.example` — the 5 environment variable **names** this project needs
  (no values — copy it to `.env` and fill in your own real values, on
  whichever machine actually runs the code):
  - `ALPACA_API_KEY`
  - `ALPACA_SECRET_KEY`
  - `FINNHUB_API_KEY`
  - `FMP_API_KEY`
  - `DISCORD_WEBHOOK_URL`

None of these are read by, or exposed to, the Streamlit frontend — they're
only ever read server-side via `config/settings.py`'s `os.getenv()` calls.

## What still requires you to act (I will not do these without your go-ahead)

### Option A steps (local Mac, recommended first step)

1. Copy the plist template and edit the paths:
   ```bash
   cp deploy/com.stockdashboard.dailyrun.plist.example ~/Library/LaunchAgents/com.stockdashboard.dailyrun.plist
   # edit ~/Library/LaunchAgents/com.stockdashboard.dailyrun.plist:
   #   replace /Users/tylertyson/stock-dashboard with your actual path (if different)
   ```
2. Confirm your Mac's timezone is America/New_York (`Settings > General > Date & Time`),
   or adjust the `Hour`/`Minute` values in the plist for your timezone.
3. When you're ready to activate it:
   ```bash
   launchctl load ~/Library/LaunchAgents/com.stockdashboard.dailyrun.plist
   ```
   This is the actual "turn on the scheduler" step — I have not run this,
   and won't without you telling me to.
4. To check it's registered: `launchctl list | grep stockdashboard`
5. To stop it later: `launchctl unload ~/Library/LaunchAgents/com.stockdashboard.dailyrun.plist`
6. Logs land in `logs/automation.log`; run history is also queryable in
   the dashboard's Alert History page or directly via
   `python -m automation.run_daily` output.

### Option B steps (VPS, only if you want a public URL)

This requires creating an account with a hosting provider — a real step
only you can take:

1. Create an account with a VPS provider (DigitalOcean/Linode/Hetzner are
   reasonable ~$4-6/mo choices) and provision the smallest Ubuntu instance.
2. `git clone` (or `scp`) this project to the VPS, e.g. to `/opt/stock-dashboard`.
3. On the VPS: create the venv and install requirements the same way you
   did locally (`python3 -m venv venv && venv/bin/pip install -r requirements.txt`) —
   note the VPS's Python version needs to be 3.12+ for `pandas-ta`, same
   constraint we hit locally.
4. Copy `.env.example` to `.env` on the VPS and fill in your real credential
   values there — **never commit `.env` or paste real values into a shared
   terminal/chat.**
5. `sudo timedatectl set-timezone America/New_York`
6. Copy and edit the systemd files (replace `/opt/stock-dashboard` and the
   `stockdash` user if different), then:
   ```bash
   sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now stock-dashboard
   sudo systemctl enable --now stock-automation.timer
   ```
7. Put something in front of port 8501 before calling it "deployed" —
   at minimum a reverse proxy (nginx/Caddy) with TLS and basic auth, since
   the dashboard shows your paper-trading account activity. I can help
   configure this when you're at this step; it's a real step requiring
   your DNS/domain decisions, so I'd rather walk through it with you than
   guess.

### If/when you want real Discord delivery from the scheduled job

Both templates run in dry-run mode by default (matching
`automation/run_daily.py`'s own safe default — the `--send` flag is what
enables real delivery, and it's absent from every template here on
purpose). Add `--send` to the `ProgramArguments` array (plist) or
`ExecStart` line (systemd/cron) only once you've decided you want
unattended real alerts — that's a meaningfully different decision from
"the scheduler runs" and I've kept them independently toggleable.

## Sleep, wake, and shutdown behavior (StartCalendarInterval)

macOS's launchd is not cron. Per Apple's own `launchd.plist(5)` man page,
`StartCalendarInterval` jobs are handled specially around sleep:

- **Asleep at the scheduled time**: the job is *not* simply skipped. launchd
  runs it as soon as the Mac next wakes. If more than one scheduled firing
  was missed while asleep, they're coalesced into a single catch-up run —
  not one run per missed occurrence.
- **Powered off entirely at the scheduled time**: the missed firing is *not*
  replayed on the next boot — there's no launchd process running to catch
  it. It simply waits for the next matching `StartCalendarInterval`
  occurrence after the machine boots back up and the LaunchAgent reloads.

Practically, for this project: if your Mac is merely asleep at 4:30 PM ET on
a trading day, the automation still runs — just later, at whatever time you
next wake it — rather than silently being missed for the day. If it's fully
shut down through 4:30 PM, that day's run genuinely doesn't happen and is
not retroactively replayed later (the NYSE-calendar check is based on
*today's* date at run time, so a catch-up run the next morning wouldn't try
to replay the prior day).

This is standard, documented `launchd` behavior (`man launchd.plist` on any
Mac, `StartCalendarInterval` section) — one of the reasons launchd exists
instead of cron on macOS in the first place, since laptops sleep and
desktops don't always stay powered on.

## Costs and free-tier notes, honestly

- **Option A (your Mac)**: $0. See the sleep/wake section above for what
  happens if the Mac isn't awake at the scheduled time — it's more forgiving
  than a flat "must be on" requirement, but a fully powered-off Mac still
  means that day's run doesn't happen.
- **Option B (VPS)**: ~$4-6/mo for the smallest instance from most
  providers, plus your time to secure it (updates, firewall, TLS cert).
- **Alpaca**: paper trading is free; this project only ever uses paper mode.
- **Finnhub free tier**: rate-limited (60 calls/min on the free tier as of
  this writing) — fine for a 7-ticker daily job, would need attention if
  the watchlist grows significantly.
- **yfinance**: free but unofficial/rate-limited (as this project has
  experienced repeatedly) — Alpaca is the reliable fallback already wired
  up as the default source for automation.
- **Streamlit Community Cloud**: free, but see the SQLite caveat above
  before choosing it.
