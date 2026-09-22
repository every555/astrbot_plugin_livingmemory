"""v3.5 Rerank 精排层测试：Reranker 单测（降级矩阵）+ 接线集成断言。"""

import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT.parent))

from astrbot_plugin_livingmemory.core.retrieval.reranker import Reranker


@dataclass
class FakeResult:
    """模拟 HybridResult（reranker 只依赖 content/score_breakdown 字段）"""

    doc_id: int
    content: str
    final_score: float = 0.5
    score_breakdown: dict = field(default_factory=dict)


class FakeRerankItem:
    def __init__(self, index, relevance_score):
        self.index = index
        self.relevance_score = relevance_score


class FakeProvider:
    def __init__(self, items=None, error=None, delay=0.0):
        self.items = items or []
        self.error = error
        self.delay = delay
        self.calls = []

    async def rerank(self, query, documents, top_n=None):
        self.calls.append((query, len(documents), top_n))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.items


def _results(n=4):
    return [FakeResult(doc_id=i, content=f"记忆内容{i}") for i in range(n)]


# ── 单测：降级矩阵 ─────────────────────────────────────


def test_disabled_passthrough():
    """开关关闭 → 原样返回（默认行为与升级前一致）"""
    r = Reranker(lambda: FakeProvider(), {"rerank_enabled": False})
    rs = _results()
    out = asyncio.run(r.rerank("query", rs))
    assert out is rs
    assert r.stats.degraded_calls == 1


def test_no_provider_passthrough():
    """provider 缺失 → 原样返回"""
    r = Reranker(lambda: None, {"rerank_enabled": True})
    rs = _results()
    out = asyncio.run(r.rerank("query", rs))
    assert out is rs


def test_empty_or_single_passthrough():
    """空/单条结果 → 直通（无精排价值）"""
    r = Reranker(lambda: FakeProvider(), {"rerank_enabled": True})
    assert asyncio.run(r.rerank("q", [])) == []
    single = _results(1)
    assert asyncio.run(r.rerank("q", single)) is single


def test_normal_rerank_reorders():
    """正常精排：按 relevance_score 重排，未命中垫尾不丢结果"""
    # 4条候选，rerank 认为 index=2 最相关(0.9)、index=0 次之(0.7)，1/3 未返回
    provider = FakeProvider(items=[FakeRerankItem(2, 0.9), FakeRerankItem(0, 0.7)])
    r = Reranker(lambda: provider, {"rerank_enabled": True})
    rs = _results(4)
    out = asyncio.run(r.rerank("夜班 腿肿", rs))
    assert [x.doc_id for x in out] == [2, 0, 1, 3]  # 命中前排 + 原序垫尾
    assert len(out) == 4  # 不丢结果
    assert out[0].score_breakdown.get("rerank_score") == pytest.approx(0.9)
    assert r.stats.success_calls == 1 and r.stats.fail_count == 0


def test_exception_degrades():
    """provider 抛异常 → 原样返回 + 记失败"""
    provider = FakeProvider(error=RuntimeError("API 挂了"))
    r = Reranker(lambda: provider, {"rerank_enabled": True})
    rs = _results()
    out = asyncio.run(r.rerank("q", rs))
    assert out is rs and r.stats.fail_count == 1


def test_timeout_degrades():
    """超时 → 降级直通"""
    provider = FakeProvider(delay=1.0)
    r = Reranker(lambda: provider, {"rerank_enabled": True, "rerank_timeout": 0.05})
    rs = _results()
    out = asyncio.run(r.rerank("q", rs))
    assert out is rs


def test_breaker_cooldown():
    """熔断：连续失败 3 次进入冷却，冷却期内直通不再调 API"""
    provider = FakeProvider(error=RuntimeError("持续故障"))
    r = Reranker(
        lambda: provider,
        {"rerank_enabled": True, "rerank_fail_threshold": 3, "rerank_cooldown_secs": 60.0},
    )
    rs = _results()
    for _ in range(3):
        asyncio.run(r.rerank("q", rs))
    assert r.stats.fail_count == 3 and r.stats.cooldown_until > time.time()
    assert len(provider.calls) == 3
    out = asyncio.run(r.rerank("q", rs))  # 冷却期内：直通不调API
    assert out is rs and len(provider.calls) == 3  # 没多打一次
    # 模拟冷却结束 + provider 恢复 → 自动恢复精排
    r.stats.cooldown_until = time.time() - 0.1
    provider.error = None
    provider.items = [FakeRerankItem(0, 0.5)]
    out2 = asyncio.run(r.rerank("q", rs))
    assert len(provider.calls) == 4 and r.stats.fail_count == 0


def test_lazy_provider_swap():
    """惰性 getter：换 provider 即时生效（WebUI 换模型不重启）"""
    p1 = FakeProvider(items=[FakeRerankItem(0, 0.1)])
    p2 = FakeProvider(items=[FakeRerankItem(1, 0.99)])
    current = {"p": p1}
    r = Reranker(lambda: current["p"], {"rerank_enabled": True})
    rs = _results(2)
    out1 = asyncio.run(r.rerank("q", rs))
    assert out1[0].doc_id == 0
    current["p"] = p2  # 热切换
    out2 = asyncio.run(r.rerank("q", rs))
    assert out2[0].doc_id == 1


# ── 集成断言：接线三件套 ───────────────────────────────


def test_wiring_memory_engine_pipeline():
    """memory_engine：Reranker 构造 + 管道序（rerank → multi_hop → eligibility → self_check）"""
    me = (_PLUGIN_ROOT / "core" / "managers" / "memory_engine.py").read_text(encoding="utf-8")
    assert "from ..retrieval.reranker import Reranker" in me, "Reranker 构造缺失"
    i_rerank = me.index("await self.reranker.rerank(query, results)")
    i_mhop = me.index("self.multi_hop_expander.expand(results)")
    i_elig = me.index("self.eligibility_filter.filter_results(results)")
    i_check = me.index("self.self_checker.check(results)")
    assert i_rerank < i_mhop < i_elig < i_check, "管道顺序错误：rerank 必须在 multi_hop 之前"


def test_wiring_initializer_chain():
    """initializer：rerank provider 加载 + getter + engine 注入 + 配置接线"""
    pi = (_PLUGIN_ROOT / "core" / "plugin_initializer.py").read_text(encoding="utf-8")
    assert "RerankProvider" in pi, "import/类型校验缺失"
    assert "provider_settings.rerank_provider_id" in pi, "配置读取缺失"
    assert "def get_rerank_provider" in pi, "惰性 getter 缺失"
    assert "_rerank_provider_getter = self.get_rerank_provider" in pi, "engine 注入缺失"
    assert "recall_engine.rerank_enabled" in pi, "engine config 接线缺失"


def test_wiring_schema_fields():
    """schema：7 个 rerank 字段齐备"""
    import json

    sch = json.loads((_PLUGIN_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    rc = sch["recall_engine"]["items"]
    for k in ["rerank_enabled", "rerank_top_n", "rerank_timeout", "rerank_fail_threshold", "rerank_cooldown_secs"]:
        assert k in rc, f"recall_engine.{k} 缺失"
    assert "rerank_provider_id" in sch["provider_settings"]["items"]
    assert rc["rerank_enabled"]["default"] is False, "默认必须关闭（升级后行为不变）"
