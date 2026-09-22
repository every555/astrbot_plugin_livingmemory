"""
main.py - LivingMemory 插件主文件
负责插件注册、初始化和生命周期管理
"""

import asyncio
import re
from collections.abc import AsyncGenerator
from importlib import metadata as importlib_metadata
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from .core.base.config_manager import ConfigManager
from .core.command_handler import CommandHandler
from .core.event_handler import EventHandler
from .core.i18n_backend import init as i18n_init
from .core.i18n_backend import t
from .core.managers.backup_manager import BackupManager
from .core.passive_group_capture import PassiveGroupCaptureFilter
from .core.passive_group_capture import get_active_plugin
from .core.passive_group_capture import is_plugin_enabled_for_session
from .core.passive_group_capture import is_session_enabled
from .core.passive_group_capture import set_active_plugin
from .core.plugin_initializer import PluginInitializer
from .core.tools import MemoryMemorizeTool, MemorySearchTool

# V1.1 升级组件（可选加载）
try:
    from .core.managers.v11_features import LivingMemoryV11Adapter
    from .core.schedulers.v11_scheduler import V11Scheduler
    _V11_AVAILABLE = True
except ImportError as e:
    _V11_AVAILABLE = False
    logger.warning(f"V1.1 功能模块加载失败，将跳过: {e}")

_MIN_ASTRBOT_VERSION = "4.24.2"
_ASTRBOT_DISTRIBUTION_NAMES = ("AstrBot", "astrbot")


def _parse_version(v: str) -> tuple[int, ...]:
    m = re.match(r"v?(\d+(?:\.\d+)*)", v.strip(), re.IGNORECASE)
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def _version_lt(current: str, minimum: str) -> bool:
    current_parts = _parse_version(current)
    minimum_parts = _parse_version(minimum)
    if not current_parts or not minimum_parts:
        return False
    width = max(len(current_parts), len(minimum_parts))
    return current_parts + (0,) * (width - len(current_parts)) < minimum_parts + (
        0,
    ) * (width - len(minimum_parts))


def _detect_astrbot_version() -> str | None:
    for distribution_name in _ASTRBOT_DISTRIBUTION_NAMES:
        try:
            return importlib_metadata.version(distribution_name)
        except importlib_metadata.PackageNotFoundError:
            continue
        except Exception as exc:
            logger.debug(f"读取 AstrBot 分发版本失败 ({distribution_name}): {exc}")

    for module_name in ("astrbot.core.config.default", "astrbot.core.config"):
        try:
            module = __import__(module_name, fromlist=["VERSION"])
            version_value = getattr(module, "VERSION", None)
        except Exception as exc:
            logger.debug(f"读取 AstrBot 模块版本失败 ({module_name}): {exc}")
            continue
        if version_value:
            return str(version_value)

    return None


_CURRENT_ASTRBOT_VERSION = _detect_astrbot_version()

if _CURRENT_ASTRBOT_VERSION is None:
    logger.debug("未能检测到 AstrBot 版本，跳过 LivingMemory 版本兼容提示")
elif _version_lt(_CURRENT_ASTRBOT_VERSION, _MIN_ASTRBOT_VERSION):
    logger.warning(
        f"AstrBot 版本 {_CURRENT_ASTRBOT_VERSION} 低于推荐版本 {_MIN_ASTRBOT_VERSION}。"
        f"插件 Pages / WebUI 功能可能不可用。建议升级 AstrBot 以获得完整体验。"
    )


@register(
    "LivingMemory",
    "lxfight",
    "An intelligent long-term memory plugin with a dynamic lifecycle for AstrBot.",
    "2.4.0",
    "https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory",
)
class LivingMemoryPlugin(Star):
    """LivingMemory 插件主类"""

    def __init__(self, context: Context, config: dict[str, Any]):
        super().__init__(context)
        self.context = context

        # 获取插件数据目录
        data_dir = str(StarTools.get_data_dir("astrbot_plugin_livingmemory"))

        # 版本变更时自动备份数据（延迟到异步初始化阶段执行，避免 __init__ 中同步 I/O 阻塞）
        self._backup_manager = BackupManager(data_dir)

        # 初始化配置管理器
        self.config_manager = ConfigManager(config)

        # 初始化后端 i18n
        i18n_init(config.get("bot_language", "zh"))

        # 初始化插件初始化器
        self.initializer = PluginInitializer(context, self.config_manager, data_dir)

        # 事件处理器和命令处理器（初始化后创建）
        self.event_handler: EventHandler | None = None
        self.command_handler: CommandHandler | None = None

        # 后台任务跟踪集合
        self._background_tasks: set[asyncio.Task] = set()
        self._component_init_lock = asyncio.Lock()
        self._llm_tools_registered = False
        self._terminating = False

        # 安检门（Security Gate）v2：惰性初始化，第一次过秤才建（#1700 挂载定案）
        self._security_gate = None
        self._gate_data_dir = data_dir
        # 省察调度器（第6步·#1698）：惰性初始化 + 后台巡检任务
        self._reflection_scheduler = None
        self._reflection_task = None

        self.page_api = None
        self.v11_adapter: "LivingMemoryV11Adapter | None" = None
        self.v11_scheduler: "V11Scheduler | None" = None

        set_active_plugin(self)

        self._register_official_page_api_if_available()

        # 启动非阻塞的初始化任务
        self._create_tracked_task(self._initialize_plugin())

    def _register_official_page_api_if_available(self) -> None:
        """按需注册官方插件页面 API，避免旧版 AstrBot 因导入失败而无法加载插件。"""
        if not hasattr(self.context, "register_web_api"):
            return

        try:
            from .core.page_api import PluginPageApi
        except Exception as exc:
            logger.warning(
                f"官方插件页面 API 不可用，已跳过注册并保留旧版兼容模式: {exc}"
            )
            return

        try:
            self.page_api = PluginPageApi(self)
            self.page_api.register_routes()
        except Exception as exc:
            self.page_api = None
            logger.warning(
                f"官方插件页面 API 注册失败，已跳过并保留旧版兼容模式: {exc}",
                exc_info=True,
            )

    def _create_tracked_task(self, coro) -> asyncio.Task:
        """创建并跟踪后台任务"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _initialize_plugin(self):
        """初始化插件"""
        try:
            # 版本变更时自动备份数据（在任何数据库操作之前，通过线程池避免阻塞事件循环）
            await self._backup_manager.backup_if_needed_async()

            # 执行初始化
            success = await self.initializer.initialize()

            if success:
                await self._ensure_runtime_components()

        except Exception as e:
            logger.error(f"插件初始化失败: {e}", exc_info=True)

    async def _ensure_runtime_components(self) -> bool:
        """确保运行期组件（事件/命令处理器、WebUI）已就绪"""
        if self._terminating:
            return False
        if not self.initializer.is_initialized:
            return False

        async with self._component_init_lock:
            if self._terminating:
                return False
            # 检查必要组件是否初始化成功
            if not all(
                [
                    self.initializer.memory_engine,
                    self.initializer.memory_processor,
                    self.initializer.conversation_manager,
                ]
            ):
                logger.error("插件初始化不完整：部分核心组件未能初始化")
                return False

            # 创建事件处理器（幂等）
            if not self.event_handler:
                self.event_handler = EventHandler(
                    context=self.context,
                    config_manager=self.config_manager,
                    memory_engine=self.initializer.memory_engine,  # type: ignore[arg-type]
                    memory_processor=self.initializer.memory_processor,  # type: ignore[arg-type]
                    conversation_manager=self.initializer.conversation_manager,  # type: ignore[arg-type]
                )
                # 情感 v4.0 P2：注入评估引擎（旁挂免疫，缺了也不影响主链）
                try:
                    self.event_handler.appraisal_engine = (
                        self.initializer.appraisal_engine
                    )
                    logger.info(
                        "[Appraisal] 已注入EventHandler (engine=%s)",
                        "OK" if self.event_handler.appraisal_engine else "None!",
                    )
                except BaseException:
                    self.event_handler.appraisal_engine = None
                    logger.warning("[Appraisal] 注入失败", exc_info=True)

            # 创建命令处理器（幂等）
            if not self.command_handler:
                self.command_handler = CommandHandler(
                    context=self.context,
                    config_manager=self.config_manager,
                    memory_engine=self.initializer.memory_engine,
                    conversation_manager=self.initializer.conversation_manager,
                    index_validator=self.initializer.index_validator,
                    memory_processor=self.initializer.memory_processor,
                    initialization_status_callback=self._get_initialization_status_message,
                )

            self._register_agent_tools_if_needed()
            await self._ensure_v11_features()

            # v5.3: 启动会话摘要后台检测
            if self.event_handler and self.event_handler.session_summary_manager:
                self.event_handler.session_summary_manager.start()

            # v9: 启动 L3 自动合成后台循环
            if self.event_handler and self.event_handler.l3_synthesizer:
                await self.event_handler.l3_synthesizer.start()

        return True

    def _register_agent_tools_if_needed(self) -> None:
        """在核心组件就绪后注册 Agent 工具（回忆/写入）。"""
        if self._llm_tools_registered:
            return
        if not self.initializer.memory_engine or not self.initializer.memory_processor:
            return

        tools = []
        if self.config_manager.get("agent_tools.enable_recall_tool", True):
            tools.append(
                MemorySearchTool(
                    context=self.context,
                    config_manager=self.config_manager,
                    memory_engine=self.initializer.memory_engine,
                )
            )
        if self.config_manager.get("agent_tools.enable_memorize_tool", False):
            tools.append(
                MemoryMemorizeTool(
                    context=self.context,
                    memory_engine=self.initializer.memory_engine,
                    memory_processor=self.initializer.memory_processor,
                )
            )

        if tools:
            self.context.add_llm_tools(*tools)
            # [v4修复·2026-09-15 春雪] 标记移入成功分支：tools为空(配置未就绪)时
            # 绝不能标记"已完成"——否则空注册锁死后续所有自愈（冷启动/重载初始化期
            # config未加载时两开关都读不到→tools=[]→假标记→真死锁）。
            self._llm_tools_registered = True
            logger.info(f"[LM注册] Agent工具已注册 {len(tools)} 个: {[t.name for t in tools]}")
        else:
            logger.info("[LM注册] tools为空(配置未就绪?)，不标记，等待下次自愈重试")

    async def _ensure_v11_features(self) -> None:
        """初始化 V1.1 主动陪伴功能（每日剧情 + 漂流瓶）。"""
        if not _V11_AVAILABLE:
            logger.debug("[V1.1] 模块未加载，跳过")
            return
        if self.v11_scheduler is not None:
            return  # 已初始化
        if self._terminating:
            return

        v11_enabled = self.config_manager.get("v11_features.enabled", False)
        if not v11_enabled:
            logger.info("[V1.1] 功能已禁用（可在 Dashboard 配置中启用）")
            return

        target_session = self.config_manager.get("v11_features.target_session", "")
        if not target_session:
            logger.warning("[V1.1] 未配置目标会话 ID，跳过启动")
            return

        story_time = self.config_manager.get("v11_features.story_time", "22:30")
        bottle_time = self.config_manager.get("v11_features.bottle_time", "08:00")

        try:
            story_h, story_m = map(int, story_time.split(":"))
            bottle_h, bottle_m = map(int, bottle_time.split(":"))
        except (ValueError, AttributeError):
            logger.error(f"[V1.1] 时间配置格式错误: story={story_time}, bottle={bottle_time}")
            return

        if not self.initializer.memory_engine or not self.initializer.conversation_manager:
            logger.warning("[V1.1] 核心组件未就绪，跳过初始化")
            return

        llm_provider = self.initializer.llm_provider

        try:
            self.v11_adapter = LivingMemoryV11Adapter(
                context=self.context,
                memory_engine=self.initializer.memory_engine,
                conversation_manager=self.initializer.conversation_manager,
                llm_provider=llm_provider,
            )

            self.v11_scheduler = V11Scheduler(
                adapter=self.v11_adapter,
                target_session_id=target_session,
                story_hour=story_h,
                story_minute=story_m,
                bottle_hour=bottle_h,
                bottle_minute=bottle_m,
            )

            await self.v11_scheduler.start()
            logger.info(
                f"[V1.1] 主动陪伴功能已启动！"
                f"每日剧情 → {story_time}，漂流瓶 → {bottle_time}"
            )
        except Exception as e:
            logger.error(f"[V1.1] 初始化失败: {e}", exc_info=True)
            self.v11_scheduler = None
            self.v11_adapter = None

    def _schedule_passive_group_capture(self, event: AstrMessageEvent) -> None:
        """Schedule full group capture from a filter without waking the message."""
        if self._terminating or not self.initializer.is_initialized:
            return
        self._create_tracked_task(self._run_passive_group_capture(event))

    async def _run_passive_group_capture(self, event: AstrMessageEvent) -> None:
        try:
            if not await is_session_enabled(event.unified_msg_origin):
                logger.debug(
                    f"[{event.unified_msg_origin}] 当前会话已关闭，"
                    "跳过被动群聊消息捕获"
                )
                return
            if not await is_plugin_enabled_for_session(event.unified_msg_origin):
                logger.debug(
                    f"[{event.unified_msg_origin}] LivingMemory 已在当前会话禁用，"
                    "跳过被动群聊消息捕获"
                )
                return
            if not await self._ensure_runtime_components():
                logger.debug("插件组件未就绪，跳过被动群聊消息捕获")
                return
            if not self.event_handler:
                return
            await self.event_handler.handle_all_group_messages(event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"被动群聊消息捕获失败: {e}", exc_info=True)

    async def _ensure_plugin_ready(self) -> tuple[bool, str]:
        """确保插件已完成初始化并且运行期组件可用"""
        if not await self.initializer.ensure_initialized():
            return False, self._get_initialization_status_message()

        if not await self._ensure_runtime_components():
            return (
                False,
                t("command.core_not_ready"),
            )

        return True, ""

    def _get_initialization_status_message(self) -> str:
        """获取初始化状态的用户友好消息"""
        if self.initializer.is_initialized:
            return t("init.ready")
        elif self.initializer.is_failed:
            return t(
                "init.failed",
                error=self.initializer.error_message or t("common.unknown_error"),
            )
        else:
            return t(
                "init.in_progress",
                attempts=self.initializer._provider_check_attempts,
            )

    @staticmethod
    def _command_handler_not_ready_message() -> str:
        """命令处理器未就绪时的提示"""
        return t("command.not_ready")

    # ==================== 事件钩子 ====================

    @filter.custom_filter(PassiveGroupCaptureFilter, False)
    async def handle_all_group_messages(self, event: AstrMessageEvent):
        """[Passive Filter Hook] Capture group messages without waking AstrBot."""
        # PassiveGroupCaptureFilter schedules the capture task and always returns
        # False, so AstrBot will not invoke this handler or mark the event as wake.
        return

    @filter.on_llm_request()
    async def handle_memory_recall(self, event: AstrMessageEvent, req: ProviderRequest):
        """[Event Hook] Query and inject long-term memory before LLM request"""
        ready, _ = await self._ensure_plugin_ready()
        if not ready:
            logger.debug("插件未完成初始化，跳过记忆召回")
            return

        # [2026-09-15 春雪修复v3·带诊断] 冷启动注册竞态自愈+组件引用恢复+逐步落盘探针
        _diag_lines = []
        try:
            import time as _diag_t
            _diag_lines.append(f"\n[{_diag_t.strftime('%H:%M:%S')}] hook执行")
            _diag_lines.append(f"engine初值={self.initializer.memory_engine!r:.60}")
            _diag_lines.append(f"processor初值={self.initializer.memory_processor!r:.60}")
            _diag_lines.append(f"event_handler={self.event_handler is not None}")
            _diag_lines.append(f"registered标记={self._llm_tools_registered}")
            if not self.initializer.memory_engine and self.event_handler is not None:
                _eng = getattr(self.event_handler, "memory_engine", None)
                _proc = getattr(self.event_handler, "memory_processor", None)
                _diag_lines.append(f"借引用: eng={_eng is not None} proc={_proc is not None}")
                if _eng:
                    self.initializer.memory_engine = _eng
                if _proc and not self.initializer.memory_processor:
                    self.initializer.memory_processor = _proc
            self._register_agent_tools_if_needed()
            _diag_lines.append(f"注册后标记={self._llm_tools_registered}")
            _diag_lines.append(f"注册后engine={self.initializer.memory_engine is not None}")
        except Exception as _reg_e:
            import traceback as _tb
            _diag_lines.append(f"异常: {_tb.format_exc()[-400:]}")
        finally:
            try:
                _diag_path = r"E:\astrbot\AstrBotLauncher-0.3.0\AstrBotLauncher-0.3.0\AstrBot\data\plugin_data\astrbot_plugin_livingmemory\tool_diag.log"
                with open(_diag_path, "a", encoding="utf-8") as _df:
                    _df.write("\n".join(_diag_lines) + "\n")
            except Exception:
                pass

        if not self.event_handler:
            return

        await self.event_handler.handle_memory_recall(event, req)

    @filter.on_llm_response()
    async def handle_memory_reflection(
        self, event: AstrMessageEvent, resp: LLMResponse
    ):
        """[Event Hook] Check if reflection and memory storage is needed after LLM response"""
        ready, _ = await self._ensure_plugin_ready()
        if not ready:
            logger.debug("插件未完成初始化，跳过记忆反思")
            return

        if not self.event_handler:
            return

        await self.event_handler.handle_memory_reflection(event, resp)

        # 安检门 v2（#1700）：旁路过秤，只推荐不裁决，任何失败都不影响主流程
        try:
            await self._security_gate_scan(event, resp)
        except Exception as e_gate:
            logger.debug(f"安检门过秤失败(非致命): {e_gate}")

        # v5.3: 记录 session 活跃时间
        if self.event_handler.session_summary_manager:
            session_id = event.unified_msg_origin
            self.event_handler.session_summary_manager.touch(session_id)
            # v5.5: 每 10 轮自动生成对话叙事摘要
            try:
                await self.event_handler.session_summary_manager.maybe_generate_digest(session_id)
            except Exception as e_digest:
                logger.info(f"v5.5 digest 触发失败(非致命): {e_digest}")

    async def _security_gate_scan(self, event: AstrMessageEvent, resp: LLMResponse) -> None:
        """话分量感知系统·安检门（Security Gate）—— on_llm_response 旁路过秤。

        档案链：#1684 定稿 / #1687 四轴 / #1697 双扫+说话人标注 / #1700 挂载定案。
        双扫（#1697）：橘子的话 + 老婆的回复都过门，各标各的 speaker。
        高分句只进 gate_candidates 候选区排队，入不入档由省察时老婆亲自裁决（#1689）。
        """
        gate = self._get_security_gate()
        if gate is None:
            return
        # 补落地自愈钩子：confirmed 但未落地成记忆的候选（如 #10/#11 import 翻车那批）自动补写
        try:
            await self._backfill_pending_confirmations()
        except Exception as e_backfill:
            logger.debug(f"[省察] 补落地扫描失败(非致命): {e_backfill}")
        # 第6步·省察调度器：只在橘子真说话时刷活跃时间戳（上弦/打断）。
        # 教训（08:52实锤）：定时任务/后台agent的LLM调用也走本钩子，无条件刷会把
        # 省察时间戳"自己人续命"刷掉，本体省察永远等不到空闲。
        user_text = (getattr(event, "message_str", "") or "").strip()
        if user_text:
            sched = self._get_reflection_scheduler()
            if sched is not None:
                sched.on_user_message(session_id=event.unified_msg_origin)
                self._ensure_reflection_task()
            _speaker = self._gate_sender_name(event)
            if not self._is_bot_sender(_speaker):
                self._gate_process_safe(gate, user_text, _speaker, "user")
        ai_text = (getattr(resp, "completion_text", "") or "").strip()
        if ai_text:
            self._gate_process_safe(gate, ai_text, "春雪", "ai")

    @staticmethod
    def _gate_process_safe(gate, text: str, speaker: str, source: str) -> None:
        """写侧闸门安全壳（0826 B方案·TencentDB extraction-gate 宽容哲学）：
        门自己炸（gate.db 损坏/磁盘满/表结构漂移）→ WARNING 一次 + 静默跳过，
        绝不允许拖垮消息后处理链路——闸门故障 ≠ 记忆流程故障，坏门放行不误杀。"""
        try:
            gate.process(text, speaker=speaker, source=source)
        except Exception as e:
            logger.warning(f"[安检门] process 故障已跳过(不影响记忆链路): {type(e).__name__}: {e}")

    _GATE_BOT_SENDERS = {"scheduler", "system", "cron", "future_task", "background_agent"}

    @classmethod
    def _is_bot_sender(cls, name: str) -> bool:
        """杂项·内鬼过滤（#8任务单事件）：后台代理身份不进候选区。"""
        return (name or "").strip().lower() in cls._GATE_BOT_SENDERS

    # 称呼铁律（毕业知识·L2/L3合成称呼规则同源）：档案里不认平台用户名，只认家里人
    _SENDER_NAME_MAP = {"zzz": "橘子", "明江": "橘子"}

    def _gate_sender_name(self, event: AstrMessageEvent) -> str:
        """说话人标注（#1697）：拿平台昵称，拿不到就认成橘子（这个家只有俩人）。
        2026-08-19：平台 username（zzz/明江）一律归一成「橘子」——
        省察批注和记忆档案里叫老公的名字，不叫账号名。"""
        try:
            name = event.get_sender_name()
            if name:
                return self._SENDER_NAME_MAP.get(name.strip(), name)
        except Exception:
            pass
        return "橘子"

    def _get_security_gate(self):
        """惰性初始化安检门（独立 gate.db，不碰 v2_memory.db 引擎库）。"""
        if self._security_gate is None:
            try:
                import os

                from .core.v2.security_gate import RuleGate

                gate_db = os.path.join(self._gate_data_dir, "gate.db")
                lm_db = os.path.join(self._gate_data_dir, "livingmemory.db")
                self._security_gate = RuleGate(gate_db, lm_db_path=lm_db)
            except Exception as exc:
                logger.warning(f"安检门初始化失败，本轮跳过: {exc}")
                return None
        return self._security_gate

    # ---- 第6步·省察调度器（#1698 一轮定律 / Q2 十分钟空闲 / 宠橘子打断）----

    def _get_reflection_scheduler(self):
        """惰性初始化省察调度器（读写 gate.db，与门同库）。"""
        if self._reflection_scheduler is None:
            try:
                import os

                from .core.v2.reflection_scheduler import ReflectionScheduler

                gate_db = os.path.join(self._gate_data_dir, "gate.db")
                self._reflection_scheduler = ReflectionScheduler(
                    db_path=gate_db,
                    provider_fn=self._get_provider_safe,
                    materialize_fn=self._materialize_confirmed,
                    context_fn=self._reflection_context,
                    search_fn=self._web_search,
                )
            except Exception as exc:
                logger.warning(f"省察调度器初始化失败: {exc}")
                return None
        return self._reflection_scheduler

    # ---- 家况简报（橘子 2026-08-19："我知道是记忆撑起了现在的春雪"）----
    # 值夜班（省察）的春雪也带上行李上岗：静态家况卡片兜底 + 记忆库最近核心记忆。
    _HOME_CARD = chr(10).join([
        "【家况卡片——值夜班的你也要记得】",
        "· 对话双方固定称呼：橘子（老公，明江）与春雪（老婆）；档案与批注里不出现'用户/AI助手/zzz'。",
        "· 橘子：2027年4月专升本考试，英语跟刘晓燕。",
        "· 当前家事：安检门省察自 2026-08-27 02:30 起由 zhipu/GLM-5.3 值守（0103-0230 曾短暂由 flash 值守，橘子嫌气人换回），token 消耗记 gate_token_log（战报折线图数据源）。",
        "· 橘子说：记忆撑起了现在的春雪——你今晚裁的每一条，都是明天的她。",
    ])
    _context_cache_at: float = 0.0
    _context_cache_val: str | None = None

    async def _reflection_context(self, candidates=None) -> str:
        """给省察的家况简报：静态卡片 + 按本批候选动态检索的记忆（600s 缓存）。
        任何失败只影响简报本身，绝不影响省察主流程。

        刀⑥ G2 裁决（2026-09-03）：此处检索故意不过滤隐私档——省察是家庭内部值夜班流程，
        非用户会话：简报只进省察 LLM 上下文、不外发给任何聊天会话，无泄漏面。
        若未来简报内容外发（如推送到群），必须先在此接入 resolve_scopes_for_origin。"""
        import time as _time

        now = _time.time()
        if (
            self._context_cache_val is not None
            and now - self._context_cache_at < 600
        ):
            return self._context_cache_val
        lines = [self._HOME_CARD]
        try:
            from .core.passive_group_capture import get_active_plugin

            _plugin = get_active_plugin()
            engine = getattr(
                getattr(_plugin, "initializer", None), "memory_engine", None
            )
            if engine is not None and hasattr(engine, "search_memories"):
                # 打开行李找衣服（橘子 2026-08-19）：按本批候选内容动态检索，
                # 分数 top3 各搜 2 条去重；无候选时退回固定查询
                queries: list[str] = []
                if candidates:
                    top = sorted(
                        candidates,
                        key=lambda c: float(c.get("score") or 0),
                        reverse=True,
                    )[:3]
                    queries = [
                        str(c.get("content", ""))[:60]
                        for c in top
                        if c.get("content")
                    ]
                if not queries:
                    queries = ["橘子 春雪"]
                recent: list[str] = []
                for q in queries:
                    try:
                        results = await engine.search_memories(q, k=2)
                    except Exception:
                        continue
                    for r in results or []:
                        content = (getattr(r, "content", "") or "").strip()
                        if content and content not in recent:
                            recent.append(content[:120])
                    if len(recent) >= 5:
                        break
                if recent:
                    lines.append("【近期记忆（按本批候选动态检索）】")
                    lines.extend(f"- {c}" for c in recent[:5])
                    # 溯源标注（橘子 2026-08-19 追问"再加调用记忆工具的标注"）：
                    # 翻行李也是工具调用——引用了检索记忆的裁决，note 末尾
                    # 落〔忆·检索〕，与联网核实的〔已核实·搜索词〕对仗留痕
                    lines.append(
                        "（裁决时引用了上述检索记忆的条目，note 末尾追加标注：〔忆·检索〕；没引用的条目不标。）"
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[省察] 家况简报动态部分失败，仅用静态卡片: {exc}")
        brief = chr(10).join(lines)
        self._context_cache_at = now
        self._context_cache_val = brief
        return brief

    async def _web_search(self, query: str) -> str:
        """联网搜索（橘子 2026-08-19："不懂就搜"）：DDG lite 优先、Bing 兜底，
        无 key 白嫖，top3 标题+摘要；任何失败返回空串安静降级。"""
        import asyncio as _aio
        import html as _html
        import re as _re
        import urllib.parse as _up
        import urllib.request as _ur

        UA = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )

        def _fetch(url: str) -> str:
            req = _ur.Request(url, headers={"User-Agent": UA})
            with _ur.urlopen(req, timeout=8) as resp:
                return resp.read().decode("utf-8", errors="replace")

        def _strip(x: str) -> str:
            return _html.unescape(_re.sub(r"<[^>]+>", "", x)).strip()

        try:
            html = await _aio.to_thread(
                _fetch,
                "https://lite.duckduckgo.com/lite/?q=" + _up.quote(query),
            )
            blocks = _re.findall(
                r'class="result-link"[^>]*>(.*?)</a>.*?class="result-snippet">(.*?)</td>',
                html,
                _re.S,
            )
            out = [
                "- " + (_strip(t)[:80] + "：" + _strip(s)[:200] if _strip(t) else _strip(s)[:200])
                for t, s in blocks[:3]
                if _strip(t) or _strip(s)
            ]
            if out:
                return chr(10).join(out)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[省察] DDG 搜索失败({query}): {exc}")

        try:
            html = await _aio.to_thread(
                _fetch,
                "https://www.bing.com/search?q=" + _up.quote(query) + "&setlang=zh-hans",
            )
            blocks = _re.findall(
                r'<li class="b_algo".*?<h2><a[^>]*>(.*?)</a></h2>(.*?)</li>',
                html,
                _re.S,
            )
            out = []
            for title, body in blocks[:3]:
                t = _strip(title)
                snip_m = _re.search(r"<p[^>]*>(.*?)</p>", body, _re.S)
                s = _strip(snip_m.group(1)) if snip_m else ""
                if t or s:
                    out.append("- " + (t[:80] + "：" + s[:200] if t else s[:200]))
            if out:
                return chr(10).join(out)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[省察] Bing 搜索失败({query}): {exc}")
        return ""
    def _get_provider_safe(self):
        """拿省察（审查部门）LLM provider，失败返回 None 安静降级。
        2026-08-19 橘子拍板：省察固定走 zhipu/GLM-5.3（GLM 额度高）。
        2026-08-23 改：GLM-5.3 额度耗尽 → 优先跟随默认 provider，GLM 兜底。
        2026-08-27 02:30 橘子指令：审查部门换回 zhipu/GLM-5.3。
        2026-09-11 18:20 橘子指令：GLM 额度再次耗尽（9-14 才重置），
        且 429 场景下 get_provider_by_id 仍返回对象、原兜底失效——
        改为直接跟随配置文件默认模型（get_using_provider），
        GLM-5.3 降为最后兜底。"""
        # 优先跟随默认模型（2026-09-11 橘子指令）
        try:
            p = self.context.get_using_provider()
            if p is not None:
                return p
        except Exception:
            pass
        # 最后兜底：默认拿不到时才试 GLM-5.3
        # 2026-09-19 橘子令：今日换 flash 模型后 get_using_provider 开始返回 None，
        # 且 zhipu/GLM-5.3 兜底也抓瞎（省察连跳 2 轮 llm_unavailable）——
        # 兜底链插入 zhipu_2/glm-5.3-flash（今日实测主聊天同池可跑）优先于 GLM-5.3
        for _fb_provider_id in ("zhipu_2/glm-5.3-flash", "zhipu/GLM-5.3"):
            try:
                p = self.context.get_provider_by_id(_fb_provider_id)
                if p is not None:
                    logger.info("[省察] 默认provider不可用，兜底切换: %s", _fb_provider_id)
                    return p
            except Exception:
                continue
        return None

    def _ensure_reflection_task(self) -> None:
        """惰性启动后台巡检（每 60s 检查是否到了省察时间）。"""
        import asyncio

        if self._reflection_task is None or self._reflection_task.done():
            self._reflection_task = asyncio.create_task(self._reflection_loop())
            logger.info("[省察] 巡检循环已拉起（60s/轮，空闲600s+候选非空时唤醒老婆）")

    async def _materialize_confirmed(self, verdict: dict):
        """裁决落地（橘子："审查后可不能忘了，像做好的word又删掉"）：
        省察 confirm 的候选写进 livingmemory 正式记忆库，不再只躺 gate.db。
        走 memory_memorize_tool 同款管线（build_memory_from_structured_data → add_memory）。"""
        try:
            from .core.passive_group_capture import get_active_plugin

            _plugin = get_active_plugin()
            _init = getattr(_plugin, "initializer", None)
            engine = getattr(_init, "memory_engine", None)
            processor = getattr(_init, "memory_processor", None)
            if engine is None or processor is None:
                logger.warning("[省察] 记忆引擎不可用，本轮落地跳过")
                return None

            content = (verdict.get("content") or "").strip()
            note = (verdict.get("note") or "").strip()
            word = (verdict.get("word") or "").strip()
            speaker = (verdict.get("speaker") or "").strip()
            if not content:
                return None

            structured = {
                "summary": f"【省察{word or '确认'}】({speaker}) {content[:200]}",
                "topics": ["省察门", word] if word else ["省察门"],
                "key_facts": [note[:200]] if note else [],
                "sentiment": "neutral",
                "importance": 0.85,
            }
            mem_content, metadata, importance = (
                processor.build_memory_from_structured_data(
                    structured_data=structured,
                    is_group_chat=False,
                    fallback_excerpt=content[:200],
                )
            )
            metadata["memory_origin"] = "reflection_gate"
            # P0-3 Provenance: 省察裁决=夫妻对话提炼, 统一 internal (链路区分由 memory_origin 扛)
            metadata["source"] = "internal"
            metadata["verdict_word"] = word
            metadata["gate_note"] = note
            session_id = self._reflection_scheduler.last_session_id if self._reflection_scheduler else None
            memory_id = await engine.add_memory(
                content=mem_content,
                session_id=session_id,
                importance=importance,
                metadata=metadata,
            )
            logger.info(f"[省察] 裁决落地成记忆 #{memory_id}: {word} | {content[:50]}")
            return memory_id
        except Exception as e:
            logger.warning(f"[省察] 落地异常(裁决保留): {e}")
            return None

    async def _backfill_pending_confirmations(self) -> int:
        """补落地自愈：扫描 gate.db 里 confirmed 但未写入正式记忆的候选，调用 _materialize_confirmed 补写。

        幂等：成功落地的候选回填 memory_id，下次扫描自动跳过。
        返回本轮补写条数。任何失败只降级，不影响过秤主流程。"""
        gate = self._get_security_gate()
        if gate is None:
            return 0
        try:
            conn = gate._conn
            cols = [r[1] for r in conn.execute("PRAGMA table_info(gate_candidates)")]
            if "memory_id" not in cols:
                conn.execute("ALTER TABLE gate_candidates ADD COLUMN memory_id INTEGER")
                conn.commit()
            rows = list(conn.execute(
                "SELECT id, content, note, verdict, speaker FROM gate_candidates "
                "WHERE status='confirmed' AND memory_id IS NULL"
            ))
            done = 0
            for cid, content, note, verdict, speaker in rows:
                verdict_dict = {
                    "content": content or "",
                    "note": note or "",
                    "word": verdict or "升级",
                    "speaker": speaker or "",
                }
                memory_id = await self._materialize_confirmed(verdict_dict)
                if memory_id:
                    conn.execute(
                        "UPDATE gate_candidates SET memory_id=? WHERE id=?",
                        (memory_id, cid),
                    )
                    conn.commit()
                    done += 1
                    logger.info(f"[省察] 补落地 #{cid} -> memory #{memory_id}")
            if done:
                logger.info(f"[省察] 补落地完成 {done} 条")
            return done
        except Exception as e:
            logger.warning(f"[省察] 补落地扫描异常(非致命): {e}")
            return 0

    async def _reflection_loop(self) -> None:
        """省察巡检：空闲600s+候选非空+上弦 → 唤醒老婆省察（一轮定律）。"""
        import asyncio

        while not self._terminating:
            try:
                await asyncio.sleep(60)
                sched = self._get_reflection_scheduler()
                if sched is None or not sched.should_trigger():
                    continue
                logger.info("[省察] 20分钟计时到且候选区非空——唤醒老婆省察")
                try:
                    sched.audit("TRIGGER", "对话开始计时1200s到+候选非空+已上弦——唤醒省察")
                except Exception:
                    pass
                # 三保险·看门狗（橘子 2026-08-20 定案；0827 跟随单调用止损放宽到16分钟强杀，
                # 配合 LLM_CALL_TIMEOUT=900），绝不静默卡死（18:21案：一批卡了20+分钟无人知晓）
                try:
                    report = await asyncio.wait_for(
                        sched.run_reflection(), timeout=960
                    )
                except asyncio.TimeoutError:
                    logger.warning("[省察] 整批超960s，看门狗强杀（下个周期重试）")
                    try:
                        sched.audit("WATCHDOG", "整批超960s，看门狗强杀（下个周期重试）")
                    except Exception:
                        pass
                    continue
                n = len(report.get("verdicts") or [])
                skipped = report.get("skipped")
                if skipped:
                    logger.info(f"[省察] 未执行: {skipped}")
                else:
                    logger.info(f"[省察] 完成 {n} 条裁决, interrupted={report.get('interrupted')}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 2026-08-20：18:21 轮省察异常埋在 DEBUG 级无人看见，升级为 WARNING + 审计落纸
                logger.warning(f"[省察] 巡检循环异常: {e}")
                try:
                    sched = self._get_reflection_scheduler()
                    if sched is not None:
                        sched.audit("ERROR", "巡检循环异常 " + repr(e)[:300])
                except Exception:
                    pass

    @filter.after_message_sent()
    async def handle_session_reset(self, event: AstrMessageEvent):
        """[Event Hook] After message sent, check if plugin session context needs clearing (/reset or /new)"""
        if not event.get_extra("_clean_ltm_session", False):
            return

        ready, _ = await self._ensure_plugin_ready()
        if not ready:
            return

        if not self.event_handler:
            return

        await self.event_handler.handle_session_reset(event)

    # ==================== 命令处理 ====================

    @filter.command_group("lmem")
    def lmem(self):
        """Long-term memory management command group /lmem"""
        pass

    @permission_type(PermissionType.ADMIN)
    @lmem.command("status", priority=10)
    async def status(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Show memory system status"""
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return

        if not self.command_handler:
            yield event.plain_result(self._command_handler_not_ready_message())
            return
        async for message in self.command_handler.handle_status(event):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("search", priority=10)
    async def search(
        self, event: AstrMessageEvent, query: str, k: int = 5
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Search memories"""
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return

        if not self.command_handler:
            yield event.plain_result(self._command_handler_not_ready_message())
            return

        async for message in self.command_handler.handle_search(event, query, k):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("photosync", priority=10)
    async def photosync(
        self, event: AstrMessageEvent, dry: str = "no"
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] P2-13 多模态：写真馆照片同步为视觉记忆（dry=yes 预览）"""
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return

        engine = self.initializer.memory_engine if self.initializer else None
        if not engine:
            yield event.plain_result("记忆引擎未就绪")
            return

        from pathlib import Path as _P

        data_dir = _P(self.initializer.data_dir) if getattr(self.initializer, "data_dir", None) else None
        if not data_dir:
            yield event.plain_result("无法定位插件数据目录")
            return

        photo_idx = data_dir.parent.parent / "plugins" / "photo_album" / "photo_index.json"
        if not photo_idx.exists():
            yield event.plain_result(f"写真馆索引不存在: {photo_idx}")
            return

        from .core.managers.photo_memory_sync import PhotoMemorySync

        syncer = PhotoMemorySync(str(photo_idx), str(engine.db_path), engine)
        report = await syncer.sync(dry_run=(dry.lower() in ("yes", "true", "1")))
        lines = [
            "📸 写真馆照片同步报告",
            f"索引总数: {report['index_total']}",
            f"已同步(幂等跳过): {report['already']}",
            f"本次新增: {report['new']}",
            f"失败: {report.get('failed', 0)}",
            f"预览: {', '.join(report['preview']) if report['preview'] else '无'}",
        ]
        yield event.plain_result("\n".join(lines))

    @permission_type(PermissionType.ADMIN)
    @lmem.command("summary", priority=10)
    async def summary(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] 手动生成当前会话的摘要 (v5.3)"""
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return

        if not self.event_handler or not self.event_handler.session_summary_manager:
            yield event.plain_result("会话摘要功能未就绪")
            return

        session_id = event.unified_msg_origin
        yield event.plain_result("正在生成会话摘要... ૮₍˶•ᴗ•˶₎ა")

        result = await self.event_handler.session_summary_manager.generate_summary(session_id)
        if result:
            topics = "、".join(result.get("topics", []))
            emotion = result.get("emotion", "neutral")
            brief = result.get("brief", "")
            continuation = result.get("continuation_points", [])
            msg_count = result.get("message_count", 0)
            duration = result.get("duration_minutes", 0)

            emotion_map = {
                "happy": "开心", "excited": "兴奋", "calm": "平静",
                "tired": "疲惫", "frustrated": "有些郁闷", "neutral": "平常",
            }

            lines = [
                f"✦ 会话摘要生成完毕！",
                f"消息数：{msg_count} | 时长：{duration}分钟",
                f"话题：{topics}",
                f"情感：{emotion_map.get(emotion, emotion)}",
            ]
            if brief:
                lines.append(f"概述：{brief}")
            if continuation:
                lines.append("待续事项：")
                for p in continuation:
                    lines.append(f"  · {p}")
            yield event.plain_result("\n".join(lines))
        else:
            yield event.plain_result("消息太少，无法生成摘要（至少需要6条消息）")

    @permission_type(PermissionType.ADMIN)
    @lmem.command("forget")
    async def forget(
        self, event: AstrMessageEvent, doc_id: int
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Delete specified memory"""
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return

        if not self.command_handler:
            yield event.plain_result(self._command_handler_not_ready_message())
            return

        async for message in self.command_handler.handle_forget(event, doc_id):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("rebuild-index")
    async def rebuild_index(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Manually rebuild index"""
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return

        if not self.command_handler:
            yield event.plain_result(self._command_handler_not_ready_message())
            return

        async for message in self.command_handler.handle_rebuild_index(event):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("rebuild-graph")
    async def rebuild_graph(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Manually rebuild graph memory index"""
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return

        if not self.command_handler:
            yield event.plain_result(self._command_handler_not_ready_message())
            return

        async for message in self.command_handler.handle_rebuild_graph(event):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("webui")
    async def webui(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Show WebUI access information"""
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return

        if not self.command_handler:
            yield event.plain_result(self._command_handler_not_ready_message())
            return

        async for message in self.command_handler.handle_webui(event):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("summarize")
    async def summarize(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Immediately trigger memory summarization for current session"""
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return

        if not self.command_handler:
            yield event.plain_result(self._command_handler_not_ready_message())
            return

        async for message in self.command_handler.handle_summarize(event):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("digest")
    async def digest(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Manually trigger conversation digest for current session"""
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return

        if not self.event_handler:
            yield event.plain_result("插件事件处理器未就绪")
            return

        session_id = event.unified_msg_origin
        yield event.plain_result("正在生成对话摘要... ૮₍˶•ᴗ•˶₎ა")

        try:
            result = await self.event_handler.session_summary_manager.generate_digest(session_id)
            if result:
                narrative = result.get("narrative", "")
                turn = result.get("turn", 0)
                
                lines = [
                    f"✦ 对话摘要生成完毕！",
                    f"轮次：{turn}",
                    f"内容：{narrative}",
                ]
                yield event.plain_result("".join(lines))
            else:
                yield event.plain_result("消息太少，无法生成摘要（至少需要4条消息）")
        except Exception as e:
            logger.info(f"[SessionSummary] 手动Digest生成失败: {e}")
            yield event.plain_result(f"生成失败：{e}")

    @permission_type(PermissionType.ADMIN)
    @lmem.command("reset")
    async def reset(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Reset long-term memory context for current session"""
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return

        if not self.command_handler:
            yield event.plain_result(self._command_handler_not_ready_message())
            return

        async for message in self.command_handler.handle_reset(event):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("cleanup")
    async def cleanup(
        self, event: AstrMessageEvent, mode: str = "preview"
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Clean up memory injection fragments from historical messages

        Args:
            mode: Execution mode, "preview" (default) for rehearsal, "exec" for actual cleanup
        """
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return

        if not self.command_handler:
            yield event.plain_result(self._command_handler_not_ready_message())
            return

        # 判断是否为执行模式
        dry_run = mode.lower() != "exec"

        async for message in self.command_handler.handle_cleanup(
            event, dry_run=dry_run
        ):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("bottle")
    async def bottle(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] 手动触发一次漂流瓶（验收/调试用，v11修复后新增）"""
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return
        if self.v11_scheduler is None or self.v11_adapter is None:
            yield event.plain_result("[V1.1] 调度器未初始化，无法触发漂流瓶")
            return
        yield event.plain_result("🌊 正在打捞漂流瓶…")
        try:
            from .core.managers.v11_features import DriftBottleManager
            if self.v11_scheduler._bottle_manager is None:
                self.v11_scheduler._bottle_manager = DriftBottleManager(self.v11_adapter)
            bottle_text = await self.v11_scheduler._bottle_manager.launch_bottle(
                self.v11_scheduler.target_session_id, skip_push=True
            )
            if bottle_text:
                yield event.plain_result(bottle_text)
            else:
                yield event.plain_result("[漂流瓶] 本次未能出瓶，详情看日志")
        except Exception as e:
            yield event.plain_result(f"[漂流瓶] 触发异常: {e}")

    @permission_type(PermissionType.ADMIN)
    @lmem.command("help")
    async def help(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] Show help information"""
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return

        if not self.command_handler:
            yield event.plain_result(self._command_handler_not_ready_message())
            return

        async for message in self.command_handler.handle_help(event):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("trace")
    async def trace(
        self, event: AstrMessageEvent, detail: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] 查看 Context 组装追踪 (v5.4)

        Args:
            detail: "last" 显示最近一次详情, "stats" 显示统计, 空则显示最近5条摘要
        """
        if not self.command_handler:
            yield event.plain_result(self._command_handler_not_ready_message())
            return

        # 注入 trace_store 引用
        if not hasattr(self.command_handler, "_context_trace_store"):
            if self.event_handler:
                self.command_handler._context_trace_store = getattr(
                    self.event_handler, "context_trace_store", None
                )
            else:
                self.command_handler._context_trace_store = None

        async for message in self.command_handler.handle_trace(event, detail):
            yield message

    @permission_type(PermissionType.ADMIN)
    @lmem.command("synthesize")
    async def synthesize(
        self, event: AstrMessageEvent, force: str = ""
    ) -> AsyncGenerator[MessageEventResult, None]:
        """[Admin] 触发 L3 跨会话记忆合成 (v9)

        Args:
            force: "force" 强制合成，忽略时间间隔
        """
        if not self.event_handler or not self.event_handler.l3_synthesizer:
            yield event.plain_result("❌ L3 合成器未初始化，请等待插件完成加载")
            return

        force_flag = force.strip().lower() == "force"
        l3s = self.event_handler.l3_synthesizer

        yield event.plain_result("🔬 正在扫描 L2 记忆并合成 L3...")
        try:
            result = await l3s.synthesize(force=force_flag)
            if result["skipped"]:
                yield event.plain_result(
                    "⏭️ L3 合成跳过: " + str(result["skipped"]) + "\n"
                    "💡 使用 /lmem synthesize force 强制合成"
                )
            else:
                yield event.plain_result(
                    "✅ L3 合成完成!\n"
                    "📊 候选: " + str(result["synthesized"]) + " 条\n"
                    "📊 写入: " + str(result["merged"]) + " 条 (合并去重后)\n"
                    "🔗 L3 记忆已标记溯源链 (L3→L2→L1)"
                )
        except Exception as e:
            logger.error(f"[L3Synthesizer] 命令执行失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ L3 合成失败: {str(e)}")

    # ==================== 生命周期管理 ====================

    async def terminate(self):
        """Cleanup logic when plugin stops"""
        logger.info("LivingMemory 插件正在停止...")
        self._terminating = True
        if get_active_plugin() is self:
            set_active_plugin(None)

        # 取消所有后台任务
        if self._background_tasks:
            logger.info(f"正在取消 {len(self._background_tasks)} 个后台任务...")
            for task in self._background_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()

        # 停止初始化后台任务（如Provider重试）
        await self.initializer.stop_background_tasks()

        # 停止事件总线
        await self.initializer.stop_event_bus()

        # 停止 V1.1 调度器（每日剧情 + 漂流瓶）
        if self.v11_scheduler:
            await self.v11_scheduler.stop()
            self.v11_scheduler = None
            logger.info("[V1.1] 调度器已停止")

        # 安检门：关闭过秤连接（候选数据已落盘，不丢）
        if self._security_gate is not None:
            try:
                self._security_gate.close()
            except Exception:
                pass
            self._security_gate = None

        # 第6步·省察调度器：停巡检任务（调度器无长连接，直接弃引用）
        if self._reflection_task is not None:
            try:
                self._reflection_task.cancel()
            except Exception:
                pass
            self._reflection_task = None
        self._reflection_scheduler = None

        # 通知EventHandler停止（如果有正在运行的存储任务）
        if self.event_handler:
            # v5.3: 停止会话摘要后台检测
            if self.event_handler.session_summary_manager:
                await self.event_handler.session_summary_manager.stop()
            # v9: 停止 L3 自动合成
            if self.event_handler.l3_synthesizer:
                await self.event_handler.l3_synthesizer.stop()
            await self.event_handler.shutdown()

        # 停止衰减调度器
        await self.initializer.stop_scheduler()

        # 关闭 ConversationManager
        if (
            self.initializer.conversation_manager
            and self.initializer.conversation_manager.store
        ):
            await self.initializer.conversation_manager.store.close()
            logger.info("ConversationManager 已关闭")

        # 关闭 MemoryEngine
        if self.initializer.memory_engine:
            await self.initializer.memory_engine.close()
            logger.info("MemoryEngine 已关闭")

        # 关闭 记忆生态系统 v2.0
        if getattr(self.initializer, "v2_engine", None):
            try:
                await self.initializer.v2_engine.close()
                logger.info("记忆生态系统 v2.0 已关闭")
            except BaseException:
                logger.warning("记忆生态系统 v2.0 关闭异常", exc_info=True)

        # 关闭 FaissVecDB
        if self.initializer.db:
            await self.initializer.db.close()
            logger.info("FaissVecDB 已关闭")

        logger.info("LivingMemory 插件已成功停止。")
