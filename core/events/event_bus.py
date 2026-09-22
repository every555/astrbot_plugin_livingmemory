"""Async event bus with publish/subscribe — adapted from DeepTutor events/event_bus.py.

Key differences from DeepTutor:
- MemoryEventType replaces tutoring EventType
- MemoryEvent is lighter (no task_id, user_input, tools_used)
- Designed for LivingMemory's memory lifecycle events
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

try:
    from astrbot.api import logger
except Exception:  # 独立测试/降级环境
    import logging

    logger = logging.getLogger("event_bus")


class MemoryEventType(str, Enum):
    """Memory lifecycle event types."""

    # ── Core lifecycle ──
    MEMORY_CREATED = "memory_created"
    MEMORY_REINFORCED = "memory_reinforced"
    MEMORY_DECAYED = "memory_decayed"
    MEMORY_RECALLED = "memory_recalled"

    # ── Expiration / cleanup ──
    MEMORY_EXPIRED = "memory_expired"
    MEMORY_FORGOTTEN = "memory_forgotten"
    MEMORY_ARCHIVED = "memory_archived"

    # ── Reinforcement cycle ──
    REVIEW_DUE = "review_due"
    REVIEW_COMPLETED = "review_completed"

    # ── Atom-level ──
    ATOM_LIFECYCLE = "atom_lifecycle"

    # ── Family collaboration (v2.1 家庭协作反馈回路) ──
    PROPHECY_VERIFIED = "prophecy_verified"      # 预言验证完成（含成功/失败）
    PROPHECY_EXPIRED = "prophecy_expired"        # 预言到期（可转提醒）
    STRENGTH_CHANGED = "strength_changed"        # 预言强度变化（重估/弱化）
    CONFLICT_CONFIRMED = "conflict_confirmed"    # 冲突确认（触发预言重估/知识沉淀）
    PROFILE_UPDATED = "profile_updated"          # 画像更新（驱动表达联动）
    CAUSAL_LINKED = "causal_linked"              # 因果链连接（触发预言生成）
    EMOTION_TREND = "emotion_trend"              # 情感趋势产生（反哺画像）
    LESSON_ADDED = "lesson_added"                # 教训记录（沉淀知识候选）
    ARCHIVE_CANDIDATE = "archive_candidate"      # 归档候选产生（上报家庭例会）


@dataclass
class MemoryEvent:
    """Lightweight event data for memory lifecycle.

    Attributes:
        type: Event type.
        memory_id: Affected memory/atom ID.
        memory_type: atom_type or document type.
        importance: Current importance (0-1).
        metadata: Arbitrary key-values for handlers.
        timestamp: Unix time when event was created.
    """

    type: MemoryEventType
    memory_id: int
    memory_type: str = ""
    importance: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "memory_id": self.memory_id,
            "memory_type": self.memory_type,
            "importance": self.importance,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }


EventHandler = Callable[[MemoryEvent], Coroutine[Any, Any, None]]


class EventBus:
    """Singleton async event bus with non-blocking delivery.

    Thread-safe-ish: all event processing runs on the same asyncio event loop.
    Handlers should be fast; slow work should spawn its own task.
    """

    _instance: EventBus | None = None
    _initialized: bool = False

    def __new__(cls) -> EventBus:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if EventBus._initialized:
            return
        EventBus._initialized = True

        self._subscribers: dict[MemoryEventType, list[EventHandler]] = {
            et: [] for et in MemoryEventType
        }
        self._wildcard_handlers: list[EventHandler] = []
        self._task_queue: asyncio.Queue[MemoryEvent] = asyncio.Queue()
        self._processor_task: asyncio.Task | None = None
        self._running: bool = False

    # ── Subscribe / Unsubscribe ──

    def subscribe(self, event_type: MemoryEventType, handler: EventHandler) -> None:
        """Register a handler for a specific event type.

        热重载去重：handler 若带 _family_id 属性，先移除同 id 的旧 handler，
        避免插件热重载后旧闭包（引用已关闭引擎）残留导致重复执行。
        """
        fam_id = getattr(handler, "_family_id", None)
        if fam_id is not None:
            self._subscribers[event_type] = [
                h
                for h in self._subscribers[event_type]
                if getattr(h, "_family_id", None) != fam_id
            ]
        if handler not in self._subscribers[event_type]:
            self._subscribers[event_type].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Register a handler for ALL event types."""
        fam_id = getattr(handler, "_family_id", None)
        if fam_id is not None:
            self._wildcard_handlers = [
                h
                for h in self._wildcard_handlers
                if getattr(h, "_family_id", None) != fam_id
            ]
        if handler not in self._wildcard_handlers:
            self._wildcard_handlers.append(handler)

    def unsubscribe(self, event_type: MemoryEventType, handler: EventHandler) -> None:
        """Remove a handler from a specific event type."""
        if handler in self._subscribers[event_type]:
            self._subscribers[event_type].remove(handler)

    # ── Publish ──

    async def publish(self, event: MemoryEvent) -> None:
        """Publish an event (non-blocking, queued for async processing)."""
        await self._task_queue.put(event)
        if not self._running:
            await self.start()

    def publish_nowait(self, event: MemoryEvent) -> None:
        """Publish an event without awaiting (fire-and-forget, best-effort).

        Use when calling from synchronous code that can't await.
        The event is queued and will be processed when the event loop runs.
        """
        try:
            self._task_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("[EventBus] Queue full, dropping event: %s", event.type.value)

    # ── Processing ──

    async def _process_events(self) -> None:
        """Main event processing loop."""
        while self._running:
            try:
                try:
                    event = await asyncio.wait_for(self._task_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                # Typed handlers
                handlers = list(self._subscribers.get(event.type, []))
                # Wildcard handlers
                handlers.extend(self._wildcard_handlers)

                if not handlers:
                    self._task_queue.task_done()
                    continue

                for handler in handlers:
                    try:
                        await handler(event)
                    except Exception:
                        logger.error(
                            "[EventBus] Handler error for %s (id=%d)",
                            event.type.value,
                            event.memory_id,
                            exc_info=True,
                        )

                self._task_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("[EventBus] Processing error", exc_info=True)

    # ── Lifecycle ──

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._processor_task = asyncio.create_task(self._process_events())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._processor_task and not self._processor_task.done():
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

    async def flush(self, timeout: float = 30.0) -> None:
        """Wait for all queued events to be processed."""
        if not self._running or self._task_queue.empty():
            return
        try:
            await asyncio.wait_for(self._task_queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "[EventBus] Flush timeout after %.0fs, %d events pending",
                timeout,
                self._task_queue.qsize(),
            )

    @classmethod
    def reset(cls) -> None:
        """Reset singleton (for testing)."""
        if cls._instance is not None:
            cls._instance._running = False
            if cls._instance._processor_task:
                cls._instance._processor_task.cancel()
        cls._instance = None
        cls._initialized = False


_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Get the singleton EventBus instance."""
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus
