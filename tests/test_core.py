import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


def install_requests_stub_if_needed():
    try:
        import requests  # noqa: F401
        from urllib3.util import Retry  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    requests_mod = types.ModuleType("requests")
    adapters_mod = types.ModuleType("requests.adapters")
    urllib3_mod = types.ModuleType("urllib3")
    urllib3_util_mod = types.ModuleType("urllib3.util")

    class RequestException(Exception):
        pass

    class ReadTimeout(RequestException):
        pass

    class ConnectionError(RequestException):
        pass

    class ChunkedEncodingError(RequestException):
        pass

    class HTTPError(RequestException):
        def __init__(self, *args, response=None):
            super().__init__(*args)
            self.response = response

    class HTTPAdapter:
        def __init__(self, *args, **kwargs):
            pass

    class Retry:
        def __init__(self, *args, **kwargs):
            pass

    class Session:
        def mount(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            raise RequestException("requests stub cannot perform network calls")

    def post(*args, **kwargs):
        raise RequestException("requests stub cannot perform network calls")

    requests_mod.Session = Session
    requests_mod.post = post
    requests_mod.HTTPError = HTTPError
    requests_mod.exceptions = types.SimpleNamespace(
        RequestException=RequestException,
        ReadTimeout=ReadTimeout,
        ConnectionError=ConnectionError,
        ChunkedEncodingError=ChunkedEncodingError,
        HTTPError=HTTPError,
    )
    adapters_mod.HTTPAdapter = HTTPAdapter
    urllib3_util_mod.Retry = Retry

    sys.modules.setdefault("requests", requests_mod)
    sys.modules.setdefault("requests.adapters", adapters_mod)
    sys.modules.setdefault("urllib3", urllib3_mod)
    sys.modules.setdefault("urllib3.util", urllib3_util_mod)


install_requests_stub_if_needed()
os.environ["STAT_TZ"] = "UTC"

import komari_traffic_report as k  # noqa: E402


class FakeTelegramResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True, "result": []}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise k.requests.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return self._payload


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.patchers = []
        self.point_runtime_paths()
        self.configure_alerts()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def patch_attr(self, name, value):
        patcher = patch.object(k, name, value)
        patcher.start()
        self.patchers.append(patcher)

    def point_runtime_paths(self):
        self.patch_attr("DATA_DIR", str(self.tmp_path))
        self.patch_attr("SAMPLES_PATH", str(self.tmp_path / "samples.json"))
        self.patch_attr("ALERTS_STATE_PATH", str(self.tmp_path / "alerts_state.json"))
        self.patch_attr("HISTORY_PATH", str(self.tmp_path / "history.json"))
        self.patch_attr("REPORT_SCHEDULES_PATH", str(self.tmp_path / "report_schedules.json"))
        self.patch_attr("TRAFFIC_DB_PATH", str(self.tmp_path / "traffic.db"))
        self.patch_attr("TG_OFFSET_PATH", str(self.tmp_path / "tg_offset.txt"))
        self.patch_attr("TG_CONFIRM_PATH", str(self.tmp_path / "tg_confirm.json"))
        self.patch_attr("AI_PACK_CACHE_PATH", str(self.tmp_path / "ai_pack_cache.json"))

    def configure_alerts(self):
        self.patch_attr("ALERTS_ENABLED", True)
        self.patch_attr("ALERT_COOLDOWN_SECONDS", 1800)
        self.patch_attr("ALERT_SILENCE_WINDOWS", "")
        self.patch_attr("ALERT_NODE_MISSING_SAMPLES", 2)
        self.patch_attr("ALERT_WINDOW_MINUTES", 60)
        self.patch_attr("ALERT_TOTAL_WINDOW_BYTES", 0)
        self.patch_attr("ALERT_NODE_WINDOW_BYTES", 0)
        self.patch_attr("ALERT_DAILY_TOTAL_BYTES", 0)
        self.patch_attr("ALERT_DAILY_NODE_BYTES", 0)
        self.patch_attr("ALERT_RECOVERY_NOTIFY", True)
        self.patch_attr("BOT_INSTANCE_NAME", "")
        self.patch_attr("KOMARI_BASE_URL", "")
        self.patch_attr("TELEGRAM_CHAT_ID", "")
        self.patch_attr("TELEGRAM_ALERT_CHAT_ID", "")
        self.patch_attr("AI_API_BASE", "")
        self.patch_attr("AI_MODEL", "")
        self.patch_attr("TOP_N", 3)
        self.patch_attr("TIMEOUT", 10)
        self.patch_attr("KOMARI_FETCH_WORKERS", 4)
        self.patch_attr("SAMPLE_INTERVAL_SECONDS", 300)
        self.patch_attr("SAMPLE_RETENTION_HOURS", 48)
        self.patch_attr("TRAFFIC_SNAPSHOT_RETENTION_DAYS", 45)
        self.patch_attr("AI_PACK_CACHE_TTL_SECONDS", 3600)
        self.patch_attr("TASK_RUN_RETENTION_DAYS", 90)
        self.patch_attr("NODE_DAILY_USAGE_RETENTION_DAYS", 365)

    def test_parse_bytes_value_supports_units(self):
        self.assertEqual(k.parse_bytes_value(""), 0)
        self.assertEqual(k.parse_bytes_value("1024"), 1024)
        self.assertEqual(k.parse_bytes_value("500MiB"), 500 * 1024 ** 2)
        self.assertEqual(k.parse_bytes_value("2 GiB"), 2 * 1024 ** 3)
        self.assertEqual(k.parse_bytes_value("1.5TiB"), int(1.5 * 1024 ** 4))

    def test_parse_bytes_value_rejects_bad_unit(self):
        with self.assertRaises(RuntimeError):
            k.parse_bytes_value("10XB", "TEST_BYTES")

    def test_save_json_atomic_uses_unique_temp_file(self):
        target = self.tmp_path / "state.json"
        fixed_tmp = self.tmp_path / "state.json.tmp"
        fixed_tmp.write_text("sentinel", encoding="utf-8")

        k.save_json_atomic(str(target), {"ok": True})

        self.assertEqual(k.load_json_strict(str(target)), {"ok": True})
        self.assertEqual(fixed_tmp.read_text(encoding="utf-8"), "sentinel")
        leftovers = [
            path.name
            for path in self.tmp_path.glob("state.json.*.tmp")
            if path.name != "state.json.tmp"
        ]
        self.assertEqual(leftovers, [])

    def test_save_archive_month_uses_unique_temp_file(self):
        fixed_tmp = self.tmp_path / "history-2026-06.json.gz.tmp"
        fixed_tmp.write_text("sentinel", encoding="utf-8")

        k.save_archive_month("2026-06", {"days": {"2026-06-01": {"n1": {"down": 2}}}})

        self.assertEqual(k.load_archive_month("2026-06")["days"]["2026-06-01"]["n1"]["down"], 2)
        self.assertEqual(fixed_tmp.read_text(encoding="utf-8"), "sentinel")
        leftovers = [
            path.name
            for path in self.tmp_path.glob("history-2026-06.json.gz.*.tmp")
            if path.name != "history-2026-06.json.gz.tmp"
        ]
        self.assertEqual(leftovers, [])

    def test_save_offset_uses_atomic_unique_temp_file(self):
        fixed_tmp = self.tmp_path / "tg_offset.txt.tmp"
        fixed_tmp.write_text("sentinel", encoding="utf-8")

        k.save_offset(12345)

        self.assertEqual(k.load_offset(), 12345)
        self.assertEqual(fixed_tmp.read_text(encoding="utf-8"), "sentinel")
        leftovers = [
            path.name
            for path in self.tmp_path.glob("tg_offset.txt.*.tmp")
            if path.name != "tg_offset.txt.tmp"
        ]
        self.assertEqual(leftovers, [])

    def test_get_updates_retries_temporary_502(self):
        calls = []
        responses = [
            FakeTelegramResponse(502),
            FakeTelegramResponse(200, {"ok": True, "result": [{"update_id": 7}]}),
        ]

        def fake_get(url, params, timeout):
            calls.append((url, params, timeout))
            return responses.pop(0)

        self.patch_attr("TELEGRAM_BOT_TOKEN", "token")
        with patch.object(k.requests, "get", side_effect=fake_get), \
             patch.object(k.time, "sleep") as sleep_mock, \
             patch.object(k.random, "random", return_value=0):
            data = k.get_updates(123)

        self.assertEqual(data["result"][0]["update_id"], 7)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], {"timeout": 50, "offset": 123})
        sleep_mock.assert_called_once_with(1.0)

    def test_get_updates_409_conflict_is_not_retried(self):
        sent = []

        self.patch_attr("TELEGRAM_BOT_TOKEN", "token")
        self.patch_attr("safe_telegram_send", lambda text: sent.append(text))
        self.patch_attr("should_alert", lambda _key, _seconds: True)

        with patch.object(k.requests, "get", return_value=FakeTelegramResponse(409)), \
             patch.object(k.time, "sleep") as sleep_mock:
            with self.assertRaises(k.requests.HTTPError):
                k.get_updates(None)

        self.assertEqual(len(sent), 1)
        self.assertIn("409 Conflict", sent[0])
        sleep_mock.assert_not_called()

    def test_silence_window_supports_cross_midnight(self):
        late = datetime(2026, 6, 6, 23, 30, tzinfo=k.TZ)
        early = datetime(2026, 6, 6, 6, 30, tzinfo=k.TZ)
        noon = datetime(2026, 6, 6, 12, 0, tzinfo=k.TZ)

        self.assertTrue(k.is_in_silence_window(late, "23:00-07:00"))
        self.assertTrue(k.is_in_silence_window(early, "23:00-07:00"))
        self.assertFalse(k.is_in_silence_window(noon, "23:00-07:00"))

    def test_strict_sample_delta_ignores_counter_reset_absolute_value(self):
        current = {"u1": {"name": "node-a", "up": 10, "down": 20}}
        previous = {"u1": {"name": "node-a", "up": 100, "down": 200}}

        deltas, warnings = k.compute_strict_sample_delta_from_maps(current, previous)

        self.assertEqual(deltas["u1"]["up"], 0)
        self.assertEqual(deltas["u1"]["down"], 0)
        self.assertEqual(warnings, ["node-a(counter_reset)"])

    def test_top_lines_orders_by_total(self):
        lines = k.top_lines(
            {
                "a": {"name": "small", "up": 1, "down": 1},
                "b": {"name": "big", "up": 10, "down": 20},
                "c": {"name": "middle", "up": 8, "down": 1},
            },
            2,
        )

        self.assertIn("big", lines[0])
        self.assertIn("middle", lines[1])
        self.assertEqual(len(lines), 2)

    def test_telegram_reports_escape_dynamic_html(self):
        deltas = {
            "u1": {"name": "node <bad> & edge", "up": 1, "down": 2},
        }

        full = k.format_report(
            "Title <T>",
            "range <1>",
            deltas,
            reset_warnings=["reset <node>"],
            skipped=["skip <node>(timeout)"],
            include_top=True,
        )
        top_only = k.format_top_only_message(
            "range <1>",
            deltas,
            reset_warnings=["reset <node>"],
            skipped=["skip <node>(timeout)"],
        )

        for message in (full, top_only):
            self.assertIn("<b>node &lt;bad&gt; &amp; edge</b>", message)
            self.assertIn("range &lt;1&gt;", message)
            self.assertIn("skip &lt;node&gt;(timeout)", message)
            self.assertIn("reset &lt;node&gt;", message)
            self.assertNotIn("<bad>", message)
            self.assertNotIn("<node>", message)
        self.assertIn("Title &lt;T&gt;", full)

    def test_alert_exception_redacts_and_escapes_failure_details(self):
        sent = []
        self.patch_attr("TELEGRAM_BOT_TOKEN", "secret-telegram-token")
        self.patch_attr("TELEGRAM_CHAT_ID", "123456789")
        self.patch_attr("KOMARI_API_TOKEN", "secret-komari-token")
        self.patch_attr("AI_API_KEY", "secret-ai-key")
        self.patch_attr("telegram_send", lambda text: sent.append(text))

        try:
            raise RuntimeError("bad <tag> secret-telegram-token secret-komari-token secret-ai-key")
        except RuntimeError as exc:
            k.alert_exception("where <x>", "cmd <run> secret-ai-key", exc)

        self.assertEqual(len(sent), 1)
        message = sent[0]
        self.assertIn("where &lt;x&gt;", message)
        self.assertIn("cmd &lt;run&gt;", message)
        self.assertIn("bad &lt;tag&gt;", message)
        self.assertNotIn("<tag>", message)
        self.assertNotIn("<x>", message)
        self.assertNotIn("<run>", message)
        self.assertNotIn("secret-telegram-token", message)
        self.assertNotIn("secret-komari-token", message)
        self.assertNotIn("secret-ai-key", message)

    def test_ai_chat_redacts_error_details(self):
        self.patch_attr("AI_API_BASE", "https://ai.example/v1")
        self.patch_attr("AI_API_KEY", "secret-ai-key")
        self.patch_attr("AI_MODEL", "gpt-test")

        with patch.object(k.requests, "post", side_effect=RuntimeError("boom secret-ai-key <tag>")):
            text = k.ai_chat([{"role": "user", "content": "hello"}])

        self.assertIn("RuntimeError", text)
        self.assertNotIn("secret-ai-key", text)
        normalized = k.normalize_ai_answer_for_telegram(text)
        self.assertIn("&lt;tag&gt;", normalized)
        self.assertNotIn("<tag>", normalized)

    def test_node_missing_alert_triggers_and_recovers(self):
        k.save_samples({
            "samples": [
                {"ts": 1000, "nodes": {}, "skipped": ["node-a(timeout)"]},
            ]
        })
        state = k.load_alerts_state()
        first = k.collect_alert_candidates(state, now_ts=1000)
        self.assertEqual(first, [])

        k.save_samples({
            "samples": [
                {"ts": 1000, "nodes": {}, "skipped": ["node-a(timeout)"]},
                {"ts": 1300, "nodes": {}, "skipped": ["node-a(timeout)"]},
            ]
        })
        second = k.collect_alert_candidates(state, now_ts=1300)
        self.assertEqual([c["key"] for c in second], ["node_missing:node-a"])

        events = k.apply_alert_candidates(state, second, now_ts=1300)
        self.assertEqual(events[0]["kind"], "alert")
        self.assertIn("节点采样异常", events[0]["message"])
        self.assertIn("节点：<b>node-a</b>", events[0]["message"])
        self.assertIn("读取节点数据超时", events[0]["message"])
        self.assertNotIn("node-a(timeout)", events[0]["message"])
        self.assertIn("node_missing:node-a", state["active"])

        k.save_samples({
            "samples": [
                {"ts": 1600, "nodes": {"u1": {"name": "node-a", "up": 1, "down": 1}}, "skipped": []},
            ]
        })
        recovered = k.collect_alert_candidates(state, now_ts=1600)
        events = k.apply_alert_candidates(state, recovered, now_ts=1600)
        self.assertEqual(events[0]["kind"], "recovery")
        self.assertIn("节点采样已恢复", events[0]["message"])
        self.assertIn("监控数据已恢复正常", events[0]["message"])
        self.assertIn("约 5 分钟", events[0]["message"])
        self.assertEqual(state["active"], {})

    def test_node_missing_empty_reason_is_human_readable(self):
        event = k._alert_event(
            "node_missing:node-a",
            "node_missing",
            "节点采样异常：node-a",
            "unused",
            details={
                "node_name": "node-a",
                "failure_count": 2,
                "failure_reason": "node-a(empty)",
            },
        )

        message = k.format_alert_message(event, now_ts=1000)

        self.assertIn("最近一次采样未返回监控数据", message)
        self.assertNotIn("node-a(empty)", message)

    def test_node_missing_does_not_double_count_same_sample(self):
        k.save_samples({
            "samples": [
                {"ts": 1000, "nodes": {}, "skipped": ["node-a(timeout)"]},
            ]
        })
        state = k.load_alerts_state()

        self.assertEqual(k.collect_alert_candidates(state, now_ts=1000), [])
        self.assertEqual(k.collect_alert_candidates(state, now_ts=1010), [])
        self.assertEqual(state["node_skips"]["node-a"]["count"], 1)

    def test_node_missing_alert_does_not_repeat_while_still_offline(self):
        candidate = k._alert_event(
            "node_missing:node-a",
            "node_missing",
            "节点连续采样异常：node-a",
            "still offline",
        )
        state = {"active": {}, "node_skips": {}, "muted_until": 0}

        first = k.apply_alert_candidates(state, [candidate], now_ts=1000)
        self.assertEqual([event["kind"] for event in first], ["alert"])
        state["active"]["node_missing:node-a"]["last_sent"] = 1000

        after_cooldown = k.apply_alert_candidates(state, [candidate], now_ts=4000)
        self.assertEqual(after_cooldown, [])

    def test_traffic_alert_still_repeats_after_cooldown(self):
        candidate = k._alert_event("window_total", "window_total", "窗口总流量超阈值", "high")
        state = {"active": {}, "node_skips": {}, "muted_until": 0}

        k.apply_alert_candidates(state, [candidate], now_ts=1000)
        state["active"]["window_total"]["last_sent"] = 1000

        repeated = k.apply_alert_candidates(state, [candidate], now_ts=2800)
        self.assertEqual([event["kind"] for event in repeated], ["alert"])
        self.assertIn("告警仍在持续", repeated[0]["message"])

    def test_window_total_alert_and_dry_run_does_not_persist(self):
        self.patch_attr("ALERT_TOTAL_WINDOW_BYTES", 100)
        self.patch_attr("ALERT_WINDOW_MINUTES", 60)
        self.patch_attr("telegram_send_alert", lambda message: self.fail("dry-run sent Telegram"))
        time_patcher = patch.object(k.time, "time", return_value=4600)
        time_patcher.start()
        self.patchers.append(time_patcher)

        k.save_samples({
            "samples": [
                {"ts": 1000, "nodes": {"u1": {"name": "node-a", "up": 0, "down": 0}}, "skipped": []},
                {"ts": 4600, "nodes": {"u1": {"name": "node-a", "up": 50, "down": 100}}, "skipped": []},
            ]
        })
        k.save_traffic_snapshot(1000, {"u1": {"name": "node-a", "up": 0, "down": 0}})
        k.save_traffic_snapshot(4600, {"u1": {"name": "node-a", "up": 50, "down": 100}})

        result = k.run_alert_check(dry_run=True, notify=True, force_sample=False)

        self.assertEqual(result["events"][0]["key"], "window_total")
        self.assertFalse((self.tmp_path / "alerts_state.json").exists())

    def test_alert_message_escapes_title(self):
        message = k.format_alert_message(
            {
                "title": "node <bad>",
                "body": "body",
            },
            now_ts=1000,
        )

        self.assertIn("node &lt;bad&gt;", message)

    def test_normalize_percent_metric_rejects_byte_like_values(self):
        self.assertEqual(k.normalize_percent_metric(50), 50)
        self.assertEqual(k.normalize_percent_metric({"used": 1, "total": 2}), 50)
        self.assertEqual(k.normalize_percent_metric(512, total=1024), 50)
        self.assertIsNone(k.normalize_percent_metric(123456789))
        self.assertIsNone(k.normalize_percent_metric({"used": 2, "total": 1}))

    def test_history_sum_migrates_json_to_sqlite_daily_usage(self):
        k.save_json_atomic(str(self.tmp_path / "history.json"), {
            "days": {
                "2026-06-01": {
                    "n1": {"name": "Node One", "up": 10, "down": 20},
                    "n2": {"name": "Node Two", "up": 5, "down": 7},
                },
                "2026-06-02": {
                    "n1": {"name": "Node One", "up": 3, "down": 4},
                },
            }
        })

        summed = k.history_sum(date(2026, 6, 1), date(2026, 6, 2))

        self.assertEqual(summed["n1"], {"name": "Node One", "up": 13, "down": 24})
        self.assertEqual(summed["n2"], {"name": "Node Two", "up": 5, "down": 7})
        self.assertTrue((self.tmp_path / "traffic.db").exists())

    def test_task_runs_record_and_filter(self):
        k.record_task_run(
            "report",
            "web:composer",
            "success",
            started_at=1000,
            finished_at=1002.5,
            summary="sent",
            metadata={"scope": "today"},
        )
        k.record_task_run("alert", "web:alerts-check", "failed", started_at=1003, finished_at=1004, error="boom")
        k.record_task_run("report", "schedule:daily", "success", started_at=1005, finished_at=1006, summary="scheduled")

        report_runs = k.list_task_runs(limit=10, task_type="report")
        all_runs = k.list_task_runs(limit=10)
        second_run = k.list_task_runs(limit=1, offset=1)

        self.assertEqual(len(report_runs), 2)
        self.assertEqual(report_runs[0]["summary"], "scheduled")
        self.assertEqual(report_runs[1]["summary"], "sent")
        self.assertEqual(report_runs[1]["metadata"]["scope"], "today")
        self.assertEqual(report_runs[1]["duration_ms"], 2500)
        self.assertEqual(len(all_runs), 3)
        self.assertEqual(all_runs[0]["summary"], "scheduled")
        self.assertEqual(second_run[0]["status"], "failed")
        self.assertEqual(k.count_task_runs(), 3)
        self.assertEqual(k.count_task_runs(task_type="report"), 2)
        self.assertEqual(k.count_task_runs(source_prefix="web:"), 2)

    def test_task_run_prune_and_vacuum_keep_traffic_rollups(self):
        k.upsert_daily_usage("2026-06-01", {
            "n1": {"name": "Node One", "up": 10, "down": 20},
        }, source="test")
        k.record_task_run("report", "old", "success", started_at=1000, finished_at=1001, summary="old")
        k.record_task_run("report", "recent", "success", started_at=150000, finished_at=150001, summary="recent")

        status = k.traffic_db_maintenance_status(retention_days=1, now_ts=200000)
        result = k.prune_task_runs(retention_days=1, now_ts=200000)
        vacuum = k.vacuum_traffic_db()

        self.assertEqual(status["old_task_runs"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(k.list_task_runs(limit=10)[0]["summary"], "recent")
        self.assertEqual(k.aggregate_daily_usage(date(2026, 6, 1), date(2026, 6, 1))["n1"]["down"], 20)
        self.assertEqual(vacuum["table_counts"]["node_daily_usage"], 1)
        self.assertIn("after_size_human", vacuum)

    def test_traffic_db_healthcheck_reports_counts_and_rejects_bad_quick_check(self):
        k.record_task_run("sample", "test", "success", started_at=1000, finished_at=1001)

        status = k.traffic_db_healthcheck()

        self.assertTrue(status["ok"])
        self.assertEqual(status["quick_check"], "ok")
        self.assertEqual(status["task_runs"], 1)

    def test_prunes_traffic_segments_with_snapshot_retention(self):
        old_start = 1000
        old_end = 1300
        recent_start = old_start + 2 * 86400
        recent_end = recent_start + 300
        k.save_traffic_segments([
            {"sample_from_ts": old_start, "sample_to_ts": old_end, "nodes": {"n1": {"name": "Old", "up": 1, "down": 2}}},
            {"sample_from_ts": recent_start, "sample_to_ts": recent_end, "nodes": {"n1": {"name": "Recent", "up": 3, "down": 4}}},
        ])

        deleted = k.prune_traffic_segments(retention_days=1, now_ts=old_start + 2 * 86400)

        self.assertEqual(deleted, 1)
        rows = k.traffic_segment_rows_between(0, recent_end + 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Recent")

    def test_prunes_node_daily_usage_by_retention_window(self):
        k.upsert_daily_usage("2025-01-01", {"old": {"name": "Old", "up": 1, "down": 1}}, source="test")
        k.upsert_daily_usage("2026-06-01", {"new": {"name": "New", "up": 2, "down": 2}}, source="test")

        result = k.prune_node_daily_usage(retention_days=365, today_value=date(2026, 6, 12))

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["remaining"], 1)
        self.assertNotIn("old", k.aggregate_daily_usage(date(2025, 1, 1), date(2025, 1, 1)))
        self.assertIn("new", k.aggregate_daily_usage(date(2026, 6, 1), date(2026, 6, 1)))

    def test_sample_worker_maintenance_due_after_scheduled_hours(self):
        monday = datetime(2026, 6, 8, 2, 0, tzinfo=k.TZ)
        sunday_before_vacuum = datetime(2026, 6, 14, 2, 30, tzinfo=k.TZ)
        sunday_after_vacuum = datetime(2026, 6, 14, 3, 0, tzinfo=k.TZ)

        self.assertEqual(k.sample_worker_maintenance_due(monday, None, None), (True, False))
        self.assertEqual(k.sample_worker_maintenance_due(monday, monday.date(), None), (False, False))
        self.assertEqual(k.sample_worker_maintenance_due(sunday_before_vacuum, None, None), (True, False))
        self.assertEqual(k.sample_worker_maintenance_due(sunday_after_vacuum, None, None), (True, True))

    def test_period_rollups_legacy_table_can_be_dropped(self):
        k.init_traffic_db()
        with k.traffic_db_session() as conn:
            conn.execute(
                """
                CREATE TABLE period_rollups (
                  period_type TEXT NOT NULL,
                  period_key TEXT NOT NULL,
                  uuid TEXT NOT NULL,
                  name TEXT NOT NULL,
                  up INTEGER NOT NULL DEFAULT 0,
                  down INTEGER NOT NULL DEFAULT 0,
                  total INTEGER NOT NULL DEFAULT 0,
                  updated_at INTEGER NOT NULL,
                  PRIMARY KEY (period_type, period_key, uuid)
                )
                """
            )
            conn.execute(
                "INSERT INTO period_rollups(period_type, period_key, uuid, name, up, down, total, updated_at) VALUES('daily','2026-06-01','n1','Node',1,2,3,1000)"
            )

        before = k.period_rollups_table_status()
        result = k.drop_period_rollups_table()
        after = k.period_rollups_table_status()

        self.assertTrue(before["exists"])
        self.assertEqual(before["rows"], 1)
        self.assertTrue(result["dropped"])
        self.assertFalse(after["exists"])

        class BadConn:
            def execute(self, _sql):
                return self

            def fetchone(self):
                return ["database disk image is malformed"]

        with patch.object(k, "init_traffic_db", lambda: None), patch.object(k, "traffic_db_session") as session_factory:
            session_factory.return_value.__enter__.return_value = BadConn()
            with self.assertRaises(RuntimeError):
                k.traffic_db_healthcheck()

    def test_runtime_config_save_load_and_validate(self):
        saved = k.save_runtime_config({
            "bot_instance_name": "edge-prod",
            "komari_base_url": "https://komari-new.example/",
            "telegram_chat_id": "987654321",
            "telegram_alert_chat_id": "123123123",
            "ai_api_base": "https://ai-new.example/v1/",
            "ai_model": "gpt-5.4-mini",
            "top_n": 6,
            "komari_timeout_seconds": 20,
            "komari_fetch_workers": 8,
            "sample_interval_seconds": 120,
            "sample_retention_hours": 24,
            "traffic_snapshot_retention_days": 120,
            "ai_pack_cache_ttl_seconds": 1800,
            "task_run_retention_days": 45,
            "alerts_enabled": False,
            "alert_recovery_notify": False,
            "alert_cooldown_seconds": 600,
            "alert_window_minutes": 30,
            "alert_node_missing_samples": 3,
            "alert_silence_windows": "23:00-07:00",
            "alert_total_window_bytes": "2GiB",
        })

        self.assertEqual(saved["bot_instance_name"], "edge-prod")
        self.assertEqual(saved["komari_base_url"], "https://komari-new.example")
        self.assertEqual(saved["alert_total_window_bytes"], 2 * 1024 ** 3)
        self.assertEqual(k.KOMARI_BASE_URL, "https://komari-new.example")
        self.assertEqual(k.TELEGRAM_CHAT_ID, "987654321")
        self.assertEqual(k.AI_API_BASE, "https://ai-new.example/v1")
        self.assertEqual(k.AI_MODEL, "gpt-5.4-mini")
        self.assertEqual(k.TOP_N, 6)
        self.assertEqual(k.TIMEOUT, 20)
        self.assertEqual(k.TRAFFIC_SNAPSHOT_RETENTION_DAYS, 120)
        self.assertFalse(k.ALERTS_ENABLED)
        self.assertEqual(k.ALERT_SILENCE_WINDOWS, "23:00-07:00")
        self.assertEqual(k.ALERT_TOTAL_WINDOW_BYTES, 2 * 1024 ** 3)
        self.assertEqual(k.load_runtime_config()["task_run_retention_days"], 45)
        self.assertEqual(k.load_runtime_config()["traffic_snapshot_retention_days"], 120)
        current = k.current_runtime_config()
        self.assertEqual(current["values"]["ai_pack_cache_ttl_seconds"], 1800)
        self.assertEqual(current["values"]["traffic_snapshot_retention_days"], 120)
        self.assertEqual(current["values"]["alert_total_window_bytes"], 2 * 1024 ** 3)
        self.assertTrue(str(self.tmp_path) in current["path"])
        fields = {item["key"]: item for item in current["editable"]}
        self.assertEqual(fields["alerts_enabled"]["type"], "boolean")
        self.assertEqual(fields["alert_total_window_bytes"]["type"], "bytes")
        self.assertEqual(fields["alert_total_window_bytes"]["value"], "2.00 GiB")

        with self.assertRaises(RuntimeError):
            k.validate_runtime_config({"top_n": 0})
        with self.assertRaises(RuntimeError):
            k.validate_runtime_config({"alert_silence_windows": "bad"})
        with self.assertRaises(RuntimeError):
            k.validate_runtime_config({"alert_total_window_bytes": "10XB"})
        with self.assertRaises(RuntimeError):
            k.validate_runtime_config({"telegram_chat_id": "bad id"})

    def test_config_validation_allows_sampling_without_telegram(self):
        self.patch_attr("KOMARI_BASE_URL", "https://komari.example")
        self.patch_attr("TELEGRAM_BOT_TOKEN", "")
        self.patch_attr("TELEGRAM_CHAT_ID", "")

        k.validate_config_or_raise()

    def test_traffic_range_summary_aggregates_daily_and_weekly(self):
        k.upsert_daily_usage("2026-06-01", {
            "n1": {"name": "Node One", "up": 10, "down": 20},
            "n2": {"name": "Node Two", "up": 5, "down": 7},
        }, source="test")
        k.upsert_daily_usage("2026-06-02", {
            "n1": {"name": "Node One", "up": 3, "down": 4},
        }, source="test")

        daily = k.traffic_range_summary(date(2026, 6, 1), date(2026, 6, 2), group="daily")
        weekly = k.traffic_range_summary(date(2026, 6, 1), date(2026, 6, 2), group="weekly")

        self.assertEqual(daily["total"]["total"], 49)
        self.assertEqual(daily["nodes"][0]["uuid"], "n1")
        self.assertEqual(len(daily["groups"]), 2)
        self.assertEqual(weekly["groups"][0]["key"], "2026-06-01")
        self.assertEqual(weekly["groups"][0]["total"]["total"], 49)

    def test_traffic_range_summary_fills_missing_days_from_snapshots(self):
        day6 = date(2026, 6, 6)
        day7 = date(2026, 6, 7)
        day8 = date(2026, 6, 8)
        day9 = date(2026, 6, 9)

        k.save_traffic_snapshot(int(k.start_of_day(day6).timestamp()), {
            "n1": {"name": "Node One", "up": 0, "down": 0},
        })
        k.save_traffic_snapshot(int(k.start_of_day(day7).timestamp()), {
            "n1": {"name": "Node One", "up": 10, "down": 20},
        })
        k.save_traffic_snapshot(int(k.start_of_day(day8).timestamp()), {
            "n1": {"name": "Node One", "up": 13, "down": 27},
        })
        k.save_traffic_snapshot(int(k.start_of_day(day9).timestamp()), {
            "n1": {"name": "Node One", "up": 20, "down": 40},
        })
        k.save_traffic_snapshot(int(k.start_of_day(day9).timestamp()) + 12 * 3600, {
            "n1": {"name": "Node One", "up": 25, "down": 50},
        })

        with patch.object(k, "today_date", return_value=day9), patch.object(
            k,
            "now_dt",
            return_value=datetime(2026, 6, 9, 12, 0, tzinfo=k.TZ),
        ):
            result = k.traffic_range_summary(day6, day9, group="daily")

        self.assertEqual(result["source"], "traffic_segments")
        self.assertEqual(result["source_parts"], ["traffic_segments"])
        self.assertEqual(result["day_count"], 4)
        self.assertEqual(result["days"], ["2026-06-06", "2026-06-07", "2026-06-08", "2026-06-09"])
        self.assertEqual(result["snapshot_days"], 4)
        self.assertEqual(result["missing_days"], [])
        self.assertEqual(result["total"]["total"], 75)
        self.assertEqual([group["key"] for group in result["groups"]], result["days"])

    def test_traffic_range_summary_prefers_snapshots_over_existing_rollup(self):
        day = date(2026, 6, 1)
        next_day = date(2026, 6, 2)
        k.upsert_daily_usage("2026-06-01", {
            "n1": {"name": "Node One", "up": 999, "down": 999},
        }, source="stale_report")
        k.save_traffic_snapshot(int(k.start_of_day(day).timestamp()), {
            "n1": {"name": "Node One", "up": 100, "down": 200},
        })
        k.save_traffic_snapshot(int(k.start_of_day(next_day).timestamp()), {
            "n1": {"name": "Node One", "up": 130, "down": 260},
        })

        with patch.object(k, "today_date", return_value=next_day), patch.object(
            k,
            "now_dt",
            return_value=datetime(2026, 6, 2, 0, 0, tzinfo=k.TZ),
        ):
            result = k.traffic_range_summary(day, day, group="daily")

        self.assertEqual(result["source"], "traffic_segments")
        self.assertEqual(result["source_parts"], ["traffic_segments"])
        self.assertEqual(result["rollup_days"], 0)
        self.assertEqual(result["snapshot_days"], 1)
        self.assertEqual(result["total"]["total"], 90)

    def test_snapshot_range_usage_aggregates_adjacent_deltas(self):
        k.save_traffic_snapshot(1000, {
            "n1": {"name": "Node One", "up": 10, "down": 20},
            "n2": {"name": "Node Two", "up": 5, "down": 5},
        })
        k.save_traffic_snapshot(1300, {
            "n1": {"name": "Node One", "up": 15, "down": 35},
            "n2": {"name": "Node Two", "up": 1, "down": 2},
        })
        k.save_traffic_snapshot(1600, {
            "n1": {"name": "Node One", "up": 20, "down": 45},
            "n2": {"name": "Node Two", "up": 4, "down": 8},
        })

        usage = k.snapshot_range_usage(1000, 1600)

        self.assertEqual(usage["source"], "traffic_segments")
        self.assertEqual(usage["sample_count"], 3)
        self.assertEqual(usage["nodes"]["n1"]["up"], 10)
        self.assertEqual(usage["nodes"]["n1"]["down"], 25)
        self.assertEqual(usage["nodes"]["n2"]["up"], 3)
        self.assertEqual(usage["nodes"]["n2"]["down"], 6)
        self.assertIn("Node Two(counter_reset)", usage["reset_warnings"])

    def test_snapshot_range_usage_prorates_window_edges(self):
        k.save_traffic_snapshot(1000, {
            "n1": {"name": "Node One", "up": 0, "down": 0},
        })
        k.save_traffic_snapshot(1100, {
            "n1": {"name": "Node One", "up": 100, "down": 200},
        })

        usage = k.snapshot_range_usage(1025, 1075)

        self.assertEqual(usage["sample_count"], 2)
        self.assertEqual(usage["segment_count"], 1)
        self.assertEqual(usage["from_ts"], 1025)
        self.assertEqual(usage["to_ts"], 1075)
        self.assertEqual(usage["nodes"]["n1"]["up"], 50)
        self.assertEqual(usage["nodes"]["n1"]["down"], 100)

    def test_last_hours_struct_uses_sqlite_snapshots_and_ignores_reset_absolute_value(self):
        self.patch_attr("take_sample_if_due", lambda **_kwargs: None)
        with patch.object(k.time, "time", return_value=1600):
            k.save_traffic_snapshot(1000, {
                "n1": {"name": "Node One", "up": 100, "down": 200},
            })
            k.save_traffic_snapshot(1300, {
                "n1": {"name": "Node One", "up": 110, "down": 230},
            })
            k.save_traffic_snapshot(1600, {
                "n1": {"name": "Node One", "up": 2, "down": 3},
            })

            result = k.get_last_hours_nodes_struct(1)
            top = k.get_top_last_hours_struct(1, 3)

        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["nodes"][0]["up"], 10)
        self.assertEqual(result["nodes"][0]["down"], 30)
        self.assertIn("Node One(counter_reset)", result["reset_warnings"])
        self.assertEqual(top["nodes"][0]["total"], 40)

    def test_records_summary_uses_sqlite_snapshots_not_komari_records(self):
        self.patch_attr("take_sample_if_due", lambda **_kwargs: None)
        self.patch_attr("fetch_node_records", lambda *_args, **_kwargs: self.fail("Komari records API should not be used for traffic totals"))
        self.patch_attr("get_json", lambda *_args, **_kwargs: self.fail("Komari nodes API should not be required for traffic totals"))

        now_ts = 100000
        from_ts = now_ts - 24 * 3600
        k.save_traffic_snapshot(from_ts, {
            "n1": {"name": "Node One", "up": 100, "down": 200},
            "n2": {"name": "Node Two", "up": 10, "down": 20},
        })
        k.save_traffic_snapshot(now_ts, {
            "n1": {"name": "Node One", "up": 160, "down": 280},
            "n2": {"name": "Node Two", "up": 40, "down": 70},
        })

        with patch.object(k.time, "time", return_value=now_ts):
            result = k.build_records_summary(24)

        by_uuid = {node["uuid"]: node for node in result["nodes"]}
        self.assertEqual(result["source"], "traffic_segments")
        self.assertEqual(result["source_parts"], ["traffic_segments"])
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(by_uuid["n1"]["up"], 60)
        self.assertEqual(by_uuid["n1"]["down"], 80)
        self.assertEqual(by_uuid["n2"]["total"], 80)
        self.assertEqual(result["total"]["total"], 220)
        self.assertEqual(result["note"], "snapshot_window")

    def test_compute_traffic_from_records_accepts_metric_aliases(self):
        result = k.compute_traffic_from_records([
            {
                "time": "2026-06-06T00:00:00Z",
                "net_total_up": 0,
                "net_total_down": 0,
                "metrics": {
                    "cpu_usage_percent": 10,
                    "memory": {"used": 1, "total": 4},
                    "hdd": {"usedPercent": 80},
                },
            },
            {
                "time": "2026-06-06T01:00:00Z",
                "net_total_up": 4,
                "net_total_down": 5,
                "status": {
                    "cpu": {"usagePercent": 20},
                    "mem_percent": 50,
                    "storage_percent": 60,
                },
            },
        ])

        self.assertEqual(result["total"], 9)
        self.assertEqual(result["cpu"]["avg"], 15)
        self.assertEqual(result["ram"]["avg"], 37.5)
        self.assertEqual(result["disk"]["avg"], 70)

    def test_hourly_by_node_summary_uses_sqlite_snapshots(self):
        k.save_traffic_snapshot(1000, {
            "n1": {"name": "Node One", "up": 10, "down": 20},
        })
        k.save_traffic_snapshot(3700, {
            "n1": {"name": "Node One", "up": 15, "down": 25},
        })

        result = k.build_snapshot_hourly_by_node_summary(1000, 3700, label_date=date(1970, 1, 1))

        self.assertEqual(result["source"], "traffic_snapshots")
        self.assertEqual(result["nodes"][0]["uuid"], "n1")
        self.assertEqual(result["nodes"][0]["total"], 10)
        self.assertEqual(result["nodes"][0]["hours"][0]["total"], 10)

    def test_hourly_summary_splits_snapshot_segments_across_hour_boundaries(self):
        k.save_traffic_snapshot(3600, {
            "n1": {"name": "Node One", "up": 0, "down": 0},
        })
        k.save_traffic_snapshot(10800, {
            "n1": {"name": "Node One", "up": 120, "down": 0},
        })

        total = k.build_snapshot_hourly_total_summary(3600, 10800)
        by_node = k.build_snapshot_hourly_by_node_summary(3600, 10800, label_date=date(1970, 1, 1))

        self.assertEqual([item["total"] for item in total["hours"]], [60, 60])
        self.assertEqual([item["total"] for item in by_node["nodes"][0]["hours"]], [60, 60])

    def test_take_sample_writes_sqlite_snapshots(self):
        samples = [
            [k.NodeTotal(uuid="n1", name="Node One", up=10, down=20)],
            [k.NodeTotal(uuid="n1", name="Node One", up=15, down=35)],
        ]
        self.patch_attr("fetch_nodes_and_totals", lambda: (samples.pop(0), []))
        with patch.object(k.time, "time", return_value=1000):
            k.take_sample_if_due(force=True, record=False)
        with patch.object(k.time, "time", return_value=1300):
            k.take_sample_if_due(force=True, record=False)

        usage = k.snapshot_range_usage(1000, 1300)

        self.assertEqual(usage["nodes"]["n1"]["up"], 5)
        self.assertEqual(usage["nodes"]["n1"]["down"], 15)
        self.assertEqual(k.traffic_segments_count(), 1)
        daily = k.aggregate_daily_usage(date(1970, 1, 1), date(1970, 1, 1))["n1"]
        self.assertEqual(daily["up"], 5)
        self.assertEqual(daily["down"], 15)

    def test_segment_materialization_splits_cross_midnight(self):
        day1 = date(2026, 6, 1)
        day2 = date(2026, 6, 2)
        start_ts = int((k.start_of_day(day2) - timedelta(minutes=30)).timestamp())
        end_ts = int((k.start_of_day(day2) + timedelta(minutes=30)).timestamp())
        k.save_traffic_snapshot(start_ts, {
            "n1": {"name": "Node One", "up": 0, "down": 0},
        })
        k.save_traffic_snapshot(end_ts, {
            "n1": {"name": "Node One", "up": 120, "down": 60},
        })

        k.rebuild_traffic_segments_from_snapshots()

        day1_usage = k.aggregate_daily_usage(day1, day1)["n1"]
        day2_usage = k.aggregate_daily_usage(day2, day2)["n1"]
        self.assertEqual(day1_usage["up"], 60)
        self.assertEqual(day1_usage["down"], 30)
        self.assertEqual(day2_usage["up"], 60)
        self.assertEqual(day2_usage["down"], 30)

    def test_live_week_uses_history_for_6_7_8_and_live_today(self):
        day6 = date(2026, 6, 6)
        day7 = date(2026, 6, 7)
        day8 = date(2026, 6, 8)
        day9 = date(2026, 6, 9)
        for day, total in ((day6, 10), (day7, 20), (day8, 30)):
            k.upsert_daily_usage(day.strftime("%Y-%m-%d"), {
                "n1": {"name": "Node One", "up": total, "down": 0},
            }, source="history_json")

        start_ts = int(k.start_of_day(day9).timestamp())
        now = datetime(2026, 6, 9, 12, 0, tzinfo=k.TZ)
        k.save_traffic_snapshot(start_ts, {
            "n1": {"name": "Node One", "up": 100, "down": 0},
        })
        k.save_traffic_snapshot(int(now.timestamp()), {
            "n1": {"name": "Node One", "up": 140, "down": 0},
        })

        with patch.object(k, "today_date", return_value=day9), patch.object(k, "now_dt", return_value=now):
            today = k.build_live_period_struct(k.start_of_day(day9), now)
            week = k.build_live_period_struct(k.start_of_day(day8), now)
            month = k.build_live_period_struct(k.start_of_day(date(2026, 6, 1)), now)

        self.assertEqual(today["total"]["total"], 40)
        self.assertEqual(week["total"]["total"], 70)
        self.assertEqual(month["total"]["total"], 100)
        self.assertNotEqual(today["total"]["total"], week["total"]["total"])
        self.assertNotEqual(week["total"]["total"], month["total"]["total"])
        self.assertEqual(week["source"], "traffic_db+traffic_segments")
        self.assertEqual(week["rollup_days"], 1)
        self.assertEqual(week["snapshot_days"], 1)
        self.assertIn("2026-06-08", week["coverage_days"])
        self.assertIn("2026-06-09", week["coverage_days"])

    def test_live_period_marks_missing_days_when_only_today_samples_exist(self):
        day9 = date(2026, 6, 9)
        now = datetime(2026, 6, 9, 12, 0, tzinfo=k.TZ)
        k.save_traffic_snapshot(int(k.start_of_day(day9).timestamp()), {
            "n1": {"name": "Node One", "up": 100, "down": 0},
        })
        k.save_traffic_snapshot(int(now.timestamp()), {
            "n1": {"name": "Node One", "up": 120, "down": 0},
        })

        with patch.object(k, "today_date", return_value=day9), patch.object(k, "now_dt", return_value=now):
            week = k.build_live_period_struct(k.start_of_day(date(2026, 6, 8)), now)

        self.assertEqual(week["total"]["total"], 20)
        self.assertEqual(week["coverage_days"], ["2026-06-09"])
        self.assertEqual(week["missing_days"], ["2026-06-08"])

    def test_live_period_uses_realtime_snapshots_without_daily_rollup(self):
        k.upsert_daily_usage("2026-06-01", {
            "n1": {"name": "Node One", "up": 999, "down": 999},
        }, source="stale_report")
        k.save_traffic_snapshot(int(k.start_of_day(date(2026, 6, 1)).timestamp()), {
            "n1": {"name": "Node One", "up": 100, "down": 200},
        })
        k.save_traffic_snapshot(int(k.start_of_day(date(2026, 6, 2)).timestamp()), {
            "n1": {"name": "Node One", "up": 130, "down": 260},
        })

        with patch.object(k, "today_date", return_value=date(2026, 6, 2)):
            period = k.build_live_period_struct(
                datetime(2026, 6, 1, tzinfo=k.TZ),
                datetime(2026, 6, 2, tzinfo=k.TZ),
            )

        by_uuid = {item["uuid"]: item for item in period["nodes"]}
        self.assertEqual(period["source"], "traffic_segments")
        self.assertEqual(period["source_parts"], ["traffic_segments"])
        self.assertEqual(period["rollup_days"], 0)
        self.assertEqual(by_uuid["n1"]["up"], 30)
        self.assertEqual(by_uuid["n1"]["down"], 60)
        self.assertEqual(period["total"]["total"], 90)

    def test_live_week_falls_back_to_completed_day_snapshots_when_rollup_missing(self):
        monday = date(2026, 6, 1)
        tuesday = date(2026, 6, 2)
        mon_start = int(k.start_of_day(monday).timestamp())
        tue_start = int(k.start_of_day(tuesday).timestamp())
        tue_one = tue_start + 3600

        k.save_traffic_snapshot(mon_start, {
            "n1": {"name": "Node One", "up": 0, "down": 0},
        })
        k.save_traffic_snapshot(tue_start, {
            "n1": {"name": "Node One", "up": 10, "down": 20},
        })
        k.save_traffic_snapshot(tue_one, {
            "n1": {"name": "Node One", "up": 15, "down": 25},
        })

        with patch.object(k, "today_date", return_value=tuesday):
            today = k.build_live_period_struct(k.start_of_day(tuesday), datetime(2026, 6, 2, 1, 0, tzinfo=k.TZ))
            week = k.build_live_period_struct(k.start_of_day(monday), datetime(2026, 6, 2, 1, 0, tzinfo=k.TZ))

        self.assertEqual(today["total"]["total"], 10)
        self.assertEqual(week["total"]["total"], 40)
        self.assertEqual(week["snapshot_days"], 2)
        self.assertEqual(week["missing_days"], [])

    def test_scheduled_report_period_parts_uses_previous_complete_period(self):
        # 2026-06-10 是周三；上一完整日/周/月分别为 06-09、06-01 起的一周、5 月
        self.patch_attr("today_date", lambda: date(2026, 6, 10))

        d_start, d_end, d_tag = k.scheduled_report_period_parts("daily")
        self.assertEqual(d_start, datetime(2026, 6, 9, 0, 0, tzinfo=k.TZ))
        self.assertEqual(d_end, datetime(2026, 6, 10, 0, 0, tzinfo=k.TZ))
        self.assertEqual(d_tag, "2026-06-09")

        w_start, w_end, w_tag = k.scheduled_report_period_parts("weekly")
        self.assertEqual(w_start, datetime(2026, 6, 1, 0, 0, tzinfo=k.TZ))
        self.assertEqual(w_end, datetime(2026, 6, 8, 0, 0, tzinfo=k.TZ))
        self.assertEqual(w_tag, "WEEK-2026-06-01")

        m_start, m_end, m_tag = k.scheduled_report_period_parts("monthly")
        self.assertEqual(m_start, datetime(2026, 5, 1, 0, 0, tzinfo=k.TZ))
        self.assertEqual(m_end, datetime(2026, 6, 1, 0, 0, tzinfo=k.TZ))
        self.assertEqual(m_tag, "MONTH-2026-05-01")

        with self.assertRaises(RuntimeError):
            k.scheduled_report_period_parts("hourly")

    def test_scheduled_report_period_parts_crosses_year_and_month_boundaries(self):
        # 2026-01-01 是周四；上一日/周/月均落在 2025 年
        self.patch_attr("today_date", lambda: date(2026, 1, 1))

        d_start, d_end, d_tag = k.scheduled_report_period_parts("daily")
        self.assertEqual(d_start, datetime(2025, 12, 31, 0, 0, tzinfo=k.TZ))
        self.assertEqual(d_end, datetime(2026, 1, 1, 0, 0, tzinfo=k.TZ))
        self.assertEqual(d_tag, "2025-12-31")

        w_start, w_end, w_tag = k.scheduled_report_period_parts("weekly")
        self.assertEqual(w_start, datetime(2025, 12, 22, 0, 0, tzinfo=k.TZ))
        self.assertEqual(w_end, datetime(2025, 12, 29, 0, 0, tzinfo=k.TZ))
        self.assertEqual(w_tag, "WEEK-2025-12-22")

        m_start, m_end, m_tag = k.scheduled_report_period_parts("monthly")
        self.assertEqual(m_start, datetime(2025, 12, 1, 0, 0, tzinfo=k.TZ))
        self.assertEqual(m_end, datetime(2026, 1, 1, 0, 0, tzinfo=k.TZ))
        self.assertEqual(m_tag, "MONTH-2025-12-01")

    def test_builtin_reports_delegate_to_scope_report_message(self):
        calls = []
        self.patch_attr("build_scope_report_message", lambda scope, top_only=False: calls.append((scope, top_only)) or f"msg:{scope}")
        sent = []
        self.patch_attr("telegram_send", sent.append)

        k.run_daily_send_yesterday()
        k.run_weekly_send_last_week()
        k.run_monthly_send_last_month()

        self.assertEqual(calls, [("daily", False), ("weekly", False), ("monthly", False)])
        self.assertEqual(sent, ["msg:daily", "msg:weekly", "msg:monthly"])

    def test_scope_report_period_label_formats(self):
        self.patch_attr("today_date", lambda: date(2026, 6, 10))

        d_start, d_end, d_tag = k.scheduled_report_period_parts("daily")
        self.assertEqual(k.scope_report_period_label("daily", d_start, d_end, d_tag), "2026-06-09")

        w_start, w_end, w_tag = k.scheduled_report_period_parts("weekly")
        self.assertEqual(k.scope_report_period_label("weekly", w_start, w_end, w_tag), "2026-06-01 → 2026-06-07")

        m_start, m_end, m_tag = k.scheduled_report_period_parts("monthly")
        self.assertEqual(k.scope_report_period_label("monthly", m_start, m_end, m_tag), "2026-05-01 → 2026-05-31")

    def test_query_usage_falls_back_to_rollup_when_segments_missing(self):
        # 场景：只有今天的 segments，但请求最近 7 天
        day7 = date(2026, 6, 7)
        day8 = date(2026, 6, 8)
        day9 = date(2026, 6, 9)

        # 历史 rollup 数据（6-7, 6-8）
        k.upsert_daily_usage("2026-06-07", {
            "n1": {"name": "Node One", "up": 100, "down": 200},
        }, source="history_json")
        k.upsert_daily_usage("2026-06-08", {
            "n1": {"name": "Node One", "up": 150, "down": 250},
        }, source="history_json")

        # 今天的 segments（6-9）
        start_ts = int(k.start_of_day(day9).timestamp())
        now_ts = start_ts + 12 * 3600
        k.save_traffic_snapshot(start_ts, {"n1": {"name": "Node One", "up": 1000, "down": 2000}})
        k.save_traffic_snapshot(now_ts, {"n1": {"name": "Node One", "up": 1050, "down": 2100}})

        with patch.object(k.time, "time", return_value=now_ts):
            # 请求最近 7 天
            usage = k.query_usage(start_ts - 2 * 86400, now_ts)

        self.assertIn("node_daily_usage", usage["source_parts"])
        self.assertIn("traffic_segments", usage["source_parts"])
        self.assertEqual(usage["nodes"]["n1"]["up"], 100 + 150 + 50)  # rollup + rollup + segment
        self.assertEqual(usage["nodes"]["n1"]["down"], 200 + 250 + 100)

    def test_query_usage_different_ranges_return_different_results(self):
        # 场景：segments 只覆盖 1 天，rollup 覆盖更长历史
        day1 = date(2026, 6, 1)
        day7 = date(2026, 6, 7)

        for i in range(1, 7):
            d = date(2026, 6, i)
            k.upsert_daily_usage(d.strftime("%Y-%m-%d"), {
                "n1": {"name": "Node One", "up": i * 10, "down": i * 20},
            }, source="history_json")

        day7_start = int(k.start_of_day(day7).timestamp())
        now_ts = day7_start + 12 * 3600
        k.save_traffic_snapshot(day7_start, {"n1": {"name": "Node One", "up": 1000, "down": 2000}})
        k.save_traffic_snapshot(now_ts, {"n1": {"name": "Node One", "up": 1070, "down": 2140}})

        with patch.object(k.time, "time", return_value=now_ts):
            usage_24h = k.query_usage(now_ts - 24 * 3600, now_ts)
            usage_7d = k.query_usage(now_ts - 7 * 86400, now_ts)
            usage_30d = k.query_usage(now_ts - 30 * 86400, now_ts)

        # 24h：6-6 12:00 → 6-7 12:00，覆盖 6-6 的 rollup(60) + 6-7 的 segment(70)
        self.assertEqual(usage_24h["nodes"]["n1"]["up"], 60 + 70)
        self.assertEqual(usage_24h["nodes"]["n1"]["down"], 120 + 140)

        # 7d：6-1 到 6-6 的 rollup（6 天）+ 6-7 的 segment
        self.assertEqual(usage_7d["nodes"]["n1"]["up"], (10 + 20 + 30 + 40 + 50 + 60) + 70)
        self.assertEqual(usage_7d["nodes"]["n1"]["down"], (20 + 40 + 60 + 80 + 100 + 120) + 140)

        # 30d：和 7d 相同（只有 6 天历史 + 今天）
        self.assertEqual(usage_30d["nodes"]["n1"]["up"], usage_7d["nodes"]["n1"]["up"])
        self.assertEqual(usage_30d["nodes"]["n1"]["down"], usage_7d["nodes"]["n1"]["down"])

        # 关键：24h 和 7d 不同
        self.assertNotEqual(usage_24h["nodes"]["n1"]["up"] + usage_24h["nodes"]["n1"]["down"],
                           usage_7d["nodes"]["n1"]["up"] + usage_7d["nodes"]["n1"]["down"])

    def test_query_usage_includes_metadata_fields(self):
        day = date(2026, 6, 10)
        now_ts = int(k.start_of_day(day).timestamp()) + 12 * 3600
        from_ts = now_ts - 24 * 3600
        k.save_traffic_snapshot(from_ts, {"n1": {"name": "Node One", "up": 100, "down": 200}})
        k.save_traffic_snapshot(now_ts, {"n1": {"name": "Node One", "up": 150, "down": 300}})

        with patch.object(k.time, "time", return_value=now_ts):
            usage = k.query_usage(from_ts, now_ts, group_by="node")

        self.assertEqual(usage["from_ts"], from_ts)
        self.assertEqual(usage["to_ts"], now_ts)
        self.assertEqual(usage["group_by"], "node")
        self.assertIn("source", usage)
        self.assertIn("source_parts", usage)
        self.assertIsInstance(usage["source_parts"], list)

    def test_counter_reset_does_not_produce_negative_traffic(self):
        # 已有测试覆盖：test_last_hours_struct_uses_sqlite_snapshots_and_ignores_reset_absolute_value
        # 验证 reset 标记
        k.save_traffic_snapshot(1000, {"n1": {"name": "Node One", "up": 100, "down": 200}})
        k.save_traffic_snapshot(1300, {"n1": {"name": "Node One", "up": 50, "down": 80}})

        usage = k.query_usage(1000, 1300)

        self.assertEqual(usage["nodes"]["n1"]["up"], 0)
        self.assertEqual(usage["nodes"]["n1"]["down"], 0)
        self.assertIn("Node One(counter_reset)", usage["reset_warnings"])

    def test_report_schedule_validation_and_due_key(self):
        schedule = k.validate_report_schedule({
            "enabled": True,
            "scope": "weekly",
            "mode": "top",
            "time": "09:30",
            "weekday": 0,
            "month_day": 1,
        })
        now = datetime(2026, 6, 8, 9, 30, tzinfo=k.TZ)

        self.assertEqual(schedule["scope"], "weekly")
        self.assertEqual(k.schedule_due_key(schedule, now), f"{schedule['id']}:2026-06-08 09:30")
        self.assertIsNone(k.schedule_due_key(schedule, datetime(2026, 6, 8, 9, 31, tzinfo=k.TZ)))
        with self.assertRaises(RuntimeError):
            k.validate_report_schedule({"scope": "daily", "mode": "full", "time": "25:00"})
        with self.assertRaises(RuntimeError):
            k.validate_report_schedule({"scope": "weekly", "mode": "full", "time": "09:00", "weekday": 7})
        with self.assertRaises(RuntimeError):
            k.validate_report_schedule({"scope": "monthly", "mode": "full", "time": "09:00", "month_day": 0})

    def test_schedule_next_run_at(self):
        daily = k.validate_report_schedule({"scope": "daily", "mode": "full", "time": "09:00"})
        weekly = k.validate_report_schedule({"scope": "weekly", "mode": "top", "time": "08:30", "weekday": 2})
        monthly = k.validate_report_schedule({"scope": "monthly", "mode": "full", "time": "10:15", "month_day": 6})

        daily_next = k.schedule_next_run_at(daily, datetime(2026, 6, 6, 9, 1, tzinfo=k.TZ))
        weekly_next = k.schedule_next_run_at(weekly, datetime(2026, 6, 6, 7, 0, tzinfo=k.TZ))
        monthly_next = k.schedule_next_run_at(monthly, datetime(2026, 6, 6, 10, 16, tzinfo=k.TZ))

        self.assertEqual(datetime.fromtimestamp(daily_next, k.TZ).strftime("%Y-%m-%d %H:%M"), "2026-06-07 09:00")
        self.assertEqual(datetime.fromtimestamp(weekly_next, k.TZ).strftime("%Y-%m-%d %H:%M"), "2026-06-10 08:30")
        self.assertEqual(datetime.fromtimestamp(monthly_next, k.TZ).strftime("%Y-%m-%d %H:%M"), "2026-07-06 10:15")


if __name__ == "__main__":
    unittest.main()
