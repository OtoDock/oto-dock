"""Tests for pending-ack cleanup on satellite deregister."""

import asyncio

import pytest


@pytest.mark.asyncio
async def test_deregister_cancels_pending_acks_for_machine():
    """When a satellite disconnects, all its pending ack futures receive
    RuntimeError immediately instead of timing out at 30s."""
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    # Create fake pending-ack entries directly (no real WS)
    loop = asyncio.get_event_loop()
    fut_a1 = loop.create_future()
    fut_a2 = loop.create_future()
    fut_b = loop.create_future()
    cm._pending_acks["cmd-a1"] = ("machine-a", fut_a1, None)
    cm._pending_acks["cmd-a2"] = ("machine-a", fut_a2, None)
    cm._pending_acks["cmd-b"] = ("machine-b", fut_b, None)

    # Pretend machine-a was connected (deregister needs to find it)
    cm._connections["machine-a"] = type("FakeConn", (), {
        "session_queues": {},
    })()

    await cm.deregister("machine-a")

    # Both machine-a futures must be done with RuntimeError
    assert fut_a1.done()
    with pytest.raises(RuntimeError):
        fut_a1.result()
    assert fut_a2.done()
    with pytest.raises(RuntimeError):
        fut_a2.result()

    # machine-b is unaffected
    assert not fut_b.done()


@pytest.mark.asyncio
async def test_deregister_no_machine_is_noop():
    """Deregistering an unknown machine doesn't raise or break state."""
    from core.remote.satellite_connection import SatelliteConnectionManager
    cm = SatelliteConnectionManager()
    await cm.deregister("never-seen")
    assert "never-seen" not in cm._connections
