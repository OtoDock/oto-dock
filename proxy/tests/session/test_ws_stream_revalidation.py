"""The dashboard socket's periodic revalidation reaches every client message,
the ones read inside a streaming turn included.

A socket can stay inside the streaming viewer loop for as long as a turn
runs (a long agentic turn, or turns chained by queued messages), so the
loop runs the same throttled re-check the between-turn multiplex runs
before it dispatches a client message: a session no longer valid is closed
4001, one held by the forced password change or 2FA enrolment 4403, the
viewer detaches and the loop ends without acting on the message.

Run: cd proxy && venv/bin/pytest tests/session/test_ws_stream_revalidation.py -v
"""

from __future__ import annotations

import asyncio
import collections
import time

import pytest

import ws.dashboard  # noqa: F401  (resolves the dashboard and dashboard_chat cycle)
from core.events import stream_pump
from storage import database as task_store
from tests.fixtures.ws_dashboard_harness import (
    ANY,
    FakeDashboardWebSocket,
    dashboard_connection,
    drain_startup,
    run_ws_scenario,
    session_cookie,
)


def _viewer(ws: FakeDashboardWebSocket, chat_id: str, sid: str, *, due: bool):
    from services.notifications.notification_manager import LiveQueue
    from ws.dashboard import _AUTHZ_REVALIDATE_S, DashboardConnection
    conn = DashboardConnection.__new__(DashboardConnection)
    conn.websocket = ws
    conn.user_sub = "user-admin"
    conn.user = task_store.get_user("user-admin")
    conn.user_role = conn.user["role"]
    conn.agent_roles, conn.user_agents = {}, []
    conn.chat_id = chat_id
    conn.session_id = sid
    conn.streaming = False
    conn.live_queue = LiveQueue()
    conn._send_lock = asyncio.Lock()
    conn._client_pushback = collections.deque()
    conn._ended = asyncio.Event()
    conn._recheck_wake = asyncio.Event()
    conn.pending_control_requests = []
    conn.implement_queue, conn.artifact_queue = [], []
    conn.notify_connection_id = f"conn-{chat_id}"
    conn._last_authz_check = time.time() - (_AUTHZ_REVALIDATE_S + 1 if due else 0)
    return conn


def _endless_pump(chat_id: str) -> stream_pump.ChatStreamPump:
    loop = asyncio.get_running_loop()
    pump = stream_pump.ChatStreamPump(chat_id=chat_id, session_id=f"sess-{chat_id}",
                                      producer=loop.create_task(asyncio.sleep(3600)),
                                      event_queue=asyncio.Queue(), perm_queue=None)
    stream_pump._active_pumps[chat_id] = pump
    return pump


async def _stream_one_message(conn, pump, message: dict) -> dict:
    loop_task = asyncio.create_task(conn._stream_via_pump(pump))
    while not pump._ws_queues:
        await asyncio.sleep(0)
    conn.websocket.client_send(message)
    return await asyncio.wait_for(loop_task, timeout=5)


@pytest.fixture
def held_admin():
    from auth import providers
    providers.clear_auth_gate_caches()
    task_store.update_user_auth_fields("user-admin", must_change_password=True)
    yield
    providers.clear_auth_gate_caches()


@pytest.mark.asyncio
async def test_a_held_session_is_closed_4403_inside_a_streaming_turn(temp_db, held_admin):
    ws = FakeDashboardWebSocket(cookie=session_cookie())
    pump = _endless_pump("rv1")
    conn = _viewer(ws, "rv1", pump.session_id, due=True)
    try:
        result = await _stream_one_message(conn, pump, {"type": "ping"})
        assert result["detached"] is True
        assert ws.closed == (4403, "must_change_password")
        assert not pump._ws_queues
        assert "pong" not in [f["type"] for f in ws.sent]
        assert conn._closed_by_revalidation is True
    finally:
        stream_pump._active_pumps.pop("rv1", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_revoked_session_is_closed_4001_inside_a_streaming_turn(temp_db):
    ws = FakeDashboardWebSocket(cookie=session_cookie())
    # The session was revoked (a logout-all or a password reset).
    ws.cookies["session"] = "no-longer-valid"
    pump = _endless_pump("rv2")
    conn = _viewer(ws, "rv2", pump.session_id, due=True)
    from core.events import input_queue
    from core.events.common_events import TurnInput
    q = input_queue.get("rv2")
    q.items.append(input_queue.QueuedInput(
        queue_id="q-1", chat_id="rv2", author_sub="user-admin", item=TurnInput("queued"),
        origin_conn=conn.notify_connection_id))
    try:
        result = await _stream_one_message(
            conn, pump, {"type": "permission_response", "request_id": "r1", "allow": True})
        assert result["detached"] is True
        assert ws.closed is not None and ws.closed[0] == 4001
        assert not pump._ws_queues
        assert ws.sent[-1] == {"type": "error", "message": "Session expired: please sign in again"}
        # A message this connection queued before the check is dropped, so
        # no delivery starts a turn for the closed socket.
        for _ in range(50):
            if not q.items:
                break
            await asyncio.sleep(0.02)
        assert q.items == []
    finally:
        stream_pump._active_pumps.pop("rv2", None)
        input_queue._registry.pop("rv2", None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_the_check_is_throttled_inside_a_streaming_turn(temp_db, held_admin):
    ws = FakeDashboardWebSocket(cookie=session_cookie())
    pump = _endless_pump("rv3")
    conn = _viewer(ws, "rv3", pump.session_id, due=False)
    try:
        loop_task = asyncio.create_task(conn._stream_via_pump(pump))
        while not pump._ws_queues:
            await asyncio.sleep(0)
        await ws.expect({"type": "chat_status", "chat_id": "rv3", "status": "streaming"})
        ws.client_send({"type": "ping"})
        await ws.expect({"type": "pong", "build_id": ANY})
        assert ws.closed is None
        ws.client_send({"type": "resume_chat", "chat_id": "elsewhere"})
        result = await asyncio.wait_for(loop_task, timeout=5)
        assert result["resume_msg"]["chat_id"] == "elsewhere"
    finally:
        stream_pump._active_pumps.pop("rv3", None)
        pump.producer.cancel()


def test_the_multiplex_closes_4403_when_a_gate_starts_mid_session(temp_db, monkeypatch):
    from auth import providers
    from ws import dashboard
    monkeypatch.setattr(dashboard, "_AUTHZ_REVALIDATE_S", 0)
    providers.clear_auth_gate_caches()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            await asyncio.to_thread(task_store.update_user_auth_fields, "user-admin",
                                    must_change_password=True)
            providers.clear_auth_gate_caches()
            ws.client_send({"type": "ping"})
            for _ in range(50):
                if ws.closed:
                    break
                await asyncio.sleep(0.05)
            assert ws.closed == (4403, "must_change_password")
    run_ws_scenario(scenario)
