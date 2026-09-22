"""P1-② 检索后逻辑自检单元测试：互检/关联/免疫降级/反射弧③。"""
import os
import sqlite3
import sys
import tempfile
import time

import pytest

BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "data", "plugins"))

from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridResult
from astrbot_plugin_livingmemory.core.retrieval.self_check import (
    RetrievalSelfCheck,
    get_recent_confirmed_alerts,
)


def _mk(doc_id, content):
    return HybridResult(
        doc_id=doc_id,
        final_score=1.0,
        rrf_score=1.0,
        bm25_score=None,
        vector_score=None,
        content=content,
        metadata={},
    )


@pytest.fixture()
def tmp_v2db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE memory_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            new_memory_id INTEGER, old_memory_id INTEGER, level INTEGER,
            conflict_type TEXT, reason TEXT, confidence REAL,
            status TEXT, created_at REAL
        )"""
    )
    conn.commit()
    conn.close()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def test_pairwise_hit(tmp_v2db):
    """对立句对（喜欢 vs 讨厌同话题）必须互标。"""
    sc = RetrievalSelfCheck(tmp_v2db, {})
    a = _mk(1, "橘子明确说他喜欢香菜拌牛肉")
    b = _mk(2, "橘子后来又说讨厌香菜这个味道")
    sc.check([a, b])
    assert a.conflict_warnings, "a 应被标注"
    assert b.conflict_warnings, "b 应被标注"
    assert "记忆#2" in a.conflict_warnings[0]


def test_no_conflict_pass(tmp_v2db):
    """无关句对不应误报。"""
    sc = RetrievalSelfCheck(tmp_v2db, {})
    a = _mk(1, "今天天气很好橘子在喝水")
    b = _mk(2, "夜班要记得穿弹力袜")
    sc.check([a, b])
    assert a.conflict_warnings == []
    assert b.conflict_warnings == []


def test_conflict_link(tmp_v2db):
    """召回记忆关联已有 conflict 候选 → 附证据链。"""
    conn = sqlite3.connect(tmp_v2db)
    conn.execute(
        "INSERT INTO memory_conflicts (new_memory_id, old_memory_id, level, conflict_type, reason, confidence, status, created_at) "
        "VALUES (101, 55, 1, 'content', '测试理由', 0.7, 'candidate', ?)",
        (time.time(),),
    )
    conn.commit()
    conn.close()
    sc = RetrievalSelfCheck(tmp_v2db, {})
    a = _mk(101, "内容甲")
    b = _mk(202, "内容乙")
    sc.check([a, b])
    assert any("候选冲突#1" in w for w in a.conflict_warnings), "101 应带冲突关联"
    assert b.conflict_warnings == [], "202 不应带"


def test_immune_degrade_bad_db():
    """v2库路径无效 → 关联降级，互检照跑，结果原样返回。"""
    sc = RetrievalSelfCheck(os.path.join(tempfile.gettempdir(), "no_such_v2.db"), {})
    a = _mk(1, "橘子说他喜欢香菜拌牛肉")
    b = _mk(2, "橘子又说讨厌香菜这个味道")
    out = sc.check([a, b])
    assert out is not None and len(out) == 2
    assert a.conflict_warnings, "互检在坏库下仍应工作"


def test_disabled():
    """开关关闭 → 原样返回不标注。"""
    sc = RetrievalSelfCheck("whatever", {"retrieval_self_check": {"enabled": False}})
    a = _mk(1, "橘子说他喜欢香菜拌牛肉")
    b = _mk(2, "橘子又说讨厌香菜这个味道")
    sc.check([a, b])
    assert a.conflict_warnings == []
    assert b.conflict_warnings == []


def test_reflex3_alerts(tmp_v2db):
    """反射弧③：24h 内 confirmed 返回、过期不返回、坏库返回[]。"""
    conn = sqlite3.connect(tmp_v2db)
    conn.execute(
        "INSERT INTO memory_conflicts (new_memory_id, old_memory_id, level, conflict_type, reason, confidence, status, created_at) "
        "VALUES (1, 2, 1, 'content', '近期已确认矛盾', 0.9, 'confirmed', ?)",
        (time.time() - 3600,),
    )
    conn.execute(
        "INSERT INTO memory_conflicts (new_memory_id, old_memory_id, level, conflict_type, reason, confidence, status, created_at) "
        "VALUES (3, 4, 1, 'content', '过期已确认矛盾', 0.9, 'confirmed', ?)",
        (time.time() - 48 * 3600,),
    )
    conn.commit()
    conn.close()
    alerts = get_recent_confirmed_alerts(tmp_v2db, hours=24)
    assert len(alerts) == 1 and "近期已确认矛盾" in alerts[0]
    assert get_recent_confirmed_alerts(os.path.join(tempfile.gettempdir(), "nope.db")) == []
