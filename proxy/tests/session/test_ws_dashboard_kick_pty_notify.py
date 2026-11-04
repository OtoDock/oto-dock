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
        from core.events.common_events import DONE, TEXT, CommonEvent

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
                # The queued turn-start "streaming" broadcast is dropped as stale at
                # drain time (the turn already ended) — only the "ready" flows.
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": "WS Dash Test finished",  # display name (harness agent), not slug
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
            "execution_mode": "", "model": TEST_MODEL, "mode": "ask",
            "process_alive": True,   # live PTY in the harness
        })
        await ws.expect({
            "type": "warmup_ready", "session_id": sid, "chat_id": cid,
            "mode": "ask", "model": TEST_MODEL,
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
                isess.output_listener(b"file-a\r\n")
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
                    # exit_code rides the frame so an abnormal CLI death is
                    # never a silent blank terminal (None here — no real pty).
                    "code": None,
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

    def test_a_settings_card_reaches_the_viewer_as_a_window(self, temp_db, monkeypatch):
        """The drainer's card item, its row id stamped, goes out as a
        ``pty_artifact`` carrying the card's event (api/settings) and the
        row id the window's dismissal is remembered by."""
        slug, cid, sid, isess = self._setup(monkeypatch)
        item = {"event_type": "settings_card", "setting": "user.general.wake-word",
                "label": "Wake word", "catalog_version": 3, "db_message_id": 41}

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                await self._resume_interactive(ws, cid, sid)
                ws.client_send({"type": "pty_attach", "chat_id": cid})
                await ws.expect({
                    "type": "pty_status", "chat_id": cid, "session_id": sid,
                    "state": "attached", "tui_theme": "light",
                })
                await isess.on_perm_event(item)
                await ws.expect({"type": "pty_artifact", "chat_id": cid, "session_id": sid,
                                 "event": {"type": "settings_card", "setting": "user.general.wake-word",
                                           "label": "Wake word", "catalog_version": 3,
                                           "db_message_id": 41}})
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_an_action_card_reaches_the_viewer_as_a_window(self, temp_db, monkeypatch):
        """The drainer's action card goes out as a ``pty_artifact`` like the
        settings card, with the row id stamped."""
        slug, cid, sid, isess = self._setup(monkeypatch)
        item = {"event_type": "action_card", "card_id": "card-9", "title": "Move Alpha to Eng",
                "text": "Places Alpha at the Team level of Eng.", "note": "",
                "buttons": [{"id": "go", "label": "Move", "kind": "do"}], "options": [], "db_message_id": 42}

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                await self._resume_interactive(ws, cid, sid)
                ws.client_send({"type": "pty_attach", "chat_id": cid})
                await ws.expect({
                    "type": "pty_status", "chat_id": cid, "session_id": sid,
                    "state": "attached", "tui_theme": "light",
                })
                await isess.on_perm_event(item)
                await ws.expect({"type": "pty_artifact", "chat_id": cid, "session_id": sid,
                                 "event": {"type": "action_card", "card_id": "card-9",
                                           "title": "Move Alpha to Eng",
                                           "text": "Places Alpha at the Team level of Eng.", "note": "",
                                           "buttons": [{"id": "go", "label": "Move", "kind": "do"}],
                                           "options": [], "db_message_id": 42}})
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_a_pushed_documents_token_reaches_only_its_own_person(self, temp_db, monkeypatch):
        """A push queued for Bob before a takeover that the next controller's
        drainer hands on: the frame reaches the controller without Bob's
        token. A push for the controller keeps theirs; the person field
        reaches nobody."""
        slug, cid, sid, isess = self._setup(monkeypatch)
        push = {"event_type": "document_preview", "wopi_url": "https://c/x",
                "filename": "x.docx", "file_id": "fid", "download_url": "/d",
                "snapshot_id": "", "generation": 1, "version": 1}
        frame = {"type": "document_preview", "wopi_url": "https://c/x",
                 "filename": "x.docx", "file_id": "fid", "download_url": "/d",
                 "snapshot_id": "", "generation": 1, "version": 1}

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                await self._resume_interactive(ws, cid, sid)
                ws.client_send({"type": "pty_attach", "chat_id": cid})
                await ws.expect({
                    "type": "pty_status", "chat_id": cid, "session_id": sid,
                    "state": "attached", "tui_theme": "light",
                })
                await isess.on_perm_event({**push, "access_token": "bobs",
                                           "access_token_ttl": 9, "token_person": "user-bob"})
                await ws.expect({"type": "pty_artifact", "chat_id": cid,
                                 "session_id": sid, "event": frame})
                await isess.on_perm_event({**push, "access_token": "mine",
                                           "access_token_ttl": 9, "token_person": "user-admin"})
                await ws.expect({"type": "pty_artifact", "chat_id": cid, "session_id": sid,
                                 "event": {**frame, "access_token": "mine", "access_token_ttl": 9}})
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
        # Golden-frame change note: ``href`` (the page a tap opens, SHARING.md)
        # was ADDED to the delivery payload deliberately; empty when the
        # notification has no page of its own.
        payload = {
            "id": 7, "notification_id": 3, "title": "Backup done",
            "body": "45GB transferred", "severity": "success",
            "scope": "user", "source": "task",
            "delivered_at": "2026-07-07T00:00:00+00:00",
            "agent_slug": "", "chat_id": "", "href": "",
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
        from core.session import interactive_session, transcript_tool_events as tte
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


class TestInteractiveColdFirstPromptArgv:
    """An engine whose TUI takes the cold prompt as a launch argument
    (``runtime.interactive_first_prompt_via_argv``) gets it stamped by the
    SERVER where the argv delivery is decided — time and viewer-focus line,
    the same shape the PTY submit path builds — so the dashboard sends raw
    text on every engine and the rollout tailer's duplicate-skip meets what
    the TUI journals (engine-contract phase 5e)."""

    def _run(self, temp_db, monkeypatch, *, focus: str):
        import dataclasses

        from core.session import interactive_session, transcript_tool_events as tte
        from storage.db_settings import set_platform_setting

        registered: list[FakeInteractiveSession] = []

        class _InteractiveSpawnLayer(FakeExecutionLayer):
            async def start_session(self, sid, agent_cfg):
                await super().start_session(sid, agent_cfg)
                if getattr(agent_cfg, "interactive", False):
                    isess = FakeInteractiveSession(sid, "")
                    registered.append(isess)
                    monkeypatch.setitem(interactive_session._sessions, sid, isess)

        layer = _InteractiveSpawnLayer()
        # The harness's real Claude descriptor, with the one flag the argv
        # decision reads flipped — the agent stays on the harness's path.
        layer.capabilities = dataclasses.replace(
            layer.capabilities,
            runtime=dataclasses.replace(
                layer.capabilities.runtime, interactive_first_prompt_via_argv=True,
            ),
        )
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        set_platform_setting("interactive_cli_enabled", "true")
        monkeypatch.setattr(tte, "_sent_prompts", {})
        from services.apps import focus_context
        monkeypatch.setattr(focus_context, "focus_line", lambda *a, **k: focus)

        out: dict = {}

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({
                    "type": "warmup", "agent": slug,
                    "text": "cold argv prompt",
                    "execution_mode": "interactive",
                })
                started = await ws.next_frame()
                assert started["type"] == "warmup_started"
                chat_id = started["chat_id"]
                for _ in range(10):
                    frame = await ws.next_frame()
                    if frame["type"] == "warmup_ready":
                        break
                else:
                    raise AssertionError("no warmup_ready")
                assert frame["interactive"] is True
                for _ in range(50):
                    if layer.started:
                        break
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.1)  # the spawn tail's submit window
                _sid, agent_cfg = layer.started[0]
                out["stamped"] = agent_cfg.interactive_first_prompt
                out["submitted"] = list(registered[0].submitted) if registered else []
                out["rows"] = [(m["role"], m["content"]) for m in temp_db.get_chat_messages(chat_id)]
                out["title"] = temp_db.get_chat(chat_id)["title"]
                out["consumed"] = tte.consume_sent_prompt(chat_id, agent_cfg.interactive_first_prompt)
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)
        return out

    def test_argv_prompt_is_stamped_server_side_and_noted(self, temp_db, monkeypatch):
        out = self._run(temp_db, monkeypatch, focus="")
        stamped = out["stamped"]
        assert stamped.startswith("[Current time: ")
        assert stamped.endswith("\n\ncold argv prompt")
        assert "looking at the app" not in stamped
        # The TUI auto-runs the argument: nothing is submitted through the PTY.
        assert out["submitted"] == []
        # The DB row is the RAW prompt and the title comes from it.
        assert out["rows"] == [("user", "cold argv prompt")]
        assert out["title"] == "cold argv prompt"
        # The note holds the STAMPED bytes the rollout will journal.
        assert out["consumed"] is True

    def test_argv_prompt_carries_the_focus_line_after_the_time(self, temp_db, monkeypatch):
        focus = '[The user is looking at the app "Home" (home) right now.]'
        out = self._run(temp_db, monkeypatch, focus=focus)
        first, second, rest = out["stamped"].split("\n\n", 2)
        assert first.startswith("[Current time: ")
        assert second == focus
        assert rest == "cold argv prompt"
        assert out["rows"] == [("user", "cold argv prompt")]


def _photo_data_url() -> str:
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


async def _frames_until(ws, kind: str, limit: int = 60) -> list[dict]:
    frames = []
    for _ in range(limit):
        frame = await ws.next_frame()
        frames.append(frame)
        if frame["type"] == kind:
            return frames
    raise AssertionError(f"no {kind} frame in {[f['type'] for f in frames]}")


def test_an_entry_that_names_nothing_is_no_message():
    from core.events.common_events import carries_prompt
    assert carries_prompt({"text": "hi"})
    assert carries_prompt({"images": [{"data": "data:image/png;base64,AA==", "name": "p.png"}]})
    assert carries_prompt({"images": [{"path": "users/u/workspace/uploads/photos/a.jpg"}]})
    assert carries_prompt({"files": [{"path": "users/u/workspace/uploads/files/a.pdf", "name": "a.pdf"}]})
    assert not carries_prompt({"images": [{}], "files": [{"name": "x"}]})
    assert not carries_prompt({"images": ["data:image/png;base64,AA=="], "files": None})
    assert not carries_prompt({"text": "", "images": [], "files": []})


class TestFirstMessageAttachments:
    """The first message of a chat (a new chat, or a chat whose session is
    gone) rides the warm-up: its photos are saved and its files checked when
    its row is written, so the row carries them like any other send's, the
    kick reads the same files back (no second copy), a message of photos
    alone starts its turn, and a photo that cannot be read back is told."""

    def _layer(self, monkeypatch):
        from core.events.common_events import DONE, TEXT, CommonEvent
        layer = FakeExecutionLayer()
        layer.turn_events = [
            CommonEvent(type=TEXT, data={"content": "answer"}),
            CommonEvent(type=DONE, data={}),
        ]
        stub_dashboard_seams(monkeypatch, layer)
        return layer

    def _run(self, layer, frame: dict) -> dict:
        out: dict = {}

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send(frame)
                started = await ws.next_frame()
                assert started["type"] == "warmup_started"
                out["chat_id"] = started["chat_id"]
                out["frames"] = await _frames_until(ws, "done")
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)
        return out

    def test_photos_and_files_are_saved_once_and_the_row_keeps_them(self, temp_db, monkeypatch):
        import json as jsonlib

        import config as cfg
        layer = self._layer(monkeypatch)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        doc = cfg.AGENTS_DIR / slug / "users/admin/workspace/uploads/files/plan.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("the plan")
        out = self._run(layer, {
            "type": "warmup", "agent": slug, "text": "look at these",
            "images": [{"data": _photo_data_url(), "name": "pic.png"}],
            "files": [{"path": "users/admin/workspace/uploads/files/plan.txt", "name": "plan.txt"}],
        })
        _sid, prompt, _kw = layer.messages[0]
        assert prompt.startswith("look at these\n\nThe user has attached 1 image(s).")
        assert "- /users/admin/workspace/uploads/photos/img_" in prompt
        assert "- /users/admin/workspace/uploads/files/plan.txt (" in prompt
        user_row = temp_db.get_chat_messages(out["chat_id"])[0]
        assert user_row["content"] == "look at these"
        meta = jsonlib.loads(user_row["event_data"])
        assert meta["images"][0]["name"] == "pic.png"
        assert meta["images"][0]["path"].startswith("users/admin/workspace/uploads/photos/img_")
        assert meta["files"] == [{"path": "users/admin/workspace/uploads/files/plan.txt", "name": "plan.txt"}]
        # Saved at the warm-up, read back by the kick: one file.
        photos = list((cfg.AGENTS_DIR / slug / "users/admin/workspace/uploads/photos").iterdir())
        assert [p.relative_to(cfg.AGENTS_DIR / slug).as_posix() for p in photos] == [meta["images"][0]["path"]]
        assert not [f for f in out["frames"] if f.get("subtype") == "photos_not_attached"]

    def test_a_message_of_photos_alone_starts_its_turn(self, temp_db, monkeypatch):
        import json as jsonlib
        layer = self._layer(monkeypatch)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        out = self._run(layer, {"type": "warmup", "agent": slug,
                                "images": [{"data": _photo_data_url(), "name": "pic.png"}]})
        _sid, prompt, _kw = layer.messages[0]
        assert prompt.startswith("\n\nThe user has attached 1 image(s).")
        rows = temp_db.get_chat_messages(out["chat_id"])
        assert [(r["role"], r["content"]) for r in rows] == [("user", ""), ("assistant", "answer")]
        assert jsonlib.loads(rows[0]["event_data"])["images"][0]["name"] == "pic.png"

    def test_files_alone_that_all_failed_send_nothing_and_say_why(self, temp_db, monkeypatch):
        from ws import wire_events as wire
        layer = self._layer(monkeypatch)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        out = self._run(layer, {"type": "warmup", "agent": slug,
                                "files": [{"path": "users/admin/workspace/uploads/files/gone.pdf",
                                           "name": "gone.pdf"}]})
        errors = [f for f in out["frames"] if f["type"] == "error"]
        assert [e["message"] for e in errors] == [wire.NOTHING_REACHED_THE_AGENT]
        assert out["frames"][-1] == {"type": "done", "chat_id": out["chat_id"]}
        assert layer.messages == []
        assert temp_db.get_chat_messages(out["chat_id"]) == []

    def test_an_idle_photo_send_that_reached_nothing_is_refused(self, temp_db, monkeypatch):
        from ws import wire_events as wire
        layer = self._layer(monkeypatch)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        out: dict = {}

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, _sid = await warm_new_chat(ws, layer, slug)
                ws.client_send({"type": "chat", "chat_id": chat_id, "text": "",
                                "images": [{"path": "users/admin/workspace/uploads/photos/gone.png",
                                            "name": "gone.png"}]})
                out["chat_id"] = chat_id
                out["frames"] = await _frames_until(ws, "done")
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)
        assert [f["message"] for f in out["frames"] if f["type"] == "error"] == [wire.NOTHING_REACHED_THE_AGENT]
        assert layer.messages == []
        assert temp_db.get_chat_messages(out["chat_id"]) == []

    def test_a_photo_that_cannot_be_read_back_is_told(self, temp_db, monkeypatch):
        import json as jsonlib
        layer = self._layer(monkeypatch)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        out = self._run(layer, {"type": "warmup", "agent": slug, "text": "look again",
                                "images": [{"path": "users/admin/workspace/uploads/photos/gone.png",
                                            "name": "gone.png"}]})
        kinds = [(f["type"], f.get("subtype")) for f in out["frames"]]
        notice = kinds.index(("system", "photos_not_attached"))
        assert notice < kinds.index(("text", None))
        frame = out["frames"][notice]
        assert frame["chat_id"] == out["chat_id"]
        assert frame["message"] == "The photo did not reach the agent. Attach it again to send it."
        _sid, prompt, _kw = layer.messages[0]
        assert prompt == "look again"
        rows = temp_db.get_chat_messages(out["chat_id"])
        assert [(r["role"], r["event_type"] or "") for r in rows] == [
            ("user", ""), ("event", "system"), ("assistant", "")]
        assert rows[0]["event_data"] == ""
        assert jsonlib.loads(rows[1]["event_data"])["subtype"] == "photos_not_attached"


class TestInteractiveFirstMessageAttachments:
    """A terminal's first message with photos: the server types the Read
    paths with it, as a typed send's are, and notes what it typed so the
    tailer adds no second row; an engine that takes its first prompt as a
    launch argument gets it through the terminal instead (the paths are
    lines)."""

    def _run(self, temp_db, monkeypatch, *, via_argv: bool, extra_images=()) -> dict:
        import dataclasses

        from core.session import interactive_session, transcript_tool_events as tte
        from storage.db_settings import set_platform_setting

        registered: list[FakeInteractiveSession] = []

        class _InteractiveSpawnLayer(FakeExecutionLayer):
            async def start_session(self, sid, agent_cfg):
                await super().start_session(sid, agent_cfg)
                if getattr(agent_cfg, "interactive", False):
                    isess = FakeInteractiveSession(sid, "")
                    registered.append(isess)
                    monkeypatch.setitem(interactive_session._sessions, sid, isess)

        layer = _InteractiveSpawnLayer()
        if via_argv:
            layer.capabilities = dataclasses.replace(
                layer.capabilities,
                runtime=dataclasses.replace(
                    layer.capabilities.runtime, interactive_first_prompt_via_argv=True,
                ),
            )
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        set_platform_setting("interactive_cli_enabled", "true")
        monkeypatch.setattr(tte, "_sent_prompts", {})
        out: dict = {}

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({
                    "type": "warmup", "agent": slug, "text": "what is this",
                    "images": [{"data": _photo_data_url(), "name": "pic.png"}, *extra_images],
                    "execution_mode": "interactive",
                })
                started = await ws.next_frame()
                assert started["type"] == "warmup_started"
                chat_id = started["chat_id"]
                frames = await _frames_until(ws, "warmup_ready", limit=10)
                assert frames[-1]["interactive"] is True
                for _ in range(100):
                    if registered and registered[0].submitted:
                        break
                    await asyncio.sleep(0.02)
                assert registered and registered[0].submitted, "nothing typed into the terminal"
                frames = list(frames)
                while not ws._outbound.empty():
                    frames.append(ws._outbound.get_nowait())
                out["frames"] = frames
                out["chat_id"] = chat_id
                _sid, agent_cfg = layer.started[0]
                out["argv"] = agent_cfg.interactive_first_prompt
                out["submitted"] = registered[0].submitted
                out["rows"] = temp_db.get_chat_messages(chat_id)
                out["consumed"] = tte.consume_sent_prompt(chat_id, registered[0].submitted[0])
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)
        return out

    def test_the_photos_ride_the_terminal_first_prompt(self, temp_db, monkeypatch):
        import json as jsonlib
        out = self._run(temp_db, monkeypatch, via_argv=False)
        submitted, = out["submitted"]
        assert submitted.startswith("[Current time: ")
        assert "\n\nwhat is this\n\nThe user has attached 1 image(s)." in submitted
        assert "- /users/admin/workspace/uploads/photos/img_" in submitted
        row, = out["rows"]
        assert row["content"] == "what is this"
        assert jsonlib.loads(row["event_data"])["images"][0]["name"] == "pic.png"
        assert out["consumed"] is True

    def test_a_photo_gone_by_the_warm_up_is_told_live_and_kept_out_of_the_prompt(
            self, temp_db, monkeypatch):
        import json as jsonlib
        gone = {"path": "users/admin/workspace/uploads/photos/gone.png", "name": "gone.png"}
        out = self._run(temp_db, monkeypatch, via_argv=False, extra_images=[gone])
        submitted, = out["submitted"]
        assert submitted.count("- /users/admin/workspace/uploads/photos/img_") == 1
        assert "gone.png" not in submitted
        notices = [f for f in out["frames"] if f.get("subtype") == "photos_not_attached"]
        assert [(n["chat_id"], n["count"]) for n in notices] == [(out["chat_id"], 1)]
        rows = out["rows"]
        assert [r["role"] for r in rows] == ["user", "event"]
        assert jsonlib.loads(rows[1]["event_data"])["subtype"] == "photos_not_attached"

    def test_a_launch_argument_engine_takes_them_through_the_terminal(self, temp_db, monkeypatch):
        out = self._run(temp_db, monkeypatch, via_argv=True)
        assert not out["argv"]
        submitted, = out["submitted"]
        assert "- /users/admin/workspace/uploads/photos/img_" in submitted
