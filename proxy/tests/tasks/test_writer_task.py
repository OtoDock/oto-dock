"""Writer-task pattern + send queue invariants.

The writer task is the only coroutine that calls ws.send_text(). Producers
enqueue messages via conn.enqueue_send(). This test pins the invariants
that make safe HTTP-over-WS multiplexing possible.
"""

import asyncio
import json

import pytest

from core.remote.satellite_connection import (
    SatelliteConnection,
    SatelliteConnectionManager,
)


class FakeWS:
    """Fake WebSocket that records send_text calls atomically (no interleave)."""

    def __init__(self, fail_after: int = 0):
        # If >0, raise on the Nth call (1-indexed).
        self.fail_after = fail_after
        self.calls: list[str] = []
        self.send_count = 0

    async def send_text(self, payload: str) -> None:
        self.send_count += 1
        if self.fail_after and self.send_count >= self.fail_after:
            raise ConnectionError("fake WS dead")
        # Small await yields control — if the writer's serialization is
        # broken, concurrent calls could observably interleave the payload.
        # We capture the payload atomically (single append) so a correct
        # implementation produces no interleave.
        await asyncio.sleep(0)
        self.calls.append(payload)


@pytest.mark.asyncio
async def test_enqueue_send_serializes_concurrent_producers():
    """100 concurrent enqueues must produce 100 sequential send_text calls
    with no interleaved frames. The writer task is the single owner of
    ws.send_text()."""
    cm = SatelliteConnectionManager()
    ws = FakeWS()
    conn = SatelliteConnection(machine_id="m1", ws=ws)  # type: ignore[arg-type]

    # Manually start writer (normally done in register())
    conn.writer_task = asyncio.create_task(cm._writer_loop(conn))

    # Fire 100 enqueues concurrently
    async def send_one(i: int) -> None:
        await conn.enqueue_send({"type": "test", "n": i})

    await asyncio.gather(*[send_one(i) for i in range(100)])

    # Wait for writer to drain
    for _ in range(50):
        if ws.send_count == 100:
            break
        await asyncio.sleep(0.01)

    assert ws.send_count == 100, f"got {ws.send_count} sends"
    # Every payload must be valid JSON (no interleave corruption)
    for payload in ws.calls:
        decoded = json.loads(payload)
        assert decoded.get("type") == "test"
        assert "n" in decoded

    conn.writer_task.cancel()


@pytest.mark.asyncio
async def test_writer_failure_triggers_deregister():
    """When ws.send_text raises, the writer task triggers deregister()
    so callers wake up immediately instead of waiting for the heartbeat
    monitor's 90s timeout."""
    cm = SatelliteConnectionManager()
    ws = FakeWS(fail_after=1)  # first send fails
    conn = SatelliteConnection(machine_id="m1", ws=ws)  # type: ignore[arg-type]

    # Register manually (skip the WS auth path)
    cm._connections["m1"] = conn
    conn.writer_task = asyncio.create_task(cm._writer_loop(conn))

    # Add a pending ack so we can verify it gets rejected on deregister
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    cm._pending_acks["cmd-1"] = ("m1", fut)

    await conn.enqueue_send({"type": "test"})

    # Wait briefly for the writer to fail and trigger deregister
    for _ in range(50):
        if "m1" not in cm._connections:
            break
        await asyncio.sleep(0.01)

    # Connection deregistered, pending ack rejected
    assert "m1" not in cm._connections
    assert fut.done()
    with pytest.raises(RuntimeError):
        fut.result()


@pytest.mark.asyncio
async def test_enqueue_drops_oldest_on_overflow():
    """When the send queue is full (10K cap), the oldest message gets
    dropped to make room. Prevents OOM during sustained disconnect with
    high send volume."""
    # Use a small queue cap for the test by injecting a fresh Queue
    ws = FakeWS()
    conn = SatelliteConnection(machine_id="m1", ws=ws)  # type: ignore[arg-type]
    conn.send_queue = asyncio.Queue(maxsize=3)

    # Don't start the writer — let the queue fill
    await conn.enqueue_send({"type": "a", "n": 1})
    await conn.enqueue_send({"type": "a", "n": 2})
    await conn.enqueue_send({"type": "a", "n": 3})
    # 4th and 5th cause overflow → drop oldest
    await conn.enqueue_send({"type": "a", "n": 4})
    await conn.enqueue_send({"type": "a", "n": 5})

    # Queue should still have exactly 3 items, but with the oldest dropped
    items = []
    while not conn.send_queue.empty():
        items.append(conn.send_queue.get_nowait())
    ns = [it["n"] for it in items]
    assert len(ns) == 3, f"queue had {len(ns)} items, expected 3"
    # The newest 3 should be retained: 3, 4, 5 (1 and 2 dropped)
    assert max(ns) == 5
    assert 1 not in ns and 2 not in ns


@pytest.mark.asyncio
async def test_writer_cancelled_on_deregister():
    """Calling deregister() cancels the writer task cleanly."""
    cm = SatelliteConnectionManager()
    ws = FakeWS()
    conn = SatelliteConnection(machine_id="m1", ws=ws)  # type: ignore[arg-type]

    cm._connections["m1"] = conn
    conn.writer_task = asyncio.create_task(cm._writer_loop(conn))

    await asyncio.sleep(0.01)  # let writer start
    assert not conn.writer_task.done()

    await cm.deregister("m1")

    # cancel() is async — give the event loop a chance to process it.
    for _ in range(20):
        if conn.writer_task.done():
            break
        await asyncio.sleep(0.01)

    assert conn.writer_task.done()


# --- control/bulk send lanes -------------------------------------------
# file_push chunks go to the BULK lane; everything else (commands, acks, pong)
# to the CONTROL lane. The writer drains control first and re-checks it between
# every bulk frame, so a large multi-chunk transfer can never delay a command
# ack / the keepalive past a single chunk.


def test_enqueue_send_bulk_routes_to_bulk_lane():
    """bulk=True lands on bulk_queue; the default lands on send_queue. They are
    independent queues (no synchronous loop needed — put_nowait + no await)."""
    async def _run():
        ws = FakeWS()
        conn = SatelliteConnection(machine_id="m1", ws=ws)  # type: ignore[arg-type]
        await conn.enqueue_send({"type": "cmd"})
        await conn.enqueue_send({"type": "chunk"}, bulk=True)
        assert conn.send_queue.qsize() == 1
        assert conn.bulk_queue.qsize() == 1
        sent = conn.send_queue.get_nowait()
        assert sent["type"] == "cmd"
        bulk = conn.bulk_queue.get_nowait()
        assert bulk["type"] == "chunk"

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_control_drains_before_pre_queued_bulk():
    """With bulk frames already queued AND a control frame queued, the writer
    sends the control frame FIRST (control-first selection), then the bulk
    frames in order."""
    cm = SatelliteConnectionManager()
    ws = FakeWS()
    conn = SatelliteConnection(machine_id="m1", ws=ws)  # type: ignore[arg-type]

    # Queue bulk BEFORE control — yet control must still go out first.
    await conn.enqueue_send({"k": "bulk", "n": 1}, bulk=True)
    await conn.enqueue_send({"k": "bulk", "n": 2}, bulk=True)
    await conn.enqueue_send({"k": "control"})

    conn.writer_task = asyncio.create_task(cm._writer_loop(conn))
    for _ in range(50):
        if ws.send_count == 3:
            break
        await asyncio.sleep(0.01)
    conn.writer_task.cancel()

    order = [json.loads(p) for p in ws.calls]
    assert order[0] == {"k": "control"}
    assert order[1:] == [{"k": "bulk", "n": 1}, {"k": "bulk", "n": 2}]


class GatedWS:
    """FakeWS that blocks inside each send_text until the test releases it, so
    a test can interleave enqueues mid-stream deterministically. The payload is
    recorded at SELECTION time (before the gate), so ``calls`` reflects the
    writer's pick order."""

    def __init__(self):
        self.calls: list[dict] = []
        self._recorded = asyncio.Event()
        self._gate = asyncio.Event()

    async def send_text(self, payload: str) -> None:
        self.calls.append(json.loads(payload))
        self._recorded.set()
        await self._gate.wait()
        self._gate.clear()

    async def await_selection(self) -> None:
        """Return once the writer has selected (recorded) the next frame."""
        await self._recorded.wait()
        self._recorded.clear()

    def release(self) -> None:
        """Let the frame currently blocked in send_text complete."""
        self._gate.set()


@pytest.mark.asyncio
async def test_control_preempts_bulk_midstream():
    """A control frame enqueued WHILE a multi-chunk bulk transfer is draining
    jumps ahead of the remaining bulk frames — the core invariant."""
    cm = SatelliteConnectionManager()
    ws = GatedWS()
    conn = SatelliteConnection(machine_id="m1", ws=ws)  # type: ignore[arg-type]

    # Three bulk frames already on the bulk lane (a file mid-transfer).
    for n in (1, 2, 3):
        await conn.enqueue_send({"k": "bulk", "n": n}, bulk=True)
    conn.writer_task = asyncio.create_task(cm._writer_loop(conn))

    # Writer selects bulk#1 and blocks in send_text.
    await ws.await_selection()
    assert ws.calls[-1] == {"k": "bulk", "n": 1}

    # While bulk#2/#3 still wait, a control frame arrives.
    await conn.enqueue_send({"k": "control"})
    ws.release()  # bulk#1 completes → writer re-selects

    # Control must preempt the queued bulk#2/#3.
    await ws.await_selection()
    assert ws.calls[-1] == {"k": "control"}

    ws.release()
    await ws.await_selection()
    assert ws.calls[-1] == {"k": "bulk", "n": 2}
    ws.release()
    await ws.await_selection()
    assert ws.calls[-1] == {"k": "bulk", "n": 3}

    conn.writer_task.cancel()


@pytest.mark.asyncio
async def test_bulk_drops_oldest_on_overflow():
    """The bulk lane drops oldest on overflow independently of control —
    parity with test_enqueue_drops_oldest_on_overflow."""
    ws = FakeWS()
    conn = SatelliteConnection(machine_id="m1", ws=ws)  # type: ignore[arg-type]
    conn.bulk_queue = asyncio.Queue(maxsize=3)

    for n in range(1, 6):  # 1..5 → oldest (1,2) dropped
        await conn.enqueue_send({"type": "chunk", "n": n}, bulk=True)

    items = []
    while not conn.bulk_queue.empty():
        items.append(conn.bulk_queue.get_nowait())
    ns = [it["n"] for it in items]
    assert len(ns) == 3
    assert max(ns) == 5
    assert 1 not in ns and 2 not in ns
    # Control lane untouched by bulk overflow.
    assert conn.send_queue.empty()
