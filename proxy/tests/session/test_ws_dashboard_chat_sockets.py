"""The chat sockets' edges, through the REAL dashboard WS handler: the
frames that start no turn reach a streaming socket at once while the
server turns still wait, a card answered on one socket closes on the
others, a chat deleted mid-turn ends its turn before its row goes, a second
page's cold send during another page's warmup joins the chat instead of
warming it again (and the warmup's own first turn still runs headless when
its person left), and a refused queue-only send names its message so the
page keeps its warmup."""

import asyncio

import pytest
from fastapi import WebSocketDisconnect

import ws.dashboard  # noqa: F401  (resolves the dashboard↔dashboard_chat cycle)
from core.events import input_queue, stream_pump
from core.events.common_events import DONE, TEXT, CommonEvent, TurnInput
from storage import database as task_store
from tests.fixtures.ws_dashboard_harness import (
    FakeDashboardWebSocket,
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
from ws import wire_events as wire
from ws.dashboard import _connections_by_user


def _clear_module_state():
    """The queue registry, the delete marks, the prompt waits (a wait opened
    with no waiter would outlive its test) and the answered ids."""
    from core.session import session_state
    from ws import dashboard_prompts
    input_queue._registry.clear()
    input_queue._deleting.clear()
    for table in (session_state._permission_events, session_state._permission_decisions,
                  dashboard_prompts._recently_answered):
        table.clear()


@pytest.fixture(autouse=True)
def _fresh_queue(monkeypatch):
    _clear_module_state()
    from services.scheduler import shared
    monkeypatch.setattr(shared, "_shutting_down", False)
    monkeypatch.setattr(input_queue, "VIEWER_GRACE_S", 0.2)
    yield
    _clear_module_state()


async def _until(ws, frame_type: str, timeout: float = 5.0) -> dict:
    """The next frame of ``frame_type``; the frames before it are skipped."""
    while True:
        frame = await ws.next_frame(timeout)
        if frame.get("type") == frame_type:
            return frame


async def _collect_until(ws, frame_type: str, timeout: float = 5.0) -> list[dict]:
    """Every frame up to and including the first of ``frame_type``."""
    out = []
    while True:
        frame = await ws.next_frame(timeout)
        out.append(frame)
        if frame.get("type") == frame_type:
            return out


async def _wait_for(cond, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def _gated_turns(gate: asyncio.Event, calls: dict):
    """Turn 1 streams t1 then parks on ``gate``; later turns finish at once."""

    def turn(sid, prompt):
        calls["n"] += 1
        n = calls["n"]

        async def gen():
            yield CommonEvent(type=TEXT, data={"content": f"t{n}"})
            if n == 1:
                await gate.wait()
            yield CommonEvent(type=DONE, data={})
        return gen()
    return turn


def _setup(monkeypatch):
    gate = asyncio.Event()
    calls = {"n": 0}
    layer = FakeExecutionLayer()
    layer.turn_events = _gated_turns(gate, calls)
    stub_dashboard_seams(monkeypatch, layer)
    slug = make_test_agent()
    set_username("user-admin", "admin")
    return layer, gate, slug


async def _start(ws, layer, slug, text: str = "start") -> tuple[str, str]:
    chat_id, sid = await warm_new_chat(ws, layer, slug)
    ws.client_send({"type": "chat", "text": text, "chat_id": chat_id})
    await _until(ws, "live_state")
    await ws.expect({"type": "text", "content": "t1", "chat_id": chat_id})
    return chat_id, sid


def _conn(sub: str = "user-admin"):
    (conn,) = _connections_by_user[sub]
    return conn


# ---------------------------------------------------------------------------
# 1. The frames that start no turn reach the streaming socket at once
# ---------------------------------------------------------------------------

def test_a_notification_reaches_the_socket_during_its_turn(temp_db, monkeypatch):
    """The bell rings mid-turn: a notification queued on the connection
    during the viewed chat's turn goes out before ``done`` (the turn's own
    status frame, always first in the queue, is held)."""
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            conn = _conn()
            conn.notify_queue.put_nowait({"type": wire.NOTIFICATION, "delivery": {"id": 1}})
            conn.notify_queue.put_nowait({"type": wire.NOTIFICATION_COUNT, "count": 3})
            conn.notify_queue.put_nowait({"type": wire.SHARE_INBOX, "pending": 1})
            conn.notify_queue.put_nowait({"type": wire.TRANSFER_PROGRESS, "transfer_id": "t1",
                                          "pct": 5})
            conn.notify_queue.put_nowait({"type": wire.FILE_UPDATED, "agent": slug, "rel_path": "a"})
            conn.notify_queue.put_nowait({"type": wire.WARMUP_HEARTBEAT, "chat_id": "other-chat"})
            await ws.expect({"type": wire.NOTIFICATION, "delivery": {"id": 1}})
            await ws.expect({"type": wire.NOTIFICATION_COUNT, "count": 3})
            await ws.expect({"type": wire.SHARE_INBOX, "pending": 1})
            await ws.expect({"type": wire.TRANSFER_PROGRESS, "transfer_id": "t1", "pct": 5})
            await ws.expect({"type": wire.FILE_UPDATED, "agent": slug, "rel_path": "a"})
            await ws.expect({"type": wire.WARMUP_HEARTBEAT, "chat_id": "other-chat"})
            gate.set()
            await _until(ws, "done")
    run_ws_scenario(scenario)


def test_a_machine_that_came_or_went_reaches_the_socket_during_its_turn(temp_db, monkeypatch):
    """A machine's connect reaches a streaming socket before ``done``, and
    the page's ``target_check`` after it is answered inside the turn too:
    the banner does not wait for the turn's end."""
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            _conn().notify_queue.put_nowait({"type": wire.MACHINE_LINK, "machine_id": "m-1"})
            await ws.expect({"type": wire.MACHINE_LINK, "machine_id": "m-1"})
            ws.client_send({"type": wire.IN_TARGET_CHECK})
            await ws.expect({"type": wire.TARGET_MISMATCH, "chat_id": chat_id})
            gate.set()
            await _until(ws, "done")
    run_ws_scenario(scenario)


def test_a_server_turn_queued_mid_turn_waits_for_the_turn(temp_db, monkeypatch):
    """A nudge queued during the turn is not run inside it, and a
    notification queued BEHIND it still goes out at once; the nudge runs
    after ``done``, in its order."""
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await _start(ws, layer, slug)
            conn = _conn()
            conn.notify_queue.put_nowait({"type": wire.NOTIFY_BG_NUDGE, "count": 1,
                                          "chat_id": chat_id, "session_id": sid})
            conn.notify_queue.put_nowait({"type": wire.NOTIFICATION, "delivery": {"id": 2}})
            await ws.expect({"type": wire.NOTIFICATION, "delivery": {"id": 2}})
            gate.set()
            frames = await _collect_until(ws, "done")
            assert not any(f.get("type") == wire.SERVER_TURN_START for f in frames)
            await _until(ws, wire.SERVER_TURN_START)
            await _until(ws, "done")
            assert [p for _s, p, _k in layer.messages][-1].startswith("Your 1 background agent")
    run_ws_scenario(scenario)


def test_another_chats_status_moves_mid_turn_and_the_viewed_chats_waits(temp_db, monkeypatch):
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            conn = _conn()
            conn.notify_queue.put_nowait({"type": wire.CHAT_STATUS, "chat_id": chat_id,
                                          "status": "ready"})
            conn.notify_queue.put_nowait({"type": wire.CHAT_STATUS, "chat_id": "other-chat",
                                          "status": "ready"})
            conn.notify_queue.put_nowait({"type": wire.CHAT_READ, "chat_id": "other-chat"})
            await ws.expect({"type": wire.CHAT_STATUS, "chat_id": "other-chat", "status": "ready"})
            await ws.expect({"type": wire.CHAT_READ, "chat_id": "other-chat"})
            gate.set()
            frames = await _collect_until(ws, "done")
            assert not any(f.get("type") == wire.CHAT_STATUS and f.get("chat_id") == chat_id
                           and f.get("status") == "ready" for f in frames)
            # The held ready goes out after the turn, before the pump's own.
            await ws.expect({"type": wire.CHAT_STATUS, "chat_id": chat_id, "status": "ready"})
    run_ws_scenario(scenario)


def test_held_items_keep_their_order_after_the_turn(temp_db, monkeypatch):
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            conn = _conn()
            conn.notify_queue.put_nowait({"type": wire.GOAL_UPDATE, "chat_id": chat_id,
                                          "goal": {"n": "a"}})
            conn.notify_queue.put_nowait({"type": wire.NOTIFICATION, "delivery": {"id": 3}})
            conn.notify_queue.put_nowait({"type": wire.GOAL_UPDATE, "chat_id": chat_id,
                                          "goal": {"n": "b"}})
            await ws.expect({"type": wire.NOTIFICATION, "delivery": {"id": 3}})
            gate.set()
            await _until(ws, "done")
            goals = []
            while len(goals) < 2:
                frame = await ws.next_frame(5)
                if frame.get("type") == wire.GOAL_UPDATE:
                    goals.append(frame["goal"]["n"])
            assert goals == ["a", "b"]
    run_ws_scenario(scenario)


def test_held_frames_of_one_key_come_back_as_one(temp_db, monkeypatch):
    """A status or read receipt of the viewed chat read mid-turn replaces
    the held one of its key, as the queue would: a long turn replays one
    frame per key after ``done``, not every one it read."""
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            conn = _conn()
            for n in (1, 2, 3):
                conn.notify_queue.put_nowait({"type": wire.CHAT_READ, "chat_id": chat_id, "n": n})
            ws.client_send({"type": "ping"})
            await _until(ws, "pong")
            gate.set()
            await _until(ws, "done")
            # The held receipts go out right after the turn: one, the last.
            assert (await _until(ws, wire.CHAT_READ))["n"] == 3
            ws.client_send({"type": "ping"})
            while True:
                frame = await ws.next_frame(5)
                if frame["type"] == "pong":
                    break
                assert frame.get("type") != wire.CHAT_READ, frame
    run_ws_scenario(scenario)


def test_a_server_kick_held_mid_turn_is_rescued_at_the_close(temp_db, monkeypatch):
    """A kick read by the stream loop and held goes back to the queue when
    the socket dies mid-turn, so the close-rescue still runs it headless."""
    layer, gate, slug = _setup(monkeypatch)
    rescued: list[str] = []

    async def _fake_kick(self, wcid, sid, text, images, files, *, force_headless=False):
        rescued.append(text)
    from ws.dashboard import DashboardConnection
    monkeypatch.setattr(DashboardConnection, "_run_kick_headless", _fake_kick)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await _start(ws, layer, slug)
            conn = _conn()
            conn.notify_queue.put_nowait({"type": wire.NOTIFY_SERVER_KICK, "session_id": sid,
                                          "chat_id": "other-chat", "text": "the kick"})
            conn.notify_queue.put_nowait({"type": wire.NOTIFICATION, "delivery": {"id": 4}})
            await ws.expect({"type": wire.NOTIFICATION, "delivery": {"id": 4}})
            ws.client_disconnect()
            gate.set()
        await _wait_for(lambda: rescued == ["the kick"])
    run_ws_scenario(scenario)


def test_a_notification_during_a_task_chats_wait_for_its_next_pump(temp_db, monkeypatch):
    """Between a task chat's turns the viewer waits for the next pump: a
    notification queued meanwhile goes out during that wait."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    from ws import dashboard_chat_stream
    monkeypatch.setattr(dashboard_chat_stream, "_PUMP_POLL_S", 0.3)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            from core.events.common_events import PRODUCER_DONE
            from core.session import session_kind
            task_cid = "task-" + chat_id[:8]
            task_store.create_chat(task_cid, "user-admin", slug)
            eq: asyncio.Queue = asyncio.Queue()
            ends = asyncio.Event()

            async def _task_produce():
                await ends.wait()
                await eq.put(CommonEvent(type=PRODUCER_DONE, data={}))
            pump = stream_pump.ChatStreamPump(
                chat_id=task_cid, session_id=sid,
                producer=asyncio.create_task(_task_produce()),
                event_queue=eq, perm_queue=None,
                source_type=session_kind.TASK.source_type,
            )
            stream_pump._active_pumps[task_cid] = pump
            pump.start()
            ws.client_send({"type": "resume_chat", "chat_id": task_cid})
            await _until(ws, "live_state")
            ends.set()
            await _until(ws, "done")
            # The task chat's history re-send, then the wait for the next pump.
            await _until(ws, "chat_history")
            conn = _conn()
            conn.notify_queue.put_nowait({"type": wire.NOTIFICATION, "delivery": {"id": 5}})
            frame = await _until(ws, wire.NOTIFICATION, timeout=3.0)
            assert frame["delivery"] == {"id": 5}
    run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# 2. A card answered on one socket closes on the others
# ---------------------------------------------------------------------------

def _perm_item(request_id: str) -> dict:
    return {"event_type": "permission_prompt", "request_id": request_id,
            "tool_name": "Bash", "tool_input": {"command": "ls"}}


def _attach_second(ws2, chat_id: str):
    ws2.client_send({"type": "resume_chat", "chat_id": chat_id})
    return _until(ws2, "live_state")


def test_a_permission_answered_on_one_socket_closes_on_the_other(temp_db, monkeypatch):
    """Two tabs on one turn: the card shows on both; the answer on A reaches
    B as ``permission_resolved`` with the answer, tagged with the chat; B's
    later answer resolves nothing and sends nothing more; the first
    decision stands."""
    from core.session import session_state
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws1, \
                dashboard_connection(session_cookie()) as ws2:
            await drain_startup(ws1)
            await drain_startup(ws2)
            chat_id, sid = await _start(ws1, layer, slug)
            await _attach_second(ws2, chat_id)
            session_state.open_permission_wait("req-a", sid)
            session_state.get_permission_queue(sid).put_nowait(_perm_item("req-a"))
            for ws in (ws1, ws2):
                card = await _until(ws, "permission_prompt")
                assert card["request_id"] == "req-a" and card["chat_id"] == chat_id
            ws1.client_send({"type": "permission_response", "request_id": "req-a",
                             "approved": False})
            resolved = {"type": "permission_resolved", "request_id": "req-a",
                        "approved": False, "chat_id": chat_id}
            assert await _until(ws2, "permission_resolved") == resolved
            # The answering socket gets it too (idempotent on the page).
            assert await _until(ws1, "permission_resolved") == resolved
            assert session_state._permission_decisions["req-a"] is False
            # The second socket's answer, before the waiter left: refused,
            # nothing more goes out, the decision stands.
            ws2.client_send({"type": "permission_response", "request_id": "req-a",
                             "approved": True})
            ws2.client_send({"type": "ping"})
            frame = await ws2.next_frame(5)
            assert frame["type"] == "pong", frame
            assert session_state._permission_decisions["req-a"] is False
            gate.set()
            await _until(ws1, "done")
            await _until(ws2, "done")
    run_ws_scenario(scenario)


def test_a_question_answered_on_one_socket_closes_on_the_other_and_a_late_answer_starts_no_turn(
        temp_db, monkeypatch):
    """A held question card stored by 1.9.0 (no question holds a turn since
    1.9.1): A's answer closes the card on B and is the person's next message,
    a turn of its own; B's late answer is dropped."""
    from core.session import session_state
    from ws import dashboard_prompts
    layer, gate, slug = _setup(monkeypatch)
    dashboard_prompts._recently_answered.clear()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws1, \
                dashboard_connection(session_cookie()) as ws2:
            await drain_startup(ws1)
            await drain_startup(ws2)
            chat_id, sid = await _start(ws1, layer, slug)
            await _attach_second(ws2, chat_id)
            session_state.get_permission_queue(sid).put_nowait({
                "event_type": "question_prompt", "request_id": "req-q",
                "tool_name": "request_user_input",
                "tool_input": {"questions": [{"id": "q1", "question": "Which?"}]}})
            for ws in (ws1, ws2):
                assert (await _until(ws, "question"))["request_id"] == "req-q"
            ws1.client_send({"type": "question_response", "request_id": "req-q",
                             "answers": {"q1": {"answers": ["a"]}}})
            assert await _until(ws2, "permission_resolved") == {
                "type": "permission_resolved", "request_id": "req-q", "chat_id": chat_id}
            gate.set()
            # A's answer is the person's next message: queued, then its turn.
            frames = await _collect_until(ws1, "done")
            assert "queue_sent" in [f["type"] for f in frames]
            assert layer.messages[-1][1] == "a"
            await _until(ws2, "done")
            # B's late answer between turns, for a question A answered:
            # dropped, never replayed as a chat turn.
            turns = len(layer.messages)
            ws2.client_send({"type": "question_response", "request_id": "req-q",
                             "answers": {"q1": {"answers": ["b"]}}})
            ws2.client_send({"type": "ping"})
            assert (await _until(ws2, "pong"))["type"] == "pong"
            await asyncio.sleep(0.3)
            assert len(layer.messages) == turns
    run_ws_scenario(scenario)


def test_a_permission_answer_after_the_waiter_left_changes_nothing(temp_db, monkeypatch):
    """The hook's own waiter takes A's answer and leaves; B's answer lands
    after that (the id unknown again): no second close, no slot move."""
    from core.session import session_state
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws1, \
                dashboard_connection(session_cookie()) as ws2:
            await drain_startup(ws1)
            await drain_startup(ws2)
            chat_id, sid = await _start(ws1, layer, slug)
            await _attach_second(ws2, chat_id)
            waiter = asyncio.create_task(session_state.wait_for_permission("req-w", sid))
            await asyncio.sleep(0)
            session_state.get_permission_queue(sid).put_nowait(_perm_item("req-w"))
            for ws in (ws1, ws2):
                assert (await _until(ws, "permission_prompt"))["request_id"] == "req-w"
            ws1.client_send({"type": "permission_response", "request_id": "req-w",
                             "approved": False})
            for ws in (ws1, ws2):
                assert (await _until(ws, "permission_resolved"))["approved"] is False
            assert await asyncio.wait_for(waiter, 5) is False
            assert not session_state.prompt_waits("req-w")
            ws2.client_send({"type": "permission_response", "request_id": "req-w",
                             "approved": True})
            for ws in (ws2, ws1):
                ws.client_send({"type": "ping"})
                frame = await ws.next_frame(5)
                assert frame["type"] == "pong", frame
            gate.set()
            await _until(ws1, "done")
            await _until(ws2, "done")
    run_ws_scenario(scenario)


def test_a_tab_opened_mid_turn_gets_the_card_waiting_between_turns(temp_db, monkeypatch):
    """A card shown between turns still waits while a later turn shows its
    own: a tab that opens the chat gets it after the turn's live state (whose
    paint replaces the chat), and the turn's own card from that live state
    alone."""
    from core.session import engine_prompts, session_state
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws1, \
                dashboard_connection(session_cookie()) as ws2:
            await drain_startup(ws1)
            await drain_startup(ws2)
            chat_id, sid = await _start(ws1, layer, slug)
            session_state.open_permission_wait("req-before", sid)
            engine_prompts.offer_hook_card(asyncio.Queue(), _perm_item("req-before"),
                                           session_id=sid, chat_id=chat_id)
            # The turn's own card: the engine's ask, held in the pump's slot
            # and registered for its chat as the pump does.
            session_state.open_permission_wait("req-turn", sid)
            engine_prompts._pending["req-turn"] = engine_prompts.EnginePrompt(
                request_id="req-turn", session_id=sid, chat_id=chat_id, tool_name="Bash",
                tool_input={"command": "ls"}, frame={
                    "request_id": "req-turn", "tool_name": "Bash", "tool_input": {"command": "ls"}})
            session_state.get_permission_queue(sid).put_nowait(_perm_item("req-turn"))
            while (await _until(ws1, "permission_prompt"))["request_id"] != "req-turn":
                pass
            try:
                ws2.client_send({"type": "resume_chat", "chat_id": chat_id})
                before = await _collect_until(ws2, "live_state")
                assert before[-1]["pending_permission"]["request_id"] == "req-turn"
                assert "permission_prompt" not in [f["type"] for f in before]
                ws2.client_send({"type": "ping"})
                after = await _collect_until(ws2, "pong")
                assert [f["request_id"] for f in after
                        if f["type"] == "permission_prompt"] == ["req-before"]
            finally:
                engine_prompts._pending.pop("req-turn", None)
                engine_prompts._hook_cards.clear()
                for rid in ("req-before", "req-turn"):
                    session_state.resolve_permission(rid, False)
                gate.set()
            await _until(ws1, "done")
            await _until(ws2, "done")
    run_ws_scenario(scenario)


def test_an_answer_to_another_card_leaves_the_turns_card_in_its_slot(temp_db, monkeypatch):
    """A card shown between turns still waits while a new turn shows its own:
    the answer to the first, from a tab on the turn, resolves it and leaves
    the turn's card where a reconnect finds it."""
    from core.session import session_state
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await _start(ws, layer, slug)
            session_state.open_permission_wait("req-turn", sid)
            session_state.get_permission_queue(sid).put_nowait(_perm_item("req-turn"))
            assert (await _until(ws, "permission_prompt"))["request_id"] == "req-turn"
            session_state.open_permission_wait("req-before", sid)
            ws.client_send({"type": "permission_response", "request_id": "req-before",
                            "approved": True})
            assert (await _until(ws, "permission_resolved"))["request_id"] == "req-before"
            assert session_state._permission_decisions["req-before"] is True
            pump = stream_pump._active_pumps[chat_id]
            assert pump._permission_active["request_id"] == "req-turn"
            assert session_state.prompt_waits("req-turn")
            ws.client_send({"type": "permission_response", "request_id": "req-turn",
                            "approved": True})
            await _until(ws, "permission_resolved")
            assert pump._permission_active is None
            gate.set()
            await _until(ws, "done")
    run_ws_scenario(scenario)


def test_a_second_plan_review_answer_changes_nothing(temp_db, monkeypatch):
    """A plan review answered on A: B gets the close with the action. B's
    own answer, clicked before its card closed and landing after the hook's
    waiter took A's (the id unknown again), flips no mode, writes no row
    and queues no implement turn."""
    from core.session import session_state
    layer, gate, slug = _setup(monkeypatch)
    review = {"event_type": "plan_review", "request_id": "req-plan", "plan": "# plan",
              "tool_input": {"planFilePath": "/p/plan.md"}, "filename": "plan.md"}

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws1, \
                dashboard_connection(session_cookie()) as ws2:
            await drain_startup(ws1)
            await drain_startup(ws2)
            chat_id, sid = await _start(ws1, layer, slug)
            await _attach_second(ws2, chat_id)
            waiter = asyncio.create_task(session_state.wait_for_permission("req-plan", sid))
            await asyncio.sleep(0)
            session_state.get_permission_queue(sid).put_nowait(review)
            for ws in (ws1, ws2):
                assert (await _until(ws, "plan_review"))["request_id"] == "req-plan"
            ws1.client_send({"type": "plan_review_response", "request_id": "req-plan",
                             "action": "reject", "filename": "plan.md"})
            assert await _until(ws2, "permission_resolved") == {
                "type": "permission_resolved", "request_id": "req-plan", "action": "reject",
                "chat_id": chat_id}
            # Cancel denies the exit (the engine stays in Plan for the turn);
            # the waiter is gone.
            assert await asyncio.wait_for(waiter, 5) is False
            assert not session_state.prompt_waits("req-plan")
            ws2.client_send({"type": "plan_review_response", "request_id": "req-plan",
                             "action": "implement_auto", "filename": "plan.md"})
            ws2.client_send({"type": "ping"})
            frame = await ws2.next_frame(5)
            assert frame["type"] == "pong", frame   # no mode_changed, nothing
            gate.set()
            await _until(ws1, "done")
            await _until(ws2, "done")
            await asyncio.sleep(0.3)
            # One turn ran (no implement turn), the mode is the pre-plan one.
            assert [p for _s, p, _k in layer.messages] == ["start"]
            assert session_state.get_session_mode(sid) == "ask"
            assert task_store.get_chat(chat_id)["permission_mode"] == "ask"
    run_ws_scenario(scenario)


def test_two_tabs_answering_a_plan_between_turns_start_one_implement_turn(temp_db, monkeypatch):
    """A plan review nothing waits on (a Codex plan), answered on two tabs at
    the same moment after its turn ended: the first answer claims the card
    before it reads the plan row, so one implement turn runs."""
    from core.session import session_state
    from ws import dashboard_prompts
    layer, gate, slug = _setup(monkeypatch)
    review = {"event_type": "plan_review", "request_id": "req-codex-plan", "plan": "# plan",
              "tool_input": {"plan": "# plan", "planFilePath": "plan-x.md", "between_turns": True}}
    real_pending = dashboard_prompts.PromptController._plan_row_pending

    async def _slow_pending(chat_id, filename):
        await asyncio.sleep(0.2)       # the window both tabs used to pass through
        return await real_pending(chat_id, filename)
    monkeypatch.setattr(dashboard_prompts.PromptController, "_plan_row_pending",
                        staticmethod(_slow_pending))

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws1, \
                dashboard_connection(session_cookie()) as ws2:
            await drain_startup(ws1)
            await drain_startup(ws2)
            chat_id, sid = await _start(ws1, layer, slug)
            await _attach_second(ws2, chat_id)
            session_state.get_permission_queue(sid).put_nowait(review)
            for ws in (ws1, ws2):
                assert (await _until(ws, "plan_review"))["request_id"] == "req-codex-plan"
            gate.set()
            await _until(ws1, "done")
            await _until(ws2, "done")
            await asyncio.sleep(0.3)
            for ws in (ws1, ws2):
                ws.client_send({"type": "plan_review_response", "request_id": "req-codex-plan",
                                "action": "implement_auto", "filename": "plan-x.md"})
            await _wait_for(lambda: len(layer.messages) >= 2)
            await asyncio.sleep(1.0)
            prompts = [p for _s, p, _k in layer.messages]
            assert sum(p.count("Please implement the plan now.") for p in prompts) == 1, prompts
            assert task_store.get_chat(chat_id)["permission_mode"] == "autoReview"
    run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# 3. Deleting a chat from its own page while its turn runs
# ---------------------------------------------------------------------------

def _admin_ctx(slug: str):
    from auth.providers import UserContext
    return UserContext(sub="user-admin", email="admin@test.com", name="Admin User",
                       role="admin", agents=[slug], agent_roles={slug: "admin"})


def test_deleting_a_chat_mid_turn_ends_its_turn_first(temp_db, monkeypatch):
    """The route ends the pump before the row goes: the rows and the turn's
    end are queued ahead of the delete, no alert fires, the dot clears,
    the session is closed, the viewer gets its ``done`` and leaves the
    chat, and what waited in the chat's queue goes with it."""
    from api.agents import chats as api
    layer, gate, slug = _setup(monkeypatch)
    pings: list = []
    statuses: list = []

    async def _fake_ephemeral(*a, **kw):
        pings.append(kw)
    monkeypatch.setattr(stream_pump.notification_manager, "fire_ephemeral", _fake_ephemeral)
    real_status = stream_pump.notification_manager.broadcast_chat_status

    def _status(owner, cid, status, agent=""):
        statuses.append((cid, status))
        real_status(owner, cid, status, agent=agent)
    monkeypatch.setattr(stream_pump.notification_manager, "broadcast_chat_status", _status)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await _start(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-wait", "user-viewer", TurnInput("a teammate's"))
            conn = _conn()
            assert conn.chat_id == chat_id
            pump = stream_pump._active_pumps[chat_id]
            res = await api.delete_chat(chat_id, user=_admin_ctx(slug))
            assert res == {"status": "ok"}
            assert pump.is_done and pump.deleting
            assert chat_id not in stream_pump._active_pumps
            assert task_store.get_chat(chat_id) is None
            assert layer.closed_sessions == [sid]
            assert input_queue.deleting(chat_id)   # the mark lingers past the route
            assert input_queue.peek(chat_id) == []
            await _until(ws, "done")
            # The ready broadcast went out; nothing finished.
            assert (chat_id, "ready") in statuses
            assert pings == []
            # The socket left the chat: a poll names it no more.
            await _wait_for(lambda: conn.chat_id is None)
            ws.client_send({"type": "ping"})
            seen = []
            while True:
                frame = await ws.next_frame(5)
                if frame["type"] == "pong":
                    break
                seen.append(frame["type"])
            assert not {"error", "chat_history", "warmup_started", "queue_cleared",
                        "turn_complete"} & set(seen), seen
    run_ws_scenario(scenario)


def test_a_delete_during_the_hold_is_refused(temp_db, monkeypatch):
    from fastapi import HTTPException

    from api.agents import chats as api
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await warm_new_chat(ws, layer, slug)
            input_queue.hold_first_turn(chat_id)
            try:
                with pytest.raises(HTTPException) as exc:
                    await api.delete_chat(chat_id, user=_admin_ctx(slug))
                assert exc.value.status_code == 409 and "still starting" in exc.value.detail
            finally:
                input_queue.first_turn_started(chat_id)
            assert task_store.get_chat(chat_id) is not None
    run_ws_scenario(scenario)


@pytest.mark.asyncio
async def test_a_deleting_chat_starts_nothing(temp_db, monkeypatch):
    """The tombstone: no claim, no delivery, no armed delivery, the entries
    dropped while the queue stays loaded."""
    from services.scheduler import shared
    monkeypatch.setattr(shared, "_shutting_down", False)
    cid = "c-deleting"
    task_store.create_chat(cid, "user-admin", "agent-x")
    q = await input_queue.loaded(cid)
    await q.add("q-1", "user-admin", TurnInput("waiting"))
    input_queue.mark_deleting(cid)
    try:
        assert input_queue.peek(cid) == [] and q.loaded
        assert q.try_claim() is None
        assert await input_queue.claim_when_free(cid, timeout=0.05) is None
        assert await input_queue.deliver(cid) is False
        await input_queue._after_pump_end(cid, return_upto=None, viewers=False)
        assert q.try_claim() is None
    finally:
        input_queue.unmark_deleting(cid, landed=True)
    # A landed delete keeps the mark a while (a slow viewer still asks),
    # then it lapses on its own.
    assert input_queue.deleting(cid)
    input_queue._deleting[cid] = 0.0
    assert not input_queue.deleting(cid) and cid not in input_queue._deleting
    assert q.try_claim() is not None


def test_a_busy_send_during_the_delete_is_refused(temp_db, monkeypatch):
    """A send into the running turn while the chat's delete is under way is
    refused by name (no entry outlives the chat)."""
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            input_queue.mark_deleting(chat_id)
            ws.client_send({"type": "chat", "text": "late", "chat_id": chat_id, "queue_id": "q-d"})
            err = await _until(ws, "error")
            assert err == {"type": "error", "message": "This chat was deleted.", "queue_id": "q-d"}
            assert input_queue.peek(chat_id) == []
            input_queue._deleting.clear()
            gate.set()
            await _until(ws, "done")
    run_ws_scenario(scenario)


@pytest.mark.asyncio
async def test_a_pump_that_outlives_the_delete_wait_is_logged(temp_db, caplog):
    """The route goes on past the wait, with a warning naming the chat (the
    row delete then races the pump's last writes)."""
    import logging

    from core.session import session_kind
    cid = "c-stuck"
    task_store.create_chat(cid, "user-admin", "agent-x")
    never = asyncio.Event()
    pump = stream_pump.ChatStreamPump(
        chat_id=cid, session_id="sid-stuck", producer=asyncio.create_task(never.wait()),
        event_queue=asyncio.Queue(), perm_queue=None,
        source_type=session_kind.DASHBOARD.source_type)
    # The pump's loop as a hung engine leaves it: not done, not ending.
    pump._task = asyncio.create_task(never.wait())
    try:
        with caplog.at_level(logging.WARNING, logger="otodock"):
            await pump.end_for_delete(timeout=0.05)
        assert pump.stopped and pump.deleting
        assert "did not end within" in caplog.text and cid in caplog.text
    finally:
        pump._task.cancel()
        pump.producer.cancel()


# ---------------------------------------------------------------------------
# 4. A second page's send during another page's warmup
# ---------------------------------------------------------------------------

def test_a_cold_send_on_a_chat_another_page_warms_joins_it(temp_db, monkeypatch):
    """The second page's text waits behind the first turn: one spawn, one
    first prompt, its chip named by the id the warmup carried, the history
    and the warmup's frames replayed, ``warmup_ready`` at the spawn's end,
    and its message as the next turn."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws1, \
                dashboard_connection(session_cookie()) as ws2:
            await drain_startup(ws1)
            await drain_startup(ws2)
            layer.start_gate = asyncio.Event()
            ws1.client_send({"type": "warmup", "agent": slug, "text": "first"})
            chat_id = (await _until(ws1, "warmup_started"))["chat_id"]
            ws2.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id,
                             "text": "second", "queue_id": "q-2"})
            hist = await _until(ws2, "chat_history")
            assert [m["content"] for m in hist["messages"]] == ["first"]
            started = await _until(ws2, "warmup_started")
            assert started["chat_id"] == chat_id
            chip = await _until(ws2, "queued")
            assert chip["queue_id"] == "q-2" and chip["text"] == "second"
            assert input_queue.getting_ready(chat_id)
            layer.start_gate.set()
            ready = await _until(ws2, "warmup_ready", timeout=8)
            assert ready["chat_id"] == chat_id and ready["session_id"]
            await _until(ws1, "warmup_ready", timeout=8)
            await _wait_for(lambda: len(layer.messages) >= 2, timeout=8)
            await asyncio.sleep(0.3)
            assert len(layer.started) == 1
            assert "first" in layer.messages[0][1] and layer.messages[1][1] == "second"
            rows = [m["content"] for m in task_store.get_chat_messages(chat_id)
                    if m["role"] == "user"]
            assert rows == ["first", "second"]
    run_ws_scenario(scenario)


def test_a_cold_send_during_the_hold_joins_and_the_drain_starts_nothing(temp_db, monkeypatch):
    """The spawn done, the first turn still enqueued: the second page's text
    waits, ``warmup_ready`` names the live session, and nothing runs the
    text as the first turn (the viewer's drain and the delivery refuse the
    hold; the claim itself stays free for the first turn's own start)."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws1, \
                dashboard_connection(session_cookie()) as ws2:
            await drain_startup(ws1)
            await drain_startup(ws2)
            chat_id, sid = await warm_new_chat(ws1, layer, slug)
            input_queue.hold_first_turn(chat_id)
            try:
                ws2.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id,
                                 "text": "second", "queue_id": "q-2"})
                ready = await _until(ws2, "warmup_ready")
                assert ready["session_id"] == sid and ready["chat_id"] == chat_id
                assert (await _until(ws2, "queued"))["queue_id"] == "q-2"
                ws2.client_send({"type": "ping"})
                await _until(ws2, "pong")
                await asyncio.sleep(0.4)
                assert layer.messages == []
                assert await input_queue.deliver(chat_id) is False
                for conn in _connections_by_user["user-admin"]:
                    assert await conn._drain_input_queue() is False
                assert layer.messages == []
                q = input_queue.get(chat_id)
                claim = q.try_claim()
                assert claim is not None
                q.release(claim, rearm=False)
                assert len(layer.started) == 1
            finally:
                input_queue.first_turn_not_run(chat_id)
            # The hold ended with no first turn: the text goes out now.
            await _wait_for(lambda: len(layer.messages) == 1, timeout=8)
            assert layer.messages[0][1] == "second"
    run_ws_scenario(scenario)


def test_a_cold_send_on_a_chat_whose_turn_runs_joins_it(temp_db, monkeypatch):
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws1, \
                dashboard_connection(session_cookie()) as ws2:
            await drain_startup(ws1)
            await drain_startup(ws2)
            chat_id, sid = await _start(ws1, layer, slug)
            from ws.dashboard import _dashboard_notify_queues
            before = _dashboard_notify_queues.get(sid)
            ws2.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id,
                             "text": "second", "queue_id": "q-2"})
            seen: dict = {}
            while not {"queued", "live_state", "warmup_ready"} <= seen.keys():
                frame = await ws2.next_frame(5)
                seen.setdefault(frame["type"], frame)
            assert seen["queued"]["queue_id"] == "q-2"
            assert seen["warmup_ready"]["session_id"] == sid
            assert len(layer.started) == 1
            # The joined socket is the session's notification sink now, as
            # a resumed one would be.
            from ws.dashboard import _dashboard_notify_queues
            assert _dashboard_notify_queues.get(sid) is not before
            assert _dashboard_notify_queues.get(sid) in {
                c.notify_queue for c in _connections_by_user["user-admin"]}
            gate.set()
            await _until(ws1, "done")
            await _wait_for(lambda: len(layer.messages) >= 2, timeout=8)
            assert layer.messages[1][1] == "second"
    run_ws_scenario(scenario)


def _spawn_nothing(monkeypatch):
    """``_do_warmup`` answers ``skip``, as its refusals do (the drive gate,
    a deleted chat)."""
    from ws.dashboard import DashboardConnection

    async def _skip(self, agent, msg):
        return "skip"
    monkeypatch.setattr(DashboardConnection, "_do_warmup", _skip)


def test_a_warmup_that_spawns_nothing_leaves_another_pages_entry(temp_db, monkeypatch):
    """A no-text warmup from a second tab on a chat another page is warming
    (an install banner's retry, an interactive cold start) that ends in no
    spawn drops only its own registration, never the page's in flight."""
    from core.session import warmup_registry
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws1, \
                dashboard_connection(session_cookie()) as ws2:
            await drain_startup(ws1)
            await drain_startup(ws2)
            layer.start_gate = asyncio.Event()
            ws1.client_send({"type": "warmup", "agent": slug, "text": "first"})
            chat_id = (await _until(ws1, "warmup_started"))["chat_id"]
            _spawn_nothing(monkeypatch)
            ws2.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id})
            ws2.client_send({"type": "ping"})
            await _until(ws2, "pong")
            assert warmup_registry.get(chat_id) is not None
            assert input_queue.getting_ready(chat_id)
            layer.start_gate.set()
            await _until(ws1, "warmup_ready", timeout=8)
            await _wait_for(lambda: len(layer.messages) == 1, timeout=8)
            assert "first" in layer.messages[0][1]
    run_ws_scenario(scenario)


def test_a_dropped_early_registration_arms_what_was_queued_meanwhile(temp_db, monkeypatch):
    """A cold warmup on an idle chat that spawns nothing (refused) ends the
    chat's getting-ready window and hands what waits to the delivery."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-t", "user-admin", TurnInput("later"))
            _spawn_nothing(monkeypatch)
            ws.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id})
            await _wait_for(lambda: len(layer.messages) == 1, timeout=8)
            assert layer.messages[0][0] == sid and "later" in layer.messages[0][1]
            assert not input_queue.getting_ready(chat_id)
    run_ws_scenario(scenario)


def test_an_inline_cold_send_holds_the_first_turn_until_its_kick(temp_db, monkeypatch):
    """A cold send on a chat whose session is alive reuses it inline: the
    chat stays getting ready from the registration's drop to the kick's
    pump, so a joined page's delivery cannot start its text first."""
    from ws.dashboard import DashboardConnection
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    seen: list[bool] = []
    real = DashboardConnection._reapply_deferred_after_warmup

    async def _record(self, cid, **kw):
        seen.append(input_queue.getting_ready(cid))
        return await real(self, cid, **kw)
    monkeypatch.setattr(DashboardConnection, "_reapply_deferred_after_warmup", _record)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            ws.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id, "text": "again"})
            await _until(ws, "done", timeout=8)
            assert seen == [False, True]   # the new chat's warmup, then the reuse
            assert [p for s_, p, _k in layer.messages if s_ == sid] and \
                "again" in layer.messages[-1][1]
            assert not input_queue.getting_ready(chat_id)
    run_ws_scenario(scenario)


def test_stop_from_the_joined_socket_takes_its_message_back_and_aborts_nothing(
        temp_db, monkeypatch):
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws1, \
                dashboard_connection(session_cookie()) as ws2:
            await drain_startup(ws1)
            await drain_startup(ws2)
            layer.start_gate = asyncio.Event()
            ws1.client_send({"type": "warmup", "agent": slug, "text": "first"})
            chat_id = (await _until(ws1, "warmup_started"))["chat_id"]
            ws2.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id,
                             "text": "second", "queue_id": "q-2"})
            await _until(ws2, "queued")
            ws2.client_send({"type": "abort"})
            seen: dict = {}
            while not {"queue_cleared", "aborted"} <= seen.keys():
                frame = await ws2.next_frame(5)
                seen.setdefault(frame["type"], frame)
            assert seen["queue_cleared"]["queue_ids"] == ["q-2"]
            assert seen["queue_cleared"]["returned"] is True
            assert layer.aborted == []
            assert input_queue.peek(chat_id) == []
            layer.start_gate.set()
            await _until(ws1, "warmup_ready", timeout=8)
            await _wait_for(lambda: len(layer.messages) >= 1, timeout=8)
            await asyncio.sleep(0.3)
            assert [p for _s, p, _k in layer.messages] == ["first"] or \
                "first" in layer.messages[0][1]
            assert len(layer.messages) == 1
    run_ws_scenario(scenario)


def test_a_held_chats_headless_kick_runs_its_first_turn(temp_db, monkeypatch):
    """The person left during the spawn (the socket gone, the close-rescue):
    the kick runs headless on the held chat, the hold ends with it."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            input_queue.hold_first_turn(chat_id)
            conn = _conn()
            await conn._run_kick_headless(chat_id, sid, "first", [], [], force_headless=True)
            await _wait_for(lambda: len(layer.messages) == 1, timeout=8)
            assert layer.messages[0][0] == sid and "first" in layer.messages[0][1]
            assert not input_queue.getting_ready(chat_id)
    run_ws_scenario(scenario)


def test_a_kick_for_a_chat_the_person_left_runs_its_held_first_turn(temp_db, monkeypatch):
    """A cold send, then a switch to a new chat during "Getting ready": the
    spawn's kick reaches the socket for a chat it no longer views and runs
    the first turn headless there (the hold set by the spawn's end)."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            layer.start_gate = asyncio.Event()
            ws.client_send({"type": "warmup", "agent": slug, "text": "first"})
            first_cid = (await _until(ws, "warmup_started"))["chat_id"]
            ws.client_send({"type": "warmup", "agent": slug})
            second_cid = (await _until(ws, "warmup_started"))["chat_id"]
            assert second_cid != first_cid
            layer.start_gate.set()
            await _wait_for(lambda: any("first" in p for _s, p, _k in layer.messages), timeout=8)
            first_sid = task_store.get_chat(first_cid)["session_id"]
            assert [s for s, p, _k in layer.messages if "first" in p] == [first_sid]
            await _wait_for(lambda: not input_queue.getting_ready(first_cid), timeout=8)
    run_ws_scenario(scenario)


def test_a_stop_on_the_second_of_two_spawns_still_stops_it(temp_db, monkeypatch):
    """One socket, two spawns: a no-text warmup of chat X still in flight
    when a cold send opens chat Y. X's end drops only its own mark, so a
    Stop on Y during Y's spawn flags Y's kick and no first turn runs."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    # The spawns wait on the gate the layer holds when each reaches it:
    # the second gate is swapped in once the first spawn is parked.
    entered: list[str] = []
    real_start = layer.start_session

    async def _start_session(sid, agent_cfg):
        entered.append(sid)
        await real_start(sid, agent_cfg)
    layer.start_session = _start_session

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            gate_x = layer.start_gate = asyncio.Event()
            ws.client_send({"type": "warmup", "agent": slug})
            x_cid = (await _until(ws, "warmup_started"))["chat_id"]
            await _wait_for(lambda: len(entered) == 1, timeout=8)
            gate_y = layer.start_gate = asyncio.Event()
            ws.client_send({"type": "warmup", "agent": slug, "text": "y-first"})
            y_cid = (await _until(ws, "warmup_started"))["chat_id"]
            assert y_cid != x_cid
            await _wait_for(lambda: len(entered) == 2, timeout=8)
            conn = _conn()
            assert conn._warmup_chat_id == y_cid
            gate_x.set()
            await _wait_for(lambda: len(layer.started) == 1, timeout=8)
            await _wait_for(lambda: x_cid not in conn._attached_warmups, timeout=8)
            assert conn._warmup_chat_id == y_cid
            ws.client_send({"type": "abort"})
            assert (await _until(ws, "aborted"))["chat_id"] == y_cid
            assert conn._warmup_abort_chat == y_cid
            gate_y.set()
            await _wait_for(lambda: len(layer.aborted) == 1, timeout=8)
            await asyncio.sleep(0.3)
            assert layer.messages == []
            assert conn._warmup_abort_chat is None and conn._warmup_chat_id is None
    run_ws_scenario(scenario)


def test_a_stopped_spawn_holds_nothing_and_the_next_message_runs(temp_db, monkeypatch):
    """A Stop during a cold send's spawn: once the spawn ends the chat is no
    longer getting ready (no hold for a kick that never comes), a teammate's
    message queued meanwhile goes out, and the person's next message runs
    as a turn instead of waiting 120 s."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            layer.start_gate = asyncio.Event()
            ws.client_send({"type": "warmup", "agent": slug, "text": "first"})
            chat_id = (await _until(ws, "warmup_started"))["chat_id"]
            ws.client_send({"type": "abort"})
            await _until(ws, "aborted")
            layer.start_gate.set()
            await _wait_for(lambda: len(layer.aborted) == 1, timeout=8)
            await _wait_for(lambda: not input_queue.getting_ready(chat_id), timeout=3)
            ws.client_send({"type": "chat", "text": "next", "chat_id": chat_id, "queue_id": "q-n"})
            await _until(ws, "done", timeout=8)
            # One turn, the next message (behind the cancelled-turn notice
            # the stamped Stop injects).
            assert len(layer.messages) == 1 and layer.messages[0][1].endswith("next")
    run_ws_scenario(scenario)


def test_a_failed_spawn_returns_a_teammates_queued_message_too(temp_db, monkeypatch):
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    real_start = layer.start_session

    async def failing_start(sid, agent_cfg):
        await real_start(sid, agent_cfg)
        raise RuntimeError("spawn failed")
    layer.start_session = failing_start

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            layer.start_gate = asyncio.Event()
            ws.client_send({"type": "warmup", "agent": slug, "text": "first"})
            chat_id = (await _until(ws, "warmup_started"))["chat_id"]
            q = await input_queue.loaded(chat_id)
            await q.add("q-t", "user-viewer", TurnInput("a teammate's"))
            ws.client_send({"type": "chat", "text": "second", "chat_id": chat_id,
                            "queue_id": "q-2", "queue_only": True})
            await _until(ws, "queued")
            layer.start_gate.set()
            cleared = await _until(ws, "queue_cleared", timeout=8)
            assert sorted(cleared["queue_ids"]) == ["q-2", "q-t"]
            assert cleared["reason"] == "turn_failed"
            assert input_queue.peek(chat_id) == []
    run_ws_scenario(scenario)


def test_stop_from_a_socket_that_warmed_another_chat_before_takes_its_own_message_back(
        temp_db, monkeypatch):
    """A socket that brought up a chat of its own earlier and now joins a
    chat another page warms is a joined page there: its Stop returns its
    own message, a teammate's stays, the warmer's kick is not flagged and
    the first turn still runs."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws1, \
                dashboard_connection(session_cookie()) as ws2:
            await drain_startup(ws1)
            await drain_startup(ws2)
            await warm_new_chat(ws2, layer, slug)
            layer.start_gate = asyncio.Event()
            ws1.client_send({"type": "warmup", "agent": slug, "text": "first"})
            chat_id = (await _until(ws1, "warmup_started"))["chat_id"]
            q = await input_queue.loaded(chat_id)
            await q.add("q-t", "user-viewer", TurnInput("a teammate's"))
            ws2.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id,
                             "text": "second", "queue_id": "q-2"})
            await _until(ws2, "queued")
            ws2.client_send({"type": "abort"})
            seen: dict = {}
            while not {"queue_cleared", "aborted"} <= seen.keys():
                frame = await ws2.next_frame(5)
                seen.setdefault(frame["type"], frame)
            assert seen["queue_cleared"]["queue_ids"] == ["q-2"]
            assert [qi.queue_id for qi in input_queue.peek(chat_id)] == ["q-t"]
            assert input_queue.getting_ready(chat_id)
            assert all(c._warmup_abort_chat != chat_id for c in _connections_by_user["user-admin"])
            layer.start_gate.set()
            await _until(ws1, "warmup_ready", timeout=8)
            await _wait_for(lambda: len(layer.messages) >= 1, timeout=8)
            assert "first" in layer.messages[0][1]
            assert len(layer.started) == 2
    run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# 5. A refused queue-only send names its message
# ---------------------------------------------------------------------------

def _refuse_attachments(monkeypatch, message: str):
    from ws.dashboard import DashboardConnection
    from ws.dashboard_chat_support import AttachmentsRefused

    async def _refuse(self, *a, **k):
        raise AttachmentsRefused(message)
    monkeypatch.setattr(DashboardConnection, "_process_attachments", _refuse)


def test_a_refused_queue_only_send_names_its_message(temp_db, monkeypatch):
    """The refusal of a send the page drew no bubble for names its id and
    nothing else (no chat tag, no done): the page keeps its warmup; the
    first turn still runs."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            layer.start_gate = asyncio.Event()
            ws.client_send({"type": "warmup", "agent": slug, "text": "first"})
            chat_id = (await _until(ws, "warmup_started"))["chat_id"]
            _refuse_attachments(monkeypatch, "no photos here")
            ws.client_send({"type": "chat", "text": "second", "chat_id": chat_id,
                            "queue_id": "q-x", "queue_only": True,
                            "images": [{"data": "data:image/png;base64,AA==", "name": "p.png"}]})
            err = await _until(ws, "error")
            assert err == {"type": "error", "message": "no photos here", "queue_id": "q-x"}
            ws.client_send({"type": "ping"})
            frame = await ws.next_frame(5)
            assert frame["type"] == "pong", frame   # no done: the page keeps its warmup
            assert input_queue.peek(chat_id) == []
            layer.start_gate.set()
            await _until(ws, "warmup_ready", timeout=8)
    run_ws_scenario(scenario)


def test_the_cap_refusal_names_the_message_and_an_idle_refusal_stays_bare(temp_db, monkeypatch):
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    monkeypatch.setattr(input_queue, "CAP", 1)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            layer.start_gate = asyncio.Event()
            ws.client_send({"type": "warmup", "agent": slug, "text": "first"})
            chat_id = (await _until(ws, "warmup_started"))["chat_id"]
            ws.client_send({"type": "chat", "text": "second", "chat_id": chat_id,
                            "queue_id": "q-2", "queue_only": True})
            await _until(ws, "queued")
            ws.client_send({"type": "chat", "text": "third", "chat_id": chat_id,
                            "queue_id": "q-3", "queue_only": True})
            err = await _until(ws, "error")
            assert err["queue_id"] == "q-3" and "chat_id" not in err
            layer.start_gate.set()
            await _until(ws, "warmup_ready", timeout=8)
            # The first turn, then the queued one as the next turn of the
            # same producer: one done at its end.
            await _wait_for(lambda: len(layer.messages) >= 2, timeout=8)
            await _until(ws, "done", timeout=8)
            ws.client_send({"type": "ping"})
            await _until(ws, "pong")
            # An idle send's refusal: bare, and done ends the turn the page armed.
            _refuse_attachments(monkeypatch, "no photos here")
            ws.client_send({"type": "chat", "text": "fourth", "chat_id": chat_id,
                            "queue_id": "q-4",
                            "images": [{"data": "data:image/png;base64,AA==", "name": "p.png"}]})
            err = await _until(ws, "error")
            assert err == {"type": "error", "message": "no photos here"}
            assert (await _until(ws, "done"))["chat_id"] == chat_id
    run_ws_scenario(scenario)


def test_a_busy_sends_refusal_names_the_message(temp_db, monkeypatch):
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            _refuse_attachments(monkeypatch, "no photos here")
            ws.client_send({"type": "chat", "text": "busy", "chat_id": chat_id, "queue_id": "q-b",
                            "images": [{"data": "data:image/png;base64,AA==", "name": "p.png"}]})
            err = await _until(ws, "error")
            assert err == {"type": "error", "message": "no photos here", "queue_id": "q-b"}
            gate.set()
            await _until(ws, "done")
    run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# 6. A task chat viewed through a sibling run: the snapshot names the queue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_resume_snapshot_names_the_queues_chat_for_a_sibling_run(temp_db):
    """The page files the chips under the viewed chat and cancels in the
    queue's own chat: the resume's snapshot of a task chat viewed through a
    sibling run's pump names that chat; a chat viewed through its own pump
    names none."""
    from ws.dashboard import DashboardConnection
    task_store.create_chat("task-run-a", "user-admin", "agent-x")
    task_store.create_chat("task-run-b", "user-admin", "agent-x")
    q = await input_queue.loaded("task-run-b")
    await q.add("q-1", "user-admin", TurnInput("later"), view_chat_id="task-run-a")
    conn = DashboardConnection.__new__(DashboardConnection)
    conn.chat_id = "task-run-a"

    class _Pump:
        chat_id = "task-run-b"
    frame = await conn._queue_snapshot(_Pump())
    assert frame == {"type": "queue_snapshot", "chat_id": "task-run-a", "queue_chat_id": "task-run-b",
                     "messages": [{"queue_id": "q-1", "text": "later", "author_sub": "user-admin",
                                   "view_chat_id": "task-run-a"}]}
    conn.chat_id = "task-run-b"
    assert "queue_chat_id" not in await conn._queue_snapshot(_Pump())
    assert "queue_chat_id" not in await conn._queue_snapshot()


def _recording_unregister(monkeypatch) -> list[str]:
    """The notify registration's removal, recorded and still done."""
    from services.notifications import notification_manager
    unregistered: list[str] = []
    real_unregister = notification_manager.unregister_user_connection

    def _unregister(user_sub, connection_id):
        unregistered.append(connection_id)
        real_unregister(user_sub, connection_id)
    monkeypatch.setattr(notification_manager, "unregister_user_connection", _unregister)
    return unregistered


def test_a_client_gone_during_the_connect_frames_ends_the_socket_cleanly(temp_db, monkeypatch):
    """The unread count is the first connect-time frame: a client that left
    before it ends the socket as every other disconnect does. The handler
    returns, the user's connection set is empty, the notify registration is
    gone."""
    _setup(monkeypatch)
    from ws.dashboard import ws_dashboard_handler
    unregistered = _recording_unregister(monkeypatch)

    class _GoneSocket(FakeDashboardWebSocket):
        async def send_json(self, data: dict) -> None:
            # Starlette's shape for a peer that closed: the transport's
            # OSError becomes a 1006 disconnect before the handler sees it.
            raise WebSocketDisconnect(code=1006)

    async def scenario():
        ws = _GoneSocket(session_cookie())
        await asyncio.wait_for(ws_dashboard_handler(ws), 5.0)
        assert ws.sent == []
        assert not _connections_by_user.get("user-admin")
        assert len(unregistered) == 1
    run_ws_scenario(scenario)


def test_a_store_error_in_the_unread_count_runs_the_cleanup(temp_db, monkeypatch, caplog):
    """A database error reading the unread count logs as a dashboard error
    and the socket's cleanup still runs."""
    import logging

    from storage.automation import notification_store
    _setup(monkeypatch)
    from ws.dashboard import ws_dashboard_handler
    unregistered = _recording_unregister(monkeypatch)

    class _StoreDown(Exception):
        pass

    def _boom(user_sub):
        raise _StoreDown("the store is down")
    monkeypatch.setattr(notification_store, "get_unread_count", _boom)

    async def scenario():
        ws = FakeDashboardWebSocket(session_cookie())
        with caplog.at_level(logging.ERROR, logger="otodock"):
            await asyncio.wait_for(ws_dashboard_handler(ws), 5.0)
        assert ws.sent == []
        assert "WebSocket dashboard error" in caplog.text
        assert not _connections_by_user.get("user-admin")
        assert len(unregistered) == 1
    run_ws_scenario(scenario)


# -- a send runs on the session of the chat its gates judged -------------------
#
# A socket that holds one chat's session and is then put on another chat holds
# no session of the second: its send starts or finds that chat's own session
# and never rides, or recycles, the first chat's, which the person may only be
# allowed to watch.

_VIEWER = {"sub": "user-viewer", "email": "viewer@test.com", "name": "Viewer User",
           "role": "member"}


def _watched_and_own(layer) -> tuple[str, str]:
    """A Shared-only agent's chat a contributor may watch, live, and that
    contributor's own chat on another agent."""
    import uuid

    from core.session.visibility import shared_chat_owner
    from tests.fixtures.ws_dashboard_harness import TEST_MODEL
    pool_agent = make_test_agent(collaborative=False, default_scope="agent")
    own_agent = make_test_agent()
    task_store.add_user_agent("user-viewer", pool_agent, "contributor", "test")
    task_store.add_user_agent("user-viewer", own_agent, "viewer", "test")
    set_username("user-viewer", "viewer")
    layer.alive.add("sid-watched")
    layer.turn_events = [CommonEvent(type=TEXT, data={"content": "ok"}),
                         CommonEvent(type=DONE, data={})]
    chats = []
    for agent, owner in ((pool_agent, shared_chat_owner(pool_agent)), (own_agent, "user-viewer")):
        cid = str(uuid.uuid4())
        task_store.create_chat(cid, owner, agent, "default", model=TEST_MODEL,
                               execution_path="claude-code-cli", source_type="chat")
        chats.append(cid)
    task_store.update_chat(chats[0], session_id="sid-watched")
    return chats[0], chats[1]


async def _watch(ws, watched: str) -> None:
    await drain_startup(ws)
    ws.client_send({"type": "resume_chat", "chat_id": watched})
    ready = await _until(ws, "warmup_ready")
    assert ready["session_id"] == "sid-watched"
    await _until(ws, "queue_snapshot")


async def _done_of(ws, chat_id: str) -> None:
    while (await _until(ws, "done")).get("chat_id") != chat_id:
        pass


def _never_the_watched_session(layer, watched: str, own: str) -> None:
    took = [sid for sid, _, _ in layer.messages]
    assert took and "sid-watched" not in took, took
    assert took == [task_store.get_chat(own)["session_id"]]
    # The watched chat's session is neither sent to nor closed.
    assert layer.closed_sessions == [] and "sid-watched" in layer.alive
    assert task_store.get_chat_messages(watched) == []


def test_a_send_after_joining_another_chats_warmup_runs_on_that_chat(temp_db, monkeypatch):
    from core.session import warmup_registry
    layer = FakeExecutionLayer()
    stub_dashboard_seams(monkeypatch, layer)
    watched, own = _watched_and_own(layer)

    async def scenario():
        async with dashboard_connection(session_cookie(**_VIEWER)) as ws:
            await _watch(ws, watched)
            # Another tab of the person warms their own chat.
            await warmup_registry.register(own, "user-viewer", "x")
            ws.client_send({"type": "resume_chat", "chat_id": own})
            while (await _until(ws, "chat_history")).get("chat_id") != own:
                pass
            # While it gets ready a message waits in the chat's queue.
            ws.client_send({"type": "chat", "chat_id": own, "text": "first"})
            await _done_of(ws, own)
            assert layer.messages == []
            # That warmup ends, and this socket adopted nothing of it.
            await warmup_registry.unregister(own)
            ws.client_send({"type": "chat", "chat_id": own, "text": "second"})
            await _done_of(ws, own)
    run_ws_scenario(scenario)
    _never_the_watched_session(layer, watched, own)


def test_a_send_after_a_first_message_to_a_moving_chat_runs_on_that_chat(temp_db, monkeypatch):
    from services.remote import chat_move
    layer = FakeExecutionLayer()
    stub_dashboard_seams(monkeypatch, layer)
    watched, own = _watched_and_own(layer)

    async def scenario():
        async with dashboard_connection(session_cookie(**_VIEWER)) as ws:
            await _watch(ws, watched)
            # A move of the person's own chat runs: it holds the chat.
            q = await input_queue.loaded(own)
            claim = q.try_claim()
            monkeypatch.setitem(chat_move._running, own, object())
            ws.client_send({"type": "warmup", "chat_id": own,
                            "agent": task_store.get_chat(own)["agent"], "text": "first"})
            # The first message waits in the chat's queue behind the move.
            await _wait_for(lambda: bool(q.of("user-viewer")))
            assert layer.messages == []
            monkeypatch.delitem(chat_move._running, own)
            q.release(claim, rearm=False)
            await _done_of(ws, own)
            ws.client_send({"type": "chat", "chat_id": own, "text": "second"})
            await _done_of(ws, own)
    run_ws_scenario(scenario)
    _never_the_watched_session(layer, watched, own)


def test_a_warmup_naming_another_chat_holds_no_session_until_its_own_is_ready(
        temp_db, monkeypatch):
    """A socket that watches a live chat and then warms the person's own chat
    by its id holds no session while that chat's own starts: a mode picked
    for the person's chat meanwhile never reaches the watched session."""
    from core.session.session_state import get_session_mode, set_session_mode
    layer = FakeExecutionLayer()
    stub_dashboard_seams(monkeypatch, layer)
    watched, own = _watched_and_own(layer)
    set_session_mode("sid-watched", "ask")
    seen: dict = {}

    async def scenario():
        async with dashboard_connection(session_cookie(**_VIEWER)) as ws:
            await _watch(ws, watched)
            layer.start_gate = asyncio.Event()
            ws.client_send({"type": "warmup", "chat_id": own,
                            "agent": task_store.get_chat(own)["agent"]})
            await _until(ws, "warmup_started")
            conn = _conn("user-viewer")
            seen["during"] = (conn.chat_id, conn.session_id)
            ws.client_send({"type": "mode_change", "chat_id": own, "mode": "autoReview"})
            await _until(ws, "mode_changed")
            seen["watched_mode"] = get_session_mode("sid-watched")
            layer.start_gate.set()
            await _until(ws, "warmup_ready")
    run_ws_scenario(scenario)
    assert seen["during"] == (own, None)
    assert seen["watched_mode"] == "ask"
    assert "sid-watched" not in [sid for sid, _ in layer.mode_changes]


def test_a_send_after_a_failed_warmup_runs_on_the_chats_own_session(temp_db, monkeypatch):
    """The same warmup, and the start of the person's own chat fails: the
    socket holds no session, and the next message starts that chat's own."""
    layer = FakeExecutionLayer()
    stub_dashboard_seams(monkeypatch, layer)
    watched, own = _watched_and_own(layer)
    real_start, failed = layer.start_session, []

    async def fail_once(sid, agent_cfg):
        if not failed:
            failed.append(sid)
            raise RuntimeError("the start failed")
        await real_start(sid, agent_cfg)

    async def scenario():
        async with dashboard_connection(session_cookie(**_VIEWER)) as ws:
            await _watch(ws, watched)
            monkeypatch.setattr(layer, "start_session", fail_once)
            ws.client_send({"type": "warmup", "chat_id": own,
                            "agent": task_store.get_chat(own)["agent"]})
            await _until(ws, "warmup_failed")
            conn = _conn("user-viewer")
            await _wait_for(lambda: conn._warmup_task is None or conn._warmup_task.done())
            assert (conn.chat_id, conn.session_id) == (own, None)
            ws.client_send({"type": "chat", "chat_id": own, "text": "second"})
            await _done_of(ws, own)
    run_ws_scenario(scenario)
    took = [sid for sid, _, _ in layer.messages]
    assert took == [task_store.get_chat(own)["session_id"]] and "sid-watched" not in took
    assert "sid-watched" not in layer.closed_sessions and "sid-watched" in layer.alive
    assert task_store.get_chat_messages(watched) == []


def test_a_resume_that_ends_early_holds_no_session_of_the_chat_it_left(temp_db, monkeypatch):
    """A resume binds the chat and may end before it reaches that chat's
    session: its layer cannot be resolved (the agent's machine is away), or
    the chat is a call's, read and never bound. The socket then holds no
    session."""
    import uuid

    import ws.dashboard as wsd
    from core.session import session_kind
    from core.session.visibility import PHONE_CHAT_OWNER
    from tests.fixtures.ws_dashboard_harness import TEST_MODEL
    layer = FakeExecutionLayer()
    stub_dashboard_seams(monkeypatch, layer)
    watched, own = _watched_and_own(layer)
    call_agent, call = make_test_agent(), str(uuid.uuid4())
    task_store.add_user_agent("user-viewer", call_agent, "manager", "test")
    task_store.create_chat(call, PHONE_CHAT_OWNER, call_agent, "default", model=TEST_MODEL,
                           execution_path="claude-code-cli",
                           source_type=session_kind.PHONE.source_type)
    real = wsd._role_and_layer

    def _machine_away(user_sub, agent, chat, **kw):
        if (chat or {}).get("id") == own:
            raise RuntimeError("the machine is offline")
        return real(user_sub, agent, chat, **kw)

    async def scenario():
        async with dashboard_connection(session_cookie(**_VIEWER)) as ws:
            await _watch(ws, watched)
            conn = _conn("user-viewer")
            monkeypatch.setattr(wsd, "_role_and_layer", _machine_away)
            ws.client_send({"type": "resume_chat", "chat_id": own})
            await _until(ws, "error")
            assert (conn.chat_id, conn.session_id) == (own, None)
            # Back on the watched chat, then a call's conversation.
            ws.client_send({"type": "resume_chat", "chat_id": watched})
            assert (await _until(ws, "warmup_ready"))["session_id"] == "sid-watched"
            ws.client_send({"type": "resume_chat", "chat_id": call})
            await _until(ws, "chat_view_only")
            await _until(ws, "chat_history")
            assert (conn.chat_id, conn.session_id) == (call, None)
    run_ws_scenario(scenario)
    assert layer.messages == [] and "sid-watched" in layer.alive


def test_a_socket_put_on_a_chat_lets_go_of_the_terminal_it_viewed(temp_db, monkeypatch):
    """``_onto_chat`` leaves the chat the socket was on whole: its terminal
    viewer is detached with its session."""
    from ws.dashboard import DashboardConnection
    layer = FakeExecutionLayer()
    stub_dashboard_seams(monkeypatch, layer)
    _watched, own = _watched_and_own(layer)
    conn = DashboardConnection.__new__(DashboardConnection)
    conn.user_sub, conn.user = "user-viewer", {"sub": "user-viewer", "role": "member"}
    conn.chat_id, conn.agent_name, conn.layer = "chat-term", "agent-x", layer
    conn.session_id = "sid-term"
    conn._pty_viewer_sid, conn._pty_listener, conn._pty_outbox = "sid-term", None, None
    asyncio.run(conn._onto_chat(task_store.get_chat(own)))
    assert conn._pty_viewer_sid is None
    assert (conn.chat_id, conn.session_id) == (own, None)


def test_a_session_reads_as_none_once_the_socket_is_on_another_chat():
    """The second layer, at the point of use: a session is the socket's only
    while it is on the chat it adopted that session on, whatever bound the
    socket to another chat."""
    from ws.dashboard import DashboardConnection
    conn = DashboardConnection.__new__(DashboardConnection)
    conn.chat_id = "chat-a"
    conn.session_id = "sid-a"
    assert conn.session_id == "sid-a"
    conn.chat_id = "chat-b"   # a binding with no reset beside it
    assert conn.session_id is None
    conn.session_id = "sid-b"
    assert conn.session_id == "sid-b"
    conn.session_id = None
    conn.chat_id = "chat-a"
    assert conn.session_id is None


def _session_reset(stmt) -> bool:
    """Whether a statement, or an ``if`` it is, sets ``self.session_id`` to None."""
    import ast
    for node in ast.walk(stmt):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            pairs = (zip(target.elts, node.value.elts, strict=True)
                     if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple)
                     else [(target, node.value)])
            for left, right in pairs:
                if (ast.unparse(left) == "self.session_id"
                        and isinstance(right, ast.Constant) and right.value is None):
                    return True
    return False


def _chat_bindings() -> list[tuple[str, int, bool]]:
    """Every assignment to ``self.chat_id`` in the socket's modules: the
    file, the line, and whether a session reset sits beside it (the
    statement itself, the one before or the one after)."""
    import ast
    from pathlib import Path
    found = []
    for path in sorted((Path(__file__).resolve().parents[2] / "ws").glob("dashboard*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block):
                    if not isinstance(stmt, ast.Assign):
                        continue
                    names = [ast.unparse(e) for t in stmt.targets
                             for e in (t.elts if isinstance(t, ast.Tuple) else [t])]
                    if "self.chat_id" in names:
                        beside = block[max(0, i - 1):i + 2]
                        found.append((path.name, stmt.lineno, any(map(_session_reset, beside))))
    return found


def test_every_chat_binding_of_the_socket_sits_beside_a_session_reset():
    """A socket put on a chat holds no session of the chat it was on: every
    place that binds ``self.chat_id`` resets ``self.session_id`` beside it,
    so a next place fails here."""
    bindings = _chat_bindings()
    assert {name for name, _, _ in bindings} == {
        "dashboard_chat_resume.py", "dashboard_chat_send.py", "dashboard_chat_stream.py",
        "dashboard_chat_support.py", "dashboard_warmup.py"}
    assert len(bindings) >= 7
    assert [(name, line) for name, line, beside in bindings if not beside] == []


def test_an_editor_never_drives_a_managers_chat_worker_that_writes_knowledge(temp_db, monkeypatch):
    """A chat-surface worker a manager delegated runs as the agent, in a
    chat of the agent's own with a run row. While its run holds the write on
    the agent's knowledge, an editor who opens that chat in the dashboard is
    refused at every frame that drives it, as the delegation's own door
    refuses them. A manager drives it, and a pool chat with no run row keeps
    the editor tier alone."""
    import uuid

    from core.session.visibility import KNOWLEDGE_RUN_REFUSED, shared_chat_owner
    from tests.fixtures.ws_dashboard_harness import TEST_MODEL
    from tests.session.test_ws_dashboard_resume_controls import _bare_connection
    layer = FakeExecutionLayer()
    layer.turn_events = [CommonEvent(type=TEXT, data={"content": "ok"}),
                         CommonEvent(type=DONE, data={})]
    stub_dashboard_seams(monkeypatch, layer)
    slug = make_test_agent()
    task_store.add_user_agent("user-viewer", slug, "editor", "test")
    task_store.add_user_agent("user-manager", slug, "manager", "test")
    worker, pool = str(uuid.uuid4()), str(uuid.uuid4())
    for cid in (worker, pool):
        task_store.create_chat(cid, shared_chat_owner(slug), slug, "auto", model=TEST_MODEL,
                               execution_path="claude-code-cli", source_type="chat")
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    task_store.create_run(run_id, "dyn-x", slug, "delegate", None, "go", "delegate", "agent",
                          "user-admin")
    task_store.update_run(run_id, status="completed", chat_id=worker, session_id="sid-worker")
    task_store.update_chat(worker, session_id="sid-worker")
    layer.alive.add("sid-worker")

    async def frames(sub: str, cid: str) -> list[dict]:
        conn = _bare_connection(cid, "sid-worker" if cid == worker else None,
                                user_sub=sub, agent=slug, layer=layer)
        conn.deferred_for, conn.deferred_model, conn._warmup_in_flight = "", "", False
        await conn._handle_mode_change({"mode": "dontAsk", "chat_id": cid})
        await conn._on_compact_context({})
        refusal = await conn._deny_task_continue(cid, quiet=True)
        return [*conn.sent, {"refusal": refusal}]

    sent = asyncio.run(frames("user-viewer", worker))
    assert sent[-1] == {"refusal": KNOWLEDGE_RUN_REFUSED}
    assert {"type": "error", "message": KNOWLEDGE_RUN_REFUSED} in sent
    assert task_store.get_chat(worker)["permission_mode"] == "auto"
    assert layer.mode_changes == []
    assert asyncio.run(frames("user-manager", worker))[-1] == {"refusal": ""}
    assert asyncio.run(frames("user-viewer", pool))[-1] == {"refusal": ""}
