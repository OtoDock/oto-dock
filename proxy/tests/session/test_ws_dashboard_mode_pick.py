"""A mode or model picked on a chat with no live session is kept.

The pick reaches the chat's row at once in every branch (a live session,
one that is away, none at all), so the next start, on another socket too
and as a terminal, runs in it; a session that was away is owed the mode
once; a task chat keeps its mode while its run is active.
"""

import asyncio
import uuid

from tests.fixtures.ws_dashboard_harness import (
    TEST_MODEL,
    FakeExecutionLayer,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    stub_dashboard_seams,
    sync_dispatch,
)
from tests.session.test_ws_dashboard_resume_controls import (
    _bare_connection,
    _frames_until_pong,
    _make_chat,
)


class TestAPickWithNoSession:
    def test_a_pick_on_a_chat_with_no_session_is_stored_at_once(self, temp_db, monkeypatch):
        """A chat whose session (or terminal) closed: the pick reaches the row
        at once, so the next start, on another socket too and as a terminal,
        runs in it, never in the row's earlier mode."""
        from core.permission_modes import terminal_mode
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        cid = _make_chat(slug, messages=(("user", "hi"), ("assistant", "hello")))

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _frames_until_pong(ws)
                ws.client_send({"type": "mode_change", "mode": "autoReview", "chat_id": cid})
                await ws.expect({"type": "mode_changed", "mode": "autoReview", "chat_id": cid})
                await sync_dispatch(ws)
                assert temp_db.get_chat(cid)["permission_mode"] == "autoReview"
            # Another socket (the page reconnected) warms the chat as a
            # terminal; the frame's stale word loses to the row.
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "warmup", "agent": slug, "chat_id": cid,
                                "permission_mode": "ask", "execution_mode": "interactive"})
                while (await ws.next_frame())["type"] != "warmup_ready":
                    pass
        run_ws_scenario(scenario)
        cfg = layer.started[-1][1]
        assert cfg.permission_mode == "autoReview"
        assert terminal_mode(cfg.permission_mode) == "auto"

    def test_a_pick_while_the_session_is_away_owes_the_engine_its_mode(self, temp_db,
                                                                       monkeypatch):
        from ws import dashboard_chat_support as support
        slug = make_test_agent()
        cid = _make_chat(slug, session_id="sid-away")
        layer = FakeExecutionLayer()    # sid-away is not alive: its machine is away

        async def scenario():
            conn = _bare_connection(cid, "sid-away", user_sub="user-admin", agent=slug,
                                    layer=layer)
            await conn._handle_mode_change({"mode": "autoReview", "chat_id": cid})
            return conn
        conn = asyncio.run(scenario())
        assert temp_db.get_chat(cid)["permission_mode"] == "autoReview"
        assert conn.deferred_mode == "autoReview" and conn.deferred_for == cid
        assert support._engine_mode_owed.get("sid-away") == "autoReview"

        # The session answers again; whichever socket starts its next turn
        # gives its engine the mode, once.
        layer.alive.add("sid-away")
        asyncio.run(support.apply_owed_engine_mode(layer, "sid-away"))
        asyncio.run(support.apply_owed_engine_mode(layer, "sid-away"))
        assert layer.mode_changes == [("sid-away", "autoReview")]

    def test_a_live_pick_after_one_made_while_away_holds_at_the_next_turn(self, temp_db,
                                                                         monkeypatch):
        """Auto picked while the machine reconnects, then Ask once it is back,
        then a message: the turn's start re-applies nothing, so the row, the
        hook and the engine read Ask (for this socket and for another one)."""
        from core.session.session_state import get_session_mode
        from ws import dashboard_chat_support as support
        slug = make_test_agent()
        cid = _make_chat(slug, session_id="sid-back")
        layer = FakeExecutionLayer()

        async def scenario():
            conn = _bare_connection(cid, "sid-back", user_sub="user-admin", agent=slug,
                                    layer=layer)
            conn.deferred_model, conn.deferred_for = "", ""
            await conn._handle_mode_change({"mode": "autoReview", "chat_id": cid})
            layer.alive.add("sid-back")             # the machine is back
            await conn._handle_mode_change({"mode": "ask", "chat_id": cid})
            # The next message: both doors a turn start uses.
            await conn._reapply_deferred_after_warmup(cid)
            await support.apply_owed_engine_mode(layer, "sid-back")
            return conn
        conn = asyncio.run(scenario())
        assert temp_db.get_chat(cid)["permission_mode"] == "ask"
        assert get_session_mode("sid-back") == "ask"
        assert layer.mode_changes == [("sid-back", "ask")]
        assert not conn.deferred_mode and conn.deferred_for == ""
        assert "sid-back" not in support._engine_mode_owed

    def test_a_task_chat_keeps_its_mode_while_its_run_is_active(self, temp_db, monkeypatch):
        from storage import database as task_store
        from ws import dashboard as ws_dashboard
        slug = make_test_agent()
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        task_store.create_run(run_id, "dyn-x", slug, "manual", None, "go",
                              "one-time", "user", "user-admin")
        cid = f"task-{run_id}"
        task_store.create_chat(cid, "user-admin", slug, "auto", execution_path="claude-code-cli")

        async def _active(chat_id):
            return chat_id == cid
        monkeypatch.setattr(ws_dashboard, "task_run_active_async", _active)

        async def scenario(bound_to):
            conn = _bare_connection(bound_to, None, user_sub="user-admin", agent=slug)
            conn.user = {"role": "admin", "username": "admin"}
            conn.user_role = "admin"
            conn.user_agents, conn.agent_roles = [slug], {}
            await conn._handle_mode_change({"mode": "ask", "chat_id": cid})
            return conn.sent
        # The bound chat, and a frame naming it from a socket bound elsewhere.
        for bound_to in (cid, "chat-elsewhere"):
            sent = asyncio.run(scenario(bound_to))
            assert sent == [{"type": "mode_changed", "chat_id": cid, "mode": "auto"}], bound_to
            assert temp_db.get_chat(cid)["permission_mode"] == "auto"

    def test_a_mode_the_chats_engine_does_not_declare_is_refused_with_no_session(
            self, temp_db, monkeypatch):
        slug = make_test_agent()
        from storage import database as task_store
        cid = str(uuid.uuid4())
        task_store.create_chat(cid, "user-admin", slug, "ask", execution_path="direct-llm")

        async def scenario():
            conn = _bare_connection(cid, None, user_sub="user-admin", agent=slug)
            await conn._handle_mode_change({"mode": "autoReview", "chat_id": cid})
            return conn.sent
        assert asyncio.run(scenario()) == [{"type": "error", "message": "Invalid mode: autoReview"}]
        assert temp_db.get_chat(cid)["permission_mode"] == "ask"

    def test_a_model_pick_on_a_chat_with_no_session_is_stored_at_once(self, temp_db,
                                                                      monkeypatch):
        """The model pick has no gap of the mode pick's kind: it writes the
        row before it asks for a session, so a closed chat keeps it."""
        from storage.billing import subscription_store
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        monkeypatch.setattr(subscription_store, "list_models", lambda layer=None: [
            {"model_id": TEST_MODEL, "enabled": True}, {"model_id": "other-model", "enabled": True}])
        slug = make_test_agent()
        cid = _make_chat(slug, messages=(("user", "hi"), ("assistant", "hello")))

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await _frames_until_pong(ws)
                ws.client_send({"type": "model_change", "model": "other-model", "chat_id": cid})
                await ws.expect({"type": "model_changed", "model": "other-model"})
                await sync_dispatch(ws)
        run_ws_scenario(scenario)
        assert temp_db.get_chat(cid)["model"] == "other-model"
        assert layer.model_changes == []
