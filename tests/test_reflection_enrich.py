"""省察行李升级：动态 context_fn + 联网核实协议
（橘子 2026-08-19 "打开行李找衣服" / "不懂就搜"）。"""
import asyncio
import os
import shutil
import sqlite3
import tempfile
import time

from astrbot_plugin_livingmemory.core.v2.reflection_scheduler import (
    ReflectionScheduler,
)

Q = chr(39)
_SCHEMA = """CREATE TABLE IF NOT EXISTS gate_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, speaker TEXT, content TEXT,
    score REAL, axes TEXT, metadata TEXT, source TEXT,
    status TEXT DEFAULT 'candidate', verdict TEXT, note TEXT,
    created_at REAL, reviewed_at REAL)"""

_VERDICT = '{"verdicts": [{"id": 1, "action": "confirm", "word": "升级", "note": "查过发布信息，确实是大事"}]}'


class _Base:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "gate.db")
        c = sqlite3.connect(self.db)
        c.execute(_SCHEMA)
        c.execute(
            "INSERT INTO gate_candidates (speaker, source, content, score, status, created_at)"
            " VALUES (" + Q + "橘子" + Q + "," + Q + "user" + Q + "," + Q + "GLM-5.5 什么时候发布" + Q + ",0.6," + Q + "candidate" + Q + ",?)",
            (time.time(),),
        )
        c.commit()
        c.close()

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSearchEnrich(_Base):
    def test_search_triggered_and_note_appended(self):
        prompts = []
        search_calls = []

        async def fake_llm(prompt):
            prompts.append(prompt)
            if prompt.startswith("预检"):
                return '{"search": "GLM-5.5 发布日期"}'
            return _VERDICT

        async def fake_search(q):
            search_calls.append(q)
            return "- 智谱官网：GLM-5.5 于 2026 年 Q3 发布"

        sched = ReflectionScheduler(
            db_path=self.db,
            provider_fn=lambda: "FAKE",
            llm_fn=fake_llm,
            clock=lambda: 1000.0,
            search_fn=fake_search,
        )
        report = asyncio.run(sched.run_reflection())
        # 旧单串格式 {"search": "..."} 必须继续被认（向后兼容）
        assert search_calls == ["GLM-5.5 发布日期"]
        assert any("联网核实 1/1" in p for p in prompts)
        # 溯源标注规则（橘子追问"使用工具后有标注没"）必须在场
        assert any("〔已核实·" in p for p in prompts)
        assert len(report["verdicts"]) == 1

    def test_two_queries_both_searched(self):
        search_calls = []
        prompts = []

        async def fake_llm(prompt):
            prompts.append(prompt)
            if prompt.startswith("预检"):
                return '{"search": ["GLM-5.5 发布日期", "智谱 GLM-5.5 参数"]}'
            return _VERDICT

        async def fake_search(q):
            search_calls.append(q)
            return "- 结果：" + q

        sched = ReflectionScheduler(
            db_path=self.db,
            provider_fn=lambda: "FAKE",
            llm_fn=fake_llm,
            clock=lambda: 1000.0,
            search_fn=fake_search,
        )
        asyncio.run(sched.run_reflection())
        assert search_calls == ["GLM-5.5 发布日期", "智谱 GLM-5.5 参数"]
        joined = chr(10).join(prompts)
        assert "联网核实 1/2" in joined and "联网核实 2/2" in joined

    def test_precheck_list_dedup_and_cap(self):
        search_calls = []

        async def fake_llm(prompt):
            if prompt.startswith("预检"):
                return '{"search": ["GLM-5.5", "GLM-5.5", "智谱A", "智谱B"]}'
            return _VERDICT

        async def fake_search(q):
            search_calls.append(q)
            return "- x"

        sched = ReflectionScheduler(
            db_path=self.db,
            provider_fn=lambda: "FAKE",
            llm_fn=fake_llm,
            clock=lambda: 1000.0,
            search_fn=fake_search,
        )
        asyncio.run(sched.run_reflection())
        # 去重 + 上限2：重复的 GLM-5.5 只搜一次，后面的截断
        assert search_calls == ["GLM-5.5", "智谱A"]

    def test_no_search_when_precheck_declines(self):
        search_calls = []

        async def fake_llm(prompt):
            if prompt.startswith("预检"):
                return '{"search": ""}'
            return _VERDICT

        async def fake_search(q):
            search_calls.append(q)
            return "nope"

        sched = ReflectionScheduler(
            db_path=self.db,
            provider_fn=lambda: "FAKE",
            llm_fn=fake_llm,
            clock=lambda: 1000.0,
            search_fn=fake_search,
        )
        report = asyncio.run(sched.run_reflection())
        assert search_calls == []
        assert len(report["verdicts"]) == 1

    def test_malformed_precheck_still_judges(self):
        async def fake_llm(prompt):
            if prompt.startswith("预检"):
                return "我觉得不需要搜"
            return _VERDICT

        async def fake_search(q):
            raise AssertionError("不该走到搜索")

        sched = ReflectionScheduler(
            db_path=self.db,
            provider_fn=lambda: "FAKE",
            llm_fn=fake_llm,
            clock=lambda: 1000.0,
            search_fn=fake_search,
        )
        report = asyncio.run(sched.run_reflection())
        assert len(report["verdicts"]) == 1

    def test_without_search_fn_no_precheck(self):
        prompts = []

        async def fake_llm(prompt):
            prompts.append(prompt)
            return _VERDICT

        sched = ReflectionScheduler(
            db_path=self.db,
            provider_fn=lambda: "FAKE",
            llm_fn=fake_llm,
            clock=lambda: 1000.0,
        )
        asyncio.run(sched.run_reflection())
        assert len(prompts) == 1


class TestContextCandidates(_Base):
    def test_context_fn_receives_candidates(self):
        received = []

        async def ctx(candidates):
            received.append(candidates)
            return "家况：测试卡片"

        async def fake_llm(prompt):
            return _VERDICT

        sched = ReflectionScheduler(
            db_path=self.db,
            provider_fn=lambda: "FAKE",
            llm_fn=fake_llm,
            clock=lambda: 1000.0,
            context_fn=ctx,
        )
        asyncio.run(sched.run_reflection())
        assert received and isinstance(received[0], list)
        assert received[0] and received[0][0]["content"].startswith("GLM-5.5")

    def test_legacy_context_fn_noargs_still_works(self):
        async def ctx():
            return "旧式固定包"

        async def fake_llm(prompt):
            return _VERDICT

        sched = ReflectionScheduler(
            db_path=self.db,
            provider_fn=lambda: "FAKE",
            llm_fn=fake_llm,
            clock=lambda: 1000.0,
            context_fn=ctx,
        )
        report = asyncio.run(sched.run_reflection())
        assert len(report["verdicts"]) == 1