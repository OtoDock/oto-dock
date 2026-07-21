"""WS ``move_chat`` op — the session-locality escape hatch
(``ws/dashboard_dispatch._handle_move_chat``).

Drives the REAL handler through the ws_dashboard harness. Covers the gate
ladder in order — chat bound, owner/admin, warmup-in-flight, streaming/pump,
open interactive turn, resolve hard-fail, no-pin/same-target — and the happy
move: DB row transitioned via the machine-removed shape (pin cleared, session
ids dropped, ``pending_history_seed='moved:<from>'``), the old headless
session closed on the layer, the connection's session binding cleared, and
the ``chat_moved`` ack emitted.
"""

import asyncio
import uuid

from tests.fixtures.ws_dashboard_harness import (
    TEST_MODEL,
    FakeExecutionLayer,
    FakeInteractiveSession,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    stub_dashboard_seams,
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


def _insert_machine(machine_id, name):
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO remote_machines (id, name, registered_by, created_at) "
            "VALUES (%s, %s, 'admin-1', '2026-06-11T00:00:00+00:00')",
            (machine_id, name),
        )
        conn.commit()


def _stub_remote_layer(monkeypatch, layer):
    """A machine-pinned chat resolves its execution layer via
    ``_get_remote_layer()`` — point it at the fake (patched at its own
    module, the harness rule) so pinned-chat resumes stay hermetic."""
    from core.session import session_manager as sm
    monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)


def _capture_connections(monkeypatch):
    """Record every DashboardConnection the handler builds, so tests can put
    the connection into states the wire protocol can't reach directly
    (a live ``_warmup_task``, the ``streaming`` flag)."""
    import ws.dashboard as wsd
    captured = []
    orig_init = wsd.DashboardConnection.__init__

    def _init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        captured.append(self)
    monkeypatch.setattr(wsd.DashboardConnection, "__init__", _init)
    return captured


async def _drain_until(ws, ftype, timeout: float = 3.0):
    seen = []
    while True:
        try:
            frame = await ws.next_frame(timeout)
        except asyncio.TimeoutError:
            raise AssertionError(f"no {ftype!r} frame; saw {seen}") from None
        if frame["type"] == ftype:
            return frame
        seen.append(frame)


# ---------------------------------------------------------------------------
# Gate ladder — every refusal in handler order.
# ---------------------------------------------------------------------------

class TestMoveChatGates:
    def test_no_open_chat(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "move_chat"})
                await ws.expect({"type": "error",
                                 "message": "Chat not found."})
        run_ws_scenario(scenario)

    def test_non_owner_refused(self, temp_db, monkeypatch):
        # A Shared-only agent's assigned viewer may OPEN the owner's chat
        # (connection binding admits shared readers), but must never relocate
        # it — a non-owner's resolve is role-forced 'local' anyway.
        from storage import database as task_store
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent(default_scope="agent", collaborative=False)
        task_store.set_user_agents("user-viewer", [slug], "user-admin",
                                   agent_roles={slug: "viewer"})
        cid = _make_chat(slug, user_sub="user-admin")

        async def scenario():
            cookie = session_cookie(sub="user-viewer", email="viewer@test.com",
                                    name="Viewer User", role="member")
            async with dashboard_connection(cookie) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                ws.client_send({"type": "move_chat"})
                await ws.expect({"type": "error",
                                 "message": "Only the chat owner can move it."})
        run_ws_scenario(scenario)

    def test_warmup_in_flight_refused(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        conns = _capture_connections(monkeypatch)
        slug = make_test_agent()
        cid = _make_chat(slug)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                conn = conns[-1]
                conn._warmup_task = asyncio.get_running_loop().create_future()
                try:
                    ws.client_send({"type": "move_chat"})
                    await ws.expect({
                        "type": "error",
                        "message": "The chat is still getting ready — "
                                   "try again in a moment."})
                finally:
                    conn._warmup_task.cancel()
                    conn._warmup_task = None
        run_ws_scenario(scenario)

    def test_streaming_refused(self, temp_db, monkeypatch):
        # The connection-scoped streaming flag guards a mid-turn move. (The
        # sibling `pump and not pump.is_done` branch shares the same refusal
        # line for a DETACHED background turn.)
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        conns = _capture_connections(monkeypatch)
        slug = make_test_agent()
        cid = _make_chat(slug)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                conn = conns[-1]
                conn.streaming = True
                try:
                    ws.client_send({"type": "move_chat"})
                    await ws.expect({
                        "type": "error",
                        "message": "Finish or stop the current turn "
                                   "before moving the chat."})
                finally:
                    conn.streaming = False
        run_ws_scenario(scenario)

    def test_open_interactive_turn_refused(self, temp_db, monkeypatch):
        # A live PTY session mid-turn (turn_open) must refuse like the
        # headless streaming gate — the TUI owns the running turn.
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        sid = str(uuid.uuid4())
        cid = _make_chat(slug, session_id=sid)

        async def scenario():
            from core.session import interactive_session as isess_mod

            fake = FakeInteractiveSession(session_id=sid, chat_id=cid)
            fake._turn_open = True
            try:
                async with dashboard_connection(session_cookie()) as ws:
                    # Register AFTER the connect snapshot — a turn_open PTY
                    # session counts as "streaming now" and would land in
                    # chat_status_snapshot otherwise.
                    await drain_startup(ws)
                    isess_mod._sessions[sid] = fake
                    ws.client_send({"type": "resume_chat", "chat_id": cid})
                    await _drain_until(ws, "queue_snapshot")
                    ws.client_send({"type": "move_chat"})
                    await ws.expect({
                        "type": "error",
                        "message": "Finish or stop the current turn "
                                   "before moving the chat."})
            finally:
                isess_mod._sessions.pop(sid, None)
        run_ws_scenario(scenario)

    def test_resolve_hard_fail_refused(self, temp_db, monkeypatch):
        # The agent's current target resolves to the offline sentinel —
        # nowhere to move to, even though the chat IS pinned elsewhere.
        from storage import remote_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        _stub_remote_layer(monkeypatch, layer)
        slug = make_test_agent()
        _insert_machine("m-office", "Office-PC")
        cid = _make_chat(slug)
        from storage import database as task_store
        task_store.update_chat(cid, execution_target="m-office")
        monkeypatch.setattr(remote_store, "resolve_execution_target",
                            lambda *a, **k: ("__offline__:m-dead", None))

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                ws.client_send({"type": "move_chat"})
                await ws.expect({
                    "type": "error",
                    "message": "The agent's current machine is "
                               "offline — nowhere to move to."})
        run_ws_scenario(scenario)

    def test_unpinned_chat_refused(self, temp_db, monkeypatch):
        # No pin → nothing to move; the next warmup resolves fresh anyway.
        from storage import database as task_store
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        cid = _make_chat(slug)
        task_store.update_chat(cid, execution_target="")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                ws.client_send({"type": "move_chat"})
                await ws.expect({
                    "type": "error",
                    "message": "This chat already runs on the "
                               "agent's current target."})
        run_ws_scenario(scenario)

    def test_pin_equals_resolve_refused(self, temp_db, monkeypatch):
        # Pinned to 'local' while the agent resolves 'local' — no mismatch,
        # same refusal as unpinned.
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        cid = _make_chat(slug)  # create_chat pins 'local'; stub resolves 'local'

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                ws.client_send({"type": "move_chat"})
                await ws.expect({
                    "type": "error",
                    "message": "This chat already runs on the "
                               "agent's current target."})
        run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# Happy path — pinned to a machine, agent resolves 'local'.
# ---------------------------------------------------------------------------

class TestMoveChatHappyPath:
    def test_move_rebinds_closes_session_and_acks(self, temp_db, monkeypatch):
        from storage import database as task_store

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        _stub_remote_layer(monkeypatch, layer)
        conns = _capture_connections(monkeypatch)
        slug = make_test_agent()
        _insert_machine("m-office", "Office-PC")
        sid = str(uuid.uuid4())
        layer.alive.add(sid)  # live idle session on the pinned machine
        from core.session.session_state import set_session_mode
        set_session_mode(sid, "default")  # as a real spawn would
        cid = _make_chat(slug, session_id=sid,
                         messages=(("user", "hi"), ("assistant", "hello")))
        task_store.update_chat(cid, execution_target="m-office",
                               codex_thread_id="thread-1", context_used=90000)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "chat_history")
                # Alive-session re-attach: warmup_ready already advertises the
                # locality mismatch to the owner (pin != independent resolve).
                await ws.expect({
                    "type": "warmup_ready", "session_id": sid,
                    "chat_id": cid, "mode": "default", "model": TEST_MODEL,
                    "execution_path": "claude-code-cli", "interactive": False,
                    "pinned_target": "m-office", "pinned_label": "Office-PC",
                    "resolved_target": "local",
                    "resolved_label": "local sandbox",
                })
                await ws.expect({"type": "queue_snapshot", "chat_id": cid,
                                 "messages": []})

                ws.client_send({"type": "move_chat"})
                await ws.expect({"type": "chat_moved", "chat_id": cid,
                                 "new_target": "local",
                                 "resolved_label": "local sandbox"})

                # Old headless session closed on its layer (slot released,
                # session file left behind by design) …
                assert layer.closed_sessions == [sid]
                # … the connection's own binding cleared …
                assert conns[-1].session_id is None
                # … and the DB row transitioned via the machine-removed shape.
                row = task_store.get_chat(cid)
                assert row["execution_target"] == ""
                assert row["session_id"] is None
                assert row["codex_thread_id"] is None
                assert row["context_used"] == 0
                assert row["pending_history_seed"] == "moved:Office-PC"
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)
