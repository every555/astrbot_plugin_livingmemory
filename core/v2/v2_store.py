"""记忆生态系统 v2.0 存储层。

独立 SQLite 连接（WAL 模式），与主 documents 表并存。
表结构：
- memory_causality   因果证据链（每条记忆的来源 + 因果角色 + 上下文快照）
- memory_conflicts   三级冲突检测记录
- memory_profile     记忆画像（core_traits + confidence + evidence_ids + evolution_log）
- memory_prophecies  记忆预言（内容 + TTL + 回溯状态）
- memory_expression_log  表达联动演化日志
"""

import json
import time
from typing import Any

import aiosqlite

try:
    from astrbot.api import logger
except ImportError:  # 独立测试环境降级
    import logging

    logger = logging.getLogger("v2_store")


def _now() -> float:
    return time.time()


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _loads(raw: Any, fallback: Any = None) -> Any:
    if raw is None:
        return fallback
    if isinstance(raw, (dict, list)):
        return raw  # 已是解析后的对象（_row_to_dict 转换过）
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


class V2Store:
    """v2.0 全部新表的统一存储入口。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """建表 + WAL 模式。幂等，可重复调用。"""
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self._create_tables()
        await self.db.commit()

    async def close(self) -> None:
        if self.db is not None:
            try:
                await self.db.close()
            except BaseException:
                pass
            self.db = None

    async def _create_tables(self) -> None:
        assert self.db is not None
        # ── 因果证据链 ──
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_causality (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id INTEGER NOT NULL,
                persona_id TEXT,
                session_id TEXT,
                trigger_type TEXT DEFAULT 'agent_tool',
                trigger_message TEXT DEFAULT '',
                pre_cause_id INTEGER,
                role TEXT DEFAULT 'fact',
                context_snapshot TEXT DEFAULT '{}',
                created_at REAL NOT NULL
            )
            """
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_causality_memory ON memory_causality(memory_id)"
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_causality_pre ON memory_causality(pre_cause_id)"
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_causality_persona ON memory_causality(persona_id)"
        )

        # ── 三级冲突记录 ──
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                new_memory_id INTEGER NOT NULL,
                old_memory_id INTEGER NOT NULL,
                level INTEGER NOT NULL DEFAULT 1,
                conflict_type TEXT DEFAULT 'content',
                reason TEXT DEFAULT '',
                confidence REAL DEFAULT 0.5,
                status TEXT DEFAULT 'candidate',
                resolution TEXT DEFAULT '{}',
                created_at REAL NOT NULL,
                resolved_at REAL
            )
            """
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conflicts_new ON memory_conflicts(new_memory_id)"
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conflicts_old ON memory_conflicts(old_memory_id)"
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conflicts_status ON memory_conflicts(status)"
        )

        # ── 记忆画像 ──
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_profile (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona_id TEXT NOT NULL,
                trait_key TEXT NOT NULL,
                trait_value TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                evidence_ids TEXT DEFAULT '[]',
                evolution_log TEXT DEFAULT '[]',
                updated_at REAL NOT NULL,
                UNIQUE(persona_id, trait_key)
            )
            """
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_profile_persona ON memory_profile(persona_id)"
        )

        # ── 记忆预言 ──
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_prophecies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona_id TEXT,
                content TEXT NOT NULL,
                base_memory_id INTEGER,
                prophecy_type TEXT DEFAULT 'causal',
                ttl_days REAL DEFAULT 7.0,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT DEFAULT 'active',
                verified_at REAL,
                verification TEXT DEFAULT '{}',
                strength_before REAL,
                strength_after REAL
            )
            """
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_prophecies_status ON memory_prophecies(status, expires_at)"
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_prophecies_persona ON memory_prophecies(persona_id)"
        )

        # ── 表达联动日志 ──
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_expression_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona_id TEXT NOT NULL,
                style_version INTEGER DEFAULT 1,
                style_snapshot TEXT DEFAULT '{}',
                trait_drivers TEXT DEFAULT '[]',
                updated_at REAL NOT NULL
            )
            """
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_expression_persona ON memory_expression_log(persona_id)"
        )

        # ── 家庭协作反馈日志（v2.1 亲情线流水：谁→谁、类型、结果）──
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_module TEXT NOT NULL,
                to_module TEXT NOT NULL,
                event_type TEXT NOT NULL,
                memory_id INTEGER,
                persona_id TEXT,
                payload TEXT DEFAULT '{}',
                result TEXT DEFAULT 'ok',
                created_at REAL NOT NULL
            )
            """
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_event ON feedback_log(event_type)"
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback_log(created_at)"
        )

        # ── 归档候选（Phase 2 归档员：沉睡记忆 → 例会汇报 → 点头 → 归档）──
        await self.db.execute(
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
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_archive_status ON archive_candidates(status, atom_id)"
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_archive_atom ON archive_candidates(atom_id)"
        )

        # ── 家庭角色分工（Phase 3：22 位家人的身份/职责/协作对象）──
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS family_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_name TEXT NOT NULL,
                tool_name TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL,
                duty TEXT DEFAULT '',
                importance REAL DEFAULT 0.5,
                cooperates_with TEXT DEFAULT '[]',
                active INTEGER DEFAULT 1,
                created_at REAL NOT NULL
            )
            """
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_family_active ON family_roles(active)"
        )

        # ── 家庭例会日报（Phase 4：每日汇总全家动态，第一议题=归档审批）──
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS family_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL UNIQUE,
                content TEXT DEFAULT '{}',
                summary TEXT DEFAULT '',
                status TEXT DEFAULT 'issued',
                created_at REAL NOT NULL
            )
            """
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_report_date ON family_reports(report_date)"
        )

    # ══════════════ 因果证据链 ══════════════

    async def add_causality(
        self,
        memory_id: int,
        persona_id: str | None,
        session_id: str | None,
        trigger_type: str = "agent_tool",
        trigger_message: str = "",
        pre_cause_id: int | None = None,
        role: str = "fact",
        context_snapshot: dict | None = None,
    ) -> int:
        assert self.db is not None
        cursor = await self.db.execute(
            """
            INSERT INTO memory_causality
            (memory_id, persona_id, session_id, trigger_type, trigger_message,
             pre_cause_id, role, context_snapshot, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                persona_id,
                session_id,
                trigger_type,
                trigger_message or "",
                pre_cause_id,
                role,
                _dumps(context_snapshot or {}),
                _now(),
            ),
        )
        await self.db.commit()
        return int(cursor.lastrowid)

    async def get_causality(self, memory_id: int) -> dict | None:
        assert self.db is not None
        cursor = await self.db.execute(
            "SELECT * FROM memory_causality WHERE memory_id = ? ORDER BY id DESC LIMIT 1",
            (memory_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    async def get_causes(self, memory_id: int) -> list[dict]:
        """前因追溯：找 pre_cause_id == memory_id 的所有记忆。"""
        assert self.db is not None
        cursor = await self.db.execute(
            "SELECT * FROM memory_causality WHERE pre_cause_id = ? ORDER BY created_at",
            (memory_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_effect_chain(self, memory_id: int, max_depth: int = 5) -> list[dict]:
        """后果展开：沿 pre_cause_id 链向下走。"""
        assert self.db is not None
        chain: list[dict] = []
        current = memory_id
        seen: set[int] = set()
        for _ in range(max_depth):
            if current in seen:
                break
            seen.add(current)
            cursor = await self.db.execute(
                "SELECT * FROM memory_causality WHERE pre_cause_id = ? ORDER BY created_at LIMIT 10",
                (current,),
            )
            rows = await cursor.fetchall()
            if not rows:
                break
            for row in rows:
                entry = self._row_to_dict(row)
                chain.append(entry)
                current = entry["memory_id"]
        return chain

    async def get_cause_chain(self, memory_id: int, max_depth: int = 5) -> list[dict]:
        """前因链：沿自身 pre_cause_id 向上回溯。"""
        assert self.db is not None
        chain: list[dict] = []
        seen: set[int] = set()
        current_mid = memory_id
        for _ in range(max_depth):
            if current_mid in seen or current_mid is None:
                break
            seen.add(current_mid)
            entry = await self.get_causality(current_mid)
            if entry is None:
                break
            chain.append(entry)
            current_mid = entry.get("pre_cause_id")
        return chain

    # ══════════════ 冲突记录 ══════════════

    async def add_conflict(
        self,
        new_memory_id: int,
        old_memory_id: int,
        level: int,
        conflict_type: str,
        reason: str,
        confidence: float,
        status: str = "candidate",
    ) -> int:
        assert self.db is not None
        cursor = await self.db.execute(
            """
            INSERT INTO memory_conflicts
            (new_memory_id, old_memory_id, level, conflict_type, reason,
             confidence, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_memory_id,
                old_memory_id,
                level,
                conflict_type,
                reason,
                confidence,
                status,
                _now(),
            ),
        )
        await self.db.commit()
        return int(cursor.lastrowid)

    async def update_conflict_status(self, conflict_id: int, status: str, resolution: dict | None = None) -> None:
        assert self.db is not None
        if resolution is not None:
            await self.db.execute(
                """
                UPDATE memory_conflicts
                SET status = ?, resolution = ?, resolved_at = ?
                WHERE id = ?
                """,
                (status, _dumps(resolution), _now(), conflict_id),
            )
        else:
            await self.db.execute(
                "UPDATE memory_conflicts SET status = ? WHERE id = ?",
                (status, conflict_id),
            )
        await self.db.commit()

    async def list_conflicts(self, status: str | None = None, limit: int = 20) -> list[dict]:
        assert self.db is not None
        if status:
            cursor = await self.db.execute(
                "SELECT * FROM memory_conflicts WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM memory_conflicts ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ══════════════ 记忆画像 ══════════════

    async def get_profile(self, persona_id: str) -> list[dict]:
        assert self.db is not None
        cursor = await self.db.execute(
            "SELECT * FROM memory_profile WHERE persona_id = ? ORDER BY confidence DESC",
            (persona_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_profile_trait(self, persona_id: str, trait_key: str) -> dict | None:
        assert self.db is not None
        cursor = await self.db.execute(
            "SELECT * FROM memory_profile WHERE persona_id = ? AND trait_key = ?",
            (persona_id, trait_key),
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def upsert_profile_trait(
        self,
        persona_id: str,
        trait_key: str,
        trait_value: str,
        confidence: float,
        evidence_id: int | None,
        evolution_note: str,
    ) -> None:
        """更新画像特征：保留历史演化日志，confidence 取新旧更稳的规则。"""
        assert self.db is not None
        existing = await self.get_profile_trait(persona_id, trait_key)
        if existing:
            old_conf = float(existing.get("confidence", 0.5))
            # 演化规则：正向证据提升，冲突证据下调；不回跳太多
            if confidence >= old_conf:
                new_conf = min(0.98, old_conf + (confidence - old_conf) * 0.6 + 0.05)
            else:
                new_conf = max(0.1, old_conf * 0.7)
            evidence_ids = _loads(existing.get("evidence_ids", "[]"), [])
            if evidence_id and evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
            evolution_log = _loads(existing.get("evolution_log", "[]"), [])
            evolution_log.append(
                {
                    "ts": _now(),
                    "note": evolution_note,
                    "confidence": round(new_conf, 3),
                    "evidence_id": evidence_id,
                }
            )
            evolution_log = evolution_log[-30:]  # 保留最近30条
            await self.db.execute(
                """
                UPDATE memory_profile
                SET trait_value = ?, confidence = ?, evidence_ids = ?,
                    evolution_log = ?, updated_at = ?
                WHERE persona_id = ? AND trait_key = ?
                """,
                (
                    trait_value,
                    new_conf,
                    _dumps(evidence_ids),
                    _dumps(evolution_log),
                    _now(),
                    persona_id,
                    trait_key,
                ),
            )
        else:
            evolution_log = [
                {
                    "ts": _now(),
                    "note": evolution_note,
                    "confidence": round(confidence, 3),
                    "evidence_id": evidence_id,
                }
            ]
            evidence_ids = [evidence_id] if evidence_id else []
            await self.db.execute(
                """
                INSERT INTO memory_profile
                (persona_id, trait_key, trait_value, confidence, evidence_ids,
                 evolution_log, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    persona_id,
                    trait_key,
                    trait_value,
                    confidence,
                    _dumps(evidence_ids),
                    _dumps(evolution_log),
                    _now(),
                ),
            )
        await self.db.commit()

    async def downweight_profile_trait(
        self,
        persona_id: str,
        trait_key: str,
        reason: str,
    ) -> None:
        """预言验证失败等场景下调画像 confidence。"""
        assert self.db is not None
        existing = await self.get_profile_trait(persona_id, trait_key)
        if not existing:
            return
        new_conf = max(0.1, float(existing.get("confidence", 0.5)) * 0.6)
        evolution_log = _loads(existing.get("evolution_log", "[]"), [])
        evolution_log.append(
            {
                "ts": _now(),
                "note": f"[下调] {reason}",
                "confidence": round(new_conf, 3),
            }
        )
        await self.db.execute(
            """
            UPDATE memory_profile
            SET confidence = ?, evolution_log = ?, updated_at = ?
            WHERE persona_id = ? AND trait_key = ?
            """,
            (new_conf, _dumps(evolution_log), _now(), persona_id, trait_key),
        )
        await self.db.commit()

    # ══════════════ 记忆预言 ══════════════

    async def add_prophecy(
        self,
        persona_id: str | None,
        content: str,
        base_memory_id: int | None,
        prophecy_type: str,
        ttl_days: float,
        strength: float | None = None,
    ) -> int:
        assert self.db is not None
        now = _now()
        cursor = await self.db.execute(
            """
            INSERT INTO memory_prophecies
            (persona_id, content, base_memory_id, prophecy_type, ttl_days,
             created_at, expires_at, status, strength_before)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                persona_id,
                content,
                base_memory_id,
                prophecy_type,
                ttl_days,
                now,
                now + ttl_days * 86400,
                strength,
            ),
        )
        await self.db.commit()
        return int(cursor.lastrowid)

    async def list_prophecies(
        self, status: str | None = None, persona_id: str | None = None, limit: int = 20
    ) -> list[dict]:
        assert self.db is not None
        sql = "SELECT * FROM memory_prophecies"
        conds: list[str] = []
        params: list[Any] = []
        if status:
            conds.append("status = ?")
            params.append(status)
        if persona_id:
            conds.append("persona_id = ?")
            params.append(persona_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = await self.db.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_expired_prophecies(self, limit: int = 20) -> list[dict]:
        assert self.db is not None
        cursor = await self.db.execute(
            """
            SELECT * FROM memory_prophecies
            WHERE status = 'active' AND expires_at <= ?
            ORDER BY expires_at LIMIT ?
            """,
            (_now(), limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def update_prophecy_result(
        self,
        prophecy_id: int,
        status: str,
        verification: dict,
        strength_after: float | None = None,
    ) -> None:
        assert self.db is not None
        await self.db.execute(
            """
            UPDATE memory_prophecies
            SET status = ?, verification = ?, verified_at = ?, strength_after = ?
            WHERE id = ?
            """,
            (status, _dumps(verification), _now(), strength_after, prophecy_id),
        )
        await self.db.commit()

    # ══════════════ 表达联动日志 ══════════════

    async def log_expression(
        self, persona_id: str, style_version: int, style_snapshot: dict, trait_drivers: list[dict]
    ) -> None:
        assert self.db is not None
        await self.db.execute(
            """
            INSERT INTO memory_expression_log
            (persona_id, style_version, style_snapshot, trait_drivers, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (persona_id, style_version, _dumps(style_snapshot), _dumps(trait_drivers), _now()),
        )
        await self.db.commit()

    async def get_latest_expression(self, persona_id: str) -> dict | None:
        assert self.db is not None
        cursor = await self.db.execute(
            """
            SELECT * FROM memory_expression_log
            WHERE persona_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (persona_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    # ══════════════ 工具 ══════════════

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict:
        data = dict(row)
        for key in ("context_snapshot", "evolution_log", "evidence_ids", "resolution", "verification", "style_snapshot", "trait_drivers"):
            if key in data and isinstance(data[key], str):
                data[key] = _loads(data[key], {})
        return data

    # ══════════════ 家庭协作反馈日志（v2.1） ══════════════

    async def record_feedback(
        self,
        from_module: str,
        to_module: str,
        event_type: str,
        memory_id: int | None = None,
        persona_id: str | None = None,
        payload: dict | None = None,
        result: str = "ok",
    ) -> int:
        """记录一条亲情线反馈（谁→谁、事件、结果），失败不影响调用方。"""
        assert self.db is not None
        cursor = await self.db.execute(
            """
            INSERT INTO feedback_log
                (from_module, to_module, event_type, memory_id, persona_id, payload, result, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                from_module,
                to_module,
                event_type,
                memory_id,
                persona_id,
                _dumps(payload or {}),
                result,
                time.time(),
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def list_feedback(
        self,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """查询亲情线流水（按时间倒序）。"""
        assert self.db is not None
        if event_type:
            cursor = await self.db.execute(
                "SELECT * FROM feedback_log WHERE event_type = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (event_type, limit, offset),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM feedback_log ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if isinstance(item.get("payload"), str):
                item["payload"] = _loads(item["payload"], {})
            result.append(item)
        return result

    async def count_feedback(self, event_type: str | None = None) -> int:
        """统计反馈条数（家庭例会/家庭总览用）。"""
        assert self.db is not None
        if event_type:
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM feedback_log WHERE event_type = ?", (event_type,)
            )
        else:
            cursor = await self.db.execute("SELECT COUNT(*) FROM feedback_log")
        row = await cursor.fetchone()
        return row[0] if row else 0
