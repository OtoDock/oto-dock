"""Daemon duplex units: connection routing, LLM client, session wiring.

No ONNX, no sockets, no network — fakes throughout (the conftest
``make_pipeline`` pattern). These pin the daemon half of the duplex
protocol: mic/turn/control frame routing on the dial-back socket, the
ProxyClient-shaped LLM client's turn flow, and DuplexSession's wiring
(duplex rates, fillers keyed at out-rate, telephony extras off).
"""

import asyncio
import json

import pytest

from duplex.connection import DuplexEngineConnection
from duplex.llm_client import DuplexLlmClient
from duplex.manager import _session_stt_provider, capabilities_frame
from proxy.client import TOOL_USE_SIGNAL
from transport.base import FRAME_AUDIO, TransportError


class FakeWs:
    """Async-iterable websocket: scripted inbound, captured outbound."""

    def __init__(self):
        self._incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.closed = False

    def push(self, item):
        self._incoming.put_nowait(item)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._incoming.get()
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        return item

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True


async def _drain_tasks():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_connection_routes_audio_and_respects_mute():
    ws = FakeWs()
    conn = DuplexEngineConnection(ws, "dx1", rate_in=16000, rate_out=24000)
    try:
        assert (conn.sample_rate_in, conn.sample_rate_out) == (16000, 24000)
        assert conn.frame_bytes_out == 960  # 20ms at 24kHz/16-bit

        ws.push(b"\x01\x02")
        await _drain_tasks()
        kind, pcm = await asyncio.wait_for(conn.read_frame(), 1)
        assert kind == FRAME_AUDIO and pcm == b"\x01\x02"

        ws.push(json.dumps({"type": "mute"}))
        await _drain_tasks()
        ws.push(b"\x03\x04")  # dropped while muted
        ws.push(json.dumps({"type": "unmute"}))
        await _drain_tasks()
        ws.push(b"\x05\x06")
        await _drain_tasks()
        kind, pcm = await asyncio.wait_for(conn.read_frame(), 1)
        assert pcm == b"\x05\x06"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_connection_turn_routing_drops_stale_turns():
    ws = FakeWs()
    conn = DuplexEngineConnection(ws, "dx2", rate_in=16000, rate_out=24000)
    try:
        q2 = conn.new_turn(2)
        ws.push(json.dumps({"type": "text", "turn": 1,
                            "data": {"content": "stale"}}))
        ws.push(json.dumps({"type": "text", "turn": 2,
                            "data": {"content": "live"}}))
        await _drain_tasks()
        msg = await asyncio.wait_for(q2.get(), 1)
        assert msg["data"]["content"] == "live"
        assert q2.empty()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_connection_end_frame_surfaces_as_transport_error():
    ws = FakeWs()
    conn = DuplexEngineConnection(ws, "dx3", rate_in=16000, rate_out=24000)
    ws.push(json.dumps({"type": "end"}))
    await _drain_tasks()
    with pytest.raises(TransportError):
        await asyncio.wait_for(conn.read_frame(), 1)
    await conn.close()


@pytest.mark.asyncio
async def test_connection_barge_in_callback_and_send_order():
    ws = FakeWs()
    conn = DuplexEngineConnection(ws, "dx4", rate_in=16000, rate_out=24000)
    try:
        hits = []

        async def _cb():
            hits.append(1)

        conn.on_barge_in = _cb
        ws.push(json.dumps({"type": "barge_in"}))
        await _drain_tasks()
        assert hits == [1]

        conn.send_audio(b"\xaa")
        conn.send_event({"type": "state", "state": "speaking"})
        await asyncio.wait_for(conn.drain(), 1)
        assert ws.sent[0] == b"\xaa"           # one writer: order preserved
        assert json.loads(ws.sent[1])["type"] == "state"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_connection_flush_drops_audio_keeps_events_and_sends_flush():
    """flush_playback (barge-in): queued AUDIO is dropped, queued JSON events
    survive, a {"type":"flush"} frame goes downstream, and the queue's
    join() bookkeeping stays balanced (drain() completes)."""
    ws = FakeWs()
    conn = DuplexEngineConnection(ws, "dx5", rate_in=16000, rate_out=24000)
    try:
        # No awaits between the enqueues and the flush — the writer task
        # can't run in between, so the queue deterministically holds all 3.
        conn.send_audio(b"\xaa")
        conn.send_event({"type": "state", "state": "speaking"})
        conn.send_audio(b"\xbb")
        conn.flush_playback()
        await asyncio.wait_for(conn.drain(), 1)
        assert all(isinstance(s, str) for s in ws.sent)  # no audio escaped
        assert [json.loads(s)["type"] for s in ws.sent] == ["state", "flush"]

        # The connection stays usable: later audio still flows.
        conn.send_audio(b"\xcc")
        await asyncio.wait_for(conn.drain(), 1)
        assert ws.sent[-1] == b"\xcc"
    finally:
        await conn.close()


class FakeConnForLlm:
    peer_addr = "duplex:test"

    def __init__(self):
        self.events: list = []
        self.queue: asyncio.Queue = asyncio.Queue()

    def new_turn(self, turn):
        return self.queue

    def send_event(self, payload):
        self.events.append(payload)


@pytest.mark.asyncio
async def test_llm_client_turn_flow_and_barge_in_chars():
    conn = FakeConnForLlm()
    llm = DuplexLlmClient(conn)
    assert llm.supports_abort is True
    assert llm.abort_erases_turn is False  # interrupt-style: never refold

    conn.queue.put_nowait({"type": "text", "data": {"content": "Hi "}})
    conn.queue.put_nowait({"type": "tool_start", "data": {}})
    conn.queue.put_nowait({"type": "text", "data": {"content": "there."}})
    conn.queue.put_nowait({"type": "done", "data": {}})

    chunks = [c async for c in llm.send_message("hello")]
    assert chunks == ["Hi ", TOOL_USE_SIGNAL, "there."]
    assert conn.events[0]["type"] == "utterance"
    assert conn.events[0]["barge_in_chars"] is None
    assert llm.messages[-1] == {"role": "assistant", "content": "Hi there."}

    # Spoken-chars bookkeeping → next utterance carries barge_in_chars.
    llm.mark_spoken(7)
    llm.annotate_interrupted_response()
    conn.queue.put_nowait({"type": "done", "data": {}})
    _ = [c async for c in llm.send_message("next")]
    assert conn.events[-1]["barge_in_chars"] == 7


@pytest.mark.asyncio
async def test_llm_client_tool_end_is_a_boundary_too():
    """tool_end must yield TOOL_USE_SIGNAL like tool_start: Codex reports
    tool calls only on completion and interactive row feeds land post-hoc,
    so tool_end may be the ONLY boundary a layer ever sends (live-test
    forensics 2026-08-10: three tool_ends, zero tool_starts — the pre-tool
    sentence sat unfinalized in the TTS context until the turn ended)."""
    conn = FakeConnForLlm()
    llm = DuplexLlmClient(conn)

    conn.queue.put_nowait({"type": "text", "data": {"content": "One sec. "}})
    conn.queue.put_nowait({"type": "tool_end", "data": {}})
    conn.queue.put_nowait({"type": "tool_end", "data": {}})
    conn.queue.put_nowait({"type": "text", "data": {"content": "Sunny."}})
    conn.queue.put_nowait({"type": "done", "data": {}})

    chunks = [c async for c in llm.send_message("weather?")]
    assert chunks == [
        "One sec. ", TOOL_USE_SIGNAL, TOOL_USE_SIGNAL, "Sunny."]
    assert llm.messages[-1] == {
        "role": "assistant", "content": "One sec. Sunny."}


@pytest.mark.asyncio
async def test_llm_client_abort_turn_sends_frame():
    """Real barge-in (R3.3): abort_turn sends an abort_turn frame for the
    CURRENT turn on the dial-back socket — fire-and-forget, idempotent
    proxy-side. Before any turn exists it must be a no-op."""
    conn = FakeConnForLlm()
    llm = DuplexLlmClient(conn)

    await llm.abort_turn()
    assert conn.events == []          # no turn yet — nothing sent

    conn.queue.put_nowait({"type": "done", "data": {}})
    _ = [c async for c in llm.send_message("hi")]
    await llm.abort_turn()
    aborts = [e for e in conn.events if e["type"] == "abort_turn"]
    assert aborts == [{"type": "abort_turn", "turn": 1}]


@pytest.mark.asyncio
async def test_interim_pump_streams_live_partial_overlay():
    """The pump samples the STT provider's live partial and sends an
    `interim` frame only on change — the caption OVERLAY. Finalized pieces
    ride `interim_final` push frames instead (R4.6), so the pump no longer
    composes segments. While the mic is held (click-to-edit) the overlay is
    suppressed — late partials must not pollute the composer draft."""
    from duplex.session import DuplexSession
    from pipeline.state import CallState

    class FakeStt:
        latest_interim = ""

    conn = FakeConnForLlm()
    conn.is_closed = False
    conn.is_muted = False
    sess = DuplexSession.__new__(DuplexSession)
    sess.conn = conn
    sess.state = CallState()
    sess.stt = FakeStt()

    task = asyncio.create_task(sess._interim_pump())
    try:
        await asyncio.sleep(0.2)
        assert conn.events == []          # silence → nothing sent

        FakeStt.latest_interim = "hey do"
        await asyncio.sleep(0.2)
        FakeStt.latest_interim = "listen"     # provider committed a final:
        await asyncio.sleep(0.2)              # partial restarts

        texts = [e["text"] for e in conn.events
                 if e.get("type") == "interim"]
        assert texts == ["hey do", "listen"]

        # Provider committed / dispatch — the overlay empties.
        FakeStt.latest_interim = ""
        await asyncio.sleep(0.2)
        assert [e["text"] for e in conn.events
                if e.get("type") == "interim"][-1] == ""

        # Held: partial changes are NOT forwarded.
        conn.is_muted = True
        FakeStt.latest_interim = "late words"
        await asyncio.sleep(0.2)
        assert [e["text"] for e in conn.events
                if e.get("type") == "interim"][-1] == ""
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_partial_final_forwarding_and_gates():
    """R4.6: mid-utterance provider finals forward as `interim_final`
    accumulate chunks — except while held (composer owns the draft) and
    during the STT echo window (TTS playing without early unmute), where
    the pipeline discards the text so the composer must too."""
    from duplex.session import DuplexSession
    from pipeline.state import CallState

    conn = FakeConnForLlm()
    conn.is_closed = False
    conn.is_muted = False
    sess = DuplexSession.__new__(DuplexSession)
    sess.conn = conn
    sess.state = CallState()

    sess._on_stt_partial_final("  hey there  ")
    assert conn.events == [{"type": "interim_final", "text": "hey there"}]

    conn.is_muted = True                       # held → suppressed
    sess._on_stt_partial_final("late final")
    conn.is_muted = False
    sess.state._tts_playing = True             # echo window → suppressed
    sess._on_stt_partial_final("echo text")
    sess.state._stt_early_unmuted = True       # real barge-in speech → kept
    sess._on_stt_partial_final("barge words")
    assert [e["text"] for e in conn.events] == ["hey there", "barge words"]

    # Cancel the orphan-sweep tasks the forwarded finals spawned — they
    # outlive this test's loop otherwise.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_playback_edges_send_speaking_and_listening():
    """R4.2: 'speaking' rides the REAL first-audio edge and 'listening'
    the segment-drain edge — the pipeline hooks, not task creation — so
    'thinking' stays visible until audio starts and the mic reads live
    between segments (silent tool phases) instead of ~4 s late."""
    from duplex.session import DuplexSession
    from pipeline.state import CallState

    conn = FakeConnForLlm()
    conn.is_closed = False
    sess = DuplexSession.__new__(DuplexSession)
    sess.conn = conn
    sess.state = CallState()

    sess._on_tts_playback_start()
    sess._on_tts_playback_end()
    assert [e["state"] for e in conn.events if e["type"] == "state"] == [
        "speaking", "listening"]


class _AbortProbeLlm:
    """LLM stub with configurable abort semantics + an abort counter."""

    def __init__(self, supports_abort=True, abort_erases_turn=False):
        self.supports_abort = supports_abort
        self.abort_erases_turn = abort_erases_turn
        self.aborts = 0

    async def abort_turn(self):
        self.aborts += 1


@pytest.mark.asyncio
async def test_queued_speech_aborts_silent_turn_interrupt_style():
    """R4.1 (the Patras case): speech queued while a turn runs a SILENT
    phase (no TTS playing) aborts the in-flight generation for
    interrupt-style clients — the token loop's own abort branch needs
    tokens flowing, so a loop parked on a tool run never fires it. During
    audible playback the barge-in machinery owns interruption (no abort),
    and erase-style (Direct) clients keep run-to-completion."""
    from duplex.session import DuplexSession
    from pipeline.state import CallState

    sess = DuplexSession.__new__(DuplexSession)
    sess.conn = FakeConnForLlm()
    sess.state = CallState()

    sess.llm = _AbortProbeLlm()
    sess._interrupt_for_queued_speech()
    await asyncio.sleep(0)
    assert sess.llm.aborts == 1

    sess.state._tts_playing = True             # audible → barge-in owns it
    sess._interrupt_for_queued_speech()
    await asyncio.sleep(0)
    assert sess.llm.aborts == 1

    sess.state._tts_playing = False
    sess.llm = _AbortProbeLlm(abort_erases_turn=True)   # erase-style
    sess._interrupt_for_queued_speech()
    await asyncio.sleep(0)
    assert sess.llm.aborts == 0

    sess.llm = _AbortProbeLlm(supports_abort=False)     # no abort at all
    sess._interrupt_for_queued_speech()
    await asyncio.sleep(0)
    assert sess.llm.aborts == 0


@pytest.mark.asyncio
async def test_connection_routes_hold_and_typed_utterance():
    """R3.8 click-to-edit frames: hold mutes AND notifies the session (it
    drops its pending turn); typed_utterance delivers the edited text."""
    ws = FakeWs()
    conn = DuplexEngineConnection(ws, duplex_id="d" * 32,
                                  rate_in=16000, rate_out=24000)
    held, typed = [], []

    async def on_hold():
        held.append(True)

    async def on_typed(text):
        typed.append(text)

    conn.on_hold = on_hold
    conn.on_typed_utterance = on_typed
    try:
        await conn._route_json({"type": "hold"})
        assert conn._muted is True and held == [True]
        await conn._route_json({"type": "unmute"})
        assert conn._muted is False
        await conn._route_json({"type": "typed_utterance", "text": "hi there"})
        assert typed == ["hi there"]
    finally:
        await conn.close()


class FakeSttQueue:
    """STT stub: an internal final queue + ownership-clear accounting."""

    latest_interim = ""

    def __init__(self, finals=None):
        self.finals = list(finals or [])
        self.cleared = 0

    def clear_queue(self):
        self.cleared += 1
        self.finals.clear()

    def drain_transcript(self):
        if not self.finals:
            return None
        parts, self.finals = self.finals, []
        return " ".join(parts)


def _bare_session():
    from duplex.session import DuplexSession
    from pipeline.state import CallState

    sess = DuplexSession.__new__(DuplexSession)
    sess.conn = FakeConnForLlm()
    sess.conn.is_closed = False
    sess.conn.is_muted = False
    sess.state = CallState()
    sess.stt = FakeSttQueue()
    sess._typed_turn = None
    return sess


@pytest.mark.asyncio
async def test_typed_utterance_dispatch_queue_and_hold():
    """Typed text dispatches as its own spoken-mode turn when idle, queues
    like overlapping speech when a turn is busy; BOTH hold and the typed
    send take ownership of the pending utterance (segments, audio buf,
    provider queue) — a Send tap arrives without any prior hold, and a
    copy left daemon-side re-dispatches the same words."""
    sess = _bare_session()
    dispatched: list[str] = []

    async def fake_process(text):
        dispatched.append(text)

    sess._process_utterance = fake_process
    await sess._on_typed_utterance("  hello there  ")
    await asyncio.sleep(0)
    assert dispatched == ["hello there"]
    assert sess.stt.cleared == 1          # typed send owns the utterance

    sess.state._tts_playing = True
    await sess._on_typed_utterance("second")
    assert list(sess.state._queued_speech) == ["second"]
    await sess._on_typed_utterance("second")   # double Send tap
    assert list(sess.state._queued_speech) == ["second"]

    sess.state._turn_segments.extend(["partial words"])
    sess.state._speech_audio_buf.extend(b"xx")
    await sess._on_hold()
    assert sess.state._turn_segments == []
    assert len(sess.state._speech_audio_buf) == 0
    assert sess.stt.cleared == 4          # hold owns it too


@pytest.mark.asyncio
async def test_typed_utterance_interrupts_running_turn():
    """A Send while a turn is IN FLIGHT (utterance task running, nothing
    playing) must queue + abort — NOT dispatch a concurrent turn. The
    concurrent dispatch abandoned the running turn's frame queue (zombie
    task holding _utterance_task forever) and rode the proxy's post-turn
    queue, so the reply only came after the full response (live-hit
    2026-08-11 09:17)."""
    sess = _bare_session()
    sess.llm = _AbortProbeLlm()
    dispatched: list[str] = []

    async def fake_process(text):
        dispatched.append(text)

    sess._process_utterance = fake_process

    async def _parked():
        await asyncio.sleep(30)

    sess.state._utterance_task = asyncio.create_task(_parked())
    try:
        await sess._on_typed_utterance("follow-up question")
        await asyncio.sleep(0)
        assert dispatched == []                       # no concurrent turn
        assert list(sess.state._queued_speech) == ["follow-up question"]
        assert sess.llm.aborts == 1                   # stop-and-send fired
    finally:
        sess.state._utterance_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sess.state._utterance_task


@pytest.mark.asyncio
async def test_orphan_final_sweep_dispatches_and_queues():
    """A provider final with no VAD speech cycle (speech under Silero's
    threshold that the STT still transcribed) must not rot in the queue:
    idle → runs the normal turn machinery; turn in flight → queued + the
    interrupt-style abort (live-hit 2026-08-11 09:17: the sentence tail
    sat in the composer until manually sent, then echoed twice)."""
    sess = _bare_session()
    sess.llm = _AbortProbeLlm()
    timers: list[str] = []

    async def fake_timer(joined):
        timers.append(joined)

    sess._turn_timeout_handler = fake_timer

    # Idle: sweep drains and starts the classify/dispatch timer.
    sess.stt.finals = ["it we're gonna have tomorrow?"]
    task = asyncio.create_task(sess._sweep_orphan_final())
    await asyncio.wait_for(task, 2)
    assert timers == ["it we're gonna have tomorrow?"]
    assert sess.state._turn_segments == ["it we're gonna have tomorrow?"]

    # Busy: sweep queues + aborts instead.
    sess.state._turn_segments.clear()
    sess.state._turn_timer = None
    sess.stt.finals = ["and one more thing"]

    async def _parked():
        await asyncio.sleep(30)

    sess.state._utterance_task = asyncio.create_task(_parked())
    try:
        await asyncio.wait_for(
            asyncio.create_task(sess._sweep_orphan_final()), 2)
        assert list(sess.state._queued_speech) == ["and one more thing"]
        assert sess.llm.aborts == 1
    finally:
        sess.state._utterance_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sess.state._utterance_task

    # Consumed queue / user audibly speaking: sweep no-ops.
    sess.state._queued_speech.clear()
    sess.state._utterance_task = None
    sess.stt.finals = []
    await asyncio.wait_for(asyncio.create_task(sess._sweep_orphan_final()), 2)
    sess.stt.finals = ["mid speech final"]
    sess.state._user_silent.clear()
    await asyncio.wait_for(asyncio.create_task(sess._sweep_orphan_final()), 2)
    assert sess.state._queued_speech == []
    assert timers == ["it we're gonna have tomorrow?"]


def test_duplex_complete_marker_shares_call_complete_machinery():
    """[DUPLEX_COMPLETE] (R3.7) rides the SAME regex as [CALL_COMPLETE]:
    detection sets the farewell flag and every TTS flush site strips it —
    either marker works in either mode."""
    from pipeline.markers import _CALL_COMPLETE_RE

    assert _CALL_COMPLETE_RE.search("Goodnight! [DUPLEX_COMPLETE]")
    assert _CALL_COMPLETE_RE.search("Bye. [CALL_COMPLETE]")
    assert _CALL_COMPLETE_RE.sub("", "Goodnight! [DUPLEX_COMPLETE]") == "Goodnight! "
    assert not _CALL_COMPLETE_RE.search("[DUPLEX_COMPLETED] lookalike")


@pytest.mark.asyncio
async def test_send_hangup_reason_provider():
    """The session wires conn.hangup_reason so a [DUPLEX_COMPLETE] farewell
    ends with reason "agent_complete" (browser skips the exit note) while
    every other hangup stays "engine"."""
    ws = FakeWs()
    conn = DuplexEngineConnection(ws, duplex_id="d" * 32,
                                  rate_in=16000, rate_out=24000)
    try:
        conn.send_hangup()                       # unwired → engine
        conn.hangup_reason = lambda: "agent_complete"
        conn.send_hangup()
        await asyncio.wait_for(conn.drain(), 1)
        ends = [json.loads(f) for f in ws.sent if isinstance(f, str)]
        assert [e["reason"] for e in ends if e["type"] == "end"] == [
            "engine", "agent_complete"]
    finally:
        await conn.close()


def test_session_stt_provider_injects_chat_endpointing():
    cfg = {"stt": {"provider_name": "deepgram",
                   "advanced": {"call_endpointing_ms": 500}},
           "chat_endpointing_ms": 1500}
    row = _session_stt_provider(cfg)
    assert row["advanced"]["call_endpointing_ms"] == 1500
    # original untouched
    assert cfg["stt"]["advanced"]["call_endpointing_ms"] == 500
    assert _session_stt_provider({"stt": None}) is None


def test_capabilities_frame_shape():
    frame = capabilities_frame()
    assert frame["type"] == "capabilities"
    assert frame["duplex"]["rates"] == [16000]
    assert isinstance(frame["duplex"]["supported"], bool)


def test_duplex_session_wiring(monkeypatch):
    from duplex import session as dsession
    from pipeline import providers as plproviders
    from config_manager import ConfigManager
    from pipeline_fakes import FakeSTT, FakeTTS, FakeVAD

    monkeypatch.setattr(dsession, "SileroVad", FakeVAD)
    monkeypatch.setattr(
        plproviders, "get_provider_class",
        lambda kind, name: FakeSTT if kind == "stt" else FakeTTS,
    )

    class FakeDuplexConn:
        call_uuid = "dx-wire-test"
        peer_addr = "duplex:wire"
        sample_rate_in = 16000
        sample_rate_out = 24000
        frame_bytes_out = 960
        is_closed = False
        on_barge_in = None

        def send_event(self, payload):
            pass

    s = dsession.DuplexSession(
        FakeDuplexConn(), ConfigManager(),
        {"stt": {"provider_name": "deepgram"},
         "tts": {"provider_name": "cartesia"},
         "language": "el", "chat_endpointing_ms": 1500},
    )
    assert (s._rate_in, s._rate_out, s._frame_bytes_out) == (16000, 24000, 960)
    assert s.vad.kwargs["sample_rate"] == 16000
    assert s._filler_key[-1] == 24000
    assert s._locked_language == "el"
    assert s._route_settings.backchannel_enabled is False
    assert s._route_settings.thinking_filler_enabled is True
    assert s._ambience is None and s._texture is None and s._breath_pcm is None
    assert s.llm.supports_abort is True
    assert s.llm.abort_erases_turn is False
    assert s._pace_catch_up_s == 1.0            # browser buffers → burst-refill
    assert s.cfg.idle_timeout_s == 600.0        # duplex view, not the call 30s
    assert s.cfg.vad_threshold == 0.40          # passthrough to the base cfg


@pytest.mark.asyncio
async def test_turn_dispatch_hook_sends_final_and_thinking():
    """Every REAL dispatched turn must consume the composer caption
    (`final`) and drive the halo (`thinking`) — the hook fires per pipeline
    loop iteration, so continuation/queued re-dispatches get frames too
    (live-hit 2026-08-12 04:07: a barge-in utterance re-dispatched from the
    queue left its caption stuck in the composer for the whole think phase
    and the halo frozen on the stale phase)."""
    from duplex.session import DuplexSession

    conn = FakeConnForLlm()
    conn.is_closed = False
    sess = DuplexSession.__new__(DuplexSession)
    sess.conn = conn

    sess._on_turn_dispatch("check the weather")
    assert conn.events == [
        {"type": "final", "text": "check the weather"},
        {"type": "state", "state": "thinking"},
    ]

    conn.is_closed = True
    sess._on_turn_dispatch("late")
    assert len(conn.events) == 2  # closed conn → no frames


@pytest.mark.asyncio
async def test_muted_frames_feed_keepalive_and_unmute_heals():
    """2026-08-12 dead-mic incident: while muted/held, dropped mic frames
    must ride a provider keepalive (Deepgram idle-kills after ~12s of
    nothing), and the unmute edge must health-check + reconnect a provider
    that died anyway — the failure was silent and permanent before."""
    ws = FakeWs()
    conn = DuplexEngineConnection(ws, duplex_id="e" * 32,
                                  rate_in=16000, rate_out=24000)
    kept, unmuted = [], []

    async def on_muted_audio(frame_len):
        kept.append(frame_len)

    async def on_unmute():
        unmuted.append(True)

    conn.on_muted_audio = on_muted_audio
    conn.on_unmute = on_unmute
    try:
        # Live: binary frames queue for the pipeline, no keepalive.
        ws.push(b"\x01\x02")
        await asyncio.sleep(0.05)
        assert kept == []
        assert conn._audio_q.qsize() == 1

        # Muted: frames drop but the keepalive callback fires per frame.
        await conn._route_json({"type": "mute"})
        ws.push(b"\x03\x04")
        ws.push(b"\x05\x06")
        await asyncio.sleep(0.05)
        assert kept == [2, 2]  # frame length rides along (silence cadence)
        assert conn._audio_q.qsize() == 1  # nothing new queued

        # Unmute: the heal hook fires once.
        await conn._route_json({"type": "unmute"})
        assert unmuted == [True]
        assert conn.is_muted is False
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_session_mute_hooks_drive_stt_lifecycle():
    """The session wires the connection hooks to the STT provider's own
    no-audio lifecycle: dropped frames feed silence at the mic cadence
    (timeline continuity — audit F16) plus the throttled keepalive, and the
    unmute edge heals via ensure_alive (re-marking STT active on success,
    clearing the muted window's discarded-by-design queue)."""
    from duplex.session import DuplexSession
    from pipeline.state import CallState

    class _SttProbe:
        def __init__(self, ok=True):
            self.ok = ok
            self.keepalives = 0
            self.silence_fed: list = []
            self.recovers: list = []

        async def feed_during_opening(self):
            self.keepalives += 1

        async def feed_during_tts(self, audio_bytes):
            self.silence_fed.append(len(audio_bytes))

        async def ensure_alive(self, language, *, clear_queue=False):
            self.recovers.append((language, clear_queue))
            return self.ok

    sess = DuplexSession.__new__(DuplexSession)
    sess.conn = FakeConnForLlm()
    sess.state = CallState()
    sess.state._stt_active = False
    sess._locked_language = "el"
    sess.stt = _SttProbe()

    await sess._on_muted_audio(640)
    await sess._on_muted_audio(640)
    assert sess.stt.keepalives == 2
    assert sess.stt.silence_fed == [640, 640]

    await sess._on_unmute()
    assert sess.stt.recovers == [("el", True)]
    assert sess.state._stt_active is True

    # A failed reconnect must not mark STT active (and must not raise).
    sess.stt = _SttProbe(ok=False)
    sess.state._stt_active = False
    await sess._on_unmute()
    assert sess.state._stt_active is False


@pytest.mark.asyncio
async def test_orphan_sweep_and_partial_finals_eligible_under_pause():
    """Barge-in pause: _tts_playing stays True while paused, but the paused
    playback's finals ARE the commit evidence — the echo gate must forward
    them and the orphan sweep must stay eligible. Audible (unpaused)
    playback keeps owning the floor: both suppressed."""
    sess = _bare_session()
    sess.llm = _AbortProbeLlm()
    timers: list[str] = []

    async def fake_timer(joined):
        timers.append(joined)

    sess._turn_timeout_handler = fake_timer

    sess.state._tts_playing = True
    sess.state._playback_paused = True
    sess._on_stt_partial_final("commit words")
    assert [e for e in sess.conn.events if e.get("type") == "interim_final"], \
        "paused playback's finals must reach the composer"
    sess.stt.finals = ["commit words"]
    await asyncio.wait_for(asyncio.create_task(sess._sweep_orphan_final()), 2)
    assert timers == ["commit words"]

    # Audible unpaused playback: echo window — both suppressed.
    sess.state._turn_segments.clear()
    sess.state._playback_paused = False
    events_before = len(sess.conn.events)
    sess._on_stt_partial_final("echo words")
    assert len(sess.conn.events) == events_before
    sess.stt.finals = ["echo words"]
    await asyncio.wait_for(asyncio.create_task(sess._sweep_orphan_final()), 2)
    assert timers == ["commit words"]

    # Cancel the sweep task the forwarded final spawned.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_connection_routes_release_with_text():
    """Sticky-mute release (2026-08-24): the frame unmutes AND carries the
    held composer draft to the session callback."""
    ws = FakeWs()
    conn = DuplexEngineConnection(ws, duplex_id="e" * 32,
                                  rate_in=16000, rate_out=24000)
    released: list[str] = []

    async def on_release(text):
        released.append(text)

    conn.on_release = on_release
    try:
        await conn._route_json({"type": "hold"})
        assert conn._muted is True
        await conn._route_json({"type": "release", "text": "held draft"})
        assert conn._muted is False
        assert released == ["held draft"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_release_seeds_draft_and_arms_timer_when_idle():
    """Unmute-after-hold with a draft and nothing running: the draft seeds
    the pending turn and the classifier timer is armed — new speech appends,
    or the timer sends it alone after the grace (operator design
    2026-08-24). The STT heal runs FIRST (its clear touches only
    muted-window artifacts, never the seed)."""
    sess = _bare_session()
    order: list[str] = []

    async def fake_unmute():
        order.append("heal")

    async def fake_timer(text):
        order.append(f"timer:{text}")

    sess._on_unmute = fake_unmute
    sess._turn_timeout_handler = fake_timer
    await sess._on_release("  the held draft  ")
    await asyncio.sleep(0)
    assert sess.state._turn_segments == ["the held draft"]
    assert order == ["heal", "timer:the held draft"]
    assert list(sess.state._queued_speech) == []
    # The seed echoes back through the ACCUMULATE caption channel so the
    # composer keeps showing the held words while the user talks on
    # (operator live-test 2026-08-25: the draft vanished at unmute).
    assert {"type": "interim_final", "text": "the held draft"} in sess.conn.events

    # Empty draft = plain unmute: heal only, nothing seeded.
    sess2 = _bare_session()
    heals: list[bool] = []

    async def fake_unmute2():
        heals.append(True)

    sess2._on_unmute = fake_unmute2
    await sess2._on_release("   ")
    assert heals == [True]
    assert sess2.state._turn_segments == []


@pytest.mark.asyncio
async def test_release_queues_draft_behind_busy_typed_turn_without_abort():
    """Audit F3+F4 (2026-08-24): with a turn in flight — INCLUDING the
    duplex typed lane, invisible to the busy checks before — the released
    draft queues with continuation semantics: no interrupt, no concurrent
    dispatch, consumed when the running turn finishes."""
    sess = _bare_session()
    sess.llm = _AbortProbeLlm()

    async def fake_unmute():
        pass

    sess._on_unmute = fake_unmute

    async def _parked():
        await asyncio.sleep(30)

    sess._typed_turn = asyncio.create_task(_parked())
    try:
        assert sess._turn_busy() is True     # the typed lane counts now
        await sess._on_release("also check patras")
        assert list(sess.state._queued_speech) == ["also check patras"]
        assert sess.state._turn_segments == []
        assert sess.llm.aborts == 0          # continuation, not a commit
    finally:
        sess._typed_turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sess._typed_turn
