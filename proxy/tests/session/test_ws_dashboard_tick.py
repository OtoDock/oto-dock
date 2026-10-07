"""The dashboard socket's revalidation reaches an idle socket: on the
connection's own tick, and at once when a person's standing changes
(``ws.dashboard.revalidate_user``, the offboarding event). A session that no
longer holds closes 4001, a viewed chat the person may no longer open
closes 1000 with the reason ``access changed``; either way the notify queue
and the warmup listeners go with the socket.

Run: cd proxy && venv/bin/pytest tests/session/test_ws_dashboard_tick.py -v
"""

from __future__ import annotations

import asyncio
import time

import pytest

import ws.dashboard  # noqa: F401  (resolves the dashboard and dashboard_chat cycle)
from ws import dashboard as dash
from auth import session_revocation
from auth.providers import validate_session_jwt
from storage.pg import get_conn
from tests.fixtures.ws_dashboard_harness import (
    ANY,
    FakeExecutionLayer,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    stub_dashboard_seams,
    warm_new_chat,
)


@pytest.fixture(autouse=True)
def _fresh():
    session_revocation.reset_for_tests()
    yield
    session_revocation.reset_for_tests()


async def _closed(ws, timeout: float = 5.0):
    for _ in range(int(timeout / 0.05)):
        if ws.closed is not None:
            return ws.closed
        await asyncio.sleep(0.05)
    raise AssertionError("the socket never closed")


class TestWake:
    def test_a_revoked_sign_in_closes_an_idle_socket_at_once(self, temp_db):
        cookie = session_cookie()

        async def scenario():
            async with dashboard_connection(cookie) as ws_:
                await drain_startup(ws_)
                session_revocation.revoke(validate_session_jwt(cookie), lifetime_s=60)
                assert dash.revalidate_user("user-admin") == 1
                await ws_.expect({"type": "error", "message": "Session expired: please sign in again"})
                assert await _closed(ws_) == (4001, "Invalid or expired session")
            assert "user-admin" not in dash._connections_by_user
        run_ws_scenario(scenario)

    def test_a_socket_whose_session_holds_stays_open(self, temp_db):
        async def scenario():
            async with dashboard_connection(session_cookie()) as ws_:
                await drain_startup(ws_)
                assert dash.revalidate_user("user-admin") == 1
                await asyncio.sleep(0.3)
                assert ws_.closed is None
                ws_.client_send({"type": "ping"})
                await ws_.expect({"type": "pong", "build_id": ANY})
            ws_.no_more_frames()
        run_ws_scenario(scenario)

    def test_nobody_to_wake_is_zero(self, temp_db):
        assert dash.revalidate_user("user-nobody") == 0


class TestTick:
    def test_the_tick_finds_a_revoked_sign_in_with_no_client_message(self, temp_db, monkeypatch):
        monkeypatch.setattr(dash, "_AUTHZ_REVALIDATE_S", 1)
        cookie = session_cookie()

        async def scenario():
            async with dashboard_connection(cookie) as ws_:
                await drain_startup(ws_)
                session_revocation.revoke(validate_session_jwt(cookie), lifetime_s=60)
                await ws_.expect({"type": "error", "message": "Session expired: please sign in again"},
                                 timeout=4.0)
                assert await _closed(ws_) == (4001, "Invalid or expired session")
        run_ws_scenario(scenario)


class TestViewedChat:
    def test_a_chat_the_person_may_no_longer_open_closes_the_socket(self, temp_db, monkeypatch):
        from core.session.session_state import _dashboard_notify_queues
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws_:
                await drain_startup(ws_)
                chat_id, sid = await warm_new_chat(ws_, layer, slug)
                assert sid in _dashboard_notify_queues
                # The chat changes hands and the person is no longer a
                # platform admin (who may open every chat): the resume's
                # rule now refuses it.
                with get_conn() as conn:
                    conn.execute("UPDATE chats SET user_sub='user-viewer' WHERE id=%s", (chat_id,))
                    conn.execute("UPDATE users SET role='member' WHERE sub='user-admin'")
                    conn.commit()
                assert dash.revalidate_user("user-admin") == 1
                await ws_.expect({"type": "error", "message": "You no longer have access to this chat"})
                assert await _closed(ws_) == (1000, "access changed")
            assert sid not in _dashboard_notify_queues
        run_ws_scenario(scenario)


class TestOffboarding:
    def test_the_event_wakes_the_persons_sockets(self, temp_db):
        from services.agents import offboarding
        dash.register_offboarding()
        event = offboarding.OffboardEvent(
            sub="user-admin", reason=offboarding.REMOVED,
            agents=(offboarding.AgentLoss("pa", "manager", ""),), actor_sub="",
        )
        cookie = session_cookie()

        async def scenario():
            async with dashboard_connection(cookie) as ws_:
                await drain_startup(ws_)
                session_revocation.revoke(validate_session_jwt(cookie), lifetime_s=60)
                await offboarding.on_user_offboarded(event)
                await ws_.expect({"type": "error", "message": "Session expired: please sign in again"})
                assert await _closed(ws_) == (4001, "Invalid or expired session")
        try:
            run_ws_scenario(scenario)
        finally:
            offboarding.unsubscribe("dashboard-sockets")


class TestConnectPhase:
    def test_a_connect_phase_that_raises_leaves_no_ticker_and_no_entry(self, temp_db, monkeypatch):
        from storage.automation import notification_store

        def _boom(sub):
            raise RuntimeError("database gone")

        monkeypatch.setattr(notification_store, "get_unread_count", _boom)

        async def scenario():
            with pytest.raises(RuntimeError):
                async with dashboard_connection(session_cookie()) as ws_:
                    await asyncio.sleep(0.2)
                    assert ws_.accepted
            assert "user-admin" not in dash._connections_by_user
            tickers = [t for t in asyncio.all_tasks()
                       if "_revalidation_ticker" in getattr(t.get_coro(), "__qualname__", "")]
            assert tickers == []
        run_ws_scenario(scenario)


def _bare_connection():
    """A connection object with only the state the revalidation reads."""
    import collections
    conn = object.__new__(dash.DashboardConnection)
    conn._client_pushback = collections.deque()
    conn._ended = asyncio.Event()
    conn._closed_by_revalidation = False
    conn._last_authz_check = 0.0
    conn._held_gate = None
    conn.chat_id = None
    return conn


class TestAfterTheClose:
    @pytest.mark.asyncio
    async def test_a_message_in_hand_is_dropped_once_the_connection_ended(self):
        from fastapi import WebSocketDisconnect
        conn = _bare_connection()
        conn._client_pushback.append('{"type": "chat", "text": "queued"}')
        conn._ended.set()
        with pytest.raises(WebSocketDisconnect):
            await conn._receive_client_text()

    @pytest.mark.asyncio
    async def test_a_socket_the_revalidation_closed_never_holds_again(self):
        conn = _bare_connection()
        conn._closed_by_revalidation = True
        conn._last_authz_check = time.time()          # inside the throttle window
        assert await conn._session_still_holds() is False

    @pytest.mark.asyncio
    async def test_a_client_message_and_a_tick_in_one_minute_read_once(self, monkeypatch):
        conn = _bare_connection()
        reads = []

        def _read():
            reads.append(1)
            return True

        monkeypatch.setattr(conn, "_revalidate_session", _read)
        assert await asyncio.gather(conn._session_still_holds(), conn._session_still_holds()) == [True, True]
        assert await conn._session_still_holds() is True
        assert reads == [1]

    @pytest.mark.asyncio
    async def test_the_close_rescue_runs_no_queued_turn_after_a_revalidation_close(self, monkeypatch):
        from unittest.mock import AsyncMock
        conn = _bare_connection()
        conn._closed_by_revalidation = True
        conn._warmup_abort_chat = None
        conn.notify_queue = asyncio.Queue()
        conn.user_sub = "user-a"
        layer = AsyncMock()
        monkeypatch.setattr(conn, "_resolve_layer_for_chat_async", AsyncMock(return_value=layer))
        ran = AsyncMock()
        monkeypatch.setattr(conn, "_run_kick_headless", ran)
        extract = AsyncMock(return_value=[
            {"chat_id": "chat-1", "session_id": "sid-1", "text": "first turn"},
        ])
        monkeypatch.setattr(dash, "_extract_server_kicks", extract)
        await conn._rescue_queued_kicks()
        await asyncio.sleep(0)
        layer.abort.assert_awaited_once_with("sid-1")
        ran.assert_not_called()
        # A delegate result left on the queue is parked for the socket's person.
        extract.assert_awaited_once_with(conn.notify_queue, person="user-a")
