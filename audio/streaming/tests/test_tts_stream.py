"""stream_tts orchestration tests with a fake provider (no network)."""

import asyncio

from audio.streaming.tts_stream import stream_tts


class FakeTTS:
    """Minimal provider-shaped fake: one audio chunk per non-empty push, paced
    through a size-1 queue so an early break genuinely leaves the producer
    blocked (exercising the cancel path)."""

    def __init__(self):
        self.rate = None
        self.pushed: list[str] = []
        self.finalized = False
        self.cancelled = False
        self._q: asyncio.Queue = asyncio.Queue(maxsize=1)

    def start_streaming_context(self, *, output_sample_rate=None, language=None):
        self.rate = output_sample_rate
        self.language = language

    async def send_text_chunk(self, text, is_last=False):
        if text:
            self.pushed.append(text)
            await self._q.put(b"AU:" + text.encode())
        if is_last:
            self.finalized = True
            await self._q.put(None)  # sentinel → receive ends

    async def receive_audio(self):
        while True:
            item = await self._q.get()
            if item is None or self.cancelled:
                return
            yield item

    def cancel(self):
        self.cancelled = True


async def test_yields_audio_per_sentence_and_sets_rate():
    p = FakeTTS()
    out = [a async for a in stream_tts(p, ["One.", "Two."], output_sample_rate=24000, language="de")]
    assert p.rate == 24000 and p.language == "de"
    assert out == [b"AU:One.", b"AU:Two."]
    assert p.finalized and p.pushed == ["One.", "Two."]


async def test_empty_chunks_still_finalizes():
    p = FakeTTS()
    out = [a async for a in stream_tts(p, [], output_sample_rate=8000)]
    assert out == []
    assert p.finalized  # no_more_inputs sent even with no text


async def test_blank_chunks_are_filtered():
    p = FakeTTS()
    out = [a async for a in stream_tts(p, ["  ", "Hi.", ""], output_sample_rate=24000)]
    assert out == [b"AU:Hi."]
    assert p.pushed == ["Hi."]


async def test_early_break_cancels_producer():
    p = FakeTTS()
    gen = stream_tts(p, ["a.", "b.", "c."], output_sample_rate=24000)
    assert await gen.__anext__() == b"AU:a."
    await gen.aclose()  # caller stops early → finally must cancel the producer
    # give the loop a tick; no exception should surface and not all sentences sent
    await asyncio.sleep(0)
    assert p.pushed != ["a.", "b.", "c."]
