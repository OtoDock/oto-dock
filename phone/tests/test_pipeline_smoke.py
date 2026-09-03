"""Behavior-preserving smoke net for the phone CallPipeline (regression guard).

Covers the deterministic, decomposition-sensitive logic: the
``[CALL_COMPLETE]`` / ``[QUESTION:]`` marker protocol, the outbound
opening-completes-call hangup (the "say X then hang up" fast path — previously
untested), and per-instance state isolation (no shared mutable state across
calls). Timing-sensitive paths (barge-in, paced playback, turn classification)
are verified live on the FreePBX VM, not here.

Mutable per-call state lives on ``pipeline.state`` (a CallState dataclass);
collaborators (conn/stt/tts/call_manager) stay on the pipeline itself.
"""

import asyncio
import contextlib
import json

from pipeline_fakes import FakeCall, FakeCallManager, FakeConn, FakeLLM, FakeTTS, make_route


# ── Marker protocol (pure regex) ────────────────────────────────────────

def test_call_complete_marker_detected():
    from pipeline import _CALL_COMPLETE_RE
    assert _CALL_COMPLETE_RE.search("All done. [CALL_COMPLETE]")
    assert _CALL_COMPLETE_RE.search("[CALL_COMPLETE]")
    assert _CALL_COMPLETE_RE.search("done[CALL_COMPLETE]now")
    assert not _CALL_COMPLETE_RE.search("the call is complete")


def test_question_marker_extracts_payload():
    from pipeline import _QUESTION_RE
    m = _QUESTION_RE.search("Let me check [QUESTION: what's the budget?]")
    assert m is not None
    assert m.group(1) == "what's the budget?"
    # DOTALL — payload may span newlines
    m2 = _QUESTION_RE.search("[QUESTION: line one\nline two]")
    assert m2 is not None
    assert "line two" in m2.group(1)
    assert _QUESTION_RE.search("no question here") is None


# ── Outbound opening: "say X then hang up" fast path ────────────────────

def _stub_audio_plumbing(p):
    """Isolate the opening's control flow from the real audio path.

    ``_stream_tts_audio`` / ``_discard_incoming_frames`` exercise paced
    playback + frame draining, which are verified live; here we only pin the
    hangup decision, so they become no-ops.
    """
    async def _noop_stream():
        return None

    async def _noop_discard(stop):
        return None

    p._stream_tts_audio = _noop_stream
    p._discard_incoming_frames = _noop_discard


def test_opening_completes_call_hangs_up(make_pipeline):
    """opening_completes_call=True → play opening, then hang up (no listen loop)."""
    call = FakeCall(
        opening_text="Your order shipped. Goodbye! [CALL_COMPLETE]",
        opening_prompt="say it",
        opening_completes_call=True,
    )
    p = make_pipeline(
        route=make_route(direction="outbound"),
        call_manager=FakeCallManager(call),
        outbound_call_id="c1",
        llm=FakeLLM(),
    )
    p.state._running = True
    p.state._stt_active = True
    _stub_audio_plumbing(p)

    asyncio.run(p._send_outbound_opening())

    assert p.state._call_complete is True
    assert p.state._running is False
    assert p.conn.hangups == 1
    # the opening text actually reached TTS
    assert any("[CALL_COMPLETE]" in text for text, _ in p.tts.text_chunks)
    # and was recorded to the call transcript
    assert ("assistant", call.opening_text) in p._call_manager.transcript


def test_opening_not_complete_proceeds_to_listen(make_pipeline):
    """opening_completes_call=False → no hangup; STT recovers for the listen loop."""
    call = FakeCall(
        opening_text="Hi, this is a quick reminder.",
        opening_completes_call=False,
    )
    p = make_pipeline(
        route=make_route(direction="outbound", language="en"),
        call_manager=FakeCallManager(call),
        outbound_call_id="c1",
        llm=FakeLLM(),
    )
    p.state._running = True
    p.state._stt_active = True
    _stub_audio_plumbing(p)

    asyncio.run(p._send_outbound_opening())

    assert p.state._call_complete is False
    assert p.conn.hangups == 0
    assert p.state._running is True
    assert p.stt.recovered == ["en"]  # recover_after_opening(language="en")


# ── Per-route provider resolution────────────────────────────────

def _cfg_with_providers():
    from config_manager import ConfigManager
    cfg = ConfigManager()
    cfg.load({
        "version": 1,
        "providers": {
            "1": {"id": 1, "provider_type": "stt", "provider_name": "deepgram",
                  "voices": {}, "advanced": {"call_endpointing_ms": 500}, "api_key": "dg"},
            "2": {"id": 2, "provider_type": "tts", "provider_name": "cartesia",
                  "voices": {"en": "v-en", "el": "v-el"}, "advanced": {}, "api_key": "c1"},
            "7": {"id": 7, "provider_type": "tts", "provider_name": "elevenlabs",
                  "voices": {"el": "11-el"}, "advanced": {}, "api_key": "c2"},
        },
        "default_stt_provider_id": 1,
        "default_tts_provider_id": 2,
        "settings": {},
        "routes": [],
    })
    return cfg


def test_route_tts_override_drives_voice_and_filler_key(make_pipeline):
    """A route's tts_provider_id override selects that provider's voice; the
    filler key is (provider_id, voice_id, language)."""
    route = make_route(direction="inbound", language="el")
    route.tts_provider_id = 7          # override → elevenlabs
    route.backchannel_mode = "off"     # per-route toggle off
    p = make_pipeline(route=route, cfg=_cfg_with_providers())

    assert p.tts.voices == {"el": "11-el"}
    assert p.tts.voice_id == "11-el"           # el voice from the override provider
    assert p._filler_key == ("7", "11-el", "el", 8000)
    assert p._route_settings.backchannel_enabled is False   # route 'off'
    assert p._route_settings.thinking_filler_enabled is True  # default 'on'


def test_route_defaults_to_call_provider_and_english_voice_fallback(make_pipeline):
    """No provider override → the call default; a language with no voice falls
    back to the English voice."""
    route = make_route(direction="inbound", language="de")  # default provider has no 'de'
    p = make_pipeline(route=route, cfg=_cfg_with_providers())

    assert p.tts.voice_id == "v-en"             # de missing → English fallback
    assert p._filler_key == ("2", "v-en", "de", 8000)  # default tts provider id=2


# ── Filler pre-warm + refresh-on-edit────────────────────────────

def _prewarm_cfg(routes):
    from config_manager import ConfigManager
    cfg = ConfigManager()
    cfg.load({
        "version": 1,
        "providers": {
            "2": {"id": 2, "provider_type": "tts", "provider_name": "cartesia",
                  "voices": {"en": "v-en", "el": "v-el"}, "advanced": {}, "api_key": "c1"},
        },
        "default_stt_provider_id": None,
        "default_tts_provider_id": 2,
        "settings": {
            "backchannel_phrases": json.dumps({"en": ["mhm"], "el": ["ναι"]}),
            "thinking_phrases": json.dumps(
                {"en": ["hmm", "one moment"], "el": ["ε", "μάλιστα"]}),
        },
        "routes": routes,
    })
    return cfg


def test_prewarm_synthesizes_enabled_route_combos(monkeypatch):
    """prewarm_fillers builds each enabled route's (provider,voice,lang) combo
    into the cache, so the first call finds it warm; one combo per (provider,lang)."""
    from fillers import FillerCache
    from pipeline import providers as plproviders

    monkeypatch.setattr(plproviders, "get_provider_class",
                        lambda kind, name: FakeTTS)
    cache = FillerCache()
    monkeypatch.setattr(plproviders, "filler_cache", cache)

    cfg = _prewarm_cfg([
        {"id": "r1", "direction": "inbound", "agent": "x", "language": "en",
         "enabled": True, "audiosocket_uuid": "u1"},
        {"id": "r2", "direction": "inbound", "agent": "x", "language": "el",
         "enabled": True, "audiosocket_uuid": "u2"},
        # second English route → same combo, deduped (synth once)
        {"id": "r3", "direction": "outbound", "agent": "x", "language": "en",
         "enabled": True},
    ])
    asyncio.run(plproviders.prewarm_fillers(cfg))

    assert cache.is_ready(("2", "v-en", "en", 8000))
    assert cache.is_ready(("2", "v-el", "el", 8000))
    assert cache.get_backchannel(("2", "v-en", "en", 8000)) is not None
    assert cache.get_thinking(("2", "v-el", "el", 8000)) is not None


def test_prewarm_prunes_combo_when_route_removed(monkeypatch):
    """A combo no route uses anymore is pruned on the next pre-warm."""
    from fillers import FillerCache
    from pipeline import providers as plproviders

    monkeypatch.setattr(plproviders, "get_provider_class", lambda kind, name: FakeTTS)
    cache = FillerCache()
    monkeypatch.setattr(plproviders, "filler_cache", cache)

    both = _prewarm_cfg([
        {"id": "r1", "direction": "inbound", "agent": "x", "language": "en",
         "enabled": True, "audiosocket_uuid": "u1"},
        {"id": "r2", "direction": "inbound", "agent": "x", "language": "el",
         "enabled": True, "audiosocket_uuid": "u2"},
    ])
    asyncio.run(plproviders.prewarm_fillers(both))
    assert cache.is_ready(("2", "v-el", "el", 8000))

    # Greek route gone → its combo pruned, English kept
    en_only = _prewarm_cfg([
        {"id": "r1", "direction": "inbound", "agent": "x", "language": "en",
         "enabled": True, "audiosocket_uuid": "u1"},
    ])
    asyncio.run(plproviders.prewarm_fillers(en_only))
    assert cache.is_ready(("2", "v-en", "en", 8000))
    assert not cache.is_ready(("2", "v-el", "el", 8000))


def test_ensure_resynthesizes_when_phrases_change():
    """ensure() refreshes a combo when its phrases change, and is a no-op when
    unchanged (refresh-on-edit)."""
    from fillers import FillerCache

    cache = FillerCache()
    tts = FakeTTS()
    tts.voices = {"en": "v-en"}
    tts.select_voice("en")
    key = ("2", "v-en", "en", 8000)

    async def run():
        await cache.ensure(key, tts,
                           backchannel_phrases={"en": ["mhm"]},
                           thinking_phrases={"en": ["hmm"]})
        assert len(cache._backchannels[key]) == 1
        # unchanged → no re-synth needed
        assert not cache.needs_synth(
            key, backchannel_phrases={"en": ["mhm"]},
            thinking_phrases={"en": ["hmm"]})
        # edited phrases → needs re-synth, and ensure picks them up
        assert cache.needs_synth(
            key, backchannel_phrases={"en": ["mhm", "ok"]},
            thinking_phrases={"en": ["hmm"]})
        await cache.ensure(key, tts,
                           backchannel_phrases={"en": ["mhm", "ok"]},
                           thinking_phrases={"en": ["hmm"]})
        assert len(cache._backchannels[key]) == 2

    asyncio.run(run())


# ── Per-call state isolation (no leak across calls) ─────────────────────

def test_pipeline_instances_have_independent_state(make_pipeline):
    """Each call gets its own CallState — guards against class-level mutable
    defaults sneaking in during the CallState extraction."""
    p1 = make_pipeline(route=make_route())
    p2 = make_pipeline(route=make_route())

    assert p1.state is not p2.state
    for attr in ("_turn_segments", "_queued_speech", "_audio_out_buf",
                 "_call_transcript", "_speech_audio_buf"):
        assert getattr(p1.state, attr) is not getattr(p2.state, attr), \
            f"{attr} shared across instances"

    p1.state._turn_segments.append("leak?")
    p1.state._queued_speech.append("leak?")
    assert p2.state._turn_segments == []
    assert p2.state._queued_speech == []


# ── Filler clip conditioning ─────────────────────────────────────────────

def test_condition_clip_trims_fades_and_caps():
    """Cached filler clips get silence-trimmed, edge-faded, and peak-capped —
    abrupt TTS boundaries on short hums read as distortion on the line."""
    import numpy as np
    import config as phone_config
    from fillers import _condition_clip

    sr = phone_config.SAMPLE_RATE
    silence = np.zeros(sr // 10, dtype=np.int16)               # 100ms silence
    tone = np.full(sr // 5, 32767, dtype=np.int16)             # 200ms, clipping-hot
    pcm = np.concatenate([silence, tone, silence]).tobytes()

    out = np.frombuffer(_condition_clip(pcm), dtype=np.int16)

    # trimmed: silence padding reduced to ~20ms per side
    assert len(out) < (sr // 10) * 2 + (sr // 5)
    assert len(out) >= sr // 5
    # peak capped below full scale
    assert np.max(np.abs(out.astype(np.int32))) <= int(0.86 * 32767)
    # edges faded: first/last samples near zero
    assert abs(int(out[0])) < 500 and abs(int(out[-1])) < 500

    # tiny clips pass through unchanged
    tiny = np.full(10, 1000, dtype=np.int16).tobytes()
    assert _condition_clip(tiny) == tiny


# ── Thinking-filler policy (latency gate + repeat damper) ───────────────

def test_thinking_filler_latency_gate_and_repeat_damper(make_pipeline, monkeypatch):
    """The filler only plays when the LLM is slower than the delay, and a
    filler in the previous turn raises the bar to repeat_delay_s."""
    from config_manager import ConfigManager
    from fillers import FillerClip
    from pipeline import playback as plplayback

    class OneClipCache:
        def get_thinking(self, key):
            return FillerClip(text="hmm", pcm_audio=b"\x00\x00" * 160,
                              duration_s=0.02)

    monkeypatch.setattr(plplayback, "filler_cache", OneClipCache())

    cfg = ConfigManager()
    cfg.load({"settings": {
        "thinking_filler_delay_s": "0.05",
        "thinking_filler_repeat_delay_s": "0.4",
    }, "routes": []})
    p = make_pipeline(cfg=cfg)

    async def run():
        # Fast LLM: cancelled in the sleep phase → nothing plays
        t = asyncio.create_task(p._play_thinking_filler())
        await asyncio.sleep(0.01)
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t
        assert p.state._filler_played_this_turn is False

        # Slow LLM: base delay elapses → filler plays
        t = asyncio.create_task(p._play_thinking_filler())
        await asyncio.sleep(0.2)
        assert p.state._filler_played_this_turn is True
        await t

        # Played last turn → the repeat delay applies: a wait past the base
        # delay but under repeat_delay stays silent
        p.state._filler_played_this_turn = False
        p.state._filler_played_last_turn = True
        t = asyncio.create_task(p._play_thinking_filler())
        await asyncio.sleep(0.2)  # > delay (0.05), < repeat (0.4)
        assert p.state._filler_played_this_turn is False
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t

    asyncio.run(run())


# ── Turn abort protocol (Direct layer) ──────────────────────────────────

def _fast_cfg():
    """ConfigManager with sub-second turn timers so abort tests run fast."""
    from config_manager import ConfigManager
    cfg = ConfigManager()
    cfg.load({"settings": {
        "turn_complete_timeout_s": "0.01",
        "turn_incomplete_timeout_s": "0.01",
        "turn_classifier_grace_s": "0.0",
        "tts_response_gap_s": "0",
        "thinking_filler_delay_s": "0",
    }, "routes": []})
    return cfg


def test_abort_requeue_when_caller_talks_before_playback(make_pipeline):
    """Caller finishes a new utterance while the LLM is still thinking and
    nothing has played: the turn aborts server-side and original + new speech
    re-dispatch as ONE batched turn (no continuation classifier needed)."""
    llm = FakeLLM(llm_mode="direct", responses=[
        ["Cameras ", "are "],        # aborted mid-stream
        ["All cameras are fine."],   # batched re-dispatch
    ])
    p = make_pipeline(route=make_route(llm_mode="direct"), llm=llm,
                      cfg=_fast_cfg())
    p.state._warmup_done.set()

    def _on_token(prompt, i, token):
        if len(llm.prompts) == 1 and i == 0:
            p.state._queued_speech.append("the external ones")
    llm.on_token = _on_token

    asyncio.run(p._process_utterance("check the cameras"))

    assert llm.aborts == 1
    assert llm.prompts == [
        "check the cameras",
        "check the cameras the external ones",
    ]
    assert p.state._aborted_turn_text is None


def test_barge_in_mid_generation_aborts_and_folds_into_next_turn(make_pipeline):
    """Barge-in while the LLM is still generating (Direct): the turn aborts
    (its user message is erased server-side) and its transcript folds into
    the next dispatched turn."""
    llm = FakeLLM(llm_mode="direct", responses=[
        ["The weather ", "today "],
        ["Here is everything."],
    ])
    p = make_pipeline(route=make_route(llm_mode="direct"), llm=llm,
                      cfg=_fast_cfg())
    p.state._warmup_done.set()

    def _on_token(prompt, i, token):
        if len(llm.prompts) == 1 and i == 1:
            p.state._utterance_cancelled = True  # barge-in confirmed
    llm.on_token = _on_token

    asyncio.run(p._process_utterance("what's the weather"))
    assert llm.aborts == 1
    assert p.state._aborted_turn_text == "what's the weather"

    llm.on_token = None
    asyncio.run(p._process_utterance("what time is it"))
    assert llm.prompts[-1] == "what's the weather what time is it"
    assert p.state._aborted_turn_text is None


def test_proxy_ws_barge_in_aborts_interrupt_style(make_pipeline):
    """WS-proxy turns abort like the duplex attach (CLI/Codex graceful
    interrupt via the proxy): upstream unwind, but NO transcript refold —
    interrupt semantics keep the turn in chat history, so resending its
    words would duplicate them."""
    llm = FakeLLM(llm_mode="proxy", responses=[["Hello ", "there "]])
    p = make_pipeline(route=make_route(llm_mode="proxy"), llm=llm,
                      cfg=_fast_cfg())
    p.state._warmup_done.set()

    def _on_token(prompt, i, token):
        if i == 1:
            p.state._utterance_cancelled = True
    llm.on_token = _on_token

    asyncio.run(p._process_utterance("hi"))
    assert llm.aborts == 1
    assert p.state._aborted_turn_text is None


def test_proxy_http_fallback_barge_in_does_not_abort(make_pipeline):
    """The HTTP-SSE fallback has no abort channel — the pipeline lets the
    turn drain; no abort call, no aborted-turn state."""
    llm = FakeLLM(llm_mode="proxy", ws_connected=False,
                  responses=[["Hello ", "there "]])
    p = make_pipeline(route=make_route(llm_mode="proxy"), llm=llm,
                      cfg=_fast_cfg())
    p.state._warmup_done.set()

    def _on_token(prompt, i, token):
        if i == 1:
            p.state._utterance_cancelled = True
    llm.on_token = _on_token

    asyncio.run(p._process_utterance("hi"))
    assert llm.aborts == 0
    assert p.state._aborted_turn_text is None


def test_duplex_rate_transport_drives_pipeline_format(make_pipeline):
    """A 16k-in / 24k-out transport must thread its rates everywhere the
    pipeline does audio math: VAD chunking, Smart Turn window, paced-sender
    frame size, and the filler-cache key (an 8k call clip must never replay
    into a 24k session)."""
    conn = FakeConn()
    conn.sample_rate_in = 16000
    conn.sample_rate_out = 24000
    conn.frame_bytes_out = 960  # 20ms at 24kHz/16-bit
    p = make_pipeline(route=make_route(), cfg=_cfg_with_providers(), conn=conn)

    assert (p._rate_in, p._rate_out, p._frame_bytes_out) == (16000, 24000, 960)
    assert p._byte_rate_out == 48000
    assert p.vad.kwargs["sample_rate"] == 16000
    assert p._filler_key[-1] == 24000
    # Smart Turn window: seconds × in-rate × width
    assert p._speech_audio_max_bytes == int(
        p.cfg.smart_turn_audio_window_s * 16000 * 2)
    # Telephony-flavoured 8k features stay off at duplex rates.
    assert p._ambience is None and p._texture is None and p._breath_pcm is None


def test_audiosocket_transport_keeps_telephony_format(make_pipeline):
    """The default (FakeConn mirrors AudioSocket) stays byte-identical 8k."""
    p = make_pipeline(route=make_route(), cfg=_cfg_with_providers())
    assert (p._rate_in, p._rate_out, p._frame_bytes_out) == (8000, 8000, 320)
    assert p._filler_key[-1] == 8000


def test_turn_dispatch_hook_fires_per_loop_iteration(make_pipeline):
    """``_on_turn_dispatch`` fires at the top of EVERY ``_process_utterance``
    loop iteration — the original utterance AND a continuation re-dispatch of
    queued speech. UI transports (the duplex session) hang their caption/state
    frames on it; firing once per outer call left re-dispatched turns frameless
    (live-hit 2026-08-12 04:07)."""
    llm = FakeLLM(responses=[
        ["Sure, tomorrow looks sunny."],
        ["Patras looks rainy."],
    ])
    p = make_pipeline(llm=llm)
    p.state._running = True
    p.state._warmup_done.set()
    p.state._user_silent.set()
    _stub_audio_plumbing(p)

    async def _noop_filler():
        p.state._thinking_filler_done.set()
    p._play_thinking_filler = _noop_filler

    dispatches = []
    p._on_turn_dispatch = dispatches.append

    # The user's follow-up lands in the queue mid-stream of turn 1; force the
    # continuation classification (the re-dispatch path under test) and an
    # immediate "complete" verdict so the batch dispatches without waiting.
    def _inject(prompt, i, token):
        if len(llm.prompts) == 1 and not p.state._queued_speech:
            p.state._queued_speech.append("and for patras")
    llm.on_token = _inject

    async def _always_continuation(original, follow_up):
        return True

    async def _instantly_complete(segments):
        return 0.0
    p._is_queued_continuation = _always_continuation
    p._classify_turn = _instantly_complete

    asyncio.run(p._process_utterance("whats the weather"))

    assert dispatches[0] == "whats the weather"
    assert len(dispatches) == 2 and "patras" in dispatches[1]
    assert len(llm.prompts) == 2  # both turns really dispatched upstream
