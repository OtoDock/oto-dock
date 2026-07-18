"""Tests for ``core/remote/upload_inflight`` — the background upload-push
registry and the bounded turn-start barrier (dashboard upload latency fix).

``/v1/upload`` returns as soon as the platform write lands and backgrounds the
satellite push; ``RemoteExecutionLayer.send_message`` barriers on the agent's
in-flight pushes before dispatching the prompt. These tests pin the registry
semantics (per-agent scoping, snapshot-at-call-time, bounded cap) and the
send_message ordering guarantee.
"""

from __future__ import annotations

import asyncio

import pytest

from core.remote import upload_inflight


@pytest.fixture(autouse=True)
def reset_registry():
    upload_inflight._inflight.clear()
    yield
    upload_inflight._inflight.clear()


# ---------------------------------------------------------------------------
# Registry semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_settled_returns_immediately_with_nothing_in_flight():
    """The common case — no uploads in flight — must not wait at all.
    This is also the LOCAL-session guarantee: local sessions never track
    pushes, so a barrier for their agent is always an instant no-op."""
    assert await upload_inflight.wait_settled("agent-x") is True
    assert upload_inflight.pending_count("agent-x") == 0


@pytest.mark.asyncio
async def test_barrier_waits_until_push_lands():
    gate = asyncio.Event()
    landed = asyncio.Event()

    async def push():
        await gate.wait()
        landed.set()

    upload_inflight.track("agent-x", push())
    assert upload_inflight.pending_count("agent-x") == 1

    async def release():
        await asyncio.sleep(0.05)
        gate.set()

    releaser = asyncio.create_task(release())
    ok = await upload_inflight.wait_settled("agent-x", timeout=5.0)
    await releaser
    assert ok is True
    # The barrier only returned after the push actually completed.
    assert landed.is_set()
    # Registry self-cleans (done callback discards + drops the empty key).
    await asyncio.sleep(0)
    assert upload_inflight.pending_count("agent-x") == 0
    assert "agent-x" not in upload_inflight._inflight


@pytest.mark.asyncio
async def test_barrier_cap_proceeds_while_push_continues():
    """A wedged push must not hang the turn: the barrier gives up at the cap
    and returns False, while the push task itself keeps running."""
    gate = asyncio.Event()

    async def stuck_push():
        await gate.wait()

    task = upload_inflight.track("agent-x", stuck_push())
    ok = await upload_inflight.wait_settled("agent-x", timeout=0.1)
    assert ok is False
    assert not task.done()  # push continues in the background
    gate.set()
    await task


@pytest.mark.asyncio
async def test_barrier_is_scoped_per_agent():
    """A turn for agent A never waits on agent B's uploads."""
    gate = asyncio.Event()

    async def stuck_push():
        await gate.wait()

    task = upload_inflight.track("agent-other", stuck_push())
    ok = await upload_inflight.wait_settled("agent-x", timeout=5.0)
    assert ok is True  # returned instantly despite agent-other's stuck push
    gate.set()
    await task


@pytest.mark.asyncio
async def test_barrier_snapshots_at_call_time():
    """A push tracked AFTER the barrier starts waiting is not awaited — a
    steady stream of uploads can't extend a turn's wait unboundedly."""
    first_gate = asyncio.Event()
    second_gate = asyncio.Event()
    second_task: list[asyncio.Task] = []

    async def first():
        await first_gate.wait()

    async def second():
        await second_gate.wait()

    upload_inflight.track("agent-x", first())

    async def track_second_then_finish_first():
        await asyncio.sleep(0.05)
        second_task.append(upload_inflight.track("agent-x", second()))
        first_gate.set()

    helper = asyncio.create_task(track_second_then_finish_first())
    ok = await upload_inflight.wait_settled("agent-x", timeout=5.0)
    await helper
    assert ok is True  # settled once FIRST landed, despite SECOND in flight
    assert upload_inflight.pending_count("agent-x") == 1
    second_gate.set()
    await second_task[0]


# ---------------------------------------------------------------------------
# send_message barrier — prompt dispatch waits for the push
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_dispatches_only_after_push_lands(monkeypatch):
    """The end-to-end ordering guarantee: with an upload push in flight for
    the session's agent, ``RemoteExecutionLayer.send_message`` must not send
    the prompt to the satellite until the push has completed."""
    from unittest.mock import AsyncMock, MagicMock

    from core.remote import remote_execution as re_mod

    order: list[str] = []
    push_gate = asyncio.Event()

    cm = MagicMock()
    cm.wait_abort_acked = AsyncMock(return_value=True)

    async def record_send(machine_id, payload, command_id=None):
        order.append("send_command")

    cm.send_command = AsyncMock(side_effect=record_send)

    layer = re_mod.RemoteExecutionLayer(cm)
    info = re_mod.RemoteSessionInfo(
        session_id="sess-1",
        machine_id="m-1",
        agent_name="agent-x",
        execution_path="claude-code-cli",
        event_queue=asyncio.Queue(),
    )
    layer._sessions["sess-1"] = info

    async def empty_stream(info):
        return
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(layer, "_stream_cli_turn", empty_stream)

    async def push():
        await push_gate.wait()
        order.append("push_landed")

    upload_inflight.track("agent-x", push())

    async def release():
        await asyncio.sleep(0.05)
        push_gate.set()

    releaser = asyncio.create_task(release())
    events = [e async for e in layer.send_message("sess-1", "read the file")]
    await releaser

    assert order == ["push_landed", "send_command"]
    assert events == []  # empty stream — no error events
