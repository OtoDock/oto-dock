"""Turn-taking classifier base.

Two backends with intentionally different inputs:
  - Smart Turn (``smart_turn.py``) — local ONNX prosody model, audio-based.
  - Groq (``groq.py``) — fast cloud inference, text-based.

They are NOT unified behind a single ``classify`` signature (audio vs text);
:class:`audio.providers.turn.dispatcher.TurnClassifierDispatcher` routes per
language and is the surface the pipeline uses. This base captures only the
shared lifecycle.
"""

from __future__ import annotations

__all__ = ["TurnClassifier"]


class TurnClassifier:
    """Common base for turn-taking classifiers (shared lifecycle only)."""

    async def close(self) -> None:
        """Release any resources (HTTP clients, sessions). Default: no-op."""
