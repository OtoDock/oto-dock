"""Streaming TTS orchestration — drive a provider's continuation context over a
sequence of text chunks, yielding PCM audio as it is synthesized.

Shared primitive: the chat sound icon (proxy) feeds it sentence chunks; the
phone pipeline keeps its own paced AudioSocket delivery. "Share the primitive,
not the policy": delivery (browser playback vs AudioSocket pacing) stays
per-surface. The caller owns connect()/close() and voice selection.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterator, Iterable

from audio.providers.tts.base import TTSProvider


async def stream_tts(
    provider: TTSProvider,
    chunks: Iterable[str],
    *,
    output_sample_rate: int | None = None,
    language: str | None = None,
) -> AsyncIterator[bytes]:
    """Yield PCM audio for ``chunks`` (sentence-level text) via the provider's
    continuation context.

    The producer pushes sentences seamlessly (no artificial gaps) while this
    coroutine yields audio as it arrives — pipelined, so first audio plays while
    later sentences are still synthesizing. The provider MUST already be
    connected. On early exit (caller stops iterating, or the request is
    cancelled), the producer task is cancelled in ``finally`` so no push loop is
    left running; the caller's ``close()``/``cancel()`` tears down the context.
    """
    provider.start_streaming_context(output_sample_rate=output_sample_rate, language=language)

    async def _produce() -> None:
        items = [c for c in chunks if c and c.strip()]
        if not items:
            await provider.send_text_chunk("", is_last=True)
            return
        last = len(items) - 1
        for i, chunk in enumerate(items):
            await provider.send_text_chunk(chunk, is_last=(i == last))

    producer = asyncio.create_task(_produce())
    try:
        async for audio in provider.receive_audio():
            yield audio
    finally:
        if not producer.done():
            producer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await producer
