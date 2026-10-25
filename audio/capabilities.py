"""Capability descriptors for STT / TTS providers.

A provider exposes a frozen capabilities object as its ``capabilities`` class
attribute. The admin dashboard reads it to decide which knobs to render (e.g.
endpointing fields only when ``supports_endpointing``); the platform uses it to
gate features (e.g. ``supports_transcribe_file`` for the transcribe endpoint).

Billing metadata (unit / rate / free-tier) lives as classmethods on the
provider ABCs, not here — it is behaviour, not a static descriptor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Unit a provider bills on. The platform multiplies the right input
# (characters for most TTS, seconds for most STT) by the provider's rate.
BillingUnit = Literal["char", "second", "minute"]


@dataclass(frozen=True)
class STTCapabilities:
    """What an STT provider can do (read by the dashboard + feature gates)."""

    supports_streaming: bool = True          # live WebSocket transcription
    supports_transcribe_file: bool = False   # batch / prerecorded (file → text)
    supports_endpointing: bool = True        # tunable endpointing delay (ms)
    supports_word_timestamps: bool = False   # per-word start/end (needed for SRT)
    is_local: bool = False                   # runs on-box; no network / credential
    # Each live result carries a word confidence on one scale (the
    # ``heard`` ledger): automatic language detection compares two streams'
    # words only when both report it, else the acoustic rule decides alone.
    reports_confidence: bool = False


@dataclass(frozen=True)
class TTSCapabilities:
    """What a TTS provider can do."""

    supports_streaming: bool = True          # incremental text → audio context
    supports_endpointing: bool = False       # most TTS has none
    is_local: bool = False
