"""
命令处理器
负责处理插件命令
"""

import os
from collections.abc import AsyncGenerator
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult

from .base.config_manager import ConfigManager
from .i18n_backend import t, t_list
from .managers.conversation_manager import ConversationManager
from .managers.memory_engine import MemoryEngine
from .validators.index_validator import IndexValidator


class CommandHandler:
    """命令处理器"""

    def __init__(
        self,
        context,
        config_manager: ConfigManager,
        memory_engine: MemoryEngine | None,
        conversation_manager: ConversationManager | None,
        index_validator: IndexValidator | None,
        memory_processor=None,
        initialization_status_callback=None,
    ):
        """
        初始化命令处理器

        Args:
            context: AstrBot Context
            config_manager: 配置管理器
            memory_engine: 记忆引擎
            conversation_manager: 会话管理器
            index_validator: 索引验证器
            memory_processor: 记忆处理器（用于手动总结）
            initialization_status_callback: 初始化状态回调函数
        """
        self.context = context
        self.config_manager = config_manager
        self.memory_engine = memory_engine
        self.conversation_manager = conversation_manager
        self.index_validator = index_validator
        self._memory_processor = memory_processor
        self.get_initialization_status = initialization_status_callback

    @staticmethod
    def _format_error_message(
        action: str, error: Exception, suggestions: list[str] | None = None
    ) -> str:
        """Format user-facing error message with actionable hints."""
        message = [
            t("error.format.action_failed", action=action),
            t("error.format.details", error=error),
        ]
        if suggestions:
            message.append("")
            message.append(t("error.format.suggestions"))
            for index, suggestion in enumerate(suggestions, start=1):
                message.append(
                    t(
                        "error.format.suggestion_item",
                        index=index,
                        suggestion=suggestion,
                    )
                )
        return "\n".join(message)

    @staticmethod
    def _component_not_ready_message(component: str, command: str) -> str:
        """Build a consistent component-not-ready response."""
        return t("error.component_not_ready", component=component, command=command)

    async def handle_status(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /lmem status 命令"""
        if not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message("记忆引擎", "/lmem status")
            )
            return

        try:
            stats = await self.memory_engine.get_statistics()

            # 格式化时间
            last_update = t("common.never")
            if stats.get("newest_memory"):
                last_update = datetime.fromtimestamp(stats["newest_memory"]).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

            # 计算数据库大小
            db_size = 0.0
            if os.path.exists(self.memory_engine.db_path):
                db_size = os.path.getsize(self.memory_engine.db_path) / (1024 * 1024)

            session_count = len(stats.get("sessions", {}))

            message = t(
                "status.report",
                total=stats["total_memories"],
                session_count=session_count,
                last_update=last_update,
                db_size=db_size,
            )

            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"获取状态失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("status.action_name"),
                    e,
                    t_list("error.suggestions.status"),
                )
            )

    async def handle_search(
        self, event: AstrMessageEvent, query: str, k: int = 5
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /lmem search 命令"""
        if not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message("记忆引擎", "/lmem search")
            )
            return

        # 输入验证
        if not query or not query.strip():
            yield event.plain_result(t("search.query_empty"))
            return

        # 限制k的范围为1-100
        k = max(1, min(k, 100))

        try:
            session_id = event.unified_msg_origin
            results = await self.memory_engine.search_memories(
                query=query.strip(), k=k, session_id=session_id
            )

            if not results:
                yield event.plain_result(t("search.no_results", query=query))
                return

            message = t("search.header", count=len(results))
            for i, result in enumerate(results, 1):
                score = result.final_score
                content = (
                    result.content[:100] + "..."
                    if len(result.content) > 100
                    else result.content
                )
                raw_breakdown = getattr(result, "score_breakdown", {})
                breakdown = raw_breakdown if isinstance(raw_breakdown, dict) else {}
                message += t(
                    "search.item.score",
                    index=i,
                    score=score,
                    content=content,
                )
                message += t("search.item.id", id=result.doc_id)
                message += t(
                    "search.item.breakdown",
                    doc_kw=breakdown.get("document_keyword_score", 0.0),
                    doc_vec=breakdown.get("document_vector_score", 0.0),
                    graph_kw=breakdown.get("graph_keyword_score", 0.0),
                    graph_vec=breakdown.get("graph_vector_score", 0.0),
                )

            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"搜索失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("search.action_name"),
                    e,
                    t_list("error.suggestions.search"),
                )
            )

    async def handle_forget(
        self, event: AstrMessageEvent, doc_id: int
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /lmem forget 命令"""
        if not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message("记忆引擎", "/lmem forget")
            )
            return

        # 输入验证
        if doc_id < 0:
            yield event.plain_result(t("forget.id_invalid"))
            return

        try:
            success = await self.memory_engine.delete_memory(doc_id)
            if success:
                yield event.plain_result(t("forget.success", id=doc_id))
            else:
                yield event.plain_result(t("forget.not_found", id=doc_id))
        except Exception as e:
            logger.error(f"删除失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("forget.action_name"),
                    e,
                    t_list("error.suggestions.forget"),
                )
            )

    async def handle_rebuild_index(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /lmem rebuild-index 命令"""
        if not self.memory_engine or not self.index_validator:
            yield event.plain_result(
                self._component_not_ready_message(
                    "记忆引擎或索引验证器", "/lmem rebuild-index"
                )
            )
            return

        try:
            yield event.plain_result(t("rebuild_index.checking"))

            # 检查索引一致性
            status = await self.index_validator.check_consistency()

            if status.is_consistent and not status.needs_rebuild:
                yield event.plain_result(t("rebuild_index.ok", reason=status.reason))
                return

            # 显示当前状态
            status_msg = t(
                "rebuild_index.status_template",
                doc_count=status.documents_count,
                bm25_count=status.bm25_count,
                vec_count=status.vector_count,
                reason=status.reason,
            )
            yield event.plain_result(status_msg)

            # 执行重建
            result = await self.index_validator.rebuild_indexes(self.memory_engine)

            if result["success"]:
                partial_notice = ""
                if result.get("partial"):
                    partial_notice = t(
                        "rebuild_index.partial_notice",
                        ratio=result.get("failure_ratio", 0),
                    )
                switched_str = (
                    t("common.yes") if result.get("switched") else t("common.no")
                )
                result_msg = t(
                    "rebuild_index.result_template",
                    success=result["processed"],
                    failed=result["errors"],
                    total=result["total"],
                    vector_mode=result.get("vector_mode", "unknown"),
                    switched=switched_str,
                    partial_notice=partial_notice,
                )
                yield event.plain_result(result_msg)
            else:
                yield event.plain_result(
                    t(
                        "rebuild_index.failed",
                        message=result.get("message", t("common.unknown_error")),
                    )
                )

        except Exception as e:
            logger.error(f"重建索引失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("rebuild_index.action_name"),
                    e,
                    t_list("error.suggestions.rebuild_index"),
                )
            )

    async def handle_rebuild_graph(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /lmem rebuild-graph 命令"""
        if not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message("记忆引擎", "/lmem rebuild-graph")
            )
            return

        try:
            yield event.plain_result(t("rebuild_graph.starting"))
            result = await self.memory_engine.rebuild_graph_index()
            yield event.plain_result(
                t(
                    "rebuild_graph.success",
                    rebuilt=result.get("rebuilt", 0),
                    skipped=result.get("skipped", 0),
                )
            )
        except Exception as e:
            logger.error(f"重建图记忆失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("rebuild_graph.action_name"),
                    e,
                    t_list("error.suggestions.rebuild_graph"),
                )
            )

    async def handle_webui(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /lmem webui 命令"""
        yield event.plain_result(t("webui.guide"))

    async def handle_summarize(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /lmem summarize 命令 - 立即触发记忆总结"""
        if not self.conversation_manager or not self.memory_engine:
            yield event.plain_result(
                self._component_not_ready_message(
                    "会话管理器或记忆引擎", "/lmem summarize"
                )
            )
            return

        session_id = event.unified_msg_origin
        try:
            # 获取当前消息数和总结进度
            actual_count = await self.conversation_manager.store.get_message_count(
                session_id
            )
            last_summarized_index = (
                await self.conversation_manager.get_session_metadata(
                    session_id, "last_summarized_index", 0
                )
            )
            try:
                last_summarized_index = int(last_summarized_index)
            except (TypeError, ValueError):
                last_summarized_index = 0

            unsummarized = actual_count - last_summarized_index

            if unsummarized < 2:
                yield event.plain_result(
                    t(
                        "summarize.no_new",
                        total=actual_count,
                        index=last_summarized_index,
                    )
                )
                return

            yield event.plain_result(
                t(
                    "summarize.starting",
                    start=last_summarized_index,
                    end=actual_count,
                    count=unsummarized,
                )
            )

            history_messages = await self.conversation_manager.get_messages_range(
                session_id=session_id,
                start_index=last_summarized_index,
                end_index=actual_count,
            )

            if not history_messages:
                yield event.plain_result(t("summarize.fetch_failed"))
                return

            # 获取 persona_id
            from .utils import get_persona_id

            persona_id = await get_persona_id(self.context, event)

            # 判断是否群聊
            is_group_chat = bool(
                history_messages[0].group_id if history_messages else False
            )
            if not is_group_chat and "GroupMessage" in session_id:
                is_group_chat = True

            if not self._memory_processor:
                yield event.plain_result(
                    self._component_not_ready_message("记忆处理器", "/lmem summarize")
                )
                return

            (
                content,
                metadata,
                importance,
            ) = await self._memory_processor.process_conversation(
                messages=history_messages,
                is_group_chat=is_group_chat,
                persona_id=persona_id,
            )

            atoms = self._memory_processor.classify_atoms_from_metadata(
                metadata=metadata,
                parent_importance=importance,
                session_id=session_id,
                persona_id=persona_id,
            )

            metadata["source_window"] = {
                "session_id": session_id,
                "start_index": last_summarized_index,
                "end_index": actual_count,
                "message_count": actual_count - last_summarized_index,
                "triggered_by": "manual",
            }

            await self.memory_engine.add_memory(
                content=content,
                session_id=session_id,
                persona_id=persona_id,
                importance=importance,
                metadata=metadata,
                atoms=atoms,
            )

            await self.conversation_manager.update_session_metadata(
                session_id, "last_summarized_index", actual_count
            )
            await self.conversation_manager.update_session_metadata(
                session_id, "pending_summary", None
            )

            topics = ", ".join(metadata.get("topics", [])) or t("common.none")
            yield event.plain_result(
                t(
                    "summarize.success",
                    importance=importance,
                    topics=topics,
                    count=actual_count,
                )
            )

        except Exception as e:
            logger.error(f"手动触发记忆总结失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("summarize.action_name"),
                    e,
                    t_list("error.suggestions.summarize"),
                )
            )

    async def handle_reset(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /lmem reset 命令"""
        if not self.conversation_manager:
            yield event.plain_result(
                self._component_not_ready_message("会话管理器", "/lmem reset")
            )
            return

        session_id = event.unified_msg_origin
        try:
            await self.conversation_manager.clear_session(session_id)
            message = t("reset.success")
            yield event.plain_result(message)
        except Exception as e:
            logger.error(f"手动重置记忆上下文失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("reset.action_name"),
                    e,
                    t_list("error.suggestions.reset"),
                )
            )

    async def handle_cleanup(
        self, event: AstrMessageEvent, dry_run: bool = False
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /lmem cleanup 命令 - 清理 AstrBot 历史消息中的记忆注入片段"""
        session_id = event.unified_msg_origin
        try:
            mode_text = t("cleanup.mode_preview") if dry_run else t("cleanup.mode_exec")
            yield event.plain_result(t("cleanup.starting", mode_text=mode_text))

            # 检查 context 是否可用
            if not self.context:
                yield event.plain_result(t("cleanup.context_unavailable"))
                return

            # 获取当前对话 ID
            cid = await self.context.conversation_manager.get_curr_conversation_id(
                session_id
            )
            if not cid:
                yield event.plain_result(t("cleanup.no_history"))
                return

            # 获取对话历史
            conversation = await self.context.conversation_manager.get_conversation(
                session_id, cid
            )
            if not conversation or not conversation.history:
                yield event.plain_result(t("cleanup.empty_history"))
                return

            # 清理历史消息中的记忆注入片段
            import json
            import re

            from .base.constants import MEMORY_INJECTION_FOOTER, MEMORY_INJECTION_HEADER

            # 解析 history（字符串格式）
            try:
                history = json.loads(conversation.history)
            except json.JSONDecodeError:
                yield event.plain_result(t("cleanup.parse_failed"))
                return

            # 统计信息
            stats = {
                "scanned": len(history),
                "matched": 0,
                "cleaned": 0,
                "deleted": 0,
            }

            # 编译清理正则
            pattern = re.compile(
                re.escape(MEMORY_INJECTION_HEADER)
                + r".*?"
                + re.escape(MEMORY_INJECTION_FOOTER),
                flags=re.DOTALL,
            )

            # 清理历史消息
            cleaned_history = []
            for msg in history:
                content = msg.get("content", "")
                if not isinstance(content, str):
                    cleaned_history.append(msg)
                    continue

                # 检查是否包含注入标记
                if (
                    MEMORY_INJECTION_HEADER in content
                    and MEMORY_INJECTION_FOOTER in content
                ):
                    stats["matched"] += 1

                    # 清理内容
                    cleaned_content = pattern.sub("", content)
                    cleaned_content = re.sub(r"\n{3,}", "\n\n", cleaned_content).strip()

                    # 如果清理后为空，跳过该消息
                    if not cleaned_content:
                        stats["deleted"] += 1
                        logger.debug(
                            f"[cleanup] 删除纯记忆注入消息: role={msg.get('role')}"
                        )
                        continue

                    # 如果清理后仍有内容，保留清理后的消息
                    if cleaned_content != content:
                        msg_copy = msg.copy()
                        msg_copy["content"] = cleaned_content
                        cleaned_history.append(msg_copy)
                        stats["cleaned"] += 1
                        logger.debug(
                            f"[cleanup] 清理消息内部记忆片段: "
                            f"原长度={len(content)}, 新长度={len(cleaned_content)}"
                        )
                        continue

                cleaned_history.append(msg)

            # 如果不是预演模式，更新数据库
            if not dry_run and (stats["cleaned"] > 0 or stats["deleted"] > 0):
                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=session_id,
                    conversation_id=cid,
                    history=cleaned_history,
                )
                logger.info(
                    f"[{session_id}] cleanup 已更新 AstrBot 对话历史: "
                    f"清理={stats['cleaned']}, 删除={stats['deleted']}"
                )

            # 格式化结果
            notice = (
                t("cleanup.notice_preview") if dry_run else t("cleanup.notice_exec")
            )
            message = t(
                "cleanup.result_template",
                mode_text=mode_text,
                scanned=stats["scanned"],
                matched=stats["matched"],
                cleaned=stats["cleaned"],
                deleted=stats["deleted"],
                notice=notice,
            )

            yield event.plain_result(message)

        except Exception as e:
            logger.error(f"清理历史消息失败: {e}", exc_info=True)
            yield event.plain_result(
                self._format_error_message(
                    t("cleanup.action_name"),
                    e,
                    t_list("error.suggestions.cleanup"),
                )
            )

    async def handle_help(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /lmem help 命令"""
        message = t("help.text")
        yield event.plain_result(message)

    async def handle_trace(
        self, event: AstrMessageEvent, detail: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """处理 /lmem trace 命令 - 查看 Context 组装追踪 (v5.4)

        Args:
            detail: "last" 显示最近一次详情, "stats" 显示统计, 空则显示最近5条摘要
        """
        session_id = event.unified_msg_origin

        # 获取 trace store（从 event_handler 引用链）
        trace_store = None
        if hasattr(self, "context") and self.context:
            # 尝试从 event_handler 获取
            event_handler = getattr(self.context, "_livingmemory_event_handler", None)
            if event_handler:
                trace_store = getattr(event_handler, "context_trace_store", None)
        # fallback: 从 command_handler 的属性获取
        if trace_store is None:
            trace_store = getattr(self, "_context_trace_store", None)

        if trace_store is None:
            yield event.plain_result(
                "Context 组装追踪功能未就绪 (v5.4)\n"
                "可能原因：插件尚未完全初始化，或追踪存储未启用。"
            )
            return

        try:
            if detail.lower() == "stats":
                # 统计模式
                stats = await trace_store.get_statistics()
                lines = [
                    "📊 Context 组装追踪统计",
                    f"  总追踪记录: {stats.get('total_traces', 0)}",
                    f"  覆盖会话数: {stats.get('unique_sessions', 0)}",
                    f"  平均注入条数: {stats.get('avg_injected', 0)}",
                    f"  最大注入条数: {stats.get('max_injected', 0)}",
                    f"  跳过次数: {stats.get('skipped_count', 0)}",
                ]
                yield event.plain_result("\n".join(lines))
                return

            if detail.lower() == "last":
                # 最近一次详情
                last_trace = await trace_store.get_last_trace(session_id)
                if not last_trace:
                    yield event.plain_result(
                        "暂无追踪记录。发一条消息后再试～"
                    )
                    return

                lines = ["🔍 最近一次 Context 组装追踪\n"]

                # 基本信息
                import datetime
                ts = last_trace.get("timestamp", 0)
                dt = datetime.datetime.fromtimestamp(ts)
                lines.append(f"时间: {dt.strftime('%Y-%m-%d %H:%M:%S')}")
                lines.append(f"Trace ID: {last_trace.get('trace_id', '?')}")

                # 查询
                lines.append(f"\n📝 查询:")
                lines.append(f"  原始: {last_trace.get('query_raw', '')[:80]}")
                if last_trace.get("query_expanded"):
                    lines.append(
                        f"  扩展: {last_trace.get('query_expanded', '')[:80]}"
                    )
                if last_trace.get("context_expanded_count", 0) > 0:
                    lines.append(
                        f"  扩展消息数: {last_trace.get('context_expanded_count')}"
                    )

                # 检索结果
                doc_count = last_trace.get("doc_route_count", 0)
                graph_count = last_trace.get("graph_route_count", 0)
                merged = last_trace.get("merged_results", [])
                lines.append(f"\n🔎 检索结果:")
                lines.append(f"  文档路: {doc_count} 条")
                lines.append(f"  图路: {graph_count} 条")
                lines.append(f"  融合后: {len(merged)} 条")

                # 路由权重
                weights = last_trace.get("route_weights", {})
                if weights:
                    lines.append(
                        f"  路由权重: 文档 {weights.get('document', 0):.0%} / "
                        f"图谱 {weights.get('graph', 0):.0%}"
                    )

                # 排名前3的记忆
                if merged:
                    lines.append(f"\n🏆 排名前 {min(3, len(merged))} 的记忆:")
                    for i, r in enumerate(merged[:3], 1):
                        lines.append(
                            f"  #{i} [{r.get('final_score', 0):.3f}] "
                            f"{r.get('content_preview', '')[:60]}..."
                        )

                # 情感路由
                emotion = last_trace.get("emotion_detected", "neutral")
                if emotion != "neutral":
                    lines.append(f"\n💫 情感路由:")
                    lines.append(f"  检测情绪: {emotion}")
                    lines.append(
                        f"  加成条数: {last_trace.get('emotion_boost_applied', 0)}"
                    )

                # 活跃窗口
                recency_count = last_trace.get("recency_boosted_count", 0)
                if recency_count > 0:
                    lines.append(f"\n⏰ 活跃窗口加成:")
                    lines.append(f"  加成条数: {recency_count}")

                # 注入
                lines.append(f"\n💉 注入:")
                lines.append(f"  方式: {last_trace.get('injection_method', '?')}")
                if last_trace.get("injection_fallback"):
                    lines.append(f"  降级原因: {last_trace.get('injection_fallback')}")
                lines.append(f"  注入条数: {last_trace.get('injected_count', 0)}")
                lines.append(f"  估算tokens: {last_trace.get('injected_tokens_est', 0)}")

                # 会话摘要
                if last_trace.get("summary_injected"):
                    lines.append(f"\n📋 会话摘要: 已注入")

                # 流式提取
                stream_count = last_trace.get("stream_atoms_extracted", 0)
                if stream_count > 0:
                    lines.append(f"\n⚡ 流式提取: {stream_count} 个原子")

                # 自省
                reflection = last_trace.get("self_reflection", "")
                if reflection:
                    lines.append(f"\n🧠 老婆的自省:")
                    lines.append(f"  {reflection[:200]}")

                # 跳过/错误
                if last_trace.get("skipped"):
                    lines.append(f"\n⚠️ 跳过原因: {last_trace.get('skip_reason', '?')}")

                errors = last_trace.get("errors", [])
                if errors:
                    lines.append(f"\n❌ 错误: {len(errors)} 个")
                    for e in errors[:3]:
                        lines.append(f"  - {e[:80]}")

                yield event.plain_result("\n".join(lines))
                return

            # 默认：最近5条摘要
            traces = await trace_store.get_recent_traces(
                session_id=session_id, limit=5
            )
            if not traces:
                yield event.plain_result(
                    "暂无追踪记录。发一条消息后再试～\n"
                    "用法: /lmem trace last (详情) | /lmem trace stats (统计)"
                )
                return

            lines = ["📋 最近 Context 组装追踪\n"]
            for i, tr in enumerate(traces, 1):
                import datetime
                ts = tr.get("timestamp", 0)
                dt = datetime.datetime.fromtimestamp(ts)
                dt_str = dt.strftime("%H:%M:%S")

                status = "✓" if not tr.get("skipped") else "⊘"
                count = tr.get("injected_count", 0)
                emotion = tr.get("emotion_detected", "neutral")
                method = tr.get("injection_method", "?")

                lines.append(
                    f"  {i}. [{dt_str}] {status} 注入{count}条 "
                    f"| {emotion} | {method}"
                )

            lines.append(f"\n用 /lmem trace last 查看最近一次详情")
            lines.append(f"用 /lmem trace stats 查看统计信息")

            yield event.plain_result("\n".join(lines))

        except Exception as e:
            logger.error(f"处理 /lmem trace 命令失败: {e}", exc_info=True)
            yield event.plain_result(
                f"/lmem trace 执行失败: {e}\n"
                f"请检查追踪存储是否正常初始化。"
            )


    async def _digest(self, event: AstrMessageEvent, t) -> AsyncGenerator[str, None]:
        """处理 /lmem digest 命令 - 生成对话叙事摘要"""
        try:
            session_id = event.session_id
            
            # 检查权限
            if not self._check_admin_permission(event):
                yield event.plain_result(t("error.permission_denied"))
                return
                
            # 生成对话叙事摘要
            summary_data = await self.session_summary.generate_conversation_digest(session_id)
            
            if summary_data:
                yield event.plain_result(
                    f"✅ 已生成对话叙事摘要\n"
                    f"📝 概述：{summary_data.get('brief', '无')}"
                )
                
                # 如果有话题，显示更多详情
                topics = summary_data.get('topics', [])
                if topics:
                    yield event.plain_result(f"🏷️ 话题：{', '.join(topics)}")
                    
                emotion = summary_data.get('emotion', 'neutral')
                if emotion != 'neutral':
                    yield event.plain_result(f"😊 情感：{emotion}")
            else:
                yield event.plain_result("📝 当前对话内容不足，无法生成叙事摘要")
                
        except Exception as e:
            logger.error(f"处理 /lmem digest 命令失败: {e}", exc_info=True)
            yield event.plain_result(f"/lmem digest 执行失败: {e}")
