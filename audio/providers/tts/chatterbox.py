"""Resemble Chatterbox Multilingual TTS — STUB (not implemented yet).

License: the Resemble Chatterbox model is MIT; individual voice packs may carry
separate licenses (some CC-BY-NC) — the operator's responsibility to verify.

The real implementation's heavy deps install via:  pip install 'oto-audio[chatterbox]'

The class is importable; only instantiation raises.
"""

from __future__ import annotations

from audio.capabilities import TTSCapabilities
from audio.providers.credential_resolver import CredentialResolver
from audio.providers.tts.base import TTSProvider

_HINT = "Chatterbox TTS is not implemented yet. Install deps with: pip install 'oto-audio[chatterbox]'"


class ChatterboxTTS(TTSProvider):
    """Stub for the Resemble Chatterbox local TTS model."""

    capabilities = TTSCapabilities(supports_streaming=True, supports_endpointing=False, is_local=True)
    is_free_tier = True  # local model — no per-use cost
    is_stub = True       # hidden from the admin add-menu until implemented

    def __init__(self, **kwargs):
        raise NotImplementedError(_HINT)

    @classmethod
    def from_row(cls, row: dict, resolver: CredentialResolver) -> "ChatterboxTTS":
        return cls()

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def synthesize(self, text: str, *, language: str | None = None) -> bytes: return b""
    def start_streaming_context(self, *, output_sample_rate: int | None = None, language: str | None = None) -> None: ...
    async def send_text_chunk(self, text: str, is_last: bool = False) -> None: ...
    async def receive_audio(self): ...
    def cancel(self) -> None: ...
