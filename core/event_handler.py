"""
事件处理器
负责处理AstrBot事件钩子
"""

import asyncio
import hashlib
import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse, ProviderRequest

from .base.config_manager import ConfigManager
from .base.constants import (
    FAKE_TOOL_CALL_ID_PREFIX,
    MEMORY_INJECTION_FOOTER,
    MEMORY_INJECTION_HEADER,
)
from .event_handler_modules import (
    GroupCapture,
    MemoryRecall,
    MemoryReflection,
    MessageUtils,
)
from .managers.conversation_manager import ConversationManager
from .managers.memory_engine import MemoryEngine
from .processors.memory_processor import MemoryProcessor
from .utils import (
    OperationContext,
    format_memories_for_fake_tool_call,
    format_memories_for_injection,
    get_persona_id,
)
from .utils.injection_adapter import InjectionAdapter

# 预编译记忆注入清理正则（热路径优化：避免每次调用 re.compile）
_INJECTION_CLEANUP_PATTERN = re.compile(
    re.escape(MEMORY_INJECTION_HEADER) + r".*?" + re.escape(MEMORY_INJECTION_FOOTER),
    flags=re.DOTALL,
)


class EventHandler:
    """事件处理器"""

    def __init__(
        self,
        context: Any,
        config_manager: ConfigManager,
        memory_engine: MemoryEngine,
        memory_processor: MemoryProcessor,
        conversation_manager: ConversationManager,
    ):
        """
        初始化事件处理器

        Args:
            context: AstrBot上下文
            config_manager: 配置管理器
            memory_engine: 记忆引擎
            memory_processor: 记忆处理器
            conversation_manager: 会话管理器
        """
        self.context = context
        self.config_manager = config_manager
        self.memory_engine = memory_engine
        self.memory_processor = memory_processor
        self.conversation_manager = conversation_manager

        # v5.4: 初始化 Context 组装追踪存储
        import os
        from .managers.context_trace_store import ContextTraceStore

        db_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data",
        )
        os.makedirs(db_dir, exist_ok=True)
        trace_db_path = os.path.join(db_dir, "context_traces.db")
        self.context_trace_store = ContextTraceStore(
            db_path=trace_db_path,
            max_traces=500,
        )
        # 异步初始化（在第一次 on_llm_request 时确保已初始化）
        self._trace_store_initialized = False

        # 初始化子模块
        self._message_utils = MessageUtils(config_manager, conversation_manager)
        self._group_capture = GroupCapture(
            config_manager, conversation_manager, self._message_utils
        )
        self._injection_adapter = InjectionAdapter()
        self._memory_recall = MemoryRecall(
            context,
            config_manager,
            memory_engine,
            conversation_manager,
            self._message_utils,
            self._injection_adapter,
            context_trace_store=self.context_trace_store,
        )

        # 后台存储任务跟踪
        self._storage_tasks: set[asyncio.Task] = set()
        self._storage_sessions_inflight: set[str] = set()
        self._storage_state_lock = asyncio.Lock()
        self._shutting_down = False

        self._memory_reflection = MemoryReflection(
            context,
            config_manager,
            memory_engine,
            memory_processor,
            conversation_manager,
            self._message_utils,
            self._storage_tasks,
            self._storage_sessions_inflight,
            self._storage_state_lock,
        )

        # v5.3: 会话摘要管理器
        from .session_summary import SessionSummaryManager

        self.session_summary_manager = SessionSummaryManager(
            config_manager=config_manager,
            memory_engine=memory_engine,
            conversation_manager=conversation_manager,
            context=context,
        )
        # 给 memory_recall 模块设置引用
        self._memory_recall._session_summary_manager = self.session_summary_manager
        self._memory_recall._event_handler_ref = self

        # v9: L3 跨会话记忆合成器
        from .l3_synthesizer import L3Synthesizer

        self.l3_synthesizer = L3Synthesizer(
            config_manager=config_manager,
            memory_engine=memory_engine,
            context=context,
        )

    async def handle_all_group_messages(self, event: AstrMessageEvent):
        """Capture all group messages for memory storage"""
        await self._group_capture.handle_all_group_messages(event)

    async def handle_memory_recall(self, event: AstrMessageEvent, req: ProviderRequest):
        """Query and inject long-term memory before LLM request"""
        # v5.4: 确保 trace store 已初始化
        if not self._trace_store_initialized:
            try:
                await self.context_trace_store.initialize()
                self._trace_store_initialized = True
            except Exception as e:
                logger.debug(f"[EventHandler] ContextTraceStore 初始化失败: {e}")
                self._trace_store_initialized = True  # 避免重复尝试

        # 情感 v4.0 P2：旁挂情感评估（fire-and-forget，绝不阻塞主链）
        try:
            _ap = getattr(self, "appraisal_engine", None)
            if _ap is None:
                logger.debug("[Appraisal] hook跳过: 引擎未注入")
            else:
                _txt = getattr(event, "message_str", None) or ""
                if _txt.strip():
                    import asyncio as _aio

                    _t = _aio.get_running_loop().create_task(
                        _ap.safe_evaluate("default", _txt)
                    )
                    try:
                        event._appraisal_task = _t  # P3-1b: reflection 存库前等待，原子标签命中当条
                    except BaseException:
                        pass
                    self._ap_tasks = getattr(self, "_ap_tasks", [])
                    self._ap_tasks.append(_t)
                    if len(self._ap_tasks) > 20:
                        self._ap_tasks = [x for x in self._ap_tasks[1:] if not x.done()]
                    logger.debug("[Appraisal] hook已发评估任务 (%d字)", len(_txt))
        except BaseException:
            logger.debug("[Appraisal] hook异常(忽略)", exc_info=True)

        # P0 心情卡片注入（桌宠设计2.2）：实时心情进系统层，断片免疫。失败全静默
        try:
            _ap0 = getattr(self, "appraisal_engine", None)
            _mc = None
            if _ap0 is not None:
                from .v2.mood_card import make_card
                _mc = make_card(getattr(_ap0, "core", None), _ap0, "default")
            if _mc:
                # v2 缓存优化: 心情值每轮微变,注system尾=前缀必断→挪user尾
                try:
                    from astrbot.core.message.components import Text as _Txt
                    if getattr(req, "extra_user_content_parts", None) is None:
                        req.extra_user_content_parts = []
                    req.extra_user_content_parts.append(_Txt(_mc))
                    logger.debug("[MoodCard] 已注入user尾: %s", _mc[:80])
                except Exception:
                    # v2.1: 宁可丢心情卡,绝不污染system前缀
                    logger.debug("[MoodCard] user尾注入失败,本轮跳过")
        except BaseException:
            logger.debug("[MoodCard] 注入失败(忽略)", exc_info=True)
        await self._memory_recall.handle_memory_recall(event, req)

    async def handle_memory_reflection(
        self, event: AstrMessageEvent, resp: LLMResponse
    ):
        """Check if reflection and memory storage is needed after LLM response"""
        await self._memory_reflection.handle_memory_reflection(event, resp)

    async def handle_session_reset(self, event: AstrMessageEvent) -> None:
        """处理 /reset 或 /new 触发的会话清空，同步清除插件侧的消息历史和总结计数器"""
        session_id = event.unified_msg_origin
        if not session_id:
            return
        try:
            await self.conversation_manager.clear_session(session_id)
            logger.info(f"[{session_id}] 已同步清空插件会话上下文（/reset 或 /new）")
        except Exception as e:
            logger.error(f"[{session_id}] 清空插件会话上下文失败: {e}", exc_info=True)

    async def shutdown(self):
        """关闭事件处理器，等待所有存储任务完成"""
        self._shutting_down = True
        self._memory_reflection.set_shutting_down(True)
        if self._storage_tasks:
            logger.info(f"等待 {len(self._storage_tasks)} 个存储任务完成...")
            await asyncio.gather(*self._storage_tasks, return_exceptions=True)
            self._storage_tasks.clear()
        self._storage_sessions_inflight.clear()
        logger.info("EventHandler 已关闭")
