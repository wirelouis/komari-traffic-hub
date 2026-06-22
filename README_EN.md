<p align="center">
  <a href="./README.md">简体中文</a> ｜ 
  <a href="./README_EN.md">English</a>
</p>

# komari-traffic-hub (Docker Edition)

A **Dockerized traffic statistics management hub** for **Komari Probe**, providing:

- 📊 Daily / Weekly / Monthly traffic reports via Telegram
- 🔥 Top N traffic consumers (supports `/top 6h`, `/top week`, etc.)
- 🤖 Interactive Telegram Bot commands
- 🧭 Web traffic analysis console
- 🐳 Docker / docker-compose deployment
- 🕒 Fixed statistics timezone (Asia/Shanghai by default)
- 🧱 Designed for multi-node and long-running environments

> This project does **not replace Komari**.  
> It enhances Komari with **long-term aggregation, arbitrary time window Top lists,
> and Telegram-based querying**.

---

## ✨ Features

- **Scheduled Reports**
  - Configure delivery schedules in the Web console with daily / weekly / monthly plus a time
  - Send full reports or Top reports
  - Run now, last result, and task history are visible in the console
- **Top Traffic Ranking**
  - `/top` – today Top N (up + down)
  - `/top 6h` – last 6 hours
  - `/top week`, `/top month`
- **Telegram Commands**
  - `/today`, `/week`, `/month`
  - `/top [Nh|today|week|month]`
  - `/ask your question (or /ai)`
- **Web Console**
  - Overview for today / week / month traffic and node Top lists
  - Alert status, manual checks, mute/unmute controls
  - Telegram test/report sending and AI data Q&A
- **Smart Alerts**
  - Consecutive node sampling failures and recovery notifications
  - Recent-window, daily total, and per-node traffic thresholds
  - Cooldown, silence windows, and optional dedicated alert chat
- **Stability & Reliability**
  - Slow or failed Komari nodes are skipped automatically
  - Telegram network errors are retried
  - Counter reset detection & fallback
- **Data Management**
  - Historical data auto-compression
  - Sampling system for arbitrary Nh queries

---

## 🧩 Requirements

- A running **Komari panel** (API accessible)
- Docker + docker-compose
- Telegram Bot Token
- Telegram Chat ID (user or group)

---

## 🚀 Quick Start (docker-compose)

### 1️⃣ Create data directory and set permissions (required)
This container runs as a non-root user (`uid:gid = 10001:10001`) and needs write access to the `data/` directory.
```
bash
mkdir -p komari-traffic && cd komari-traffic
mkdir -p data
sudo chown -R 10001:10001 data
sudo chmod -R u+rwX,go+rX data
```
> If you encounter `PermissionError: [Errno 13] Permission denied: '/data/...'` in the logs after startup,
> re-execute the above `chown` / `chmod` commands and restart the container.
### 2️⃣ Create .env
```
cp env.example .env
# Then edit .env as needed.

# Or create .env manually:
cat > .env <<'ENV'
# Komari panel base URL (no trailing slash)
KOMARI_BASE_URL=https://your-komari.example

# Komari API timeout (seconds)
KOMARI_TIMEOUT_SECONDS=15

# Komari API auth (optional)
KOMARI_API_TOKEN=
KOMARI_API_TOKEN_HEADER=Authorization
KOMARI_API_TOKEN_PREFIX=Bearer

# Komari fetch concurrency
KOMARI_FETCH_WORKERS=6

# Telegram
TELEGRAM_BOT_TOKEN=123456:YOUR_BOT_TOKEN
TELEGRAM_CHAT_ID=123456789

# Allowed command chats (optional, comma-separated)
TELEGRAM_ALLOWED_CHAT_IDS=

# Admin chats (optional, comma-separated)
TELEGRAM_ADMIN_CHAT_IDS=

# AI (optional, enables /ask and /ai)
AI_API_BASE=
AI_API_KEY=
AI_MODEL=

# AI data-pack cache TTL in seconds (default 3600; set 0 to disable)
AI_PACK_CACHE_TTL_SECONDS=3600

# Web console (WEB_PASSWORD is required)
WEB_USERNAME=admin
WEB_PASSWORD=
# Empty means a temporary session secret is generated on each start
WEB_SESSION_SECRET=
WEB_PORT=8080

# Startup notification (optional)
# Set to 0 to disable startup message
BOT_START_NOTIFY=1

# Instance label shown in startup message (optional)
BOT_INSTANCE_NAME=

# Container data directory (do not change)
DATA_DIR=/data

# Statistics timezone (default Asia/Shanghai)
STAT_TZ=Asia/Shanghai

# Top ranking size
TOP_N=3

# Continuous snapshots: sample every 5 minutes and keep raw snapshots for 45 days by default
# samples is only a short compatibility cache; traffic stats use traffic.db snapshots
SAMPLE_INTERVAL_SECONDS=300
SAMPLE_RETENTION_HOURS=2
TRAFFIC_SNAPSHOT_RETENTION_DAYS=45

# History retention
HISTORY_HOT_DAYS=60
HISTORY_RETENTION_DAYS=400
TASK_RUN_RETENTION_DAYS=90

# Smart alerts
ALERTS_ENABLED=1
# Optional alert chat; empty means TELEGRAM_CHAT_ID
TELEGRAM_ALERT_CHAT_ID=
# Repeat cooldown for the same active alert (seconds)
ALERT_COOLDOWN_SECONDS=1800
# Optional daily silence windows, e.g. 23:00-07:00 or 12:00-13:00,23:00-07:00
ALERT_SILENCE_WINDOWS=
# Alert after N consecutive failed node samples
ALERT_NODE_MISSING_SAMPLES=2
# Window size for recent-window traffic thresholds
ALERT_WINDOW_MINUTES=60
# Traffic thresholds: bytes or MiB/GiB/TiB; empty or 0 disables each rule
ALERT_TOTAL_WINDOW_BYTES=
ALERT_NODE_WINDOW_BYTES=
ALERT_DAILY_TOTAL_BYTES=
ALERT_DAILY_NODE_BYTES=
# Send recovery notifications
ALERT_RECOVERY_NOTIFY=1

# Logging
LOG_LEVEL=INFO
LOG_FILE=
ENV
```
### 3️⃣ docker-compose.yml
```
version: "3.9"

services:
  bot:
    image: ghcr.io/wirelouis/komari-traffic-hub:latest
    env_file: .env
    environment:
      - TZ=Asia/Shanghai
      - STAT_TZ=Asia/Shanghai
    volumes:
      - ./data:/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "/app/komari_traffic_report.py", "health"]
      interval: 30s
      timeout: 10s
      retries: 3
    command: ["python", "/app/komari_traffic_report.py", "listen"]

  web:
    image: ghcr.io/wirelouis/komari-traffic-hub:latest
    env_file: .env
    environment:
      - TZ=Asia/Shanghai
      - STAT_TZ=Asia/Shanghai
    volumes:
      - ./data:/data
    ports:
      - "${WEB_PORT:-8080}:8080"
    restart: unless-stopped
    command: ["uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "8080"]
```
Start services:
```
docker compose up -d
```
Web console: `http://localhost:8080`

Configure delivery schedules in the Web console under Telegram delivery. They are executed by the `bot` service running in `listen` mode. If an older deployment still has a `cron` service, remove that service and the `./crontab:/app/crontab:ro` mount from `docker-compose.yml` to avoid duplicate reports.

## 🧭 Web Console

The Web console is a lightweight dashboard for traffic overview, node analysis, long-term analytics with range CSV export, alert controls, Telegram delivery, AI Q&A, and system health.

- Username: `WEB_USERNAME` (default `admin`)
- Password: `WEB_PASSWORD` (required)
- Port: `WEB_PORT` (default `8080`)
- Session secret: `WEB_SESSION_SECRET`; empty values generate a temporary secret, so sessions expire after container restart

The node page automatically binds traffic nodes to Komari machines by `uuid`. You can also override a binding in the console. Manual overrides are stored only in `./data/node_bindings.json` and do not modify Komari itself; clicking a table row only selects the detail panel, and the “Open” button opens `KOMARI_BASE_URL/instance/{uuid}`.

The Telegram delivery page can create app-managed schedules with plain controls such as daily / weekly / monthly plus a time. These schedules are stored in `./data/report_schedules.json`. The System page can edit low-risk runtime settings such as instance label, Top size, AI cache TTL, and task-run retention; tokens, keys, and Web passwords are never editable or returned to the browser.

The console never returns Telegram tokens, Komari tokens, AI keys, or the Web password to the browser.

## 🤖 Telegram Command Examples
| Command      | Description                 |
| ------------ | --------------------------- |
| `/today`     | Today traffic (00:00 → now) |
| `/week`      | Current week                |
| `/month`     | Current month               |
| `/top`       | Today Top N                 |
| `/top 6h`    | Top in last 6 hours         |
| `/top week`  | Weekly Top                  |
| `/top month` | Monthly Top                 |
| `/ask question` | AI answer based on data pack |
| `/ai question`  | Alias of `/ask` |
| `/alerts`       | Show alert status (admin) |
| `/mute_alerts 2h` | Mute alerts for 2 hours (admin) |
| `/unmute_alerts` | Unmute alerts (admin) |
| `/help`         | Show command help |
## 🕒 Timezone

Statistics timezone: STAT_TZ (default Asia/Shanghai)

Scheduler timezone: container TZ

This ensures daily reports are triggered at local midnight.

## 📦 Data Persistence

All runtime data is stored in ./data:

traffic.db (primary SQLite storage: continuous traffic snapshots, per-day node rollups, task runs)

History (daily records & compressed archives, migrated into SQLite on read)

Samples (short compatibility cache for consecutive node-failure state; traffic stats use SQLite snapshots)

Telegram update offset

alerts_state (active alerts / cooldown / mute state)

node_bindings (manual Web-console overrides from traffic nodes to Komari machines)

report_schedules (app-managed Web-console delivery schedules)

Upgrades and restarts will not lose data.

Current periods, recent Nh windows, hourly distributions, and alert windows are computed from adjacent deltas in `traffic.db` continuous snapshots. If a node counter rolls back or resets, that negative segment is treated as 0 instead of counting the current absolute value as window traffic. `TRAFFIC_SNAPSHOT_RETENTION_DAYS` controls raw snapshot retention and defaults to `45`; the sampler continuously materializes durable daily rollups on every sample (independent of report delivery), so long-term week/month/range analytics do not require keeping every raw snapshot forever.

`TASK_RUN_RETENTION_DAYS` controls the suggested retention window for Web-console task run history. The default is `90` days; set it to `0` to disable pruning. The System page maintenance actions only prune old `task_runs` rows or run SQLite vacuum; they do not delete daily, weekly, or monthly traffic rollups.

## 🚨 Smart Alerts

Node consecutive sampling failure detection is enabled by default. Traffic threshold rules only trigger after you configure a threshold, so upgrades will not suddenly spam alerts.

- `ALERT_TOTAL_WINDOW_BYTES`: all-node total over the recent `ALERT_WINDOW_MINUTES` window
- `ALERT_NODE_WINDOW_BYTES`: per-node traffic over the recent window
- `ALERT_DAILY_TOTAL_BYTES`: all-node total for today
- `ALERT_DAILY_NODE_BYTES`: per-node total for today

Thresholds support `500MiB`, `2GiB`, `1TiB`, or raw bytes. Empty or `0` disables that rule.

Admin commands:

```
/alerts
/mute_alerts 2h
/unmute_alerts
```

Manual check inside the container:

```
python /app/komari_traffic_report.py check_alerts --dry-run
```

## 🔄 Upgrade
```
docker compose pull
docker compose up -d
```

This version does not add new required environment variables. If you are using an older `docker-compose.yml`, check that:

- the `web` service exists and mounts `./data:/data`
- the `bot` service runs `python /app/komari_traffic_report.py listen`; app-managed schedules are executed there
- `.env` sets `WEB_PASSWORD`
- `KOMARI_BASE_URL` points to a Komari address reachable from your browser, used for node detail links
- the old `cron` service and `crontab` mount are no longer needed; remove them from older compose files and manage delivery schedules in the Web console

`./data/node_bindings.json`, `./data/report_schedules.json`, and `./data/traffic.db` are created automatically. `traffic.db` is preferred for weekly/monthly/longer rollups and now also stores `task_runs` execution history; legacy `history.json` plus compressed archives are migrated on read.

### Confirm `latest` Is Fresh

After a push to `main`, GitHub Actions builds and publishes `ghcr.io/wirelouis/komari-traffic-hub:latest`. On your VPS, update and verify with:

```
docker compose pull
docker compose up -d
docker compose ps
docker image inspect ghcr.io/wirelouis/komari-traffic-hub:latest --format '{{.Id}}'
```

To confirm the image is the one just built by GitHub Actions, check that the repository Actions page shows `build-and-publish` succeeded, then compare the digest printed by `docker compose pull` on the VPS. After upgrading, open the Web console System page and check version/commit, SQLite, configuration health, and recent task runs. App-managed schedules are executed by the `bot` service; the Web console reads recent results from `traffic.db`.


## 🔐 Admin commands (admin chats only)

- `/archive` -> sends a confirmation code, then execute with `/confirm_archive <code>`
- `/alerts` -> show alert status
- `/mute_alerts 2h` / `/unmute_alerts` -> temporarily mute or resume alerts

> By default only `TELEGRAM_CHAT_ID` is admin. Set `TELEGRAM_ADMIN_CHAT_IDS` to override.

## 🤖 /ask data scope

`/ask` and `/ai` can only use computed `data_pack` fields, including:

- today per-node deltas (with human-readable units)
- last 1h per-node detail (for questions like “how much did each node use in the last hour?”)
- last 24h top nodes (with human-readable units)
- last 24h hourly buckets (including peak/valley hour)
- today per-node hourly trend (for “today by hour” node analysis)
- yesterday per-node hourly trend (for node-specific busy-hour analysis)
- last 7 days daily totals + per-node aggregate ranking (with human-readable units)

If data is insufficient, AI should explicitly say it cannot determine from current data.


## ℹ️ Startup notification

By default the bot sends a startup message containing: instance label, stats timezone, and number of allowed command chats.

- Use `BOT_INSTANCE_NAME` to set a meaningful instance label (e.g. `hk-vps-prod`).
- Use `BOT_START_NOTIFY=0` to disable startup notifications.


> Note: data is continuously sampled/generated into local files (e.g. samples/history), but it is **not continuously pushed to AI in background**. AI is called only when `/ask` or `/ai` is triggered.

Additionally, `/ask` data-pack caching is enabled locally by default (1 hour): short follow-up questions reuse cached pack; expired cache is rebuilt automatically.  
For highly time-sensitive questions such as “the last hour” or “today by hour”, the bot bypasses cache and rebuilds the pack immediately to avoid stale answers.


### About TELEGRAM_ALLOWED_CHAT_IDS / TELEGRAM_ADMIN_CHAT_IDS

Both variables can be left empty; they fallback to `TELEGRAM_CHAT_ID`, so the bot can still receive commands by default.

## 🔗 Acknowledgements

This project is shared with support from the LINUX DO community.

[![LINUX DO](https://img.shields.io/badge/COMMUNITY-LINUXDO-00AEEF?style=for-the-badge&labelColor=555555)](https://linux.do)
