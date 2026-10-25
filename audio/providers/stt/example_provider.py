"""Reference STT provider — a heavily-commented, working skeleton.

This is NOT a stub: it instantiates and runs (it just echoes transcripts you
push into it). Copy it as the starting point for a real provider, and read
``audio/CONTRIBUTING_PROVIDERS.md`` alongside it.

It is intentionally NOT registered in ``registry.KNOWN_STT_PROVIDERS`` — it's a
teaching/testing reference, not a selectable provider. It also doubles as the
no-paid-credentials test double (CONTRIBUTING_PROVIDERS.md "Testing").
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging

from audio.capabilities import STTCapabilities
from audio.providers.credential_resolver import CredentialResolver
from audio.providers.stt.base import Heard, STTProvider

logger = logging.getLogger(__name__)


class ExampleSTT(STTProvider):
    """Minimal STT reference. Push transcripts via ``feed_transcript`` to simulate STT output."""

    # (1) Declare what you can do. The dashboard renders knobs from this, and
    #     the platform gates features (e.g. supports_transcribe_file) on it.
    capabilities = STTCapabilities(
        supports_streaming=True,
        supports_transcribe_file=False,
        supports_endpointing=True,
        supports_word_timestamps=False,
        is_local=True,   # this fake runs on-box with no credential
    )
    is_free_tier = True  # local → cost reports show "self-hosted", not "$0.00"

    def __init__(self, *, api_key: str = "", endpointing_ms: int = 200, reports_confidence: bool = False):
        # Providers are PURE: take secrets/params as kwargs, never read DB/env/files.
        self._api_key = api_key
        self._endpointing_ms = endpointing_ms
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._rate = 16000
        self._heard_until = 0.0
        # (5) Report confidence only when EVERY live result carries one on the
        #     scale the language rule compares (a word's probability, 0 to 1):
        #     automatic language detection then reads the ``heard`` ledger and
        #     may force an endpoint while speech goes on. Without it the
        #     acoustic rule decides alone. A real provider sets this on the
        #     class; the flag lets tests cover both.
        if reports_confidence:
            self.capabilities = dataclasses.replace(type(self).capabilities, reports_confidence=True)

    # (2) The registry builds you from an audio_providers row + a resolver.
    #     Map the row's `advanced`/`voices`/`credential_key` to constructor kwargs.
    @classmethod
    def from_row(cls, row: dict, resolver: CredentialResolver) -> ExampleSTT:
        advanced = row.get("advanced") or {}
        return cls(
            api_key=resolver(row.get("credential_key", "")),
            endpointing_ms=int(advanced.get("call_endpointing_ms", 200)),
        )

    # (3) Billing metadata (override only if you actually bill).
    @classmethod
    def cost_per_unit(cls) -> float:
        return 0.0  # local model — free

    @classmethod
    def default_advanced_settings(cls) -> dict:
        return {"call_endpointing_ms": 200, "chat_endpointing_ms": 2000}

    @classmethod
    def validate_advanced(cls, settings: dict) -> dict[str, str]:
        # Return {} on success or {field: message} to surface field-level errors.
        errors = {}
        if "call_endpointing_ms" in settings and int(settings["call_endpointing_ms"]) < 0:
            errors["call_endpointing_ms"] = "must be >= 0"
        return errors

    # ── Test hook (not part of the ABC) ───────────────────────────
    def feed_transcript(self, text: str, *, confidence: float | None = None) -> None:
        """Simulate the provider producing a finalized transcript. With a
        confidence it is also the ``heard`` entry over the audio offered since
        the previous one (an empty text is a zero-word entry: heard, nothing
        found)."""
        if text:
            self._queue.put_nowait(text)
            self._ready.set()
        if confidence is not None and self.capabilities.reports_confidence:
            words = len(text.split())
            end = self.audio_offered_s
            self._heard_final(Heard(start=self._heard_until, end=end, words=words,
                                    confidence=confidence if words else 0.0, final=True))
            self._heard_until = end

    # ── Streaming surface (a real provider opens a socket here) ────
    @property
    def endpointing_ms(self) -> int:
        return self._endpointing_ms

    async def start(self, language: str = "multi", sample_rate: int | None = None, interim_results: bool = False, endpointing_ms: int | None = None) -> None:
        self._is_open = True  # a real provider opens its connection here (at `sample_rate`; stream partials if `interim_results`; honor the `endpointing_ms` override)
        self._rate = sample_rate or self._rate

    async def send_audio(self, audio_bytes: bytes) -> None:
        # (6) The audio clock: every byte offered counts, sent or not (a dead
        #     socket's skipped bytes too), so ``heard`` entries stay on one
        #     clock across reconnects.
        self._count_offered(len(audio_bytes), self._rate)
        ...  # a real provider streams audio to its backend here

    def drain_transcript(self) -> str | None:
        parts = []
        while not self._queue.empty():
            parts.append(self._queue.get_nowait())
        self._ready.clear()
        text = " ".join(parts).strip() or None
        # (4) Rule #1: log transcripts ONLY through the gate — never logger.info(text).
        if text:
            self._log_transcript("Example drain", text)
        return text

    async def wait_for_transcript(self, timeout: float = 1.0) -> str | None:
        self._ready.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        return self.drain_transcript()

    def clear_queue(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        self._ready.clear()

    async def finish(self) -> str | None:
        self._is_open = False
        return self.drain_transcript()

    async def close(self) -> None:
        self._is_open = False
