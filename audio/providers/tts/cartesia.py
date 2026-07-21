"""Cartesia Sonic TTS provider (streaming WebSocket).

Cartesia SDK v3.x API:
  conn = await client.tts.websocket_connect().enter()   # long-lived connection
  ctx  = conn.context(model_id=, voice={"mode":"id","id":…}, output_format={…}, language=)
  await ctx.push(text)                                  # stream text (continuations)
  await ctx.no_more_inputs()                            # end of input for this context
  async for r in ctx.receive():                         # r.type=="chunk" → r.audio (decoded bytes)
      ...
  await conn.close()

``output_format`` is a plain dict (``RawOutputFormatParam``) — no private types import. A single
connection multiplexes many sequential contexts (one per utterance / one-shot clip).
"""

from __future__ import annotations

import contextlib
import logging

from cartesia import AsyncCartesia

from audio.capabilities import TTSCapabilities
from audio.constants import SAMPLE_RATE
from audio.log_policy import log_transcript
from audio.providers.credential_resolver import CredentialResolver
from audio.providers.tts.base import TTSProvider

logger = logging.getLogger(__name__)


def _output_format(sample_rate: int) -> dict:
    """Raw 16-bit signed LE mono PCM at ``sample_rate`` (8 kHz matches AudioSocket)."""
    return {"container": "raw", "encoding": "pcm_s16le", "sample_rate": sample_rate}


# Default Cartesia model. Admin-overridable per provider via ``advanced.model_id``
# (Cartesia deprecates model IDs periodically — e.g. sonic / sonic-2 / sonic-turbo were retired
# 2026-06-01 — so the operator sets the current one in the TTS pill without a code change).
_MODEL_ID = "sonic-3.5"

# Cartesia Sonic bills per character (~$40 / 1M chars on the Pro tier, 2026-06-03).
# Varies by plan — verify and override per instance via advanced.rate_override_per_unit.
_COST_PER_CHAR = 0.00004


class CartesiaTTS(TTSProvider):
    """Streaming text-to-speech via Cartesia WebSocket."""

    capabilities = TTSCapabilities(supports_streaming=True, supports_endpointing=False, is_local=False)

    # Public-library voices (account-independent ids), production-verified on
    # a live install; Sonic is multilingual, so the ``en`` voice covers every
    # language without its own entry. Admin-configured voices always win.
    default_voices = {
        "en": "79f8b5fb-2cc8-479a-80df-29f7a7cf1a3e",
        "el": "50849023-76e9-46c7-af52-9ec39888a165",
    }

    def __init__(
        self,
        *,
        api_key: str = "",
        voice_id: str = "",
        voices: dict[str, str] | None = None,
        advanced: dict | None = None,
    ):
        self._api_key = api_key
        self._client = AsyncCartesia(api_key=api_key)
        self._conn = None       # AsyncTTSResourceConnection — long-lived, reused across contexts
        self._ctx = None        # AsyncWebSocketContext — the current streaming utterance
        self._cancelled = False
        self.voices: dict[str, str] = voices or {}
        self.voice_id: str = voice_id or ""
        self._advanced = advanced or {}
        self._model_id = self._advanced.get("model_id") or _MODEL_ID

    # ── Factory / metadata ─────────────────────────────────────────

    @classmethod
    def from_row(cls, row: dict, resolver: CredentialResolver) -> "CartesiaTTS":
        return cls(
            api_key=resolver(row.get("credential_key", "")),
            voices=row.get("voices") or {},
            advanced=row.get("advanced") or {},
        )

    @classmethod
    def cost_per_unit(cls) -> float:
        return _COST_PER_CHAR

    def _voice(self) -> dict:
        return {"mode": "id", "id": self.voice_id}

    # ── Connection lifecycle ───────────────────────────────────────

    async def connect(self) -> None:
        """Open the long-lived WebSocket connection to Cartesia."""
        self._conn = await self._client.tts.websocket_connect().enter()
        self._cancelled = False
        logger.info("Cartesia TTS WebSocket connected")

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self._conn:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None
        self._ctx = None
        with contextlib.suppress(Exception):
            await self._client.close()

    # ── One-shot synthesis (greetings / fillers) ───────────────────

    async def synthesize(self, text: str, *, language: str | None = None) -> bytes:
        """Synthesize complete text to PCM audio (one-shot). Drives a dedicated
        context to completion on the shared connection. ``language`` (base code)
        tells the engine which language to pronounce (None → voice default)."""
        if not self._conn:
            await self.connect()

        ctx = self._conn.context(
            model_id=self._model_id, voice=self._voice(),
            output_format=_output_format(SAMPLE_RATE),
            language=language,
        )
        await ctx.push(text)
        await ctx.no_more_inputs()

        chunks = []
        async for r in ctx.receive():
            if r.type == "chunk" and r.audio:
                chunks.append(r.audio)

        result = b"".join(chunks)
        # The synthesized text is agent speech that can quote user-personal data —
        # route it through the transcript gate; keep only the byte count at INFO.
        log_transcript(logger, "TTS synthesize", text)
        logger.info(f"TTS synthesized {len(result)} bytes")
        return result

    # ── Streaming context (LLM → TTS bridge) ───────────────────────

    def start_streaming_context(
        self, *, output_sample_rate: int | None = None, language: str | None = None,
    ) -> None:
        """Start a new streaming context for the LLM-to-TTS bridge.

        Continuations (``push`` with the context's defaults) preserve prosody
        across multiple text chunks. ``output_sample_rate`` overrides the 8 kHz
        telephony default (chat streams 24 kHz); ``language`` (e.g. ``de``/``el``)
        tells Cartesia which language to pronounce (``None`` → voice default).
        """
        if not self._conn:
            raise RuntimeError("TTS WebSocket not connected")
        self._ctx = self._conn.context(
            model_id=self._model_id, voice=self._voice(),
            output_format=_output_format(output_sample_rate or SAMPLE_RATE),
            language=language,
        )
        self._cancelled = False
        logger.debug("TTS streaming context started")

    async def send_text_chunk(self, text: str, is_last: bool = False) -> None:
        """Send a text chunk to the current streaming context.

        Args:
            text: Text to synthesize (empty is allowed — used to flush the end).
            is_last: If True, signals no more text will follow.
        """
        if not self._ctx or self._cancelled:
            return

        try:
            if text:
                await self._ctx.push(text)
            if is_last:
                await self._ctx.no_more_inputs()
        except Exception as e:
            if not self._cancelled:
                logger.error(f"TTS send error: {e}")

    async def receive_audio(self):
        """Yield PCM audio chunks from the current streaming context."""
        if not self._ctx:
            return

        try:
            async for r in self._ctx.receive():
                if self._cancelled:
                    break
                if r.type == "chunk" and r.audio:
                    yield r.audio
        except Exception as e:
            if not self._cancelled:
                logger.error(f"TTS receive error: {e}")

    def cancel(self) -> None:
        """Cancel the current streaming context (for barge-in).

        Sets a flag that stops ``receive_audio`` iteration and abandons the
        context; a new one is created for the next turn.
        """
        self._cancelled = True
        self._ctx = None
        logger.debug("TTS streaming cancelled (barge-in)")
