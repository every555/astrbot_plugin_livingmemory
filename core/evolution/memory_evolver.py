# -*- coding: utf-8 -*-
"""A-MEM 演化 v1（P1-①，升级路线第4条）。

新记忆入库后异步做一次向量检索，把相似度≥阈值的旧记忆用 variant_of 边
串进 memory_causality（role='variant_of'，trigger_type='a_mem_evolution'）。
multi_hop 沿 causality 边扩展，新边自动可走——A-MEM 喂多跳，闭环。

设计红线（照 A-MEM 论文但收着做）：
- 只建边，不改写任何旧记忆内容（改写归 dream 归并管，橘子裁决）
- fire-and-forget：失败只记日志，绝不影响主写入链
- 边可逆：删边即回滚；多跳没开时这些边躺着，零影响"""

import asyncio
import logging

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD = 0.80
DEFAULT_TOP_K = 6


class MemoryEvolver:
    def __init__(self, engine, enabled=True, threshold=DEFAULT_THRESHOLD, top_k=DEFAULT_TOP_K):
        self._engine = engine
        self.enabled = enabled
        self.threshold = threshold
        self.top_k = top_k

    async def link_variants(self, new_doc_id, content, persona_id=None, session_id=None):
        """找相似旧记忆建 variant_of 边。返回建边成功的 doc_id 列表；无事发生返回 None。"""
        if not self.enabled:
            return None
        try:
            retriever = getattr(self._engine, "hybrid_retriever", None)
            if retriever is None:
                return None
            results = await retriever.search(content, k=self.top_k + 1)
            linked = []
            for r in results or []:
                doc_id = getattr(r, "doc_id", None)
                score = float(getattr(r, "score", 0.0) or 0.0)
                if doc_id is None or doc_id == new_doc_id:
                    continue
                if score < self.threshold:
                    continue
                if await self._add_edge(new_doc_id, doc_id, score, persona_id, session_id):
                    linked.append(doc_id)
            return linked or None
        except Exception as e:
            logger.warning(f"[MemoryEvolver] 互链失败(降级): {e}")
            return None

    async def _add_edge(self, new_id, old_id, similarity, persona_id, session_id):
        v2 = getattr(self._engine, "v2_store", None) or getattr(self._engine, "v2_engine", None)
        if v2 is None or not hasattr(v2, "add_causality"):
            return False
        try:
            await v2.add_causality(
                memory_id=new_id,
                persona_id=persona_id,
                session_id=session_id,
                trigger_type="a_mem_evolution",
                trigger_message="",
                pre_cause_id=old_id,
                role="variant_of",
                context_snapshot={"similarity": round(similarity, 3), "evolver": "a_mem_v1"},
            )
            return True
        except Exception as e:
            logger.warning(f"[MemoryEvolver] 建边失败(跳过 id={old_id}): {e}")
            return False

    def schedule(self, new_doc_id, content, persona_id=None, session_id=None) -> bool:
        """fire-and-forget：add_memory 尾部调用，无运行循环时安静放弃。"""
        if not self.enabled:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        loop.create_task(self.link_variants(new_doc_id, content, persona_id, session_id))
        return True