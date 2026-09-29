"""Characterization (golden-master) suite for ``ws_dashboard_handler`` — part 1.

Pins the CURRENT outbound-frame sequences of the dashboard WebSocket handler,
bugs and all, as the safety net for decomposing ``ws/dashboard.py``. Any
assertion change during the refactor is a red flag: stop and justify it.

Covered here: connection auth gate, connect-time startup frames, ping/close,
dispatcher error paths, warmup validation, the full backgrounded new-chat
warmup, and a full user chat turn streamed through the real pump.
Further flows (resume, permissions, PTY, server notifications, abort) live in
their own modules alongside this one.
"""

import asyncio
import uuid

from tests.fixtures.ws_dashboard_harness import (
    ANY,
    TEST_MODEL,
    FakeExecutionLayer,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    set_username,
    stub_dashboard_seams,
    warm_new_chat,
)


# ---------------------------------------------------------------------------
# Auth gate — the handler closes 4001 BEFORE accepting.
# ---------------------------------------------------------------------------

class TestAuthGate:
    def test_missing_cookie_closes_4001(self, temp_db):
        async def scenario():
            from ws.dashboard import ws_dashboard_handler
            from tests.fixtures.ws_dashboard_harness import FakeDashboardWebSocket
            ws = FakeDashboardWebSocket(cookie=None)
            await ws_dashboard_handler(ws)
            assert ws.closed == (4001, "No session cookie")
            assert not ws.accepted
            assert ws.sent == []
        run_ws_scenario(scenario)

    def test_invalid_cookie_closes_4001(self, temp_db):
        async def scenario():
            from ws.dashboard import ws_dashboard_handler
            from tests.fixtures.ws_dashboard_harness import FakeDashboardWebSocket
            ws = FakeDashboardWebSocket(cookie="not-a-jwt")
            await ws_dashboard_handler(ws)
            assert ws.closed == (4001, "Invalid or expired session")
            assert not ws.accepted
            assert ws.sent == []
        run_ws_scenario(scenario)

    def test_unknown_user_closes_4001(self, temp_db):
        async def scenario():
            from ws.dashboard import ws_dashboard_handler
            from tests.fixtures.ws_dashboard_harness import FakeDashboardWebSocket
            ws = FakeDashboardWebSocket(
                cookie=session_cookie(sub="user-ghost", email="g@test.com",
                                      name="Ghost", role="admin"))
            await ws_dashboard_handler(ws)
            assert ws.closed == (4001, "User not found")
            assert not ws.accepted
            assert ws.sent == []
        run_ws_scenario(scenario)


    # ── the Origin rule ──────────────────────────────

    def _origin_scenario(self, origin, *, host="otodock.example", public=""):
        import config
        from ws import dashboard

        async def scenario():
            old = config.DASHBOARD_PUBLIC_URL
            config.DASHBOARD_PUBLIC_URL = public
            try:
                from tests.fixtures.ws_dashboard_harness import FakeDashboardWebSocket
                headers = {"host": host}
                if origin is not None:
                    headers["origin"] = origin
                ws = FakeDashboardWebSocket(cookie=session_cookie(), headers=headers)
                task = asyncio.create_task(dashboard.ws_dashboard_handler(ws))
                for _ in range(100):
                    if ws.accepted or ws.closed or task.done():
                        break
                    await asyncio.sleep(0.01)
                if not task.done():
                    ws.client_disconnect()
                await asyncio.wait_for(task, 5)
                return ws
            finally:
                config.DASHBOARD_PUBLIC_URL = old
        return scenario

    def test_foreign_origin_closes_4003_before_accept(self, temp_db):
        out = {}

        async def run():
            out["ws"] = await self._origin_scenario("https://www.example")()
        run_ws_scenario(run)
        ws = out["ws"]
        assert ws.closed == (4003, "Origin not allowed") and not ws.accepted and ws.sent == []

    def test_null_origin_closes_4003_before_accept(self, temp_db):
        out = {}

        async def run():
            out["ws"] = await self._origin_scenario("null")()
        run_ws_scenario(run)
        assert out["ws"].closed[0] == 4003 and not out["ws"].accepted

    def test_own_public_and_missing_origins_are_accepted(self, temp_db):
        out = []

        async def run():
            out.append(await self._origin_scenario(None)())
            out.append(await self._origin_scenario("http://otodock.example")())
            out.append(await self._origin_scenario(
                "https://agents.example.com", host="127.0.0.1:8400",
                public="https://agents.example.com")())
        run_ws_scenario(run)
        assert all(ws.accepted for ws in out)

    # ── the gate and the password timeline on the socket ───────────

    def test_a_held_session_closes_4403_after_accept_with_nothing_sent(self, temp_db):
        from auth import providers
        from storage import database as db
        from ws import dashboard
        providers.clear_auth_gate_caches()
        db.update_user_auth_fields("user-viewer", must_change_password=True)

        async def scenario():
            from tests.fixtures.ws_dashboard_harness import FakeDashboardWebSocket
            before = dict(dashboard._dashboard_notify_queues)
            ws = FakeDashboardWebSocket(cookie=session_cookie(
                sub="user-viewer", email="viewer@test.com", name="Viewer User", role="member"))
            await dashboard.ws_dashboard_handler(ws)
            assert ws.accepted and ws.closed == (4403, "must_change_password")
            assert ws.sent == []
            assert dashboard._dashboard_notify_queues == before
        run_ws_scenario(scenario)

    def test_a_cookie_older_than_the_password_change_closes_4001(self, temp_db):
        import time as _time
        from datetime import datetime, timezone

        import jwt

        import config
        from storage.pg import get_conn
        from ws import dashboard
        with get_conn() as conn:
            conn.execute("UPDATE users SET password_changed_at=%s WHERE sub='user-admin'",
                         (datetime.now(timezone.utc).isoformat(),))
            conn.commit()
        stale = jwt.encode({"purpose": "session", "sub": "user-admin", "email": "admin@test.com",
                            "name": "Admin User", "role": "admin", "auth_provider": "local",
                            "iat": int(_time.time()) - 3600, "exp": int(_time.time()) + 3600},
                           config.JWT_SECRET, algorithm="HS256")

        async def scenario():
            from tests.fixtures.ws_dashboard_harness import FakeDashboardWebSocket
            ws = FakeDashboardWebSocket(cookie=stale)
            await dashboard.ws_dashboard_handler(ws)
            assert ws.closed == (4001, "Invalid or expired session") and not ws.accepted
        run_ws_scenario(scenario)

    def test_revalidation_finds_a_gate_that_started_mid_session(self, temp_db):
        from auth import providers
        from storage import database as db
        from ws.dashboard import DashboardConnection
        from tests.fixtures.ws_dashboard_harness import FakeDashboardWebSocket
        providers.clear_auth_gate_caches()
        ws = FakeDashboardWebSocket(cookie=session_cookie())
        conn = DashboardConnection(ws, user_sub="user-admin", user=db.get_user("user-admin"))
        assert conn._revalidate_session() and conn._held_gate == ""
        db.update_user_auth_fields("user-admin", must_change_password=True)
        assert conn._revalidate_session() and conn._held_gate == "must_change_password"


# ---------------------------------------------------------------------------
# Connection lifecycle + dispatcher error paths.
# ---------------------------------------------------------------------------

class TestConnectionLifecycle:
    def test_startup_frames_ping_and_clean_close(self, temp_db):
        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                assert not ws.accepted  # accept happens inside the handler
                await drain_startup(ws)
                assert ws.accepted

                ws.client_send({"type": "ping"})
                await ws.expect({"type": "pong", "build_id": ANY})

                ws.client_send({"type": "close"})
            # clean close: the handler returns without closing the socket
            assert ws.closed is None
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_disconnect_mid_idle_exits_cleanly(self, temp_db):
        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_disconnect()
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_invalid_json_and_unknown_type(self, temp_db):
        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)

                ws.client_send_raw("{not json")
                await ws.expect({"type": "error", "message": "Invalid JSON"})

                ws.client_send({"type": "bogus_frame"})
                await ws.expect(
                    {"type": "error",
                     "message": "Unknown message type: bogus_frame"})
        run_ws_scenario(scenario)

    def test_connect_snapshot_filters_to_visible_chats(self, temp_db,
                                                       monkeypatch):
        # The connect-time chat_status_snapshot carries only streaming chats
        # this viewer may see: their own + shared-only chats (synthetic
        # agent:: owner) of accessible agents — never another user's
        # personal chat, admin or not.
        from core.session import session_state
        slug = make_test_agent()
        temp_db.create_chat("snap-own", "user-admin", slug)
        temp_db.create_chat("snap-other", "user-viewer", slug)
        temp_db.create_chat("snap-shared", f"agent::{slug}", slug)
        for cid in ("snap-own", "snap-other", "snap-shared"):
            monkeypatch.setitem(session_state._chat_streaming_state, cid,
                                {"streaming": True})

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await ws.expect({"type": "notification_count", "count": 0})
                await ws.expect({"type": "satellite_update_sync",
                                 "inflight": []})
                snap = await ws.expect({"type": "chat_status_snapshot",
                                        "chat_ids": ANY})
                assert set(snap["chat_ids"]) == {"snap-own", "snap-shared"}
        run_ws_scenario(scenario)

    def test_connect_snapshot_hides_shared_chats_from_unassigned_member(
            self, temp_db, monkeypatch):
        # An unassigned member gets the EMPTY snapshot even while a shared-only
        # chat is streaming (drain_startup pins the empty golden frame).
        from core.session import session_state
        slug = make_test_agent()
        temp_db.create_chat("snap-shared", f"agent::{slug}", slug)
        monkeypatch.setitem(session_state._chat_streaming_state, "snap-shared",
                            {"streaming": True})

        async def scenario():
            cookie = session_cookie(sub="user-viewer", email="viewer@test.com",
                                    name="Viewer User", role="member")
            async with dashboard_connection(cookie) as ws:
                await drain_startup(ws)
        run_ws_scenario(scenario)

    def test_chat_read_marks_and_echoes(self, temp_db):
        # chat_read upserts the viewer's read marker under the chat's owner
        # identity and the echo returns on the sender's own connection (other
        # tabs / shared-chat viewers drop the unread dot live off the same
        # broadcast).
        slug = make_test_agent()
        temp_db.create_chat("read-1", "user-admin", slug)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "chat_read", "chat_id": "read-1"})
                await ws.expect({"type": "chat_read", "chat_id": "read-1"})
        run_ws_scenario(scenario)

        from storage.pg import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT user_sub FROM chat_reads WHERE chat_id='read-1'"
            ).fetchall()
        assert [dict(r)["user_sub"] for r in rows] == ["user-admin"]


# ---------------------------------------------------------------------------
# Warmup — validation errors.
# ---------------------------------------------------------------------------

class TestWarmupValidation:
    def test_warmup_without_agent(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "warmup"})
                await ws.expect({"type": "error",
                                 "message": "Agent name required"})
                # warmup dispatch always follows with a fresh unread count
                await ws.expect({"type": "notification_count", "count": 0})
        run_ws_scenario(scenario)

    def test_warmup_access_denied_for_unassigned_member(self, temp_db,
                                                        monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()

        async def scenario():
            cookie = session_cookie(sub="user-viewer", email="viewer@test.com",
                                    name="Viewer User", role="member")
            async with dashboard_connection(cookie) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "warmup", "agent": slug})
                await ws.expect(
                    {"type": "error",
                     "message": f"Access denied for agent '{slug}'"})
                await ws.expect({"type": "notification_count", "count": 0})
        run_ws_scenario(scenario)

    def test_warmup_nonexistent_agent(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "warmup", "agent": "no-such-agent"})
                await ws.expect(
                    {"type": "error",
                     "message": "Agent 'no-such-agent' no longer exists"})
                await ws.expect({"type": "notification_count", "count": 0})
        run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# Full new-chat warmup: warmup_started inline, spawn backgrounded,
# warmup_ready from the tail.
# ---------------------------------------------------------------------------

class TestNewChatWarmup:
    def test_new_chat_warmup_frame_sequence(self, temp_db, monkeypatch):
        layer = FakeExecutionLayer()
        layer.start_gate = asyncio.Event()  # hold the backgrounded spawn
        slots = stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)

                ws.client_send({"type": "warmup", "agent": slug})
                started = await ws.expect({
                    "type": "warmup_started",
                    "chat_id": ANY,
                    "agent": slug,
                    "execution_path": "claude-code-cli",
                    "execution_target": "local",
                    "new_chat": True,
                })
                chat_id = started["chat_id"]
                # dispatcher continues while the spawn is gated
                await ws.expect({"type": "notification_count", "count": 0})

                layer.start_gate.set()
                ready = await ws.expect({
                    "type": "warmup_ready",
                    "session_id": ANY,
                    "chat_id": chat_id,
                    "mode": "default",
                    "model": TEST_MODEL,
                    "execution_path": "claude-code-cli",
                    "execution_target": "local",
                    "fallback_reason": None,
                    "offline_machine_name": "",
                    "interactive": False,
                })
                sid = ready["session_id"]

                # spawned fresh (not resume), chat row bound to the session
                assert [s for s, _cfg in layer.started] == [sid]
                _sid, cfg = layer.started[0]
                assert cfg.resume is False
                chat_row = temp_db.get_chat(chat_id)
                assert chat_row["session_id"] == sid
                assert chat_row["agent"] == slug
                assert chat_row["user_sub"] == "user-admin"
                assert sid in [s for s, _kw in slots.acquired]
                # The funnel waits its turn in the admission queue and names
                # the session's MCP config.
                import config as _config
                _kw = dict(slots.acquired)[sid]
                assert _kw["queue_wait_s"] == _config.ADMISSION_QUEUE_WAIT_S
                assert _kw["mcp_config_path"] == cfg.mcp_config_path
                assert _kw["user_sub"] == "user-admin"

                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)


    def test_socket_close_keeps_the_live_sessions_reservation(self, temp_db, monkeypatch):
        """Closing the socket leaves a live session's admission slot in
        place; the path that ends the session releases it."""
        layer = FakeExecutionLayer()
        slots = stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        seen = {}

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                _chat_id, sid = await warm_new_chat(ws, layer, slug)
                seen["sid"] = sid
                ws.client_disconnect()
        run_ws_scenario(scenario)
        assert seen["sid"] in [s for s, _kw in slots.acquired]
        assert seen["sid"] not in slots.released


# ---------------------------------------------------------------------------
# warmup_started.new_chat: true only for a chat THIS warmup minted. The
# dashboard moves the new-chat page's draft onto the frame's chat_id only
# then — a re-warmed existing chat's frame must never take it.
# ---------------------------------------------------------------------------

class TestWarmupStartedNewChatFlag:
    def test_existing_chat_rewarm_is_not_new(self, temp_db, monkeypatch):
        from storage import database as task_store
        layer = FakeExecutionLayer()
        layer.start_gate = asyncio.Event()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        cid = str(uuid.uuid4())
        task_store.create_chat(cid, "user-admin", slug, "default",
                               model=TEST_MODEL,
                               execution_path="claude-code-cli")
        # A bound session that is neither alive nor resumable: the send
        # re-warms the chat through the backgrounded spawn.
        task_store.update_chat(cid, session_id="dead-sid")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "warmup", "agent": slug,
                                "chat_id": cid})
                await ws.expect({
                    "type": "warmup_started", "chat_id": cid, "agent": slug,
                    "execution_path": "claude-code-cli",
                    "execution_target": "local", "new_chat": False,
                })
                await ws.expect({"type": "notification_count", "count": 0})
                layer.start_gate.set()
                ready = await ws.expect({
                    "type": "warmup_ready", "session_id": ANY,
                    "chat_id": cid, "mode": "default", "model": TEST_MODEL,
                    "execution_path": "claude-code-cli",
                    "execution_target": "local", "fallback_reason": None,
                    "offline_machine_name": "", "interactive": False,
                })
                # The dead session was not resumed: a fresh spawn, the row
                # rebound to it, the history seed flagged for the next turn.
                assert ready["session_id"] != "dead-sid"
                _sid, cfg = layer.started[0]
                assert cfg.resume is False
                row = temp_db.get_chat(cid)
                assert row["session_id"] == ready["session_id"]
                assert row["pending_history_seed"] == "resume_failed"
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_unknown_chat_id_mints_a_new_chat(self, temp_db, monkeypatch):
        layer = FakeExecutionLayer()
        layer.start_gate = asyncio.Event()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "warmup", "agent": slug,
                                "chat_id": "no-such-chat"})
                started = await ws.expect({
                    "type": "warmup_started", "chat_id": ANY, "agent": slug,
                    "execution_path": "claude-code-cli",
                    "execution_target": "local", "new_chat": True,
                })
                assert started["chat_id"] != "no-such-chat"
                await ws.expect({"type": "notification_count", "count": 0})
                layer.start_gate.set()
                await ws.expect({
                    "type": "warmup_ready", "session_id": ANY,
                    "chat_id": started["chat_id"], "mode": "default",
                    "model": TEST_MODEL, "execution_path": "claude-code-cli",
                    "execution_target": "local", "fallback_reason": None,
                    "offline_machine_name": "", "interactive": False,
                })
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# Full chat turn through the REAL pump.
# ---------------------------------------------------------------------------

class TestChatTurn:
    def test_chat_turn_streams_text_and_done(self, temp_db, monkeypatch):
        from core.events.common_events import CommonEvent, TEXT, DONE

        layer = FakeExecutionLayer()
        layer.turn_events = [
            CommonEvent(type=TEXT, data={"content": "Hello there!"}),
            CommonEvent(type=DONE, data={}),
        ]
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)

                ws.client_send({"type": "warmup", "agent": slug})
                started = await ws.expect({
                    "type": "warmup_started", "chat_id": ANY, "agent": slug,
                    "execution_path": "claude-code-cli",
                    "execution_target": "local", "new_chat": True,
                })
                chat_id = started["chat_id"]
                await ws.expect({"type": "notification_count", "count": 0})
                ready = await ws.expect({
                    "type": "warmup_ready", "session_id": ANY,
                    "chat_id": chat_id, "mode": "default",
                    "model": TEST_MODEL, "execution_path": "claude-code-cli",
                    "execution_target": "local", "fallback_reason": None,
                    "offline_machine_name": "", "interactive": False,
                })
                sid = ready["session_id"]

                ws.client_send({"type": "chat", "text": "Say hello",
                                "chat_id": chat_id})

                await ws.expect({"type": "title_updated", "chat_id": chat_id,
                                 "title": "Say hello"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.expect({
                    "type": "live_state", "chat_id": chat_id,
                    "streaming": True, "session_id": sid, "started_at": ANY,
                    "live_blocks": [], "active_tools": [], "active_agents": [],
                    "active_delegates": [], "active_commands": [],
                    "pending_permission": None, "thinking_active": False,
                    "thinking_text": "", "thinking_tokens": 0, "todos": [],
                    "goal": None, "meeting_agent": None, "meeting_participants": [],
                    "workflows": {},
                })
                await ws.expect({"type": "text", "content": "Hello there!",
                                 "chat_id": chat_id})
                await ws.expect({"type": "done", "chat_id": chat_id})

                # pump broadcasts drained by the main loop between turns
                # The queued turn-start "streaming" broadcast is dropped as stale at
                # drain time (the turn already ended) — only the "ready" flows.
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                # ephemeral end-of-turn ping, routed to the sending device
                # (title = the agent DISPLAY NAME since 2026-08-29, via agent_label)
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": "WS Dash Test finished",  # display name (harness agent), not slug
                                 "body": "Response ready"})

                # the turn hit the layer with the raw prompt
                assert layer.messages == [
                    (sid, "Say hello", {"inject_time": True}),
                ]
                # user + assistant rows persisted
                msgs = temp_db.get_chat_messages(chat_id)
                roles = [(m["role"], m["content"]) for m in msgs]
                assert roles == [
                    ("user", "Say hello"),
                    ("assistant", "Hello there!"),
                ]
                # the pump's end stamped the unread-indicator timestamp
                assert temp_db.get_chat(chat_id)["last_response_at"]

                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_chat_turn_with_photo_persists_saved_path(self, temp_db,
                                                      monkeypatch):
        """A chat-attached photo persists its SAVED upload path in the user
        row's event_data (name alone can't render after a reload — the
        frontend serves past photos via GET /v1/agents/<agent>/files/<path>)."""
        import base64 as b64
        import io
        import json as jsonlib

        from PIL import Image

        from core.events.common_events import CommonEvent, TEXT, DONE

        layer = FakeExecutionLayer()
        layer.turn_events = [
            CommonEvent(type=TEXT, data={"content": "nice photo"}),
            CommonEvent(type=DONE, data={}),
        ]
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        buf = io.BytesIO()
        Image.new("RGB", (4, 4), (200, 30, 30)).save(buf, format="PNG")
        data_url = ("data:image/png;base64,"
                    + b64.b64encode(buf.getvalue()).decode("ascii"))

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "chat", "text": "look at this",
                                "chat_id": chat_id,
                                "images": [{"data": data_url,
                                            "name": "pic.png"}]})
                await ws.expect({"type": "title_updated", "chat_id": chat_id,
                                 "title": "look at this"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.expect({
                    "type": "live_state", "chat_id": chat_id,
                    "streaming": True, "session_id": sid, "started_at": ANY,
                    "live_blocks": [], "active_tools": [],
                    "active_agents": [], "active_delegates": [],
                    "active_commands": [], "pending_permission": None,
                    "thinking_active": False, "thinking_text": "",
                    "thinking_tokens": 0, "todos": [], "goal": None, "meeting_agent": None,
                    "meeting_participants": [], "workflows": {},
                })
                await ws.expect({"type": "text", "content": "nice photo",
                                 "chat_id": chat_id})
                await ws.expect({"type": "done", "chat_id": chat_id})
                # The queued turn-start "streaming" broadcast is dropped as stale at
                # drain time (the turn already ended) — only the "ready" flows.
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": "WS Dash Test finished",  # display name (harness agent), not slug
                                 "body": "Response ready"})

                # the CLI prompt got the sandbox-virtual Read path appended
                _sid, prompt, _kw = layer.messages[0]
                assert prompt.startswith(
                    "look at this\n\nThe user has attached 1 image(s).")
                assert "/users/admin/workspace/uploads/photos/img_" in prompt

                # user row event_data carries name + agent-relative saved path
                user_row = temp_db.get_chat_messages(chat_id)[0]
                meta = jsonlib.loads(user_row["event_data"])
                assert list(meta) == ["images"]
                img_meta, = meta["images"]
                assert img_meta["name"] == "pic.png"
                assert img_meta["path"].startswith(
                    "users/admin/workspace/uploads/photos/img_")
                # An opaque PNG is re-encoded as a JPEG (only transparency,
                # a palette or a deep image keeps PNG).
                assert img_meta["path"].endswith(".jpg")

                import config as cfg
                assert (cfg.AGENTS_DIR / slug / img_meta["path"]).is_file()

                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)
