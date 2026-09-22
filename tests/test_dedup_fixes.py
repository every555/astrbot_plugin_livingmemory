"""深夜修_bug 回归锁（橘子 2026-08-19 23:05 "现在就修，靠老婆了"）：
两处病根——
① scheduler confirm 后不回填 memory_id → backfill 自愈扫描误判漏网 → 重复入库（#2018 案）
② 新异比较池只看 candidate → 已裁决原句退池 → 引用/粘贴原文再次进门重复建候选（#375 案）"""
import asyncio
import os
import shutil
import sqlite3
import tempfile
import time

from astrbot_plugin_livingmemory.core.v2.reflection_scheduler import (
    ReflectionScheduler,
)
from astrbot_plugin_livingmemory.core.v2.security_gate import RuleGate

_SCHEMA = """CREATE TABLE IF NOT EXISTS gate_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, speaker TEXT, content TEXT,
    score REAL, axes TEXT, metadata TEXT, source TEXT,
    status TEXT DEFAULT 'candidate', verdict TEXT, note TEXT,
    created_at REAL, reviewed_at REAL, memory_id INTEGER)"""


def test_materialize_writes_back_memory_id():
    """刀1回归：confirm 落地成功必须回填 gate_candidates.memory_id，
    否则 backfill 自愈扫描会重复补写（#2018 重复入库的真凶）。"""
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "gate.db")
    try:
        c = sqlite3.connect(db)
        c.execute(_SCHEMA)
        c.execute(
            "INSERT INTO gate_candidates (speaker, source, content, score, status, created_at)"
            " VALUES ('橘子','user','下周三我生日',0.6,'candidate',?)",
            (time.time(),),
        )
        c.commit()
        c.close()

        async def fake_llm(prompt):
            return '{"verdicts": [{"id": 1, "action": "confirm", "word": "升级", "note": "大事"}]}'

        async def fake_mat(verdict):
            return 9999  # 模拟 livingmemory 返回的新记忆 id

        sched = ReflectionScheduler(
            db_path=db,
            provider_fn=lambda: "FAKE",
            llm_fn=fake_llm,
            clock=lambda: 1000.0,
            materialize_fn=fake_mat,
        )
        asyncio.run(sched.run_reflection())
        c = sqlite3.connect(db)
        mid = c.execute(
            "SELECT memory_id FROM gate_candidates WHERE id=1"
        ).fetchone()[0]
        c.close()
        assert mid == 9999, f"memory_id 应回填 9999，实际 {mid}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_adjudicated_original_still_guards_against_repeat():
    """刀2回归：已裁决(非candidate)的原句仍在防重池——
    同内容再进门应 bump 原件，不许新建（#375：橘子粘贴原文又建一条）。"""
    gate = RuleGate(db_path=":memory:")
    text = "明天下午三点我们去医院复查拿药，记得带上次的片子"
    first = gate.process(text, speaker="橘子", source="user")
    assert first is not None
    # 模拟夜班裁完：原候选退出 candidate 状态
    gate._conn.execute(
        "UPDATE gate_candidates SET status='confirmed', verdict='升级' WHERE id=?",
        (first["id"],),
    )
    gate._conn.commit()
    # 同内容再来（引用/粘贴场景）
    second = gate.process(text, speaker="橘子", source="user")
    assert second is not None
    assert second["id"] == first["id"], (
        f"应 bump 原件 #{first['id']}，实际新建 #{second['id']}"
    )
    total = gate._conn.execute(
        "SELECT COUNT(*) FROM gate_candidates"
    ).fetchone()[0]
    assert total == 1, f"只该有 1 条，实际 {total}"