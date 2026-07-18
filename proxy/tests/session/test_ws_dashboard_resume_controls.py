"""Characterization suite for ``ws_dashboard_handler`` — part 2.

Pins resume_chat (validation, dead-session lazy path, alive-session
re-attach), mode/model change (idle paths + the cross-layer refusal),
message queueing + cancel during a streaming turn, abort mid-turn, and a
mid-turn permission prompt/response round-trip. Same golden-master rules as
part 1: any assertion change during the decomposition is a red flag.
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
    sync_dispatch,
    warm_new_chat,
)


def _make_chat(agent: str, *, session_id: str | None = None,
               messages: tuple[tuple[str, str], ...] = (),
               user_sub: str = "user-admin") -> str:
    from storage import database as task_store
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, user_sub, agent, "default",
                           model=TEST_MODEL,
                           execution_path="claude-code-cli")
    if session_id:
        task_store.update_chat(cid, session_id=session_id)
    for role, content in messages:
        task_store.add_chat_message(cid, role, content, author_sub=user_sub)
    return cid


# ---------------------------------------------------------------------------
# resume_chat — validation.
# ---------------------------------------------------------------------------

class TestResumeValidation:
    def test_missing_and_unknown_chat(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)

                ws.client_send({"type": "resume_chat"})
                await ws.expect({"type": "error", "message": "chat_id required"})

                ws.client_send({"type": "resume_chat", "chat_id": "nope"})
                await ws.expect({"type": "error", "message": "Chat not found"})
        run_ws_scenario(scenario)

    def test_foreign_chat_access_denied(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        cid = _make_chat(slug, user_sub="user-admin")

        async def scenario():
            cookie = session_cookie(sub="user-viewer", email="viewer@test.com",
                                    name="Viewer User", role="member")
            async with dashboard_connection(cookie) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await ws.expect({"type": "error", "message": "Access denied"})
        run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# resume_chat — idle chat, dead vs alive session.
# ---------------------------------------------------------------------------

class TestResumeIdleChat:
    def test_dead_session_lazy_resume(self, temp_db, monkeypatch):
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        dead_sid = str(uuid.uuid4())  # never in layer.alive
        cid = _make_chat(slug, session_id=dead_sid,
                         messages=(("user", "hi"), ("assistant", "hello")))

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                history = await ws.expect({
                    "type": "chat_history", "chat_id": cid, "agent": ANY,
                    "messages": ANY, "has_more": False,
                    "restore": {"todos": [], "meeting": None, "goal": None},
                    "plans": [], "total_cost": 0,
                    "context_used": 0, "context_max": 0,
                    "cache_read": 0, "cache_write": 0, "output_tokens": 0,
                    "execution_path": "claude-code-cli",
                    "execution_mode": "", "model": TEST_MODEL, "mode": "default",
                })
                assert [(m["role"], m["content"])
                        for m in history["messages"]] == [
                    ("user", "hi"), ("assistant", "hello"),
                ]
                # dead session → NO spawn for browsing, warmup deferred to send
                await ws.expect({
                    "type": "warmup_ready", "session_id": None,
                    "chat_id": cid, "mode": "default", "model": TEST_MODEL,
                    "execution_path": "claude-code-cli",
                    "execution_mode": "", "needs_warmup": True,
                })
                await ws.expect({"type": "queue_snapshot", "chat_id": cid,
                                 "messages": []})
                assert layer.started == []
        run_ws_scenario(scenario)

    def test_alive_session_reattach(self, temp_db, monkeypatch):
        from core.session.session_state import set_session_mode

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        live_sid = str(uuid.uuid4())
        layer.alive.add(live_sid)
        set_session_mode(live_sid, "acceptEdits")  # as a real spawn would
        cid = _make_chat(slug, session_id=live_sid,
                         messages=(("user", "hi"),))

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await ws.expect({
                    "type": "chat_history", "chat_id": cid, "agent": ANY,
                    "messages": ANY, "has_more": False,
                    "restore": {"todos": [], "meeting": None, "goal": None},
                    "plans": [], "total_cost": 0,
                    "context_used": 0, "context_max": 0,
                    "cache_read": 0, "cache_write": 0, "output_tokens": 0,
                    "execution_path": "claude-code-cli",
                    "execution_mode": "", "model": TEST_MODEL, "mode": "default",
                })
                await ws.expect({
                    "type": "warmup_ready", "session_id": live_sid,
                    "chat_id": cid, "mode": "acceptEdits",
                    "model": TEST_MODEL,
                    "execution_path": "claude-code-cli",
                    "interactive": False,
                })
                await ws.expect({"type": "queue_snapshot", "chat_id": cid,
                                 "messages": []})
                assert layer.started == []  # re-attach, never a spawn
        run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# mode/model change on an idle live session.
# ---------------------------------------------------------------------------

class TestModeModelChange:
    def test_mode_change_applied_and_persisted(self, temp_db, monkeypatch):
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "mode_change", "mode": "acceptEdits"})
                await ws.expect({"type": "mode_changed",
                                 "mode": "acceptEdits"})
                await sync_dispatch(ws)
                assert layer.mode_changes == [(sid, "acceptEdits")]
                assert temp_db.get_chat(chat_id)["permission_mode"] == \
                    "acceptEdits"

                ws.client_send({"type": "mode_change", "mode": "yolo"})
                await ws.expect({"type": "error",
                                 "message": "Invalid mode: yolo"})
        run_ws_scenario(scenario)

    def test_model_change_applied_vs_foreign_refused(self, temp_db,
                                                     monkeypatch):
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                # foreign model (not served by this layer) → refused, selector
                # resynced to the chat's real model. NB: the refusal frame
                # carries chat_id, the accepted frame does not.
                ws.client_send({"type": "model_change",
                                "model": "foreign-model"})
                await ws.expect({"type": "model_changed", "model": TEST_MODEL,
                                 "chat_id": chat_id})
                assert layer.model_changes == []
                assert temp_db.get_chat(chat_id)["model"] == TEST_MODEL

                ws.client_send({"type": "model_change", "model": TEST_MODEL})
                await ws.expect({"type": "model_changed",
                                 "model": TEST_MODEL})
                await sync_dispatch(ws)
                assert layer.model_changes == [(sid, TEST_MODEL)]
        run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# Streaming turn: queueing, cancel, abort, permission round-trip.
# ---------------------------------------------------------------------------

class TestStreamingTurnControls:
    def test_queue_and_cancel_during_stream(self, temp_db, monkeypatch):
        from core.events.common_events import CommonEvent, TEXT, DONE

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            hold = asyncio.Event()

            async def first_turn():
                yield CommonEvent(type=TEXT, data={"content": "working…"})
                await hold.wait()
                yield CommonEvent(type=DONE, data={})

            def script(sid, prompt):
                if not layer.messages or len(layer.messages) == 1:
                    return first_turn()
                return [
                    CommonEvent(type=TEXT, data={"content": "drained"}),
                    CommonEvent(type=DONE, data={}),
                ]
            layer.turn_events = script

            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "chat", "text": "long job",
                                "chat_id": chat_id})
                await ws.expect({"type": "title_updated", "chat_id": chat_id,
                                 "title": "long job"})
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
                await ws.expect({"type": "text", "content": "working…",
                                 "chat_id": chat_id})

                # queue two messages while streaming, cancel the first
                ws.client_send({"type": "chat", "text": "queued A"})
                await ws.expect({"type": "queued", "index": 0,
                                 "text": "queued A", "chat_id": chat_id})
                ws.client_send({"type": "chat", "text": "queued B"})
                await ws.expect({"type": "queued", "index": 1,
                                 "text": "queued B", "chat_id": chat_id})
                ws.client_send({"type": "cancel_queued", "index": 0})
                await ws.expect({"type": "queue_removed", "index": 0,
                                 "text": "queued A", "chat_id": chat_id})

                hold.set()  # first turn completes; queue drains as turn 2
                await ws.expect({"type": "queue_sent", "text": "queued B",
                                 "chat_id": chat_id})
                await ws.expect({"type": "text", "content": "drained",
                                 "chat_id": chat_id})
                await ws.expect({"type": "done", "chat_id": chat_id})

                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": f"{slug} finished",
                                 "body": "Response ready"})

                msgs = temp_db.get_chat_messages(chat_id)
                assert [(m["role"], m["content"]) for m in msgs] == [
                    ("user", "long job"),
                    ("assistant", "working…"),
                    ("user", "queued B"),
                    ("assistant", "drained"),
                ]
                assert [p for _s, p, _k in layer.messages] == [
                    "long job", "queued B",
                ]
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_steer_accepted_mid_stream(self, temp_db, monkeypatch):
        # A steer-capable engine (Codex) takes the mid-turn message INTO the
        # running turn: `steered` frame (no queue entry), user row persisted
        # immediately, and the queue drain never runs it as a second turn.
        from core.events.common_events import CommonEvent, TEXT, DONE

        layer = FakeExecutionLayer()
        layer.steer_accepts = True
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            hold = asyncio.Event()

            async def first_turn():
                yield CommonEvent(type=TEXT, data={"content": "working…"})
                await hold.wait()
                yield CommonEvent(type=DONE, data={})

            layer.turn_events = lambda sid, prompt: first_turn()

            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "chat", "text": "long job",
                                "chat_id": chat_id})
                await ws.expect({"type": "title_updated", "chat_id": chat_id,
                                 "title": "long job"})
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
                await ws.expect({"type": "text", "content": "working…",
                                 "chat_id": chat_id})

                ws.client_send({"type": "chat", "text": "also check logs"})
                await ws.expect({"type": "steered", "text": "also check logs",
                                 "chat_id": chat_id})
                assert layer.steered == [(sid, "also check logs")]

                hold.set()  # the (steered) turn completes — nothing queued
                await ws.expect({"type": "done", "chat_id": chat_id})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": f"{slug} finished",
                                 "body": "Response ready"})

                msgs = temp_db.get_chat_messages(chat_id)
                assert [(m["role"], m["content"]) for m in msgs] == [
                    ("user", "long job"),
                    ("user", "also check logs"),   # persisted at steer time
                    ("assistant", "working…"),
                ]
                # The steered message never became a second turn.
                assert [p for _s, p, _k in layer.messages] == ["long job"]
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_graceful_abort_keeps_producer_and_stamps_flag(self, temp_db, monkeypatch):
        # Graceful path (engine kept the partial turn): the producer is NOT
        # cancelled — when the engine closes the turn the surviving pump
        # persists the partial text — and the graceful flag suppresses the
        # next turn's cancelled-context injection while last_turn_aborted
        # still feeds the delegate user_interrupted status.
        from core.events.common_events import CommonEvent, TEXT, DONE

        layer = FakeExecutionLayer()
        layer.abort_graceful = True
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            hold = asyncio.Event()

            async def interruptible_turn():
                yield CommonEvent(type=TEXT, data={"content": "partial"})
                await hold.wait()          # the engine-side interrupt closes it
                yield CommonEvent(type=DONE, data={})

            layer.turn_events = lambda sid, prompt: interruptible_turn()

            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "chat", "text": "never ends",
                                "chat_id": chat_id})
                await ws.expect({"type": "title_updated", "chat_id": chat_id,
                                 "title": "never ends"})
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
                await ws.expect({"type": "text", "content": "partial",
                                 "chat_id": chat_id})

                ws.client_send({"type": "abort"})
                await ws.expect({"type": "aborted", "chat_id": chat_id})
                assert layer.aborted == [sid]
                chat = temp_db.get_chat(chat_id)
                assert chat["last_turn_aborted"] is True
                assert chat["last_abort_graceful"] is True

                # The engine closes the interrupted turn; the SURVIVING
                # producer/pump persists the partial text.
                hold.set()
                for _ in range(60):
                    msgs = temp_db.get_chat_messages(chat_id)
                    if any(m["role"] == "assistant" and m["content"] == "partial"
                           for m in msgs):
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("partial turn was never persisted")
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)

    def test_abort_during_stream(self, temp_db, monkeypatch):
        from core.events.common_events import CommonEvent, TEXT

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            hold = asyncio.Event()

            async def stuck_turn():
                yield CommonEvent(type=TEXT, data={"content": "partial"})
                await hold.wait()

            layer.turn_events = lambda sid, prompt: stuck_turn()

            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "chat", "text": "never ends",
                                "chat_id": chat_id})
                await ws.expect({"type": "title_updated", "chat_id": chat_id,
                                 "title": "never ends"})
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
                await ws.expect({"type": "text", "content": "partial",
                                 "chat_id": chat_id})

                ws.client_send({"type": "abort"})
                await ws.expect({"type": "aborted", "chat_id": chat_id})
                assert layer.aborted == [sid]
                assert temp_db.get_chat(chat_id)["last_turn_aborted"] is True

                # detached pump finishes headless; its broadcasts drain later.
                # A CLI abort kills the whole process group, so the liveness
                # cohort clears ride along (clear_session_liveness → notify).
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.expect({"type": "bg_agents_complete", "count": 0})
                await ws.expect({"type": "bg_commands_complete", "count": 0})
                await ws.expect({"type": "fg_agents_complete"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)

    def test_abort_interactive_sends_esc_no_flags(self, temp_db, monkeypatch):
        # An interactive PTY chat's Stop is "press ESC in the TUI": the abort
        # must branch to interrupt_turn — never the layer/pump machinery, no
        # last_turn_aborted stamp (the TUI keeps its partial turn natively),
        # and the turn state closes later via the transcript markers.
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        class _FakePty:
            def __init__(self):
                self.closed = False
                self.written = []

            def write(self, data):
                self.written.append(data)

            def resize(self, rows, cols):
                pass

            def scrollback(self):
                return b""

            def close(self, signal_child=True):
                self.closed = True

        async def scenario():
            from core.session import interactive_session as isess

            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)
                s = isess.InteractiveSession(
                    session_id=sid, chat_id=chat_id, agent_name=slug,
                )
                s.pty = _FakePty()
                s._turn_open = True
                isess._sessions[sid] = s
                try:
                    ws.client_send({"type": "abort"})
                    await ws.expect({"type": "aborted", "chat_id": chat_id})
                    assert s.pty.written == [b"\x1b"]
                    assert layer.aborted == []
                    assert not temp_db.get_chat(chat_id)["last_turn_aborted"]
                finally:
                    isess._sessions.pop(sid, None)
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)

    def test_permission_prompt_roundtrip(self, temp_db, monkeypatch):
        from core.events.common_events import CommonEvent, TEXT, DONE
        from core.session.session_state import get_permission_queue

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        request_id = f"req-{uuid.uuid4().hex[:8]}"

        async def scenario():
            approved = asyncio.Event()

            async def perm_turn(sid):
                yield CommonEvent(type=TEXT, data={"content": "let me check"})
                # a hook would enqueue this while the CLI blocks on approval
                get_permission_queue(sid).put_nowait({
                    "event_type": "permission_prompt",
                    "request_id": request_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": "ls"},
                })
                await approved.wait()
                yield CommonEvent(type=TEXT, data={"content": "approved!"})
                yield CommonEvent(type=DONE, data={})

            layer.turn_events = lambda sid, prompt: perm_turn(sid)

            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "chat", "text": "run ls",
                                "chat_id": chat_id})
                await ws.expect({"type": "title_updated", "chat_id": chat_id,
                                 "title": "run ls"})
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
                await ws.expect({"type": "text", "content": "let me check",
                                 "chat_id": chat_id})
                await ws.expect({
                    "type": "permission_prompt", "request_id": request_id,
                    "tool_name": "Bash", "tool_input": {"command": "ls"},
                    "chat_id": chat_id,
                })

                ws.client_send({"type": "permission_response",
                                "request_id": request_id, "approved": True})
                # give the response a beat to be consumed, then unblock the
                # "CLI" exactly as the resolved hook long-poll would
                await asyncio.sleep(0.2)
                approved.set()

                await ws.expect({"type": "text", "content": "approved!",
                                 "chat_id": chat_id})
                await ws.expect({"type": "done", "chat_id": chat_id})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": f"{slug} finished",
                                 "body": "Response ready"})
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)


class TestPlanFilenameCarryOver:
    def test_plan_filename_carries_to_next_turn_pump(self, temp_db,
                                                     monkeypatch):
        """A turn that wrote a plan file must hand its filename to the NEXT
        turn's pump, so plan edits keep updating the same plan instead of
        forking a second file. (Was a dead closure local — the carry-over
        only worked through the resume-from-DB path.)"""
        from core.events.common_events import (
            CommonEvent, TEXT, TOOL_INPUT, DONE,
        )
        from core.events.stream_pump import _active_pumps

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        plan_path = "/home/u/.claude/plans/plan-abc123.md"

        async def scenario():
            hold = asyncio.Event()

            def script(sid, prompt):
                if len(layer.messages) == 1:
                    return [
                        CommonEvent(type=TOOL_INPUT, data={
                            "name": "Write", "summary": "Writing plan",
                            "tool_input": {"file_path": plan_path},
                            "file_path": plan_path,
                        }),
                        CommonEvent(type=DONE, data={}),
                    ]

                async def second_turn():
                    yield CommonEvent(type=TEXT, data={"content": "editing"})
                    await hold.wait()
                    yield CommonEvent(type=DONE, data={})
                return second_turn()
            layer.turn_events = script

            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "chat", "text": "make a plan",
                                "chat_id": chat_id})
                await ws.expect({"type": "title_updated", "chat_id": chat_id,
                                 "title": "make a plan"})
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
                await ws.expect({"type": "tool_info", "chat_id": chat_id,
                                 "name": "Write", "summary": "Writing plan",
                                 "tool_input": {"file_path": plan_path},
                                 "file_path": plan_path})
                await ws.expect({"type": "done", "chat_id": chat_id})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": f"{slug} finished",
                                 "body": "Response ready"})

                # second turn: its pump must inherit the plan filename
                ws.client_send({"type": "chat", "text": "edit the plan",
                                "chat_id": chat_id})
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
                await ws.expect({"type": "text", "content": "editing",
                                 "chat_id": chat_id})
                pump2 = _active_pumps[chat_id]
                assert pump2._plan_filename == "plan-abc123.md"

                hold.set()
                await ws.expect({"type": "done", "chat_id": chat_id})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": f"{slug} finished",
                                 "body": "Response ready"})
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)


class TestWedgedPumpReap:
    """resume_chat's stall-reap: event silence alone must not kill a turn —
    only a probe-confirmed dead process, a severed stream, or the hard
    ceiling reaps (the Mode D false-reap incident)."""

    def _hanging_turn(self, layer):
        from core.events.common_events import CommonEvent, TEXT

        async def turn(sid, prompt):
            yield CommonEvent(type=TEXT, data={"content": "working"})
            await asyncio.Event().wait()  # stalls forever (network stall)
        layer.turn_events = turn

    async def _start_stalled_turn(self, ws, layer, slug):
        chat_id, sid = await warm_new_chat(ws, layer, slug)
        ws.client_send({"type": "chat", "text": "go", "chat_id": chat_id})
        # Drain frames until the turn's text proves the pump is live.
        await self._drain_until(ws, "text")
        return chat_id, sid

    async def _drain_until(self, ws, ftype, timeout: float = 3.0):
        seen = []
        while True:
            try:
                frame = await ws.next_frame(timeout)
            except asyncio.TimeoutError:
                raise AssertionError(
                    f"no {ftype!r} frame; saw {seen}") from None
            if frame["type"] == ftype:
                return frame
            seen.append(frame)

    def test_stalled_but_alive_is_not_reaped(self, temp_db, monkeypatch):
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        self._hanging_turn(layer)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await self._start_stalled_turn(ws, layer, slug)
                layer.idle_seconds[sid] = 500.0  # way past STALE_TURN_SECS

                ws.client_send({"type": "resume_chat", "chat_id": chat_id})
                await self._drain_until(ws, "chat_history")
                ws.client_send({"type": "ping"})
                await self._drain_until(ws, "pong", timeout=8.0)
                # Alive process → the turn is left to recover: no reap.
                assert layer.prepared_resume == []
                from core.events.stream_pump import _active_pumps
                assert chat_id in _active_pumps
                _active_pumps[chat_id].abort()  # test teardown
        run_ws_scenario(scenario)

    def test_stalled_dead_process_is_reaped(self, temp_db, monkeypatch):
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        self._hanging_turn(layer)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await self._start_stalled_turn(ws, layer, slug)
                layer.idle_seconds[sid] = 500.0
                layer.probed_dead.add(sid)

                ws.client_send({"type": "resume_chat", "chat_id": chat_id})
                await self._drain_until(ws, "chat_history")
                ws.client_send({"type": "ping"})
                await self._drain_until(ws, "pong", timeout=8.0)
                assert layer.prepared_resume == [sid]
        run_ws_scenario(scenario)

    def test_hard_ceiling_reaps_even_alive(self, temp_db, monkeypatch):
        import config as app_config
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        self._hanging_turn(layer)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await self._start_stalled_turn(ws, layer, slug)
                layer.idle_seconds[sid] = app_config.CLAUDE_TIMEOUT + 60.0

                ws.client_send({"type": "resume_chat", "chat_id": chat_id})
                await self._drain_until(ws, "chat_history")
                ws.client_send({"type": "ping"})
                await self._drain_until(ws, "pong", timeout=8.0)
                assert layer.prepared_resume == [sid]
        run_ws_scenario(scenario)

    def test_reaped_task_run_stamped_failed_with_reason(self, temp_db,
                                                        monkeypatch):
        from storage import database as task_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        self._hanging_turn(layer)
        run_id = uuid.uuid4().hex[:12]
        chat_id = f"task-run-{run_id}"
        task_store.create_run(f"run-{run_id}", "task-x", slug, "manual",
                              None, "do things")
        task_store.update_run(f"run-{run_id}", status="running",
                              chat_id=chat_id)
        task_store.create_chat(chat_id, "user-admin", slug, "default",
                               model=TEST_MODEL,
                               execution_path="claude-code-cli")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                # Give the task chat a live session, then drive a turn on it —
                # the same streaming shape as a task continuation turn.
                _, sid = await warm_new_chat(ws, layer, slug)
                task_store.update_chat(chat_id, session_id=sid)
                ws.client_send({"type": "resume_chat", "chat_id": chat_id})
                await self._drain_until(ws, "queue_snapshot")
                ws.client_send({"type": "chat", "text": "go",
                                "chat_id": chat_id})
                await self._drain_until(ws, "text")
                layer.idle_seconds[sid] = 500.0
                layer.probed_dead.add(sid)

                ws.client_send({"type": "resume_chat", "chat_id": chat_id})
                await self._drain_until(ws, "chat_history")
                # Task chats poll ~10s for a next-turn pump after the reap
                # before the loop breaks — allow for it.
                ws.client_send({"type": "ping"})
                await self._drain_until(ws, "pong", timeout=14.0)
                assert layer.prepared_resume == [sid]
                run = task_store.get_run(f"run-{run_id}")
                assert run["status"] == "failed"
                assert "reaped by platform" in run["error_message"]
                assert "process dead" in run["error_message"]
                assert run["completed_at"]
        run_ws_scenario(scenario, timeout=30.0)


class TestDeterministicTitlePrelude:
    """Interactive sends reach _deterministic_title with the injected
    [Current time: ...] stamp already prepended — it must not become the
    title (tailer twins strip it; this is the send-time chokepoint)."""

    def _title(self, text: str) -> str:
        from ws.dashboard_chat import ChatController
        return ChatController._deterministic_title(None, text)

    def test_time_prelude_stripped(self):
        text = ("[Current time: Wednesday, July 08, 2026 13:00 (1:00 PM) "
                "UTC (UTC+00:00)]\n\nPlease delegate a task to yourself")
        assert self._title(text) == "Please delegate a task to yourself"

    def test_prelude_only_prompt_falls_back(self):
        assert self._title(
            "[Current time: Wednesday, July 08, 2026 13:00 (1:00 PM) UTC (UTC+00:00)]\n"
        ) == "New Chat"

    def test_plain_prompt_unchanged(self):
        assert self._title("run the tests please") == "run the tests please"

    def test_app_action_framing_titles_as_app_and_label(self):
        """A chat STARTED by a mini-app button (front-page action → fresh
        chat) titles as "App — Label", never the raw framing brackets. Both
        rails: send-time chokepoint here, tailer twin below (interactive
        terminals type the same framed text into the PTY)."""
        framed = ('[action from mini-app "Infra Dashboard" — Refresh data]\n'
                  '```text\nRefresh the dashboard with fresh metrics\n```')
        assert self._title(framed) == "Infra Dashboard — Refresh data"
        # Interactive delivery prepends the time stamp before the framing.
        stamped = ("[Current time: Friday, July 10, 2026 19:00 (7:00 PM) "
                   "UTC (UTC+00:00)]\n" + framed)
        assert self._title(stamped) == "Infra Dashboard — Refresh data"

    def test_tailer_twin_recognizes_app_action_framing(self):
        from core.session.transcript_tailer import _title_from_prompt
        framed = ('[action from mini-app "Infra Dashboard" — Refresh data]\n'
                  '```text\nRefresh please\n```')
        assert _title_from_prompt(framed) == "Infra Dashboard — Refresh data"
        assert _title_from_prompt("plain words") == "plain words"


class TestTaskChatModeRestore:
    """A task-run chat's chat_history must carry the chat row's stored
    permission mode — the scheduler runs tasks with 'auto' (Don't Ask), and
    the frontend restores the chip from this field for task- chats. Before
    the fix NO chat_history frame carried `mode`, so the viewer's sticky
    selection (or a stale previous chat's mode) showed instead of the run's
    real posture."""

    def test_task_chat_history_carries_auto_mode(self, temp_db, monkeypatch):
        import uuid as _uuid
        from storage import database as task_store

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        run_id = _uuid.uuid4().hex[:12]
        cid = f"task-run-{run_id}"
        task_store.create_chat(cid, "user-admin", slug, "auto",
                               model=TEST_MODEL,
                               execution_path="claude-code-cli",
                               source_type="task")
        task_store.add_chat_message(cid, "user", "do the task",
                                    author_sub="user-admin")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                history = await ws.next_frame()
                assert history["type"] == "chat_history"
                assert history["chat_id"] == cid
                assert history["mode"] == "auto"
                assert history["model"] == TEST_MODEL
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)
