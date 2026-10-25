"""WS ``move_chat``: the socket's door to a chat move
(``ws/dashboard_chat_move.handle_move_chat`` → ``services/remote/chat_move``).

Drives the REAL handler through the ws_dashboard harness: the refusals
come back as ``error`` frames with their sentence; a move runs as a job
and its outcome reaches this socket as the ``chat_moved`` broadcast (no
direct ack). The happy path here is a chat pinned to a machine that is
not connected, moved to the server: its files cannot travel, so the chat
is rebound the reseed way (``moved:<move id>``) and the move row says why.
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


def _insert_machine(machine_id, name, *, scope="admin", owner="admin-1"):
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO remote_machines (id, name, registered_by, pairing_scope, created_at) "
            "VALUES (%s, %s, %s, %s, '2026-06-11T00:00:00+00:00')",
            (machine_id, name, owner, scope),
        )
        conn.commit()


def _stub_remote_layer(monkeypatch, layer):
    from core.session import session_manager as sm
    monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)


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


async def _open(ws, cid):
    await drain_startup(ws)
    ws.client_send({"type": "resume_chat", "chat_id": cid})
    await _drain_until(ws, "queue_snapshot")


class TestMoveChatRefusals:
    def test_no_open_chat(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "move_chat"})
                await ws.expect({"type": "error", "message": "Chat not found."})
        run_ws_scenario(scenario)

    def test_a_viewer_of_a_shared_chat_cannot_move_it(self, temp_db, monkeypatch):
        from core.session.visibility import shared_chat_owner
        from storage import database as task_store
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent(default_scope="agent", collaborative=False)
        task_store.set_user_agents("user-viewer", [slug], "user-admin",
                                   agent_roles={slug: "viewer"})
        cid = _make_chat(slug, user_sub=shared_chat_owner(slug))

        async def scenario():
            cookie = session_cookie(sub="user-viewer", email="viewer@test.com",
                                    name="Viewer User", role="member")
            async with dashboard_connection(cookie) as ws:
                await _open(ws, cid)
                ws.client_send({"type": "move_chat", "target": "local"})
                await ws.expect({"type": "error", "message":
                                 "Only someone who can continue this chat can move it."})
        run_ws_scenario(scenario)

    def test_a_warmup_in_flight_refuses(self, temp_db, monkeypatch):
        from core.session import warmup_registry
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        _insert_machine("m-office", "Office-PC")
        cid = _make_chat(slug)
        from storage import database as task_store
        task_store.update_chat(cid, execution_target="m-office")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await _open(ws, cid)
                await warmup_registry.register(cid, "user-admin", slug)
                try:
                    ws.client_send({"type": "move_chat", "target": "local"})
                    await ws.expect({"type": "error", "message":
                                     "Finish or stop the current turn before moving the chat."})
                finally:
                    await warmup_registry.unregister(cid)
        run_ws_scenario(scenario)

    def test_an_open_terminal_turn_refuses(self, temp_db, monkeypatch):
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        _insert_machine("m-office", "Office-PC")
        sid = str(uuid.uuid4())
        cid = _make_chat(slug, session_id=sid)
        from storage import database as task_store
        task_store.update_chat(cid, execution_target="m-office")

        async def scenario():
            from core.session import interactive_session as isess_mod
            fake = FakeInteractiveSession(session_id=sid, chat_id=cid)
            fake._turn_open = True
            try:
                async with dashboard_connection(session_cookie()) as ws:
                    await drain_startup(ws)
                    isess_mod._sessions[sid] = fake
                    ws.client_send({"type": "resume_chat", "chat_id": cid})
                    await _drain_until(ws, "queue_snapshot")
                    ws.client_send({"type": "move_chat", "target": "local"})
                    await ws.expect({"type": "error", "message":
                                     "Finish or stop the current turn before moving the chat."})
            finally:
                isess_mod._sessions.pop(sid, None)
        run_ws_scenario(scenario)

    def test_the_same_place_is_refused(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        cid = _make_chat(slug)  # pinned local; the default is local

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await _open(ws, cid)
                ws.client_send({"type": "move_chat"})
                await ws.expect({"type": "error", "message": "This chat already runs there."})
        run_ws_scenario(scenario)

    def test_a_place_not_offered_is_refused(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        cid = _make_chat(slug)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await _open(ws, cid)
                ws.client_send({"type": "move_chat", "target": "no-such-machine"})
                await ws.expect({"type": "error", "message": "You can't move this chat there."})
        run_ws_scenario(scenario)

    def test_a_pick_against_a_chat_that_moved_meanwhile_is_refused(self, temp_db, monkeypatch):
        stub_dashboard_seams(monkeypatch, FakeExecutionLayer())
        slug = make_test_agent()
        _insert_machine("m-office", "Office-PC")
        cid = _make_chat(slug)
        from storage import database as task_store
        task_store.update_chat(cid, execution_target="m-office")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await _open(ws, cid)
                ws.client_send({"type": "move_chat", "target": "local", "from": "local"})
                await ws.expect({"type": "error", "message": "This chat just moved to Office-PC."})
        run_ws_scenario(scenario)


class TestMoveChatHappyPath:
    def test_an_offline_source_moves_by_summary_and_tells_the_socket(self, temp_db, monkeypatch):
        from storage import database as task_store, move_schema, remote_store

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        _stub_remote_layer(monkeypatch, layer)
        slug = make_test_agent()
        _insert_machine("m-office", "Office-PC")
        sid = str(uuid.uuid4())
        cid = _make_chat(slug, session_id=sid,
                         messages=(("user", "hi"), ("assistant", "hello")))
        task_store.update_chat(cid, execution_target="m-office", context_used=90000)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _drain_until(ws, "queue_snapshot")
                ws.client_send({"type": "move_chat", "target": "local", "from": "m-office"})
                moved = await _drain_until(ws, "chat_moved", timeout=10.0)
                assert moved["ok"] is True and moved["memory"] == move_schema.MEMORY_SUMMARY
                assert moved["new_target"] == "local" and moved["label"] == "OtoDock platform"
                assert moved["from_label"] == "Office-PC"
                assert "Office-PC was offline" in moved["message"]
                row = task_store.get_chat(cid)
                assert row["execution_target"] == "local"
                assert row["session_id"] is None and row["context_used"] == 0
                move = remote_store.latest_chat_move(cid)
                assert row["pending_history_seed"] == f"moved:{move['id']}"
                assert move["state"] == move_schema.SETTLED and move["why"] == move_schema.WHY_OFFLINE
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)


# -- the lock on every drive path (ws/dashboard_chat_support._deny_task_continue) --

_LOCK = "This chat is running on Ann's computer."


def _locked_connection(monkeypatch, cid, slug, *, user_sub="user-bob"):
    """A socket-less dashboard connection of ``user_sub`` on ``cid``, with the
    chat locked for everyone but Ann (the machine's owner)."""
    from services.remote import chat_move
    from ws import dashboard_chat_move
    from ws.dashboard import DashboardConnection
    monkeypatch.setattr(chat_move, "lock_sentence",
                        lambda chat, sub: "" if sub == "user-ann" else _LOCK)
    dashboard_chat_move._lock_cache.clear()
    conn = DashboardConnection.__new__(DashboardConnection)
    conn.user_sub = user_sub
    conn.user = {"role": "member", "username": user_sub}
    conn.user_role = "member"
    conn.chat_id = cid
    conn.agent_name = slug
    conn.session_id = "sid-locked"
    conn.layer = None
    conn.streaming = True
    conn.pending_control_requests = []
    conn.message_queue = []
    conn.artifact_queue = []
    conn.notify_connection_id = "conn-test"
    conn.sent = []

    async def _send(frame):
        conn.sent.append(frame)

    async def _send_error(text):
        conn.sent.append({"type": "error", "message": text})
    conn._send = _send
    conn._send_error = _send_error
    return conn


class _Pump:
    """A streaming turn of the locked chat: nothing may be queued into it."""

    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.session_id = "sid-locked"
        self._meeting_agent = None
        self.queued: list = []

    def queue_message(self, item):
        self.queued.append(item)
        return 0


class TestTheLockOnEveryDrivePath:
    def test_the_gate_answers_with_the_lock_and_lets_the_owner_through(self, temp_db, monkeypatch):
        slug = make_test_agent()
        cid = _make_chat(slug, user_sub="user-ann")

        async def go():
            bob = _locked_connection(monkeypatch, cid, slug)
            assert await bob._deny_task_continue(cid) == _LOCK
            assert bob.sent == [{"type": "error", "message": _LOCK}]
            assert await bob._deny_task_continue(cid, quiet=True) == _LOCK
            assert len(bob.sent) == 1                     # quiet sends nothing
            ann = _locked_connection(monkeypatch, cid, slug, user_sub="user-ann")
            assert await ann._deny_task_continue(cid) == "" and ann.sent == []
        asyncio.run(go())

    def test_a_prompt_answer_and_a_pick_are_refused(self, temp_db, monkeypatch):
        from storage import database as task_store
        slug = make_test_agent()
        cid = _make_chat(slug, user_sub="user-ann")
        task_store.upsert_user("user-bob", "bob@test.com", "Bob", "member")
        task_store.add_user_agent("user-bob", slug, "editor", "test")

        async def go():
            bob = _locked_connection(monkeypatch, cid, slug)
            assert await bob._may_resolve_permission("req-unknown") is False
            assert {"type": "error", "message": _LOCK} in bob.sent
            bob.sent.clear()
            await bob._handle_mode_change({"type": "mode_change", "mode": "dontAsk",
                                           "chat_id": cid})
            # The refused pick answers with the chat's stored mode.
            assert task_store.get_chat(cid)["permission_mode"] in ("default", "ask")
            assert any(f.get("type") == "mode_changed" for f in bob.sent), bob.sent
        asyncio.run(go())

    def test_the_mid_stream_actions_and_cancel_queued_are_refused(self, temp_db, monkeypatch):
        from ws.dashboard_dispatch import StreamCtx
        slug = make_test_agent()
        cid = _make_chat(slug, user_sub="user-ann")

        async def go():
            bob = _locked_connection(monkeypatch, cid, slug)
            pump = _Pump(cid)
            stream = StreamCtx(pump, asyncio.Queue(), cid)
            await bob._on_artifact_interaction(
                {"type": "artifact_interaction", "chat_id": cid, "token": "t-1",
                 "title": "x", "payload": {}}, stream=stream)
            await bob._on_app_action(
                {"type": "app_action", "chat_id": cid, "app_id": "a-1", "action_id": "go"},
                stream=stream)
            await bob._on_cancel_queued({"type": "cancel_queued", "queue_id": "q-1"},
                                        stream=stream)
            acks = [f for f in bob.sent if f.get("type") in ("artifact_ack", "app_action_ack")]
            assert [a["status"] for a in acks] == ["denied", "denied"]
            assert all(a["reason"] == _LOCK for a in acks)
            assert {"type": "error", "message": _LOCK} in bob.sent    # cancel_queued
            assert pump.queued == []
        asyncio.run(go())


# -- the new chat's place ---------------------------------------------------------

def _online(monkeypatch, *machine_ids):
    from services.remote import remote_status
    up = set(machine_ids)

    def _live(mid, *, machine=None):
        ok = mid in up
        return {"state": "online" if ok else "disconnected", "last_heartbeat_age_s": 1,
                "last_seen_iso": "", "reachable": ok}
    monkeypatch.setattr(remote_status, "get_live_machine_status", _live)
    monkeypatch.setattr(remote_status, "is_reachable", lambda mid: mid in up)


class TestTheNewChatsPlace:
    def test_a_new_chat_starts_on_the_place_its_page_picked(self, temp_db, monkeypatch):
        """The person's default is their own computer; the page picked the
        platform before the first message: the chat is minted pinned there,
        the placement is recorded as theirs, and the first spawn runs there."""
        from services.remote import remote_status
        from storage import database as task_store, move_schema, remote_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        _insert_machine("m-def", "home-pc", scope="user", owner="user-admin")
        _online(monkeypatch, "m-def")
        monkeypatch.setattr(remote_status, "resolve_execution_target",
                            lambda *a, **k: ("m-def", None))

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "warmup", "agent": slug, "target": "local",
                                "text": "hello"})
                started = await _drain_until(ws, "warmup_started")
                await _drain_until(ws, "warmup_ready")
                return started["chat_id"]
        cid = asyncio.run(asyncio.wait_for(scenario(), 15))
        assert task_store.get_chat(cid)["execution_target"] == "local"
        placed = remote_store.latest_chat_move(cid)
        assert (placed["why"], placed["state"], placed["memory"], placed["origin"]) == (
            move_schema.WHY_PLACED, move_schema.SETTLED, move_schema.MEMORY_SUMMARY,
            move_schema.ORIGIN_CHIP)
        assert (placed["source"], placed["target"], placed["actor"]) == (
            "m-def", "local", "user-admin")
        assert layer.started and layer.started[-1][1].execution_target == "local"

    def test_a_pick_the_menu_does_not_offer_mints_nothing(self, temp_db, monkeypatch):
        from storage import database as task_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        _insert_machine("m-bob", "bob-pc", scope="user", owner="user-bob")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "warmup", "agent": slug, "target": "m-bob",
                                "text": "hello"})
                return await _drain_until(ws, "warmup_failed")
        failed = asyncio.run(asyncio.wait_for(scenario(), 15))
        assert failed == {"type": "warmup_failed", "error": "You can't start this chat there.",
                          "reason": "target_unavailable"}
        assert task_store.list_chats("user-admin", slug) == []
        assert layer.started == []

    def test_a_pick_by_name_or_of_the_default_is_no_pick(self, temp_db, monkeypatch):
        from ws import dashboard_chat_move
        _insert_machine("m-def", "home-pc", scope="user", owner="user-admin")
        _online(monkeypatch, "m-def")
        from services.remote import remote_status
        monkeypatch.setattr(remote_status, "resolve_execution_target",
                            lambda *a, **k: ("m-def", None))
        conn = type("C", (), {"user_sub": "user-admin",
                              "user": {"role": "admin", "username": "admin"}})()
        slug = make_test_agent()

        def place(wanted):
            return asyncio.run(dashboard_chat_move.new_chat_place(conn, slug, wanted,
                                                                  "claude-code-cli"))
        assert place("m-def") == ("", "home-pc", "m-def", "")         # the default
        assert place("home-pc")[3] == "You can't start this chat there."   # ids only
        assert place("local") == ("local", "OtoDock platform", "m-def", "")
        assert place("") == ("", "", "", "")
