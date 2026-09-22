"""P0-1 Provenance 验收测试：source 字段 / v11 迁移 / 检索降权 / 工具参数。"""

import asyncio
import json
import os
import sys
import tempfile
import time

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from astrbot_plugin_livingmemory.core.models.memory_atom import MemoryAtom, AtomType
from astrbot_plugin_livingmemory.storage.db_migration import DBMigration
from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import HybridRetriever
from astrbot_plugin_livingmemory.core.retrieval.rrf_fusion import FusedResult


class TestProvenanceModel:
    """用例1：MemoryAtom 携带 source 字段，默认 internal。"""

    def test_default_source_is_internal(self):
        atom = MemoryAtom(parent_memory_id=1, content="x")
        assert atom.source == "internal"

    def test_external_source_roundtrip(self):
        atom = MemoryAtom(parent_memory_id=1, content="y", source="external")
        assert atom.source == "external"


class TestMigrationV11:
    """用例2：v10 -> v11 迁移加 source 列并回填 documents。"""

    @pytest.mark.asyncio
    async def test_v10_to_v11(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "m.db")
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    """CREATE TABLE memory_atoms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, parent_memory_id INTEGER NOT NULL,
                    atom_type TEXT NOT NULL DEFAULT 'unknown', content TEXT NOT NULL,
                    entities TEXT DEFAULT '[]', importance REAL NOT NULL DEFAULT 0.5,
                    confidence REAL NOT NULL DEFAULT 0.7, created_at REAL NOT NULL,
                    last_accessed_at REAL NOT NULL, last_reinforced_at REAL, event_time REAL,
                    ttl_days REAL NOT NULL DEFAULT 30.0, expires_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active', reinforcement_count INTEGER NOT NULL DEFAULT 0,
                    decay_type TEXT NOT NULL DEFAULT 'exponential', session_id TEXT, persona_id TEXT,
                    metadata TEXT DEFAULT '{}', tier INTEGER NOT NULL DEFAULT 2,
                    source_ids TEXT DEFAULT '[]', reinforcement_state TEXT DEFAULT NULL)""")
                await db.execute(
                    "INSERT INTO memory_atoms (parent_memory_id, content, created_at, last_accessed_at, expires_at)"
                    " VALUES (1, 'old', 1.0, 1.0, 2.0)")
                await db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, metadata TEXT)")
                await db.execute("INSERT INTO documents (text, metadata) VALUES (?, ?)", ("a", json.dumps({"importance": 0.5})))
                await db.execute("INSERT INTO documents (text, metadata) VALUES (?, ?)", ("b", json.dumps({"source": "external"})))
                await db.commit()
            mig = DBMigration(db_path)
            await mig.initialize_version_table()
            await mig._migrate_v10_to_v11()
            async with aiosqlite.connect(db_path) as db:
                cur = await db.execute("SELECT source FROM memory_atoms WHERE id = 1")
                row = await cur.fetchone()
                assert row[0] == "internal"
                cur = await db.execute("SELECT metadata FROM documents ORDER BY id")
                rows = await cur.fetchall()
                m1 = json.loads(rows[0][0])
                m2 = json.loads(rows[1][0])
                assert m1["source"] == "internal"  # 缺失回填
                assert m2["source"] == "external"  # 已有不动
            # 幂等：再跑一遍不炸
            await mig._migrate_v10_to_v11()


class TestRetrievalPenalty:
    """用例3：external 同条件下检索得分 = internal × penalty。"""

    def _make_retriever(self, penalty=0.85):
        return HybridRetriever(None, None, None, {"external_source_penalty": penalty})

    def _fused(self, rrf, meta):
        return FusedResult(doc_id=1, rrf_score=rrf, bm25_score=1.0, vector_score=1.0,
                           content="c", metadata=meta)

    def test_external_ranks_lower(self):
        r = self._make_retriever()
        now = time.time()
        meta = {"importance": 0.7, "create_time": now, "last_access_time": now}
        internal = self._fused(0.9, dict(meta, source="internal"))
        external = self._fused(0.9, dict(meta, source="external"))
        results = r._apply_weighting([internal, external], now)
        by_src = {x.metadata["source"]: x for x in results}
        assert by_src["external"].final_score == pytest.approx(by_src["internal"].final_score * 0.85)
        assert by_src["external"].score_breakdown["source"] == "external"

    def test_invalid_source_falls_back_internal(self):
        """用例4：非法 source 值按 internal 处理（不加惩罚）。"""
        r = self._make_retriever()
        now = time.time()
        meta = {"importance": 0.7, "create_time": now, "last_access_time": now}
        good = self._fused(0.9, dict(meta, source="internal"))
        bad = self._fused(0.9, dict(meta, source="hacker-inject"))
        results = r._apply_weighting([good, bad], now)
        by_pen = {x.score_breakdown["source_penalty"] for x in results}
        assert by_pen == {1.0}  # 两条都不惩罚

    def test_penalty_configurable(self):
        r = self._make_retriever(penalty=0.5)
        assert r.external_source_penalty == 0.5
