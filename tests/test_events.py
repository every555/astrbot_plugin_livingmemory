"""Tests for EventBus."""

import asyncio
import pytest

from astrbot_plugin_livingmemory.core.events.event_bus import (
    EventBus,
    MemoryEvent,
    MemoryEventType,
)


@pytest.fixture(autouse=True)
def reset():
    EventBus.reset()
    yield
    EventBus.reset()


@pytest.fixture
def bus():
    return EventBus()


# ── Singleton ──────────────────────

def test_singleton():
    b1 = EventBus()
    b2 = EventBus()
    assert b1 is b2


# ── Event.to_dict ──────────────────

def test_event_to_dict():
    event = MemoryEvent(
        type=MemoryEventType.MEMORY_REINFORCED,
        memory_id=10,
        memory_type="atom",
        importance=0.85,
        metadata={"is_correct": True},
    )
    d = event.to_dict()
    assert d["type"] == "memory_reinforced"
    assert d["memory_id"] == 10
    assert d["importance"] == 0.85
    assert d["metadata"]["is_correct"] is True


# ── Event types ────────────────────

def test_all_event_types_present():
    expected = {
        "memory_created", "memory_reinforced", "memory_decayed",
        "memory_recalled", "memory_expired", "memory_forgotten",
        "memory_archived", "review_due", "review_completed", "atom_lifecycle",
    }
    actual = {e.value for e in MemoryEventType}
    missing = expected - actual
    assert not missing, f"Missing: {missing}"


# ── Async: full cycle ──────────────

@pytest.mark.asyncio
async def test_full_publish_subscribe(bus):
    received = []
    async def handler(event: MemoryEvent):
        received.append(event.memory_id)

    bus.subscribe(MemoryEventType.MEMORY_CREATED, handler)
    await bus.start()

    await bus.publish(MemoryEvent(type=MemoryEventType.MEMORY_CREATED, memory_id=42))
    await bus.flush(timeout=5.0)

    assert received == [42]
    await bus.stop()


@pytest.mark.asyncio
async def test_unsubscribe(bus):
    received = []
    async def handler(event):
        received.append(event.memory_id)

    bus.subscribe(MemoryEventType.MEMORY_REINFORCED, handler)
    await bus.start()

    await bus.publish(MemoryEvent(type=MemoryEventType.MEMORY_REINFORCED, memory_id=1))
    await bus.flush()
    assert received == [1]

    bus.unsubscribe(MemoryEventType.MEMORY_REINFORCED, handler)
    received.clear()

    await bus.publish(MemoryEvent(type=MemoryEventType.MEMORY_REINFORCED, memory_id=2))
    await bus.flush()
    assert received == []

    await bus.stop()


@pytest.mark.asyncio
async def test_wildcard_subscriber(bus):
    seen = []
    async def catch_all(event):
        seen.append(event.type)

    bus.subscribe_all(catch_all)
    await bus.start()

    await bus.publish(MemoryEvent(type=MemoryEventType.MEMORY_CREATED, memory_id=1))
    await bus.publish(MemoryEvent(type=MemoryEventType.MEMORY_DECAYED, memory_id=2))
    await bus.flush()

    assert len(seen) == 2
    await bus.stop()


@pytest.mark.asyncio
async def test_multiple_handlers(bus):
    ids = []
    async def h1(event): ids.append(("h1", event.memory_id))
    async def h2(event): ids.append(("h2", event.memory_id))

    bus.subscribe(MemoryEventType.MEMORY_RECALLED, h1)
    bus.subscribe(MemoryEventType.MEMORY_RECALLED, h2)
    await bus.start()

    await bus.publish(MemoryEvent(type=MemoryEventType.MEMORY_RECALLED, memory_id=77))
    await bus.flush()

    assert ("h1", 77) in ids
    assert ("h2", 77) in ids
    await bus.stop()


@pytest.mark.asyncio
async def test_handler_error_isolation(bus):
    results = []
    async def bad(event): raise RuntimeError("boom")
    async def good(event): results.append(event.memory_id)

    bus.subscribe(MemoryEventType.MEMORY_EXPIRED, bad)
    bus.subscribe(MemoryEventType.MEMORY_EXPIRED, good)
    await bus.start()

    await bus.publish(MemoryEvent(type=MemoryEventType.MEMORY_EXPIRED, memory_id=55))
    await bus.flush()

    assert results == [55]
    await bus.stop()


@pytest.mark.asyncio
async def test_no_subscriber_no_crash(bus):
    await bus.start()
    await bus.publish(MemoryEvent(type=MemoryEventType.REVIEW_COMPLETED, memory_id=1))
    await bus.flush()
    # Should not crash
    await bus.stop()


@pytest.mark.asyncio
async def test_publish_nowait(bus):
    received = []
    async def handler(event):
        received.append(event.memory_id)

    bus.subscribe(MemoryEventType.MEMORY_FORGOTTEN, handler)
    await bus.start()

    bus.publish_nowait(MemoryEvent(type=MemoryEventType.MEMORY_FORGOTTEN, memory_id=99))
    await bus.flush()

    assert received == [99]
    await bus.stop()
