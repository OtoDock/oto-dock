"""Perm-queue routing on the pump: blocking prompt types must never drop.

Codex ``request_user_input`` holds the daemon turn open until the dashboard
answers, so a ``question_prompt`` that never reaches the blocking-prompt gate
hangs the turn forever (no card, no turn-end, mid-turn messages undeliverable
until Stop). Regression for the missing ``_handle_perm_event`` branch.

Run: env TEST_DATABASE_URL=... venv/bin/python -m pytest tests/session/test_pump_perm_routing.py -q
"""

import asyncio

import pytest

from core.events import stream_pump
from core.events.stream_pump import ChatStreamPump, _pending_permissions


def _mk_pump(chat_id: str) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    pump = ChatStreamPump(
        chat_id=chat_id,
        session_id=f"sess-{chat_id}",
        producer=producer,
        event_queue=asyncio.Queue(),
        perm_queue=None,
    )
    pump.attach()  # a viewer is watching — prompts surface instead of parking
    return pump


def _question_item(request_id: str) -> dict:
    return {
        "event_type": "question_prompt",
        "request_id": request_id,
        "tool_name": "request_user_input",
        "tool_input": {"questions": [{"question": "Delete the keypair file?"}]},
    }


@pytest.mark.asyncio
async def test_question_prompt_reaches_blocking_gate(temp_db, monkeypatch):
    temp_db.create_chat("qp1", "user-admin", "a1")
    fired: list[dict] = []

    async def _fake_ephemeral(*a, **kw):
        fired.append(kw)

    monkeypatch.setattr(
        stream_pump.notification_manager, "fire_ephemeral", _fake_ephemeral
    )
    pump = _mk_pump("qp1")
    try:
        await pump._handle_perm_event(_question_item("req-q1"))
        # The question is the ACTIVE blocking prompt, stored for reconnect
        # replay, and forwarded as the question card frame.
        assert pump._permission_active["request_id"] == "req-q1"
        assert _pending_permissions[pump.session_id]["request_id"] == "req-q1"
        frame = pump._ws_queues[0].get_nowait()
        assert frame["pump_type"] == "perm_question_prompt"
        assert frame["perm_data"]["request_id"] == "req-q1"
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_question_prompt_respects_one_prompt_gate(temp_db, monkeypatch):
    temp_db.create_chat("qp2", "user-admin", "a1")

    async def _fake_ephemeral(*a, **kw):
        pass

    monkeypatch.setattr(
        stream_pump.notification_manager, "fire_ephemeral", _fake_ephemeral
    )
    pump = _mk_pump("qp2")
    try:
        await pump._handle_perm_event(
            {"event_type": "permission_prompt", "request_id": "req-p1",
             "tool_name": "Bash", "tool_input": {"command": "rm x"}}
        )
        await pump._handle_perm_event(_question_item("req-q2"))
        # Permission stays active; the question buffers behind it...
        assert pump._permission_active["request_id"] == "req-p1"
        assert [p["request_id"] for p in pump._permission_buffer] == ["req-q2"]
        # ...and advances into the active slot once the permission resolves.
        await pump.resolve_active_permission()
        assert pump._permission_active["request_id"] == "req-q2"
        assert _pending_permissions[pump.session_id]["request_id"] == "req-q2"
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


def _perm_item(request_id: str) -> dict:
    return {"event_type": "permission_prompt", "request_id": request_id,
            "tool_name": "Bash", "tool_input": {"command": "ls"}}


def _frames(pump) -> list[dict]:
    out = []
    while not pump._ws_queues[0].empty():
        out.append(pump._ws_queues[0].get_nowait())
    return out


@pytest.mark.asyncio
async def test_a_retired_prompt_leaves_the_slot_and_its_card_goes(temp_db, monkeypatch):
    """(g): the active prompt's wait ended with no answer: the pump frees
    the slot (the next buffered prompt shows), forgets it for reconnects
    and tells the viewers to drop its card."""
    temp_db.create_chat("rt1", "user-admin", "a1")
    pump = _mk_pump("rt1")
    try:
        await pump._handle_perm_event(_perm_item("req-dead"))
        await pump._handle_perm_event(_perm_item("req-retry"))
        _frames(pump)
        await pump._handle_perm_event({"event_type": "prompt_retired", "request_id": "req-dead"})
        frames = _frames(pump)
        assert {"pump_type": "ws_event",
                "event": {"type": "prompt_retired", "request_id": "req-dead"}} in frames
        assert pump._permission_active["request_id"] == "req-retry"
        assert _pending_permissions[pump.session_id]["request_id"] == "req-retry"
        shown = [f["perm_data"]["request_id"] for f in frames
                 if f.get("pump_type") == "perm_permission_prompt"]
        assert shown == ["req-retry"]
        # The last one retired: nothing is pending for a reconnect.
        await pump._handle_perm_event({"event_type": "prompt_retired", "request_id": "req-retry"})
        assert pump._permission_active is None
        assert pump.session_id not in _pending_permissions
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_retired_prompt_still_buffered_never_shows(temp_db, monkeypatch):
    temp_db.create_chat("rt2", "user-admin", "a1")
    pump = _mk_pump("rt2")
    try:
        await pump._handle_perm_event(_perm_item("req-shown"))
        await pump._handle_perm_event(_perm_item("req-gone"))
        await pump._handle_perm_event({"event_type": "prompt_retired", "request_id": "req-gone"})
        assert pump._permission_active["request_id"] == "req-shown"
        assert pump._permission_buffer == []
        await pump.resolve_active_permission()
        assert pump._permission_active is None
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_an_answer_to_a_retired_card_leaves_the_retry_in_the_slot(temp_db, monkeypatch):
    """(g): a click on the dead card that was in flight when the retire
    freed the slot resolves nothing and leaves the retry's prompt active
    (still re-presented on a reconnect); an answer to the retry moves on."""
    import ws.dashboard  # noqa: F401  (assembles the connection's mixins)
    from ws.dashboard import DashboardConnection
    from ws.dashboard_dispatch import StreamCtx
    from core.session import session_state

    temp_db.create_chat("rt3", "user-admin", "a1")
    pump = _mk_pump("rt3")
    conn = DashboardConnection.__new__(DashboardConnection)

    async def _may(_rid):
        return True
    monkeypatch.setattr(conn, "_may_resolve_permission", _may, raising=False)
    stream = StreamCtx(pump, pump._ws_queues[0], "rt3")
    waiter = asyncio.ensure_future(session_state.wait_for_permission("req-retry", pump.session_id, 5))
    try:
        await pump._handle_perm_event(_perm_item("req-dead"))
        await pump._handle_perm_event(_perm_item("req-retry"))
        await pump._handle_perm_event({"event_type": "prompt_retired", "request_id": "req-dead"})
        assert pump._permission_active["request_id"] == "req-retry"
        await conn._on_permission_response({"request_id": "req-dead", "approved": True}, stream=stream)
        assert pump._permission_active["request_id"] == "req-retry"
        assert _pending_permissions[pump.session_id]["request_id"] == "req-retry"
        await asyncio.sleep(0)
        await conn._on_permission_response({"request_id": "req-retry", "approved": True}, stream=stream)
        assert await asyncio.wait_for(waiter, 1) is True
        assert pump._permission_active is None
    finally:
        waiter.cancel()
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_an_answer_to_a_retired_plan_review_leaves_the_retry_in_the_slot(temp_db, monkeypatch):
    """(g): a plan review answer for a card already retired resolves nothing
    and leaves the retried call's review holding the slot; the answer to the
    retry moves on."""
    import ws.dashboard  # noqa: F401  (assembles the connection's mixins)
    from ws.dashboard import DashboardConnection
    from ws.dashboard_dispatch import StreamCtx
    from core.session import session_state

    temp_db.create_chat("rt5", "user-admin", "a1")
    pump = _mk_pump("rt5")
    conn = DashboardConnection.__new__(DashboardConnection)
    conn.chat_id = "rt5"

    async def _may(_rid):
        return True
    monkeypatch.setattr(conn, "_may_resolve_permission", _may, raising=False)
    stream = StreamCtx(pump, pump._ws_queues[0], "rt5")

    def _review(rid):
        return {"event_type": "plan_review", "request_id": rid, "plan": "the plan",
                "tool_input": {"planFilePath": "/p/plan.md"}}
    waiter = asyncio.ensure_future(session_state.wait_for_permission("req-retry", pump.session_id, 5))
    try:
        await pump._handle_perm_event(_review("req-dead"))
        await pump._handle_perm_event(_review("req-retry"))
        await pump._handle_perm_event({"event_type": "prompt_retired", "request_id": "req-dead",
                                       "caller_gone": True})
        assert pump._permission_active["request_id"] == "req-retry"
        answer = {"request_id": "req-dead", "action": "edit", "filename": "plan.md"}
        await conn._on_plan_review_response(answer, stream=stream)
        assert pump._permission_active["request_id"] == "req-retry"
        assert _pending_permissions[pump.session_id]["request_id"] == "req-retry"
        await asyncio.sleep(0)
        await conn._on_plan_review_response({**answer, "request_id": "req-retry"}, stream=stream)
        assert await asyncio.wait_for(waiter, 1) is False  # "edit" keeps plan mode
        assert pump._permission_active is None
    finally:
        waiter.cancel()
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_plan_review_stays_in_the_turn_unless_its_caller_went_away(temp_db, monkeypatch):
    """(g): a plan review released by a Stop or a close, or out of time,
    stays the transcript's record; one whose caller went away leaves the
    turn's blocks (the retried call raises its own)."""
    temp_db.create_chat("rt4", "user-admin", "a1")
    pump = _mk_pump("rt4")

    def _review(rid):
        return {"event_type": "plan_review", "request_id": rid, "plan": "the plan",
                "tool_input": {"planFilePath": "/p/plan.md"}}
    try:
        await pump._handle_perm_event(_review("req-released"))
        await pump._handle_perm_event({"event_type": "prompt_retired", "request_id": "req-released"})
        await pump._handle_perm_event(_review("req-gone"))
        await pump._handle_perm_event({"event_type": "prompt_retired", "request_id": "req-gone",
                                       "caller_gone": True})
        kept = [b["request_id"] for b in pump._turn_blocks if b.get("type") == "plan_review"]
        assert kept == ["req-released"]
        assert pump._permission_active is None
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()
