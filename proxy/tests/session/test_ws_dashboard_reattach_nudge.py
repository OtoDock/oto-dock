"""Characterization suite for ``ws_dashboard_handler`` — part 4.

Pins the mid-turn re-attach (resume_chat while the chat streams → truncated
history + live_state replay), the bg-nudge server-driven review turn, and
the inline warmup reuse of an alive session. Completes the Step-1 minimum
flow list; same golden-master rules as parts 1-3.
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


class TestMidTurnReattach:
    def test_resume_chat_during_stream_reattaches(self, temp_db, monkeypatch):
        from core.events.common_events import CommonEvent, TEXT, DONE

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def scenario():
            hold = asyncio.Event()

            async def slow_turn():
                yield CommonEvent(type=TEXT, data={"content": "working…"})
                await hold.wait()
                yield CommonEvent(type=DONE, data={})

            layer.turn_events = lambda sid, prompt: slow_turn()

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

                # reload mid-turn: history is TRUNCATED at the pump cutoff
                # (the in-flight tail rides live_state), then re-attach.
                ws.client_send({"type": "resume_chat", "chat_id": chat_id})
                history = await ws.expect({
                    "type": "chat_history", "chat_id": chat_id, "agent": ANY,
                    "messages": ANY, "has_more": False,
                    "restore": {"todos": [], "meeting": None, "goal": None},
                    "plans": [], "total_cost": 0, "context_used": 0,
                    "context_max": 0, "cache_read": 0, "cache_write": 0,
                    "output_tokens": 0,
                    "execution_path": "claude-code-cli",
                    "execution_mode": "", "model": TEST_MODEL, "mode": "default",
                })
                assert [(m["role"], m["content"])
                        for m in history["messages"]] == [
                    ("user", "long job"),
                ]
                await ws.expect({
                    "type": "warmup_ready", "session_id": sid,
                    "chat_id": chat_id, "mode": "default",
                    "model": TEST_MODEL,
                    "execution_path": "claude-code-cli",
                })
                await ws.expect({"type": "queue_snapshot",
                                 "chat_id": chat_id, "messages": []})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                live = await ws.expect({
                    "type": "live_state", "chat_id": chat_id,
                    "streaming": True, "session_id": sid, "started_at": ANY,
                    "live_blocks": ANY, "active_tools": [],
                    "active_agents": [], "active_delegates": [],
                    "active_commands": [], "pending_permission": None,
                    "thinking_active": False, "thinking_text": "",
                    "thinking_tokens": 0, "todos": [], "goal": None, "meeting_agent": None,
                    "meeting_participants": [], "workflows": {},
                })
                assert live["live_blocks"] == [
                    {"type": "text", "content": "working…"},
                ]

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


class TestBgNudgeServerTurn:
    def test_bg_nudge_runs_review_turn_on_viewed_chat(self, temp_db,
                                                      monkeypatch):
        from core.events.common_events import CommonEvent, TEXT, DONE
        from core.session.session_state import _dashboard_notify_queues

        layer = FakeExecutionLayer()
        layer.turn_events = [
            CommonEvent(type=TEXT, data={"content": "reviewed"}),
            CommonEvent(type=DONE, data={}),
        ]
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        nudge_text = ("Your 2 background agent(s) have completed. "
                      "Please review the results and continue.")

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)

                # the bg monitor's transport: the session's registered
                # dashboard notify queue
                _dashboard_notify_queues[sid].put_nowait({
                    "type": "bg_nudge", "count": 2,
                    "chat_id": chat_id, "session_id": sid,
                })

                await ws.expect({"type": "bg_agents_complete", "count": 2})
                await ws.expect({"type": "server_turn_start",
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
                await ws.expect({"type": "text", "content": "reviewed",
                                 "chat_id": chat_id})
                await ws.expect({"type": "done", "chat_id": chat_id})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "streaming"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id,
                                 "status": "ready"})
                # NO turn_complete: this chat never recorded a turn origin
                # (no user send preceded the nudge), so the ephemeral ping
                # has no routing target.

                assert [p for _s, p, _k in layer.messages] == [nudge_text]
                msgs = temp_db.get_chat_messages(chat_id)
                assert [(m["role"], m["event_type"] or m["content"])
                        for m in msgs] == [
                    ("event", "bg_nudge"),
                    ("assistant", "reviewed"),
                ]
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)


class TestExistingChatWarmupInline:
    def test_alive_session_warmup_reuses_inline(self, temp_db, monkeypatch):
        from core.session.session_state import set_session_mode
        from storage import database as task_store

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        sid = str(uuid.uuid4())
        layer.alive.add(sid)
        set_session_mode(sid, "default")
        cid = str(uuid.uuid4())
        task_store.create_chat(cid, "user-admin", slug, "default",
                               model=TEST_MODEL,
                               execution_path="claude-code-cli")
        task_store.update_chat(cid, session_id=sid)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "warmup", "agent": slug,
                                "chat_id": cid})
                # inline reuse: warmup_ready BEFORE the dispatch's count frame
                await ws.expect({
                    "type": "warmup_ready", "session_id": sid,
                    "chat_id": cid, "mode": "default", "model": TEST_MODEL,
                    "execution_path": "claude-code-cli",
                    "interactive": False,
                })
                await ws.expect({"type": "notification_count", "count": 0})
                assert layer.started == []
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)
