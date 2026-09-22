"""P1-① Sleeptime 施工测试：互链器单测 + 反射弧⑥/时间窗/预言循环挂载集成断言。"""

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT.parent))

from astrbot_plugin_livingmemory.core.v2.sleeptime_linker import SleeptimeLinker


class FakeStore:
    """收集 add_causality 调用的假 store（学 test_evolution 模式）。"""

    def __init__(self):
        self.calls = []

    async def add_causality(self, memory_id, persona_id, session_id,
                           trigger_type, trigger_message, pre_cause_id,
                           role, context_snapshot):
        self.calls.append({
            "memory_id": memory_id, "pre_cause_id": pre_cause_id,
            "role": role, "trigger_type": trigger_type,
        })
        return len(self.calls)


def _make_main_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, metadata TEXT)")
    meta_merged = json.dumps({
        "merged_into": "101", "merge_similarity": 0.72, "persona_id": "default"})
    meta_plain = json.dumps({"importance": 0.6})
    conn.executemany(
        "INSERT INTO documents (id, metadata) VALUES (?, ?)",
        [(101, meta_plain), (205, meta_merged), (206, meta_plain)])
    conn.commit()
    conn.close()


def test_scan_merged_pairs(tmp_path):
    main_db = tmp_path / "main.db"
    _make_main_db(str(main_db))
    linker = SleeptimeLinker(str(main_db), str(tmp_path / "v2.db"), FakeStore())
    pairs = linker._scan_merged_pairs()
    assert len(pairs) == 1
    assert pairs[0]["merged_id"] == 205
    assert pairs[0]["keeper_id"] == 101
    assert pairs[0]["similarity"] == pytest.approx(0.72)


def test_link_once_idempotent(tmp_path):
    main_db = tmp_path / "main.db"
    v2_db = tmp_path / "v2.db"
    _make_main_db(str(main_db))
    store = FakeStore()
    linker = SleeptimeLinker(str(main_db), str(v2_db), store)
    r1 = asyncio.run(linker.link_once())
    assert r1["linked"] == 1 and r1["scanned"] == 1
    assert len(store.calls) == 1
    c = store.calls[0]
    assert c["memory_id"] == 205 and c["pre_cause_id"] == 101
    assert c["role"] == "variant_of" and c["trigger_type"] == "sleeptime_consolidate"
    r2 = asyncio.run(linker.link_once())
    assert r2["linked"] == 0 and r2["skipped"] == 1
    assert len(store.calls) == 1  # 幂等：不重复建边


def test_disabled_without_paths():
    linker = SleeptimeLinker("", "", None)
    assert linker.enabled is False
    r = asyncio.run(linker.link_once())
    assert r == {"scanned": 0, "linked": 0, "skipped": 0}


def test_link_pair_defensive_no_store(tmp_path):
    main_db = tmp_path / "main.db"
    _make_main_db(str(main_db))
    linker = SleeptimeLinker(str(main_db), str(tmp_path / "v2.db"), None)
    ok = asyncio.run(linker._link_pair(
        {"merged_id": 1, "keeper_id": 2, "similarity": 0.5}))
    assert ok is False


# ━━ 集成断言：反射弧⑥ / 时间窗 / 预言循环挂载（源码级，学 test_reflex3_integration）━━

_V2 = _PLUGIN_ROOT / "core" / "v2"
_HELPER = _PLUGIN_ROOT.parent / "astrbot_plugin_livingmemory_helper" / "core"

def test_archive_confirm_reflex6_mounted():
    src = (_V2 / "archive_manager.py").read_text(encoding="utf-8")
    assert "parent_memory_id FROM memory_atoms WHERE id=?" in src
    assert "archived_'||role" in src
    assert "反射弧⑥" in src


def test_dream_should_run_work_window():
    src = (_HELPER / "dream_engine.py").read_text(encoding="utf-8")
    assert "20 <= datetime.now().hour < 24" in src
    assert src.index("if force:") < src.index("20 <= datetime.now().hour < 24")


def test_prophecy_loop_links_sleeptime():
    src = (_PLUGIN_ROOT / "core" / "plugin_initializer.py").read_text(encoding="utf-8")
    assert 'getattr(self.v2_engine, "sleeptime_linker", None)' in src
    assert "await _linker.link_once()" in src


def test_v2_engine_constructs_linker():
    src = (_V2 / "v2_engine.py").read_text(encoding="utf-8")
    assert "self.sleeptime_linker = SleeptimeLinker(" in src
    assert "SleeptimeLinker 初始化失败(忽略)" in src