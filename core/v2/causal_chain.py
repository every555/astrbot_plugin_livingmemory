"""因果证据链服务。

写入记忆时自动记录：
- source：来源（session_id / trigger_type / trigger_message）
- causality：因果角色（pre_cause_id / role / 后续效应）
- context_snapshot：写入时的上下文快照

春雪原创：记忆不是孤立点，而是因果叙事的一部分。
参考 Cyrene-Agent 的 evidence 机制（quoteSnippet + conversationId + messageIds），
但升级为带因果角色的有向链。
"""

import time
from typing import Any

from .v2_store import V2Store

try:
    from astrbot.api import logger
except ImportError:  # 独立测试环境降级
    import logging

    logger = logging.getLogger("v2_causal_chain")

# 事件总线（v2.1 家庭协作反馈，独立测试环境降级为无操作）
try:
    from ..events.event_bus import MemoryEvent, MemoryEventType, get_event_bus

    _HAS_BUS = True
except Exception:  # pragma: no cover
    _HAS_BUS = False

    MemoryEventType = None
    MemoryEvent = None

    def get_event_bus():  # type: ignore
        return None


class CausalChainService:
    """因果证据链服务。"""

    def __init__(self, store: V2Store, db_connection=None):
        self.store = store
        self.db = db_connection  # 主 documents 连接的引用（可选，用于读取历史）

    async def record(
        self,
        memory_id: int,
        persona_id: str | None,
        session_id: str | None,
        metadata: dict[str, Any] | None,
        content: str = "",
    ) -> dict:
        """写入记忆时自动生成证据链记录。

        提取 source / trigger / context_snapshot，并尝试定位 pre_cause。
        """
        metadata = metadata or {}
        # ── source 提取 ──
        source_window = metadata.get("source_window") or {}
        if isinstance(source_window, str):
            try:
                import json

                source_window = json.loads(source_window)
            except (TypeError, ValueError):
                source_window = {}
        # trigger_type：优先从 memory_origin 判断来源（更可靠）
        origin = str(metadata.get("memory_origin") or "")
        if "agent" in origin or "tool" in origin:
            trigger_type = "agent_tool"
        elif origin == "conversation" or origin == "auto":
            trigger_type = "conversation"
        elif origin == "reflection":
            trigger_type = "reflection"
        else:
            trigger_type = str(source_window.get("triggered_by") or "agent_tool")
        trigger_message = str(
            source_window.get("trigger_message")
            or metadata.get("memorize_reason")
            or ""
        )
        if not trigger_message:
            trigger_message = str(source_window.get("tool_name") or "")

        # ── 定位 pre_cause：同 persona 最近一条记忆（时间因果） ──
        pre_cause_id = await self._find_pre_cause(persona_id, memory_id)

        # ── context_snapshot：把 metadata 里的辅助信息作为上下文快照 ──
        context_snapshot = {
            "topics": metadata.get("topics") or [],
            "key_facts": metadata.get("key_facts") or [],
            "summary_quality": metadata.get("summary_quality"),
            "memory_origin": metadata.get("memory_origin"),
            "sentiment": metadata.get("sentiment"),
        }

        causality_id = await self.store.add_causality(
            memory_id=memory_id,
            persona_id=persona_id,
            session_id=session_id,
            trigger_type=trigger_type,
            trigger_message=trigger_message,
            pre_cause_id=pre_cause_id,
            role="result" if pre_cause_id else "fact",
            context_snapshot=context_snapshot,
        )
        logger.debug(
            f"[v2] 因果证据链已记录: memory_id={memory_id} "
            f"pre_cause={pre_cause_id} trigger={trigger_type}"
        )
        # v2.1 家庭反馈 A4：因果链连接 → CAUSAL_LINKED（预言生成已在 v2 流水线触发）
        try:
            if _HAS_BUS and MemoryEventType is not None:
                bus = get_event_bus()
                if bus is not None:
                    await bus.publish(
                        MemoryEvent(
                            type=MemoryEventType.CAUSAL_LINKED,
                            memory_id=memory_id,
                            memory_type="causality",
                            metadata={
                                "persona_id": persona_id,
                                "pre_cause_id": pre_cause_id,
                                "causality_id": causality_id,
                                "role": "result" if pre_cause_id else "fact",
                            },
                        )
                    )
        except BaseException:
            logger.warning("[v2] CAUSAL_LINKED 事件发布失败", exc_info=True)
        try:
            await self.store.record_feedback(
                from_module="causal",
                to_module="prophecy",
                event_type="causal_linked",
                memory_id=memory_id,
                persona_id=persona_id,
                payload={"pre_cause_id": pre_cause_id, "role": "result" if pre_cause_id else "fact"},
            )
        except BaseException:
            logger.warning("[v2] feedback_log 记录失败", exc_info=True)
        return {
            "causality_id": causality_id,
            "pre_cause_id": pre_cause_id,
            "trigger_type": trigger_type,
        }

    async def _find_pre_cause(self, persona_id: str | None, current_memory_id: int) -> int | None:
        """找到当前记忆的"前因"：同 persona 最近一条有因果记录的旧记忆。"""
        if self.db is None:
            return None
        try:
            if persona_id:
                cursor = await self.db.execute(
                    """
                    SELECT id FROM documents
                    WHERE json_extract(metadata, '$.persona_id') = ?
                      AND id != ?
                    ORDER BY COALESCE(CAST(json_extract(metadata, '$.create_time') AS REAL), id) DESC
                    LIMIT 1
                    """,
                    (persona_id, current_memory_id),
                )
            else:
                cursor = await self.db.execute(
                    """
                    SELECT id FROM documents
                    WHERE id != ?
                    ORDER BY COALESCE(CAST(json_extract(metadata, '$.create_time') AS REAL), id) DESC
                    LIMIT 1
                    """,
                    (current_memory_id,),
                )
            row = await cursor.fetchone()
            if row is None:
                return None
            candidate_id = int(row["id"] if isinstance(row, dict) else row[0])
            # 只在与自身不同的情况下建立前因
            return candidate_id if candidate_id != current_memory_id else None
        except BaseException:
            return None

    async def trace(self, memory_id: int, direction: str = "both", max_depth: int = 5) -> dict:
        """因果链遍历。

        Args:
            memory_id: 起点记忆
            direction: 'cause'（前因追溯）| 'effect'（后果展开）| 'both'（完整叙事）
            max_depth: 最大深度
        """
        result: dict[str, Any] = {"memory_id": memory_id, "nodes": {}}
        if direction in ("cause", "both"):
            causes = await self.store.get_cause_chain(memory_id, max_depth)
            result["causes"] = causes
        if direction in ("effect", "both"):
            effects = await self.store.get_effect_chain(memory_id, max_depth)
            result["effects"] = effects
        if direction == "both":
            # 完整叙事：前因 + 自身 + 后果（按时间正序）
            narrative: list[dict] = []
            cause_ids = [c["memory_id"] for c in reversed(result.get("causes", []))]
            effect_ids = [e["memory_id"] for e in result.get("effects", [])]
            self_entry = await self.store.get_causality(memory_id)
            all_ids = []
            for cid in cause_ids:
                if cid not in all_ids:
                    all_ids.append(cid)
            if memory_id not in all_ids:
                all_ids.append(memory_id)
            for eid in effect_ids:
                if eid not in all_ids:
                    all_ids.append(eid)
            for mid in all_ids:
                entry = await self.store.get_causality(mid)
                if entry:
                    narrative.append(entry)
            result["narrative"] = narrative
            _ = self_entry
        return result
