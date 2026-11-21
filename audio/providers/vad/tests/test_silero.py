"""SileroVad tests — constructs the bundled ONNX model and runs offline.

The model ships inside the package, so this needs no external file or
network. Silence must not trigger SPEECH_START. Skipped entirely on lean
installs without the ``localmodels`` extra (e.g. the proxy venv).
"""

import pytest

pytest.importorskip("onnxruntime")

from audio.providers.vad.base import VadEvent, VadState
from audio.providers.vad.silero import SileroVad


def _make_vad() -> SileroVad:
    return SileroVad(
        threshold=0.5,
        silence_duration_ms=800,
        speech_pad_ms=64,
        min_energy_rms=200.0,
        bargein_threshold=0.7,
        bargein_debounce_ms=200,
        bargein_chunk_ratio=0.6,
        bargein_silence_duration_ms=1200,
    )


def test_constructs_and_starts_idle():
    vad = _make_vad()
    assert vad.state == VadState.IDLE


def test_silence_does_not_trigger_speech():
    vad = _make_vad()
    silence = b"\x00" * 320  # one AudioSocket frame of digital silence
    for _ in range(50):  # ~1s of silence
        assert vad.process(silence) in (VadEvent.NONE,)
    assert vad.state == VadState.IDLE


def test_reset_returns_to_idle():
    vad = _make_vad()
    vad.process(b"\x00" * 320)
    vad.reset()
    assert vad.state == VadState.IDLE


def test_16k_instance_runs_real_model():
    # Duplex sessions feed 16 kHz mic audio; the model derives its 32 ms
    # window from the rate (512 samples, with 64 of context in front), and
    # silence still stays IDLE.
    vad = SileroVad(
        threshold=0.5, silence_duration_ms=800, speech_pad_ms=64,
        min_energy_rms=200.0, bargein_threshold=0.7, bargein_debounce_ms=200,
        bargein_chunk_ratio=0.6, bargein_silence_duration_ms=1200,
        sample_rate=16000,
    )
    assert vad._chunk_bytes == 512 * 2 and vad._model.context_samples == 64
    for _ in range(50):
        assert vad.process(b"\x00" * 640) in (VadEvent.NONE,)
    assert vad.state == VadState.IDLE


def test_a_spoken_sentence_at_the_phone_rate_gets_a_speech_start(monkeypatch):
    """The other half of the silence test: the model must hear speech at
    8 kHz with the phone's own settings, from a zeroed state and after the
    barge-in mode a greeting leaves behind. The silence timer reads the wall
    clock, so the module's clock follows the audio fed."""
    import os
    import wave

    from audio.providers.vad import silero

    class Clock:
        t = 1000.0

        def monotonic(self) -> float:
            return self.t

    clock = Clock()
    monkeypatch.setattr(silero, "time", clock)
    sample = os.path.join(os.path.dirname(__file__), "..", "..", "lid", "tests", "fixtures", "greek-8k.wav")
    with wave.open(sample, "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (8000, 1, 2)
        speech = w.readframes(w.getnframes())

    def feed(vad: SileroVad, pcm: bytes) -> list[VadEvent]:
        events = []
        for i in range(0, len(pcm) - 319, 320):
            clock.t += 0.02
            event = vad.process(pcm[i:i + 320])
            if event != VadEvent.NONE:
                events.append(event)
        return events

    def phone_vad() -> SileroVad:
        # phone/config_manager.py's defaults, the STT's 550 ms silence padding.
        return SileroVad(
            threshold=0.4, silence_duration_ms=550, speech_pad_ms=64, min_energy_rms=150.0,
            bargein_threshold=0.35, bargein_debounce_ms=300, bargein_chunk_ratio=0.5,
            bargein_silence_duration_ms=500, sample_rate=8000,
        )

    quiet = b"\x00" * (8000 * 2)                      # one second of a silent line
    vad = phone_vad()
    assert feed(vad, quiet + speech + quiet) == [VadEvent.SPEECH_START, VadEvent.SPEECH_END]

    vad = phone_vad()
    vad.set_bargein_mode(True)                        # a greeting plays
    assert feed(vad, quiet) == []
    vad.set_bargein_mode(False)                       # and ends: the caller's turn
    assert feed(vad, quiet + speech + quiet) == [VadEvent.SPEECH_START, VadEvent.SPEECH_END]


def test_a_reader_of_the_scores_changes_no_decision(monkeypatch):
    """The same audio through two VADs, one of them read after every frame
    and from a second thread the whole time: every event, the final state
    and every probability are the same. Reading is not deciding."""
    import os
    import threading
    import wave

    from audio.providers.vad import silero

    class Clock:
        t = 1000.0

        def monotonic(self) -> float:
            return self.t

    sample = os.path.join(os.path.dirname(__file__), "..", "..", "lid", "tests", "fixtures", "greek-8k.wav")
    with wave.open(sample, "rb") as w:
        speech = w.readframes(w.getnframes())
    quiet = b"\x00" * (8000 * 2)
    audio = quiet + speech + quiet + speech[: 8000 * 2] + quiet

    def run(read: bool):
        clock = Clock()
        monkeypatch.setattr(silero, "time", clock)
        vad = SileroVad(
            threshold=0.4, silence_duration_ms=550, speech_pad_ms=64, min_energy_rms=150.0,
            bargein_threshold=0.35, bargein_debounce_ms=300, bargein_chunk_ratio=0.5,
            bargein_silence_duration_ms=500, sample_rate=8000,
        )
        stop = threading.Event()
        reads = []

        def reader():
            while not stop.is_set():
                reads.append(vad.scored(0.0, 1e12).windows)
                vad.by_loudness()

        thread = threading.Thread(target=reader) if read else None
        if thread is not None:
            thread.start()
        events = []
        try:
            for n, i in enumerate(range(0, len(audio) - 319, 320)):
                if n == 40:
                    vad.set_bargein_mode(True)            # an answer plays over the first words
                if n == 200:
                    vad.set_bargein_mode(False)
                clock.t += 0.02
                events.append(vad.process(audio[i:i + 320]))
                if read:
                    vad.scored(clock.t - 3.0, clock.t)
                    vad.by_loudness()
        finally:
            stop.set()
            if thread is not None:
                thread.join()
        return events, vad.state, [entry[1:] for entry in vad._scores], len(reads), vad.by_loudness()

    plain_events, plain_state, plain_scores, _, plain_loudness = run(read=False)
    read_events, read_state, read_scores, reads, read_loudness = run(read=True)
    assert reads > 0
    assert VadEvent.SPEECH_START in plain_events and VadEvent.SPEECH_END in plain_events
    assert read_events == plain_events
    assert read_state == plain_state
    assert read_scores == plain_scores
    # The counts by loudness are the same too, and they hold every window fed.
    assert read_loudness == plain_loudness
    counted = sum(windows for bands in plain_loudness for windows, _ in bands)
    assert counted == len(audio) // 512
    assert sum(windows for windows, _ in plain_loudness.bargein) > 0 < sum(w for w, _ in plain_loudness.listening)
