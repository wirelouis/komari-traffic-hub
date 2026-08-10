<p align="center">
  <a href="./README.md">简体中文</a> ｜ 
  <a href="./README_EN.md">English</a>
</p>

# komari-traffic-hub（Docker 版）

基于 **Komari 探针** 的流量统计管理中心，提供：

- 📊 Telegram **流量日报 / 周报 / 月报**
- 🔥 **Top N 流量消耗榜**（支持 `/top 6h`、`/top week` 等任意时间窗口）
- 🤖 Telegram Bot **交互式查询**
- 🧭 **Web 流量分析面板**
- 🐳 **Docker / docker-compose 部署**
- 🕒 统计口径固定为 **北京时间（Asia/Shanghai）**
- 🧱 适合多节点、长期运行场景

> 本项目不替代 Komari 官方功能，而是在其基础上补充  
> **长期统计 / 任意时间窗口 Top 榜 / Telegram 查询能力**。

---

## ✨ 功能特性

- **日报 / 周报 / 月报**
  - 在 Web 面板用「每日 / 每周 / 每月 + 时间」配置推送计划
  - 支持完整报表或 Top 报表
  - 立即发送、最近运行结果和任务历史可在面板查看
- **Top 流量消耗榜**
  - `/top`：今日 Top N（上下行合计）
  - `/top 6h`：最近 6 小时 Top
  - `/top week`、`/top month`
- **交互命令**
  - `/today` `/week` `/month`
  - `/top [Nh|today|week|month]`
  - `/ask 你的问题（或 /ai）`
- **Web 面板**
  - 总览今日 / 本周 / 本月流量与节点 Top
  - 查看告警状态，手动检查、静默或恢复告警
  - 测试 Telegram 推送，手动发送报表，并使用 AI 问答
- **智能告警**
  - 节点连续采样失败记录与恢复时长通知
  - 最近窗口 / 今日总量 / 单节点流量阈值告警
  - 支持冷却时间、静默时段和单独告警 chat
- **稳定性**
  - Komari 节点超时自动跳过，不影响整体报表
  - Telegram 网络异常自动重试
  - 探针/节点重启自动兜底计数器
- **数据管理**
  - 历史数据自动压缩归档
  - 采样数据用于支持任意 Nh 查询

---

## 🧩 依赖说明

- 已部署并可访问的 **Komari 面板**
- Docker + docker-compose
- 一个 Telegram Bot Token
- Telegram Chat ID（个人 / 群组）

---

## 🚀 快速部署（docker-compose）

### 1️⃣ 创建目录并赋予权限
容器默认以**非 root 用户（uid:gid = 10001:10001）**运行，需要对 `data/` 目录具有写权限。
```
bash
mkdir -p komari-traffic && cd komari-traffic
mkdir -p data
sudo chown -R 10001:10001 data
sudo chmod -R u+rwX,go+rX data
```
> 如果启动后日志中出现 `PermissionError: [Errno 13] Permission denied: '/data/...'`，
> 请重新执行上述 `chown` / `chmod` 命令后重启容器。
### 2️⃣ 创建 .env 配置文件
```
cat > .env <<'ENV'
# Komari 面板地址（不要以 / 结尾）
KOMARI_BASE_URL=https://your-komari.example

# Komari API 超时（秒）
KOMARI_TIMEOUT_SECONDS=15

# Komari API 鉴权（可选）
KOMARI_API_TOKEN=
KOMARI_API_TOKEN_HEADER=Authorization
KOMARI_API_TOKEN_PREFIX=Bearer

# Komari 节点并发请求数
KOMARI_FETCH_WORKERS=6

# Telegram
TELEGRAM_BOT_TOKEN=123456:YOUR_BOT_TOKEN
TELEGRAM_CHAT_ID=123456789

# 允许接收命令的 chat（可选，逗号分隔）
TELEGRAM_ALLOWED_CHAT_IDS=

# 管理员 chat（可选，逗号分隔）
TELEGRAM_ADMIN_CHAT_IDS=

# AI（可选，启用 /ask 与 /ai）
AI_API_BASE=
AI_API_KEY=
AI_MODEL=

# AI 数据包缓存时长（秒），默认 3600；设为 0 关闭缓存
AI_PACK_CACHE_TTL_SECONDS=3600

# Web 面板（WEB_PASSWORD 必填）
WEB_USERNAME=admin
WEB_PASSWORD=
# 留空时每次启动生成临时会话密钥；公网部署建议固定填写随机长字符串
WEB_SESSION_SECRET=
WEB_PORT=8080

# 启动通知（可选）
# 设为 0 可关闭启动消息
BOT_START_NOTIFY=1

# 启动通知显示的实例名（可选，建议填机器名/环境名）
BOT_INSTANCE_NAME=

# 容器内数据目录（固定）
DATA_DIR=/data

# 统计时区（默认 Asia/Shanghai）
STAT_TZ=Asia/Shanghai

# Top 榜数量
TOP_N=3

# 连续快照：每 5 分钟采样一次，默认保留 45 天原始快照
# samples 仅保留短期兼容缓存，流量统计以 traffic.db 快照为准
SAMPLE_INTERVAL_SECONDS=300
SAMPLE_RETENTION_HOURS=2
TRAFFIC_SNAPSHOT_RETENTION_DAYS=45

# 历史数据策略
HISTORY_HOT_DAYS=60
HISTORY_RETENTION_DAYS=400
TASK_RUN_RETENTION_DAYS=90
NODE_DAILY_USAGE_RETENTION_DAYS=365

# 智能告警
ALERTS_ENABLED=1
# 告警发送 chat（可选；留空时复用 TELEGRAM_CHAT_ID）
TELEGRAM_ALERT_CHAT_ID=
# 同一 active 告警的重复提醒冷却时间（秒）
ALERT_COOLDOWN_SECONDS=1800
# 每日静默窗口（可选），格式：23:00-07:00 或 12:00-13:00,23:00-07:00
ALERT_SILENCE_WINDOWS=
# 节点连续采样失败 N 次后记为离线；离线期间不推送，恢复后通知离线时长
ALERT_NODE_MISSING_SAMPLES=2
# 窗口流量阈值统计范围
ALERT_WINDOW_MINUTES=60
# 流量阈值：支持纯字节或 MiB/GiB/TiB；留空或 0 表示关闭对应规则
ALERT_TOTAL_WINDOW_BYTES=
ALERT_NODE_WINDOW_BYTES=
ALERT_DAILY_TOTAL_BYTES=
ALERT_DAILY_NODE_BYTES=
# 告警恢复后是否通知
ALERT_RECOVERY_NOTIFY=1

# 日志
LOG_LEVEL=INFO
LOG_FILE=
ENV
```
### 3️⃣ 使用 docker-compose 启动
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
启动：
```
docker compose up -d
docker compose ps
```
Web 面板默认访问：`http://localhost:8080`

推送计划直接在 Web 面板的「推送控制」里配置，由 `bot` 服务的 `listen` 进程执行。旧部署如果还保留 `cron` 服务，可以从 `docker-compose.yml` 中删除该服务以及 `./crontab:/app/crontab:ro` 挂载，避免重复推送。

## 🧭 Web 面板

Web 面板提供轻量控制台，用于查看总览、节点流量、长期分析、区间 CSV 导出、告警状态、Telegram 推送、AI 问答和系统健康。

- 登录账号：`WEB_USERNAME`（默认 `admin`）
- 登录密码：`WEB_PASSWORD`（必须设置）
- 端口：`WEB_PORT`（默认 `8080`）
- 会话密钥：`WEB_SESSION_SECRET`；留空时生成临时密钥，容器重启后登录态会失效

登录页的「记住我」默认关闭。勾选后只会延长当前浏览器的登录态，并允许浏览器或密码管理器询问是否保存账号密码；项目本身不会保存 Web 密码明文，也不会把密码写入 `localStorage`、Cookie、后端文件或 SQLite。未勾选时仍保持浏览器会话级登录，关闭浏览器后需要重新登录。

节点页会按 Komari `uuid` 自动绑定探针机器，也可以在面板里手动覆盖绑定。手动覆盖只保存在 `./data/node_bindings.json`，不会修改 Komari 本体配置；点击表格行只会查看详情，点击「打开」按钮才会打开 `KOMARI_BASE_URL/instance/{uuid}`。

推送控制页支持直接用「每日 / 每周 / 每月 + 时间」创建应用内计划任务，配置会保存在 `./data/report_schedules.json`。系统页开放实例名、Top 数量、AI 缓存 TTL、任务记录保留天数等低敏配置编辑；不会开放 token、密钥或登录密码。

面板不会向前端返回 Telegram Token、Komari Token、AI Key 或 Web 密码。

## 🧱 论坛部署最小步骤

如果你是从论坛帖子第一次部署，按这个顺序走即可：

1. 在 VPS 上准备 `komari-traffic/` 目录和 `data/` 目录，并按上面的命令给 `10001:10001` 写入权限。
2. 创建 `.env`，至少填写 `KOMARI_BASE_URL`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`WEB_PASSWORD`。
3. 建议同时填写一个固定的 `WEB_SESSION_SECRET`，可以用 `openssl rand -base64 48` 生成。
4. 使用本文的 `docker-compose.yml` 启动，确认只有 `bot` 和 `web` 两个服务，不需要旧的 `cron` 服务。
5. 打开 Web 面板，先看「系统」页是否正常，再到「推送控制」里测试发送。

启动后建议执行：

```
docker compose ps
docker compose logs --tail=100 bot
docker compose logs --tail=100 web
```

## 🔒 公开部署安全建议

- 必须设置强 `WEB_PASSWORD`，不要继续使用示例密码。
- 公网访问建议放在 HTTPS 反向代理后面，例如 Nginx、Caddy、Cloudflare Tunnel。
- 建议固定 `WEB_SESSION_SECRET`，否则容器重启后登录态会失效。
- 不要把 `.env`、`data/`、`traffic.db`、`node_bindings.json` 暴露到 Web 根目录。
- Web 登录带有基础失败限流和安全响应头，但它不是防火墙；如果面板公开到公网，仍建议限制来源 IP 或加一层反向代理鉴权。
- 公共设备或多人共用浏览器不要勾选「记住我」，也不要让浏览器保存密码。
- Web 面板只允许编辑低敏配置；Telegram Token、Komari Token、AI Key、Web 密码仍应通过 `.env` 管理。

## 🤖 Telegram 命令示例
| 命令           | 说明               |
| ------------ | ---------------- |
| `/today`     | 今日流量（00:00 → 当前） |
| `/week`      | 本周流量             |
| `/month`     | 本月流量             |
| `/top`       | 今日 Top N         |
| `/top 6h`    | 最近 6 小时 Top      |
| `/top week`  | 本周 Top           |
| `/top month` | 本月 Top           |
| `/ask 问题`   | AI 基于数据包分析回答 |
| `/ai 问题`    | `/ask` 别名         |
| `/alerts`     | 查看告警状态（管理员） |
| `/mute_alerts 2h` | 静默告警 2 小时（管理员） |
| `/unmute_alerts` | 解除告警静默（管理员） |
| `/help`       | 查看命令帮助         |

## 🕒 关于时区
统计口径时区：STAT_TZ（默认 Asia/Shanghai）

定时触发时区：容器 TZ（默认 Asia/Shanghai）

因此：

“每天 0 点” = 北京时间 0 点

与宿主机系统时区无关

## 📦 数据说明
所有数据均保存在 ./data 目录中：

traffic.db（SQLite 主存储：连续流量快照、每日节点汇总、任务执行记录）

history（日报历史 & 压缩归档，旧数据会在读取时迁移进 SQLite）

samples（短期兼容缓存，用于节点连续失败状态；流量统计以 SQLite 快照为准）

Telegram offset

alerts_state（告警 active 状态 / 冷却 / 静默）

node_bindings（Web 面板节点到 Komari 机器的手动绑定覆盖）

report_schedules（Web 面板应用内推送计划）

升级 / 重启容器不会丢数据。

当前周期、最近 Nh、小时分布和告警窗口都基于 `traffic.db` 里的连续快照做相邻差分；如果节点计数器回退或重置，该段负差会按 0 处理，不会把当前累计值误算成窗口流量。`TRAFFIC_SNAPSHOT_RETENTION_DAYS` 控制连续快照保留天数，默认 45 天；采样器会在每次采样时把当天数据持续物化进每日汇总（与是否发送报表无关），因此长期周/月/区间分析不依赖保留全部原始快照。

`TASK_RUN_RETENTION_DAYS` 控制 Web 面板任务运行记录的建议保留天数，默认 `90` 天；设为 `0` 表示关闭清理。`NODE_DAILY_USAGE_RETENTION_DAYS` 控制每日节点汇总保留天数，默认 `365` 天；设为 `0` 表示关闭清理。系统页的「数据维护」可手动清理旧运行记录、删除未使用的 `period_rollups` 遗留表或执行 SQLite 压缩。

## 🚨 智能告警

告警默认启用节点连续采样失败检测；流量阈值类规则只有在你配置对应阈值后才会触发，避免升级后突然刷屏。

节点连续采样失败达到阈值后只在内部记录为 active，离线期间不发送 Telegram；节点恢复上报后发送一条汇总通知，包含恢复时间和从首次失败采样开始计算的离线时长。`ALERT_COOLDOWN_SECONDS` 仅控制持续触发的流量阈值告警多久重复提醒一次。

- `ALERT_TOTAL_WINDOW_BYTES`：最近 `ALERT_WINDOW_MINUTES` 分钟所有节点合计超阈值
- `ALERT_NODE_WINDOW_BYTES`：最近窗口内单节点超阈值
- `ALERT_DAILY_TOTAL_BYTES`：今日所有节点合计超阈值
- `ALERT_DAILY_NODE_BYTES`：今日单节点超阈值

阈值支持 `500MiB`、`2GiB`、`1TiB` 或纯数字字节；留空或 `0` 表示关闭该规则。

可用命令：

```
/alerts
/mute_alerts 2h
/unmute_alerts
```

也可以在容器内手动检查：

```
python /app/komari_traffic_report.py check_alerts --dry-run
```

## 🔄 升级方式
```
docker compose pull
docker compose up -d
docker compose ps
```

本版本没有新增必填环境变量。如果你沿用较旧的 `docker-compose.yml`，请确认：

- `web` 服务存在，并且挂载 `./data:/data`
- `bot` 服务以 `python /app/komari_traffic_report.py listen` 运行；应用内计划任务由它执行
- `.env` 中已设置 `WEB_PASSWORD`
- `KOMARI_BASE_URL` 指向浏览器可访问的 Komari 地址，用于节点详情跳转
- 旧 `cron` 服务和 `crontab` 挂载已不再需要；升级后建议从 compose 文件里删除，推送计划统一在 Web 面板维护

`./data/node_bindings.json`、`./data/report_schedules.json` 和 `./data/traffic.db` 会自动创建，不需要手动准备。`traffic.db` 会优先用于周/月/更长周期聚合，并记录 `task_runs` 任务运行历史；旧 `history.json` 和压缩归档会在读取时自动迁移。

### 升级检查清单

- `docker compose pull` 输出显示镜像已拉取，或提示本地已经是最新。
- `docker compose ps` 中 `bot` 和 `web` 都处于运行状态。
- 打开 Web 面板后，登录页不会预填账号密码。
- 「系统」页显示 Komari、Telegram、SQLite、计划任务为正常或给出明确处理建议。
- 「推送控制」里测试发送一次，确认 Telegram 能收到消息。
- 如果旧部署曾经使用 `cron`，确认 compose 中已经删掉 `cron` 服务，避免和应用内计划任务重复推送。

### 确认 latest 已更新

推送到 `main` 后，GitHub Actions 会自动构建并发布 `ghcr.io/wirelouis/komari-traffic-hub:latest`。在 VPS 上可以按下面顺序确认：

```
docker compose pull
docker compose up -d
docker compose ps
docker image inspect ghcr.io/wirelouis/komari-traffic-hub:latest --format '{{.Id}}'
```

如果你想确认拉到的是 GitHub Actions 刚构建的版本，可以在 GitHub 仓库的 Actions 页面查看 `build-and-publish` 是否成功，再对比 VPS 上 `docker compose pull` 输出的 digest。升级后打开 Web 面板的「系统」页，检查版本/commit、SQLite、配置健康、最近任务运行记录是否正常；应用内计划任务由 `bot` 服务执行，Web 页会通过 `traffic.db` 展示最近运行结果。

## ⚠️ 常见错误排查

### Web 面板打不开

先执行 `docker compose ps`，确认 `web` 服务在运行；再执行 `docker compose logs --tail=100 web`。如果日志提示端口占用，把 `.env` 里的 `WEB_PORT` 改成其他端口后重启。

### 登录提示密码未配置

`.env` 里必须填写 `WEB_PASSWORD`，修改后执行 `docker compose up -d` 重启。公网部署还建议填写固定 `WEB_SESSION_SECRET`。

### 系统页提示 Komari 不可达

检查 `KOMARI_BASE_URL` 是否能从 VPS 访问，地址末尾不要带 `/`。如果 Komari API 需要鉴权，再检查 `KOMARI_API_TOKEN`、`KOMARI_API_TOKEN_HEADER`、`KOMARI_API_TOKEN_PREFIX`。

### Telegram 收不到测试消息

检查 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`。如果是群组，请先把 bot 拉进群并发一条消息，再确认 chat id 是否正确。

### /top 6h 暂时没数据

短窗口排行依赖采样积累，默认每 5 分钟采样一次；刚启动时需要等待一段时间。

### Komari 某个节点超时

程序会跳过超时节点，不影响其他节点报表。可以在 Komari 面板确认该机器是否在线，或适当调大 `KOMARI_TIMEOUT_SECONDS`。


## 🔐 管理员命令（需管理员 chat）

- `/archive` → 先发确认码，再通过 `/confirm_archive <code>` 执行
- `/alerts` → 查看告警状态
- `/mute_alerts 2h` / `/unmute_alerts` → 临时静默或恢复告警

> 默认管理员为 `TELEGRAM_CHAT_ID`。配置 `TELEGRAM_ADMIN_CHAT_IDS` 后按该列表生效。

## 🤖 /ask 数据范围说明

`/ask` 与 `/ai` 只基于程序计算出的 `data_pack` 回答，主要包含：

- 今日按节点增量（含可读单位）
- 最近 1 小时按节点明细（可回答“刚刚这一小时哪台机器用了多少”）
- 最近 24 小时 Top（含可读单位）
- 最近 24 小时按小时分桶（含峰值/低谷小时）
- 今天按节点小时级走势（可回答“今天每小时各机器用了多少”）
- 昨天按节点小时级走势（可回答“某节点昨天最忙时段”）
- 最近 7 天按日总量 + 按节点累计排行（含可读单位）

若数据不足，AI 会明确说明无法判断。


## ℹ️ 启动提示说明

默认会发送一条启动提示，内容为“实例名 + 统计时区 + 可接收命令 chat 数”。

- 通过 `BOT_INSTANCE_NAME` 自定义实例标识（如 `hk-vps-prod`）。
- 通过 `BOT_START_NOTIFY=0` 关闭启动提示。


> 说明：数据会持续由 bot 采样/生成并写入本地文件（samples/history 等），但**不会在后台持续主动推送给 AI**；仅在你触发 `/ask` 或 `/ai` 时才会临时组包并调用 AI。

另外：`/ask` 数据包支持本地缓存（默认 1 小时），同一时间段连续追问会复用缓存，过期后自动重建。  
但像“刚刚这一小时 / 今天按小时”这类强时效问题，会自动绕过缓存并实时重建数据包，避免拿旧数据回答。


### 关于 TELEGRAM_ALLOWED_CHAT_IDS / TELEGRAM_ADMIN_CHAT_IDS

这两个变量可以留空；留空时默认回退为 `TELEGRAM_CHAT_ID`，不会导致 bot 收不到消息。
## 🔗 Acknowledgements

感谢 LINUX DO 社区提供交流与分享平台。

[![LINUX DO](https://img.shields.io/badge/COMMUNITY-LINUXDO-00AEEF?style=for-the-badge&labelColor=555555)](https://linux.do)
