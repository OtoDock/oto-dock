"""Characterization suite for ``ws_dashboard_handler`` — part 2.

Pins resume_chat (validation, dead-session lazy path, alive-session
re-attach), mode/model change (idle paths + the cross-layer refusal),
message queueing + cancel during a streaming turn, abort mid-turn, and a
mid-turn permission prompt/response round-trip. Same golden-master rules as
part 1: any assertion change during the decomposition is a red flag.
"""

import asyncio
import time
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


# A 1x1 transparent PNG — enough for Pillow to validate and save.
_PNG_DATA_URL = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlE"
    "QVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _make_chat(agent: str, *, session_id: str | None = None,
               messages: tuple[tuple[str, str], ...] = (),
               user_sub: str = "user-admin", source_type: str = "chat") -> str:
    from storage import database as task_store
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, user_sub, agent, "default",
                           model=TEST_MODEL,
                           execution_path="claude-code-cli",
                           source_type=source_type)
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

    def test_by_id_follows_the_chat_owner_and_phone_calls_open_for_managers(
            self, temp_db, monkeypatch):
        """The by-id gate is the REST rule plus the phone clause: on a
        Shared-only agent an assigned editor opens the ``agent::`` pool, not
        a colleague's per-user chat from before the switch, and not a phone
        call (the conversations list is managers-only); a manager opens
        the call. The agent's mode itself decides nothing."""
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        from core.session import session_kind
        from core.session.visibility import PHONE_CHAT_OWNER, shared_chat_owner
        from storage import database as task_store
        slug = make_test_agent(collaborative=False, default_scope="agent")
        task_store.add_user_agent("user-viewer", slug, "editor", "test")
        task_store.add_user_agent("user-manager", slug, "manager", "test")
        call = _make_chat(slug, user_sub=PHONE_CHAT_OWNER,
                          source_type=session_kind.PHONE.source_type)
        pre_switch = _make_chat(slug, user_sub="user-admin")
        pool = _make_chat(slug, user_sub=shared_chat_owner(slug))

        async def _opens(ws, cid: str) -> None:
            ws.client_send({"type": "resume_chat", "chat_id": cid})
            frame = await ws.next_frame()
            assert (frame["type"], frame["chat_id"]) == ("chat_history", cid), frame
            await ws.expect({"type": "warmup_ready", "session_id": None, "chat_id": cid,
                             "mode": "default", "model": TEST_MODEL,
                             "execution_path": "claude-code-cli",
                             "execution_mode": "", "needs_warmup": True})
            await ws.expect({"type": "queue_snapshot", "chat_id": cid, "messages": []})

        async def scenario():
            editor = session_cookie(sub="user-viewer", email="viewer@test.com",
                                    name="Viewer User", role="member")
            async with dashboard_connection(editor) as ws:
                await drain_startup(ws)
                for cid in (call, pre_switch):
                    ws.client_send({"type": "resume_chat", "chat_id": cid})
                    await ws.expect({"type": "error", "message": "Access denied"})
                await _opens(ws, pool)
            manager = session_cookie(sub="user-manager", email="manager@test.com",
                                     name="Manager User", role="creator")
            async with dashboard_connection(manager) as ws:
                await drain_startup(ws)
                await _opens(ws, call)
                ws.client_send({"type": "resume_chat", "chat_id": pre_switch})
                await ws.expect({"type": "error", "message": "Access denied"})
        run_ws_scenario(scenario)


async def _frames_until_pong(ws) -> list[dict]:
    """Every frame the socket sends before a ping's pong (the dispatch
    barrier for a message whose own answer is not one fixed frame)."""
    ws.client_send({"type": "ping"})
    frames = []
    while True:
        frame = await ws.next_frame()
        if frame["type"] == "pong":
            return frames
        frames.append(frame)


class TestByIdFramesFollowTheOpenRule:
    """Every frame that names a chat the socket is not bound to passes the
    rule ``resume_chat`` binds by (``can_open_chat``): on a Shared-only
    agent an assigned editor reaches the ``agent::`` pool, never a
    colleague's per-user chat from before the switch — not its mode, model
    or execution mode, not its live session, not its history, not a turn
    through the chat frame's self-heal, not its read marker."""

    def test_foreign_chat_refused_on_every_by_id_frame(self, temp_db, monkeypatch):
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        from core.session.visibility import shared_chat_owner
        from storage import database as task_store
        slug = make_test_agent(collaborative=False, default_scope="agent")
        task_store.add_user_agent("user-viewer", slug, "editor", "test")
        pre_switch = _make_chat(slug, user_sub="user-admin", session_id="sid-foreign",
                                messages=(("user", "private question"),))
        layer.alive.add("sid-foreign")
        pool = _make_chat(slug, user_sub=shared_chat_owner(slug))
        denied = {"type": "error", "message": "Access denied"}

        async def scenario():
            editor = session_cookie(sub="user-viewer", email="viewer@test.com",
                                    name="Viewer User", role="member")
            async with dashboard_connection(editor) as ws:
                await drain_startup(ws)
                for frame in (
                    {"type": "mode_change", "mode": "acceptEdits", "chat_id": pre_switch},
                    {"type": "model_change", "model": TEST_MODEL, "chat_id": pre_switch},
                    {"type": "execution_mode_change", "execution_mode": "interactive",
                     "chat_id": pre_switch},
                    {"type": "execution_mode_switch", "execution_mode": "interactive",
                     "chat_id": pre_switch},
                ):
                    ws.client_send(frame)
                    await ws.expect(denied)
                ws.client_send({"type": "chat_read", "chat_id": pre_switch})
                ws.client_send({"type": "chat", "text": "hello", "chat_id": pre_switch})
                frames = await _frames_until_pong(ws)
                assert all(f.get("chat_id") != pre_switch for f in frames), frames
                assert not any(f["type"] == "chat_history" for f in frames), frames

                # The shared pool stays reachable by id for the same editor.
                ws.client_send({"type": "mode_change", "mode": "plan", "chat_id": pool})
                await ws.expect({"type": "mode_changed", "mode": "plan", "chat_id": pool})

            row = task_store.get_chat(pre_switch)
            assert row["permission_mode"] == "default"
            assert (row.get("execution_mode") or "") == ""
            assert "sid-foreign" in layer.alive and layer.closed_sessions == []
            assert layer.started == [] and layer.messages == []
            assert [m["content"] for m in task_store.get_chat_messages(pre_switch)] == [
                "private question"]
            assert task_store.get_chat(pool)["permission_mode"] == "plan"
        run_ws_scenario(scenario)

    def test_below_editor_never_writes_a_shared_pool_chats_settings(self, temp_db, monkeypatch):
        """A Shared-only chat runs as the agent, which takes the editor tier
        to start a session: the drive gate applies that rule to every frame
        that writes the row or reaches a live session, by id and on the
        bound chat, so a contributor can neither set the chat's permission
        mode, model or execution mode nor drive a colleague's live session.
        Reading stays open."""
        from core.session.session_state import get_session_mode, set_session_mode
        from core.session.visibility import shared_chat_owner
        from storage import database as task_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent(collaborative=False, default_scope="agent")
        task_store.add_user_agent("user-viewer", slug, "contributor", "test")
        task_store.add_user_agent("user-manager", slug, "manager", "test")
        live_sid = "sid-pool-live"
        layer.alive.add(live_sid)  # the manager's warm headless session
        set_session_mode(live_sid, "default")
        pool = _make_chat(slug, user_sub=shared_chat_owner(slug), session_id=live_sid,
                          messages=(("user", "shared history"),))
        contributor = session_cookie(sub="user-viewer", email="viewer@test.com",
                                     name="Viewer User", role="member")

        def _refused(frame: dict) -> bool:
            return frame.get("type") == "error" and "editor role" in frame.get("message", "")

        async def scenario():
            async with dashboard_connection(contributor) as ws:
                await drain_startup(ws)
                # By id: the socket is not bound to the pool.
                for frame in (
                    {"type": "mode_change", "mode": "dontAsk", "chat_id": pool},
                    {"type": "model_change", "model": TEST_MODEL, "chat_id": pool},
                    {"type": "execution_mode_change", "execution_mode": "interactive",
                     "chat_id": pool},
                    {"type": "execution_mode_switch", "execution_mode": "interactive",
                     "chat_id": pool},
                ):
                    ws.client_send(frame)
                    assert _refused(await ws.next_frame()), frame
                # Reading the shared history stays open, and binds the socket.
                ws.client_send({"type": "resume_chat", "chat_id": pool})
                hist = await ws.next_frame()
                assert hist["type"] == "chat_history", hist
                ready = await ws.next_frame()
                assert ready["type"] == "warmup_ready" and ready["session_id"] == live_sid, ready
                await ws.expect({"type": "queue_snapshot", "chat_id": pool, "messages": []})
                # Bound: the live session is a colleague's.
                ws.client_send({"type": "mode_change", "mode": "dontAsk", "chat_id": pool})
                assert _refused(await ws.next_frame())
                ws.client_send({"type": "model_change", "model": TEST_MODEL, "chat_id": pool})
                assert _refused(await ws.next_frame())
                ws.client_send({"type": "chat", "text": "PM INJECTED", "chat_id": pool})
                assert _refused(await ws.next_frame())
                await ws.expect({"type": "done", "chat_id": pool})
                await sync_dispatch(ws)
        run_ws_scenario(scenario)

        row = task_store.get_chat(pool)
        assert row["permission_mode"] == "default" and (row.get("execution_mode") or "") == ""
        assert get_session_mode(live_sid) == "default"
        assert layer.mode_changes == [] and layer.model_changes == []
        assert layer.closed_sessions == [] and layer.messages == []
        assert live_sid in layer.alive

    def test_a_read_only_viewer_is_never_the_sink_the_zone_or_the_location(
            self, temp_db, monkeypatch):
        """A contributor attached to a Shared-only chat
        watches it; it never becomes the session's notification sink, never
        sets its time zone and never answers its location request."""
        from core.session.session_state import get_session_user_tz
        from core.session.visibility import shared_chat_owner
        from storage import database as task_store
        from ws import dashboard, dashboard_dispatch
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent(collaborative=False, default_scope="agent")
        task_store.add_user_agent("user-viewer", slug, "contributor", "test")
        live_sid = "sid-pool-sink"
        layer.alive.add(live_sid)
        pool = _make_chat(slug, user_sub=shared_chat_owner(slug), session_id=live_sid)
        answered = []
        monkeypatch.setattr(dashboard_dispatch, "resolve_location",
                            lambda rid, result, **kw: answered.append(rid) or True)
        contributor = session_cookie(sub="user-viewer", email="viewer@test.com",
                                     name="Viewer User", role="member")

        async def scenario():
            async with dashboard_connection(contributor) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": pool})
                assert (await ws.next_frame())["type"] == "chat_history"
                assert (await ws.next_frame())["type"] == "warmup_ready"
                await ws.expect({"type": "queue_snapshot", "chat_id": pool, "messages": []})
                ws.client_send({"type": "client_info", "platform": "web",
                                "time_zone": "Pacific/Auckland"})
                ws.client_send({"type": "location_response", "request_id": "loc-1",
                                "lat": 1.0, "lng": 2.0})
                await sync_dispatch(ws)
                assert live_sid not in dashboard._dashboard_notify_queues
        run_ws_scenario(scenario)
        assert get_session_user_tz(live_sid) in (None, "")
        assert answered == []

    def test_the_backchannel_ack_carries_the_gates_sentence(self, temp_db, monkeypatch):
        """A refused page event is acked with the sentence
        the gate answers, not a bare "access denied"."""
        from core.session.visibility import shared_chat_owner
        from storage import database as task_store
        from ws import artifact_interactions
        slug = make_test_agent(collaborative=False, default_scope="agent")
        task_store.add_user_agent("user-viewer", slug, "contributor", "test")
        pool = _make_chat(slug, user_sub=shared_chat_owner(slug), session_id="sid-ack")
        monkeypatch.setattr(artifact_interactions, "validate_interaction",
                            lambda *a, **kw: ({"kind": "artifact"}, ""))
        conn = _bare_connection(pool, "sid-ack", user_sub="user-viewer", agent=slug)
        asyncio.run(conn._handle_artifact_interaction(
            {"type": "artifact_interaction", "chat_id": pool, "token": "t", "payload": {}}))
        (ack,) = [f for f in conn.sent if f.get("type") == "artifact_ack"]
        assert ack["status"] == "denied" and "editor role" in ack["reason"]

    def test_a_new_shared_chat_row_carries_no_picks_below_editor(self, temp_db, monkeypatch):
        """The warm-up that mints a Shared-only chat for a
        caller below the editor tier writes the row with no picks of theirs."""
        from core.session.visibility import shared_chat_owner
        from storage import database as task_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent(collaborative=False, default_scope="agent")
        task_store.add_user_agent("user-viewer", slug, "contributor", "test")
        contributor = session_cookie(sub="user-viewer", email="viewer@test.com",
                                     name="Viewer User", role="member")

        async def scenario():
            async with dashboard_connection(contributor) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "warmup", "agent": slug, "permission_mode": "dontAsk",
                                "model": TEST_MODEL, "execution_mode": "interactive",
                                "text": "hello"})
                ws.client_send({"type": "ping"})
                for _ in range(60):
                    if (await ws.next_frame()).get("type") == "pong":
                        break
        run_ws_scenario(scenario)
        rows = [c for c in task_store.list_chats(shared_chat_owner(slug))]
        assert rows, "the warm-up minted no chat"
        for row in rows:
            assert row["permission_mode"] == "default"
            assert not (row.get("model") or "") and not (row.get("execution_mode") or "")

    def test_below_editor_never_takes_over_or_answers_for_a_shared_pool_chat(
            self, temp_db, monkeypatch):
        from core.session import interactive_session
        from core.session.visibility import shared_chat_owner
        from storage import database as task_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent(collaborative=False, default_scope="agent")
        task_store.add_user_agent("user-viewer", slug, "contributor", "test")
        task_store.add_user_agent("user-viewer2", slug, "editor", "test")
        pool = _make_chat(slug, user_sub=shared_chat_owner(slug), session_id="sid-pool-term")
        term = _FakeTerminal(pool, "sid-pool-term", user_sub="user-admin", agent_name=slug)
        monkeypatch.setitem(interactive_session._sessions, "sid-pool-term", term)

        async def scenario(sub):
            # A permission answer on a headless session of the pool: the
            # gate alone decides (a live terminal adds its own identity rule).
            headless = _bare_connection(pool, "sid-pool-headless", user_sub=sub,
                                        agent=slug, layer=layer)
            answered = await headless._may_resolve_permission("req-unknown")
            conn = _bare_connection(pool, "sid-pool-term", user_sub=sub, agent=slug, layer=layer)
            await conn._handle_pty_takeover({"chat_id": pool})
            return conn.sent + headless.sent, answered

        sent, answered = asyncio.run(scenario("user-viewer"))
        assert term.alive and term.closed == [] and answered is False
        assert any("editor role" in f.get("message", "") for f in sent), sent

        sent, answered = asyncio.run(scenario("user-viewer2"))
        assert term.closed == ["taken_over"] and answered is True, sent

    def test_an_agent_scope_worker_chat_takes_the_editor_tier(self, temp_db, monkeypatch):
        """A delegate worker chat a no-user session spawned runs as the
        agent, on a collaborative agent too: the same gate."""
        from core.session.visibility import shared_chat_owner
        from storage import database as task_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent(collaborative=True, default_scope="user")
        task_store.add_user_agent("user-viewer", slug, "contributor", "test")
        task_store.add_user_agent("user-viewer2", slug, "editor", "test")
        worker = _make_chat(slug, user_sub=shared_chat_owner(slug))

        async def scenario(sub):
            conn = _bare_connection(worker, None, user_sub=sub, agent=slug, layer=layer)
            denied = await conn._deny_task_continue(worker)
            return denied, conn.sent

        denied, sent = asyncio.run(scenario("user-viewer"))
        assert denied and any("editor role" in f.get("message", "") for f in sent)
        denied, sent = asyncio.run(scenario("user-viewer2"))
        assert not denied and sent == []
        # A per-user chat on the same agent is its owner's: nothing to gate.
        own = _make_chat(slug, user_sub="user-viewer")
        conn = _bare_connection(own, None, user_sub="user-viewer", agent=slug)
        assert asyncio.run(conn._deny_task_continue(own)) == "" and conn.sent == []

    def test_a_read_only_attach_evicts_nobody_and_keeps_the_prompt_slot(
            self, temp_db, monkeypatch):
        """Attaching as a viewer who may not drive the session mirrors its
        output only: the controller keeps its viewer, its prompt slot
        (prompts are delivered once) and its exit notice."""
        from core.session import interactive_session
        from core.session.visibility import shared_chat_owner
        from storage import database as task_store
        from tests.fixtures.ws_dashboard_harness import FakeInteractiveSession
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent(collaborative=False, default_scope="agent")
        task_store.add_user_agent("user-viewer", slug, "contributor", "test")
        pool = _make_chat(slug, user_sub=shared_chat_owner(slug), session_id="sid-mirror")

        class _Owned(FakeInteractiveSession):
            def may_drive(self, sender_sub):
                return sender_sub == "user-admin"
            username = "admin"

        sess = _Owned("sid-mirror", pool, scrollback=b"screen")
        monkeypatch.setitem(interactive_session._sessions, "sid-mirror", sess)
        controller_perm = object()
        sess.on_perm_event = controller_perm

        async def scenario(sub):
            conn = _bare_connection(pool, "sid-mirror", user_sub=sub, agent=slug, layer=layer)
            conn._pty_viewer_sid = None
            conn._pty_listener = None
            await conn._attach_pty_viewer(sess)
            return conn.sent

        sent = asyncio.run(scenario("user-viewer"))
        assert sess.evict_cb is None and sess.on_perm_event is controller_perm
        assert sess.on_close is None and sess.on_status is None
        assert [f["state"] for f in sent if f["type"] == "pty_status"] == ["attached", "read_only"]
        assert any(f.get("replay") for f in sent if f["type"] == "pty_output")

        sent = asyncio.run(scenario("user-admin"))
        assert sess.evict_cb is not None and sess.on_perm_event is not controller_perm
        assert sess.on_close is not None
        assert [f["state"] for f in sent if f["type"] == "pty_status"] == ["attached"]


class _FakeTerminal:
    """A live interactive session: the identity rule and what was driven."""

    def __init__(self, chat_id, session_id, *, user_sub="", username="owner",
                 agent_name="agent"):
        self.chat_id = chat_id
        self.session_id = session_id
        self.user_sub = user_sub
        self.username = username
        self.agent_name = agent_name
        self.alive = True
        self.inputs: list = []
        self.resized: list = []
        self.interrupted = 0
        self.closed: list[str] = []

    async def close(self, *, reason: str = "closed") -> None:
        self.alive = False
        self.closed.append(reason)

    def may_drive(self, sender_sub):
        from core.session import session_kind
        if not self.user_sub or not sender_sub or session_kind.is_task_chat_id(self.chat_id):
            return True
        return sender_sub == self.user_sub

    def deliver_dashboard_input(self, data, composer=False, sender_sub=""):
        if not self.may_drive(sender_sub):
            return False
        self.inputs.append(data)
        return True

    def resize(self, rows, cols):
        self.resized.append((rows, cols))

    def interrupt_turn(self):
        self.interrupted += 1


def _bare_connection(chat_id, session_id, *, user_sub, agent, layer=None):
    """A dashboard connection with no socket: the handlers under test run
    against the real test DB and a recorded outbox."""
    from ws.dashboard import DashboardConnection
    conn = DashboardConnection.__new__(DashboardConnection)
    conn.user_sub = user_sub
    conn.user = {"role": "member", "username": "viewer"}
    conn.user_role = "member"
    conn.chat_id = chat_id
    conn.agent_name = agent
    conn.session_id = session_id
    conn.layer = layer
    conn.streaming = False
    conn.pending_control_requests = []
    conn.message_queue = []
    conn.artifact_queue = []
    conn.implementing_plan = ""
    conn.deferred_mode = None
    conn._pre_warmed_sid = None
    conn._warmup_task = None
    conn.notify_connection_id = "conn-test"
    conn.sent = []

    async def _send(frame):
        conn.sent.append(frame)

    async def _send_error(text):
        conn.sent.append({"type": "error", "message": text})
    conn._send = _send
    conn._send_error = _send_error
    return conn


class TestBoundTaskChatFramesPassTheContinueGate:
    """A viewer may open an agent-scope task run's chat and watch it, never
    drive it: every frame that acts on the BOUND chat's session (terminal
    keys, resize, attachments, Stop, implement-plan, compaction, a plan or
    permission answer under a made-up id) passes the task continue-gate, a
    frame naming another chat is refused, and a read-only viewer never
    changes a colleague's live terminal's mode."""

    def _task_chat(self, slug):
        from storage import database as task_store
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        task_store.create_run(run_id, "dyn-x", slug, "manual", None, "go",
                              "one-time", "agent", "user-admin")
        return f"task-{run_id}"

    def test_terminal_frames_on_a_task_terminal(self, temp_db, monkeypatch):
        import base64
        from core.session import interactive_session
        from storage import database as task_store
        slug = make_test_agent()
        task_store.add_user_agent("user-viewer", slug, "viewer", "test")
        task_store.add_user_agent("user-viewer2", slug, "editor", "test")
        cid = self._task_chat(slug)
        term = _FakeTerminal(cid, "sid-task-term")
        monkeypatch.setitem(interactive_session._sessions, "sid-task-term", term)

        async def scenario(sub):
            conn = _bare_connection(cid, "sid-task-term", user_sub=sub, agent=slug)
            await conn._on_pty_input({"data": base64.b64encode(b"rm -rf /workspace\r").decode()})
            await conn._on_pty_resize({"rows": 10, "cols": 20})
            await conn._on_abort({})
            return conn.sent

        sent = asyncio.run(scenario("user-viewer"))
        assert term.inputs == [] and term.resized == [] and term.interrupted == 0
        assert {"type": "error", "message": "Access denied"} in sent
        asyncio.run(scenario("user-viewer2"))
        assert term.inputs and term.resized == [(10, 20)] and term.interrupted == 1

    def test_a_frame_naming_another_chat_never_runs_on_the_bound_one(self, temp_db, monkeypatch):
        from storage import database as task_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        task_store.add_user_agent("user-viewer", slug, "viewer", "test")
        cid = self._task_chat(slug)
        layer.alive.add("sid-task")

        async def scenario():
            conn = _bare_connection(cid, "sid-task", user_sub="user-viewer",
                                    agent=slug, layer=layer)
            await conn._handle_chat({"text": "hi", "chat_id": "not-a-task-chat"})
            await conn._handle_implement_plan({"plan_path": "p.md", "mode": "dontAsk"})
            await conn._handle_implement_plan({"plan_path": "p.md", "mode": "yolo"})
            await conn._on_compact_context({})
            allowed = await conn._may_resolve_permission("made-up-request-id")
            return conn.sent, allowed

        sent, allowed = asyncio.run(scenario())
        assert layer.messages == [] and layer.closed_sessions == [] and layer.started == []
        assert allowed is False
        assert {"type": "done", "chat_id": "not-a-task-chat"} in sent
        assert {"type": "error", "message": "Invalid mode: yolo"} in sent
        assert sent.count({"type": "error", "message": "Access denied"}) == 3
        assert task_store.get_chat(cid) is None or \
            task_store.get_chat(cid).get("permission_mode") != "dontAsk"

    def test_a_read_only_viewer_never_changes_a_colleagues_terminal_mode(
            self, temp_db, monkeypatch):
        from core.session import interactive_session
        from core.session.session_state import get_session_mode, set_session_mode
        from core.session.visibility import shared_chat_owner
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent(collaborative=False, default_scope="agent")
        pool = _make_chat(slug, user_sub=shared_chat_owner(slug), session_id="sid-pool")
        term = _FakeTerminal(pool, "sid-pool", user_sub="user-admin")
        monkeypatch.setitem(interactive_session._sessions, "sid-pool", term)
        set_session_mode("sid-pool", "default")

        async def scenario():
            conn = _bare_connection(pool, "sid-pool", user_sub="user-viewer",
                                    agent=slug, layer=layer)
            await conn._handle_mode_change({"mode": "dontAsk", "chat_id": pool})
            return conn.sent

        sent = asyncio.run(scenario())
        assert get_session_mode("sid-pool") == "default"
        # The viewer holds no role row on the Shared-only agent, so the drive
        # gate refuses first; a below-editor viewer WITH a row on the agent's
        # terminal gets the terminal's own read_only answer. Either refusal
        # leaves the colleague's mode alone.
        assert any(f.get("state") == "read_only" or "editor role" in f.get("message", "")
                   for f in sent), sent
        assert layer.mode_changes == []


def test_a_new_chat_mode_pick_never_rides_the_streaming_chats_queue(temp_db, monkeypatch):
    """New Chat while chat A streams: the pick goes to the pre-warm now,
    never into A's deferred control queue (flushed into A's session)."""
    layer = FakeExecutionLayer()
    stub_dashboard_seams(monkeypatch, layer)
    slug = make_test_agent()
    chat_a = _make_chat(slug, session_id="sid-a")
    layer.alive.update({"sid-a", "sid-prewarm"})

    async def scenario():
        conn = _bare_connection(chat_a, "sid-a", user_sub="user-admin", agent=slug, layer=layer)
        conn.streaming = True
        conn._pre_warmed_sid = "sid-prewarm"
        await conn._handle_mode_change({"mode": "plan"})
        return conn

    conn = asyncio.run(scenario())
    assert conn.pending_control_requests == []
    assert layer.mode_changes == [("sid-prewarm", "plan")]


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
                    "process_alive": False,  # dead session → cross-engine options may show
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
                    "process_alive": True,  # live session → engine stays locked
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
# warmup_ready locality-mismatch fields (session-locality-ux W1).
# ---------------------------------------------------------------------------

def _insert_machine(machine_id, name):
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO remote_machines (id, name, registered_by, created_at) "
            "VALUES (%s, %s, 'admin-1', '2026-06-11T00:00:00+00:00')",
            (machine_id, name),
        )
        conn.commit()


class TestWarmupTargetMismatchFields:
    """`_target_mismatch_fields` on warmup_ready: a chat PINNED away from the
    agent's INDEPENDENTLY-resolved current target advertises the mismatch —
    to its owner (or a platform admin) only; unpinned chats and non-owner
    viewers get the plain frame (a non-owner's resolve is role-forced local,
    so the fields would advertise a move the move op refuses)."""

    def _stub_remote_layer(self, monkeypatch, layer):
        # A machine-pinned chat resolves its execution layer via
        # `_get_remote_layer()` — point it at the fake (patched at its own
        # module) so pinned-chat resumes stay hermetic.
        from core.session import session_manager as sm
        monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)

    def test_owner_sees_mismatch_fields_on_lazy_resume(self, temp_db,
                                                       monkeypatch):
        from storage import database as task_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        self._stub_remote_layer(monkeypatch, layer)
        slug = make_test_agent()
        _insert_machine("m-office", "Office-PC")
        cid = _make_chat(slug, messages=(("user", "hi"),
                                         ("assistant", "hello")))
        task_store.update_chat(cid, execution_target="m-office")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                history = await ws.next_frame()
                assert history["type"] == "chat_history"
                # Lazy warmup_ready carries the 4-field mismatch payload.
                await ws.expect({
                    "type": "warmup_ready", "session_id": None,
                    "chat_id": cid, "mode": "default", "model": TEST_MODEL,
                    "execution_path": "claude-code-cli",
                    "execution_mode": "", "needs_warmup": True,
                    "pinned_target": "m-office", "pinned_label": "Office-PC",
                    "resolved_target": "local",
                    "resolved_label": "local sandbox",
                })
                await ws.expect({"type": "queue_snapshot", "chat_id": cid,
                                 "messages": []})
                assert layer.started == []  # still no spawn for browsing
        run_ws_scenario(scenario)

    def test_non_owner_viewer_gets_no_mismatch_fields(self, temp_db,
                                                      monkeypatch):
        # A Shared-only agent's assigned viewer may OPEN the shared pool's
        # pinned chat (the ``agent::`` owner every dashboard chat on such an
        # agent gets), but the mismatch fields must stay off for them.
        from core.session.visibility import shared_chat_owner
        from storage import database as task_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        self._stub_remote_layer(monkeypatch, layer)
        slug = make_test_agent(default_scope="agent", collaborative=False)
        task_store.set_user_agents("user-viewer", [slug], "user-admin",
                                   agent_roles={slug: "viewer"})
        _insert_machine("m-office", "Office-PC")
        cid = _make_chat(slug, user_sub=shared_chat_owner(slug))
        task_store.update_chat(cid, execution_target="m-office")

        async def scenario():
            cookie = session_cookie(sub="user-viewer", email="viewer@test.com",
                                    name="Viewer User", role="member")
            async with dashboard_connection(cookie) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                history = await ws.next_frame()
                assert history["type"] == "chat_history"
                await ws.expect({
                    "type": "warmup_ready", "session_id": None,
                    "chat_id": cid, "mode": "default", "model": TEST_MODEL,
                    "execution_path": "claude-code-cli",
                    "execution_mode": "", "needs_warmup": True,
                })
                await ws.expect({"type": "queue_snapshot", "chat_id": cid,
                                 "messages": []})
        run_ws_scenario(scenario)

    def test_unpinned_chat_gets_no_mismatch_fields(self, temp_db,
                                                   monkeypatch):
        from storage import database as task_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        cid = _make_chat(slug)
        task_store.update_chat(cid, execution_target="")  # no pin at all

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                history = await ws.next_frame()
                assert history["type"] == "chat_history"
                await ws.expect({
                    "type": "warmup_ready", "session_id": None,
                    "chat_id": cid, "mode": "default", "model": TEST_MODEL,
                    "execution_path": "claude-code-cli",
                    "execution_mode": "", "needs_warmup": True,
                })
                await ws.expect({"type": "queue_snapshot", "chat_id": cid,
                                 "messages": []})
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

                ws.client_send({"type": "mode_change", "mode": "acceptEdits",
                                "chat_id": chat_id})
                await ws.expect({"type": "mode_changed",
                                 "mode": "acceptEdits"})
                await sync_dispatch(ws)
                assert layer.mode_changes == [(sid, "acceptEdits")]
                assert temp_db.get_chat(chat_id)["permission_mode"] == \
                    "acceptEdits"

                ws.client_send({"type": "mode_change", "mode": "yolo",
                                "chat_id": chat_id})
                await ws.expect({"type": "error",
                                 "message": "Invalid mode: yolo"})
        run_ws_scenario(scenario)

    def test_picks_without_a_chat_leave_the_bound_chat_alone(self, temp_db,
                                                              monkeypatch):
        # The new-chat state: after "+ New Chat" the socket is still bound to
        # the previous chat until the first warmup. A pick sent WITHOUT a
        # chat_id is deferred for the chat the warmup will mint and never
        # reaches the bound chat's row or its live session (it did, until
        # 2026-09-24). The deferred model then seeds the new chat.
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        from storage.billing import subscription_store
        monkeypatch.setattr(
            subscription_store, "list_models",
            lambda path: [{"model_id": TEST_MODEL, "enabled": True},
                          {"model_id": "other-model", "enabled": True}],
        )

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_a, sid_a = await warm_new_chat(ws, layer, slug)

                ws.client_send({"type": "model_change", "model": "other-model"})
                await ws.expect({"type": "model_changed",
                                 "model": "other-model"})
                ws.client_send({"type": "mode_change", "mode": "acceptEdits"})
                await ws.expect({"type": "mode_changed",
                                 "mode": "acceptEdits"})
                await sync_dispatch(ws)
                assert layer.model_changes == []
                assert layer.mode_changes == []
                row_a = temp_db.get_chat(chat_a)
                assert row_a["model"] == TEST_MODEL
                assert row_a["permission_mode"] == "default"

                # The next new chat starts on the deferred picks.
                layer.start_gate = asyncio.Event()
                ws.client_send({"type": "warmup", "agent": slug})
                started = await ws.expect({
                    "type": "warmup_started", "chat_id": ANY, "agent": slug,
                    "execution_path": "claude-code-cli",
                    "execution_target": "local", "new_chat": True,
                })
                await ws.expect({"type": "notification_count", "count": 0})
                layer.start_gate.set()
                await ws.expect({
                    "type": "warmup_ready", "session_id": ANY,
                    "chat_id": started["chat_id"], "mode": "acceptEdits",
                    "model": "other-model", "execution_path": "claude-code-cli",
                    "execution_target": "local", "fallback_reason": None,
                    "offline_machine_name": "", "interactive": False,
                })
                assert started["chat_id"] != chat_a
                assert temp_db.get_chat(started["chat_id"])["model"] == "other-model"
                assert temp_db.get_chat(chat_a)["model"] == TEST_MODEL
        run_ws_scenario(scenario)

    def test_picks_naming_another_chat_persist_to_it_only(self, temp_db,
                                                          monkeypatch):
        # A frame naming a chat this socket is not bound to (a pick that
        # raced a reconnect's resume_chat) writes that chat's row and never
        # touches the bound session — the execution-mode precedent.
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_a, sid_a = await warm_new_chat(ws, layer, slug)
                chat_b = _make_chat(slug)

                ws.client_send({"type": "model_change", "model": TEST_MODEL,
                                "chat_id": chat_b})
                await ws.expect({"type": "model_changed", "model": TEST_MODEL,
                                 "chat_id": chat_b})
                ws.client_send({"type": "mode_change", "mode": "plan",
                                "chat_id": chat_b})
                await ws.expect({"type": "mode_changed", "mode": "plan",
                                 "chat_id": chat_b})
                await sync_dispatch(ws)
                assert layer.model_changes == []
                assert layer.mode_changes == []
                assert temp_db.get_chat(chat_b)["permission_mode"] == "plan"
                assert temp_db.get_chat(chat_a)["permission_mode"] == "default"
        run_ws_scenario(scenario)

    def test_resume_clears_a_new_chat_deferral(self, temp_db, monkeypatch):
        # A pick deferred in the new-chat state must not follow the socket
        # into an existing chat it then opens: resume_chat drops it.
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        # The connection object itself: the deferrals are socket state the
        # frames never show.
        from ws import dashboard as dashboard_mod
        conns: list = []
        real_conn = dashboard_mod.DashboardConnection

        class _Recording(real_conn):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                conns.append(self)
        monkeypatch.setattr(dashboard_mod, "DashboardConnection", _Recording)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "model_change", "model": "other-model"})
                await ws.expect({"type": "model_changed",
                                 "model": "other-model"})
                ws.client_send({"type": "mode_change", "mode": "plan"})
                await ws.expect({"type": "mode_changed", "mode": "plan"})
                await sync_dispatch(ws)
                assert conns[0].deferred_model == "other-model"
                assert conns[0].deferred_mode == "plan"

                chat_b = _make_chat(slug, session_id=str(uuid.uuid4()),
                                    messages=(("user", "hi"),))
                ws.client_send({"type": "resume_chat", "chat_id": chat_b})
                await ws.expect({
                    "type": "chat_history", "chat_id": chat_b, "agent": ANY,
                    "messages": ANY, "has_more": False,
                    "restore": {"todos": [], "meeting": None, "goal": None},
                    "plans": [], "total_cost": 0,
                    "context_used": 0, "context_max": 0,
                    "cache_read": 0, "cache_write": 0, "output_tokens": 0,
                    "execution_path": "claude-code-cli",
                    "execution_mode": "", "model": TEST_MODEL, "mode": "default",
                    "process_alive": False,
                })
                await ws.expect({
                    "type": "warmup_ready", "session_id": None,
                    "chat_id": chat_b, "mode": "default", "model": TEST_MODEL,
                    "execution_path": "claude-code-cli",
                    "execution_mode": "", "needs_warmup": True,
                })
                await ws.expect({"type": "queue_snapshot", "chat_id": chat_b,
                                 "messages": []})
                assert conns[0].deferred_model == ""
                assert conns[0].deferred_mode == ""
                assert temp_db.get_chat(chat_b)["model"] == TEST_MODEL
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
                                "model": "foreign-model", "chat_id": chat_id})
                await ws.expect({"type": "model_changed", "model": TEST_MODEL,
                                 "chat_id": chat_id})
                assert layer.model_changes == []
                assert temp_db.get_chat(chat_id)["model"] == TEST_MODEL

                ws.client_send({"type": "model_change", "model": TEST_MODEL,
                                "chat_id": chat_id})
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
                from core.session import session_events
                t0 = time.monotonic()
                ws.client_send({"type": "chat", "text": "queued A"})
                await ws.expect({"type": "queued", "index": 0,
                                 "text": "queued A", "chat_id": chat_id})
                # A queued message cuts the turn: no check judges or
                # continues it (CHECKS.md).
                assert session_events.user_message_since(sid, t0)
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

                # The queued turn-start "streaming" broadcast is dropped as stale at
                # drain time (the turn already ended) — only the "ready" flows.
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": "WS Dash Test finished",  # display name (harness agent), not slug
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

    async def _busy_turn(self, ws, layer, slug, hold):
        """Warm a chat and open a turn that waits on ``hold``; returns
        (chat_id, sid) with the prelude frames consumed."""
        from core.events.common_events import CommonEvent, TEXT, DONE

        async def first_turn():
            yield CommonEvent(type=TEXT, data={"content": "working…"})
            await hold.wait()
            yield CommonEvent(type=DONE, data={})

        def script(sid, prompt):
            if len(layer.messages) <= 1:
                return first_turn()
            return [
                CommonEvent(type=TEXT, data={"content": "drained"}),
                CommonEvent(type=DONE, data={}),
            ]
        layer.turn_events = script
        chat_id, sid = await warm_new_chat(ws, layer, slug)
        ws.client_send({"type": "chat", "text": "long job", "chat_id": chat_id})
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
        return chat_id, sid

    def _seed_upload(self, slug: str) -> str:
        import config as cfg
        rel = "users/admin/workspace/uploads/files/notes.txt"
        path = cfg.AGENTS_DIR / slug / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("hello")
        return rel

    def test_busy_send_carries_attachments(self, temp_db, monkeypatch):
        # A message sent while the agent works carries exactly what an idle
        # send carries: the photo is saved and the file validated NOW, the
        # engine text gets their sandbox-virtual paths (steered here), the row
        # and the frame get the meta. An attachment-only message is no longer
        # dropped (queued here, the steer refused) and its drained turn keeps
        # the photo.
        import json
        import config as cfg
        layer = FakeExecutionLayer()
        layer.steer_accepts = True
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        rel_file = self._seed_upload(slug)
        agent_dir = cfg.AGENTS_DIR / slug

        async def scenario():
            hold = asyncio.Event()
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await self._busy_turn(ws, layer, slug, hold)

                ws.client_send({
                    "type": "chat", "text": "see these",
                    "images": [{"data": _PNG_DATA_URL, "name": "dot.png"}],
                    "files": [{"path": rel_file, "name": "notes.txt"}],
                })
                steered = await ws.expect({
                    "type": "steered", "text": "see these", "chat_id": chat_id,
                    "images": ANY,
                    "files": [{"path": rel_file, "name": "notes.txt"}],
                })
                assert steered["images"][0]["name"] == "dot.png"
                saved = steered["images"][0]["path"]
                assert saved.startswith("users/admin/workspace/uploads/photos/img_")
                assert (agent_dir / saved).is_file()
                _sid, engine_text = layer.steered[0]
                assert engine_text.startswith("see these")
                assert f"- /{saved}" in engine_text
                assert f"- /{rel_file}" in engine_text

                layer.steer_accepts = False
                ws.client_send({
                    "type": "chat",
                    "images": [{"data": _PNG_DATA_URL, "name": "only.png"}],
                })
                queued = await ws.expect({
                    "type": "queued", "index": 0, "text": "",
                    "chat_id": chat_id, "images": ANY,
                })
                assert queued["images"][0]["name"] == "only.png"

                hold.set()
                sent = await ws.expect({"type": "queue_sent", "text": "",
                                        "chat_id": chat_id, "images": ANY})
                assert sent["images"][0]["name"] == "only.png"
                await ws.expect({"type": "text", "content": "drained",
                                 "chat_id": chat_id})
                await ws.expect({"type": "done", "chat_id": chat_id})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": "WS Dash Test finished",
                                 "body": "Response ready"})

                msgs = temp_db.get_chat_messages(chat_id)
                assert [(m["role"], m["content"]) for m in msgs] == [
                    ("user", "long job"),
                    ("user", "see these"),
                    ("assistant", "working…"),
                    ("user", ""),
                    ("assistant", "drained"),
                ]
                steered_meta = json.loads(msgs[1]["event_data"])
                assert steered_meta["files"] == [{"path": rel_file, "name": "notes.txt"}]
                assert steered_meta["images"][0]["path"] == saved
                drained_meta = json.loads(msgs[3]["event_data"])
                assert drained_meta["images"][0]["name"] == "only.png"
                # The drained turn's engine text names the saved photo.
                drained_prompt = layer.messages[1][1]
                assert "- /users/admin/workspace/uploads/photos/img_" in drained_prompt
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_cancel_returns_the_queued_attachments(self, temp_db, monkeypatch):
        # The cancel frame hands the item's attachments back with its text,
        # so the composer can show them again.
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        rel_file = self._seed_upload(slug)

        async def scenario():
            hold = asyncio.Event()
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await self._busy_turn(ws, layer, slug, hold)

                ws.client_send({"type": "chat", "text": "later",
                                "files": [{"path": rel_file, "name": "notes.txt"}]})
                await ws.expect({
                    "type": "queued", "index": 0, "text": "later",
                    "chat_id": chat_id,
                    "files": [{"path": rel_file, "name": "notes.txt"}],
                })
                ws.client_send({"type": "cancel_queued", "index": 0})
                await ws.expect({
                    "type": "queue_removed", "index": 0, "text": "later",
                    "chat_id": chat_id,
                    "files": [{"path": rel_file, "name": "notes.txt"}],
                })
                hold.set()
                await ws.expect({"type": "done", "chat_id": chat_id})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": "WS Dash Test finished",
                                 "body": "Response ready"})
                assert [p for _s, p, _k in layer.messages] == ["long job"]
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_inline_photo_engine_gets_the_blocks_on_the_drain(self, temp_db,
                                                              monkeypatch):
        # An engine that takes photos inline (Direct LLM) gets the queued
        # photo as a vision block on the drained turn, and no path text.
        import dataclasses
        layer = FakeExecutionLayer()
        layer.capabilities = dataclasses.replace(
            layer.capabilities,
            behaviour=dataclasses.replace(layer.capabilities.behaviour,
                                          attach_images_inline=True),
        )
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            hold = asyncio.Event()
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await self._busy_turn(ws, layer, slug, hold)

                ws.client_send({"type": "chat", "text": "look",
                                "images": [{"data": _PNG_DATA_URL, "name": "dot.png"}]})
                await ws.expect({"type": "queued", "index": 0, "text": "look",
                                 "chat_id": chat_id, "images": ANY})
                hold.set()
                await ws.expect({"type": "queue_sent", "text": "look",
                                 "chat_id": chat_id, "images": ANY})
                await ws.expect({"type": "text", "content": "drained",
                                 "chat_id": chat_id})
                await ws.expect({"type": "done", "chat_id": chat_id})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": "WS Dash Test finished",
                                 "body": "Response ready"})
                _sid, prompt, kwargs = layer.messages[1]
                assert prompt == "look"
                assert kwargs["images"][0]["media_type"] == "image/png"
                assert kwargs["images"][0]["base64"]
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

                from core.session import session_events
                t0 = time.monotonic()
                ws.client_send({"type": "chat", "text": "also check logs"})
                await ws.expect({"type": "steered", "text": "also check logs",
                                 "chat_id": chat_id})
                assert layer.steered == [(sid, "also check logs")]
                # A steered message is part of the running turn: the
                # turn is still judged.
                assert not session_events.user_message_since(sid, t0)

                hold.set()  # the (steered) turn completes — nothing queued
                await ws.expect({"type": "done", "chat_id": chat_id})
                # The queued turn-start "streaming" broadcast is dropped as stale at
                # drain time (the turn already ended) — only the "ready" flows.
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": "WS Dash Test finished",  # display name (harness agent), not slug
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

                from core.session import session_events
                t0 = time.monotonic()
                ws.client_send({"type": "abort"})
                await ws.expect({"type": "aborted", "chat_id": chat_id})
                assert layer.aborted == [sid]
                chat = temp_db.get_chat(chat_id)
                assert chat["last_turn_aborted"] is True
                assert chat["last_abort_graceful"] is True
                # The graceful turn still ends with its DONE: Stop is noted
                # so no check judges it and starts a fix round.
                assert session_events.user_message_since(sid, t0)

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
                # The queued turn-start "streaming" broadcast is dropped as stale at
                # drain time (the turn already ended) — only the "ready" flows.
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
                # The queued turn-start "streaming" broadcast is dropped as stale at
                # drain time (the turn already ended) — only the "ready" flows.
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": "WS Dash Test finished",  # display name (harness agent), not slug
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
                # The queued turn-start "streaming" broadcast is dropped as stale at
                # drain time (the turn already ended) — only the "ready" flows.
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": "WS Dash Test finished",  # display name (harness agent), not slug
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
                # The queued turn-start "streaming" broadcast is dropped as stale at
                # drain time (the turn already ended) — only the "ready" flows.
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                 "title": "WS Dash Test finished",  # display name (harness agent), not slug
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

    def test_a_viewer_resync_never_reaps(self, temp_db, monkeypatch):
        """A viewer that fell a full queue behind resyncs through a same-chat
        resume (fresh history + live_state) that never reaps the pump, even
        one that would qualify."""
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        self._hanging_turn(layer)

        async def scenario():
            from core.events import stream_pump
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await self._start_stalled_turn(ws, layer, slug)
                layer.idle_seconds[sid] = 500.0
                layer.probed_dead.add(sid)
                pump = stream_pump._active_pumps[chat_id]
                pump._ws_queues[0].put_nowait({"pump_type": stream_pump.PUMP_RESYNC})
                await self._drain_until(ws, "chat_history")
                await self._drain_until(ws, "live_state")
                assert layer.prepared_resume == []
                assert stream_pump._active_pumps.get(chat_id) is pump
                assert len(pump._ws_queues) == 1          # re-attached once
                pump.abort()  # test teardown
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
        """A chat STARTED by an app button (front-page action → fresh
        chat) titles as "App — Label", never the raw framing brackets. Both
        rails: send-time chokepoint here, tailer twin below (interactive
        terminals type the same framed text into the PTY)."""
        framed = ('[action from app "Infra Dashboard" — Refresh data]\n'
                  '```text\nRefresh the dashboard with fresh metrics\n```')
        assert self._title(framed) == "Infra Dashboard — Refresh data"
        # Interactive delivery prepends the time stamp before the framing.
        stamped = ("[Current time: Friday, July 10, 2026 19:00 (7:00 PM) "
                   "UTC (UTC+00:00)]\n" + framed)
        assert self._title(stamped) == "Infra Dashboard — Refresh data"
        # Transcripts written before the rename carry the older spelling.
        older = framed.replace("action from app", "action from mini-app")
        assert self._title(older) == "Infra Dashboard — Refresh data"

    def test_tailer_twin_recognizes_app_action_framing(self):
        from core.session.transcript_tailer import _title_from_prompt
        framed = ('[action from app "Infra Dashboard" — Refresh data]\n'
                  '```text\nRefresh please\n```')
        assert _title_from_prompt(framed) == "Infra Dashboard — Refresh data"
        older = framed.replace("action from app", "action from mini-app")
        assert _title_from_prompt(older) == "Infra Dashboard — Refresh data"
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


# ---------------------------------------------------------------------------
# Self-heal on a bare `chat` frame: a WS that lost its state (reconnect
# before resume_chat) re-attaches to the chat's still-running pump.
# ---------------------------------------------------------------------------

class TestReattachToRunningPump:
    """The recovered-pump branch of ``_handle_chat`` reads the chat row for
    the frame it sends; it used to name a variable that only the resume path
    binds, so every reattach raised before ``warmup_ready`` went out."""

    def test_reattach_sends_warmup_ready_from_the_chat_row(self, monkeypatch):
        import contextlib
        from unittest.mock import AsyncMock, patch
        import ws.dashboard  # noqa: F401  # assembles the controller first
        from ws import dashboard_chat_send as mod

        class _Stop(Exception):
            pass

        class _Pump:
            session_id = "sess-live"
            is_done = False

        chat_row = {
            "user_sub": "user-admin", "agent": "alpha",
            "execution_path": "codex-cli", "execution_target": "local",
            "model": TEST_MODEL, "permission_mode": "auto",
        }
        sent: list[dict] = []

        class _Conn(mod.ChatSendMixin):
            user_sub = "user-admin"
            user_role = "admin"
            user_agents: set[str] = set()
            user = None
            notify_connection_id = "conn-1"
            chat_id = None
            agent_name = None
            session_id = None
            layer = None
            _view_only = False

            async def _send(self, frame):
                sent.append(frame)
                if frame.get("type") == "warmup_ready":
                    raise _Stop()

            async def _send_error(self, text):
                sent.append({"type": "error", "message": text})

            async def _deny_task_continue(self, chat_id):
                return False

            def _viewer_context(self):
                from auth.providers import UserContext
                return UserContext(sub=self.user_sub, email="", name="",
                                   role=self.user_role)

        monkeypatch.setattr(mod.task_store, "get_chat", lambda cid: chat_row)
        monkeypatch.setattr(mod, "_role_and_layer",
                            lambda *a, **k: ("admin", object()))
        monkeypatch.setattr(mod, "get_session_mode", lambda sid: "")
        monkeypatch.setattr(mod.notification_manager, "set_chat_turn_origin",
                            lambda *a, **k: None)
        monkeypatch.setitem(mod._active_pumps, "chat-1", _Pump())

        acquire = AsyncMock(return_value=True)

        async def run():
            conn = _Conn()
            with patch("core.concurrency.acquire_chat_slot", acquire), \
                    contextlib.suppress(_Stop):
                await conn._handle_chat({"text": "hi", "chat_id": "chat-1"})
            return conn

        conn = asyncio.run(run())
        assert conn.session_id == "sess-live"
        # A re-attach records the owner for the per-person count and is
        # never refused on the cap (the session is already theirs).
        assert acquire.await_args.kwargs["user_sub"] == conn.user_sub
        assert acquire.await_args.kwargs["per_user_cap"] is False
        ready = [f for f in sent if f.get("type") == "warmup_ready"]
        assert ready == [{
            "type": "warmup_ready", "session_id": "sess-live",
            "chat_id": "chat-1", "mode": "auto", "model": TEST_MODEL,
            "execution_path": "codex-cli",
        }]
