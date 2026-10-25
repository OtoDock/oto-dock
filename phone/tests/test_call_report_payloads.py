"""The daemon's call-log reports name the warmed proxy session.

All three report sites carry ``session_id`` (empty when the call never
reached the agent) so the proxy can join the call's identity label and the
tools it ran onto the call-log row.
"""

import asyncio

from pipeline_fakes import FakeConn, FakeLLM, make_route

import main
from calls.call_manager import CallManager, CallStatus


def _inbound(**kw):
    return main._inbound_report(
        FakeConn(), "uuid-1", make_route(), {"phone": "+15550001111", "did": "+1608"},
        outcome="completed", pin_attempts=1, started_at="2026-09-06T00:00:00+00:00",
        duration_s=12, **kw,
    )


def test_inbound_report_names_the_session():
    payload = _inbound(session_id="11111111-2222-4333-8444-555555555555")
    assert payload["session_id"] == "11111111-2222-4333-8444-555555555555"
    assert payload["from_number"] == "+15550001111" and payload["direction"] == "inbound"


def test_inbound_report_without_a_session_is_empty_not_missing():
    assert _inbound()["session_id"] == ""
    # A refused call has no LLM client — the getattr guard in _run_call
    # produces "" rather than an AttributeError on ``pipeline.llm is None``.
    assert (getattr(None, "session_id", "") or "") == ""


def test_outbound_terminal_report_carries_the_prewarmed_session(monkeypatch):
    sent = []

    async def _fake_report(payload):
        sent.append(payload)
    import proxy.client as proxy_client
    monkeypatch.setattr(proxy_client, "report_call", _fake_report)

    async def run():
        cm = CallManager()
        call = cm.register_call("+15559998888", "say hi")
        call.route_id = "r1"
        call.warmup_session_id = "22222222-2222-4333-8444-555555555555"
        cm.update_status(call.call_id, CallStatus.COMPLETED)
        await asyncio.sleep(0)   # let the fire-and-forget task run
    asyncio.run(run())
    assert len(sent) == 1
    assert sent[0]["session_id"] == "22222222-2222-4333-8444-555555555555"
    assert sent[0]["direction"] == "outbound" and sent[0]["outcome"] == "completed"
    assert sent[0]["pin_attempts"] == 0


def test_a_transfer_whose_dial_already_failed_reports_transfer_failed(monkeypatch):
    """Twilio's action can answer within the Dial's first second, before the
    daemon's cleanup reports the row: both report sites read the pending
    dial by the row's uuid (the CallSid inbound, the audio uuid outbound)."""
    from telephony.transfer import PendingDial, pending_dials
    sent = []

    async def _fake_report(payload):
        sent.append(payload)
    import proxy.client as proxy_client
    monkeypatch.setattr(proxy_client, "report_call", _fake_report)
    pending_dials.clear()

    async def run():
        cm = CallManager()
        call = cm.register_call("+15559998888", "say hi")
        call.transferred_to = "Manager"
        pending_dials.add(PendingDial(
            call_sid="CAout", report_uuid=call.audio_uuid, label="Manager", language="en",
            unanswered_text="", unreachable_text="", outcome="failed"))
        cm.update_status(call.call_id, CallStatus.COMPLETED)
        await asyncio.sleep(0)
    asyncio.run(run())
    assert [p["outcome"] for p in sent] == ["transfer_failed"]
    conn = FakeConn()
    conn.call_uuid = "CAin"
    pending_dials.add(PendingDial(
        call_sid="CAin", report_uuid="CAin", label="Manager", language="en",
        unanswered_text="", unreachable_text="", outcome="no-answer"))
    assert main.dial_outcome(conn.call_uuid) == "transfer_failed"
    pending_dials.clear()
    assert main.dial_outcome("CAin") == ""


def test_outbound_terminal_report_carries_the_pin_gates_result(monkeypatch):
    """A call the gate refused on a route tied to a user: the gate's outcome
    word and the attempts ride the record into the row, as on an inbound
    call (the daemon rebuilds its windows from them after a restart)."""
    sent = []

    async def _fake_report(payload):
        sent.append(payload)
    import proxy.client as proxy_client
    monkeypatch.setattr(proxy_client, "report_call", _fake_report)

    async def run():
        cm = CallManager()
        call = cm.register_call("+15559998888", "say hi")
        call.pin_attempts, call.pin_outcome = 3, "pin_failed"
        cm.update_status(call.call_id, CallStatus.FAILED,
                         error="the person who answered did not enter the PIN")
        await asyncio.sleep(0)
    asyncio.run(run())
    assert sent[0]["outcome"] == "pin_failed" and sent[0]["pin_attempts"] == 3
    assert sent[0]["to_number"] == "+15559998888"


def test_a_call_the_person_hung_up_reports_hangup(monkeypatch):
    """The record's flag, not its error text, carries the hangup: a PIN
    verdict outranks it, and an ending with neither stays ``failed``."""
    sent = []

    async def _fake_report(payload):
        sent.append(payload)
    import proxy.client as proxy_client
    monkeypatch.setattr(proxy_client, "report_call", _fake_report)

    async def run():
        cm = CallManager()
        for hung_up, pin_outcome, error in (
                (True, "", "the person hung up before the call was completed"),
                (True, "pin_failed", "the person who answered did not enter the PIN"),
                (False, "", "ended after 60 s of silence"),
                (False, "", "call timed out")):
            call = cm.register_call("+15559998888", "say hi")
            call.hung_up, call.pin_outcome = hung_up, pin_outcome
            cm.update_status(call.call_id, CallStatus.FAILED, error=error)
            await asyncio.sleep(0)
    asyncio.run(run())
    assert [p["outcome"] for p in sent] == ["hangup", "pin_failed", "failed", "failed"]


def test_the_pipeline_notes_the_persons_hangup_only_on_an_unfinished_call(make_pipeline):
    """The listen loop's hangup frame and a closed socket mark the call hung
    up unless the agent had ended it (the marker, a pending end), the gate
    had judged it, or an error had: those keep their word."""
    p = make_pipeline(route=make_route(direction="outbound"), llm=FakeLLM())
    p._note_remote_hangup()
    assert p.state.call_outcome == "hangup"
    for outcome, flags in (("completed", {"_call_complete": True}),
                           ("pin_failed", {}), ("error", {}), ("transferred", {})):
        p = make_pipeline(route=make_route(direction="outbound"), llm=FakeLLM())
        p.state.call_outcome = outcome
        for k, v in flags.items():
            setattr(p.state, k, v)
        p._note_remote_hangup()
        assert p.state.call_outcome == outcome
    p = make_pipeline(route=make_route(direction="outbound"), llm=FakeLLM())
    p._record_call_control("end", 1, {})
    p._note_remote_hangup()
    assert p.state.call_outcome == "completed"


def test_a_standalone_calls_record_names_how_it_ended_without_the_marker(make_pipeline):
    """The three endings of the finalize's last branch: the person's hangup
    (the log's ``hangup``), an error, and the idle timeout (both ``failed``)."""
    def _finalize(outcome):
        cm = CallManager()
        call = cm.register_call("+30210", "task")
        p = make_pipeline(route=make_route(direction="outbound"), llm=FakeLLM(),
                          call_manager=cm, outbound_call_id=call.call_id)
        p.state.call_outcome = outcome
        asyncio.run(p._finalize_outbound_call())
        return call
    call = _finalize("hangup")
    assert call.status is CallStatus.FAILED and call.hung_up
    assert call.error == "the person hung up before the call was completed"
    call = _finalize("error")
    assert call.status is CallStatus.FAILED and not call.hung_up and call.error == "ended by an error"
    call = _finalize("completed")
    assert call.status is CallStatus.FAILED and not call.hung_up
    assert call.error.startswith("ended after ") and call.error.endswith(" s of silence")


def test_pipeline_llm_session_id_is_what_the_report_reads():
    llm = FakeLLM(session_id="33333333-2222-4333-8444-555555555555")
    assert (getattr(llm, "session_id", "") or "") == "33333333-2222-4333-8444-555555555555"


def test_a_call_cut_at_its_maximum_duration_ends_its_outbound_record(make_pipeline, monkeypatch):
    """The cap cancels the pipeline before its own terminal edge: the
    outbound record ends FAILED with the reason, so the call log gets its
    row instead of a record left CONNECTED for the collector."""
    from config_manager import ConfigManager
    sent = []

    async def _fake_report(payload):
        sent.append(payload)
    import proxy.client as proxy_client
    monkeypatch.setattr(proxy_client, "report_call", _fake_report)

    cfg = ConfigManager()
    cfg.load({"settings": {"call_max_duration_s": "1"}, "routes": []})
    cm = CallManager()
    monkeypatch.setattr(main, "_call_manager", cm)
    call = cm.register_call("+15559998888", "say hi")
    call.route_id = "r1"

    async def run():
        import logging
        await main._run_call(
            FakeConn(hold_open=True), call.audio_uuid, make_route(direction="outbound"),
            True, None, cfg, logging.getLogger("test"))
        await asyncio.sleep(0)
    asyncio.run(run())

    assert call.status is CallStatus.FAILED
    assert call.error == "ended at the call's maximum duration"
    assert len(sent) == 1 and sent[0]["outcome"] == "failed"
    assert sent[0]["duration_s"] is None or sent[0]["duration_s"] >= 1
