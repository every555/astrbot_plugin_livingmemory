"""Time-aware atom-level retriever for memory atoms.

v2.5: confidence now participates in scoring via a confidence_weight
factor.  The formula is:

    final_score = base_score * temporal_score * confidence_weight

where confidence_weight = confidence_floor + (1 - confidence_floor) * confidence.

This ensures low-confidence atoms are demoted but never fully zeroed out.

v5.4: Recency Window Boost — 近期高频访问的原子获得额外加成
    final_score = base_score * temporal_score * confidence_weight * recency_boost

recency_boost = 1.0 + recency_weight * recency_signal
recency_signal 基于 last_accessed_at 距今的时间衰减
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from ...storage.atom_store import AtomStore
from ..models.memory_atom import MemoryAtom

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AtomRetrievalResult:
    """A single atom retrieval result with temporal + confidence scoring."""

    atom_id: int
    parent_memory_id: int
    content: str
    base_score: float  # BM25 or vector similarity
    temporal_score: float  # decay multiplier
    confidence_weight: float  # confidence-derived multiplier (v2.5)
    final_score: float  # base_score * temporal_score * confidence_weight * recency_boost
    atom_type: str
    importance: float
    confidence: float
    ttl_days: float
    decay_type: str
    metadata: dict[str, Any]
    recency_boost: float = 1.0  # v5.4: 活跃窗口加成系数


class AtomRetriever:
    """Retrieve memory atoms with time-aware and confidence-aware scoring.

    v2.5: Scoring formula now includes confidence as a multiplier:

        final_score = base_score * temporal_score * confidence_weight

    confidence_weight = floor + (1 - floor) * confidence

    - confidence=1.0 → weight=1.0 (no penalty)
    - confidence=0.7 → weight=0.79 (mild penalty)
    - confidence=0.5 → weight=0.65 (moderate penalty)
    - confidence=0.0 → weight=floor (never fully zeroed)

    v5.4: Recency Window Boost

        final_score = base_score * temporal_score * confidence_weight * recency_boost

    recency_boost 对最近 N 小时内被访问过的原子给予加成，
    使得"活跃窗口"内的记忆更容易被召回。
    """

    def __init__(
        self,
        atom_store: AtomStore,
        config: dict[str, Any] | None = None,
    ):
        self.atom_store = atom_store
        self.config = config or {}
        # Floor: minimum confidence weight so low-confidence atoms still surface
        self.confidence_floor = float(self.config.get("confidence_floor", 0.3))

        # v5.4: 活跃窗口配置
        self.recency_window_hours = float(
            self.config.get("recency_window_hours", 6.0)
        )  # 活跃窗口大小（小时）
        self.recency_max_boost = float(
            self.config.get("recency_max_boost", 0.3)
        )  # 最大加成 (0.3 = +30%)

    def _compute_confidence_weight(self, confidence: float) -> float:
        """Map raw confidence [0,1] to a multiplicative weight [floor, 1]."""
        c = max(0.0, min(1.0, confidence))
        return self.confidence_floor + (1.0 - self.confidence_floor) * c

    def _compute_recency_boost(
        self, last_accessed_at: float, reference_time: float | None = None
    ) -> float:
        """v5.4: 计算活跃窗口加成

        在 recency_window_hours 内被访问过的原子获得线性递减的加成：
        - 刚刚访问 → 1.0 + recency_max_boost
        - window 边缘 → 1.0
        - window 外 → 1.0 (不加成)
        """
        now = reference_time or time.time()
        elapsed_hours = (now - last_accessed_at) / 3600.0

        if elapsed_hours < 0:
            return 1.0  # 时钟异常，不加成

        if elapsed_hours >= self.recency_window_hours:
            return 1.0  # 超出窗口，不加成

        # 线性递减: 0h → max_boost, window_edge → 0
        ratio = 1.0 - (elapsed_hours / self.recency_window_hours)
        return 1.0 + self.recency_max_boost * ratio

    async def search(
        self,
        query: str,
        k: int = 10,
        session_id: str | None = None,
        persona_id: str | None = None,
    ) -> list[AtomRetrievalResult]:
        """Search atoms by FTS, score by relevance, temporal decay, and confidence."""
        atoms = await self.atom_store.search_fts(
            query=query,
            limit=max(k * 2, k),
            session_id=session_id,
            persona_id=persona_id,
        )

        now = time.time()
        results: list[AtomRetrievalResult] = []
        recency_boosted_count = 0

        for atom in atoms:
            base_score = float(atom.metadata.get("bm25_score", 0.5))
            temporal_score = float(atom.metadata.get("temporal_score", 1.0))
            confidence_weight = self._compute_confidence_weight(atom.confidence)
            recency_boost = self._compute_recency_boost(atom.last_accessed_at, now)

            if recency_boost > 1.0:
                recency_boosted_count += 1

            final_score = base_score * temporal_score * confidence_weight * recency_boost
            results.append(
                AtomRetrievalResult(
                    atom_id=atom.atom_id,
                    parent_memory_id=atom.parent_memory_id,
                    content=atom.content,
                    base_score=round(base_score, 4),
                    temporal_score=round(temporal_score, 4),
                    confidence_weight=round(confidence_weight, 4),
                    final_score=round(final_score, 4),
                    atom_type=atom.atom_type.value,
                    importance=round(atom.importance, 4),
                    confidence=round(atom.confidence, 4),
                    ttl_days=round(atom.ttl_days, 2),
                    decay_type=atom.decay_type.value,
                    metadata=dict(atom.metadata),
                    recency_boost=round(recency_boost, 4),
                )
            )

        results.sort(key=lambda r: r.final_score, reverse=True)

        # Log confidence + recency impact for top results (debug level)
        if results and logger.isEnabledFor(logging.DEBUG):
            top = results[0]
            logger.debug(
                f"[AtomRetriever] top result: base={top.base_score:.3f} "
                f"temporal={top.temporal_score:.3f} conf_w={top.confidence_weight:.3f} "
                f"recency={top.recency_boost:.3f} final={top.final_score:.3f} | "
                f"recency_boosted={recency_boosted_count}/{len(results)}"
            )

        return results[:k]

    async def get_atoms_for_memory(self, parent_memory_id: int) -> list[MemoryAtom]:
        """Return all atoms belonging to a parent memory."""
        return await self.atom_store.get_by_parent(parent_memory_id)

    async def touch(self, atom_id: int) -> None:
        """Update access time for an atom."""
        await self.atom_store.touch(atom_id)


__all__ = ["AtomRetriever", "AtomRetrievalResult"]
