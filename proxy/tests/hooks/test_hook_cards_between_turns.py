"""The PreToolUse hook's own cards when no turn runs.

A background sub-agent keeps calling tools after its chat's turn ended; a
call the hook asks a person about (Ask, a critical-tier tool, a plan
review, a question) raised its card onto the session's permission queue,
which only a turn's pump reads, so the card showed only inside the reply to
the person's NEXT message. With no live pump the card is shown at once
(``engine_prompts.offer_hook_card``): the frame to the chat's viewers, the
"needs your input" notice, the card found again by a viewer who opens the
chat, its row when it ends. With a live pump it goes onto the queue as
before. What the hook decides does not change.
"""

from __future__ import annotations

import asyncio

import pytest

from api.hooks import permission, routing
from auth.path_policy import SecurityContext
from core.events import chat_writer, stream_pump
from core.session import engine_prompts, session_state

SID = "sess-hook-cards"
CHAT = "chat-hook-cards"
TOOL = "mcp__demo__do_thing"


class _LivePump:
    """A registered pump reading the session's permission queue."""

    def __init__(self, queue, *, done: bool = False):
        self.perm_queue = queue
        self.session_id = SID
        self.chat_id = CHAT
        self.implementing_plan = ""
        self.is_done = done


@pytest.fixture
def seen(monkeypatch):
    """The session in Ask on a dashboard chat, its chat id resolved, and what
    the surface does recorded: frames, rows, notices."""
    from services.mcp import mcp_permissions
    from services.notifications import notification_manager
    from storage import database as task_store

    out: dict[str, list] = {"frames": [], "rows": [], "pings": []}
    session_state._sessions[SID] = {"client_type": "dashboard"}
    session_state._session_security[SID] = SecurityContext(
        role="admin", username="", agent="demo", is_admin_agent=True)
    session_state.set_session_mode(SID, "ask")
    monkeypatch.setattr(mcp_permissions, "resolve_tool_tier", lambda s, t: "standard")

    async def _chat_id(session_id):
        return CHAT
    monkeypatch.setattr(routing, "resolve_hook_chat_id", _chat_id)
    monkeypatch.setattr(session_state, "broadcast_chat_frame",
                        lambda chat_id, frame: out["frames"].append(frame))

    def _submit(chat_id, fn, *, label=""):
        out["rows"].append((label, fn.args, dict(fn.keywords)))
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut
    monkeypatch.setattr(chat_writer, "submit", _submit)
    monkeypatch.setattr(task_store, "get_chat",
                        lambda cid: {"id": cid, "user_sub": "u-1", "agent": "demo"})
    monkeypatch.setattr(task_store, "get_active_meeting_for_chat", lambda cid: None)
    monkeypatch.setattr(notification_manager, "agent_label", lambda slug: "Demo")

    async def _ping(user_sub, title, body, chat_id=None, **kw):
        out["pings"].append((user_sub, title, body, chat_id))
    monkeypatch.setattr(notification_manager, "fire_ephemeral", _ping)
    yield out
    for table in (session_state._sessions, session_state._session_modes,
                  session_state._session_security, session_state._session_tool_allows,
                  session_state._permission_emitters):
        table.pop(SID, None)
    engine_prompts._hook_cards.clear()


async def _card(seen, coro):
    """Start the hook's call and wait until its card is out."""
    task = asyncio.ensure_future(coro)
    for _ in range(200):
        if seen["frames"] or task.done():
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # the notice's spawn
    return task


def _rows(seen, label):
    return [json_of(kw) for lab, _a, kw in seen["rows"] if lab == label]


def json_of(kw: dict) -> dict:
    import json
    return json.loads(kw["event_data"])


def _forget_wait(request_id: str) -> None:
    session_state._permission_events.pop(request_id, None)
    session_state._permission_request_sessions.pop(request_id, None)
    session_state._session_permission_requests.pop(SID, None)


@pytest.mark.asyncio
async def test_an_ask_with_no_turn_running_shows_its_card_at_once(seen):
    queue = session_state.get_permission_queue(SID)
    task = await _card(seen, permission.decide_tool_permission(SID, TOOL, {"x": 1}))
    frame = seen["frames"][0]
    assert frame["type"] == "permission_prompt" and frame["chat_id"] == CHAT
    assert frame["tool_name"] == TOOL and frame["tool_input"] == {"x": 1}
    assert queue.empty()
    # A viewer who opens the chat while it waits is sent it (the resume's read).
    [p] = engine_prompts.pending_for_chat(CHAT)
    assert engine_prompts.card_frame(p) == frame
    assert seen["pings"] == [("u-1", "Demo needs your input", f"Allow {TOOL}?", CHAT)]
    # No row while it waits: a reload draws the waiting card from the re-send.
    assert seen["rows"] == []
    assert session_state.resolve_permission(frame["request_id"], True)
    assert (await asyncio.wait_for(task, 2))["decision"] == "allow"
    assert engine_prompts.pending_for_chat(CHAT) == []
    [row] = _rows(seen, "hook_card")
    assert row["request_id"] == frame["request_id"]
    assert row["resolved"] is True and row["approved"] is True


@pytest.mark.asyncio
async def test_a_refused_card_reads_refused_in_the_history(seen):
    task = await _card(seen, permission.decide_tool_permission(SID, TOOL, {}))
    rid = seen["frames"][0]["request_id"]
    assert session_state.resolve_permission(rid, False)
    assert (await asyncio.wait_for(task, 2))["decision"] == "deny"
    [row] = _rows(seen, "hook_card")
    assert row["approved"] is False


@pytest.mark.asyncio
async def test_with_a_turn_running_the_card_goes_onto_its_queue_as_before(seen, monkeypatch):
    queue = session_state.get_permission_queue(SID)
    monkeypatch.setitem(stream_pump._active_pumps, CHAT, _LivePump(queue))
    task = asyncio.ensure_future(permission.decide_tool_permission(SID, TOOL, {}))
    item = await asyncio.wait_for(queue.get(), 2)
    assert item["event_type"] == "permission_prompt"
    assert seen["frames"] == [] and engine_prompts.pending_for_chat(CHAT) == []
    assert session_state.resolve_permission(item["request_id"], True)
    assert (await asyncio.wait_for(task, 2))["decision"] == "allow"
    assert seen["rows"] == []


@pytest.mark.asyncio
async def test_a_pump_that_ended_or_reads_no_queue_does_not_count(seen, monkeypatch):
    # An ended pump, and a wake capture's pump (no permission queue).
    queue = session_state.get_permission_queue(SID)
    monkeypatch.setitem(stream_pump._active_pumps, CHAT, _LivePump(queue, done=True))
    monkeypatch.setitem(stream_pump._active_pumps, "chat-wake", _LivePump(None))
    task = await _card(seen, permission.decide_tool_permission(SID, TOOL, {}))
    assert seen["frames"] and queue.empty()
    session_state.resolve_permission(seen["frames"][0]["request_id"], True)
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_a_card_that_runs_out_is_retired_on_the_viewers(seen):
    queue = session_state.get_permission_queue(SID)
    task = await _card(seen, permission.decide_tool_permission(SID, TOOL, {}, wait_s=0.05))
    rid = seen["frames"][0]["request_id"]
    assert (await asyncio.wait_for(task, 2))["decision"] == "deny"
    assert seen["frames"][-1] == {"type": "prompt_retired", "chat_id": CHAT, "request_id": rid}
    assert queue.empty()
    [row] = _rows(seen, "hook_card")
    assert row["approved"] is False
    assert engine_prompts.pending_for_chat(CHAT) == []


@pytest.mark.asyncio
async def test_a_card_whose_caller_went_away_leaves_no_row(seen):
    task = await _card(seen, permission.decide_tool_permission(SID, TOOL, {}))
    rid = seen["frames"][0]["request_id"]
    task.cancel()   # the hook's stream dropped: the retried call raises its own
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen["frames"][-1] == {"type": "prompt_retired", "chat_id": CHAT, "request_id": rid}
    assert _rows(seen, "hook_card") == []


@pytest.mark.asyncio
async def test_a_card_a_pump_showed_is_retired_on_the_viewers_once_it_ended(seen, monkeypatch):
    queue = session_state.get_permission_queue(SID)
    pump = _LivePump(queue)
    monkeypatch.setitem(stream_pump._active_pumps, CHAT, pump)
    task = asyncio.ensure_future(permission.decide_tool_permission(SID, TOOL, {}, wait_s=0.2))
    item = await asyncio.wait_for(queue.get(), 2)
    # The pump showed it (the resume's copy) and ended before the wait did.
    monkeypatch.setitem(stream_pump._pending_permissions, SID, item)
    pump.is_done = True
    assert (await asyncio.wait_for(task, 2))["decision"] == "deny"
    assert queue.empty()
    assert seen["frames"] == [{"type": "prompt_retired", "chat_id": CHAT,
                               "request_id": item["request_id"]}]
    assert SID not in stream_pump._pending_permissions


@pytest.mark.asyncio
async def test_a_plan_review_with_no_turn_running(seen):
    session_state.set_session_mode(SID, "plan")
    task = await _card(seen, permission.decide_tool_permission(
        SID, "ExitPlanMode", {"plan": "# The plan", "planFilePath": "/p/x/plan-a.md"}))
    frame = seen["frames"][0]
    assert frame["type"] == "plan_review" and frame["filename"] == "plan-a.md"
    assert frame["plan"] == "# The plan"
    [(label, args, _kw)] = [r for r in seen["rows"] if r[0] == "chat_plan"]
    assert args == (CHAT, "plan-a.md", "# The plan")
    assert seen["pings"][0][2] == "Review the plan"
    [p] = engine_prompts.pending_for_chat(CHAT)
    assert engine_prompts.card_frame(p) == frame
    # The person chose Implement in Auto (the dashboard records the action
    # and moves the mode before it answers).
    engine_prompts.note_card_action(frame["request_id"], "implement_auto")
    session_state.set_session_mode(SID, "autoReview")
    assert session_state.resolve_permission(frame["request_id"], True)
    assert (await asyncio.wait_for(task, 2))["decision"] == "allow"
    [row] = _rows(seen, "hook_card")
    assert row["type"] == "plan_review" and row["action"] == "implement_auto"
    assert row["filename"] == "plan-a.md"


@pytest.mark.asyncio
async def test_a_question_with_no_turn_running(seen, monkeypatch):
    from core.session.session_manager import get_layer_by_path
    layer = get_layer_by_path("claude-code-cli")
    monkeypatch.setattr("core.session.session_manager.engine_layer_for_session", lambda sid: layer)
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: False)
    questions = {"questions": [{"question": "Which colour?", "options": [{"label": "Green"}]}]}
    out = await permission.decide_tool_permission(SID, "AskUserQuestion", questions)
    await asyncio.sleep(0.05)
    assert out["decision"] == "deny"   # as before: the next message is the answer
    frame = seen["frames"][0]
    assert frame == {"type": "question", "tool_name": "AskUserQuestion",
                     "tool_input": questions, "chat_id": CHAT}
    [row] = _rows(seen, "hook_question")
    assert row == {"type": "question", "tool_name": "AskUserQuestion", "tool_input": questions}
    assert seen["pings"][0][2] == "Waiting for your answer"
    assert session_state.get_permission_queue(SID).empty()


@pytest.mark.asyncio
async def test_a_terminal_tasks_question_is_written_once(seen, monkeypatch, tmp_path):
    """An unattended terminal (a task's interactive Claude) that asks: the
    hook denies the call so the turn goes on, and the terminal journals the
    call, so its tailer writes the question's row. The hook's card goes onto
    the session's queue, which the terminal's drainer forwards to a viewer
    without a row: one question row, not a second from the hook."""
    import json

    from core.session import transcript_tailer
    from core.session.session_manager import get_layer_by_path
    from storage import database as task_store
    layer = get_layer_by_path("claude-code-cli")
    monkeypatch.setattr("core.session.session_manager.engine_layer_for_session", lambda sid: layer)
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: True)
    session_state._sessions[SID] = {"client_type": "task"}
    questions = {"questions": [{"question": "Which colour?", "options": [{"label": "Green"}]}]}
    out = await permission.decide_tool_permission(SID, "AskUserQuestion", questions)
    await asyncio.sleep(0.05)
    assert out["decision"] == "deny"

    tailed: list[tuple[str, dict]] = []

    def _add(chat_id, role, content="", event_type="", event_data="", **kw):
        if role == "event":
            tailed.append((event_type, json.loads(event_data)))
    monkeypatch.setattr(task_store, "add_chat_message", _add)
    monkeypatch.setattr(task_store, "update_chat", lambda cid, **kw: None)
    monkeypatch.setattr(task_store, "set_chat_title_if_unset", lambda cid, title: True)
    monkeypatch.setattr(task_store, "rebuild_chat_search", lambda cid: None)
    journal = tmp_path / "transcript.jsonl"
    journal.write_text(json.dumps({"type": "assistant", "uuid": "a-q", "message": {
        "role": "assistant", "content": [
            {"type": "tool_use", "id": "t-q", "name": "AskUserQuestion", "input": questions}]}}) + "\n")
    try:
        transcript_tailer.tail_transcript(SID, CHAT, str(journal))
    finally:
        for table in (transcript_tailer._offsets, transcript_tailer._tool_events,
                      transcript_tailer._attach_ts):
            table.pop(SID, None)
    rows = _rows(seen, "hook_question") + [d for kind, d in tailed if kind == "question"]
    assert rows == [{"type": "question", "tool_name": "AskUserQuestion", "tool_input": questions}]
    assert session_state.get_permission_queue(SID).get_nowait() == {
        "event_type": "question", "tool_name": "AskUserQuestion", "tool_input": questions}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,journaled", [("request_user_input", True),
                                            ("request_user_input_async", False)])
async def test_a_codex_terminal_tasks_question_follows_what_its_tailer_writes(
        seen, monkeypatch, tool, journaled):
    """Codex's rollout tailer writes a ``request_user_input`` call as the
    question's row, so the hook's card goes onto the queue; the non-blocking
    ``request_user_input_async`` is a plain tool row there, so the hook's own
    card and row are the question's only ones."""
    from core.session.session_manager import get_layer_by_path
    layer = get_layer_by_path("codex-cli")
    monkeypatch.setattr("core.session.session_manager.engine_layer_for_session", lambda sid: layer)
    monkeypatch.setattr(permission, "_is_interactive_session", lambda sid: True)
    session_state._sessions[SID] = {"client_type": "task"}
    questions = {"questions": [{"id": "c", "question": "Which colour?", "options": [{"label": "Green"}]}]}
    out = await permission.decide_tool_permission(SID, tool, questions)
    await asyncio.sleep(0.05)
    assert out["decision"] == "deny"
    queue = session_state.get_permission_queue(SID)
    if journaled:
        assert _rows(seen, "hook_question") == [] and seen["frames"] == []
        assert queue.get_nowait()["tool_name"] == tool
    else:
        assert _rows(seen, "hook_question") == [
            {"type": "question", "tool_name": tool, "tool_input": questions}]
        assert seen["frames"][0]["type"] == "question" and queue.empty()


@pytest.mark.asyncio
async def test_no_notice_for_a_session_nobody_attends(seen):
    session_state._sessions[SID] = {"client_type": "task"}
    session_state.set_session_mode(SID, "ask")
    engine_prompts.offer_hook_card(
        session_state.get_permission_queue(SID),
        {"event_type": "permission_prompt", "request_id": "r-task", "tool_name": TOOL,
         "tool_input": {}}, session_id=SID, chat_id=CHAT)
    await asyncio.sleep(0.05)
    assert seen["frames"] and seen["pings"] == []


@pytest.mark.asyncio
async def test_a_paired_machines_session_shows_its_card_the_same_way(seen, monkeypatch):
    # The satellite's hook call comes through the tunnel to the same route
    # and queue: nothing differs for the card.
    from core.placement import KIND_ADMIN_REMOTE, PlacementCapabilities
    from services import path_policy_v2
    monkeypatch.setattr(path_policy_v2, "check_target_still_valid", lambda ctx: None)
    session_state._session_security[SID] = SecurityContext(
        role="admin", username="", agent="demo", is_admin_agent=True,
        placement=PlacementCapabilities(kind=KIND_ADMIN_REMOTE, machine_id="m-1",
                                        home_dir="/home/u", allow_full_fs=True))
    task = await _card(seen, permission.decide_tool_permission(SID, TOOL, {}))
    assert seen["frames"][0]["type"] == "permission_prompt"
    assert [p.request_id for p in engine_prompts.pending_for_chat(CHAT)] == [
        seen["frames"][0]["request_id"]]
    session_state.resolve_permission(seen["frames"][0]["request_id"], True)
    assert (await asyncio.wait_for(task, 2))["decision"] == "allow"


@pytest.mark.asyncio
async def test_a_card_put_after_the_pumps_final_drain_is_handed_off(seen):
    """The pump looked live when the hook put its card, then ended without
    draining again: its end shows the card between turns and leaves every
    other item queued for the next pump."""
    queue = session_state.get_permission_queue(SID)
    other = {"event_type": "image_gallery", "images": []}
    queue.put_nowait(other)
    session_state.open_permission_wait("r-late", SID)
    queue.put_nowait({"event_type": "permission_prompt", "request_id": "r-late",
                      "tool_name": TOOL, "tool_input": {}})
    try:
        engine_prompts.hand_off_hook_cards(queue, session_id=SID, chat_id=CHAT)
        await asyncio.sleep(0.05)
        assert seen["frames"][0]["request_id"] == "r-late"
        assert queue.get_nowait() == other and queue.empty()
    finally:
        _forget_wait("r-late")


@pytest.mark.asyncio
async def test_the_hand_off_leaves_a_card_nobody_waits_on_queued(seen):
    """A card whose hook was released before the pump ended is not shown
    dead: it stays queued in order with its retire, as before."""
    queue = session_state.get_permission_queue(SID)
    dead = {"event_type": "permission_prompt", "request_id": "r-dead", "tool_name": TOOL,
            "tool_input": {}}
    retired = {"event_type": "prompt_retired", "request_id": "r-dead"}
    live = {**dead, "request_id": "r-live"}
    session_state.open_permission_wait("r-live", SID)
    for item in (dead, retired, live):
        queue.put_nowait(item)
    try:
        engine_prompts.hand_off_hook_cards(queue, session_id=SID, chat_id=CHAT)
        assert [f["request_id"] for f in seen["frames"]] == ["r-live"]
        assert queue.get_nowait() is dead and queue.get_nowait() is retired and queue.empty()
        assert [p.request_id for p in engine_prompts.pending_for_chat(CHAT)] == ["r-live"]
    finally:
        _forget_wait("r-live")


@pytest.mark.asyncio
async def test_a_chat_read_that_fails_leaves_the_card_on_its_queue(seen, monkeypatch):
    """The card's chat cannot be read now: the card waits on the queue as
    before, never a deny the person was not asked about."""
    async def _down(session_id):
        raise RuntimeError("no database")
    monkeypatch.setattr(routing, "resolve_hook_chat_id", _down)
    queue = session_state.get_permission_queue(SID)
    task = asyncio.ensure_future(permission.decide_tool_permission(SID, TOOL, {}))
    item = await asyncio.wait_for(queue.get(), 2)
    assert seen["frames"] == [] and item["tool_name"] == TOOL
    assert session_state.resolve_permission(item["request_id"], True)
    assert (await asyncio.wait_for(task, 2))["decision"] == "allow"


@pytest.mark.asyncio
async def test_a_card_whose_end_fails_keeps_the_persons_answer(seen, monkeypatch):
    """Ending the card (its row, its retire) raises: the hook still answers
    with the person's Allow."""
    def _broken(*a, **kw):
        raise RuntimeError("the row was not written")
    monkeypatch.setattr(engine_prompts, "end_hook_card", _broken)
    task = await _card(seen, permission.decide_tool_permission(SID, TOOL, {}))
    session_state.resolve_permission(seen["frames"][0]["request_id"], True)
    assert (await asyncio.wait_for(task, 2))["decision"] == "allow"


@pytest.mark.asyncio
async def test_a_hand_off_that_fails_still_ends_the_pump(temp_db, monkeypatch):
    """The hand-off raises at the pump's end: the queues still close and the
    turn ends for its viewers."""
    from core.events.common_events import PRODUCER_DONE, TEXT, CommonEvent

    def _broken(*a, **kw):
        raise RuntimeError("the card was not shown")
    monkeypatch.setattr(engine_prompts, "hand_off_hook_cards", _broken)
    temp_db.create_chat("hand-off-1", "user-admin", "a1")
    events: asyncio.Queue = asyncio.Queue()
    producer = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
    pump = stream_pump.ChatStreamPump(chat_id="hand-off-1", session_id="sess-hand-off-1",
                                      producer=producer, event_queue=events,
                                      perm_queue=asyncio.Queue())
    stream_pump._active_pumps["hand-off-1"] = pump
    viewer = pump.attach(bounded=True)
    await events.put(CommonEvent(type=TEXT, data={"content": "done"}))
    await events.put(CommonEvent(type=PRODUCER_DONE, data={}))
    try:
        await pump._run()
        kinds = []
        while not viewer.empty():
            kinds.append(viewer.get_nowait()["pump_type"])
        assert pump.queue_closed and kinds[-1] == "pump_ended"
    finally:
        stream_pump._active_pumps.pop("hand-off-1", None)
        stream_pump._chat_streaming_state.pop("hand-off-1", None)


@pytest.mark.asyncio
async def test_the_answer_between_turns_reaches_the_hook(seen, monkeypatch):
    """The dashboard's answer with no stream resolves the hook's wait by its
    request id and closes the card on every viewer."""
    from ws.dashboard_prompts import PromptController

    class _Conn(PromptController):
        pass
    conn = _Conn.__new__(_Conn)
    conn.session_id = SID
    conn.chat_id = CHAT

    async def _may(rid):
        return True
    conn._may_resolve_permission = _may
    task = await _card(seen, permission.decide_tool_permission(SID, TOOL, {}))
    rid = seen["frames"][0]["request_id"]
    monkeypatch.setattr("ws.dashboard_prompts.broadcast_chat_frame",
                        lambda chat_id, frame: seen["frames"].append(frame))
    await conn._on_permission_response({"request_id": rid, "approved": True})
    assert (await asyncio.wait_for(task, 2))["decision"] == "allow"
    assert {"type": "permission_resolved", "request_id": rid, "approved": True,
            "chat_id": CHAT} in seen["frames"]
    [row] = _rows(seen, "hook_card")
    assert row["approved"] is True
