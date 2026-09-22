"""供 Agent 主动调用的长期记忆写入工具。"""

import asyncio
import json
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.platform import MessageType
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..utils import get_persona_id


def _json_result(data: dict[str, Any]) -> str:
    """将工具结果稳定序列化为 JSON 文本。"""
    return json.dumps(data, ensure_ascii=False, default=str)


def _normalize_list(value: Any, limit: int = 5) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()][:limit]
    if isinstance(value, str) and value.strip():
        return [value.strip()][:limit]
    return []


@dataclass
class MemoryMemorizeTool(FunctionTool[AstrAgentContext]):
    """长期记忆主动写入工具。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    memory_engine: Any = None
    memory_processor: Any = None

    name: str = "memorize_long_term_memory"
    description: str = (
        "Memorize durable long-term memory when the user explicitly asks to remember something, "
        "or when stable preferences, identity details, agreements, or project context appear. "
        "Write concise factual memory, not the full conversation."
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "memory": {
                    "type": "string",
                    "description": "Concise factual long-term memory to save. Do not copy the full conversation.",
                },
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional short topic tags for this memory, up to 5.",
                    "default": [],
                },
                "key_facts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional key facts supporting the memory, up to 5.",
                    "default": [],
                },
                "sentiment": {
                    "type": "string",
                    "description": "Sentiment of the memory: positive, neutral, or negative.",
                    "default": "neutral",
                },
                "importance": {
                    "type": "number",
                    "description": "Importance from 0.0 to 1.0. Use higher values for durable preferences, commitments, or identity facts.",
                    "default": 0.7,
                },
                "reason": {
                    "type": "string",
                    "description": "Optional short reason why this information should be remembered.",
                    "default": "",
                },
                "source": {
                    "type": "string",
                    "enum": ["internal", "external"],
                    "description": "Provenance: internal = couple chat/agent insight; external = web fetch, search results, or tool-returned content. External memories rank lower in retrieval.",
                    "default": "internal",
                },
            },
            "required": ["memory"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        memory: str,
        topics: list[str] | None = None,
        key_facts: list[str] | None = None,
        sentiment: str = "neutral",
        importance: float = 0.7,
        reason: str = "",
        source: str = "internal",
    ) -> ToolExecResult:
        """执行长期记忆写入。"""
        # === 热重载自愈：动态获取最新插件实例（旧工具对象也能拿到新连接）===
        try:
            from ..passive_group_capture import get_active_plugin

            _plugin = get_active_plugin()
            if _plugin is not None:
                _init = getattr(_plugin, "initializer", None)
                if _init is not None:
                    _me = getattr(_init, "memory_engine", None)
                    _mp = getattr(_init, "memory_processor", None)
                    _ctx = getattr(_init, "context", None)
                    if _me is not None:
                        self.memory_engine = _me
                    if _mp is not None:
                        self.memory_processor = _mp
                    if _ctx is not None:
                        self.context = _ctx
        except BaseException:
            pass
        # === DEBUG: 确认 call 被调用 ===
        try:
            with open(r"E:\astrbot\AstrBotLauncher-0.3.0\AstrBotLauncher-0.3.0\AstrBot\data\plugins\astrbot_plugin_livingmemory\memory_call_debug.log", "a", encoding="utf-8") as _f_debug:
                _f_debug.write("ENTERED call()\n")
        except BaseException:
            pass
        cleaned_memory = (memory or "").strip()
        if not cleaned_memory:
            return _json_result({"memorized": False, "error": "memory is empty"})

        normalized_sentiment = str(sentiment or "neutral").strip().lower()
        if normalized_sentiment not in {"positive", "neutral", "negative"}:
            normalized_sentiment = "neutral"

        # P0-1 Provenance: 归一化来源标记（非法值一律回退 internal，保守取向）
        normalized_source = str(source or "internal").strip().lower()
        if normalized_source not in {"internal", "external"}:
            normalized_source = "internal"

        if (
            self.context is None
            or self.memory_engine is None
            or self.memory_processor is None
        ):
            return _json_result(
                {
                    "memorized": False,
                    "error": "memory memorize tool is not initialized",
                }
            )

        logger.debug(f"[memorize_debug] 开始写入记忆: memory={cleaned_memory[:100]!r}")
        try:
            event = context.context.event
            logger.debug(f"[memorize_debug] event={event!r}")
            session_id = event.unified_msg_origin
            logger.debug(f"[memorize_debug] session_id={session_id!r}")
            persona_id = await get_persona_id(self.context, event)
            logger.debug(f"[memorize_debug] persona_id={persona_id!r}")
            is_group_chat = event.get_message_type() == MessageType.GROUP_MESSAGE
            logger.debug(f"[memorize_debug] is_group_chat={is_group_chat!r}")

            structured_data = {
                "summary": cleaned_memory,
                "topics": _normalize_list(topics),
                "key_facts": _normalize_list(key_facts),
                "sentiment": normalized_sentiment,
                "importance": importance,
            }
            logger.debug(f"[memorize_debug] structured_data={structured_data!r}")

            content, metadata, normalized_importance = (
                self.memory_processor.build_memory_from_structured_data(
                    structured_data=structured_data,
                    is_group_chat=is_group_chat,
                    fallback_excerpt=cleaned_memory,
                )
            )
            logger.debug(f"[memorize_debug] content={content!r}, importance={normalized_importance!r}")
            metadata["source_window"] = {
                "session_id": session_id,
                "triggered_by": "agent_tool",
                "tool_name": self.name,
            }
            metadata["memory_origin"] = "agent_memorize_tool"
            metadata["source"] = normalized_source
            cleaned_reason = (reason or "").strip()
            if cleaned_reason:
                metadata["memorize_reason"] = cleaned_reason
            logger.debug(f"[memorize_debug] metadata keys={list(metadata.keys())!r}")

            memory_id = await self.memory_engine.add_memory(
                content=content,
                session_id=session_id,
                persona_id=persona_id,
                importance=normalized_importance,
                metadata=metadata,
            )
            logger.debug(f"[memorize_debug] add_memory 返回: memory_id={memory_id!r}")

            # 豁免登记自动化（橘子 2026-08-20 晨，家规代码化）：
            # 保存成功→顺手把内容喂给安检门指纹表，老婆不用记得手动 exempt。
            # best-effort：失败只 log，绝不影响保存结果。
            try:
                from ..passive_group_capture import get_active_plugin

                _gate = get_active_plugin()._get_security_gate()
                if _gate is not None:
                    texts = [content]
                    facts = key_facts or []
                    if isinstance(facts, list):
                        texts.extend(str(f) for f in facts if str(f).strip())
                    n = _gate.mark_memorized(texts)
                    logger.debug(f"[memorize_debug] 豁免指纹自动登记 {n} 条")
            except Exception as _e:
                logger.debug(f"[memorize_debug] 豁免登记跳过: {_e}")

            return _json_result(
                {
                    "memorized": True,
                    "id": memory_id,
                    "content": content,
                    "source": normalized_source,
                    "importance": normalized_importance,
                    "session_id": session_id,
                    "persona_id": persona_id,
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            import traceback
            exc_type = type(e).__name__
            exc_msg = str(e)
            exc_tb = traceback.format_exc()
            logger.error("记忆工具写入失败: %s | %s", exc_type, exc_msg)
            try:
                debug_log_path = r"E:\astrbot\AstrBotLauncher-0.3.0\AstrBotLauncher-0.3.0\AstrBot\data\plugins\astrbot_plugin_livingmemory\memorize_debug.log"
                with open(debug_log_path, "a", encoding="utf-8") as f:
                    f.write("[memorize_debug] 异常类型: " + exc_type + "\n")
                    f.write("[memorize_debug] 异常信息: " + exc_msg + "\n")
                    f.write("[memorize_debug] traceback:\n" + exc_tb + "\n")
                    f.write("-" * 60 + "\n")
            except BaseException:
                pass
            return _json_result({"memorized": False, "error": "internal_error"})
