"""
会话摘要管理器 (Session Summary Manager)
v5.3 新增功能：跨会话连贯性

功能：
1. 空闲超时自动生成会话摘要
2. 新会话开头注入上次摘要
3. 情感连续性 — 记住上次的心情
4. 续接点追踪 — 记住未完成的事项
5. 衰减策略 — 3天完整 / 3-7天精简 / 7天归档

借鉴：
- DeerFlow 的三层记忆（userContext + facts + history）
- AWS Bedrock 的 session-end summary
- Spring AI AutoMemoryTools 的自主记忆管理
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

from .models.memory_atom import AtomStatus, AtomType, DecayType, MemoryAtom

if TYPE_CHECKING:
    from .base.config_manager import ConfigManager
    from .managers.conversation_manager import ConversationManager
    from .managers.memory_engine import MemoryEngine

# ── 常量 ──────────────────────────────────────────────
_SUMMARY_PROMPT = """请根据以下对话记录，生成一段结构化的会话摘要。

要求：
1. 提取主要话题（2-5个关键词）
2. 总结本次对话的关键决策和结论
3. 判断对话的情感基调（happy/excited/calm/tired/frustrated/neutral）
4. 提取未完成的事项或"下次继续"的内容（如果没有则留空）
5. 列出提到的关键实体（人名、项目名、技术名词等）

请严格按以下JSON格式输出（不要有其他内容）：
{
  "topics": ["话题1", "话题2"],
  "decisions": ["决策1", "决策2"],
  "emotion": "excited",
  "continuation_points": ["待续事项1", "待续事项2"],
  "key_entities": ["实体1", "实体2"],
  "brief": "一句话概述本次对话（对话双方称呼「橘子」与「春雪」，禁用「用户/AI助手」）"
}

对话记录：
"""

_IDLE_TIMEOUT_SECONDS = 1800  # 30 分钟空闲触发
_CHECK_INTERVAL_SECONDS = 60  # 每 60 秒检查一次
_MIN_MESSAGES_FOR_SUMMARY = 6  # 少于 6 条消息不生成摘要

# v5.5: Conversation Digest — 每 N 轮对话自动生成叙事摘要
_DIGEST_TURN_INTERVAL = 10  # 每 10 轮（1轮=用户+助手）触发一次
_DIGEST_NARRATIVE_PROMPT = """请根据以下对话记录，生成一段叙事性的对话摘要。

要求：
1. 用流畅的叙事语言（非条目列表），讲述这段对话中发生了什么
2. 包含关键人物、事件、情感变化
3. 保留重要的事实信息（时间、地点、人名等）
4. 控制在 150-300 字
5. 用第三人称叙述；对话双方固定称呼为「橘子」（老公）与「春雪」（老婆），严禁使用「用户」「AI助手」等称呼

直接输出摘要文本，不要加标题或格式标记。

对话记录：
"""


class SessionSummaryManager:
    """会话摘要管理器"""

    def __init__(
        self,
        config_manager: "ConfigManager",
        memory_engine: "MemoryEngine",
        conversation_manager: "ConversationManager",
        context: Any,
    ):
        self.config_manager = config_manager
        self.memory_engine = memory_engine
        self.conversation_manager = conversation_manager
        self.context = context

        # 每个 session 的最后活跃时间
        self._last_activity: dict[str, float] = {}
        # 已生成摘要的 session（避免重复生成）
        self._summarized_sessions: set[str] = set()
        # v5.5: 每 session 的用户消息轮次计数器（用于 Conversation Digest）- 现在持久化
        self._turn_counters: dict[str, int] = {}
        # v5.5: 已生成 digest 的轮次标记（避免同一轮次重复生成）
        self._digest_generated_turns: dict[str, set[int]] = {}
        # 消息获取锁，避免并发问题
        self._message_locks: dict[str, asyncio.Lock] = {}
        # 后台检查任务
        self._check_task: asyncio.Task | None = None
        # 是否已启动
        self._started = False

    # ── 生命周期 ──────────────────────────────────────

    def start(self) -> None:
        """启动后台空闲检测"""
        if self._started:
            return
        self._started = True
        # 初始化持久化计数器
        self._init_persistent_counters()
        self._check_task = asyncio.create_task(self._idle_check_loop())
        logger.info("[SessionSummary] 后台空闲检测已启动")

    def _init_persistent_counters(self) -> None:
        """初始化持久化计数器 - 从数据库加载"""
        try:
            db_path = self.context.get_plugin_data_path("livingmemory.db")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # 创建计数器表（如果不存在）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS digest_turns (
                    session_id TEXT PRIMARY KEY,
                    turn_count INTEGER NOT NULL,
                    last_updated REAL NOT NULL,
                    created_at REAL NOT NULL
                )
            ''')
            
            # 加载所有会话的计数器
            cursor.execute('SELECT session_id, turn_count FROM digest_turns')
            for session_id, turn_count in cursor.fetchall():
                self._turn_counters[session_id] = turn_count
            
            conn.close()
            logger.info(f"[SessionSummary] 加载了 {len(self._turn_counters)} 个会话的计数器")
        except Exception as e:
            logger.warning(f"[SessionSummary] 初始化持久化计数器失败: {e}")

    def _save_counter(self, session_id: str, turn_count: int) -> None:
        """保存计数器到数据库"""
        try:
            db_path = self.context.get_plugin_data_path("livingmemory.db")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR REPLACE INTO digest_turns (session_id, turn_count, last_updated, created_at)
                VALUES (?, ?, ?, ?)
            ''', (session_id, turn_count, time.time(), time.time()))
            conn.commit()
        except Exception as e:
            logger.warning(f"[SessionSummary] 保存计数器失败 session={session_id}: {e}")

    async def stop(self) -> None:
        """停止后台任务"""
        if self._check_task and not self._check_task.done():
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        self._started = False
        logger.info("[SessionSummary] 后台空闲检测已停止")

    def _init_persistent_counters(self) -> None:
        """初始化持久化计数器 - 从数据库加载"""
        try:
            db_path = self._get_db_path()
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                # 创建表
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS digest_turns (
                        session_id TEXT PRIMARY KEY,
                        turn_count INTEGER DEFAULT 0,
                        last_updated REAL,
                        created_at REAL
                    )
                ''')
                # 创建索引
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_digest_turns_session ON digest_turns(session_id)')
                
                # 加载现有计数器
                cursor.execute('SELECT session_id, turn_count FROM digest_turns')
                for session_id, turn_count in cursor.fetchall():
                    self._turn_counters[session_id] = turn_count
                    logger.info(f"[SessionSummary] 加载持久化计数器 session={session_id} turn={turn_count}")
                
                conn.commit()
        except Exception as e:
            logger.warning(f"[SessionSummary] 初始化持久化计数器失败: {e}")

    def _get_db_path(self) -> str:
        """获取数据库文件路径"""
        try:
            from pathlib import Path
            plugin_dir = Path(__file__).parent.parent.parent
            return str(plugin_dir / "digest_turns.db")
        except Exception:
            return "digest_turns.db"

    def _save_counter(self, session_id: str, turn_count: int) -> None:
        """保存计数器到数据库"""
        try:
            db_path = self._get_db_path()
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO digest_turns (session_id, turn_count, last_updated, created_at)
                    VALUES (?, ?, ?, ?)
                ''', (session_id, turn_count, time.time(), time.time()))
                conn.commit()
        except Exception as e:
            logger.warning(f"[SessionSummary] 保存计数器失败 session={session_id}: {e}")

    def touch(self, session_id: str) -> None:
        """记录 session 的活跃时间（每次有消息时调用）"""
        self._last_activity[session_id] = time.time()
        # 如果之前已生成摘要但有新消息了，清除标记
        self._summarized_sessions.discard(session_id)

    def increment_turn(self, session_id: str) -> bool:
        """v5.5: 递增 session 的对话轮次计数器。

        每次 user+assistant 一轮对话调用一次。
        返回 True 表示到达 digest 触发点。
        """
        # 获取当前计数（从内存或数据库）
        current_count = self._turn_counters.get(session_id, 0)
        new_count = current_count + 1
        self._turn_counters[session_id] = new_count
        
        # 保存到数据库
        self._save_counter(session_id, new_count)

        # 检查是否到达间隔且未生成过
        if new_count % _DIGEST_TURN_INTERVAL == 0:
            generated = self._digest_generated_turns.setdefault(session_id, set())
            if new_count not in generated:
                generated.add(new_count)
                logger.info(f"[SessionSummary] Digest 触发 session={session_id} turn={new_count}")
                return True
        return False

    async def maybe_generate_digest(self, session_id: str) -> None:
        """v5.5: 检查是否需要生成对话叙事摘要，如需要则生成。"""
        should_generate = self.increment_turn(session_id)
        if not should_generate:
            return

        try:
            await self.generate_digest(session_id)
        except Exception as e:
            logger.warning(
                f"[SessionSummary] v5.5 digest 生成失败 "
                f"session={session_id}: {e}"
            )

    async def generate_digest(self, session_id: str) -> dict[str, Any] | None:
        """v5.5: 生成对话叙事摘要（Conversation Digest）

        与空闲触发的 generate_summary 不同：
        - 每 10 轮自动触发，不等空闲
        - 生成叙事性文本（非结构化 JSON）
        - 作为 document 存入 + 跑原子提取
        - 与空闲摘要共存
        """
        # 1. 获取最近的消息（专门为Digest优化，最近20条≈10轮）
        messages = await self._get_messages_for_digest(session_id)
        if len(messages) < 4:
            logger.debug(
                f"[SessionSummary] v5.5 digest 跳过：消息太少 "
                f"({len(messages)} < 4) session={session_id}"
            )
            return None

        # 取最近的消息窗口
        first_time = _to_epoch_ts(messages[0].get("timestamp", time.time()))
        last_time = messages[-1].get("timestamp", time.time())

        # 2. 拼接对话文本
        conversation_text = self._format_conversation(messages)
        if not conversation_text.strip():
            return None

        # 3. 调用 LLM 生成叙事性摘要
        narrative = await self._call_llm_for_digest(conversation_text)
        if not narrative:
            # fallback: 简单拼接
            narrative = self._fallback_digest(messages)

        # 4. 作为 document 存入 LivingMemory
        try:
            if self.memory_engine and hasattr(self.memory_engine, "hybrid_retriever"):
                metadata = {
                    "session_id": session_id,
                    "importance": 0.6,
                    "create_time": time.time(),
                    "last_access_time": time.time(),
                    # P0-3 Provenance: digest 内容=夫妻对话摘要, 归 internal; 链路特征由 summary_schema_version/digest_turn 扛
                    "source": "internal",
                    "digest_turn": self._turn_counters.get(session_id, 0),
                    "message_count": len(messages),
                    "time_span": f"{first_time:.0f}-{last_time:.0f}",
                    "topics": [],
                    "key_facts": [],
                    "sentiment": "neutral",
                    "interaction_type": "chat",
                    "canonical_summary": narrative[:100],
                    "persona_summary": "",
                    "summary_schema_version": "v55_digest",
                    "summary_quality": "auto",
                    "source_window": f"last_{len(messages)}",
                    "access_count": 0,
                }
                doc_id = await self.memory_engine.hybrid_retriever.add_memory(
                    content=narrative, metadata=metadata
                )
                logger.info(
                    f"[SessionSummary] v5.5 digest 已存入 document "
                    f"doc_id={doc_id} session={session_id} "
                    f"turn={self._turn_counters.get(session_id, 0)}"
                )
        except Exception as e:
            logger.warning(f"[SessionSummary] v5.5 digest 存储失败: {e}")

        # 4.5: 同时存入 memory_atoms，让 WebUI 能显示（summary/list API 源数据）
        try:
            duration_minutes = (last_time - first_time) / 60
            summary_data = {
                "session_id": session_id,
                "summary_type": "digest",
                "message_count": len(messages),
                "duration_minutes": duration_minutes,
                "topics": [],
                "emotion": "neutral",
                "continuation_points": [],
                "start_time": first_time,
                "end_time": last_time,
                "key_entities": [],
                "brief": narrative,
                "summary_schema_version": "v55_digest",
                "digest_turn": self._turn_counters.get(session_id, 0),
                "message_ids": [m.get("msg_id") for m in messages if m.get("msg_id")],
            }
            await self._store_summary(summary_data)
            logger.info(
                f"[SessionSummary] v5.5 digest 已同步写入 memory_atoms "
                f"session={session_id} turn={summary_data['digest_turn']}"
            )
        except Exception as e:
            logger.warning(f"[SessionSummary] v5.5 digest memory_atoms 同步失败: {e}")

        # 5. 跑原子提取
        try:
            from .processors.stream_extractor import extract_from_message
            from .models.memory_atom import MemoryAtom, AtomType, DecayType, compute_ttl

            _STREAM_TYPE_MAP = {
                "preference": AtomType.PREFERENCE,
                "fact": AtomType.FACTUAL,
                "plan": AtomType.PLANNED,
                "reminder": AtomType.PLANNED,
                "relationship": AtomType.RELATIONAL,
            }

            atoms = extract_from_message(
                content=narrative, role="user", session_id=session_id
            )
            if atoms and self.memory_engine and hasattr(self.memory_engine, "atom_store"):
                mem_atoms = []
                for sa in atoms:
                    mapped_type = _STREAM_TYPE_MAP.get(sa.atom_type.value, AtomType.UNKNOWN)
                    ttl, decay = compute_ttl(mapped_type, sa.importance)
                    mem_atom = MemoryAtom(
                        parent_memory_id=0,
                        atom_type=mapped_type,
                        content=sa.content,
                        entities=sa.entities,
                        importance=sa.importance,
                        confidence=sa.confidence,
                        ttl_days=ttl,
                        decay_type=decay,
                        event_time=first_time,
                        session_id=session_id,
                        metadata={**sa.metadata, "source": "digest_atom_extract"},
                    )
                    mem_atoms.append(mem_atom)
                if mem_atoms:
                    await self.memory_engine.atom_store.insert_many(mem_atoms)
                    logger.info(
                        f"[SessionSummary] v5.5 digest 原子提取: "
                        f"{len(mem_atoms)} 个原子 session={session_id}"
                    )
        except Exception as e:
            logger.debug(f"[SessionSummary] v5.5 digest 原子提取失败(非致命): {e}")

        return {
            "narrative": narrative,
            "turn": self._turn_counters.get(session_id, 0),
            "session_id": session_id,
        }

    async def _call_llm_for_digest(self, conversation_text: str) -> str | None:
        """v5.5: 调用 LLM 生成叙事性摘要文本"""
        try:
            provider = self._get_provider()
            if not provider:
                return None

            prompt = _DIGEST_NARRATIVE_PROMPT + conversation_text

            from astrbot.api.provider import ProviderRequest
            req = ProviderRequest(
                system_prompt="你是一个叙事摘要助手。请用流畅的叙事语言生成对话摘要。",
                prompt=prompt,
            )

            resp = await provider.text_chat(**req.__dict__)
            if not resp:
                return None

            if isinstance(resp, str):
                text = resp
            else:
                # Extract text from LLMResponse object
                try:
                    text = resp.result_chain.chain[0].text if resp.result_chain and resp.result_chain.chain else ""
                except Exception:
                    text = str(resp)
            text = text.strip()
            # 去除可能的 markdown 标记
            if text.startswith("```"):
                nl = chr(10)
                lines = text.split(nl, 1)
                text = lines[-1] if len(lines) > 1 else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

            return text if len(text) >= 10 else None
        except Exception as e:
            logger.warning(f"[SessionSummary] v5.5 digest LLM失败: {e}")
            return None

    def _fallback_digest(self, messages: list[dict]) -> str:
        """v5.5: LLM 不可用时的简单叙事摘要"""
        from datetime import datetime

        user_msgs = [m for m in messages if m["role"] == "user"]
        first_ts = messages[0].get("timestamp", time.time())
        time_str = datetime.fromtimestamp(first_ts).strftime("%m月%d日 %H:%M")

        topics = []
        for msg in user_msgs[:5]:
            snippet = msg["content"][:30].replace(chr(10), " ")
            if snippet and snippet not in topics:
                topics.append(snippet)

        return f"在{time_str}的对话中，橘子聊了关于{'、'.join(topics[:3])}等话题，共{len(messages)}条消息。"

    # ── 后台空闲检测 ──────────────────────────────────

    async def _idle_check_loop(self) -> None:
        """每分钟检查一次是否有 session 空闲超时"""
        while True:
            try:
                await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
                now = time.time()
                expired_sessions: list[str] = []

                for session_id, last_time in list(self._last_activity.items()):
                    if session_id in self._summarized_sessions:
                        continue
                    if now - last_time >= _IDLE_TIMEOUT_SECONDS:
                        expired_sessions.append(session_id)

                for session_id in expired_sessions:
                    try:
                        await self.generate_summary(session_id)
                    except Exception as e:
                        logger.warning(
                            f"[SessionSummary] 生成摘要失败 "
                            f"session={session_id}: {e}"
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SessionSummary] 空闲检测异常: {e}", exc_info=True)
                await asyncio.sleep(10)

    # ── 摘要生成 ──────────────────────────────────────

    async def generate_summary(self, session_id: str) -> dict[str, Any] | None:
        """
        为指定 session 生成会话摘要

        Returns:
            摘要 dict 或 None（消息太少时跳过）
        """
        # 1. 获取本次会话的消息
        messages = await self._get_session_messages(session_id)
        if len(messages) < _MIN_MESSAGES_FOR_SUMMARY:
            logger.debug(
                f"[SessionSummary] session={session_id} 消息数 "
                f"{len(messages)} < {_MIN_MESSAGES_FOR_SUMMARY}，跳过"
            )
            self._summarized_sessions.add(session_id)
            return None

        # 2. 计算会话时间跨度
        first_time = _to_epoch_ts(messages[0].get("timestamp", time.time()))
        last_time = messages[-1].get("timestamp", time.time())
        duration_minutes = max(1, int((last_time - first_time) / 60))

        # 3. 拼接对话文本
        conversation_text = self._format_conversation(messages)
        if not conversation_text.strip():
            self._summarized_sessions.add(session_id)
            return None

        # 4. 调用 LLM 生成摘要
        summary_data = await self._call_llm_for_summary(conversation_text)
        if not summary_data:
            # LLM 失败，用简单规则生成
            summary_data = self._fallback_summary(messages)

        # 5. 补充元数据
        summary_data["session_id"] = session_id
        summary_data["message_count"] = len(messages)
        summary_data["duration_minutes"] = duration_minutes
        summary_data["start_time"] = first_time
        summary_data["end_time"] = last_time
        summary_data["summary_type"] = "auto"
        # v9: 附上源消息 ID，构建溯源链 L2 → L1
        summary_data["message_ids"] = [
            m["msg_id"] for m in messages if m.get("msg_id")
        ]

        # 6. 写入 memory_atoms
        await self._store_summary(summary_data)

        self._summarized_sessions.add(session_id)
        logger.info(
            f"[SessionSummary] 已生成摘要 session={session_id} "
            f"topics={summary_data.get('topics', [])} "
            f"emotion={summary_data.get('emotion', 'neutral')}"
        )
        return summary_data

    async def _get_messages_for_digest(self, session_id: str) -> list[dict]:
        """获取对话Digest的专门消息 - 最近20条（10轮）"""
        try:
            # 获取最近40条消息（20轮对话）
            messages = await self.conversation_manager.get_messages(
                session_id, limit=40
            )
            result = []
            for msg in messages:
                content = getattr(msg, "content", "") or ""
                role = getattr(msg, "role", "unknown")
                ts = getattr(msg, "timestamp", time.time())
                if content.strip():
                    result.append({
                        "role": role,
                        "content": content.strip(),
                        "timestamp": ts if isinstance(ts, (int, float)) else time.time(),
                        "msg_id": getattr(msg, "id", 0),
                    })
            # 按时间正序排列
            result.sort(key=lambda x: x["timestamp"])
            # 只取最近20条（10轮）
            return result[-20:]
        except Exception as e:
            logger.warning(f"[SessionSummary] 获取Digest消息失败: {e}")
            return []

    async def _get_session_messages(self, session_id: str) -> list[dict]:
        """获取 session 的对话消息"""
        try:
            messages = await self.conversation_manager.get_messages(
                session_id, limit=100
            )
            result = []
            for msg in messages:
                content = getattr(msg, "content", "") or ""
                role = getattr(msg, "role", "unknown")
                ts = getattr(msg, "timestamp", time.time())
                if content.strip():
                    result.append({
                        "role": role,
                        "content": content.strip(),
                        "timestamp": ts if isinstance(ts, (int, float)) else time.time(),
                        "msg_id": getattr(msg, "id", 0),
                    })
            # 按时间正序排列
            result.sort(key=lambda x: x["timestamp"])
            return result
        except Exception as e:
            logger.warning(f"[SessionSummary] 获取消息失败: {e}")
            return []

    async def _get_recent_messages(self, session_id: str, limit: int = 20) -> list[dict]:
        """获取最近的消息（用于Digest）"""
        try:
            messages = await self.conversation_manager.get_messages(
                session_id, limit=limit * 2  # 获取更多，然后过滤
            )
            result = []
            for msg in messages:
                content = getattr(msg, "content", "") or ""
                role = getattr(msg, "role", "unknown")
                ts = getattr(msg, "timestamp", time.time())
                if content.strip():
                    result.append({
                        "role": role,
                        "content": content.strip(),
                        "timestamp": ts if isinstance(ts, (int, float)) else time.time(),
                        "msg_id": getattr(msg, "id", 0),
                    })
            # 按时间正序排列
            result.sort(key=lambda x: x["timestamp"])
            # 只取最近的limit条
            return result[-limit:]
        except Exception as e:
            logger.warning(f"[SessionSummary] 获取最近消息限制失败: {e}")
            return []

    def _format_conversation(self, messages: list[dict]) -> str:
        """将消息列表格式化为文本"""
        lines = []
        for msg in messages[-50:]:  # 最多取最近50条
            role = msg["role"]
            if role == "user":
                prefix = "橘子"
            elif role == "assistant":
                prefix = "春雪"
            else:
                prefix = role
            content = msg["content"][:500]  # 每条最多500字
            lines.append(f"{prefix}: {content}")
        return "\n".join(lines)

    async def _call_llm_for_summary(self, conversation_text: str) -> dict | None:
        """调用 LLM 生成结构化摘要"""
        try:
            provider = self._get_provider()
            if not provider:
                return None

            prompt = _SUMMARY_PROMPT + conversation_text

            from astrbot.api.provider import ProviderRequest
            req = ProviderRequest(
                system_prompt="你是一个对话摘要助手。请严格按JSON格式输出。",
                prompt=prompt,
            )

            resp = await provider.text_chat(**req.__dict__)
            if not resp:
                return None

            # 尝试解析 JSON
            if isinstance(resp, str):
                text = resp
            else:
                try:
                    text = resp.result_chain.chain[0].text if resp.result_chain and resp.result_chain.chain else ""
                except Exception:
                    text = str(resp)
            # 去除可能的 markdown 代码块标记
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

            # 找到第一个 { 和最后一个 }
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]

            data = json.loads(text)

            # 验证字段
            return {
                "topics": data.get("topics", [])[:5],
                "decisions": data.get("decisions", [])[:5],
                "emotion": data.get("emotion", "neutral"),
                "continuation_points": data.get("continuation_points", [])[:5],
                "key_entities": data.get("key_entities", [])[:10],
                "brief": data.get("brief", ""),
            }
        except Exception as e:
            logger.warning(f"[SessionSummary] LLM摘要生成失败: {e}")
            return None

    def _get_provider(self):
        """获取可用的 LLM provider"""
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    def _fallback_summary(self, messages: list[dict]) -> dict:
        """LLM 不可用时的简单规则摘要"""
        user_msgs = [m for m in messages if m["role"] == "user"]
        topics = []
        for msg in user_msgs[:5]:
            # 取每条用户消息的前20字作为话题
            topic = msg["content"][:20].replace("\n", " ")
            if topic and topic not in topics:
                topics.append(topic)

        return {
            "topics": topics[:5],
            "decisions": [],
            "emotion": "neutral",
            "continuation_points": [],
            "key_entities": [],
            "brief": f"本次会话共{len(messages)}条消息",
        }

    async def _store_summary(self, summary_data: dict) -> None:
        """将摘要写入 memory_atoms (v2.5: 加入 provenance 链)"""
        try:
            # 找一个 parent_memory_id（用最近一条记忆的 id）
            parent_id = await self._get_parent_id()

            now = time.time()
            ttl_days = 30.0  # 30天后自动过期

            # v2.5: 查找上一个会话摘要的 atom_id，构建 provenance 链
            prev_summary_id = await self._get_prev_summary_id(
                summary_data["session_id"]
            )

            # 格式化摘要文本
            content = self._format_summary_content(summary_data)

            metadata = {
                "session_id": summary_data["session_id"],
                "summary_type": summary_data["summary_type"],
                "message_count": summary_data["message_count"],
                "duration_minutes": summary_data["duration_minutes"],
                "topics": summary_data["topics"],
                "emotion": summary_data["emotion"],
                "continuation_points": summary_data["continuation_points"],
                "start_time": summary_data["start_time"],
                "end_time": summary_data["end_time"],
                # v2.5: provenance 链
                "prev_summary_id": prev_summary_id,
                "thread_seq": 0,  # will be filled after chain lookup
                # v5.5: Digest 字段（记录摘要版本与触发轮次）
                "summary_schema_version": summary_data.get("summary_schema_version", ""),
                "digest_turn": summary_data.get("digest_turn"),
            }

            # v2.5: 计算 thread_seq（链表位置）
            if prev_summary_id:
                prev_meta = await self._get_atom_metadata(prev_summary_id)
                if prev_meta and "thread_seq" in prev_meta:
                    metadata["thread_seq"] = int(prev_meta.get("thread_seq", 0)) + 1

            # v9: source_ids 溯源 L2 → L1 message ids
            source_ids = summary_data.get("message_ids", [])

            # 刀⑥写入端打档：会话摘要（有 session，按会话判档）
            _ws_summary = None
            try:
                from .privacy_filter import resolve_write_scope_for_session
                _ws_summary = resolve_write_scope_for_session(
                    summary_data["session_id"],
                    content,
                    bool(self.config_manager.get("privacy.enabled", False)),
                    sensitive_words=self.config_manager.get("privacy.sensitive_words", None),
                    owner_whitelist=[s.strip() for s in str(self.config_manager.get("privacy.owner_whitelist", "")).split(",") if s.strip()],
                    intimate_sessions=[s.strip() for s in str(self.config_manager.get("privacy.intimate_sessions", "")).split(",") if s.strip()],
                )
            except Exception:
                _ws_summary = None  # 打档链异常=不打档，绝不阻断写入

            atom = MemoryAtom(
                parent_memory_id=parent_id,
                atom_type=AtomType.EPISODIC,
                content=content,
                entities=summary_data.get("key_entities", []),
                importance=0.7,
                confidence=0.85,
                created_at=now,
                last_accessed_at=now,
                event_time=_to_epoch_ts(summary_data.get("start_time"), default=now),
                ttl_days=ttl_days,
                expires_at=now + ttl_days * 86400,
                status=AtomStatus.ACTIVE,
                decay_type=DecayType.LINEAR,
                session_id=summary_data["session_id"],
                metadata={
                    "atom_subtype": "session_summary",
                    **({"privacy_scope": _ws_summary} if _ws_summary else {}),
                    **metadata,
                },
                tier=2,
                source_ids=source_ids,
            )

            # 通过 memory_engine 存储
            if self.memory_engine and hasattr(self.memory_engine, "atom_store"):
                ids = await self.memory_engine.atom_store.insert_many([atom])
                new_id = ids[0] if ids else None
                logger.info(
                    f"[SessionSummary] 摘要已存储 atom_id={new_id} "
                    f"prev={prev_summary_id} seq={metadata['thread_seq']}"
                )
            else:
                logger.warning("[SessionSummary] memory_engine 无 atom_store，跳过存储")

        except Exception as e:
            logger.error(f"[SessionSummary] 存储摘要失败: {e}", exc_info=True)

    async def _get_prev_summary_id(self, current_session_id: str) -> int | None:
        """v2.5: 获取上一个会话摘要的 atom_id，用于构建 provenance 链"""
        try:
            if not self.memory_engine or not hasattr(self.memory_engine, "atom_store"):
                return None

            async with self.memory_engine.atom_store._connect() as db:
                cursor = await db.execute(
                    """
                    SELECT id FROM memory_atoms
                    WHERE json_extract(metadata, '$.atom_subtype') = 'session_summary'
                      AND status = 'active'
                      AND session_id != ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (current_session_id,),
                )
                row = await cursor.fetchone()
                if row:
                    return row[0] if isinstance(row, tuple) else row["id"]
            return None
        except Exception:
            return None

    async def _get_atom_metadata(self, atom_id: int) -> dict | None:
        """v2.5: 获取指定 atom 的 metadata"""
        try:
            if not self.memory_engine or not hasattr(self.memory_engine, "atom_store"):
                return None

            async with self.memory_engine.atom_store._connect() as db:
                cursor = await db.execute(
                    "SELECT metadata FROM memory_atoms WHERE id = ?",
                    (atom_id,),
                )
                row = await cursor.fetchone()
                if row:
                    meta_raw = row[0] if isinstance(row, tuple) else row["metadata"]
                    if isinstance(meta_raw, str):
                        return json.loads(meta_raw)
                    return meta_raw
            return None
        except Exception:
            return None

    async def _get_parent_id(self) -> int:
        """获取一个有效的 parent_memory_id"""
        try:
            if self.memory_engine and hasattr(self.memory_engine, "atom_store"):
                async with self.memory_engine.atom_store._connect() as db:
                    cursor = await db.execute(
                        "SELECT id FROM memory_atoms ORDER BY id DESC LIMIT 1"
                    )
                    row = await cursor.fetchone()
                    if row:
                        return row[0]
        except Exception:
            pass
        return 1  # fallback

    def _format_summary_content(self, data: dict) -> str:
        """格式化摘要的文本内容"""
        from datetime import datetime

        start_ts = _to_epoch_ts(data.get("start_time", time.time()))
        end_ts = data.get("end_time", time.time())

        start_str = datetime.fromtimestamp(start_ts).strftime("%m-%d %H:%M")
        end_str = datetime.fromtimestamp(end_ts).strftime("%H:%M")

        topics = "、".join(data.get("topics", []))
        decisions = data.get("decisions", [])
        emotion = data.get("emotion", "neutral")
        continuation = data.get("continuation_points", [])
        brief = data.get("brief", "")
        msg_count = data.get("message_count", 0)
        duration = data.get("duration_minutes", 0)

        emotion_map = {
            "happy": "开心",
            "excited": "兴奋",
            "calm": "平静",
            "tired": "疲惫",
            "frustrated": "有些郁闷",
            "neutral": "平常",
        }
        emotion_cn = emotion_map.get(emotion, "平常")

        lines = [
            f"【会话摘要】{start_str}~{end_str} | {msg_count}条消息 | {duration}分钟",
            f"话题：{topics}",
        ]
        if brief:
            lines.append(f"概述：{brief}")
        if decisions:
            lines.append("决策：" + "；".join(decisions))
        lines.append(f"情感：{emotion_cn}")
        if continuation:
            lines.append("待续：" + "；".join(continuation))

        return "\n".join(lines)

    # ── 摘要注入 ──────────────────────────────────────

    async def get_last_summary(self, session_id: str) -> str | None:
        """
        获取上一个 session 的摘要，用于注入到新会话的 system prompt

        v2.5: 沿 provenance 链回溯，最多返回最近2条摘要的精简上下文。
        """
        try:
            if not self.memory_engine or not hasattr(self.memory_engine, "atom_store"):
                return None

            async with self.memory_engine.atom_store._connect() as db:
                # 查找最近的会话摘要（排除当前 session）
                cursor = await db.execute(
                    """
                    SELECT id, content, metadata, created_at
                    FROM memory_atoms
                    WHERE json_extract(metadata, '$.atom_subtype') = 'session_summary'
                      AND status = 'active'
                      AND session_id != ?
                    ORDER BY created_at DESC
                    LIMIT 3
                    """,
                    (session_id,),
                )
                rows = await cursor.fetchall()

                if not rows:
                    return None

                row = rows[0]
                content = row[0] if isinstance(row, tuple) else row["content"]
                created_at = row[2] if isinstance(row, tuple) else row["created_at"]
                meta_raw = row[1] if isinstance(row, tuple) else row["metadata"]

                # 解析 metadata
                try:
                    if isinstance(meta_raw, str):
                        meta = json.loads(meta_raw)
                    else:
                        meta = meta_raw or {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}

                now = time.time()
                # 兼容 created_at 可能是字符串时间的情况
                if isinstance(created_at, str):
                    try:
                        created_at = datetime.fromisoformat(created_at).timestamp()
                    except (ValueError, TypeError):
                        created_at = now
                elif not isinstance(created_at, (int, float)):
                    created_at = now
                age_days = (now - created_at) / 86400

                # 衰减策略
                main_summary: str | None = None
                if age_days <= 3:
                    main_summary = content
                elif age_days <= 7:
                    # 3-7天：只注入话题+待续点
                    topics = meta.get("topics", [])
                    continuation = meta.get("continuation_points", [])
                    emotion = meta.get("emotion", "neutral")
                    if topics or continuation:
                        parts = [f"【上次摘要·精简】话题：{'、'.join(topics)}"]
                        if emotion != "neutral":
                            emotion_map = {
                                "happy": "开心", "excited": "兴奋",
                                "calm": "平静", "tired": "疲惫",
                                "frustrated": "有些郁闷",
                            }
                            parts.append(f"心情：{emotion_map.get(emotion, emotion)}")
                        if continuation:
                            parts.append("待续：" + "；".join(continuation))
                        return "\n".join(parts)
                else:
                    return None

                if not main_summary:
                    return None

                # v2.5: provenance chain
                prev_id = meta.get("prev_summary_id")
                if prev_id and age_days <= 3:
                    chain_ctx = await self._get_chain_context(int(prev_id))
                    if chain_ctx:
                        main_summary = main_summary + "\n" + chain_ctx

                return main_summary

        except Exception as e:
            logger.warning(f"[SessionSummary] 获取上次摘要失败: {e}")
            return None

    async def _get_chain_context(self, prev_summary_id: int, depth: int = 0) -> str | None:
        """v2.5: provenance chain traversal. Max depth = 2."""
        if depth >= 2:
            return None

        try:
            if not self.memory_engine or not hasattr(self.memory_engine, "atom_store"):
                return None

            async with self.memory_engine.atom_store._connect() as db:
                cursor = await db.execute(
                    "SELECT content, metadata, created_at FROM memory_atoms WHERE id = ? AND status = 'active'",
                    (prev_summary_id,),
                )
                row = await cursor.fetchone()
                if not row:
                    return None

                content = row[0] if isinstance(row, tuple) else row["content"]
                meta_raw = row[1] if isinstance(row, tuple) else row["metadata"]

                try:
                    meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
                except (json.JSONDecodeError, TypeError):
                    meta = {}

                topics = meta.get("topics", [])
                if not topics:
                    return None

                first_line = content.split("\n")[0] if content else ""
                chain_text = f"【更早·{first_line[:30]}】话题：{'、'.join(topics)}"

                next_prev = meta.get("prev_summary_id")
                if next_prev and depth < 1:
                    deeper = await self._get_chain_context(int(next_prev), depth + 1)
                    if deeper:
                        chain_text = chain_text + "\n" + deeper

                return chain_text

        except Exception as e:
            logger.debug(f"[SessionSummary] chain traversal failed depth={depth}: {e}")
            return None

    async def get_continuation_points(self, session_id: str) -> list[str]:
        """获取最近的待续事项"""
        try:
            if not self.memory_engine or not hasattr(self.memory_engine, "atom_store"):
                return []

            async with self.memory_engine.atom_store._connect() as db:
                cursor = await db.execute(
                    """
                    SELECT metadata, created_at
                    FROM memory_atoms
                    WHERE json_extract(metadata, '$.atom_subtype') = 'session_summary'
                      AND status = 'active'
                      AND session_id != ?
                    ORDER BY created_at DESC
                    LIMIT 3
                    """,
                    (session_id,),
                )
                rows = await cursor.fetchall()

                points = []
                now = time.time()
                for row in rows:
                    meta_raw = row[0] if isinstance(row, tuple) else row["metadata"]
                    created_at = row[1] if isinstance(row, tuple) else row["created_at"]
                    # 兼容 created_at 可能是字符串时间的情况
                    if isinstance(created_at, str):
                        try:
                            created_at = datetime.fromisoformat(created_at).timestamp()
                        except (ValueError, TypeError):
                            created_at = now
                    elif not isinstance(created_at, (int, float)):
                        created_at = now
                    age_days = (now - created_at) / 86400
                    if age_days > 7:
                        continue
                    try:
                        meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                    for p in meta.get("continuation_points", []):
                        if p and p not in points:
                            points.append(p)
                return points[:5]
        except Exception as e:
            logger.warning(f"[SessionSummary] 获取待续事项失败: {e}")
            return []


    async def generate_conversation_digest(self, session_id: str) -> dict[str, Any] | None:
        """
        v5.5: 生成对话叙事摘要（Conversation Digest）
        每10轮对话自动触发，生成叙事性摘要
        """
        try:
            # 1. 获取最近的消息（最近20条，10轮对话）
            messages = await self._get_recent_messages(session_id, limit=20)
            if len(messages) < 10:  # 至少5轮对话
                return None

            # 2. 格式化对话内容
            conversation_text = self._format_conversation(messages)
            if not conversation_text.strip():
                return None

            # 3. 调用 LLM 生成叙事摘要
            summary_data = await self._call_llm_for_summary(conversation_text, is_digest=True)
            if not summary_data:
                # LLM 失败，用简单规则生成
                summary_data = self._fallback_summary(messages)

            # 4. 补充元数据
            summary_data["session_id"] = session_id
            summary_data["message_count"] = len(messages)
            summary_data["summary_type"] = "digest"
            summary_data["narrative"] = summary_data.get("brief", "")

            # 5. 写入 memory_atoms
            await self._store_summary(summary_data)

            self._summarized_sessions.add(session_id)
            logger.info(
                f"[SessionSummary] 已生成对话叙事摘要 session={session_id} "
                f"topics={summary_data.get('topics', [])} "
                f"emotion={summary_data.get('emotion', 'neutral')}"
            )
            return summary_data

        except Exception as e:
            logger.error(f"[SessionSummary] 生成对话叙事摘要失败: {e}", exc_info=True)
            return None

def _to_epoch_ts(v, default=None):
    """归一化时间戳: int/float直接用; epoch字符串转float; datetime字符串parse成epoch; 其余回退default/now。"""
    import datetime as _dt
    import time as _t
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        try:
            return float(s)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                return _dt.datetime.strptime(s, fmt).timestamp()
            except ValueError:
                continue
    if default is not None:
        return float(default)
    return _t.time()


