"""Per-language turn-classifier dispatcher + factory.

Routes turn classification to the right backend per language (e.g. Greek → Groq,
English → Smart Turn). Continuation detection always uses Groq (text/semantic).
Fail-open everywhere: any error returns "complete" so the pipeline falls back to
dispatching with the grace timer.

``build_dispatcher`` is the pure factory (explicit args, no ConfigManager
coupling) — the caller maps its own config/credentials to the kwargs.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np

from audio.providers.turn.groq import GroqTurnClassifier
from audio.providers.turn.smart_turn import SmartTurnClassifier, default_model_path

logger = logging.getLogger(__name__)


class TurnClassifierDispatcher:
    """Routes turn classification to the right backend per language.

    Smart Turn (audio-based) for supported languages, Groq (text-based) for
    others. Continuation detection always uses Groq (semantic/text-based).
    """

    def __init__(
        self,
        smart_turn: Optional[SmartTurnClassifier],
        groq: Optional[GroqTurnClassifier],
        lang_map: dict[str, str],
        default_backend: str,
    ) -> None:
        self._smart_turn = smart_turn
        self._groq = groq
        self._lang_map = lang_map
        self._default_backend = default_backend
        logger.info(
            f"Turn classifier dispatcher: lang_map={lang_map}, "
            f"default={default_backend}, "
            f"smart_turn={'yes' if smart_turn else 'no'}, "
            f"groq={'yes' if groq else 'no'}"
        )

    def backend_for_language(self, lang: str) -> str:
        """Return the EFFECTIVE backend ('smart_turn' or 'groq') for a language.

        The configured choice, but falling back to the other backend when the
        chosen one isn't available — e.g. a language set to Groq falls back to
        Smart Turn when no Groq key is configured (and vice versa).
        """
        backend = self._lang_map.get(lang, self._default_backend)
        if backend == "groq" and self._groq is None and self._smart_turn is not None:
            return "smart_turn"
        if backend == "smart_turn" and self._smart_turn is None and self._groq is not None:
            return "groq"
        return backend

    def needs_audio(self, lang: str) -> bool:
        """True if the backend for this language needs raw audio (Smart Turn)."""
        backend = self.backend_for_language(lang)
        return backend == "smart_turn" and self._smart_turn is not None

    async def classify(
        self,
        text: str,
        context: str,
        language: str,
        audio_16k: Optional[np.ndarray] = None,
    ) -> Optional[bool]:
        """Classify turn completeness. Routes to the right backend.

        Returns True (complete), False (incomplete), or None (use heuristic).
        """
        backend = self.backend_for_language(language)

        if backend == "smart_turn" and self._smart_turn is not None:
            if audio_16k is not None and len(audio_16k) > 0:
                return await self._smart_turn.classify_audio(audio_16k)
            else:
                logger.warning("Smart Turn selected but no audio available, falling back")
                # Fall through to Groq if available
                if self._groq is not None:
                    return await self._groq.classify(text, context)
                return None

        if backend == "groq" and self._groq is not None:
            return await self._groq.classify(text, context)

        # No backend available
        return None

    async def classify_continuation(
        self, original: str, follow_up: str, context: str,
    ) -> bool:
        """Classify follow-up as continuation or new request. Always uses Groq."""
        if self._groq is not None:
            return await self._groq.classify_continuation(original, follow_up, context)
        return False  # no Groq → treat as new request

    def drain_usage(self) -> tuple[int, int]:
        """(input, output) Groq classifier tokens since the last drain, then reset.
        Smart Turn is local (no cost) → contributes nothing; no Groq backend → (0, 0)."""
        if self._groq is not None:
            return self._groq.drain_usage()
        return 0, 0

    def classifier_model(self) -> str:
        """The Groq model used for classification (for local cost pricing), or "" when
        there's no Groq backend (Smart-Turn-only routes)."""
        return self._groq.model if self._groq is not None else ""

    async def close(self) -> None:
        if self._groq is not None:
            await self._groq.close()


def build_dispatcher(
    *,
    smart_turn_enabled: bool,
    smart_turn_threshold: float,
    smart_turn_onnx_threads: int,
    smart_turn_audio_window_s: float,
    groq_api_key: str,
    lang_map: dict[str, str],
    default_backend: str,
    model_path: str | None = None,
    groq_base_url: str | None = None,
) -> Optional[TurnClassifierDispatcher]:
    """Build a dispatcher from explicit settings (pure — no ConfigManager).

    The caller maps its config/credentials to these kwargs. Groq is enabled
    whenever a key is
    supplied (``groq_api_key`` resolves from the Direct LLM execution layer); its
    model is fixed. ``groq_base_url`` overrides Groq's endpoint — for the hosted
    relay path it points at the OtoDock relay (and ``groq_api_key`` is then a
    minted session token); ``None`` uses Groq directly (BYO key). Returns None
    when no backend is available. The Smart Turn model defaults to the bundled
    ONNX (``default_model_path()``) when ``model_path`` is None.
    """
    smart_turn = None
    groq = None

    if smart_turn_enabled:
        path = model_path or default_model_path()
        if os.path.isfile(path):
            try:
                smart_turn = SmartTurnClassifier(
                    model_path=path,
                    threshold=smart_turn_threshold,
                    num_threads=smart_turn_onnx_threads,
                    audio_window_s=smart_turn_audio_window_s,
                )
            except Exception as e:
                logger.error(f"Failed to load Smart Turn classifier: {e}")
        else:
            logger.warning(f"Smart Turn model not found: {path}")

    if groq_api_key:
        groq = GroqTurnClassifier(groq_api_key, base_url=groq_base_url)

    if smart_turn is None and groq is None:
        logger.info("No turn classifier backends available")
        return None

    return TurnClassifierDispatcher(
        smart_turn=smart_turn,
        groq=groq,
        lang_map=lang_map,
        default_backend=default_backend,
    )
