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

import pytest

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
    answer resolves the pump's active permission. ``waits``: the turn waits
    on the answer as Claude's ExitPlanMode hook does, and goes on to
    implement in the same turn; else nobody waits (a Codex card answered
    before its turn's end) and the implement message runs behind it."""

    def _scenario(self, temp_db, monkeypatch, action: str, *, waits: bool = False):
        from core.events.common_events import DONE, TEXT, CommonEvent
        from core.session.session_state import get_permission_queue, wait_for_permission

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
                waiter = (asyncio.create_task(wait_for_permission(request_id, sid, timeout=30))
                          if waits else None)
                get_permission_queue(sid).put_nowait(_plan_review_item(request_id))
                if waiter is not None:
                    seen["approved"] = await waiter
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
                # The answer's mode frame names its chat (a tab viewing
                # another chat ignores it).
                mode = {"reject": "ask", "implement_auto": "autoReview",
                        "implement_ask": "ask"}[action]
                await ws.expect({"type": "mode_changed", "mode": mode, "chat_id": chat_id})
                if waits:
                    # The answer took the wait: the card closes on every viewer.
                    await ws.expect({"type": "permission_resolved", "request_id": request_id,
                                     "action": action, "chat_id": chat_id})
                await asyncio.sleep(0.2)
                answered.set()

                await ws.expect({"type": "text", "content": "noted", "chat_id": chat_id})
                if action == "reject":
                    await _turn_end(ws, chat_id)
                elif waits:
                    # The turn that waited implements: no implement message,
                    # the plan marked when that turn ends.
                    await ws.expect({"type": "done", "chat_id": chat_id})
                    await ws.expect({"type": "plan_status", "filename": PLAN_FILE,
                                     "status": "implemented"})
                    await ws.expect({"type": "chat_status", "chat_id": chat_id, "status": "ready"})
                    await ws.expect({"type": "turn_complete", "chat_id": chat_id,
                                     "title": "WS Dash Test finished", "body": "Response ready"})
                else:
                    # The queued implement prompt drains as the next turn.
                    await ws.expect({"type": "queue_sent", "queue_ids": [],
                                     "message_ids": ANY,
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

    @pytest.mark.parametrize("waits", [False, True])
    def test_reject_restores_the_pre_plan_mode(self, temp_db, monkeypatch, waits):
        # Cancel denies a waiting ExitPlanMode (the engine stays in Plan for
        # the rest of the turn) and the engine's own mode follows after it.
        layer, seen = self._scenario(temp_db, monkeypatch, "reject", waits=waits)
        assert seen.get("approved", False) is False
        chat = temp_db.get_chat(seen["chat_id"])
        assert chat["permission_mode"] == "ask"
        plans = temp_db.get_chat_plans(seen["chat_id"])
        assert [(p["filename"], p["status"]) for p in plans] == [(PLAN_FILE, "rejected")]
        assert layer.control_requests == [(seen["sid"], "set_permission_mode", {"mode": "ask"})]
        assert layer.mode_changes == []

    @pytest.mark.parametrize("waits", [False, True])
    @pytest.mark.parametrize("action,mode", [("implement_auto", "autoReview"), ("implement_ask", "ask")])
    def test_implement_queues_the_control_request(self, temp_db, monkeypatch, action, mode, waits):
        # Both answers tell the engine its mode after the turn (the CLI's
        # own word for Auto is its classifier mode), whether the turn
        # implements itself or the implement message does.
        layer, seen = self._scenario(temp_db, monkeypatch, action, waits=waits)
        assert seen.get("approved", True) is True
        chat = temp_db.get_chat(seen["chat_id"])
        assert chat["permission_mode"] == mode
        plans = temp_db.get_chat_plans(seen["chat_id"])
        assert [(p["filename"], p["status"]) for p in plans] == [(PLAN_FILE, "implemented")]
        assert (seen["sid"], "set_permission_mode", {"mode": mode}) in layer.control_requests
        assert layer.mode_changes == []


class TestPlanReviewBetweenTurns:
    """The review card outlived its turn (a Codex plan, or the CLI blocked
    and the turn ended): the answer arrives between turns and acts while the
    plan row is pending. An implement starts the implement turn itself."""

    def _scenario(self, temp_db, monkeypatch, action: str, *, answered_before: bool = False):
        from core.events.common_events import DONE, TEXT, CommonEvent
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
                await _turn_end(ws, chat_id)
                if answered_before:
                    # Another tab answered it: the row is no longer pending.
                    await asyncio.to_thread(temp_db.update_chat_plan_status,
                                            chat_id, PLAN_FILE, "rejected")

                ws.client_send({"type": "plan_review_response", "request_id": request_id,
                                "action": action, "filename": PLAN_FILE})
                seen["frames"] = []
                if answered_before:
                    await sync_dispatch(ws)
                    ws.client_send({"type": "close"})
                    ws.no_more_frames()
                    return
                # The card closes on every viewer through the chat's
                # broadcast, which lands beside the answer's own frames.
                frames = []
                last = "turn_complete" if action != "reject" else "mode_changed"
                while not frames or frames[-1]["type"] != last:
                    frames.append(await ws.next_frame())
                if not any(f["type"] == "permission_resolved" for f in frames):
                    frames.append(await ws.next_frame())
                seen["frames"] = frames
                await sync_dispatch(ws)
                ws.client_send({"type": "close"})
            ws.no_more_frames()
        run_ws_scenario(scenario)
        return layer, seen

    @staticmethod
    def _answer_frames(seen: dict, action: str) -> list:
        chat_id = seen["chat_id"]
        resolved = [f for f in seen["frames"] if f["type"] == "permission_resolved"]
        assert [(f["action"], f["chat_id"]) for f in resolved] == [(action, chat_id)]
        return [f for f in seen["frames"] if f["type"] != "permission_resolved"]

    def test_reject_between_turns(self, temp_db, monkeypatch):
        layer, seen = self._scenario(temp_db, monkeypatch, "reject")
        assert self._answer_frames(seen, "reject") == [
            {"type": "mode_changed", "mode": "ask", "chat_id": seen["chat_id"]}]
        chat = temp_db.get_chat(seen["chat_id"])
        assert chat["permission_mode"] == "ask"
        plans = temp_db.get_chat_plans(seen["chat_id"])
        assert [(p["filename"], p["status"]) for p in plans] == [(PLAN_FILE, "rejected")]
        # Back to the mode Plan was entered from, the engine too.
        assert layer.mode_changes == [(seen["sid"], "ask")]

    # ``implement_accept_edits``: the answer of a tab opened before 1.9.
    @pytest.mark.parametrize("action,answer,mode", [
        ("implement_auto", "implement_auto", "autoReview"),
        ("implement_accept_edits", "implement_auto", "autoReview"),
        ("implement_ask", "implement_ask", "ask"),
    ])
    def test_implement_changes_the_mode_and_starts_the_implement_turn(
            self, temp_db, monkeypatch, action, answer, mode):
        layer, seen = self._scenario(temp_db, monkeypatch, action)
        chat_id = seen["chat_id"]
        frames = self._answer_frames(seen, answer)
        assert [f["type"] for f in frames] == [
            "mode_changed", "chat_status", "live_state", "text", "done", "plan_status",
            "chat_status", "turn_complete"]
        assert frames[0] == {"type": "mode_changed", "mode": mode, "chat_id": chat_id}
        assert frames[3] == {"type": "text", "content": "implementing", "chat_id": chat_id}
        assert frames[5] == {"type": "plan_status", "filename": PLAN_FILE, "status": "implemented"}
        chat = temp_db.get_chat(chat_id)
        assert chat["permission_mode"] == mode
        assert layer.mode_changes == [(seen["sid"], mode)]
        assert layer.control_requests == []

    def test_an_answer_to_a_plan_no_longer_pending_changes_nothing(self, temp_db, monkeypatch):
        """The row is what a reload, a second tab and a restart read: an answer
        for a plan already implemented or rejected is dropped."""
        layer, seen = self._scenario(temp_db, monkeypatch, "implement_auto", answered_before=True)
        assert seen["frames"] == []
        assert temp_db.get_chat(seen["chat_id"])["permission_mode"] == "autoReview"
        assert layer.mode_changes == []


class TestImplementPlanMode:
    @pytest.mark.parametrize("path,picked,runs", [
        ("direct-llm", "autoReview", "ask"),          # Direct LLM offers no Auto
        ("claude-code-cli", "acceptEdits", "autoReview"),
    ])
    def test_the_picked_mode_is_entered_as_the_chats_engine_offers_it(
            self, temp_db, monkeypatch, path, picked, runs):
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        seen: dict = {}

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, _sid = await warm_new_chat(ws, layer, slug)
                await asyncio.to_thread(temp_db.update_chat, chat_id, execution_path=path)
                ws.client_send({"type": "implement_plan", "plan_path": "plans/p.md", "mode": picked})
                while True:
                    frame = await ws.next_frame()
                    if frame["type"] == "warmup_ready":
                        seen["ready"] = frame
                        break
                seen["chat_id"] = chat_id
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)
        assert seen["ready"]["mode"] == runs
        assert temp_db.get_chat(seen["chat_id"])["permission_mode"] == runs


class TestCancelAllQueuedMidStream:
    def test_clears_the_pump_queue_with_a_tagged_frame(self, temp_db, monkeypatch):
        from core.events.common_events import DONE, TEXT, CommonEvent

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

                ws.client_send({"type": "chat", "text": "queued A", "queue_id": "qa"})
                await ws.expect({"type": "queued", "index": 0, "queue_id": "qa",
                                 "text": "queued A", "author_sub": "user-admin",
                                 "chat_id": chat_id})
                ws.client_send({"type": "chat", "text": "queued B", "queue_id": "qb"})
                await ws.expect({"type": "queued", "index": 1, "queue_id": "qb",
                                 "text": "queued B", "author_sub": "user-admin",
                                 "chat_id": chat_id})
                # Edit: the person's own messages back into their composer.
                ws.client_send({"type": "cancel_all_queued"})
                await ws.expect({"type": "queue_cleared", "queue_ids": ["qa", "qb"],
                                 "reason": "cancel", "returned": True,
                                 "text": "queued A\n\nqueued B", "chat_id": chat_id})
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

    @pytest.mark.parametrize("answer, armed", [({"post_tokens": 99}, True), (None, False)])
    def test_a_completed_compaction_arms_the_memory_reminder(self, temp_db, monkeypatch, answer, armed):
        from services.memory import memory_nudge
        layer = FakeExecutionLayer()
        stub_dashboard_seams(monkeypatch, layer)
        slug = make_test_agent()
        set_username("user-admin", "admin")
        arms: list[str] = []
        monkeypatch.setattr(memory_nudge, "arm", arms.append)

        async def compact(sid):
            return answer
        layer.compact = compact
        seen: dict = {}

        async def scenario():
            async with dashboard_connection(session_cookie()) as ws:
                await drain_startup(ws)
                chat_id, sid = await warm_new_chat(ws, layer, slug)
                seen["sid"] = sid
                ws.client_send({"type": "compact_context"})
                await ws.expect({"type": "context_compact", "phase": "started",
                                 "trigger": "manual", "chat_id": chat_id})
                await ws.expect({"type": "context_compact", "phase": "completed", "trigger": "manual",
                                 "post_tokens": 99, "chat_id": chat_id} if armed else
                                {"type": "context_compact", "phase": "failed", "chat_id": chat_id})
                if not armed:
                    await ws.expect({"type": "error", "message": "Context compaction failed or is "
                                                                 "not supported by this engine."})
                await sync_dispatch(ws)
                ws.client_send({"type": "close"})
        run_ws_scenario(scenario)

        assert arms == ([seen["sid"]] if armed else [])


class _PlanConnection:
    """The prompt mixin with only what a plan review's answer between turns
    touches: the mode change and the chat turn recorded, the plan row
    pending, the row writes kept."""

    def __init__(self, *, session_id: str | None, chat_id: str = "chat-plan"):
        from ws.dashboard_prompts import PromptController

        class _Conn(PromptController):
            pass
        conn = _Conn.__new__(_Conn)
        conn.session_id = session_id
        conn.chat_id = chat_id
        conn.implementing_plan = ""
        conn.implement_queue = []
        conn.modes = []
        conn.chats = []

        async def _may_resolve_permission(rid):
            return True

        async def _handle_mode_change(msg):
            conn.modes.append(msg["mode"])

        async def _handle_chat(msg):
            conn.chats.append(msg["text"])

        async def _plan_row_pending(chat_id, filename):
            return True
        conn._may_resolve_permission = _may_resolve_permission
        conn._handle_mode_change = _handle_mode_change
        conn._handle_chat = _handle_chat
        conn._plan_row_pending = _plan_row_pending
        self.conn = conn


@pytest.fixture
def plan_rows(monkeypatch):
    from ws import dashboard_prompts
    rows: list = []

    class _Writer:
        @staticmethod
        def submit(chat_id, fn, *, label=""):
            rows.append((label, fn.args[1:]))
            fut = asyncio.get_running_loop().create_future()
            fut.set_result(None)
            return fut
    monkeypatch.setattr(dashboard_prompts, "chat_writer", _Writer)
    monkeypatch.setattr(dashboard_prompts, "broadcast_chat_frame", lambda chat_id, frame: None)
    return rows


class TestImplementBetweenTurns:
    """The implement message only when nothing waits on the answer."""

    def _answer(self, conn, rid: str):
        asyncio.run(conn._on_plan_review_response({
            "type": "plan_review_response", "request_id": rid,
            "action": "implement_auto", "filename": PLAN_FILE}))

    def test_a_dead_session_gets_no_implement_message(self, plan_rows):
        conn = _PlanConnection(session_id=None).conn
        self._answer(conn, f"req-{uuid.uuid4().hex[:8]}")
        assert conn.modes == ["autoReview"]
        assert conn.chats == [] and conn.implement_queue == []
        # No flag on the socket: it would follow the socket to another chat.
        assert conn.implementing_plan == ""
        assert plan_rows == [("plan_status", (PLAN_FILE, "implemented"))]

    def test_a_live_session_with_nothing_waiting_gets_it_as_the_persons_message(self, plan_rows):
        conn = _PlanConnection(session_id="sid-live").conn
        self._answer(conn, f"req-{uuid.uuid4().hex[:8]}")
        assert conn.chats == ["Please implement the plan now."]
        assert conn.implementing_plan == PLAN_FILE

    def test_a_waiting_call_answered_from_another_socket_implements_in_its_turn(
            self, plan_rows, monkeypatch):
        from core.events import stream_pump
        from core.session import session_state
        conn = _PlanConnection(session_id="sid-live").conn
        rid = f"req-{uuid.uuid4().hex[:8]}"

        class _Pump:
            is_done = False
            implementing_plan = ""

            def push_ws_event(self, frame):
                return True
        pump = _Pump()
        monkeypatch.setitem(stream_pump._active_pumps, conn.chat_id, pump)
        session_state.open_permission_wait(rid, "sid-live")
        try:
            self._answer(conn, rid)
        finally:
            session_state._permission_events.pop(rid, None)
            session_state._permission_decisions.pop(rid, None)
            session_state._permission_request_sessions.pop(rid, None)
            session_state._session_permission_requests.pop("sid-live", None)
        assert conn.chats == []
        assert pump.implementing_plan == PLAN_FILE
        assert conn.implementing_plan == ""
