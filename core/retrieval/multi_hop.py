# -*- coding: utf-8 -*-
"""P1-3 多跳检索扩展器：先图后向量（causal_chain 优先）。

从混合检索命中的种子记忆出发，沿 v2 memory_causality 因果边扩展邻居，
把"相关但未直接命中"的记忆以折扣分并入结果尾部。

设计原则：
- 默认关闭（multi_hop_enabled=False），主链零影响，橘子点头再开
- 只读：documents / memory_causality 全部只读查询，不写任何表
- 降级：任何异常返回原结果，绝不阻断主检索链
- 防环：seen 集合 + max_hops 上限 + max_expand 截断
"""
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from astrbot.api import logger


@dataclass
class HopCandidate:
    """一个多跳邻居候选"""
    doc_id: str
    content: str
    metadata: dict
    seed_doc_id: str
    hops: int
    seed_score: float


class MultiHopExpander:
    """多跳扩展器：检索结果的因果邻居增强。"""

    def __init__(self, main_db_path: str, v2_db_path: str, config: Optional[dict] = None):
        config = config or {}
        self.main_db_path = main_db_path
        self.v2_db_path = v2_db_path
        self.enabled = bool(config.get("multi_hop_enabled", False))
        self.max_hops = max(1, int(config.get("multi_hop_max_hops", 1)))
        self.seed_count = max(1, int(config.get("multi_hop_seed_count", 5)))
        self.max_expand = max(0, int(config.get("multi_hop_max_expand", 5)))
        self.hop_decay = float(config.get("multi_hop_hop_decay", 0.5))
        self.min_importance = float(config.get("multi_hop_min_importance", 0.0))

    # ── 内部工具 ──

    def _connect_main(self):
        return sqlite3.connect(f"file:{self.main_db_path}?mode=ro", uri=True)

    def _connect_v2(self):
        return sqlite3.connect(f"file:{self.v2_db_path}?mode=ro", uri=True)

    def _map_doc_ids(self, main_conn, doc_ids) -> dict:
        """doc_id -> documents.id 映射。"""
        if not doc_ids:
            return {}
        ph = ",".join("?" * len(doc_ids))
        rows = main_conn.execute(
            f"SELECT doc_id, id FROM documents WHERE doc_id IN ({ph})", list(doc_ids)
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def _get_neighbors(self, v2_conn, memory_id: int) -> list:
        """一跳双向邻居：以它为因的果 + 它的因。返回 [(neighbor_id, relation), ...]"""
        out = []
        for r in v2_conn.execute(
            "SELECT memory_id FROM memory_causality WHERE pre_cause_id = ?", (memory_id,),
        ):
            out.append((r[0], "effect"))
        for r in v2_conn.execute(
            "SELECT pre_cause_id FROM memory_causality WHERE memory_id = ? AND pre_cause_id IS NOT NULL",
            (memory_id,),
        ):
            out.append((r[0], "cause"))
        return out

    def _load_docs(self, main_conn, ids) -> dict:
        """按 documents.id 批量取 doc_id/text/metadata。"""
        if not ids:
            return {}
        ph = ",".join("?" * len(ids))
        rows = main_conn.execute(
            f"SELECT id, doc_id, text, metadata FROM documents WHERE id IN ({ph})", list(ids)
        ).fetchall()
        out = {}
        for r in rows:
            try:
                meta = json.loads(r[3]) if r[3] else {}
            except Exception:
                meta = {}
            out[r[0]] = {"doc_id": r[1], "content": r[2] or "", "metadata": meta}
        return out

    # ── 主入口 ──

    def expand(self, results: list) -> list:
        """对混合检索结果做多跳扩展。任何异常降级返回原结果。

        Args:
            results: HybridResult 列表（原顺序保持不变，追加邻居在尾部）
        Returns:
            扩展后的结果列表（原结果在前，邻居按分数降序追加）
        """
        if not self.enabled or not results:
            return results
        t0 = time.time()
        try:
            return self._do_expand(results)
        except Exception as e:
            logger.warning(f"[MultiHop] 扩展失败(降级返回原结果): {e}")
            return results
        finally:
            dt = (time.time() - t0) * 1000
            if dt > 200:
                logger.warning(f"[MultiHop] 扩展耗时 {dt:.0f}ms 偏慢")

    def _do_expand(self, results: list) -> list:
        seeds = results[: self.seed_count]
        existing_doc_ids = {getattr(r, "doc_id", None) for r in results}

        with self._connect_main() as mc, self._connect_v2() as vc:
            id_map = self._map_doc_ids(mc, [s.doc_id for s in seeds])
            if not id_map:
                return results

            # BFS 沿因果边扩展，记录 (doc_id, seed_doc_id, hops, seed_score)
            frontier = []  # (memory_id, seed_doc_id, hops, seed_score)
            for seed in seeds:
                mid = id_map.get(seed.doc_id)
                if mid:
                    frontier.append((mid, seed.doc_id, 1, float(seed.final_score)))

            candidates: dict = {}  # doc_id -> HopCandidate
            seen_ids = set(id_map.values())
            expanded = 0

            while frontier and expanded < self.max_expand:
                mid, seed_doc, hops, seed_score = frontier.pop(0)
                if hops > self.max_hops:
                    continue
                for nb_id, relation in self._get_neighbors(vc, mid):
                    if nb_id in seen_ids:
                        continue
                    seen_ids.add(nb_id)
                    frontier.append((nb_id, seed_doc, hops + 1, seed_score))
                    docs = self._load_docs(mc, [nb_id])
                    doc = docs.get(nb_id)
                    if not doc:
                        continue
                    if doc["doc_id"] in existing_doc_ids or doc["doc_id"] in candidates:
                        continue
                    imp = float(doc["metadata"].get("importance", 0.5) or 0.5)
                    if imp < self.min_importance:
                        continue
                    candidates[doc["doc_id"]] = HopCandidate(
                        doc_id=doc["doc_id"],
                        content=doc["content"],
                        metadata=doc["metadata"],
                        seed_doc_id=seed_doc,
                        hops=hops,
                        seed_score=seed_score,
                    )
                    expanded += 1
                    if expanded >= self.max_expand:
                        break

            if not candidates:
                return results

            # 构造 HybridResult 形状的补充条目（鸭子类型，字段与 HybridResult 对齐）
            hop_results = []
            for c in candidates.values():
                score = max(0.05, c.seed_score * (self.hop_decay ** c.hops))
                hr = type(results[0])(
                    doc_id=c.doc_id,
                    final_score=score,
                    rrf_score=score,
                    bm25_score=None,
                    vector_score=None,
                    content=c.content,
                    metadata=c.metadata,
                    score_breakdown={
                        "multi_hop": True,
                        "hops": c.hops,
                        "seed_doc_id": c.seed_doc_id,
                        "seed_score": round(c.seed_score, 4),
                        "hop_discount": round(self.hop_decay ** c.hops, 4),
                    },
                )
                hop_results.append(hr)

            hop_results.sort(key=lambda x: x.final_score, reverse=True)
            logger.info(
                f"[MultiHop] 扩展 {len(hop_results)} 条因果邻居 "
                f"(seeds={len(id_map)}, hops<= {self.max_hops})"
            )
            return list(results) + hop_results