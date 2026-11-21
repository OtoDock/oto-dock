"""A call on a route tied to a person writes in that person's own chat: the
dashboard's socket over it while the call holds it and after the hang-up (the
agent's conversation, read-only for good, is in
``test_ws_dashboard_resume_controls.py``'s ``TestPhoneChatReadOnly``)."""

from __future__ import annotations

import asyncio
import uuid

from tests.fixtures.ws_dashboard_harness import (
    FakeExecutionLayer,
    dashboard_connection,
    drain_startup,
    make_test_agent,
    run_ws_scenario,
    session_cookie,
    set_username,
    stub_dashboard_seams,
)


class TestPersonsOwnCallChat:
    """A call on a route tied to a person writes in that person's own chat:
    locked while the call holds it, theirs again when it ends, and the first
    text after it builds the chat's own session on the call's conversation."""

    def _chat(self, monkeypatch, *, held: bool):
        from core.events import input_queue
        from core.session import session_kind
        from services.phone import call_chats
        from services.scheduler import shared
        from storage.identity import db_users
        input_queue._registry.clear()
        call_chats._held.clear()
        monkeypatch.setattr(shared, "_shutting_down", False)
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-manager", "mgr")
        db_users.set_user_agents("user-manager", [slug], "user-admin",
                                 agent_roles={slug: "manager"})
        sid, cid = str(uuid.uuid4()), str(uuid.uuid4())
        # The row exactly as the call makes it, then the call's two turns.
        from services.phone.phone_identity import RouteIdentity
        from storage import database as task_store
        from ws.phone import _create_call_chat
        _create_call_chat(cid, slug, RouteIdentity(
            mode="user", role="external", external=None,
            user={"sub": "user-manager", "username": "mgr"}, user_role="manager"),
            execution_path="claude-code-cli", session_id=sid, identity_id="", call_type="inbound")
        for role, content in (("user", "is the gate shut"), ("assistant", "It is shut.")):
            task_store.add_chat_message(cid, role, content, author_sub="user-manager")
        assert session_kind.of_chat(task_store.get_chat(cid)) is session_kind.PHONE
        if held:
            call_chats.hold(cid, sid)
            layer.alive.add(sid)
        else:
            layer.resumable.add(sid)
        return layer, slug, cid, sid

    @staticmethod
    def _owner():
        return session_cookie(sub="user-manager", email="manager@test.com",
                              name="Manager User", role="creator")

    def test_the_owner_reads_it_locked_while_the_call_writes(self, temp_db, monkeypatch):
        from core.session.visibility import CALL_IN_PROGRESS
        layer, _slug, cid, _sid = self._chat(monkeypatch, held=True)

        async def scenario():
            async with dashboard_connection(self._owner()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                await ws.expect({"type": "chat_view_only", "chat_id": cid, "kind": "phone",
                                 "lock": CALL_IN_PROGRESS})
                frame = await ws.next_frame()
                assert (frame["type"], frame["chat_id"]) == ("chat_history", cid), frame
                await asyncio.sleep(0.2)
                ws.no_more_frames()
                ws.client_send({"type": "chat", "text": "one more thing", "chat_id": cid})
                await ws.expect({"type": "error", "message": CALL_IN_PROGRESS})
                await asyncio.sleep(0.2)
                assert layer.messages == [] and layer.started == []
        run_ws_scenario(scenario)

    def test_after_the_call_the_first_text_builds_the_chats_own_session(self, temp_db, monkeypatch):
        from core.events.common_events import DONE, TEXT, CommonEvent
        layer, slug, cid, sid = self._chat(monkeypatch, held=False)
        layer.turn_events = [CommonEvent(type=TEXT, data={"content": "Done."}),
                             CommonEvent(type=DONE, data={})]
        seen: dict = {}

        async def scenario():
            async with dashboard_connection(self._owner()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "resume_chat", "chat_id": cid})
                frame = await ws.next_frame()
                assert (frame["type"], frame["chat_id"]) == ("chat_history", cid), frame
                ready = await ws.next_frame()
                assert (ready["type"], ready["needs_warmup"]) == ("warmup_ready", True), ready
                seen["ready_mode"] = ready.get("mode")
                ws.client_send({"type": "warmup", "agent": slug, "chat_id": cid,
                                "text": "and the back door?"})
                while (await ws.next_frame(10))["type"] != "turn_complete":
                    pass
        run_ws_scenario(scenario, timeout=20)
        # The call's conversation goes on in the chat's own session: the same
        # id resumed, built as a chat in the mode a new chat of that person
        # starts in (Auto), never the call's unattended one.
        assert [s for s, _cfg in layer.started] == [sid]
        cfg = layer.started[0][1]
        assert (cfg.client_type, cfg.resume, cfg.permission_mode) == ("dashboard", True, "autoReview")
        assert seen["ready_mode"] == "autoReview"
        assert [s for s, _text, _kw in layer.messages] == [sid]
        assert "and the back door?" in layer.messages[0][1]
