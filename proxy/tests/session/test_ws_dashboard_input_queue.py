"""The chat's own queue through the REAL dashboard WS handler: a message
typed while a turn runs waits in the chat (``core/events/input_queue``), not
in the socket that typed it, so it goes out as the next turn whatever that
socket does meanwhile (switches chats, closes), every socket of the chat
sees the same chips, a steer's row sorts where the engine read it, a fresh
send takes the person's waiting messages ahead of itself, a failed turn and
a Stop give the queue back, and a delivery re-reads its author's standing
before it runs as them."""

import asyncio
import json

import pytest  # noqa: F401  (temp_db / monkeypatch fixtures)

from core.events import chat_writer, input_queue, stream_pump
from core.events.common_events import (
    CommonEvent, DONE, ERROR, PRODUCER_DONE, TEXT, TOOL_RESULT, TOOL_USE, TurnInput,
)
from core.session import session_kind
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
from ws import wire_events as wire


@pytest.fixture(autouse=True)
def _fresh_queue(monkeypatch):
    input_queue._registry.clear()
    from services.scheduler import shared
    monkeypatch.setattr(shared, "_shutting_down", False)
    monkeypatch.setattr(input_queue, "VIEWER_GRACE_S", 0.2)
    yield
    input_queue._registry.clear()


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


def _setup(monkeypatch, turns=None):
    gate = asyncio.Event()
    calls = {"n": 0}
    layer = FakeExecutionLayer()
    layer.turn_events = turns(layer, gate, calls) if turns else _gated_turns(gate, calls)
    stub_dashboard_seams(monkeypatch, layer)
    slug = make_test_agent()
    set_username("user-admin", "admin")
    return layer, gate, slug


async def _until(ws, frame_type: str, timeout: float = 5.0) -> dict:
    """The next frame of ``frame_type``; the frames before it are skipped."""
    while True:
        frame = await ws.next_frame(timeout)
        if frame.get("type") == frame_type:
            return frame


async def _start(ws, layer, slug, text: str = "start") -> tuple[str, str]:
    chat_id, sid = await warm_new_chat(ws, layer, slug)
    ws.client_send({"type": "chat", "text": text, "chat_id": chat_id})
    await _until(ws, "live_state")
    await ws.expect({"type": "text", "content": "t1", "chat_id": chat_id})
    return chat_id, sid


class _TurnEndHold:
    """Parks a pump after its producer ended and before its ``all_done``."""

    def __init__(self, pump):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        real = pump._drain_writes

        async def _held():
            self.entered.set()
            await self.release.wait()
            await real()
        pump._drain_writes = _held


async def _wait_for(cond, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def _rows(chat_id: str) -> list[tuple[str, str]]:
    return [(m["role"], m["content"] or m.get("event_type") or "")
            for m in task_store.get_chat_messages(chat_id)]


def _cards(chat_id: str) -> list[dict]:
    out = []
    for m in task_store.get_chat_messages(chat_id):
        if m["role"] == "event" and m.get("event_type") == "system":
            block = json.loads(m["event_data"] or "{}")
            if block.get("subtype") == "undelivered_input":
                out.append(block)
    return out


def test_a_message_queued_as_the_turn_ends_goes_out_with_no_viewer(temp_db, monkeypatch):
    """The socket switches to another chat before the turn ends: the message
    is the chat's, so it goes out as that chat's next turn at once, with no
    viewer and no return to the chat."""
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await _start(ws, layer, slug)
            hold = _TurnEndHold(stream_pump._active_pumps[chat_id])
            gate.set()
            await hold.entered.wait()
            ws.client_send({"type": "chat", "text": "one more", "chat_id": chat_id,
                            "queue_id": "q-1"})
            await ws.expect({"type": "queued", "index": 0, "queue_id": "q-1",
                             "text": "one more", "author_sub": "user-admin",
                             "chat_id": chat_id})
            other = "chat-elsewhere"
            task_store.create_chat(other, "user-admin", slug)
            ws.client_send({"type": "resume_chat", "chat_id": other})
            await _until(ws, "warmup_ready")
            hold.release.set()
            await _wait_for(lambda: len(layer.messages) == 2)
            assert [(m[0], m[1]) for m in layer.messages] == [(sid, "start"), (sid, "one more")]
            await _wait_for(lambda: stream_pump._active_pumps.get(chat_id) is None)
            await chat_writer.drain(chat_id, timeout=5)
            assert [r for r in _rows(chat_id) if r[0] != "event"] == [
                ("user", "start"), ("assistant", "t1"), ("user", "one more"),
                ("assistant", "t2")]
            assert task_store.list_chat_input_queue(chat_id) == []
    run_ws_scenario(scenario)


def test_a_queued_message_survives_the_socket_closing(temp_db, monkeypatch):
    """The tab closes before the turn ends: no card, the message goes out as
    the chat's next turn, run as its author through the connection that
    queued it."""
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await _start(ws, layer, slug)
            hold = _TurnEndHold(stream_pump._active_pumps[chat_id])
            gate.set()
            await hold.entered.wait()
            ws.client_send({"type": "chat", "text": "while away", "chat_id": chat_id})
            await _until(ws, "queued")
        hold.release.set()
        await _wait_for(lambda: len(layer.messages) == 2)
        assert layer.messages[1][1] == "while away"
        await _wait_for(lambda: stream_pump._active_pumps.get(chat_id) is None)
        await chat_writer.drain(chat_id, timeout=5)
        assert _cards(chat_id) == []
        assert ("user", "while away") in _rows(chat_id)
    run_ws_scenario(scenario)


def test_two_sockets_see_one_queue(temp_db, monkeypatch):
    """Every socket of the chat gets the chips (a second tab included) and
    its resume snapshot lists them; a cancel by id clears both."""
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws1, \
                dashboard_connection(session_cookie()) as ws2:
            await drain_startup(ws1)
            await drain_startup(ws2)
            chat_id, _sid = await _start(ws1, layer, slug)
            ws1.client_send({"type": "chat", "text": "from tab one", "chat_id": chat_id,
                             "queue_id": "q-1"})
            chip = {"type": "queued", "index": 0, "queue_id": "q-1", "text": "from tab one",
                    "author_sub": "user-admin", "chat_id": chat_id}
            await ws1.expect(chip)
            # Tab two views another chat: the chip still reaches it.
            assert await _until(ws2, "queued") == chip
            ws2.client_send({"type": "resume_chat", "chat_id": chat_id})
            snap = await _until(ws2, "queue_snapshot")
            assert snap["messages"] == [{"queue_id": "q-1", "text": "from tab one",
                                         "author_sub": "user-admin"}]
            ws2.client_send({"type": "cancel_queued", "queue_id": "q-1"})
            removed = {"type": "queue_removed", "queue_id": "q-1", "index": 0,
                       "returned": True, "text": "from tab one", "chat_id": chat_id}
            assert await _until(ws1, "queue_removed") == removed
            assert await _until(ws2, "queue_removed") == removed
            gate.set()
            await _until(ws1, "done")
            assert [m[1] for m in layer.messages] == ["start"]
    run_ws_scenario(scenario)


def test_a_steered_row_sorts_after_the_blocks_before_it(temp_db, monkeypatch):
    """Text and a tool, then a steer, then more text: the steer's row lands
    after the blocks the engine produced before it took it, and its frame
    names that row."""

    def turns(layer, gate, calls):
        def turn(sid, prompt):
            async def gen():
                yield CommonEvent(type=TEXT, data={"content": "before"})
                yield CommonEvent(type=TOOL_USE, data={"name": "Bash", "tool_id": "t1"})
                yield CommonEvent(type=TOOL_RESULT, data={"tool_id": "t1", "content": "ok"})
                await gate.wait()
                yield CommonEvent(type=TEXT, data={"content": "after"})
                yield CommonEvent(type=DONE, data={})
            return gen()
        return turn

    layer, gate, slug = _setup(monkeypatch, turns)
    layer.steer_accepts = True

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws, \
                dashboard_connection(session_cookie()) as ws2:
            await drain_startup(ws)
            await drain_startup(ws2)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "prompt", "chat_id": chat_id})
            await _until(ws, "tool_end")
            ws.client_send({"type": "chat", "text": "steer me", "chat_id": chat_id,
                            "queue_id": "q-s"})
            steered = await _until(ws, "steered")
            assert steered["queue_id"] == "q-s" and steered["text"] == "steer me"
            # A revisit mid-turn: the history carries what came before the
            # steer and the steer itself, the live state none of it.
            ws2.client_send({"type": "resume_chat", "chat_id": chat_id})
            history = await _until(ws2, "chat_history")
            assert [m["content"] or m["event_type"] for m in history["messages"]] == [
                "prompt", "before", "tool", "steer me"]
            live = await _until(ws2, "live_state")
            assert live.get("live_blocks", []) == []
            gate.set()
            await _until(ws, "done")
            await chat_writer.drain(chat_id, timeout=5)
            rows = task_store.get_chat_messages(chat_id)
            assert [(m["role"], m["content"] or m["event_type"]) for m in rows] == [
                ("user", "prompt"), ("assistant", "before"), ("event", "tool"),
                ("user", "steer me"), ("assistant", "after")]
            assert steered["message_id"] == rows[3]["id"]
            assert layer.steered == [(sid, "steer me")]
    run_ws_scenario(scenario)


def test_a_steer_under_another_persons_queue_id_keeps_their_row(temp_db, monkeypatch):
    """A steer that names a teammate's waiting id takes its own id, so its
    accept never deletes their row."""
    layer, gate, slug = _setup(monkeypatch)
    layer.steer_accepts = True

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-t", "user-b", TurnInput("theirs"))
            ws.client_send({"type": "chat", "text": "mine", "chat_id": chat_id,
                            "queue_id": "q-t"})
            steered = await _until(ws, "steered")
            assert steered["queue_id"] != "q-t" and steered["text"] == "mine"
            # The steer's accept (its row, under its own id) left the
            # teammate's row in place; the turn's end then delivers it.
            await chat_writer.drain(chat_id, timeout=5)
            assert [r["queue_id"] for r in task_store.list_chat_input_queue(chat_id)] == ["q-t"]
            assert [qi.queue_id for qi in q.items] == ["q-t"]
            gate.set()
            await _until(ws, "done")
            await _wait_for(lambda: "theirs" in [m[1] for m in layer.messages])
    run_ws_scenario(scenario)


def test_a_failed_accept_ends_the_turn_typed(temp_db, monkeypatch):
    """The queue turn's accept raises (a DB error): the turn ends typed
    `error` with its row instead of dying unannounced, and the message's
    row stays in the table for the next delivery."""
    layer, gate, slug = _setup(monkeypatch)

    def broken_accept(self, taken, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(input_queue.ChatInputQueue, "accept_job", broken_accept)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "later", "chat_id": chat_id})
            await _until(ws, "queued")
            gate.set()
            err = await _until(ws, "error")
            assert err["reason"] == "error" and "could not be saved" in err["message"]
            await _until(ws, "done")
            await chat_writer.drain(chat_id, timeout=5)
            assert ("event", "system") in _rows(chat_id)
            assert [r["text"] for r in task_store.list_chat_input_queue(chat_id)] == ["later"]
    run_ws_scenario(scenario)


def test_a_fresh_send_takes_the_persons_waiting_messages_ahead_of_itself(temp_db,
                                                                         monkeypatch):
    """A message left waiting (a turn that could not start, a restart) goes
    out with the person's next send: one turn, its rows first."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-old", "user-admin", TurnInput("waiting"))
            ws.client_send({"type": "chat", "text": "fresh", "chat_id": chat_id})
            sent = await _until(ws, "queue_sent")
            assert sent["queue_ids"] == ["q-old"] and sent["text"] == "waiting"
            await _until(ws, "done")
            assert [m[1] for m in layer.messages] == ["waiting\n\nfresh"]
            await chat_writer.drain(chat_id, timeout=5)
            assert [r for r in _rows(chat_id) if r[0] == "user"] == [
                ("user", "waiting"), ("user", "fresh")]
            assert q.items == [] and task_store.list_chat_input_queue(chat_id) == []
    run_ws_scenario(scenario)


def test_a_fresh_send_while_a_turn_starts_is_queued_not_a_second_turn(temp_db, monkeypatch):
    """Another starter holds the chat (a queued message's delivery bringing
    its turn up): a person's send waits in the queue instead of starting a
    second producer, and goes out once."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await warm_new_chat(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            claim = q.try_claim()
            assert claim is not None
            ws.client_send({"type": "chat", "text": "meanwhile", "chat_id": chat_id,
                            "queue_id": "q-m"})
            # No turn attached to this socket: its composer is freed (done)
            # and the message waits under its chip (queued, through the
            # connection's notify queue, so either may come first).
            seen: dict = {}
            while not {"queued", "done"} <= seen.keys():
                frame = await ws.next_frame(8.0)
                seen.setdefault(frame.get("type"), frame)
            assert seen["queued"]["queue_id"] == "q-m"
            assert seen["done"]["chat_id"] == chat_id
            assert layer.messages == []
            q.release(claim)
            assert await input_queue.deliver(chat_id)
            await _wait_for(lambda: len(layer.messages) == 1)
            assert layer.messages[0][1] == "meanwhile"
    run_ws_scenario(scenario)


def test_a_delivery_that_cannot_start_leaves_its_messages_queued(temp_db, monkeypatch):
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        from ws.dashboard_chat_stream import ChatStreamMixin

        async def no_stream(self, *a, **kw):
            return None

        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await warm_new_chat(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-1", "user-admin", TurnInput("not yet"))
            # The turn before ended short: the context it owes waits for the
            # turn that will carry the message.
            task_store.update_chat(chat_id, last_turn_aborted=True, last_abort_graceful=False)
            monkeypatch.setattr(ChatStreamMixin, "_start_new_stream", no_stream)
            assert await input_queue.deliver(chat_id) is False
            assert [qi.queue_id for qi in q.items] == ["q-1"]
            await chat_writer.drain(chat_id, timeout=5)
            assert [r["queue_id"] for r in task_store.list_chat_input_queue(chat_id)] == ["q-1"]
            assert _cards(chat_id) == []
            assert not q.claimed
            assert task_store.get_chat(chat_id)["last_turn_aborted"] is True
    run_ws_scenario(scenario)


def test_a_delivery_for_an_author_held_by_the_gate_is_returned_with_the_card(temp_db,
                                                                            monkeypatch):
    """The author's sign-in no longer drives turns by the time the queue goes
    out (a forced password change here): the message is not run as them,
    it comes back as the chat's undelivered card."""
    from auth import providers
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            hold = _TurnEndHold(stream_pump._active_pumps[chat_id])
            gate.set()
            await hold.entered.wait()
            ws.client_send({"type": "chat", "text": "as me", "chat_id": chat_id})
            await _until(ws, "queued")
        providers.clear_auth_gate_caches()
        task_store.update_user_auth_fields("user-admin", must_change_password=True)
        try:
            hold.release.set()
            await _wait_for(lambda: _cards(chat_id) != [])
            assert [c["message"] for c in _cards(chat_id)] == ["as me"]
            assert [m[1] for m in layer.messages] == ["start"]
            assert task_store.list_chat_input_queue(chat_id) == []
        finally:
            task_store.update_user_auth_fields("user-admin", must_change_password=False)
            providers.clear_auth_gate_caches()
    run_ws_scenario(scenario)


def test_a_failed_turn_gives_the_queue_back_with_the_card(temp_db, monkeypatch):
    """The turn ends on an error: what was queued goes back to the composer
    and stays in the chat as a card, never sent into the same wall."""

    def turns(layer, gate, calls):
        def turn(sid, prompt):
            calls["n"] += 1

            async def gen():
                yield CommonEvent(type=TEXT, data={"content": "t1"})
                await gate.wait()
                yield CommonEvent(type=ERROR, data={"message": "API Error: Connection dropped"})
            return gen()
        return turn

    layer, gate, slug = _setup(monkeypatch, turns)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "queued", "chat_id": chat_id,
                            "queue_id": "q-1"})
            await _until(ws, "queued")
            gate.set()
            await _until(ws, "error")
            await _until(ws, "done")
            cleared = await _until(ws, "queue_cleared")
            assert cleared == {"type": "queue_cleared", "queue_ids": ["q-1"],
                               "reason": "turn_failed", "returned": True, "text": "queued",
                               "chat_id": chat_id}
            await chat_writer.drain(chat_id, timeout=5)
            assert [c["reason"] for c in _cards(chat_id)] == ["turn_failed"]
            assert [m[1] for m in layer.messages] == ["start"]
    run_ws_scenario(scenario)


def test_a_message_sent_between_the_error_and_done_starts_the_next_turn(temp_db,
                                                                      monkeypatch):
    """Send again right after the card: the message came after the failure,
    so it is the next turn, never returned with the failed turn's queue."""

    def turns(layer, gate, calls):
        def turn(sid, prompt):
            calls["n"] += 1
            n = calls["n"]

            async def gen():
                if n == 1:
                    yield CommonEvent(type=ERROR, data={"message": "boom"})
                    return
                yield CommonEvent(type=TEXT, data={"content": "answered"})
                yield CommonEvent(type=DONE, data={})
            return gen()
        return turn

    layer, gate, slug = _setup(monkeypatch, turns)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await warm_new_chat(ws, layer, slug)
            pumps: list = []
            real_start = stream_pump.ChatStreamPump.start

            def _hold_start(self):
                if not pumps:
                    pumps.append(_TurnEndHold(self))
                return real_start(self)
            monkeypatch.setattr(stream_pump.ChatStreamPump, "start", _hold_start)
            ws.client_send({"type": "chat", "text": "start", "chat_id": chat_id})
            await _until(ws, "error")
            await pumps[0].entered.wait()
            ws.client_send({"type": "chat", "text": "again", "chat_id": chat_id})
            await _until(ws, "queued")
            pumps[0].release.set()
            await _until(ws, "queue_sent")
            assert (await _until(ws, "text"))["content"] == "answered"
            assert [m[1] for m in layer.messages] == ["start", "again"]
            await chat_writer.drain(chat_id, timeout=5)
            assert _cards(chat_id) == []
    run_ws_scenario(scenario)


def test_stop_gives_the_queue_back(temp_db, monkeypatch):
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "not now", "chat_id": chat_id,
                            "queue_id": "q-1"})
            await _until(ws, "queued")
            ws.client_send({"type": "abort"})
            seen: dict = {}
            while len(seen) < 2:
                frame = await ws.next_frame(5)
                if frame["type"] in ("queue_cleared", "aborted"):
                    seen[frame["type"]] = frame
            assert seen["queue_cleared"] == {
                "type": "queue_cleared", "queue_ids": ["q-1"], "reason": "stopped",
                "returned": True, "text": "not now", "chat_id": chat_id}
            gate.set()
            await asyncio.sleep(0.5)
            assert [m[1] for m in layer.messages] == ["start"]
            await chat_writer.drain(chat_id, timeout=5)
            assert _cards(chat_id) == []
            assert task_store.list_chat_input_queue(chat_id) == []
    run_ws_scenario(scenario)


def test_a_duplicate_queue_id_is_answered_once_before_and_after_acceptance(temp_db,
                                                                         monkeypatch):
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            for _ in range(2):
                ws.client_send({"type": "chat", "text": "once", "chat_id": chat_id,
                                "queue_id": "q-dup"})
                assert (await _until(ws, "queued"))["queue_id"] == "q-dup"
            assert len(input_queue.get(chat_id).items) == 1
            gate.set()
            await _until(ws, "queue_sent")
            await _until(ws, "done")
            # The re-send after the turn took it (a reconnect replay).
            ws.client_send({"type": "chat", "text": "once", "chat_id": chat_id,
                            "queue_id": "q-dup"})
            await _until(ws, "done")
            assert [m[1] for m in layer.messages] == ["start", "once"]
    run_ws_scenario(scenario)


def test_a_task_pumps_message_waits_for_the_run(temp_db, monkeypatch):
    """A pump that takes no typed message (a task run's here, the steer
    refused): the message waits in the chat and goes out when the run's
    pump ends, through the viewer's own drain."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            eq: asyncio.Queue = asyncio.Queue()
            ends = asyncio.Event()

            async def _task_produce():
                await ends.wait()
                await eq.put(CommonEvent(type=PRODUCER_DONE, data={}))
            pump = stream_pump.ChatStreamPump(
                chat_id=chat_id, session_id=sid,
                producer=asyncio.create_task(_task_produce()),
                event_queue=eq, perm_queue=None,
                source_type=session_kind.TASK.source_type,
            )
            stream_pump._active_pumps[chat_id] = pump
            pump.start()
            ws.client_send({"type": "resume_chat", "chat_id": chat_id})
            await _until(ws, "live_state")
            ws.client_send({"type": "chat", "text": "for the agent", "chat_id": chat_id})
            await _until(ws, "queued")
            ends.set()
            await _until(ws, "done")
            sent = await _until(ws, "queue_sent")
            assert sent["text"] == "for the agent" and sent["message_ids"]
            await _until(ws, "done")
            assert [(m[0], m[1]) for m in layer.messages] == [(sid, "for the agent")]
    run_ws_scenario(scenario)


def test_the_resume_snapshot_lists_the_chats_queue(temp_db, monkeypatch):
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await warm_new_chat(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-1", "user-viewer", TurnInput("a teammate's",
                        files=[{"path": "u/f.txt", "name": "f.txt"}]))
            ws.client_send({"type": "resume_chat", "chat_id": chat_id})
            snap = await _until(ws, "queue_snapshot")
            assert snap == {"type": "queue_snapshot", "chat_id": chat_id, "messages": [
                {"queue_id": "q-1", "text": "a teammate's", "author_sub": "user-viewer",
                 "files": [{"path": "u/f.txt", "name": "f.txt"}]}]}
    run_ws_scenario(scenario)


def test_a_resume_of_an_idle_chat_sends_the_persons_waiting_messages(temp_db, monkeypatch):
    """A message of this person waits on an idle chat (its delivery found
    nobody to run it as): opening the chat sends it as the next turn (the
    resume's pump loop drains it on its way out)."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-1", "user-admin", TurnInput("mine"))
            ws.client_send({"type": "resume_chat", "chat_id": chat_id})
            await _until(ws, "queue_snapshot")
            await _wait_for(lambda: len(layer.messages) == 1)
            assert [(m[0], m[1]) for m in layer.messages] == [(sid, "mine")]
            await _wait_for(lambda: stream_pump._active_pumps.get(chat_id) is None)
            await chat_writer.drain(chat_id, timeout=5)
            assert task_store.list_chat_input_queue(chat_id) == []
            assert [r for r in _rows(chat_id) if r[0] != "event"] == [
                ("user", "mine"), ("assistant", "t1")]
    run_ws_scenario(scenario)


def test_a_drain_whose_heal_raises_keeps_the_message_queued(temp_db, monkeypatch):
    """The viewer's drain took the person's message and the heal raised (a
    warmup on a machine that is offline): the message goes back in the
    queue before the claim is let go, so the delivery the release arms, or
    the machine's reconnect, still sends it."""
    from ws.dashboard_chat_send import ChatSendMixin
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    real_heal = ChatSendMixin._heal_viewed_session
    raised: list[str] = []

    async def _heal(self, msg):
        if not raised:
            raised.append(msg.get("chat_id") or "")
            raise RuntimeError("Agent targets remote machine m-1 which is offline.")
        return await real_heal(self, msg)
    monkeypatch.setattr(ChatSendMixin, "_heal_viewed_session", _heal)
    monkeypatch.setattr(input_queue, "VIEWER_GRACE_S", 0.05)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-h", "user-admin", TurnInput("kept"))
            ws.client_send({"type": "resume_chat", "chat_id": chat_id})
            await _wait_for(lambda: bool(raised))
            await _wait_for(lambda: len(layer.messages) == 1)
            assert [(m[0], m[1]) for m in layer.messages] == [(sid, "kept")]
    run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# The cancel rule and the sender's session rule, unit-level
# ---------------------------------------------------------------------------

class _CancelConnection:
    def __init__(self, sub: str, role: str, chat_id: str):
        from ws.dashboard_dispatch import ClientMessageDispatcher
        self._on_cancel_queued = ClientMessageDispatcher._on_cancel_queued.__get__(self)
        self.user_sub, self.user_role, self.chat_id = sub, role, chat_id
        self.errors: list[str] = []

    async def _deny_task_continue(self, cid, chat=None, *, quiet=False):
        return ""

    async def _send_error(self, message: str) -> None:
        self.errors.append(message)


@pytest.mark.asyncio
async def test_an_admin_cancels_another_persons_message_and_a_member_cannot(temp_db,
                                                                            monkeypatch):
    from services.notifications import notification_manager
    monkeypatch.setattr(notification_manager, "push_live", lambda *a, **k: 1)
    cid = "chat-shared-q"
    temp_db.create_chat(cid, "user-admin", "agent-a", "default")
    q = await input_queue.loaded(cid)
    await q.add("q-admin", "user-admin", TurnInput("admin's"))
    await q.add("q-viewer", "user-viewer", TurnInput("viewer's"))
    member = _CancelConnection("user-viewer", "member", cid)
    await member._on_cancel_queued({"queue_id": "q-admin"})
    assert member.errors and [qi.queue_id for qi in q.items] == ["q-admin", "q-viewer"]
    await member._on_cancel_queued({"queue_id": "q-viewer"})
    assert [qi.queue_id for qi in q.items] == ["q-admin"]
    admin = _CancelConnection("user-admin", "admin", cid)
    await admin._on_cancel_queued({"queue_id": "q-admin"})
    assert q.items == [] and admin.errors == []


def test_a_shared_session_serves_only_the_person_it_was_warmed_for(monkeypatch):
    from types import SimpleNamespace
    from core.session import session_state
    from services.engines import subscription_pool
    from ws.dashboard_chat_send import ChatSendMixin
    conn = SimpleNamespace(user_sub="user-b", user={"username": "bob"})
    serves = ChatSendMixin._session_serves_sender.__get__(conn)
    monkeypatch.setattr(subscription_pool, "get_session_payer_sub", lambda sid: "")
    monkeypatch.setattr(session_state, "get_session_security",
                        lambda sid: SimpleNamespace(username="alice"))
    assert serves("s1") is False   # warmed as another person: recycled
    monkeypatch.setattr(session_state, "get_session_security",
                        lambda sid: SimpleNamespace(username="bob"))
    assert serves("s1") is True
    monkeypatch.setattr(subscription_pool, "get_session_payer_sub", lambda sid: "user-a")
    assert serves("s1") is False   # paid by another person
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: None)
    monkeypatch.setattr(subscription_pool, "get_session_payer_sub", lambda sid: "")
    assert serves("s1") is False   # unproven: fails closed


@pytest.mark.asyncio
async def test_a_steer_is_recorded_in_stream_order_by_the_pump(temp_db):
    """The steer event waits behind the blocks the engine produced before it
    and ahead of the ones after: text that streams while its row is written
    is never cut from the live state."""
    from core.events.stream_pump import ChatStreamPump, STEER_ACCEPTED
    cid = "chat-order"
    temp_db.create_chat(cid, "user-admin", "agent-a", "default")
    events: asyncio.Queue = asyncio.Queue()
    pump = ChatStreamPump(chat_id=cid, session_id="sess-order",
                          producer=asyncio.get_running_loop().create_task(asyncio.sleep(3600)),
                          event_queue=events, perm_queue=None)
    stream_pump._active_pumps[cid] = pump
    qi = input_queue.QueuedInput(queue_id="q-s", chat_id=cid, author_sub="user-admin",
                                 item=TurnInput("mid-turn"))
    await events.put(CommonEvent(type=TEXT, data={"content": "before "}))
    assert pump.record_steer(qi) is True
    assert events._queue[-1].type == STEER_ACCEPTED
    await events.put(CommonEvent(type=TEXT, data={"content": "after"}))
    await events.put(CommonEvent(type=PRODUCER_DONE, data={}))
    try:
        pump.start()
        await asyncio.wait_for(pump._task, 5)
    finally:
        stream_pump._active_pumps.pop(cid, None)
        pump.producer.cancel()
    await chat_writer.drain(cid, timeout=5)
    assert [(m["role"], m["content"]) for m in task_store.get_chat_messages(cid)] == [
        ("assistant", "before "), ("user", "mid-turn"), ("assistant", "after")]
    assert pump.accepts_in_flight == 0
    assert ANY  # the harness import is used by the scenarios above


@pytest.mark.asyncio
async def test_a_steer_during_an_open_tool_keeps_the_tool_live_and_after_the_steer(temp_db):
    """The person types while a long tool call runs: the steer's save writes
    the text before it, the running tool stays in the live state (a revisit
    shows it), and when it completes its row lands right after the steer's,
    ahead of what the engine produced after."""
    from core.events.stream_pump import ChatStreamPump, _chat_streaming_state
    cid = "chat-tool-steer"
    temp_db.create_chat(cid, "user-admin", "agent-a", "default")
    events: asyncio.Queue = asyncio.Queue()
    pump = ChatStreamPump(chat_id=cid, session_id="sess-tool",
                          producer=asyncio.get_running_loop().create_task(asyncio.sleep(3600)),
                          event_queue=events, perm_queue=None)
    stream_pump._active_pumps[cid] = pump
    qi = input_queue.QueuedInput(queue_id="q-t", chat_id=cid, author_sub="user-admin",
                                 item=TurnInput("while it runs"))
    await events.put(CommonEvent(type=TEXT, data={"content": "checking "}))
    await events.put(CommonEvent(type=TOOL_USE, data={"name": "Bash", "tool_id": "t1"}))
    try:
        pump.start()
        assert pump.record_steer(qi) is True
        for _ in range(100):
            if pump.accepts_in_flight == 0:
                break
            await asyncio.sleep(0.02)
        await chat_writer.drain(cid, timeout=5)
        live = _chat_streaming_state[cid]["live_blocks"]
        assert [(b.get("type"), b.get("tool_id")) for b in live] == [("tool", "t1")]
        await events.put(CommonEvent(type=TOOL_RESULT, data={"name": "Bash", "tool_id": "t1"}))
        await events.put(CommonEvent(type=TEXT, data={"content": "done"}))
        await events.put(CommonEvent(type=PRODUCER_DONE, data={}))
        await asyncio.wait_for(pump._task, 5)
    finally:
        stream_pump._active_pumps.pop(cid, None)
        pump.producer.cancel()
    await chat_writer.drain(cid, timeout=5)
    rows = [(m["role"], m["content"] or m.get("event_type"))
            for m in task_store.get_chat_messages(cid)]
    assert rows == [("assistant", "checking "), ("user", "while it runs"),
                    ("event", "tool"), ("assistant", "done")]


@pytest.mark.asyncio
async def test_a_restart_leaves_a_steered_satellite_turn_to_its_replay_once(temp_db):
    """A satellite turn the proxy re-adopts after a restart is replayed
    whole: the blocks its steer saved early are deleted at the shutdown,
    so the replay writes them once (the steer's row stays)."""
    from core.events.stream_pump import ChatStreamPump, _recovery_suppress_flush
    cid = "chat-replay"
    temp_db.create_chat(cid, "user-admin", "agent-a", "default")
    events: asyncio.Queue = asyncio.Queue()
    hold = asyncio.Event()

    async def _producer():
        await hold.wait()
    pump = ChatStreamPump(chat_id=cid, session_id="sess-replay",
                          producer=asyncio.get_running_loop().create_task(_producer()),
                          event_queue=events, perm_queue=None)
    pump._recovery_eligible = True
    stream_pump._active_pumps[cid] = pump
    try:
        pump.start()
        await events.put(CommonEvent(type=TEXT, data={"content": "early"}))
        pump.record_steer(input_queue.QueuedInput(
            queue_id="q-s", chat_id=cid, author_sub="user-admin", item=TurnInput("steer")))
        await _wait_for(lambda: pump.accepts_in_flight == 0 and pump._steer_saved_ranges)
        await chat_writer.drain(cid, timeout=5)
        assert [m["content"] for m in task_store.get_chat_messages(cid)] == ["early", "steer"]
        stream_pump.suppress_recovery_flush(cid)
        assert [m["content"] for m in task_store.get_chat_messages(cid)] == ["steer"]
    finally:
        _recovery_suppress_flush.discard(cid)
        stream_pump._active_pumps.pop(cid, None)
        pump.producer.cancel()
        pump.abort()


# ---------------------------------------------------------------------------
# A turn start rides out its machine's reconnect grace
# ---------------------------------------------------------------------------

def test_a_send_during_a_short_reconnect_runs_on_the_same_session(temp_db, monkeypatch):
    """The viewed send's heal: a session whose machine is in its reconnect
    grace is waited for, never resumed over (the dead path pops its record,
    drops its background state and may put a fresh session in its place)."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            layer.alive.discard(sid)           # the machine dropped …
            layer.reconnecting[sid] = 0.05     # … and is back inside the wait
            ws.client_send({"type": "chat", "text": "after the blip", "chat_id": chat_id})
            await _until(ws, "done")
            assert [(m[0], m[1]) for m in layer.messages] == [(sid, "after the blip")]
            assert layer.prepared_resume == []
            assert [s for s, _ in layer.started] == [sid]
            assert layer.reconnect_waits == [sid]
    run_ws_scenario(scenario)


def test_a_send_whose_machine_stays_away_takes_the_dead_path(temp_db, monkeypatch):
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            layer.alive.discard(sid)
            layer.reconnecting[sid] = None     # still away when the wait ends
            ws.client_send({"type": "chat", "text": "later", "chat_id": chat_id})
            await _until(ws, "done")
            assert layer.prepared_resume == [sid]
    run_ws_scenario(scenario)


def test_a_queued_turn_during_a_short_reconnect_runs_on_the_same_session(temp_db, monkeypatch):
    """The headless chokepoint: a queued message delivered while the chat's
    machine reconnects waits once and runs on the live session."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-blip", "user-admin", TurnInput("queued across the blip"))
            layer.alive.discard(sid)
            layer.reconnecting[sid] = 0.05
            assert await input_queue.deliver(chat_id)
            await _wait_for(lambda: len(layer.messages) == 1)
            assert layer.messages[0][:2] == (sid, "queued across the blip")
            assert layer.prepared_resume == []
            assert layer.reconnect_waits == [sid]
    run_ws_scenario(scenario)


def test_opening_a_chat_during_a_short_reconnect_reuses_its_session(temp_db, monkeypatch):
    """The warmup on open: a chat whose machine is reconnecting keeps its
    live session instead of a resume over it."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            layer.alive.discard(sid)
            layer.reconnecting[sid] = 0.05
            async with dashboard_connection(session_cookie()) as tab:
                await drain_startup(tab)
                tab.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id})
                ready = await _until(tab, "warmup_ready")
                assert ready["session_id"] == sid
            assert [s for s, _ in layer.started] == [sid]
            assert layer.prepared_resume == []
            assert layer.reconnect_waits == [sid]
    run_ws_scenario(scenario)


# ---------------------------------------------------------------------------
# A turn start inside the grace past the wait: nothing is destroyed; a send
# waits in the queue (with why), a queued turn defers, a platform prompt is
# kept as the chat's wake, the chat opens on its session.
# ---------------------------------------------------------------------------

def _reconnecting(layer, sid: str) -> None:
    """The machine dropped and is still away when the wait ends; its grace
    still holds the session."""
    layer.alive.discard(sid)
    layer.reconnecting[sid] = None
    layer.grace_held.add(sid)


def _back(layer, sid: str) -> None:
    layer.alive.add(sid)
    layer.grace_held.discard(sid)


@pytest.mark.parametrize("queue_id", ["q-away", None])
def test_a_send_while_the_machine_is_still_reconnecting_waits_in_the_queue(
        temp_db, monkeypatch, queue_id):
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    monkeypatch.setattr(input_queue, "VIEWER_GRACE_S", 0.05)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            _reconnecting(layer, sid)
            frame = {"type": "chat", "text": "while it was away", "chat_id": chat_id}
            if queue_id:
                frame["queue_id"] = queue_id
            ws.client_send(frame)
            queued = await _until(ws, "queued")
            assert queued["waiting"] == input_queue.WAITING_RECONNECT
            assert queued["text"] == "while it was away"
            assert queued["queue_id"] == (queue_id or queued["queue_id"]) and queued["queue_id"]
            await _until(ws, "done")
            # Nothing destroyed: no resume over the live session, no fresh one.
            assert layer.prepared_resume == []
            assert [s for s, _ in layer.started] == [sid]
            assert layer.messages == []
            await asyncio.sleep(0.2)          # the armed delivery defers too
            assert layer.messages == [] and layer.prepared_resume == []
            assert [r["text"] for r in task_store.list_chat_input_queue(chat_id)] == [
                "while it was away"]
            # The machine is back: the waiting message goes out on the same session.
            _back(layer, sid)
            assert await input_queue.deliver(chat_id)
            await _wait_for(lambda: len(layer.messages) == 1)
            assert layer.messages[0][:2] == (sid, "while it was away")
    run_ws_scenario(scenario)


def test_a_queued_turn_while_reconnecting_defers_and_shows_why(temp_db, monkeypatch):
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-def", "user-admin", TurnInput("typed before the drop"))
            _reconnecting(layer, sid)
            assert await input_queue.deliver(chat_id) is False
            assert layer.messages == [] and layer.prepared_resume == []
            assert [qi.queue_id for qi in q.items] == ["q-def"]
            assert q.items[0].waiting == input_queue.WAITING_RECONNECT
            snap = await _until(ws, "queue_snapshot")
            assert snap["messages"][0]["waiting"] == input_queue.WAITING_RECONNECT
    run_ws_scenario(scenario)


@pytest.mark.parametrize("item, prompt", [
    ({"type": "continuation_prompt", "prompt": "check the upload", "task_id": "dyn-x"},
     "check the upload"),
    ({"type": "bg_command_nudge", "count": 1, "labels": ["the build"]}, "the build"),
    ({"type": "bg_nudge", "count": 1, "labels": ["the review"]}, "the review"),
    ({"type": "task_result_prompt", "result_prompt": "the worker is done",
      "task_id": "t-1", "task_name": "lane", "delegate_agent": "pa"}, "the worker is done"),
], ids=["check-back", "command-review", "agent-review", "delegate-result"])
def test_a_platform_prompt_while_reconnecting_is_kept_as_the_chats_wake(
        temp_db, monkeypatch, item, prompt):
    from core.session.session_state import _dashboard_notify_queues
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            _reconnecting(layer, sid)
            await _dashboard_notify_queues[sid].put({**item, "session_id": sid, "chat_id": chat_id})
            await _wait_for(lambda: bool(
                (task_store.get_chat(chat_id) or {}).get("pending_delegate_wake")))
            assert prompt in task_store.get_chat(chat_id)["pending_delegate_wake"]
            assert layer.prepared_resume == [] and layer.messages == []
            # Stored once: the delegate result's own fallback does not repeat it.
            assert len(task_store.claim_pending_delegate_wake(chat_id)) == 1
    run_ws_scenario(scenario)


def test_opening_a_chat_while_reconnecting_keeps_the_persons_message_queued(
        temp_db, monkeypatch):
    """The resume's pump loop drains the person's waiting message: its
    machine is still away, so the message stays queued, saying why, and
    goes out once the machine is back."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    monkeypatch.setattr(input_queue, "VIEWER_GRACE_S", 0.05)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-r", "user-admin", TurnInput("mine, later"))
            _reconnecting(layer, sid)
            ws.client_send({"type": "resume_chat", "chat_id": chat_id})
            await _wait_for(lambda: bool(q.items) and q.items[0].waiting == input_queue.WAITING_RECONNECT)
            await asyncio.sleep(0.2)
            assert layer.messages == [] and layer.prepared_resume == []
            # No `done`: the page is not streaming, and a `done` on its empty
            # bubble would refetch and drain again every wait.
            frames = []
            while True:
                try:
                    frames.append(await ws.next_frame(0.1))
                except (asyncio.TimeoutError, TimeoutError):
                    break
            assert "done" not in [f.get("type") for f in frames]
            assert [qi.queue_id for qi in q.items] == ["q-r"]
            _back(layer, sid)
            assert await input_queue.deliver(chat_id)
            await _wait_for(lambda: len(layer.messages) == 1)
            assert layer.messages[0][:2] == (sid, "mine, later")
    run_ws_scenario(scenario)


def test_a_machine_that_drops_between_the_heal_and_the_start_says_not_sent(
        temp_db, monkeypatch):
    from ws.dashboard_chat_send import ChatSendMixin
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    real_heal = ChatSendMixin._heal_viewed_session
    dropped: list[str] = []

    async def _heal(self, msg):
        out = await real_heal(self, msg)
        if not dropped and self.session_id:
            dropped.append(self.session_id)
            _reconnecting(layer, self.session_id)
        return out
    monkeypatch.setattr(ChatSendMixin, "_heal_viewed_session", _heal)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "dropped", "chat_id": chat_id})
            line = await _until(ws, "system")
            assert line["subtype"] == wire.SUBTYPE_MACHINE_RECONNECTING
            assert "reconnecting" in line["message"]
            await _until(ws, "done")
            assert layer.messages == [] and layer.prepared_resume == []
            assert [s for s, _ in layer.started] == [sid]
    run_ws_scenario(scenario)


def test_every_turn_start_is_reviewed_for_a_machine_that_is_reconnecting():
    """``_start_new_stream`` raises ``TurnDeferred`` while the session's
    machine is still away: each caller decides what waits. A new caller
    fails here until it is reviewed."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "ws"
    found: set[tuple[str, str, str]] = set()
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("_start_new_stream", "_run_server_turn")):
                    found.add((path.name, fn.name, node.func.attr))
    assert found == {
        ("dashboard_chat_send.py", "_deliver_queued", "_start_new_stream"),
        ("dashboard_chat_send.py", "_handle_claimed_chat", "_start_new_stream"),
        ("dashboard_chat_stream.py", "_drain_input_queue", "_start_new_stream"),
        ("dashboard_server_events.py", "_run_server_turn", "_start_new_stream"),
        ("dashboard_server_events.py", "_handle_server_notification", "_run_server_turn"),
        ("dashboard_server_events.py", "_run_server_turn_or_keep", "_run_server_turn"),
        ("dashboard_server_events.py", "_run_kick_headless_turn", "_run_server_turn"),
    }


def test_opening_a_chat_while_reconnecting_keeps_its_session(temp_db, monkeypatch):
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            _reconnecting(layer, sid)
            async with dashboard_connection(session_cookie()) as tab:
                await drain_startup(tab)
                tab.client_send({"type": "resume_chat", "chat_id": chat_id})
                ready = await _until(tab, "warmup_ready")
                assert ready["session_id"] == sid and not ready.get("needs_warmup")
                tab.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id})
                ready = await _until(tab, "warmup_ready")
                assert ready["session_id"] == sid
            assert [s for s, _ in layer.started] == [sid]
            assert layer.prepared_resume == []
    run_ws_scenario(scenario)


def test_a_click_queued_behind_a_waiting_message_rides_the_same_turn(temp_db, monkeypatch):
    """A message and an artifact click both arrive after the producer closed
    its queues and before the pump ended, so the viewer's drain carries them:
    one turn with the message and the click's frame, the click's row first
    (the drain carried the message alone and the click was lost)."""
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await _start(ws, layer, slug)
            task_store.create_media_token("ui-tok-1", "/nowhere/poll.html",
                                          media_kind="ui", chat_id=chat_id)
            hold = _TurnEndHold(stream_pump._active_pumps[chat_id])
            gate.set()
            await hold.entered.wait()
            ws.client_send({"type": "chat", "text": "later", "chat_id": chat_id, "queue_id": "q-1"})
            await _until(ws, "queued")
            ws.client_send({"type": "artifact_interaction", "chat_id": chat_id, "token": "ui-tok-1",
                            "title": "Poll", "payload": {"vote": 1}})
            assert (await _until(ws, "artifact_ack"))["status"] == "queued"
            hold.release.set()
            await _wait_for(lambda: len(layer.messages) == 2)
            prompt = layer.messages[1][1]
            assert prompt.startswith("later"), prompt
            assert '[interaction from artifact "Poll"]' in prompt and '"vote":1' in prompt
            await _wait_for(lambda: stream_pump._active_pumps.get(chat_id) is None)
            await asyncio.sleep(0.3)
            assert len(layer.messages) == 2  # one turn, not a second one for the click
            await chat_writer.drain(chat_id, timeout=5)
            rows = _rows(chat_id)
            assert ("event", "artifact_interaction") in rows, rows
            assert rows.index(("event", "artifact_interaction")) < rows.index(("user", "later"))
    run_ws_scenario(scenario)


@pytest.mark.asyncio
async def test_a_headless_first_message_deferred_by_a_reconnect_leaves_its_card(
        temp_db, monkeypatch):
    """The person left the chat during its spawn, so its first message runs
    headless, and the machine is still reconnecting when the kick starts:
    the message keeps its row with the undelivered card under it for the
    person's return (nothing sends it later), where the live case shows the
    reconnecting line."""
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from ws.dashboard import DashboardConnection
    from ws.dashboard_chat_text import TurnDeferred
    slug = make_test_agent()
    cid = str(uuid.uuid4())
    task_store.create_chat(cid, "user-admin", slug, "default", model="m",
                           execution_path="claude-code-cli")
    task_store.add_chat_message(cid, "user", "first words", author_sub="user-admin")
    conn = object.__new__(DashboardConnection)
    conn.user = {"username": "admin"}
    layer = SimpleNamespace(capabilities_for=lambda sid: SimpleNamespace(
        behaviour=SimpleNamespace(attach_images_inline=False)))
    monkeypatch.setattr(conn, "_resolve_layer_for_chat_async", AsyncMock(return_value=layer))
    monkeypatch.setattr(conn, "_process_attachments",
                        AsyncMock(return_value=("first words", [], [{"name": "a.png", "path": "p"}], [])))

    async def _deferred(*a, **k):
        raise TurnDeferred()
    monkeypatch.setattr(conn, "_run_server_turn", _deferred)

    await conn._run_kick_headless(cid, "sid-1", "first words", [], [])
    await chat_writer.drain(cid, timeout=5)
    assert [(c["reason"], c["message"]) for c in _cards(cid)] == [
        (wire.UNDELIVERED_QUEUED, "first words\n\nAttached: a.png")]


# ---------------------------------------------------------------------------
# A deferred pick belongs to one chat; the chat's claim covers every starter.
# ---------------------------------------------------------------------------

def _conn():
    from ws.dashboard import _connections_by_user
    conns = list(_connections_by_user.get("user-admin", ()))
    assert len(conns) == 1
    return conns[0]


def _chat_mode(chat_id: str) -> str:
    return (task_store.get_chat(chat_id) or {})["permission_mode"]


def test_a_new_chat_pick_never_reaches_the_previous_chats_queued_delivery(temp_db, monkeypatch):
    """The person queued a message in chat A, opened the new-chat page (the
    socket stays bound to A) and picked a mode there, a frame with no chat.
    A's queued delivery runs on this connection: A keeps its mode and its
    session, and the pick waits for the chat the warmup mints."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            await asyncio.sleep(0.3)
            conn = _conn()
            layer.mode_changes.clear()
            ws.client_send({"type": "mode_change", "mode": "acceptEdits"})
            await _wait_for(lambda: conn.deferred_mode == "acceptEdits")
            assert conn.deferred_for == ""
            q = await input_queue.loaded(chat_id)
            await q.add("q-a", "user-admin", TurnInput("queued in A"),
                        conn=conn, origin_conn=conn.notify_connection_id)
            assert await input_queue.deliver(chat_id)
            await _wait_for(lambda: len(layer.messages) == 1)
            await _wait_for(lambda: stream_pump._active_pumps.get(chat_id) is None)
            await chat_writer.drain(chat_id, timeout=5)
            assert await asyncio.to_thread(_chat_mode, chat_id) == "default"
            assert layer.mode_changes == []
            assert (conn.deferred_mode, conn.deferred_for) == ("acceptEdits", "")
    run_ws_scenario(scenario)


def test_a_new_chat_pick_never_reaches_the_previous_chat_after_a_woken_turn(temp_db, monkeypatch):
    """A woken turn runs on chat A with the person attached; they queue a
    message in A, open the new-chat page and pick a mode there. The woken
    turn ends and the socket's drain sends the message: A keeps its mode."""
    from services.scheduler import delivery
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            await asyncio.sleep(0.3)
            conn = _conn()
            layer.mode_changes.clear()
            echo = asyncio.create_task(delivery._run_echo_turn_pumped(
                layer, sid, chat_id, slug, "a check-back"))
            await _wait_for(lambda: len(layer.messages) == 1)
            ws.client_send({"type": "resume_chat", "chat_id": chat_id})
            await _wait_for(lambda: conn.streaming)
            ws.client_send({"type": "chat", "text": "queued in A", "chat_id": chat_id,
                            "queue_id": "q-a"})
            q = await input_queue.loaded(chat_id)
            await _wait_for(lambda: bool(q.items))
            ws.client_send({"type": "mode_change", "mode": "acceptEdits"})
            await _wait_for(lambda: conn.deferred_mode == "acceptEdits")
            gate.set()
            await asyncio.wait_for(echo, 5)
            await _wait_for(lambda: len(layer.messages) == 2)
            await _wait_for(lambda: stream_pump._active_pumps.get(chat_id) is None)
            await chat_writer.drain(chat_id, timeout=5)
            assert await asyncio.to_thread(_chat_mode, chat_id) == "default"
            assert layer.mode_changes == []
            assert conn.deferred_mode == "acceptEdits"
    run_ws_scenario(scenario)


def test_a_pick_made_for_the_chat_while_away_applies_before_its_queued_turn(temp_db, monkeypatch):
    """A mode picked for chat A while its machine was away is deferred for A.
    A's queued delivery waits out the reconnect and then applies the pick to
    the engine before the turn goes out, and the deferral is spent."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            await asyncio.sleep(0.3)
            conn = _conn()
            q = await input_queue.loaded(chat_id)
            await q.add("q-1", "user-admin", TurnInput("queued during the blip"),
                        conn=conn, origin_conn=conn.notify_connection_id)
            layer.alive.discard(sid)
            layer.mode_changes.clear()
            ws.client_send({"type": "mode_change", "mode": "plan", "chat_id": chat_id})
            await _wait_for(lambda: conn.deferred_mode == "plan")
            assert conn.deferred_for == chat_id
            layer.reconnecting[sid] = 0.05
            sent_modes: list = []
            real_send = layer.send_message

            async def _send(s, prompt, **kw):
                sent_modes.append(list(layer.mode_changes))
                async for ev in real_send(s, prompt, **kw):
                    yield ev
            layer.send_message = _send
            assert await input_queue.deliver(chat_id)
            await _wait_for(lambda: len(layer.messages) == 1)
            assert sent_modes[0] == [(sid, "plan")]
            assert (conn.deferred_mode, conn.deferred_for) == ("", "")
    run_ws_scenario(scenario)


def test_the_re_apply_writes_the_row_off_the_loop(temp_db, monkeypatch):
    from storage import pg
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            conn = _conn()
            conn.deferred_mode, conn.deferred_for = "plan", chat_id
            with pg.loop_guard():
                await conn._reapply_deferred_after_warmup(chat_id)
            await chat_writer.drain(chat_id, timeout=5)
            assert await asyncio.to_thread(_chat_mode, chat_id) == "plan"
            assert layer.mode_changes == [(sid, "plan")]
    run_ws_scenario(scenario)


def test_a_platform_turn_waits_while_a_queued_delivery_holds_the_chat(temp_db, monkeypatch):
    """A queued delivery holds the chat's claim and has not registered its
    pump yet; a platform turn (a stored wake replayed at the reconnect, a
    delegate result, a job review) asks to start meanwhile: it does not, so
    one pump drives the chat and the engine gets one turn."""
    from core.events.stream_pump import _active_pumps
    from services.scheduler import delivery
    from ws.dashboard_chat_send import ChatSendMixin
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            conn = _conn()
            in_start = asyncio.Event()
            go = asyncio.Event()
            real_push = ChatSendMixin._push_batch_attachments

            async def _paused(agent, batch, session_id):
                in_start.set()
                await go.wait()
                return await real_push(agent, batch, session_id)
            monkeypatch.setattr(ChatSendMixin, "_push_batch_attachments", staticmethod(_paused))
            q = await input_queue.loaded(chat_id)
            await q.add("q-1", "user-admin", TurnInput("queued"),
                        conn=conn, origin_conn=conn.notify_connection_id)
            delivering = asyncio.create_task(input_queue.deliver(chat_id))
            await in_start.wait()
            assert q.claimed and _active_pumps.get(chat_id) is None
            assert await delivery._run_echo_turn_pumped(
                layer, sid, chat_id, slug, "a stored wake replayed at the reconnect") is None
            assert _active_pumps.get(chat_id) is None
            go.set()
            assert await asyncio.wait_for(delivering, 5)
            await _wait_for(lambda: len(layer.messages) == 1)
            await _wait_for(lambda: _active_pumps.get(chat_id) is None)
            await asyncio.sleep(0.2)
            assert [m[1] for m in layer.messages] == ["queued"]
    run_ws_scenario(scenario)


def test_a_server_turn_is_kept_as_the_wake_while_another_starter_holds_the_chat(
        temp_db, monkeypatch):
    """The socket's own server turn (a job review) meets a held claim: it
    starts nothing and the prompt is stored as the chat's wake."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            conn = _conn()
            q = await input_queue.loaded(chat_id)
            claim = q.try_claim()
            assert claim is not None
            started: list = []
            real_start = conn._start_new_stream

            async def _start(*a, **k):
                started.append(1)
                return await real_start(*a, **k)
            monkeypatch.setattr(conn, "_start_new_stream", _start)
            await conn._run_server_turn_or_keep(
                "the job's review", target_session_id=sid, target_chat_id=chat_id,
                target_layer=layer)
            assert started == []
            wakes = await asyncio.to_thread(task_store.claim_pending_wake_records, chat_id)
            assert [w.get("prompt") for w in wakes] == ["the job's review"]
            q.release(claim)
    run_ws_scenario(scenario)


class _ReachConnection(_CancelConnection):
    """A connection viewing ``chat_id`` that names another chat in a frame."""

    def __init__(self, sub: str, role: str, chat_id: str):
        super().__init__(sub, role, chat_id)
        from ws.dashboard_dispatch import ClientMessageDispatcher
        self._may_reach_chat = ClientMessageDispatcher._may_reach_chat.__get__(self)
        self._on_cancel_all_queued = ClientMessageDispatcher._on_cancel_all_queued.__get__(self)

    def _viewer_context(self):
        return "viewer"

    def _cancel_artifacts(self, cid):
        pass


@pytest.mark.asyncio
async def test_a_cancel_frame_for_a_chat_the_person_cannot_open_loads_nothing(temp_db,
                                                                             monkeypatch):
    """A cancel with no stream for a chat other than the viewed one asks the
    REST rule first: a chat the person cannot open, or none at all, is
    refused and never loaded into the registry."""
    import api.agents.chats as chats_api
    monkeypatch.setattr(chats_api, "can_access_chat", lambda ctx, chat: False)
    temp_db.create_chat("chat-other", "user-admin", "agent-a", "default")
    conn = _ReachConnection("user-viewer", "member", "chat-mine")
    await conn._on_cancel_queued({"queue_id": "x", "chat_id": "chat-other"})
    await conn._on_cancel_all_queued({"chat_id": "chat-other"})
    await conn._on_cancel_queued({"queue_id": "x", "chat_id": "no-such-chat"})
    assert "chat-other" not in input_queue._registry
    assert "no-such-chat" not in input_queue._registry
    assert len(conn.errors) == 3


def test_the_registry_forgets_idle_chats_past_its_cap(monkeypatch):
    """Past the cap the least recently touched idle queues go; a queue with a
    waiting message or a claim stays whatever its age."""
    monkeypatch.setattr(input_queue, "IDLE_EVICT_S", 0.0)
    waiting = input_queue.get("chat-waiting")
    waiting.items.append(input_queue.QueuedInput(
        queue_id="w1", chat_id="chat-waiting", author_sub="a", item=TurnInput("x")))
    claimed = input_queue.get("chat-claimed")
    token = claimed.try_claim()
    for i in range(input_queue.REGISTRY_CAP + 200):
        input_queue.get(f"chat-{i}")
    assert len(input_queue._registry) <= input_queue.REGISTRY_CAP
    assert "chat-waiting" in input_queue._registry and "chat-claimed" in input_queue._registry
    claimed.release(token, rearm=False)


@pytest.mark.asyncio
async def test_a_batch_put_back_after_a_drop_keeps_the_dropped_out(temp_db, monkeypatch):
    """``drop_where`` racing a batch a delivery already took: the delivery's
    deferral puts the batch back, the dropped author's messages stay gone."""
    from services.notifications import notification_manager
    monkeypatch.setattr(notification_manager, "push_live", lambda *a, **k: 1)
    cid = "chat-drop-race"
    temp_db.create_chat(cid, "user-admin", "agent-a", "default")
    q = await input_queue.loaded(cid)
    await q.add("a1", "user-a", TurnInput("one"))
    await q.add("b1", "user-b", TurnInput("two"))
    await chat_writer.drain(cid, timeout=2)
    taken = q.take("user-a")
    await input_queue.drop_where(author_sub="user-a")
    q.put_back(taken)
    assert [qi.queue_id for qi in q.items] == ["b1"]
    assert [r["queue_id"] for r in task_store.list_chat_input_queue(cid)] == ["b1"]


@pytest.mark.asyncio
async def test_a_starter_waits_for_the_claim_within_its_bound(monkeypatch):
    """``claim_when_free`` takes the claim once its holder lets go, gives up at
    its bound, and with ``supersede`` passes a running pump (the caller
    replaces its own) but never another starter's claim."""
    from types import SimpleNamespace
    q = input_queue.get("chat-cwf")
    held = q.try_claim()

    async def free():
        await asyncio.sleep(0.2)
        q.release(held, rearm=False)
    freer = asyncio.create_task(free())
    got = await input_queue.claim_when_free("chat-cwf", timeout=2)
    await freer
    assert got is not None and q.claimed
    assert await input_queue.claim_when_free("chat-cwf", timeout=0.2) is None
    q.release(got, rearm=False)

    monkeypatch.setitem(stream_pump._active_pumps, "chat-sup", SimpleNamespace(
        is_done=False, _return_upto=None, source_type="phone"))
    assert await input_queue.claim_when_free("chat-sup", timeout=0.2) is None
    own = await input_queue.claim_when_free("chat-sup", timeout=0.2, supersede=True)
    assert own is not None
    assert await input_queue.claim_when_free("chat-sup", timeout=0.2, supersede=True) is None
    input_queue.get("chat-sup").release(own, rearm=False)


def test_a_send_deferred_after_its_heal_keeps_its_batch_as_one_card(temp_db, monkeypatch):
    """A5: the machine drops between the heal and the start of a send that
    took the person's waiting messages ahead of itself: their rows are
    written, nothing reached the engine, so one undelivered card keeps every
    message of the batch for a re-send."""
    from ws.dashboard_chat_send import ChatSendMixin
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    real_heal = ChatSendMixin._heal_viewed_session
    dropped: list[str] = []

    async def _heal(self, msg):
        out = await real_heal(self, msg)
        if not dropped and self.session_id:
            dropped.append(self.session_id)
            _reconnecting(layer, self.session_id)
        return out
    monkeypatch.setattr(ChatSendMixin, "_heal_viewed_session", _heal)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-early", "user-admin", TurnInput("early"))
            ws.client_send({"type": "chat", "text": "typed", "chat_id": chat_id})
            await _until(ws, "done")
            await chat_writer.drain(chat_id, timeout=5)
            assert layer.messages == []
            [card] = _cards(chat_id)
            assert "early" in card["message"] and "typed" in card["message"]
    run_ws_scenario(scenario)


def test_a_busy_send_queues_behind_the_same_persons_waiting_message(temp_db, monkeypatch):
    """A steer never overtakes the same author's earlier waiting message: it
    queues behind it, so the engine reads them in the order they were sent."""
    layer, gate, slug = _setup(monkeypatch)
    layer.steer_accepts = True

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await _start(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-first", "user-admin", TurnInput("first"))
            ws.client_send({"type": "chat", "text": "second", "chat_id": chat_id,
                            "queue_id": "q-second"})
            frame = await _until(ws, "queued")
            while frame["queue_id"] != "q-second":
                frame = await _until(ws, "queued")
            assert layer.steered == []
            assert [qi.queue_id for qi in q.items] == ["q-first", "q-second"]
            gate.set()
            await _until(ws, "done")
    run_ws_scenario(scenario)


def test_a_drain_a_teammates_terminal_refuses_keeps_the_messages_marked(temp_db,
                                                                        monkeypatch):
    """Edge case (a): the heal refused this person because a teammate's live
    terminal is open on the chat. Nothing was typed: the messages stay in the
    queue, marked as waiting for that terminal, and no turn starts."""
    from ws.dashboard_chat_send import ChatSendMixin
    from ws.dashboard_chat_text import HEAL_REFUSED
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def _refused(self, msg):
        return HEAL_REFUSED

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            monkeypatch.setattr(ChatSendMixin, "_heal_viewed_session", _refused)
            q = await input_queue.loaded(chat_id)
            await q.add("q-1", "user-admin", TurnInput("mine"))
            ws.client_send({"type": "resume_chat", "chat_id": chat_id})
            await _wait_for(lambda: q.items and q.items[0].waiting == input_queue.WAITING_TERMINAL)
            assert layer.messages == []
            assert not q.claimed
            assert [r["queue_id"] for r in task_store.list_chat_input_queue(chat_id)] == ["q-1"]
            # The terminal's close clears the mark and arms the delivery.
            input_queue.clear_waiting(chat_id, input_queue.WAITING_TERMINAL)
            assert q.items[0].waiting == ""
    run_ws_scenario(scenario)


@pytest.mark.asyncio
async def test_a_queued_turns_attachments_go_only_where_they_are_missing(tmp_path, monkeypatch):
    """A7: before a headless queued turn, a photo or file goes only to the
    chat's own machine and only when that machine lacks it; a session the
    remote layer does not hold pushes nothing."""
    import config
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout
    from ws.dashboard_chat_send import ChatSendMixin
    agent_dir = tmp_path / "agent"
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "workspace" / "held.png").write_bytes(b"x" * 10)
    (agent_dir / "workspace" / "new.pdf").write_bytes(b"y" * 20)
    monkeypatch.setattr(config, "get_agent_dir", lambda agent: agent_dir)
    held = {"workspace/held.png": {"exists": True, "size": 10, "mtime_ns": 0}}

    async def _probe(sid, rel):
        return held.get(rel, {"exists": False})
    pushes: list[tuple[str, str | None]] = []

    async def _fan_out(agent, rel, source, **kw):
        pushes.append((rel, kw.get("only_machine_id")))
    monkeypatch.setattr(remote_file_flow, "stat_probe", _probe)
    monkeypatch.setattr(workspace_fanout, "fan_out_write", _fan_out)
    batch = TurnInput("t", image_meta=[{"path": "workspace/held.png", "name": "held.png"}],
                      files=[{"path": "workspace/new.pdf", "name": "new.pdf"}])
    monkeypatch.setattr(remote_file_flow, "remote_machine_id", lambda sid: "m-1")
    await ChatSendMixin._push_batch_attachments("agent-a", batch, "sid-remote")
    assert pushes == [("workspace/new.pdf", "m-1")]
    pushes.clear()
    monkeypatch.setattr(remote_file_flow, "remote_machine_id", lambda sid: "")
    await ChatSendMixin._push_batch_attachments("agent-a", batch, "sid-local")
    assert pushes == []


def test_a_send_while_the_chat_gets_ready_waits_behind_its_first_turn(temp_db, monkeypatch):
    """Every send typed during "Getting ready" is kept: it waits in the
    chat's queue (no ``done``, no turn of its own) and goes out after the
    first message, as the next turn, in the order it was typed."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            layer.start_gate = asyncio.Event()
            ws.client_send({"type": "warmup", "agent": slug, "text": "first"})
            chat_id = (await _until(ws, "warmup_started"))["chat_id"]
            for n in (2, 3):
                ws.client_send({"type": "chat", "text": f"send {n}", "chat_id": chat_id,
                                "queue_id": f"q-{n}", "queue_only": True})
                assert (await _until(ws, "queued"))["queue_id"] == f"q-{n}"
            assert input_queue.getting_ready(chat_id)
            assert await input_queue.deliver(chat_id) is False
            assert layer.messages == []
            layer.start_gate.set()
            await _wait_for(lambda: len(layer.messages) >= 2)
            await asyncio.sleep(0.3)
            assert "first" in layer.messages[0][1]
            # The person's waiting messages ride one turn, in order.
            assert len(layer.messages) == 2
            assert layer.messages[1][1].index("send 2") < layer.messages[1][1].index("send 3")
            assert not input_queue.getting_ready(chat_id)
            assert input_queue.peek(chat_id) == []
    run_ws_scenario(scenario)


def test_a_second_tabs_send_while_the_chat_gets_ready_is_queued_and_frees_its_composer(
        temp_db, monkeypatch):
    """A page that is not in the warmup's "Getting ready" (a second tab)
    sends a turn of its own: it is queued behind the first turn all the same,
    and ``done`` ends that page's streaming state."""
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            layer.start_gate = asyncio.Event()
            ws.client_send({"type": "warmup", "agent": slug, "text": "first"})
            chat_id = (await _until(ws, "warmup_started"))["chat_id"]
            ws.client_send({"type": "chat", "text": "from tab two", "chat_id": chat_id,
                            "queue_id": "q-tab2"})
            seen: dict = {}
            while not {"queued", "done"} <= seen.keys():
                frame = await ws.next_frame(8.0)
                seen.setdefault(frame.get("type"), frame)
            assert seen["queued"]["queue_id"] == "q-tab2"
            assert seen["done"]["chat_id"] == chat_id
            layer.start_gate.set()
            await _wait_for(lambda: len(layer.messages) >= 2)
            assert "first" in layer.messages[0][1]
            assert layer.messages[1][1] == "from tab two"
    run_ws_scenario(scenario)


def test_a_failed_warmup_hands_the_sends_back(temp_db, monkeypatch):
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
            ws.client_send({"type": "chat", "text": "second", "chat_id": chat_id,
                            "queue_id": "q-2", "queue_only": True})
            await _until(ws, "queued")
            layer.start_gate.set()
            cleared = await _until(ws, "queue_cleared", timeout=8)
            assert cleared["queue_ids"] == ["q-2"] and cleared["returned"] is True
            assert cleared["text"] == "second" and cleared["reason"] == "turn_failed"
            assert input_queue.peek(chat_id) == []
            assert not input_queue.getting_ready(chat_id)
            assert layer.messages == []
    run_ws_scenario(scenario)


def test_stop_during_the_warmup_hands_the_sends_back(temp_db, monkeypatch):
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            layer.start_gate = asyncio.Event()
            ws.client_send({"type": "warmup", "agent": slug, "text": "first"})
            chat_id = (await _until(ws, "warmup_started"))["chat_id"]
            ws.client_send({"type": "chat", "text": "second", "chat_id": chat_id,
                            "queue_id": "q-2", "queue_only": True})
            await _until(ws, "queued")
            ws.client_send({"type": "abort"})
            seen: dict = {}
            while len(seen) < 2:
                frame = await ws.next_frame(5)
                if frame["type"] in ("queue_cleared", "aborted"):
                    seen[frame["type"]] = frame
            assert seen["queue_cleared"]["reason"] == "stopped"
            assert seen["queue_cleared"]["returned"] is True
            layer.start_gate.set()
            await asyncio.sleep(0.5)
            assert layer.messages == []
            assert input_queue.peek(chat_id) == []
    run_ws_scenario(scenario)


def test_the_first_turn_hold_ends_when_it_starts_or_never_runs(monkeypatch):
    input_queue.hold_first_turn("c-hold")
    assert input_queue.getting_ready("c-hold")
    assert input_queue.first_turn_started("c-hold") is True
    assert not input_queue.getting_ready("c-hold")
    assert input_queue.first_turn_started("c-hold") is False
    # A kick that never ran: the queue's own delivery takes what waits,
    # whether the hold was still set or had lapsed, unless a pump drives
    # the chat (its end drains it).
    armed: list[str] = []
    monkeypatch.setattr(input_queue, "arm", armed.append)
    monkeypatch.setattr(input_queue, "peek", lambda cid: ["waiting"])
    input_queue.hold_first_turn("c-hold")
    input_queue.first_turn_not_run("c-hold")
    assert armed == ["c-hold"] and not input_queue.getting_ready("c-hold")
    input_queue.first_turn_not_run("c-hold")
    assert armed == ["c-hold", "c-hold"]

    class _Live:
        is_done = False
    monkeypatch.setitem(stream_pump._active_pumps, "c-hold", _Live())
    input_queue.first_turn_not_run("c-hold")
    assert armed == ["c-hold", "c-hold"]
    # A hold whose kick never came stops counting.
    monkeypatch.setattr(input_queue, "FIRST_TURN_HOLD_S", 0.0)
    input_queue.hold_first_turn("c-hold")
    assert not input_queue.getting_ready("c-hold")


def test_two_authors_on_a_personal_chat_ride_its_next_turn(temp_db, monkeypatch):
    """A personal chat's producer drains every waiting message, whoever
    typed it: a teammate's (through a share) and the owner's go out as one
    turn after the running one."""
    layer, gate, slug = _setup(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-team", "user-manager", TurnInput("from a teammate"))
            ws.client_send({"type": "chat", "text": "from me", "chat_id": chat_id,
                            "queue_id": "q-me"})
            await _until(ws, "queued")
            gate.set()
            await _wait_for(lambda: len(layer.messages) >= 2)
            await asyncio.sleep(0.3)
            assert len(layer.messages) == 2
            second = layer.messages[1][1]
            assert second.index("from a teammate") < second.index("from me")
            assert input_queue.peek(chat_id) == []
    run_ws_scenario(scenario)


def test_on_a_shared_only_chat_each_queued_message_runs_as_its_author(temp_db, monkeypatch):
    """A Shared-only chat runs every message as the person who typed it: the
    producer drains its own person's, and the delivery at its end sends the
    teammate's as the teammate."""
    gate = asyncio.Event()
    calls = {"n": 0}
    layer = FakeExecutionLayer()
    layer.turn_events = _gated_turns(gate, calls)
    stub_dashboard_seams(monkeypatch, layer)
    slug = make_test_agent(default_scope="agent", collaborative=False)
    set_username("user-admin", "admin")
    set_username("user-manager", "manager")
    task_store.set_user_agents("user-manager", [slug], "user-admin",
                               agent_roles={slug: "manager"})

    async def scenario():
        teammate = session_cookie(sub="user-manager", email="manager@test.com",
                                  name="Manager User", role="creator")
        async with dashboard_connection(session_cookie()) as ws, \
                dashboard_connection(teammate) as ws2:
            await drain_startup(ws)
            await drain_startup(ws2)
            chat_id, _sid = await _start(ws, layer, slug)
            ws2.client_send({"type": "chat", "text": "from a teammate", "chat_id": chat_id,
                             "queue_id": "q-team"})
            await _until(ws2, "queued")
            ws.client_send({"type": "chat", "text": "from me", "chat_id": chat_id,
                            "queue_id": "q-me"})
            # Every socket of the chat sees each chip: wait for this one's.
            while (await _until(ws, "queued")).get("queue_id") != "q-me":
                pass
            gate.set()
            await _wait_for(lambda: len(layer.messages) >= 3, timeout=8)
            assert "from me" in layer.messages[1][1]
            assert "from a teammate" not in layer.messages[1][1]
            assert "from a teammate" in layer.messages[2][1]
            await chat_writer.drain(chat_id, timeout=5)
            authors = {m["content"]: m.get("author_sub")
                       for m in task_store.get_chat_messages(chat_id) if m["role"] == "user"}
            assert authors["from me"] == "user-admin"
            assert authors["from a teammate"] == "user-manager"
    run_ws_scenario(scenario)


def test_a_send_typed_while_the_chat_got_ready_never_stops_or_steers_its_first_turn(
        temp_db, monkeypatch):
    """A send the page marks ``queue_only`` (typed during "Getting ready")
    that reaches the proxy after the first turn started follows that turn:
    no steer into it, no stop-and-send interrupt of it."""
    layer, gate, slug = _setup(monkeypatch)
    layer.steer_accepts = True
    layer.interrupt_for_queued_accepts = True

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await _start(ws, layer, slug)
            ws.client_send({"type": "chat", "text": "after the first", "chat_id": chat_id,
                            "queue_id": "q-after", "queue_only": True})
            assert (await _until(ws, "queued"))["queue_id"] == "q-after"
            await asyncio.sleep(0.2)
            assert layer.steered == [] and layer.queued_interrupts == []
            gate.set()
            await _wait_for(lambda: len(layer.messages) == 2)
            assert layer.messages[1][1] == "after the first"
    run_ws_scenario(scenario)


class _OwnTerminal:
    """A live terminal the queued messages' author drives."""

    def __init__(self, chat_id: str, sid: str):
        self.chat_id = chat_id
        self.session_id = sid
        self.alive = True
        self.typed: list[bytes] = []

    def may_drive(self, sub):
        return True

    def deliver_dashboard_input(self, data, *, composer=False, sender_sub=""):
        self.typed.append(data)


def test_a_delivery_types_the_authors_batch_into_their_own_terminal(temp_db, monkeypatch):
    """Sends queued while the person's interactive warmup got ready go into
    the terminal it opened (one paste for the batch, one row for it), never
    left waiting for that terminal to close."""
    from core.session import interactive_session
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            held = q.try_claim()
            ws.client_send({"type": "chat", "text": "into the terminal", "chat_id": chat_id,
                            "queue_id": "q-term"})
            await _until(ws, "queued")
            term = _OwnTerminal(chat_id, sid)
            monkeypatch.setattr(interactive_session, "find_live_for_chat",
                                lambda cid: term if cid == chat_id else None)
            q.release(held)
            await _wait_for(lambda: term.typed)
            assert b"into the terminal" in term.typed[0]
            assert term.typed[0].startswith(b"\x1b[200~") and term.typed[0].endswith(b"\r")
            sent = await _until(ws, "queue_sent")
            assert sent["queue_ids"] == ["q-term"]
            await chat_writer.drain(chat_id, timeout=5)
            assert ("user", "into the terminal") in _rows(chat_id)
            assert task_store.list_chat_input_queue(chat_id) == []
            assert layer.messages == []
    run_ws_scenario(scenario)
