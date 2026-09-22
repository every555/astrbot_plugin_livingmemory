"""
L3 跨会话记忆合成器 (L3 Cross-Session Memory Synthesizer)
v9: 借鉴 DeepTutor 三层记忆架构

功能：
1. 扫描 tier=2 (L2 会话摘要) 的记忆原子
2. 按主题分组，调用 LLM 提炼跨会话的深层认知
3. 生成 tier=3 (L3 跨域合成) 的记忆原子，附带 source_ids 溯源链

设计理念：
- L1: 原始消息（conversation_store messages 表）
- L2: 会话摘要（SessionSummaryManager 生成）
- L3: 跨会话合成（本模块生成）
- 溯源链: L3 → L2(source_ids) → L1(source_ids)
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

from .models.memory_atom import (
    AtomStatus,
    AtomType,
    DecayType,
    MemoryAtom,
)

if TYPE_CHECKING:
    from .base.config_manager import ConfigManager
    from .managers.memory_engine import MemoryEngine

# ── 常量 ──────────────────────────────────────────────
_L3_SYNTHESIS_PROMPT = """你是一个记忆合成专家。请阅读以下来自不同会话的【L2 摘要记忆】，提炼出跨会话的深层认知。

输入格式: 每条记忆有 id、content(摘要内容)、session_id(来源会话)。

要求：
1. 识别跨多个会话反复出现的主题、模式或趋势
2. 提炼橘子（春雪的老公）的长期偏好、习惯、性格特征
3. 发现知识范围的演变（学到的新话题、掌握的新技能）
4. 生成 3-8 条合成记忆，每条一句话
5. 称呼铁律：对话双方固定称呼为「橘子」（老公，明江）与「春雪」（老婆）；严禁使用「用户」「AI」「助手」等称呼

输出严格的 JSON 数组（不要有其他内容）：
[
  {
    "content": "橘子（春雪的老公）的长期偏好/模式描述",
    "category": "preference|habit|personality|knowledge|relationship",
    "importance": 0.7,
    "source_ids": ["源L2记忆的id列表"]
  }
]

输入记忆：
"""

_L3_MERGE_PROMPT = """你是一个记忆整理专家。请阅读现有的 L3 合成记忆和新的候选记忆，判断是否需要合并或更新。

现有 L3 记忆（可能有重复/过时的）：
{existing}

新的候选记忆：
{candidates}

要求：
1. 合并内容相似的记忆（去重）
2. 更新过时的信息
3. 保留仍然有效的记忆
4. 输出合并后的完整 L3 记忆列表
5. 称呼铁律：对话双方固定称呼为「橘子」（老公，明江）与「春雪」（老婆）；严禁使用「用户」「AI」「助手」等称呼

输出严格的 JSON 数组（不要有其他内容）：
[
  {
    "content": "合并/保留后的记忆描述",
    "category": "preference|habit|personality|knowledge|relationship",
    "importance": 0.7,
    "source_ids": ["合并后的所有源id"]
  }
]
"""

# 两次合成之间的最小间隔（秒）
_MIN_SYNTHESIS_INTERVAL = 3600  # 1 小时
# 最少需要多少条新的 L2 记忆才触发合成
_MIN_NEW_L2_COUNT = 3
# 自动合成检查间隔（秒）
_AUTO_CHECK_INTERVAL = 1800  # 30 分钟检查一次


class L3Synthesizer:
    """L3 跨会话记忆合成器"""

    def __init__(
        self,
        config_manager: "ConfigManager",
        memory_engine: "MemoryEngine",
        context: Any,
    ):
        self.config_manager = config_manager
        self.memory_engine = memory_engine
        self.context = context

        # 上次合成时间
        self._last_synthesis_time: float = 0.0
        # 已合入 L3 的 L2 记忆 id 集合（去重）
        self._processed_l2_ids: set[int] = set()
        # 上次扫描时最新 L2 记忆的时间戳
        self._last_l2_timestamp: float = 0.0
        # 自动合成后台任务
        self._auto_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动自动合成后台循环"""
        if self._auto_task is not None:
            return
        self._auto_task = asyncio.create_task(self._auto_synthesize_loop())
        logger.info(f"[L3Synthesizer] 自动合成已启动 (每 {_AUTO_CHECK_INTERVAL}s 检查)")

    async def stop(self) -> None:
        """停止自动合成后台循环"""
        if self._auto_task is not None:
            self._auto_task.cancel()
            self._auto_task = None
            logger.info("[L3Synthesizer] 自动合成已停止")

    async def _auto_synthesize_loop(self) -> None:
        """定期检查是否有足够的新 L2 记忆，自动触发合成"""
        # 首次启动延迟 60 秒，等插件完全加载
        await asyncio.sleep(60)
        while True:
            try:
                result = await self.synthesize(force=False)
                if result["skipped"]:
                    logger.debug(f"[L3Synthesizer] 自动检查跳过: {result['skipped']}")
                else:
                    logger.info(
                        f"[L3Synthesizer] 自动合成完成: "
                        f"候选 {result['synthesized']} 条, 写入 {result['merged']} 条"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[L3Synthesizer] 自动合成异常: {e}", exc_info=True)
            await asyncio.sleep(_AUTO_CHECK_INTERVAL)

    async def synthesize(self, force: bool = False) -> dict[str, Any]:
        """
        执行一次完整的 L3 合成周期。

        Args:
            force: 强制合成，忽略时间间隔限制

        Returns:
            dict: {"synthesized": int, "merged": int, "skipped": str}
        """
        now = time.time()

        # 检查时间间隔
        if not force and (now - self._last_synthesis_time) < _MIN_SYNTHESIS_INTERVAL:
            remaining = int(_MIN_SYNTHESIS_INTERVAL - (now - self._last_synthesis_time))
            logger.info(f"[L3Synthesizer] 跳过合成，距离上次仅 {remaining}s")
            return {"synthesized": 0, "merged": 0, "skipped": f"冷却中，还需 {remaining}s"}

        # 1. 获取所有活跃的 L2 记忆（tier=2, 会话摘要）
        l2_atoms = await self._fetch_l2_atoms()
        if not l2_atoms:
            logger.info("[L3Synthesizer] 没有足够的 L2 记忆可合成")
            return {"synthesized": 0, "merged": 0, "skipped": "没有足够的 L2 记忆"}

        # 2. 筛选新的 L2 记忆（尚未被处理过的）
        new_l2 = [a for a in l2_atoms if a.atom_id not in self._processed_l2_ids]
        if len(new_l2) < _MIN_NEW_L2_COUNT and not force:
            logger.info(f"[L3Synthesizer] 新 L2 记忆不足 ({len(new_l2)} < {_MIN_NEW_L2_COUNT})")
            return {"synthesized": 0, "merged": 0, "skipped": f"新 L2 记忆不足 ({len(new_l2)})"}

        # 3. 调用 LLM 合成 L3 候选
        candidates = await self._llm_synthesize(new_l2)
        if not candidates:
            logger.warning("[L3Synthesizer] LLM 合成返回空结果")
            return {"synthesized": 0, "merged": 0, "skipped": "LLM 合成返回空"}

        # 4. 读取现有 L3 记忆
        existing_l3 = await self._fetch_l3_atoms()

        # 5. 合并去重
        merged = await self._llm_merge(existing_l3, candidates)

        # 6. 写入新的 L3 记忆
        saved_count = await self._save_l3_atoms(merged)

        # 7. 标记已处理
        for atom in new_l2:
            self._processed_l2_ids.add(atom.atom_id)
        self._last_synthesis_time = now

        logger.info(
            f"[L3Synthesizer] 合成完成: {saved_count} 条 L3 记忆 "
            f"(来自 {len(new_l2)} 条 L2, 合并自 {len(candidates)} 条候选)"
        )
        return {
            "synthesized": len(candidates),
            "merged": saved_count,
            "skipped": "",
        }

    async def _fetch_l2_atoms(self) -> list[MemoryAtom]:
        """获取所有活跃的 L2 (会话摘要) 记忆原子"""
        try:
            if not self.memory_engine or not hasattr(self.memory_engine, "atom_store"):
                return []

            async with self.memory_engine.atom_store._connect() as db:
                db.row_factory = __import__("aiosqlite").Row
                cursor = await db.execute(
                    """
                    SELECT * FROM memory_atoms
                    WHERE tier = 2
                      AND status = 'active'
                      AND json_extract(metadata, '$.atom_subtype') = 'session_summary'
                    ORDER BY created_at ASC
                    """
                )
                rows = await cursor.fetchall()

            store = self.memory_engine.atom_store
            return [store._row_to_atom(row) for row in rows]
        except Exception as e:
            logger.warning(f"[L3Synthesizer] 获取 L2 记忆失败: {e}")
            return []

    async def _fetch_l3_atoms(self) -> list[MemoryAtom]:
        """获取所有现有的 L3 记忆原子"""
        try:
            if not self.memory_engine or not hasattr(self.memory_engine, "atom_store"):
                return []

            async with self.memory_engine.atom_store._connect() as db:
                db.row_factory = __import__("aiosqlite").Row
                cursor = await db.execute(
                    """
                    SELECT * FROM memory_atoms
                    WHERE tier = 3
                      AND status = 'active'
                    ORDER BY importance DESC
                    """
                )
                rows = await cursor.fetchall()

            store = self.memory_engine.atom_store
            return [store._row_to_atom(row) for row in rows]
        except Exception as e:
            logger.warning(f"[L3Synthesizer] 获取 L3 记忆失败: {e}")
            return []

    async def _llm_synthesize(
        self, l2_atoms: list[MemoryAtom]
    ) -> list[dict[str, Any]]:
        """调用 LLM 从 L2 记忆中合成 L3 候选"""
        provider = self._get_provider()
        if not provider:
            logger.warning("[L3Synthesizer] LLM provider 不可用，使用规则合成")
            return self._fallback_synthesize(l2_atoms)

        # 构建输入
        l2_texts = []
        for a in l2_atoms:
            session_id = a.session_id or "unknown"
            brief = a.content[:200] if a.content else ""
            l2_texts.append(f"- [id={a.atom_id}] [{session_id}] {brief}")
        l2_input = "\n".join(l2_texts)

        prompt = _L3_SYNTHESIS_PROMPT + l2_input

        try:
            response = await provider.text_chat(prompt)
            # 提取 JSON
            json_str = self._extract_json(response)
            if json_str:
                candidates = json.loads(json_str)
                if isinstance(candidates, list):
                    return candidates
        except Exception as e:
            logger.warning(f"[L3Synthesizer] LLM 合成调用失败: {e}")

        return self._fallback_synthesize(l2_atoms)

    async def _llm_merge(
        self,
        existing: list[MemoryAtom],
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """调用 LLM 合并现有 L3 记忆和新候选"""
        if not existing:
            return candidates
        if not candidates:
            return []

        provider = self._get_provider()
        if not provider or len(existing) + len(candidates) < 3:
            # 简单合并：新候选直接追加
            return candidates

        existing_texts = []
        for a in existing:
            existing_texts.append(
                f"- [id={a.atom_id}] [{a.metadata.get('category', 'unknown')}] {a.content}"
            )
        candidate_texts = []
        for c in candidates:
            candidate_texts.append(
                f"- [category={c.get('category', 'unknown')}] {c.get('content', '')}"
            )

        # 注意：不能用 str.format —— 模板正文含字面 JSON 花括号（输出格式示例），
        # format 会把它当占位符 → KeyError（2026-08-14 修复）
        prompt = (
            _L3_MERGE_PROMPT
            .replace("{existing}", chr(10).join(existing_texts))
            .replace("{candidates}", chr(10).join(candidate_texts))
        )

        try:
            response = await provider.text_chat(prompt)
            json_str = self._extract_json(response)
            if json_str:
                merged = json.loads(json_str)
                if isinstance(merged, list):
                    return merged
        except Exception as e:
            logger.warning(f"[L3Synthesizer] LLM 合并失败: {e}")

        return candidates

    async def _save_l3_atoms(self, merged: list[dict[str, Any]]) -> int:
        """将合并后的 L3 候选写入 memory_atoms（tier=3）"""
        if not merged:
            return 0

        # 先停用旧的 L3 记忆
        try:
            await self._deactivate_old_l3()
        except Exception as e:
            logger.warning(f"[L3Synthesizer] 停用旧 L3 记忆失败: {e}")

        # 写入新的
        now = time.time()
        atoms: list[MemoryAtom] = []

        # 找一个 parent_memory_id
        parent_id = await self._get_parent_id()

        for item in merged:
            content = item.get("content", "")
            if not content:
                continue

            category = item.get("category", "synthesis")
            importance = float(item.get("importance", 0.6))
            source_ids = item.get("source_ids", [])

            # 转为 int 列表（LLM 可能返回字符串）
            clean_source_ids = []
            for sid in source_ids:
                try:
                    clean_source_ids.append(int(sid))
                except (ValueError, TypeError):
                    pass

            # 刀⑥写入端打档：L3 归纳跨会话无 session，按来源继承最严档（owner > intimate > public）；全无档=不打档维持 fail-closed
            _ws_l3 = await self._inherit_strictest_scope(clean_source_ids)
            metadata = {
                "atom_subtype": "l3_synthesis",
                "category": category,
                "synthesized_at": datetime.now(timezone.utc).isoformat(),
                "l2_source_count": len(clean_source_ids),
                **({"privacy_scope": _ws_l3} if _ws_l3 else {}),
            }

            atom = MemoryAtom(
                parent_memory_id=parent_id,
                atom_type=AtomType.FACTUAL,
                content=content,
                entities=[],
                importance=importance,
                confidence=0.75,
                created_at=now,
                last_accessed_at=now,
                event_time=now,
                ttl_days=60.0,  # L3 记忆保存更久
                expires_at=now + 60.0 * 86400,
                status=AtomStatus.ACTIVE,
                decay_type=DecayType.LINEAR,
                session_id=None,  # L3 跨会话，不属于单个 session
                metadata=metadata,
                tier=3,
                source_ids=clean_source_ids,
            )
            atoms.append(atom)

        if atoms and self.memory_engine and hasattr(self.memory_engine, "atom_store"):
            ids = await self.memory_engine.atom_store.insert_many(atoms)
            logger.info(f"[L3Synthesizer] 已写入 {len(ids)} 条 L3 记忆")
            return len(ids)

        return 0

    async def _deactivate_old_l3(self) -> None:
        """将旧的 L3 记忆标记为 superseded"""
        try:
            if not self.memory_engine or not hasattr(self.memory_engine, "atom_store"):
                return

            async with self.memory_engine.atom_store._connect() as db:
                await db.execute(
                    """
                    UPDATE memory_atoms
                    SET status = 'superseded'
                    WHERE tier = 3 AND status = 'active'
                    """
                )
                await db.commit()
        except Exception as e:
            logger.warning(f"[L3Synthesizer] 停用旧 L3 记忆失败: {e}")

    async def _inherit_strictest_scope(self, source_ids: list) -> str | None:
        """刀⑥：L3 按来源继承最严档（owner > intimate > public）；来源无档/查询失败 → None（维持 fail-closed）"""
        if not source_ids or not self.memory_engine or not hasattr(self.memory_engine, "atom_store"):
            return None
        try:
            placeholders = ",".join("?" for _ in source_ids)
            async with self.memory_engine.atom_store._connect() as db:
                cursor = await db.execute(
                    f"SELECT metadata FROM memory_atoms WHERE id IN ({placeholders})",
                    source_ids,
                )
                rows = await cursor.fetchall()
            rank = {"owner": 3, "intimate": 2, "public": 1}
            scopes = []
            for row in rows:
                try:
                    meta = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
                except Exception:
                    meta = {}
                sc = meta.get("privacy_scope")
                if sc in rank:
                    scopes.append(sc)
            return max(scopes, key=lambda s: rank[s]) if scopes else None
        except Exception as e:
            logger.debug(f"[L3Synthesizer] 档位继承失败（不打档）: {e}")
            return None

    async def _get_parent_id(self) -> int:
        """获取一个有效的 parent_memory_id"""
        try:
            if self.memory_engine and hasattr(self.memory_engine, "atom_store"):
                async with self.memory_engine.atom_store._connect() as db:
                    cursor = await db.execute(
                        "SELECT parent_memory_id FROM memory_atoms WHERE status='active' LIMIT 1"
                    )
                    row = await cursor.fetchone()
                    if row:
                        return int(row[0])
        except Exception:
            pass
        return 1

    def _get_provider(self):
        """获取 LLM provider"""
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    def _extract_json(self, text) -> str | None:
        """从 LLM 响应中提取 JSON（兼容 str / v4.27 LLMResponse 对象）"""
        # v4.27 起 provider.text_chat 返回 LLMResponse（dataclass），
        # 直接 .strip() 会报 'LLMResponse' object has no attribute 'strip'
        if text is not None and not isinstance(text, str):
            ct = getattr(text, "completion_text", None)
            text = ct if isinstance(ct, str) else ""
        if not text:
            return None
        # 尝试直接解析
        text = text.strip()
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            pass
        # 尝试提取 ```json ... ``` 代码块
        import re
        m = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if m:
            return m.group(1).strip()
        # 尝试提取 [ ... ]
        m = re.search(r'\[[\s\S]*\]', text)
        if m:
            return m.group(0).strip()
        return None

    def _fallback_synthesize(
        self, l2_atoms: list[MemoryAtom]
    ) -> list[dict[str, Any]]:
        """规则合成（LLM 不可用时）"""
        # 按 topic 简单聚合
        topic_map: dict[str, list[int]] = {}
        for a in l2_atoms:
            topics = a.metadata.get("topics", [])
            if not topics:
                topics = ["general"]
            for topic in topics:
                topic_lower = topic.lower().strip()
                if topic_lower not in topic_map:
                    topic_map[topic_lower] = []
                topic_map[topic_lower].append(a.atom_id)

        results = []
        for topic, source_ids in topic_map.items():
            if len(source_ids) >= 2:
                results.append({
                    "content": f"多次讨论 '{topic}' 相关话题",
                    "category": "knowledge",
                    "importance": 0.5,
                    "source_ids": source_ids,
                })

        return results


__all__ = ["L3Synthesizer"]
