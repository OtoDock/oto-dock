"""Runtime configuration manager for the phone server.

Replaces static config.py as the runtime config source. Populated from
the proxy's management WebSocket and updated live when settings change
in the dashboard.

All phone server modules read from a shared ConfigManager instance
instead of importing config.py settings.
"""

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("config_manager")

_DID_DIGITS_RE = re.compile(r"\D")


def normalize_did(did: str) -> str:
    """Canonicalize a dialed/configured number for exact-match lookups:
    digits only. Twilio delivers ``+`` E.164 while admins type numbers with
    formatting — dropping the ``+`` and every separator lets ``+1555…``
    match ``1 (555) …``. A number typed WITHOUT its country code still
    cannot match (inherently ambiguous); the provisioning instructions
    surface the canonical E.164 for exactly that reason.
    """
    return _DID_DIGITS_RE.sub("", did or "")



# In-code phrase defaults, per-key-merged under the admin's seeded/custom
# ``phrases`` JSON (see ``ConfigManager.voice_phrases``). The ``pin_*`` set
# covers every seeded language so the PIN gate speaks natively on existing
# installs whose seeded JSON predates these keys.
_DEFAULT_PHRASES: dict[str, dict[str, str]] = {
    "en": {
        "hold_message": "One moment please, let me check.",
        "greeting_fallback": "Hello, how can I help you?",
        "pin_prompt": "This line is protected. Please enter your access code on the keypad, then press the pound key.",
        "pin_retry": "That code wasn't right. Please try again.",
        "pin_fail_goodbye": "Sorry, that code isn't right. Goodbye.",
        "pin_lockout": "Too many failed attempts. Please try again later. Goodbye.",
    },
    "el": {
        "hold_message": "Μια στιγμή παρακαλώ, θα το ελέγξω.",
        "greeting_fallback": "Γεια σας, πώς μπορώ να σας βοηθήσω;",
        "pin_prompt": "Αυτή η γραμμή προστατεύεται. Παρακαλώ πληκτρολογήστε τον κωδικό πρόσβασης και μετά το πλήκτρο της δίεσης.",
        "pin_retry": "Ο κωδικός δεν ήταν σωστός. Παρακαλώ δοκιμάστε ξανά.",
        "pin_fail_goodbye": "Λυπάμαι, ο κωδικός δεν είναι σωστός. Αντίο σας.",
        "pin_lockout": "Πολλές αποτυχημένες προσπάθειες. Παρακαλώ δοκιμάστε ξανά αργότερα. Αντίο σας.",
    },
    "de": {
        "pin_prompt": "Diese Leitung ist geschützt. Bitte geben Sie Ihren Zugangscode über die Tastatur ein und drücken Sie dann die Rautetaste.",
        "pin_retry": "Der Code war nicht richtig. Bitte versuchen Sie es erneut.",
        "pin_fail_goodbye": "Der Code ist leider nicht richtig. Auf Wiederhören.",
        "pin_lockout": "Zu viele Fehlversuche. Bitte versuchen Sie es später erneut. Auf Wiederhören.",
    },
    "es": {
        "pin_prompt": "Esta línea está protegida. Introduzca su código de acceso en el teclado y pulse la tecla de almohadilla.",
        "pin_retry": "El código no es correcto. Inténtelo de nuevo.",
        "pin_fail_goodbye": "Lo siento, el código no es correcto. Adiós.",
        "pin_lockout": "Demasiados intentos fallidos. Inténtelo de nuevo más tarde. Adiós.",
    },
    "fr": {
        "pin_prompt": "Cette ligne est protégée. Veuillez saisir votre code d'accès sur le clavier, puis appuyez sur la touche dièse.",
        "pin_retry": "Ce code n'est pas correct. Veuillez réessayer.",
        "pin_fail_goodbye": "Désolé, ce code n'est pas correct. Au revoir.",
        "pin_lockout": "Trop de tentatives échouées. Veuillez réessayer plus tard. Au revoir.",
    },
    "it": {
        "pin_prompt": "Questa linea è protetta. Inserisca il suo codice di accesso sulla tastiera, poi prema il tasto cancelletto.",
        "pin_retry": "Il codice non è corretto. Riprovi per favore.",
        "pin_fail_goodbye": "Mi dispiace, il codice non è corretto. Arrivederci.",
        "pin_lockout": "Troppi tentativi falliti. Riprovi più tardi. Arrivederci.",
    },
}

@dataclass
class PhoneRoute:
    """Route configuration for a phone call."""
    id: str
    direction: str        # "inbound" | "outbound"
    agent: str            # Proxy agent name (e.g., "unified", "caller")
    llm_mode: str         # "proxy" | "direct"
    greeting: str         # TTS greeting text (empty = use default)
    language: str         # Language code ("en", "el")
    phone_context_override: str  # Extra call context (DB column name preserved)
    enabled: bool
    # Outbound-specific
    ami_caller_id: str
    ami_outbound_context: str
    dial_prefix: str          # prepended to the dialed number → selects the SIP line
    # Per-route audio overrides. NULL provider id → the call default; the
    # filler modes are plain per-route toggles ('on'/'off', default on).
    stt_provider_id: int | None = None
    tts_provider_id: int | None = None
    backchannel_mode: str = "on"
    thinking_filler_mode: str = "on"
    # Background ambience bed ('off' | template id — see phone/ambience.py)
    background_sound: str = "off"
    # Provisioning identity (Twilio media transport routes calls by these;
    # AudioSocket routes ignore them).
    did: str = ""
    phone_server_id: int | None = None
    # Inbound PIN gate. Plaintext digits delivered by the config push
    # (encrypted at rest proxy-side); repr=False so a stray log of the route
    # object can never leak it. Empty = gate off.
    pin: str = field(default="", repr=False)


@dataclass
class RouteSettings:
    """Per-route resolved audio settings (provider rows + filler toggles).

    Built by ``ConfigManager.resolve_route_settings`` from the route's
    overrides, falling back to the call-default providers. ``stt_provider`` /
    ``tts_provider`` are provider-map entries ({provider_name, voices, advanced,
    api_key}) or None when nothing is configured.
    """
    stt_provider: dict | None
    tts_provider: dict | None
    backchannel_enabled: bool
    thinking_filler_enabled: bool


class ConfigManager:
    """Live configuration source for the phone server.

    Receives full config JSON from the proxy via management WebSocket.
    All settings are read from self._data which is atomically replaced
    on each config push.
    """

    def __init__(self):
        self._data: dict = {}
        self._routes_by_uuid: dict[str, PhoneRoute] = {}
        self._routes_by_server_did: dict[tuple[int, str], PhoneRoute] = {}
        self._routes_outbound: dict[str, PhoneRoute] = {}
        self._default_outbound: PhoneRoute | None = None
        # Provider map (id → {provider_name, voices, advanced, api_key}) + the
        # call-default ids, pushed by the proxy so routes resolve their STT/TTS.
        self._providers: dict[str, dict] = {}
        self._default_stt_provider_id = None
        self._default_tts_provider_id = None
        # Per-server map (id → {adapter_type, twilio creds / AMI coords}).
        self._servers: dict[str, dict] = {}

    def load(self, config_data: dict) -> None:
        """Atomically replace config. Called on WS push."""
        self._data = config_data
        self._providers = config_data.get("providers", {}) or {}
        self._default_stt_provider_id = config_data.get("default_stt_provider_id")
        self._default_tts_provider_id = config_data.get("default_tts_provider_id")
        self._servers = config_data.get("servers", {}) or {}
        self._rebuild_route_index()
        version = config_data.get("version", "?")
        logger.info(f"Config loaded (version={version}, {len(self._routes_by_uuid)} inbound, {len(self._routes_outbound)} outbound)")

    def _rebuild_route_index(self) -> None:
        """Rebuild route lookup dicts from config data."""
        self._routes_by_uuid.clear()
        self._routes_by_server_did.clear()
        self._routes_outbound.clear()
        self._default_outbound = None

        for r in self._data.get("routes", []):
            if not r.get("enabled", True):
                continue
            route = PhoneRoute(
                id=r.get("id", ""),
                direction=r.get("direction", "inbound"),
                agent=r.get("agent", "unified"),
                llm_mode=r.get("llm_mode", "proxy"),
                greeting=r.get("greeting", ""),
                language=r.get("language", "en"),
                phone_context_override=r.get("phone_context_override", ""),
                enabled=r.get("enabled", True),
                ami_caller_id=r.get("ami_caller_id", ""),
                ami_outbound_context=r.get("ami_outbound_context", ""),
                dial_prefix=r.get("dial_prefix", ""),
                stt_provider_id=r.get("stt_provider_id"),
                tts_provider_id=r.get("tts_provider_id"),
                backchannel_mode=r.get("backchannel_mode", "on"),
                thinking_filler_mode=r.get("thinking_filler_mode", "on"),
                background_sound=r.get("background_sound", "off") or "off",
                did=r.get("did") or "",
                phone_server_id=r.get("phone_server_id"),
                pin=r.get("pin") or "",
            )
            if route.direction == "inbound":
                uuid = r.get("audiosocket_uuid")
                if uuid:
                    self._routes_by_uuid[uuid] = route
                did = normalize_did(route.did)
                if did and route.phone_server_id is not None:
                    self._routes_by_server_did[(int(route.phone_server_id), did)] = route
            else:
                self._routes_outbound[route.id] = route
                if self._default_outbound is None:
                    self._default_outbound = route

    # -----------------------------------------------------------------------
    # Route lookup
    # -----------------------------------------------------------------------

    def resolve_inbound_route(self, uuid: str) -> PhoneRoute | None:
        """Look up an inbound route by AudioSocket UUID."""
        return self._routes_by_uuid.get(uuid)

    def resolve_inbound_route_by_did(self, server_id, did: str) -> PhoneRoute | None:
        """Look up an inbound route by (phone server, dialed number) — the
        Twilio webhook path. Both sides are ``normalize_did``-canonical."""
        try:
            key = (int(server_id), normalize_did(did))
        except (TypeError, ValueError):
            return None
        return self._routes_by_server_did.get(key)

    # -----------------------------------------------------------------------
    # Per-server config (Twilio credentials, AMI coordinates)
    # -----------------------------------------------------------------------

    def twilio_server(self, server_id) -> dict | None:
        """The pushed Twilio config for a server ({account_sid, auth_token,
        public_base_url}), or None when the server isn't a usable Twilio row.
        Map keys are strings after the JSON push — normalize like
        ``get_provider``."""
        if server_id is None or server_id == "":
            return None
        entry = self._servers.get(str(server_id))
        if not entry or entry.get("adapter_type") != "twilio":
            return None
        if not (entry.get("account_sid") and entry.get("auth_token")):
            return None
        return entry

    def server_ami(self, server_id) -> dict | None:
        """Per-route AMI coordinates from the ``servers`` map (mixed-provider
        installs), or None → caller falls back to the flat default-server
        settings (single-Asterisk compat)."""
        if server_id is None or server_id == "":
            return None
        entry = self._servers.get(str(server_id)) or {}
        if not entry.get("ami_host"):
            return None
        return {
            "host": str(entry.get("ami_host", "")),
            "port": int(entry.get("ami_port") or 5038),
            "username": str(entry.get("ami_username", "")),
            "secret": str(entry.get("ami_secret", "")),
        }

    def ami_event_servers(self) -> dict[str, dict]:
        """Servers eligible for a persistent AMI event listener.

        Stricter than ``server_ami``: the proxy mirrors ``ami_host`` from
        the server row's plain host for every non-Twilio server, so a
        listener requires full credentials (username AND secret) — anything
        less would loop failed logins against the PBX (fail2ban bait).
        """
        eligible: dict[str, dict] = {}
        for key, entry in self._servers.items():
            host = str(entry.get("ami_host") or "")
            username = str(entry.get("ami_username") or "")
            secret = str(entry.get("ami_secret") or "")
            if not (host and username and secret):
                continue
            eligible[str(key)] = {
                "host": host,
                "port": int(entry.get("ami_port") or 5038),
                "username": username,
                "secret": secret,
            }
        return eligible

    def get_outbound_route(self, route_id: str) -> PhoneRoute | None:
        """Get a specific outbound route by ID."""
        return self._routes_outbound.get(route_id)

    def get_default_outbound_route(self) -> PhoneRoute | None:
        """Get the first enabled outbound route (default)."""
        return self._default_outbound

    def enabled_routes(self) -> list[PhoneRoute]:
        """All enabled routes (inbound + outbound) — used by the filler pre-warm.

        Both index dicts hold only enabled routes (``_rebuild_route_index`` skips
        disabled ones); inbound routes without a UUID can't receive calls so are
        absent here too.
        """
        return list(self._routes_by_uuid.values()) + list(self._routes_outbound.values())

    # -----------------------------------------------------------------------
    # Per-route provider + toggle resolution
    # -----------------------------------------------------------------------

    def get_provider(self, provider_id) -> dict | None:
        """Provider-map entry ({provider_name, voices, advanced, api_key}) by id.

        Accepts int or str ids (the map is keyed by str); None/empty → None.
        """
        if provider_id is None or provider_id == "":
            return None
        return self._providers.get(str(provider_id))

    def resolve_route_settings(self, route: PhoneRoute) -> RouteSettings:
        """Resolve a route's effective STT/TTS providers + filler toggles.

        Provider: the route's override id, else the call default. Filler
        toggles are per-route on/off (anything but an explicit 'off' plays,
        so a stale value degrades to the default-on behavior).
        """
        stt = self.get_provider(route.stt_provider_id) or self.get_provider(self._default_stt_provider_id)
        tts = self.get_provider(route.tts_provider_id) or self.get_provider(self._default_tts_provider_id)
        return RouteSettings(
            stt_provider=stt,
            tts_provider=tts,
            backchannel_enabled=route.backchannel_mode != "off",
            thinking_filler_enabled=route.thinking_filler_mode != "off",
        )

    # -----------------------------------------------------------------------
    # Settings accessors (all read from self._data["settings"])
    # -----------------------------------------------------------------------

    def _s(self, key: str, default: str = "") -> str:
        return self._data.get("settings", {}).get(key, default)

    # VAD
    @property
    def vad_threshold(self) -> float:
        return float(self._s("vad_threshold", "0.40"))

    @property
    def vad_silence_duration_ms(self) -> int:
        return int(self._s("vad_silence_duration_ms", "350"))

    @property
    def vad_speech_pad_ms(self) -> int:
        return int(self._s("vad_speech_pad_ms", "64"))

    @property
    def vad_min_energy_rms(self) -> float:
        return float(self._s("vad_min_energy_rms", "150"))

    @property
    def vad_silence_offset_ms(self) -> int:
        return int(self._s("vad_silence_offset_ms", "50"))

    # Barge-in
    @property
    def bargein_threshold(self) -> float:
        return float(self._s("bargein_threshold", "0.35"))

    @property
    def bargein_debounce_ms(self) -> int:
        return int(self._s("bargein_debounce_ms", "300"))

    @property
    def bargein_chunk_ratio(self) -> float:
        return float(self._s("bargein_chunk_ratio", "0.5"))

    @property
    def bargein_silence_duration_ms(self) -> int:
        return int(self._s("bargein_silence_duration_ms", "500"))

    @property
    def bargein_timer_s(self) -> float:
        # Minimum speech-EPISODE duration for a barge-in commit (operator
        # decision 2026-08-24: revived from the pre-pause design, where it
        # was the abort timer). Composes with the transcript requirement:
        # commit = episode >= this AND a non-empty final. Shorter episodes
        # ("ok", "ναι", a cough) are ignored entirely and playback resumes
        # — the language-independent wasteful-word filter.
        return float(self._s("bargein_timer_s", "0.6"))

    @property
    def bargein_confirm_timeout_s(self) -> float:
        # Backstop for a barge-in pause that never resolves (stuck VAD, no
        # transcript): resume playback after this long. In-code default is
        # the operative mechanism on upgraded installs (seeds never re-run).
        return float(self._s("bargein_confirm_timeout_s", "3.0"))

    @property
    def bargein_resume_grace_s(self) -> float:
        # After SPEECH_END on a paused playback: how long the STT final gets
        # to arrive before playback resumes where it froze.
        return float(self._s("bargein_resume_grace_s", "1.0"))

    # Turn timing
    @property
    def turn_complete_timeout_s(self) -> float:
        return float(self._s("turn_complete_timeout_s", "1.0"))

    @property
    def turn_incomplete_timeout_s(self) -> float:
        return float(self._s("turn_incomplete_timeout_s", "2.0"))

    @property
    def turn_classifier_grace_s(self) -> float:
        return float(self._s("turn_classifier_grace_s", "0.0"))

    @property
    def turn_classifier_default_backend(self) -> str:
        # groq since 2026-08-25 (operator A/B: better completeness verdicts
        # in every tested language); the dispatcher falls back to
        # smart_turn automatically when Groq isn't configured.
        return self._s("turn_classifier_default_backend", "groq")

    # Smart Turn
    @property
    def smart_turn_enabled(self) -> bool:
        return self._s("smart_turn_enabled", "false").lower() in ("true", "1", "yes")

    @property
    def smart_turn_threshold(self) -> float:
        return float(self._s("smart_turn_threshold", "0.65"))

    @property
    def smart_turn_onnx_threads(self) -> int:
        return int(self._s("smart_turn_onnx_threads", "1"))

    @property
    def smart_turn_audio_window_s(self) -> float:
        return float(self._s("smart_turn_audio_window_s", "8.0"))

    # Fillers (enable/disable is per-route — see resolve_route_settings)
    @property
    def backchannel_min_segments(self) -> int:
        return int(self._s("backchannel_min_segments", "1"))

    @property
    def backchannel_min_gap_s(self) -> float:
        return float(self._s("backchannel_min_gap_s", "0.4"))

    @property
    def thinking_filler_delay_s(self) -> float:
        return float(self._s("thinking_filler_delay_s", "0.5"))

    @property
    def thinking_filler_repeat_delay_s(self) -> float:
        """Raised delay when a filler already played in the previous turn —
        slow multi-turn stretches shouldn't chirp on every single turn."""
        return float(self._s("thinking_filler_repeat_delay_s", "2.0"))

    # Background ambience
    @property
    def background_sound_gain(self) -> float:
        return float(self._s("background_sound_gain", "0.17"))

    @property
    def voice_texture(self) -> float:
        """Grain + early-reflection amount gluing the voice into the bed
        (0-1, 0 = off; active only on routes with ambience)."""
        return float(self._s("voice_texture", "0.4"))

    # Pre-response breath
    @property
    def breath_enabled(self) -> bool:
        return self._s("breath_enabled", "true").lower() in ("true", "1", "yes")

    @property
    def breath_gain(self) -> float:
        return float(self._s("breath_gain", "0.27"))

    @property
    def breath_min_gap_s(self) -> float:
        """Minimum agent-voice silence before a response gets an inhale —
        a breath mid rapid exchange reads as uncanny."""
        return float(self._s("breath_min_gap_s", "1.5"))

    # TTS
    @property
    def tts_buffer_chars(self) -> int:
        return int(self._s("tts_buffer_chars", "20"))

    @property
    def tts_response_gap_s(self) -> float:
        return float(self._s("tts_response_gap_s", "0.4"))

    @property
    def tts_prebuffer_ms(self) -> int:
        """Audio banked before a TTS segment's first frame goes out (0 = off).
        The absolute pacing schedule carries the offset forward, so the whole
        segment gains that much chunk-arrival slack — a jitter lead at a
        bounded one-off first-audio cost."""
        return int(self._s("tts_prebuffer_ms", "120"))

    @property
    def tts_hold_max_s(self) -> float:
        """Max seconds a READY reply is held back by VAD "user speaking" that
        produces no transcript evidence (phantom/line noise). Past this the
        reply plays anyway — barge-in still cuts it if the speech is real."""
        return float(self._s("tts_hold_max_s", "4.0"))

    @property
    def deepgram_endpointing_ms(self) -> int:
        return int(self._s("deepgram_endpointing_ms", "500"))

    # Timeouts
    @property
    def idle_timeout_s(self) -> int:
        return int(self._s("idle_timeout_s", "30"))

    @property
    def call_max_duration_s(self) -> int:
        return int(self._s("call_max_duration_s", "600"))

    @property
    def outbound_call_timeout_s(self) -> int:
        return int(self._s("outbound_call_timeout_s", "300"))

    @property
    def question_answer_timeout_s(self) -> int:
        return int(self._s("question_answer_timeout_s", "40"))

    @property
    def stt_mute_reconnect_s(self) -> float:
        """Caller-speech seconds with ZERO provider results before the STT
        guard treats the session as mute and reconnects (failure mode #3 —
        socket open, sends flowing, nothing back; live-hit 2026-08-14).
        Worst false-positive cost: one latched ~300 ms reconnect per window
        of continuous noise-as-speech."""
        return float(self._s("stt_mute_reconnect_s", "7"))

    # Inbound PIN gate (attempt/cooldown limits are design-fixed in
    # calls/pin_failures.py; only the timing envelope is tunable).
    @property
    def pin_entry_timeout_s(self) -> float:
        """Whole-gate budget: prompts + all attempts. 60 s is reachable by a
        legitimate slow caller across 3 attempts — default 90."""
        return float(self._s("pin_entry_timeout_s", "90"))

    @property
    def pin_interdigit_timeout_s(self) -> float:
        return float(self._s("pin_interdigit_timeout_s", "8"))

    @property
    def pin_max_attempts(self) -> int:
        return int(self._s("pin_max_attempts", "3"))

    # Infrastructure
    @property
    def audiosocket_host(self) -> str:
        return self._s("audiosocket_host", "0.0.0.0")

    @property
    def audiosocket_port(self) -> int:
        return int(self._s("audiosocket_port", "9092"))

    @property
    def http_api_port(self) -> int:
        return int(self._s("http_api_port", "9093"))

    @property
    def ami_host(self) -> str:
        return self._s("ami_host", "")

    @property
    def ami_port(self) -> int:
        return int(self._s("ami_port", "5038"))

    @property
    def ami_username(self) -> str:
        return self._s("ami_username", "")

    @property
    def ami_dtmf_listener(self) -> str:
        """Kill switch for the persistent AMI DTMF event listeners
        (``"off"`` disables; absent/anything else = on). Seeded proxy-side
        as ``phone_ami_dtmf_listener`` so it is visible in the settings UI."""
        return self._s("ami_dtmf_listener", "on")

    @property
    def log_level(self) -> str:
        return self._s("log_level", "INFO")

    # Limits + security (defaults chosen well above normal load — they only
    # fire under a flood, so legitimate traffic is unaffected)
    @property
    def http_api_host(self) -> str:
        return self._s("http_api_host", "0.0.0.0")

    @property
    def max_live_calls(self) -> int:
        return int(self._s("max_live_calls", "50"))

    @property
    def max_outbound_calls(self) -> int:
        return int(self._s("max_outbound_calls", "20"))

    @property
    def rate_limit_calls_per_min(self) -> int:
        return int(self._s("rate_limit_calls_per_min", "60"))

    @property
    def rate_limit_register_per_min(self) -> int:
        return int(self._s("rate_limit_register_per_min", "300"))

    @property
    def rate_limit_twilio_per_min(self) -> int:
        """Shared budget for the Twilio webhook + status callbacks. The media
        WS upgrade is deliberately NOT rate-limited (signature-authenticated
        and bounded by max_live_calls) — a callback burst must never 429 a
        live call's media socket."""
        return int(self._s("rate_limit_twilio_per_min", "300"))

    # -----------------------------------------------------------------------
    # Credentials (from self._data["credentials"])
    # -----------------------------------------------------------------------

    @property
    def groq_api_key(self) -> str:
        return self._data.get("credentials", {}).get("groq_api_key", "")

    @property
    def groq_base_url(self) -> str:
        # Empty → classifier uses Groq directly (BYO key). Set to the OtoDock
        # relay base when the turn classifier runs through the hosted relay.
        return self._data.get("credentials", {}).get("groq_base_url", "")

    @property
    def ami_secret(self) -> str:
        return self._data.get("credentials", {}).get("ami_secret", "")

    @property
    def register_secrets(self) -> list[str]:
        """Per-server secrets that authenticate ``POST /v1/calls/register`` — one
        per phone server, pushed by the proxy. The daemon accepts ANY of them (it
        fronts all servers); a deleted server's secret drops from the list →
        revoked. Defaults to ``[]`` so an empty/absent value authorizes nothing
        (fail-closed). The ``isinstance`` guard keeps a malformed scalar from
        being iterated character-by-character."""
        secrets = self._data.get("credentials", {}).get("register_secrets", [])
        return secrets if isinstance(secrets, list) else []

    # -----------------------------------------------------------------------
    # Per-language data (JSON-parsed from settings)
    # -----------------------------------------------------------------------

    @property
    def voice_phrases(self) -> dict[str, dict[str, str]]:
        """Language → phrase map (hold_message, greeting_fallback, pin_*).

        Per-KEY merge of the in-code defaults with the admin's ``phrases``
        JSON (admin wins). The merge matters: the proxy seeds ``phrases`` on
        every install's first boot, so a replace-wholesale read would mean
        keys added in later releases (the ``pin_*`` set) never exist on
        existing installs — their seeded JSON predates the keys and
        installs never re-seed.
        """
        merged = {lang: dict(data) for lang, data in _DEFAULT_PHRASES.items()}
        raw = self._s("phrases", "")
        if raw:
            try:
                configured = json.loads(raw)
            except json.JSONDecodeError:
                configured = {}
            for lang, data in configured.items():
                if isinstance(data, dict):
                    merged.setdefault(lang, {}).update(data)
                else:
                    merged[lang] = data
        return merged

    def pin_phrase(self, language: str, key: str) -> str:
        """PIN-gate phrase with an explicit fallback chain: route language
        → en → hardcoded en constant (a gated route must never go
        mute because its language has no ``pin_*`` translations)."""
        phrases = self.voice_phrases
        for lang in (language, "en"):
            entry = phrases.get(lang)
            if isinstance(entry, dict) and entry.get(key):
                return entry[key]
        return _DEFAULT_PHRASES["en"][key]

    @property
    def backchannel_phrases(self) -> dict[str, list[str]]:
        raw = self._s("backchannel_phrases", "")
        if not raw:
            return {"el": ["\u03bd\u03b1\u03b9", "mmm", "mhm"], "en": ["mhm", "ok", "right", "mm-hmm", "uh-huh"]}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    @property
    def thinking_phrases(self) -> dict[str, list[str]]:
        """Language \u2192 merged thinking-filler phrase list."""
        raw = self._s("thinking_phrases", "")
        if not raw:
            return {
                "el": ["\u03b5\u03b5", "\u03bc\u03ac\u03bb\u03b9\u03c3\u03c4\u03b1", "\u03b3\u03b9\u03b1 \u03bd\u03b1 \u03b4\u03c9"],
                "en": ["hmm", "let me see", "one moment"],
            }
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    # -----------------------------------------------------------------------
    # Parsed maps (from string settings)
    # -----------------------------------------------------------------------

    @property
    def lang_backend_map(self) -> dict[str, str]:
        """Build language → classifier backend map from the phrases config.

        Each language in the phrases config can have a 'turn_classifier'
        field ('smart_turn' or 'groq'); languages without one fall back to
        the default backend at dispatch time.
        """
        phrases = self.voice_phrases
        result: dict[str, str] = {}
        for lang, data in phrases.items():
            if isinstance(data, dict) and data.get("turn_classifier"):
                result[lang] = data["turn_classifier"]
        return result
