"""P2-09 检索资格与保留解耦（AMV-L：沉睡记忆不删但滑出检索池）。

现状（2026-08-23 三查结论）：memory_atoms 里 expired/forgotten/superseded/archived
共 2759 条死记忆（33%），但检索主链（dual_route→hybrid→documents）零状态过滤，
死记忆照样参与打分被召回；归档 confirm 只标 atoms，parent document 毫不知情。

设计：
- documents.metadata.retrieval_eligible（缺省 True=存量记忆不误杀）
- EligibilityFilter：search_memories 出口过滤，一处管 BM25/向量/图/多跳
- sync_parent_eligibility：归档联动——parent 下 active atom 清零才标 false
- 开关 retrieval_eligibility_enabled 默认 False（动主链，钥匙在橘子手上）

红线：
- 只改检索资格，绝不删数据（保留与资格解耦的本义）
- 过滤器任何异常降级放行，不许炸检索主链
"""
import json
import logging
import sqlite3

logger = logging.getLogger(__name__)


class EligibilityFilter:
    """检索结果资格过滤器：剔除 retrieval_eligible=False 的 document。

    同步实现（单次 id IN 回表，毫秒级），挂在 search_memories 出口。
    """

    def __init__(self, db_path: str, enabled: bool = True):
        self.db_path = db_path
        self.enabled = enabled

    def _fetch_flags(self, doc_ids):
        """回表查 {id: eligible_bool}。只查请求的 id，不全表扫。"""
        ids = [int(d) for d in doc_ids if d is not None]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        con = sqlite3.connect("file:" + self.db_path.replace("\\", "/") + "?mode=ro", uri=True)
        try:
            rows = con.execute(
                f"SELECT id, metadata FROM documents WHERE id IN ({placeholders})", ids
            ).fetchall()
        finally:
            con.close()
        flags = {}
        for row_id, meta_json in rows:
            eligible = True
            try:
                meta = json.loads(meta_json or "{}")
                if isinstance(meta, dict) and meta.get("retrieval_eligible") is False:
                    eligible = False
            except (ValueError, TypeError):
                pass                                    # metadata 坏了=放行，不误杀
            flags[row_id] = eligible
        return flags

    def filter_results(self, results):
        """过滤 HybridResult 列表；异常一律降级放行。"""
        if not self.enabled or not results:
            return results
        try:
            ids = [getattr(r, "doc_id", None) for r in results]
            flags = self._fetch_flags(ids)
            kept = [r for r in results if flags.get(getattr(r, "doc_id", None), True)]
            dropped = len(results) - len(kept)
            if dropped:
                logger.info(f"[Eligibility] 本轮检索剔除 {dropped} 条失格记忆")
            return kept
        except Exception as e:
            logger.warning(f"[Eligibility] 资格过滤失败(降级放行): {e}")
            return results


def sync_parent_eligibility(main_conn, atom_id: int) -> bool:
    """归档联动：atom 死后检查 parent document 是否全空。

    parent 下已无任何 active atom 时，把 document.metadata.retrieval_eligible
    置 False（滑出检索池，数据保留）。返回是否发生标记。
    调用方负责 commit 与异常兜底。
    """
    try:
        row = main_conn.execute(
            "SELECT parent_memory_id FROM memory_atoms WHERE id=?", (int(atom_id),)
        ).fetchone()
        if row is None:
            return False
        parent = row[0] if not isinstance(row, sqlite3.Row) else row["parent_memory_id"]
        if parent is None:
            return False
        active_cnt = main_conn.execute(
            "SELECT COUNT(*) FROM memory_atoms WHERE parent_memory_id=? AND status='active'",
            (int(parent),),
        ).fetchone()[0]
        if active_cnt > 0:
            return False
        doc = main_conn.execute(
            "SELECT metadata FROM documents WHERE id=?", (int(parent),)
        ).fetchone()
        if doc is None:
            return False
        meta = json.loads(doc[0] or "{}")
        if not isinstance(meta, dict):
            meta = {}
        if meta.get("retrieval_eligible") is False:
            return False                                # 已标记，幂等
        meta["retrieval_eligible"] = False
        meta["eligibility_synced_at"] = True
        main_conn.execute(
            "UPDATE documents SET metadata=?, updated_at=datetime('now','localtime') WHERE id=?",
            (json.dumps(meta, ensure_ascii=False), int(parent)),
        )
        return True
    except Exception as e:
        logger.warning(f"[Eligibility] 归档联动失败(atom={atom_id} 跳过): {e}")
        return False
