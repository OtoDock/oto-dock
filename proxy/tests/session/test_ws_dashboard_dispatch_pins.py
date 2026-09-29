"""Characterization pins for the dispatcher paths no other golden-master
module drove before core-seams phase 6 folded the two client-message chains
into one table: ``plan_review_response`` in both loops (reject, implement
with accepted edits, implement in default mode), ``cancel_all_queued``
mid-stream, and ``compact_context`` (the refusal without a session, the
frames and the row with one).

Written against the two chains BEFORE the fold and kept as they were: an
assertion change here is a behaviour change and must be stated in the
plan. Two asymmetries are pinned on purpose, not fixed — the mid-stream
``implement_default`` queues no control request where the between-turns
path changes the mode through the layer, and the plan-review tail's
``mode_changed`` frame carries no chat tag in either loop.
"""

import asyncio
import uuid

from tests.fixtures.ws_dashboard_harness import (
    ANY,
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

PLAN_PATH = "/home/u/.claude/plans/plan-pin.md"
PLAN_FILE = "plan-pin.md"


def _live_state(chat_id: str, sid: str) -> dict:
    return {
        "type": "live_state", "chat_id": chat_id,
        "streaming": True, "session_id": sid, "started_at": ANY,
        "live_blocks": [], "active_tools": [],
        "active_agents": [], "active_delegates": [],
        "active_commands": [], "pending_permission": None,
        "thinking_active": False, "thinking_text": "",
        "thinking_tokens": 0, "todos": [], "goal": None, "meeting_agent": None,
        "meeting_participants": [], "workflows": {},
    }


async def _turn_end(ws, chat_id: str) -> None:
    await ws.expect({"type": "done", "chat_id": chat_id})
    await ws.expect({"type": "chat_status", "chat_id": chat_id, "status": "ready"})
    await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                     "title": "WS Dash Test finished", "body": "Response ready"})


def _plan_review_item(request_id: str) -> dict:
    return {
        "event_type": "plan_review", "request_id": request_id,
        "plan": "# The plan", "tool_input": {"planFilePath": PLAN_PATH},
    }


class TestPlanReviewMidStream:
    """The held turn: the review card arrives while the turn streams and the
    answer resolves the pump's active permission."""

    def _scenario(self, temp_db, monkeypatch, action: str):
        from core.events.common_events import CommonEvent, TEXT, DONE
        from core.session.session_state import get_permission_queue

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        request_id = f"req-{uuid.uuid4().hex[:8]}"
        seen: dict = {}

        async def scenario():
            answered = asyncio.Event()

            async def review_turn(sid):
                yield CommonEvent(type=TEXT, data={"content": "here is a plan"})
                get_permission_queue(sid).put_nowait(_plan_review_item(request_id))
                await answered.wait()
                yield CommonEvent(type=TEXT, data={"content": "noted"})
                yield CommonEvent(type=DONE, data={})

            async def implement_turn(sid):
                yield CommonEvent(type=TEXT, data={"content": "implementing"})
                yield CommonEvent(type=DONE, data={})

            def script(sid, prompt):
                if prompt.startswith("Please implement"):
                    return implement_turn(sid)
                return review_turn(sid)
            layer.turn_events = script

            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)
                seen["chat_id"], seen["sid"] = chat_id, sid

                ws.client_send({"type": "chat", "text": "plan it", "chat_id": chat_id})
                await ws.expect({"type": "title_updated", "chat_id": chat_id, "title": "plan it"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id, "status": "streaming"})
                await ws.expect(_live_state(chat_id, sid))
                await ws.expect({"type": "text", "content": "here is a plan", "chat_id": chat_id})
                await ws.expect({
                    "type": "plan_review", "request_id": request_id,
                    "plan": "# The plan", "tool_input": {"planFilePath": PLAN_PATH},
                    "filename": PLAN_FILE, "chat_id": chat_id,
                })

                ws.client_send({"type": "plan_review_response", "request_id": request_id,
                                "action": action, "filename": PLAN_FILE})
                # The plan-review tail's mode_changed frame carries NO chat tag
                # in the streaming loop (pinned: a tagged one would be dropped
                # by a tab viewing another chat).
                mode = {"reject": "default", "implement_accept_edits": "acceptEdits",
                        "implement_default": "default"}[action]
                await ws.expect({"type": "mode_changed", "mode": mode})
                await asyncio.sleep(0.2)
                answered.set()

                await ws.expect({"type": "text", "content": "noted", "chat_id": chat_id})
                if action == "reject":
                    await _turn_end(ws, chat_id)
                else:
                    # The queued implement prompt drains as the next turn.
                    await ws.expect({"type": "queue_sent",
                                     "text": "Please implement the plan now.", "chat_id": chat_id})
                    await ws.expect({"type": "text", "content": "implementing", "chat_id": chat_id})
                    await ws.expect({"type": "done", "chat_id": chat_id})
                    # The loop's tail stamps the plan implemented before the
                    # pump's queued "ready" broadcast drains.
                    await ws.expect({"type": "plan_status", "filename": PLAN_FILE,
                                     "status": "implemented"})
                    await ws.expect({"type": "chat_status", "chat_id": chat_id, "status": "ready"})
                    await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                     "title": "WS Dash Test finished", "body": "Response ready"})
                await sync_dispatch(ws)
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)
        return layer, seen

    def test_reject_restores_the_pre_plan_mode(self, temp_db, monkeypatch):
        layer, seen = self._scenario(temp_db, monkeypatch, "reject")
        chat = temp_db.get_chat(seen["chat_id"])
        assert chat["permission_mode"] == "default"
        plans = temp_db.get_chat_plans(seen["chat_id"])
        assert [(p["filename"], p["status"]) for p in plans] == [(PLAN_FILE, "rejected")]
        assert layer.control_requests == []
        assert layer.mode_changes == []

    def test_implement_with_accepted_edits_queues_the_control_request(self, temp_db, monkeypatch):
        layer, seen = self._scenario(temp_db, monkeypatch, "implement_accept_edits")
        chat = temp_db.get_chat(seen["chat_id"])
        assert chat["permission_mode"] == "acceptEdits"
        plans = temp_db.get_chat_plans(seen["chat_id"])
        assert [(p["filename"], p["status"]) for p in plans] == [(PLAN_FILE, "implemented")]
        # The mode reaches the engine as a control request flushed after the turn.
        assert (seen["sid"], "set_permission_mode", {"mode": "acceptEdits"}) in layer.control_requests

    def test_implement_in_default_mode_queues_no_control_request(self, temp_db, monkeypatch):
        # The pinned asymmetry: the streaming loop's implement_default writes
        # the row and the frame but tells the engine nothing (the between-turns
        # path goes through _handle_mode_change and does).
        layer, seen = self._scenario(temp_db, monkeypatch, "implement_default")
        chat = temp_db.get_chat(seen["chat_id"])
        assert chat["permission_mode"] == "default"
        assert layer.control_requests == []
        assert layer.mode_changes == []


class TestPlanReviewBetweenTurns:
    """The review card outlived its turn (the CLI blocked, the turn ended):
    the answer arrives between turns and the dispatcher resolves it."""

    def _scenario(self, temp_db, monkeypatch, action: str):
        from core.events.common_events import CommonEvent, TEXT, DONE
        from core.session.session_state import get_permission_queue

        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        request_id = f"req-{uuid.uuid4().hex[:8]}"
        seen: dict = {}

        async def scenario():
            async def review_turn(sid):
                yield CommonEvent(type=TEXT, data={"content": "here is a plan"})
                get_permission_queue(sid).put_nowait(_plan_review_item(request_id))
                await asyncio.sleep(0.05)
                yield CommonEvent(type=DONE, data={})
            layer.turn_events = lambda sid, prompt: review_turn(sid)

            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)
                seen["chat_id"], seen["sid"] = chat_id, sid

                ws.client_send({"type": "chat", "text": "plan it", "chat_id": chat_id})
                await ws.expect({"type": "title_updated", "chat_id": chat_id, "title": "plan it"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id, "status": "streaming"})
                await ws.expect(_live_state(chat_id, sid))
                await ws.expect({"type": "text", "content": "here is a plan", "chat_id": chat_id})
                await ws.expect({
                    "type": "plan_review", "request_id": request_id,
                    "plan": "# The plan", "tool_input": {"planFilePath": PLAN_PATH},
                    "filename": PLAN_FILE, "chat_id": chat_id,
                })
                await _turn_end(ws, chat_id)

                ws.client_send({"type": "plan_review_response", "request_id": request_id,
                                "action": action, "filename": PLAN_FILE})
                mode = {"reject": "default", "implement_accept_edits": "acceptEdits",
                        "implement_default": "default"}[action]
                await ws.expect({"type": "mode_changed", "mode": mode})
                await sync_dispatch(ws)
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)
        return layer, seen

    def test_reject_between_turns(self, temp_db, monkeypatch):
        layer, seen = self._scenario(temp_db, monkeypatch, "reject")
        chat = temp_db.get_chat(seen["chat_id"])
        assert chat["permission_mode"] == "default"
        plans = temp_db.get_chat_plans(seen["chat_id"])
        assert [(p["filename"], p["status"]) for p in plans] == [(PLAN_FILE, "rejected")]
        assert layer.mode_changes == []

    def test_implement_with_accepted_edits_changes_the_mode_through_the_layer(self, temp_db, monkeypatch):
        layer, seen = self._scenario(temp_db, monkeypatch, "implement_accept_edits")
        chat = temp_db.get_chat(seen["chat_id"])
        assert chat["permission_mode"] == "acceptEdits"
        assert layer.mode_changes == [(seen["sid"], "acceptEdits")]
        assert layer.control_requests == []

    def test_implement_in_default_mode_changes_the_mode_through_the_layer(self, temp_db, monkeypatch):
        layer, seen = self._scenario(temp_db, monkeypatch, "implement_default")
        chat = temp_db.get_chat(seen["chat_id"])
        assert chat["permission_mode"] == "default"
        assert layer.mode_changes == [(seen["sid"], "default")]


class TestCancelAllQueuedMidStream:
    def test_clears_the_pump_queue_with_a_tagged_frame(self, temp_db, monkeypatch):
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
            layer.turn_events = lambda sid, prompt: first_turn()

            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)
                ws.client_send({"type": "chat", "text": "long job", "chat_id": chat_id})
                await ws.expect({"type": "title_updated", "chat_id": chat_id, "title": "long job"})
                await ws.expect({"type": "chat_status", "chat_id": chat_id, "status": "streaming"})
                await ws.expect(_live_state(chat_id, sid))
                await ws.expect({"type": "text", "content": "working…", "chat_id": chat_id})

                ws.client_send({"type": "chat", "text": "queued A"})
                await ws.expect({"type": "queued", "index": 0, "text": "queued A", "chat_id": chat_id})
                ws.client_send({"type": "chat", "text": "queued B"})
                await ws.expect({"type": "queued", "index": 1, "text": "queued B", "chat_id": chat_id})
                ws.client_send({"type": "cancel_all_queued"})
                await ws.expect({"type": "queue_cleared", "text": "queued A\n\nqueued B",
                                 "chat_id": chat_id})
                hold.set()
                await _turn_end(ws, chat_id)
                # Nothing drained: the queue was cleared.
                assert [m[1] for m in layer.messages] == ["long job"]
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)


class TestCompactContext:
    def test_refused_without_a_session(self, temp_db, monkeypatch):
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                ws.client_send({"type": "compact_context"})
                await ws.expect({"type": "error",
                                 "message": "No active session — send a message first, then compact."})
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)

    def test_frames_and_the_persisted_row(self, temp_db, monkeypatch):
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")

        async def compact(sid):
            return {"post_tokens": 1234}
        layer.compact = compact
        seen: dict = {}

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)
                seen["chat_id"] = chat_id
                ws.client_send({"type": "compact_context"})
                await ws.expect({"type": "context_compact", "phase": "started",
                                 "trigger": "manual", "chat_id": chat_id})
                await ws.expect({"type": "context_compact", "phase": "completed",
                                 "trigger": "manual", "post_tokens": 1234, "chat_id": chat_id})
                await sync_dispatch(ws)
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)

        rows = temp_db.get_chat_messages(seen["chat_id"])
        events = [r for r in rows if r["role"] == "event"]
        assert len(events) == 1
        import json
        assert json.loads(events[0]["event_data"]) == {
            "type": "context_compact", "phase": "completed", "trigger": "manual",
            "post_tokens": 1234,
        }
        # Core-seams phase 6 (D9a): the manual compaction's row carries the
        # kind the pump writes for the same block (it wrote none before).
        assert events[0]["event_type"] == "context_compact"
        assert temp_db.get_chat(seen["chat_id"])["context_used"] == 1234
