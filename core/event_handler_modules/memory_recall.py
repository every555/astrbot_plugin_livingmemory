"""
记忆召回模块
负责长期记忆的检索和注入
v2.5: 新增流式提取 hook，在消息存储后立即提取记忆原子
v5.4: Context 组装显式化 — 全流程 AssemblyTrace 埋点
"""

import asyncio
import time
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart

from ..models.assembly_trace import AssemblyTrace, RouteResultDetail
from ..processors.stream_extractor import extract_from_message
from ..utils import (
    OperationContext,
    format_memories_for_fake_tool_call,
    format_memories_for_injection,
    get_persona_id,
)

if TYPE_CHECKING:
    from ..base.config_manager import ConfigManager
    from ..managers.context_trace_store import ContextTraceStore
    from ..managers.conversation_manager import ConversationManager
    from ..managers.memory_engine import MemoryEngine
    from ..utils.injection_adapter import InjectionAdapter
    from .message_utils import MessageUtils


class MemoryRecall:
    """记忆召回类"""

    def __init__(
        self,
        context,
        config_manager: "ConfigManager",
        memory_engine: "MemoryEngine",
        conversation_manager: "ConversationManager",
        message_utils: "MessageUtils",
        injection_adapter: "InjectionAdapter",
        context_trace_store: "ContextTraceStore | None" = None,
    ):
        """
        初始化记忆召回模块

        Args:
            context: AstrBot上下文
            config_manager: 配置管理器
            memory_engine: 记忆引擎
            conversation_manager: 会话管理器
            message_utils: 消息处理工具
            injection_adapter: 注入适配器
            context_trace_store: v5.4 Context 组装追踪存储
        """
        self.context = context
        self.config_manager = config_manager
        self.memory_engine = memory_engine
        self.conversation_manager = conversation_manager
        self.message_utils = message_utils
        self.injection_adapter = injection_adapter
        self.context_trace_store = context_trace_store

    async def handle_memory_recall(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """Query and inject long-term memory before LLM request"""
        # v5.4: 创建 AssemblyTrace
        trace = AssemblyTrace(session_id=event.unified_msg_origin)
        trace_save = False  # 是否需要保存 trace

        try:
            session_id = event.unified_msg_origin
            logger.debug(f"[DEBUG-Recall] 获取到 unified_msg_origin: {session_id}")

            # 检测异常session_id
            if session_id and (
                "Error:" in session_id or "error:" in session_id.lower()
            ):
                logger.warning(
                    f"[{session_id}] 检测到异常的session_id，这可能导致记忆功能异常。"
                )

            # ── v5.3 会话摘要注入 ──
            await self._inject_session_summary(event, req, session_id, trace)
            # ── P0-B MemGPT 式开场预取：热记忆+画像（6h 冷却）──
            await self._prefetch_warmup(event, req, session_id, trace)

            async with OperationContext("记忆召回", session_id):
                prompt_text = getattr(req, "prompt", "")
                extra_parts = getattr(req, "extra_user_content_parts", [])
                has_prompt_text = isinstance(prompt_text, str) and bool(
                    prompt_text.strip()
                )
                has_extra_parts = bool(extra_parts)

                if not has_prompt_text and not has_extra_parts:
                    logger.debug(f"[{session_id}] 请求中无可用用户内容，跳过记忆召回")
                    return

                normalized = self._normalize_text_only_context_parts(req, session_id)
                if normalized > 0:
                    logger.info(f"[{session_id}] 已归一化 {normalized} 条纯文本历史消息")

                # 自动删除旧的注入记忆
                if self.config_manager.get("recall_engine.auto_remove_injected", True):
                    removed = self._remove_injected_memories_from_context(
                        req, session_id
                    )
                    removed += self._remove_fake_tool_call_from_context(req, session_id)
                    if removed > 0:
                        logger.info(
                            f"[{session_id}] 已清理 {removed} 处历史记忆注入片段"
                        )

                # 先提取用户消息（消息存储和召回都需要）
                actual_query = await self.message_utils.get_event_message_str(event)

                request_query = (
                    prompt_text.strip() if isinstance(prompt_text, str) else ""
                )

                # 存储用户消息（仅私聊），无论是否启用召回都需要
                is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
                if not is_group and actual_query:
                    message_to_store = request_query
                    if not message_to_store:
                        message_to_store = (
                            await self.message_utils.extract_message_content(event, req)
                        )
                    if not message_to_store:
                        message_to_store = actual_query.strip()
                    await self.conversation_manager.add_message_from_event(
                        event=event,
                        role="user",
                        content=message_to_store,
                    )
                    await self.message_utils.enforce_message_limit(session_id)

                    # v2.5: 流式提取 — 消息存储后立即提取记忆原子
                    try:
                        from ..models.memory_atom import MemoryAtom, AtomType, DecayType, compute_ttl
                        from ..processors.stream_extractor import extract_from_message, detect_explicit_memory

                        _STREAM_TYPE_MAP = {
                            "preference": AtomType.PREFERENCE,
                            "fact": AtomType.FACTUAL,
                            "plan": AtomType.PLANNED,
                            "reminder": AtomType.PLANNED,
                            "relationship": AtomType.RELATIONAL,
                        }

                        # v5.5: 显式记忆指令 — 同步写入高置信度(0.9)记忆原子
                        is_explicit, explicit_content = detect_explicit_memory(message_to_store)
                        if is_explicit and hasattr(self, "memory_engine") and self.memory_engine:
                            try:
                                # 刀⑥写入端打档：显式记忆直写 atoms（parent=0 无从继承，构造点打档）
                                _ws_explicit = None
                                try:
                                    from ..privacy_filter import resolve_write_scope_for_session
                                    _ws_explicit = resolve_write_scope_for_session(
                                        session_id,
                                        message_to_store,
                                        bool(self.config_manager.get("privacy.enabled", False)),
                                        sensitive_words=self.config_manager.get("privacy.sensitive_words", None),
                                        owner_whitelist=[s.strip() for s in str(self.config_manager.get("privacy.owner_whitelist", "")).split(",") if s.strip()],
                                        intimate_sessions=[s.strip() for s in str(self.config_manager.get("privacy.intimate_sessions", "")).split(",") if s.strip()],
                                    )
                                except Exception:
                                    _ws_explicit = None  # 打档链异常=不打档，绝不阻断写入
                                explicit_atom = MemoryAtom(
                                    parent_memory_id=0,
                                    atom_type=AtomType.FACTUAL,
                                    content=f"[显式记忆] {explicit_content}",
                                    entities=[],
                                    importance=0.9,
                                    confidence=0.9,
                                    ttl_days=365.0,
                                    expires_at=time.time() + 365.0 * 86400,
                                    decay_type=DecayType.LINEAR,
                                    session_id=session_id,
                                    metadata={
                                        "source": "explicit_memory",
                                        "original_message": message_to_store[:200],
                                        "extraction_method": "pattern_match_v55",
                                        **({"privacy_scope": _ws_explicit} if _ws_explicit else {}),
                                    },
                                )
                                ids = await self.memory_engine.atom_store.insert_many([explicit_atom])
                                logger.info(
                                    f"[{session_id}] v5.5 显式记忆已同步写入 "
                                    f"atom_id={ids[0] if ids else 'N/A'} "
                                    f"confidence=0.9 content={explicit_content[:50]}"
                                )
                                trace.stream_atoms_extracted = trace.stream_atoms_extracted + 1 if hasattr(trace, 'stream_atoms_extracted') else 1
                            except Exception as e_explicit:
                                logger.warning(f"[{session_id}] v5.5 显式记忆写入失败: {e_explicit}")

                        stream_atoms = extract_from_message(
                            content=message_to_store,
                            role="user",
                            session_id=session_id,
                        )
                        if stream_atoms and hasattr(self, "memory_engine") and self.memory_engine:
                            # 刀⑥写入端打档：流式提取直写 atoms（parent=0 无从继承，构造点打档）
                            _ws_stream = None
                            try:
                                from ..privacy_filter import resolve_write_scope_for_session as _rwsfs
                                _ws_stream = _rwsfs(
                                    session_id,
                                    message_to_store,
                                    bool(self.config_manager.get("privacy.enabled", False)),
                                    sensitive_words=self.config_manager.get("privacy.sensitive_words", None),
                                    owner_whitelist=[s.strip() for s in str(self.config_manager.get("privacy.owner_whitelist", "")).split(",") if s.strip()],
                                    intimate_sessions=[s.strip() for s in str(self.config_manager.get("privacy.intimate_sessions", "")).split(",") if s.strip()],
                                )
                                logger.warning(
                                    f"[刀⑥探针-流式] session={session_id[:50]} "
                                    f"enabled_raw={self.config_manager.get('privacy.enabled', 'KEY_MISSING')} "
                                    f"scope={_ws_stream}"
                                )
                            except Exception as _e_probe:
                                logger.warning(
                                    f"[刀⑥探针-流式] 异常 {type(_e_probe).__name__}: {_e_probe}"
                                )
                                _ws_stream = None
                            mem_atoms = []
                            for sa in stream_atoms:
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
                                    session_id=session_id,
                                    metadata={**sa.metadata, "source": "stream_extractor", **({"privacy_scope": _ws_stream} if _ws_stream else {})},
                                )
                                mem_atoms.append(mem_atom)
                            if mem_atoms:
                                await self.memory_engine.atom_store.insert_many(mem_atoms)
                                # v5.4: 记录流式提取数量
                                trace.stream_atoms_extracted = len(mem_atoms)
                                logger.debug(
                                    f"[{session_id}] 流式提取: {len(mem_atoms)} 个原子 "
                                    f"已存入 atom_store"
                                )
                    except Exception as e_stream:
                        logger.debug(f"[{session_id}] 流式提取失败(非致命): {e_stream}")

                # 若 top_k <= 0，跳过记忆检索和注入，但上述清理和消息存储已执行
                top_k = self.config_manager.get("recall_engine.top_k", 5)
                if top_k <= 0:
                    logger.info(
                        f"[{session_id}] top_k={top_k} <= 0，跳过记忆检索和注入"
                    )
                    trace.skipped = True
                    trace.skip_reason = f"top_k={top_k}"
                    trace_save = True
                    return

                if not actual_query:
                    logger.warning(f"[{session_id}] 原始用户消息为空，跳过记忆召回")
                    trace.skipped = True
                    trace.skip_reason = "empty_user_message"
                    trace_save = True
                    return

                # 获取过滤配置
                filtering_config = self.config_manager.filtering_settings
                use_persona_filtering = filtering_config.get(
                    "use_persona_filtering", True
                )
                use_session_filtering = filtering_config.get(
                    "use_session_filtering", True
                )

                # 获取 persona_id，与 AstrBot 主流程保持一致的三级优先级：
                # 1. session_service_config（最高）
                # 2. req.conversation.persona_id（会话级）
                # 3. 全局默认人格（最低）
                # 注意：on_llm_request 钩子在 _ensure_persona_and_skills 之前触发，
                # 因此不能直接依赖 req.system_prompt 已注入人格，需自行走完整优先级。
                persona_id = await get_persona_id(self.context, event)

                recall_session_id = session_id if use_session_filtering else None
                recall_persona_id = persona_id if use_persona_filtering else None

                # 使用原始用户输入作为召回关键字
                query_for_search = actual_query
                trace.query_raw = actual_query[:200]

                # 上下文扩展：拼接最近2轮对话作为查询，提升检索精准度
                if self.config_manager.get(
                    "recall_engine.inject_with_recent_context", False
                ):
                    try:
                        recent_messages = (
                            await self.conversation_manager.get_context(
                                session_id, max_messages=5
                            )
                        )
                        if recent_messages and len(recent_messages) > 1:
                            # recent_messages 按 timestamp DESC 排列（最新在前）
                            # 跳过索引0（当前消息），取后续消息作为扩展上下文
                            context_parts = []
                            for msg in reversed(recent_messages[1:]):
                                content = msg.get("content", "")
                                if content and content.strip():
                                    context_parts.append(content.strip())
                            if context_parts:
                                expanded = " | ".join(context_parts)
                                query_for_search = expanded + " " + actual_query
                                trace.query_expanded = query_for_search[:300]
                                trace.context_expanded_count = len(context_parts)
                                logger.info(
                                    f"[{session_id}] 上下文扩展查询: "
                                    f"{len(context_parts)}条历史消息 + 当前消息"
                                )
                    except Exception as e:
                        logger.warning(f"[{session_id}] 获取上下文扩展失败: {e}")

                # 执行记忆召回
                logger.info(
                    f"[{session_id}] 开始记忆召回，查询='{query_for_search[:80]}...'"
                )

                # 刀⑥ 隐私分档：按触发会话身份解可见档（enabled=False→None→零行为变化；fail-closed 只见 public）
                try:
                    from ..privacy_filter import resolve_scopes_for_origin
                    _vs = resolve_scopes_for_origin(
                        session_id,
                        bool(self.config_manager.get("privacy.enabled", False)),
                        [s.strip() for s in str(self.config_manager.get("privacy.owner_whitelist", "webchat:FriendMessage:webchat!zzz")).split(",") if s.strip()],
                        [s.strip() for s in str(self.config_manager.get("privacy.intimate_sessions", "webchat:FriendMessage:webchat!zzz")).split(",") if s.strip()],
                    )
                except Exception:
                    _vs = None  # 判定链异常：不过滤，绝不阻断召回主链
                recalled_memories = await self.memory_engine.search_memories(
                    query=query_for_search,
                    k=self.config_manager.get("recall_engine.top_k", 5),
                    session_id=recall_session_id,
                    persona_id=recall_persona_id,
                    visible_scopes=_vs,
                )

                if recalled_memories:
                    logger.info(
                        f"[{session_id}] 检索到 {len(recalled_memories)} 条记忆"
                    )
                    trace_save = True

                    # v5.4: 记录检索结果到 trace
                    for mem in recalled_memories:
                        meta = getattr(mem, "metadata", {}) or {}
                        trace.merged_results.append(
                            RouteResultDetail(
                                doc_id=getattr(mem, "doc_id", 0),
                                content_preview=mem.content[:100],
                                final_score=round(mem.final_score, 4),
                                score_breakdown={
                                    "final_score": round(mem.final_score, 4),
                                    "importance": meta.get("importance", 0.5),
                                    "create_time": meta.get("create_time", 0),
                                },
                            )
                        )
                    trace.injected_count = len(recalled_memories)

                    # v5.4: 尝试获取路由权重（从 dual_route_retriever）
                    try:
                        if hasattr(self.memory_engine, "dual_route_retriever"):
                            retriever = self.memory_engine.dual_route_retriever
                            if hasattr(retriever, "config"):
                                gc = retriever.config
                                trace.route_weights = {
                                    "document": getattr(gc, "document_route_weight", 0.65),
                                    "graph": getattr(gc, "graph_route_weight", 0.35),
                                }
                    except Exception:
                        pass

                    # 格式化并注入记忆
                    memory_list = [
                        {
                            "id": getattr(mem, "doc_id", None),
                            "content": mem.content,
                            "score": mem.final_score,
                            "metadata": mem.metadata,
                            "timestamp": mem.metadata.get("create_time"),
                        }
                        for mem in recalled_memories
                    ]

                    # v2.6 P2-14 上下文预算工程：证据闸门 → 冗余去重 → Token 预算装填
                    if self.config_manager.get("recall_engine.context_budget.enabled", True) and memory_list:
                        try:
                            from ..retrieval.context_budget import (
                                apply_evidence_gate,
                                dedup_by_prefix,
                                pack_with_budget,
                            )

                            gated, gate_dropped = apply_evidence_gate(
                                memory_list,
                                min_score=float(self.config_manager.get(
                                    "recall_engine.context_budget.min_score", 0.30)),
                                relative_ratio=float(self.config_manager.get(
                                    "recall_engine.context_budget.relative_ratio", 0.35)),
                            )
                            deduped, dedup_dropped = dedup_by_prefix(gated)
                            budgeted, cb_report = pack_with_budget(
                                deduped,
                                budget_tokens=int(self.config_manager.get(
                                    "recall_engine.context_budget.max_tokens", 1200)),
                            )
                            if gate_dropped or dedup_dropped or cb_report.truncated:
                                logger.info(
                                    f"[{session_id}] [上下文预算] 闸门拦{gate_dropped}条 "
                                    f"去重拦{dedup_dropped}条 截断{cb_report.truncated}条 "
                                    f"→ 注入{cb_report.injected}条 "
                                    f"估算{cb_report.est_tokens}tok/预算{cb_report.budget_tokens}"
                                )
                            if not budgeted:
                                logger.info(
                                    f"[{session_id}] [上下文预算] 证据闸门拦下全部 "
                                    f"{len(memory_list)} 条候选，本轮不注入"
                                )
                            memory_list = budgeted
                        except Exception as e_cb:
                            logger.warning(
                                f"[{session_id}] [上下文预算] 执行失败(降级为全量注入): {e_cb}"
                            )

                    # 输出详细记忆信息
                    for i, mem in enumerate(recalled_memories, 1):
                        logger.debug(
                            f"[{session_id}] 记忆 #{i}: 得分={mem.final_score:.3f}, "
                            f"重要性={mem.metadata.get('importance', 0.5):.2f}, "
                            f"内容={mem.content[:100]}..."
                        )

                    # 根据配置选择注入方式（含 Provider 兼容降级）
                    configured_method = self.config_manager.get(
                        "recall_engine.injection_method", "extra_user_content"
                    )
                    provider = None
                    if configured_method in (
                        "fake_tool_call",
                        "fake_tool_call_deepseek_v4",
                    ):
                        try:
                            provider = self.context.get_using_provider(session_id)
                        except Exception as e:
                            logger.warning(
                                f"[{session_id}] 获取当前 Provider 失败，"
                                f"将按无 Provider 继续解析注入模式: {e}"
                            )
                    injection_method, fallback_reason = (
                        self.injection_adapter.resolve(provider, configured_method)
                    )
                    if fallback_reason:
                        logger.warning(
                            f"[{session_id}] 注入模式从 {configured_method} 降级为 "
                            f"{injection_method}: {fallback_reason}"
                        )
                    # v5.4: 记录注入方式
                    trace.injection_method = injection_method
                    trace.injection_fallback = fallback_reason

                    memory_str = format_memories_for_injection(memory_list)

                    # P1-② 反射弧③：最近已确认记忆矛盾提醒（免疫降级，有才拼）
                    try:
                        _dbp = getattr(self.memory_engine, "db_path", "")
                        if isinstance(_dbp, str) and _dbp:  # Mock/无库环境静默跳过，不发噪音warning
                            import os as _os
                            from ..retrieval.self_check import get_recent_confirmed_alerts
                            _v2db = _os.path.join(_os.path.dirname(_dbp), "v2_memory.db")
                            _alerts = get_recent_confirmed_alerts(_v2db)
                            if _alerts:
                                memory_str += (
                                    "\n\n⚠️[已确认矛盾提醒] 以下矛盾已经裁决确认，引用相关记忆时注意:\n- "
                                    + "\n- ".join(_alerts)
                                )
                    except Exception as _e:
                        logger.warning(f"[{session_id}] 反射弧③提醒失败(忽略): {_e}")

                    # v5.4: 记录注入文本
                    trace.injected_text = memory_str
                    trace.injected_tokens_est = int(len(memory_str) / 1.5)

                    if injection_method == "user_message_before":
                        req.prompt = memory_str + "\n\n" + (req.prompt or "")
                        logger.info(
                            f"[{session_id}] 成功向用户消息前注入 {len(recalled_memories)} 条记忆"
                        )
                    elif injection_method == "user_message_after":
                        req.prompt = (req.prompt or "") + "\n\n" + memory_str
                        logger.info(
                            f"[{session_id}] 成功向用户消息后注入 {len(recalled_memories)} 条记忆"
                        )
                    elif injection_method == "fake_tool_call":
                        fake_messages = format_memories_for_fake_tool_call(
                            memory_list,
                            query=actual_query,
                            k=self.config_manager.get("recall_engine.top_k", 5),
                            session_filtered=use_session_filtering,
                            persona_filtered=use_persona_filtering,
                        )
                        if fake_messages:
                            req.contexts.extend(fake_messages)
                            logger.info(
                                f"[{session_id}] 成功以伪造工具调用方式注入 "
                                f"{len(recalled_memories)} 条记忆"
                            )
                    else:
                        # extra_user_content（推荐）：追加到用户消息末尾，
                        # 不影响前缀缓存且 mark_as_temp 后不污染对话历史
                        req.extra_user_content_parts.append(
                            TextPart(text=memory_str).mark_as_temp()
                        )
                        logger.info(
                            f"[{session_id}] 成功向用户消息末尾注入 "
                            f"{len(recalled_memories)} 条记忆"
                        )
                else:
                    logger.info(f"[{session_id}] 未找到相关记忆")
                    trace_save = True

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"处理 on_llm_request 钩子时发生错误: {e}", exc_info=True)
            trace.errors.append(str(e))
            trace_save = True
        finally:
            # v5.4: 保存 AssemblyTrace
            if trace_save and self.context_trace_store:
                try:
                    trace.generate_self_reflection()
                    await self.context_trace_store.save_trace(trace)
                except Exception as e_trace:
                    logger.debug(f"[{trace.session_id}] 保存 AssemblyTrace 失败: {e_trace}")

    def _remove_injected_memories_from_context(
        self, req: ProviderRequest, session_id: str
    ) -> int:
        """从请求上下文中移除临时注入的记忆片段"""
        import re
        from ..base.constants import MEMORY_INJECTION_FOOTER, MEMORY_INJECTION_HEADER

        removed = 0

        # 清理 system_prompt（兼容旧版本注入残留）
        if hasattr(req, "system_prompt") and req.system_prompt:
            if isinstance(req.system_prompt, str):
                original_prompt = req.system_prompt
                if (
                    MEMORY_INJECTION_HEADER in original_prompt
                    and MEMORY_INJECTION_FOOTER in original_prompt
                ):
                    # 使用正则清理记忆片段
                    pattern = re.compile(
                        re.escape(MEMORY_INJECTION_HEADER)
                        + r".*?"
                        + re.escape(MEMORY_INJECTION_FOOTER),
                        re.DOTALL,
                    )
                    cleaned_prompt = pattern.sub("", original_prompt)
                    cleaned_prompt = re.sub(r"\n{3,}", "\n\n", cleaned_prompt).strip()
                    req.system_prompt = cleaned_prompt
                    if cleaned_prompt != original_prompt:
                        removed += 1

        # 清理 extra_user_content_parts（通过 mark_as_temp/_no_save 标记）
        parts_before = len(getattr(req, "extra_user_content_parts", []))
        if parts_before > 0:
            req.extra_user_content_parts = [
                part
                for part in req.extra_user_content_parts
                if not self._is_livingmemory_temp_part(part)
            ]
            parts_after = len(req.extra_user_content_parts)
            removed += parts_before - parts_after

        return removed

    def _is_livingmemory_temp_part(self, part) -> bool:
        """判断是否为 LivingMemory 本轮临时注入的 extra_user_content part"""
        from ..base.constants import MEMORY_INJECTION_FOOTER, MEMORY_INJECTION_HEADER

        text = getattr(part, "text", "")
        return (
            getattr(part, "_no_save", False)
            and isinstance(text, str)
            and MEMORY_INJECTION_HEADER in text
            and MEMORY_INJECTION_FOOTER in text
        )

    def _normalize_text_only_context_parts(
        self, req: ProviderRequest, session_id: str
    ) -> int:
        """把历史中的纯文本 content parts 折叠回字符串，避免污染长期上下文格式"""
        contexts = getattr(req, "contexts", None)
        if not isinstance(contexts, list):
            return 0

        normalized = 0
        for msg in contexts:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list) or not content:
                continue

            text_parts = []
            text_only = True
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    text_only = False
                    break
                text_parts.append(str(part.get("text", "") or ""))

            if not text_only:
                continue

            msg["content"] = "".join(text_parts)
            normalized += 1

        if normalized:
            logger.debug(f"[{session_id}] 已归一化 {normalized} 条纯文本历史 content parts")
        return normalized

    def _remove_fake_tool_call_from_context(
        self, req: ProviderRequest, session_id: str
    ) -> int:
        """从请求上下文中移除伪造的工具调用记忆（fake_tool_call 注入方式）

        识别并移除以 FAKE_TOOL_CALL_ID_PREFIX 为 ID 前缀的
        assistant(tool_calls) + tool(result) 消息对。
        """
        from ..base.constants import FAKE_TOOL_CALL_ID_PREFIX

        if not hasattr(req, "contexts") or not req.contexts:
            return 0

        removed = 0
        indices_to_remove: set[int] = set()
        fake_call_ids: set[str] = set()

        try:
            # 单轮扫描：同时收集伪造 assistant(tool_calls) 和对应 tool(result) 消息
            for i, msg in enumerate(req.contexts):
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                if role == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        tc_id = (
                            tc.get("id", "")
                            if isinstance(tc, dict)
                            else getattr(tc, "id", "")
                        )
                        if tc_id.startswith(FAKE_TOOL_CALL_ID_PREFIX):
                            fake_call_ids.add(tc_id)
                            indices_to_remove.add(i)
                elif role == "tool":
                    tc_id = msg.get("tool_call_id", "")
                    if tc_id in fake_call_ids:
                        indices_to_remove.add(i)

            # 从后往前删除，避免索引偏移
            for i in sorted(indices_to_remove, reverse=True):
                req.contexts.pop(i)
                removed += 1

        except Exception:
            pass

        return removed

    async def _inject_session_summary(
        self, event: AstrMessageEvent, req: ProviderRequest, session_id: str,
        trace: "AssemblyTrace | None" = None,
    ) -> None:
        """
        v5.3: 注入上次会话摘要到 extra_user_content_parts

        在 FTS 召回之前注入，让老婆知道"上次聊到哪了"。
        包含：话题、决策、情感基调、待续事项。
        """
        try:
            # 从插件实例获取 session_summary_manager
            # 通过 EventHandler 的 context 引用链获取
            summary_manager = getattr(self, "_session_summary_manager", None)
            if summary_manager is None:
                # 尝试从 event_handler 获取
                event_handler = getattr(self, "_event_handler_ref", None)
                if event_handler:
                    summary_manager = getattr(event_handler, "session_summary_manager", None)
                if summary_manager is None:
                    return

            # 获取上次会话摘要
            summary_text = await summary_manager.get_last_summary(session_id)
            if not summary_text:
                return

            # 获取待续事项
            continuation_points = await summary_manager.get_continuation_points(session_id)

            # 构建注入文本
            parts = ["## 上次会话摘要"]
            parts.append(summary_text)

            if continuation_points:
                parts.append("\n## 上次待续事项")
                for p in continuation_points:
                    parts.append(f"- {p}")

            injection_text = "\n".join(parts)

            # 注入到 extra_user_content_parts
            req.extra_user_content_parts.append(
                TextPart(text=injection_text).mark_as_temp()
            )

            # v5.4: 记录会话摘要注入（P0-B 修复：trace 由调用方传入，修 NameError 静默失败）
            if trace is not None:
                trace.summary_injected = True
                trace.summary_text = summary_text[:200]
                trace.summary_continuation_count = len(continuation_points)

            logger.info(
                f"[{session_id}] 已注入上次会话摘要 "
                f"({len(injection_text)} 字符)"
            )

        except Exception as e:
            logger.debug(f"[{session_id}] 会话摘要注入跳过: {e}")

    async def _prefetch_warmup(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        session_id: str,
        trace: "AssemblyTrace | None" = None,
    ) -> None:
        """P0-B MemGPT 式开场预取（2026-09-07 论文落地）。

        距上次预取超过 6h（≈隔夜/下班回来）时，不依赖 query 主动注入：
        ① 热记忆 top3（importance×recency，MemoryEngine.get_warmup_memories）
        ② v2 画像摘要 top3（≈MemGPT core memory）
        免疫降级：任何失败只 debug 日志，绝不阻断召回主链。
        开关：recall_engine.warmup_prefetch_enabled（默认 true）。"""
        try:
            if not self.config_manager.get(
                "recall_engine.warmup_prefetch_enabled", True
            ):
                return
            me = getattr(self, "memory_engine", None)
            cm = getattr(self, "conversation_manager", None)
            if me is None or cm is None:
                return

            now = time.time()
            try:
                last_ts = float(
                    await cm.get_session_metadata(session_id, "last_warmup_ts", 0) or 0
                )
            except Exception:
                last_ts = 0.0
            if (now - last_ts) <= 6 * 3600:
                return  # 6h 内预取过，冷却中

            parts: list[str] = []
            warm_count = 0

            # ① 热记忆 top3
            try:
                warm = await me.get_warmup_memories(k=3)
                if warm:
                    parts.append("## 开场速览·近期热记忆")
                    for m in warm:
                        parts.append(f"- {m['text']}")
                    warm_count = len(warm)
            except Exception as e:
                logger.debug(f"[{session_id}] 开场预取热记忆跳过: {e}")

            # ② v2 画像摘要（≈MemGPT core memory）
            prof_count = 0
            v2 = getattr(me, "v2_engine", None)
            if v2 is not None and getattr(v2, "enabled", False):
                try:
                    prof = await v2.get_profile_summary("default", limit=3)
                    traits = (prof or {}).get("traits") or []
                    if traits:
                        parts.append("")
                        parts.append("## 开场速览·对他了解")
                        for t in traits[:3]:
                            key = t.get("key") or ""
                            val = t.get("value") or ""
                            if val:
                                parts.append(f"- {key}: {val}" if key else f"- {val}")
                        prof_count = min(3, len(traits))
                except Exception as e:
                    logger.debug(f"[{session_id}] 开场预取画像跳过: {e}")

            if not parts:
                return

            injection_text = "\n".join(parts)
            req.extra_user_content_parts.append(
                TextPart(text=injection_text).mark_as_temp()
            )
            await cm.update_session_metadata(session_id, "last_warmup_ts", now)
            if trace is not None:
                try:
                    trace.warmup_injected = True
                    trace.warmup_memories = warm_count
                    trace.warmup_traits = prof_count
                except Exception:
                    pass
            logger.info(
                f"[{session_id}] 开场预取注入: 热记忆{warm_count}条 画像{prof_count}条"
            )
        except Exception as e:
            logger.debug(f"[{session_id}] 开场预取跳过: {e}")

