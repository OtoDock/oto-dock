"""The tracker: feeding, the upsampler, the run cadence, stale results,
polled events, take, close."""

import asyncio
import math

import numpy as np
import pytest
from audio.providers.lid import rule as R, tracker as T
from audio.providers.lid.base import LanguageIdentifier
from audio.providers.lid.rule import LEAN, Event, LanguageRule
from audio.providers.lid.tracker import LanguageTracker, Upsampler2x


class ScriptedIdentifier(LanguageIdentifier):
    """Answers the scripted log scores in order (the last one repeats);
    records every window it saw."""

    labels = ["en", "el", "de", "fr"]

    def __init__(self, *answers, delay_s: float = 0.0):
        self.answers = list(answers)
        self.delay_s = delay_s
        self.windows: list[np.ndarray] = []

    def scores(self, pcm_16k):
        import time
        if self.delay_s:
            time.sleep(self.delay_s)
        self.windows.append(pcm_16k.copy())
        probs = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return np.array([math.log(probs.get(lang, 1e-6)) for lang in self.labels])


def voiced(seconds: float, rate: int = 16000) -> bytes:
    n = int(seconds * rate)
    t = np.arange(n) / rate
    return (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16).tobytes()


def silence(seconds: float, rate: int = 16000) -> bytes:
    return bytes(int(seconds * rate) * 2)


def tracker(ident, *, rate_in=16000, switch_at=LEAN, lean_at_take=False):
    rule = LanguageRule("en", ["en", "el", "de"], switch_at=switch_at, prior=0.0, lean_at_take=lean_at_take)
    return LanguageTracker(ident, rule, rate_in=rate_in)


async def settle(tr: LanguageTracker) -> list[Event]:
    """Let the in-flight run finish and hand back the events."""
    if tr._task is not None:
        await tr._task
    return tr.poll()


def test_the_upsampler_doubles_the_rate_and_keeps_the_tone_below_four_kilohertz():
    up = Upsampler2x()
    t = np.arange(8000) / 8000
    x8 = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    out = np.concatenate([up.process(x8[:3000]), up.process(x8[3000:])])
    assert len(out) == 16000
    spectrum = np.abs(np.fft.rfft(out[2000:14000] * np.hanning(12000)))
    freqs = np.fft.rfftfreq(12000, 1 / 16000)
    peak = freqs[int(spectrum.argmax())]
    assert abs(peak - 1000) < 5
    image = spectrum[(freqs > 6500) & (freqs < 7500)].max()
    assert image < spectrum.max() * 0.01     # the mirrored image is 40 dB down


def test_a_tracker_takes_eight_or_sixteen_kilohertz_only():
    with pytest.raises(ValueError):
        tracker(ScriptedIdentifier({"en": 1.0}), rate_in=44100)
    with pytest.raises(ValueError):
        LanguageTracker(ScriptedIdentifier({"en": 1.0}), LanguageRule("en", ["en", "xx"], switch_at=LEAN), rate_in=16000)


def test_nothing_before_the_first_voice_and_the_first_run_at_one_second():
    ident = ScriptedIdentifier({"el": 0.8, "en": 0.15, "de": 0.05})
    tr = tracker(ident)

    async def run():
        tr.feed(silence(2.0), voiced=False)
        assert tr.voiced_s == 0.0 and tr._task is None
        tr.feed(voiced(0.9), voiced=True)
        assert tr._task is None                  # under MIN_VOICED_S
        tr.feed(voiced(0.1), voiced=True)
        assert tr._task is not None              # the first run at 1.0 s
        events = await settle(tr)
        assert events == [Event(R.OPEN, "el")]
        assert len(ident.windows) == 1 and len(ident.windows[0]) == 16000
    asyncio.run(run())


def test_runs_every_half_second_of_voice_and_the_window_is_three_seconds():
    ident = ScriptedIdentifier({"el": 0.8, "en": 0.15, "de": 0.05})
    tr = tracker(ident)

    async def run():
        for _ in range(8):                        # 4 s of voice in 0.5 s pieces
            tr.feed(voiced(0.5), voiced=True)
            await settle(tr)
        assert tr.runs == 7                       # at 1.0, 1.5, ..., 4.0 s
        assert len(ident.windows[-1]) == 3 * 16000
        # switched at 1.5 s; the final needs three stable seconds after it
        assert (tr.rule.current, tr.rule.final) == ("el", None)
    asyncio.run(run())


def test_the_switch_and_the_final_arrive_as_events_in_order():
    ident = ScriptedIdentifier({"el": 0.8, "en": 0.15, "de": 0.05})
    tr = tracker(ident)

    async def run():
        kinds = []
        for _ in range(10):
            tr.feed(voiced(0.5), voiced=True)
            kinds.extend(e.kind for e in await settle(tr))
        return kinds
    kinds = asyncio.run(run())
    assert kinds == [R.OPEN, R.SWITCH, R.FINAL]


def test_a_stale_result_is_dropped():
    ident = ScriptedIdentifier({"el": 0.9, "en": 0.05, "de": 0.05}, delay_s=0.05)
    tr = tracker(ident)

    async def run():
        tr.feed(voiced(1.0), voiced=True)         # the run starts, covering 1.0 s
        tr.feed(voiced(1.0), voiced=True)         # a slow run: a second of voice behind is still read
        await settle(tr)
        assert tr.dropped == 0 and tr.rule.latest is not None
        tr.feed(voiced(2.0), voiced=True)         # the next run starts; the feed races two seconds ahead
        tr.feed(voiced(2.0), voiced=True)
        await settle(tr)
        assert tr.dropped == 1
    asyncio.run(run())


def test_the_window_expires_twenty_seconds_after_the_first_voice():
    ident = ScriptedIdentifier({"en": 0.9, "el": 0.05, "de": 0.05})
    tr = tracker(ident)

    async def run():
        tr.feed(voiced(1.0), voiced=True)
        await settle(tr)
        tr.feed(silence(T.WINDOW_MAX_S), voiced=False)
        assert tr.poll() == [Event(R.FINAL, "en")]
    asyncio.run(run())


def test_take_runs_one_fresh_reading_and_applies_the_take_rule():
    ident = ScriptedIdentifier({"el": 0.7, "en": 0.25, "de": 0.05})
    tr = tracker(ident, switch_at=R.SURE, lean_at_take=True)

    async def run():
        tr.feed(voiced(1.2), voiced=True)
        await settle(tr)                          # a lean: open, no switch (SURE on calls)
        assert tr.rule.current == "en"
        tr.feed(voiced(0.2), voiced=True)         # more voice since the reading
        events = await tr.take()
        assert Event(R.SWITCH, "el") in events and tr.rule.current == "el"
        assert ident.windows[-1].shape[0] == int(1.4 * 16000)
    asyncio.run(run())


def test_a_failing_identifier_stops_the_readings_without_breaking_the_feed():
    class Broken(ScriptedIdentifier):
        def scores(self, pcm_16k):
            raise RuntimeError("no model")

    tr = tracker(Broken({"en": 1.0}))

    async def run():
        tr.feed(voiced(1.0), voiced=True)
        await settle(tr)
        assert tr.failed and tr.poll() == []
        tr.feed(voiced(1.0), voiced=True)
        assert tr._task.done()                    # no new run
    asyncio.run(run())


def test_close_discards_a_late_result():
    ident = ScriptedIdentifier({"el": 0.9, "en": 0.05, "de": 0.05}, delay_s=0.05)
    tr = tracker(ident)

    async def run():
        tr.feed(voiced(1.0), voiced=True)
        tr.close()
        with pytest.raises(asyncio.CancelledError):
            await tr._task
        assert tr.poll() == [] and tr.rule.current == "en"
    asyncio.run(run())


def test_a_cancel_aimed_at_the_taker_goes_through():
    """New speech cancels the turn timer while it waits in ``take``: the
    cancel must end the handler, not be swallowed with the reading's own."""
    ident = ScriptedIdentifier({"el": 0.9, "en": 0.05, "de": 0.05}, delay_s=0.25)
    tr = tracker(ident, switch_at=R.SURE, lean_at_take=True)

    async def run():
        tr.feed(voiced(1.0), voiced=True)         # a slow run starts
        reached = []

        async def handler():
            await tr.take()
            reached.append("dispatch")
        task = asyncio.create_task(handler())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert reached == [] and tr.rule._takes == 0
        await tr._task                            # the reading itself still lands
    asyncio.run(run())


def test_the_trackers_own_close_does_not_end_the_taker():
    ident = ScriptedIdentifier({"el": 0.9, "en": 0.05, "de": 0.05}, delay_s=0.25)
    tr = tracker(ident, switch_at=R.SURE, lean_at_take=True)

    async def run():
        tr.feed(voiced(1.0), voiced=True)
        task = asyncio.create_task(tr.take())
        await asyncio.sleep(0.05)
        tr.close()                                # cancels the run, not the taker
        assert await task == [] and tr.closed
    asyncio.run(run())


def test_a_run_that_waited_until_stale_is_skipped_before_the_model_runs():
    """One worker per process: a job queued behind a long one is dropped
    when the worker reaches it, without running the model."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    ident = ScriptedIdentifier({"el": 0.9, "en": 0.05, "de": 0.05})
    rule = LanguageRule("en", ["en", "el", "de"], switch_at=LEAN, prior=0.0)
    executor = ThreadPoolExecutor(max_workers=1)
    tr = LanguageTracker(ident, rule, rate_in=16000, executor=executor)
    gate = threading.Event()

    async def run():
        executor.submit(gate.wait)                # another consumer's long run holds the worker
        tr.feed(voiced(1.0), voiced=True)         # this run queues behind it, covering 1.0 s
        tr.feed(voiced(2.0), voiced=True)         # the feed runs 2 s ahead: stale by the time the worker is free
        gate.set()
        await settle(tr)
        assert ident.windows == [] and tr.dropped == 1 and tr.runs == 0
        tr.feed(voiced(0.5), voiced=True)         # the next step runs normally
        await settle(tr)
        assert len(ident.windows) == 1 and tr.runs == 1
    asyncio.run(run())
    executor.shutdown(wait=False)


def test_the_window_is_the_last_three_seconds_in_order():
    ident = ScriptedIdentifier({"el": 0.9, "en": 0.05, "de": 0.05})   # a leader that keeps the rule open past 3 s
    tr = tracker(ident)

    async def run():
        n = 16000 * 4
        ramp = np.arange(n, dtype=np.float32) / n                       # a monotonic ramp over 4 s
        pcm = (ramp * 20000).astype(np.int16).tobytes()
        for i in range(0, len(pcm), 640):                               # 20 ms frames
            tr.feed(pcm[i:i + 640], voiced=True)
            await settle(tr)
        window = ident.windows[-1]
        assert len(window) == 3 * 16000
        assert np.all(np.diff(window) >= 0)                              # oldest first, no seam
        assert abs(float(window[-1]) - 20000 / 32768) < 0.01             # ends at the newest sample
    asyncio.run(run())


def test_every_applied_reading_is_one_line_of_numbers(caplog):
    import logging
    ident = ScriptedIdentifier({"el": 0.8, "en": 0.15, "de": 0.05})
    tr = tracker(ident)

    async def run():
        with caplog.at_level(logging.INFO, logger="audio.providers.lid.tracker"):
            tr.feed(voiced(1.0), voiced=True)
            await settle(tr)
    asyncio.run(run())
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("language reading at")]
    assert len(lines) == 1
    assert lines[0].startswith("language reading at 1.0 s voiced: el 0.80, en 0.15 (")
    assert lines[0].endswith("s behind)")


def test_the_old_hebrew_label_serves_the_current_code():
    class Labelled(ScriptedIdentifier):
        labels = ["en", "iw"]

    rule = LanguageRule("he", ["he", "en"], switch_at=LEAN, prior=0.0)
    tr = LanguageTracker(Labelled({"iw": 0.9, "en": 0.1}), rule, rate_in=16000)

    async def run():
        tr.feed(voiced(1.0), voiced=True)
        await settle(tr)
    asyncio.run(run())
    assert tr.rule.latest["he"] > 0.8
