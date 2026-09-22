"""P1-① Sleeptime 互链器（总案 v3.0 病根4 补全）

Dream 归并（helper DreamEngine）只在主库 documents.metadata 标 merged_into，不建边——
多跳检索沿 memory_causality 扩展时走不动归并关系。本模块由本体预言回溯循环每轮调用：
扫未处理的 merged 标记 → 补 variant_of 边进 memory_causality
(role=variant_of, trigger_type=sleeptime_consolidate)，多跳自动可达。

设计红线（照 memory_evolver 同款家规）：
- 只建边，不改写任何记忆内容（原句全保留=铁律零删除）
- 边方向与演化器同构：memory_id=merged(后来的重复), pre_cause_id=keeper(保留原句)
- 免疫降级：任何异常只记日志，绝不影响预言循环
- 幂等去重：v2 库 sleeptime_links 表，已建边不重复建；边可逆(去 archived_ 前缀/删边即回滚)"""

import asyncio
import json
import logging
import sqlite3

logger = logging.getLogger("sleeptime_linker")


class SleeptimeLinker:
    """消费 dream 归并标记，补 variant_of 因果边（同步扫库+异步建边）。"""

    def __init__(self, main_db_path: str, v2_db_path: str, store=None):
        self.main_db_path = str(main_db_path or "")
        self.v2_db_path = str(v2_db_path or "")
        self.store = store
        self.enabled = bool(self.main_db_path and self.v2_db_path)

    # ━━ 同步层（WAL 并发读安全，P0-② 验证过同款姿势）━━

    def _scan_merged_pairs(self, limit: int = 500) -> list[dict]:
        """扫主库 documents 带 merged_into 标记的记忆对。"""
        conn = sqlite3.connect(self.main_db_path, timeout=10)
        try:
            rows = conn.execute(
                "SELECT id, metadata FROM documents "
                "WHERE json_extract(metadata, '$.merged_into') IS NOT NULL "
                "ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
        pairs = []
        for doc_id, meta_raw in rows:
            try:
                meta = json.loads(meta_raw or "{}")
                keeper_raw = meta.get("merged_into")
                if keeper_raw is None or str(doc_id) == str(keeper_raw):
                    continue
                pairs.append({
                    "merged_id": int(doc_id),
                    "keeper_id": int(keeper_raw),
                    "similarity": float(meta.get("merge_similarity") or 0.0),
                    "persona_id": meta.get("persona_id"),
                })
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        return pairs

    def _ensure_done_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sleeptime_links ("
            " merged_id INTEGER PRIMARY KEY, keeper_id INTEGER NOT NULL,"
            " similarity REAL DEFAULT 0, linked_at REAL NOT NULL)"
        )

    def _load_done_ids(self) -> set[int]:
        conn = sqlite3.connect(self.v2_db_path, timeout=10)
        try:
            self._ensure_done_table(conn)
            rows = conn.execute("SELECT merged_id FROM sleeptime_links").fetchall()
            return {int(r[0]) for r in rows}
        finally:
            conn.close()

    def _mark_done(self, pair: dict) -> bool:
        import time as _t
        conn = sqlite3.connect(self.v2_db_path, timeout=10)
        try:
            self._ensure_done_table(conn)
            conn.execute(
                "INSERT OR REPLACE INTO sleeptime_links (merged_id, keeper_id, similarity, linked_at)"
                " VALUES (?, ?, ?, ?)",
                (pair["merged_id"], pair["keeper_id"], pair["similarity"], _t.time()),
            )
            conn.commit()
            return True
        except sqlite3.Error as e:
            logger.warning(f"[SleeptimeLinker] 去重表写入失败(跳过 {pair['merged_id']}): {e}")
            return False
        finally:
            conn.close()

    # ━━ 异步层（对外入口）━━

    async def _link_pair(self, pair: dict) -> bool:
        store = self.store
        if store is None or not hasattr(store, "add_causality"):
            return False
        try:
            await store.add_causality(
                memory_id=pair["merged_id"],
                persona_id=pair.get("persona_id"),
                session_id=None,
                trigger_type="sleeptime_consolidate",
                trigger_message="",
                pre_cause_id=pair["keeper_id"],
                role="variant_of",
                context_snapshot={
                    "similarity": pair["similarity"],
                    "linker": "sleeptime_v1",
                    "source": "dream_consolidate",
                },
            )
            return True
        except BaseException as e:
            logger.warning(f"[SleeptimeLinker] 建边失败(跳过 {pair['merged_id']}): {e}")
            return False

    async def link_once(self) -> dict:
        """跑一轮互链（预言回溯循环每轮调用）。免疫降级，绝不外抛。"""
        result = {"scanned": 0, "linked": 0, "skipped": 0}
        if not self.enabled:
            return result
        try:
            pairs = await asyncio.to_thread(self._scan_merged_pairs)
            result["scanned"] = len(pairs)
            if not pairs:
                return result
            done = await asyncio.to_thread(self._load_done_ids)
            for p in pairs:
                if p["merged_id"] in done:
                    result["skipped"] += 1
                    continue
                if await self._link_pair(p):
                    if await asyncio.to_thread(self._mark_done, p):
                        result["linked"] += 1
            if result["linked"]:
                logger.info(
                    f"[SleeptimeLinker] 补建 variant_of 边 {result['linked']} 条"
                    f" (扫描 {result['scanned']}, 跳过 {result['skipped']})"
                )
        except BaseException as e:
            logger.warning(f"[SleeptimeLinker] 互链异常(降级): {e}")
        return result
