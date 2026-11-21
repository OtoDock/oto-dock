"""The dashboard socket with no compute machine: the platform runs no session
itself, so a chat whose session ended can start only on a compute machine.

While the machine reconnects, a chat opens on its held session and a message
waits. While the install's own machine restarted a moment ago, a message
waits for its sign-in. Past that it is refused with the sentence, its row
written first. A new chat, and a chat id the sender may not open, are
refused before any row. During the start's hold an opened chat reads that
OtoDock is starting.
"""

import asyncio

import pytest

import ws.dashboard  # noqa: F401  (resolves the dashboard↔dashboard_chat cycle)
from core.events import chat_writer, input_queue
from core.events.common_events import TurnInput
from storage import database as task_store
from tests.fixtures.ws_dashboard_harness import (
    dashboard_connection,
    drain_startup,
    run_ws_scenario,
    session_cookie,
    warm_new_chat,
)
from tests.session.test_ws_dashboard_input_queue import (
    _fresh_queue,  # noqa: F401  (the module's autouse fixture: a fresh queue registry per test)
    _reconnecting,
    _rows,
    _setup,
    _until,
    _wait_for,
)
from ws import wire_events as wire


def _ended_with_no_machine(monkeypatch, layer, sid: str, *, back_soon: bool) -> None:
    """The chat's session is gone (nobody holds it) and no compute machine
    serves; ``back_soon`` says whether the install's own dropped just now."""
    from services.remote import compute_bootstrap
    from tests._compute import serve_none
    layer.alive.discard(sid)
    layer.started[:] = [(s, c) for s, c in layer.started if s != sid]
    serve_none(monkeypatch)
    monkeypatch.setattr(compute_bootstrap, "back_soon", lambda: back_soon)


def _machine_back(monkeypatch) -> None:
    from services.remote import compute_bootstrap, remote_status
    from tests.fixtures.ws_dashboard_harness import HARNESS_COMPUTE
    monkeypatch.setattr(remote_status, "serving_compute_machine", lambda *a, **k: dict(HARNESS_COMPUTE))
    monkeypatch.setattr(compute_bootstrap, "back_soon", lambda: False)


def test_a_chat_whose_machine_reconnects_is_opened_and_its_message_waits(temp_db, monkeypatch):
    """The compute machine dropped a moment ago: it still holds the chat's
    session in its reconnect window, and nothing can take a new session.
    Opening the chat re-attaches the held session, with no refusal, and a
    message sent there waits in the queue, its chip saying the machine is
    reconnecting. Nothing of the session is touched."""
    from tests._compute import serve_none
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
        _reconnecting(layer, sid)
        serve_none(monkeypatch)
        async with dashboard_connection(session_cookie()) as tab:
            await drain_startup(tab)
            tab.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id})
            seen = []
            while True:
                frame = await tab.next_frame(5.0)
                seen.append(frame["type"])
                if frame["type"] in ("warmup_ready", wire.WARMUP_FAILED, "error"):
                    break
            assert frame["type"] == "warmup_ready" and frame["session_id"] == sid, seen
            tab.client_send({"type": "chat", "text": "while it reconnects", "chat_id": chat_id,
                             "queue_id": "q-grace"})
            queued = await _until(tab, "queued")
            assert queued["waiting"] == input_queue.WAITING_RECONNECT and queued["queue_id"] == "q-grace"
            assert layer.prepared_resume == [] and [s for s, _ in layer.started] == [sid]
            assert layer.messages == []
    run_ws_scenario(scenario)


def test_a_send_in_a_chat_whose_session_ended_waits_for_the_compute_machines_return(
        temp_db, monkeypatch):
    """The install's own compute machine restarted a moment ago: a message
    in an existing chat waits in the chat's queue, its chip saying the
    machine is reconnecting, nothing of the session is touched, and it goes
    out at the machine's sign-in."""
    from services.remote import compute_bootstrap
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    monkeypatch.setattr(input_queue, "VIEWER_GRACE_S", 0.05)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            _ended_with_no_machine(monkeypatch, layer, sid, back_soon=True)
            ws.client_send({"type": "chat", "text": "during the restart", "chat_id": chat_id,
                            "queue_id": "q-restart"})
            queued = await _until(ws, "queued")
            assert queued["waiting"] == input_queue.WAITING_RECONNECT
            assert queued["queue_id"] == "q-restart" and queued["text"] == "during the restart"
            await _until(ws, "done")
            await asyncio.sleep(0.2)          # the armed delivery defers too
            assert layer.prepared_resume == [] and layer.started == [] and layer.messages == []
            assert [r["text"] for r in task_store.list_chat_input_queue(chat_id)] == ["during the restart"]
            q = await input_queue.loaded(chat_id)
            assert q.items[0].waiting == input_queue.WAITING_RECONNECT
            # The machine signs in: every chat with waiting messages is
            # armed and the message goes out on a session started there.
            _machine_back(monkeypatch)
            await compute_bootstrap._arm_waiting()
            await _wait_for(lambda: len(layer.messages) == 1)
            assert layer.messages[0][1] == "during the restart"
            assert task_store.list_chat_input_queue(chat_id) == []
    run_ws_scenario(scenario)


def test_a_cold_send_on_an_existing_chat_waits_for_the_compute_machines_return(temp_db, monkeypatch):
    """The same wait through a page that opens the chat with its message
    (the warmup frame carries the text): no card, the chip, and the message
    out at the sign-in."""
    from services.remote import compute_bootstrap
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    monkeypatch.setattr(input_queue, "VIEWER_GRACE_S", 0.05)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
        _ended_with_no_machine(monkeypatch, layer, sid, back_soon=True)
        async with dashboard_connection(session_cookie()) as tab:
            await drain_startup(tab)
            tab.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id,
                             "text": "sent cold", "queue_id": "q-cold"})
            seen = []
            while True:
                frame = await tab.next_frame(5.0)
                seen.append(frame["type"])
                if frame["type"] == "queued":
                    break
            assert frame["waiting"] == input_queue.WAITING_RECONNECT and frame["text"] == "sent cold"
            assert wire.WARMUP_FAILED not in seen and "error" not in seen
            await asyncio.sleep(0.2)
            assert layer.started == [] and layer.messages == []
            _machine_back(monkeypatch)
            await compute_bootstrap._arm_waiting()
            await _wait_for(lambda: len(layer.messages) == 1)
            assert layer.messages[0][1] == "sent cold"
    run_ws_scenario(scenario)


def test_past_the_restart_window_a_message_is_refused_with_the_sentence(temp_db, monkeypatch):
    """The compute machine is away longer than a restart takes: an existing
    chat's message is not queued. A cold send gets the card with the
    sentence and what became of the message, a send on an open chat the
    sentence, and no session is started or resumed."""
    from core import placement
    from ws.dashboard_chat_text import NO_COMPUTE_NOT_SENT
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            _ended_with_no_machine(monkeypatch, layer, sid, back_soon=False)
            ws.client_send({"type": "chat", "text": "nobody home", "chat_id": chat_id})
            failed = await _until(ws, wire.WARMUP_FAILED)
            assert failed["error"] == placement.NO_COMPUTE_SENTENCE
            assert failed["reason"] == wire.REASON_TARGET_UNAVAILABLE
            assert layer.started == [] and layer.messages == []
            assert task_store.list_chat_input_queue(chat_id) == []
        async with dashboard_connection(session_cookie()) as tab:
            await drain_startup(tab)
            tab.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id, "text": "sent cold"})
            failed = await _until(tab, wire.WARMUP_FAILED)
            assert failed["error"] == NO_COMPUTE_NOT_SENT and failed["chat_id"] == chat_id
            assert failed["reason"] == wire.REASON_TARGET_UNAVAILABLE
            assert layer.started == [] and layer.messages == []
            # The message's row is there, above the card that says it was not sent.
            await chat_writer.drain(chat_id)
            assert ("user", "sent cold") in _rows(chat_id)
            assert task_store.list_chat_input_queue(chat_id) == []
    run_ws_scenario(scenario)


def test_during_the_starts_hold_an_opened_chat_reads_that_otodock_is_starting(temp_db, monkeypatch):
    """In the first minute after a start the install's compute machine has
    not signed in yet. A page that opens a chat whose session ended (a
    terminal's start, a banner's button) gets the card with the starting
    line, never the sentence about the compute machine. A message sent cold
    waits in the queue, as in every hold."""
    from core import drain
    layer, gate, slug = _setup(monkeypatch)
    gate.set()

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
        _ended_with_no_machine(monkeypatch, layer, sid, back_soon=False)
        try:
            async with dashboard_connection(session_cookie()) as tab:
                await drain_startup(tab)
                drain.hold_for_start()
                tab.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id})
                failed = await _until(tab, wire.WARMUP_FAILED)
                assert failed["error"] == drain.STARTING_LINE and failed["chat_id"] == chat_id
                assert failed["reason"] == wire.REASON_TARGET_UNAVAILABLE
                tab.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id, "text": "sent in the hold",
                                 "queue_id": "q-hold"})
                queued = await _until(tab, "queued")
                assert queued["waiting"] == input_queue.WAITING_UPDATING and queued["queue_id"] == "q-hold"
                assert layer.started == [] and layer.messages == []
        finally:
            drain.drop_start_hold()
    run_ws_scenario(scenario)


def test_a_new_chat_with_no_text_reads_the_holds_line_during_a_hold(temp_db, monkeypatch):
    """A terminal chat's start, opened in the first minute after a start or
    while a backup has the compute service stopped: the hold's own line, as
    every other door says it, never the sentence about the compute machine.
    Outside a hold it is the sentence."""
    from core import drain, placement
    from tests._compute import serve_none
    layer, gate, slug = _setup(monkeypatch)
    serve_none(monkeypatch)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            before = {c["id"] for c in task_store.list_chats("user-admin", agent=slug, limit=50)}
            ws.client_send({"type": "warmup", "agent": slug})
            assert placement.NO_COMPUTE_SENTENCE in (await _until(ws, "error"))["message"]
            drain.hold_for_start()
            try:
                ws.client_send({"type": "warmup", "agent": slug})
                assert (await _until(ws, "error"))["message"] == drain.STARTING_LINE
            finally:
                drain.drop_start_hold()
            await drain.drain(0)
            try:
                ws.client_send({"type": "warmup", "agent": slug})
                assert (await _until(ws, "error"))["message"] == wire.PLATFORM_UPDATING
            finally:
                await drain.resume()
            assert {c["id"] for c in task_store.list_chats("user-admin", agent=slug, limit=50)} == before
            assert layer.started == []
    run_ws_scenario(scenario)


def test_a_page_that_opens_a_chat_whose_message_waits_never_reads_that_it_was_not_sent(temp_db, monkeypatch):
    """The message is in the chat's queue with its chip, which says that it
    goes out when the machine is back. It will be sent: a card that said
    otherwise would have the person send it twice."""
    from core import placement
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    monkeypatch.setattr(input_queue, "VIEWER_GRACE_S", 0.05)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
            _ended_with_no_machine(monkeypatch, layer, sid, back_soon=True)
            ws.client_send({"type": "chat", "text": "it waits", "chat_id": chat_id, "queue_id": "q-waits"})
            assert (await _until(ws, "queued"))["waiting"] == input_queue.WAITING_RECONNECT
            await _until(ws, "done")
        # The machine stays away past its window, and another page opens the chat.
        _ended_with_no_machine(monkeypatch, layer, sid, back_soon=False)
        async with dashboard_connection(session_cookie()) as tab:
            await drain_startup(tab)
            tab.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id})
            failed = await _until(tab, wire.WARMUP_FAILED)
            assert failed["error"] == placement.NO_COMPUTE_SENTENCE and "was not sent" not in failed["error"]
            assert [r["text"] for r in task_store.list_chat_input_queue(chat_id)] == ["it waits"]
            assert (await input_queue.loaded(chat_id)).items[0].waiting == input_queue.WAITING_RECONNECT
    run_ws_scenario(scenario)


def test_the_sign_in_leaves_a_queue_that_is_held_for_a_waiter_alone(temp_db, monkeypatch):
    """A chat whose reply a usage limit cut keeps its queue held for the
    continuation's waiter (``hold_first_turn``), so a message that waited
    goes first on the new account. A compute machine's sign-in arms every
    chat with waiting messages: the held one is left alone, its message
    stays queued and the hold stands. Once the waiter ended the hold, the
    next arming sends the message."""
    from services.remote import compute_bootstrap
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    monkeypatch.setattr(input_queue, "VIEWER_GRACE_S", 0.05)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, _sid = await warm_new_chat(ws, layer, slug)
            q = await input_queue.loaded(chat_id)
            await q.add("q-held", "user-admin", TurnInput("waits for the new account"))
            input_queue.hold_first_turn(chat_id)
            await compute_bootstrap._arm_waiting()
            await asyncio.sleep(0.3)
            assert layer.messages == [] and [qi.queue_id for qi in q.items] == ["q-held"]
            assert input_queue.getting_ready(chat_id), "the waiter's hold stands"
            input_queue.first_turn_started(chat_id)
            await compute_bootstrap._arm_waiting()
            await _wait_for(lambda: len(layer.messages) == 1)
            assert layer.messages[0][1] == "waits for the new account"
    run_ws_scenario(scenario)


def test_a_chat_id_of_another_person_is_refused_as_a_new_chat_and_keeps_nothing(temp_db, monkeypatch):
    """A cold send that names a chat the sender may not open, while the
    compute machine restarts: it is refused as a new chat is, with the
    sentence. Nothing is queued in the other person's chat and no row is
    written there, wait or no wait."""
    from core import placement
    layer, gate, slug = _setup(monkeypatch)
    gate.set()
    from storage.identity import db_users
    db_users.add_user_agent("user-manager", slug, "manager", "user-admin")

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            chat_id, sid = await warm_new_chat(ws, layer, slug)
        _ended_with_no_machine(monkeypatch, layer, sid, back_soon=True)
        before = _rows(chat_id)
        other = session_cookie(sub="user-manager", email="manager@test.com", name="Manager", role="member")
        async with dashboard_connection(other) as tab:
            await drain_startup(tab)
            tab.client_send({"type": "warmup", "agent": slug, "chat_id": chat_id,
                             "text": "into another person's chat", "queue_id": "q-other"})
            seen = []
            while True:
                frame = await tab.next_frame(5.0)
                seen.append(frame["type"])
                if frame["type"] in ("queued", "warmup_ready", wire.WARMUP_FAILED, "error"):
                    break
            assert frame["type"] == "error" and placement.NO_COMPUTE_SENTENCE in frame["message"], seen
        await chat_writer.drain(chat_id)
        assert _rows(chat_id) == before
        assert task_store.list_chat_input_queue(chat_id) == []
        assert layer.started == [] and layer.messages == []
    run_ws_scenario(scenario)


@pytest.mark.parametrize("engine", ["", "direct-llm"])
@pytest.mark.parametrize("back_soon", [True, False])
def test_a_new_chat_with_no_compute_machine_is_refused_before_any_row(temp_db, monkeypatch, back_soon, engine):
    """A new chat cannot wait anywhere: it is refused with the sentence
    before its chat exists, restart or not, and no session is started. The
    same on Direct LLM, whose model runs in the proxy: its tools are part
    of the session and need the machine."""
    from core import placement
    from services.remote import compute_bootstrap
    from tests._compute import serve_none
    layer, gate, slug = _setup(monkeypatch)
    serve_none(monkeypatch)
    monkeypatch.setattr(compute_bootstrap, "back_soon", lambda: back_soon)

    async def scenario():
        async with dashboard_connection(session_cookie()) as ws:
            await drain_startup(ws)
            before = {c["id"] for c in task_store.list_chats("user-admin", agent=slug, limit=50)}
            ws.client_send({"type": "warmup", "agent": slug, "text": "hello",
                            **({"execution_path": engine} if engine else {})})
            error = await _until(ws, "error")
            assert placement.NO_COMPUTE_SENTENCE in error["message"]
            assert {c["id"] for c in task_store.list_chats("user-admin", agent=slug, limit=50)} == before
            assert layer.started == [] and layer.messages == []
    run_ws_scenario(scenario)
