"""记忆预言 + 预言回溯引擎。

预言生成（写入后触发）：
- causal：因果链末端为空 + 画像行为模式 → 生成"接下来会发生什么"的预言
- periodic：检测周期性表述（每周/每天/经常）→ 生成周期预言
- profile：高置信画像特征 → 生成"会继续/会保持"的预言

预言回溯（到期验证）：
- 搜索从预言创建到现在的记忆，与预言内容共享主题则 verified，否则 failed
- verified → 强化 base memory 权重
- failed → 下调相关画像特征 confidence
"""

import time
from typing import Any

from .conflict_detector import has_shared_topic
from .v2_store import V2Store

try:
    from astrbot.api import logger
except ImportError:  # 独立测试环境降级
    import logging

    logger = logging.getLogger("v2_prophecy")

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


PERIODIC_PATTERNS = ["每周", "每天", "每月", "经常", "总是", "习惯", "固定", "每周末", "每天早上", "每晚"]


class ProphecyEngine:
    """记忆预言 + 回溯。"""

    def __init__(self, store: V2Store, db_connection=None, memory_engine=None):
        self.store = store
        self.db = db_connection
        self.memory_engine = memory_engine  # 用于回溯时强化权重（可选）

    # ─────────── 生成 ───────────

    async def maybe_generate(
        self,
        memory_id: int,
        persona_id: str | None,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict]:
        """新记忆写入后尝试生成预言。返回生成的预言列表。"""
        if not content or len(content.strip()) < 6:
            return []
        metadata = metadata or {}
        generated: list[dict] = []
        strength = float(metadata.get("importance") or 0.5)

        # 1) periodic 预言：内容含周期表述
        for pat in PERIODIC_PATTERNS:
            if pat in content:
                prophecy_text = self._build_periodic_prophecy(content, pat)
                pid = await self.store.add_prophecy(
                    persona_id=persona_id,
                    content=prophecy_text,
                    base_memory_id=memory_id,
                    prophecy_type="periodic",
                    ttl_days=7.0,
                    strength=strength,
                )
                generated.append({"id": pid, "type": "periodic", "content": prophecy_text})
                break

        # 2) causal 预言：因果链末端为空 → 预判后续发展
        if persona_id and not generated:
            entry = await self.store.get_causality(memory_id)
            effects = await self.store.get_effect_chain(memory_id, max_depth=1) if entry else []
            if entry and not effects:
                prophecy_text = self._build_causal_prophecy(content)
                pid = await self.store.add_prophecy(
                    persona_id=persona_id,
                    content=prophecy_text,
                    base_memory_id=memory_id,
                    prophecy_type="causal",
                    ttl_days=14.0,
                    strength=strength,
                )
                generated.append({"id": pid, "type": "causal", "content": prophecy_text})

        # 3) profile 预言：高置信画像特征 → 保持预言
        if persona_id and not generated:
            profile = await self.store.get_profile(persona_id)
            high_traits = [t for t in profile if float(t.get("confidence") or 0) >= 0.8]
            if high_traits:
                trait = high_traits[0]
                prophecy_text = f"「{trait.get('trait_key')}」特征会继续保持：{str(trait.get('trait_value'))[:30]}"
                pid = await self.store.add_prophecy(
                    persona_id=persona_id,
                    content=prophecy_text,
                    base_memory_id=memory_id,
                    prophecy_type="profile",
                    ttl_days=21.0,
                    strength=max(strength, 0.6),
                )
                generated.append({"id": pid, "type": "profile", "content": prophecy_text})

        if generated:
            logger.info(f"[v2] 生成 {len(generated)} 条记忆预言 (memory_id={memory_id})")
        return generated

    def _build_periodic_prophecy(self, content: str, pattern: str) -> str:
        core = content.strip()
        if len(core) > 50:
            core = core[:50] + "…"
        return f"[周期预言] {pattern}模式会延续：{core}"

    def _build_causal_prophecy(self, content: str) -> str:
        core = content.strip()
        if len(core) > 50:
            core = core[:50] + "…"
        return f"[因果预言] 基于「{core}」，后续很可能会发展出相关新进展"

    # ─────────── 回溯验证 ───────────

    async def run_backfill(self, limit: int = 20) -> dict:
        """扫描所有到期预言并验证。返回处理统计。"""
        expired = await self.store.get_expired_prophecies(limit=limit)
        stats = {"checked": 0, "verified": 0, "failed": 0, "skipped": 0}
        for prophecy in expired:
            result = await self.verify(prophecy)
            stats[result["status"]] = stats.get(result["status"], 0) + 1
            stats["checked"] += 1
        return stats

    async def verify(self, prophecy: dict) -> dict:
        """验证单条预言。

        规则：从预言创建时间到现在，若出现与预言内容共享主题的新记忆 → verified。
        """
        pid = int(prophecy["id"])
        created_at = float(prophecy.get("created_at") or 0)
        content = str(prophecy.get("content") or "")
        base_id = prophecy.get("base_memory_id")
        persona_id = prophecy.get("persona_id")

        follow_up_memories = await self._fetch_follow_up_memories(
            persona_id, created_at, exclude_id=base_id, limit=15
        )
        matched = False
        matched_memory = None
        for m in follow_up_memories:
            if has_shared_topic(content, m.get("content") or ""):
                matched = True
                matched_memory = m
                break

        if matched:
            status = "verified"
            verification = {
                "correct": True,
                "matched_memory_id": matched_memory.get("id") if matched_memory else None,
                "note": "后续记忆验证了预言",
            }
            # 强化 base memory 权重
            strength_after = float(prophecy.get("strength_before") or 0.5) + 0.1
            if self.memory_engine is not None and base_id:
                try:
                    await self.memory_engine.update_importance(
                        int(base_id), min(0.98, strength_after)
                    )
                except BaseException:
                    pass
            await self.store.update_prophecy_result(pid, status, verification, min(0.98, strength_after))
            # 上调画像（若预言关联了画像特征）
            await self._adjust_profile_from_verification(persona_id, content, up=True)
            # v2.1 家庭反馈 A1/A7：预言验证成功 → 事件广播（画像置信已就地调整）
            await self._emit_prophecy_feedback(
                pid, "verified", content, persona_id,
                {"correct": True, "matched_memory_id": verification.get("matched_memory_id")},
            )
        else:
            status = "failed"
            verification = {
                "correct": False,
                "note": "到期未见后续记忆证实",
            }
            strength_after = max(0.1, float(prophecy.get("strength_before") or 0.5) - 0.15)
            await self.store.update_prophecy_result(pid, status, verification, strength_after)
            await self._adjust_profile_from_verification(persona_id, content, up=False)
            # v2.1 家庭反馈 A1/A6/A7：预言失败 → PROPHECY_VERIFIED(false) + PROPHECY_EXPIRED（供转提醒）
            await self._emit_prophecy_feedback(
                pid, "failed", content, persona_id, {"correct": False}
            )
            await self._emit_prophecy_expired(pid, content, persona_id)

        return {"id": pid, "status": status, "strength_after": strength_after}

    async def re_evaluate(self, conflict_id: int, resolution_type: str = "", reason: str = "") -> dict:
        """A2：冲突确认后重估相关预言。

        找到 active 预言中与冲突原因共享主题的条目，
        标记 re_evaluated（证据被推翻，弱化 strength），并记 feedback_log。
        """
        re_evaluated: list[int] = []
        if not reason:
            return {"checked": 0, "re_evaluated": re_evaluated}
        try:
            active = await self.store.list_prophecies(status="active", limit=50)
        except BaseException:
            logger.warning("[v2] 预言重估：读取 active 预言失败", exc_info=True)
            return {"checked": 0, "re_evaluated": re_evaluated}
        for prophecy in active:
            content = str(prophecy.get("content") or "")
            if content and has_shared_topic(reason, content):
                pid = int(prophecy["id"])
                old_strength = float(prophecy.get("strength_before") or 0.5)
                new_strength = max(0.1, old_strength - 0.05)
                verification = dict(prophecy.get("verification") or {})
                verification["re_evaluated"] = True
                verification["conflict_id"] = conflict_id
                verification["note"] = "关联冲突被确认，预言强度弱化"
                try:
                    await self.store.update_prophecy_result(pid, "active", verification, new_strength)
                    re_evaluated.append(pid)
                except BaseException:
                    logger.warning(f"[v2] 预言重估失败 pid={pid}", exc_info=True)
        try:
            await self.store.record_feedback(
                from_module="conflict",
                to_module="prophecy",
                event_type="prophecy_re_evaluated",
                memory_id=conflict_id,
                payload={"resolution_type": resolution_type, "re_evaluated": re_evaluated},
            )
        except BaseException:
            logger.warning("[v2] 预言重估 feedback_log 记录失败", exc_info=True)
        # A1 补全：强度变化 → STRENGTH_CHANGED 事件（供外部观察/联动）
        if re_evaluated and _HAS_BUS and MemoryEventType is not None:
            try:
                bus = get_event_bus()
                if bus is not None:
                    bus.publish_nowait(
                        MemoryEvent(
                            type=MemoryEventType.STRENGTH_CHANGED,
                            memory_id=conflict_id,
                            memory_type="prophecy",
                            metadata={
                                "prophecy_ids": re_evaluated,
                                "reason": reason[:80],
                                "note": "关联冲突确认，预言强度弱化",
                            },
                        )
                    )
            except BaseException:
                logger.warning("[v2] STRENGTH_CHANGED 事件发布失败", exc_info=True)
        return {"checked": len(active), "re_evaluated": re_evaluated}

    async def _emit_prophecy_feedback(
        self,
        pid: int,
        status: str,
        content: str,
        persona_id: str | None,
        payload: dict,
    ) -> None:
        """A1/A7：预言验证完成 → 发布 PROPHECY_VERIFIED + 记 feedback_log。"""
        try:
            if _HAS_BUS and MemoryEventType is not None:
                bus = get_event_bus()
                if bus is not None:
                    await bus.publish(
                        MemoryEvent(
                            type=MemoryEventType.PROPHECY_VERIFIED,
                            memory_id=pid,
                            memory_type="prophecy",
                            metadata={"status": status, "persona_id": persona_id, **payload},
                        )
                    )
        except BaseException:
            logger.warning("[v2] PROPHECY_VERIFIED 事件发布失败", exc_info=True)
        try:
            await self.store.record_feedback(
                from_module="prophecy",
                to_module="profile",
                event_type="prophecy_verified",
                memory_id=pid,
                persona_id=persona_id,
                payload={"status": status, "content": content[:80], **payload},
            )
        except BaseException:
            logger.warning("[v2] feedback_log 记录失败", exc_info=True)

    async def _emit_prophecy_expired(self, pid: int, content: str, persona_id: str | None) -> None:
        """A6：预言到期未证实 → 发布 PROPHECY_EXPIRED（helper 订阅转提醒）。"""
        try:
            if _HAS_BUS and MemoryEventType is not None:
                bus = get_event_bus()
                if bus is not None:
                    await bus.publish(
                        MemoryEvent(
                            type=MemoryEventType.PROPHECY_EXPIRED,
                            memory_id=pid,
                            memory_type="prophecy",
                            metadata={"persona_id": persona_id, "content": content},
                        )
                    )
        except BaseException:
            logger.warning("[v2] PROPHECY_EXPIRED 事件发布失败", exc_info=True)

    async def _adjust_profile_from_verification(self, persona_id: str | None, content: str, up: bool) -> None:
        """预言验证结果 → 画像 confidence 微调。"""
        if not persona_id:
            return
        profile = await self.store.get_profile(persona_id)
        for trait in profile:
            trait_value = str(trait.get("trait_value") or "")
            if trait_value and has_shared_topic(content, trait_value):
                if up:
                    # 上调：轻微（0.9 封顶）
                    conf = min(0.95, float(trait.get("confidence") or 0.5) + 0.05)
                    await self.store.upsert_profile_trait(
                        persona_id,
                        str(trait.get("trait_key") or ""),
                        trait_value,
                        conf,
                        None,
                        "预言验证成功，特征置信上调",
                    )
                else:
                    await self.store.downweight_profile_trait(
                        persona_id,
                        str(trait.get("trait_key") or ""),
                        "预言未兑现，特征置信下调",
                    )
                break

    async def _fetch_follow_up_memories(
        self, persona_id: str | None, after_ts: float, exclude_id: int | None, limit: int = 15
    ) -> list[dict]:
        """取某时间点之后的新记忆（用于验证预言）。"""
        if self.db is None:
            return []
        try:
            if persona_id:
                cursor = await self.db.execute(
                    """
                    SELECT id, text FROM documents
                    WHERE json_extract(metadata, '$.persona_id') = ?
                      AND id != ?
                      AND COALESCE(CAST(json_extract(metadata, '$.create_time') AS REAL), id) > ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (persona_id, exclude_id or 0, after_ts, limit),
                )
            else:
                cursor = await self.db.execute(
                    """
                    SELECT id, text FROM documents
                    WHERE id != ?
                      AND COALESCE(CAST(json_extract(metadata, '$.create_time') AS REAL), id) > ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (exclude_id or 0, after_ts, limit),
                )
            rows = await cursor.fetchall()
            return [
                {
                    "id": row["id"] if isinstance(row, dict) else row[0],
                    "content": row["text"] if isinstance(row, dict) else row[1],
                }
                for row in rows
            ]
        except BaseException:
            return []
