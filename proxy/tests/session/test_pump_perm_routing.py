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


def _mk_pump(chat_id: str, **kw) -> ChatStreamPump:
    producer = asyncio.get_event_loop().create_task(asyncio.sleep(3600))
    pump = ChatStreamPump(
        chat_id=chat_id,
        session_id=f"sess-{chat_id}",
        producer=producer,
        event_queue=asyncio.Queue(),
        perm_queue=None,
        **kw,
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


# ── The "needs your input" ping for a shown card ────────────────────────────


@pytest.fixture
def pings(monkeypatch):
    """The pings fired, and the prompt waits the test opened, cleared after."""
    from core.session import session_state
    fired: list[dict] = []

    async def _fake_ephemeral(user_sub, title, body, chat_id=None, **kw):
        fired.append({"user_sub": user_sub, "title": title, "body": body, "chat_id": chat_id,
                      "mid_turn": kw.get("mid_turn", False)})

    monkeypatch.setattr(stream_pump.notification_manager, "fire_ephemeral", _fake_ephemeral)
    maps = (session_state._permission_events, session_state._permission_decisions,
            session_state._session_permission_requests, session_state._permission_request_sessions,
            session_state._question_events)
    yield fired
    for m in maps:
        m.clear()


def _wait(pump, rid: str) -> None:
    """The prompt's wait is open, as the hook receiver opens it before the
    pump reads the prompt."""
    from core.session import session_state
    session_state.open_permission_wait(rid, pump.session_id)


def _end_wait(rid: str) -> None:
    from core.session import session_state
    session_state._permission_events.pop(rid, None)


async def _settle() -> None:
    while stream_pump._pings:
        await asyncio.gather(*list(stream_pump._pings))


def _browser(rid: str) -> dict:
    return {"event_type": "permission_prompt", "request_id": rid,
            "tool_name": "mcp__platform__browser_navigate",
            "tool_input": {"url": "https://example.com/?token=s3cret"}}


@pytest.mark.asyncio
async def test_a_shown_card_pings_once_and_a_buffered_one_when_it_shows(temp_db, pings):
    temp_db.create_chat("np1", "user-admin", "a1")
    pump = _mk_pump("np1")
    try:
        for rid in ("req-a", "req-b"):
            _wait(pump, rid)
        await pump._handle_perm_event(_browser("req-a"))
        await pump._handle_perm_event(_perm_item("req-b"))
        await _settle()
        assert len(pings) == 1
        assert pings[0]["user_sub"] == "user-admin" and pings[0]["chat_id"] == "np1"
        # Mid-turn: the origin socket is streaming this very chat.
        assert pings[0]["mid_turn"] is True
        assert pings[0]["title"].endswith("needs your input")
        # The tool, never its input: the body rides the push to the phone.
        assert pings[0]["body"] == "Allow mcp__platform__browser_navigate?"
        _end_wait("req-a")
        await pump.resolve_active_permission()
        await _settle()
        assert [p["body"] for p in pings] == ["Allow mcp__platform__browser_navigate?",
                                              "Allow Bash?"]
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_the_second_wave_card_is_forwarded_live_and_pings(temp_db, pings, monkeypatch):
    """The batched turn of the finding: three cards shown one by one as they
    are answered, then the call the engine ran after them raises a fourth
    card once the third answer freed the slot."""
    import ws.dashboard  # noqa: F401  (assembles the connection's mixins)
    from ws.dashboard import DashboardConnection
    from ws.dashboard_dispatch import StreamCtx
    from core.session import session_state

    temp_db.create_chat("np2", "user-admin", "a1")
    pump = _mk_pump("np2")
    conn = DashboardConnection.__new__(DashboardConnection)

    async def _may(_rid):
        return True
    monkeypatch.setattr(conn, "_may_resolve_permission", _may, raising=False)
    stream = StreamCtx(pump, pump._ws_queues[0], "np2")
    first = ("req-me", "req-issues", "req-query")
    waiters = [asyncio.ensure_future(session_state.wait_for_permission(r, pump.session_id, 30))
               for r in (*first, "req-browser")]
    pump._init_live_state()
    try:
        await asyncio.sleep(0)
        for rid in first:
            await pump._handle_perm_event(_perm_item(rid))
        await _settle()
        for rid in first:
            await conn._on_permission_response({"request_id": rid, "approved": True}, stream=stream)
            await _settle()
            await asyncio.sleep(0)
        assert pump._permission_active is None
        _frames(pump)
        await pump._handle_perm_event(_browser("req-browser"))
        shown = [f["perm_data"]["request_id"] for f in _frames(pump)
                 if f.get("pump_type") == "perm_permission_prompt"]
        assert shown == ["req-browser"]
        assert _pending_permissions[pump.session_id]["request_id"] == "req-browser"
        live = stream_pump._chat_streaming_state["np2"]
        assert live["pending_permission"]["request_id"] == "req-browser"
        await _settle()
        assert len(pings) == 4
        assert pings[-1]["body"] == "Allow mcp__platform__browser_navigate?"
    finally:
        for w in waiters:
            w.cancel()
        stream_pump._chat_streaming_state.pop("np2", None)
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("pump_kw,extra", [
    ({"source_type": "task"}, {}),
    ({"source_type": "meeting"}, {}),
    ({}, {"meeting_agent": "helper"}),
])
async def test_task_and_meeting_turns_never_ping(temp_db, pings, pump_kw, extra):
    temp_db.create_chat("np3", "user-admin", "a1")
    pump = _mk_pump("np3", **pump_kw)
    try:
        _wait(pump, "req-x")
        await pump._handle_perm_event({**_perm_item("req-x"), **extra})
        await _settle()
        assert pump._permission_active["request_id"] == "req-x"
        assert pings == []
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_chat_in_a_meeting_never_pings(temp_db, pings, monkeypatch):
    temp_db.create_chat("np4", "user-admin", "a1")
    monkeypatch.setattr(stream_pump.task_store, "get_active_meeting_for_chat",
                        lambda _cid: {"id": "mtg-1"})
    pump = _mk_pump("np4")
    try:
        _wait(pump, "req-x")
        await pump._handle_perm_event(_perm_item("req-x"))
        await _settle()
        assert pings == []
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_cards_whose_wait_ended_never_ping(temp_db, pings):
    """A dropped connection ends several waits at once and queues their
    retires in a row: retiring the active card shows the next for a
    moment, and that card must not ping. Nor one answered while its chat
    row was read."""
    temp_db.create_chat("np5", "user-admin", "a1")
    pump = _mk_pump("np5")
    try:
        _wait(pump, "req-a")
        await pump._handle_perm_event(_browser("req-a"))
        await pump._handle_perm_event(_perm_item("req-b"))
        await pump._handle_perm_event({**_perm_item("req-c"), "tool_input": {"command": "pwd"}})
        await _settle()
        assert len(pings) == 1
        _end_wait("req-a")
        for rid in ("req-a", "req-b", "req-c"):
            await pump._handle_perm_event({"event_type": "prompt_retired", "request_id": rid,
                                           "caller_gone": True})
        await _settle()
        assert len(pings) == 1 and pump._permission_active is None
        # Only the card that pinged waits for its retry; the stale ones left
        # nothing behind to quiet a later card.
        assert list(pump._retried) == [stream_pump._call_key(_browser("req-a"))]
        _wait(pump, "req-d")
        await pump._handle_perm_event(_perm_item("req-d"))
        _end_wait("req-d")  # answered before the ping's read came back
        await _settle()
        assert len(pings) == 1
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


async def _retire_gone(pump, rid: str) -> None:
    _end_wait(rid)
    await pump._handle_perm_event({"event_type": "prompt_retired", "request_id": rid,
                                   "caller_gone": True})


@pytest.mark.asyncio
async def test_a_hook_call_retried_after_its_connection_dropped_pings_once(temp_db, pings):
    temp_db.create_chat("np6", "user-admin", "a1")
    pump = _mk_pump("np6")
    try:
        _wait(pump, "req-1")
        await pump._handle_perm_event(_browser("req-1"))
        await _settle()
        await _retire_gone(pump, "req-1")
        _wait(pump, "req-2")
        await pump._handle_perm_event(_browser("req-2"))
        await _settle()
        # The quiet retry is itself retried: still quiet.
        await _retire_gone(pump, "req-2")
        _wait(pump, "req-3")
        await pump._handle_perm_event(_browser("req-3"))
        await _settle()
        assert len(pings) == 1
        assert pump._permission_active["request_id"] == "req-3"
        # Answered: the same call made again later is a new request.
        _end_wait("req-3")
        await pump.resolve_active_permission()
        _wait(pump, "req-4")
        await pump._handle_perm_event(_browser("req-4"))
        await _settle()
        assert len(pings) == 2
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_retry_that_arrives_before_the_retire_pings_once(temp_db, pings):
    temp_db.create_chat("np7", "user-admin", "a1")
    pump = _mk_pump("np7")
    try:
        _wait(pump, "req-1")
        await pump._handle_perm_event(_browser("req-1"))
        await _settle()
        _wait(pump, "req-2")
        await pump._handle_perm_event(_browser("req-2"))   # buffered behind the old card
        await _retire_gone(pump, "req-1")
        await _settle()
        assert pump._permission_active["request_id"] == "req-2"
        assert len(pings) == 1
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_retire_with_no_retry_in_time_lets_the_next_call_ping(temp_db, pings):
    temp_db.create_chat("np8", "user-admin", "a1")
    pump = _mk_pump("np8")
    try:
        _wait(pump, "req-1")
        await pump._handle_perm_event(_browser("req-1"))
        await _settle()
        await _retire_gone(pump, "req-1")
        (key,) = pump._retried
        pump._retried[key] -= stream_pump._RETRY_QUIET_S + 1
        _wait(pump, "req-2")
        await pump._handle_perm_event(_browser("req-2"))
        await _settle()
        assert len(pings) == 2
        assert pump._retried == {}
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_question_card_pings_with_its_question_and_once_across_a_retry(temp_db, pings):
    from core.session import session_state
    temp_db.create_chat("np9", "user-admin", "a1")
    pump = _mk_pump("np9")
    try:
        session_state._question_events["req-q1"] = asyncio.Event()
        await pump._handle_perm_event(_question_item("req-q1"))
        await _settle()
        session_state._question_events.pop("req-q1")
        await pump._handle_perm_event({"event_type": "prompt_retired", "request_id": "req-q1",
                                       "caller_gone": True})
        session_state._question_events["req-q2"] = asyncio.Event()
        await pump._handle_perm_event(_question_item("req-q2"))
        await _settle()
        assert [p["body"] for p in pings] == ["Delete the keypair file?"]
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_an_echo_turn_pings_only_while_someone_watches(temp_db, pings):
    """The delegate echo's watchdog tells the person about a card nobody
    watches; with a viewer attached the pump pings as any chat does."""
    temp_db.create_chat("np10", "user-admin", "a1")
    pump = _mk_pump("np10", driver_tells_parked=True)
    try:
        _wait(pump, "req-seen")
        await pump._handle_perm_event(_perm_item("req-seen"))
        await _settle()
        assert len(pings) == 1
        pump.detach(pump._ws_queues[0])
        _end_wait("req-seen")
        await pump.resolve_active_permission()
        _wait(pump, "req-unseen")
        await pump._handle_perm_event({**_perm_item("req-unseen"), "tool_input": {"command": "pwd"}})
        await _settle()
        assert len(pings) == 1
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("retire_first", [True, False])
async def test_a_ping_that_never_went_out_leaves_the_retry_to_ping(temp_db, pings, retire_first):
    """The caller went away while the chat row was read: nothing reached
    the person, so the call's retry pings, whichever of the two the pump
    handled first."""
    temp_db.create_chat("np11", "user-admin", "a1")
    pump = _mk_pump("np11")
    try:
        _wait(pump, "req-1")
        await pump._handle_perm_event(_browser("req-1"))
        if retire_first:
            await _retire_gone(pump, "req-1")
            await _settle()
        else:
            _end_wait("req-1")
            await _settle()
            await pump._handle_perm_event({"event_type": "prompt_retired", "request_id": "req-1",
                                           "caller_gone": True})
        assert pings == [] and pump._retried == {}
        _wait(pump, "req-2")
        await pump._handle_perm_event(_browser("req-2"))
        await _settle()
        assert len(pings) == 1
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_the_card_never_waits_on_the_chat_read(temp_db, pings, monkeypatch):
    temp_db.create_chat("np12", "user-admin", "a1")
    gate = asyncio.Event()
    real_run_db = stream_pump.run_db

    async def _slow_run_db(fn, *a, **kw):
        await gate.wait()
        return await real_run_db(fn, *a, **kw)
    monkeypatch.setattr(stream_pump, "run_db", _slow_run_db)
    pump = _mk_pump("np12")
    try:
        _wait(pump, "req-x")
        await asyncio.wait_for(pump._handle_perm_event(_perm_item("req-x")), 1)
        shown = [f["perm_data"]["request_id"] for f in _frames(pump)
                 if f.get("pump_type") == "perm_permission_prompt"]
        assert shown == ["req-x"] and pings == []
        gate.set()
        await _settle()
        assert len(pings) == 1
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()


@pytest.mark.asyncio
async def test_a_plan_review_neither_pings_nor_takes_a_retry_entry(temp_db, pings):
    temp_db.create_chat("np13", "user-admin", "a1")
    pump = _mk_pump("np13")
    review = {"event_type": "plan_review", "request_id": "req-plan", "plan": "the plan",
              "tool_input": {"planFilePath": "/p/plan.md"}}
    try:
        _wait(pump, "req-1")
        await pump._handle_perm_event(_browser("req-1"))
        await _settle()
        await _retire_gone(pump, "req-1")
        _wait(pump, "req-plan")
        await pump._handle_perm_event(review)
        await _settle()
        assert len(pings) == 1
        assert list(pump._retried) == [stream_pump._call_key(_browser("req-1"))]
    finally:
        _pending_permissions.pop(pump.session_id, None)
        pump.producer.cancel()
