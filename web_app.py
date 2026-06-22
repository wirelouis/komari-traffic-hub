#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import base64
import concurrent.futures
import csv
import hashlib
import hmac
import io
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import komari_traffic_report as k


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
SESSION_COOKIE = "komari_traffic_session"
LEGACY_SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600
SESSION_BROWSER_SECONDS = 12 * 3600
SESSION_REMEMBER_SECONDS = 30 * 24 * 3600
LOGIN_RATE_LIMIT_ATTEMPTS = 5
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 5 * 60
LOGIN_RATE_LIMIT_LOCK_SECONDS = 10 * 60
LOGIN_RATE_LIMIT_MAX_KEYS = 1000
UNSAFE_HTTP_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
OVERVIEW_NODE_LIMIT = 8
ANALYTICS_NODE_LIMIT = 10
WEB_HEALTH_FETCH_LIMIT = max(0, int(os.environ.get("WEB_HEALTH_FETCH_LIMIT", "64")))
WEB_TRUST_PROXY = bool(int(os.environ.get("WEB_TRUST_PROXY", "0")))
WEB_SESSION_SECRET = os.environ.get("WEB_SESSION_SECRET", "").strip() or secrets.token_urlsafe(32)
WEB_SESSION_SECRET_TEMPORARY = not bool(os.environ.get("WEB_SESSION_SECRET", "").strip())
LOGIN_FAILURES: dict[str, dict[str, float | int]] = {}

app = FastAPI(title="Komari Traffic Web", docs_url=None, redoc_url=None)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""
    remember: bool = False


class AlertCheckRequest(BaseModel):
    notify: bool = False


class AlertMuteRequest(BaseModel):
    hours: int


class TelegramReportRequest(BaseModel):
    scope: str
    mode: str = "full"


class AiAskRequest(BaseModel):
    question: str


class NodeBindingRequest(BaseModel):
    source_id: str
    komari_uuid: str = ""


class ScheduleRequest(BaseModel):
    enabled: bool = True
    scope: str = "daily"
    mode: str = "full"
    time: str = "09:00"
    weekday: int = 0
    month_day: int = 1
    chat: str = ""


class MaintenancePruneRequest(BaseModel):
    retention_days: int | None = None


class RuntimeConfigRequest(BaseModel):
    bot_instance_name: str | None = None
    komari_base_url: str | None = None
    telegram_chat_id: str | None = None
    telegram_alert_chat_id: str | None = None
    ai_api_base: str | None = None
    ai_model: str | None = None
    top_n: int | None = None
    komari_timeout_seconds: int | None = None
    komari_fetch_workers: int | None = None
    sample_interval_seconds: int | None = None
    sample_retention_hours: int | None = None
    traffic_snapshot_retention_days: int | None = None
    ai_pack_cache_ttl_seconds: int | None = None
    task_run_retention_days: int | None = None
    alerts_enabled: bool | None = None
    alert_recovery_notify: bool | None = None
    alert_cooldown_seconds: int | None = None
    alert_window_minutes: int | None = None
    alert_node_missing_samples: int | None = None
    alert_silence_windows: str | None = None
    alert_total_window_bytes: str | int | None = None
    alert_node_window_bytes: str | int | None = None
    alert_daily_total_bytes: str | int | None = None
    alert_daily_node_bytes: str | int | None = None


def api_ok(data: Any = None, **extra):
    payload = {"ok": True, "data": data}
    payload.update(extra)
    return JSONResponse(payload)


def redact_web_sensitive_text(value: str) -> str:
    text = k.redact_sensitive_text(value)
    for secret in (web_password(), WEB_SESSION_SECRET):
        secret = str(secret or "").strip()
        if not secret:
            continue
        masked = "***" if len(secret) <= 6 else f"{secret[:3]}***{secret[-3:]}"
        text = text.replace(secret, masked)
    return text


def redact_web_sensitive_data(value):
    if isinstance(value, dict):
        return {str(key): redact_web_sensitive_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_web_sensitive_data(item) for item in value]
    if isinstance(value, str):
        return redact_web_sensitive_text(value)
    return value


def api_error(message: str, status_code: int = 400, code: str = "error", **extra):
    payload = {"ok": False, "error": {"code": code, "message": redact_web_sensitive_text(str(message))}}
    payload.update(redact_web_sensitive_data(extra))
    return JSONResponse(payload, status_code=status_code)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    if not unsafe_request_has_same_origin(request):
        return add_security_headers(api_error("cross-site requests are not allowed", status_code=403, code="csrf_blocked"))
    response = await call_next(request)
    return add_security_headers(response)


@app.exception_handler(HTTPException)
async def http_error_handler(_request: Request, exc: HTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "request failed"
    code = "unauthorized" if exc.status_code == 401 else "http_error"
    return api_error(message, status_code=exc.status_code, code=code)


@app.exception_handler(Exception)
async def error_handler(_request: Request, exc: Exception):
    return api_error(str(exc), status_code=500, code=type(exc).__name__)


def add_security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    return response


def forwarded_header_value(request: Request, name: str) -> str:
    return request.headers.get(name, "").split(",")[0].strip()


def request_public_origin(request: Request) -> str:
    if WEB_TRUST_PROXY:
        scheme = forwarded_header_value(request, "x-forwarded-proto").lower() or request.url.scheme
        host = forwarded_header_value(request, "x-forwarded-host") or request.headers.get("host", "") or request.url.netloc
    else:
        scheme = request.url.scheme
        host = request.headers.get("host", "") or request.url.netloc
    return f"{scheme}://{host}".lower()


def same_origin(request: Request, value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}" == request_public_origin(request)


def unsafe_request_has_same_origin(request: Request) -> bool:
    if request.method.upper() not in UNSAFE_HTTP_METHODS:
        return True
    origin = request.headers.get("origin", "").strip()
    if origin:
        return same_origin(request, origin)
    referer = request.headers.get("referer", "").strip()
    if referer:
        return same_origin(request, referer)
    return True


def web_username() -> str:
    return os.environ.get("WEB_USERNAME", "admin").strip() or "admin"


def web_password() -> str:
    return os.environ.get("WEB_PASSWORD", "").strip()


def web_password_configured() -> bool:
    return bool(web_password())


def request_is_https(request: Request) -> bool:
    if WEB_TRUST_PROXY:
        forwarded_proto = forwarded_header_value(request, "x-forwarded-proto").lower()
        return request.url.scheme == "https" or forwarded_proto == "https"
    return request.url.scheme == "https"


def login_rate_key(request: Request) -> str:
    host = request.client.host if request.client else ""
    if WEB_TRUST_PROXY:
        forwarded_for = forwarded_header_value(request, "x-forwarded-for")
        return forwarded_for or host or "unknown"
    return host or "unknown"


def prune_login_failures(now_ts: float | None = None):
    now_ts = time.time() if now_ts is None else now_ts
    for key, state in list(LOGIN_FAILURES.items()):
        first = float(state.get("first", now_ts))
        locked_until = float(state.get("locked_until", 0))
        if locked_until and locked_until <= now_ts:
            LOGIN_FAILURES.pop(key, None)
        elif not locked_until and now_ts - first > LOGIN_RATE_LIMIT_WINDOW_SECONDS:
            LOGIN_FAILURES.pop(key, None)
    overflow = len(LOGIN_FAILURES) - LOGIN_RATE_LIMIT_MAX_KEYS
    if overflow > 0:
        oldest = sorted(
            LOGIN_FAILURES,
            key=lambda item: float(LOGIN_FAILURES[item].get("first", now_ts)),
        )[:overflow]
        for key in oldest:
            LOGIN_FAILURES.pop(key, None)


def login_limited(key: str, now_ts: float | None = None) -> bool:
    now_ts = time.time() if now_ts is None else now_ts
    prune_login_failures(now_ts)
    state = LOGIN_FAILURES.get(key)
    if not state:
        return False
    locked_until = float(state.get("locked_until", 0))
    if locked_until > now_ts:
        return True
    if locked_until:
        LOGIN_FAILURES.pop(key, None)
    return False


def record_login_failure(key: str, now_ts: float | None = None):
    now_ts = time.time() if now_ts is None else now_ts
    prune_login_failures(now_ts)
    state = LOGIN_FAILURES.get(key) or {"count": 0, "first": now_ts, "locked_until": 0}
    if now_ts - float(state.get("first", now_ts)) > LOGIN_RATE_LIMIT_WINDOW_SECONDS:
        state = {"count": 0, "first": now_ts, "locked_until": 0}
    state["count"] = int(state.get("count", 0)) + 1
    if int(state["count"]) >= LOGIN_RATE_LIMIT_ATTEMPTS:
        state["locked_until"] = now_ts + LOGIN_RATE_LIMIT_LOCK_SECONDS
    LOGIN_FAILURES[key] = state
    prune_login_failures(now_ts)


def clear_login_failures(key: str):
    LOGIN_FAILURES.pop(key, None)


def _sign_session(body: str) -> str:
    password_hash = hashlib.sha256(web_password().encode("utf-8")).hexdigest()
    key = f"{WEB_SESSION_SECRET}|{password_hash}".encode("utf-8")
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(username: str, max_age_seconds: int = SESSION_BROWSER_SECONDS) -> str:
    issued = int(time.time())
    expires = issued + max(1, int(max_age_seconds))
    nonce = secrets.token_urlsafe(12)
    body = f"{username}|{issued}|{expires}|{nonce}"
    token = f"{body}|{_sign_session(body)}"
    return base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii")


def validate_session_token(token: str | None) -> str | None:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        parts = raw.split("|")
        if len(parts) == 5:
            username, issued_text, expires_text, nonce, signature = parts
            body = f"{username}|{issued_text}|{expires_text}|{nonce}"
            if not hmac.compare_digest(signature, _sign_session(body)):
                return None
            issued = int(issued_text)
            expires = int(expires_text)
            now_ts = int(time.time())
            if issued <= 0 or expires <= issued or now_ts >= expires:
                return None
        elif len(parts) == 4:
            username, issued_text, nonce, signature = parts
            body = f"{username}|{issued_text}|{nonce}"
            if not hmac.compare_digest(signature, _sign_session(body)):
                return None
            issued = int(issued_text)
            if issued <= 0 or int(time.time()) - issued > LEGACY_SESSION_MAX_AGE_SECONDS:
                return None
        else:
            return None
        if username != web_username():
            return None
        return username
    except Exception:
        return None


def current_user(request: Request) -> str:
    username = validate_session_token(request.cookies.get(SESSION_COOKIE))
    if not username:
        raise HTTPException(status_code=401, detail="unauthorized")
    return username


def mask_value(value: str, visible: int = 3) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= visible * 2:
        return "*" * len(text)
    return f"{text[:visible]}***{text[-visible:]}"


def safe_call(func, *args, **kwargs) -> dict:
    try:
        return {"ok": True, "data": func(*args, **kwargs)}
    except Exception as exc:
        return {"ok": False, "error": {"code": type(exc).__name__, "message": redact_web_sensitive_text(str(exc))}}


def node_bindings_path() -> Path:
    return Path(k.DATA_DIR) / "node_bindings.json"


def load_node_bindings() -> dict:
    data = k.load_json(str(node_bindings_path()), {"version": 1, "bindings": {}})
    bindings = data.get("bindings", {}) if isinstance(data, dict) else {}
    if not isinstance(bindings, dict):
        bindings = {}
    return {"version": 1, "bindings": bindings}


def save_node_bindings(data: dict):
    k.ensure_dirs()
    payload = {"version": 1, "bindings": data.get("bindings", {}) if isinstance(data, dict) else {}}
    k.save_json_atomic(str(node_bindings_path()), payload)


def komari_instance_url(uuid: str) -> str:
    base = k.KOMARI_BASE_URL.rstrip("/")
    if not base or not uuid:
        return ""
    return f"{base}/instance/{quote(str(uuid), safe='')}"


EMPTY_METRIC_STATS = {"avg": None, "max": None, "min": None}


def nested_dict_values(node: dict, keys: tuple[str, ...]) -> list[dict]:
    values = []
    for key in keys:
        value = node.get(key)
        if isinstance(value, dict):
            values.append(value)
    return values


def first_present_value(sources: list[dict], keys: tuple[str, ...]):
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            if key in source and source.get(key) not in (None, ""):
                return source.get(key)
    return None


def machine_metric_stats(node: dict, metric: str) -> dict:
    nested_sources = nested_dict_values(node, ("status", "state", "metrics", "latest", "system"))
    sources = [node, *nested_sources]
    aliases = {
        "cpu": ("cpu", "CPU", "cpu_percent", "cpu_usage", "cpu_usage_percent"),
        "ram": ("ram", "RAM", "mem", "memory", "ram_percent", "mem_percent", "memory_percent"),
        "disk": ("disk", "Disk", "hdd", "storage", "disk_percent", "hdd_percent", "storage_percent"),
    }
    total_aliases = {
        "cpu": ("cpu_total", "cpuTotal"),
        "ram": ("ram_total", "ramTotal", "mem_total", "memTotal", "memory_total", "memoryTotal"),
        "disk": ("disk_total", "diskTotal", "hdd_total", "hddTotal", "storage_total", "storageTotal"),
    }
    value = first_present_value(sources, aliases.get(metric, (metric,)))
    total = first_present_value(sources, total_aliases.get(metric, (f"{metric}_total",)))
    percent = k.normalize_percent_metric(value, total)
    if percent is None:
        return dict(EMPTY_METRIC_STATS)
    return {"avg": percent, "max": percent, "min": percent}


def metric_stats_has_value(stats: dict | None) -> bool:
    if not isinstance(stats, dict):
        return False
    return any(stats.get(key) is not None for key in ("avg", "max", "min"))


def health_stats_complete(item: dict | None) -> bool:
    if not isinstance(item, dict):
        return False
    return all(metric_stats_has_value(item.get(metric)) for metric in ("cpu", "ram", "disk"))


def attach_machine_health(item: dict, machine: dict | None):
    if not machine:
        return
    for metric in ("cpu", "ram", "disk"):
        if not metric_stats_has_value(item.get(metric)) and metric_stats_has_value(machine.get(metric)):
            item[metric] = dict(machine[metric])


def health_target_uuid(item: dict) -> str:
    binding_uuid = str(((item.get("binding") or {}).get("komari_uuid")) or "")
    if binding_uuid:
        return binding_uuid
    return str(item.get("uuid") or "")


def records_health_stats(uuid: str, hours: int) -> dict | None:
    try:
        records = k.fetch_node_records(uuid, hours)
        summary = k.compute_traffic_from_records(records)
    except Exception:
        return None
    health = {
        metric: summary.get(metric, dict(EMPTY_METRIC_STATS))
        for metric in ("cpu", "ram", "disk")
    }
    return health if any(metric_stats_has_value(health.get(metric)) for metric in health) else None


def records_health_map(items: list[dict], hours: int) -> dict[str, dict]:
    if not k.KOMARI_BASE_URL or WEB_HEALTH_FETCH_LIMIT <= 0:
        return {}
    targets = []
    seen = set()
    for item in items or []:
        if health_stats_complete(item):
            continue
        uuid = health_target_uuid(item)
        if not uuid or uuid in seen:
            continue
        seen.add(uuid)
        targets.append(uuid)
        if len(targets) >= WEB_HEALTH_FETCH_LIMIT:
            break
    if not targets:
        return {}
    max_workers = max(1, min(len(targets), int(getattr(k, "KOMARI_FETCH_WORKERS", 4) or 4), 8))
    health: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(records_health_stats, uuid, hours): uuid for uuid in targets}
        for future in concurrent.futures.as_completed(future_map):
            uuid = future_map[future]
            try:
                stats = future.result()
            except Exception:
                stats = None
            if stats:
                health[uuid] = stats
    return health


def attach_records_health(item: dict, health_map: dict[str, dict]):
    stats = health_map.get(health_target_uuid(item))
    if not stats:
        return
    for metric in ("cpu", "ram", "disk"):
        if not metric_stats_has_value(item.get(metric)) and metric_stats_has_value(stats.get(metric)):
            item[metric] = dict(stats[metric])
    machine = (item.get("komari") or {}).get("machine")
    if isinstance(machine, dict):
        for metric in ("cpu", "ram", "disk"):
            if not metric_stats_has_value(machine.get(metric)) and metric_stats_has_value(stats.get(metric)):
                machine[metric] = dict(stats[metric])


def normalize_machine(node: dict) -> dict:
    uuid = str(node.get("uuid") or "")
    name = str(node.get("name") or uuid or "unknown")
    machine = {
        "uuid": uuid,
        "name": name,
        "region": node.get("region") or "",
        "group": node.get("group") or "",
        "os": node.get("os") or "",
        "arch": node.get("arch") or "",
        "version": node.get("version") or "",
        "tags": node.get("tags") if isinstance(node.get("tags"), list) else [],
        "web_url": komari_instance_url(uuid),
        "cpu": machine_metric_stats(node, "cpu"),
        "ram": machine_metric_stats(node, "ram"),
        "disk": machine_metric_stats(node, "disk"),
    }
    return machine


def fetch_komari_machines() -> list[dict]:
    if not k.KOMARI_BASE_URL:
        raise RuntimeError("KOMARI_BASE_URL 未设置")
    nodes_resp = k.get_json(f"{k.KOMARI_BASE_URL}/api/nodes")
    if not (isinstance(nodes_resp, dict) and nodes_resp.get("status") == "success"):
        raise RuntimeError(f"/api/nodes 返回异常：{nodes_resp}")
    nodes = nodes_resp.get("data", [])
    if not isinstance(nodes, list):
        raise RuntimeError(f"/api/nodes data 非列表：{nodes_resp}")
    machines = [normalize_machine(node) for node in nodes if isinstance(node, dict) and node.get("uuid")]
    machines.sort(key=lambda item: (str(item.get("name", "")).lower(), str(item.get("uuid", ""))))
    return machines


def machine_context() -> tuple[list[dict], dict[str, dict]]:
    machines = fetch_komari_machines()
    return machines, {str(machine.get("uuid")): machine for machine in machines}


def resolve_node_binding(source_id: str, machine_index: dict[str, dict], bindings: dict) -> dict:
    source_id = str(source_id or "")
    manual = bindings.get(source_id) if isinstance(bindings, dict) else None
    manual_uuid = str((manual or {}).get("komari_uuid") or "")
    if manual_uuid:
        machine = machine_index.get(manual_uuid)
        return {
            "source_id": source_id,
            "mode": "manual",
            "komari_uuid": manual_uuid,
            "stale": machine is None,
            "updated_at": int((manual or {}).get("updated_at") or 0),
        }
    machine = machine_index.get(source_id)
    return {
        "source_id": source_id,
        "mode": "auto" if machine else "missing",
        "komari_uuid": source_id if machine else "",
        "stale": machine is None,
        "updated_at": 0,
    }


def node_binding_payload(source_id: str, bindings: dict, machines: list[dict], machine_error: str = "") -> dict:
    machine_index = {str(machine.get("uuid")): machine for machine in machines}
    binding = resolve_node_binding(source_id, machine_index, bindings)
    machine = machine_index.get(binding.get("komari_uuid", ""))
    return {
        "source_id": source_id,
        "configured": bool(k.KOMARI_BASE_URL),
        "base_url": k.KOMARI_BASE_URL,
        "binding": binding,
        "machine": machine,
        "web_url": machine.get("web_url", "") if machine else "",
        "machines": machines,
        "machine_error": machine_error,
    }


def enrich_nodes_with_komari(nodes: list[dict], machines: list[dict] | None = None, bindings_data: dict | None = None) -> tuple[list[dict], list[dict]]:
    if machines is None:
        try:
            machines, machine_index = machine_context()
        except Exception:
            machines, machine_index = [], {}
    else:
        machine_index = {str(machine.get("uuid")): machine for machine in machines}
    bindings = (bindings_data or load_node_bindings()).get("bindings", {})
    enriched = []
    for node in nodes or []:
        item = dict(node)
        source_id = str(item.get("uuid") or item.get("name") or "")
        binding = resolve_node_binding(source_id, machine_index, bindings)
        machine = machine_index.get(binding.get("komari_uuid", ""))
        item["binding"] = binding
        item["komari"] = {
            "machine": machine,
            "web_url": machine.get("web_url", "") if machine else "",
        }
        attach_machine_health(item, machine)
        enriched.append(item)
    return enriched, machines


def enrich_records_summary(summary: dict) -> dict:
    payload = dict(summary or {})
    try:
        machines, _machine_index = machine_context()
    except Exception:
        machines = []
    bindings_data = load_node_bindings()
    nodes, machines = enrich_nodes_with_komari(payload.get("nodes", []), machines, bindings_data)
    top_nodes, _machines = enrich_nodes_with_komari(payload.get("top_nodes", []), machines, bindings_data)
    hours = int(payload.get("hours") or 24)
    health_map = records_health_map([*nodes, *top_nodes], hours) if machines else {}
    for item in [*nodes, *top_nodes]:
        attach_records_health(item, health_map)
    payload["nodes"] = nodes
    payload["top_nodes"] = top_nodes
    payload["machines"] = machines
    return payload


def enrich_period_result(result: dict) -> dict:
    if not isinstance(result, dict) or not result.get("ok"):
        return result
    data = result.get("data")
    if not isinstance(data, dict):
        return result
    result = dict(result)
    result["data"] = enrich_records_summary(data)
    return result


def compact_other_node(rows: list[dict]) -> dict:
    up = sum(int(item.get("up", 0) or 0) for item in rows)
    down = sum(int(item.get("down", 0) or 0) for item in rows)
    total = up + down
    return {
        "uuid": "__other__",
        "name": f"{len(rows)} 个其他节点",
        "up": up,
        "down": down,
        "total": total,
        "up_human": k.human_bytes(up),
        "down_human": k.human_bytes(down),
        "total_human": k.human_bytes(total),
        "compact_other": True,
    }


def compact_node_rows(nodes: list[dict], limit: int) -> tuple[list[dict], int]:
    rows = list(nodes or [])
    max_rows = max(1, int(limit or 1))
    if len(rows) <= max_rows:
        return rows, 0
    visible = rows[:max_rows]
    hidden = rows[max_rows:]
    return [*visible, compact_other_node(hidden)], len(hidden)


def compact_summary_nodes(summary: dict, limit: int) -> dict:
    payload = dict(summary or {})
    nodes = payload.get("nodes", [])
    if not isinstance(nodes, list):
        return payload
    compacted, hidden_count = compact_node_rows(nodes, limit)
    payload["nodes"] = compacted
    payload["node_count"] = len(nodes)
    payload["hidden_node_count"] = hidden_count
    payload["compact"] = bool(hidden_count)
    return payload


def compact_result_nodes(result: dict, limit: int) -> dict:
    if not isinstance(result, dict) or not result.get("ok") or not isinstance(result.get("data"), dict):
        return result
    compacted = dict(result)
    compacted["data"] = compact_summary_nodes(result["data"], limit)
    return compacted


def compact_nodes_payload(data: dict) -> dict:
    payload = dict(data or {})
    heavy_keys = {"hourly_distribution"}
    for key in heavy_keys:
        payload.pop(key, None)
    for list_key in ("nodes", "top_nodes"):
        rows = payload.get(list_key)
        if isinstance(rows, list):
            payload[list_key] = [
                {k: v for k, v in dict(row).items() if k not in heavy_keys}
                if isinstance(row, dict) else row
                for row in rows
            ]
    payload["compact"] = True
    return payload


def compact_traffic_range_payload(data: dict, node_limit: int = ANALYTICS_NODE_LIMIT) -> dict:
    payload = compact_summary_nodes(data, node_limit)
    groups = []
    for group in payload.get("groups", []) or []:
        if not isinstance(group, dict):
            continue
        groups.append({
            "key": group.get("key", ""),
            "label": group.get("label", group.get("key", "")),
            "total": group.get("total", {}),
        })
    payload["groups"] = groups
    payload["group_count"] = len(groups)
    payload["compact"] = True
    return payload


def traffic_range_csv_response(data: dict) -> Response:
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([
        "uuid",
        "name",
        "down_bytes",
        "up_bytes",
        "total_bytes",
        "down",
        "up",
        "total",
        "share_percent",
    ])
    total = max(1, int((data.get("total") or {}).get("total") or 0))
    for node in data.get("nodes", []) or []:
        node_total = int(node.get("total", 0) or 0)
        writer.writerow([
            node.get("uuid", ""),
            node.get("name", ""),
            int(node.get("down", 0) or 0),
            int(node.get("up", 0) or 0),
            node_total,
            node.get("down_human", ""),
            node.get("up_human", ""),
            node.get("total_human", ""),
            f"{(node_total / total) * 100:.2f}",
        ])
    filename = f"komari-traffic-{data.get('from', 'from')}-{data.get('to', 'to')}.csv"
    quoted = quote(filename)
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quoted}"},
    )


def seconds_text(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds >= 3600:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def timestamp_text(ts: int | float | str | None) -> str:
    try:
        value = int(float(ts or 0))
    except Exception:
        value = 0
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value, k.TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def task_run_response(run: dict | None) -> dict | None:
    if not run:
        return None
    started_at = int(run.get("started_at") or 0)
    finished_at = int(run.get("finished_at") or 0)
    duration_ms = int(run.get("duration_ms") or 0)
    return {
        "id": run.get("id"),
        "type": run.get("type", ""),
        "source": run.get("source", ""),
        "status": run.get("status", ""),
        "summary": run.get("summary", ""),
        "error": run.get("error", ""),
        "started_at": started_at,
        "started_at_text": timestamp_text(started_at),
        "finished_at": finished_at,
        "finished_at_text": timestamp_text(finished_at),
        "duration_ms": duration_ms,
        "duration_text": f"{duration_ms}ms" if duration_ms < 1000 else f"{duration_ms / 1000:.1f}s",
        "metadata": run.get("metadata", {}) if isinstance(run.get("metadata", {}), dict) else {},
    }


def build_info() -> dict:
    commit = k.GIT_COMMIT or ""
    return {
        "version": k.APP_VERSION,
        "commit": commit,
        "commit_short": commit[:12] if commit else "",
        "build_date": k.BUILD_DATE,
        "image_source": k.IMAGE_SOURCE,
    }


def file_status(path: str | Path, label: str) -> dict:
    p = Path(path)
    try:
        stat = p.stat()
        return {
            "label": label,
            "path": str(p),
            "exists": True,
            "size": stat.st_size,
            "size_human": k.human_bytes(stat.st_size),
            "mtime": int(stat.st_mtime),
            "mtime_text": timestamp_text(stat.st_mtime),
        }
    except FileNotFoundError:
        return {"label": label, "path": str(p), "exists": False, "size": 0, "size_human": "0 B", "mtime": 0, "mtime_text": ""}
    except Exception as exc:
        return {"label": label, "path": str(p), "exists": False, "size": 0, "size_human": "0 B", "mtime": 0, "mtime_text": "", "error": str(exc)}


def build_telegram_status_struct() -> dict:
    app_schedules = k.load_report_schedules().get("schedules", [])
    return {
        "configured": bool(k.TELEGRAM_BOT_TOKEN and k.TELEGRAM_CHAT_ID),
        "bot_token_configured": bool(k.TELEGRAM_BOT_TOKEN),
        "chat": mask_value(k.TELEGRAM_CHAT_ID),
        "alert_chat": mask_value(k.telegram_alert_chat_id()),
        "schedules": app_schedules,
        "scheduler": {"type": "app", "path": k.REPORT_SCHEDULES_PATH},
    }


def schedule_response(schedule: dict) -> dict:
    item = k.normalize_report_schedule(schedule)
    raw_chat = str(item.get("chat") or "").strip()
    item["label"] = k.schedule_label(item)
    item["chat"] = ""
    item["uses_default_chat"] = not bool(raw_chat)
    item["chat_masked"] = mask_value(raw_chat or k.TELEGRAM_CHAT_ID)
    next_run = k.schedule_next_run_at(item)
    item["next_run"] = next_run or 0
    item["next_run_text"] = timestamp_text(next_run)
    last_run = k.latest_task_run("report", metadata_key="schedule_id", metadata_value=item.get("id"))
    item["last_run"] = task_run_response(last_run)
    item["last_status"] = (last_run or {}).get("status", "")
    item["last_summary"] = (last_run or {}).get("summary", "")
    return item


def schedule_payload(req: ScheduleRequest, schedule_id: str | None = None) -> dict:
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    if schedule_id:
        payload["id"] = schedule_id
    return k.validate_report_schedule(payload)


def build_schedules_struct() -> dict:
    data = k.load_report_schedules()
    return {
        "schedules": [schedule_response(item) for item in data.get("schedules", [])],
        "last_runs": data.get("last_runs", {}),
        "path": k.REPORT_SCHEDULES_PATH,
    }


def build_traffic_db_status() -> dict:
    result = {
        "path": k.TRAFFIC_DB_PATH,
        "exists": Path(k.TRAFFIC_DB_PATH).exists(),
        "size_human": file_status(k.TRAFFIC_DB_PATH, "traffic.db").get("size_human", "0 B"),
        "ok": True,
        "daily_rows": 0,
        "task_runs": 0,
        "table_counts": {},
        "error": "",
    }
    try:
        result.update(k.traffic_db_healthcheck())
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
    result["exists"] = Path(k.TRAFFIC_DB_PATH).exists()
    result["size_human"] = file_status(k.TRAFFIC_DB_PATH, "traffic.db").get("size_human", "0 B")
    return result


def build_maintenance_status() -> dict:
    try:
        status = k.traffic_db_maintenance_status()
        status["ok"] = True
        return status
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "retention_days": k.TASK_RUN_RETENTION_DAYS,
            "retention_enabled": k.TASK_RUN_RETENTION_DAYS > 0,
            "old_task_runs": 0,
            "task_runs": 0,
            "table_counts": {},
            "db_size": 0,
            "db_size_human": "0 B",
        }


def build_system_status_struct(include_recent: bool = True) -> dict:
    schedules = k.load_report_schedules().get("schedules", [])
    alert_status = build_alert_status_struct()
    ai_status = build_ai_status_struct()
    db_status = build_traffic_db_status()
    recent_runs = [task_run_response(run) for run in k.list_task_runs(limit=20)] if include_recent else []
    latest = {
        "report": task_run_response(k.latest_task_run("report")),
        "alert": task_run_response(k.latest_task_run("alert")),
        "ai": task_run_response(k.latest_task_run("ai")),
        "sample": task_run_response(k.latest_task_run("sample")),
    }
    maintenance = build_maintenance_status()
    recent_failures = len([run for run in recent_runs if run and run.get("status") != "success"])
    latest_sample_ts = int(db_status.get("latest_sample_ts", 0) or 0)
    sample_lag = db_status.get("sample_lag_seconds")
    sample_stale = bool(db_status.get("sample_stale"))
    service_items = [
        {
            "key": "komari",
            "label": "探针连接",
            "level": "ok" if k.KOMARI_BASE_URL else "bad",
            "ok": bool(k.KOMARI_BASE_URL),
            "detail": "已填写 Komari 地址。" if k.KOMARI_BASE_URL else "还没有填写 Komari 地址。",
            "message": "节点数据可以读取。" if k.KOMARI_BASE_URL else "面板暂时无法读取探针数据。",
            "fix": "" if k.KOMARI_BASE_URL else "在 .env 中设置 KOMARI_BASE_URL，然后重启容器。",
        },
        {
            "key": "telegram",
            "label": "Telegram 推送",
            "level": "ok" if k.TELEGRAM_BOT_TOKEN and k.TELEGRAM_CHAT_ID else "bad",
            "ok": bool(k.TELEGRAM_BOT_TOKEN and k.TELEGRAM_CHAT_ID),
            "detail": mask_value(k.TELEGRAM_CHAT_ID) or "未配置 Chat",
            "message": "报表和告警可以推送。" if k.TELEGRAM_BOT_TOKEN and k.TELEGRAM_CHAT_ID else "推送目标还没有配置完整。",
            "fix": "" if k.TELEGRAM_BOT_TOKEN and k.TELEGRAM_CHAT_ID else "检查 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID。",
        },
        {
            "key": "ai",
            "label": "AI 问答",
            "level": "ok" if ai_status.get("configured") else "muted",
            "ok": bool(ai_status.get("configured")),
            "detail": ai_status.get("model") or "未配置模型",
            "message": "AI 问答可用。" if ai_status.get("configured") else "AI 未启用，不影响流量统计和推送。",
            "fix": "" if ai_status.get("configured") else "需要 AI 问答时再配置 AI_API_BASE / AI_API_KEY / AI_MODEL。",
        },
        {
            "key": "alerts",
            "label": "告警",
            "level": "ok" if alert_status.get("enabled") else "muted",
            "ok": bool(alert_status.get("enabled")),
            "detail": f"当前 {alert_status.get('active_count', 0)} 个未恢复事件",
            "message": "告警正在工作。" if alert_status.get("enabled") else "告警已关闭，不会产生新的提醒。",
            "fix": "" if alert_status.get("enabled") else "可在告警页或系统页重新启用。",
        },
        {
            "key": "web",
            "label": "Web 登录",
            "level": "warn" if WEB_SESSION_SECRET_TEMPORARY else ("ok" if web_password_configured() else "bad"),
            "ok": web_password_configured() and not WEB_SESSION_SECRET_TEMPORARY,
            "detail": "会话密钥未固定" if WEB_SESSION_SECRET_TEMPORARY else "登录保护已配置",
            "message": "容器重启后需要重新登录。" if WEB_SESSION_SECRET_TEMPORARY else "登录状态稳定。",
            "fix": "公网部署建议设置 WEB_SESSION_SECRET。" if WEB_SESSION_SECRET_TEMPORARY else "",
        },
        {
            "key": "sqlite",
            "label": "长期统计",
            "level": "ok" if db_status.get("ok") else "bad",
            "ok": bool(db_status.get("ok")),
            "detail": "统计库可用" if db_status.get("ok") else "统计库异常",
            "message": "每日/每周/每月统计可以继续累积。" if db_status.get("ok") else "长期统计可能无法保存。",
            "fix": "" if db_status.get("ok") else "检查 data 目录权限和容器日志。",
        },
    ]
    data_status = [
        {
            "key": "db",
            "label": "长期统计",
            "level": "ok" if db_status.get("ok") else "bad",
            "message": "长期统计库正常。" if db_status.get("ok") else "长期统计库无法读取或写入。",
            "detail": "历史流量会继续自动保存。" if db_status.get("ok") else "无法保存历史流量。",
            "fix": "" if db_status.get("ok") else db_status.get("error") or "检查 data 目录权限。",
        },
        {
            "key": "sampling",
            "label": "实时采样",
            "level": "warn" if (not latest_sample_ts or sample_stale) else "ok",
            "message": "采样链路正常。" if latest_sample_ts and not sample_stale else ("采样已滞后。" if latest_sample_ts else "还没有采样快照。"),
            "detail": (
                f"最近采样：{db_status.get('latest_sample_at') or '暂无'}；"
                f"滞后：{sample_lag if sample_lag is not None else '-'} 秒。"
            ),
            "fix": "" if latest_sample_ts and not sample_stale else "确认 bot/listen 服务正在运行。",
        },
        {
            "key": "runs",
            "label": "任务记录",
            "level": "bad" if recent_failures else "ok",
            "message": "最近任务没有失败。" if not recent_failures else f"最近有 {recent_failures} 次任务失败。",
            "detail": "只保留最近需要排查的记录。",
            "fix": "" if not recent_failures else "查看下方最近记录里的红色失败原因。",
        },
        {
            "key": "maintenance",
            "label": "数据维护",
            "level": "bad" if not maintenance.get("ok", True) else ("warn" if maintenance.get("old_task_runs") else "ok"),
            "message": "数据维护状态正常。" if maintenance.get("ok", True) and not maintenance.get("old_task_runs") else ("有旧运行记录可以清理。" if maintenance.get("ok", True) else "数据维护检查失败。"),
            "detail": f"保留策略：{maintenance.get('retention_days', k.TASK_RUN_RETENTION_DAYS)} 天。",
            "fix": "" if maintenance.get("ok", True) else maintenance.get("error", "检查容器日志。"),
        },
    ]
    healthy_count = sum(1 for item in service_items if item.get("level") == "ok")
    warnings = [item["label"] for item in service_items + data_status if item.get("level") == "warn"]
    issues = [item["label"] for item in service_items + data_status if item.get("level") == "bad"]
    return {
        "now": k.now_dt().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "stat_tz": k.STAT_TZ,
        "instance": k.BOT_INSTANCE_NAME or "default",
        "build": build_info(),
        "summary": {
            "healthy": healthy_count,
            "total": len(service_items),
            "issues": issues,
            "warnings": warnings,
            "recent_failures": recent_failures,
        },
        "services": service_items,
        "health_items": service_items,
        "data_status": data_status,
        "config": {
            "komari_base_url": k.KOMARI_BASE_URL,
            "komari_api_token_configured": bool(k.KOMARI_API_TOKEN),
            "telegram_chat": mask_value(k.TELEGRAM_CHAT_ID),
            "telegram_alert_chat": mask_value(k.telegram_alert_chat_id()),
            "ai_base_url": k.AI_API_BASE,
            "ai_model": k.AI_MODEL,
            "web_username": web_username(),
            "web_password_configured": web_password_configured(),
        },
        "data": {
            "data_dir": str(k.DATA_DIR),
            "files": [
                file_status(k.HISTORY_PATH, "history.json"),
                file_status(k.SAMPLES_PATH, "samples.json"),
                file_status(k.REPORT_SCHEDULES_PATH, "report_schedules.json"),
                file_status(node_bindings_path(), "node_bindings.json"),
                file_status(k.AI_PACK_CACHE_PATH, "ai_pack_cache.json"),
                file_status(k.ALERTS_STATE_PATH, "alerts_state.json"),
            ],
            "sqlite": db_status,
            "maintenance": maintenance,
        },
        "runtime": {
            "process": "web",
            "sample_thread_alive": bool(k.SAMPLE_THREAD and k.SAMPLE_THREAD.is_alive()),
            "scheduler_thread_alive": bool(k.SCHEDULER_THREAD and k.SCHEDULER_THREAD.is_alive()),
            "sample_interval_seconds": k.SAMPLE_INTERVAL_SECONDS,
            "latest_sample_ts": latest_sample_ts,
            "latest_sample_at": db_status.get("latest_sample_at", ""),
            "sample_lag_seconds": sample_lag,
            "sample_stale": sample_stale,
            "scheduler_note": "Docker 部署中通常由 bot/listen 服务执行采样和应用内计划，Web 服务负责展示状态。",
            "schedules": {
                "total": len(schedules),
                "enabled": len([item for item in schedules if item.get("enabled")]),
                "path": k.REPORT_SCHEDULES_PATH,
            },
        },
        "latest_runs": latest,
        "recent_runs": recent_runs,
    }


def summarize_alert_check_result(result: dict, notify: bool) -> dict:
    if not result.get("enabled", True):
        return {"level": "muted", "title": "告警未启用", "message": "检查未执行：告警功能当前关闭。"}
    events = result.get("events", []) or []
    active_count = int(result.get("active_count", 0) or 0)
    suppressed = [event for event in events if event.get("suppressed")]
    alerts = [event for event in events if event.get("kind") == "alert"]
    recoveries = [event for event in events if event.get("kind") == "recovery"]
    if not events:
        return {
            "level": "ok",
            "title": "检查完成，暂无异常",
            "message": f"没有发现新的告警事件，当前 active 告警 {active_count} 个。",
            "events_count": 0,
            "active_count": active_count,
            "notified": bool(notify),
        }
    parts = []
    if alerts:
        parts.append(f"新增/持续告警 {len(alerts)} 个")
    if recoveries:
        parts.append(f"恢复 {len(recoveries)} 个")
    if suppressed:
        parts.append(f"静默或冷却 {len(suppressed)} 个")
    titles = [str(event.get("title") or event.get("key") or "告警") for event in events[:3]]
    return {
        "level": "warn" if alerts else "ok",
        "title": "检查完成，发现需要关注的事件" if alerts else "检查完成，告警状态已更新",
        "message": "；".join(parts) + ("。" if parts else "。"),
        "events_count": len(events),
        "active_count": active_count,
        "notified": bool(notify),
        "items": titles,
    }


def report_message_from_request(req: TelegramReportRequest) -> tuple[str, str, str]:
    scope = req.scope.strip().lower()
    mode = req.mode.strip().lower()
    if scope not in ("today", "week", "month"):
        raise ValueError("scope must be today, week, or month")
    if mode not in ("full", "top"):
        raise ValueError("mode must be full or top")
    start, now, tag = period_parts(scope)
    message = k.build_period_report_message(start, now, tag, top_only=(mode == "top"))
    return scope, mode, message


def build_ai_status_struct() -> dict:
    cache = k.load_ai_pack_cache()
    created_at = int(cache.get("created_at", 0) or 0) if isinstance(cache, dict) else 0
    pack = cache.get("pack", {}) if isinstance(cache, dict) else {}
    now_ts = int(time.time())
    data_sources = []
    if isinstance(pack, dict):
        for key, value in pack.items():
            if key in ("now", "stat_tz"):
                continue
            failed = isinstance(value, dict) and value.get("error") == "failed"
            count = 0
            if isinstance(value, dict):
                if isinstance(value.get("nodes"), list):
                    count = len(value.get("nodes") or [])
                elif isinstance(value.get("top_nodes"), list):
                    count = len(value.get("top_nodes") or [])
                elif isinstance(value.get("days"), list):
                    count = len(value.get("days") or [])
            data_sources.append({
                "key": key,
                "status": "failed" if failed else "ok",
                "count": count,
            })
    return {
        "configured": k.ai_enabled(),
        "model": k.AI_MODEL,
        "cache_created_at": created_at,
        "cache_created_at_text": timestamp_text(created_at),
        "cache_age_seconds": max(0, now_ts - created_at) if created_at else 0,
        "cache_ttl_seconds": k.AI_PACK_CACHE_TTL_SECONDS,
        "cache_valid": bool(created_at and (k.AI_PACK_CACHE_TTL_SECONDS <= 0 or now_ts - created_at <= k.AI_PACK_CACHE_TTL_SECONDS)),
        "data_sources": data_sources,
    }


def to_node_rows(deltas: dict) -> list[dict]:
    rows = []
    for uuid, item in deltas.items():
        up = int(item.get("up", 0))
        down = int(item.get("down", 0))
        total = up + down
        rows.append({
            "uuid": uuid,
            "name": item.get("name") or uuid,
            "up": up,
            "down": down,
            "total": total,
            "up_human": k.human_bytes(up),
            "down_human": k.human_bytes(down),
            "total_human": k.human_bytes(total),
        })
    rows.sort(key=lambda x: (x["total"], x["down"], x["up"], x["name"].lower()), reverse=True)
    return rows


def total_from_nodes(nodes: list[dict]) -> dict:
    up = sum(int(n.get("up", 0)) for n in nodes)
    down = sum(int(n.get("down", 0)) for n in nodes)
    total = up + down
    return {
        "up": up,
        "down": down,
        "total": total,
        "up_human": k.human_bytes(up),
        "down_human": k.human_bytes(down),
        "total_human": k.human_bytes(total),
    }


def period_parts(scope: str):
    now = k.now_dt()
    today = k.today_date()
    if scope == "today":
        start = k.start_of_day(today)
        tag = today.strftime("%Y-%m-%d")
    elif scope == "week":
        week_start = k.start_of_week(today)
        start = k.start_of_day(week_start)
        tag = f"WEEK-{week_start.strftime('%Y-%m-%d')}"
    elif scope == "month":
        month_start = k.start_of_month(today)
        start = k.start_of_day(month_start)
        tag = f"MONTH-{month_start.strftime('%Y-%m-%d')}"
    else:
        raise RuntimeError("scope must be today, week, or month")
    return start, now, tag


def build_period_summary(scope: str) -> dict:
    start, now, tag = period_parts(scope)
    current_period = k.build_live_period_struct(start, now, tag)
    nodes = current_period.get("nodes", [])
    return {
        "scope": scope,
        "tag": tag,
        "from": start.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "to": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "note": current_period.get("note", "snapshot_window"),
        "nodes": nodes,
        "top_nodes": nodes[: max(0, int(k.TOP_N))],
        "total": current_period.get("total", total_from_nodes(nodes)),
        "skipped": current_period.get("skipped", []),
        "reset_warnings": current_period.get("reset_warnings", []),
        "sample_count": current_period.get("sample_count", 0),
        "segment_count": current_period.get("segment_count", 0),
        "rollup_days": current_period.get("rollup_days", 0),
        "snapshot_days": current_period.get("snapshot_days", 0),
        "segment_days": current_period.get("segment_days", 0),
        "coverage_days": current_period.get("coverage_days", []),
        "missing_days": current_period.get("missing_days", []),
        "source": current_period.get("source", "traffic_snapshots"),
        "source_parts": current_period.get("source_parts", []),
        "latest_sample_ts": current_period.get("latest_sample_ts", 0),
        "latest_sample_at": current_period.get("latest_sample_at", ""),
        "sample_lag_seconds": current_period.get("sample_lag_seconds"),
        "sample_stale": current_period.get("sample_stale", False),
    }


def build_alert_status_struct() -> dict:
    state = k.load_alerts_state()
    muted_until = k.alerts_muted_until_dt(state)
    active = []
    for key, rec in (state.get("active", {}) or {}).items():
        active.append({
            "key": key,
            "title": rec.get("title", key),
            "type": rec.get("type", ""),
            "last_seen": rec.get("last_seen", 0),
            "last_sent": rec.get("last_sent", 0),
            "last_seen_text": timestamp_text(rec.get("last_seen", 0)),
            "last_sent_text": timestamp_text(rec.get("last_sent", 0)),
        })
    active.sort(key=lambda item: int(item.get("last_seen") or 0), reverse=True)
    return {
        "enabled": bool(k.ALERTS_ENABLED),
        "active_count": len(active),
        "active": active,
        "muted_until": muted_until.strftime("%Y-%m-%d %H:%M:%S %Z") if muted_until else "",
        "in_silence_window": k.is_in_silence_window(),
        "silence_windows": k.ALERT_SILENCE_WINDOWS,
        "alert_chat": mask_value(k.telegram_alert_chat_id()),
        "cooldown_seconds": k.ALERT_COOLDOWN_SECONDS,
        "cooldown_text": seconds_text(k.ALERT_COOLDOWN_SECONDS),
        "window_minutes": k.ALERT_WINDOW_MINUTES,
        "thresholds": {
            "total_window": k.human_bytes(k.ALERT_TOTAL_WINDOW_BYTES) if k.ALERT_TOTAL_WINDOW_BYTES else "",
            "node_window": k.human_bytes(k.ALERT_NODE_WINDOW_BYTES) if k.ALERT_NODE_WINDOW_BYTES else "",
            "daily_total": k.human_bytes(k.ALERT_DAILY_TOTAL_BYTES) if k.ALERT_DAILY_TOTAL_BYTES else "",
            "daily_node": k.human_bytes(k.ALERT_DAILY_NODE_BYTES) if k.ALERT_DAILY_NODE_BYTES else "",
        },
    }


def safe_records_summary(hours: int, enrich: bool = True) -> dict:
    if hours not in (1, 6, 24, 168, 720):
        raise RuntimeError("hours must be one of 1, 6, 24, 168, 720")
    summary = k.build_records_summary(hours)
    return enrich_records_summary(summary) if enrich else summary


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/")
@app.get("/nodes")
@app.get("/alerts")
@app.get("/telegram")
@app.get("/ai")
@app.get("/analytics")
@app.get("/system")
async def index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return api_error("static/index.html not found", status_code=500, code="static_missing")
    return FileResponse(str(index_path), headers={"Cache-Control": "no-store"})


@app.post("/api/auth/login")
async def login(req: LoginRequest, request: Request):
    rate_key = login_rate_key(request)
    if login_limited(rate_key):
        return api_error("登录失败次数过多，请稍后再试。", status_code=429, code="login_rate_limited")
    if not web_password_configured():
        return api_error("WEB_PASSWORD is not configured", status_code=503, code="web_password_missing")
    if not secrets.compare_digest(req.username, web_username()) or not secrets.compare_digest(req.password, web_password()):
        record_login_failure(rate_key)
        return api_error("invalid username or password", status_code=401, code="invalid_login")
    clear_login_failures(rate_key)
    response = api_ok({
        "username": web_username(),
        "session_secret_temporary": WEB_SESSION_SECRET_TEMPORARY,
    })
    session_seconds = SESSION_REMEMBER_SECONDS if req.remember else SESSION_BROWSER_SECONDS
    cookie_kwargs = {
        "httponly": True,
        "samesite": "lax",
        "secure": request_is_https(request),
    }
    if req.remember:
        cookie_kwargs["max_age"] = SESSION_REMEMBER_SECONDS
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(web_username(), session_seconds),
        **cookie_kwargs,
    )
    return response


@app.post("/api/auth/logout")
async def logout():
    response = api_ok({"logged_out": True})
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/api/auth/session")
async def session(request: Request):
    username = validate_session_token(request.cookies.get(SESSION_COOKIE))
    return api_ok({
        "authenticated": bool(username),
        "username": username or "",
        "web_password_configured": web_password_configured(),
        "session_secret_temporary": WEB_SESSION_SECRET_TEMPORARY,
    })


@app.get("/api/overview")
def overview(_user: str = Depends(current_user)):
    today = compact_result_nodes(safe_call(build_period_summary, "today"), OVERVIEW_NODE_LIMIT)
    week = compact_result_nodes(safe_call(build_period_summary, "week"), OVERVIEW_NODE_LIMIT)
    month = compact_result_nodes(safe_call(build_period_summary, "month"), OVERVIEW_NODE_LIMIT)
    last_24h = compact_result_nodes(safe_call(safe_records_summary, 24, False), OVERVIEW_NODE_LIMIT)
    last_7d = compact_result_nodes(safe_call(safe_records_summary, 168, False), OVERVIEW_NODE_LIMIT)
    return api_ok({
        "now": k.now_dt().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "stat_tz": k.STAT_TZ,
        "instance": k.BOT_INSTANCE_NAME or "default",
        "session_secret_temporary": WEB_SESSION_SECRET_TEMPORARY,
        "services": {
            "komari": {"configured": bool(k.KOMARI_BASE_URL), "base_url": k.KOMARI_BASE_URL},
            "telegram": {"configured": bool(k.TELEGRAM_BOT_TOKEN and k.TELEGRAM_CHAT_ID), "chat": mask_value(k.TELEGRAM_CHAT_ID)},
            "ai": {"configured": k.ai_enabled(), "model": k.AI_MODEL},
            "alerts": {"enabled": bool(k.ALERTS_ENABLED)},
        },
        "periods": {"today": today, "week": week, "month": month},
        "records": {"last_24h": last_24h, "last_7d": last_7d},
    })


@app.get("/api/nodes")
async def nodes(hours: int = 24, compact: bool = False, _user: str = Depends(current_user)):
    result = safe_call(safe_records_summary, hours)
    if not result["ok"]:
        return api_error(result["error"]["message"], status_code=502, code=result["error"]["code"], data=result)
    return api_ok(compact_nodes_payload(result["data"]) if compact else result["data"])


@app.get("/api/komari/machines")
async def komari_machines(_user: str = Depends(current_user)):
    result = safe_call(fetch_komari_machines)
    if not result["ok"]:
        return api_error(result["error"]["message"], status_code=502, code=result["error"]["code"], data=result)
    return api_ok({
        "configured": bool(k.KOMARI_BASE_URL),
        "base_url": k.KOMARI_BASE_URL,
        "machines": result["data"],
    })


@app.get("/api/node-bindings")
async def node_bindings(_user: str = Depends(current_user)):
    bindings_data = load_node_bindings()
    machines_result = safe_call(fetch_komari_machines)
    machines = machines_result["data"] if machines_result["ok"] else []
    machine_index = {str(machine.get("uuid")): machine for machine in machines}
    resolved = {
        source_id: resolve_node_binding(source_id, machine_index, bindings_data.get("bindings", {}))
        for source_id in bindings_data.get("bindings", {}).keys()
    }
    return api_ok({
        "configured": bool(k.KOMARI_BASE_URL),
        "base_url": k.KOMARI_BASE_URL,
        "machines": machines,
        "bindings": bindings_data.get("bindings", {}),
        "resolved": resolved,
        "machine_error": "" if machines_result["ok"] else machines_result["error"]["message"],
    })


@app.post("/api/node-bindings")
async def save_node_binding(req: NodeBindingRequest, _user: str = Depends(current_user)):
    source_id = req.source_id.strip()
    komari_uuid = req.komari_uuid.strip()
    if not source_id:
        return api_error("source_id is required", status_code=400, code="missing_source_id")
    bindings_data = load_node_bindings()
    bindings = bindings_data.get("bindings", {})
    machines_result = safe_call(fetch_komari_machines)
    machines = machines_result["data"] if machines_result["ok"] else []
    machine_error = "" if machines_result["ok"] else machines_result["error"]["message"]
    machine_index = {str(machine.get("uuid")): machine for machine in machines}
    # Binding to the same uuid is equivalent to the automatic path; keep the
    # override file focused on real manual exceptions.
    if komari_uuid == source_id:
        komari_uuid = ""
    if komari_uuid:
        if not machines_result["ok"]:
            return api_error(machine_error, status_code=502, code=machines_result["error"]["code"], data=machines_result)
        if komari_uuid not in machine_index:
            return api_error("komari_uuid not found", status_code=404, code="komari_uuid_not_found")
        bindings[source_id] = {
            "komari_uuid": komari_uuid,
            "updated_at": int(time.time()),
        }
    else:
        bindings.pop(source_id, None)
    save_node_bindings({"bindings": bindings})
    payload = node_binding_payload(source_id, bindings, machines, machine_error)
    payload["cleared"] = not bool(komari_uuid)
    return api_ok(payload)


@app.get("/api/nodes/{uuid}")
def node_detail(uuid: str, hours: int = 24, _user: str = Depends(current_user)):
    summary = safe_call(safe_records_summary, hours)
    if not summary["ok"]:
        return api_error(summary["error"]["message"], status_code=502, code=summary["error"]["code"], data=summary)
    nodes_data = summary["data"].get("nodes", [])
    matched = next((n for n in nodes_data if str(n.get("uuid")) == uuid), None)
    if matched is None:
        safe_uuid = re.sub(r"[^a-zA-Z0-9_.:-]", "", uuid)
        matched = next((n for n in nodes_data if str(n.get("name")) == safe_uuid), None)
    if matched is None:
        return api_error("node not found in selected range", status_code=404, code="node_not_found")
    hourly = safe_call(k.build_snapshot_hourly_by_node_summary, summary["data"].get("from_ts", 0), summary["data"].get("to_ts", 0)) if hours == 24 else {"ok": True, "data": None}
    node_payload = dict(matched)
    if hourly.get("ok") and isinstance(hourly.get("data"), dict):
        hourly_nodes = hourly["data"].get("nodes") or []
        node_hourly = next((item for item in hourly_nodes if str(item.get("uuid")) == str(matched.get("uuid"))), None)
        if node_hourly:
            node_payload["hourly_distribution"] = {
                "hours": node_hourly.get("hours", []),
                "peak_hour": node_hourly.get("peak_hour"),
                "valley_hour": node_hourly.get("valley_hour"),
                "sample_count": hourly["data"].get("sample_count", 0),
                "segment_count": hourly["data"].get("segment_count", 0),
                "source": hourly["data"].get("source", "traffic_snapshots"),
            }
        else:
            node_payload["hourly_distribution"] = {
                "hours": [],
                "sample_count": hourly["data"].get("sample_count", 0),
                "segment_count": hourly["data"].get("segment_count", 0),
                "source": hourly["data"].get("source", "traffic_snapshots"),
            }
    elif not hourly.get("ok"):
        node_payload["hourly_distribution"] = {"error": hourly.get("error", {}).get("message", "failed")}
    return api_ok({"node": node_payload, "range": summary["data"], "hourly": hourly})


@app.get("/api/traffic/range")
def traffic_range(
    from_day: str = Query(..., alias="from"),
    to_day: str = Query(..., alias="to"),
    group: str = "daily",
    compact: bool = True,
    _user: str = Depends(current_user),
):
    try:
        start = k.parse_date_yyyy_mm_dd(from_day)
        end = k.parse_date_yyyy_mm_dd(to_day)
        data = k.traffic_range_summary(start, end, group=group)
        if compact:
            data = compact_traffic_range_payload(data)
    except Exception as exc:
        return api_error(str(exc), status_code=400, code="invalid_range")
    return api_ok(data)


@app.get("/api/traffic/range/export.csv")
def traffic_range_export_csv(
    from_day: str = Query(..., alias="from"),
    to_day: str = Query(..., alias="to"),
    group: str = "daily",
    _user: str = Depends(current_user),
):
    try:
        start = k.parse_date_yyyy_mm_dd(from_day)
        end = k.parse_date_yyyy_mm_dd(to_day)
        data = k.traffic_range_summary(start, end, group=group)
    except Exception as exc:
        return api_error(str(exc), status_code=400, code="invalid_range")
    return traffic_range_csv_response(data)


@app.get("/api/tasks/runs")
async def task_runs(limit: int = 50, offset: int = 0, task_type: str = Query("", alias="type"), _user: str = Depends(current_user)):
    safe_limit = min(200, max(1, int(limit or 50)))
    safe_offset = max(0, int(offset or 0))
    try:
        runs = [task_run_response(run) for run in k.list_task_runs(limit=safe_limit, offset=safe_offset, task_type=task_type)]
        total_count = k.count_task_runs(task_type=task_type)
    except Exception as exc:
        return api_error(str(exc), status_code=500, code=type(exc).__name__)
    return api_ok({"runs": runs, "limit": safe_limit, "offset": safe_offset, "total_count": total_count, "type": task_type})


@app.get("/api/system/status")
async def system_status(_user: str = Depends(current_user)):
    return api_ok(build_system_status_struct())


@app.get("/api/system/config")
async def system_config(_user: str = Depends(current_user)):
    return api_ok(k.current_runtime_config())


@app.post("/api/system/config")
async def system_config_save(req: RuntimeConfigRequest, _user: str = Depends(current_user)):
    payload = req.model_dump(exclude_unset=True) if hasattr(req, "model_dump") else req.dict(exclude_unset=True)
    started = time.time()
    try:
        config = k.save_runtime_config(payload)
        k.safe_record_task_run(
            "maintenance",
            "web:config",
            "success",
            started_at=started,
            finished_at=time.time(),
            summary="低敏配置已更新",
            metadata={"keys": sorted(payload.keys())},
        )
    except Exception as exc:
        k.safe_record_task_run(
            "maintenance",
            "web:config",
            "failed",
            started_at=started,
            finished_at=time.time(),
            error=str(exc),
            metadata={"keys": sorted(payload.keys())},
        )
        return api_error(str(exc), status_code=400, code="invalid_runtime_config")
    return api_ok({"config": k.current_runtime_config(), "values": config})


@app.get("/api/system/maintenance")
async def system_maintenance(_user: str = Depends(current_user)):
    return api_ok(build_maintenance_status())


@app.post("/api/system/maintenance/prune-task-runs")
async def system_prune_task_runs(req: MaintenancePruneRequest, _user: str = Depends(current_user)):
    started = time.time()
    try:
        result = k.prune_task_runs(req.retention_days)
        k.safe_record_task_run(
            "maintenance",
            "web:prune-task-runs",
            "success",
            started_at=started,
            finished_at=time.time(),
            summary=f"清理旧运行记录 {result.get('deleted', 0)} 条",
            metadata={
                "retention_days": result.get("retention_days"),
                "deleted": result.get("deleted"),
                "cutoff": result.get("cutoff"),
            },
        )
    except Exception as exc:
        k.safe_record_task_run(
            "maintenance",
            "web:prune-task-runs",
            "failed",
            started_at=started,
            finished_at=time.time(),
            error=str(exc),
        )
        return api_error(str(exc), status_code=400, code=type(exc).__name__)
    return api_ok({"maintenance": build_maintenance_status(), "result": result})


@app.post("/api/system/maintenance/vacuum")
async def system_vacuum(_user: str = Depends(current_user)):
    started = time.time()
    try:
        result = k.vacuum_traffic_db()
        k.safe_record_task_run(
            "maintenance",
            "web:vacuum",
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
    except Exception as exc:
        k.safe_record_task_run(
            "maintenance",
            "web:vacuum",
            "failed",
            started_at=started,
            finished_at=time.time(),
            error=str(exc),
        )
        return api_error(str(exc), status_code=500, code=type(exc).__name__)
    return api_ok({"maintenance": build_maintenance_status(), "result": result})


@app.post("/api/system/maintenance/drop-period-rollups")
async def system_drop_period_rollups(_user: str = Depends(current_user)):
    started = time.time()
    try:
        result = k.drop_period_rollups_table()
        k.safe_record_task_run(
            "maintenance",
            "web:drop-period-rollups",
            "success",
            started_at=started,
            finished_at=time.time(),
            summary="未使用 period_rollups 表已删除" if result.get("dropped") else "period_rollups 表不存在",
            metadata=result,
        )
    except Exception as exc:
        k.safe_record_task_run(
            "maintenance",
            "web:drop-period-rollups",
            "failed",
            started_at=started,
            finished_at=time.time(),
            error=str(exc),
        )
        return api_error(str(exc), status_code=500, code=type(exc).__name__)
    return api_ok({"maintenance": build_maintenance_status(), "result": result})


@app.get("/api/alerts")
async def alerts(_user: str = Depends(current_user)):
    return api_ok(build_alert_status_struct())


@app.post("/api/alerts/check")
def alerts_check(req: AlertCheckRequest, _user: str = Depends(current_user)):
    result = k.run_alert_check(dry_run=not req.notify, notify=req.notify, force_sample=True, source="web:alerts-check")
    result["summary"] = summarize_alert_check_result(result, notify=req.notify)
    return api_ok(result)


@app.post("/api/alerts/mute")
async def alerts_mute(req: AlertMuteRequest, _user: str = Depends(current_user)):
    if req.hours <= 0:
        return api_error("hours must be > 0", status_code=400, code="invalid_hours")
    muted_until = k.set_alerts_muted_for(req.hours)
    return api_ok({"muted_until": muted_until.strftime("%Y-%m-%d %H:%M:%S %Z")})


@app.post("/api/alerts/unmute")
async def alerts_unmute(_user: str = Depends(current_user)):
    k.clear_alerts_muted()
    return api_ok({"muted": False})


@app.get("/api/alerts/history")
async def alerts_history(limit: int = 10, offset: int = 0, _user: str = Depends(current_user)):
    safe_limit = min(200, max(1, int(limit or 10)))
    safe_offset = max(0, int(offset or 0))
    try:
        runs = [task_run_response(run) for run in k.list_task_runs(limit=safe_limit, offset=safe_offset, task_type="alert")]
        total_count = k.count_task_runs(task_type="alert")
    except Exception as exc:
        return api_error(str(exc), status_code=500, code=type(exc).__name__)
    return api_ok({"runs": runs, "limit": safe_limit, "offset": safe_offset, "total_count": total_count})


@app.post("/api/telegram/test")
async def telegram_test(_user: str = Depends(current_user)):
    message = (
        "✅ Komari traffic Web 面板测试\n"
        f"实例：{k.BOT_INSTANCE_NAME or 'default'}\n"
        f"时间：{k.now_dt().strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )
    k.telegram_send_to_chat(message, k.TELEGRAM_CHAT_ID, parse_mode=None)
    return api_ok({"sent": True, "chat": mask_value(k.TELEGRAM_CHAT_ID)})


@app.get("/api/telegram/status")
async def telegram_status(_user: str = Depends(current_user)):
    return api_ok(build_telegram_status_struct())


@app.get("/api/schedules")
async def schedules(_user: str = Depends(current_user)):
    return api_ok(build_schedules_struct())


@app.post("/api/schedules")
async def create_schedule(req: ScheduleRequest, _user: str = Depends(current_user)):
    try:
        item = schedule_payload(req)
    except Exception as exc:
        return api_error(str(exc), status_code=400, code="invalid_schedule")
    data = k.load_report_schedules()
    data["schedules"].append(item)
    k.save_report_schedules(data)
    return api_ok({"schedule": schedule_response(item), "schedules": build_schedules_struct()["schedules"]})


@app.patch("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: str, req: ScheduleRequest, _user: str = Depends(current_user)):
    data = k.load_report_schedules()
    schedules = data.get("schedules", [])
    index = next((i for i, item in enumerate(schedules) if item.get("id") == schedule_id), None)
    if index is None:
        return api_error("schedule not found", status_code=404, code="schedule_not_found")
    try:
        item = schedule_payload(req, schedule_id=schedule_id)
    except Exception as exc:
        return api_error(str(exc), status_code=400, code="invalid_schedule")
    schedules[index] = item
    data["schedules"] = schedules
    k.save_report_schedules(data)
    return api_ok({"schedule": schedule_response(item), "schedules": build_schedules_struct()["schedules"]})


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str, _user: str = Depends(current_user)):
    data = k.load_report_schedules()
    before = len(data.get("schedules", []))
    data["schedules"] = [item for item in data.get("schedules", []) if item.get("id") != schedule_id]
    data.get("last_runs", {}).pop(schedule_id, None)
    if len(data["schedules"]) == before:
        return api_error("schedule not found", status_code=404, code="schedule_not_found")
    k.save_report_schedules(data)
    return api_ok({"deleted": True, "schedules": build_schedules_struct()["schedules"]})


@app.post("/api/schedules/{schedule_id}/run-now")
async def run_schedule_now(schedule_id: str, _user: str = Depends(current_user)):
    data = k.load_report_schedules()
    item = next((entry for entry in data.get("schedules", []) if entry.get("id") == schedule_id), None)
    if not item:
        return api_error("schedule not found", status_code=404, code="schedule_not_found")
    try:
        result = k.run_report_schedule(item, source="web:run-now")
    except Exception as exc:
        return api_error(str(exc), status_code=502, code=type(exc).__name__)
    if isinstance(result.get("schedule"), dict):
        result["schedule"] = schedule_response(result["schedule"])
    result["chat"] = mask_value(result.get("chat", ""))
    return api_ok(result)


@app.post("/api/telegram/preview")
async def telegram_preview(req: TelegramReportRequest, _user: str = Depends(current_user)):
    try:
        scope, mode, message = report_message_from_request(req)
    except ValueError as exc:
        return api_error(str(exc), status_code=400, code="invalid_report_request")
    return api_ok({
        "scope": scope,
        "mode": mode,
        "message": message,
        "chat": mask_value(k.TELEGRAM_CHAT_ID),
    })


@app.post("/api/telegram/report")
async def telegram_report(req: TelegramReportRequest, _user: str = Depends(current_user)):
    try:
        scope, mode, message = report_message_from_request(req)
    except ValueError as exc:
        return api_error(str(exc), status_code=400, code="invalid_report_request")
    def send_now():
        k.telegram_send(message)
        return {"sent": True, "scope": scope, "mode": mode, "chat": k.TELEGRAM_CHAT_ID, "label": f"Web 手动发送 {scope}/{mode}"}

    try:
        result = k.run_with_task_record(
            "report",
            "web:composer",
            send_now,
            summary_func=lambda item: item.get("label", ""),
            metadata={"scope": scope, "mode": mode},
        )
    except Exception as exc:
        return api_error(str(exc), status_code=502, code=type(exc).__name__)
    result["chat"] = mask_value(result.get("chat", ""))
    return api_ok(result)


@app.get("/api/ai/status")
async def ai_status(_user: str = Depends(current_user)):
    return api_ok(build_ai_status_struct())


@app.post("/api/ai/refresh")
async def ai_refresh(_user: str = Depends(current_user)):
    if not k.ai_enabled():
        return api_error("AI is not configured", status_code=400, code="ai_disabled")
    def refresh_pack():
        pack = k.build_ai_data_pack()
        k.save_ai_pack_cache(pack)
        return {"summary": "AI 数据包缓存已刷新"}

    try:
        k.run_with_task_record("ai", "web:refresh", refresh_pack)
    except Exception as exc:
        return api_error(str(exc), status_code=502, code=type(exc).__name__)
    return api_ok(build_ai_status_struct())


@app.post("/api/ai/ask")
def ai_ask(req: AiAskRequest, _user: str = Depends(current_user)):
    question = req.question.strip()
    if not question:
        return api_error("question is required", status_code=400, code="empty_question")
    if not k.ai_enabled():
        return api_error("AI is not configured", status_code=400, code="ai_disabled")
    data_pack = k.build_ai_data_pack() if k.question_requires_fresh_ai_pack(question) else k.get_ai_data_pack_cached()

    start_time = time.time()
    answer = k.normalize_ai_answer_for_telegram(k.ask_ai_with_data(question, data_pack))
    duration_ms = int((time.time() - start_time) * 1000)

    try:
        data_pack_hash = k.get_ai_data_pack_hash(data_pack)
        k.save_ai_qa_record(question, answer, data_pack_hash, duration_ms)
    except Exception as exc:
        import logging
        logging.warning(f"Failed to save AI Q&A record: {exc}")

    return api_ok({"answer": answer})


@app.get("/api/ai/history")
def ai_history(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _user: str = Depends(current_user)
):
    try:
        history_list = k.list_ai_qa_history(limit=limit, offset=offset)
        return api_ok({"items": history_list, "total": len(history_list)})
    except Exception as exc:
        return api_error(str(exc), status_code=500, code=type(exc).__name__)


@app.delete("/api/ai/history")
def ai_history_clear(
    days: int = Query(30, ge=1),
    _user: str = Depends(current_user)
):
    try:
        cutoff_timestamp = int((datetime.now().timestamp() - days * 86400))
        deleted_count = k.delete_old_ai_qa_records(cutoff_timestamp)
        return api_ok({"deleted": deleted_count})
    except Exception as exc:
        return api_error(str(exc), status_code=500, code=type(exc).__name__)
