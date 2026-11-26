"""接口形状回归测试（纯标准库 unittest）。

目的：锁定各聚合函数的响应字段集合，任何结构变化都必须在这里显式更新断言。
运行：仓库根目录 `python -m unittest discover -s tests -v`
"""

from __future__ import annotations

import csv
import http.client
import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

DAY_MS = 86_400_000

# 期望的键集合（契约）。新增字段可以，但必须同步改这里。
EXPECTED_TOP_KEYS = {
    "aggregate": {
        "ok", "range", "range_label", "generated_at", "db_path", "db_exists",
        "chart_days", "heat_days", "totals", "insights", "days", "heatmap_weeks",
        "hourly", "hourly_models", "weekday_hour", "by_model", "by_project",
        "by_session", "composition",
    },
    "aggregate_agents": {"ok", "range", "range_label", "generated_at", "since", "totals", "by_agent"},
    "first_round_report": {
        "ok", "range", "range_label", "generated_at", "agent", "since", "definition",
        "totals", "buckets", "by_agent", "rounds", "pending",
    },
    "sessions_by_root": {"ok", "range", "range_label", "generated_at", "since", "totals", "by_root"},
    "session_detail": {"ok", "id", "title", "directory", "project_name", "created", "updated", "totals", "models", "turns"},
    "get_day_hourly": {
        "ok", "date", "total", "total_fmt", "turns", "input", "output", "cache", "cache_hit",
        "avg", "avg_fmt", "models", "hourly", "hourly_models", "peak_hour",
    },
    "get_days_hourly": {
        "ok", "dates", "day_count", "total", "total_fmt", "turns", "input", "output", "cache",
        "cache_hit", "avg", "avg_fmt", "models", "hourly", "hourly_models", "peak_hour",
        "by_model", "by_project", "by_session",
    },
}

EXPECTED_TOTALS_KEYS = {
    "aggregate": {
        "tokens", "fmt", "today", "today_fmt", "turns", "input", "output", "reasoning",
        "cache_read", "cache_write", "cache", "cache_hit", "avg_daily", "avg_daily_fmt",
        "avg_per_turn", "avg_per_turn_fmt", "sessions", "projects", "models",
    },
    "aggregate_agents": {"agents", "tokens", "fmt", "turns", "input", "output", "cache_read", "cache_write", "cache_hit"},
    "first_round_report": {"n", "zero", "zero_pct", "avg_hit", "hit_weighted", "tokens", "input", "cache_read", "buckets"},
    "sessions_by_root": {"roots", "raw_sessions", "child_sessions", "tokens", "fmt", "child_tokens"},
    "session_detail": {"tokens", "input", "output", "reasoning", "cache_read", "cache_write", "turns", "fmt", "cache_hit"},
}

CSV_HEADER = [
    "time", "session_id", "title", "project", "directory", "model",
    "input", "output", "reasoning", "cache_read", "cache_write", "total", "agent",
]


def _assistant(created_ms, input_t, output_t, cache_read, cache_write=0, total=None, model="m-test"):
    tok = {"input": input_t, "output": output_t, "cache": {"read": cache_read, "write": cache_write}}
    if total is not None:
        tok["total"] = total
    return json.dumps({"role": "assistant", "modelID": model, "time": {"created": created_ms}, "tokens": tok})


def _zero_assistant(created_ms):
    tok = {"input": 0, "output": 0, "cache": {"read": 0, "write": 0}}
    return json.dumps({"role": "assistant", "modelID": "m-test", "time": {"created": created_ms}, "tokens": tok})


def _user(created_ms):
    return json.dumps({"role": "user", "time": {"created": created_ms}, "text": "hi"})


class FixtureMixin:
    """建最小 schema 的 SQLite 并把 server 指向它。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "mimocode.db"
        self.now_ms = int(time.time() * 1000)
        t_old = self.now_ms - 10 * DAY_MS       # 10 天前：不在 7 天内
        t_recent = self.now_ms - 2 * DAY_MS     # 2 天前：在 7 天内
        t_today = self.now_ms - 60_000          # 1 分钟前：今天
        self.t_old, self.t_recent, self.t_today = t_old, t_recent, t_today

        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT, name TEXT)")
        c.execute(
            "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, "
            "directory TEXT, title TEXT, time_created INTEGER, time_updated INTEGER)"
        )
        c.execute(
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, agent_id TEXT, "
            "time_created INTEGER, time_updated INTEGER, data TEXT)"
        )
        c.execute("INSERT INTO project VALUES ('proj-1', '/proj/a', 'a')")
        sessions = [
            ("ses_root", "proj-1", None, "/proj/a", "Root one", t_old, t_today),
            ("ses_child", "proj-1", "ses_root", "/proj/a", "Child of one", t_today, t_today),
            ("ses_old", "proj-1", None, "/proj/b", "Old one", t_old, t_old),
        ]
        c.executemany("INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)", sessions)
        # fmt: off
        messages = [
            # ses_root / main：user、全 0 壳子、首轮（命中 47%）、第二轮（命中 90%）
            ("m01", "ses_root", "main",     t_recent - 5000, _user(t_recent - 5000)),
            ("m02", "ses_root", "main",     t_recent - 4000, _zero_assistant(t_recent - 4000)),
            ("m03", "ses_root", "main",     t_recent - 3000, _assistant(t_recent - 3000, 1000, 100, 900)),
            ("m04", "ses_root", "main",     t_recent - 2000, _assistant(t_recent - 2000, 500, 50, 4500)),
            # ses_root / general-1：首轮零命中
            ("m05", "ses_root", "general-1", t_recent - 1000, _assistant(t_recent - 1000, 2000, 20, 0)),
            # ses_child / checkpoint-writer-1：今天，首轮命中 90%
            ("m06", "ses_child", "checkpoint-writer-1", t_today, _assistant(t_today, 300, 30, 2700)),
            # ses_old / main：10 天前，带显式 total
            ("m07", "ses_old", "main",      t_old,         _assistant(t_old, 100, 10, 0)),
            ("m08", "ses_old", "main",      t_old + 1000,  _assistant(t_old + 1000, 100, 10, 0, total=1234)),
        ]
        # fmt: on
        c.executemany("INSERT INTO message VALUES (?, ?, ?, ?, ?, ?)", [
            (mid, sid, ag, tc, tc, data) for mid, sid, ag, tc, data in messages
        ])
        conn.commit()
        conn.close()

        self._saved_db = server.MIMO_DB_PATH
        server.MIMO_DB_PATH = db_path
        self._reset_server_cache()

    def tearDown(self):
        server.MIMO_DB_PATH = self._saved_db
        self._reset_server_cache()
        self.tmp.cleanup()
        super().tearDown()

    @staticmethod
    def _reset_server_cache():
        server.cache_clear()
        server._FULL_ITEMS = None
        server._FULL_META = None
        server._FULL_FIRST = None
        server._FULL_PENDING = None
        server._FULL_ITEMS_AT = 0.0

    @property
    def recent_day(self) -> str:
        return server.day_key(self.t_recent)

    @property
    def today_day(self) -> str:
        return server.day_key(self.t_today)


class ApiShapeTest(FixtureMixin, unittest.TestCase):
    def _call(self):
        d = self.recent_day
        return {
            "aggregate": lambda: server.aggregate("7"),
            "aggregate_agents": lambda: server.aggregate_agents("7"),
            "first_round_report": lambda: server.first_round_report("7"),
            "sessions_by_root": lambda: server.sessions_by_root("7"),
            "session_detail": lambda: server.session_detail("ses_root"),
            "get_day_hourly": lambda: server.get_day_hourly(d),
            "get_days_hourly": lambda: server.get_days_hourly([d]),
        }

    def test_top_level_keys(self):
        for name, fn in self._call().items():
            with self.subTest(fn=name):
                self.assertEqual(set(fn().keys()), EXPECTED_TOP_KEYS[name])

    def test_totals_keys(self):
        fns = {
            "aggregate": lambda: server.aggregate("7"),
            "aggregate_agents": lambda: server.aggregate_agents("7"),
            "first_round_report": lambda: server.first_round_report("7"),
            "sessions_by_root": lambda: server.sessions_by_root("7"),
            "session_detail": lambda: server.session_detail("ses_root"),
        }
        for name, fn in fns.items():
            with self.subTest(fn=name):
                self.assertEqual(set(fn()["totals"].keys()), EXPECTED_TOTALS_KEYS[name])

    def test_export_csv_header(self):
        rows = list(csv.reader(io.StringIO(server.export_csv("7"))))
        self.assertEqual(rows[0], CSV_HEADER)
        self.assertEqual(len(rows) - 1, 4)  # 7 天内 4 条有效 assistant

    def test_rank_pct_relative_to_max(self):
        agg = server.aggregate("7")
        for key in ("by_model", "by_project", "by_session"):
            self.assertEqual(agg[key][0]["pct"], 100, key)
        # 第二名相对最大值而非总量：ses_child 3030 / ses_root 9070 = 33%，share 为 25%
        self.assertEqual(agg["by_session"][1]["pct"], 33)
        self.assertEqual(agg["by_session"][1]["share"], 25.0)
        self.assertEqual(server.aggregate_agents("7")["by_agent"][0]["pct"], 100)
        self.assertEqual(server.sessions_by_root("7")["by_root"][0]["pct"], 100)
        # 选中日期模式下的 by_model（经 /api/days-hourly）同样要带 pct
        self.assertEqual(server.aggregate_dates_ranks([self.recent_day])["by_model"][0]["pct"], 100)


class ParseTokensTest(FixtureMixin, unittest.TestCase):
    def test_user_role_ignored(self):
        self.assertIsNone(server.parse_tokens(_user(1)))

    def test_invalid_json_ignored(self):
        self.assertIsNone(server.parse_tokens("not json"))
        self.assertIsNone(server.parse_tokens(""))
        self.assertIsNone(server.parse_tokens(None))

    def test_zero_assistant_dropped_by_default(self):
        self.assertIsNone(server.parse_tokens(_zero_assistant(1)))

    def test_zero_assistant_kept_with_flag(self):
        parsed = server.parse_tokens(_zero_assistant(1), keep_zero=True)
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed["zero"])
        self.assertEqual(parsed["total"], 0)

    def test_normal_message_zero_flag_false(self):
        parsed = server.parse_tokens(_assistant(1, 10, 2, 5), keep_zero=True)
        self.assertFalse(parsed["zero"])
        self.assertEqual(parsed["total"], 17)

    def test_total_field_preferred(self):
        parsed = server.parse_tokens(_assistant(1, 10, 2, 5, total=999))
        self.assertEqual(parsed["total"], 999)

    def test_model_fallbacks(self):
        d = json.loads(_assistant(1, 1, 1, 0))
        d.pop("modelID")
        self.assertEqual(server.parse_tokens(json.dumps(d))["model"], "unknown")


class RangeWindowTest(FixtureMixin, unittest.TestCase):
    def test_all_is_zero(self):
        self.assertEqual(server.range_since_ms("all"), 0)

    def test_today_is_local_midnight(self):
        expected = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        self.assertEqual(server.range_since_ms("today"), expected)

    def test_n_days_back(self):
        before = int((time.time() - 7 * 86400) * 1000)
        got = server.range_since_ms("7")
        after = int((time.time() - 7 * 86400) * 1000)
        self.assertGreaterEqual(got, before)
        self.assertLessEqual(got, after)


class FirstRoundSemanticsTest(FixtureMixin, unittest.TestCase):
    """首轮 = 全历史里每个 (session, agent) 第一条 total>0；窗口只筛结果。"""

    def test_first_round_located_on_full_history(self):
        rep = server.first_round_report("30")
        keys = {(r["session_id"], r["agent"]) for r in rep["rounds"]}
        self.assertEqual(keys, {("ses_root", "main"), ("ses_root", "general-1"), ("ses_child", "checkpoint-writer-1"), ("ses_old", "main")})

    def test_window_only_filters(self):
        rep7 = server.first_round_report("7")
        keys7 = {(r["session_id"], r["agent"]) for r in rep7["rounds"]}
        self.assertNotIn(("ses_old", "main"), keys7)  # 首轮在 10 天前，不进 7 天窗口
        self.assertEqual(len(keys7), 3)

    def test_pending_counts_zero_shell_before_first_round(self):
        rep = server.first_round_report("7")
        self.assertEqual(rep["pending"]["rows"], 1)     # m02
        self.assertEqual(rep["pending"]["groups"], 1)
        self.assertEqual(rep["pending"]["unlanded"], 0)  # ses_root/main 后来落盘了

    def test_zero_first_round_goes_to_zero_bucket(self):
        rep = server.first_round_report("7")
        row = next(r for r in rep["rounds"] if r["agent"] == "general-1")
        self.assertEqual(row["bucket"], "zero")
        self.assertEqual(row["hit"], 0)


class SessionRootTest(FixtureMixin, unittest.TestCase):
    def test_child_merged_into_root(self):
        res = server.sessions_by_root("30")
        root = next(r for r in res["by_root"] if r["id"] == "ses_root")
        self.assertEqual(root["merged_sessions"], 2)
        self.assertEqual(len(root["children"]), 1)
        self.assertEqual(root["children"][0]["id"], "ses_child")
        self.assertEqual(res["totals"]["child_sessions"], 1)

    def test_cycle_safe(self):
        # 环内「根」没有良定义；契约是：终止，且只返回环内节点
        sessions = {"a": {"parent_id": "b"}, "b": {"parent_id": "a"}, "c": {"parent_id": "missing"}}
        self.assertIn(server._root_session_id("a", sessions), {"a", "b"})
        self.assertIn(server._root_session_id("b", sessions), {"a", "b"})
        self.assertEqual(server._root_session_id("c", sessions), "c")  # 悬空 parent：自身为根


class StaticPathTest(unittest.TestCase):
    """静态文件路径解析：按路径段判断包含关系。"""

    def test_inside_root(self):
        target = server._resolve_static("css/style.css")
        self.assertIsNotNone(target)
        self.assertTrue(target.is_file())

    def test_sibling_with_same_prefix_is_rejected(self):
        # str.startswith 会放行的场景：同名前缀的兄弟目录
        self.assertIsNone(server._resolve_static(f"../{server.FRONTEND_ROOT.name}-x/server.py"))

    def test_traversal_is_rejected(self):
        self.assertIsNone(server._resolve_static("../../etc/passwd"))
        self.assertIsNone(server._resolve_static("js/../../server.py"))


class HttpHeaderTest(FixtureMixin, unittest.TestCase):
    """HTTP 层：不下发 CORS 头，且不响应 OPTIONS（前端与 API 同源，无预检）。"""

    def test_no_cors_header_and_no_options(self):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        quiet = server.Handler.log_message
        server.Handler.log_message = lambda *a, **k: None
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/auth")
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 200)
            self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("OPTIONS", "/api/overview")
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 501)
            self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
            conn.close()

            # 静态路由：同名前缀的兄弟目录必须 403（旧实现 startswith 放行后落到 404）
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", f"/css/../../{server.FRONTEND_ROOT.name}-x/server.py")
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 403)
            conn.close()
        finally:
            server.Handler.log_message = quiet
            httpd.shutdown()
            httpd.server_close()


class FirstRoundBucketTest(unittest.TestCase):
    """先取整再分桶：显示 0% 的一定落在 0% 桶（README 口径）。"""

    def test_boundaries(self):
        b = server._first_round_bucket
        self.assertEqual([b(0), b(1), b(49), b(50), b(89), b(90), b(100)], ["zero", "low", "low", "mid", "mid", "high", "high"])

    def test_tiny_nonzero_read_rounds_to_zero_bucket(self):
        # 400 / 100000 = 0.4% → 取整为 0 → 必须进 zero 桶，而不是 low
        hit = server.cache_hit_rate(400, 0, 99_600)
        self.assertEqual(hit, 0)
        self.assertEqual(server._first_round_bucket(hit), "zero")


class HygieneTest(FixtureMixin, unittest.TestCase):
    def test_no_dead_code_or_deprecated_api(self):
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertFalse("mktemp(" in src, "server.py 不应再使用已废弃的 tempfile.mktemp")
        self.assertFalse(hasattr(server, "_parse_message_rows"), "_parse_message_rows 为死代码，应已删除")

    def test_csv_rows_carry_agent(self):
        rows = list(csv.reader(io.StringIO(server.export_csv("7"))))
        self.assertTrue(all(len(r) == len(CSV_HEADER) for r in rows))
        self.assertEqual({r[-1] for r in rows[1:]}, {"main", "general-1", "checkpoint-writer-1"})


class QuotaTempFileTest(unittest.TestCase):
    """get_user_quota 的 Cookie 临时副本：无有效 SSO 时不发网络请求，且临时文件必被清理。"""

    def _run(self, fake: Path, fail_connect: bool = False) -> Dict[str, Any]:
        created = []
        real_ntf = tempfile.NamedTemporaryFile
        real_opener = urllib.request.urlopen
        real_connect = sqlite3.connect

        def spy(*a, **k):
            fh = real_ntf(*a, **k)
            created.append(fh.name)
            return fh

        def boom(*a, **k):
            raise RuntimeError("connect exploded")

        tempfile.NamedTemporaryFile = spy
        urllib.request.urlopen = lambda *a, **k: self.fail("不应发起网络请求")
        if fail_connect:
            sqlite3.connect = boom
        saved = server.COOKIE_CANDIDATES
        server.COOKIE_CANDIDATES = [fake]
        try:
            res = server.get_user_quota()
        finally:
            server.COOKIE_CANDIDATES = saved
            tempfile.NamedTemporaryFile = real_ntf
            urllib.request.urlopen = real_opener
            sqlite3.connect = real_connect
        self.assertEqual(len(created), 1)
        self.assertFalse(Path(created[0]).exists(), "临时 Cookie 副本未被清理")
        return res

    def test_no_sso_returns_before_network_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "Cookies"
            conn = sqlite3.connect(fake)
            conn.execute("CREATE TABLE cookies (name TEXT, value TEXT)")
            conn.commit()
            conn.close()
            self.assertEqual(self._run(fake)["reason"], "no-sso")

    def test_failure_path_also_cleans_up(self):
        # 副本已落盘、随后读库失败：旧实现在 except 里直接 return，跳过 unlink
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "Cookies"
            fake.write_bytes(b"")
            self.assertEqual(self._run(fake, fail_connect=True)["reason"], "cookie-read-failed")


if __name__ == "__main__":
    unittest.main()
