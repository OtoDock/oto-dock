"""A turn that ends typed, through the REAL dashboard WS handler: the socket
gets ``error`` with the reason THEN ``done`` once the rows landed, the chat
keeps a ``turn_ended`` row where the card belongs, the abort flags follow
the reason, a next ``chat`` starts a new turn, and a local turn silent with
a dead process is reaped with the ``lost`` ending."""

import asyncio
import json

import pytest  # noqa: F401  (temp_db / monkeypatch fixtures)

from core.events import turn_ending
from core.events.common_events import CommonEvent, DONE, ERROR, TEXT
from storage import database as task_store
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
    warm_new_chat,
)

import ws.dashboard  # noqa: F401  (resolves the dashboard↔dashboard_chat cycle)


def _error_event(reason: str, detail: str = "boom", **extra) -> CommonEvent:
    ending = turn_ending.TurnEnding(reason, detail=detail, **extra)
    return CommonEvent(type=ERROR, data={"message": ending.line(), "ending": ending.as_dict()})


async def _until(ws, frame_type: str, timeout: float = 5.0) -> dict:
    while True:
        frame = await ws.next_frame(timeout)
        if frame.get("type") == frame_type:
            return frame


def _turn_ended_rows(chat_id: str) -> list[dict]:
    out = []
    for m in task_store.get_chat_messages(chat_id):
        if m["role"] == "event" and m.get("event_type") == "system":
            block = json.loads(m["event_data"] or "{}")
            if block.get("subtype") == "turn_ended":
                out.append(block)
    return out


def _setup(monkeypatch, events):
    layer = FakeExecutionLayer()
    calls = {"n": 0}

    def turn(sid, prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            return list(events)
        return [CommonEvent(type=TEXT, data={"content": "again"}), CommonEvent(type=DONE, data={})]
    layer.turn_events = turn
    stub_dashboard_seams(monkeypatch, layer)
    slug = make_test_agent()
    set_username("user-admin", "admin")
    return layer, slug


def test_an_error_ending_sends_error_then_done_and_keeps_the_row(temp_db, monkeypatch):
    layer, slug = _setup(monkeypatch, [
        CommonEvent(type=TEXT, data={"content": "half an answer"}),
        _error_event(turn_ending.ERROR, "API Error: Connection dropped (ECONNRESET)"),
    ])

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "start", "chat_id": chat_id})
            await _until(ws, "text")
            err = await ws.expect({"type": "error", "message": ANY, "reason": "error",
                                   "resets_at": "", "chat_id": chat_id})
            assert "ECONNRESET" in err["message"]
            await ws.expect({"type": "done", "chat_id": chat_id})
            await ws.expect({"type": "chat_status", "chat_id": chat_id, "status": "ready"})

            rows = task_store.get_chat_messages(chat_id)
            kinds = [(m["role"], m.get("event_type") or "") for m in rows]
            assert kinds[:2] == [("user", ""), ("assistant", "")]
            [block] = _turn_ended_rows(chat_id)
            assert block["reason"] == "error" and block["message"] == err["message"]
            assert "ECONNRESET" in block["detail"]
            assert rows[-1]["event_type"] == "system"
            chat = task_store.get_chat(chat_id)
            assert chat["last_turn_aborted"] is False

            # The next message is a new turn on the same session, not a
            # steer into a turn the client still believes is running.
            ws.client_send({"type": "chat", "text": "once more", "chat_id": chat_id})
            assert (await _until(ws, "text"))["content"] == "again"
            await _until(ws, "done")
            assert [(m[0], m[1]) for m in layer.messages] == [(sid, "start"), (sid, "once more")]
            users = [m["content"] for m in task_store.get_chat_messages(chat_id)
                     if m["role"] == "user"]
            assert users == ["start", "once more"]
    run_ws_scenario(scenario)


def test_an_exited_ending_stamps_the_abort_flags(temp_db, monkeypatch):
    layer, slug = _setup(monkeypatch, [
        _error_event(turn_ending.EXITED, "exit 137: killed", exit_code=137),
    ])

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await warm_new_chat(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "start", "chat_id": chat_id})
            await _until(ws, "error")
            await _until(ws, "done")
            from core.events import chat_writer
            assert await chat_writer.drain(chat_id, timeout=5)
            [block] = _turn_ended_rows(chat_id)
            assert block["reason"] == "exited" and block["exit_code"] == 137
            chat = task_store.get_chat(chat_id)
            assert chat["last_turn_aborted"] is True
            assert chat["last_abort_graceful"] is False
    run_ws_scenario(scenario)


def test_an_untyped_error_is_the_error_ending_with_its_row(temp_db, monkeypatch):
    """Every error that ends a turn is typed: a session gone mid-send keeps
    its card after a reload instead of a reply that vanished."""
    layer, slug = _setup(monkeypatch, [
        CommonEvent(type=TEXT, data={"content": "half"}),
        CommonEvent(type=ERROR, data={"message": "Session not found"}),
    ])
    line = turn_ending.TurnEnding(turn_ending.ERROR, detail="Session not found").line()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await warm_new_chat(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "start", "chat_id": chat_id})
            await _until(ws, "text")
            await ws.expect({"type": "error", "message": line, "chat_id": chat_id,
                             "reason": "error", "resets_at": ""})
            await ws.expect({"type": "done", "chat_id": chat_id})
            from core.events import chat_writer
            assert await chat_writer.drain(chat_id, timeout=5)
            [block] = _turn_ended_rows(chat_id)
            assert block["reason"] == "error" and block["detail"] == "Session not found"
            chat = task_store.get_chat(chat_id)
            assert not chat["last_turn_aborted"]
    run_ws_scenario(scenario)


def test_a_local_turn_silent_with_a_dead_process_is_reaped_lost(temp_db, monkeypatch):
    layer = FakeExecutionLayer()

    async def turn(sid, prompt):
        yield CommonEvent(type=TEXT, data={"content": "working"})
        await asyncio.Event().wait()
    layer.turn_events = turn
    stub_dashboard_seams(monkeypatch, layer)
    slug = make_test_agent()
    set_username("user-admin", "admin")

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "go", "chat_id": chat_id})
            await _until(ws, "text")
            # The local layer now answers the stall probe: silent past the
            # soft ceiling and a dead process.
            layer.idle_seconds[sid] = 500.0
            layer.probed_dead.add(sid)
            ws.client_send({"type": "resume_chat", "chat_id": chat_id})
            await _until(ws, "chat_history")
            ws.client_send({"type": "ping"})
            await _until(ws, "pong", timeout=8.0)
            assert layer.prepared_resume == [sid]
            from core.events import chat_writer
            assert await chat_writer.drain(chat_id, timeout=5)
            [block] = _turn_ended_rows(chat_id)
            assert block["reason"] == "lost" and "process dead" in block["detail"]
            chat = task_store.get_chat(chat_id)
            assert chat["last_turn_aborted"] is True and chat["last_abort_graceful"] is False
    run_ws_scenario(scenario)


def _prompts_setup(monkeypatch, first_events):
    layer = FakeExecutionLayer()
    prompts: list[str] = []

    def turn(sid, prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return list(first_events)
        return [CommonEvent(type=TEXT, data={"content": "ok"}), CommonEvent(type=DONE, data={})]
    layer.turn_events = turn
    stub_dashboard_seams(monkeypatch, layer)
    slug = make_test_agent()
    set_username("user-admin", "admin")
    return layer, slug, prompts


def test_the_context_after_an_ending_says_the_turn_ended_and_skips_a_repeat(
        temp_db, monkeypatch):
    """After an exit the next turn re-injects what was lost, worded as an
    ending (not the person's Stop), and Send again (the same words) gets
    no copy of the turn it repeats."""
    layer, slug, prompts = _prompts_setup(monkeypatch, [
        CommonEvent(type=TEXT, data={"content": "half an answer"}),
        _error_event(turn_ending.EXITED, "exit 137: killed", exit_code=137),
    ])

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await warm_new_chat(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "start", "chat_id": chat_id})
            await _until(ws, "done")
            ws.client_send({"type": "chat", "text": "start", "chat_id": chat_id})
            await _until(ws, "done")
            assert prompts[1].endswith("start") and "cancelled" not in prompts[1]
    run_ws_scenario(scenario)

    layer, slug, prompts = _prompts_setup(monkeypatch, [
        CommonEvent(type=TEXT, data={"content": "half an answer"}),
        _error_event(turn_ending.EXITED, "exit 137: killed", exit_code=137),
    ])

    async def scenario2():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await warm_new_chat(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "start", "chat_id": chat_id})
            await _until(ws, "done")
            ws.client_send({"type": "chat", "text": "something else", "chat_id": chat_id})
            await _until(ws, "done")
            assert "ended early (exited)" in prompts[1] and "User said: start" in prompts[1]
            assert "half an answer" in prompts[1]
    run_ws_scenario(scenario2)


def test_a_send_that_takes_waiting_messages_names_the_turn_that_ended(temp_db, monkeypatch):
    """After an exit, a fresh send takes the person's waiting message ahead
    of itself: the re-injected context names the turn that ended, never the
    waiting message the same send carries."""
    from core.events import input_queue
    from core.events.common_events import TurnInput
    layer, slug, prompts = _prompts_setup(monkeypatch, [
        CommonEvent(type=TEXT, data={"content": "half an answer"}),
        _error_event(turn_ending.EXITED, "exit 137: killed", exit_code=137),
    ])

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await warm_new_chat(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "start", "chat_id": chat_id})
            await _until(ws, "done")
            # The loop's way out has drained the queue: the socket is idle.
            ws.client_send({"type": "ping"})
            await _until(ws, "pong", timeout=8.0)
            q = await input_queue.loaded(chat_id)
            await q.add("q-w", "user-admin", TurnInput("waiting one"))
            ws.client_send({"type": "chat", "text": "fresh one", "chat_id": chat_id})
            await _until(ws, "done")
            assert "ended early (exited)" in prompts[1]
            assert "User said: start" in prompts[1]
            assert "User said: waiting one" not in prompts[1]
            assert prompts[1].rstrip().endswith("waiting one\n\nfresh one")
    run_ws_scenario(scenario)

