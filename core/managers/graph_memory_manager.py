"""Manage graph-memory indexing and synchronization."""

from __future__ import annotations

from typing import Any

from ...storage.graph_store import GraphStore
from ..processors.graph_extractor import GraphExtractor
from ..retrieval.graph_vector_retriever import GraphVectorRetriever


class GraphMemoryManager:
    """Synchronize graph-memory artifacts with the document memory store."""

    def __init__(
        self,
        graph_store: GraphStore,
        graph_vector_retriever: GraphVectorRetriever,
        graph_extractor: GraphExtractor,
    ):
        self.graph_store = graph_store
        self.graph_vector_retriever = graph_vector_retriever
        self.graph_extractor = graph_extractor

    async def index_memory(
        self,
        source_memory_id: int,
        content: str,
        metadata: dict[str, Any] | None,
        atoms: list | None = None,
    ) -> None:
        """Rebuild graph artifacts for one source memory.

        When atoms are provided, each atom independently contributes
        nodes/edges/entries with per-atom confidence scores.
        """
        await self.delete_memory(source_memory_id)

        extracted = self.graph_extractor.extract(
            source_memory_id, content, metadata, atoms
        )
        if not extracted.entries:
            return

        node_key_to_id = await self.graph_store.upsert_nodes(extracted.nodes)

        edge_key_to_id = await self.graph_store.add_edges(
            extracted.edges,
            node_key_to_id,
        )

        entry_ids = await self.graph_store.add_entries(
            extracted.entries,
            node_key_to_id,
            edge_key_to_id,
        )
        if len(entry_ids) != len(extracted.entries):
            raise RuntimeError(
                "graph entry id count mismatch: "
                f"ids={len(entry_ids)}, entries={len(extracted.entries)}"
            )
        entry_vector_doc_ids: dict[int, int] = {}
        try:
            for entry_id, entry in zip(entry_ids, extracted.entries, strict=True):
                vector_doc_id = await self._add_entry_with_retry(
                    entry.content, dict(entry.metadata), entry_id,
                )
                if vector_doc_id is not None:
                    entry_vector_doc_ids[entry_id] = vector_doc_id
        finally:
            await self.graph_store.update_entry_vector_doc_ids(entry_vector_doc_ids)

    async def _add_entry_with_retry(
        self, content: str, metadata: dict[str, Any], entry_id: int
    ) -> int | None:
        """单条图记忆向量插入：瞬态错误退避重试，耗尽则跳过不炸链。

        2026-09-16: zhipu embedding 偶发 503(50505过载) 曾把 index_memory 整链
        炸穿(TRACEBACK)。现改为：瞬态网络类异常重试3次(1.5s/3s/4.5s退避)，
        仍失败则跳过该条——SQLite图数据已落库，向量缺失项由后续rebuild补齐。
        代码bug类异常(ValueError等)不吞，照常抛出。
        """
        import asyncio

        from astrbot.api import logger

        last_err: BaseException | None = None
        for attempt in range(3):
            try:
                return await self.graph_vector_retriever.add_entry(content, metadata)
            except Exception as e:
                if not _is_transient_embed_error(e):
                    raise
                last_err = e
                wait = 1.5 * (attempt + 1)
                logger.warning(
                    "[GraphMemory] entry#%s 向量化瞬态失败(第%d次): %.200s, %.1fs后重试",
                    entry_id, attempt + 1, e, wait,
                )
                await asyncio.sleep(wait)
        logger.warning(
            "[GraphMemory] entry#%s 向量化重试耗尽，跳过(等rebuild补齐): %.200s",
            entry_id, last_err,
        )
        return None


    async def delete_memory(self, source_memory_id: int) -> None:
        """Delete graph artifacts belonging to one source memory."""
        vector_doc_ids = await self.graph_store.delete_memory(source_memory_id)
        for vector_doc_id in vector_doc_ids:
            await self.graph_vector_retriever.delete_entry(vector_doc_id)

    async def batch_delete_memories(self, source_memory_ids: list[int]) -> None:
        """Batch delete graph artifacts for multiple source memories."""
        if not source_memory_ids:
            return
        memory_vec_map = await self.graph_store.batch_delete_memories(source_memory_ids)
        for vector_doc_ids in memory_vec_map.values():
            for vector_doc_id in vector_doc_ids:
                await self.graph_vector_retriever.delete_entry(vector_doc_id)


__all__ = ["GraphMemoryManager"]


def _is_transient_embed_error(e: BaseException) -> bool:
    """仅瞬态类(过载/超时/连接)值得重试；其余异常照常抛出。"""
    name = type(e).__name__
    if name in (
        "InternalServerError", "APIConnectionError", "APITimeoutError",
        "TimeoutError", "ReadTimeout", "ConnectTimeout",
    ):
        return True
    status = getattr(e, "status_code", None)
    if status is None:
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
    if status in (429, 500, 502, 503, 504):
        return True
    msg = str(e).lower()
    return any(k in msg for k in ("overloaded", "timeout", "timed out", "connection", "temporarily", "50505"))
