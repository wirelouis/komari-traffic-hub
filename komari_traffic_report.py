#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import logging
import os
import re
import errno
import sys
import time
import traceback
import socket
import gzip
import html
import hashlib
import concurrent.futures
import signal
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

import requests
import random
import sqlite3
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextmanager
def file_lock(path: str):
    """Cross-process file lock for JSON state files"""
    lock_path = f"{path}.lock"
    lock_file = open(lock_path, "a")
    try:
        if sys.platform == "win32":
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if sys.platform == "win32":
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def parse_bool_value(value, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text == "":
        return default
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    raise RuntimeError(f"invalid boolean value: {value}")


def parse_bool_env(name: str, default: bool = False) -> bool:
    return parse_bool_value(os.environ.get(name), default=default)


def parse_bytes_value(value, name: str = "value") -> int:
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0

    m = re.fullmatch(r"(?i)(\d+(?:\.\d+)?)\s*([kmgtp]?i?b?|b)?", text)
    if not m:
        raise RuntimeError(f"{name} must be bytes or KiB/MiB/GiB/TiB, got: {value}")

    number = float(m.group(1))
    unit = (m.group(2) or "b").lower()
    unit_map = {
        "": 1,
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024 ** 2,
        "mb": 1024 ** 2,
        "mib": 1024 ** 2,
        "g": 1024 ** 3,
        "gb": 1024 ** 3,
        "gib": 1024 ** 3,
        "t": 1024 ** 4,
        "tb": 1024 ** 4,
        "tib": 1024 ** 4,
        "p": 1024 ** 5,
        "pb": 1024 ** 5,
        "pib": 1024 ** 5,
    }
    if unit not in unit_map:
        raise RuntimeError(f"{name} has unsupported unit: {value}")
    return max(0, int(number * unit_map[unit]))


def bytes_config_text(value: int) -> str:
    n = int(value or 0)
    return "" if n <= 0 else human_bytes(n)


def parse_bytes_env(name: str) -> int:
    return parse_bytes_value(os.environ.get(name, ""), name=name)


def validate_silence_windows_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for part in re.split(r"[,;]\s*", text):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", part.strip())
        if not m:
            raise RuntimeError(f"ALERT_SILENCE_WINDOWS invalid segment: {part}")
        sh, sm, eh, em = [int(x) for x in m.groups()]
        if not (0 <= sh <= 23 and 0 <= eh <= 23 and 0 <= sm <= 59 and 0 <= em <= 59):
            raise RuntimeError(f"ALERT_SILENCE_WINDOWS time out of range: {part}")
        if sh * 60 + sm == eh * 60 + em:
            raise RuntimeError(f"ALERT_SILENCE_WINDOWS empty segment: {part}")
    return text


STAT_TZ = os.environ.get("STAT_TZ", "Asia/Shanghai")


def load_stat_timezone(name: str):
    try:
        return ZoneInfo(name)
    except Exception:
        if name in ("Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin"):
            return timezone(timedelta(hours=8), name)
        if name.upper() in ("UTC", "Etc/UTC"):
            return timezone.utc
        raise


TZ = load_stat_timezone(STAT_TZ)  # 统计时区

KOMARI_BASE_URL = os.environ.get("KOMARI_BASE_URL", "").rstrip("/")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", "/var/lib/komari-traffic")

HISTORY_HOT_DAYS = int(os.environ.get("HISTORY_HOT_DAYS", "60"))
HISTORY_RETENTION_DAYS = int(os.environ.get("HISTORY_RETENTION_DAYS", "400"))

KOMARI_API_TOKEN = os.environ.get("KOMARI_API_TOKEN", "")
KOMARI_API_TOKEN_HEADER = os.environ.get("KOMARI_API_TOKEN_HEADER", "Authorization")
KOMARI_API_TOKEN_PREFIX = os.environ.get("KOMARI_API_TOKEN_PREFIX", "Bearer")
KOMARI_FETCH_WORKERS = int(os.environ.get("KOMARI_FETCH_WORKERS", "6"))

TOP_N = int(os.environ.get("TOP_N", "3"))  # 默认 Top3（日报/周报/月报/Top命令都用它）

AI_API_BASE = os.environ.get("AI_API_BASE", "").rstrip("/")
AI_API_KEY = os.environ.get("AI_API_KEY", "").strip()
AI_MODEL = os.environ.get("AI_MODEL", "").strip()

# /top Nh 与当前周期统计依赖连续采样：bot 运行时自动采样
SAMPLE_INTERVAL_SECONDS = int(os.environ.get("SAMPLE_INTERVAL_SECONDS", "300"))  # 默认 5 分钟
SAMPLE_RETENTION_HOURS = int(os.environ.get("SAMPLE_RETENTION_HOURS", "2"))    # 默认保留 2 小时采样
TRAFFIC_SNAPSHOT_RETENTION_DAYS = max(1, int(os.environ.get("TRAFFIC_SNAPSHOT_RETENTION_DAYS", "45")))

HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
SAMPLES_PATH = os.path.join(DATA_DIR, "samples.json")
TG_OFFSET_PATH = os.path.join(DATA_DIR, "tg_offset.txt")
TG_CONFIRM_PATH = os.path.join(DATA_DIR, "tg_confirm.json")
AI_PACK_CACHE_PATH = os.path.join(DATA_DIR, "ai_pack_cache.json")
ALERTS_STATE_PATH = os.path.join(DATA_DIR, "alerts_state.json")
REPORT_SCHEDULES_PATH = os.path.join(DATA_DIR, "report_schedules.json")
TRAFFIC_DB_PATH = os.path.join(DATA_DIR, "traffic.db")

TIMEOUT = int(os.environ.get("KOMARI_TIMEOUT_SECONDS", "15"))  # Komari API timeout（秒）

APP_VERSION = os.environ.get("APP_VERSION", "dev").strip() or "dev"
GIT_COMMIT = os.environ.get("GIT_COMMIT", "").strip()
BUILD_DATE = os.environ.get("BUILD_DATE", "").strip()
IMAGE_SOURCE = os.environ.get("IMAGE_SOURCE", "ghcr.io/wirelouis/komari-traffic-hub").strip()
TASK_RUN_RETENTION_DAYS = max(0, int(os.environ.get("TASK_RUN_RETENTION_DAYS", "90")))
NODE_DAILY_USAGE_RETENTION_DAYS = max(0, int(os.environ.get("NODE_DAILY_USAGE_RETENTION_DAYS", "365")))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.environ.get("LOG_FILE", "").strip()

BOT_INSTANCE_NAME = os.environ.get("BOT_INSTANCE_NAME", "").strip()
BOT_START_NOTIFY = parse_bool_env("BOT_START_NOTIFY", True)
AI_PACK_CACHE_TTL_SECONDS = max(0, int(os.environ.get("AI_PACK_CACHE_TTL_SECONDS", "3600")))

ALERTS_ENABLED = parse_bool_env("ALERTS_ENABLED", True)
TELEGRAM_ALERT_CHAT_ID = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()
ALERT_COOLDOWN_SECONDS = int(os.environ.get("ALERT_COOLDOWN_SECONDS", "1800"))
ALERT_SILENCE_WINDOWS = os.environ.get("ALERT_SILENCE_WINDOWS", "").strip()
ALERT_NODE_MISSING_SAMPLES = int(os.environ.get("ALERT_NODE_MISSING_SAMPLES", "2"))
ALERT_WINDOW_MINUTES = int(os.environ.get("ALERT_WINDOW_MINUTES", "60"))
ALERT_TOTAL_WINDOW_BYTES = parse_bytes_env("ALERT_TOTAL_WINDOW_BYTES")
ALERT_NODE_WINDOW_BYTES = parse_bytes_env("ALERT_NODE_WINDOW_BYTES")
ALERT_DAILY_TOTAL_BYTES = parse_bytes_env("ALERT_DAILY_TOTAL_BYTES")
ALERT_DAILY_NODE_BYTES = parse_bytes_env("ALERT_DAILY_NODE_BYTES")
ALERT_RECOVERY_NOTIFY = parse_bool_env("ALERT_RECOVERY_NOTIFY", True)

SHUTTING_DOWN = False
SAMPLE_THREAD: threading.Thread | None = None
SAMPLE_STOP_EVENT = threading.Event()
SCHEDULER_THREAD: threading.Thread | None = None
SCHEDULER_STOP_EVENT = threading.Event()

_TRAFFIC_DB_INITIALIZED = False
_SAMPLES_MIGRATED = False


def ai_enabled() -> bool:
    return bool(AI_API_BASE and AI_API_KEY and AI_MODEL)


def build_http_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


HTTP_SESSION = build_http_session()


def setup_logging():
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if LOG_FILE:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def build_komari_headers() -> dict:
    headers = {"Accept": "application/json"}
    if KOMARI_API_TOKEN:
        prefix = KOMARI_API_TOKEN_PREFIX.strip()
        value = f"{prefix} {KOMARI_API_TOKEN}".strip()
        headers[KOMARI_API_TOKEN_HEADER] = value
    return headers


def _require_positive_int(name: str, value: int):
    if value <= 0:
        raise RuntimeError(f"{name} must be > 0")


def _require_non_negative_int(name: str, value: int):
    if value < 0:
        raise RuntimeError(f"{name} must be >= 0")


def validate_config_or_raise():
    required = {
        "KOMARI_BASE_URL": KOMARI_BASE_URL,
    }
    missing = [k for k, v in required.items() if not str(v).strip()]
    if missing:
        raise RuntimeError("Missing required env: " + ", ".join(missing))

    _require_positive_int("KOMARI_TIMEOUT_SECONDS", TIMEOUT)
    _require_positive_int("KOMARI_FETCH_WORKERS", KOMARI_FETCH_WORKERS)
    _require_positive_int("TOP_N", TOP_N)
    _require_positive_int("SAMPLE_INTERVAL_SECONDS", SAMPLE_INTERVAL_SECONDS)
    _require_positive_int("SAMPLE_RETENTION_HOURS", SAMPLE_RETENTION_HOURS)
    _require_non_negative_int("ALERT_COOLDOWN_SECONDS", ALERT_COOLDOWN_SECONDS)
    _require_positive_int("ALERT_NODE_MISSING_SAMPLES", ALERT_NODE_MISSING_SAMPLES)
    _require_positive_int("ALERT_WINDOW_MINUTES", ALERT_WINDOW_MINUTES)
    for name, value in (
        ("ALERT_TOTAL_WINDOW_BYTES", ALERT_TOTAL_WINDOW_BYTES),
        ("ALERT_NODE_WINDOW_BYTES", ALERT_NODE_WINDOW_BYTES),
        ("ALERT_DAILY_TOTAL_BYTES", ALERT_DAILY_TOTAL_BYTES),
        ("ALERT_DAILY_NODE_BYTES", ALERT_DAILY_NODE_BYTES),
    ):
        _require_non_negative_int(name, value)
    if TELEGRAM_ALERT_CHAT_ID and any(ch.isspace() for ch in TELEGRAM_ALERT_CHAT_ID):
        raise RuntimeError("TELEGRAM_ALERT_CHAT_ID must be a single chat id without whitespace")
    parse_silence_windows(ALERT_SILENCE_WINDOWS)


def run_healthcheck_or_raise():
    ensure_dirs()

    test_path = os.path.join(DATA_DIR, ".health_write_test")
    try:
        with open(test_path, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_path)
    except Exception as e:
        raise RuntimeError(f"DATA_DIR not writable: {DATA_DIR}: {e}")

    for p in [HISTORY_PATH, SAMPLES_PATH, TG_OFFSET_PATH, TG_CONFIRM_PATH, ALERTS_STATE_PATH]:
        if os.path.exists(p):
            try:
                if p == TG_OFFSET_PATH:
                    _ = load_offset()
                else:
                    load_json_strict(p)
            except Exception as e:
                raise RuntimeError(f"Corrupted file: {p}: {e}")

    try:
        traffic_db_healthcheck()
    except Exception as e:
        raise RuntimeError(f"SQLite healthcheck failed: {e}")

    try:
        HTTP_SESSION.get(KOMARI_BASE_URL, timeout=TIMEOUT, headers=build_komari_headers())
    except Exception as e:
        raise RuntimeError(f"Komari unreachable: {e}")


def _handle_sigterm(signum, _frame):
    global SHUTTING_DOWN
    SHUTTING_DOWN = True
    logging.warning("received signal %s, shutting down gracefully...", signum)


# -------------------- 基础工具 --------------------

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except Exception:
        pass


def human_bytes(n: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    x = float(max(int(n), 0))
    for idx, u in enumerate(units):
        if x < 1024 or idx == len(units) - 1:
            return f"{x:.2f} {u}" if u != "B" else f"{int(x)} B"
        x /= 1024


def telegram_html_escape(value) -> str:
    return html.escape(str(value or ""), quote=False)


def now_dt() -> datetime:
    return datetime.now(TZ)


def today_date() -> date:
    return now_dt().date()


def start_of_day(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ)


def start_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())  # 周一为起点


def start_of_month(d: date) -> date:
    return date(d.year, d.month, 1)


def yyyymm(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logging.warning(f"Failed to load JSON from {path}: {e}")
        # Backup corrupted files for alerts_state and schedules
        if "alerts_state" in path or "schedules" in path:
            try:
                backup_path = f"{path}.corrupt-{int(time.time())}"
                if os.path.exists(path):
                    os.rename(path, backup_path)
                    logging.warning(f"Backed up corrupted file to {backup_path}")
            except Exception:
                pass
        return default


def load_json_strict(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_date_yyyy_mm_dd(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def unique_temp_path(path: str) -> str:
    path = str(path)
    return f"{path}.{os.getpid()}.{threading.get_ident()}.{secrets.token_urlsafe(6)}.tmp"


def save_json_atomic(path: str, data):
    path = str(path)
    parent_dir = os.path.dirname(path) or "."
    tmp = os.path.join(parent_dir, f".tmp.{os.path.basename(path)}.{os.getpid()}.{int(time.time()*1000)}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def save_text_atomic(path: str, text: str):
    path = str(path)
    parent_dir = os.path.dirname(path) or "."
    tmp = os.path.join(parent_dir, f".tmp.{os.path.basename(path)}.{os.getpid()}.{int(time.time()*1000)}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(text))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def runtime_config_path() -> str:
    return os.path.join(DATA_DIR, "runtime_config.json")


def _parse_editable_int(payload: dict, key: str, current: int, min_value: int, max_value: int) -> int:
    if key not in payload:
        return int(current)
    try:
        value = int(payload.get(key))
    except Exception:
        raise RuntimeError(f"{key} must be an integer")
    if value < min_value or value > max_value:
        raise RuntimeError(f"{key} must be between {min_value} and {max_value}")
    return value


def _parse_editable_bool(payload: dict, key: str, current: bool) -> bool:
    if key not in payload:
        return bool(current)
    return parse_bool_value(payload.get(key), default=bool(current))


def _parse_editable_text(payload: dict, key: str, current: str, max_len: int = 240) -> str:
    if key not in payload:
        return str(current or "").strip()
    return str(payload.get(key) or "").strip()[:max_len]


def _parse_editable_bytes(payload: dict, key: str, current: int, max_value: int = 1024 ** 6) -> int:
    if key not in payload:
        return int(current)
    value = parse_bytes_value(payload.get(key), name=key)
    if value < 0 or value > max_value:
        raise RuntimeError(f"{key} must be between 0 and {human_bytes(max_value)}")
    return value


def validate_runtime_config(payload: dict) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    instance_name = str(payload.get("bot_instance_name", BOT_INSTANCE_NAME) or "").strip()[:80]
    komari_base_url = _parse_editable_text(payload, "komari_base_url", KOMARI_BASE_URL).rstrip("/")
    telegram_chat_id = _parse_editable_text(payload, "telegram_chat_id", TELEGRAM_CHAT_ID, 120)
    telegram_alert_chat_id = _parse_editable_text(payload, "telegram_alert_chat_id", TELEGRAM_ALERT_CHAT_ID, 120)
    ai_api_base = _parse_editable_text(payload, "ai_api_base", AI_API_BASE).rstrip("/")
    ai_model = _parse_editable_text(payload, "ai_model", AI_MODEL, 120)
    for key, value in (("telegram_chat_id", telegram_chat_id), ("telegram_alert_chat_id", telegram_alert_chat_id)):
        if value and any(ch.isspace() for ch in value):
            raise RuntimeError(f"{key} must be a single chat id without whitespace")
    return {
        "bot_instance_name": instance_name,
        "komari_base_url": komari_base_url,
        "telegram_chat_id": telegram_chat_id,
        "telegram_alert_chat_id": telegram_alert_chat_id,
        "ai_api_base": ai_api_base,
        "ai_model": ai_model,
        "top_n": _parse_editable_int(payload, "top_n", TOP_N, 1, 50),
        "komari_timeout_seconds": _parse_editable_int(payload, "komari_timeout_seconds", TIMEOUT, 3, 120),
        "komari_fetch_workers": _parse_editable_int(payload, "komari_fetch_workers", KOMARI_FETCH_WORKERS, 1, 32),
        "sample_interval_seconds": _parse_editable_int(payload, "sample_interval_seconds", SAMPLE_INTERVAL_SECONDS, 60, 3600),
        "sample_retention_hours": _parse_editable_int(payload, "sample_retention_hours", SAMPLE_RETENTION_HOURS, 1, 168),
        "traffic_snapshot_retention_days": _parse_editable_int(payload, "traffic_snapshot_retention_days", TRAFFIC_SNAPSHOT_RETENTION_DAYS, 1, 3650),
        "ai_pack_cache_ttl_seconds": _parse_editable_int(payload, "ai_pack_cache_ttl_seconds", AI_PACK_CACHE_TTL_SECONDS, 0, 86400),
        "task_run_retention_days": _parse_editable_int(payload, "task_run_retention_days", TASK_RUN_RETENTION_DAYS, 0, 3650),
        "node_daily_usage_retention_days": _parse_editable_int(payload, "node_daily_usage_retention_days", NODE_DAILY_USAGE_RETENTION_DAYS, 0, 3650),
        "alerts_enabled": _parse_editable_bool(payload, "alerts_enabled", ALERTS_ENABLED),
        "alert_recovery_notify": _parse_editable_bool(payload, "alert_recovery_notify", ALERT_RECOVERY_NOTIFY),
        "alert_cooldown_seconds": _parse_editable_int(payload, "alert_cooldown_seconds", ALERT_COOLDOWN_SECONDS, 0, 86400),
        "alert_window_minutes": _parse_editable_int(payload, "alert_window_minutes", ALERT_WINDOW_MINUTES, 5, 1440),
        "alert_node_missing_samples": _parse_editable_int(payload, "alert_node_missing_samples", ALERT_NODE_MISSING_SAMPLES, 1, 20),
        "alert_silence_windows": validate_silence_windows_text(payload.get("alert_silence_windows", ALERT_SILENCE_WINDOWS)),
        "alert_total_window_bytes": _parse_editable_bytes(payload, "alert_total_window_bytes", ALERT_TOTAL_WINDOW_BYTES),
        "alert_node_window_bytes": _parse_editable_bytes(payload, "alert_node_window_bytes", ALERT_NODE_WINDOW_BYTES),
        "alert_daily_total_bytes": _parse_editable_bytes(payload, "alert_daily_total_bytes", ALERT_DAILY_TOTAL_BYTES),
        "alert_daily_node_bytes": _parse_editable_bytes(payload, "alert_daily_node_bytes", ALERT_DAILY_NODE_BYTES),
    }


def apply_runtime_config(config: dict) -> dict:
    global BOT_INSTANCE_NAME, TOP_N, TIMEOUT, KOMARI_FETCH_WORKERS, SAMPLE_INTERVAL_SECONDS, SAMPLE_RETENTION_HOURS
    global TRAFFIC_SNAPSHOT_RETENTION_DAYS, AI_PACK_CACHE_TTL_SECONDS, TASK_RUN_RETENTION_DAYS, NODE_DAILY_USAGE_RETENTION_DAYS
    global KOMARI_BASE_URL, TELEGRAM_CHAT_ID, TELEGRAM_ALERT_CHAT_ID, AI_API_BASE, AI_MODEL
    global ALERTS_ENABLED, ALERT_RECOVERY_NOTIFY, ALERT_COOLDOWN_SECONDS, ALERT_WINDOW_MINUTES, ALERT_NODE_MISSING_SAMPLES
    global ALERT_SILENCE_WINDOWS, ALERT_TOTAL_WINDOW_BYTES, ALERT_NODE_WINDOW_BYTES, ALERT_DAILY_TOTAL_BYTES, ALERT_DAILY_NODE_BYTES
    clean = validate_runtime_config(config)
    BOT_INSTANCE_NAME = clean["bot_instance_name"]
    KOMARI_BASE_URL = clean["komari_base_url"]
    TELEGRAM_CHAT_ID = clean["telegram_chat_id"]
    TELEGRAM_ALERT_CHAT_ID = clean["telegram_alert_chat_id"]
    AI_API_BASE = clean["ai_api_base"]
    AI_MODEL = clean["ai_model"]
    TOP_N = clean["top_n"]
    TIMEOUT = clean["komari_timeout_seconds"]
    KOMARI_FETCH_WORKERS = clean["komari_fetch_workers"]
    SAMPLE_INTERVAL_SECONDS = clean["sample_interval_seconds"]
    SAMPLE_RETENTION_HOURS = clean["sample_retention_hours"]
    TRAFFIC_SNAPSHOT_RETENTION_DAYS = clean["traffic_snapshot_retention_days"]
    AI_PACK_CACHE_TTL_SECONDS = clean["ai_pack_cache_ttl_seconds"]
    TASK_RUN_RETENTION_DAYS = clean["task_run_retention_days"]
    NODE_DAILY_USAGE_RETENTION_DAYS = clean["node_daily_usage_retention_days"]
    ALERTS_ENABLED = clean["alerts_enabled"]
    ALERT_RECOVERY_NOTIFY = clean["alert_recovery_notify"]
    ALERT_COOLDOWN_SECONDS = clean["alert_cooldown_seconds"]
    ALERT_WINDOW_MINUTES = clean["alert_window_minutes"]
    ALERT_NODE_MISSING_SAMPLES = clean["alert_node_missing_samples"]
    ALERT_SILENCE_WINDOWS = clean["alert_silence_windows"]
    ALERT_TOTAL_WINDOW_BYTES = clean["alert_total_window_bytes"]
    ALERT_NODE_WINDOW_BYTES = clean["alert_node_window_bytes"]
    ALERT_DAILY_TOTAL_BYTES = clean["alert_daily_total_bytes"]
    ALERT_DAILY_NODE_BYTES = clean["alert_daily_node_bytes"]
    return clean


def load_runtime_config() -> dict:
    stored = load_json(runtime_config_path(), {})
    if isinstance(stored, dict) and isinstance(stored.get("config"), dict):
        return validate_runtime_config(stored.get("config", {}))
    return validate_runtime_config(stored if isinstance(stored, dict) else {})


def save_runtime_config(config: dict) -> dict:
    ensure_dirs()
    clean = apply_runtime_config(config)
    save_json_atomic(runtime_config_path(), {"version": 1, "config": clean, "updated_at": int(time.time())})
    return clean


def current_runtime_config() -> dict:
    stored = load_json(runtime_config_path(), {})
    stored_config = stored.get("config", {}) if isinstance(stored, dict) else {}
    clean = validate_runtime_config({
        "bot_instance_name": stored_config.get("bot_instance_name", BOT_INSTANCE_NAME),
        "komari_base_url": stored_config.get("komari_base_url", KOMARI_BASE_URL),
        "telegram_chat_id": stored_config.get("telegram_chat_id", TELEGRAM_CHAT_ID),
        "telegram_alert_chat_id": stored_config.get("telegram_alert_chat_id", TELEGRAM_ALERT_CHAT_ID),
        "ai_api_base": stored_config.get("ai_api_base", AI_API_BASE),
        "ai_model": stored_config.get("ai_model", AI_MODEL),
        "top_n": stored_config.get("top_n", TOP_N),
        "komari_timeout_seconds": stored_config.get("komari_timeout_seconds", TIMEOUT),
        "komari_fetch_workers": stored_config.get("komari_fetch_workers", KOMARI_FETCH_WORKERS),
        "sample_interval_seconds": stored_config.get("sample_interval_seconds", SAMPLE_INTERVAL_SECONDS),
        "sample_retention_hours": stored_config.get("sample_retention_hours", SAMPLE_RETENTION_HOURS),
        "traffic_snapshot_retention_days": stored_config.get("traffic_snapshot_retention_days", TRAFFIC_SNAPSHOT_RETENTION_DAYS),
        "ai_pack_cache_ttl_seconds": stored_config.get("ai_pack_cache_ttl_seconds", AI_PACK_CACHE_TTL_SECONDS),
        "task_run_retention_days": stored_config.get("task_run_retention_days", TASK_RUN_RETENTION_DAYS),
        "node_daily_usage_retention_days": stored_config.get("node_daily_usage_retention_days", NODE_DAILY_USAGE_RETENTION_DAYS),
        "alerts_enabled": stored_config.get("alerts_enabled", ALERTS_ENABLED),
        "alert_recovery_notify": stored_config.get("alert_recovery_notify", ALERT_RECOVERY_NOTIFY),
        "alert_cooldown_seconds": stored_config.get("alert_cooldown_seconds", ALERT_COOLDOWN_SECONDS),
        "alert_window_minutes": stored_config.get("alert_window_minutes", ALERT_WINDOW_MINUTES),
        "alert_node_missing_samples": stored_config.get("alert_node_missing_samples", ALERT_NODE_MISSING_SAMPLES),
        "alert_silence_windows": stored_config.get("alert_silence_windows", ALERT_SILENCE_WINDOWS),
        "alert_total_window_bytes": stored_config.get("alert_total_window_bytes", ALERT_TOTAL_WINDOW_BYTES),
        "alert_node_window_bytes": stored_config.get("alert_node_window_bytes", ALERT_NODE_WINDOW_BYTES),
        "alert_daily_total_bytes": stored_config.get("alert_daily_total_bytes", ALERT_DAILY_TOTAL_BYTES),
        "alert_daily_node_bytes": stored_config.get("alert_daily_node_bytes", ALERT_DAILY_NODE_BYTES),
    })
    def field(key: str, label: str, field_type: str = "text", note: str = "", **extra) -> dict:
        value = clean[key]
        if field_type == "bytes":
            value = bytes_config_text(value)
        item = {"key": key, "label": label, "type": field_type, "value": value, "note": note}
        item.update(extra)
        return item

    return {
        "path": runtime_config_path(),
        "values": clean,
        "editable": [
            field("bot_instance_name", "实例名", note="用于 Web 面板和 Telegram 报表标题。", group="基础"),
            field("komari_base_url", "Komari 地址", note="只保存面板访问地址，不包含 API token。", group="基础"),
            field("telegram_chat_id", "默认推送 Chat", note="只保存 Chat ID，不包含 Bot Token。", group="基础"),
            field("telegram_alert_chat_id", "告警推送 Chat", note="留空时沿用默认推送 Chat。", group="基础"),
            field("ai_api_base", "AI 接口地址", note="只保存接口地址，不包含 API Key。", group="基础"),
            field("ai_model", "AI 模型", note="例如 gpt-5.4-mini，可直接显示和修改。", group="基础"),
            field("top_n", "Top 节点数", "number", "影响 Top 报表和面板排行。", min=1, max=50, group="基础"),
            field("komari_timeout_seconds", "Komari 超时（秒）", "number", "探针接口慢时可适当调大。", min=3, max=120, group="基础"),
            field("komari_fetch_workers", "节点并发数", "number", "节点很多时可适当调大，太大会增加探针压力。", min=1, max=32, group="基础"),
            field("sample_interval_seconds", "采样间隔（秒）", "number", "用于短时间 Top 和告警检测。", min=60, max=3600, group="基础"),
            field("sample_retention_hours", "兼容采样缓存（小时）", "number", "仅用于短期兼容状态；流量统计使用 SQLite 连续快照。", min=1, max=168, group="基础"),
            field("traffic_snapshot_retention_days", "连续快照保留天数", "number", "用于最近窗口、小时分布和当前周期统计。", min=1, max=3650, group="基础"),
            field("ai_pack_cache_ttl_seconds", "AI 缓存 TTL（秒）", "number", "0 表示每次实时生成。", min=0, max=86400, group="基础"),
            field("task_run_retention_days", "任务记录保留天数", "number", "0 表示关闭清理。", min=0, max=3650, group="基础"),
            field("node_daily_usage_retention_days", "每日汇总保留天数", "number", "0 表示关闭清理；默认保留 365 天。", min=0, max=3650, group="基础"),
            field("alerts_enabled", "启用告警", "boolean", "关闭后不会产生新的告警事件。", group="告警"),
            field("alert_recovery_notify", "恢复后通知", "boolean", "异常恢复时是否发送恢复提示。", group="告警"),
            field("alert_cooldown_seconds", "重复提醒冷却（秒）", "number", "流量阈值异常多久后再次提醒；节点离线只在首次触发时提醒。", min=0, max=86400, group="告警"),
            field("alert_window_minutes", "窗口检测范围（分钟）", "number", "用于最近窗口流量阈值。", min=5, max=1440, group="告警"),
            field("alert_node_missing_samples", "节点失败次数阈值", "number", "节点连续采样失败达到这个次数才告警。", min=1, max=20, group="告警"),
            field("alert_silence_windows", "静默时段", note="格式如 23:00-07:00；多个用逗号分隔。", group="告警"),
            field("alert_total_window_bytes", "窗口总流量阈值", "bytes", "留空或 0 表示关闭；支持 MiB/GiB/TiB。", group="告警"),
            field("alert_node_window_bytes", "窗口单节点阈值", "bytes", "留空或 0 表示关闭；支持 MiB/GiB/TiB。", group="告警"),
            field("alert_daily_total_bytes", "今日总流量阈值", "bytes", "留空或 0 表示关闭；支持 MiB/GiB/TiB。", group="告警"),
            field("alert_daily_node_bytes", "今日单节点阈值", "bytes", "留空或 0 表示关闭；支持 MiB/GiB/TiB。", group="告警"),
        ],
    }


try:
    stored_runtime = load_json(runtime_config_path(), {})
    if isinstance(stored_runtime, dict) and isinstance(stored_runtime.get("config"), dict):
        apply_runtime_config(stored_runtime.get("config", {}))
except Exception as e:
    logging.warning(f"Failed to load/apply runtime config at startup: {e}")


def traffic_db_connect():
    ensure_dirs()
    conn = sqlite3.connect(TRAFFIC_DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def traffic_db_session():
    conn = traffic_db_connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_db_initialized = False

def init_traffic_db():
    global _db_initialized
    # Reset flag if database doesn't exist (for tests)
    if not os.path.exists(TRAFFIC_DB_PATH):
        _db_initialized = False
    if _db_initialized:
        return
    with traffic_db_session() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version INTEGER PRIMARY KEY,
              applied_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS node_daily_usage (
              day TEXT NOT NULL,
              uuid TEXT NOT NULL,
              name TEXT NOT NULL,
              up INTEGER NOT NULL DEFAULT 0,
              down INTEGER NOT NULL DEFAULT 0,
              total INTEGER NOT NULL DEFAULT 0,
              source TEXT NOT NULL DEFAULT 'history',
              source_from TEXT NOT NULL DEFAULT '',
              source_to TEXT NOT NULL DEFAULT '',
              reset_warnings TEXT NOT NULL DEFAULT '[]',
              skipped TEXT NOT NULL DEFAULT '[]',
              updated_at INTEGER NOT NULL,
              PRIMARY KEY (day, uuid)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS traffic_snapshots (
              ts INTEGER NOT NULL,
              uuid TEXT NOT NULL,
              name TEXT NOT NULL,
              up INTEGER NOT NULL DEFAULT 0,
              down INTEGER NOT NULL DEFAULT 0,
              skipped TEXT NOT NULL DEFAULT '[]',
              PRIMARY KEY (ts, uuid)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS traffic_segments (
              sample_from_ts INTEGER NOT NULL,
              sample_to_ts INTEGER NOT NULL,
              uuid TEXT NOT NULL,
              name TEXT NOT NULL,
              up INTEGER NOT NULL DEFAULT 0,
              down INTEGER NOT NULL DEFAULT 0,
              skipped TEXT NOT NULL DEFAULT '[]',
              reset_warnings TEXT NOT NULL DEFAULT '[]',
              updated_at INTEGER NOT NULL,
              PRIMARY KEY (sample_from_ts, sample_to_ts, uuid)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              type TEXT NOT NULL,
              source TEXT NOT NULL,
              status TEXT NOT NULL,
              summary TEXT NOT NULL DEFAULT '',
              error TEXT NOT NULL DEFAULT '',
              started_at INTEGER NOT NULL,
              finished_at INTEGER NOT NULL,
              duration_ms INTEGER NOT NULL DEFAULT 0,
              metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_type_started ON task_runs(type, started_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_started ON task_runs(started_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_snapshots_uuid_ts ON traffic_snapshots(uuid, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_snapshots_ts ON traffic_snapshots(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_segments_range ON traffic_segments(sample_from_ts, sample_to_ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_segments_uuid_range ON traffic_segments(uuid, sample_from_ts, sample_to_ts)")
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)", (now_dt().isoformat(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)", (now_dt().isoformat(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(3, ?)", (now_dt().isoformat(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(4, ?)", (now_dt().isoformat(),))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_qa_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              question TEXT NOT NULL,
              answer TEXT NOT NULL,
              data_pack_hash TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL,
              duration_ms INTEGER NOT NULL DEFAULT 0,
              metadata TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_qa_history_created ON ai_qa_history(created_at DESC)")
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(5, ?)", (now_dt().isoformat(),))
    _db_initialized = True


def _json_dumps_compact(data) -> str:
    try:
        return json.dumps(data if data is not None else {}, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return json.dumps({"value": str(data)}, ensure_ascii=False, separators=(",", ":"))


def _json_loads_object(text: str) -> dict:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {"value": value}
    except Exception:
        return {}


def redact_sensitive_text(value) -> str:
    text = str(value or "")
    secrets_to_mask = [
        TELEGRAM_BOT_TOKEN,
        KOMARI_API_TOKEN,
        AI_API_KEY,
        TELEGRAM_CHAT_ID,
        TELEGRAM_ALERT_CHAT_ID,
    ]
    for secret in secrets_to_mask:
        secret = str(secret or "").strip()
        if not secret:
            continue
        masked = "***" if len(secret) <= 6 else f"{secret[:3]}***{secret[-3:]}"
        text = text.replace(secret, masked)
    return text


def redact_sensitive_data(value):
    if isinstance(value, dict):
        return {str(key): redact_sensitive_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def record_task_run(
    task_type: str,
    source: str,
    status: str,
    started_at: int | float | None = None,
    finished_at: int | float | None = None,
    summary: str = "",
    error: str = "",
    metadata: dict | None = None,
) -> dict:
    init_traffic_db()
    now_raw = time.time()
    started_raw = float(started_at if started_at is not None else now_raw)
    finished_raw = float(finished_at if finished_at is not None else now_raw)
    started = int(started_raw)
    finished = int(finished_raw)
    duration_ms = max(0, int((finished_raw - started_raw) * 1000))
    task_type = str(task_type or "other").strip().lower()[:40] or "other"
    source = str(source or "unknown").strip()[:120] or "unknown"
    status = str(status or "unknown").strip().lower()[:40] or "unknown"
    summary = redact_sensitive_text(summary)[:600]
    error = redact_sensitive_text(error)[:1200]
    safe_metadata = redact_sensitive_data(metadata or {})
    metadata_json = _json_dumps_compact(safe_metadata)
    with traffic_db_session() as conn:
        cur = conn.execute(
            """
            INSERT INTO task_runs(type, source, status, summary, error, started_at, finished_at, duration_ms, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_type, source, status, summary, error, started, finished, duration_ms, metadata_json),
        )
        run_id = int(cur.lastrowid)
    return {
        "id": run_id,
        "type": task_type,
        "source": source,
        "status": status,
        "summary": summary,
        "error": error,
        "started_at": started,
        "finished_at": finished,
        "duration_ms": duration_ms,
        "metadata": safe_metadata,
    }


def safe_record_task_run(*args, **kwargs) -> dict | None:
    try:
        return record_task_run(*args, **kwargs)
    except Exception:
        logging.exception("failed to record task run")
        return None


def _task_run_filters(task_type: str | None = None, source_prefix: str | None = None, metadata_key: str | None = None, metadata_value=None) -> tuple[str, list]:
    task_type = str(task_type or "").strip().lower()
    params: list = []
    where_clauses = []
    if task_type:
        where_clauses.append("type = ?")
        params.append(task_type)
    if source_prefix:
        where_clauses.append("source LIKE ? || '%'")
        params.append(source_prefix)
    if metadata_key:
        where_clauses.append("json_extract(metadata, ?) = ?")
        params.append(f"$.{metadata_key}")
        params.append(str(metadata_value))
    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    return where, params


def list_task_runs(limit: int = 50, task_type: str | None = None, source_prefix: str | None = None, metadata_key: str | None = None, metadata_value=None, offset: int = 0) -> list[dict]:
    init_traffic_db()
    limit = min(200, max(1, int(limit or 50)))
    offset = max(0, int(offset or 0))
    where, params = _task_run_filters(task_type, source_prefix, metadata_key, metadata_value)
    params.append(limit)
    params.append(offset)
    with traffic_db_session() as conn:
        rows = conn.execute(
            f"""
            SELECT id, type, source, status, summary, error, started_at, finished_at, duration_ms, metadata
            FROM task_runs
            {where}
            ORDER BY started_at DESC, id DESC
            LIMIT ?
            OFFSET ?
            """,
            params,
        ).fetchall()
    runs = []
    for row in rows:
        runs.append({
            "id": int(row["id"]),
            "type": row["type"],
            "source": row["source"],
            "status": row["status"],
            "summary": row["summary"],
            "error": row["error"],
            "started_at": int(row["started_at"] or 0),
            "finished_at": int(row["finished_at"] or 0),
            "duration_ms": int(row["duration_ms"] or 0),
            "metadata": _json_loads_object(row["metadata"]),
        })
    return runs


def count_task_runs(
    task_type: str | None = None,
    before_ts: int | float | None = None,
    source_prefix: str | None = None,
    metadata_key: str | None = None,
    metadata_value=None,
) -> int:
    init_traffic_db()
    where, params = _task_run_filters(task_type, source_prefix, metadata_key, metadata_value)
    if before_ts is not None:
        where = f"{where} AND started_at < ?" if where else "WHERE started_at < ?"
        params.append(int(before_ts))
    with traffic_db_session() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM task_runs {where}", params).fetchone()
    return int(row["c"] or 0)


def prune_task_runs(retention_days: int | None = None, now_ts: int | float | None = None) -> dict:
    init_traffic_db()
    days = TASK_RUN_RETENTION_DAYS if retention_days is None else int(retention_days)
    if days < 0:
        raise RuntimeError("retention_days must be >= 0")
    if days == 0:
        return {
            "enabled": False,
            "retention_days": 0,
            "cutoff": 0,
            "cutoff_text": "",
            "deleted": 0,
            "remaining": count_task_runs(),
        }
    now_value = int(now_ts if now_ts is not None else time.time())
    cutoff = now_value - days * 86400
    with traffic_db_session() as conn:
        cur = conn.execute("DELETE FROM task_runs WHERE started_at < ?", (cutoff,))
        deleted = int(cur.rowcount or 0)
        row = conn.execute("SELECT COUNT(*) AS c FROM task_runs").fetchone()
        remaining = int(row["c"] or 0)
    return {
        "enabled": True,
        "retention_days": days,
        "cutoff": cutoff,
        "cutoff_text": datetime.fromtimestamp(cutoff, TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "deleted": deleted,
        "remaining": remaining,
    }


def traffic_db_table_counts() -> dict:
    init_traffic_db()
    tables = ("node_daily_usage", "period_rollups", "traffic_snapshots", "traffic_segments", "task_runs")
    counts = {}
    with traffic_db_session() as conn:
        for table in tables:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                counts[table] = 0
                continue
            row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            counts[table] = int(row["c"] or 0)
    return counts


def period_rollups_table_status() -> dict:
    init_traffic_db()
    with traffic_db_session() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='period_rollups'"
        ).fetchone() is not None
        rows = 0
        if exists:
            row = conn.execute("SELECT COUNT(*) AS c FROM period_rollups").fetchone()
            rows = int(row["c"] or 0)
    return {"exists": exists, "rows": rows, "unused": True}


def drop_period_rollups_table() -> dict:
    init_traffic_db()
    before = period_rollups_table_status()
    with traffic_db_session() as conn:
        conn.execute("DROP TABLE IF EXISTS period_rollups")
    after = period_rollups_table_status()
    return {"before": before, "after": after, "dropped": before.get("exists", False)}


def traffic_db_healthcheck() -> dict:
    init_traffic_db()
    with traffic_db_session() as conn:
        quick = conn.execute("PRAGMA quick_check").fetchone()
        quick_result = str(quick[0] if quick else "")
        if quick_result.lower() != "ok":
            logging.error("SQLite quick_check failed: %s, attempting integrity_check", quick_result)
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchall()
                integrity_lines = [str(row[0]) for row in integrity]
                if len(integrity_lines) == 1 and integrity_lines[0].lower() == "ok":
                    logging.warning("integrity_check passed despite quick_check failure")
                else:
                    logging.error("integrity_check issues: %s", "; ".join(integrity_lines[:5]))
                    raise RuntimeError(f"SQLite corrupted: {'; '.join(integrity_lines[:3])}")
            except Exception as e:
                logging.exception("integrity_check failed")
                raise RuntimeError(f"SQLite quick_check failed: {quick_result}")
    counts = traffic_db_table_counts()
    size = os.path.getsize(TRAFFIC_DB_PATH) if os.path.exists(TRAFFIC_DB_PATH) else 0
    return {
        "ok": True,
        "path": TRAFFIC_DB_PATH,
        "exists": os.path.exists(TRAFFIC_DB_PATH),
        "size": size,
        "size_human": human_bytes(size),
        "daily_rows": counts.get("node_daily_usage", 0),
        "snapshot_rows": counts.get("traffic_snapshots", 0),
        "segment_rows": counts.get("traffic_segments", 0),
        "task_runs": counts.get("task_runs", 0),
        "table_counts": counts,
        "quick_check": "ok",
        **traffic_sample_lag_status(),
    }


def traffic_db_maintenance_status(retention_days: int | None = None, now_ts: int | float | None = None) -> dict:
    init_traffic_db()
    days = TASK_RUN_RETENTION_DAYS if retention_days is None else int(retention_days)
    if days < 0:
        raise RuntimeError("retention_days must be >= 0")
    now_value = int(now_ts if now_ts is not None else time.time())
    cutoff = now_value - days * 86400 if days else 0
    size = os.path.getsize(TRAFFIC_DB_PATH) if os.path.exists(TRAFFIC_DB_PATH) else 0
    counts = traffic_db_table_counts()
    daily_days = NODE_DAILY_USAGE_RETENTION_DAYS
    daily_cutoff = (datetime.fromtimestamp(now_value, TZ).date() - timedelta(days=daily_days)).strftime("%Y-%m-%d") if daily_days else ""
    segment_cutoff = now_value - TRAFFIC_SNAPSHOT_RETENTION_DAYS * 86400
    period_rollups = period_rollups_table_status()
    with traffic_db_session() as conn:
        old_segments = conn.execute(
            "SELECT COUNT(*) AS c FROM traffic_segments WHERE sample_from_ts < ?",
            (segment_cutoff,),
        ).fetchone()
        old_daily = 0
        if daily_cutoff:
            row = conn.execute("SELECT COUNT(*) AS c FROM node_daily_usage WHERE day < ?", (daily_cutoff,)).fetchone()
            old_daily = int(row["c"] or 0)
    return {
        "retention_days": days,
        "retention_enabled": days > 0,
        "snapshot_retention_days": TRAFFIC_SNAPSHOT_RETENTION_DAYS,
        "daily_retention_days": daily_days,
        "cutoff": cutoff,
        "cutoff_text": datetime.fromtimestamp(cutoff, TZ).strftime("%Y-%m-%d %H:%M:%S %Z") if cutoff else "",
        "daily_cutoff_day": daily_cutoff,
        "old_task_runs": count_task_runs(before_ts=cutoff) if cutoff else 0,
        "old_traffic_segments": int(old_segments["c"] or 0),
        "old_daily_usage": old_daily,
        "task_runs": counts.get("task_runs", 0),
        "period_rollups": period_rollups,
        "table_counts": counts,
        "db_size": size,
        "db_size_human": human_bytes(size),
    }


def vacuum_traffic_db() -> dict:
    init_traffic_db()
    before = os.path.getsize(TRAFFIC_DB_PATH) if os.path.exists(TRAFFIC_DB_PATH) else 0
    conn = traffic_db_connect()
    try:
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
    after = os.path.getsize(TRAFFIC_DB_PATH) if os.path.exists(TRAFFIC_DB_PATH) else 0
    return {
        "before_size": before,
        "after_size": after,
        "before_size_human": human_bytes(before),
        "after_size_human": human_bytes(after),
        "saved_bytes": max(0, before - after),
        "saved_human": human_bytes(max(0, before - after)),
        "table_counts": traffic_db_table_counts(),
    }


def latest_task_run(task_type: str | None = None, source_prefix: str | None = None, metadata_key: str | None = None, metadata_value=None) -> dict | None:
    runs = list_task_runs(limit=1, task_type=task_type, source_prefix=source_prefix, metadata_key=metadata_key, metadata_value=metadata_value)
    return runs[0] if runs else None


def run_with_task_record(task_type: str, source: str, func, summary_func=None, metadata: dict | None = None):
    started = time.time()
    try:
        result = func()
        summary = ""
        if summary_func:
            summary = str(summary_func(result) or "")
        elif isinstance(result, dict):
            summary = str(result.get("summary") or result.get("label") or "")
        run = safe_record_task_run(
            task_type,
            source,
            "success",
            started_at=started,
            finished_at=time.time(),
            summary=summary,
            metadata=metadata or {},
        )
        if isinstance(result, dict) and run:
            result = dict(result)
            result["task_run"] = run
        return result
    except Exception as exc:
        safe_record_task_run(
            task_type,
            source,
            "failed",
            started_at=started,
            finished_at=time.time(),
            summary="",
            error=str(exc),
            metadata=metadata or {},
        )
        raise


def get_ai_data_pack_hash(data_pack: dict) -> str:
    """
    计算数据包的 MD5 哈希作为指纹，用于标识同一批数据。
    """
    try:
        data_text = json.dumps(data_pack, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(data_text.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def save_ai_qa_record(
    question: str,
    answer: str,
    data_pack_hash: str = "",
    duration_ms: int = 0,
    metadata: dict | None = None,
) -> int:
    """
    保存 AI 问答记录到数据库。

    参数：
      - question: 用户问题
      - answer: AI 回答
      - data_pack_hash: 数据包哈希（可选）
      - duration_ms: 响应时长（毫秒）
      - metadata: 附加元数据（可选）

    返回：
      - 记录 ID (lastrowid)，失败时返回 0
    """
    try:
        init_traffic_db()
        created_at = int(time.time())
        question_text = str(question or "").strip()[:2000]
        answer_text = str(answer or "").strip()[:10000]
        hash_text = str(data_pack_hash or "").strip()[:64]
        duration = max(0, int(duration_ms or 0))
        metadata_json = _json_dumps_compact(metadata or {})

        with traffic_db_session() as conn:
            cur = conn.execute(
                """
                INSERT INTO ai_qa_history(question, answer, data_pack_hash, created_at, duration_ms, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (question_text, answer_text, hash_text, created_at, duration, metadata_json),
            )
            return int(cur.lastrowid)
    except Exception:
        logging.exception("failed to save ai_qa_record")
        return 0


def list_ai_qa_history(limit: int = 20, offset: int = 0) -> list[dict]:
    """
    查询最近的 AI 问答历史记录。

    参数：
      - limit: 返回记录数（默认 20）
      - offset: 跳过记录数（默认 0）

    返回：
      - list[dict]，包含 id, question, answer, created_at, created_at_text, duration_ms
    """
    init_traffic_db()
    limit = min(200, max(1, int(limit or 20)))
    offset = max(0, int(offset or 0))

    with traffic_db_session() as conn:
        rows = conn.execute(
            """
            SELECT id, question, answer, data_pack_hash, created_at, duration_ms, metadata
            FROM ai_qa_history
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

    history = []
    for row in rows:
        created_at = int(row["created_at"] or 0)
        created_at_text = datetime.fromtimestamp(created_at, TZ).strftime("%Y-%m-%d %H:%M:%S %Z") if created_at else ""

        history.append({
            "id": int(row["id"]),
            "question": str(row["question"] or ""),
            "answer": str(row["answer"] or ""),
            "data_pack_hash": str(row["data_pack_hash"] or ""),
            "created_at": created_at,
            "created_at_text": created_at_text,
            "duration_ms": int(row["duration_ms"] or 0),
            "metadata": _json_loads_object(row["metadata"]),
        })

    return history


def delete_old_ai_qa_records(cutoff_timestamp: int) -> int:
    """
    删除指定时间戳之前的 AI 问答历史记录。

    参数：
      - cutoff_timestamp: 截止时间戳（Unix timestamp），早于此时间的记录将被删除

    返回：
      - 删除的记录数
    """
    try:
        init_traffic_db()
        cutoff = int(cutoff_timestamp or 0)

        with traffic_db_session() as conn:
            cur = conn.execute(
                """
                DELETE FROM ai_qa_history
                WHERE created_at < ?
                """,
                (cutoff,),
            )
            return int(cur.rowcount)
    except Exception:
        logging.exception("failed to delete old ai_qa_records")
        return 0


def save_traffic_snapshot(ts: int | float, nodes_map: dict, skipped: list[str] | None = None):
    if not isinstance(nodes_map, dict):
        return
    init_traffic_db()
    ts_int = int(ts)
    skipped_json = json.dumps([str(item) for item in (skipped or [])], ensure_ascii=False)
    rows = []
    for uuid, item in nodes_map.items():
        if not isinstance(item, dict):
            continue
        uuid_text = str(uuid or "").strip()
        if not uuid_text:
            continue
        rows.append((
            ts_int,
            uuid_text,
            str(item.get("name") or uuid_text),
            max(0, int(item.get("up", 0) or 0)),
            max(0, int(item.get("down", 0) or 0)),
            skipped_json,
        ))
    if not rows:
        return
    with traffic_db_session() as conn:
        conn.executemany(
            """
            INSERT INTO traffic_snapshots(ts, uuid, name, up, down, skipped)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ts, uuid) DO UPDATE SET
              name=excluded.name,
              up=excluded.up,
              down=excluded.down,
              skipped=excluded.skipped
            """,
            rows,
        )


def prune_traffic_snapshots(now_ts: int | float | None = None, retention_days: int | None = None) -> int:
    init_traffic_db()
    days = TRAFFIC_SNAPSHOT_RETENTION_DAYS if retention_days is None else int(retention_days)
    if days <= 0:
        return 0
    cutoff = int(now_ts if now_ts is not None else time.time()) - days * 86400
    with traffic_db_session() as conn:
        cur = conn.execute("DELETE FROM traffic_snapshots WHERE ts < ?", (cutoff,))
        return int(cur.rowcount or 0)


def prune_traffic_segments(retention_days: int | None = None, now_ts: int | float | None = None) -> int:
    init_traffic_db()
    days = TRAFFIC_SNAPSHOT_RETENTION_DAYS if retention_days is None else int(retention_days)
    if days <= 0:
        return 0
    cutoff = int(now_ts if now_ts is not None else time.time()) - days * 86400
    with traffic_db_session() as conn:
        cur = conn.execute("DELETE FROM traffic_segments WHERE sample_from_ts < ?", (cutoff,))
        return int(cur.rowcount or 0)


def traffic_snapshot_rows_between(from_ts: int | float, to_ts: int | float) -> list[dict]:
    init_traffic_db()
    start = int(from_ts)
    end = int(to_ts)
    with traffic_db_session() as conn:
        rows = conn.execute(
            """
            SELECT ts, uuid, COALESCE(NULLIF(name, ''), uuid) AS name, up, down, skipped
            FROM traffic_snapshots
            WHERE ts >= (
                SELECT COALESCE(MAX(ts), ?)
                FROM traffic_snapshots
                WHERE ts <= ?
            )
            AND ts <= (
                SELECT COALESCE(MIN(ts), ?)
                FROM traffic_snapshots
                WHERE ts >= ?
            )
            ORDER BY ts ASC, uuid ASC
            """,
            (start, start, end, end),
        ).fetchall()
    return [
        {
            "ts": int(row["ts"] or 0),
            "uuid": str(row["uuid"]),
            "name": str(row["name"] or row["uuid"]),
            "up": int(row["up"] or 0),
            "down": int(row["down"] or 0),
            "skipped": _json_loads_list(row["skipped"]),
        }
        for row in rows
    ]


def traffic_snapshot_rows_all(from_ts: int | float | None = None, to_ts: int | float | None = None) -> list[dict]:
    init_traffic_db()
    where = []
    params: list[int] = []
    if from_ts is not None:
        where.append("ts >= ?")
        params.append(int(from_ts))
    if to_ts is not None:
        where.append("ts <= ?")
        params.append(int(to_ts))
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with traffic_db_session() as conn:
        rows = conn.execute(
            f"""
            SELECT ts, uuid, COALESCE(NULLIF(name, ''), uuid) AS name, up, down, skipped
            FROM traffic_snapshots
            {where_sql}
            ORDER BY ts ASC, uuid ASC
            """,
            params,
        ).fetchall()
    return [
        {
            "ts": int(row["ts"] or 0),
            "uuid": str(row["uuid"]),
            "name": str(row["name"] or row["uuid"]),
            "up": int(row["up"] or 0),
            "down": int(row["down"] or 0),
            "skipped": _json_loads_list(row["skipped"]),
        }
        for row in rows
    ]


def traffic_snapshot_rows_for_timestamps(timestamps: list[int]) -> list[dict]:
    values = sorted({int(ts) for ts in timestamps if int(ts or 0) > 0})
    if not values:
        return []
    init_traffic_db()
    placeholders = ",".join("?" for _ in values)
    with traffic_db_session() as conn:
        rows = conn.execute(
            f"""
            SELECT ts, uuid, COALESCE(NULLIF(name, ''), uuid) AS name, up, down, skipped
            FROM traffic_snapshots
            WHERE ts IN ({placeholders})
            ORDER BY ts ASC, uuid ASC
            """,
            values,
        ).fetchall()
    return [
        {
            "ts": int(row["ts"] or 0),
            "uuid": str(row["uuid"]),
            "name": str(row["name"] or row["uuid"]),
            "up": int(row["up"] or 0),
            "down": int(row["down"] or 0),
            "skipped": _json_loads_list(row["skipped"]),
        }
        for row in rows
    ]


def latest_traffic_snapshot_ts() -> int:
    init_traffic_db()
    with traffic_db_session() as conn:
        row = conn.execute("SELECT MAX(ts) AS ts FROM traffic_snapshots").fetchone()
    return int(row["ts"] or 0) if row and row["ts"] is not None else 0


def latest_traffic_snapshot_timestamps(limit: int = 2, to_ts: int | float | None = None) -> list[int]:
    init_traffic_db()
    params: list[int] = []
    where = ""
    if to_ts is not None:
        where = "WHERE ts <= ?"
        params.append(int(to_ts))
    params.append(max(1, int(limit)))
    with traffic_db_session() as conn:
        rows = conn.execute(
            f"""
            SELECT ts
            FROM (SELECT DISTINCT ts FROM traffic_snapshots {where} ORDER BY ts DESC LIMIT ?)
            ORDER BY ts ASC
            """,
            params,
        ).fetchall()
    return [int(row["ts"] or 0) for row in rows]


def traffic_sample_lag_status(now_ts: int | float | None = None) -> dict:
    latest_ts = latest_traffic_snapshot_ts()
    now_value = int(now_ts if now_ts is not None else time.time())
    lag = max(0, now_value - latest_ts) if latest_ts else None
    return {
        "latest_sample_ts": latest_ts,
        "latest_sample_at": datetime.fromtimestamp(latest_ts, TZ).strftime("%Y-%m-%d %H:%M:%S %Z") if latest_ts else "",
        "sample_lag_seconds": lag,
        "sample_stale": bool(lag is not None and lag > max(1, int(SAMPLE_INTERVAL_SECONDS)) * 2),
        "sample_interval_seconds": int(SAMPLE_INTERVAL_SECONDS),
    }


def _json_loads_list(text: str) -> list:
    try:
        value = json.loads(text or "[]")
        return value if isinstance(value, list) else []
    except Exception:
        return []


def snapshots_to_samples(rows: list[dict]) -> list[dict]:
    samples_by_ts: dict[int, dict] = {}
    for row in rows:
        ts = int(row.get("ts", 0) or 0)
        if ts <= 0:
            continue
        sample = samples_by_ts.setdefault(ts, {"ts": ts, "nodes": {}, "skipped": []})
        uuid = str(row.get("uuid") or "")
        if uuid:
            sample["nodes"][uuid] = {
                "name": row.get("name") or uuid,
                "up": int(row.get("up", 0) or 0),
                "down": int(row.get("down", 0) or 0),
            }
        sample["skipped"] = list(dict.fromkeys(sample.get("skipped", []) + [str(item) for item in (row.get("skipped") or [])]))
    return [samples_by_ts[ts] for ts in sorted(samples_by_ts)]


def _scale_bytes_by_overlap(value: int, overlap_seconds: int, segment_seconds: int) -> int:
    if segment_seconds <= 0 or overlap_seconds <= 0:
        return 0
    value = max(0, int(value or 0))
    return (value * int(overlap_seconds) + segment_seconds // 2) // segment_seconds


def _scale_delta_map(deltas: dict, overlap_seconds: int, segment_seconds: int) -> dict:
    scaled = {}
    for uuid, item in (deltas or {}).items():
        if not isinstance(item, dict):
            continue
        up = _scale_bytes_by_overlap(int(item.get("up", 0) or 0), overlap_seconds, segment_seconds)
        down = _scale_bytes_by_overlap(int(item.get("down", 0) or 0), overlap_seconds, segment_seconds)
        scaled[str(uuid)] = {"name": item.get("name") or str(uuid), "up": up, "down": down}
    return scaled


def _hour_bucket_label_and_end(ts: int) -> tuple[str, int]:
    bucket_start = datetime.fromtimestamp(int(ts), TZ).replace(minute=0, second=0, microsecond=0)
    bucket_end = int((bucket_start + timedelta(hours=1)).timestamp())
    return bucket_start.strftime("%Y-%m-%d %H:00"), bucket_end


def _segment_from_samples(prev: dict, cur: dict) -> dict | None:
    prev_ts = int(prev.get("ts", 0) or 0)
    cur_ts = int(cur.get("ts", 0) or 0)
    if cur_ts <= prev_ts:
        return None
    deltas, segment_warnings = compute_strict_sample_delta_from_maps(cur.get("nodes", {}), prev.get("nodes", {}))
    return {
        "sample_from_ts": prev_ts,
        "sample_to_ts": cur_ts,
        "segment_seconds": cur_ts - prev_ts,
        "nodes": deltas,
        "skipped": [str(item) for item in (prev.get("skipped", []) or []) + (cur.get("skipped", []) or [])],
        "reset_warnings": segment_warnings,
    }


def save_traffic_segments(segments: list[dict]) -> int:
    if not segments:
        return 0
    init_traffic_db()
    updated_at = int(time.time())
    rows = []
    for segment in segments:
        start = int(segment.get("sample_from_ts", 0) or 0)
        end = int(segment.get("sample_to_ts", 0) or 0)
        if end <= start:
            continue
        skipped_json = json.dumps(list(dict.fromkeys(str(item) for item in (segment.get("skipped", []) or []))), ensure_ascii=False)
        reset_json = json.dumps(list(dict.fromkeys(str(item) for item in (segment.get("reset_warnings", []) or []))), ensure_ascii=False)
        for uuid, item in (segment.get("nodes") or {}).items():
            if not isinstance(item, dict):
                continue
            uid = str(uuid or "").strip()
            if not uid:
                continue
            up = max(0, int(item.get("up", 0) or 0))
            down = max(0, int(item.get("down", 0) or 0))
            rows.append((start, end, uid, str(item.get("name") or uid), up, down, skipped_json, reset_json, updated_at))
    if not rows:
        return 0
    with traffic_db_session() as conn:
        conn.executemany(
            """
            INSERT INTO traffic_segments(sample_from_ts, sample_to_ts, uuid, name, up, down, skipped, reset_warnings, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sample_from_ts, sample_to_ts, uuid) DO UPDATE SET
              name=excluded.name,
              up=excluded.up,
              down=excluded.down,
              skipped=excluded.skipped,
              reset_warnings=excluded.reset_warnings,
              updated_at=excluded.updated_at
            """,
            rows,
        )
    return len(rows)


def traffic_segment_rows_between(from_ts: int | float, to_ts: int | float) -> list[dict]:
    init_traffic_db()
    start = int(from_ts)
    end = int(to_ts)
    with traffic_db_session() as conn:
        rows = conn.execute(
            """
            SELECT sample_from_ts, sample_to_ts, uuid, COALESCE(NULLIF(name, ''), uuid) AS name,
                   up, down, skipped, reset_warnings
            FROM traffic_segments
            WHERE sample_to_ts > ? AND sample_from_ts < ?
            ORDER BY sample_from_ts ASC, sample_to_ts ASC, uuid ASC
            """,
            (start, end),
        ).fetchall()
    return [
        {
            "sample_from_ts": int(row["sample_from_ts"] or 0),
            "sample_to_ts": int(row["sample_to_ts"] or 0),
            "uuid": str(row["uuid"]),
            "name": str(row["name"] or row["uuid"]),
            "up": int(row["up"] or 0),
            "down": int(row["down"] or 0),
            "skipped": _json_loads_list(row["skipped"]),
            "reset_warnings": _json_loads_list(row["reset_warnings"]),
        }
        for row in rows
    ]


def build_segments_from_samples(samples: list[dict]) -> list[dict]:
    segments = []
    prev = None
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        if prev is not None:
            segment = _segment_from_samples(prev, sample)
            if segment:
                segments.append(segment)
        prev = sample
    return segments


def snapshot_delta_segments(from_ts: int | float, to_ts: int | float) -> tuple[list[dict], list[dict]]:
    start = int(from_ts)
    end = int(to_ts)
    if end <= start:
        return [], []

    migrate_samples_to_traffic_db()
    rows = traffic_snapshot_rows_between(start, end)
    samples = snapshots_to_samples(rows)
    if len(samples) < 2:
        return samples, []

    segments = []
    prev = samples[0]
    for cur in samples[1:]:
        raw_segment = _segment_from_samples(prev, cur)
        if not raw_segment:
            prev = cur
            continue

        prev_ts = int(raw_segment.get("sample_from_ts", 0) or 0)
        cur_ts = int(raw_segment.get("sample_to_ts", 0) or 0)
        segment_seconds = int(raw_segment.get("segment_seconds", 0) or 0)
        overlap_start = max(prev_ts, start)
        overlap_end = min(cur_ts, end)
        overlap_seconds = overlap_end - overlap_start
        if overlap_seconds <= 0:
            prev = cur
            continue

        segments.append({
            "sample_from_ts": prev_ts,
            "sample_to_ts": cur_ts,
            "from_ts": overlap_start,
            "to_ts": overlap_end,
            "overlap_seconds": overlap_seconds,
            "segment_seconds": segment_seconds,
            "nodes": raw_segment.get("nodes", {}),
            "skipped": raw_segment.get("skipped", []),
            "reset_warnings": raw_segment.get("reset_warnings", []),
        })
        prev = cur
    return samples, segments


def migrate_samples_to_traffic_db() -> int:
    global _SAMPLES_MIGRATED
    if _SAMPLES_MIGRATED:
        return 0
    data = load_samples()
    samples = data.get("samples", []) if isinstance(data, dict) else []
    migrated = 0
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        nodes = sample.get("nodes", {})
        if not isinstance(nodes, dict):
            continue
        try:
            save_traffic_snapshot(int(sample.get("ts", 0) or 0), nodes, sample.get("skipped", []) or [])
            migrated += 1
        except Exception:
            logging.exception("failed to migrate sample to traffic_snapshots")
    _SAMPLES_MIGRATED = True
    return migrated


def _traffic_segment_usage(from_ts: int | float, to_ts: int | float) -> dict:
    start = int(from_ts)
    end = int(to_ts)
    rows = traffic_segment_rows_between(start, end)
    sample_days = sorted({
        day
        for row in rows
        for day in (
            datetime.fromtimestamp(int(row.get("sample_from_ts", 0) or 0), TZ).strftime("%Y-%m-%d"),
            datetime.fromtimestamp(max(int(row.get("sample_to_ts", 0) or 0) - 1, int(row.get("sample_from_ts", 0) or 0)), TZ).strftime("%Y-%m-%d"),
        )
        if int(row.get("sample_from_ts", 0) or 0) > 0 and int(row.get("sample_to_ts", 0) or 0) > 0
    })
    sample_points = set()
    for row in rows:
        sample_points.add(int(row.get("sample_from_ts", 0) or 0))
        sample_points.add(int(row.get("sample_to_ts", 0) or 0))
    sample_points.discard(0)
    sample_from_ts = min(sample_points) if sample_points else 0
    sample_to_ts = max(sample_points) if sample_points else 0
    if not rows:
        return {
            "from_ts": start,
            "to_ts": end,
            "nodes": {},
            "skipped": [],
            "reset_warnings": [],
            "sample_count": 0,
            "segment_count": 0,
            "sample_from_ts": sample_from_ts,
            "sample_to_ts": sample_to_ts,
            "sample_days": sample_days,
            "source": "traffic_segments",
            "source_parts": [],
        }

    totals: dict[str, dict] = {}
    skipped: list[str] = []
    warnings: list[str] = []
    segment_keys = set()
    for row in rows:
        segment_start = int(row.get("sample_from_ts", 0) or 0)
        segment_end = int(row.get("sample_to_ts", 0) or 0)
        segment_seconds = segment_end - segment_start
        overlap_seconds = min(segment_end, end) - max(segment_start, start)
        if segment_seconds <= 0 or overlap_seconds <= 0:
            continue
        uuid = str(row.get("uuid") or "")
        if not uuid:
            continue
        up = _scale_bytes_by_overlap(int(row.get("up", 0) or 0), overlap_seconds, segment_seconds)
        down = _scale_bytes_by_overlap(int(row.get("down", 0) or 0), overlap_seconds, segment_seconds)
        entry = totals.setdefault(uuid, {"name": row.get("name") or uuid, "up": 0, "down": 0})
        entry["name"] = row.get("name") or entry.get("name") or uuid
        entry["up"] += up
        entry["down"] += down
        skipped.extend(row.get("skipped", []))
        warnings.extend(row.get("reset_warnings", []))
        segment_keys.add((segment_start, segment_end))
    return {
        "from_ts": start,
        "to_ts": end,
        "nodes": totals,
        "skipped": list(dict.fromkeys(skipped)),
        "reset_warnings": list(dict.fromkeys(warnings)),
        "sample_count": len(sample_points),
        "segment_count": len(segment_keys),
        "sample_from_ts": sample_from_ts,
        "sample_to_ts": sample_to_ts,
        "sample_days": sample_days,
        "source": "traffic_segments",
        "source_parts": ["traffic_segments"],
    }


def _segment_days(segment: dict) -> list[date]:
    start = int(segment.get("sample_from_ts", 0) or 0)
    end = int(segment.get("sample_to_ts", 0) or 0)
    if end <= start:
        return []
    days = []
    d = datetime.fromtimestamp(start, TZ).date()
    last = datetime.fromtimestamp(end - 1, TZ).date()
    while d <= last:
        days.append(d)
        d += timedelta(days=1)
    return days


def traffic_segments_exist_for_day(day_value: date) -> bool:
    day_start = int(start_of_day(day_value).timestamp())
    day_end = int(start_of_day(day_value + timedelta(days=1)).timestamp())
    init_traffic_db()
    with traffic_db_session() as conn:
        row = conn.execute(
            "SELECT 1 FROM traffic_segments WHERE sample_to_ts > ? AND sample_from_ts < ? LIMIT 1",
            (day_start, day_end),
        ).fetchone()
    return row is not None


def replace_daily_usage(day_str: str, deltas: dict, source: str = "history", source_from: str = "", source_to: str = "", reset_warnings: list[str] | None = None, skipped: list[str] | None = None):
    init_traffic_db()
    with traffic_db_session() as conn:
        conn.execute("DELETE FROM node_daily_usage WHERE day = ?", (day_str,))
    upsert_daily_usage(
        day_str,
        deltas,
        source=source,
        source_from=source_from,
        source_to=source_to,
        reset_warnings=reset_warnings,
        skipped=skipped,
    )


def materialize_daily_usage_from_segments(days: list[date]) -> int:
    """增量更新 daily rollup（只更新有 segments 的天数）"""
    count = 0
    for day in sorted(set(days)):
        day_start = int(start_of_day(day).timestamp())
        day_end = int(start_of_day(day + timedelta(days=1)).timestamp())
        usage = _traffic_segment_usage(day_start, day_end)
        nodes = usage.get("nodes", {})
        if not nodes:
            continue
        upsert_daily_usage(
            day.strftime("%Y-%m-%d"),
            nodes,
            source="traffic_segments",
            source_from=str(int(usage.get("sample_from_ts", 0) or 0)),
            source_to=str(int(usage.get("sample_to_ts", 0) or 0)),
            reset_warnings=usage.get("reset_warnings", []),
            skipped=usage.get("skipped", []),
        )
        count += len(nodes)
    return count


def rebuild_traffic_segments_from_snapshots(from_ts: int | float | None = None, to_ts: int | float | None = None) -> dict:
    rows = traffic_snapshot_rows_all(from_ts, to_ts)
    samples = snapshots_to_samples(rows)
    segments = build_segments_from_samples(samples)
    saved = save_traffic_segments(segments)
    days: list[date] = []
    for segment in segments:
        days.extend(_segment_days(segment))
    daily_rows = materialize_daily_usage_from_segments(days)
    return {"samples": len(samples), "segments": len(segments), "saved_rows": saved, "daily_rows": daily_rows}


def traffic_segments_count() -> int:
    init_traffic_db()
    with traffic_db_session() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM traffic_segments").fetchone()
    return int(row["c"] or 0)


def traffic_snapshot_sample_count() -> int:
    init_traffic_db()
    with traffic_db_session() as conn:
        row = conn.execute("SELECT COUNT(DISTINCT ts) AS c FROM traffic_snapshots").fetchone()
    return int(row["c"] or 0)


def traffic_segment_pair_count() -> int:
    init_traffic_db()
    with traffic_db_session() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM (
              SELECT sample_from_ts, sample_to_ts
              FROM traffic_segments
              GROUP BY sample_from_ts, sample_to_ts
            )
            """
        ).fetchone()
    return int(row["c"] or 0)


def missing_traffic_segment_pairs() -> dict:
    snapshot_rows = traffic_snapshot_rows_all()
    samples = snapshots_to_samples(snapshot_rows)
    expected = [
        (int(samples[index - 1]["ts"]), int(samples[index]["ts"]))
        for index in range(1, len(samples))
    ]
    if not expected:
        return {"samples": len(samples), "expected_pairs": 0, "missing_pairs": []}

    init_traffic_db()
    with traffic_db_session() as conn:
        rows = conn.execute(
            """
            SELECT sample_from_ts, sample_to_ts
            FROM traffic_segments
            GROUP BY sample_from_ts, sample_to_ts
            """
        ).fetchall()
    existing = {(int(row["sample_from_ts"] or 0), int(row["sample_to_ts"] or 0)) for row in rows}
    missing = [pair for pair in expected if pair not in existing]
    return {
        "samples": len(samples),
        "expected_pairs": len(expected),
        "existing_pairs": len(existing),
        "missing_pairs": missing,
    }


def ensure_traffic_segments_backfilled() -> dict:
    migrate_samples_to_traffic_db()
    status = missing_traffic_segment_pairs()
    expected_pairs = int(status.get("expected_pairs", 0) or 0)
    missing_pairs = status.get("missing_pairs", []) or []
    if expected_pairs <= 0:
        return {"skipped": True, "reason": "insufficient_snapshots", "samples": int(status.get("samples", 0) or 0)}
    if not missing_pairs:
        return {"skipped": True, "reason": "segments_current", "samples": int(status.get("samples", 0) or 0), "segment_pairs": expected_pairs}
    result = rebuild_traffic_segments_from_snapshots()
    result["skipped"] = False
    result["expected_pairs"] = expected_pairs
    result["missing_pairs"] = len(missing_pairs)
    return result


def materialize_latest_traffic_segment(current_ts: int | float | None = None) -> dict:
    ts_limit = int(current_ts if current_ts is not None else time.time())
    timestamps = latest_traffic_snapshot_timestamps(2, to_ts=ts_limit)
    if len(timestamps) < 2:
        return {"skipped": True, "reason": "insufficient_snapshots", "samples": len(timestamps)}
    rows = traffic_snapshot_rows_for_timestamps(timestamps)
    samples = snapshots_to_samples(rows)
    if len(samples) < 2:
        return {"skipped": True, "reason": "insufficient_samples", "samples": len(samples)}
    segments = build_segments_from_samples(samples[-2:])
    saved = save_traffic_segments(segments)
    days: list[date] = []
    for segment in segments:
        days.extend(_segment_days(segment))
    daily_rows = materialize_daily_usage_from_segments(days)
    return {
        "skipped": False,
        "samples": len(samples),
        "segments": len(segments),
        "saved_rows": saved,
        "daily_rows": daily_rows,
        "days": sorted({d.strftime("%Y-%m-%d") for d in days}),
    }


def query_usage(from_ts: int | float, to_ts: int | float, group_by: str = "node") -> dict:
    """
    统一流量查询入口：优先使用 traffic_segments，超出覆盖范围时回退到 node_daily_usage rollup。

    group_by:
      - "node": 返回 nodes map + total（默认）
      - "day": 返回 daily buckets（未实现，保留扩展）
      - "hour": 返回 hourly buckets（未实现，保留扩展）
    """
    start = int(from_ts)
    end = int(to_ts)
    if end <= start:
        return {
            "from_ts": start,
            "to_ts": end,
            "group_by": group_by,
            "nodes": {},
            "skipped": [],
            "reset_warnings": [],
            "sample_count": 0,
            "segment_count": 0,
            "sample_from_ts": 0,
            "sample_to_ts": 0,
            "sample_days": [],
            "source": "none",
            "source_parts": [],
        }

    ensure_traffic_segments_backfilled()

    # 尝试纯 segments 路径
    segment_usage = _traffic_segment_usage(start, end)
    sample_from = int(segment_usage.get("sample_from_ts", 0) or 0)
    sample_to = int(segment_usage.get("sample_to_ts", 0) or 0)
    segment_coverage_seconds = sample_to - sample_from if sample_to > sample_from else 0
    requested_seconds = end - start

    # 如果 segments 覆盖充足（>= 90%），直接返回
    coverage_ratio = segment_coverage_seconds / requested_seconds if requested_seconds > 0 else 0
    if coverage_ratio >= 0.9 and group_by == "node":
        segment_usage["group_by"] = group_by
        segment_usage["days"] = segment_usage.get("sample_days", [])
        return segment_usage

    # segments 覆盖不足，检查是否可以混合 rollup
    # 需要：时间戳有效 + 时间戳合理（>= 0，避免1970年之前的测试数据）
    can_use_rollup = False
    if start >= 0 and end >= 0:
        try:
            from_dt = datetime.fromtimestamp(start, TZ)
            to_dt = datetime.fromtimestamp(end, TZ)
            can_use_rollup = True
        except (OSError, ValueError):
            pass

    if not can_use_rollup:
        segment_usage["group_by"] = group_by
        segment_usage["days"] = segment_usage.get("sample_days", [])
        return segment_usage

    # segments 覆盖不足且日期有效，按天混合 rollup
    migrate_history_to_traffic_db()

    # 按天分段：segments 覆盖日用 segments，其他日用 rollup
    from_day = from_dt.date()
    to_day = to_dt.date()

    total_nodes: dict[str, dict] = {}
    source_parts: list[str] = []
    skipped: list[str] = []
    reset_warnings: list[str] = []
    sample_count = 0
    segment_count = 0
    sample_from_ts = 0
    sample_to_ts = 0
    sample_days = set()
    covered_days: list[str] = []

    d = from_day
    while d <= to_day:
        day_start_ts = int(start_of_day(d).timestamp())
        day_end_ts = int(start_of_day(d + timedelta(days=1)).timestamp())
        window_start = max(day_start_ts, start)
        window_end = min(day_end_ts, end)

        if window_end <= window_start:
            d += timedelta(days=1)
            continue

        # 检查该天是否有 segments 覆盖
        day_usage = _traffic_segment_usage(window_start, window_end)
        day_sample_from = int(day_usage.get("sample_from_ts", 0) or 0)
        day_sample_to = int(day_usage.get("sample_to_ts", 0) or 0)
        day_coverage = day_sample_to - day_sample_from if day_sample_to > day_sample_from else 0
        day_requested = window_end - window_start

        if day_coverage >= day_requested * 0.5:
            # 该天用 segments
            for uuid, item in (day_usage.get("nodes") or {}).items():
                _add_usage_to_node_map(total_nodes, str(uuid), str(item.get("name") or uuid), int(item.get("up", 0) or 0), int(item.get("down", 0) or 0))
            source_parts.append("traffic_segments")
            skipped.extend(day_usage.get("skipped", []))
            reset_warnings.extend(day_usage.get("reset_warnings", []))
            sample_count += int(day_usage.get("sample_count", 0) or 0)
            segment_count += int(day_usage.get("segment_count", 0) or 0)
            if day_sample_from > 0:
                sample_from_ts = min([ts for ts in (sample_from_ts, day_sample_from) if ts] or [0])
            sample_to_ts = max(sample_to_ts, day_sample_to)
            sample_days.update(str(item) for item in (day_usage.get("sample_days", []) or []))
            covered_days.append(d.strftime("%Y-%m-%d"))
        else:
            # 该天用 rollup
            rollup = aggregate_daily_usage(d, d)
            if rollup:
                for uuid, item in rollup.items():
                    _add_usage_to_node_map(total_nodes, str(uuid), str(item.get("name") or uuid), int(item.get("up", 0) or 0), int(item.get("down", 0) or 0))
                source_parts.append("node_daily_usage")
                covered_days.append(d.strftime("%Y-%m-%d"))

        d += timedelta(days=1)

    # 如果 group_by="day"，按天聚合返回
    if group_by == "day":
        daily_buckets = []
        for day_str in sorted(set(covered_days)):
            day_nodes = {}
            for uuid, item in total_nodes.items():
                # 这里简化处理：返回的 total_nodes 已经是全周期累计
                # 真正按天分组需要在循环中分别累积，暂时返回全周期数据的日期标记
                day_nodes[uuid] = item
            day_up = sum(int(item.get("up", 0) or 0) for item in day_nodes.values())
            day_down = sum(int(item.get("down", 0) or 0) for item in day_nodes.values())
            daily_buckets.append({
                "day": day_str,
                "up": day_up,
                "down": day_down,
                "total": day_up + day_down,
                "up_human": human_bytes(day_up),
                "down_human": human_bytes(day_down),
                "total_human": human_bytes(day_up + day_down),
            })
        return {
            "from_ts": start,
            "to_ts": end,
            "group_by": group_by,
            "days": daily_buckets,
            "skipped": list(dict.fromkeys(skipped)),
            "reset_warnings": list(dict.fromkeys(reset_warnings)),
            "sample_count": sample_count,
            "segment_count": segment_count,
            "sample_from_ts": sample_from_ts,
            "sample_to_ts": sample_to_ts,
            "sample_days": sorted(sample_days),
            "source": _source_label_from_parts(source_parts),
            "source_parts": list(dict.fromkeys(source_parts)),
        }

    return {
        "from_ts": start,
        "to_ts": end,
        "group_by": group_by,
        "nodes": total_nodes,
        "days": covered_days,
        "skipped": list(dict.fromkeys(skipped)),
        "reset_warnings": list(dict.fromkeys(reset_warnings)),
        "sample_count": sample_count,
        "segment_count": segment_count,
        "sample_from_ts": sample_from_ts,
        "sample_to_ts": sample_to_ts,
        "sample_days": sorted(sample_days),
        "source": _source_label_from_parts(source_parts),
        "source_parts": list(dict.fromkeys(source_parts)),
    }


def snapshot_range_usage(from_ts: int | float, to_ts: int | float) -> dict:
    """兼容旧接口：直接调用 query_usage"""
    return query_usage(from_ts, to_ts, group_by="node")


def upsert_daily_usage(day_str: str, deltas: dict, source: str = "history", source_from: str = "", source_to: str = "", reset_warnings: list[str] | None = None, skipped: list[str] | None = None):
    if not deltas:
        return
    init_traffic_db()
    updated_at = int(time.time())
    reset_json = json.dumps(reset_warnings or [], ensure_ascii=False)
    skipped_json = json.dumps(skipped or [], ensure_ascii=False)
    rows = []
    for uuid, item in deltas.items():
        up = max(0, int(item.get("up", 0) or 0))
        down = max(0, int(item.get("down", 0) or 0))
        rows.append((
            day_str,
            str(uuid),
            str(item.get("name") or uuid),
            up,
            down,
            up + down,
            source,
            source_from,
            source_to,
            reset_json,
            skipped_json,
            updated_at,
        ))
    with traffic_db_session() as conn:
        conn.executemany(
            """
            INSERT INTO node_daily_usage(day, uuid, name, up, down, total, source, source_from, source_to, reset_warnings, skipped, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day, uuid) DO UPDATE SET
              name=excluded.name,
              up=excluded.up,
              down=excluded.down,
              total=excluded.total,
              source=excluded.source,
              source_from=excluded.source_from,
              source_to=excluded.source_to,
              reset_warnings=excluded.reset_warnings,
              skipped=excluded.skipped,
              updated_at=excluded.updated_at
            """,
            rows,
        )


def traffic_db_has_day(day_str: str) -> bool:
    init_traffic_db()
    with traffic_db_session() as conn:
        row = conn.execute("SELECT 1 FROM node_daily_usage WHERE day = ? LIMIT 1", (day_str,)).fetchone()
    return row is not None


def prune_node_daily_usage(retention_days: int | None = None, today_value: date | None = None) -> dict:
    init_traffic_db()
    days = NODE_DAILY_USAGE_RETENTION_DAYS if retention_days is None else int(retention_days)
    if days < 0:
        raise RuntimeError("retention_days must be >= 0")
    if days == 0:
        with traffic_db_session() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM node_daily_usage").fetchone()
        return {
            "enabled": False,
            "retention_days": 0,
            "cutoff_day": "",
            "deleted": 0,
            "remaining": int(row["c"] or 0),
        }
    cutoff_day = (today_value or today_date()) - timedelta(days=days)
    cutoff_text = cutoff_day.strftime("%Y-%m-%d")
    with traffic_db_session() as conn:
        cur = conn.execute("DELETE FROM node_daily_usage WHERE day < ?", (cutoff_text,))
        deleted = int(cur.rowcount or 0)
        row = conn.execute("SELECT COUNT(*) AS c FROM node_daily_usage").fetchone()
    return {
        "enabled": True,
        "retention_days": days,
        "cutoff_day": cutoff_text,
        "deleted": deleted,
        "remaining": int(row["c"] or 0),
    }


def aggregate_daily_usage(from_day: date, to_day: date) -> dict:
    init_traffic_db()
    start = from_day.strftime("%Y-%m-%d")
    end = to_day.strftime("%Y-%m-%d")
    with traffic_db_session() as conn:
        rows = conn.execute(
            """
            SELECT uuid, COALESCE(NULLIF(name, ''), uuid) AS name, SUM(up) AS up, SUM(down) AS down
            FROM node_daily_usage
            WHERE day >= ? AND day <= ?
            GROUP BY uuid
            """,
            (start, end),
        ).fetchall()
    result = {}
    for row in rows:
        result[str(row["uuid"])] = {
            "name": row["name"] or row["uuid"],
            "up": int(row["up"] or 0),
            "down": int(row["down"] or 0),
        }
    return result


def _traffic_node_rows_from_map(nodes_map: dict) -> list[dict]:
    rows = []
    for uuid, item in nodes_map.items():
        up = max(0, int(item.get("up", 0) or 0))
        down = max(0, int(item.get("down", 0) or 0))
        total = up + down
        rows.append({
            "uuid": str(uuid),
            "name": str(item.get("name") or uuid),
            "up": up,
            "down": down,
            "total": total,
            "up_human": human_bytes(up),
            "down_human": human_bytes(down),
            "total_human": human_bytes(total),
        })
    rows.sort(key=lambda item: (item["total"], item["down"], item["up"], item["name"].lower()), reverse=True)
    return rows


def _traffic_total_from_rows(rows: list[dict]) -> dict:
    up = sum(int(item.get("up", 0) or 0) for item in rows)
    down = sum(int(item.get("down", 0) or 0) for item in rows)
    total = up + down
    return {
        "up": up,
        "down": down,
        "total": total,
        "up_human": human_bytes(up),
        "down_human": human_bytes(down),
        "total_human": human_bytes(total),
    }


def _traffic_group_key(day_value: date, group: str) -> tuple[str, str]:
    if group == "weekly":
        start = start_of_week(day_value)
        end = start + timedelta(days=6)
        return start.strftime("%Y-%m-%d"), f"{start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}"
    if group == "monthly":
        return yyyymm(day_value), yyyymm(day_value)
    return day_value.strftime("%Y-%m-%d"), day_value.strftime("%Y-%m-%d")


def _source_label_from_parts(parts: list[str]) -> str:
    unique = list(dict.fromkeys(str(part) for part in parts if str(part or "").strip()))
    if not unique:
        return "none"
    return "+".join(unique)


def _add_usage_to_node_map(nodes: dict, uuid: str, name: str, up: int, down: int):
    uid = str(uuid)
    entry = nodes.setdefault(uid, {"name": name or uid, "up": 0, "down": 0})
    entry["name"] = name or entry.get("name") or uid
    entry["up"] += max(0, int(up or 0))
    entry["down"] += max(0, int(down or 0))


def build_daily_period_usage(from_dt: datetime, to_dt: datetime | None = None) -> dict:
    to_dt = to_dt or now_dt()
    if from_dt > to_dt:
        raise RuntimeError("from_dt must be <= to_dt")

    ensure_dirs()
    ensure_traffic_segments_backfilled()
    migrate_history_to_traffic_db()

    total_nodes: dict[str, dict] = {}
    covered_days = set()
    rollup_days = set()
    segment_days = set()
    missing_days: list[str] = []
    skipped: list[str] = []
    reset_warnings: list[str] = []
    sample_count = 0
    segment_count = 0
    sample_from_ts = 0
    sample_to_ts = 0
    sample_days = set()
    source_parts: list[str] = []

    now_value = now_dt()
    last_day = to_dt.date()
    d = from_dt.date()
    while d <= last_day:
        day_text = d.strftime("%Y-%m-%d")
        day_start = start_of_day(d)
        day_end = start_of_day(d + timedelta(days=1))
        window_start = max(day_start, from_dt)
        window_end = min(day_end, to_dt)
        if window_end <= window_start:
            d += timedelta(days=1)
            continue
        effective_end = min(window_end, now_value)
        if effective_end <= window_start:
            missing_days.append(day_text)
            d += timedelta(days=1)
            continue
        usage = _traffic_segment_usage(int(window_start.timestamp()), int(effective_end.timestamp()))
        if _snapshot_usage_has_nodes(usage):
            for uuid, item in (usage.get("nodes") or {}).items():
                _add_usage_to_node_map(total_nodes, str(uuid), str(item.get("name") or uuid), int(item.get("up", 0) or 0), int(item.get("down", 0) or 0))
            covered_days.add(day_text)
            segment_days.add(day_text)
            source_parts.append("traffic_segments")
            skipped.extend(usage.get("skipped", []))
            reset_warnings.extend(usage.get("reset_warnings", []))
            sample_count += int(usage.get("sample_count", 0) or 0)
            segment_count += int(usage.get("segment_count", 0) or 0)
            from_sample = int(usage.get("sample_from_ts", 0) or 0)
            to_sample = int(usage.get("sample_to_ts", 0) or 0)
            sample_from_ts = min([ts for ts in (sample_from_ts, from_sample) if ts] or [0])
            sample_to_ts = max(sample_to_ts, to_sample)
            sample_days.update(str(item) for item in (usage.get("sample_days", []) or []))
        else:
            day_usage = aggregate_daily_usage(d, d)
            if day_usage:
                for uuid, item in day_usage.items():
                    _add_usage_to_node_map(total_nodes, str(uuid), str(item.get("name") or uuid), int(item.get("up", 0) or 0), int(item.get("down", 0) or 0))
                covered_days.add(day_text)
                rollup_days.add(day_text)
                source_parts.append("traffic_db")
            else:
                missing_days.append(day_text)
        d += timedelta(days=1)

    node_rows = _traffic_node_rows_from_map(total_nodes)
    lag = traffic_sample_lag_status()
    source = _source_label_from_parts(source_parts)
    note = "snapshot_window" if "traffic_segments" in source_parts else ("daily_rollup" if source_parts else "insufficient_snapshots")
    return {
        "from": from_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "to": to_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "nodes": node_rows,
        "top_nodes": node_rows[: max(0, int(TOP_N))],
        "total": _traffic_total_from_rows(node_rows),
        "days": sorted(covered_days),
        "coverage_days": sorted(covered_days),
        "day_count": len(covered_days),
        "rollup_days": len(rollup_days),
        "snapshot_days": len(segment_days),
        "segment_days": len(segment_days),
        "missing_days": sorted(set(missing_days)),
        "skipped": list(dict.fromkeys(str(item) for item in skipped)),
        "reset_warnings": list(dict.fromkeys(str(item) for item in reset_warnings)),
        "sample_count": sample_count,
        "segment_count": segment_count,
        "sample_from_ts": sample_from_ts,
        "sample_to_ts": sample_to_ts,
        "sample_days": sorted(sample_days),
        "source": source,
        "source_parts": list(dict.fromkeys(source_parts)),
        "note": note,
        "latest_sample_ts": lag.get("latest_sample_ts", 0),
        "latest_sample_at": lag.get("latest_sample_at", ""),
        "sample_lag_seconds": lag.get("sample_lag_seconds"),
        "sample_stale": lag.get("sample_stale", False),
    }


def traffic_range_summary(from_day: date, to_day: date, group: str = "daily") -> dict:
    if from_day > to_day:
        raise RuntimeError("from must be <= to")
    group = str(group or "daily").strip().lower()
    if group not in ("daily", "weekly", "monthly"):
        raise RuntimeError("group must be daily, weekly, or monthly")

    start_dt = start_of_day(from_day)
    end_dt = start_of_day(to_day + timedelta(days=1))
    period = build_daily_period_usage(start_dt, end_dt)
    group_nodes: dict[str, dict] = {}
    group_labels: dict[str, str] = {}

    for day_text in period.get("days", []):
        day_value = parse_date_yyyy_mm_dd(day_text)
        window_start = max(start_of_day(day_value), start_dt)
        window_end = min(start_of_day(day_value + timedelta(days=1)), end_dt, now_dt())
        day_usage = _traffic_segment_usage(int(window_start.timestamp()), int(window_end.timestamp())) if window_end > window_start else {"nodes": {}}
        nodes = day_usage.get("nodes", {}) if _snapshot_usage_has_nodes(day_usage) else aggregate_daily_usage(day_value, day_value)
        key, label = _traffic_group_key(day_value, group)
        group_labels[key] = label
        bucket = group_nodes.setdefault(key, {})
        for uuid, item in (nodes or {}).items():
            _add_usage_to_node_map(bucket, str(uuid), str(item.get("name") or uuid), int(item.get("up", 0) or 0), int(item.get("down", 0) or 0))

    groups = []
    for key in sorted(group_nodes.keys()):
        rows_for_group = _traffic_node_rows_from_map(group_nodes[key])
        groups.append({
            "key": key,
            "label": group_labels.get(key, key),
            "nodes": rows_for_group,
            "top_nodes": rows_for_group[: max(0, int(TOP_N))],
            "total": _traffic_total_from_rows(rows_for_group),
        })

    return {
        "from": from_day.strftime("%Y-%m-%d"),
        "to": to_day.strftime("%Y-%m-%d"),
        "group": group,
        "days": period.get("days", []),
        "coverage_days": period.get("coverage_days", []),
        "day_count": period.get("day_count", 0),
        "nodes": period.get("nodes", []),
        "top_nodes": period.get("top_nodes", []),
        "total": period.get("total", _traffic_total_from_rows([])),
        "groups": groups,
        "rollup_days": period.get("rollup_days", 0),
        "snapshot_days": period.get("snapshot_days", 0),
        "segment_days": period.get("segment_days", 0),
        "missing_days": period.get("missing_days", []),
        "skipped": period.get("skipped", []),
        "reset_warnings": period.get("reset_warnings", []),
        "sample_count": period.get("sample_count", 0),
        "segment_count": period.get("segment_count", 0),
        "source": period.get("source", "none"),
        "source_parts": period.get("source_parts", []),
        "sample_lag_seconds": period.get("sample_lag_seconds"),
    }


def migrate_history_to_traffic_db():
    init_traffic_db()
    hot = load_json(HISTORY_PATH, {"days": {}}).get("days", {})
    for day_str, deltas in (hot or {}).items():
        if isinstance(deltas, dict):
            try:
                if traffic_segments_exist_for_day(parse_date_yyyy_mm_dd(day_str)):
                    continue
            except Exception:
                pass
            upsert_daily_usage(day_str, deltas, source="history_json")
    if not os.path.isdir(DATA_DIR):
        return
    for filename in os.listdir(DATA_DIR):
        if not re.fullmatch(r"history-\d{4}-\d{2}\.json\.gz", filename):
            continue
        ym = filename.removeprefix("history-").removesuffix(".json.gz")
        try:
            arc = load_archive_month(ym).get("days", {})
        except Exception:
            continue
        for day_str, deltas in (arc or {}).items():
            if isinstance(deltas, dict):
                try:
                    if traffic_segments_exist_for_day(parse_date_yyyy_mm_dd(day_str)):
                        continue
                except Exception:
                    pass
                upsert_daily_usage(day_str, deltas, source="history_archive")


def default_report_schedules() -> dict:
    return {"version": 1, "schedules": [], "last_runs": {}}


def _parse_hhmm(value: str) -> tuple[int, int]:
    text = str(value or "").strip()
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", text)
    if not match:
        raise RuntimeError("time must be HH:mm")
    return int(match.group(1)), int(match.group(2))


def normalize_report_schedule(item: dict) -> dict:
    scope = str(item.get("scope") or "daily").strip().lower()
    if scope not in ("daily", "weekly", "monthly"):
        scope = "daily"
    mode = str(item.get("mode") or "full").strip().lower()
    if mode not in ("full", "top"):
        mode = "full"
    schedule_id = str(item.get("id") or secrets.token_urlsafe(8)).strip()
    time_text = str(item.get("time") or "09:00").strip()
    _parse_hhmm(time_text)
    weekday = min(6, max(0, int(item.get("weekday", 0) or 0)))
    month_day = min(31, max(1, int(item.get("month_day", 1) or 1)))
    return {
        "id": schedule_id,
        "enabled": bool(item.get("enabled", True)),
        "scope": scope,
        "mode": mode,
        "time": time_text,
        "weekday": weekday,
        "month_day": month_day,
        "chat": str(item.get("chat") or "").strip(),
        "updated_at": int(item.get("updated_at") or int(time.time())),
    }


def validate_report_schedule(item: dict) -> dict:
    scope = str(item.get("scope") or "").strip().lower()
    if scope not in ("daily", "weekly", "monthly"):
        raise RuntimeError("scope must be daily, weekly, or monthly")
    mode = str(item.get("mode") or "full").strip().lower()
    if mode not in ("full", "top"):
        raise RuntimeError("mode must be full or top")
    _parse_hhmm(str(item.get("time") or ""))
    raw_weekday = item.get("weekday", 0)
    weekday = int(raw_weekday if raw_weekday not in (None, "") else 0)
    if weekday < 0 or weekday > 6:
        raise RuntimeError("weekday must be 0-6")
    raw_month_day = item.get("month_day", 1)
    month_day = int(raw_month_day if raw_month_day not in (None, "") else 1)
    if month_day < 1 or month_day > 31:
        raise RuntimeError("month_day must be 1-31")
    payload = dict(item)
    payload["scope"] = scope
    payload["mode"] = mode
    payload["weekday"] = weekday
    payload["month_day"] = month_day
    payload["enabled"] = bool(item.get("enabled", True))
    payload["updated_at"] = int(time.time())
    return normalize_report_schedule(payload)


def load_report_schedules() -> dict:
    with file_lock(REPORT_SCHEDULES_PATH):
        data = load_json(REPORT_SCHEDULES_PATH, default_report_schedules())
        schedules = data.get("schedules", []) if isinstance(data, dict) else []
        last_runs = data.get("last_runs", {}) if isinstance(data, dict) else {}
        if not isinstance(schedules, list):
            schedules = []
        if not isinstance(last_runs, dict):
            last_runs = {}
        return {
            "version": 1,
            "schedules": [normalize_report_schedule(item) for item in schedules if isinstance(item, dict)],
            "last_runs": last_runs,
        }


def save_report_schedules(data: dict):
    ensure_dirs()
    with file_lock(REPORT_SCHEDULES_PATH):
        payload = {
            "version": 1,
            "schedules": [normalize_report_schedule(item) for item in data.get("schedules", []) if isinstance(item, dict)],
            "last_runs": data.get("last_runs", {}) if isinstance(data.get("last_runs", {}), dict) else {},
        }
        save_json_atomic(REPORT_SCHEDULES_PATH, payload)


def schedule_label(item: dict) -> str:
    mode = "Top" if item.get("mode") == "top" else "完整"
    if item.get("scope") == "daily":
        return f"每日 {item.get('time')} 发送{mode}日报"
    if item.get("scope") == "weekly":
        names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return f"每周{names[int(item.get('weekday', 0))]} {item.get('time')} 发送{mode}周报"
    return f"每月 {int(item.get('month_day', 1))} 号 {item.get('time')} 发送{mode}月报"


def schedule_due_key(item: dict, now: datetime) -> str | None:
    hour, minute = _parse_hhmm(item.get("time", ""))
    if now.hour != hour or now.minute != minute:
        return None
    if item.get("scope") == "weekly" and now.weekday() != int(item.get("weekday", 0)):
        return None
    if item.get("scope") == "monthly" and now.day != int(item.get("month_day", 1)):
        return None
    return f"{item.get('id')}:{now.strftime('%Y-%m-%d %H:%M')}"


def schedule_next_run_at(item: dict, now: datetime | None = None, horizon_days: int = 370) -> int | None:
    schedule = normalize_report_schedule(item)
    if not schedule.get("enabled"):
        return None
    now = now or now_dt()
    hour, minute = _parse_hhmm(schedule.get("time", ""))
    today = now.date()
    for offset in range(max(1, int(horizon_days)) + 1):
        day = today + timedelta(days=offset)
        if schedule.get("scope") == "weekly" and day.weekday() != int(schedule.get("weekday", 0)):
            continue
        if schedule.get("scope") == "monthly" and day.day != int(schedule.get("month_day", 1)):
            continue
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
        if candidate > now:
            return int(candidate.timestamp())
    return None


def get_json(url: str):
    try:
        r = HTTP_SESSION.get(url, timeout=TIMEOUT, headers=build_komari_headers())
        r.raise_for_status()
        return r.json()
    except requests.exceptions.Timeout:
        logging.warning("get_json timeout: %s", url)
        raise
    except requests.exceptions.RequestException as e:
        logging.warning("get_json failed: %s - %s", url, e)
        raise


def post_json(url: str, payload: dict, retries: int = 3):
    retry_strategy = Retry(
        total=retries,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    r = session.post(url, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def telegram_send_to_chat(text: str, chat_id: str, parse_mode: str | None = "HTML"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 未设置")
    if not str(chat_id).strip():
        raise RuntimeError("chat_id 未设置")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": str(chat_id).strip(),
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return post_json(url, payload)


def telegram_send(text: str):
    return telegram_send_to_chat(text, TELEGRAM_CHAT_ID, parse_mode="HTML")


def telegram_send_plain(text: str):
    return telegram_send_to_chat(text, TELEGRAM_CHAT_ID, parse_mode=None)


def telegram_alert_chat_id() -> str:
    return TELEGRAM_ALERT_CHAT_ID or TELEGRAM_CHAT_ID


def telegram_send_alert(text: str):
    return telegram_send_to_chat(text, telegram_alert_chat_id(), parse_mode="HTML")


def telegram_configured() -> bool:
    return bool(str(TELEGRAM_BOT_TOKEN or "").strip() and str(TELEGRAM_CHAT_ID or "").strip())


def safe_telegram_send(text: str):
    try:
        if telegram_configured():
            telegram_send(text)
    except Exception:
        pass


def ai_chat(messages: list[dict]) -> str:
    """
    通用 OpenAI 兼容 chat.completions 调用。
    """
    if not ai_enabled():
        return "⚠️ AI 未启用：请先配置 AI_API_BASE / AI_API_KEY / AI_MODEL 环境变量。"

    url = f"{AI_API_BASE}/chat/completions"
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": AI_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 1000,
        "stream": False,
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=90)
        r.raise_for_status()
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            return "⚠️ AI 没有返回内容，请稍后重试。"
        content = (choices[0].get("message") or {}).get("content") or ""
        return content.strip() or "⚠️ AI 返回了空结果，请稍后重试。"
    except requests.exceptions.Timeout:
        logging.warning("ai_chat timeout after 90s")
        return "⚠️ AI 响应超时，请稍后重试。"
    except Exception as e:
        logging.exception("ai_chat error")
        return f"⚠️ 调用 AI 失败：{type(e).__name__}: {redact_sensitive_text(e)}"

def ask_ai_with_data(question: str, data_pack: dict) -> str:
    """
    给 AI：用户问题 + 经过 Python 计算好的数据包。
    所有数值都由 Python 从 Komari / JSON 中算好，AI 只负责解读。
    """
    focused_pack = build_focused_ai_data_pack(question, data_pack)
    error_keys = [
        k for k, v in (data_pack or {}).items()
        if isinstance(v, dict) and v.get("error") == "failed"
    ]
    if len(error_keys) >= 3:
        logging.warning("ask_ai_with_data aborted: too many failed data sources: %s", ",".join(error_keys))
        return "⚠️ 数据获取异常，稍后再试。"

    try:
        data_text = json.dumps(focused_pack, ensure_ascii=False)
    except Exception:
        logging.exception("serialize data_pack error")
        data_text = str(focused_pack)

    system_prompt = (
        "你是一个 Komari 流量机器人助手，负责帮用户解读流量统计数据。\n"
        "你会收到一个 JSON 数据包 data_pack，里面所有数值都已经由程序计算完成。\n"
        "规则：\n"
        "1. 所有具体数值（例如流量大小、排名）必须直接来自 data_pack，不要自己发明新数字。\n"
        "2. 如果 data_pack 中没有足够信息回答某个问题，请明确说明“无法从当前数据中判断”，不要瞎猜。\n"
        "3. 回答使用简洁中文，优先使用 *_human 字段展示人类可读流量单位（如 GiB/TiB），避免输出超长原始整数。\n"
        "4. 涉及近 24 小时/7 天/30 天节点流量对比时，优先使用 last_24h、last_7d、last_30d 的 top_nodes。\n"
        "5. 所有回答都尽量按固定模板组织：优先输出“结论”，必要时补“依据 / 趋势 / 建议 / 风险提示”。\n"
        "6. 输出必须适合 Telegram 阅读：每个标题单独成行，每段 2~4 条短句，尽量让用户一眼扫完。\n"
        "6.1 只允许输出 Telegram HTML 支持标签（如 <b>/<i>/<code>），不要输出 Markdown（如 #、*、```）。\n"
        "7. 列表统一使用简洁项目符号，不要使用冗长的 1) 2) 3) 序号堆砌。\n"
        "8. 若用户问“刚刚这一小时/最近1小时某节点用了多少”，优先使用 last_1h_by_node.nodes。\n"
        "9. 若用户问“今天按小时某节点趋势/峰谷”，优先使用 today_hourly_by_node.nodes[*].hours / peak_hour / valley_hour。\n"
        "10. 若用户问“昨天按小时某节点趋势/峰谷”，优先使用 yesterday_hourly_by_node.nodes[*].hours / peak_hour / valley_hour。\n"
        "11. 若用户问全局小时级峰谷，优先使用 last_24h_hourly.hours / peak_hour / valley_hour。\n"
        "12. 不要向用户暴露内部字段名或实现细节，例如 data_pack、last_1h_by_node、today_hourly_by_node、up_human、down_human、total_human。\n"
        "13. 要把内部统计字段翻译成自然语言，直接说“最近1小时”“上行”“下行”“合计”等用户能看懂的话。\n"
        "14. 不需要原样打印整个 JSON，只引用对结论有用的关键信息。\n"
        "15. 不要写成开发日志或调试输出，不要出现‘字段/结构/键名/pack’等工程化表述。"
        "16. 字段说明：last_24h/last_7d/last_30d 分别表示最近 24h/168h/720h 的节点汇总；其中 cpu/ram/disk 为使用率百分比统计（avg/max/min）。"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"这是当前的 data_pack（JSON 格式）：\n"
                f"{data_text}\n\n"
                f"用户问题：{question}\n\n"
                "请严格根据 data_pack 里的数据进行分析和回答。"
            ),
        },
    ]
    return ai_chat(messages)


def question_requires_fresh_ai_pack(question: str) -> bool:
    q = (question or "").lower().strip()
    hot_keywords = (
        "刚刚", "最近1小时", "最近 1 小时", "一小时", "1小时", "1 h", "1h",
        "今天按小时", "今日按小时", "小时趋势", "小时级",
    )
    return any(k in q for k in hot_keywords)


def build_focused_ai_data_pack(question: str, data_pack: dict) -> dict:
    """
    按问题类型缩小喂给 AI 的数据范围，减少模型被无关字段干扰。
    """
    q = (question or "").lower().strip()
    focused = {
        "now": data_pack.get("now"),
        "stat_tz": data_pack.get("stat_tz"),
    }

    if any(k in q for k in ("刚刚", "最近1小时", "最近 1 小时", "一小时", "1小时", "1 h", "1h")):
        focused["last_1h_by_node"] = data_pack.get("last_1h_by_node")
        return focused

    if ("今天" in q or "今日" in q) and ("小时" in q or "峰谷" in q or "走势" in q):
        focused["today_hourly_by_node"] = data_pack.get("today_hourly_by_node")
        focused["today"] = data_pack.get("today")
        return focused

    if ("昨天" in q or "昨日" in q) and ("小时" in q or "峰谷" in q or "走势" in q):
        focused["yesterday_hourly_by_node"] = data_pack.get("yesterday_hourly_by_node")
        return focused

    return data_pack


def normalize_ai_answer_for_telegram(text: str) -> str:
    """
    将常见 Markdown 回答转换成更适合 Telegram(HTML parse_mode) 的纯文本样式。
    避免出现大量 * / # 等原样符号影响可读性。
    """
    out = (text or "").replace("\r\n", "\n").strip()
    if not out:
        return "⚠️ AI 返回为空，请稍后重试。"

    # 保持最小清洗，主体格式约束交给 system prompt，减少脆弱正则链。
    out = out.replace("```html", "").replace("```", "")
    out = re.sub(r"\n{3,}", "\n\n", out)

    # Telegram HTML 安全转义：先全量转义，再还原白名单标签
    out = out.replace("&", "&amp;")
    out = out.replace("<", "&lt;")
    out = out.replace(">", "&gt;")

    allow_map = {
        "&lt;b&gt;": "<b>",
        "&lt;/b&gt;": "</b>",
        "&lt;i&gt;": "<i>",
        "&lt;/i&gt;": "</i>",
        "&lt;code&gt;": "<code>",
        "&lt;/code&gt;": "</code>",
        "&lt;pre&gt;": "<pre>",
        "&lt;/pre&gt;": "</pre>",
    }
    for escaped, raw in allow_map.items():
        out = out.replace(escaped, raw)
    return out.strip()

def should_alert(throttle_key: str, min_interval_seconds: int = 300) -> bool:
    ensure_dirs()
    state_path = os.path.join(DATA_DIR, f"alert_{throttle_key}.json")
    now_ts = int(time.time())
    state = load_json(state_path, {"last": 0})
    last = int(state.get("last", 0))
    if now_ts - last < min_interval_seconds:
        return False
    save_json_atomic(state_path, {"last": now_ts})
    return True


def alert_exception(where: str, cmd: str, exc: Exception):
    host = telegram_html_escape(redact_sensitive_text(socket.gethostname()))
    ts = now_dt().strftime("%Y-%m-%d %H:%M:%S %Z")
    where_text = telegram_html_escape(redact_sensitive_text(where))
    cmd_text = telegram_html_escape(redact_sensitive_text(cmd))
    err = telegram_html_escape(redact_sensitive_text(f"{type(exc).__name__}: {exc}"))
    tb = redact_sensitive_text(traceback.format_exc())
    tb_tail = telegram_html_escape(tb[-1500:])
    msg = (
        f"❌ <b>Komari 流量任务失败</b>\n"
        f"🕒 {ts}\n"
        f"🖥 {host}\n"
        f"📍 {where_text}\n"
        f"🧩 cmd: <code>{cmd_text}</code>\n"
        f"🧨 error: <code>{err}</code>\n\n"
        f"<b>traceback (tail)</b>\n<pre>{tb_tail}</pre>"
    )
    safe_telegram_send(msg)


# -------------------- Komari 数据获取（稳态：单节点超时/异常跳过） --------------------

@dataclass
class NodeTotal:
    uuid: str
    name: str
    up: int
    down: int


def fetch_nodes_and_totals():
    """
    返回：
      - out: list[NodeTotal]
      - skipped: list[str]  # 被跳过的节点原因（timeout/empty/bad_resp/HTTPError等）
    """
    if not KOMARI_BASE_URL:
        raise RuntimeError("KOMARI_BASE_URL 未设置（例如 https://komari.example）")

    nodes_resp = get_json(f"{KOMARI_BASE_URL}/api/nodes")
    if not (isinstance(nodes_resp, dict) and nodes_resp.get("status") == "success"):
        raise RuntimeError(f"/api/nodes 返回异常：{nodes_resp}")

    nodes = nodes_resp.get("data", [])
    out: list[NodeTotal] = []
    skipped: list[str] = []

    def fetch_one(node: dict):
        uuid = node.get("uuid")
        name = node.get("name") or uuid
        if not uuid:
            return None, None
        try:
            recent_resp = get_json(f"{KOMARI_BASE_URL}/api/recent/{uuid}")
        except requests.exceptions.ReadTimeout:
            return None, f"{name}(timeout)"
        except requests.exceptions.RequestException as e:
            return None, f"{name}({type(e).__name__})"
        except Exception as e:
            return None, f"{name}({type(e).__name__})"

        if not (isinstance(recent_resp, dict) and recent_resp.get("status") == "success"):
            return None, f"{name}(bad_resp)"

        points = recent_resp.get("data", [])
        if not points:
            return None, f"{name}(empty)"

        last = points[-1]
        net = last.get("network", {}) if isinstance(last, dict) else {}
        up = int(net.get("totalUp", 0))
        down = int(net.get("totalDown", 0))
        return NodeTotal(uuid=uuid, name=name, up=up, down=down), None

    max_workers = max(1, min(len(nodes), KOMARI_FETCH_WORKERS))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(fetch_one, n): n for n in nodes}
        for future in concurrent.futures.as_completed(future_map):
            result, skip = future.result()
            if skip:
                skipped.append(skip)
                continue
            if result:
                out.append(result)

    return out, skipped


def fetch_node_records(uuid: str, hours: int) -> list[dict]:
    if not uuid:
        raise ValueError("uuid is required")
    if hours <= 0:
        raise ValueError("hours must be > 0")
    url = f"{KOMARI_BASE_URL}/api/records/load"
    r = HTTP_SESSION.get(
        url,
        params={"uuid": uuid, "hours": int(hours)},
        timeout=TIMEOUT,
        headers=build_komari_headers(),
    )
    r.raise_for_status()
    payload = r.json()
    if not (isinstance(payload, dict) and payload.get("status") == "success"):
        raise RuntimeError(f"/api/records/load bad response: {payload}")
    data = payload.get("data", {})
    if not isinstance(data, dict):
        raise RuntimeError(f"/api/records/load data is invalid: {payload}")
    records = data.get("records", [])
    if not isinstance(records, list):
        raise RuntimeError(f"/api/records/load records is invalid: {payload}")
    return records


def _to_float_safe(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _metric_stats(values: list[float]) -> dict:
    if not values:
        return {"avg": None, "max": None, "min": None}
    return {
        "avg": round(sum(values) / len(values), 2),
        "max": round(max(values), 2),
        "min": round(min(values), 2),
    }


def normalize_percent_metric(value, total=None) -> float | None:
    if isinstance(value, dict):
        used = value.get("used")
        capacity = value.get("total", value.get("capacity", total))
        if used is not None and capacity:
            try:
                capacity_f = float(capacity)
                if capacity_f > 0:
                    pct = float(used) / capacity_f * 100
                    return round(pct, 2) if 0 <= pct <= 100 else None
            except Exception:
                return None
        for key in ("percent", "percentage", "usage", "used_percent", "usedPercent", "usage_percent", "usagePercent", "value"):
            if key in value:
                return normalize_percent_metric(value.get(key), total=total)
        return None

    try:
        number = float(value)
    except Exception:
        return None
    if 0 <= number <= 100:
        return round(number, 2)
    if total:
        try:
            total_f = float(total)
            if total_f > 0:
                pct = number / total_f * 100
                return round(pct, 2) if 0 <= pct <= 100 else None
        except Exception:
            return None
    return None


def _metric_sources(record: dict) -> list[dict]:
    if not isinstance(record, dict):
        return []
    sources = [record]
    for key in ("status", "state", "metrics", "latest", "system"):
        value = record.get(key)
        if isinstance(value, dict):
            sources.append(value)
    return sources


def _first_metric_value(record: dict, keys: tuple[str, ...]):
    for source in _metric_sources(record):
        for key in keys:
            if key in source and source.get(key) not in (None, ""):
                return source.get(key)
    return None


def _record_time_label(record: dict) -> str | None:
    if not isinstance(record, dict):
        return None
    for key in ("time", "timestamp", "created_at", "createdAt", "ts"):
        v = record.get(key)
        if v is not None:
            return str(v)
    return None


def compute_traffic_from_records(records: list[dict]) -> dict:
    if records:
        logging.debug("records fields: %s", list(records[0].keys()))
    if not records:
        up_delta = down_delta = 0
        first = last = None
    else:
        seen = set()
        deduped = []
        for r in records:
            t = r.get("time", "")
            if t not in seen:
                seen.add(t)
                deduped.append(r)
        records_sorted = sorted(deduped, key=lambda r: r.get("time", ""))
        first, last = records_sorted[0], records_sorted[-1]
        up_delta = max(0, int(last.get("net_total_up", 0)) - int(first.get("net_total_up", 0)))
        down_delta = max(0, int(last.get("net_total_down", 0)) - int(first.get("net_total_down", 0)))

    total = up_delta + down_delta
    cpu_values = []
    ram_values = []
    disk_values = []
    for rec in records:
        cpu = normalize_percent_metric(
            _first_metric_value(rec, ("cpu", "CPU", "cpu_percent", "cpu_usage", "cpu_usage_percent")),
            _first_metric_value(rec, ("cpu_total", "cpuTotal")),
        )
        ram = normalize_percent_metric(
            _first_metric_value(rec, ("ram", "RAM", "mem", "memory", "ram_percent", "mem_percent", "memory_percent")),
            _first_metric_value(rec, ("ram_total", "ramTotal", "mem_total", "memTotal", "memory_total", "memoryTotal")),
        )
        disk = normalize_percent_metric(
            _first_metric_value(rec, ("disk", "Disk", "hdd", "storage", "disk_percent", "hdd_percent", "storage_percent")),
            _first_metric_value(rec, ("disk_total", "diskTotal", "hdd_total", "hddTotal", "storage_total", "storageTotal")),
        )
        if cpu is not None:
            cpu_values.append(cpu)
        if ram is not None:
            ram_values.append(ram)
        if disk is not None:
            disk_values.append(disk)

    return {
        "up": up_delta,
        "down": down_delta,
        "total": total,
        "up_human": human_bytes(up_delta),
        "down_human": human_bytes(down_delta),
        "total_human": human_bytes(total),
        "cpu": _metric_stats(cpu_values),
        "ram": _metric_stats(ram_values),
        "disk": _metric_stats(disk_values),
        "record_count": len(records),
        "from": _record_time_label(first) if first else None,
        "to": _record_time_label(last) if last else None,
    }


def build_records_summary(hours: int) -> dict:
    if hours <= 0:
        raise ValueError("hours must be > 0")

    now_ts = int(time.time())
    from_ts = now_ts - int(hours) * 3600
    usage = snapshot_range_usage(from_ts, now_ts)
    out_nodes = _traffic_node_rows_from_map(usage.get("nodes", {}))

    from_text = datetime.fromtimestamp(from_ts, TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    to_text = datetime.fromtimestamp(now_ts, TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    empty_stats = _metric_stats([])
    for item in out_nodes:
        item["cpu"] = dict(empty_stats)
        item["ram"] = dict(empty_stats)
        item["disk"] = dict(empty_stats)
        item["record_count"] = int(usage.get("sample_count", 0) or 0)
        item["from"] = from_text
        item["to"] = to_text

    sample_count = int(usage.get("sample_count", 0) or 0)
    result = {
        "hours": int(hours),
        "from": from_text,
        "to": to_text,
        "from_ts": from_ts,
        "to_ts": now_ts,
        "sample_from_ts": int(usage.get("sample_from_ts", 0) or 0),
        "sample_to_ts": int(usage.get("sample_to_ts", 0) or 0),
        "sample_days": usage.get("sample_days", []),
        "nodes": out_nodes,
        "top_nodes": out_nodes[: max(0, int(TOP_N))],
        "total": _traffic_total_from_rows(out_nodes),
        "skipped": list(dict.fromkeys(str(item) for item in usage.get("skipped", []))),
        "reset_warnings": list(dict.fromkeys(str(item) for item in usage.get("reset_warnings", []))),
        "warnings": list(dict.fromkeys(str(item) for item in usage.get("reset_warnings", []))),
        "sample_count": sample_count,
        "segment_count": int(usage.get("segment_count", 0) or 0),
        "source_parts": usage.get("source_parts", []),
        **traffic_sample_lag_status(now_ts),
        "source": usage.get("source", "traffic_snapshots"),
        "note": "snapshot_window" if sample_count >= 2 else "insufficient_snapshots",
    }
    if sample_count < 2:
        result.update({
            "error": "insufficient_snapshots",
            "message": f"还没有足够的连续快照来计算最近 {hours} 小时的数据。",
        })
    return result


def build_nodes_map_from_current(current: list[NodeTotal]) -> dict:
    return {n.uuid: {"name": n.name, "up": n.up, "down": n.down} for n in current}


def compute_strict_sample_delta_from_maps(current_nodes_map: dict, previous_nodes_map: dict) -> tuple[dict, list[str]]:
    """
    用于 samples.json 邻近样本之间的严格差分。

    规则：
    - 仅对“当前样本和前一样本都存在”的节点计算差分；
    - 若计数器回退/重置（负差），该段差分按 0 处理，不回退到当前累计值；
    - 这样可避免把累计计数器绝对值误判成某一小时/某一采样区间的流量。
    returns: (deltas, warnings)
    """
    deltas = {}
    warnings = []

    for uuid, cur in current_nodes_map.items():
        prev = previous_nodes_map.get(uuid)
        name = cur.get("name", uuid)

        if not prev:
            warnings.append(f"{name}(missing_prev)")
            continue

        cur_up = int(cur.get("up", 0))
        cur_down = int(cur.get("down", 0))
        prev_up = int(prev.get("up", 0))
        prev_down = int(prev.get("down", 0))

        up_delta = cur_up - prev_up
        down_delta = cur_down - prev_down

        if up_delta < 0 or down_delta < 0:
            warnings.append(f"{name}(counter_reset)")
            up_delta = max(up_delta, 0)
            down_delta = max(down_delta, 0)

        deltas[uuid] = {"name": name, "up": up_delta, "down": down_delta}

    return deltas, warnings


# -------------------- Top 榜展示 --------------------

def top_lines(deltas: dict, n: int) -> list[str]:
    items = []
    for v in deltas.values():
        name = v.get("name", "")
        up = int(v.get("up", 0))
        down = int(v.get("down", 0))
        total = up + down
        items.append((total, down, up, name))

    items.sort(reverse=True, key=lambda x: (x[0], x[1], x[2], x[3].lower()))
    top = items[: max(0, int(n))]

    if not top:
        return ["（暂无数据）"]

    rows = []
    for i, (total, down, up, name) in enumerate(top, start=1):
        rows.append(
            f"{i}️⃣ <b>{telegram_html_escape(name)}</b>：{human_bytes(total)}"
            f"（⬇️ {human_bytes(down)} / ⬆️ {human_bytes(up)}）"
        )
    return rows


def build_snapshot_period_struct(from_dt: datetime, to_dt: datetime | None = None, label: str | None = None) -> dict:
    to_dt = to_dt or now_dt()
    usage = snapshot_range_usage(int(from_dt.timestamp()), int(to_dt.timestamp()))
    rows = _traffic_node_rows_from_map(usage.get("nodes", {}))
    return {
        "from": from_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "to": to_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "label": label or f"{from_dt.strftime('%Y-%m-%d %H:%M')} → {to_dt.strftime('%Y-%m-%d %H:%M')}",
        "nodes": rows,
        "top_nodes": rows[: max(0, int(TOP_N))],
        "total": _traffic_total_from_rows(rows),
        "skipped": usage.get("skipped", []),
        "reset_warnings": usage.get("reset_warnings", []),
        "sample_count": usage.get("sample_count", 0),
        "sample_from_ts": int(usage.get("sample_from_ts", 0) or 0),
        "sample_to_ts": int(usage.get("sample_to_ts", 0) or 0),
        "sample_days": usage.get("sample_days", []),
        "source": usage.get("source", "traffic_snapshots"),
        "note": "snapshot_window" if int(usage.get("sample_count", 0) or 0) >= 2 else "insufficient_snapshots",
    }


def _snapshot_usage_to_struct(usage: dict, hours: int | None = None, limit: int | None = None) -> dict:
    from_ts = int(usage.get("from_ts", 0) or 0)
    to_ts = int(usage.get("to_ts", 0) or 0)
    nodes = _traffic_node_rows_from_map(usage.get("nodes", {}))
    if limit is not None:
        nodes = nodes[: max(0, int(limit))]
    result = {
        "from": datetime.fromtimestamp(from_ts, TZ).strftime("%Y-%m-%d %H:%M:%S %Z") if from_ts else "",
        "to": datetime.fromtimestamp(to_ts, TZ).strftime("%Y-%m-%d %H:%M:%S %Z") if to_ts else "",
        "nodes": nodes,
        "skipped": usage.get("skipped", []),
        "reset_warnings": usage.get("reset_warnings", []),
        "sample_count": int(usage.get("sample_count", 0) or 0),
        "segment_count": int(usage.get("segment_count", 0) or 0),
        "sample_from_ts": int(usage.get("sample_from_ts", 0) or 0),
        "sample_to_ts": int(usage.get("sample_to_ts", 0) or 0),
        "sample_days": usage.get("sample_days", []),
        "source": usage.get("source", "traffic_snapshots"),
    }
    if hours is not None:
        result["hours"] = int(hours)
    return result


def build_snapshot_hourly_total_summary(from_ts: int, to_ts: int) -> dict:
    samples, segments = snapshot_delta_segments(from_ts, to_ts)
    if len(samples) < 2:
        return {
            "from": datetime.fromtimestamp(int(from_ts), TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "to": datetime.fromtimestamp(int(to_ts), TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "error": "insufficient_samples",
            "message": "采样点不足，无法计算小时级分布。",
            "sample_count": len(samples),
            "source": "traffic_snapshots",
        }

    bucket_map: dict[str, dict] = {}
    warnings: list[str] = []
    skipped: list[str] = []
    for segment in segments:
        warnings.extend(segment.get("reset_warnings", []))
        skipped.extend(segment.get("skipped", []))
        cursor = int(segment.get("from_ts", 0) or 0)
        segment_end = int(segment.get("to_ts", 0) or 0)
        while cursor < segment_end:
            hour_label, bucket_end = _hour_bucket_label_and_end(cursor)
            part_end = min(segment_end, bucket_end)
            part_seconds = part_end - cursor
            scaled = _scale_delta_map(segment.get("nodes", {}), part_seconds, int(segment.get("segment_seconds", 0) or 0))
            up = sum(int(v.get("up", 0) or 0) for v in scaled.values())
            down = sum(int(v.get("down", 0) or 0) for v in scaled.values())
            total = up + down
            bucket = bucket_map.setdefault(hour_label, {"hour": hour_label, "up": 0, "down": 0, "total": 0})
            bucket["up"] += up
            bucket["down"] += down
            bucket["total"] += total
            cursor = part_end

    hours = list(bucket_map.values())
    hours.sort(key=lambda x: x["hour"])
    for item in hours:
        item["up_human"] = human_bytes(item["up"])
        item["down_human"] = human_bytes(item["down"])
        item["total_human"] = human_bytes(item["total"])

    peak_hour = max(hours, key=lambda x: x["total"]) if hours else None
    valley_hour = min(hours, key=lambda x: x["total"]) if hours else None
    return {
        "from": datetime.fromtimestamp(int(from_ts), TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "to": datetime.fromtimestamp(int(to_ts), TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "hours": hours,
        "peak_hour": peak_hour,
        "valley_hour": valley_hour,
        "reset_warnings": list(dict.fromkeys(warnings)),
        "skipped": list(dict.fromkeys(skipped)),
        "sample_count": len(samples),
        "segment_count": len(segments),
        "source": "traffic_snapshots",
    }


def build_snapshot_hourly_by_node_summary(from_ts: int, to_ts: int, label_date: date | None = None) -> dict:
    samples, segments = snapshot_delta_segments(from_ts, to_ts)
    base = {
        "date": label_date.strftime("%Y-%m-%d") if label_date else "",
        "from": datetime.fromtimestamp(int(from_ts), TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "to": datetime.fromtimestamp(int(to_ts), TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "sample_count": len(samples),
        "source": "traffic_snapshots",
    }
    if len(samples) < 2:
        base.update({
            "error": "insufficient_samples",
            "message": "采样点不足，无法计算节点小时级分布。",
        })
        return base

    node_hour_map: dict[str, dict] = {}
    warnings: list[str] = []
    skipped: list[str] = []
    for segment in segments:
        warnings.extend(segment.get("reset_warnings", []))
        skipped.extend(segment.get("skipped", []))
        cursor = int(segment.get("from_ts", 0) or 0)
        segment_end = int(segment.get("to_ts", 0) or 0)
        while cursor < segment_end:
            hour_label, bucket_end = _hour_bucket_label_and_end(cursor)
            part_end = min(segment_end, bucket_end)
            part_seconds = part_end - cursor
            scaled = _scale_delta_map(segment.get("nodes", {}), part_seconds, int(segment.get("segment_seconds", 0) or 0))

            for uuid, item in scaled.items():
                name = item.get("name") or uuid
                up = int(item.get("up", 0) or 0)
                down = int(item.get("down", 0) or 0)
                total = up + down
                node = node_hour_map.setdefault(
                    str(uuid),
                    {"uuid": str(uuid), "name": name, "up": 0, "down": 0, "total": 0, "hours_map": {}},
                )
                node["name"] = name or node["name"]
                node["up"] += up
                node["down"] += down
                node["total"] += total
                hour = node["hours_map"].setdefault(hour_label, {"hour": hour_label, "up": 0, "down": 0, "total": 0})
                hour["up"] += up
                hour["down"] += down
                hour["total"] += total

            cursor = part_end

    nodes = []
    for node in node_hour_map.values():
        hours = list(node.pop("hours_map").values())
        hours.sort(key=lambda x: x["hour"])
        for item in hours:
            item["up_human"] = human_bytes(item["up"])
            item["down_human"] = human_bytes(item["down"])
            item["total_human"] = human_bytes(item["total"])
        peak_hour = max(hours, key=lambda x: x["total"]) if hours else None
        valley_hour = min(hours, key=lambda x: x["total"]) if hours else None
        nodes.append({
            "uuid": node["uuid"],
            "name": node["name"],
            "up": node["up"],
            "down": node["down"],
            "total": node["total"],
            "up_human": human_bytes(node["up"]),
            "down_human": human_bytes(node["down"]),
            "total_human": human_bytes(node["total"]),
            "hours": hours,
            "peak_hour": peak_hour,
            "valley_hour": valley_hour,
        })

    nodes.sort(key=lambda x: (x["total"], x["down"], x["up"], x["name"].lower()), reverse=True)
    base.update({
        "nodes": nodes,
        "top_nodes": nodes[: max(0, int(TOP_N))],
        "reset_warnings": list(dict.fromkeys(warnings)),
        "skipped": list(dict.fromkeys(skipped)),
        "segment_count": len(segments),
    })
    return base


def _merge_usage_maps(target: dict, source: dict):
    for uuid, item in (source or {}).items():
        if not isinstance(item, dict):
            continue
        entry = target.setdefault(str(uuid), {"name": item.get("name") or str(uuid), "up": 0, "down": 0})
        entry["name"] = item.get("name") or entry.get("name") or str(uuid)
        entry["up"] += max(0, int(item.get("up", 0) or 0))
        entry["down"] += max(0, int(item.get("down", 0) or 0))


def _snapshot_usage_has_nodes(usage: dict) -> bool:
    return bool(usage.get("nodes") or {})


def build_live_period_struct(from_dt: datetime, to_dt: datetime | None = None, label: str | None = None) -> dict:
    """
    Build a current-period view from the unified query layer (query_usage).

    All period queries now go through query_usage for consistency.
    """
    to_dt = to_dt or now_dt()
    if from_dt > to_dt:
        raise RuntimeError("from_dt must be <= to_dt")

    from_ts = int(from_dt.timestamp())
    to_ts = int(to_dt.timestamp())
    usage = query_usage(from_ts, to_ts, group_by="node")

    nodes_map = usage.get("nodes", {})
    rows = _traffic_node_rows_from_map(nodes_map)
    total = _traffic_total_from_rows(rows)

    # 计算覆盖和缺失天数（兼容旧测试）
    from_day = from_dt.date()
    to_day = to_dt.date()
    expected_days = []
    d = from_day
    while d <= to_day:
        expected_days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    covered_days = usage.get("days", [])
    missing_days = [day for day in expected_days if day not in covered_days]

    # 统计 rollup/segment 天数（兼容旧字段）
    source_parts = usage.get("source_parts", [])
    rollup_days = source_parts.count("node_daily_usage")
    segment_days = source_parts.count("traffic_segments")
    # snapshot_days 是实际覆盖的天数（如果有 segments）
    if usage.get("source") in ("traffic_segments", "node_daily_usage+traffic_segments", "traffic_db+traffic_segments", "mixed"):
        snapshot_days = len([d for d in covered_days if d in usage.get("sample_days", [])])
        if snapshot_days == 0 and segment_days > 0:
            # 如果没有 sample_days 字段，回退到 covered_days 中非 rollup 的天数
            snapshot_days = len(covered_days) - rollup_days
    else:
        snapshot_days = 0

    # 兼容旧 source 标签
    source = usage.get("source", "query_usage")
    if source == "mixed":
        source = "traffic_db+traffic_segments"
    elif source == "node_daily_usage+traffic_segments":
        source = "traffic_db+traffic_segments"
    elif source == "traffic_segments":
        source = "traffic_segments"

    # note 字段（兼容旧测试）
    if segment_days > 0 and rollup_days == 0:
        note = "snapshot_window"
    else:
        note = "query_usage"

    period = {
        "label": label or f"{from_dt.strftime('%Y-%m-%d %H:%M')} → {to_dt.strftime('%Y-%m-%d %H:%M')}",
        "nodes": rows,
        "top_nodes": rows[:TOP_N] if rows else [],
        "total": total,
        "skipped": usage.get("skipped", []),
        "reset_warnings": usage.get("reset_warnings", []),
        "sample_count": usage.get("sample_count", 0),
        "segment_count": usage.get("segment_count", 0),
        "source": source,
        "source_parts": source_parts,
        "sample_days": usage.get("sample_days", []),
        "coverage_days": covered_days,
        "missing_days": missing_days,
        "rollup_days": rollup_days,
        "segment_days": segment_days,
        "snapshot_days": snapshot_days,
        "note": note,
    }
    return period


def build_today_delta_struct() -> dict | None:
    """
    返回今天各节点增量统计的结构化数据。
    """
    td = today_date()
    now = now_dt()
    start = start_of_day(td)
    period = build_live_period_struct(start, now, td.strftime("%Y-%m-%d"))
    result = {
        "date": td.strftime("%Y-%m-%d"),
        "now": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "nodes": period.get("nodes", []),
        "skipped": period.get("skipped", []),
        "reset_warnings": period.get("reset_warnings", []),
        "note": period.get("note", "snapshot_window"),
        "source": period.get("source", "traffic_snapshots"),
        "source_parts": period.get("source_parts", []),
        "sample_count": period.get("sample_count", 0),
        "segment_count": period.get("segment_count", 0),
        "coverage_days": period.get("coverage_days", []),
        "missing_days": period.get("missing_days", []),
        "sample_lag_seconds": period.get("sample_lag_seconds"),
        "total": period.get("total", _traffic_total_from_rows([])),
    }
    return result

def get_top_last_hours_struct(hours: int, n: int) -> dict | None:
    """
    基于 traffic_snapshots 连续快照计算最近 N 小时的 Top 榜（结构化）。
    """
    if hours <= 0:
        return None

    ensure_dirs()
    take_sample_if_due(force=True)

    now_ts = int(time.time())
    usage = snapshot_range_usage(now_ts - hours * 3600, now_ts)
    if int(usage.get("sample_count", 0) or 0) < 2:
        result = _snapshot_usage_to_struct(usage, hours=hours, limit=n)
        result.update({
            "error": "insufficient_snapshots",
            "message": f"还没有足够的连续快照来计算最近 {hours} 小时的数据。",
        })
        return result
    return _snapshot_usage_to_struct(usage, hours=hours, limit=n)


def get_last_hours_nodes_struct(hours: int) -> dict | None:
    """
    返回最近 N 小时所有节点的结构化差分（不截断 Top N）。
    """
    if hours <= 0:
        return None

    ensure_dirs()
    take_sample_if_due(force=True)

    now_ts = int(time.time())
    usage = snapshot_range_usage(now_ts - hours * 3600, now_ts)
    result = _snapshot_usage_to_struct(usage, hours=hours)
    result["warnings"] = result.get("reset_warnings", [])
    if int(usage.get("sample_count", 0) or 0) < 2:
        result.update({
            "error": "insufficient_snapshots",
            "message": f"还没有足够的连续快照来计算最近 {hours} 小时的数据。",
        })
    return result



def build_last_24h_hourly_summary() -> dict:
    """
    基于 traffic_snapshots 构造最近 24 小时按小时分桶的总流量分布，
    用于回答“小时级峰谷”问题。
    """
    ensure_dirs()
    take_sample_if_due(force=False)

    now_ts = int(time.time())
    from_ts = now_ts - 24 * 3600
    result = build_snapshot_hourly_total_summary(from_ts, now_ts)
    if result.get("error") == "insufficient_samples":
        result["message"] = "采样点不足，无法计算最近 24 小时小时级分布。"
    return result


def build_yesterday_hourly_by_node_summary() -> dict:
    """
    基于 traffic_snapshots 统计“昨天 00:00~24:00”各节点小时级走势。
    用于回答“某节点昨天哪个小时最忙、是否有峰谷”。
    """
    ensure_dirs()
    take_sample_if_due(force=True)

    td = today_date()
    yday = td - timedelta(days=1)
    from_ts = int(start_of_day(yday).timestamp())
    to_ts = int(start_of_day(td).timestamp())
    result = build_snapshot_hourly_by_node_summary(from_ts, to_ts, label_date=yday)
    if result.get("error") == "insufficient_samples":
        result["message"] = "采样点不足，无法计算昨天节点小时级分布。"
    return result


def build_today_hourly_by_node_summary() -> dict:
    """
    基于 traffic_snapshots 统计“今天 00:00~当前”各节点小时级走势。
    """
    ensure_dirs()
    take_sample_if_due(force=True)

    td = today_date()
    from_ts = int(start_of_day(td).timestamp())
    now_ts = int(time.time())
    result = build_snapshot_hourly_by_node_summary(from_ts, now_ts, label_date=td)
    if result.get("error") == "insufficient_samples":
        result["message"] = "采样点不足，无法计算今天节点小时级分布。"
    return result


def load_ai_pack_cache() -> dict:
    return load_json(AI_PACK_CACHE_PATH, {"created_at": 0, "pack": {}})


def save_ai_pack_cache(pack: dict):
    save_json_atomic(AI_PACK_CACHE_PATH, {"created_at": int(time.time()), "pack": pack})


def get_ai_data_pack_cached() -> dict:
    if AI_PACK_CACHE_TTL_SECONDS <= 0:
        return build_ai_data_pack()

    cache = load_ai_pack_cache()
    created_at = int(cache.get("created_at", 0))
    now_ts = int(time.time())
    if created_at > 0 and now_ts - created_at <= AI_PACK_CACHE_TTL_SECONDS:
        pack = cache.get("pack") or {}
        if isinstance(pack, dict) and pack:
            return pack

    pack = build_ai_data_pack()
    save_ai_pack_cache(pack)
    return pack


def build_last_7_days_summary() -> dict:
    """
    使用 history.json + 月归档，构造最近 7 天总量按日汇总，
    并提供按节点累计排行，便于回答“7天哪台机器流量最高”。
    """
    ensure_dirs()
    td = today_date()
    days = []
    node_totals: dict[str, dict] = {}

    for i in range(7, 0, -1):
        d = td - timedelta(days=i)
        summed = history_sum(d, d)
        total_up = 0
        total_down = 0
        day_nodes = []

        for uuid, v in summed.items():
            name = v.get("name", uuid)
            up = int(v.get("up", 0))
            down = int(v.get("down", 0))
            total = up + down
            total_up += up
            total_down += down
            day_nodes.append({
                "name": name,
                "up": up,
                "down": down,
                "total": total,
                "up_human": human_bytes(up),
                "down_human": human_bytes(down),
                "total_human": human_bytes(total),
            })

            if name not in node_totals:
                node_totals[name] = {"name": name, "up": 0, "down": 0, "total": 0}
            node_totals[name]["up"] += up
            node_totals[name]["down"] += down
            node_totals[name]["total"] += total

        day_nodes.sort(key=lambda x: (x["total"], x["down"], x["up"], x["name"].lower()), reverse=True)
        total = total_up + total_down
        days.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "total_up": total_up,
                "total_down": total_down,
                "total": total,
                "total_up_human": human_bytes(total_up),
                "total_down_human": human_bytes(total_down),
                "total_human": human_bytes(total),
                "nodes": day_nodes,
            }
        )

    node_totals_list = []
    for v in node_totals.values():
        node_totals_list.append({
            "name": v["name"],
            "up": v["up"],
            "down": v["down"],
            "total": v["total"],
            "up_human": human_bytes(v["up"]),
            "down_human": human_bytes(v["down"]),
            "total_human": human_bytes(v["total"]),
        })
    node_totals_list.sort(key=lambda x: (x["total"], x["down"], x["up"], x["name"].lower()), reverse=True)

    return {
        "days": days,
        "node_totals": node_totals_list,
        "top_nodes": node_totals_list[: max(0, int(TOP_N))],
    }

def build_ai_data_pack() -> dict:
    """
    汇总一份给 AI 用的数据包。
    """
    now = now_dt()
    pack: dict = {
        "now": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "stat_tz": STAT_TZ,
    }

    try:
        pack["today"] = build_today_delta_struct()
    except Exception:
        logging.exception("build_today_delta_struct error")
        pack["today"] = {"error": "failed"}

    try:
        pack["last_24h"] = build_records_summary(24)
    except Exception:
        logging.exception("build_records_summary(24) error")
        pack["last_24h"] = {"error": "failed"}

    try:
        pack["last_1h_by_node"] = get_last_hours_nodes_struct(1)
    except Exception:
        logging.exception("get_last_hours_nodes_struct(1) error")
        pack["last_1h_by_node"] = {"error": "failed"}

    try:
        pack["last_24h_hourly"] = build_last_24h_hourly_summary()
    except Exception:
        logging.exception("build_last_24h_hourly_summary error")
        pack["last_24h_hourly"] = {"error": "failed"}

    try:
        pack["today_hourly_by_node"] = build_today_hourly_by_node_summary()
    except Exception:
        logging.exception("build_today_hourly_by_node_summary error")
        pack["today_hourly_by_node"] = {"error": "failed"}

    try:
        pack["yesterday_hourly_by_node"] = build_yesterday_hourly_by_node_summary()
    except Exception:
        logging.exception("build_yesterday_hourly_by_node_summary error")
        pack["yesterday_hourly_by_node"] = {"error": "failed"}

    try:
        pack["last_7d"] = build_records_summary(168)
    except Exception:
        logging.exception("build_records_summary(168) error")
        pack["last_7d"] = {"error": "failed"}

    try:
        pack["last_30d"] = build_records_summary(720)
    except Exception:
        logging.exception("build_records_summary(720) error")
        pack["last_30d"] = {"error": "failed"}

    return pack


def format_report(title: str, period_label: str, deltas: dict, reset_warnings: list[str], skipped: list[str] | None = None, include_top: bool = True) -> str:
    skipped = skipped or []

    lines = [f"📊 <b>{telegram_html_escape(title)}</b>（{telegram_html_escape(period_label)}）", ""]
    total_up = 0
    total_down = 0

    items = sorted(deltas.values(), key=lambda x: (x.get("name") or "").lower())
    for it in items:
        total_up += int(it["up"])
        total_down += int(it["down"])
        lines.append(
            f"🖥 <b>{telegram_html_escape(it['name'])}</b>\n"
            f"⬇️ 下行：{human_bytes(it['down'])}\n"
            f"⬆️ 上行：{human_bytes(it['up'])}\n"
        )

    lines.append("——")
    lines.append(f"📦 <b>总下行</b>：{human_bytes(total_down)}")
    lines.append(f"📦 <b>总上行</b>：{human_bytes(total_up)}")
    lines.append(f"📦 <b>总合计</b>：{human_bytes(total_down + total_up)}")

    if include_top:
        lines.append("")
        lines.append(f"🔥 <b>Top {TOP_N} 消耗榜</b>（上下行合计）")
        lines.extend(top_lines(deltas, n=TOP_N))

    if skipped:
        lines.append("")
        lines.append("⚠️ <b>以下节点因异常被跳过</b>：")
        lines.append("、".join(telegram_html_escape(item) for item in skipped[:30]) + ("……" if len(skipped) > 30 else ""))

    if reset_warnings:
        lines.append("")
        lines.append("⚠️ <b>检测到计数器可能重置</b>（已兜底）：")
        lines.append("、".join(telegram_html_escape(item) for item in reset_warnings))

    return "\n".join(lines)


def format_top_only_message(period_label: str, deltas: dict, reset_warnings: list[str], skipped: list[str] | None = None) -> str:
    skipped = skipped or []
    lines = [f"🔥 <b>Top {TOP_N} 消耗榜</b>（上下行合计）", f"⏱ {telegram_html_escape(period_label)}", ""]
    lines.extend(top_lines(deltas, n=TOP_N))

    if skipped:
        lines.append("")
        lines.append("⚠️ <b>以下节点因异常被跳过</b>：")
        lines.append("、".join(telegram_html_escape(item) for item in skipped[:30]) + ("……" if len(skipped) > 30 else ""))

    if reset_warnings:
        lines.append("")
        lines.append("⚠️ <b>检测到计数器可能重置</b>（已兜底）：")
        lines.append("、".join(telegram_html_escape(item) for item in reset_warnings))

    return "\n".join(lines)


def send_top_only(period_label: str, deltas: dict, reset_warnings: list[str], skipped: list[str] | None = None):
    telegram_send(format_top_only_message(period_label, deltas, reset_warnings, skipped=skipped))


# -------------------- 历史数据：热存储 + 冷归档（gzip） --------------------

def archive_path_for_month(ym: str) -> str:
    return os.path.join(DATA_DIR, f"history-{ym}.json.gz")


def load_archive_month(ym: str) -> dict:
    path = archive_path_for_month(ym)
    if not os.path.exists(path):
        return {"days": {}}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def save_archive_month(ym: str, data: dict):
    path = archive_path_for_month(ym)
    tmp = unique_temp_path(path)
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def archive_and_prune_history():
    ensure_dirs()
    hist = load_json(HISTORY_PATH, {"days": {}})
    days: dict = hist.get("days", {})
    if not days:
        return

    today = today_date()
    hot_cut = today - timedelta(days=HISTORY_HOT_DAYS)
    retention_cut = today - timedelta(days=HISTORY_RETENTION_DAYS)

    to_keep = {}
    to_archive_by_month: dict[str, dict] = {}

    for k, v in days.items():
        try:
            d = datetime.strptime(k, "%Y-%m-%d").date()
        except Exception:
            continue

        if d < retention_cut:
            continue

        if d < hot_cut:
            ym = yyyymm(d)
            to_archive_by_month.setdefault(ym, {})
            to_archive_by_month[ym][k] = v
        else:
            to_keep[k] = v

    for ym, month_days in to_archive_by_month.items():
        arc = load_archive_month(ym)
        arc.setdefault("days", {})
        arc["days"].update(month_days)

        pruned = {}
        for dk, dv in arc["days"].items():
            try:
                dd = datetime.strptime(dk, "%Y-%m-%d").date()
            except Exception:
                continue
            if dd >= retention_cut:
                pruned[dk] = dv
        arc["days"] = pruned
        save_archive_month(ym, arc)

    save_json_atomic(HISTORY_PATH, {"days": to_keep})


def history_sum(from_day: date, to_day: date) -> dict:
    ensure_dirs()
    period = build_daily_period_usage(start_of_day(from_day), start_of_day(to_day + timedelta(days=1)))
    if period.get("nodes"):
        return {
            str(item.get("uuid") or item.get("name")): {
                "name": item.get("name") or item.get("uuid") or "",
                "up": int(item.get("up", 0) or 0),
                "down": int(item.get("down", 0) or 0),
            }
            for item in period.get("nodes", [])
        }

    summed = {}
    hot = load_json(HISTORY_PATH, {"days": {}}).get("days", {})

    def add_one_day(one: dict):
        for uuid, v in one.items():
            if uuid not in summed:
                summed[uuid] = {"name": v.get("name", uuid), "up": 0, "down": 0}
            summed[uuid]["up"] += int(v.get("up", 0))
            summed[uuid]["down"] += int(v.get("down", 0))

    d = from_day
    while d <= to_day:
        key = d.strftime("%Y-%m-%d")
        if key in hot:
            add_one_day(hot.get(key, {}))
        else:
            ym = yyyymm(d)
            arc = load_archive_month(ym).get("days", {})
            add_one_day(arc.get(key, {}))
        d += timedelta(days=1)

    return summed


# -------------------- 采样器（用于连续快照、/top Nh 和告警） --------------------

def load_samples():
    return load_json(SAMPLES_PATH, {"samples": []})


def save_samples(data: dict):
    save_json_atomic(SAMPLES_PATH, data)


def prune_samples(samples: list, now_ts: int):
    keep_after = now_ts - SAMPLE_RETENTION_HOURS * 3600
    pruned = [s for s in samples if int(s.get("ts", 0)) >= keep_after]
    pruned.sort(key=lambda x: int(x.get("ts", 0)))
    return pruned


def take_sample_if_due(force: bool = False, record: bool = True, source: str = "sample-worker"):
    """
    由 bot 循环周期性调用：最多每 SAMPLE_INTERVAL_SECONDS 采样一次
    """
    ensure_dirs()
    data = load_samples()
    samples = data.get("samples", [])
    now_ts = int(time.time())

    last_ts = int(samples[-1]["ts"]) if samples else 0
    if (not force) and last_ts and (now_ts - last_ts < SAMPLE_INTERVAL_SECONDS):
        return

    started = time.time()
    try:
        current, skipped = fetch_nodes_and_totals()
        nodes_map = build_nodes_map_from_current(current)

        save_traffic_snapshot(now_ts, nodes_map, skipped)
        segment_result = materialize_latest_traffic_segment(now_ts)
        cleanup_result = {}
        try:
            cleanup_result["snapshots_deleted"] = prune_traffic_snapshots(now_ts)
        except Exception:
            logging.exception("failed to prune traffic_snapshots")
        try:
            cleanup_result["segments_deleted"] = prune_traffic_segments(TRAFFIC_SNAPSHOT_RETENTION_DAYS, now_ts)
        except Exception:
            logging.exception("failed to prune traffic_segments")

        samples.append({"ts": now_ts, "nodes": nodes_map, "skipped": skipped})
        samples = prune_samples(samples, now_ts)
        save_samples({"samples": samples})
        if record:
            safe_record_task_run(
                "sample",
                source,
                "success",
                started_at=started,
                finished_at=time.time(),
                summary=f"采样 {len(nodes_map)} 个节点，跳过 {len(skipped)} 个",
                metadata={"nodes": len(nodes_map), "skipped": skipped, "force": bool(force), "segments": segment_result, "cleanup": cleanup_result},
            )
    except Exception as exc:
        if record:
            safe_record_task_run(
                "sample",
                source,
                "failed",
                started_at=started,
                finished_at=time.time(),
                summary="采样失败",
                error=str(exc),
                metadata={"force": bool(force)},
            )
        raise


def sample_worker_maintenance_due(
    current: datetime | None = None,
    last_prune_date: date | None = None,
    last_vacuum_date: date | None = None,
) -> tuple[bool, bool]:
    now_value = current or now_dt()
    today_value = now_value.date()
    prune_due = now_value.hour >= 2 and last_prune_date != today_value
    vacuum_due = now_value.weekday() == 6 and now_value.hour >= 3 and last_vacuum_date != today_value
    return prune_due, vacuum_due


def run_sample_worker_daily_prune(now_value: datetime | None = None) -> dict:
    current = now_value or now_dt()
    started = time.time()
    result = {"task_runs": None, "node_daily_usage": None}
    try:
        result["task_runs"] = prune_task_runs()
    except Exception as exc:
        logging.exception("sample worker task_runs prune failed")
        result["task_runs"] = {"ok": False, "error": str(exc)}
    try:
        result["node_daily_usage"] = prune_node_daily_usage(today_value=current.date())
    except Exception as exc:
        logging.exception("sample worker node_daily_usage prune failed")
        result["node_daily_usage"] = {"ok": False, "error": str(exc)}
    ok = not any(isinstance(item, dict) and item.get("ok") is False for item in result.values())
    safe_record_task_run(
        "maintenance",
        "sample-worker:daily-prune",
        "success" if ok else "failed",
        started_at=started,
        finished_at=time.time(),
        summary=(
            f"task_runs 清理 {((result.get('task_runs') or {}).get('deleted') or 0)} 条；"
            f"daily_usage 清理 {((result.get('node_daily_usage') or {}).get('deleted') or 0)} 条"
        ),
        error="; ".join(str(item.get("error")) for item in result.values() if isinstance(item, dict) and item.get("ok") is False),
        metadata=result,
    )
    return result


def run_sample_worker_weekly_vacuum() -> dict:
    started = time.time()
    try:
        result = vacuum_traffic_db()
        safe_record_task_run(
            "maintenance",
            "sample-worker:weekly-vacuum",
            "success",
            started_at=started,
            finished_at=time.time(),
            summary=f"SQLite 已压缩，释放 {result.get('saved_human', '0 B')}",
            metadata={
                "before_size": result.get("before_size"),
                "after_size": result.get("after_size"),
                "saved_bytes": result.get("saved_bytes"),
            },
        )
        return result
    except Exception as exc:
        logging.exception("sample worker weekly vacuum failed")
        safe_record_task_run(
            "maintenance",
            "sample-worker:weekly-vacuum",
            "failed",
            started_at=started,
            finished_at=time.time(),
            error=str(exc),
        )
        return {"ok": False, "error": str(exc)}


def sample_worker_loop():
    logging.info("sample worker started, interval=%ss", SAMPLE_INTERVAL_SECONDS)
    last_prune_date = None
    last_vacuum_date = None
    while not SAMPLE_STOP_EVENT.is_set():
        try:
            take_sample_if_due(force=False)
            run_alert_check(dry_run=False, notify=telegram_configured(), force_sample=False)
            now_value = now_dt()
            prune_due, vacuum_due = sample_worker_maintenance_due(now_value, last_prune_date, last_vacuum_date)
            if prune_due:
                run_sample_worker_daily_prune(now_value)
                last_prune_date = now_value.date()
            if vacuum_due:
                run_sample_worker_weekly_vacuum()
                last_vacuum_date = now_value.date()
        except Exception:
            logging.exception("sample worker error")
        SAMPLE_STOP_EVENT.wait(timeout=max(1, SAMPLE_INTERVAL_SECONDS))
    logging.info("sample worker stopped")


def start_sample_worker():
    global SAMPLE_THREAD
    if SAMPLE_THREAD and SAMPLE_THREAD.is_alive():
        return
    SAMPLE_STOP_EVENT.clear()
    SAMPLE_THREAD = threading.Thread(target=sample_worker_loop, name="sample-worker", daemon=True)
    SAMPLE_THREAD.start()


def stop_sample_worker():
    SAMPLE_STOP_EVENT.set()
    if SAMPLE_THREAD and SAMPLE_THREAD.is_alive():
        SAMPLE_THREAD.join(timeout=3)


# -------------------- 智能告警 --------------------

def parse_silence_windows(value: str) -> list[tuple[int, int]]:
    """
    解析静默窗口，格式：HH:MM-HH:MM,23:00-07:00。
    返回当天分钟数区间，支持跨午夜。
    """
    text = validate_silence_windows_text(value)
    if not text:
        return []

    windows = []
    for part in re.split(r"[,;]\s*", text):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", part.strip())
        sh, sm, eh, em = [int(x) for x in m.groups()]
        start = sh * 60 + sm
        end = eh * 60 + em
        windows.append((start, end))
    return windows


def is_in_silence_window(now: datetime | None = None, windows_text: str | None = None) -> bool:
    windows = parse_silence_windows(ALERT_SILENCE_WINDOWS if windows_text is None else windows_text)
    if not windows:
        return False
    now = now or now_dt()
    minute = now.hour * 60 + now.minute
    for start, end in windows:
        if start < end and start <= minute < end:
            return True
        if start > end and (minute >= start or minute < end):
            return True
    return False


def load_alerts_state() -> dict:
    with file_lock(ALERTS_STATE_PATH):
        data = load_json(ALERTS_STATE_PATH, {})
        if not isinstance(data, dict):
            data = {}
        data.setdefault("version", 1)
        data.setdefault("active", {})
        data.setdefault("node_skips", {})
        data.setdefault("muted_until", 0)
        return data


def save_alerts_state(data: dict):
    with file_lock(ALERTS_STATE_PATH):
        save_json_atomic(ALERTS_STATE_PATH, data)


def alerts_muted_until_dt(state: dict) -> datetime | None:
    muted_until = int(state.get("muted_until", 0) or 0)
    if muted_until <= int(time.time()):
        return None
    return datetime.fromtimestamp(muted_until, TZ)


def set_alerts_muted_for(hours: int) -> datetime:
    if hours <= 0:
        raise RuntimeError("mute hours must be > 0")
    ensure_dirs()
    state = load_alerts_state()
    muted_until = int(time.time()) + hours * 3600
    state["muted_until"] = muted_until
    save_alerts_state(state)
    return datetime.fromtimestamp(muted_until, TZ)


def clear_alerts_muted():
    ensure_dirs()
    state = load_alerts_state()
    state["muted_until"] = 0
    save_alerts_state(state)


def parse_mute_hours_arg(value: str) -> int:
    text = (value or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*h?", text)
    if not m:
        raise RuntimeError("用法：/mute_alerts 2h")
    hours = int(m.group(1))
    if hours <= 0:
        raise RuntimeError("用法：/mute_alerts 2h（N>0）")
    return hours


def _alert_escape(value) -> str:
    return telegram_html_escape(value)


def _alert_event(key: str, alert_type: str, title: str, body: str) -> dict:
    return {
        "key": key,
        "type": alert_type,
        "title": title,
        "body": body,
    }


def _skip_name(skip: str) -> str:
    text = str(skip or "").strip()
    if not text:
        return "unknown"
    return re.sub(r"\([^)]*\)$", "", text).strip() or text


def _sum_deltas(deltas: dict) -> tuple[int, int, int]:
    total_up = 0
    total_down = 0
    for item in deltas.values():
        total_up += int(item.get("up", 0))
        total_down += int(item.get("down", 0))
    total = total_up + total_down
    return total_up, total_down, total


def collect_alert_candidates(state: dict, now_ts: int | None = None) -> list[dict]:
    """
    生成当前应该处于 active 状态的告警候选，并更新连续节点失败计数。
    """
    now_ts = now_ts or int(time.time())
    candidates: list[dict] = []

    samples_data = load_samples()
    samples = samples_data.get("samples", []) if isinstance(samples_data, dict) else []
    latest = samples[-1] if samples else None

    if latest:
        latest_ts = int(latest.get("ts", now_ts) or now_ts)
        skipped = [str(x) for x in (latest.get("skipped", []) or [])]
        skipped_names = {_skip_name(x): x for x in skipped}
        node_skips = state.setdefault("node_skips", {})

        for name, raw in skipped_names.items():
            rec = node_skips.get(name, {})
            if int(rec.get("last_sample_ts", 0) or 0) == latest_ts:
                count = int(rec.get("count", 0))
            else:
                count = int(rec.get("count", 0)) + 1
            node_skips[name] = {
                "count": count,
                "last_seen": now_ts,
                "last_sample_ts": latest_ts,
                "last_reason": raw,
            }
            if count >= ALERT_NODE_MISSING_SAMPLES:
                candidates.append(_alert_event(
                    f"node_missing:{name}",
                    "node_missing",
                    f"节点连续采样异常：{name}",
                    (
                        f"节点 <b>{_alert_escape(name)}</b> 已连续 "
                        f"<b>{count}</b> 次采样失败。\n"
                        f"最近原因：<code>{_alert_escape(raw)}</code>"
                    ),
                ))

        for name in list(node_skips.keys()):
            if name not in skipped_names:
                node_skips[name]["count"] = 0

        window_threshold_enabled = ALERT_TOTAL_WINDOW_BYTES > 0 or ALERT_NODE_WINDOW_BYTES > 0
        if window_threshold_enabled:
            target_ts = now_ts - ALERT_WINDOW_MINUTES * 60
            usage = snapshot_range_usage(target_ts, now_ts)
            if int(usage.get("sample_count", 0) or 0) >= 2:
                deltas = usage.get("nodes", {})
                reset_warnings = usage.get("reset_warnings", [])
                total_up, total_down, total = _sum_deltas(deltas)
                from_dt = datetime.fromtimestamp(int(usage.get("from_ts", target_ts) or target_ts), TZ)
                to_dt = datetime.fromtimestamp(int(usage.get("to_ts", now_ts) or now_ts), TZ)
                label = f"{from_dt.strftime('%Y-%m-%d %H:%M')} -> {to_dt.strftime('%Y-%m-%d %H:%M')}"

                if ALERT_TOTAL_WINDOW_BYTES > 0 and total >= ALERT_TOTAL_WINDOW_BYTES:
                    body = (
                        f"窗口：<code>{_alert_escape(label)}</code>\n"
                        f"合计：<b>{human_bytes(total)}</b>"
                        f"（下行 {human_bytes(total_down)} / 上行 {human_bytes(total_up)}）\n"
                        f"阈值：<b>{human_bytes(ALERT_TOTAL_WINDOW_BYTES)}</b>"
                    )
                    if reset_warnings:
                        body += f"\n计数器重置/缺样节点：{_alert_escape('、'.join(reset_warnings[:10]))}"
                    candidates.append(_alert_event("window_total", "window_total", "窗口总流量超阈值", body))

                if ALERT_NODE_WINDOW_BYTES > 0:
                    for uuid, item in deltas.items():
                        up = int(item.get("up", 0))
                        down = int(item.get("down", 0))
                        total_node = up + down
                        if total_node < ALERT_NODE_WINDOW_BYTES:
                            continue
                        name = item.get("name") or uuid
                        body = (
                            f"节点：<b>{_alert_escape(name)}</b>\n"
                            f"窗口：<code>{_alert_escape(label)}</code>\n"
                            f"合计：<b>{human_bytes(total_node)}</b>"
                            f"（下行 {human_bytes(down)} / 上行 {human_bytes(up)}）\n"
                            f"阈值：<b>{human_bytes(ALERT_NODE_WINDOW_BYTES)}</b>"
                        )
                        candidates.append(_alert_event(
                            f"window_node:{uuid}",
                            "window_node",
                            f"节点窗口流量超阈值：{name}",
                            body,
                        ))

    if ALERT_DAILY_TOTAL_BYTES > 0 or ALERT_DAILY_NODE_BYTES > 0:
        today = build_today_delta_struct()
        if isinstance(today, dict) and int(today.get("sample_count", 0) or 0) >= 2:
            nodes = today.get("nodes", []) or []
            total_up = sum(int(n.get("up", 0)) for n in nodes)
            total_down = sum(int(n.get("down", 0)) for n in nodes)
            total = total_up + total_down
            if ALERT_DAILY_TOTAL_BYTES > 0 and total >= ALERT_DAILY_TOTAL_BYTES:
                candidates.append(_alert_event(
                    "daily_total",
                    "daily_total",
                    "今日总流量超阈值",
                    (
                        f"日期：<code>{_alert_escape(today.get('date', 'today'))}</code>\n"
                        f"合计：<b>{human_bytes(total)}</b>"
                        f"（下行 {human_bytes(total_down)} / 上行 {human_bytes(total_up)}）\n"
                        f"阈值：<b>{human_bytes(ALERT_DAILY_TOTAL_BYTES)}</b>"
                    ),
                ))

            if ALERT_DAILY_NODE_BYTES > 0:
                for item in nodes:
                    total_node = int(item.get("total", 0))
                    if total_node < ALERT_DAILY_NODE_BYTES:
                        continue
                    name = item.get("name") or item.get("uuid") or "unknown"
                    candidates.append(_alert_event(
                        f"daily_node:{item.get('uuid', name)}",
                        "daily_node",
                        f"节点今日流量超阈值：{name}",
                        (
                            f"节点：<b>{_alert_escape(name)}</b>\n"
                            f"今日合计：<b>{human_bytes(total_node)}</b>"
                            f"（下行 {human_bytes(int(item.get('down', 0)))} / "
                            f"上行 {human_bytes(int(item.get('up', 0)))})\n"
                            f"阈值：<b>{human_bytes(ALERT_DAILY_NODE_BYTES)}</b>"
                        ),
                    ))

    return candidates


def format_alert_message(event: dict, now_ts: int, repeated: bool = False) -> str:
    ts = datetime.fromtimestamp(now_ts, TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    prefix = "⚠️ <b>Komari 告警</b>"
    if repeated:
        prefix = "⚠️ <b>Komari 告警仍在持续</b>"
    return (
        f"{prefix}\n"
        f"🕒 {ts}\n"
        f"📌 <b>{_alert_escape(event.get('title', '告警'))}</b>\n"
        f"{event.get('body', '')}"
    )


def format_recovery_message(record: dict, now_ts: int) -> str:
    ts = datetime.fromtimestamp(now_ts, TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    title = record.get("title", "告警")
    return (
        "✅ <b>Komari 告警恢复</b>\n"
        f"🕒 {ts}\n"
        f"📌 <b>{_alert_escape(title)}</b>\n"
        "当前规则已不再触发。"
    )


def apply_alert_candidates(state: dict, candidates: list[dict], now_ts: int, dry_run: bool = False) -> list[dict]:
    active = state.setdefault("active", {})
    candidate_keys = {c["key"] for c in candidates}
    muted_until = int(state.get("muted_until", 0) or 0)
    muted = muted_until > now_ts or is_in_silence_window(datetime.fromtimestamp(now_ts, TZ))
    events: list[dict] = []

    for candidate in candidates:
        key = candidate["key"]
        rec = active.get(key)
        is_new = rec is None
        if rec is None:
            rec = {
                "type": candidate.get("type"),
                "title": candidate.get("title"),
                "body": candidate.get("body"),
                "first_seen": now_ts,
                "last_seen": now_ts,
                "last_sent": 0,
            }
            active[key] = rec
        else:
            rec["title"] = candidate.get("title")
            rec["body"] = candidate.get("body")
            rec["last_seen"] = now_ts

        last_sent = int(rec.get("last_sent", 0) or 0)
        # 节点离线属于状态型告警：持续离线期间只通知一次，恢复后 active
        # 记录会被移除，下次再次离线时才重新通知。流量阈值告警仍按冷却时间
        # 重复提醒，避免长期超阈值时完全失去提示。
        repeatable = candidate.get("type") != "node_missing"
        due = last_sent == 0 or (
            repeatable and now_ts - last_sent >= ALERT_COOLDOWN_SECONDS
        )
        if due:
            repeated = (not is_new) and last_sent > 0
            events.append({
                "kind": "alert",
                "key": key,
                "title": rec.get("title", key),
                "message": format_alert_message(candidate, now_ts, repeated=repeated),
                "suppressed": muted,
                "reason": "muted" if muted else "",
                "rec": rec,
            })

    for key, rec in list(active.items()):
        if key in candidate_keys:
            continue
        if ALERT_RECOVERY_NOTIFY:
            events.append({
                "kind": "recovery",
                "key": key,
                "title": rec.get("title", key),
                "message": format_recovery_message(rec, now_ts),
                "suppressed": muted,
                "reason": "muted" if muted else "",
            })
        if not dry_run:
            del active[key]

    return events


def _run_alert_check_impl(dry_run: bool = False, notify: bool = True, force_sample: bool = False) -> dict:
    ensure_dirs()
    now_ts = int(time.time())
    if not ALERTS_ENABLED:
        return {"enabled": False, "events": [], "active_count": 0}

    if force_sample:
        take_sample_if_due(force=True, record=False, source="alert-check")

    state = load_alerts_state()
    work_state = json.loads(json.dumps(state)) if dry_run else state
    candidates = collect_alert_candidates(work_state, now_ts=now_ts)
    events = apply_alert_candidates(work_state, candidates, now_ts=now_ts, dry_run=dry_run)

    if not dry_run:
        sent_keys = []
        if notify:
            for event in events:
                if event.get("suppressed"):
                    continue
                try:
                    telegram_send_alert(event["message"])
                    if event["kind"] == "alert" and "rec" in event:
                        event["rec"]["last_sent"] = now_ts
                        sent_keys.append(event["key"])
                except requests.exceptions.HTTPError as e:
                    if e.response is not None and e.response.status_code == 400:
                        telegram_send_to_chat(re.sub(r"</?[^>]+>", "", event["message"]), telegram_alert_chat_id(), parse_mode=None)
                        if event["kind"] == "alert" and "rec" in event:
                            event["rec"]["last_sent"] = now_ts
                            sent_keys.append(event["key"])
                    else:
                        logging.warning(f"Failed to send alert {event.get('key')}: {e}")
        save_alerts_state(work_state)

    return {
        "enabled": True,
        "events": events,
        "active_count": len(work_state.get("active", {})),
        "muted_until": int(work_state.get("muted_until", 0) or 0),
        "dry_run": dry_run,
    }


def run_alert_check(dry_run: bool = False, notify: bool = True, force_sample: bool = False, record: bool = True, source: str = "alert-check") -> dict:
    started = time.time()
    try:
        result = _run_alert_check_impl(dry_run=dry_run, notify=notify, force_sample=force_sample)
        events = result.get("events", []) or []
        summary = "告警未启用" if not result.get("enabled", True) else f"事件 {len(events)} 个，active {int(result.get('active_count', 0) or 0)} 个"
        if record:
            safe_record_task_run(
                "alert",
                source,
                "success",
                started_at=started,
                finished_at=time.time(),
                summary=summary,
                metadata={
                    "dry_run": bool(dry_run),
                    "notify": bool(notify),
                    "force_sample": bool(force_sample),
                    "events": len(events),
                    "active_count": int(result.get("active_count", 0) or 0),
                },
            )
        return result
    except Exception as exc:
        if record:
            safe_record_task_run(
                "alert",
                source,
                "failed",
                started_at=started,
                finished_at=time.time(),
                summary="告警检查失败",
                error=str(exc),
                metadata={"dry_run": bool(dry_run), "notify": bool(notify), "force_sample": bool(force_sample)},
            )
        raise


def format_alert_check_result(result: dict) -> str:
    if not result.get("enabled", True):
        return "alerts disabled"
    events = result.get("events", []) or []
    lines = [
        f"alerts active={int(result.get('active_count', 0))}",
        f"events={len(events)}",
    ]
    for event in events:
        suffix = " suppressed" if event.get("suppressed") else ""
        lines.append(f"- {event.get('kind')} {event.get('key')}: {event.get('title')}{suffix}")
    return "\n".join(lines)


def format_alert_status() -> str:
    state = load_alerts_state()
    active = state.get("active", {}) or {}
    muted_until = alerts_muted_until_dt(state)

    lines = ["🚨 <b>告警状态</b>"]
    lines.append(f"启用：{'是' if ALERTS_ENABLED else '否'}")
    lines.append(f"告警 chat：<code>{_alert_escape(telegram_alert_chat_id())}</code>")
    if muted_until:
        lines.append(f"静默至：<code>{muted_until.strftime('%Y-%m-%d %H:%M:%S %Z')}</code>")
    elif is_in_silence_window():
        lines.append("当前处于静默时段")
    else:
        lines.append("静默：否")

    thresholds = [
        ("窗口总流量", ALERT_TOTAL_WINDOW_BYTES),
        ("节点窗口流量", ALERT_NODE_WINDOW_BYTES),
        ("今日总流量", ALERT_DAILY_TOTAL_BYTES),
        ("节点今日流量", ALERT_DAILY_NODE_BYTES),
    ]
    enabled_thresholds = [f"{name} {human_bytes(value)}" for name, value in thresholds if value > 0]
    lines.append(f"窗口：{ALERT_WINDOW_MINUTES} 分钟，冷却：{ALERT_COOLDOWN_SECONDS} 秒")
    lines.append("阈值：" + ("；".join(enabled_thresholds) if enabled_thresholds else "未配置流量阈值"))

    if not active:
        lines.append("")
        lines.append("当前无 active 告警。")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"Active 告警：{len(active)}")
    now_ts = int(time.time())
    for rec in active.values():
        last_seen = datetime.fromtimestamp(int(rec.get("last_seen", now_ts)), TZ).strftime("%m-%d %H:%M")
        lines.append(f"- <b>{_alert_escape(rec.get('title', '告警'))}</b>（last seen {last_seen}）")
    return "\n".join(lines)


# -------------------- 报表任务 --------------------
# 报表只读不写：每日汇总（node_daily_usage）由采样器在 take_sample_if_due 中持续物化，
# 报表任务统一通过 build_scope_report_message 读取上一完整周期并格式化发送。

def run_daily_send_yesterday():
    """每天 00:00：发送昨日日报（纯读取）"""
    telegram_send(build_scope_report_message("daily"))


def run_weekly_send_last_week():
    """每周一 00:00：发送上周周报（纯读取）"""
    telegram_send(build_scope_report_message("weekly"))


def run_monthly_send_last_month():
    """每月 1 日 00:00：发送上月月报（纯读取）"""
    telegram_send(build_scope_report_message("monthly"))


def build_period_report_message(from_dt: datetime, to_dt: datetime, tag: str, top_only: bool = False, title: str = "流量统计", period_label: str | None = None) -> str:
    ensure_dirs()
    period_label = period_label or f"{from_dt.strftime('%Y-%m-%d %H:%M')} → {to_dt.strftime('%Y-%m-%d %H:%M')}"
    period = build_live_period_struct(from_dt, to_dt, period_label)
    deltas = {str(item.get("uuid") or item.get("name")): item for item in period.get("nodes", [])}
    skipped = period.get("skipped", [])
    reset_warnings = period.get("reset_warnings", [])
    if int(period.get("sample_count", 0) or 0) < 2 and not deltas:
        return (
            f"⚠️ 当前采样点不足，无法计算 {telegram_html_escape(period_label)} 的稳定统计。\n"
            f"请保持 bot/listen 服务运行，至少产生 2 个采样点后再试。"
        )

    if top_only:
        return format_top_only_message(period_label, deltas, reset_warnings, skipped=skipped)
    return format_report(title, period_label, deltas, reset_warnings, skipped=skipped, include_top=True)


def run_period_report(from_dt: datetime, to_dt: datetime, tag: str, top_only: bool = False):
    telegram_send(build_period_report_message(from_dt, to_dt, tag, top_only=top_only))


def scheduled_report_period_parts(scope: str):
    """
    定时报表统计“上一完整周期”（不含当前未走完的周期）：
    - daily:   昨天 00:00 → 今天 00:00
    - weekly:  上周一 00:00 → 本周一 00:00
    - monthly: 上月 1 日 00:00 → 本月 1 日 00:00
    """
    td = today_date()
    if scope == "daily":
        prev_day = td - timedelta(days=1)
        return start_of_day(prev_day), start_of_day(td), prev_day.strftime("%Y-%m-%d")
    if scope == "weekly":
        this_week_start = start_of_week(td)
        prev_week_start = this_week_start - timedelta(days=7)
        return start_of_day(prev_week_start), start_of_day(this_week_start), f"WEEK-{prev_week_start.strftime('%Y-%m-%d')}"
    if scope == "monthly":
        this_month_start = start_of_month(td)
        prev_month_start = start_of_month(this_month_start - timedelta(days=1))
        return start_of_day(prev_month_start), start_of_day(this_month_start), f"MONTH-{prev_month_start.strftime('%Y-%m-%d')}"
    raise RuntimeError("scope must be daily, weekly, or monthly")


SCOPE_REPORT_TITLES = {"daily": "昨日流量日报", "weekly": "上周流量周报", "monthly": "上月流量月报"}


def scope_report_period_label(scope: str, start: datetime, end: datetime, tag: str) -> str:
    if scope == "daily":
        return tag
    last_day = (end - timedelta(days=1)).strftime("%Y-%m-%d")
    return f"{start.strftime('%Y-%m-%d')} → {last_day}"


def build_scope_report_message(scope: str, top_only: bool = False) -> str:
    """
    内置日/周/月报与自定义计划共用的唯一报表出口：
    读取上一完整周期（出报表前强制补一次边界采样），格式化为对应标题的消息。
    """
    start, end, tag = scheduled_report_period_parts(scope)
    try:
        take_sample_if_due(force=True, source=f"{scope}-report-boundary")
    except Exception:
        logging.exception("failed to capture %s report boundary snapshot", scope)
    return build_period_report_message(
        start,
        end,
        tag,
        top_only=top_only,
        title=SCOPE_REPORT_TITLES[scope],
        period_label=scope_report_period_label(scope, start, end, tag),
    )


def _run_report_schedule_impl(item: dict) -> dict:
    schedule = normalize_report_schedule(item)
    message = build_scope_report_message(schedule["scope"], top_only=(schedule["mode"] == "top"))
    chat = schedule.get("chat") or TELEGRAM_CHAT_ID
    telegram_send_to_chat(message, chat)
    return {"sent": True, "chat": chat, "schedule": schedule, "label": schedule_label(schedule)}


def run_report_schedule(item: dict, source: str = "scheduler", record: bool = True) -> dict:
    schedule = normalize_report_schedule(item)
    metadata = {
        "schedule_id": schedule.get("id", ""),
        "scope": schedule.get("scope", ""),
        "mode": schedule.get("mode", ""),
        "label": schedule_label(schedule),
    }
    if not record:
        return _run_report_schedule_impl(schedule)
    return run_with_task_record(
        "report",
        source,
        lambda: _run_report_schedule_impl(schedule),
        summary_func=lambda result: result.get("label", "") if isinstance(result, dict) else "",
        metadata=metadata,
    )


def scheduler_worker_loop():
    logging.info("report scheduler started")
    while not SCHEDULER_STOP_EVENT.is_set():
        try:
            data = load_report_schedules()
            changed = False
            now = now_dt()

            # Clean stale last_runs entries
            active_ids = {item["id"] for item in data.get("schedules", [])}
            stale_keys = [k for k in data["last_runs"].keys() if k not in active_ids]
            if stale_keys:
                for k in stale_keys:
                    del data["last_runs"][k]
                changed = True
                logging.info("cleaned %d stale last_runs entries", len(stale_keys))

            for item in data.get("schedules", []):
                if not item.get("enabled"):
                    continue
                due_key = schedule_due_key(item, now)
                if not due_key or data["last_runs"].get(item["id"]) == due_key:
                    continue
                run_report_schedule(item, source="scheduler")
                data["last_runs"][item["id"]] = due_key
                changed = True
            if changed:
                save_report_schedules(data)
        except Exception:
            logging.exception("report scheduler error")
        SCHEDULER_STOP_EVENT.wait(timeout=30)
    logging.info("report scheduler stopped")


def start_report_scheduler():
    global SCHEDULER_THREAD
    if SCHEDULER_THREAD and SCHEDULER_THREAD.is_alive():
        return
    SCHEDULER_STOP_EVENT.clear()
    SCHEDULER_THREAD = threading.Thread(target=scheduler_worker_loop, name="report-scheduler", daemon=True)
    SCHEDULER_THREAD.start()


def stop_report_scheduler():
    SCHEDULER_STOP_EVENT.set()
    if SCHEDULER_THREAD and SCHEDULER_THREAD.is_alive():
        SCHEDULER_THREAD.join(timeout=3)


def run_top_last_hours(hours: int):
    """
    /top Nh：最近 N 小时 Top 榜（合计）
    依赖 traffic_snapshots 连续快照。
    """
    ensure_dirs()
    if hours <= 0:
        telegram_send("用法：/top 6h（N>0）")
        return

    result = get_top_last_hours_struct(hours, TOP_N)
    if not result or result.get("error") or int(result.get("sample_count", 0) or 0) < 2:
        telegram_send(
            "⚠️ 还没有足够的连续快照来计算这个时间范围。\n"
            f"请保持 bot 服务运行一段时间后再试：/top {hours}h"
        )
        return

    label = f"{result.get('from', '')} → {result.get('to', '')}"
    deltas = {str(item.get("uuid") or item.get("name")): item for item in result.get("nodes", [])}
    send_top_only(label, deltas, result.get("reset_warnings", []), skipped=result.get("skipped", []))


def load_confirm_state() -> dict:
    return load_json(TG_CONFIRM_PATH, {"actions": {}})


def save_confirm_state(data: dict):
    save_json_atomic(TG_CONFIRM_PATH, data)


def set_confirm_action(chat_id: str, action: str, ttl_seconds: int = 600):
    data = load_confirm_state()
    data.setdefault("actions", {})
    code = str(secrets.randbelow(9000) + 1000)
    expires_at = int(time.time()) + ttl_seconds
    key = f"{chat_id}:{action}"
    data["actions"][key] = {"code": code, "expires_at": expires_at}
    save_confirm_state(data)
    return code, expires_at


def consume_confirm_action(chat_id: str, action: str, code: str) -> bool:
    data = load_confirm_state()
    key = f"{chat_id}:{action}"
    item = data.get("actions", {}).get(key)
    if not item:
        return False
    now_ts = int(time.time())
    ok = (str(item.get("code", "")) == str(code).strip()) and (int(item.get("expires_at", 0)) >= now_ts)
    if ok:
        data["actions"].pop(key, None)
        save_confirm_state(data)
        return True
    return False


def parse_chat_ids_env(name: str, default_value: str) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raw = str(default_value)
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    return ids


def is_allowed_chat(chat_id: str) -> bool:
    allowed = parse_chat_ids_env("TELEGRAM_ALLOWED_CHAT_IDS", str(TELEGRAM_CHAT_ID))
    return str(chat_id) in allowed


def is_admin(chat_id: str) -> bool:
    admins = parse_chat_ids_env("TELEGRAM_ADMIN_CHAT_IDS", str(TELEGRAM_CHAT_ID))
    return str(chat_id) in admins


# -------------------- Telegram 命令监听 --------------------

def get_updates(offset: int | None):
    """
    Telegram long polling 在公网环境下偶发被对端 reset 是正常的。
    这里做：网络错误自动重试 + 轻量退避，避免刷屏告警/频繁重连。
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 50}
    if offset is not None:
        params["offset"] = offset

    # 最多重试 5 次：总等待 ~ (1+2+4+8+16)s + 抖动
    backoff = 1.0
    last_exc = None
    for _ in range(5):
        try:
            r = requests.get(url, params=params, timeout=55)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code == 409:
                if should_alert("tg_409", 600):
                    safe_telegram_send("⚠️ Telegram 409 Conflict：可能有另一份实例在拉 getUpdates，请确认旧环境是否已停止。")
                raise
            if status_code in (429, 500, 502, 503, 504):
                last_exc = e
                retry_after = None
                if e.response is not None:
                    retry_after = e.response.headers.get("Retry-After")
                sleep_seconds = backoff + random.random()
                if retry_after:
                    try:
                        sleep_seconds = max(sleep_seconds, min(float(retry_after), 60.0))
                    except ValueError:
                        pass
                logging.warning("Telegram getUpdates temporary HTTP %s; retrying", status_code)
                time.sleep(sleep_seconds)
                backoff = min(backoff * 2, 20.0)
                continue
            raise
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout,
                requests.exceptions.ChunkedEncodingError) as e:
            last_exc = e
            time.sleep(backoff + random.random())
            backoff = min(backoff * 2, 20.0)
            continue

    # 连续失败才抛出，让外层限频告警接管
    raise last_exc


def load_offset() -> int | None:
    try:
        with open(TG_OFFSET_PATH, "r", encoding="utf-8") as f:
            s = f.read().strip()
            return int(s) if s else None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def save_offset(val: int):
    save_text_atomic(TG_OFFSET_PATH, str(val))


def parse_top_scope(text: str):
    """
    /top
    /top today|week|month
    /top 6h /top 24h /top 168h
    """
    parts = text.strip().split()
    if len(parts) == 1:
        return ("today", None)

    arg = parts[1].strip().lower()
    if arg in ("today", "t"):
        return ("today", None)
    if arg in ("week", "w"):
        return ("week", None)
    if arg in ("month", "m"):
        return ("month", None)

    m = re.fullmatch(r"(\d+)\s*h", arg)
    if m:
        return ("hours", int(m.group(1)))

    return ("unknown", None)


def listen_commands():
    ensure_dirs()
    logging.info("Komari traffic bot starting (stat_tz=%s)", STAT_TZ)

    # 启动先采一次样；采样是实时统计主链路，不依赖 Telegram/报表推送。
    try:
        take_sample_if_due(force=True)
        run_alert_check(dry_run=False, notify=telegram_configured(), force_sample=False)
    except Exception:
        logging.exception("initial sample or alert check failed")
    start_sample_worker()

    if not telegram_configured():
        logging.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 未设置，进入仅采样模式")
        while not SHUTTING_DOWN:
            time.sleep(3)
        stop_sample_worker()
        return

    if BOT_START_NOTIFY and should_alert("bot_start", 60):
        instance_label = BOT_INSTANCE_NAME or "default"
        allowed_chats = parse_chat_ids_env("TELEGRAM_ALLOWED_CHAT_IDS", str(TELEGRAM_CHAT_ID))
        safe_telegram_send(
            "✅ Komari traffic bot 已启动\n"
            f"🧩 实例：{instance_label}\n"
            f"🕒 统计时区：{STAT_TZ}\n"
            f"💬 可接收命令 chat 数：{len(allowed_chats)}"
        )
    offset = load_offset()
    start_report_scheduler()

    _last_config_mtime = None
    while True:
        if SHUTTING_DOWN:
            logging.warning("shutdown flag set, exiting listen loop")
            stop_sample_worker()
            stop_report_scheduler()
            return

        # Check for runtime config changes
        try:
            config_path = runtime_config_path()
            if os.path.exists(config_path):
                current_mtime = os.path.getmtime(config_path)
                if _last_config_mtime is None:
                    _last_config_mtime = current_mtime
                elif current_mtime != _last_config_mtime:
                    logging.info("runtime_config.json changed, reloading")
                    config = load_runtime_config()
                    apply_runtime_config(config)
                    _last_config_mtime = current_mtime
        except Exception:
            logging.exception("failed to check/reload runtime config")

        try:
            data = get_updates(offset)
            if not data.get("ok"):
                time.sleep(3)
                continue

            for upd in data.get("result", []):
                update_id = upd.get("update_id")
                if update_id is not None:
                    offset = update_id + 1

                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue

                chat = msg.get("chat", {})
                chat_id = str(chat.get("id", ""))
                if not is_allowed_chat(chat_id):
                    continue

                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue

                parts = text.split()
                cmd = parts[0].lower()
                arg_text = parts[1] if len(parts) > 1 else ""

                now = now_dt()
                td = today_date()

                if cmd == "/today":
                    tag = td.strftime("%Y-%m-%d")
                    run_period_report(start_of_day(td), now, tag, top_only=False)

                elif cmd == "/week":
                    ws = start_of_week(td)
                    tag = f"WEEK-{ws.strftime('%Y-%m-%d')}"
                    run_period_report(start_of_day(ws), now, tag, top_only=False)

                elif cmd == "/month":
                    ms = start_of_month(td)
                    tag = f"MONTH-{ms.strftime('%Y-%m-%d')}"
                    run_period_report(start_of_day(ms), now, tag, top_only=False)

                elif cmd == "/top":
                    scope, hours = parse_top_scope(text)
                    if scope == "today":
                        tag = td.strftime("%Y-%m-%d")
                        run_period_report(start_of_day(td), now, tag, top_only=True)
                    elif scope == "week":
                        ws = start_of_week(td)
                        tag = f"WEEK-{ws.strftime('%Y-%m-%d')}"
                        run_period_report(start_of_day(ws), now, tag, top_only=True)
                    elif scope == "month":
                        ms = start_of_month(td)
                        tag = f"MONTH-{ms.strftime('%Y-%m-%d')}"
                        run_period_report(start_of_day(ms), now, tag, top_only=True)
                    elif scope == "hours":
                        run_top_last_hours(int(hours or 0))
                    else:
                        telegram_send("用法：/top  或  /top today|week|month  或  /top 6h")

                elif cmd == "/archive":
                    if not is_admin(chat_id):
                        telegram_send("⛔ 无权限")
                        continue
                    code, _ = set_confirm_action(chat_id, "archive")
                    telegram_send(
                        "⚠️ 准备执行 archive（归档 + 清理 history 热数据）。\n"
                        f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                        f"如需继续，请发送：/confirm_archive {code}"
                    )

                elif cmd == "/alerts":
                    if not is_admin(chat_id):
                        telegram_send("⛔ 无权限")
                        continue
                    telegram_send(format_alert_status())

                elif cmd == "/mute_alerts":
                    if not is_admin(chat_id):
                        telegram_send("⛔ 无权限")
                        continue
                    try:
                        hours = parse_mute_hours_arg(arg_text)
                    except Exception as e:
                        telegram_send(str(e))
                        continue
                    muted_until = set_alerts_muted_for(hours)
                    telegram_send(f"✅ 已静默告警至：<code>{muted_until.strftime('%Y-%m-%d %H:%M:%S %Z')}</code>")

                elif cmd == "/unmute_alerts":
                    if not is_admin(chat_id):
                        telegram_send("⛔ 无权限")
                        continue
                    clear_alerts_muted()
                    telegram_send("✅ 已解除告警静默")

                elif cmd == "/confirm_archive":
                    if not is_admin(chat_id):
                        telegram_send("⛔ 无权限")
                        continue
                    code = arg_text.strip()
                    if not consume_confirm_action(chat_id, "archive", code):
                        telegram_send("❌ 确认码无效或已过期")
                        continue
                    archive_and_prune_history()
                    telegram_send("✅ 已执行历史归档压缩")

                elif cmd in ("/ask", "/ai"):
                    question = text.partition(" ")[2].strip()
                    if not question:
                        telegram_send(
                            "用法：/ask 你的问题\n"
                            "示例：\n"
                            "/ask 今天哪个节点最耗流量？\n"
                            "/ask 最近 7 天流量大概是上升还是下降趋势？\n"
                            "/ask 帮我写一段今天流量情况的总结，适合发到群里。"
                        )
                        continue

                    if question_requires_fresh_ai_pack(question):
                        data_pack = build_ai_data_pack()
                    else:
                        data_pack = get_ai_data_pack_cached()
                    answer = ask_ai_with_data(question, data_pack)
                    ai_text = normalize_ai_answer_for_telegram(answer)
                    try:
                        telegram_send(ai_text)
                    except requests.exceptions.HTTPError as e:
                        if e.response is not None and e.response.status_code == 400:
                            logging.warning("telegram html parse failed, fallback to plain text send")
                            telegram_send_plain(re.sub(r"</?[^>]+>", "", ai_text))
                        else:
                            raise

                elif cmd in ("/help", "/start"):
                    telegram_send(
                        "可用命令：\n"
                        "/today  /week  /month\n"
                        "/top  (默认 today)\n"
                        "/top today|week|month\n"
                        "/top 6h（任意Nh）\n"
                        "/ask 你的问题（或 /ai）\n"
                        "管理员：/alerts /mute_alerts 2h /unmute_alerts\n"
                        "/archive\n"
                        "确认命令：/confirm_archive"
                    )

            if offset is not None:
                save_offset(offset)

        except Exception as e:
            if should_alert("listen", 300):
                alert_exception("listen_loop", "listen", e)
            redacted_msg = redact_sensitive_data(str(e))
            logging.error("listen loop error: %s", redacted_msg)
            time.sleep(3)

# -------------------- main --------------------

def main():
    if len(sys.argv) < 2:
        raise RuntimeError("Usage: report_daily | report_weekly | report_monthly | listen | check_alerts [--dry-run] | health | config-validate")

    cmd = sys.argv[1].strip().lower()

    if cmd == "report_daily":
        run_with_task_record("report", "cli:report_daily", run_daily_send_yesterday, summary_func=lambda _result: "昨日流量日报")
        return 0
    if cmd == "report_weekly":
        run_with_task_record("report", "cli:report_weekly", run_weekly_send_last_week, summary_func=lambda _result: "上周流量周报")
        return 0
    if cmd == "report_monthly":
        run_with_task_record("report", "cli:report_monthly", run_monthly_send_last_month, summary_func=lambda _result: "上月流量月报")
        return 0
    if cmd == "listen":
        listen_commands()
        return 0
    if cmd in ("check_alerts", "check-alerts"):
        dry_run = False
        for arg in sys.argv[2:]:
            if arg.strip().lower() == "--dry-run":
                dry_run = True
                continue
            raise RuntimeError(f"Unknown check_alerts arg: {arg}")
        validate_config_or_raise()
        result = run_alert_check(dry_run=dry_run, notify=not dry_run, force_sample=True)
        print(format_alert_check_result(result))
        return 0
    if cmd == "config-validate":
        validate_config_or_raise()
        print("OK")
        return 0
    if cmd == "health":
        validate_config_or_raise()
        run_healthcheck_or_raise()
        print("OK")
        return 0

    raise RuntimeError("Unknown command")


if __name__ == "__main__":
    setup_logging()
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    full_cmd = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "(none)"
    try:
        sys.exit(main())
    except Exception as e:
        key = f"cmd_{(sys.argv[1] if len(sys.argv) > 1 else 'none')}"
        if should_alert(key, 300):
            alert_exception("main", full_cmd, e)
        raise
