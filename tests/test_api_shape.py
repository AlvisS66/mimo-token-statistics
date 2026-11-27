"""接口形状回归测试（纯标准库 unittest）。

目的：锁定各聚合函数的响应字段集合，任何结构变化都必须在这里显式更新断言。
运行：仓库根目录 `python -m unittest discover -s tests -v`
"""

from __future__ import annotations

import contextlib
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
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

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
    "aggregate_agents": {"ok", "range", "range_label", "dates", "generated_at", "since", "totals", "by_agent"},
    "first_round_report": {
        "ok", "range", "range_label", "dates", "generated_at", "agent", "since", "definition",
        "totals", "buckets", "by_agent", "rounds", "pending",
    },
    "sessions_by_root": {"ok", "range", "range_label", "dates", "generated_at", "since", "totals", "by_root"},
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


def _assistant(created_ms, input_t, output_t, cache_read, cache_write=0, total=None, model="m-test",
               provider=None, variant=None):
    tok = {"input": input_t, "output": output_t, "cache": {"read": cache_read, "write": cache_write}}
    if total is not None:
        tok["total"] = total
    msg = {"role": "assistant", "modelID": model, "time": {"created": created_ms}, "tokens": tok}
    if provider is not None:
        msg["providerID"] = provider
    if variant is not None:
        msg["variant"] = variant
    return json.dumps(msg)


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
            ("m03", "ses_root", "main",     t_recent - 3000, _assistant(t_recent - 3000, 1000, 100, 900,
                                                                       provider="prov-a", variant="max")),
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


class SessionDetailTurnTest(FixtureMixin, unittest.TestCase):
    """每条请求要带服务商与思考强度：抽屉卡片按它们显示 provider / variant。"""

    def test_turns_carry_provider_and_variant(self):
        turns = {t["time"]: t for t in server.session_detail("ses_root")["turns"]}
        for t in turns.values():
            self.assertIn("provider", t, t["time"])
            self.assertIn("variant", t, t["time"])
        # m03 两个字段都有，m04 一个都没有
        self.assertEqual(turns[self.t_recent - 3000]["provider"], "prov-a")
        self.assertEqual(turns[self.t_recent - 3000]["variant"], "max")
        self.assertIsNone(turns[self.t_recent - 2000]["provider"])
        self.assertIsNone(turns[self.t_recent - 2000]["variant"])

    def test_every_turn_is_returned(self):
        """旧实现只回最近 200 条 ⇒ 抽屉滚到底也只有 200 条，提示永远停在 200 / N。"""
        big = "ses_big"
        conn = sqlite3.connect(server.MIMO_DB_PATH)
        try:
            conn.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
                (big, "proj-1", None, "/proj/a", "Big one", self.t_recent, self.t_recent),
            )
            conn.executemany(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        f"b{i:04d}",
                        big,
                        "main",
                        self.t_recent + i * 1000,
                        self.t_recent + i * 1000,
                        _assistant(self.t_recent + i * 1000, 100, 10, 90),
                    )
                    for i in range(210)
                ],
            )
            conn.commit()
        finally:
            conn.close()
        self._reset_server_cache()
        detail = server.session_detail(big)
        self.assertEqual(detail["totals"]["turns"], 210)
        self.assertEqual(len(detail["turns"]), 210)
        self.assertEqual(detail["turns"][0]["time"], self.t_recent)
        self.assertEqual(detail["turns"][-1]["time"], self.t_recent + 209 * 1000)

    def test_model_rows_carry_providers(self):
        """模型分布每行要知道这个模型走过哪些服务商，按条数降序；没有服务商的请求不占位。"""
        sid = "ses_prov"
        conn = sqlite3.connect(server.MIMO_DB_PATH)
        try:
            conn.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, "proj-1", None, "/proj/a", "Providers", self.t_recent, self.t_recent),
            )
            rows = [
                ("p01", self.t_recent + 1000, _assistant(self.t_recent + 1000, 10, 1, 0, provider="p-b")),
                ("p02", self.t_recent + 2000, _assistant(self.t_recent + 2000, 10, 1, 0, provider="p-a")),
                ("p03", self.t_recent + 3000, _assistant(self.t_recent + 3000, 10, 1, 0, provider="p-a")),
                ("p04", self.t_recent + 4000, _assistant(self.t_recent + 4000, 10, 1, 0)),
            ]
            conn.executemany(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?)",
                [(mid, sid, "main", tc, tc, data) for mid, tc, data in rows],
            )
            conn.commit()
        finally:
            conn.close()
        self._reset_server_cache()
        models = server.session_detail(sid)["models"]
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["providers"], ["p-a", "p-b"])


class ModelProvidersTest(FixtureMixin, unittest.TestCase):
    """主面板「模型用量」每行要能显示服务商：两份 by_model 都得带 providers。"""

    def test_overview_by_model_carries_providers(self):
        rows = {r["model"]: r for r in server.aggregate("7")["by_model"]}
        self.assertIn("m-test", rows, "模型用量里应有 fixture 的 m-test")
        self.assertEqual(rows["m-test"]["providers"], ["prov-a"])

    def test_dates_ranks_by_model_carries_providers(self):
        ranks = server.aggregate_dates_ranks([self.recent_day])
        rows = {r["model"]: r for r in ranks["by_model"]}
        self.assertIn("m-test", rows, "选中日期口径的模型用量里应有 m-test")
        self.assertEqual(rows["m-test"]["providers"], ["prov-a"])

    def test_providers_sorted_by_turns(self):
        sid = "ses_prov2"
        conn = sqlite3.connect(server.MIMO_DB_PATH)
        try:
            conn.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, "proj-1", None, "/proj/a", "Providers 2", self.t_recent, self.t_recent),
            )
            # p-z 出现 2 次、p-a 出现 1 次：按次数降序是 [p-z, p-a]，纯字典序会反过来
            rows = [
                ("q01", self.t_recent + 1000, _assistant(self.t_recent + 1000, 10, 1, 0, provider="p-z")),
                ("q02", self.t_recent + 2000, _assistant(self.t_recent + 2000, 10, 1, 0, provider="p-a")),
                ("q03", self.t_recent + 3000, _assistant(self.t_recent + 3000, 10, 1, 0, provider="p-z")),
            ]
            conn.executemany(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?, ?)",
                [(mid, sid, "main", tc, tc, data) for mid, tc, data in rows],
            )
            conn.commit()
        finally:
            conn.close()
        self._reset_server_cache()
        rows = {r["model"]: r for r in server.aggregate("7")["by_model"]}
        self.assertIn("m-test", rows)
        self.assertEqual(rows["m-test"]["providers"][:2], ["p-z", "p-a"])
        self.assertIn("prov-a", rows["m-test"]["providers"])


class ProviderScopeTest(FixtureMixin, unittest.TestCase):
    """providers 只加在会话明细与两处 by_model 行上；pack_mstat 被 /api/agents 等复用，不要顺手加进去。"""

    def test_pack_mstat_has_no_providers(self):
        st = {"tokens": 10, "turns": 1, "input": 5, "output": 5, "reasoning": 0, "cache_read": 0, "cache_write": 0}
        self.assertNotIn("providers", server.pack_mstat("m-x", st))

    def test_agent_rows_have_no_providers(self):
        rows = server.aggregate_agents("7")["by_agent"]
        self.assertTrue(rows, "fixture 里应有 agent 行")
        self.assertNotIn("providers", rows[0])
        for model_row in rows[0]["models"]:
            self.assertNotIn("providers", model_row)


class RangeWindowTest(FixtureMixin, unittest.TestCase):
    def test_all_is_zero(self):
        self.assertEqual(server.range_since_ms("all"), 0)

    def test_today_is_local_midnight(self):
        # 从日期键反推零点，而不是照抄生产里的 replace(hour=0, …)
        today = server.day_key(int(time.time() * 1000))
        expected = int(datetime.strptime(today, "%Y-%m-%d").timestamp() * 1000)
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


class DatesSelectionTest(FixtureMixin, unittest.TestCase):
    """三个维度接口的 dates 参数：按本地日期筛条目，给出时忽略 range。"""

    FNS = {
        "agents": server.aggregate_agents,
        "first_round": server.first_round_report,
        "root": server.sessions_by_root,
    }

    @property
    def old_day(self) -> str:
        return server.day_key(self.t_old)

    @staticmethod
    def midnight_ms(day: str) -> int:
        return int(datetime.strptime(day, "%Y-%m-%d").timestamp() * 1000)

    def test_agents_sum_to_selected_day(self):
        recent = server.aggregate_agents("7", dates=[self.recent_day])
        self.assertEqual(recent["totals"]["tokens"], 9070)
        self.assertEqual({r["agent"]: r["tokens"] for r in recent["by_agent"]}, {"main": 7050, "general-1": 2020})
        today = server.aggregate_agents("7", dates=[self.today_day])
        self.assertEqual(today["totals"]["tokens"], 3030)
        self.assertEqual([r["agent"] for r in today["by_agent"]], ["checkpoint-writer-1"])

    def test_dates_override_range(self):
        # 10 天前不在 7 天窗口内：给出 dates 后 range 不再生效
        old = server.aggregate_agents("7", dates=[self.old_day])
        self.assertEqual(old["totals"]["tokens"], 110 + 1234)
        self.assertEqual(old["range_label"], self.old_day)
        self.assertEqual(old["since"], self.midnight_ms(self.old_day))

    def test_three_responses_echo_dates(self):
        for name, fn in self.FNS.items():
            with self.subTest(fn=name):
                self.assertEqual(fn("7", dates=[self.recent_day])["dates"], [self.recent_day])
                self.assertIsNone(fn("7")["dates"])

    def test_first_round_and_pending_follow_dates(self):
        recent = server.first_round_report("7", dates=[self.recent_day])
        self.assertEqual(recent["totals"]["n"], 2)
        self.assertEqual({r["agent"] for r in recent["rounds"]}, {"main", "general-1"})
        self.assertEqual(recent["pending"]["rows"], 1)  # m02 零壳子排在首轮 m03 之前
        today = server.first_round_report("7", dates=[self.today_day])
        self.assertEqual(today["totals"]["n"], 1)
        self.assertEqual([r["agent"] for r in today["rounds"]], ["checkpoint-writer-1"])
        self.assertEqual(today["pending"]["rows"], 0)

    def test_sessions_root_follow_dates(self):
        recent = server.sessions_by_root("7", dates=[self.recent_day])
        self.assertEqual(recent["totals"]["tokens"], 9070)
        self.assertEqual(recent["totals"]["child_sessions"], 0)
        today = server.sessions_by_root("7", dates=[self.today_day])
        # 当天只有子会话 ses_child 有消耗，归并到 ses_root 名下
        self.assertEqual(today["totals"]["roots"], 1)
        self.assertEqual(today["totals"]["child_sessions"], 1)
        self.assertEqual(today["totals"]["child_tokens"], 3030)

    def test_cache_keeps_dates_apart_from_range(self):
        # 缓存键必须带上 dates，否则带日期与不带日期的请求会互相串台
        for name, fn in self.FNS.items():
            with self.subTest(fn=name):
                base = fn("7")
                picked = fn("7", dates=[self.today_day])
                again = fn("7")
                self.assertIsNone(base["dates"])
                self.assertEqual(picked["dates"], [self.today_day])
                self.assertIsNone(again["dates"])
                self.assertEqual(again, base)
                self.assertNotEqual(picked["totals"]["tokens"], base["totals"]["tokens"])

    def test_cache_key_cannot_be_forged(self):
        # range / agent 是用户输入：缓存键若靠分隔符拼接，"7|2026-11-26" 能命中 dates 模式的缓存
        server.aggregate_agents("7", dates=[self.today_day])
        server.sessions_by_root("7", dates=[self.today_day])
        server.first_round_report("7", "", dates=[self.today_day])
        forged_agents = server.aggregate_agents(f"7|{self.today_day}")
        self.assertIsNone(forged_agents["dates"])
        self.assertEqual(forged_agents["totals"]["tokens"], 12100)
        forged_roots = server.sessions_by_root(f"7|{self.today_day}")
        self.assertIsNone(forged_roots["dates"])
        self.assertEqual(forged_roots["totals"]["tokens"], 12100)
        forged_fr = server.first_round_report("7", f"|{self.today_day}")
        self.assertIsNone(forged_fr["dates"])
        self.assertEqual(forged_fr["totals"]["n"], 0)

    def test_agent_filter_combines_with_dates(self):
        res = server.first_round_report("7", "main", dates=[self.recent_day])
        self.assertEqual(res["dates"], [self.recent_day])
        self.assertEqual([r["agent"] for r in res["rounds"]], ["main"])

    def test_at_most_32_dates_and_reject_malformed(self):
        many = [server.day_key(self.t_today - i * DAY_MS) for i in range(40)]
        self.assertEqual(server.aggregate_agents("7", dates=many)["dates"], many[: server.MAX_DATES])
        for bad in (["2026-13-45"], ["not-a-date"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                server.aggregate_agents("7", dates=bad)

    def test_days_hourly_honors_max_dates(self):
        # MAX_DATES 是维度接口与 /api/days-hourly 共用的上限：改小它，days-hourly 也必须跟着变——
        # 默认值下写死 32 的旧实现也能过，只有改小才露馅
        saved, server.MAX_DATES = server.MAX_DATES, 3
        try:
            many = [server.day_key(self.t_today - i * DAY_MS) for i in range(40)]
            out = server.get_days_hourly(many)
        finally:
            server.MAX_DATES = saved
        self.assertEqual(out["dates"], many[:3])
        self.assertEqual(out["day_count"], 3)

    def test_window_helpers(self):
        since, wanted = server.selection_window("7")
        self.assertEqual(since, server.range_since_ms("7"))
        self.assertIsNone(wanted)
        since, wanted = server.selection_window("7", [self.old_day])
        self.assertEqual((since, wanted), (self.midnight_ms(self.old_day), {self.old_day}))


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

    def test_only_whitelisted_dirs_are_served(self):
        # /css/../server.py：解析后仍在仓库根内，但不在放行目录里
        for rel in ("css/../server.py", "js/../.git/config", "assets/../index.html", "css/..", "js/../"):
            self.assertIsNone(server._resolve_static(rel), rel)
        for rel in ("css/style.css", "js/app.js", "assets/dashboard.png", "css/../css/style.css"):
            self.assertIsNotNone(server._resolve_static(rel), rel)


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

    def test_source_via_traversal_is_forbidden(self):
        # 静态路由排在鉴权之前：/css/../server.py 解析后仍在仓库根内，旧实现免鉴权 200 回源码
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        quiet = server.Handler.log_message
        server.Handler.log_message = lambda *a, **k: None
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            for path in ("/css/../server.py", "/assets/../index.html", "/js/../README.md"):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", path)
                resp = conn.getresponse()
                resp.read()
                conn.close()
                self.assertEqual(resp.status, 403, path)

            # 放行目录内的文件照常可取（免鉴权）
            for path in ("/css/style.css", "/js/app.js"):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", path)
                resp = conn.getresponse()
                resp.read()
                conn.close()
                self.assertEqual(resp.status, 200, path)
        finally:
            server.Handler.log_message = quiet
            httpd.shutdown()
            httpd.server_close()


class DatesHttpTest(FixtureMixin, unittest.TestCase):
    """HTTP 层：三条路由接受 dates；空值等同未传；格式错误回 400 而不是带病算出空数据。"""

    def test_routes_accept_dates(self):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        quiet, auth = server.Handler.log_message, server.AUTH_ENABLED
        server.Handler.log_message = lambda *a, **k: None
        server.AUTH_ENABLED = False
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def get(conn, path):
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())

        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            paths = ("/api/agents", "/api/first-round", "/api/sessions-root")
            for path in paths:
                with self.subTest(path=path):
                    status, body = get(conn, f"{path}?range=7&dates={self.today_day}")
                    self.assertEqual((status, body["dates"]), (200, [self.today_day]))
                    status, body = get(conn, f"{path}?dates=not-a-date")
                    self.assertEqual(status, 400)
                    status, body = get(conn, f"{path}?range=7&dates=")
                    self.assertEqual((status, body["dates"]), (200, None))
            status, body = get(conn, f"/api/agents?range=7&dates={self.today_day}")
            self.assertEqual(body["totals"]["tokens"], 3030)
            conn.close()
        finally:
            server.AUTH_ENABLED = auth
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

    def test_cache_set_purges_expired_entries(self):
        """过期的会话明细（现在含全部 turns，单条可达几百 KB）不能一直留在内存里。"""
        server.cache_set("sess:gone", {"turns": ["x"] * 10})
        server._CACHE["sess:gone"] = (time.time() - server.CACHE_TTL - 1, {"turns": []})
        server.cache_set("fresh", 1)
        self.assertNotIn("sess:gone", server._CACHE)
        self.assertIn("fresh", server._CACHE)

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


PAGE_SESSIONS = 12

SORT_FIELDS = {
    "tokens": "tokens",
    "turns": "turns",
    "avg": "avg",
    "input": "input",
    "output": "output",
    "cache": "cache",
    "hit": "cache_hit",
    "last": "last",
}


class PagingFixtureMixin:
    """分页专用 fixture：12 个根会话 + 2 个子会话 + 2 个项目。

    tokens / 时间 / 轮次刻意错开，用来断言排序；9 / 10 号 tokens 故意相同，用来锁定并列时的二级键。
    子会话的消耗压到最小，免得它们在按 tokens 排序时插进根会话中间。
    """

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "mimocode.db"
        self.now_ms = int(time.time() * 1000)
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
        c.execute("INSERT INTO project VALUES ('proj-a', '/proj/a', 'a')")
        c.execute("INSERT INTO project VALUES ('proj-b', '/proj/b', 'b')")

        sessions, messages = [], []
        for i in range(1, PAGE_SESSIONS + 1):
            sid = f"ses{i:02d}"
            odd = i % 2 == 1
            created = self.now_ms - (PAGE_SESSIONS + 1 - i) * 60_000  # 编号越大越新
            title = "Special seven" if i == 7 else f"Sess {i:02d}"
            sessions.append(
                (
                    sid,
                    "proj-a" if odd else "proj-b",
                    None,
                    "/proj/a" if odd else "/proj/b",
                    title,
                    created,
                    created,
                )
            )
            tokens = 4400 if i in (9, 10) else 1100 * (PAGE_SESSIONS + 1 - i)
            cache_read = 100 * i
            messages.append(
                (
                    f"m{i:02d}",
                    sid,
                    "main",
                    created,
                    created,
                    _assistant(created, tokens - cache_read - 50, 50, cache_read),
                )
            )
            if i % 4 == 0:  # 04 / 08 / 12 多一轮，用来测按轮次排序
                messages.append(
                    (
                        f"m{i:02d}b",
                        sid,
                        "main",
                        created + 1000,
                        created + 1000,
                        _assistant(created + 1000, 10, 1, 50, model="m-gamma"),
                    )
                )

        child_created = self.now_ms - (PAGE_SESSIONS + 2) * 60_000
        sessions.append(("ses-child-a", "proj-a", "ses01", "/proj/a", "Child alpha", child_created, child_created))
        sessions.append(("ses-child-b", "proj-b", "ses02", "/proj/b", "Child beta", child_created, child_created))
        messages.append(("mc1", "ses-child-a", "checkpoint-writer-1", child_created, child_created,
                         _assistant(child_created, 10, 1, 5)))
        messages.append(("mc2", "ses-child-b", "checkpoint-writer-1", child_created, child_created,
                         _assistant(child_created, 10, 1, 5)))

        c.executemany("INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)", sessions)
        c.executemany("INSERT INTO message VALUES (?, ?, ?, ?, ?, ?)", messages)
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
    def today_day(self) -> str:
        return server.day_key(self.now_ms)

    @contextlib.contextmanager
    def _serve(self):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        quiet, auth = server.Handler.log_message, server.AUTH_ENABLED
        server.Handler.log_message = lambda *a, **k: None
        server.AUTH_ENABLED = False
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)

        def get(path):
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())

        try:
            yield get
        finally:
            conn.close()
            server.AUTH_ENABLED = auth
            server.Handler.log_message = quiet
            httpd.shutdown()
            httpd.server_close()


class SessionPaginationTest(PagingFixtureMixin, unittest.TestCase):
    """会话用量表：服务端 limit / offset / 过滤 / 排序 + sessions_total。"""

    def test_pages_do_not_overlap_or_drop_rows(self):
        with self._serve() as get:
            seen = []
            for offset in (0, 5, 10):
                status, body = get(f"/api/overview?range=all&session_limit=5&session_offset={offset}")
                self.assertEqual(status, 200)
                self.assertEqual(body["sessions_total"], 14)
                seen.extend(r["id"] for r in body["by_session"])
            # offset=10 只剩 4 条（14 条总数），前两页各 5 条
            self.assertEqual(len(seen), 14)
            self.assertEqual(len(set(seen)), 14)

    def test_limit_zero_returns_everything(self):
        with self._serve() as get:
            status, body = get("/api/overview?range=all&session_limit=0")
            self.assertEqual(status, 200)
            self.assertEqual(len(body["by_session"]), 14)
            self.assertEqual(body["sessions_total"], 14)

    def test_sort_tokens_with_stable_tie_break(self):
        with self._serve() as get:
            _, head = get("/api/overview?range=all&session_sort=tokens&session_limit=5&session_offset=0")
            _, tail = get("/api/overview?range=all&session_sort=tokens&session_limit=5&session_offset=5")
        self.assertEqual([r["id"] for r in head["by_session"]], ["ses01", "ses02", "ses03", "ses04", "ses05"])
        # 09 / 10 号 tokens 相同：并列时按 id 升序，翻页不会重复或漏行
        self.assertEqual([r["id"] for r in tail["by_session"]], ["ses06", "ses07", "ses08", "ses09", "ses10"])

    def test_sort_turns_continues_across_pages(self):
        with self._serve() as get:
            _, first = get("/api/overview?range=all&session_sort=turns&session_limit=3&session_offset=0")
            _, second = get("/api/overview?range=all&session_sort=turns&session_limit=3&session_offset=3")
        self.assertEqual([r["id"] for r in first["by_session"]], ["ses04", "ses08", "ses12"])
        self.assertEqual([r["id"] for r in second["by_session"]], ["ses01", "ses02", "ses03"])

    def test_every_sort_key_is_monotonic(self):
        with self._serve() as get:
            for mode, field in SORT_FIELDS.items():
                with self.subTest(sort=mode):
                    status, body = get(f"/api/overview?range=all&session_sort={mode}&session_limit=0")
                    self.assertEqual(status, 200)
                    values = [r[field] for r in body["by_session"]]
                    self.assertEqual(values, sorted(values, reverse=True), mode)
                    self.assertEqual(body["sessions_total"], 14)

    def test_unknown_sort_falls_back_instead_of_failing(self):
        with self._serve() as get:
            status, body = get("/api/overview?range=all&session_sort=not-a-key&session_limit=0")
            self.assertEqual(status, 200)
            values = [r["last"] for r in body["by_session"]]
            self.assertEqual(values, sorted(values, reverse=True), "未知排序键应回落到默认的按最近")

    def test_search_and_filters(self):
        with self._serve() as get:
            _, q = get("/api/overview?range=all&session_q=special&session_limit=0")
            self.assertEqual([r["id"] for r in q["by_session"]], ["ses07"])
            self.assertEqual(q["sessions_total"], 1)

            # 先过滤再切片：若实现反了，limit=1 拿到的是未匹配的 ses01，结果是空
            _, q1 = get("/api/overview?range=all&session_q=special&session_limit=1")
            self.assertEqual([r["id"] for r in q1["by_session"]], ["ses07"])
            self.assertEqual(q1["sessions_total"], 1)

            _, proj = get("/api/overview?range=all&session_project=a&session_limit=0")
            self.assertEqual({r["project_name"] for r in proj["by_session"]}, {"a"})
            self.assertEqual(proj["sessions_total"], len(proj["by_session"]))

            _, model = get("/api/overview?range=all&session_model=m-gamma&session_sort=tokens&session_limit=0")
            self.assertEqual([r["id"] for r in model["by_session"]], ["ses04", "ses08", "ses12"])

            # 搜索与筛选叠加：a 项目里标题含 special 的只有 07
            _, both = get("/api/overview?range=all&session_q=special&session_project=a&session_limit=0")
            self.assertEqual([r["id"] for r in both["by_session"]], ["ses07"])

    def test_offset_beyond_total_returns_empty_list(self):
        with self._serve() as get:
            status, body = get("/api/overview?range=all&session_limit=10&session_offset=99")
            self.assertEqual(status, 200)
            self.assertEqual(body["by_session"], [])
            self.assertEqual(body["sessions_total"], 14)

    def test_pagination_is_applied_after_cache(self):
        # 分页参数不能进缓存键，否则每翻一页都要重扫全库
        with self._serve() as get:
            get("/api/overview?range=all&session_limit=5")
            get("/api/overview?range=all&session_limit=10&session_offset=5")
        agg_keys = [k for k in server._CACHE if k.startswith("agg:")]
        self.assertEqual(agg_keys, ["agg:all"])
        cached = server._CACHE["agg:all"][1]
        self.assertEqual(len(cached["by_session"]), 14, "缓存里必须是全量行，分页在响应组装时做")

    def test_bad_paging_params_return_400(self):
        with self._serve() as get:
            for path, param in (
                ("/api/overview?range=all&session_limit=abc", "session_limit"),
                ("/api/overview?range=all&session_offset=-1", "session_offset"),
                ("/api/overview?range=all&project_limit=x", "project_limit"),
                ("/api/overview?range=all&project_offset=-5", "project_offset"),
            ):
                with self.subTest(path=path):
                    status, body = get(path)
                    self.assertEqual(status, 400, body)
                    # 报错要点名坏掉的那个参数：张冠李戴（如把 session_limit 报成 project_limit）会在这里挂
                    self.assertEqual(body.get("error"), f"bad {param}")


class ProjectPaginationTest(PagingFixtureMixin, unittest.TestCase):
    """项目榜：project_limit / offset / sort + projects_total。"""

    def test_pages_and_total(self):
        with self._serve() as get:
            status, body = get("/api/overview?range=all&project_limit=1&project_offset=0&project_sort=tokens")
            self.assertEqual(status, 200)
            self.assertEqual([r["name"] for r in body["by_project"]], ["a"])
            self.assertEqual(body["projects_total"], 2)

            _, second = get("/api/overview?range=all&project_limit=1&project_offset=1&project_sort=tokens")
            self.assertEqual([r["name"] for r in second["by_project"]], ["b"])

    def test_project_sort_is_honored(self):
        # a 的 tokens 更大、b 更近（fixture 刻意错开）：忽略 project_sort 则永远拿不到 b
        with self._serve() as get:
            _, default = get("/api/overview?range=all&project_limit=1")
            self.assertEqual([r["name"] for r in default["by_project"]], ["a"])
            _, by_last = get("/api/overview?range=all&project_limit=1&project_sort=last")
            self.assertEqual([r["name"] for r in by_last["by_project"]], ["b"])

    def test_offset_beyond_total_returns_empty_list(self):
        with self._serve() as get:
            status, body = get("/api/overview?range=all&project_limit=5&project_offset=9")
            self.assertEqual(status, 200)
            self.assertEqual(body["by_project"], [])
            self.assertEqual(body["projects_total"], 2)

    def test_project_names_stay_complete_while_paged(self):
        # 项目筛选下拉要用全量项目名，不能被分页切掉
        with self._serve() as get:
            _, body = get("/api/overview?range=all&project_limit=1&project_offset=0")
        self.assertEqual(len(body["by_project"]), 1)
        self.assertEqual(body["project_names"], ["a", "b"])


class RootsPaginationTest(PagingFixtureMixin, unittest.TestCase):
    """会话归并表：limit / offset / q / sort + total。"""

    def test_pages_and_total(self):
        with self._serve() as get:
            status, body = get("/api/sessions-root?range=all&limit=5&offset=0")
            self.assertEqual(status, 200)
            self.assertEqual(len(body["by_root"]), 5)
            self.assertEqual(body["total"], 12)
            self.assertEqual([r["id"] for r in body["by_root"]][0], "ses01")

            _, second = get("/api/sessions-root?range=all&limit=5&offset=5")
            self.assertEqual(len(second["by_root"]), 5)
            overlap = {r["id"] for r in body["by_root"]} & {r["id"] for r in second["by_root"]}
            self.assertEqual(overlap, set())

    def test_sort_is_honored(self):
        # 默认 tokens 降序给 01；按最近应给 12（fixture 里编号越大越新）
        with self._serve() as get:
            _, by_last = get("/api/sessions-root?range=all&sort=last&limit=1")
            self.assertEqual([r["id"] for r in by_last["by_root"]], ["ses12"])
            self.assertEqual(by_last["total"], 12)

    def test_search_matches_child_titles(self):
        with self._serve() as get:
            _, body = get("/api/sessions-root?range=all&q=alpha&limit=0")
            self.assertEqual([r["id"] for r in body["by_root"]], ["ses01"])
            self.assertEqual(body["total"], 1)

    def test_offset_beyond_total_returns_empty_list(self):
        with self._serve() as get:
            status, body = get("/api/sessions-root?range=all&limit=5&offset=99")
            self.assertEqual(status, 200)
            self.assertEqual(body["by_root"], [])
            self.assertEqual(body["total"], 12)

    def test_bad_paging_params_return_400(self):
        with self._serve() as get:
            for path, param in (
                ("/api/sessions-root?range=all&limit=x", "limit"),
                ("/api/sessions-root?range=all&offset=-2", "offset"),
            ):
                with self.subTest(path=path):
                    status, body = get(path)
                    self.assertEqual(status, 400, body)
                    self.assertEqual(body.get("error"), f"bad {param}")


class DatesModePaginationTest(PagingFixtureMixin, unittest.TestCase):
    """选中日期模式（/api/days-hourly）同样分页：它与 overview 共用同一套参数。"""

    def test_days_hourly_paginates(self):
        with self._serve() as get:
            _, full = get(f"/api/days-hourly?dates={self.today_day}&session_sort=tokens&session_limit=0")
            total = full["sessions_total"]
            self.assertEqual(total, len(full["by_session"]))

            status, page = get(
                f"/api/days-hourly?dates={self.today_day}&session_sort=tokens&session_limit=3&session_offset=0"
            )
            self.assertEqual(status, 200)
            self.assertEqual(len(page["by_session"]), 3)
            self.assertEqual(page["sessions_total"], total)
            self.assertEqual([r["id"] for r in page["by_session"]], [r["id"] for r in full["by_session"][:3]])

    def test_days_hourly_project_pagination(self):
        with self._serve() as get:
            status, page = get(f"/api/days-hourly?dates={self.today_day}&project_limit=1&project_offset=1")
            self.assertEqual(status, 200)
            self.assertEqual(len(page["by_project"]), 1)
            self.assertEqual(page["projects_total"], 2)


if __name__ == "__main__":
    unittest.main()
