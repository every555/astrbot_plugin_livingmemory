"""
v3.5 Rerank 精排层（检索管道第0个后处理，挂在 hybrid 融合之后、multi_hop 之前）

设计要点：
- 惰性 provider_getter：每次调用现取 RerankProvider —— WebUI 换模型即时生效，无需重启
- 免疫降级：provider 缺失/开关关闭/空结果/超时/异常 → 原样返回，绝不阻断检索
- 熔断：连续失败 N 次进入冷却期（默认 3 次/300 秒），防止 API 故障拖慢每次检索
- 全量重排不丢结果：按 relevance_score 重排全部候选，未返回者按原序垫尾

对齐 AstrBot 官方 RerankProvider 接口（astrbot/core/provider/provider.py:415）：
    async def rerank(query: str, documents: list[str], top_n: int | None) -> list[RerankResult]
RerankResult 字段（astrbot/core/provider/entities.py:452）：index: int, relevance_score: float
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from astrbot.api import logger


@dataclass
class RerankStats:
    """运行统计（供健康检查/日志）"""

    total_calls: int = 0
    success_calls: int = 0
    degraded_calls: int = 0  # 降级直通次数（含禁用/provider缺失）
    fail_count: int = 0  # 连续失败计数
    last_error: str = ""
    cooldown_until: float = 0.0  # 熔断冷却截止时间戳


class Reranker:
    """Rerank 精排器：把 RRF 融合后的候选交给 Rerank 模型精排。"""

    def __init__(
        self,
        provider_getter: Callable[[], Any] | None,
        config: dict[str, Any] | None = None,
    ):
        cfg = config or {}
        self._get_provider = provider_getter
        self.enabled: bool = bool(cfg.get("rerank_enabled", False))
        self.top_n: int = int(cfg.get("rerank_top_n", 10) or 10)
        self.timeout: float = float(cfg.get("rerank_timeout", 5.0) or 5.0)
        fail_threshold: int = int(cfg.get("rerank_fail_threshold", 3) or 3)
        cooldown_secs: float = float(cfg.get("rerank_cooldown_secs", 300.0) or 300.0)
        self._fail_threshold = max(1, fail_threshold)
        self._cooldown_secs = max(0.0, cooldown_secs)
        self.stats = RerankStats()

    # ── 内部 ──────────────────────────────────────────────

    def _in_cooldown(self) -> bool:
        return time.time() < self.stats.cooldown_until

    def _mark_fail(self, err: str) -> None:
        st = self.stats
        st.fail_count += 1
        st.last_error = err[:200]
        if st.fail_count >= self._fail_threshold:
            st.cooldown_until = time.time() + self._cooldown_secs
            logger.warning(
                f"[Reranker] 连续失败 {st.fail_count} 次进入熔断冷却 {self._cooldown_secs:.0f}s: {err[:120]}"
            )

    def _mark_success(self) -> None:
        self.stats.fail_count = 0
        self.stats.cooldown_until = 0.0

    # ── 主入口 ────────────────────────────────────────────

    async def rerank(self, query: str, results: list[Any]) -> list[Any]:
        """精排入口。任何异常都降级为原样返回（免疫三原则之一）。"""
        st = self.stats
        st.total_calls += 1

        if not self.enabled:
            st.degraded_calls += 1
            return results
        if not query or not query.strip() or not results or len(results) <= 1:
            st.degraded_calls += 1
            return results
        if self._in_cooldown():
            st.degraded_calls += 1
            return results

        provider = self._get_provider() if self._get_provider else None
        if provider is None or not hasattr(provider, "rerank"):
            st.degraded_calls += 1
            return results

        # 提取候选文本（HybridResult.content / dict 兼容）
        docs: list[str] = []
        for r in results:
            content = getattr(r, "content", None)
            if content is None and isinstance(r, dict):
                content = r.get("content") or r.get("text") or ""
            docs.append(str(content or "")[:2000])  # 单条截断防 token 超限

        try:
            rerank_results = await asyncio.wait_for(
                provider.rerank(query, docs, top_n=len(docs)),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            self._mark_fail(f"rerank 超时(>{self.timeout}s)")
            st.degraded_calls += 1
            return results
        except Exception as e:  # noqa: BLE001 — 免疫降级：任何 provider 异常都不阻断
            self._mark_fail(repr(e))
            st.degraded_calls += 1
            return results

        if not rerank_results:
            self._mark_fail("rerank 返回空结果")
            st.degraded_calls += 1
            return results

        # 按 relevance_score 重排：命中的进前排，未命中的按原序垫尾（不丢结果）
        scored: list[tuple[float, int]] = []
        for item in rerank_results:
            idx = getattr(item, "index", None)
            score = getattr(item, "relevance_score", None)
            if idx is None or score is None:
                continue
            if isinstance(idx, int) and 0 <= idx < len(results):
                scored.append((float(score), idx))
        if not scored:
            self._mark_fail("rerank 结果字段不兼容(index/score缺失)")
            st.degraded_calls += 1
            return results

        self._mark_success()
        st.success_calls += 1

        scored.sort(key=lambda t: -t[0])
        hit_indices = [idx for _, idx in scored]
        tail_indices = [i for i in range(len(results)) if i not in set(hit_indices)]
        reranked = [results[i] for i in hit_indices + tail_indices]

        # 顺手把精排分写进 score_breakdown（供下游观测，不改变字段结构）
        score_map = {idx: sc for sc, idx in scored}
        for i, r in enumerate(reranked[: len(hit_indices)]):
            origin_idx = hit_indices[i]
            bd = getattr(r, "score_breakdown", None)
            if isinstance(bd, dict):
                bd["rerank_score"] = round(score_map.get(origin_idx, 0.0), 4)

        return reranked
