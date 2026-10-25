"""One-time seed: populate audio + phone settings and the built-in providers.

Runs at proxy startup. Settings are written only for keys missing from
platform_settings; audio providers seed idempotently (ON CONFLICT).

Phone routes are NOT seeded here — a route requires a phone_server FK, which
is provisioned via the admin UI + adapter. The platform ships with zero
routes until then.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from adapters.phone import PHONE_CONTEXT, PHONE_OUTBOUND
from storage import database as task_store
from storage.pg import get_conn

logger = logging.getLogger("phone_config_seed")

# Default audio (shared chat+call) + phone (call-only) settings, seeded only
# when the key is absent.
# Keys carry an ``audio_`` prefix (VAD / smart-turn, shared with chat audio)
# or a ``phone_`` prefix (call-only). The phone server reads these with the
# prefix stripped (see phone_config.assemble_phone_config).
_DEFAULT_SETTINGS: dict[str, str] = {
    # VAD (production-tuned) — shared audio
    "audio_vad_threshold": "0.40",
    "audio_vad_silence_duration_ms": "350",
    "audio_vad_speech_pad_ms": "64",
    "audio_vad_min_energy_rms": "150",
    "audio_vad_silence_offset_ms": "50",
    # Turn classifier — Smart Turn (production-tuned) — shared audio
    "audio_smart_turn_enabled": "true",
    "audio_smart_turn_threshold": "0.65",
    "audio_smart_turn_onnx_threads": "1",
    "audio_smart_turn_audio_window_s": "8.0",
    # The caller's language — the local identifier (audio/providers/lid), on
    # calls, live voice and dictation; the languages it may pick (the
    # install's list: an install that speaks two languages cannot be moved
    # to a third one)
    "audio_language_identify_enabled": "true",
    "audio_language_identify_threads": "1",
    "audio_language_candidates": "en,el,de,es,fr,it",
    # Chat audio policy (chat sound/mic icons; consumed by the resolver)
    "audio_chat_user_policy": "native_preferred",
    "audio_show_experimental": "false",
    # Chat sound/mic icons — admin kill-switch (default on). Icon visibility
    # is availability-driven: the capability resolver hides the icons on its
    # own when no usable speech path (native or provider) exists.
    "audio_chat_enabled": "true",
    # Full-duplex chat voice (the phone daemon's conversation engine on
    # dashboard chats). Requires an explicit "true" — absence does NOT
    # enable, unlike audio_chat_enabled — and the capability additionally
    # needs a duplex-capable daemon + usable chat providers, so installs
    # without the phone service stay dictation-only either way.
    "audio_chat_duplex_enabled": "true",
    # Whole-session budget for one duplex session (proxy-enforced timer).
    "audio_duplex_max_seconds": "1800",
    # Wake word ("hey <agent>" → duplex). Install-wide kill switch for the
    # on-device browser listener; the real gate is the PER-USER opt-in
    # (user_audio_prefs.wake_word_enabled, default off). Threshold is the
    # spotter's acoustic trigger probability — RAISE it to cut false
    # accepts, lower toward the model default 0.25 if wakes are missed.
    # 0.35 missed real accented speech in live testing (2026-08-14);
    # 0.30 is the field-tuned default.
    "audio_wake_word_enabled": "true",
    "audio_wake_word_threshold": "0.30",
    # Per-line sensitivity for the PLATFORM ("hey OtoDock") keyword lines
    # only — agent-name lines keep the globals. Boost scales the keyword's
    # path score in the spotter's search (raise to favor it), the platform
    # threshold replaces the global one for those lines (lower = easier to
    # fire). Both must stay > 0 — non-positive/junk values fall back to
    # these defaults in code and are never emitted.
    "audio_wake_word_platform_boost": "2.0",
    "audio_wake_word_platform_threshold": "0.20",
    # The same pair for the AGENT-NAME lines (2026-09-11, measured on an
    # operator's real recordings: an accented "hey personal assistant" went
    # from 14 % to 30 % recall with these riders and a beam of 8, with zero
    # false fires on control speech; boost above 3 pollutes the beam).
    "audio_wake_word_agent_boost": "2.0",
    "audio_wake_word_agent_threshold": "0.20",
    # Chat STT defaults (per-VAD/limits for the WS session)
    "audio_chat_stt_max_seconds": "300",
    "audio_tts_max_chars_per_request": "5000",
    "audio_tts_rate_limit_chars_per_min": "10000",
    "audio_transcribe_max_upload_mb": "100",
    "audio_transcribe_max_duration_min": "60",
    # AMI DTMF event listener kill switch — phone. Seeded (unlike the pin_*
    # timing knobs) so the switch is VISIBLE in the admin settings UI: it is
    # what an operator reaches for when a misbehaving PBX needs the
    # persistent AMI connections gone. Daemon default is on either way.
    "phone_ami_dtmf_listener": "on",
    # Barge-in (production-tuned) — phone. VAD speech during playback
    # PAUSES the sender; a commit (cancel TTS + abort the turn) requires a
    # non-empty final transcript AND a speech episode >= bargein_timer_s —
    # the timer key kept its name and default across the pause/confirm/
    # commit redesign, so upgraded installs keep their tuned value; only
    # its role changed (was: VAD-only abort timer; now: minimum episode
    # duration composed with the transcript requirement).
    "phone_bargein_threshold": "0.35",
    "phone_bargein_debounce_ms": "300",
    "phone_bargein_chunk_ratio": "0.5",
    "phone_bargein_silence_duration_ms": "500",
    "phone_bargein_timer_s": "0.6",
    "phone_bargein_confirm_timeout_s": "3.0",
    "phone_bargein_resume_grace_s": "1.0",
    # Turn timing (production-tuned) — phone
    "phone_turn_complete_timeout_s": "1.0",
    "phone_turn_incomplete_timeout_s": "2.0",
    "phone_turn_classifier_grace_s": "0.0",
    "phone_turn_classifier_lang_map": "",
    # groq since 2026-08-25 (operator decision after live A/B): the text
    # classifier judges sentence completeness better than SmartTurn's
    # audio prosody in EVERY tested language, English included (SmartTurn
    # dispatched "Final thing I wanted to ask here is" as complete). The
    # dispatcher still falls back to smart_turn automatically when no
    # Groq relay/key is configured.
    "phone_turn_classifier_default_backend": "groq",
    # Fillers — phone. Enable/disable is per-route (phone_routes
    # backchannel_mode / thinking_filler_mode toggles); these tune timing.
    # The thinking filler is latency-gated: it only plays when the LLM
    # hasn't produced audio within delay_s (repeat_delay_s when one already
    # played in the previous turn).
    "phone_backchannel_min_segments": "1",
    "phone_backchannel_min_gap_s": "0.4",
    "phone_thinking_filler_delay_s": "0.5",
    "phone_thinking_filler_repeat_delay_s": "2.0",
    # Background ambience bed volume (0-1; the per-route template lives on
    # phone_routes.background_sound). Live-tuned: phone codecs compress the
    # bed louder than raw playback suggests — it must sit clearly UNDER the
    # voice, never compete with it.
    "phone_background_sound_gain": "0.17",
    # Voice texture (0-1): grain + early reflections that glue the TTS voice
    # into the ambience bed; active only on routes with a bed.
    "phone_voice_texture": "0.4",
    # Pre-response breath: a subtle inhale before the agent's voice, only
    # when it hasn't spoken for min_gap_s (mid-exchange breaths read as
    # uncanny). The clip carries a crescendo envelope (soft attack rising
    # into speech onset).
    "phone_breath_enabled": "true",
    "phone_breath_gain": "0.27",
    "phone_breath_min_gap_s": "1.5",
    # TTS pacing — phone (provider defaults, voices and endpointing live on the
    # audio_providers rows now, see phone_config.assemble_phone_config).
    "phone_tts_buffer_chars": "20",
    "phone_tts_response_gap_s": "0.4",
    # Timeouts — phone
    "phone_idle_timeout_s": "30",
    "phone_call_max_duration_s": "600",
    "phone_outbound_call_timeout_s": "300",
    "phone_question_answer_timeout_s": "40",
    # Infrastructure — phone (AMI host/port/user + secret now live on the
    # default phone_servers row, see phone_config.assemble_phone_config).
    "phone_audiosocket_host": "0.0.0.0",
    "phone_audiosocket_port": "9092",
    "phone_http_api_port": "9093",
    "phone_log_level": "INFO",
    # Call context prompts: the adapter's texts, one source for the seed, the
    # empty-setting fallback and the dashboard's Restore Default.
    "phone_context_inbound": PHONE_CONTEXT,
    "phone_context_outbound": PHONE_OUTBOUND,
    # Duplex-mode context (dashboard full-duplex voice): the spoken-reply
    # rules, adapted from the call contexts — same language + brevity + never
    # read structured output aloud, but the user HAS a screen, so rich content
    # goes through the display tools instead. Injected as a prefix on the
    # first duplex turn after each attach (not a warmup suffix — duplex
    # toggles mid-session). One call-control marker: [DUPLEX_COMPLETE].
    "chat_duplex_context": (
        "You are in a live spoken conversation — the user hears your replies "
        "aloud AND sees the chat on screen.\n"
        "RULES:\n"
        "- ALWAYS respond in the SAME LANGUAGE the user speaks. Never switch language mid-conversation.\n"
        "- Keep spoken replies SHORT: 1-3 conversational sentences. Summarize instead of listing details.\n"
        "- Talk naturally, the way you'd say it out loud — no stiff or formal phrasing.\n"
        "- NEVER read tables, lists, JSON, code, or URLs aloud. Show rich content on screen with your "
        "display tools instead, then describe it in one short sentence.\n"
        "- NO sources, citations, footnotes, or links in your replies — everything you write is spoken "
        "aloud, and 'Sources: …' sounds terrible. If sources matter, put them on screen via a display tool.\n"
        "- Before ANY tool call, FIRST write one short spoken line saying what you're doing "
        "(e.g. 'Let me check that' — in the SAME language as the conversation). NEVER go silent "
        "into a tool — in a spoken conversation silence feels broken. During multi-step tool "
        "work, add a brief spoken progress line between steps, then summarize the result in "
        "1-2 short sentences.\n"
        "ENDING THE CONVERSATION: append [DUPLEX_COMPLETE] at the very end of "
        "your reply to close the spoken session after it is read aloud. Use it "
        "when the user clearly says goodbye or is done ('thanks, good night'), "
        "or when you take on LONG work that should not keep the microphone "
        "open: say you'll continue and notify them when it's done (schedule a "
        "notification if appropriate), then end with the marker and keep "
        "working in the normal chat. The user can always reopen the "
        "conversation manually. Do NOT use it for quick answers.\n"
    ),
    # Per-language JSON blobs — phone. A language may pin its classifier
    # with a "turn_classifier" field ("groq" | "smart_turn"); none do by
    # default since 2026-08-25 — the default backend (groq, smart_turn
    # fallback) covers every language.
    "phone_phrases": json.dumps({
        "en": {
            "hold_message": "One moment please, let me check.",
            "greeting_fallback": "Hello, how can I help you?",
        },
        "el": {
            "hold_message": "Μια στιγμή παρακαλώ, θα το ελέγξω.",
            "greeting_fallback": "Γεια σας, πώς μπορώ να σας βοηθήσω;",
        },
        "de": {
            "hold_message": "Einen Moment bitte, ich schaue nach.",
            "greeting_fallback": "Hallo, wie kann ich Ihnen helfen?",
        },
        "es": {
            "hold_message": "Un momento por favor, déjeme comprobar.",
            "greeting_fallback": "Hola, ¿en qué puedo ayudarle?",
        },
        "fr": {
            "hold_message": "Un instant s'il vous plaît, je vérifie.",
            "greeting_fallback": "Bonjour, comment puis-je vous aider ?",
        },
        "it": {
            "hold_message": "Un momento per favore, controllo subito.",
            "greeting_fallback": "Salve, come posso aiutarla?",
        },
    }),
    "phone_backchannel_phrases": json.dumps({
        "el": ["ναι", "mmm", "mhm"],
        "en": ["mhm", "ok", "right", "mm-hmm", "uh-huh"],
        "de": ["mhm", "okay", "ja", "genau", "aha"],
        "es": ["ajá", "sí", "vale", "claro", "mmm"],
        "fr": ["mhm", "oui", "d'accord", "hmm", "voilà"],
        "it": ["mhm", "sì", "certo", "okay", "aha"],
    }),
    # One merged list per language — a natural mix of short hesitations and
    # fuller phrases; the pipeline picks randomly with repeat avoidance.
    "phone_thinking_phrases": json.dumps({
        "el": ["εε", "χμμ", "για να δω", "μισό λεπτό", "μάλιστα"],
        "en": ["hmm", "uhh", "let me see", "one moment", "let me check"],
        "de": ["hmm", "äh", "mal sehen", "einen Moment"],
        "es": ["mmm", "eh", "déjeme ver", "un momento", "a ver"],
        "fr": ["hmm", "euh", "voyons voir", "un instant", "alors"],
        "it": ["mmm", "ehm", "vediamo", "un attimo", "allora"],
    }),
}

# Built-in audio providers seeded on first install. Credentials are entered
# separately via the admin UI (infra_credentials keyed by ``credential_key``,
# inner key ``API_KEY``). ``voices`` (per-language TTS voice IDs) and
# ``advanced`` (endpointing) live on the row — the source of truth the phone
# config push reads. Neither a model id nor a voice is seeded: the provider
# classes ship both, and a row that stores none follows a later default.
# Fields: (type, name, label, credential_key, advanced, voices).
_DEFAULT_PROVIDERS = [
    ("stt", "deepgram", "Deepgram", "audio-deepgram",
     json.dumps({"call_endpointing_ms": 500, "chat_endpointing_ms": 1500}),
     json.dumps({})),
    ("tts", "cartesia", "Cartesia", "audio-cartesia",
     json.dumps({}),
     json.dumps({})),
]

#: The former default model ids the boot walk has already cleared from the
#: rows (a JSON list): a later explicit pick of the same id stays.
FORMER_DEFAULTS_WALKED = "audio_former_defaults_walked"

# Every call prompt an earlier release seeded (the dashboard's Restore Default
# wrote the oldest), as ``_prompt_digest`` of the text: a stored prompt that
# still reads as one of them was never edited and follows the adapter's
# current text at boot; any other text is an admin's and stays. A release that
# changes PHONE_CONTEXT or PHONE_OUTBOUND adds the digest of the text it
# replaces here, never the current one.
SHIPPED_CALL_PROMPTS: dict[str, frozenset[str]] = {
    "phone_context_inbound": frozenset({
        "8420e63f15387681a5fdb370ee4c57a42af7df01932b8959d7d88651af94c5fc",
        "10cc48190da79daa4b90c43ccb4320522a045c5c1b6804219a621c083236b7be",
        "a96225005990342b27929c7efebea0da05469bbcf25c2180a9258bc50f35f185",
    }),
    "phone_context_outbound": frozenset({
        "d33b8ae69333070854e54ff2bd3c833b5eb2232dbe882f4d50a8dcb7735b9119",
        "d2fa303c1ae2ac9d23b68c2eaa28c57182886169d7fb140e51717b5025196a1c",
        "bc92d4d08e110701011979c079f3674e09364c039ad90beafaf28252b4e345bb",
    }),
}


def _prompt_digest(text: str) -> str:
    """The identity of a call prompt's text: surrounding and trailing
    whitespace do not count (a textarea save drops a trailing newline)."""
    lines = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def remap_shipped_call_prompts() -> list[str]:
    """The boot walk over the two call prompts: a stored prompt that still
    reads as a text an earlier release seeded (``SHIPPED_CALL_PROMPTS``)
    takes the adapter's current text; an edited prompt stays. Idempotent (the
    current text is in no release's set). Returns the keys changed."""
    stored = task_store.get_all_platform_settings()
    changed: list[str] = []
    for key, digests in SHIPPED_CALL_PROMPTS.items():
        value = stored.get(key)
        if value is None or _prompt_digest(value) not in digests:
            continue
        task_store.set_platform_setting(key, _DEFAULT_SETTINGS[key])
        logger.info("%s read as an earlier release's default; it follows the current text", key)
        changed.append(key)
    return changed


def remap_retired_audio_models() -> dict[str, int]:
    """The boot walk over the audio providers' model ids
    (``audio.providers.model_succession``, the LLM registry's rule applied to
    audio): a RETIRED id stored on a row moves to its successor's chain end
    on every boot (the provider builds resolve it too, so a row written
    after the walk runs the successor as well); a FORMER DEFAULT is cleared
    from the rows once, so the live default applies, and the setting
    remembers it so an admin's later pick of that id stays. Returns
    ``{old id: rows changed}``; never raises past one row."""
    from audio.providers.model_succession import (
        DEFAULT_MODELS,
        FORMER_DEFAULTS,
        MODEL_ID_KEYS,
        MODEL_SUCCESSORS,
        successor_of,
    )

    from storage.prefs import audio_provider_store

    try:
        walked = set(json.loads(task_store.get_all_platform_settings().get(FORMER_DEFAULTS_WALKED) or "[]"))
    except (TypeError, ValueError):
        walked = set()
    changed: dict[str, int] = {}
    failed = False
    for row in audio_provider_store.get_all_providers():
        kind = (row["provider_type"], row["provider_name"])
        successors = MODEL_SUCCESSORS.get(kind, {})
        former = set(FORMER_DEFAULTS.get(kind, ())) - walked
        defaults = DEFAULT_MODELS.get(kind, {})
        advanced = dict(row.get("advanced") or {})
        moved = False
        for key in MODEL_ID_KEYS:
            stored = advanced.get(key)
            if not stored:
                continue
            if stored in successors:
                successor = successor_of(successors, stored)
                if successor == defaults.get(key):
                    # The successor is the live default: the row follows
                    # it unpinned, so a later default moves it too.
                    del advanced[key]
                else:
                    advanced[key] = successor
                logger.info("audio model %s on %s/%s row %s -> %s (retired)",
                            stored, *kind, row["id"], successor)
            elif stored in former:
                del advanced[key]
                logger.info("audio model %s on %s/%s row %s cleared (a former default; the live one applies)",
                            stored, *kind, row["id"])
            else:
                continue
            changed[stored] = changed.get(stored, 0) + 1
            moved = True
        if moved:
            try:
                audio_provider_store.update_provider(row["id"], {"advanced": advanced})
            except Exception:
                failed = True
                logger.exception("audio model remap on row %s failed (non-fatal)", row["id"])
    every_former = {m for members in FORMER_DEFAULTS.values() for m in members}
    # A row the walk could not write keeps its former default; the marker
    # waits for a boot that clears it, or that row would read as a pick.
    if not failed and not every_former <= walked:
        task_store.set_platform_setting(FORMER_DEFAULTS_WALKED, json.dumps(sorted(walked | every_former)))
    return changed


def _seed_audio_providers() -> None:
    """Insert the built-in audio providers. Idempotent via ON CONFLICT."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        for ptype, pname, label, cred_key, advanced, voices in _DEFAULT_PROVIDERS:
            conn.execute(
                """INSERT INTO audio_providers
                   (provider_type, provider_name, label, credential_key,
                    advanced, voices, is_default_calls, is_default_chat,
                    created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, TRUE, TRUE, %s, %s)
                   ON CONFLICT (provider_type, provider_name) DO NOTHING""",
                (ptype, pname, label, cred_key, advanced, voices, now, now),
            )
        conn.commit()


def seed_phone_config() -> None:
    """Seed audio/phone settings + the built-in audio providers.

    Idempotent: settings are only written for keys absent from DB; providers
    insert with ON CONFLICT DO NOTHING.
    """
    existing = task_store.get_all_platform_settings()
    seeded_settings = 0
    for key, default in _DEFAULT_SETTINGS.items():
        if key not in existing:
            # Legacy env override: strip the audio_/phone_ prefix, uppercase
            # (e.g. audio_vad_threshold → VAD_THRESHOLD).
            env_key = key.removeprefix("audio_").removeprefix("phone_").upper()
            env_val = os.environ.get(env_key, "")
            value = env_val if env_val else default
            task_store.set_platform_setting(key, value)
            seeded_settings += 1

    if seeded_settings:
        logger.info("Seeded %s audio/phone settings", seeded_settings)

    _seed_audio_providers()
