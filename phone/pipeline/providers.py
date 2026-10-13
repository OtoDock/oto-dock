"""Build a route's STT/TTS provider instances from pushed config + pre-warm
fillers.

Shared by the per-call pipeline (``CallPipeline.__init__``) and the filler
pre-warm, so pre-warmed clips use the exact provider + voice + language a real
call on that route will resolve to. Keeping ``get_provider_class`` resolved in
this module (not core) lets both paths be faked together in tests.
"""

import asyncio
import contextlib
import logging

import config
from audio.providers.registry import get_provider_class
from audio.providers.tts.base import TTSProvider
from audio.providers.turn.dispatcher import build_dispatcher
from audio.providers.turn.smart_turn import prewarm_smart_turn
from audio.streaming.lang import base_lang
from config_manager import ConfigManager
from fillers import filler_cache

logger = logging.getLogger("pipeline.providers")


def prewarm_turn_classifier(cfg: ConfigManager) -> None:
    """Load the Smart Turn model ahead of any call. Synchronous and run off
    the loop (a process's first load takes seconds); a no-op once loaded
    and while Smart Turn is off. A failed load is logged here and again by
    the dispatcher build, which then runs on Groq alone."""
    if not cfg.smart_turn_enabled:
        return
    try:
        prewarm_smart_turn(None, cfg.smart_turn_onnx_threads)
    except Exception as e:
        logger.warning(f"Smart Turn pre-warm failed: {e}")


async def build_turn_classifier(cfg: ConfigManager):
    """The per-call turn classifier: the Smart Turn load in a thread, the
    dispatcher on the loop (its Groq client is cheap to build)."""
    await asyncio.to_thread(prewarm_turn_classifier, cfg)
    return build_dispatcher(
        smart_turn_enabled=cfg.smart_turn_enabled,
        smart_turn_threshold=cfg.smart_turn_threshold,
        smart_turn_onnx_threads=cfg.smart_turn_onnx_threads,
        smart_turn_audio_window_s=cfg.smart_turn_audio_window_s,
        groq_api_key=cfg.groq_api_key,
        groq_base_url=cfg.groq_base_url,
        lang_map=cfg.lang_backend_map,
        default_backend=cfg.turn_classifier_default_backend,
    )


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
        model = cls.live_model_id(advanced.get("model_id"))
        if model:
            kwargs["model"] = model
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
    active_keys: set[tuple] = set()
    had_error = False

    for route in cfg.enabled_routes():
        provider = cfg.resolve_route_settings(route).tts_provider
        if not provider:
            continue  # no real TTS configured → nothing to synthesize
        locked = base_lang(route.language)

        tts = None
        try:
            # Object construction is cheap (no network); build it to resolve the
            # voice exactly as a call would (the route's pinned voice, else the
            # map with its English fallback), then only open the connection
            # when this combo actually needs (re)synthesis. Routes sharing a
            # filler key synth once; two routes on one provider and language
            # with different pinned voices are two combos.
            tts = build_tts(provider)
            if route.voice_id:
                tts.pin_voice(route.voice_id)
            tts.select_voice(locked)
            key = filler_key_for(provider, tts, locked)
            if key in active_keys:
                continue
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
            logger.warning(f"Filler pre-warm failed for route {route.id} ({locked}): {e}")
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
