"""P1-① A-MEM 演化 v1 测试：入库后向量互链，建 variant_of 边（只建边不改旧记忆）。"""
import asyncio
import os
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from astrbot_plugin_livingmemory.core.evolution.memory_evolver import MemoryEvolver


def _result(doc_id, score):
    return NS(doc_id=doc_id, score=score)


class FakeRetriever:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def search(self, query, k=10, **kw):
        self.calls.append((query, k))
        return self.results


class FakeV2Store:
    def __init__(self):
        self.edges = []

    async def add_causality(self, memory_id, persona_id, session_id,
                            trigger_type="agent_tool", trigger_message="",
                            pre_cause_id=None, role="fact", context_snapshot=None):
        self.edges.append(dict(memory_id=memory_id, pre_cause_id=pre_cause_id,
                               role=role, trigger_type=trigger_type,
                               context_snapshot=context_snapshot))
        return len(self.edges)


def _evolver(retriever, v2, enabled=True):
    engine = NS(hybrid_retriever=retriever, v2_store=v2)
    return MemoryEvolver(engine, enabled=enabled)


class TestLinkVariants:
    def test_links_similar_old_memory(self):
        r = FakeRetriever([_result(2312, 0.88), _result(2300, 0.62)])
        v2 = FakeV2Store()
        linked = asyncio.run(_evolver(r, v2).link_variants(2321, "新记忆文本"))
        assert linked == [2312]
        assert len(v2.edges) == 1
        edge = v2.edges[0]
        assert edge["memory_id"] == 2321 and edge["pre_cause_id"] == 2312
        assert edge["role"] == "variant_of"
        assert edge["trigger_type"] == "a_mem_evolution"
        assert edge["context_snapshot"]["similarity"] == pytest.approx(0.88)

    def test_skips_self(self):
        r = FakeRetriever([_result(2321, 0.99)])
        v2 = FakeV2Store()
        assert asyncio.run(_evolver(r, v2).link_variants(2321, "x")) is None
        assert v2.edges == []

    def test_below_threshold_no_edge(self):
        r = FakeRetriever([_result(2300, 0.62)])
        v2 = FakeV2Store()
        assert asyncio.run(_evolver(r, v2).link_variants(2321, "x")) is None
        assert v2.edges == []

    def test_disabled_noop(self):
        r = FakeRetriever([_result(2312, 0.95)])
        v2 = FakeV2Store()
        assert asyncio.run(_evolver(r, v2, enabled=False).link_variants(2321, "x")) is None
        assert r.calls == [] and v2.edges == []

    def test_retriever_exception_degrades(self):
        class Boom:
            async def search(self, *a, **k):
                raise RuntimeError("embedding 未就绪")
        v2 = FakeV2Store()
        assert asyncio.run(_evolver(Boom(), v2).link_variants(2321, "x")) is None
        assert v2.edges == []

    def test_edge_failure_skips_and_continues(self):
        class HalfBrokenV2:
            def __init__(self):
                self.n = 0
            async def add_causality(self, **kw):
                self.n += 1
                if self.n == 1:
                    raise sqlite_err()
                return self.n
        def sqlite_err():
            e = Exception("db locked")
            return e
        r = FakeRetriever([_result(2312, 0.9), _result(2300, 0.85)])
        v2 = HalfBrokenV2()
        linked = asyncio.run(_evolver(r, v2).link_variants(2321, "x"))
        assert linked == [2300]  # 第一条失败跳过，第二条仍建成功