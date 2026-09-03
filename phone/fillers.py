"""Pre-synthesized filler audio for natural voice conversations.

Two kinds of filler:
  - Backchannel sounds ("mhm", "ok") — played between a caller's speech
    segments while they're still talking, to signal active listening.
  - Thinking fillers ("hmm", "let me see") — played after a turn is dispatched
    to the LLM, to mask processing delay (one merged per-language list; the
    play policy — latency gating + repeat damping — lives in the pipeline).

Clips are synthesized per ``(provider_id, voice_id, language)`` combo so a
route's fillers always match its resolved TTS voice (a call locked to Greek on a
Greek voice gets Greek clips in that voice; an English route on another provider
gets its own). The cache is process-wide and shared across calls; playback is
zero-latency (just sending the cached PCM frames).

A combo is also keyed by a content signature of its phrases, so an admin edit to
a language's filler phrases re-synthesizes exactly the affected combos on the
next config push (``ensure`` is a no-op when the phrases are unchanged). The
phone daemon pre-warms every active route's combo on config push
(``pipeline.providers.prewarm_fillers``); ``prune`` drops combos no route uses
anymore.
"""

import asyncio
import logging
import random
from dataclasses import dataclass

import numpy as np

import config

logger = logging.getLogger("fillers")


def _condition_clip(pcm: bytes, sample_rate: int = config.SAMPLE_RATE) -> bytes:
    """Clean a one-shot TTS clip's mechanical artifacts before caching.

    Short non-lexical phrases ("χμμ", "hmm") often come back with abrupt
    boundaries (mid-waveform cut → an audible click that reads as
    distortion) and occasionally hot peaks. Trim leading/trailing
    near-silence (keeping 20ms of padding), apply a 10ms linear fade at
    both edges, and scale DOWN if the peak clips hot (never boost — that
    would raise synth noise). Returns the input unchanged when too short
    to condition.
    """
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    fade_n = sample_rate // 100      # 10ms
    pad_n = sample_rate // 50        # 20ms
    if len(samples) < 4 * fade_n:
        return pcm

    # Trim to the audible region (threshold well under speech, above hiss)
    audible = np.flatnonzero(np.abs(samples) > 250)
    if audible.size:
        start = max(0, int(audible[0]) - pad_n)
        end = min(len(samples), int(audible[-1]) + pad_n)
        if end - start >= 4 * fade_n:
            samples = samples[start:end]

    # Cap hot peaks (scale down only)
    peak = float(np.max(np.abs(samples))) or 1.0
    if peak > 0.85 * 32767:
        samples = samples * (0.85 * 32767 / peak)

    # Edge fades kill boundary clicks
    ramp = np.linspace(0.0, 1.0, fade_n, dtype=np.float32)
    samples[:fade_n] *= ramp
    samples[-fade_n:] *= ramp[::-1]

    return samples.astype(np.int16).tobytes()

# Cache key: (provider_id, voice_id, language, sample_rate) — an 8 kHz call
# clip must never replay into a 24 kHz duplex session (chipmunk pitch).
FillerKey = tuple


@dataclass
class FillerClip:
    """A pre-synthesized filler audio clip."""
    text: str
    pcm_audio: bytes
    duration_s: float


class FillerCache:
    """Process-wide filler cache keyed by ``(provider_id, voice_id, language)``.

    Call ``ensure(key, tts, ...)`` once per combo (idempotent + per-combo
    locked) to synthesize its clips; ``get_backchannel`` / ``get_thinking`` then
    return clips instantly.
    """

    def __init__(self):
        self._backchannels: dict[FillerKey, list[FillerClip]] = {}
        self._thinking: dict[FillerKey, list[FillerClip]] = {}
        self._ready: set[FillerKey] = set()
        self._sigs: dict[FillerKey, tuple] = {}  # content signature per combo
        self._locks: dict[FillerKey, asyncio.Lock] = {}

        # Repeat avoidance: track last played index per combo + category
        self._last_backchannel: dict[FillerKey, int] = {}
        self._last_thinking: dict[FillerKey, int] = {}

    def is_ready(self, key: FillerKey) -> bool:
        """Whether ``key``'s clips have already been synthesized."""
        return key in self._ready

    def _lock_for(self, key: FillerKey) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    @staticmethod
    def _content_sig(
        language: str,
        backchannel_phrases: dict[str, list[str]],
        thinking_phrases: dict[str, list[str]],
    ) -> tuple:
        """Hashable signature of the phrases that drive a combo's clips, so an
        admin edit to a language's fillers invalidates exactly those combos."""
        return (
            tuple(backchannel_phrases.get(language, []) or []),
            tuple(thinking_phrases.get(language, []) or []),
        )

    def needs_synth(
        self,
        key: FillerKey,
        *,
        backchannel_phrases: dict[str, list[str]],
        thinking_phrases: dict[str, list[str]],
    ) -> bool:
        """Whether ``ensure`` would (re)synthesize this combo — it's missing or
        its phrase content changed. Lets the pre-warm skip opening a TTS
        connection for combos already warm with the same phrases."""
        sig = self._content_sig(key[2], backchannel_phrases, thinking_phrases)
        return self._sigs.get(key) != sig

    async def ensure(
        self,
        key: FillerKey,
        tts,
        *,
        backchannel_phrases: dict[str, list[str]],
        thinking_phrases: dict[str, list[str]],
    ) -> None:
        """Synthesize ``key``'s filler clips, (re)doing it only when the combo is
        new or its phrases changed (idempotent, concurrency-safe).

        ``tts`` must already have its voice selected for this combo; the language
        is ``key[2]``. Phrases are the full per-language maps (only the combo's
        language is read). Safe to call on every call — returns immediately once
        the combo is cached with the same phrases (refresh-on-edit otherwise).
        """
        sig = self._content_sig(key[2], backchannel_phrases, thinking_phrases)
        if self._sigs.get(key) == sig:
            return
        async with self._lock_for(key):
            if self._sigs.get(key) == sig:
                return

            lang = key[2]
            rate = key[3] if len(key) > 3 else config.SAMPLE_RATE
            logger.info(f"Filler cache: synthesizing {key} ...")

            self._backchannels[key] = await self._synth(
                tts, lang, backchannel_phrases.get(lang, []), "backchannel", rate)
            self._thinking[key] = await self._synth(
                tts, lang, thinking_phrases.get(lang, []), "thinking", rate)

            # Clip lists were just replaced — reset repeat-avoidance pointers.
            self._last_backchannel.pop(key, None)
            self._last_thinking.pop(key, None)

            self._sigs[key] = sig
            self._ready.add(key)
            total = len(self._backchannels[key]) + len(self._thinking[key])
            logger.info(f"Filler cache: {total} clips ready for {key}")

    def prune(self, active_keys: set) -> None:
        """Drop cached combos no longer used by any active route.

        ``active_keys`` is the full set of ``(provider, voice, language)`` combos
        the current config still uses. Called by the pre-warm after a config
        reload so voice/provider/route changes don't leak stale clips. A combo
        still in use by an in-flight call degrades gracefully if pruned (no
        filler, no crash). Locks are left in place (cheap, avoids a synth race).
        """
        for key in list(self._ready):
            if key in active_keys:
                continue
            self._backchannels.pop(key, None)
            self._thinking.pop(key, None)
            self._sigs.pop(key, None)
            self._last_backchannel.pop(key, None)
            self._last_thinking.pop(key, None)
            self._ready.discard(key)
            logger.info(f"Filler cache: pruned stale combo {key}")

    async def _synth(
        self, tts, lang: str, phrases: list[str], label: str,
        sample_rate: int = config.SAMPLE_RATE,
    ) -> list[FillerClip]:
        """Synthesize a list of phrases in ``lang`` on the given TTS instance."""
        clips: list[FillerClip] = []
        for phrase in phrases:
            try:
                pcm = _condition_clip(
                    await tts.synthesize(
                        phrase, language=lang, output_sample_rate=sample_rate),
                    sample_rate)
                duration = len(pcm) / (sample_rate * config.SAMPLE_WIDTH)
                clips.append(FillerClip(text=phrase, pcm_audio=pcm, duration_s=duration))
                logger.debug(f"Filler: synthesized {label} [{lang}] '{phrase}' ({duration:.2f}s)")
            except Exception as e:
                logger.warning(f"Filler: failed to synthesize {label} '{phrase}' [{lang}]: {e}")
        return clips

    def _pick_random(
        self, clips: list[FillerClip], last_map: dict[FillerKey, int], key: FillerKey,
    ) -> FillerClip | None:
        """Pick a random clip avoiding the last-played one for this combo."""
        if not clips:
            return None
        if len(clips) == 1:
            return clips[0]

        last_idx = last_map.get(key, -1)
        candidates = [i for i in range(len(clips)) if i != last_idx]
        idx = random.choice(candidates)
        last_map[key] = idx
        return clips[idx]

    def get_backchannel(self, key: FillerKey) -> FillerClip | None:
        """Get a random backchannel clip for the combo."""
        return self._pick_random(self._backchannels.get(key, []), self._last_backchannel, key)

    def get_thinking(self, key: FillerKey) -> FillerClip | None:
        """Get a random thinking filler clip for the combo."""
        return self._pick_random(self._thinking.get(key, []), self._last_thinking, key)


# Module-level singleton — shared across all call pipelines
filler_cache = FillerCache()
