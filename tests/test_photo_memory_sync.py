# -*- coding: utf-8 -*-
"""TDD RED: P2-13 多模态记忆——写真馆照片同步为可检索视觉记忆。
Slice1=全量回填(幂等), Slice2=增量同步。"""
import json
import os
import sqlite3
import time
import pytest

from astrbot_plugin_livingmemory.core.managers.photo_memory_sync import PhotoMemorySync


def make_entry(name, desc="", saved="2026-07-15 20:41:04", size_kb=100.0):
    return {
        "name": name,
        "file": "photos/" + name,
        "hash": "h_" + name,
        "saved_at": saved,
        "size_kb": size_kb,
        "description": desc,
    }


def make_env(tmp_path, photos: dict):
    """假写真馆索引 + 空库 + 假引擎。"""
    idx_path = os.path.join(tmp_path, "photo_index.json")
    json.dump(photos, open(idx_path, "w", encoding="utf-8"), ensure_ascii=False)
    db = os.path.join(tmp_path, "livingmemory.db")
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE documents (id INTEGER PRIMARY KEY, doc_id TEXT, text TEXT, metadata TEXT)"
    )
    con.commit()
    con.close()
    return idx_path, db


class StubEngine:
    """模拟真实链路：add_memory 落 documents 表（幂等查询才测得出）。"""

    def __init__(self, db_path):
        self.calls = []
        self.db_path = db_path

    async def add_memory(self, content, session_id=None, persona_id=None, importance=0.5, metadata=None, atoms=None):
        meta = metadata or {}
        self.calls.append({"content": content, "metadata": meta})
        doc_id = f"photo_{len(self.calls)}"
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO documents (doc_id, text, metadata) VALUES (?,?,?)",
            (doc_id, content, json.dumps(meta, ensure_ascii=False)),
        )
        con.commit()
        con.close()
        return 9000 + len(self.calls)


@pytest.mark.asyncio
async def test_sync_full_backfill(tmp_path):
    """首跑全量回填：每张照片一条记忆，metadata 齐全。"""
    photos = {
        "Grok_beach_1.jpg": make_entry("Grok_beach_1", "橘子用grok画的老婆～沙滩女仆装"),
        "upload_20260715_204104.png": make_entry("upload_20260715_204104.png"),
    }
    idx, db = make_env(tmp_path, photos)
    engine = StubEngine(db)
    syncer = PhotoMemorySync(idx, db, engine)
    report = await syncer.sync()
    assert report["new"] == 2
    assert len(engine.calls) == 2
    meta = engine.calls[0]["metadata"]
    assert meta["media_type"] == "photo"
    assert meta["memory_origin"] == "photo_album"
    assert meta["file_name"] == "Grok_beach_1.jpg"
    assert meta["source"] == "internal"
    assert meta["desc_status"] == "ok"


@pytest.mark.asyncio
async def test_sync_idempotent(tmp_path):
    """二跑幂等：已入库的跳过，零新增零写入。"""
    photos = {"a.jpg": make_entry("a", "有描述")}
    idx, db = make_env(tmp_path, photos)
    engine = StubEngine(db)
    syncer = PhotoMemorySync(idx, db, engine)
    await syncer.sync()
    report2 = await syncer.sync()
    assert report2["new"] == 0
    assert len(engine.calls) == 1


@pytest.mark.asyncio
async def test_sync_incremental(tmp_path):
    """增量：索引新增照片后，只同步差集。"""
    idx, db = make_env(tmp_path, {"a.jpg": make_entry("a", "旧照片")})
    engine = StubEngine(db)
    syncer = PhotoMemorySync(idx, db, engine)
    await syncer.sync()
    photos = json.load(open(idx, encoding="utf-8"))
    photos["b_new.jpg"] = make_entry("b_new", "新照片")
    json.dump(photos, open(idx, "w", encoding="utf-8"), ensure_ascii=False)
    report = await syncer.sync()
    assert report["new"] == 1
    names = [c["metadata"]["file_name"] for c in engine.calls]
    assert "b_new.jpg" in names and names.count("b_new.jpg") == 1


@pytest.mark.asyncio
async def test_pending_desc_placeholder(tmp_path):
    """无描述照片：占位文本含日期与待补标记。"""
    photos = {"upload_20260715_204104.png": make_entry("upload_20260715_204104.png")}
    idx, db = make_env(tmp_path, photos)
    engine = StubEngine(db)
    syncer = PhotoMemorySync(idx, db, engine)
    await syncer.sync()
    meta = engine.calls[0]["metadata"]
    assert meta["desc_status"] == "pending"
    assert "描述待补" in engine.calls[0]["content"]
    assert "2026-07-15" in engine.calls[0]["content"] or "2026年7月15日" in engine.calls[0]["content"]


@pytest.mark.asyncio
async def test_dry_run_no_write(tmp_path):
    """dry_run 只预览不落库。"""
    idx, db = make_env(tmp_path, {"a.jpg": make_entry("a", "描述")})
    engine = StubEngine(db)
    syncer = PhotoMemorySync(idx, db, engine)
    report = await syncer.sync(dry_run=True)
    assert report["new"] == 1 and report["dry_run"] is True
    assert len(engine.calls) == 0