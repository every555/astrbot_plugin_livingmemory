# -*- coding: utf-8 -*-
"""Phase 2 归档员（家庭协作 v2.1）。

职责：管理「沉睡记忆」的归档生命周期：
    candidate（扫描发现）→ proposed（例会汇报）→ 橘子点头 → archived（归档）
或                          → declined（拒绝归档）

混合触发模式：
    A. 后台循环：plugin_initializer 每日扫描一次（30 天沉睡判据）
    B. 对话触发：haruyuki_archive 工具 action=scan（橘子/春雪随时手动扫）

数据归属：
    - archive_candidates 表 → v2_memory.db（v2 家族领域表，V2Store 建表）
    - 归档执行（UPDATE memory_atoms.status='archived'）→ livingmemory.db
采用同步 sqlite3 连接（双库独立连接），不依赖 aiosqlite 事件循环，
helper 插件可经 _import_lm 直接复用同一类（模块级单例事件总线同源）。

原则：任何一步失败不影响主流程；归档不删除数据，仅标记 status。
"""
import json
import logging
import os
import sqlite3
import time

logger = logging.getLogger("archive_manager")

# 事件总线（livingmemory 进程内与 helper _import_lm 同源）
try:
    from ..events.event_bus import MemoryEvent, MemoryEventType, get_event_bus

    _HAS_BUS = True
except Exception:  # pragma: no cover - 独立测试环境降级
    _HAS_BUS = False
    MemoryEventType = None
    MemoryEvent = None

    def get_event_bus():  # type: ignore
        return None


from ..retrieval.eligibility import sync_parent_eligibility


class ArchiveManager:
    """沉睡记忆扫描 + 归档候选状态机 + 归档执行。"""

    # 候选状态机
    STATUS_CANDIDATE = "candidate"
    STATUS_PROPOSED = "proposed"
    STATUS_CONFIRMED = "confirmed"
    STATUS_DECLINED = "declined"
    STATUS_ARCHIVED = "archived"
    _OPEN_STATUSES = (STATUS_CANDIDATE, STATUS_PROPOSED, STATUS_CONFIRMED)

    def __init__(self, v2_db_path: str, main_db_path: str, store=None, config=None):
        self.config = config or {}
        self.v2_db_path = v2_db_path
        self.main_db_path = main_db_path
        self.store = store  # V2Store（可选，用于 record_feedback 亲情线日志）
        self._v2: sqlite3.Connection | None = None
        self._main: sqlite3.Connection | None = None

    # ─────────── 连接管理 ───────────

    def _v2_conn(self) -> sqlite3.Connection:
        if self._v2 is None:
            os.makedirs(os.path.dirname(self.v2_db_path), exist_ok=True)
            self._v2 = sqlite3.connect(self.v2_db_path, timeout=10)
            self._v2.row_factory = sqlite3.Row
            self._v2.execute("PRAGMA busy_timeout=10000")
        return self._v2

    def _main_conn(self) -> sqlite3.Connection:
        if self._main is None:
            self._main = sqlite3.connect(self.main_db_path, timeout=10)
            self._main.row_factory = sqlite3.Row
            self._main.execute("PRAGMA busy_timeout=10000")
        return self._main

    def close(self) -> None:
        for c in (self._v2, self._main):
            if c is not None:
                try:
                    c.close()
                except BaseException:
                    pass
        self._v2 = self._main = None

    def _ensure_table(self) -> None:
        """幂等建表（V2Store 也会建；helper 独立实例时兜底）。"""
        self._v2_conn().execute(
            """
            CREATE TABLE IF NOT EXISTS archive_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                atom_id INTEGER NOT NULL,
                content_preview TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                score REAL DEFAULT 0.0,
                status TEXT DEFAULT 'candidate',
                created_at REAL NOT NULL,
                proposed_at REAL,
                confirmed_at REAL,
                archived_at REAL,
                metadata TEXT DEFAULT '{}'
            )
            """
        )
        self._v2_conn().commit()

    # ─────────── 扫描：30 天沉睡判据 ───────────

    def scan_sleeping_atoms(self, days: int = 30, min_importance: float = 0.6,
                            max_candidates: int = 20, dry_run: bool = False) -> dict:
        """扫描沉睡记忆并写入候选表。

        判据（全部满足）：
            - status='active'
            - created_at 距今 >= days 天
            - last_reinforced_at 为空 或 距今 >= days 天（沉睡天数）
            - importance <= min_importance（低重要）
            - 不在复习计划中（reinforcement_state 为空/None/'{}'，保守跳过）
            - 未在候选表中处于打开状态（candidate/proposed/confirmed，去重）

        沉睡分数 score = (1-importance)*50 + min(沉睡天数/30, 30)，越高越该归档。
        """
        self._ensure_table()
        now = time.time()
        cutoff = now - days * 86400
        try:
            rows = self._main_conn().execute(
                """
                SELECT id, content, importance, created_at, last_reinforced_at,
                       last_accessed_at, reinforcement_state, metadata
                FROM memory_atoms
                WHERE status='active'
                  AND created_at < ?
                  AND (last_reinforced_at IS NULL OR last_reinforced_at < ?)
                  AND importance <= ?
                ORDER BY created_at ASC
                LIMIT 500
                """,
                (cutoff, cutoff, min_importance),
            ).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning(f"[Archive] 扫描 memory_atoms 失败（可能旧表结构）: {e}")
            return {"status": "error", "msg": str(e), "new_candidates": 0, "candidates": []}

        # 已打开候选的 atom_id 集合（去重）
        opened = {
            r["atom_id"]
            for r in self._v2_conn().execute(
                "SELECT atom_id FROM archive_candidates WHERE status IN (?,?,?)",
                self._OPEN_STATUSES,
            ).fetchall()
        }

        created: list[dict] = []
        for row in rows:
            atom_id = int(row["id"])
            if atom_id in opened:
                continue
            content = str(row["content"] or "")[:80]
            importance = float(row["importance"] or 0.0)
            last_r = row["last_reinforced_at"]
            sleep_days = max(0.0, (now - (last_r if last_r else row["created_at"])) / 86400)
            rs = row["reinforcement_state"]
            # 复习计划中的记忆不打扰（保守跳过）
            if rs and str(rs).strip() not in ("", "{}", "null"):
                continue
            score = round((1 - importance) * 50 + min(sleep_days / 30, 30), 1)
            reason = (
                f"已沉睡 {int(sleep_days)} 天（创建于 "
                f"{time.strftime('%Y-%m-%d', time.localtime(row['created_at']))}），"
                f"重要性 {importance}"
            )
            created.append({
                "atom_id": atom_id,
                "content_preview": content,
                "reason": reason,
                "score": score,
                "sleep_days": round(sleep_days, 1),
                "importance": importance,
            })

        created.sort(key=lambda x: x["score"], reverse=True)
        created = created[:max_candidates]

        if not dry_run:
            for c in created:
                self._v2_conn().execute(
                    """
                    INSERT INTO archive_candidates
                        (atom_id, content_preview, reason, score, status, created_at, metadata)
                    VALUES (?, ?, ?, ?, 'candidate', ?, '{}')
                    """,
                    (c["atom_id"], c["content_preview"], c["reason"], c["score"], now),
                )
            self._v2_conn().commit()
            if created:
                self._emit_event(MemoryEventType.ARCHIVE_CANDIDATE, created[0]["atom_id"],
                                 {"count": len(created), "days": days})
                self._record_feedback("archive", "family_meeting", "archive_candidate",
                                      payload={"count": len(created), "days": days})
        return {
            "status": "ok",
            "new_candidates": len(created),
            "scanned": len(rows),
            "days": days,
            "candidates": created,
            "dry_run": dry_run,
        }

    # ─────────── 候选查询 ───────────

    def list_candidates(self, status: str | None = None, limit: int = 20) -> list[dict]:
        self._ensure_table()
        if status and status not in {
            self.STATUS_CANDIDATE, self.STATUS_PROPOSED, self.STATUS_CONFIRMED,
            self.STATUS_DECLINED, self.STATUS_ARCHIVED,
        }:
            return [{"error": f"未知状态: {status}（可选 candidate/proposed/confirmed/declined/archived）"}]
        try:
            if status:
                rows = self._v2_conn().execute(
                    "SELECT * FROM archive_candidates WHERE status=? ORDER BY score DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._v2_conn().execute(
                    "SELECT * FROM archive_candidates ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning(f"[Archive] 候选查询失败: {e}")
            return [{"error": str(e)}]
        return [self._fmt_candidate(r) for r in rows]

    def get_candidate(self, candidate_id: int) -> dict | None:
        try:
            row = self._v2_conn().execute(
                "SELECT * FROM archive_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return self._fmt_candidate(row) if row else None

    def _fmt_candidate(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["sleep_days"] = None
        return d

    # ─────────── 状态流转 ───────────

    def propose(self, candidate_ids: list[int]) -> dict:
        """candidate → proposed（例会汇报给橘子，等点头）。"""
        self._ensure_table()
        now = time.time()
        moved = 0
        for cid in candidate_ids:
            cur = self.get_candidate(cid)
            if not cur:
                continue
            if cur["status"] not in (self.STATUS_CANDIDATE, self.STATUS_PROPOSED):
                continue
            self._v2_conn().execute(
                "UPDATE archive_candidates SET status='proposed', proposed_at=? WHERE id=?",
                (now, cid),
            )
            moved += 1
        self._v2_conn().commit()
        return {"status": "ok", "moved": moved, "new_status": self.STATUS_PROPOSED}

    def confirm(self, candidate_ids: list[int], note: str = "") -> dict:
        """橘子点头 → 归档：候选标记 archived + memory_atoms.status='archived' + 发事件。"""
        self._ensure_table()
        now = time.time()
        archived = []
        failed = []
        for cid in candidate_ids:
            cur = self.get_candidate(cid)
            if not cur:
                failed.append({"candidate_id": cid, "error": "候选不存在"})
                continue
            if cur["status"] not in (self.STATUS_CANDIDATE, self.STATUS_PROPOSED, self.STATUS_CONFIRMED):
                failed.append({"candidate_id": cid, "error": f"状态 {cur['status']} 不可归档"})
                continue
            atom_id = int(cur["atom_id"])
            try:
                cur_main = self._main_conn().execute(
                    "SELECT status, metadata FROM memory_atoms WHERE id=?", (atom_id,)
                ).fetchone()
                if cur_main is None:
                    failed.append({"candidate_id": cid, "error": f"atom {atom_id} 不存在"})
                    continue
                if str(cur_main["status"]) == "archived":
                    # 已归档过：仅推进候选状态
                    meta = {}
                else:
                    meta = json.loads(cur_main["metadata"] or "{}")
                    meta["archived_at"] = now
                    meta["archive_note"] = note[:200] or "家庭例会确认归档"
                    meta["archived_by"] = "archive_manager"
                    self._main_conn().execute(
                        "UPDATE memory_atoms SET status='archived', metadata=? WHERE id=?",
                        (json.dumps(meta, ensure_ascii=False), atom_id),
                    )
                # P2-09 归档联动：parent 下全死才把 document 滑出检索池（默认开）
                if self.config.get("archive_eligibility_sync", True):
                    sync_parent_eligibility(self._main_conn(), atom_id)
                # P1-① 反射弧⑥：parent 彻底沉睡 → causal 悬空边打 archived_ 前缀（可逆标记，可走多跳被识别）
                try:
                    prow = self._main_conn().execute(
                        "SELECT parent_memory_id FROM memory_atoms WHERE id=?", (atom_id,)
                    ).fetchone()
                    if prow is not None:
                        parent_id = int(prow["parent_memory_id"])
                        alive = self._main_conn().execute(
                            "SELECT COUNT(*) FROM memory_atoms "
                            "WHERE parent_memory_id=? AND status!='archived'", (parent_id,)
                        ).fetchone()[0]
                        if alive == 0:
                            marked = self._v2_conn().execute(
                                "UPDATE memory_causality SET role='archived_'||role "
                                "WHERE (memory_id=? OR pre_cause_id=?) AND role NOT LIKE 'archived_%'",
                                (parent_id, parent_id),
                            ).rowcount
                            if marked:
                                self._v2_conn().commit()
                                logger.info(f"[Archive] 反射弧⑥: parent#{parent_id} 全沉睡，标记 {marked} 条悬空边")
                except BaseException as e:
                    logger.warning(f"[Archive] 反射弧⑥清边失败(跳过 atom={atom_id}): {e}")
                self._v2_conn().execute(
                    "UPDATE archive_candidates SET status='archived', confirmed_at=?, archived_at=?, "
                    "metadata=? WHERE id=?",
                    (now, now, json.dumps({"note": note[:200]}, ensure_ascii=False), cid),
                )
                self._v2_conn().commit()
                self._main_conn().commit()
                archived.append({"candidate_id": cid, "atom_id": atom_id})
                self._emit_event(MemoryEventType.MEMORY_ARCHIVED, atom_id,
                                 {"candidate_id": cid, "note": note[:80]})
                self._record_feedback("archive", "atoms", "memory_archived",
                                      memory_id=atom_id,
                                      payload={"candidate_id": cid, "note": note[:80]})
            except sqlite3.Error as e:
                logger.warning(f"[Archive] 归档失败 candidate#{cid}: {e}")
                failed.append({"candidate_id": cid, "error": str(e)})
        return {"status": "ok", "archived": archived, "failed": failed,
                "archived_count": len(archived), "failed_count": len(failed)}

    def decline(self, candidate_ids: list[int], reason: str = "") -> dict:
        """拒绝归档（candidate/proposed → declined，不触碰 atoms）。"""
        self._ensure_table()
        now = time.time()
        moved = 0
        for cid in candidate_ids:
            cur = self.get_candidate(cid)
            if not cur:
                continue
            if cur["status"] not in (self.STATUS_CANDIDATE, self.STATUS_PROPOSED):
                continue
            self._v2_conn().execute(
                "UPDATE archive_candidates SET status='declined', metadata=? WHERE id=?",
                (json.dumps({"decline_reason": reason[:200]}, ensure_ascii=False), cid),
            )
            moved += 1
        self._v2_conn().commit()
        return {"status": "ok", "moved": moved, "new_status": self.STATUS_DECLINED}

    def stats(self) -> dict:
        """候选状态统计。"""
        self._ensure_table()
        try:
            rows = self._v2_conn().execute(
                "SELECT status, COUNT(*) AS cnt FROM archive_candidates GROUP BY status"
            ).fetchall()
        except sqlite3.OperationalError as e:
            return {"status": "error", "msg": str(e)}
        counts = {r["status"]: r["cnt"] for r in rows}
        total = sum(counts.values())
        return {
            "status": "ok",
            "total": total,
            "breakdown": counts,
            "pending": counts.get(self.STATUS_CANDIDATE, 0) + counts.get(self.STATUS_PROPOSED, 0),
        }

    # ─────────── 事件与亲情线 ───────────

    def _emit_event(self, event_type, memory_id: int, metadata: dict) -> None:
        if not _HAS_BUS or MemoryEventType is None:
            return
        try:
            bus = get_event_bus()
            if bus is not None:
                bus.publish_nowait(
                    MemoryEvent(type=event_type, memory_id=memory_id,
                                memory_type="atom", metadata=metadata)
                )
        except BaseException:
            logger.warning(f"[Archive] 事件发布失败 {event_type}", exc_info=True)

    def _record_feedback(self, from_module: str, to_module: str, event_type: str,
                         memory_id: int | None = None, payload: dict | None = None) -> None:
        """亲情线流水：优先走 V2Store.record_feedback（异步），失败降级直插。"""
        if self.store is not None:
            try:
                import asyncio
                loop = None
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    loop.create_task(
                        self.store.record_feedback(
                            from_module=from_module, to_module=to_module,
                            event_type=event_type, memory_id=memory_id,
                            persona_id=None, payload=payload or {},
                        )
                    )
                    return
            except BaseException:
                pass
        try:
            self._v2_conn().execute(
                "INSERT INTO feedback_log (from_module, to_module, event_type, memory_id, "
                "persona_id, payload, result, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (from_module, to_module, event_type, memory_id, None,
                 json.dumps(payload or {}, ensure_ascii=False), "ok", time.time()),
            )
            self._v2_conn().commit()
        except BaseException:
            logger.warning("[Archive] feedback_log 直插失败", exc_info=True)
