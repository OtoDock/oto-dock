"""The location bridge (``core/session/location_bridge.py``): a request
the proxy pushed to one dashboard session is answered by that session
only; any other connection's answer is dropped (the
session binding).
"""

import asyncio

import pytest

from core.session import location_bridge as lb


@pytest.mark.asyncio
async def test_only_the_asked_session_answers_a_location_request():
    waiter = asyncio.create_task(lb.wait_for_location("req-1", timeout=2.0, session_id="sid-a"))
    await asyncio.sleep(0)
    assert lb.resolve_location("req-1", {"lat": 1.0}, session_id="sid-b") is False
    assert lb.resolve_location("req-1", {"lat": 1.0}, session_id="sid-a") is True
    assert await waiter == {"lat": 1.0}
    assert "req-1" not in lb._location_sessions


@pytest.mark.asyncio
async def test_a_request_pushed_without_a_session_keeps_the_open_answer():
    waiter = asyncio.create_task(lb.wait_for_location("req-2", timeout=2.0))
    await asyncio.sleep(0)
    assert lb.resolve_location("req-2", {"error": "denied"}, session_id="sid-x") is True
    assert await waiter == {"error": "denied"}
    assert lb.resolve_location("req-2", {}) is False
