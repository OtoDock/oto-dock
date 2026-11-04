"""A turn that ends typed, through the REAL dashboard WS handler: the socket
gets ``error`` with the reason THEN ``done`` once the rows landed, the chat
keeps a ``turn_ended`` row where the card belongs, the abort flags follow
the reason, a next ``chat`` starts a new turn, and a local turn silent with
a dead process is reaped with the ``lost`` ending."""

import asyncio
import json

import pytest

import ws.dashboard  # noqa: F401  (resolves the dashboard↔dashboard_chat cycle)
from core.events import turn_ending
from core.events.common_events import DONE, ERROR, TEXT, CommonEvent
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
from ws import wire_events as wire


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


def test_a_second_resume_still_carries_the_prompt_that_started_the_work(temp_db, monkeypatch):
    """The card's Resume sends one sentence (``wire.RESUME_TEXT``). After a
    second ending the next Resume repeats it, and the re-injected context
    still names the prompt that started the work and both partial replies:
    the builder passes over the earlier Resume row."""
    layer = FakeExecutionLayer()
    prompts: list[str] = []

    def turn(sid, prompt):
        prompts.append(prompt)
        if len(prompts) <= 2:
            return [CommonEvent(type=TEXT, data={"content": f"part {len(prompts)}"}),
                    _error_event(turn_ending.EXITED, "exit 137: killed", exit_code=137)]
        return [CommonEvent(type=TEXT, data={"content": "ok"}), CommonEvent(type=DONE, data={})]
    layer.turn_events = turn
    stub_dashboard_seams(monkeypatch, layer)
    slug = make_test_agent()
    set_username("user-admin", "admin")

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await warm_new_chat(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "start", "chat_id": chat_id})
            await _until(ws, "done")
            ws.client_send({"type": "chat", "text": wire.RESUME_TEXT, "chat_id": chat_id})
            await _until(ws, "done")
            assert "User said: start" in prompts[1] and "part 1" in prompts[1]
            ws.client_send({"type": "chat", "text": wire.RESUME_TEXT, "chat_id": chat_id})
            await _until(ws, "done")
            assert "ended early (exited)" in prompts[2] and "User said: start" in prompts[2]
            assert "part 1" in prompts[2] and "part 2" in prompts[2]
            assert prompts[2].rstrip().endswith(wire.RESUME_TEXT)
    run_ws_scenario(scenario)


def test_after_a_restart_the_next_turn_repeats_nothing_of_the_cut_one(temp_db, monkeypatch):
    """A reply the restart cut is in the engine's own record (its transcript,
    or the chat rows Direct LLM reads back): the next message goes alone,
    with no copy of the cut turn and no note about it."""
    layer, slug, prompts = _prompts_setup(monkeypatch, [
        CommonEvent(type=TEXT, data={"content": "half an answer"}),
        _error_event(turn_ending.RESTARTED, ""),
    ])

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await warm_new_chat(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "start", "chat_id": chat_id})
            await _until(ws, "done")
            assert [b["reason"] for b in _turn_ended_rows(chat_id)] == ["restarted"]
            chat = task_store.get_chat(chat_id)
            assert chat["last_turn_aborted"] is True and chat["last_abort_graceful"] is True
            ws.client_send({"type": "chat", "text": "go on", "chat_id": chat_id})
            await _until(ws, "done")
            assert prompts[1].rstrip().endswith("go on")
            assert "ended early" not in prompts[1] and "half an answer" not in prompts[1]
    run_ws_scenario(scenario)


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


# ── the cancelled context names the cut message's attachments ─────────────


def _person(db, cid, text, *photos, files=()):
    meta = {}
    if photos:
        meta["images"] = [{"name": "p.jpg", "path": p} for p in photos]
    if files:
        meta["files"] = [{"name": "f", "path": f} for f in files]
    db.add_chat_message(cid, "user", text, event_data=json.dumps(meta) if meta else "")


def _ended(db, cid, reason="lost"):
    db.add_chat_message(cid, "event", "", event_type="system", event_data=json.dumps(
        {"type": "system", "subtype": "turn_ended", "reason": reason, "message": "x"}))


PHOTO = "users/admin/workspace/uploads/photos/img_1.jpg"
DOC = "users/admin/workspace/uploads/files/plan.pdf"


class TestCancelledContextAttachments:
    def test_resume_names_the_cut_messages_photos_and_files(self, temp_db):
        from ws.dashboard_cancelled import cancelled_context
        temp_db.create_chat("cc-1", "user-admin", "agent-x")
        _person(temp_db, "cc-1", "read these", PHOTO, files=[DOC])
        temp_db.add_chat_message("cc-1", "assistant", "Looking")
        _ended(temp_db, "cc-1")
        _person(temp_db, "cc-1", "Continue from where you stopped.")
        ctx = cancelled_context("cc-1")
        assert "ended early (lost)" in ctx
        assert "User said: read these" in ctx
        assert f"- /{PHOTO}" in ctx and f"- /{DOC}" in ctx
        assert "Your partial response before cancellation:\nLooking" in ctx

    def test_a_message_of_photos_alone_is_the_cut_message(self, temp_db):
        from ws.dashboard_cancelled import cancelled_context
        temp_db.create_chat("cc-2", "user-admin", "agent-x")
        _person(temp_db, "cc-2", "an earlier message")
        temp_db.add_chat_message("cc-2", "assistant", "an earlier answer")
        _person(temp_db, "cc-2", "", PHOTO)
        _ended(temp_db, "cc-2")
        _person(temp_db, "cc-2", "Continue from where you stopped.")
        ctx = cancelled_context("cc-2")
        assert "User sent attachments with no text." in ctx
        assert f"- /{PHOTO}" in ctx
        assert "an earlier message" not in ctx

    def test_send_again_with_the_same_photos_gets_no_copy(self, temp_db):
        from ws.dashboard_cancelled import cancelled_context
        temp_db.create_chat("cc-3", "user-admin", "agent-x")
        _person(temp_db, "cc-3", "", PHOTO)
        _ended(temp_db, "cc-3", "exited")
        _person(temp_db, "cc-3", "", PHOTO)
        assert cancelled_context("cc-3") == ""

    def test_the_same_text_with_other_photos_is_a_new_message(self, temp_db):
        from ws.dashboard_cancelled import cancelled_context
        temp_db.create_chat("cc-4", "user-admin", "agent-x")
        _person(temp_db, "cc-4", "look", PHOTO)
        _ended(temp_db, "cc-4", "exited")
        _person(temp_db, "cc-4", "look", "users/admin/workspace/uploads/photos/img_2.jpg")
        assert f"- /{PHOTO}" in cancelled_context("cc-4")

    def test_send_again_missing_a_photo_that_is_gone_gets_no_copy(self, temp_db):
        from ws.dashboard_cancelled import cancelled_context
        temp_db.create_chat("cc-5", "user-admin", "agent-x")
        _person(temp_db, "cc-5", "look", PHOTO, "users/admin/workspace/uploads/photos/gone.jpg")
        _ended(temp_db, "cc-5", "exited")
        _person(temp_db, "cc-5", "look", PHOTO)
        assert cancelled_context("cc-5") == ""

    def test_an_engine_that_takes_photos_inline_gets_their_count(self, temp_db):
        from ws.dashboard_cancelled import cancelled_context
        temp_db.create_chat("cc-6", "user-admin", "agent-x")
        _person(temp_db, "cc-6", "look", PHOTO, files=[DOC])
        _ended(temp_db, "cc-6", "exited")
        _person(temp_db, "cc-6", "Continue from where you stopped.")
        ctx = cancelled_context("cc-6", inline_photos=True)
        assert "Attached to it: 1 photo(s)." in ctx
        assert f"/{PHOTO}" not in ctx and f"- /{DOC}" in ctx

    def test_the_messages_that_went_with_it_are_named_too(self, temp_db):
        from ws.dashboard_cancelled import cancelled_context
        temp_db.create_chat("cc-7", "user-admin", "agent-x")
        temp_db.add_chat_message("cc-7", "assistant", "the answer before")
        # A batch: a waiting message taken along with the person's send.
        _person(temp_db, "cc-7", "first part", PHOTO)
        _person(temp_db, "cc-7", "second part")
        _ended(temp_db, "cc-7")
        _person(temp_db, "cc-7", "Continue from where you stopped.")
        ctx = cancelled_context("cc-7")
        assert "User said: first part\n\nsecond part" in ctx
        assert f"- /{PHOTO}" in ctx
        assert "the answer before" not in ctx

    def test_send_again_of_a_joined_batch_gets_no_copy(self, temp_db):
        # The dashboard's Send again joins the cut turn's messages with a
        # blank line (lastTurnPrompt), as this builder joins the cut rows.
        from ws.dashboard_cancelled import cancelled_context
        temp_db.create_chat("cc-8", "user-admin", "agent-x")
        temp_db.add_chat_message("cc-8", "assistant", "the answer before")
        _person(temp_db, "cc-8", "first part", PHOTO)
        _person(temp_db, "cc-8", "second part")
        _ended(temp_db, "cc-8", "exited")
        _person(temp_db, "cc-8", "first part\n\nsecond part", PHOTO)
        assert cancelled_context("cc-8") == ""


@pytest.mark.asyncio
async def test_a_queued_batch_on_an_engine_that_takes_photos_inline_gets_their_count(temp_db):
    """A queued batch delivered as the next turn reads the cut turn's context
    as an idle send does: an engine with no Read tool gets the photos
    counted, never paths it cannot open."""
    from types import SimpleNamespace

    from ws.dashboard import DashboardConnection
    temp_db.create_chat("cc-q", "user-admin", "agent-x")
    _person(temp_db, "cc-q", "look", PHOTO, files=[DOC])
    _ended(temp_db, "cc-q", "exited")
    temp_db.update_chat("cc-q", last_turn_aborted=True, last_abort_graceful=False)
    conn = object.__new__(DashboardConnection)
    conn.user_sub = "user-admin"
    conn.user = {"username": "admin"}

    def _layer(inline):
        return SimpleNamespace(capabilities_for=lambda sid: SimpleNamespace(
            behaviour=SimpleNamespace(attach_images_inline=inline)))

    prompt, consumed = await conn._prepared_prompt("cc-q", "go on", new_text="go on",
                                                   layer=_layer(True), session_id="s-1")
    assert consumed == {"last_turn_aborted": True, "last_abort_graceful": False}
    assert "Attached to it: 1 photo(s)." in prompt and f"/{PHOTO}" not in prompt
    assert f"- /{DOC}" in prompt and prompt.endswith("go on")

    temp_db.update_chat("cc-q", last_turn_aborted=True, last_abort_graceful=False)
    prompt, _consumed = await conn._prepared_prompt("cc-q", "go on", new_text="go on",
                                                    layer=_layer(False), session_id="s-1")
    assert f"- /{PHOTO}" in prompt
