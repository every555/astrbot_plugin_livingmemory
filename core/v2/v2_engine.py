"""记忆生态系统 v2.0 集成层（V2Engine）。

把七大功能串成一条流水线，挂在 MemoryEngine.add_memory 之后：
1. 因果证据链记录（source + causality + context_snapshot）
2. 三级冲突检测（L1 内容 / L2 因果 / L3 画像）
3. 记忆画像更新（core_traits + confidence + evolution_log）
4. 记忆预言生成（causal / periodic / profile）
5. 预言回溯（到期验证，由外部定时调用 run_prophecy_backfill）
6. 因果链遍历（trace）
7. 表达联动（build_style / apply_to_prompt）

设计原则：v2 全部能力可选、可降级、失败不影响主记忆流程。
"""

import time
from typing import Any

from .causal_chain import CausalChainService
from .conflict_detector import ConflictDetector
from .expression_evolver import ExpressionEvolver
from .profile_manager import ProfileManager
from .prophecy_engine import ProphecyEngine
from .v2_store import V2Store
from .archive_manager import ArchiveManager
from .family_meeting_manager import FamilyMeetingManager

try:
    from astrbot.api import logger
except ImportError:  # 独立测试环境降级
    import logging

    logger = logging.getLogger("v2_engine")


class V2Engine:
    """v2.0 集成引擎。"""

    def __init__(self, db_path: str, db_connection=None, memory_engine=None):
        self.store = V2Store(db_path)
        self.db_connection = db_connection
        self.memory_engine = memory_engine
        self.causal = CausalChainService(self.store, db_connection)
        self.conflicts = ConflictDetector(self.store, db_connection)
        self.profile = ProfileManager(self.store)
        self.prophecy = ProphecyEngine(self.store, db_connection, memory_engine)
        self.expression = ExpressionEvolver(self.store)
        self.archive = ArchiveManager(db_path, self._resolve_main_db(), self.store)
        self.family_meeting = FamilyMeetingManager(
            db_path, self._resolve_main_db(), self.store
        )
        # P1-① Sleeptime：dream 归并互链器（免疫降级构造，缺库自动禁用）
        self.sleeptime_linker = None
        try:
            from .sleeptime_linker import SleeptimeLinker
            self.sleeptime_linker = SleeptimeLinker(
                self._resolve_main_db(), db_path, self.store
            )
        except BaseException:
            logger.warning("[v2] SleeptimeLinker 初始化失败(忽略)", exc_info=True)

        # 情感 v4.0：EmotionCore 连续性状态层（旁挂免疫，同步sqlite直连本库）
        self.emotion_core = None
        try:
            from .emotion_core import EmotionCore
            self.emotion_core = EmotionCore(db_path)
        except BaseException:
            logger.warning("[v2] EmotionCore 初始化失败(忽略)", exc_info=True)
        self.enabled = False
        self._last_backfill_ts = 0.0

    def _resolve_main_db(self) -> str:
        """推导 livingmemory.db 路径：优先 memory_engine.db_path，否则与 v2 同目录。"""
        me = getattr(self, "memory_engine", None)
        me_path = getattr(me, "db_path", None)
        if me_path:
            return str(me_path)
        import os

        return os.path.join(os.path.dirname(self.store.db_path), "livingmemory.db")

    async def initialize(self) -> None:
        await self.store.initialize()
        self.enabled = True
        logger.info("[v2] 记忆生态系统 v2.0 存储已初始化")

    async def close(self) -> None:
        await self.store.close()
        try:
            self.archive.close()
        except BaseException:
            pass
        try:
            self.family_meeting.close()
        except BaseException:
            pass
        self.enabled = False

    # ─────────── 主钩子：记忆写入后执行 v2 全流程 ───────────

    async def on_memory_written(
        self,
        memory_id: int,
        persona_id: str | None,
        session_id: str | None,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        """MemoryEngine.add_memory 完成后的 v2 钩子。

        执行：证据链 → 冲突检测 → 画像更新 → 预言生成。
        任何一步失败都不影响记忆主流程。
        """
        if not self.enabled:
            return {"enabled": False}
        metadata = metadata or {}
        result: dict[str, Any] = {"memory_id": memory_id, "enabled": True}

        # 1. 因果证据链
        try:
            causal_info = await self.causal.record(
                memory_id, persona_id, session_id, metadata, content
            )
            result["causality"] = causal_info
        except BaseException as e:
            logger.warning(f"[v2] 证据链记录失败: {e}")
            result["causality_error"] = str(e)

        # 2. 三级冲突检测
        try:
            conflicts = await self.conflicts.detect_all(
                memory_id, content, persona_id, metadata
            )
            result["conflicts"] = conflicts
        except BaseException as e:
            logger.warning(f"[v2] 冲突检测失败: {e}")
            result["conflicts_error"] = str(e)

        # 3. 记忆画像更新
        try:
            profile_updates = await self.profile.update_from_memory(
                memory_id, persona_id, content, metadata
            )
            result["profile_updates"] = profile_updates
        except BaseException as e:
            logger.warning(f"[v2] 画像更新失败: {e}")
            result["profile_error"] = str(e)

        # 4. 记忆预言生成
        try:
            prophecies = await self.prophecy.maybe_generate(
                memory_id, persona_id, content, metadata
            )
            result["prophecies"] = prophecies
        except BaseException as e:
            logger.warning(f"[v2] 预言生成失败: {e}")
            result["prophecy_error"] = str(e)

        return result

    # ─────────── 预言回溯（定时调用） ───────────

    async def run_prophecy_backfill(self, limit: int = 20) -> dict:
        """扫描并验证到期预言。返回统计。"""
        if not self.enabled:
            return {"checked": 0, "verified": 0, "failed": 0}
        return await self.prophecy.run_backfill(limit=limit)

    # ─────────── 查询能力（供 Agent 工具使用） ───────────

    async def trace_causal_chain(self, memory_id: int, direction: str = "both", max_depth: int = 5) -> dict:
        """因果链遍历。"""
        if not self.enabled:
            return {"error": "v2 disabled"}
        return await self.causal.trace(memory_id, direction, max_depth)

    async def get_profile_summary(self, persona_id: str, limit: int = 10) -> dict:
        """画像摘要。"""
        if not self.enabled:
            return {"error": "v2 disabled"}
        return await self.profile.get_summary(persona_id, limit)

    async def get_conflicts(self, status: str | None = None, limit: int = 20) -> list[dict]:
        """冲突记录列表。"""
        if not self.enabled:
            return []
        return await self.store.list_conflicts(status, limit)

    async def get_prophecies(self, status: str | None = None, limit: int = 20) -> list[dict]:
        """预言列表。"""
        if not self.enabled:
            return []
        return await self.store.list_prophecies(status=status, limit=limit)

    async def get_expression_style(self, persona_id: str) -> dict:
        """当前表达风格。"""
        if not self.enabled:
            return {"error": "v2 disabled"}
        return await self.expression.get_or_log_style(persona_id)
