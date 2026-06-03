"""Groq turn classifier (fast cloud inference, text-based).

Two jobs: classify whether the caller's current speech is a complete turn, and
classify whether a follow-up is a continuation of the original request. Both
fail open (return the safe default on error) so the pipeline keeps moving.
"""

from __future__ import annotations

import logging

import httpx

from audio.log_policy import log_transcript
from audio.providers.turn.base import TurnClassifier

logger = logging.getLogger(__name__)

# gpt-oss is a reasoning model: thinking tokens count against the completion
# budget, and the one-word verdict is only emitted after them. The budget must
# leave room for both — if reasoning exhausts it the content comes back empty
# and the classifier falls open (returns the safe default). reasoning_effort
# "low" keeps the thinking short; include_reasoning=False drops the reasoning
# text from the response (it rides a separate field on gpt-oss, never content).
_MAX_COMPLETION_TOKENS = 512

_SYSTEM_PROMPT = (
    "You are a turn-taking classifier for a live phone call with an AI assistant. "
    "The caller speaks naturally with pauses between phrases. "
    "Speech is transcribed in real-time and split into numbered segments at pauses.\n\n"
    "Given the recent conversation and the caller's current speech segments, "
    "determine if they have finished their request or are still mid-thought.\n\n"
    "STT transcription quirks (IMPORTANT):\n"
    "- The speech-to-text engine often OMITS question marks from questions. "
    "A segment ending with a period (.) may actually be a question — judge by meaning, not punctuation.\n"
    "- Transcription may be garbled or grammatically imperfect. Focus on the caller's INTENT.\n"
    "- Example: 'Πώς είναι ο πρόεδρος της Αμερικής.' is a COMPLETE question despite the period.\n"
    "- Example: 'Can you tell me what the weather is.' is a COMPLETE question despite the period.\n\n"
    "Guidelines:\n"
    "- A complete turn is a full question, request, or statement that can be acted on.\n"
    "- INCOMPLETE indicators: ends with a preposition, conjunction, article, or dangling verb "
    "expecting an object (e.g. 'Tell me about...', 'I want to...').\n"
    "- A single greeting like 'Γεια' followed by a question is complete once the question ends.\n"
    "- When in doubt, lean toward 'complete' — it's better to respond quickly than wait.\n\n"
    'Reply ONLY with "complete" or "incomplete".'
)


class GroqTurnClassifier(TurnClassifier):
    """Turn classifier using Groq's fast inference API."""

    # Groq's native OpenAI-compatible endpoint (used for a BYO key). The hosted
    # path passes the OtoDock relay base instead — the request path stays
    # ``/chat/completions``, so httpx composes ``{base}/chat/completions`` either
    # way (relay base already ends in ``/v1/relay/groq/v1``).
    DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(
        self,
        api_key: str,
        model: str = "openai/gpt-oss-120b",
        base_url: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url or self.DEFAULT_BASE_URL
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
        )
        # Accumulated Groq token usage across this call's classifications, drained
        # once at call teardown for local per-agent cost tracking (/admin/usage).
        # The hosted relay bills these calls independently; this is local display.
        self._in_tokens = 0
        self._out_tokens = 0
        hosted = self._base_url != self.DEFAULT_BASE_URL
        logger.info(f"Groq turn classifier initialized (model={model}, hosted={hosted})")

    @property
    def model(self) -> str:
        return self._model

    def _accumulate_usage(self, data: dict) -> None:
        """Add the response's reported token usage to the running totals (best-effort —
        a non-streaming Groq response carries ``usage`` inline)."""
        u = data.get("usage") or {}
        self._in_tokens += int(u.get("prompt_tokens") or 0)
        self._out_tokens += int(u.get("completion_tokens") or 0)

    def drain_usage(self) -> tuple[int, int]:
        """Return (input, output) tokens accumulated since the last drain, then reset.
        Called once at call teardown → one local usage row per call (not per classify)."""
        in_tok, out_tok = self._in_tokens, self._out_tokens
        self._in_tokens = 0
        self._out_tokens = 0
        return in_tok, out_tok

    async def classify(self, text: str, context: str) -> bool:
        """Classify turn completeness via Groq API. Returns True if complete.

        ``text`` may contain numbered segments (``[1] ...\n[2] ...``) or
        a single plain string for one-segment turns.
        """
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"[Recent conversation]\n{context}\n\n"
                    f"[Caller's current speech]\n{text}"
                ),
            },
        ]

        try:
            resp = await self._client.post(
                "/chat/completions",
                json={
                    "model": self._model,
                    "messages": messages,
                    "max_completion_tokens": _MAX_COMPLETION_TOKENS,
                    "temperature": 0.0,
                    "reasoning_effort": "low",
                    "include_reasoning": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._accumulate_usage(data)
            answer = (data["choices"][0]["message"]["content"] or "").strip().lower()
            is_complete = not answer.startswith("inc")
            log_transcript(logger, f"Groq '{answer}' for", text[:80])
            return is_complete
        except Exception as e:
            logger.warning(f"Groq classifier error: {e}")
            return True  # fail-open

    async def classify_continuation(
        self, original: str, follow_up: str, context: str,
    ) -> bool:
        """Classify whether follow-up speech is a continuation of the original.

        Returns True if follow_up is a continuation/addition (should be
        discarded), False if it's a new separate request.
        """
        messages = [
            {
                "role": "system",
                "content": (
                    "You classify follow-up speech in a live phone call.\n"
                    "The caller said something, the AI started responding, "
                    "then the caller added more words.\n\n"
                    "Determine if the follow-up is:\n"
                    "- 'continuation': completes an unfinished sentence, or adds "
                    "detail/clarification/specification to the original request. "
                    "IMPORTANT: if the original ends mid-sentence (e.g. ends with "
                    "a verb expecting an object, a preposition, or a conjunction), "
                    "the follow-up is almost certainly a continuation that finishes "
                    "the sentence.\n"
                    "- 'new': a completely different topic or an independent request "
                    "that has no grammatical or semantic link to the original.\n\n"
                    'Reply ONLY with "continuation" or "new".'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"[Recent conversation]\n{context}\n\n"
                    f"[Original request]: {original}\n"
                    f"[AI is now responding to the above]\n"
                    f"[Caller then added]: {follow_up}"
                ),
            },
        ]

        try:
            resp = await self._client.post(
                "/chat/completions",
                json={
                    "model": self._model,
                    "messages": messages,
                    "max_completion_tokens": _MAX_COMPLETION_TOKENS,
                    "temperature": 0.0,
                    "reasoning_effort": "low",
                    "include_reasoning": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._accumulate_usage(data)
            answer = (data["choices"][0]["message"]["content"] or "").strip().lower()
            is_continuation = answer.startswith("cont")
            log_transcript(
                logger,
                f"Groq continuation '{answer}'",
                f"original={original[:50]!r} follow_up={follow_up[:50]!r}",
            )
            return is_continuation
        except Exception as e:
            logger.warning(f"Groq continuation classifier error: {e}")
            return False  # fail-open: treat as new request (safer)

    async def close(self) -> None:
        await self._client.aclose()
