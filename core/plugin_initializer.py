"""
插件初始化器
负责插件的初始化逻辑
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.provider.provider import EmbeddingProvider, Provider, RerankProvider

from ..storage.conversation_store import ConversationStore
from ..storage.db_migration import DBMigration
from .base.config_manager import ConfigManager
from .base.exceptions import InitializationError, ProviderNotReadyError
from .managers.conversation_manager import ConversationManager
from .managers.memory_engine import MemoryEngine
from .processors.memory_processor import MemoryProcessor
from .schedulers.decay_scheduler import DecayScheduler
from .validators.index_validator import IndexValidator

FaissVecDB: Any = None

# ── Faiss C++ fopen() 在 Windows 上使用 ANSI codepage ──
# Python 传给 Faiss 的路径是 UTF-8 字节，Windows fopen 期望 ANSI 编码，
# 含非 ASCII 字符的路径（如 C:\Users\<中文名>\...）被解读为乱码 →
# RuntimeError: could not open ... for reading: No such file or directory。
# 通过 monkey-patch faiss.read_index / write_index，经纯 ASCII 临时文件桥接。


def _needs_bridge(path: str) -> bool:
    """判断是否需要 ASCII 临时文件桥接。"""
    path = os.fspath(path)
    return os.name == "nt" and not path.isascii()


def _safe_temp_dir() -> str:
    """返回保证纯 ASCII 且可写的临时目录。"""
    if os.name == "nt":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        temp_dir = os.path.join(root, "Temp")
        if temp_dir.isascii() and os.path.isdir(temp_dir) and os.access(temp_dir, os.W_OK):
            return temp_dir
        tmp = tempfile.gettempdir()
        if tmp.isascii():
            return tmp
        raise OSError("_safe_temp_dir: 无法找到可写的纯 ASCII 临时目录")
    return tempfile.gettempdir()


def _make_temp_file(prefix: str) -> str:
    """创建 Faiss 桥接临时文件，返回纯 ASCII 路径。"""
    safe_dir = _safe_temp_dir()
    fd, path = tempfile.mkstemp(prefix=f"{prefix}_", suffix=".faiss", dir=safe_dir)
    os.close(fd)
    return path


def _sanitize_path(path: str) -> str:
    """脱敏路径：非 ASCII 部分替换为 [***]，避免日志泄露中文用户名。"""
    path = os.fspath(path)
    if path.isascii():
        return path
    parts: list[str] = []
    for ch in path:
        if ch.isascii():
            parts.append(ch)
        elif not parts or parts[-1] != "[***]":
            parts.append("[***]")
    return "".join(parts)


class PluginInitializer:
    """插件初始化器"""

    def __init__(self, context: Context, config_manager: ConfigManager, data_dir: str):
        """
        初始化插件初始化器

        Args:
            context: AstrBot上下文
            config_manager: 配置管理器
            data_dir: 插件数据目录路径
        """
        self.context = context
        self.config_manager = config_manager
        self.data_dir = data_dir

        # 组件实例
        self.embedding_provider: EmbeddingProvider | None = None
        self.rerank_provider: RerankProvider | None = None  # v3.5 精排模型（可空，检索自动降级）
        self.llm_provider: Provider | None = None
        self.db: Any | None = None
        self.graph_db: Any | None = None
        self.memory_engine: MemoryEngine | None = None
        self.v2_engine: Any | None = None  # 记忆生态系统 v2.0
        self._v2_backfill_task: Any | None = None  # 预言回溯循环任务
        self.memory_processor: MemoryProcessor | None = None
        self.db_migration: DBMigration | None = None
        self.conversation_manager: ConversationManager | None = None
        self.index_validator: IndexValidator | None = None
        self.decay_scheduler: DecayScheduler | None = None

        # 初始化状态
        self._initialization_complete = False
        self._initialization_lock = asyncio.Lock()
        self._initialization_failed = False
        self._initialization_error: str | None = None
        self._providers_ready = False
        self._provider_check_attempts = 0
        self._max_provider_attempts = 60
        self._retry_task: asyncio.Task | None = None

    async def initialize(self) -> bool:
        """
        执行初始化

        Returns:
            bool: 是否初始化成功
        """
        async with self._initialization_lock:
            if self._initialization_complete or self._initialization_failed:
                return self._initialization_complete

        logger.info("LivingMemory 插件开始后台初始化...")

        try:
            # 1. 等待 Provider 就绪
            if not await self._wait_for_providers_non_blocking():
                missing = []
                if not self.embedding_provider:
                    missing.append(
                        "Embedding Provider（请在 AstrBot 中配置向量嵌入模型）"
                    )
                if not self.llm_provider:
                    missing.append("LLM Provider（请在 AstrBot 中配置语言模型）")
                logger.warning(
                    f"以下 Provider 暂时不可用，将在后台继续尝试: {', '.join(missing)}"
                )
                self._start_retry_task_if_needed()
                return False

            # 2. Provider 就绪，继续完整初始化
            await self._complete_initialization()
            return True

        except Exception as e:
            logger.error(f"LivingMemory 插件初始化失败: {e}", exc_info=True)
            self._initialization_failed = True
            self._initialization_error = str(e)
            return False

    def _start_retry_task_if_needed(self) -> None:
        """启动后台重试任务（避免重复启动）"""
        if self._retry_task and not self._retry_task.done():
            return

        self._retry_task = asyncio.create_task(self._retry_initialization())
        self._retry_task.add_done_callback(self._on_retry_task_done)

    def _on_retry_task_done(self, task: asyncio.Task) -> None:
        """重试任务完成回调，回收状态并记录异常"""
        self._retry_task = None
        if task.cancelled():
            return
        try:
            exc = task.exception()
            if exc:
                logger.error(f"Provider 重试任务异常退出: {exc}")
        except Exception:
            # 防御性处理：读取 task.exception() 时不应阻断主流程
            pass

    async def _wait_for_providers_non_blocking(self, max_wait: float = 15.0) -> bool:
        """非阻塞地检查 Provider 是否可用"""
        start_time = time.time()
        check_interval = 1.0

        while time.time() - start_time < max_wait:
            self._initialize_providers(silent=True)

            if self.embedding_provider and self.llm_provider:
                logger.info(
                    "Provider check passed: embedding and llm providers are ready."
                )
                self._providers_ready = True
                return True

            await asyncio.sleep(check_interval)
            self._provider_check_attempts += 1

        logger.debug(
            f"Provider 在 {max_wait}秒内未就绪（已尝试 {self._provider_check_attempts} 次）"
            f"：embedding={'ready' if self.embedding_provider else 'not ready'}, "
            f"llm={'ready' if self.llm_provider else 'not ready'}"
        )
        return False

    async def _retry_initialization(self):
        """后台重试初始化任务（指数退避策略）"""
        base_interval = 2.0
        max_interval = 30.0
        current_interval = base_interval
        log_interval = 5

        while (
            not self._initialization_complete
            and not self._initialization_failed
            and self._provider_check_attempts < self._max_provider_attempts
        ):
            await asyncio.sleep(current_interval)

            self._initialize_providers(silent=True)
            self._provider_check_attempts += 1

            if self._provider_check_attempts % log_interval == 0:
                missing = []
                if not self.embedding_provider:
                    missing.append("Embedding Provider")
                if not self.llm_provider:
                    missing.append("LLM Provider")
                logger.info(
                    f"等待 Provider 就绪（未就绪: {', '.join(missing)}）..."
                    f"（已尝试 {self._provider_check_attempts}/{self._max_provider_attempts} 次，"
                    f"下次重试间隔 {current_interval:.1f}s）"
                )

            if self.embedding_provider and self.llm_provider:
                logger.info(
                    f"Provider 在第 {self._provider_check_attempts} 次尝试后就绪，继续初始化。"
                )
                self._providers_ready = True

                try:
                    async with self._initialization_lock:
                        if not self._initialization_complete:
                            await self._complete_initialization()
                except Exception as e:
                    logger.error(f"重试初始化失败: {e}", exc_info=True)
                    self._initialization_failed = True
                    self._initialization_error = str(e)
                break

            # 指数退避，最大30秒
            current_interval = min(current_interval * 1.5, max_interval)

        if not self._initialization_complete and not self._initialization_failed:
            missing = []
            if not self.embedding_provider:
                missing.append("Embedding Provider（请配置向量嵌入模型）")
            if not self.llm_provider:
                missing.append("LLM Provider（请配置语言模型）")
            logger.error(
                f"以下 Provider 在 {self._provider_check_attempts} 次尝试后仍未就绪，初始化失败: "
                f"{', '.join(missing) if missing else '未知'}"
            )
            self._initialization_failed = True
            self._initialization_error = (
                "Provider 初始化超时。"
                f"未就绪 Provider: {', '.join(missing) if missing else '未知'}。"
                "请检查 provider_settings 配置和 AstrBot 默认 Provider。"
            )

    def _initialize_providers(self, silent: bool = False):
        """初始化 Embedding 和 LLM provider"""
        # 初始化 Embedding Provider
        emb_id = self.config_manager.get("provider_settings.embedding_provider_id")
        if emb_id:
            provider = self._get_provider_by_id(emb_id, silent=silent)
            if provider and isinstance(provider, EmbeddingProvider):
                self.embedding_provider = provider
                if not silent:
                    logger.info(f"成功从配置加载 Embedding Provider: {emb_id}")
            elif provider and not silent:
                logger.warning(f"Provider {emb_id} 不是 EmbeddingProvider 类型")

        if not self.embedding_provider:
            embedding_providers = self.context.get_all_embedding_providers()
            if embedding_providers:
                self.embedding_provider = embedding_providers[0]
                if not silent:
                    provider_id = getattr(
                        self.embedding_provider.provider_config,
                        "id",
                        self.embedding_provider.provider_config.get("id", "unknown"),
                    )
                    logger.info(f"未指定 Embedding Provider，使用默认的: {provider_id}")
            else:
                self.embedding_provider = None
                if not silent:
                    logger.debug("没有可用的 Embedding Provider")

        # v3.5 初始化 Rerank Provider（可选：未配置则检索自动降级直通）
        rerank_id = self.config_manager.get("provider_settings.rerank_provider_id")
        if rerank_id:
            rp = self._get_provider_by_id(rerank_id, silent=silent)
            if rp and isinstance(rp, RerankProvider):
                self.rerank_provider = rp
                if not silent:
                    logger.info(f"v3.5 成功加载 Rerank Provider: {rerank_id}")
            elif rp and not silent:
                logger.warning(f"Provider {rerank_id} 不是 RerankProvider 类型")

        # 初始化 LLM Provider
        self.llm_provider = None
        llm_id = self.config_manager.get("provider_settings.llm_provider_id")
        if llm_id:
            provider = self._get_provider_by_id(llm_id, silent=silent)
            if provider and isinstance(provider, Provider):
                self.llm_provider = provider
                if not silent:
                    logger.info(f"成功从配置加载 LLM Provider: {llm_id}")
            elif provider and not silent:
                logger.warning(
                    f"Provider {llm_id} 不是聊天 Provider 类型，已忽略该配置。"
                )

        if not self.llm_provider:
            try:
                if silent and not self.context.get_all_providers():
                    self.llm_provider = None
                    return
                default_provider = self.context.get_using_provider()
                if default_provider and not isinstance(default_provider, Provider):
                    if not silent:
                        logger.warning(
                            "AstrBot 默认 Provider 类型不正确，期望聊天 Provider。"
                        )
                    self.llm_provider = None
                else:
                    self.llm_provider = default_provider
                if not silent and self.llm_provider:
                    logger.info("使用 AstrBot 当前默认的 LLM Provider。")
            except (ValueError, Exception) as e:
                if not silent:
                    logger.debug(f"获取默认 LLM Provider 失败: {e}")
                self.llm_provider = None

    def get_rerank_provider(self):
        """v3.5 惰性取当前 Rerank Provider（换模型即时生效）。"""
        return self.rerank_provider

    def _get_provider_by_id(self, provider_id: str, *, silent: bool):
        """静默检查阶段绕过会打印 warning 的 AstrBot 查询接口。"""
        if not provider_id:
            return None
        if not silent:
            return self.context.get_provider_by_id(provider_id)
        provider_manager = getattr(self.context, "provider_manager", None)
        inst_map = getattr(provider_manager, "inst_map", None)
        if isinstance(inst_map, dict):
            return inst_map.get(provider_id)
        return None

    def _check_faiss_runtime(self) -> None:
        # 2026-09-20 修复（橘子令）：启动高峰期 10s 超时过紧会误杀
        # （手动实测 import faiss 仅 1.67s，但插件初始化时系统繁忙子进程可超 10s）
        # 加固三层：超时放宽 45s + 一次重试 + 本进程导入兜底
        last_exc = None
        for attempt in (1, 2):
            try:
                result = subprocess.run(
                    [sys.executable, "-c", "import faiss"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=45,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                last_exc = exc
                if attempt == 1:
                    time.sleep(3)
                    continue
                # 子进程两轮都异常 → 本进程兜底直接试（最权威的运行时证据）
                try:
                    import faiss  # noqa: F401
                    logger.warning(
                        "[初始化] FAISS 子进程检查异常(%r)，但本进程导入成功，放行",
                        last_exc,
                    )
                    return
                except Exception as inner:
                    raise InitializationError(
                        "FAISS 运行时检查失败，无法安全初始化向量数据库。"
                        "请确认 faiss-cpu 已正确安装，或改用兼容当前 CPU 的 FAISS 包。"
                        f"（子进程异常: {last_exc!r}；本进程导入异常: {inner!r}）"
                    ) from exc

            if result.returncode != 0:
                details = (result.stderr or result.stdout or "").strip()
                if result.returncode < 0:
                    details = f"进程被信号 {-result.returncode} 终止。{details}".strip()
                raise InitializationError(
                    "FAISS 初始化失败，当前 CPU 或运行环境可能不兼容 faiss-cpu。"
                    "无 AVX2 的 CPU 上可能触发 Illegal instruction；"
                    "请使用支持 AVX2 的 CPU、安装兼容版本 FAISS，或更换运行环境。"
                    f"{' 原始错误: ' + details if details else ''}"
                )
            return  # 子进程检查通过

    def _load_faiss_vec_db_class(self):
        global FaissVecDB
        if FaissVecDB is not None:
            return FaissVecDB

        self._check_faiss_runtime()
        try:
            import faiss as _faiss

            _orig_read = _faiss.read_index
            _orig_write = _faiss.write_index

            def _patched_read_index(path: str, *args, **kwargs):
                if isinstance(path, (str, bytes, os.PathLike)) and _needs_bridge(path):
                    tmp = _make_temp_file("_faiss_read")
                    try:
                        shutil.copy2(path, tmp)
                        return _orig_read(tmp, *args, **kwargs)
                    finally:
                        if os.path.exists(tmp):
                            try:
                                os.remove(tmp)
                            except OSError:
                                pass
                return _orig_read(path, *args, **kwargs)

            def _patched_write_index(index, path, *args, **kwargs) -> None:
                # 仅在第二个参数为路径类对象 且 Windows 非 ASCII 时桥接；
                # 否则原样转发（如 VectorIOWriter / FILE* 等非路径对象）
                if isinstance(path, (str, bytes, os.PathLike)) and _needs_bridge(path):
                    dirname = os.path.dirname(path)
                    if dirname:
                        os.makedirs(dirname, exist_ok=True)
                    tmp = _make_temp_file("_faiss_write")
                    try:
                        _orig_write(index, tmp, *args, **kwargs)
                        # os.replace 原子覆盖，同卷 rename 跨卷 copy+delete
                        try:
                            os.replace(tmp, path)
                        except OSError:
                            shutil.copy2(tmp, path)
                            try:
                                os.remove(tmp)
                            except OSError:
                                pass
                    finally:
                        if os.path.exists(tmp):
                            try:
                                os.remove(tmp)
                            except OSError:
                                pass
                    return
                _orig_write(index, path, *args, **kwargs)

            _faiss.read_index = _patched_read_index
            _faiss.write_index = _patched_write_index

            from astrbot.core.db.vec_db.faiss_impl.vec_db import (
                FaissVecDB as LoadedFaissVecDB,
            )
        except (ImportError, ModuleNotFoundError, SystemError, OSError) as exc:
            raise InitializationError(
                "FAISS 初始化失败，无法加载 AstrBot FaissVecDB。"
                "请检查 faiss-cpu 安装状态和 CPU 指令集兼容性。"
            ) from exc

        FaissVecDB = LoadedFaissVecDB
        return LoadedFaissVecDB

    async def _complete_initialization(self):
        """完成完整的初始化流程"""
        if self._initialization_complete:
            return

        logger.info("开始完整初始化流程...")

        try:
            # 初始化数据库
            data_dir_path = Path(self.data_dir)
            db_path = data_dir_path / "livingmemory.db"
            index_path = data_dir_path / "livingmemory.index"
            graph_doc_path = data_dir_path / "livingmemory_graph_documents.db"
            graph_index_path = data_dir_path / "livingmemory_graph.index"
            graph_memory_enabled = self.config_manager.get("graph_memory.enabled", True)

            if not self.embedding_provider:
                raise ProviderNotReadyError("Embedding Provider 未初始化")
            if not self.llm_provider or not isinstance(self.llm_provider, Provider):
                raise ProviderNotReadyError("LLM Provider 未初始化或类型不正确")

            faiss_vec_db_cls = self._load_faiss_vec_db_class()

            # 检查索引文件维度与当前 embedding provider 维度是否一致
            await self._check_and_fix_dimension_mismatch(str(index_path))
            if graph_memory_enabled:
                await self._check_and_fix_dimension_mismatch(str(graph_index_path))

            self.db = faiss_vec_db_cls(
                str(db_path),
                str(index_path),
                self.embedding_provider,
            )
            await self.db.initialize()
            self.graph_db = None
            if graph_memory_enabled:
                self.graph_db = faiss_vec_db_cls(
                    str(graph_doc_path),
                    str(graph_index_path),
                    self.embedding_provider,
                )
                await self.graph_db.initialize()
            logger.info(f"数据库已初始化。数据目录: {self.data_dir}")

            # 初始化数据库迁移管理器
            self.db_migration = DBMigration(str(db_path))

            # 检查并执行数据库迁移
            if self.config_manager.get("migration_settings.auto_migrate", True):
                await self._check_and_migrate_database()

            # 初始化MemoryEngine
            stopwords_dir = data_dir_path / "stopwords"
            stopwords_dir.mkdir(parents=True, exist_ok=True)

            memory_engine_config = {
                "rrf_k": self.config_manager.get("fusion_strategy.rrf_k", 60),
                # v3.5 Rerank 精排配置（recall_engine 段）
                "rerank_enabled": self.config_manager.get("recall_engine.rerank_enabled", False),
                "rerank_top_n": self.config_manager.get("recall_engine.rerank_top_n", 10),
                "rerank_timeout": self.config_manager.get("recall_engine.rerank_timeout", 5.0),
                "rerank_fail_threshold": self.config_manager.get("recall_engine.rerank_fail_threshold", 3),
                "rerank_cooldown_secs": self.config_manager.get("recall_engine.rerank_cooldown_secs", 300.0),
                # P1(2026-09-07) 解耦：检索时 recency 衰减独立成键。原共享 importance_decay.decay_rate
                # 一鱼两吃——该键还喂 DecayScheduler 每日改库；本机该键=0.0 时检索 recency 被连坐关死。
                # 新键只管检索排序，改库衰减仍走旧键。默认 0.05/天≈一周-30%，重要记忆有护盾更慢。
                "decay_rate": self.config_manager.get(
                    "recall_engine.recency_decay_rate", 0.05
                ),
                "access_decay_window_days": self.config_manager.get(
                    "importance_decay.access_decay_window_days", 30.0
                ),
                "access_decay_max_count": self.config_manager.get(
                    "importance_decay.access_decay_max_count", 10
                ),
                "access_count_decay_multiplier": self.config_manager.get(
                    "importance_decay.access_count_decay_multiplier", 0.5
                ),
                "importance_weight": self.config_manager.get(
                    "recall_engine.importance_weight", 1.0
                ),
                "search_cache_enabled": self.config_manager.get(
                    "recall_engine.search_cache_enabled", True
                ),
                "search_cache_ttl_seconds": self.config_manager.get(
                    "recall_engine.search_cache_ttl_seconds", 45.0
                ),
                "search_cache_max_size": self.config_manager.get(
                    "recall_engine.search_cache_max_size", 256
                ),
                "fallback_enabled": self.config_manager.get(
                    "recall_engine.fallback_to_vector", True
                ),
                "cleanup_days_threshold": self.config_manager.get(
                    "forgetting_agent.cleanup_days_threshold", 30
                ),
                "cleanup_importance_threshold": self.config_manager.get(
                    "forgetting_agent.cleanup_importance_threshold", 0.3
                ),
                "auto_cleanup_enabled": self.config_manager.get(
                    "forgetting_agent.auto_cleanup_enabled", True
                ),
                "stopwords_path": str(stopwords_dir),
                "graph_memory_enabled": graph_memory_enabled,
                "document_route_weight": self.config_manager.get(
                    "graph_memory.document_route_weight", 0.65
                ),
                "graph_route_weight": self.config_manager.get(
                    "graph_memory.graph_route_weight", 0.35
                ),
                "cross_route_bonus": self.config_manager.get(
                    "graph_memory.cross_route_bonus", 0.08
                ),
                "graph_expansion_limit": self.config_manager.get(
                    "graph_memory.expansion_limit", 24
                ),
                "graph_expansion_hops": self.config_manager.get(
                    "graph_memory.expansion_hops", 1
                ),
                "graph_second_hop_weight": self.config_manager.get(
                    "graph_memory.second_hop_weight", 0.4
                ),
                "dynamic_route_weighting": self.config_manager.get(
                    "graph_memory.dynamic_route_weighting", True
                ),
                "graph_max_topics": self.config_manager.get(
                    "graph_memory.max_topics_per_memory", 6
                ),
                "graph_max_participants": self.config_manager.get(
                    "graph_memory.max_participants_per_memory", 8
                ),
                "graph_max_facts": self.config_manager.get(
                    "graph_memory.max_facts_per_memory", 8
                ),
                "atom_enabled": self.config_manager.get(
                    "graph_memory.atom_enabled", True
                ),
                "atom_maintenance_interval_hours": self.config_manager.get(
                    "graph_memory.atom_maintenance_interval_hours", 24.0
                ),
                "atom_forget_delay_days": self.config_manager.get(
                    "graph_memory.atom_forget_delay_days", 7.0
                ),
                "atom_purge_delay_days": self.config_manager.get(
                    "graph_memory.atom_purge_delay_days", 30.0
                ),
                "index_rebuild_batch_size": self.config_manager.get(
                    "index_rebuild_settings.batch_size", 50
                ),
                "index_rebuild_embedding_batch_size": self.config_manager.get(
                    "index_rebuild_settings.embedding_batch_size", 8
                ),
                "index_rebuild_tasks_limit": self.config_manager.get(
                    "index_rebuild_settings.tasks_limit", 1
                ),
                "index_rebuild_max_retries": self.config_manager.get(
                    "index_rebuild_settings.max_retries", 5
                ),
                "index_rebuild_retry_base_delay": self.config_manager.get(
                    "index_rebuild_settings.retry_base_delay", 30.0
                ),
                "index_rebuild_batch_delay": self.config_manager.get(
                    "index_rebuild_settings.batch_delay", 5.0
                ),
                "index_rebuild_request_delay": self.config_manager.get(
                    "index_rebuild_settings.request_delay", 5.0
                ),
                "index_rebuild_max_failure_ratio": self.config_manager.get(
                    "index_rebuild_settings.max_failure_ratio", 0.02
                ),
            }

            self.memory_engine = MemoryEngine(
                db_path=str(db_path),
                faiss_db=self.db,
                graph_vector_db=self.graph_db,
                llm_provider=self.llm_provider,
                config=memory_engine_config,
            )
            # v3.5 注入 Rerank provider 惰性 getter（WebUI 换模型即时生效）
            self.memory_engine._rerank_provider_getter = self.get_rerank_provider
            # 刀⑥：隐私分档写入端 getter——实时读插件 conf（开关热改即时生效，不吃启动快照）。
            # 返回嵌套形态 {"privacy": {...}} 与 engine.config 原始形态同构，resolve_write_scope 通吃。
            _cm_for_privacy = getattr(self, "config_manager", None)

            def _privacy_conf_getter(_cm=_cm_for_privacy):
                try:
                    return {
                        "privacy": {
                            "enabled": bool(_cm.get("privacy.enabled", False)),
                            "sensitive_words": _cm.get("privacy.sensitive_words", None),
                            "owner_whitelist": _cm.get("privacy.owner_whitelist", ""),
                            "intimate_sessions": _cm.get("privacy.intimate_sessions", ""),
                        }
                    }
                except Exception:
                    return None

            self.memory_engine._privacy_conf_getter = _privacy_conf_getter
            await self.memory_engine.initialize()
            logger.info("MemoryEngine 已初始化")

            # ── 记忆生态系统 v2.0：因果证据链 + 冲突检测 + 画像 + 预言 + 表达联动 ──
            try:
                from .v2.v2_engine import V2Engine

                v2_db_path = data_dir_path / "v2_memory.db"
                self.v2_engine = V2Engine(
                    db_path=str(v2_db_path),
                    db_connection=self.memory_engine.db_connection,
                    memory_engine=self.memory_engine,
                )
                # 情感 v4.0 P2：AppraisalEngine（glm-5.3-flash 账房先生，只打分不说话）
                self.appraisal_engine = None
                try:
                    from .v2.appraisal_engine import AppraisalEngine, resolve_provider_id
                    _ec = getattr(self.v2_engine, "emotion_core", None)
                    if _ec is not None:
                        _ap_pid = resolve_provider_id(getattr(self, 'config_manager', None))
                        self.appraisal_engine = AppraisalEngine(self.context, _ec, provider_id=_ap_pid)
                        self.v2_engine.appraisal_engine = self.appraisal_engine  # P3: 挂v2引擎，memory_engine/router可取
                        # P3: 检索层情感源注入（hybrid_retriever 情感路由用）
                        try:
                            _hr = getattr(self.memory_engine, "hybrid_retriever", None)
                            if _hr is not None:
                                _hr.emotion_source = self.v2_engine
                        except BaseException:
                            pass
                        logger.info(f"[v2] AppraisalEngine 就绪 (provider={self.appraisal_engine.provider_id})")
                except BaseException:
                    logger.warning("[v2] AppraisalEngine 初始化失败(忽略)", exc_info=True)
                await self.v2_engine.initialize()
                self.memory_engine.v2_engine = self.v2_engine
                logger.info("记忆生态系统 v2.0 已启用（因果链/冲突/画像/预言/表达联动）")
                # v2.1: 启动预言回溯循环（每小时扫描到期预言）
                self._v2_backfill_task = asyncio.create_task(self._v2_prophecy_loop())
            except BaseException as e:
                self.v2_engine = None
                logger.warning(f"记忆生态系统 v2.0 初始化失败，本次会话降级为 v1 模式: {e}")

            # 初始化 ConversationManager
            conversation_db_path = data_dir_path / "conversations.db"
            conversation_store = ConversationStore(str(conversation_db_path))
            await conversation_store.initialize()

            session_config = self.config_manager.session_manager
            self.conversation_manager = ConversationManager(
                store=conversation_store,
                max_cache_size=session_config.get("max_sessions", 100),
                context_window_size=session_config.get("context_window_size", 50),
                session_ttl=session_config.get("session_ttl", 3600),
            )
            logger.info("ConversationManager 已初始化")

            # 自动修复 message_count 不一致问题
            await self._repair_message_counts(conversation_store)

            # 初始化 MemoryProcessor
            # 注意：MemoryProcessor 不直接持有 llm_provider 实例引用，
            # 而是在每次调用时通过 AstrBot 上下文动态解析 Provider，
            # 以避免 AstrBot 重新创建 Provider 后旧实例的 httpx client 被关闭
            # 导致的 "Cannot send a request, as the client has been closed" 错误。
            llm_id = self.config_manager.get("provider_settings.llm_provider_id")
            self.memory_processor = MemoryProcessor(
                self.context,
                llm_provider=llm_id if llm_id else None,
                config={
                    "atom_enabled": memory_engine_config["atom_enabled"],
                },
            )
            logger.info("MemoryProcessor 已初始化")

            # 初始化索引验证器并自动重建索引
            self.index_validator = IndexValidator(str(db_path), self.db)
            await self._auto_rebuild_index_if_needed()

            # 异步初始化 TextProcessor
            if self.memory_engine and hasattr(self.memory_engine, "text_processor"):
                if self.memory_engine.text_processor and hasattr(
                    self.memory_engine.text_processor, "async_init"
                ):
                    await self.memory_engine.text_processor.async_init()
                    logger.info("TextProcessor 停用词已加载")

            # 启动重要性衰减调度器
            decay_rate = self.config_manager.get("importance_decay.decay_rate", 0.01)
            auto_cleanup_enabled = self.config_manager.get(
                "forgetting_agent.auto_cleanup_enabled", True
            )
            if self.memory_engine and (decay_rate > 0 or auto_cleanup_enabled):
                backup_enabled = self.config_manager.get(
                    "backup_settings.enabled", True
                )
                backup_keep_days = self.config_manager.get(
                    "backup_settings.keep_days", 7
                )
                scheduler = DecayScheduler(
                    memory_engine=self.memory_engine,
                    decay_rate=decay_rate,
                    data_dir=self.data_dir,
                    db_migration=self.db_migration,
                    backup_enabled=backup_enabled,
                    backup_keep_days=backup_keep_days,
                )
                await scheduler.start()
                self.decay_scheduler = scheduler
                logger.info("DecayScheduler 已启动")

            # 启动事件总线
            await self.start_event_bus()

            # 标记初始化完成
            self._initialization_complete = True
            logger.info("LivingMemory 插件初始化成功。")

        except Exception as e:
            logger.error(f"完整初始化流程失败: {e}", exc_info=True)
            self._initialization_failed = True
            self._initialization_error = str(e)
            raise InitializationError(f"初始化失败: {e}") from e

    async def _check_and_migrate_database(self):
        """检查并执行数据库迁移"""
        try:
            if not self.db_migration:
                logger.warning("数据库迁移管理器未初始化")
                return

            needs_migration = await self.db_migration.needs_migration()

            if not needs_migration:
                logger.info("数据库版本已是最新，无需迁移")
                return

            logger.info("检测到旧版本数据库，开始自动迁移。")

            if self.config_manager.get("migration_settings.create_backup", True):
                backup_path = await self.db_migration.create_backup()
                if backup_path:
                    logger.info(f"数据库备份已创建: {backup_path}")

            result = await self.db_migration.migrate(progress_callback=None)

            if result.get("success"):
                logger.info(f"数据库迁移结果: {result.get('message')}")
                logger.info(f"   耗时: {result.get('duration', 0):.2f}秒")
            else:
                logger.error(f"数据库迁移失败: {result.get('message')}")

        except Exception as e:
            logger.error(f"数据库迁移检查失败: {e}", exc_info=True)

    async def _auto_rebuild_index_if_needed(self):
        """自动检查并重建索引"""
        try:
            if not self.index_validator or not self.memory_engine:
                return

            # 检查v1迁移状态
            (
                needs_migration_rebuild,
                pending_count,
            ) = await self.index_validator.get_migration_status()

            if needs_migration_rebuild:
                logger.info(f"检测到 v1 迁移数据需要重建索引（{pending_count} 条文档）")
                logger.info("开始自动重建索引。")

                result = await self.index_validator.rebuild_indexes(self.memory_engine)

                if result["success"]:
                    logger.info(
                        f"索引自动重建完成: 成功 {result['processed']} 条, 失败 {result['errors']} 条"
                    )
                else:
                    logger.error(f"索引自动重建失败: {result.get('message')}")
                return

            # 检查索引一致性
            status = await self.index_validator.check_consistency()

            if not status.is_consistent and status.needs_rebuild:
                logger.warning(f"检测到索引不一致: {status.reason}")
                logger.info(
                    f"当前索引计数 - Documents: {status.documents_count}, BM25: {status.bm25_count}, Vector: {status.vector_count}"
                )
                logger.info("开始自动重建索引。")

                result = await self.index_validator.rebuild_indexes(self.memory_engine)

                if result["success"]:
                    logger.info(
                        f"索引自动重建完成: 成功 {result['processed']} 条, 失败 {result['errors']} 条"
                    )
                else:
                    logger.error(f"索引自动重建失败: {result.get('message')}")
            else:
                logger.info(f"索引一致性检查通过: {status.reason}")

        except Exception as e:
            logger.error(f"自动重建索引失败: {e}", exc_info=True)

    async def _repair_message_counts(self, conversation_store: ConversationStore):
        """修复会话表中 message_count 与实际消息数量不一致的问题"""
        try:
            logger.info("开始检查并修复 message_count 一致性。")
            fixed_sessions = await conversation_store.sync_message_counts()

            if fixed_sessions:
                logger.info(f"已修复 {len(fixed_sessions)} 个会话的 message_count")
            else:
                logger.debug("所有会话的 message_count 均正确")

        except Exception as e:
            logger.error(f"修复 message_count 失败: {e}", exc_info=True)

    @property
    def is_initialized(self) -> bool:
        """是否已初始化"""
        return self._initialization_complete

    @property
    def is_failed(self) -> bool:
        """是否初始化失败"""
        return self._initialization_failed

    @property
    def error_message(self) -> str | None:
        """错误消息"""
        return self._initialization_error

    async def ensure_initialized(self, timeout: float = 30.0) -> bool:
        """
        确保插件已初始化

        Args:
            timeout: 超时时间（秒）

        Returns:
            bool: 是否初始化成功
        """
        if self._initialization_complete:
            return True

        if self._initialization_failed:
            return False

        # 等待初始化完成
        start_time = time.time()
        while not self._initialization_complete and not self._initialization_failed:
            if time.time() - start_time > timeout:
                logger.error(f"等待插件初始化超时（{timeout}秒）")
                return False
            await asyncio.sleep(0.2)

        return self._initialization_complete

    async def _check_and_fix_dimension_mismatch(self, index_path: str) -> None:
        """
        检查 FAISS 索引维度与当前 embedding provider 维度是否一致

        当用户更换 embedding provider 后，旧索引的维度可能与新模型不匹配，
        导致 FAISS 插入时报错 "assert d == self.d"。
        此方法检测并自动删除不兼容的旧索引，让系统重建。

        Args:
            index_path: FAISS 索引文件路径
        """
        if not os.path.exists(index_path):
            return

        # 空文件不是有效索引，直接删除让 initialize() 重建，避免 faiss 抛异常
        try:
            if os.path.getsize(index_path) == 0:
                os.remove(index_path)
                logger.debug(f"已删除空索引文件: {_sanitize_path(index_path)}")
                return
        except OSError:
            pass

        try:
            import faiss
        except (ImportError, ModuleNotFoundError, SystemError, OSError) as exc:
            raise InitializationError(
                "FAISS 初始化失败，无法读取索引文件。"
                "请检查 faiss-cpu 安装状态和 CPU 指令集兼容性。"
            ) from exc

        # 读取索引文件 — 仅在 FAISS I/O 失败时进入坏索引处理
        try:
            old_index = self._faiss_read_index_safe(index_path)
        except InitializationError:
            raise
        except Exception as e:
            error_msg = str(e)
            # 文件在 os.path.exists 和 faiss.read_index 之间消失（如被外部进程删除），
            # 这不是坏文件，不需要隔离，让 initialize() 自动重建即可
            if "No such file" in error_msg or "could not open" in error_msg:
                logger.debug(f"FAISS 索引文件({_sanitize_path(index_path)})在检查时不可访问，将由 initialize() 重建: {e}")
                return
            # 真正的坏文件：直接删除（系统会自动重建），避免累积 .corrupt_* 文件
            try:
                os.remove(index_path)
                logger.error(
                    f"FAISS 索引文件已损坏并被删除: {_sanitize_path(index_path)}。"
                    "系统将创建空索引，并在初始化后尝试分批重建。",
                    exc_info=True,
                )
            except OSError:
                logger.error(
                    f"检查索引维度时出错，且删除坏索引失败: {e}",
                    exc_info=True,
                )
            return

        # 对比维度 — 放在坏索引处理之外，避免 embedding_provider 异常误删健康索引
        old_dim = old_index.d
        new_dim = self.embedding_provider.get_dim()  # type: ignore

        if old_dim != new_dim:
            logger.warning(
                f"检测到 FAISS 索引维度不匹配: 索引维度={old_dim}, "
                f"当前 Embedding Provider 维度={new_dim}"
            )
            logger.warning(
                "这通常由 Embedding 模型切换导致。"
                "旧索引将被删除，系统会自动重建索引。"
            )

            os.remove(index_path)
            logger.info(f"已删除不兼容的旧索引文件: {_sanitize_path(index_path)}")
            logger.info("注意: 向量检索功能将暂时不可用，直到重新导入记忆数据。")

    @staticmethod
    def _faiss_read_index_safe(index_path: str):
        """通过 ASCII 临时路径桥接 FAISS read_index。

        monkey-patch 已覆盖全局 faiss.read_index，此方法作为显式后备。
        """
        if not _needs_bridge(index_path):
            import faiss
            return faiss.read_index(index_path)
        tmp = _make_temp_file("_faiss_read")
        try:
            shutil.copy2(index_path, tmp)
            import faiss
            return faiss.read_index(tmp)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    async def stop_scheduler(self) -> None:
        """停止衰减调度器"""
        if self.decay_scheduler:
            await self.decay_scheduler.stop()
            self.decay_scheduler = None

    async def _v2_prophecy_loop(self) -> None:
        """记忆生态系统 v2.0：每小时扫描到期预言回溯验证；每 24 小时扫一次沉睡记忆（归档员）+ 生成家庭例会日报。"""
        archive_tick = 0
        try:
            while True:
                try:
                    if self.v2_engine and self.v2_engine.enabled:
                        stats = await self.v2_engine.run_prophecy_backfill(limit=50)
                        if stats and (stats.get("verified") or stats.get("failed")):
                            logger.info(f"[v2] 预言回溯完成: {stats}")
                        # Phase 2 归档员：每 24 次迭代（约 24h）扫一次沉睡记忆
                        archive_tick += 1
                        if archive_tick % 24 == 0:
                            try:
                                result = await asyncio.to_thread(
                                    self.v2_engine.archive.scan_sleeping_atoms,
                                    days=30, min_importance=0.6, max_candidates=10,
                                )
                                if result.get("new_candidates"):
                                    logger.info(
                                        f"[v2] 归档员每日扫描: 新增 {result['new_candidates']} 个沉睡记忆候选"
                                    )
                            except BaseException as e:
                                logger.warning(f"[v2] 归档员每日扫描异常: {e}")
                            # P2-13 多模态：写真馆照片每日增量同步为视觉记忆
                            try:
                                from .managers.photo_memory_sync import PhotoMemorySync

                                photo_idx = (
                                    Path(self.data_dir).parent.parent
                                    / "plugins"
                                    / "photo_album"
                                    / "photo_index.json"
                                )
                                if photo_idx.exists():
                                    photo_syncer = PhotoMemorySync(
                                        str(photo_idx),
                                        str(getattr(self.memory_engine, "db_path", "")),
                                        self.memory_engine
                                    )
                                    photo_report = await photo_syncer.sync()
                                    if photo_report.get("new") or photo_report.get("failed"):
                                        logger.info(
                                            f"[v2] 写真馆照片同步: 新增 {photo_report['new']} "
                                            f"失败 {photo_report.get('failed', 0)}"
                                        )
                            except BaseException as e:
                                logger.warning(f"[v2] 写真馆照片同步异常: {e}")
                            # Phase 4 家庭例会：生成今日日报（第一议题=归档审批）
                            try:
                                report = await asyncio.to_thread(
                                    self.v2_engine.family_meeting.generate_report
                                )
                                logger.info(
                                    f"[v2] 家庭例会日报已生成: {report.get('report_date')}"
                                )
                            except BaseException as e:
                                logger.warning(f"[v2] 家庭例会日报生成异常: {e}")
                            # P1-① Sleeptime 互链：消费 dream 归并标记，补 variant_of 边
                            try:
                                _linker = getattr(self.v2_engine, "sleeptime_linker", None)
                                if _linker is not None:
                                    _lr = await _linker.link_once()
                                    if _lr.get("linked"):
                                        logger.info(f"[v2] Sleeptime互链: 新增 {_lr['linked']} 条 variant_of 边")
                            except BaseException as e:
                                logger.warning(f"[v2] Sleeptime互链异常: {e}")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"[v2] 预言回溯循环异常: {e}")
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass

    async def stop_background_tasks(self) -> None:
        """停止初始化阶段的后台任务（如Provider重试）"""
        if self._retry_task and not self._retry_task.done():
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
        self._retry_task = None
        if self._v2_backfill_task and not self._v2_backfill_task.done():
            self._v2_backfill_task.cancel()
            try:
                await self._v2_backfill_task
            except asyncio.CancelledError:
                pass
        self._v2_backfill_task = None

    async def start_event_bus(self) -> None:
        """启动事件总线"""
        from .events.event_bus import get_event_bus, MemoryEventType
        bus = get_event_bus()
        await bus.start()
        # v2.1 家庭协作订阅者注册（反馈回路 A2 冲突→预言 / A3 画像→表达 / A5 情感→画像）
        self._register_family_subscribers(bus, MemoryEventType)
        logger.info("[EventBus] 已启动")

    def _register_family_subscribers(self, bus, MemoryEventType) -> None:
        """v2.1 家庭协作订阅者：家人之间"说一声"响应，全部 try/except 隔离。"""
        from .events.event_bus import MemoryEvent

        # A2：冲突确认 → 预言重估（相关预言弱化）
        async def on_conflict_confirmed(event: MemoryEvent) -> None:
            if not self.v2_engine or not self.v2_engine.enabled:
                return
            meta = event.metadata or {}
            try:
                await self.v2_engine.prophecy.re_evaluate(
                    event.memory_id,
                    str(meta.get("resolution_type") or ""),
                    str(meta.get("reason") or ""),
                )
            except BaseException:
                logger.warning("[Family] A2 冲突→预言重估失败", exc_info=True)

        on_conflict_confirmed._family_id = "lm_sub_a2_conflict_reval"  # 热重载去重

        # A3：画像更新 → 表达联动刷新（新画像特征驱动表达风格）
        async def on_profile_updated(event: MemoryEvent) -> None:
            if not self.v2_engine or not self.v2_engine.enabled:
                return
            persona_id = (event.metadata or {}).get("persona_id")
            if not persona_id:
                return
            try:
                await self.v2_engine.expression.get_or_log_style(persona_id, force_log=True)
            except BaseException:
                logger.warning("[Family] A3 画像→表达刷新失败", exc_info=True)

        on_profile_updated._family_id = "lm_sub_a3_profile_expr"  # 热重载去重

        # A5：情感趋势 → 画像反哺（helper 侧发布 EMOTION_TREND）
        async def on_emotion_trend(event: MemoryEvent) -> None:
            if not self.v2_engine or not self.v2_engine.enabled:
                return
            meta = event.metadata or {}
            persona_id = meta.get("persona_id")
            if not persona_id:
                return
            try:
                await self.v2_engine.profile.upsert_from_emotion(persona_id, meta.get("trend") or {})
            except BaseException:
                logger.warning("[Family] A5 情感→画像失败", exc_info=True)

        on_emotion_trend._family_id = "lm_sub_a5_emotion_profile"  # 热重载去重

        bus.subscribe(MemoryEventType.CONFLICT_CONFIRMED, on_conflict_confirmed)
        bus.subscribe(MemoryEventType.PROFILE_UPDATED, on_profile_updated)
        bus.subscribe(MemoryEventType.EMOTION_TREND, on_emotion_trend)
        logger.info("[Family] 已注册家庭订阅者: A2冲突→预言 / A3画像→表达 / A5情感→画像")

    async def stop_event_bus(self) -> None:
        """停止事件总线"""
        from .events.event_bus import get_event_bus
        bus = get_event_bus()
        await bus.stop()
        logger.info("[EventBus] 已停止")
