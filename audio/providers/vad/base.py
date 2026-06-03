"""Shared VAD types — the IDLE/SPEAKING state machine's event vocabulary.

These enums are the contract the call pipeline (and the chat-STT VAD gate)
consume; the Silero implementation lives in ``silero.py``. There is one VAD
implementation today, so there is deliberately no provider ABC here — add one
when a second VAD backend actually lands (YAGNI).
"""

from __future__ import annotations

import enum

__all__ = ["VadState", "VadEvent"]


class VadState(enum.Enum):
    IDLE = "idle"
    SPEAKING = "speaking"


class VadEvent(enum.Enum):
    NONE = "none"
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"
    SPEECH_PROBABLE = "speech_probable"  # early hint during barge-in (~64 ms)
