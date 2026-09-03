"""Build a route's STT/TTS provider instances from pushed config + pre-warm
fillers.

Shared by the per-call pipeline (``CallPipeline.__init__``) and the filler
pre-warm, so pre-warmed clips use the exact provider + voice + language a real
call on that route will resolve to. Keeping ``get_provider_class`` resolved in
this module (not core) lets both paths be faked together in tests.
"""

import contextlib
import logging

import config
from audio.providers.registry import get_provider_class
from audio.providers.tts.base import TTSProvider
from audio.streaming.lang import base_lang
from config_manager import ConfigManager
from fillers import filler_cache

logger = logging.getLogger("pipeline.providers")


def build_stt(cfg: ConfigManager, provider: dict | None):
    """Build the call STT from a resolved provider-map entry.

    Deepgram (the only streaming call STT today) takes the phone's audio knobs +
    the provider's call endpointing / model; other STT providers build from their
    row factory. ``provider`` is None only when no STT default is configured — we
    still build Deepgram on the global knobs so construction never crashes.
    """
    p = provider or {}
    name = p.get("provider_name", "deepgram")
    advanced = p.get("advanced") or {}
    api_key = p.get("api_key", "")
    cls = get_provider_class("stt", name)
    if name == "deepgram":
        kwargs = dict(
            api_key=api_key,
            endpointing_ms=int(advanced.get("call_endpointing_ms", cfg.deepgram_endpointing_ms)),
            vad_silence_offset_ms=cfg.vad_silence_offset_ms,
        )
        if advanced.get("model_id"):
            kwargs["model"] = advanced["model_id"]
        return cls(**kwargs)
    return cls.from_row(
        {"credential_key": "_", "advanced": advanced, "voices": p.get("voices") or {}},
        lambda _k: api_key,
    )


def build_tts(provider: dict | None) -> TTSProvider:
    """Build the call TTS from a resolved provider-map entry (None → Cartesia
    with no voices, a safety fallback). Voices + advanced come from the provider
    row; the call-language voice is selected by the caller via ``select_voice``."""
    p = provider or {}
    name = p.get("provider_name", "cartesia")
    cls = get_provider_class("tts", name)
    return cls.from_row(
        {"credential_key": "_", "voices": p.get("voices") or {}, "advanced": p.get("advanced") or {}},
        lambda _k: p.get("api_key", ""),
    )


def filler_key_for(
    provider: dict | None, tts: TTSProvider, language: str,
    sample_rate: int = config.SAMPLE_RATE,
) -> tuple:
    """The ``(provider_id, voice_id, language, sample_rate)`` filler-cache key
    for a built TTS.

    Must match the key the call path computes (see fillers.py) so a pre-warmed
    combo is found by the call. The rate rides the key so telephony clips and
    duplex clips never cross (same voice, different PCM rate).
    """
    pid = (provider or {}).get("id", "default")
    return (str(pid), tts.voice_id, language, sample_rate)


async def prewarm_fillers(cfg: ConfigManager) -> None:
    """Synthesize each enabled route's filler clips ahead of the first call.

    Runs in the background after every config (re)load so a real call never pays
    the synth latency, and admin phrase/voice edits refresh the affected combos.
    Combos already warm with the same phrases are skipped without opening a TTS
    connection (the ``needs_synth`` gate). Best-effort — one combo's failure never
    blocks the others or the daemon.
    """
    backchannel = cfg.backchannel_phrases
    thinking = cfg.thinking_phrases
    seen: set[tuple] = set()
    active_keys: set[tuple] = set()
    had_error = False

    for route in cfg.enabled_routes():
        provider = cfg.resolve_route_settings(route).tts_provider
        if not provider:
            continue  # no real TTS configured → nothing to synthesize
        locked = base_lang(route.language)
        # Dedupe by (provider, language) — that pair fully determines the voice
        # (select_voice) and thus the combo, so routes sharing it synth once.
        dedup = (provider.get("id"), locked)
        if dedup in seen:
            continue
        seen.add(dedup)

        tts = None
        try:
            # Object construction is cheap (no network); build it to resolve the
            # voice exactly as a call would (English fallback), then only open the
            # connection when this combo actually needs (re)synthesis.
            tts = build_tts(provider)
            tts.select_voice(locked)
            key = filler_key_for(provider, tts, locked)
            active_keys.add(key)
            if filler_cache.needs_synth(
                key, backchannel_phrases=backchannel, thinking_phrases=thinking,
            ):
                await tts.connect()
                await filler_cache.ensure(
                    key, tts,
                    backchannel_phrases=backchannel, thinking_phrases=thinking,
                )
        except Exception as e:
            had_error = True
            logger.warning(f"Filler pre-warm failed for {dedup}: {e}")
        finally:
            if tts is not None:
                with contextlib.suppress(Exception):
                    await tts.close()

    # Evict combos no longer used by any active route (voice/provider/route
    # change). Skipped if any combo failed to build, so a transient error never
    # evicts a still-valid combo. A pruned combo in use by an in-flight call
    # degrades gracefully (no filler for the rest of that rare race).
    if not had_error:
        filler_cache.prune(active_keys)
    logger.info(f"Filler pre-warm complete ({len(active_keys)} combo(s))")
