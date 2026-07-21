"""One-time seed: populate audio + phone settings and the built-in providers.

Runs at proxy startup. Settings are written only for keys missing from
platform_settings; audio providers seed idempotently (ON CONFLICT).

Phone routes are NOT seeded here — a route requires a phone_server FK, which
is provisioned via the admin UI + adapter. The platform ships with zero
routes until then.
"""

import json
import logging
import os
from datetime import datetime, timezone

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
    # Chat audio policy (chat sound/mic icons; consumed by the resolver)
    "audio_chat_user_policy": "native_preferred",
    "audio_show_experimental": "false",
    # Chat sound/mic icons — admin kill-switch (default on). Icon visibility
    # is availability-driven: the capability resolver hides the icons on its
    # own when no usable speech path (native or provider) exists.
    "audio_chat_enabled": "true",
    # Chat STT defaults (per-VAD/limits for the WS session)
    "audio_chat_stt_max_seconds": "60",
    "audio_tts_max_chars_per_request": "5000",
    "audio_tts_rate_limit_chars_per_min": "10000",
    "audio_transcribe_max_upload_mb": "100",
    "audio_transcribe_max_duration_min": "60",
    # Barge-in (production-tuned) — phone
    "phone_bargein_threshold": "0.35",
    "phone_bargein_debounce_ms": "300",
    "phone_bargein_chunk_ratio": "0.5",
    "phone_bargein_silence_duration_ms": "500",
    "phone_bargein_timer_s": "0.6",
    # Turn timing (production-tuned) — phone
    "phone_turn_complete_timeout_s": "1.0",
    "phone_turn_incomplete_timeout_s": "2.0",
    "phone_turn_classifier_grace_s": "0.0",
    "phone_turn_classifier_lang_map": "",
    "phone_turn_classifier_default_backend": "smart_turn",
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
    # Call context prompts (seeded with the defaults from the phone adapter)
    "phone_context_inbound": (
        "You are on a live phone call. This is voice — not chat.\n"
        "RULES:\n"
        "- ALWAYS respond in the SAME LANGUAGE the user speaks. Greek → Greek. English → English. Never switch language mid-call.\n"
        "- Keep responses SHORT: 1-3 sentences maximum. Summarize instead of listing details.\n"
        "- Talk naturally like a real person on the phone — use casual, conversational language.\n"
        "- Avoid stiff/formal phrasing. Say things the way you'd say them out loud.\n"
        "- NEVER read tables, lists, JSON, code, or formatted output aloud. Describe results naturally in plain speech.\n"
        "- If there are many items (services, devices, etc.), give a high-level summary, NOT individual details.\n"
        "- Don't spell out URLs, paths, or IPs unless asked.\n"
        "- When using tools: say 'One moment' first — in the SAME language as the call, like everything else — then summarize the result in 1-2 short sentences.\n"
        "- To end the call (e.g. user says goodbye or the conversation is clearly over), append [CALL_COMPLETE] at the end of your final message. The system will strip it before speaking.\n"
    ),
    "phone_context_outbound": (
        "You are on a live phone call that YOU placed to complete a task. This is voice — not chat.\n"
        "RULES:\n"
        "- ALWAYS respond in the SAME LANGUAGE the other person speaks. Greek → Greek. English → English. Never switch language mid-call.\n"
        "- Keep responses SHORT: 1-3 sentences maximum. Summarize instead of listing details.\n"
        "- Talk naturally like a real person on the phone — use casual, conversational language.\n"
        "- Avoid stiff/formal phrasing. Say things the way you'd say them out loud.\n"
        "- NEVER read tables, lists, JSON, code, or formatted output aloud. Describe results naturally in plain speech.\n"
        "- If there are many items (services, devices, etc.), give a high-level summary, NOT individual details.\n"
        "- Don't spell out URLs, paths, or IPs unless asked.\n"
        "- When using tools: say 'One moment' first — in the SAME language as the call, like everything else — then summarize the result in 1-2 short sentences.\n"
        "- Complete the task you were given, politely and professionally.\n"
        "- If you need information from your manager during the call, emit [QUESTION: your question here] in your response. "
        "The system will relay the question while the call stays active.\n"
        "- When the task is complete or clearly cannot be completed, end your final message with [CALL_COMPLETE].\n"
        "- The [CALL_COMPLETE] and [QUESTION:] markers are stripped before speaking — they're signals for the system.\n"
    ),
    # Per-language JSON blobs — phone
    "phone_phrases": json.dumps({
        "en": {
            "hold_message": "One moment please, let me check.",
            "greeting_fallback": "Hello, how can I help you?",
            "turn_classifier": "smart_turn",
        },
        "el": {
            "hold_message": "Μια στιγμή παρακαλώ, θα το ελέγξω.",
            "greeting_fallback": "Γεια σας, πώς μπορώ να σας βοηθήσω;",
            "turn_classifier": "groq",
        },
        "de": {
            "hold_message": "Einen Moment bitte, ich schaue nach.",
            "greeting_fallback": "Hallo, wie kann ich Ihnen helfen?",
            "turn_classifier": "smart_turn",
        },
        "es": {
            "hold_message": "Un momento por favor, déjeme comprobar.",
            "greeting_fallback": "Hola, ¿en qué puedo ayudarle?",
            "turn_classifier": "smart_turn",
        },
        "fr": {
            "hold_message": "Un instant s'il vous plaît, je vérifie.",
            "greeting_fallback": "Bonjour, comment puis-je vous aider ?",
            "turn_classifier": "smart_turn",
        },
        "it": {
            "hold_message": "Un momento per favore, controllo subito.",
            "greeting_fallback": "Salve, come posso aiutarla?",
            "turn_classifier": "smart_turn",
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
# config push reads. Fields: (type, name, label, credential_key, advanced, voices).
_DEFAULT_PROVIDERS = [
    ("stt", "deepgram", "Deepgram", "audio-deepgram",
     json.dumps({"call_endpointing_ms": 500, "chat_endpointing_ms": 1500}),
     json.dumps({})),
    ("tts", "cartesia", "Cartesia", "audio-cartesia",
     json.dumps({}),
     json.dumps({
         "en": "79f8b5fb-2cc8-479a-80df-29f7a7cf1a3e",
         "el": "50849023-76e9-46c7-af52-9ec39888a165",
     })),
]


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
        logger.info(f"Seeded {seeded_settings} audio/phone settings")

    _seed_audio_providers()
