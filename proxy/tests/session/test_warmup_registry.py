"""Unit tests for proxy.core.warmup_registry.

Focused on the registry data structure: register, attach, emit, fanout,
history bounds, unregister, sweeper, concurrent listener mutation.
"""

import asyncio
import time

import pytest

from core.session import warmup_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    """Each test gets a clean registry. Module-level state is shared with
    the real proxy; in tests this guarantees isolation."""
    warmup_registry._inflight.clear()
    yield
    warmup_registry._inflight.clear()


@pytest.mark.asyncio
async def test_register_creates_entry():
    rec = await warmup_registry.register("c1", "user-a", "agent-x")
    assert rec.chat_id == "c1"
    assert rec.user_sub == "user-a"
    assert rec.agent == "agent-x"
    assert warmup_registry.get("c1") is rec


@pytest.mark.asyncio
async def test_register_idempotent():
    rec1 = await warmup_registry.register("c1", "user-a", "agent-x")
    rec2 = await warmup_registry.register("c1", "user-a", "agent-x")
    assert rec1 is rec2


@pytest.mark.asyncio
async def test_attach_listener_returns_none_if_unregistered():
    async def _send(ev):
        pass

    rec = await warmup_registry.attach_listener("missing", _send)
    assert rec is None


@pytest.mark.asyncio
async def test_emit_fans_out_to_all_listeners():
    received_a: list[dict] = []
    received_b: list[dict] = []

    async def _send_a(ev):
        received_a.append(ev)

    async def _send_b(ev):
        received_b.append(ev)

    await warmup_registry.register("c1", "u", "a")
    await warmup_registry.attach_listener("c1", _send_a)
    await warmup_registry.attach_listener("c1", _send_b)

    await warmup_registry.emit("c1", {"type": "warmup_progress", "pct": 10})
    await warmup_registry.emit("c1", {"type": "warmup_progress", "pct": 20})
    await warmup_registry.drain_listeners()

    assert len(received_a) == 2
    assert len(received_b) == 2
    assert received_a[1]["pct"] == 20
    assert received_b[1]["pct"] == 20


@pytest.mark.asyncio
async def test_event_history_bounded_at_50():
    sent: list[dict] = []

    async def _send(ev):
        sent.append(ev)

    await warmup_registry.register("c1", "u", "a")
    await warmup_registry.attach_listener("c1", _send)

    for i in range(60):
        await warmup_registry.emit("c1", {"type": "warmup_progress", "pct": i})
    await warmup_registry.drain_listeners()

    # All 60 reach the live listener.
    assert len(sent) == 60
    # But history is bounded.
    rec = warmup_registry.get("c1")
    assert rec is not None
    assert len(rec.event_history) == 50
    # The last 50 are kept (10..59).
    assert rec.event_history[0]["pct"] == 10
    assert rec.event_history[-1]["pct"] == 59


@pytest.mark.asyncio
async def test_detach_listener_safe_after_unregister():
    async def _send(ev):
        pass

    await warmup_registry.register("c1", "u", "a")
    await warmup_registry.attach_listener("c1", _send)
    await warmup_registry.unregister("c1")
    # Should not raise.
    await warmup_registry.detach_listener("c1", _send)


@pytest.mark.asyncio
async def test_unregister_clears():
    await warmup_registry.register("c1", "u", "a")
    assert warmup_registry.get("c1") is not None
    await warmup_registry.unregister("c1")
    assert warmup_registry.get("c1") is None


@pytest.mark.asyncio
async def test_unregister_sets_completed_event():
    rec = await warmup_registry.register("c1", "u", "a")
    assert not rec.completed.is_set()
    await warmup_registry.unregister("c1")
    assert rec.completed.is_set()


@pytest.mark.asyncio
async def test_emit_after_unregister_drops_silently():
    sent: list[dict] = []

    async def _send(ev):
        sent.append(ev)

    await warmup_registry.register("c1", "u", "a")
    await warmup_registry.attach_listener("c1", _send)
    await warmup_registry.unregister("c1")
    # Should not raise or send.
    await warmup_registry.emit("c1", {"type": "warmup_progress"})
    assert sent == []


@pytest.mark.asyncio
async def test_emit_continues_when_one_listener_raises():
    received_b: list[dict] = []

    async def _send_a(ev):
        raise RuntimeError("simulated WS write failure")

    async def _send_b(ev):
        received_b.append(ev)

    await warmup_registry.register("c1", "u", "a")
    await warmup_registry.attach_listener("c1", _send_a)
    await warmup_registry.attach_listener("c1", _send_b)

    # _send_a's exception must not stop _send_b from receiving.
    await warmup_registry.emit("c1", {"type": "warmup_progress"})
    await warmup_registry.drain_listeners()
    assert len(received_b) == 1


@pytest.mark.asyncio
async def test_a_slow_listener_delays_only_itself_and_keeps_its_order():
    """One tab's paused socket must not hold the other tabs' warmup frames
    (or the server kick behind warmup_ready): the emit returns at once and
    each listener's sends run on their own chain, in order."""
    gate = asyncio.Event()
    slow: list[int] = []
    fast: list[int] = []

    async def _slow(ev):
        await gate.wait()
        slow.append(ev["n"])

    async def _fast(ev):
        fast.append(ev["n"])

    await warmup_registry.register("c1", "u", "a")
    await warmup_registry.attach_listener("c1", _slow)
    await warmup_registry.attach_listener("c1", _fast)
    for n in range(3):
        await asyncio.wait_for(warmup_registry.emit("c1", {"type": "warmup_progress", "n": n}), 0.5)
    await asyncio.sleep(0)
    assert fast == [0, 1, 2]
    assert slow == []
    gate.set()
    await warmup_registry.drain_listeners()
    assert slow == [0, 1, 2]


@pytest.mark.asyncio
async def test_sweeper_removes_completed_entries():
    rec = await warmup_registry.register("c1", "u", "a")
    rec.completed.set()
    # Stale-by-age entry as well.
    rec2 = await warmup_registry.register("c2", "u", "a")
    rec2.started_at = time.monotonic() - 700  # > 600s threshold
    # Active entry that should survive.
    await warmup_registry.register("c3", "u", "a")

    removed = await warmup_registry.sweep_stale()
    assert removed == 2
    assert warmup_registry.get("c1") is None
    assert warmup_registry.get("c2") is None
    assert warmup_registry.get("c3") is not None


@pytest.mark.asyncio
async def test_concurrent_attach_detach_safe():
    """Spawn many listeners concurrently attaching/detaching while a
    producer emits. No exceptions; no orphan listeners after."""
    await warmup_registry.register("c1", "u", "a")

    async def _producer():
        for i in range(20):
            await warmup_registry.emit("c1", {"type": "warmup_progress", "pct": i})
            await asyncio.sleep(0)

    async def _attach_detach(n: int):
        async def _send(ev):
            pass
        for _ in range(5):
            await warmup_registry.attach_listener("c1", _send)
            await asyncio.sleep(0)
            await warmup_registry.detach_listener("c1", _send)

    tasks = [_producer()] + [_attach_detach(i) for i in range(10)]
    await asyncio.gather(*tasks)

    rec = warmup_registry.get("c1")
    assert rec is not None
    # All listeners attached and then detached themselves — none remain.
    assert len(rec.listeners) == 0
