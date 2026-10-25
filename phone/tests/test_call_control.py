"""The call tools on the daemon: ``end_call``, ``transfer_call`` and
``ask_initiating_agent`` arrive as ``call_control`` frames on the per-call
client and are recorded and answered at once; the pipeline acts at the TTS
drain after the words before the tool have played, never at the engine
stream's end. The text markers stay the fallback."""

import asyncio
import contextlib
import json

import pytest
from pipeline_fakes import FakeCallManager, FakeLLM, make_route

from config_manager import transfer_target_for
from proxy.client import TOOL_USE_SIGNAL, ProxyClient
from telephony.transfer import (
    AmiTransfer,
    TransferError,
    TwilioTransfer,
    dial_action_twiml,
    dial_outcome,
    dialable,
    e164,
    loggable_label,
    pending_dials,
    say_voice,
)


class ControlLLM(FakeLLM):
    """A FakeLLM with the client's call-control surface."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.on_call_control = None
        self.results = []          # (request_id, ok, message)
        self.discarded = 0
        self.current_turn = 1

    async def send_call_control_result(self, request_id, ok, message):
        self.results.append((request_id, ok, message))

    def discard_turn(self):
        self.discarded += 1


class FakeTransfer:
    def __init__(self, *, reason="", fail=False):
        self.reason = reason
        self.fail = fail
        self.executed = []
        self.failed_texts = []
        self.languages = []

    def unavailable_reason(self):
        return self.reason

    async def execute(self, number, *, unanswered_text="", unreachable_text="",
                      language="", label=""):
        if self.fail:
            raise TransferError("Redirect refused: no such channel")
        self.executed.append(number)
        self.failed_texts.append(unanswered_text)
        self.unreachable_texts = getattr(self, "unreachable_texts", []) + [unreachable_text]
        self.languages.append(language)


class GatedTransfer(FakeTransfer):
    """A handle whose PBX or vendor answers only when the test says so."""

    def __init__(self, *, fail=False):
        super().__init__(fail=fail)
        self.gate = asyncio.Event()

    async def execute(self, number, **kw):
        await self.gate.wait()
        await super().execute(number, **kw)


def _frame(action, turn=1, request_id="r1", **args):
    return {"type": "call_control", "turn": turn,
            "data": {"request_id": request_id, "action": action, **args}}


def _spoken(p) -> str:
    return "".join(text for text, _ in p.tts.text_chunks)


async def _settle():
    for _ in range(3):
        await asyncio.sleep(0)


def _route(**kw):
    route = make_route(**kw)
    route.transfer_targets = [{"label": "Reception", "number": "200"},
                              {"label": "Maria", "number": "+306912345678"}]
    return route


def _inject_at(p, llm, index, frame):
    """Deliver ``frame`` when token ``index`` streams (the proxy relays the
    tool call while the engine still runs)."""
    def on_token(prompt, i, token):
        if i == index:
            p._on_call_control(frame)
    llm.on_token = on_token


async def _run_turn(p, text):
    p.state._utterance_task = asyncio.current_task()
    await p._process_utterance(text)
    await _settle()


# ---------------------------------------------------------------------------
# end_call
# ---------------------------------------------------------------------------

def test_end_after_the_goodbye_plays_the_goodbye_whole_and_hangs_up(make_pipeline):
    llm = ControlLLM(responses=[[
        "Goodbye, ", "have a nice day! ", TOOL_USE_SIGNAL, "I have ended the call now.",
    ]])
    p = make_pipeline(route=make_route(), llm=llm)
    p._install_call_control()
    p.state._warmup_done.set()
    _inject_at(p, llm, 2, _frame("end", reason="done"))

    asyncio.run(_run_turn(p, "thanks, bye"))

    assert "Goodbye, have a nice day!" in _spoken(p)
    assert "ended the call" not in _spoken(p)
    # The goodbye went to TTS as a sentence chunk before the tool; the
    # boundary must close the streaming context (is_last) or a real provider
    # never speaks it and the drain never comes (live-hit on the T1 walk).
    assert any(is_last for _, is_last in p.tts.text_chunks)
    assert p.conn.hangups == 1 and p.state._running is False
    assert p.state._call_complete is True
    assert llm.results and llm.results[0][1] is True and "say nothing more" in llm.results[0][2]
    assert llm.discarded == 1              # the engine's tail is the proxy's transcript


def test_tool_first_speaks_one_sentence_then_closes(make_pipeline):
    llm = ControlLLM(responses=[[TOOL_USE_SIGNAL, "Goodbye for now. ", "Here is my report: ", "everything went fine."]])
    p = make_pipeline(route=make_route(), llm=llm)
    p._install_call_control()
    p.state._warmup_done.set()
    _inject_at(p, llm, 0, _frame("end"))

    asyncio.run(_run_turn(p, "bye"))

    assert _spoken(p).strip() == "Goodbye for now."
    assert p.conn.hangups == 1


def test_tool_first_with_no_words_plays_the_goodbye_phrase(make_pipeline):
    llm = ControlLLM(responses=[[TOOL_USE_SIGNAL]])
    p = make_pipeline(route=make_route(language="el"), llm=llm)
    p._install_call_control()
    p.state._warmup_done.set()
    _inject_at(p, llm, 0, _frame("end"))
    spoken_phrases = []

    async def synth(text, *, language=None, output_sample_rate=None):
        spoken_phrases.append((text, language))
        return b"\x00\x00" * 160
    p.tts.synthesize = synth

    asyncio.run(_run_turn(p, "αντίο"))

    assert spoken_phrases == [("Αντίο σας.", "el")]
    assert p.conn.sent_audio and p.conn.hangups == 1


def test_a_tool_and_a_marker_in_one_reply_end_once(make_pipeline):
    llm = ControlLLM(responses=[["Bye! [CALL_COMPLETE]", TOOL_USE_SIGNAL]])
    p = make_pipeline(route=make_route(), llm=llm)
    p._install_call_control()
    p.state._warmup_done.set()
    _inject_at(p, llm, 1, _frame("end"))

    asyncio.run(_run_turn(p, "bye"))

    assert "Bye!" in _spoken(p) and "[CALL_COMPLETE]" not in _spoken(p)
    assert p.conn.hangups == 1


def test_the_marker_alone_still_ends_the_call(make_pipeline):
    llm = ControlLLM(responses=[["Goodbye! ", "[CALL_COMPLETE]"]])
    p = make_pipeline(route=make_route(), llm=llm)
    p._install_call_control()
    p.state._warmup_done.set()

    asyncio.run(_run_turn(p, "bye"))

    assert "Goodbye!" in _spoken(p) and p.conn.hangups == 1
    assert llm.results == []


def test_a_frame_for_an_older_turn_is_refused(make_pipeline):
    llm = ControlLLM()
    llm.current_turn = 2
    p = make_pipeline(route=make_route(), llm=llm)
    p._install_call_control()

    async def run():
        p.state._utterance_task = asyncio.current_task()
        p._on_call_control(_frame("end", turn=1))
        await _settle()
    asyncio.run(run())

    assert llm.results[0][1] is False and "interrupted" in llm.results[0][2]
    assert p.state._control_action == ""


def test_a_second_end_is_refused_while_one_is_pending(make_pipeline):
    llm = ControlLLM()
    p = make_pipeline(route=make_route(), llm=llm)
    p._install_call_control()

    async def run():
        p.state._utterance_task = asyncio.current_task()
        p._on_call_control(_frame("end", request_id="a"))
        p._on_call_control(_frame("end", request_id="b"))
        await _settle()
    asyncio.run(run())

    assert [r[1] for r in llm.results] == [True, False]
    assert "already pending" in llm.results[1][2]


def test_an_end_raised_between_turns_hangs_up_after_the_drain(make_pipeline):
    llm = ControlLLM()
    p = make_pipeline(route=make_route(), llm=llm)
    p._install_call_control()
    p.state._running = True

    async def run():
        p._on_call_control(_frame("end"))
        await asyncio.sleep(0.8)
    asyncio.run(run())

    assert p.conn.hangups == 1 and p.state._running is False


def test_a_barge_in_drops_a_pending_end_but_keeps_a_transfer(make_pipeline):
    llm = ControlLLM()
    p = make_pipeline(route=_route(), llm=llm)
    p._transfer = FakeTransfer()
    p._install_call_control()

    async def run():
        p.state._utterance_task = asyncio.current_task()
        p._on_call_control(_frame("end"))
        p._clear_call_control("barge-in", keep_transfer=True)
        assert p.state._control_action == ""
        p._on_call_control(_frame("transfer", request_id="t", target="reception"))
        p._clear_call_control("barge-in", keep_transfer=True)
        assert p.state._control_action == "transfer"
        await _settle()
    asyncio.run(run())


# ---------------------------------------------------------------------------
# transfer_call
# ---------------------------------------------------------------------------

def test_transfer_target_resolution():
    targets = [{"label": "Reception", "number": "200"}, {"label": "Maria", "number": "+306912345678"}]
    assert transfer_target_for(targets, "reception")["number"] == "200"
    assert transfer_target_for(targets, "200")["label"] == "Reception"
    assert transfer_target_for(targets, "0030 6912345678") is None
    assert transfer_target_for(targets, "306912345678")["label"] == "Maria"
    assert transfer_target_for(targets, "Kitchen") is None
    assert transfer_target_for(targets, "") is None
    assert dialable("+306912345678") == "306912345678"
    from telephony.transfer import e164
    assert e164(" +306912345678 ") == "+306912345678"     # Twilio keeps the plus


def test_transfer_after_the_handover_sentence_redirects_and_ends(make_pipeline):
    llm = ControlLLM(responses=[["I'll put you through to reception now. ", TOOL_USE_SIGNAL, "Transferred."]])
    p = make_pipeline(route=_route(), llm=llm)
    handle = FakeTransfer()
    p._transfer = handle
    p._install_call_control()
    p.state._warmup_done.set()
    p.state._running = True
    _inject_at(p, llm, 1, _frame("transfer", target="Reception"))

    asyncio.run(_run_turn(p, "can I talk to someone at reception"))

    assert "put you through" in _spoken(p) and "Transferred." not in _spoken(p)
    assert handle.executed == ["200"]
    assert p.conn.hangups == 0                 # the PBX moved the caller; no hangup frame
    assert p.state._running is False
    assert p.state.call_outcome == "transferred" and p.state._transferred_to == "Reception"
    assert llm.results[0][1] is True and "Reception" in llm.results[0][2]


def test_transfer_to_an_unknown_target_is_refused_with_the_labels(make_pipeline):
    llm = ControlLLM()
    p = make_pipeline(route=_route(), llm=llm)
    p._transfer = FakeTransfer()
    p._install_call_control()

    async def run():
        p.state._utterance_task = asyncio.current_task()
        p._on_call_control(_frame("transfer", target="Kitchen"))
        await _settle()
    asyncio.run(run())

    ok, message = llm.results[0][1], llm.results[0][2]
    assert ok is False and "Kitchen" in message and "Reception, Maria" in message
    assert p.state._control_action == ""


def test_transfer_without_a_handle_or_targets_is_refused(make_pipeline):
    llm = ControlLLM()
    p = make_pipeline(route=_route(), llm=llm)
    p._install_call_control()

    async def run():
        p.state._utterance_task = asyncio.current_task()
        p._on_call_control(_frame("transfer", request_id="a", target="Reception"))
        p.route.transfer_targets = []
        p._on_call_control(_frame("transfer", request_id="b", target="Reception"))
        await _settle()
    asyncio.run(run())

    assert "not available" in llm.results[0][2]
    assert "no transfer targets" in llm.results[1][2]


def test_a_failed_transfer_resumes_the_call_with_the_failure_in_the_next_dispatch(make_pipeline):
    llm = ControlLLM(responses=[["One moment, I'm transferring you. ", TOOL_USE_SIGNAL], ["I'm sorry, I'll stay with you."]])
    p = make_pipeline(route=_route(), llm=llm)
    p._transfer = FakeTransfer(fail=True)
    p._install_call_control()
    p.state._warmup_done.set()
    p.state._running = True
    _inject_at(p, llm, 1, _frame("transfer", target="Reception"))

    asyncio.run(_run_turn(p, "reception please"))

    assert p.conn.hangups == 0 and p.state._running is True
    assert p.state.call_outcome == "completed"
    assert llm.prompts[1].startswith("[The transfer to Reception failed")
    assert "I'll stay with you" in _spoken(p)


def _transfer_turn_closed_under_the_move(make_pipeline, *, fail):
    """The move is requested, the vendor applies it and closes the media
    leg before it answers: run() leaves the listen loop and cleans up while
    the request is in flight (the operator's first Twilio transfer: the
    cleanup cancelled the request and no outcome was ever logged)."""
    llm = ControlLLM(responses=[["Putting you through. ", TOOL_USE_SIGNAL]])
    p = make_pipeline(route=_route(language="el"), llm=llm)
    handle = GatedTransfer(fail=fail)
    p._transfer = handle
    p._install_call_control()
    p.state._warmup_done.set()
    p.state._running = True
    _inject_at(p, llm, 1, _frame("transfer", target="Reception"))

    async def run():
        turn = asyncio.create_task(_run_turn(p, "reception please"))
        for _ in range(300):
            if p.state._transfer_task is not None:
                break
            await asyncio.sleep(0.01)
        assert p.state._transfer_task is not None
        p.conn._closed = True
        cleanup = asyncio.create_task(p._cleanup())
        await asyncio.sleep(0.05)
        handle.gate.set()
        await cleanup
        with contextlib.suppress(asyncio.CancelledError):
            await turn
    asyncio.run(run())
    return p, handle


def test_a_transfer_answered_after_the_line_closed_is_still_recorded(make_pipeline):
    p, handle = _transfer_turn_closed_under_the_move(make_pipeline, fail=False)
    assert handle.executed == ["200"] and handle.languages == ["el"]   # the call's language
    assert p.state.call_outcome == "transferred" and p.state._transferred_to == "Reception"
    assert p.state._transfer_settled is True and p.state._running is False


def test_a_transfer_refused_after_the_line_closed_restores_the_outcome(make_pipeline):
    p, handle = _transfer_turn_closed_under_the_move(make_pipeline, fail=True)
    assert handle.executed == []
    assert p.state.call_outcome == "completed" and p.state._transferred_to == ""
    assert p.state._transfer_settled is True


def test_a_transfer_unanswered_at_the_bound_reads_as_the_move_only_on_a_closed_line(make_pipeline, monkeypatch):
    from pipeline import call_control as plcc
    monkeypatch.setattr(plcc, "TRANSFER_SETTLE_S", 0.05)
    async def move(p, moved):
        p.state._transfer_target = {"label": "Reception", "number": "200"}
        await p._do_transfer(p.state._transfer_target)
        assert await p._settle_transfer() is bool(moved)   # settled once; the same answer again

    for closed, outcome, moved in ((True, "transferred", "Reception"), (False, "completed", "")):
        p = make_pipeline(route=_route(), llm=ControlLLM())
        p._transfer = GatedTransfer()             # never answers
        p._install_call_control()
        p.state._running = True
        p.conn._closed = closed
        asyncio.run(move(p, moved))
        assert (p.state.call_outcome, p.state._transferred_to) == (outcome, moved), closed


def test_ami_transfer_sends_a_redirect_to_from_internal():
    actions = []

    class Client:
        connected = True

        async def send_action(self, action):
            actions.append(action)
            return {"Response": "Success"}

    class Manager:
        def action_client(self, key):
            return Client() if key == "1" else None

    handle = AmiTransfer(Manager(), "1", "1700000000.1", "PJSIP/201-0000001a")
    assert handle.unavailable_reason() == ""
    asyncio.run(handle.execute("+30200"))
    assert actions == [{"Action": "Redirect", "Channel": "PJSIP/201-0000001a",
                        "Context": "from-internal", "Exten": "30200", "Priority": "1"}]
    assert "channel" in AmiTransfer(Manager(), "1", "x", "").unavailable_reason()
    assert "down" in AmiTransfer(Manager(), "9", "x", "PJSIP/1").unavailable_reason()


def test_twilio_transfer_waits_for_the_farewell_then_dials():
    calls = []
    drained = []

    class Rest:
        async def update_call(self, call_sid, twiml):
            calls.append((call_sid, twiml))

    class Conn:
        async def wait_playback_drained(self, timeout):
            drained.append(timeout)
            return True

    sid = "CA" + "0" * 32
    cfg = {"account_sid": "AC1", "auth_token": "tok"}
    pending_dials.clear()
    handle = TwilioTransfer(
        cfg, sid, Conn(), rest_client_factory=lambda c: Rest(),
        caller_id="+16083191947", action_url="https://phone.example.com/v1/twilio/transfer/7",
        report_uuid="row-1")
    assert handle.unavailable_reason() == ""
    asyncio.run(handle.execute("00306912345678", unanswered_text="Nobody & goodbye",
                               unreachable_text="Not possible, goodbye", language="el",
                               label="Manager"))
    assert drained == [3.0]
    assert calls[0][0] == sid
    # The Dial presents the route's own number (a Greek carrier refused the
    # caller's, SIP 500 in zero seconds), names the action that reports its
    # end, dials the 00 form as E.164, and speaks nothing itself: the action's
    # answer decides the goodbye.
    assert calls[0][1] == (
        '<Response><Dial callerId="+16083191947" '
        'action="https://phone.example.com/v1/twilio/transfer/7" method="POST">'
        '+306912345678</Dial></Response>')
    entry = pending_dials.by_call_sid(sid)
    assert entry is not None and entry.report_uuid == "row-1" and entry.label == "Manager"
    assert pending_dials.by_report_uuid("row-1") is entry and not entry.failed
    assert dial_outcome("row-1") == ""
    # Nobody answered: the unanswered goodbye in the call's language and voice.
    assert dial_action_twiml(entry, "no-answer", False) == (
        '<Response><Say language="el-GR" voice="Google.el-GR-Standard-B">Nobody &amp; goodbye</Say>'
        '<Hangup/></Response>')
    # Busy or refused: the unreachable goodbye; the colleague had it: silence.
    assert "Not possible, goodbye" in dial_action_twiml(entry, "failed", False)
    assert "Not possible, goodbye" in dial_action_twiml(entry, "busy", False)
    assert dial_action_twiml(entry, "completed", False) == "<Response><Hangup/></Response>"
    # The caller hung up during the ring: nothing is spoken into a dead call.
    assert dial_action_twiml(entry, "no-answer", True) == "<Response><Hangup/></Response>"
    entry.outcome = "failed"
    assert entry.failed and dial_outcome("row-1") == "transfer_failed"
    entry.outcome = "caller_gone"
    assert not entry.failed and dial_outcome("row-1") == ""
    # No callerId when the route has none; a language outside the table
    # keeps the plain <Say>.
    plain = TwilioTransfer(cfg, sid, Conn(), rest_client_factory=lambda c: Rest())
    asyncio.run(plain.execute("+306912345678", unanswered_text="Adiós", language="pt"))
    assert calls[1][1] == "<Response><Dial>+306912345678</Dial></Response>"
    assert "<Say>Adiós</Say>" in dial_action_twiml(pending_dials.by_call_sid(sid), "no-answer", False)
    assert "call id" in TwilioTransfer(cfg, "bogus", Conn()).unavailable_reason()
    assert "credentials" in TwilioTransfer({}, sid, Conn()).unavailable_reason()
    assert say_voice("de-DE") == ("de-DE", "Google.de-DE-Standard-G") and say_voice("") is None
    assert e164(" 00306974163468 ") == "+306974163468" and e164("+30697") == "+30697"
    assert e164("2101234567") == "2101234567"
    pending_dials.clear()


# ---------------------------------------------------------------------------
# ask_initiating_agent
# ---------------------------------------------------------------------------

class QuestionCallManager(FakeCallManager):
    def __init__(self, call=None, answer="Outdoor seating is fine"):
        super().__init__(call)
        self.questions = []
        self.answer = answer

    def post_question(self, call_id, text):
        self.questions.append(text)
        return True

    async def wait_for_answer(self, call_id, timeout=120):
        return self.answer


def test_ask_is_refused_on_an_inbound_call(make_pipeline):
    llm = ControlLLM()
    p = make_pipeline(route=make_route(), llm=llm)
    p._install_call_control()

    async def run():
        p.state._utterance_task = asyncio.current_task()
        p._on_call_control(_frame("ask", question="Indoor or outdoor?"))
        await _settle()
    asyncio.run(run())
    assert llm.results[0][1] is False and "not placed by another agent" in llm.results[0][2]


def test_ask_relays_the_question_and_the_answer_comes_as_the_next_turn(make_pipeline):
    llm = ControlLLM(responses=[
        ["Let me check that with my office. ", TOOL_USE_SIGNAL, "I'm checking."],
        ["Outdoor it is, see you Saturday. [CALL_COMPLETE]"],
    ])
    manager = QuestionCallManager()
    p = make_pipeline(route=_route(direction="outbound"), llm=llm,
                      call_manager=manager, outbound_call_id="c1")
    p._install_call_control()
    p.state._warmup_done.set()
    _inject_at(p, llm, 1, _frame("ask", question="Indoor or outdoor seating?"))

    async def _noop_silence():
        return None
    p._send_hold_silence = _noop_silence

    asyncio.run(_run_turn(p, "do you want indoor or outdoor"))

    assert manager.questions == ["Indoor or outdoor seating?"]
    assert llm.prompts[1].startswith("[ANSWER FROM MANAGER: Outdoor seating is fine]")
    assert llm.results[0][1] is True and "40 seconds" in llm.results[0][2]
    assert p.conn.hangups == 1


def test_ask_is_absent_on_a_call_inside_the_callers_chat(make_pipeline):
    llm = ControlLLM()
    p = make_pipeline(route=_route(direction="outbound"), llm=llm,
                      call_manager=QuestionCallManager(), outbound_call_id="c1")
    p._in_session = True
    p._install_call_control()

    async def run():
        p.state._utterance_task = asyncio.current_task()
        p._on_call_control(_frame("ask", question="x?"))
        await _settle()
    asyncio.run(run())
    assert llm.results[0][1] is False


# ---------------------------------------------------------------------------
# The client: frames, results, the warmup flag, the discarded turn
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


def _client():
    c = ProxyClient(llm_mode="proxy")
    c._ws = FakeWS()
    c._ws_connected = True
    return c


def test_client_hands_call_control_frames_to_the_handler_whatever_their_turn():
    async def run():
        c = _client()
        seen = []
        c.on_call_control = seen.append
        c._ws.push(_frame("end", turn=7))
        c._ws.push({"type": "call_control", "data": {"request_id": "x", "action": "end"}})
        c._ws.end()
        await c._recv_loop()
        return seen
    seen = asyncio.run(run())
    assert [f["data"]["request_id"] for f in seen] == ["r1", "x"]


def test_client_without_a_handler_refuses_the_frame_at_once():
    async def run():
        c = _client()
        task = asyncio.create_task(c._recv_loop())
        c._ws.push(_frame("end", request_id="q"))
        await asyncio.sleep(0.05)
        c._ws.end()
        await task
        return c._ws.sent
    sent = asyncio.run(run())
    assert sent == [{"type": "call_control_result",
                     "data": {"request_id": "q", "ok": False, "message": "The call is not live yet."}}]


def test_client_warmup_advertises_call_control_and_discard_drops_the_turn():
    async def run():
        c = _client()
        c._ws.push({"type": "warmup_ready", "data": {"session_id": "s1", "llm_mode": "proxy"}})
        c._recv_task = asyncio.create_task(c._recv_loop())
        await c.warmup_session()
        assert c._ws.sent[-1]["call_control"] is True
        assert c._ws.sent[-1]["transfer"] is True            # a line that can hand over
        c._ws.push({"type": "text", "turn": 1, "data": {"content": "a"}})
        gen = c._ws_send_message("hello", system=True)
        first = await gen.__anext__()
        assert first == "a" and c._ws.sent[-1]["system"] is True
        c.discard_turn()
        assert c.turn_in_flight is False and c._turn_queue is None
        await gen.aclose()
        await c.send_call_control_result("r", True, "ok")
        assert c._ws.sent[-1] == {"type": "call_control_result",
                                  "data": {"request_id": "r", "ok": True, "message": "ok"}}
        c._ws.end()
        await c._recv_task
    asyncio.run(run())


# ---------------------------------------------------------------------------
# The opening (pre-warm) and the outcome
# ---------------------------------------------------------------------------

def test_an_end_during_the_opening_makes_the_opening_the_whole_call():
    from calls.call_manager import OutboundCall
    from calls.http_api import OutboundCallAPI

    class Client:
        def __init__(self):
            self.results = []

        async def send_call_control_result(self, rid, ok, message):
            self.results.append((rid, ok, message))

    call = OutboundCall("c1", "+30210", "say hi", "", "u1")
    client = Client()

    async def run():
        OutboundCallAPI._opening_call_control(call, client, _frame("end"))
        OutboundCallAPI._opening_call_control(call, client, _frame("transfer", request_id="t", target="x"))
        await _settle()
    asyncio.run(run())
    assert call.opening_completes_call is True
    assert client.results[0][1] is True and client.results[1][1] is False


def test_an_end_during_the_opening_survives_the_marker_search(monkeypatch):
    """The frame arrives while the opening streams: the flag it set must not
    be overwritten by the marker search at the stream's end (the opening
    would play and the line stay open with the agent told to say no more)."""
    import proxy.client as pc
    from calls.call_manager import CallManager
    from calls.http_api import OutboundCallAPI
    from config_manager import ConfigManager

    class PrewarmClient:
        instances = []

        def __init__(self, **kw):
            self.kw = kw
            self.on_call_control = None
            self.session_id = None
            self.results = []
            self.closed = False
            PrewarmClient.instances.append(self)

        async def connect(self):
            return True

        async def warmup_session(self):
            self.session_id = "sid-pre"
            return self.session_id

        async def send_message(self, text, *, system=False):
            yield "Hello, this is a reminder: your appointment is tomorrow at ten. "
            self.on_call_control(_frame("end", reason="said it"))
            yield "Goodbye."

        async def send_call_control_result(self, rid, ok, message):
            self.results.append((rid, ok, message))

        async def close(self):
            self.closed = True

    monkeypatch.setattr(pc, "ProxyClient", PrewarmClient)
    manager = CallManager()
    api = OutboundCallAPI(manager, ConfigManager())
    call = manager.register_call("+30210", "say the reminder, then hang up")
    route = _route(direction="outbound")

    async def run():
        await api._warmup_caller_session(call.call_id, route)
        await _settle()
    asyncio.run(run())
    client = PrewarmClient.instances[0]
    assert client.results[0][1] is True
    assert call.opening_completes_call is True                 # kept past the marker search
    assert "Goodbye." in (call.opening_text or "") and call.warmup_client is client
    assert client.kw["transfer_available"] is False            # an outbound AMI call names no channel


def test_a_client_that_cannot_transfer_says_so_at_warmup():
    async def run():
        c = ProxyClient(llm_mode="proxy", transfer_available=False)
        c._ws = FakeWS()
        c._ws_connected = True
        c._ws.push({"type": "warmup_ready", "data": {"session_id": "s1", "llm_mode": "proxy"}})
        c._recv_task = asyncio.create_task(c._recv_loop())
        await c.warmup_session()
        c._ws.end()
        await c._recv_task
        return c._ws.sent[-1]
    assert asyncio.run(run())["transfer"] is False


def test_the_twilio_fallback_is_the_unanswered_goodbye(make_pipeline):
    """Twilio speaks the fallback when nobody answers the <Dial>, after the
    agent's stream left: a goodbye, never the "I'm still here" line."""
    handle = FakeTransfer()
    p = make_pipeline(route=_route(direction="inbound"), llm=ControlLLM())
    p._transfer = handle
    p.state._running = True
    asyncio.run(p._do_transfer({"label": "Maria", "number": "+306912345678"}))
    assert handle.executed == ["+306912345678"]
    assert handle.failed_texts == [p.cfg.phrase("en", "transfer_unanswered")]
    assert "Goodbye" in handle.failed_texts[0] and "still here" not in handle.failed_texts[0]


def test_a_transferred_outbound_call_reports_the_transferred_outcome(monkeypatch):
    import proxy.client as pc
    from calls.call_manager import CallManager, CallStatus
    reported = []

    async def fake_report(payload):
        reported.append(payload)
    monkeypatch.setattr(pc, "report_call", fake_report)

    async def run():
        manager = CallManager()
        call = manager.register_call("+30210", "task")
        call.transferred_to = "Reception"
        manager.update_status(call.call_id, CallStatus.COMPLETED, outcome="Transferred to Reception")
        await _settle()
        assert call.to_dict()["transferred_to"] == "Reception"
    asyncio.run(run())
    assert reported[0]["outcome"] == "transferred"


def test_finalize_marks_a_transferred_call_completed_without_a_summary_turn(make_pipeline):
    from calls.call_manager import CallManager, CallStatus
    llm = ControlLLM()
    manager = CallManager()
    call = manager.register_call("+30210", "task")
    p = make_pipeline(route=_route(direction="outbound"), llm=llm,
                      call_manager=manager, outbound_call_id=call.call_id)
    p.state._transferred_to = "Maria"
    asyncio.run(p._finalize_outbound_call())
    assert call.status is CallStatus.COMPLETED and call.transferred_to == "Maria"
    assert llm.prompts == []


def test_finalize_counts_a_pending_end_at_the_callees_hangup_as_completed(make_pipeline):
    from calls.call_manager import CallManager, CallStatus
    llm = ControlLLM(responses=[["Reservation confirmed."]])
    manager = CallManager()
    call = manager.register_call("+30210", "task")
    p = make_pipeline(route=_route(direction="outbound"), llm=llm,
                      call_manager=manager, outbound_call_id=call.call_id)
    p._record_call_control("end", 1, {})
    asyncio.run(p._finalize_outbound_call())
    assert call.status is CallStatus.COMPLETED and call.outcome == "Reservation confirmed."


@pytest.mark.parametrize("key,lang,expected", [
    ("goodbye", "el", "Αντίο σας."),
    ("transferring", "fr", "Un instant, je vous transfère."),
    ("transfer_failed", "xx", "I'm sorry, the transfer didn't go through. I'm still here."),
    ("transfer_unanswered", "el", "Κανείς δεν απάντησε στη μεταφορά. Παρακαλώ καλέστε ξανά αργότερα. Αντίο σας."),
    ("nope", "en", ""),
])
def test_phrases_fall_back_to_english(key, lang, expected):
    from config_manager import ConfigManager
    assert ConfigManager().phrase(lang, key) == expected


def test_a_log_names_a_transfer_target_by_its_label_never_its_number():
    """An unlabelled target's label is its number: the log says so in a
    fixed word (the call record keeps the number, the log never)."""
    assert loggable_label("Manager") == "Manager"
    assert loggable_label("+306912345678") == "an unlabelled target"
    assert loggable_label("2101234567") == "an unlabelled target"
    assert loggable_label("") == "an unlabelled target"


# ---------------------------------------------------------------------------
# The acceptance cuts the turn at once
# ---------------------------------------------------------------------------

class HoldingLLM(ControlLLM):
    """A ControlLLM whose stream holds between tokens: an ``asyncio.Event``
    in a response parks the stream until the test sets it (the engine
    running a tool), or for good."""

    async def send_message(self, text):
        self.prompts.append(text)
        tokens = self._responses.pop(0) if self._responses else []
        for i, token in enumerate(tokens):
            if isinstance(token, asyncio.Event):
                await token.wait()
                continue
            if self.on_token:
                self.on_token(text, i, token)
            yield token


def _accept_later(p, frame, delay=0.05):
    asyncio.get_running_loop().call_later(delay, p._on_call_control, frame)


def _held_pipeline(make_pipeline, tokens, *, route=None):
    llm = HoldingLLM(responses=[tokens])
    p = make_pipeline(route=route or make_route(), llm=llm)
    p._install_call_control()
    p.state._warmup_done.set()
    return p, llm


def test_an_end_accepted_while_the_engine_holds_cuts_the_turn_with_words_at_the_tts(make_pipeline, monkeypatch):
    """The goodbye went to the TTS as a chunk; the engine then parks (the
    tool runs, no frame for seconds). The acceptance alone closes the
    context, leaves the stream and hangs up: the engine never resumes."""
    from pipeline import llm as llm_mod
    monkeypatch.setattr(llm_mod, "TTS_PAUSE_FLUSH_S", 5.0)
    hold = asyncio.Event()
    p, llm = _held_pipeline(make_pipeline, ["Goodbye, have a nice day. ", hold, "I ended it."])

    async def run():
        _accept_later(p, _frame("end"))
        start = asyncio.get_running_loop().time()
        await asyncio.wait_for(_run_turn(p, "thanks, bye"), timeout=3)
        return asyncio.get_running_loop().time() - start
    elapsed = asyncio.run(run())

    assert p.tts.text_chunks == [("Goodbye, have a nice day. ", False), ("", True)]
    assert p.conn.hangups == 1 and p.state._running is False
    assert llm.discarded == 1 and not hold.is_set()
    assert elapsed < 1.5                       # not the stream's end, not a pause timer


def test_an_end_accepted_with_words_in_the_buffer_sends_them_as_the_end_of_input(make_pipeline):
    hold = asyncio.Event()
    p, llm = _held_pipeline(make_pipeline, ["Goodbye", hold])

    async def run():
        _accept_later(p, _frame("end"))
        await asyncio.wait_for(_run_turn(p, "bye"), timeout=3)
    asyncio.run(run())

    assert p.tts.text_chunks == [("Goodbye", True)]
    assert p.conn.hangups == 1 and llm.discarded == 1


def test_an_end_accepted_before_any_words_keeps_the_one_sentence_rule(make_pipeline):
    """Nothing spoken when the acceptance arrives: the wake is consumed and
    the first sentence after the tool's boundary plays, then the line
    closes (the tool-first rule as before)."""
    hold = asyncio.Event()
    p, llm = _held_pipeline(
        make_pipeline, [hold, TOOL_USE_SIGNAL, "Goodbye for now. ", "Here is my report: all fine."])

    async def run():
        _accept_later(p, _frame("end"))
        asyncio.get_running_loop().call_later(0.2, hold.set)
        await asyncio.wait_for(_run_turn(p, "bye"), timeout=3)
    asyncio.run(run())

    assert _spoken(p).strip() == "Goodbye for now."
    assert p.conn.hangups == 1 and llm.discarded == 1
    assert not p.state._control_wake.is_set()


def test_a_transfer_accepted_while_the_engine_holds_cuts_and_redirects(make_pipeline):
    hold = asyncio.Event()
    p, llm = _held_pipeline(
        make_pipeline, ["One moment, I am putting you through to reception. ", hold],
        route=_route())
    p._transfer = FakeTransfer()

    async def run():
        _accept_later(p, _frame("transfer", target="reception"))
        await asyncio.wait_for(_run_turn(p, "reception please"), timeout=3)
    asyncio.run(run())

    assert p.tts.text_chunks[-1] == ("", True)
    assert p._transfer.executed == ["200"] and llm.discarded == 1
    assert p.state._transferred_to == "Reception" and p.state._running is False


def test_an_ask_never_wakes_the_loop(make_pipeline):
    """The agent's one short sentence after an ask must come through: the
    acceptance of an ask sets no wake, the stream is read to its end."""
    cm = QuestionCallManager()
    hold = asyncio.Event()
    llm = HoldingLLM(responses=[["Let me check that. ", hold, "One moment please. "], []])
    p = make_pipeline(route=make_route(direction="outbound"), llm=llm,
                      call_manager=cm, outbound_call_id="c1")
    p._install_call_control()
    p.state._warmup_done.set()
    p._handle_question = lambda q: asyncio.sleep(0, result="Yes, fine")
    woke = []

    async def run():
        def accept():
            p._on_call_control(_frame("ask", question="Is Tuesday ok?"))
            woke.append(p.state._control_wake.is_set())
            hold.set()
        asyncio.get_running_loop().call_later(0.05, accept)
        await asyncio.wait_for(_run_turn(p, "can you do Tuesday"), timeout=3)
    asyncio.run(run())

    assert woke == [False]
    assert llm.results[0][1] is True and "checking" in llm.results[0][2]
    assert "One moment please." in _spoken(p) and llm.discarded == 0


def test_the_acceptance_after_the_tools_own_boundary_closes_the_fresh_context(make_pipeline):
    """The CLI order: the tool_start frame closes the first context and opens
    a fresh one for the text after the tool; the acceptance then arrives
    while the tool runs. The fresh context gets the empty end of input (a
    second close on the first one would change nothing) and the line closes."""
    hold = asyncio.Event()
    p, llm = _held_pipeline(
        make_pipeline, ["Thanks, goodbye.", TOOL_USE_SIGNAL, hold, "Done."])

    async def run():
        _accept_later(p, _frame("end"), delay=0.15)
        await asyncio.wait_for(_run_turn(p, "bye"), timeout=3)
    asyncio.run(run())

    assert p.tts.text_chunks == [("Thanks, goodbye.", True), ("", True)]
    assert p.tts.contexts_started == 2
    assert p.conn.hangups == 1 and llm.discarded == 1


def test_the_farewell_check_ignores_speech_from_before_the_acceptance(make_pipeline):
    """The goodbye played while the context stayed open, the caller said
    "thanks, bye" over that silence, then the tool was accepted: the
    caller's words started BEFORE the acceptance, the line closes."""
    hold = asyncio.Event()
    p, llm = _held_pipeline(make_pipeline, ["Goodbye, have a nice day. ", hold])

    async def run():
        def caller_spoke_then_accept():
            p.state._user_silent.clear()
            p.state._user_silent.set()
            p.state._queued_speech.append("thanks bye")
            p._on_call_control(_frame("end"))
        asyncio.get_running_loop().call_later(0.05, caller_spoke_then_accept)
        await asyncio.wait_for(_run_turn(p, "bye"), timeout=3)
    asyncio.run(run())

    assert p.conn.hangups == 1 and p.state._running is False


def test_the_farewell_check_resumes_on_speech_that_started_after_the_acceptance(make_pipeline):
    hold = asyncio.Event()
    p, llm = _held_pipeline(make_pipeline, ["Goodbye, have a nice day. ", hold])
    llm._responses.append(["Sure, what else? "])

    async def run():
        def accept_then_caller_spoke():
            p._on_call_control(_frame("end"))
            p.state._user_silent.clear()
            p.state._user_silent.set()
            p.state._queued_speech.append("wait one more thing")
        asyncio.get_running_loop().call_later(0.05, accept_then_caller_spoke)
        await asyncio.wait_for(_run_turn(p, "bye"), timeout=3)
    asyncio.run(run())

    assert p.conn.hangups == 0 and p.state._call_complete is False
    assert p.state._control_action == ""                 # the end was dropped
    assert "what else" in _spoken(p)                     # the queued words became a turn


def test_the_parallel_collector_leaves_the_stream_at_the_acceptance(make_pipeline):
    """The parallel path (a reply collected while the previous one plays):
    an acceptance with words already collected ends the collection at once,
    the words before the tool are the whole reply."""
    hold = asyncio.Event()
    llm = HoldingLLM(responses=[["Goodbye now. ", hold, "Report: fine."]])
    p = make_pipeline(route=make_route(), llm=llm)
    p._install_call_control()

    async def run():
        p.state._utterance_task = asyncio.current_task()
        _accept_later(p, _frame("end"))
        text = await asyncio.wait_for(p._collect_llm_response("bye"), timeout=3)
        await _settle()
        return text
    text = asyncio.run(run())

    assert text == "Goodbye now. " and llm.discarded == 1
    assert p.state._parallel_pre_tool_ready.is_set() and not hold.is_set()


def test_words_queued_before_the_control_frame_are_spoken_with_the_cut(make_pipeline):
    """Over the real client: text frames the proxy sent BEFORE the control
    frame sit in the client's queue when the acceptance wakes the loop; they
    are the goodbye's own words and go out with the cut, never left unread."""
    client = _client()
    p = make_pipeline(route=make_route(), llm=client)
    p._install_call_control()
    p.state._warmup_done.set()

    async def run():
        reader = asyncio.create_task(client._recv_loop())
        turn = asyncio.create_task(_run_turn(p, "thanks, bye"))
        await asyncio.sleep(0.05)
        frame = lambda text: {"type": "text", "turn": 1, "data": {"content": text}}
        client._ws.push(frame("Goodbye, "))
        client._ws.push(frame("have a nice day. "))
        client._ws.push(_frame("end", turn=1))
        await asyncio.wait_for(turn, timeout=3)
        client._ws.end()
        await reader
    asyncio.run(run())

    assert _spoken(p) == "Goodbye, have a nice day."        # the cut strips the end of input
    assert p.tts.text_chunks[-1][1] is True and p.conn.hangups == 1
    assert any(f.get("type") == "call_control_result" and f["data"]["ok"] for f in client._ws.sent)


def test_the_farewell_check_resumes_on_a_typed_message_after_the_acceptance(make_pipeline):
    """A duplex message typed after the acceptance has no voice stamp: its
    own stamp counts, the farewell resumes instead of hanging up over it."""
    import time
    hold = asyncio.Event()
    p, llm = _held_pipeline(make_pipeline, ["Goodbye, have a nice day. ", hold])
    llm._responses.append(["Sure, what else? "])

    async def run():
        def accept_then_typed():
            p._on_call_control(_frame("end"))
            p.state._typed_input_at = time.monotonic()
            p.state._queued_speech.append("wait one more thing")
        asyncio.get_running_loop().call_later(0.05, accept_then_typed)
        await asyncio.wait_for(_run_turn(p, "bye"), timeout=3)
    asyncio.run(run())

    assert p.conn.hangups == 0 and "what else" in _spoken(p)
