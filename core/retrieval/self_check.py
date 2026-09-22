"""P1-② 检索后逻辑自检（逻辑升级 v3.0 第四处方，反射弧③配套）。

挂载在 MemoryEngine.search_memories 尾部（第4个后处理管道，照 multi_hop/eligibility 模式）：
1. 互检：召回结果两两立场比对（复用 v2.conflict_detector.find_possible_conflict 纯函数）
2. 冲突关联：直连 v2_memory.db 查 memory_conflicts，召回记忆涉及冲突则附证据链
3. 免疫降级：任何异常原样返回 results，绝不阻断检索（与 P0-② 同款设计）

反射弧③：模块级 get_recent_confirmed_alerts() 供 memory_recall 注入"已确认矛盾提醒"。
对上下文传递检查：互检覆盖"召回集内部"，conflict 关联覆盖"历史裁决"，双保险。
"""
from __future__ import annotations

import sqlite3
import time

try:
    from astrbot.api import logger
except ImportError:  # 独立测试环境降级
    import logging

    logger = logging.getLogger("self_check")


class RetrievalSelfCheck:
    """检索后逻辑自检器。零 LLM、纯规则、同步毫秒级。"""

    def __init__(self, v2_db_path: str, config: dict | None = None):
        self.v2_db_path = v2_db_path
        cfg = config or {}
        sc_cfg = cfg.get("retrieval_self_check", {}) if isinstance(cfg, dict) else {}
        self.enabled = bool(sc_cfg.get("enabled", True))
        self.max_results = int(sc_cfg.get("max_results", 20))

    def check(self, results: list) -> list:
        """主入口：互检 + 冲突关联标注，返回同一批 results（对象原地标注）。

        免疫降级：任何异常返回未处理结果，绝不阻断检索主流程。
        """
        if not self.enabled or not results:
            return results
        try:
            self._pairwise_check(results)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[self_check] 互检失败(免疫降级): {e}")
        try:
            self._conflict_link(results)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[self_check] 冲突关联失败(免疫降级): {e}")
        return results

    # ─────────── 1. 互检：召回集内部两两立场比对 ───────────

    def _pairwise_check(self, results: list) -> None:
        from ..v2.conflict_detector import find_possible_conflict  # 延迟导入防循环依赖

        pool = results[: self.max_results]  # 截断防闭包爆炸（铁律：限流）
        n = len(pool)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = pool[i], pool[j]
                hit = find_possible_conflict(
                    getattr(a, "content", ""), getattr(b, "content", "")
                )
                if hit:
                    reason = str(hit.get("reason", ""))[:80]
                    a.conflict_warnings.append(f"与记忆#{b.doc_id}矛盾: {reason}")
                    b.conflict_warnings.append(f"与记忆#{a.doc_id}矛盾: {reason}")

    # ─────────── 2. 冲突关联：召回记忆 vs 历史冲突裁决 ───────────

    def _conflict_link(self, results: list) -> None:
        ids = [r.doc_id for r in results[: self.max_results]]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        sql = (
            "SELECT id, new_memory_id, old_memory_id, status, reason "
            f"FROM memory_conflicts WHERE new_memory_id IN ({placeholders}) "
            f"OR old_memory_id IN ({placeholders})"
        )
        conn = sqlite3.connect(self.v2_db_path, timeout=5)
        try:
            rows = conn.execute(sql, ids + ids).fetchall()
        finally:
            conn.close()
        for cid, new_id, old_id, status, reason in rows:
            tag = "已确认" if status == "confirmed" else "候选"
            for r in results:
                if r.doc_id in (new_id, old_id):
                    r.conflict_warnings.append(
                        f"涉及{tag}冲突#{cid}: {str(reason)[:80]}"
                    )


def get_recent_confirmed_alerts(
    v2_db_path: str, hours: int = 24, limit: int = 3
) -> list:
    """反射弧③：最近 N 小时已确认冲突列表（memory_recall 注入提醒用）。

    失败返回 []（免疫降级，绝不阻断注入）。
    """
    try:
        since = time.time() - hours * 3600
        conn = sqlite3.connect(v2_db_path, timeout=5)
        try:
            rows = conn.execute(
                "SELECT id, reason FROM memory_conflicts "
                "WHERE status='confirmed' AND created_at > ? "
                "ORDER BY created_at DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        finally:
            conn.close()
        return [f"冲突#{cid}: {str(reason)[:100]}" for cid, reason in rows]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[self_check] 反射弧③查询失败(免疫降级): {e}")
        return []
