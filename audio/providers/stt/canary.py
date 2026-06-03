"""NVIDIA Canary 1B v2 STT — STUB (not implemented yet).

License: NVIDIA Canary 1B v2 uses the NVIDIA Open Model License (Apache-
compatible, but with trademark restrictions). The operator must accept NVIDIA's
license before downloading the model weights.

The real implementation's heavy deps install via:  pip install 'oto-audio[canary]'

The class is importable (so the registry can list it); only instantiation
raises.
"""

from __future__ import annotations

from audio.capabilities import STTCapabilities
from audio.providers.credential_resolver import CredentialResolver
from audio.providers.stt.base import STTProvider

_HINT = "NVIDIA Canary STT is not implemented yet. Install deps with: pip install 'oto-audio[canary]'"


class NvidiaCanarySTT(STTProvider):
    """Stub for the NVIDIA Canary local STT model."""

    capabilities = STTCapabilities(
        supports_streaming=True,
        supports_transcribe_file=True,
        supports_endpointing=False,
        supports_word_timestamps=True,
        is_local=True,
    )
    is_free_tier = True  # local model — no per-use cost
    is_stub = True       # hidden from the admin add-menu until implemented

    def __init__(self, **kwargs):
        raise NotImplementedError(_HINT)

    @classmethod
    def from_row(cls, row: dict, resolver: CredentialResolver) -> "NvidiaCanarySTT":
        return cls()

    # Concrete (unreachable — __init__ raises) implementations of the ABC surface
    # so the class is instantiable enough to produce the helpful error above.
    @property
    def endpointing_ms(self) -> int: return 0
    async def start(self, language: str = "multi", sample_rate: int | None = None, interim_results: bool = False, endpointing_ms: int | None = None) -> None: ...
    async def send_audio(self, audio_bytes: bytes) -> None: ...
    def drain_transcript(self) -> str | None: return None
    async def wait_for_transcript(self, timeout: float = 1.0) -> str | None: return None
    def clear_queue(self) -> None: ...
    async def finish(self) -> str | None: return None
    async def close(self) -> None: ...
