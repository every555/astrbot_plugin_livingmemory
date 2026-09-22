"""暂存捞回 + 裁决者溯源（actor）——8/19 橘子验收发现的闭环洞。

背景：WebUI 点暂存后卡片消失（所有查看入口只显示 candidate），
且 gate_candidates 不记录裁决者，无法区分省察/网页/对话三路。
本组测试钉死：①裁决落库必须带 actor；②pending 可捞回队列。"""

import json
import sqlite3
import time

import pytest

from astrbot_plugin_livingmemory.core.v2.reflection_scheduler import (
    ReflectionScheduler,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gate_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    score REAL DEFAULT 0.0,
    axes TEXT DEFAULT '{}',
    source TEXT DEFAULT '',
    status TEXT DEFAULT 'candidate',
    note TEXT DEFAULT '',
    verdict TEXT DEFAULT '',
    created_at REAL NOT NULL,
    reviewed_at REAL,
    metadata TEXT DEFAULT '{}'
);
"""


@pytest.fixture
def sched(tmp_path):
    s = ReflectionScheduler(str(tmp_path / "gate.db"))
    c = sqlite3.connect(str(tmp_path / "gate.db"))
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.execute(
        """INSERT INTO gate_candidates
           (speaker, content, score, axes, source, status, created_at)
           VALUES ('春雪', '测试原句甲', 0.9, '{}', 'chat', 'candidate', ?)""",
        (time.time(),),
    )
    c.execute(
        """INSERT INTO gate_candidates
           (speaker, content, score, axes, source, status, created_at)
           VALUES ('橘子', '测试原句乙', 0.8, '{}', 'chat', 'candidate', ?)""",
        (time.time(),),
    )
    c.commit()
    c.close()
    return s


def _row(sched: ReflectionScheduler, cid: int) -> sqlite3.Row:
    c = sched._conn()
    try:
        return c.execute(
            "SELECT * FROM gate_candidates WHERE id=?", (cid,)
        ).fetchone()
    finally:
        c.close()


class TestActorTrace:
    def test_default_actor_is_reflection(self, sched):
        applied = sched._apply_verdict({"id": 1, "action": "confirm", "word": "升级"})
        assert applied is not None
        meta = json.loads(_row(sched, 1)["metadata"] or "{}")
        assert meta.get("actor") == "reflection"
        assert meta.get("actor_at", 0) > 0

    def test_webui_actor_passthrough(self, sched):
        applied = sched._apply_verdict({"id": 1, "action": "decline", "actor": "webui"})
        assert applied is not None
        meta = json.loads(_row(sched, 1)["metadata"] or "{}")
        assert meta.get("actor") == "webui"

    def test_actor_preserves_existing_metadata(self, sched):
        c = sched._conn()
        c.execute(
            "UPDATE gate_candidates SET metadata=? WHERE id=1",
            (json.dumps({"jaccard": 0.11, "penalty": 1.0}),),
        )
        c.commit()
        c.close()
        sched._apply_verdict({"id": 1, "action": "decline", "actor": "webui"})
        meta = json.loads(_row(sched, 1)["metadata"])
        assert meta["jaccard"] == 0.11 and meta["actor"] == "webui"


class TestPendingWord:
    def test_pending_without_word_stores_chinese(self, sched):
        applied = sched._apply_verdict({"id": 1, "action": "pending", "actor": "webui"})
        assert applied is not None
        row = _row(sched, 1)
        assert row["status"] == "pending"
        assert row["verdict"] == "暂存"


class TestRestore:
    def test_restore_pending_back_to_candidate(self, sched):
        sched._apply_verdict({"id": 1, "action": "pending", "actor": "webui"})
        assert sched.restore_candidate(1, actor="webui") is True
        row = _row(sched, 1)
        assert row["status"] == "candidate"
        assert row["verdict"] == ""
        meta = json.loads(row["metadata"] or "{}")
        assert meta.get("restored_by") == "webui"
        assert meta.get("restored_at", 0) > 0

    def test_restore_leaves_verdict_note_in_history(self, sched):
        sched._apply_verdict(
            {"id": 1, "action": "pending", "word": "暂存", "note": "橘子好奇点的", "actor": "webui"}
        )
        sched.restore_candidate(1, actor="webui")
        meta = json.loads(_row(sched, 1)["metadata"] or "{}")
        assert meta.get("prev_verdict") == "暂存"
        assert meta.get("prev_note") == "橘子好奇点的"

    def test_restore_rejects_non_pending(self, sched):
        sched._apply_verdict({"id": 1, "action": "confirm"})
        assert sched.restore_candidate(1) is False

    def test_restore_missing_id(self, sched):
        assert sched.restore_candidate(999) is False
