"""Application event bus — adapted from DeepTutor's events/ module.

Provides async publish/subscribe for inter-module communication.
Subscribers (WebUI, dream engine, sentry, etc.) listen for memory events
without tight coupling to the storage layer.

Architecture:
    EventBus (singleton) → Queue → async processor → handlers per EventType

Usage:
    from astrbot_plugin_livingmemory.core.events import get_event_bus, MemoryEventType

    bus = get_event_bus()
    bus.subscribe(MemoryEventType.MEMORY_REINFORCED, my_handler)
    await bus.publish(MemoryEvent(type=MemoryEventType.MEMORY_REINFORCED, ...))
"""

from .event_bus import EventBus, EventHandler, MemoryEvent, MemoryEventType, get_event_bus

__all__ = [
    "EventBus",
    "EventHandler",
    "MemoryEvent",
    "MemoryEventType",
    "get_event_bus",
]
