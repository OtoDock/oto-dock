"""Outbound calls that run inside the caller's chat: the grant and the
eligibility (``services/phone/in_session_calls.py``), the relay's body and
its poll refusal, and the attach on ``/ws/phone`` that runs the call as one
long-lived pump on the chat's session (``ws/phone_attach.py``)."""

from __future__ import annotations

import asyncio

import pytest

from core.events.common_events import (
    DONE,
    ERROR,
    PRODUCER_DONE,
    QUEUE_TURN,
    CommonEvent,
)
from services.phone import call_control, in_session_calls, phone_config
from tests.conftest import live_session_token
from tests.phone.test_phone_relay import (  # noqa: F401
    AGENT,
    _FakeResponse,
    _post,
    _route,
    _user,
    fake_daemon,
    relay_app,
)
from tests.phone.test_ws_phone_turns import _run_handler, phone_ws_env  # noqa: F401
from ws import phone_attach

SID = "6f1c2d3e-4b5a-4c6d-8e9f-0a1b2c3d4e5f"
CHAT = "chat-in-session"


@pytest.fixture(autouse=True)
def _clean_registries():
    in_session_calls.reset()
    phone_attach.reset()
    yield
    in_session_calls.reset()
    phone_attach.reset()


@pytest.fixture
def daemon_in_session(monkeypatch):
    """A connected daemon that advertises in-session calls."""
    marker = object()
    phone_config.set_management_capabilities(marker, {"in_session_calls": True, "call_control": True})
    yield marker
    phone_config.clear_management_capabilities(marker)


# ---------------------------------------------------------------------------
# the grant
# ---------------------------------------------------------------------------

def test_a_grant_is_one_per_chat_claimed_by_its_agent_and_session():
    g = in_session_calls.mint(session_id=SID, chat_id=CHAT, agent="a", user_sub="u")
    with pytest.raises(ValueError, match="already running"):
        in_session_calls.mint(session_id=SID, chat_id=CHAT, agent="a", user_sub="u")
    assert in_session_calls.claim(g, agent="b", session_id=SID) is None
    assert in_session_calls.claim(g, agent="a", session_id="other") is None
    assert in_session_calls.claim("nope", agent="a", session_id=SID) is None
    rec = in_session_calls.claim(g, agent="a", session_id=SID)
    assert rec is not None and rec.chat_id == CHAT and rec.claimed
    assert in_session_calls.claim(g, agent="a", session_id=SID) is rec     # a reconnect
    in_session_calls.release(g)
    assert in_session_calls.live_for_chat(CHAT) is None
    assert in_session_calls.claim(g, agent="a", session_id=SID) is None


def test_an_unclaimed_grant_expires(monkeypatch):
    g = in_session_calls.mint(session_id=SID, chat_id=CHAT, agent="a", user_sub="u")
    rec = in_session_calls._grants[g]
    rec.created -= in_session_calls.GRANT_TTL_S + 1
    assert in_session_calls.claim(g, agent="a", session_id=SID) is None
    assert in_session_calls.live_for_chat(CHAT) is None
    # A claimed grant lives until released.
    g2 = in_session_calls.mint(session_id=SID, chat_id=CHAT, agent="a", user_sub="u")
    in_session_calls.claim(g2, agent="a", session_id=SID)
    in_session_calls._grants[g2].created -= in_session_calls.GRANT_TTL_S + 1
    assert in_session_calls.claim(g2, agent="a", session_id=SID) is not None


# ---------------------------------------------------------------------------
# the eligibility
# ---------------------------------------------------------------------------

def _chat(chat_id, owner, *, session_id, agent=AGENT, source_type="chat"):
    from storage import database as task_store
    task_store.create_chat(chat_id, owner, agent, "auto", model="m",
                           execution_path="direct-llm", source_type=source_type)
    task_store.update_chat(chat_id, session_id=session_id)


def _user_route(tied):
    return {"id": "r-x", "direction": "outbound", "agent": AGENT, "enabled": True,
            "identity_mode": "user", "identity_user_sub": tied}


def _eligible(**kw):
    return asyncio.run(in_session_calls.eligible_chat(**kw))


def test_eligibility_needs_the_tied_users_own_dashboard_chat(temp_db, daemon_in_session, monkeypatch):
    tied = _user("tied-e", "editor")
    _chat(CHAT, tied, session_id=SID)
    base = dict(sid=SID, agent=AGENT, user_sub=tied, route=_user_route(tied))
    assert _eligible(**base) == (CHAT, "")
    assert _eligible(**{**base, "route": {**_user_route(tied), "identity_mode": "caller"}})[0] == ""
    assert _eligible(**{**base, "route": _user_route("someone-else")})[0] == ""
    assert _eligible(**{**base, "user_sub": ""})[0] == ""
    assert _eligible(**{**base, "sid": "unknown-sid"})[0] == ""
    other = _user("other-e", "editor")
    _chat("chat-other", other, session_id="sid-other")
    assert _eligible(**{**base, "sid": "sid-other"})[1] == "not the caller's own chat"
    _chat("chat-task", tied, session_id="sid-task", source_type="task")
    assert "task" in _eligible(**{**base, "sid": "sid-task"})[1]
    _chat("chat-agent-y", tied, session_id="sid-y", agent="agent-y")
    assert _eligible(**{**base, "sid": "sid-y"})[1] == "another agent's chat"


def test_eligibility_needs_a_daemon_that_runs_calls_in_chats(temp_db):
    tied = _user("tied-d", "editor")
    _chat(CHAT, tied, session_id=SID)
    chat_id, why = _eligible(sid=SID, agent=AGENT, user_sub=tied, route=_user_route(tied))
    assert chat_id == "" and "daemon" in why


def test_eligibility_refuses_a_terminal_a_spoken_conversation_and_a_busy_chat(
        temp_db, daemon_in_session, monkeypatch):
    tied = _user("tied-b", "editor")
    _chat(CHAT, tied, session_id=SID)
    base = dict(sid=SID, agent=AGENT, user_sub=tied, route=_user_route(tied))
    from core.session import interactive_session
    monkeypatch.setattr(interactive_session, "find_live_for_chat", lambda cid, **kw: object())
    assert "terminal" in _eligible(**base)[1]
    monkeypatch.setattr(interactive_session, "find_live_for_chat", lambda cid, **kw: None)
    assert "spoken" in _eligible(**base, spoken_conversation_live=lambda cid: object())[1]
    assert _eligible(**base, spoken_conversation_live=lambda cid: None) == (CHAT, "")
    in_session_calls.mint(session_id=SID, chat_id=CHAT, agent=AGENT, user_sub=tied)
    assert _eligible(**base)[1] == in_session_calls.BUSY_LINE


# ---------------------------------------------------------------------------
# the relay
# ---------------------------------------------------------------------------

def _tie_route_to(client_sub):
    from storage.phone import phone_route_store
    phone_route_store.update_route(
        "r-x", {"identity_mode": "user", "identity_user_sub": client_sub})


def test_an_eligible_call_rides_the_grant_and_its_poll_is_refused(
        relay_app, fake_daemon, daemon_in_session):  # noqa: F811
    from api.phone import phone_relay
    tied = _user("tied-r", "editor")
    _tie_route_to(tied)
    _chat(CHAT, tied, session_id="sid-1")
    fake_daemon.response = _FakeResponse(
        status_code=202, content=b'{"call_id": "c-9", "status": "dialing", "in_session": true}')
    r = _post(relay_app, tied)
    assert r.status_code == 202, r.text
    body = fake_daemon.calls[0]["json"]
    assert body["in_session"]["session_id"] == "sid-1"
    grant = body["in_session"]["grant"]
    assert in_session_calls.claim(grant, agent=AGENT, session_id="sid-1") is not None
    assert in_session_calls.is_in_session_call("c-9")
    token = live_session_token("sid-1", AGENT, tied)
    w = relay_app.get("/v1/phone/calls/c-9/wait", headers={"Authorization": f"Bearer {token}"})
    assert w.status_code == 409 and "end your turn" in w.json()["detail"]
    # A second call while the first holds the chat is refused before the caps.
    windows_before = sum(len(dq) for dq in phone_relay._call_windows.values())
    r2 = _post(relay_app, tied)
    assert r2.status_code == 409
    assert sum(len(dq) for dq in phone_relay._call_windows.values()) == windows_before


def test_a_daemon_that_ignores_the_field_keeps_the_call_outside(
        relay_app, fake_daemon, daemon_in_session):  # noqa: F811
    tied = _user("tied-o", "editor")
    _tie_route_to(tied)
    _chat(CHAT, tied, session_id="sid-1")
    fake_daemon.response = _FakeResponse(
        status_code=202, content=b'{"call_id": "c-8", "status": "dialing"}')
    assert _post(relay_app, tied).status_code == 202
    assert "in_session" in fake_daemon.calls[0]["json"]
    assert in_session_calls.live_for_chat(CHAT) is None          # the grant went
    assert not in_session_calls.is_in_session_call("c-8")
    token = live_session_token("sid-1", AGENT, tied)
    fake_daemon.response = _FakeResponse(status_code=200, content=b'{"event": "timeout"}')
    w = relay_app.get("/v1/phone/calls/c-8/wait", headers={"Authorization": f"Bearer {token}"})
    assert w.status_code == 200


def test_a_refused_origination_releases_the_grant(relay_app, fake_daemon, daemon_in_session):  # noqa: F811
    tied = _user("tied-f", "editor")
    _tie_route_to(tied)
    _chat(CHAT, tied, session_id="sid-1")
    fake_daemon.response = _FakeResponse(status_code=429, content=b'{"error": "busy"}')
    assert _post(relay_app, tied).status_code == 429
    assert in_session_calls.live_for_chat(CHAT) is None


def test_an_ineligible_call_runs_as_before(relay_app, fake_daemon, daemon_in_session):  # noqa: F811
    ed = _user("ed-plain", "editor")                 # a caller-identity route
    _chat(CHAT, ed, session_id="sid-1")
    fake_daemon.response = _FakeResponse(
        status_code=202, content=b'{"call_id": "c-7", "status": "dialing"}')
    assert _post(relay_app, ed).status_code == 202
    assert "in_session" not in fake_daemon.calls[0]["json"]


# ---------------------------------------------------------------------------
# the attach
# ---------------------------------------------------------------------------

class FakeCallPump:
    """The pump the attach creates: user rows from ``queue_turn`` events,
    ``is_done`` at each turn boundary, ``all_done`` when the producer ends."""

    instances: list = []

    def __init__(self, *, chat_id, session_id, producer, event_queue, perm_queue,
                 scope, source_type):
        self.chat_id = chat_id
        self.source_type = source_type
        self.producer = producer
        self.event_queue = event_queue
        self.rows: list[str] = []
        self.wake_person = ""
        self.queue_closed = True
        self.closed_queue = False
        self._ws_queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._done = False
        FakeCallPump.instances.append(self)

    @property
    def is_done(self):
        return self._done

    def attach(self):
        self._ws_queue = asyncio.Queue()
        return self._ws_queue

    def detach(self, q):
        pass

    def close_queue(self):
        self.closed_queue = True

    def start(self):
        async def _pump():
            from core.events.stream_pump import _active_pumps
            try:
                while True:
                    ev = await self.event_queue.get()
                    if ev.type == QUEUE_TURN:
                        self.rows.append(ev.data["text"])
                        ev.data["accepted"].set_result([len(self.rows)])
                    elif ev.type == DONE:
                        self._ws_queue.put_nowait({"pump_type": "is_done"})
                    elif ev.type == ERROR:
                        self._ws_queue.put_nowait({"pump_type": "error", "message": ev.data.get("message", "")})
                        return
                    elif ev.type == PRODUCER_DONE:
                        self._ws_queue.put_nowait({"pump_type": "all_done"})
                        return
                    else:
                        self._ws_queue.put_nowait({"pump_type": "ws_event",
                                                   "event": {"type": ev.type, **ev.data}})
            finally:
                self._done = True
                if _active_pumps.get(self.chat_id) is self:
                    del _active_pumps[self.chat_id]
        self._task = asyncio.create_task(_pump())
        return self._task


@pytest.fixture
def attach_env(phone_ws_env, monkeypatch):  # noqa: F811
    """The phone socket env plus the attach's collaborators faked: the chat
    row, the role, the layer, the pump and the context text."""
    ws, layer = phone_ws_env
    layer.closed = []

    async def _close(sid):
        layer.closed.append(sid)
    layer.close_session = _close
    layer.alive = True

    async def _alive(sid):
        return layer.alive
    layer.is_session_alive = _alive

    from core.events import chat_writer, stream_pump
    FakeCallPump.instances = []
    stream_pump._active_pumps.clear()
    monkeypatch.setattr(stream_pump, "ChatStreamPump", FakeCallPump)
    # The chat's durable note (the attach ended under the call) and the
    # proxy's close of the daemon's socket, both observable.
    layer.notes = []
    monkeypatch.setattr(stream_pump, "store_chat_wake",
                        lambda cid, text, person="": layer.notes.append((cid, text, person)) or True)
    monkeypatch.setattr(chat_writer, "submit", lambda cid, job, label="": job())
    ws.closed = []

    async def _close_ws(code=1000, reason=""):
        ws.closed.append(code)
        ws.disconnect()
    ws.close = _close_ws
    monkeypatch.setattr(phone_attach.task_store, "get_chat", lambda cid: {
        "id": cid, "session_id": SID, "execution_path": "direct-llm",
        "agent": "unified", "user_sub": "u1"})
    import auth.providers as providers
    monkeypatch.setattr(providers, "acting_role_of", lambda sub, agent: "editor")
    from core.session import session_manager, visibility
    monkeypatch.setattr(session_manager, "get_execution_layer", lambda *a, **kw: layer)
    monkeypatch.setattr(visibility, "is_shared_only", lambda agent: False)
    monkeypatch.setattr(phone_attach, "_context_text", lambda rid, can_transfer=True: "CTX")
    grant = in_session_calls.mint(session_id=SID, chat_id=CHAT, agent="unified",
                                  user_sub="u1", phone_number="+30210")
    return ws, layer, grant


def _attach_warmup(grant, **extra):
    return {"type": "warmup", "model": "unified", "llm_mode": "direct",
            "attach_session_id": SID, "attach_grant": grant, "call_control": True,
            "caller_phone": "+30210", **extra}


async def _pump_finished():
    for _ in range(50):
        if FakeCallPump.instances and FakeCallPump.instances[-1].is_done:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the call's pump did not finish")


def test_an_in_session_call_runs_as_one_pump_on_the_chats_session(attach_env):
    ws, layer, grant = attach_env

    async def run():
        handler = await _run_handler(ws)
        ws.push(_attach_warmup(grant))
        ready = await ws.wait_for_frame(lambda f: f["type"] in ("warmup_ready", "error"))
        assert ready["type"] == "warmup_ready" and ready["data"]["session_id"] == SID
        assert layer.started == [] and layer.admissions == []       # no build, no slot
        binding = call_control.binding_for(SID)
        assert binding is not None and binding.ask_back is False
        # The pipeline adopting the pre-warm client warms up again: same answer.
        ws.push(_attach_warmup(grant))
        await asyncio.sleep(0.05)
        assert sum(1 for f in ws.sent if f["type"] == "warmup_ready") == 2

        # The opening: a system turn, no user row, the context on the first prompt.
        ws.push({"type": "chat", "prompt": "Task: book. The person answered.",
                 "turn": 1, "system": True})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 1)
        pump = FakeCallPump.instances[-1]
        assert pump.rows == [] and pump.wake_person == "u1"
        assert pump.source_type == "phone"
        assert layer.prompts[0].startswith("<system-reminder>\nCTX") and "Task: book" in layer.prompts[0]
        texts = [f for f in ws.sent if f["type"] == "text" and f["turn"] == 1]
        assert texts and "reply-to:" in texts[0]["data"]["content"]

        # The person's words: a prefixed row and the same line to the engine.
        ws.push({"type": "chat", "prompt": "hello, who is this?", "turn": 2})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 2)
        assert pump.rows == ["Phone (+30210): hello, who is this?"]
        assert layer.prompts[1] == "Phone (+30210): hello, who is this?"
        assert len(FakeCallPump.instances) == 1                       # still the one pump

        # The resume note, then the daemon closes: the note runs, the pump ends.
        ws.push({"type": "chat", "prompt": "[The call ended]", "turn": 3,
                 "system": True, "resume": True})
        ws.push({"type": "close"})
        await handler
        await _pump_finished()
        assert layer.prompts[2] == "[The call ended]"
        assert len(layer.prompts) == 3                               # no note of our own
        assert pump.closed_queue
    asyncio.run(run())
    assert layer.closed == []                                        # the chat keeps its session
    assert call_control.binding_for(SID) is None
    assert in_session_calls.live_for_chat(CHAT) is None
    assert phone_attach.live_for_chat(CHAT) is None


def test_a_socket_that_ends_without_the_note_tells_the_chat_itself(attach_env):
    ws, layer, grant = attach_env

    async def run():
        handler = await _run_handler(ws)
        ws.push(_attach_warmup(grant))
        await ws.wait_for_frame(lambda f: f["type"] == "warmup_ready")
        ws.push({"type": "chat", "prompt": "opening", "turn": 1, "system": True})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 1)
        ws.disconnect()
        await handler
        await _pump_finished()
    asyncio.run(run())
    assert len(layer.prompts) == 2 and "phone call ended" in layer.prompts[1]
    assert FakeCallPump.instances[-1].rows == []                     # the note is no user row
    assert layer.closed == [] and phone_attach.live_for_chat(CHAT) is None


def test_a_chat_whose_session_is_not_running_refuses_the_attach(attach_env):
    ws, layer, grant = attach_env
    layer.alive = False

    async def run():
        handler = await _run_handler(ws)
        ws.push(_attach_warmup(grant))
        frame = await ws.wait_for_frame(lambda f: f["type"] in ("warmup_ready", "error"))
        assert frame["type"] == "error" and "not running" in frame["data"]["message"]
        ws.disconnect()
        await handler
    asyncio.run(run())
    assert in_session_calls.live_for_chat(CHAT) is None
    assert call_control.binding_for(SID) is None


def test_an_unknown_grant_refuses_the_attach(attach_env):
    ws, layer, grant = attach_env

    async def run():
        handler = await _run_handler(ws)
        ws.push(_attach_warmup("not-a-grant"))
        frame = await ws.wait_for_frame(lambda f: f["type"] in ("warmup_ready", "error"))
        assert frame["type"] == "error" and "grant" in frame["data"]["message"]
        ws.disconnect()
        await handler
    asyncio.run(run())


def test_a_barge_in_cancels_the_turn_and_the_call_goes_on(attach_env):
    ws, layer, grant = attach_env

    async def run():
        handler = await _run_handler(ws)
        ws.push(_attach_warmup(grant))
        await ws.wait_for_frame(lambda f: f["type"] == "warmup_ready")
        layer.gate = asyncio.Event()
        ws.push({"type": "chat", "prompt": "long one", "turn": 1})
        await ws.wait_for_frame(lambda f: f["type"] == "text" and f.get("turn") == 1)
        ws.push({"type": "abort", "turn": 1})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 1)
        assert len(layer.cancelled) == 1 and layer.cancelled[0].endswith("Phone (+30210): long one")
        layer.gate = None
        ws.push({"type": "chat", "prompt": "sorry, go on", "turn": 2})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 2)
        # The interruption rides the next prompt (the fake layer rebuilds nothing).
        assert layer.prompts[-1].startswith("[The person interrupted")
        assert len(FakeCallPump.instances) == 1
        ws.push({"type": "chat", "prompt": "bye", "turn": 3, "system": True, "resume": True})
        ws.push({"type": "close"})
        await handler
        await _pump_finished()
    asyncio.run(run())
    assert layer.closed == []


def test_the_chat_runs_its_turn_first(attach_env):
    """The make_call turn is still ending when the daemon's opening arrives:
    the attach waits for the chat's pump to end before it starts its own."""
    ws, layer, grant = attach_env
    from core.events import stream_pump

    class LivePump:
        is_done = False
        source_type = "chat"

    live = LivePump()
    stream_pump._active_pumps[CHAT] = live

    async def run():
        handler = await _run_handler(ws)
        ws.push(_attach_warmup(grant))
        await ws.wait_for_frame(lambda f: f["type"] == "warmup_ready")
        ws.push({"type": "chat", "prompt": "opening", "turn": 1, "system": True})
        await asyncio.sleep(0.3)
        assert not [f for f in ws.sent if f["type"] in ("text", "done")]
        live.is_done = True
        del stream_pump._active_pumps[CHAT]
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 1)
        ws.push({"type": "chat", "prompt": "bye", "turn": 2, "system": True, "resume": True})
        ws.push({"type": "close"})
        await handler
        await _pump_finished()
    asyncio.run(run())
    assert layer.prompts and "opening" in layer.prompts[0]


def _erroring_layer(layer, *, dead_after=False):
    """The fake layer's turn errors on a prompt that says so."""
    from core.events.common_events import ERROR, TEXT

    async def send_message(session_id, prompt, **kwargs):
        layer.prompts.append(prompt)
        if "boom" in prompt:
            yield CommonEvent(ERROR, {"message": "the engine fell over"})
            if dead_after:
                layer.alive = False
            return
        yield CommonEvent(TEXT, {"content": f"reply-to:{prompt}"})
    layer.send_message = send_message


def test_an_engine_error_in_one_turn_leaves_the_call_running(attach_env):
    ws, layer, grant = attach_env
    _erroring_layer(layer)

    async def run():
        handler = await _run_handler(ws)
        ws.push(_attach_warmup(grant))
        await ws.wait_for_frame(lambda f: f["type"] == "warmup_ready")
        ws.push({"type": "chat", "prompt": "opening", "turn": 1, "system": True})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 1)
        ws.push({"type": "chat", "prompt": "boom please", "turn": 2})
        err = await ws.wait_for_frame(lambda f: f["type"] == "error" and f.get("turn") == 2)
        assert "fell over" in err["data"]["message"]
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 2)
        # The call goes on: the pump is the same, the next utterance runs.
        ws.push({"type": "chat", "prompt": "are you there?", "turn": 3})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 3)
        assert len(FakeCallPump.instances) == 1 and not FakeCallPump.instances[0].is_done
        assert ws.closed == []
        ws.push({"type": "chat", "prompt": "bye", "turn": 4, "system": True, "resume": True})
        ws.push({"type": "close"})
        await handler
        await _pump_finished()
    asyncio.run(run())
    assert layer.notes == []
    assert phone_attach.live_for_chat(CHAT) is None


def test_a_session_that_dies_under_the_call_ends_it_and_tells_the_chat(attach_env):
    ws, layer, grant = attach_env
    _erroring_layer(layer, dead_after=True)

    async def run():
        handler = await _run_handler(ws)
        ws.push(_attach_warmup(grant))
        await ws.wait_for_frame(lambda f: f["type"] == "warmup_ready")
        ws.push({"type": "chat", "prompt": "opening", "turn": 1, "system": True})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 1)
        ws.push({"type": "chat", "prompt": "boom", "turn": 2})
        await ws.wait_for_frame(lambda f: f["type"] == "error" and f.get("turn") == 2)
        # The proxy closes the daemon's socket: the daemon's client marks the
        # attach lost and the pipeline hangs up with its apology.
        await asyncio.wait_for(handler, timeout=5.0)
        await _pump_finished()
    asyncio.run(run())
    assert ws.closed == [1000]
    assert len(layer.notes) == 1 and "session stopped running" in layer.notes[0][1]
    assert layer.notes[0][0] == CHAT and layer.notes[0][2] == "u1"
    assert layer.closed == []                                        # the chat keeps its session
    assert in_session_calls.live_for_chat(CHAT) is None
    assert phone_attach.live_for_chat(CHAT) is None
    assert call_control.binding_for(SID) is None


def test_a_stop_in_the_chat_ends_the_call_and_tells_the_chat(attach_env):
    ws, layer, grant = attach_env

    async def run():
        handler = await _run_handler(ws)
        ws.push(_attach_warmup(grant))
        await ws.wait_for_frame(lambda f: f["type"] == "warmup_ready")
        ws.push({"type": "chat", "prompt": "opening", "turn": 1, "system": True})
        await ws.wait_for_frame(lambda f: f["type"] == "done" and f.get("turn") == 1)
        attach = phone_attach.live_for_chat(CHAT)
        attach.producer.cancel()                                    # what the dashboard's Stop does
        await asyncio.wait_for(handler, timeout=5.0)
        await _pump_finished()
    asyncio.run(run())
    assert ws.closed == [1000]
    assert len(layer.notes) == 1 and "stopped" in layer.notes[0][1]
    assert phone_attach.live_for_chat(CHAT) is None and in_session_calls.live_for_chat(CHAT) is None


def test_a_chat_that_never_frees_itself_ends_the_call(attach_env, monkeypatch):
    ws, layer, grant = attach_env
    from core.events import stream_pump
    monkeypatch.setattr(phone_attach, "FIRST_CLAIM_WAIT_S", 0.3)

    class LivePump:
        is_done = False
        source_type = "chat"
    stream_pump._active_pumps[CHAT] = LivePump()

    async def run():
        handler = await _run_handler(ws)
        ws.push(_attach_warmup(grant))
        await ws.wait_for_frame(lambda f: f["type"] == "warmup_ready")
        ws.push({"type": "chat", "prompt": "opening", "turn": 1, "system": True})
        await asyncio.wait_for(handler, timeout=5.0)
        del stream_pump._active_pumps[CHAT]
    asyncio.run(run())
    assert ws.closed == [1000]
    assert len(layer.notes) == 1 and "busy" in layer.notes[0][1]
    assert FakeCallPump.instances == []
    assert phone_attach.live_for_chat(CHAT) is None and in_session_calls.live_for_chat(CHAT) is None


def test_the_socket_keeps_reading_while_the_first_claim_waits(attach_env, monkeypatch):
    """The opening waits for the chat's own turn in its own task: a tool
    result, an abort or a close from the daemon is read meanwhile."""
    ws, layer, grant = attach_env
    from core.events import stream_pump
    monkeypatch.setattr(phone_attach, "FIRST_CLAIM_WAIT_S", 30.0)

    class LivePump:
        is_done = False
        source_type = "chat"
    stream_pump._active_pumps[CHAT] = LivePump()

    async def run():
        handler = await _run_handler(ws)
        ws.push(_attach_warmup(grant))
        await ws.wait_for_frame(lambda f: f["type"] == "warmup_ready")
        ws.push({"type": "chat", "prompt": "opening", "turn": 1, "system": True})
        await asyncio.sleep(0.2)
        req = asyncio.create_task(call_control.request(SID, "end", {}))
        sent = await ws.wait_for_frame(lambda f: f["type"] == "call_control", timeout=2.0)
        ws.push({"type": "call_control_result",
                 "data": {"request_id": sent["data"]["request_id"], "ok": True, "message": "closing"}})
        assert (await asyncio.wait_for(req, 2.0))["ok"] is True   # answered during the wait
        ws.push({"type": "close"})
        await asyncio.wait_for(handler, timeout=2.0)              # the close was read at once
        del stream_pump._active_pumps[CHAT]
    asyncio.run(run())
    assert len(layer.notes) == 1                                  # the chat learns of the call
    assert phone_attach.live_for_chat(CHAT) is None


def test_a_cap_refusal_holds_no_grant_and_a_race_answers_409(relay_app, fake_daemon, daemon_in_session, monkeypatch):  # noqa: F811
    from fastapi import HTTPException

    from api.phone import phone_relay
    tied = _user("tied-c", "editor")
    _tie_route_to(tied)
    _chat(CHAT, tied, session_id="sid-1")

    def refuse(caller):
        raise HTTPException(status_code=429, detail="cap")
    take_slots = phone_relay._take_cap_slots
    monkeypatch.setattr(phone_relay, "_take_cap_slots", refuse)
    assert _post(relay_app, tied).status_code == 429
    assert in_session_calls.live_for_chat(CHAT) is None           # the chat may try again
    monkeypatch.setattr(phone_relay, "_take_cap_slots", take_slots)
    # Two make_calls of one chat raced past the eligibility: the second
    # finds the first's grant at the mint and answers the busy line, not 500.
    monkeypatch.setattr(in_session_calls, "live_for_chat", lambda cid: None)
    in_session_calls.mint(session_id="sid-1", chat_id=CHAT, agent=AGENT, user_sub=tied)
    r = _post(relay_app, tied)
    assert r.status_code == 409 and "already running" in r.json()["detail"]


def test_the_dashboard_builder_adds_the_call_tools_for_a_phone_agent(monkeypatch):
    """A dashboard session of an agent with phone-mcp carries the call-control
    MCP: an in-session call needs its tools in that very session."""
    import inspect

    from core.config import config_builder
    src = inspect.getsource(config_builder.build_agent_config)
    assert "CALL_CONTROL_MCP" in src and "PHONE_MCP in _names" in src
    assert 'client_type == session_kind.DASHBOARD.name and not phone_mode' in src


def test_the_in_session_context_cancels_the_question_rule():
    """The outbound rules tell the model to ask the agent that placed the
    call; a call inside the caller's own chat has none, so the context says
    the rule does not apply, last, after the tools block."""
    text = phone_attach._context_text("", False)
    assert "ask_initiating_agent" in text
    assert "does not apply on this call" in text
    assert text.rstrip().endswith("Decide from what you know.")


@pytest.mark.parametrize("body", [b"[1, 2]", b'"queued"', b"not json", b"{}"])
def test_a_daemon_body_that_is_no_object_releases_the_grant(monkeypatch, body):
    """The daemon's answer is read as an object or not at all: a list or a
    string never raises after the call was placed."""
    from fastapi import Response

    from api.phone import phone_relay
    released, remembered = [], []
    monkeypatch.setattr(in_session_calls, "release", released.append)
    monkeypatch.setattr(in_session_calls, "remember_call", lambda cid, grant: remembered.append(cid))
    phone_relay._remember_in_session(Response(content=body), "grant-1")
    assert released == ["grant-1"] and remembered == []
