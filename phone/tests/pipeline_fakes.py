"""Lightweight fakes for the heavy collaborators a CallPipeline injects.

These implement only the surface the pipeline actually touches, so pipeline
logic can be driven offline — no ONNX, no provider SDKs, no network. Grow them
as later smoke scenarios need more of the contract.

Contracts mirrored here:
  - AudioSocketConnection  (phone/telephony/audio_socket.py)
  - TTSProvider            (audio/providers/tts/base.py)
  - STTProvider            (audio/providers/stt/base.py)
  - Silero VAD             (audio/providers/vad/silero.py — set_bargein_mode surface)
  - ProxyClient LLM backend / outbound CallManager + call object
"""


class FakeConn:
    """Stand-in for AudioSocketConnection.

    ``hold_open=True`` makes ``read_frame`` block forever once the queued
    frames run out (a live-but-silent line) instead of raising — the PIN-gate
    tests need silence that doesn't end the call.
    """

    has_dtmf_events = False
    transport_name = "audiosocket"

    def __init__(self, frames=None, hold_open=False):
        self.peer_addr = "test:0"
        self.call_uuid = "uuid-test"
        self.sample_rate_in = 8000
        self.sample_rate_out = 8000
        self.frame_bytes_out = 320
        self._closed = False
        self.hangups = 0
        self.sent_audio = []
        self.hold_open = hold_open
        # frames handed out by read_frame(); AudioSocketError once exhausted
        self._frames = list(frames or [])
        # AMI side-channel digits (mirrors AudioSocketConnection)
        self._dtmf_injected = []

    def inject_dtmf(self, digit, duration_ms=0):
        self._dtmf_injected.append((digit, duration_ms))

    def poll_dtmf(self):
        if self._dtmf_injected:
            return self._dtmf_injected.pop(0)
        return None

    def send_audio(self, pcm):
        self.sent_audio.append(pcm)

    def send_hangup(self):
        self.hangups += 1

    def flush_playback(self):
        self.flushes = getattr(self, "flushes", 0) + 1

    def pause_playback(self):
        # Barge-in pause (MediaTransport surface): counted so tests assert
        # the pause/confirm/commit sequencing.
        self.pauses = getattr(self, "pauses", 0) + 1

    def resume_playback(self):
        self.resumes = getattr(self, "resumes", 0) + 1

    async def drain(self):
        pass

    async def close(self):
        self._closed = True

    @property
    def is_closed(self):
        return self._closed

    async def read_frame(self):
        import asyncio

        from telephony.audio_socket import AudioSocketError
        if self._frames:
            return self._frames.pop(0)
        if self.hold_open:
            await asyncio.Event().wait()
        raise AudioSocketError("no more frames")


class FakeTTS:
    """Stand-in for a TTSProvider (e.g. Cartesia)."""

    def __init__(self, api_key="", **kw):
        self.voice_id = ""
        self.voices = {}
        self.connected = False
        self.cancelled = False
        self.contexts_started = 0
        self.text_chunks = []                 # (text, is_last)
        self._audio_chunks = [b"\x00\x00" * 160]  # one 320-byte 20ms PCM frame

    @classmethod
    def from_row(cls, row, resolver):
        inst = cls(api_key=resolver(row.get("credential_key", "")))
        inst.voices = row.get("voices") or {}
        return inst

    async def connect(self, *, output_sample_rate=None):
        self.connected = True
        self.connect_rate = output_sample_rate

    async def close(self):
        self.connected = False

    async def synthesize(self, text, *, language=None, output_sample_rate=None):
        return b"\x00\x00" * 160

    def start_streaming_context(self, **kw):
        self.contexts_started += 1

    async def send_text_chunk(self, text, is_last=False):
        self.text_chunks.append((text, is_last))

    async def receive_audio(self):
        for chunk in self._audio_chunks:
            yield chunk

    def cancel(self):
        self.cancelled = True

    def select_voice(self, language):
        # Mirror TTSProvider.select_voice: per-language voice, English fallback.
        self.voice_id = self.voices.get(language) or self.voices.get("en") or self.voice_id
        return self.voice_id


class FakeSTT:
    """Stand-in for DeepgramSTT (final-only call surface)."""

    def __init__(self, **kw):
        self.vad_silence_padding_ms = 550
        self.needs_pre_connect = False
        self.endpointing_ms = 500
        self.stay_open_between_turns = True
        self.supports_early_unmute = True
        self.transcript_wait_timeout_s = 1.0
        self.started = []                     # languages start() was called with
        self.recovered = []                   # languages ensure_alive() saw
        self.keepalives = 0                   # guard-task keepalive() calls
        self.probes = 0                       # ensure_alive(clear_queue=False) probes
        self.last_result_monotonic = 0.0      # bumped by tests to simulate results
        self._transcripts = []                # queued finalized transcripts (full-run tests)
        self.latest_interim = ""              # settable (real provider: polled property)

    @property
    def has_pending_finals(self):
        # Mirrors the real providers: finalized-but-undrained queue entries
        # are barge-in speech evidence.
        return bool(self._transcripts)

    async def start(self, language="multi", sample_rate=None, interim_results=False):
        self.started.append(language)

    async def send_audio(self, audio):
        pass

    def drain_transcript(self):
        return self._transcripts.pop(0) if self._transcripts else None

    async def wait_for_transcript(self, timeout=1.0):
        return self._transcripts.pop(0) if self._transcripts else None

    def clear_queue(self):
        self._transcripts.clear()

    async def finish(self):
        return None

    async def force_endpoint(self):
        pass

    async def close(self):
        pass

    async def feed_during_tts(self, audio):
        pass

    async def feed_during_opening(self):
        pass

    def on_tts_finished(self, was_interrupted):
        pass

    @property
    def is_open(self):
        return True

    async def keepalive(self):
        self.keepalives += 1

    def pop_fatal_error(self):
        return None

    async def ensure_alive(self, language, *, clear_queue=False):
        # `recovered` keeps its historical meaning (opening-semantics
        # recover, clear_queue=True); guard/post-playback probes count
        # separately so smoke assertions stay stable.
        if clear_queue:
            self.recovered.append(language)
        else:
            self.probes += 1
        return True

    async def recover_after_opening(self, language):
        return await self.ensure_alive(language, clear_queue=True)


class FakeVAD:
    """Stand-in for SileroVad — barge-in mode + the ``state`` the TTS
    startup sequencing reads (the real pipeline checks ``vad.state`` when
    the first audio chunk arrives)."""

    def __init__(self, **kw):
        from audio.providers.vad.base import VadState
        self.kwargs = kw
        self.bargein_mode = False
        self.state = VadState.IDLE

    def set_bargein_mode(self, on):
        self.bargein_mode = on


class FakeLLM:
    """Stand-in for the ProxyClient LLM backend.

    ``responses`` is one token list per turn; ``on_token`` (if set) is called
    with (prompt, index, token) before each yield — tests use it to inject
    queued speech or barge-in flags at a precise point in the stream.
    """

    def __init__(self, llm_mode="proxy", session_id=None, responses=None,
                 ws_connected=True):
        self.messages = []
        self.session_id = session_id
        self.llm_mode = llm_mode
        self.prompts = []
        self.aborts = 0
        self.on_token = None
        self._responses = list(responses or [])
        # Mirrors ProxyClient._ws_connected: proxy-mode abort exists only
        # while the WS channel is up (HTTP fallback = run-to-completion).
        self._ws_connected = ws_connected
        # Mirrors ProxyClient.turn_in_flight — gates _cancel_tts's upstream
        # abort (a post-turn commit must not fire a session-scoped abort).
        self.turn_in_flight = True

    def set_session_id(self, sid):
        self.session_id = sid

    async def close(self):
        pass

    @property
    def supports_abort(self):
        # Same rule as ProxyClient: direct always; proxy only over the WS.
        return self.llm_mode == "direct" or (
            self.llm_mode == "proxy" and self._ws_connected)

    @property
    def abort_erases_turn(self):
        # Same rule as ProxyClient: only Direct pops the un-answered user
        # message; proxy-mode aborts interrupt and keep the partial turn.
        return self.llm_mode == "direct"

    async def abort_turn(self):
        self.aborts += 1

    async def send_message(self, text):
        self.prompts.append(text)
        tokens = self._responses.pop(0) if self._responses else []
        for i, token in enumerate(tokens):
            if self.on_token:
                self.on_token(text, i, token)
            yield token


class FakeCall:
    """Stand-in for an outbound CallManager call object."""

    def __init__(self, *, opening_text="", opening_prompt="",
                 opening_completes_call=False, task_description="test task",
                 instructions="", warmup_session_id=None):
        import asyncio
        self.opening_text = opening_text
        self.opening_prompt = opening_prompt
        self.opening_completes_call = opening_completes_call
        self.task_description = task_description
        self.instructions = instructions
        self.warmup_session_id = warmup_session_id
        self._opening_ready = asyncio.Event()
        self._opening_ready.set()


class FakeCallManager:
    """Stand-in for the outbound CallManager."""

    def __init__(self, call=None):
        self._call = call
        self.transcript = []
        self.status = None

    def get_call(self, call_id):
        return self._call

    def add_transcript_entry(self, call_id, role, text):
        self.transcript.append((role, text))


def make_route(direction="inbound", language="en", agent="unified", llm_mode="proxy",
               pin=""):
    """Build a PhoneRoute with all required fields (incl. dial_prefix)."""
    from config_manager import PhoneRoute
    return PhoneRoute(
        id="r1", direction=direction, agent=agent, llm_mode=llm_mode,
        greeting="Hello", language=language, phone_context_override="",
        enabled=True, ami_caller_id="", ami_outbound_context="", dial_prefix="",
        pin=pin,
    )
