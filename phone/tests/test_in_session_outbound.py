"""An outbound call that runs inside the caller's chat, daemon side: the
``in_session`` body field, the client's attach warmup, the opening and the
resume note as ``system`` turns, no question cycle, and a call the chat
cannot take ends with an apology."""

import asyncio
import json

import pytest
from pipeline_fakes import FakeLLM, make_route

from calls.call_manager import CallManager, CallStatus
from config_manager import ConfigManager
from duplex import capabilities_frame
from proxy.client import ProxyClient
from validation import ValidationError, validate_outbound_call

GRANT = "a" * 43
SID = "6f1c2d3e-4b5a-4c6d-8e9f-0a1b2c3d4e5f"
BODY = {"phone_number": "+302101234567", "task_description": "book", "route_id": "r-1"}


class NoteLLM(FakeLLM):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.notes = []
        self.on_call_control = None

    async def send_call_control_result(self, request_id, ok, message):
        pass

    async def send_system_note(self, text, *, resume=False):
        self.notes.append((text, resume))
        return True


# ---------------------------------------------------------------------------
# the body
# ---------------------------------------------------------------------------

def test_the_body_carries_the_attach_when_the_relay_sends_one():
    out = validate_outbound_call({**BODY, "in_session": {"session_id": SID, "grant": GRANT}})
    assert out["in_session"] == {"session_id": SID, "grant": GRANT}
    assert "in_session" not in validate_outbound_call(BODY)


@pytest.mark.parametrize("bad", [
    "x", {"session_id": SID}, {"grant": GRANT}, {"session_id": "no spaces", "grant": GRANT},
    {"session_id": SID, "grant": "short"}, {"session_id": SID, "grant": "bad;chars" * 3},
])
def test_a_malformed_attach_is_refused(bad):
    with pytest.raises(ValidationError, match="in_session"):
        validate_outbound_call({**BODY, "in_session": bad})


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------

class FakeWS:
    def __init__(self):
        self.sent = []
        self._queue = asyncio.Queue()

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def push(self, msg):
        self._queue.put_nowait(json.dumps(msg))

    def end(self):
        self._queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


def test_the_warmup_presents_the_grant_and_the_notes_are_system_turns():
    async def run():
        c = ProxyClient(attach={"session_id": SID, "grant": GRANT})
        c._ws = FakeWS()
        c._ws_connected = True
        c._ws.push({"type": "warmup_ready", "data": {"session_id": SID, "llm_mode": "proxy"}})
        c._recv_task = asyncio.create_task(c._recv_loop())
        sid = await c.warmup_session()
        assert sid == SID
        warm = c._ws.sent[-1]
        assert warm["attach_session_id"] == SID and warm["attach_grant"] == GRANT
        assert warm["call_control"] is True
        assert await c.send_system_note("[The call ended]", resume=True) is True
        note = c._ws.sent[-1]
        assert note["type"] == "chat" and note["system"] is True and note["resume"] is True
        assert note["prompt"] == "[The call ended]" and note["turn"] == c.current_turn
        c._ws.end()
        await c._recv_task
    asyncio.run(run())


def test_an_in_session_client_that_cannot_connect_is_lost(monkeypatch):
    import proxy.client as pc

    async def refuse(*a, **kw):
        raise OSError("refused")
    monkeypatch.setattr(pc.websockets, "connect", refuse)

    async def run():
        c = ProxyClient(attach={"session_id": SID, "grant": GRANT})
        assert await c.connect() is False
        assert c.proxy_lost is True
        assert await c.warmup_session() is None
        said = [chunk async for chunk in c.send_message("hello")]
        assert said == []                                        # the pipeline ends the call, no error speech
        assert await c.send_system_note("x") is False
    asyncio.run(run())


def test_a_closed_socket_marks_the_proxy_lost_and_tells_the_pipeline():
    seen = []

    async def run():
        c = ProxyClient(attach={"session_id": SID, "grant": GRANT})
        c._ws = FakeWS()
        c._ws_connected = True
        c.on_proxy_lost = lambda: seen.append("lost")
        c._recv_task = asyncio.create_task(c._recv_loop())
        c._ws.push({"type": "text", "turn": 1, "data": {"content": "a"}})
        gen = c._ws_send_message("hello")
        assert await gen.__anext__() == "a"
        c._ws.end()                                          # the proxy closed the call's socket
        rest = [chunk async for chunk in gen]
        await c._recv_task
        return c, rest
    c, rest = asyncio.run(run())
    assert rest == [] and c.proxy_lost is True and seen == ["lost"]


def test_the_capabilities_frame_names_the_two_features():
    frame = capabilities_frame()
    assert frame["call_control"] is True and frame["in_session_calls"] is True
    assert "duplex" in frame


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------

def _in_session_pipeline(make_pipeline, llm, **state):
    manager = CallManager()
    call = manager.register_call("+30210", "task")
    call.in_session = {"session_id": SID, "grant": GRANT}
    p = make_pipeline(route=make_route(direction="outbound"), llm=llm,
                      call_manager=manager, outbound_call_id=call.call_id)
    p._in_session = True
    for k, v in state.items():
        setattr(p.state, k, v)
    return p, call


def test_finalize_sends_the_resume_note_and_no_summary_turn(make_pipeline):
    llm = NoteLLM()
    p, call = _in_session_pipeline(make_pipeline, llm, _call_complete=True)
    asyncio.run(p._finalize_outbound_call())
    assert llm.prompts == []
    assert len(llm.notes) == 1 and llm.notes[0][1] is True
    note = llm.notes[0][0]
    assert "completed: you ended it" in note and "phone rules no longer apply" in note
    assert call.status is CallStatus.COMPLETED and "Ran in the chat" in (call.outcome or "")


def test_finalize_after_the_persons_hangup_says_so(make_pipeline):
    llm = NoteLLM()
    p, call = _in_session_pipeline(make_pipeline, llm, call_outcome="hangup")
    asyncio.run(p._finalize_outbound_call())
    assert "the person hung up" in llm.notes[0][0]
    assert call.status is CallStatus.FAILED and call.hung_up


def test_finalize_after_the_idle_timeout_names_the_silence(make_pipeline):
    """Nothing ended the call but the line's silence: no hangup is claimed."""
    llm = NoteLLM()
    p, call = _in_session_pipeline(make_pipeline, llm)
    asyncio.run(p._finalize_outbound_call())
    assert "s of silence" in llm.notes[0][0] and "hung up" not in llm.notes[0][0]
    assert call.status is CallStatus.FAILED and not call.hung_up


def test_finalize_after_a_transfer_names_the_colleague(make_pipeline):
    llm = NoteLLM()
    p, call = _in_session_pipeline(make_pipeline, llm, _transferred_to="Maria")
    asyncio.run(p._finalize_outbound_call())
    assert "transferred to Maria" in llm.notes[0][0]
    assert call.status is CallStatus.COMPLETED and call.transferred_to == "Maria"


def test_a_call_the_chat_cannot_take_ends_with_an_apology(make_pipeline):
    llm = NoteLLM(session_id=None)
    p, call = _in_session_pipeline(make_pipeline, llm)
    p.state._warmup_done.set()
    p.state._running = True
    asyncio.run(p._send_outbound_opening())
    assert p.conn.hangups == 1 and p.state._running is False
    assert p.state.call_outcome == "error"
    assert llm.prompts == []                       # no opening on a fresh session
    assert p.conn.sent_audio, "the apology played"
    asyncio.run(p._finalize_outbound_call())
    assert call.status is CallStatus.FAILED and "error" in (call.error or "")


def test_a_question_marker_in_session_is_dropped_not_asked(make_pipeline):
    llm = NoteLLM(responses=[["Let me check. ", "[QUESTION: which day?]", " One moment."]])
    p, call = _in_session_pipeline(make_pipeline, llm)
    p.state._warmup_done.set()
    p.state._running = True
    p._install_call_control()

    async def run():
        p.state._utterance_task = asyncio.current_task()
        await p._process_utterance("can you come on Monday?")
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(run())
    spoken = "".join(text for text, _ in p.tts.text_chunks)
    assert "[QUESTION" not in spoken
    assert call.pending_question is None
    assert p.state._running is True and p.conn.hangups == 0


def test_a_lost_attach_between_turns_ends_the_call_with_the_apology(make_pipeline):
    llm = NoteLLM()
    p, call = _in_session_pipeline(make_pipeline, llm)
    p.state._warmup_done.set()
    p.state._running = True
    p._install_call_control()
    assert llm.on_proxy_lost is not None

    async def run():
        llm.on_proxy_lost()                                  # the client's receive loop ended
        for _ in range(20):
            await asyncio.sleep(0.05)
            if p.conn.hangups:
                break
    asyncio.run(run())
    assert p.conn.hangups == 1 and p.state._running is False
    assert p.conn.sent_audio, "the apology played"
    assert p.state.call_outcome == "error"
    asyncio.run(p._finalize_outbound_call())
    assert call.status is CallStatus.FAILED and "error" in (call.error or "")


def test_a_lost_attach_during_a_turn_ends_the_call_after_it(make_pipeline):
    class LostLLM(NoteLLM):
        async def send_message(self, text, *, system=False):
            self.prompts.append(text)
            self.proxy_lost = True                           # the socket closed mid-stream
            if False:
                yield ""

    llm = LostLLM()
    p, call = _in_session_pipeline(make_pipeline, llm)
    p.state._warmup_done.set()
    p.state._running = True
    p._install_call_control()

    async def run():
        p.state._utterance_task = asyncio.current_task()
        await p._process_utterance("hello?")
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(run())
    assert llm.prompts == ["hello?"]
    assert p.conn.hangups == 1 and p.state._running is False
    spoken = "".join(text for text, _ in p.tts.text_chunks)
    assert "Sorry" not in spoken                             # no error speech, the apology phrase instead
    assert p.conn.sent_audio


def test_the_apology_phrase_exists_in_every_language():
    cfg = ConfigManager()
    for lang in ("en", "el", "de", "es", "fr", "it"):
        assert cfg.phrase(lang, "cannot_continue")
    assert cfg.phrase("pt", "cannot_continue") == cfg.phrase("en", "cannot_continue")
