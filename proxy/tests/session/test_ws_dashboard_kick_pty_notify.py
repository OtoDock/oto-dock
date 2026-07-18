"""Characterization suite for ``ws_dashboard_handler`` — part 3.

Pins the server-kicked FIRST turn (warmup carrying text → durable kick via
the notify queue), the PTY-viewer surface (interactive resume, pty_attach
replay, input/resize routing, permission/artifact/close forwarding), and
server-notification delivery (active toast vs idle silent). Same
golden-master rules as parts 1-2.
"""

import asyncio
import base64
import uuid

from tests.fixtures.ws_dashboard_harness import (
    ANY,
    TEST_MODEL,
    FakeExecutionLayer,
    FakeInteractiveSession,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    set_username,
    stub_dashboard_seams,
    sync_dispatch,
    warm_new_chat,
)


class TestServerKickFirstTurn:
    def test_warmup_with_text_runs_kicked_turn(self, temp_db, monkeypatch):
        from core.events.common_events import CommonEvent, TEXT, DONE

        layer = FakeExecutionLayer()
        layer.turn_events = [
            CommonEvent(type=TEXT, data={"content": "answer"}),
            CommonEvent(type=DONE, data={}),
        ]
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug,
                                                   text="cold first prompt")

                # the kick drains from the notify queue and streams inline —
                # NO title_updated (title persisted at send-time) and NO
                # duplicate user row.
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
                await ws.expect({"type": "text", "content": "answer",
                                 "chat_id": chat_id})
                await ws.expect({"type": "done", "chat_id": chat_id})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": f"{slug} finished",
                                 "body": "Response ready"})

                assert [p for _s, p, _k in layer.messages] == [
                    "cold first prompt",
                ]
                chat_row = temp_db.get_chat(chat_id)
                assert chat_row["title"] == "cold first prompt"
                msgs = temp_db.get_chat_messages(chat_id)
                assert [(m["role"], m["content"]) for m in msgs] == [
                    ("user", "cold first prompt"),
                    ("assistant", "answer"),
                ]
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)


class TestPtyViewer:
    def _setup(self, monkeypatch, scrollback: bytes = b""):
        from core.session import interactive_session
        from core.session.session_state import set_session_mode
        from storage import database as task_store

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        sid = str(uuid.uuid4())
        cid = str(uuid.uuid4())
        task_store.create_chat(cid, "user-admin", slug, "default",
                               model=TEST_MODEL,
                               execution_path="claude-code-cli")
        task_store.update_chat(cid, session_id=sid)
        set_session_mode(sid, "default")
        isess = FakeInteractiveSession(sid, cid, scrollback=scrollback,
                                       tui_theme="light")
        monkeypatch.setitem(interactive_session._sessions, sid, isess)
        return slug, cid, sid, isess

    async def _resume_interactive(self, ws, cid, sid, turn_open=False):
        ws.client_send({"type": "resume_chat", "chat_id": cid})
        await ws.expect({
            "type": "chat_history", "chat_id": cid, "agent": ANY, "messages": [],
            "has_more": False, "restore": {"todos": [], "meeting": None, "goal": None},
            "plans": [], "total_cost": 0, "context_used": 0,
            "context_max": 0, "cache_read": 0, "cache_write": 0,
            "output_tokens": 0, "execution_path": "claude-code-cli",
            "execution_mode": "", "model": TEST_MODEL, "mode": "default",
        })
        await ws.expect({
            "type": "warmup_ready", "session_id": sid, "chat_id": cid,
            "mode": "default", "model": TEST_MODEL,
            "execution_path": "claude-code-cli", "interactive": True,
            "turn_open": turn_open,
        })
        await ws.expect({"type": "queue_snapshot", "chat_id": cid,
                         "messages": []})

    def test_interactive_resume_attach_and_io(self, temp_db, monkeypatch):
        slug, cid, sid, isess = self._setup(monkeypatch,
                                            scrollback=b"$ claude\r\n")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                await self._resume_interactive(ws, cid, sid)

                # client mounts its terminal, then attaches
                ws.client_send({"type": "pty_attach", "chat_id": cid})
                await ws.expect({
                    "type": "pty_status", "chat_id": cid, "session_id": sid,
                    "state": "attached", "tui_theme": "light",
                })
                await ws.expect({
                    "type": "pty_output", "chat_id": cid, "session_id": sid,
                    "data": base64.b64encode(b"$ claude\r\n").decode(),
                    "replay": True,
                })

                # keystrokes route by the VIEWED session id
                ws.client_send({
                    "type": "pty_input",
                    "data": base64.b64encode(b"ls\r").decode(),
                })
                ws.client_send({"type": "pty_resize", "rows": 50, "cols": 132})
                await sync_dispatch(ws)
                assert isess.inputs == [b"ls\r"]
                assert isess.resizes == [(50, 132)]

                # live PTY output mirrors to the socket
                await isess.output_listener(b"file-a\r\n")
                await ws.expect({
                    "type": "pty_output", "chat_id": cid, "session_id": sid,
                    "data": base64.b64encode(b"file-a\r\n").decode(),
                })

                # blocking prompt + artifact + exit forwarding
                await isess.on_perm_event({
                    "event_type": "permission_prompt",
                    "request_id": "req-1", "tool_name": "Bash",
                    "tool_input": {"command": "rm x"},
                })
                await ws.expect({
                    "type": "pty_permission", "kind": "permission",
                    "chat_id": cid, "session_id": sid, "request_id": "req-1",
                    "tool_name": "Bash", "tool_input": {"command": "rm x"},
                })
                await isess.on_close(isess, "exited")
                await ws.expect({
                    "type": "pty_exit", "chat_id": cid, "session_id": sid,
                    "reason": "exited",
                })
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_interactive_resume_mid_turn_reports_turn_open(self, temp_db,
                                                           monkeypatch):
        # Visiting a MID-TURN interactive chat must carry the live turn state:
        # the resume's chat_history makes the client reset the chat to 'ready'
        # (no pump live_state ever re-arms it), so warmup_ready.turn_open is
        # the only signal that keeps the sidebar live state truthful.
        slug, cid, sid, isess = self._setup(monkeypatch)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                # Turn opens after connect (would land in the connect-time
                # chat_status_snapshot otherwise), then the user visits.
                isess._turn_open = True
                await self._resume_interactive(ws, cid, sid, turn_open=True)
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_pty_attach_guards_chat_mismatch(self, temp_db, monkeypatch):
        slug, cid, sid, isess = self._setup(monkeypatch)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                await self._resume_interactive(ws, cid, sid)

                # a fast chat-switch must not attach the wrong terminal
                ws.client_send({"type": "pty_attach",
                                "chat_id": "some-other-chat"})
                await sync_dispatch(ws)
                assert isess.output_listener is None
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)


class TestNotificationDelivery:
    def test_active_toast_vs_idle_silent(self, temp_db, monkeypatch):
        from services.notifications import notification_manager

        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        delivery = {
            "id": 7, "notification_id": 3, "title": "Backup done",
            "body": "45GB transferred", "severity": "success",
            "scope": "user", "source": "task",
            "delivered_at": "2026-07-07T00:00:00+00:00",
            "agent_slug": "", "chat_id": "",
        }
        payload = {
            "id": 7, "notification_id": 3, "title": "Backup done",
            "body": "45GB transferred", "severity": "success",
            "scope": "user", "source": "task",
            "delivered_at": "2026-07-07T00:00:00+00:00",
            "agent_slug": "", "chat_id": "",
        }

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)

                # fresh connections are ACTIVE → toast event
                await notification_manager._deliver_to_user(
                    "user-admin", dict(delivery))
                await ws.expect({"type": "notification",
                                 "delivery": payload})

                # after user_idle the same delivery goes silent (inbox/badge)
                ws.client_send({"type": "user_idle"})
                await sync_dispatch(ws)
                await notification_manager._deliver_to_user(
                    "user-admin", dict(delivery))
                await ws.expect({"type": "notification_silent",
                                 "delivery": payload})

                # user_active restores the toast path
                ws.client_send({"type": "user_active"})
                await sync_dispatch(ws)
                await notification_manager._deliver_to_user(
                    "user-admin", dict(delivery))
                await ws.expect({"type": "notification",
                                 "delivery": payload})
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_user_idle_away_flag_reaches_connection_state(self, temp_db, monkeypatch):
        # The FE's presence tri-state rides user_idle as `away` (visible but
        # input-idle). Pin the dispatch wiring: the flag must land on the
        # ConnectionInfo (it drives the end-of-turn FCM fallthrough), and both
        # user_active and a plain (hidden-tab) user_idle must clear it.
        from services.notifications import notification_manager

        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())

        def _conn_state():
            conns = notification_manager._user_connections.get("user-admin", [])
            assert len(conns) == 1
            return conns[0].active, conns[0].away

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                assert _conn_state() == (True, False)

                ws.client_send({"type": "user_idle", "away": True})
                await sync_dispatch(ws)
                assert _conn_state() == (False, True)

                ws.client_send({"type": "user_idle"})
                await sync_dispatch(ws)
                assert _conn_state() == (False, False)

                ws.client_send({"type": "user_idle", "away": True})
                await sync_dispatch(ws)
                ws.client_send({"type": "user_active"})
                await sync_dispatch(ws)
                assert _conn_state() == (True, False)
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)


class TestInteractiveColdFirstPrompt:
    """A cold send's text rides the warmup and the SERVER delivers it into the
    fresh interactive session (stamped, tailer-noted) — the client holds no
    stash, so the prompt survives switching chats / reloading mid-warmup
    (the lost-prompt bug: warmup_ready for a non-viewed chat is dropped by the
    frontend staleness guard, and the old stash died with resetSession)."""

    def test_warmup_text_submits_server_side_stamped(self, temp_db, monkeypatch):
        from core.session import interactive_session
        from core.session import transcript_tool_events as tte
        from storage.db_settings import set_platform_setting

        registered: list[FakeInteractiveSession] = []

        class _InteractiveSpawnLayer(FakeExecutionLayer):
            """Registers the InteractiveSession the real CLI layer would
            create for an interactive agent_cfg."""

            async def start_session(self, sid, agent_cfg):
                await super().start_session(sid, agent_cfg)
                if getattr(agent_cfg, "interactive", False):
                    isess = FakeInteractiveSession(sid, "")
                    registered.append(isess)
                    monkeypatch.setitem(
                        interactive_session._sessions, sid, isess)

        layer = _InteractiveSpawnLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        set_platform_setting("interactive_cli_enabled", "true")
        monkeypatch.setattr(tte, "_sent_prompts", {})

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({
                    "type": "warmup", "agent": slug,
                    "text": "cold interactive prompt",
                    "execution_mode": "interactive",
                })
                started = await ws.next_frame()
                assert started["type"] == "warmup_started"
                chat_id = started["chat_id"]
                # Drain until the interactive warmup_ready lands (frame set
                # varies on this path — not a golden-master pin).
                for _ in range(10):
                    frame = await ws.next_frame()
                    if frame["type"] == "warmup_ready":
                        break
                else:
                    raise AssertionError("no warmup_ready")
                assert frame["chat_id"] == chat_id
                assert frame["interactive"] is True

                # The submit runs in the backgrounded spawn tail after
                # warmup_ready — poll briefly.
                for _ in range(50):
                    if registered and registered[0].submitted:
                        break
                    await asyncio.sleep(0.02)
                assert registered, "interactive session never spawned"
                isess = registered[0]
                assert len(isess.submitted) == 1
                submitted = isess.submitted[0]
                # Stamped at delivery, raw text intact.
                assert submitted.startswith("[Current time: ")
                assert submitted.endswith("\n\ncold interactive prompt")

                # Send-time persistence: the DB row is the RAW prompt (the
                # stamp is a delivery artifact) and the chat got its title.
                msgs = temp_db.get_chat_messages(chat_id)
                assert [(m["role"], m["content"]) for m in msgs] == [
                    ("user", "cold interactive prompt"),
                ]
                assert temp_db.get_chat(chat_id)["title"] == "cold interactive prompt"

                # Tailer dedup: the note now holds the STAMPED bytes — the
                # CLI journals exactly what the PTY received, so the tailer's
                # byte-for-byte consume must match or the user row lands twice.
                assert tte.consume_sent_prompt(chat_id, submitted) is True

                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)
