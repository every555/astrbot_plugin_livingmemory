"""P1 升级验收测试：② 重要性护盾衰减 + ③ 多跳扩展器。"""
import json
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from astrbot_plugin_livingmemory.core.retrieval.hybrid_retriever import (
    HybridResult,
    HybridRetriever,
)
from astrbot_plugin_livingmemory.core.retrieval.multi_hop import MultiHopExpander
from astrbot_plugin_livingmemory.core.retrieval.rrf_fusion import FusedResult


def _fused(rrf, meta, doc_id=1):
    return FusedResult(doc_id=doc_id, rrf_score=rrf, bm25_score=1.0, vector_score=1.0,
                       content="c", metadata=meta)


YEAR_AGO = time.time() - 365 * 86400


class TestImportanceDecayShield:
    """P1-2: 高重要性记忆衰减更慢。"""

    def _run(self, shield, importances):
        r = HybridRetriever(None, None, None, {"importance_decay_shield": shield})
        now = time.time()
        fused = [_fused(0.9, {"importance": imp, "create_time": YEAR_AGO}, doc_id=i + 1)
                 for i, imp in enumerate(importances)]
        return r._apply_weighting(fused, now)

    def test_shield_slows_decay_for_important(self):
        results = self._run(0.7, [0.9, 0.2])
        hi = {x.metadata["importance"]: x for x in results}
        assert hi[0.9].score_breakdown["recency_weight"] > hi[0.2].score_breakdown["recency_weight"]
        assert hi[0.9].score_breakdown["effective_decay_rate"] < hi[0.2].score_breakdown["effective_decay_rate"]

    def test_shield_zero_restores_uniform_decay(self):
        results = self._run(0.0, [0.9, 0.2])
        hi = {x.metadata["importance"]: x for x in results}
        assert hi[0.9].score_breakdown["recency_weight"] == pytest.approx(
            hi[0.2].score_breakdown["recency_weight"]
        )

    def test_default_config_has_shield(self):
        r = HybridRetriever(None, None, None, {})
        assert r.importance_decay_shield == pytest.approx(0.7)


class TestMultiHopExpander:
    """P1-3: 因果邻居扩展。"""

    @staticmethod
    def _make_dbs(td):
        main_db = os.path.join(td, "livingmemory.db")
        v2_db = os.path.join(td, "v2_memory.db")
        mc = sqlite3.connect(main_db)
        mc.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "doc_id TEXT UNIQUE, text TEXT, metadata TEXT, created_at REAL, updated_at REAL)")
        docs = [
            ("seedA", "种子记忆A", {"importance": 0.8}),
            ("nbrB", "邻居记忆B", {"importance": 0.6}),
            ("isolated", "孤岛记忆C", {"importance": 0.5}),
        ]
        for i, (d, t, m) in enumerate(docs, 1):
            mc.execute("INSERT INTO documents(id,doc_id,text,metadata) VALUES(?,?,?,?)",
                       (i, d, t, json.dumps(m)))
        mc.commit()
        mc.close()
        vc = sqlite3.connect(v2_db)
        vc.execute("CREATE TABLE memory_causality (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "memory_id INTEGER, pre_cause_id INTEGER, role TEXT, created_at REAL)")
        # 边: 2(因) -> 1(果): seedA 与 nbrB 相连, isolated 无边
        vc.execute("INSERT INTO memory_causality(memory_id,pre_cause_id,role,created_at) "
                   "VALUES(1,2,'result',1.0)")
        vc.commit()
        vc.close()
        return main_db, v2_db

    @staticmethod
    def _hr(doc_id, score=0.8):
        return HybridResult(doc_id=doc_id, final_score=score, rrf_score=score,
                            bm25_score=None, vector_score=None, content="", metadata={})

    def _expander(self, td, **kw):
        main_db, v2_db = self._make_dbs(td)
        cfg = {"multi_hop_enabled": True}
        cfg.update(kw)
        return MultiHopExpander(main_db, v2_db, cfg)

    def test_expand_appends_neighbor(self, tmp_path):
        ex = self._expander(str(tmp_path))
        out = ex.expand([self._hr("seedA", 0.8)])
        assert len(out) == 2
        assert out[0].doc_id == "seedA", "原结果必须在前"
        nb = out[1]
        assert nb.doc_id == "nbrB"
        assert nb.score_breakdown["multi_hop"] is True
        assert nb.score_breakdown["seed_doc_id"] == "seedA"
        assert nb.final_score == pytest.approx(0.8 * 0.5, abs=1e-6)

    def test_disabled_returns_original(self, tmp_path):
        main_db, v2_db = self._make_dbs(str(tmp_path))
        ex = MultiHopExpander(main_db, v2_db, {"multi_hop_enabled": False})
        src = [self._hr("seedA")]
        out = ex.expand(src)
        assert out is src

    def test_existing_dedup(self, tmp_path):
        ex = self._expander(str(tmp_path))
        out = ex.expand([self._hr("seedA", 0.8), self._hr("nbrB", 0.7)])
        assert [x.doc_id for x in out] == ["seedA", "nbrB"], "邻居已在结果中不得重复"

    def test_missing_v2_db_degrades(self, tmp_path):
        main_db, v2_db = self._make_dbs(str(tmp_path))
        os.remove(v2_db)
        ex = MultiHopExpander(main_db, v2_db, {"multi_hop_enabled": True})
        src = [self._hr("seedA")]
        out = ex.expand(src)
        assert out == src

    def test_bogus_paths_degrade(self, tmp_path):
        ex = MultiHopExpander(os.path.join(str(tmp_path), "nope1.db"),
                              os.path.join(str(tmp_path), "nope2.db"),
                              {"multi_hop_enabled": True})
        src = [self._hr("seedA")]
        assert ex.expand(src) == src

    def test_isolated_seed_no_expand(self, tmp_path):
        ex = self._expander(str(tmp_path))
        out = ex.expand([self._hr("isolated", 0.8)])
        assert len(out) == 1