"""Phone server configuration assembly and push.

Builds the full config JSON that the phone server needs from:
  - platform_settings (audio_* + phone_* keys)
  - phone_routes table
  - infra_credentials (encrypted API keys)

Broadcasts config updates to all connected phone servers via the
management WebSocket.
"""

import asyncio
import json
import logging
import secrets
import time

from core.background import spawn
from storage import database as task_store, pg
from storage.identity import credential_store
from storage.phone import phone_route_store, phone_server_store
from storage.prefs import audio_provider_store

logger = logging.getLogger("phone_config")

#: The classifier's relay mint on the async path: its own bound, and how
#: long a failure is remembered. Daemons connecting together would each wait
#: on a stalled relay; the config goes out without the classifier instead,
#: and one broadcast after the window tries again while a daemon is
#: connected.
MINT_TIMEOUT_S = 5.0
MINT_FAILURE_TTL_S = 60.0
_mint_inflight: asyncio.Task | None = None
_mint_failed_at: float = 0.0
_mint_retry: asyncio.Task | None = None

# One lock per loop around every assemble-and-send: a daemon keeps the last
# frame it got (no version check), so an older assembly must never land
# after a newer one. Created on the loop that uses it: a lock bound at
# import would belong to no loop.
_push_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


def push_lock() -> asyncio.Lock:
    """The lock the connect-time push, the capabilities answer, a
    ``request_config`` and the change broadcast all take around their
    assembly and send."""
    global _push_lock
    loop = asyncio.get_running_loop()
    if _push_lock is None or _push_lock[0] is not loop:
        _push_lock = (loop, asyncio.Lock())
    return _push_lock[1]

# Connected phone server management WebSocket clients.
# Populated by phone_management.py on connect/disconnect.
_management_clients: set = set()

# Per-connection capability metadata (ws → the daemon's ``capabilities`` frame
# payload, sans "type"). Pre-duplex daemons never send the frame → no entry.
# Dict insertion order is connection order, so the LAST duplex-capable entry is
# the newest daemon — v1's single-daemon pick; the duplex_open correlation ids
# keep a real multi-daemon router possible without protocol change.
_management_meta: dict = {}


def set_management_capabilities(ws, caps: dict) -> None:
    """Record a daemon's advertised capabilities (management ``capabilities``
    frame). Called by phone_management.py; cleared on disconnect."""
    _management_meta[ws] = caps or {}


def clear_management_capabilities(ws) -> None:
    _management_meta.pop(ws, None)


def pick_duplex_client():
    """Newest duplex-capable management connection, or None."""
    for ws in reversed(list(_management_meta)):
        caps = _management_meta.get(ws) or {}
        if (caps.get("duplex") or {}).get("supported"):
            return ws
    return None


#: The daemon gates an outbound call on a route's PIN before the agent
#: speaks (a released daemon gates inbound calls only): the API accepts a
#: PIN on an outbound route only while a connected daemon says so.
OUTBOUND_PIN_CAPABILITY = "outbound_pin"


def daemon_capability(name: str) -> bool:
    """A connected daemon advertised ``name`` in its capabilities frame
    (``call_control``, ``in_session_calls``, ``outbound_pin``): the proxy
    offers the matching behaviour only to a daemon that speaks it."""
    return any(bool((caps or {}).get(name)) for caps in _management_meta.values())


def duplex_engine_available() -> bool:
    """A connected phone daemon advertising duplex support. This is the
    engine half of the duplex capability gate — provider/policy checks live
    in ``audio_service.duplex_capability``."""
    return pick_duplex_client() is not None


def assemble_duplex_session_config(user_sub: str, browser_language: str = "") -> dict:
    """Per-session config for one duplex session (rides ``duplex_open``).

    Identity-and-provider scope ONLY: the chat-context STT/TTS provider rows
    with decrypted keys, the chat endpointing, and the user's dictation
    language. Behaviour knobs (``phone_tts_buffer_chars``, ``phone_bargein_*``,
    the turn-classifier language map + Groq credentials) ride the GLOBAL
    config push — one source per knob, never duplicated here.
    """
    from storage.prefs import user_audio_prefs_store

    stt = audio_provider_store.get_default_provider("stt", "chat") or {}
    tts = audio_provider_store.get_default_provider("tts", "chat") or {}
    adv = stt.get("advanced") or {}
    try:
        endpointing = int(adv.get("chat_endpointing_ms")
                          or adv.get("call_endpointing_ms") or 1500)
    except (TypeError, ValueError):
        endpointing = 1500
    prefs = user_audio_prefs_store.get_prefs(user_sub) or {}
    # The user's per-language voice picks (the dashboard voice picker,
    # ``tts_voice_map``) OVERLAY the provider row's voices map: the spoken
    # conversation must use the same voice the user hears from every other
    # chat TTS surface — without this the engine resolved the row/stock
    # voice, so fillers AND replies played a different voice than the one
    # the user chose (live-hit 2026-08-11). Only platform-source picks
    # apply (``native`` is a browser/device voice the engine can't use).
    voices = dict(tts.get("voices") or {})
    for lang, pref in (prefs.get("tts_voice_map") or {}).items():
        if (isinstance(pref, dict) and pref.get("source") == "platform"
                and pref.get("voiceId")):
            voices[str(lang).split("-", 1)[0].lower()] = pref["voiceId"]
    return {
        "stt": {
            "id": stt.get("id"),
            "provider_name": stt.get("provider_name", ""),
            "advanced": adv,
            "api_key": _provider_api_key(stt),
        },
        "tts": {
            "id": tts.get("id"),
            "provider_name": tts.get("provider_name", ""),
            "voices": voices,
            "advanced": tts.get("advanced") or {},
            "api_key": _provider_api_key(tts),
        },
        "chat_endpointing_ms": endpointing,
        # "auto" is the Automatic pick: the session starts in the browser's
        # language when the install lists it, else in the first listed one,
        # and reads the speaker's from the audio as it arrives.
        "language": (_detect_default(browser_language) if _is_auto(prefs.get("stt_language"))
                     else (prefs.get("stt_language") or "")),
        "language_detect": _is_auto(prefs.get("stt_language")),
    }


def _is_auto(tag) -> bool:
    from audio.streaming.lang import is_auto
    return is_auto(tag)


def _detect_default(tag: str) -> str:
    """The Automatic pick's starting language: the browser's when the
    install lists it, else the first listed (the admin's list bounds every
    outcome, the start included)."""
    from audio.streaming.lang import supported_base

    from services.media.audio_service import language_candidates
    allowed = language_candidates()
    code = supported_base(tag, allowed[0])
    return code if code in allowed else allowed[0]


def _provider_api_key(provider: dict | None) -> str:
    """Resolve a provider's API key from infra_credentials (uniform inner key)."""
    if not provider or not provider.get("credential_key"):
        return ""
    creds = credential_store.get_infra_credentials(provider["credential_key"])
    return creds.get(audio_provider_store.CREDENTIAL_INNER_KEY, "")


def direct_llm_groq_credentials() -> tuple[str, str]:
    """Resolve the call turn classifier's ``(api_key, base_url)`` from the Direct
    LLM execution layer's platform Groq subscription (where the admin configures
    it once). The classifier reuses it — there is no separate phone Groq credential.

    BYO-wins: a stored API key (the admin's own Groq key) is used directly against
    Groq (``base_url=""`` → the classifier's default Groq endpoint). Otherwise, if
    the platform Groq sub is hosted (``auth_type='relay'``), mint a **system**
    relay token (``user_sub=""``) and point the classifier at the OtoDock relay —
    reusing the SAME path the Direct-LLM execution layer uses
    (``subscription_pool.relay_llm_credentials``), so there is no duplicated mint
    logic. Returns ``("", "")`` when nothing is configured / the relay is
    unavailable (the dispatcher then falls back to Smart Turn).

    Note: minting here is cheap — ``mint_session_token`` is in-process TTL-cached,
    so config (re)assembly hits the relay at most once per cache window, never per
    call. The per-turn classifier traffic runs phone-daemon → relay, not via the
    proxy. Sync: the socket handlers use :func:`classifier_credentials`, which
    runs the same two halves off the loop.
    """
    source, key = _groq_source()
    if source == "key":
        return key, ""
    if source == "relay":
        return _mint_groq_relay()
    return "", ""


def _groq_source() -> tuple[str, str]:
    """The store's half: ``("key", <key>)`` for a stored BYO key (it beats
    the hosted relay: lower latency, no credits), ``("relay", "")`` for a
    credential-less hosted sub, ``("", "")`` for none."""
    from storage.billing import subscription_store

    # Pool view (contribute_platform + active + owner-is-admin); reading the store
    # directly here bypasses acquire_subscription, so list_platform_pool is what
    # applies the owner-is-admin guard.
    groq_subs = subscription_store.list_platform_pool(layer="direct-llm", provider="groq")
    for sub in groq_subs:
        key = subscription_store.get_credential_data(sub["id"]).get("api_key", "")
        if key:
            return "key", key
    if any(s.get("auth_type") == "relay" for s in groq_subs):
        return "relay", ""
    return "", ""


def _mint_groq_relay() -> tuple[str, str]:
    """The network half: a system relay token and the relay's Groq endpoint,
    ``("", "")`` when the relay is unavailable or refuses (logged there)."""
    from services.engines import subscription_pool
    creds = subscription_pool.relay_llm_credentials("direct-llm", "groq", "")
    return creds if creds else ("", "")


async def classifier_credentials() -> tuple[str, str]:
    """:func:`direct_llm_groq_credentials` off the loop: the store reads on
    the bulk DB lane, the relay mint in a thread of its own (never on the DB
    lane, which a stalled relay would starve), bounded by ``MINT_TIMEOUT_S``
    and shared by every caller in flight (ten daemons reconnecting in one
    second mint once); a failure is remembered ``MINT_FAILURE_TTL_S`` and
    the classifier stays unset meanwhile."""
    global _mint_inflight
    source, key = await pg.run_db(_groq_source)
    if source == "key":
        return key, ""
    if source != "relay":
        return "", ""
    if _mint_failed_at and time.monotonic() - _mint_failed_at < MINT_FAILURE_TTL_S:
        return "", ""
    if _mint_inflight is None or _mint_inflight.done():
        _mint_inflight = asyncio.create_task(_mint_bounded())
    # A caller cancelled meanwhile (its socket closed) leaves the mint to
    # finish for the others.
    return await asyncio.shield(_mint_inflight)


async def _mint_bounded() -> tuple[str, str]:
    global _mint_failed_at
    try:
        creds = await asyncio.wait_for(
            asyncio.to_thread(_mint_groq_relay), timeout=MINT_TIMEOUT_S)
    except asyncio.TimeoutError:
        creds = ("", "")
        logger.warning(
            "Phone config: the relay did not answer the turn classifier's mint "
            "in %.0f s; the config goes out without the classifier", MINT_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001  # the config must go out whatever the relay path raised
        creds = ("", "")
        logger.warning("Phone config: the turn classifier's mint failed (%s); "
                       "the config goes out without the classifier", e)
    if creds == ("", ""):
        _mint_failed_at = time.monotonic()
        _schedule_mint_retry()
    else:
        _mint_failed_at = 0.0
    return creds


def _schedule_mint_retry() -> None:
    """After a failed mint, one broadcast once the failure window has
    passed: the config is assembled again for the connected daemons, the
    mint tries again, a failure schedules the next retry and a success
    ends the chain. Nothing while no daemon is connected."""
    global _mint_retry
    if not _management_clients or (_mint_retry is not None and not _mint_retry.done()):
        return
    _mint_retry = spawn(_retry_classifier_push(), name="classifier-mint-retry",
                        owner="phone_config")


async def _retry_classifier_push() -> None:
    global _mint_retry
    await asyncio.sleep(MINT_FAILURE_TTL_S)
    # Released before the broadcast: a mint that fails inside it schedules
    # the next retry.
    _mint_retry = None
    if not _management_clients:
        return
    logger.info("Phone config: trying the turn classifier's mint again")
    await notify_phone_config_changed()


def forget_mint_failure() -> None:
    """Drop the remembered failure and a pending retry (tests)."""
    global _mint_failed_at, _mint_inflight, _mint_retry
    _mint_failed_at = 0.0
    _mint_inflight = None
    if _mint_retry is not None and not _mint_retry.done():
        _mint_retry.cancel()
    _mint_retry = None


def groq_classifier_configured() -> bool:
    """True if the turn classifier has a Groq path configured — a BYO key **or** a
    hosted relay sub — **without minting** a token. Backs the read-only admin
    "active" indicator (a GET shouldn't trigger a relay round-trip); whether a
    hosted call actually succeeds still depends on credits at call time.
    """
    from storage.billing import subscription_store
    for sub in subscription_store.list_platform_pool(layer="direct-llm", provider="groq"):
        if sub.get("auth_type") == "relay":
            return True
        if subscription_store.get_credential_data(sub["id"]).get("api_key", ""):
            return True
    return False


def ensure_register_secret(server_id: int) -> str:
    """Return a phone server's register secret, minting one if absent.

    The register secret authenticates that server's dialplan when it calls
    ``POST /v1/calls/register`` on the phone daemon (each server's bootstrap
    snippet embeds its own). Idempotent + race-safe (first-writer-wins via
    ``set_infra_credentials_if_absent``): it covers servers created before this
    feature with no migration, and the config-push and snippet-render paths
    converge on one value if they mint concurrently.
    """
    name = phone_server_store.register_cred_name(server_id)
    existing = credential_store.get_infra_credentials(name).get(
        phone_server_store.REGISTER_SECRET_KEY,
    )
    if existing:
        return existing
    minted = secrets.token_urlsafe(32)
    effective = credential_store.set_infra_credentials_if_absent(
        name, {phone_server_store.REGISTER_SECRET_KEY: minted},
    )
    return effective.get(phone_server_store.REGISTER_SECRET_KEY, minted)


def ensure_ami_user(server_id: int) -> tuple[str, str]:
    """Return ``(username, secret)`` for a server's AMI manager user, minting
    both if the admin never configured them.

    Drives the one-paste bootstrap: the generated manager-user snippet and the
    stored credentials come from the same mint, so Verify works without the
    admin typing anything back. Admin-set values always win — minting only
    fills gaps (username default ``otodock``; secret race-safe via
    ``set_infra_credentials_if_absent``, same pattern as the register secret).
    """
    server = phone_server_store.get_server(server_id) or {}
    cfg = dict(server.get("config") or {})
    username = (cfg.get("ami_username") or "").strip()
    if not username:
        username = "otodock"
        cfg["ami_username"] = username
        phone_server_store.update_server(server_id, {"config": cfg})
    name = phone_server_store.ami_cred_name(server_id)
    secret = credential_store.get_infra_credentials(name).get(
        phone_server_store.AMI_SECRET_KEY, "",
    )
    if not secret:
        minted = secrets.token_urlsafe(24)
        effective = credential_store.set_infra_credentials_if_absent(
            name, {phone_server_store.AMI_SECRET_KEY: minted},
        )
        secret = effective.get(phone_server_store.AMI_SECRET_KEY, minted)
    return username, secret


async def build_phone_config() -> dict:
    """:func:`assemble_phone_config` off the loop: the store reads on the
    bulk DB lane, the classifier's credentials through
    :func:`classifier_credentials`. The socket handlers and the change
    broadcast use this; a daemon's connect never holds the loop on the
    store or the relay."""
    data = await pg.run_db(assemble_phone_config, classifier=False)
    key, base = await classifier_credentials()
    data["credentials"]["groq_api_key"] = key
    data["credentials"]["groq_base_url"] = base
    return data


def assemble_phone_config(*, classifier: bool = True) -> dict:
    """Build the full config JSON for the phone server from DB state.

    Returns a dict with:
      - version: timestamp for change detection
      - routes: list of phone route dicts
      - credentials: decrypted API keys
      - settings: audio_* + phone_* platform settings (prefix stripped)

    The phone server's ``ConfigManager`` reads the prefix-stripped keys
    (``vad_threshold``, ``bargein_threshold``, …), so the audio/phone split in
    ``platform_settings`` is invisible to it. ``audio_*`` (shared with chat
    audio) is collected first; ``phone_*`` (call-only) overrides on any
    post-strip collision (e.g. ``log_level``).

    Provider rows travel in the ``providers`` map (per provider_id: voices,
    advanced, decrypted API key) so a route resolves its own STT/TTS. The only
    flat wire keys re-injected from the ``audio_providers`` / ``phone_servers``
    source-of-truth rows are the default-for-calls ids, the
    ``deepgram_endpointing_ms`` fallback, and the AMI server config (``ami_host``
    …).
    """
    all_settings = task_store.get_all_platform_settings()
    settings: dict[str, str] = {}
    for k, v in all_settings.items():
        if k.startswith("audio_"):
            settings[k.removeprefix("audio_")] = v
    for k, v in all_settings.items():
        if k.startswith("phone_"):
            settings[k.removeprefix("phone_")] = v

    # Phone routes. PINs ride the push in plaintext for local enforcement
    # — the standing rule for daemon secrets on the
    # master-key-authenticated management WS (Twilio auth token, AMI secret,
    # provider API keys below) — on the routes that may carry one (every
    # inbound route, an outbound route tied to a user): a PIN stranded on a
    # route the rules no longer allow one for gates nobody. The PIN never
    # exists as a route column, so the raw rows themselves stay clean.
    from services.phone.phone_identity import pin_allowed
    routes = phone_route_store.get_all_routes()
    for r in routes:
        if pin_allowed(r):
            pin = phone_route_store.get_route_pin(r["id"])
            if pin:
                r["pin"] = pin
        # Route identity / role / remember-callers are proxy-side session
        # policy (the warmup resolves them from the route id); the daemon has
        # no use for them and the tied user's sub must not travel.
        for proxy_only in ("identity_mode", "identity_user_sub", "role", "remember_callers"):
            r.pop(proxy_only, None)

    # Default-for-calls providers set the per-call default STT/TTS ids (below) +
    # the global endpointing fallback. Per-provider voices, advanced and API keys
    # travel in the `providers` map; a route with no override uses these ids.
    stt_default = audio_provider_store.get_default_provider("stt", "calls")
    tts_default = audio_provider_store.get_default_provider("tts", "calls")
    if stt_default:
        endpointing = (stt_default.get("advanced") or {}).get("call_endpointing_ms")
        if endpointing is not None:
            settings["deepgram_endpointing_ms"] = str(endpointing)

    # AMI from the default phone server (host/port/user in config JSONB; secret
    # encrypted in infra_credentials). Empty until a server is added.
    server = phone_server_store.get_default_server()
    ami_secret = ""
    if server:
        cfg = server.get("config") or {}
        # AMI host falls back to the server row's `host` (same rule as the
        # control-plane adapter + the "defaults to host" UI hint) so a blank
        # "AMI Host" field still works for the daemon's outbound Originate.
        ami_host = cfg.get("ami_host") or server.get("host") or ""
        if ami_host:
            settings["ami_host"] = str(ami_host)
        if cfg.get("ami_port"):
            settings["ami_port"] = str(cfg["ami_port"])
        if cfg.get("ami_username"):
            settings["ami_username"] = str(cfg["ami_username"])
        ami_secret = credential_store.get_infra_credentials(
            phone_server_store.ami_cred_name(server["id"]),
        ).get(phone_server_store.AMI_SECRET_KEY, "")

    # Per-server register secrets: every phone server has its own minted secret
    # that authenticates its dialplan's POST /v1/calls/register. Shipped as a
    # flat LIST the daemon accepts ANY of (one daemon fronts all servers); a
    # deleted server drops out of get_all_servers → its secret leaves the list →
    # revoked. MUST be a list, never a set — json.dumps can't serialize a set, so
    # the config push (notify_phone_config_changed) would raise.
    all_servers = phone_server_store.get_all_servers()
    register_secrets = sorted(
        ensure_register_secret(s["id"]) for s in all_servers
    )

    # Per-server map, keyed by str(id) (JSON object keys): Twilio rows carry
    # the media-plane credentials the daemon needs (signature validation,
    # TwiML URLs, REST origination); Asterisk rows carry their AMI
    # coordinates so a route on a NON-default server can still Originate
    # (the flat ami_* settings below stay the default-server fallback for
    # released daemons and single-server installs).
    servers: dict[str, dict] = {}
    for s in all_servers:
        s_cfg = s.get("config") or {}
        entry: dict = {"id": s["id"], "adapter_type": s.get("adapter_type", "")}
        if entry["adapter_type"] == "twilio":
            from services.phone.phone_adapters.twilio import (
                resolve_twilio_public_base,
            )
            entry["account_sid"] = str(s_cfg.get("account_sid") or "")
            # The RESOLVED base (always the dashboard public URL) — the
            # daemon validates signatures and builds TwiML against the same
            # base the adapter wrote into the numbers' VoiceUrls.
            entry["public_base_url"] = resolve_twilio_public_base()
            entry["auth_token"] = credential_store.get_infra_credentials(
                phone_server_store.twilio_cred_name(s["id"]),
            ).get(phone_server_store.TWILIO_AUTH_TOKEN_KEY, "")
        else:
            s_ami_host = s_cfg.get("ami_host") or s.get("host") or ""
            if s_ami_host:
                entry["ami_host"] = str(s_ami_host)
                entry["ami_port"] = int(s_cfg.get("ami_port") or 5038)
                entry["ami_username"] = str(s_cfg.get("ami_username") or "")
                entry["ami_secret"] = credential_store.get_infra_credentials(
                    phone_server_store.ami_cred_name(s["id"]),
                ).get(phone_server_store.AMI_SECRET_KEY, "")
        servers[str(s["id"])] = entry

    # Per-provider map (keyed by provider_id) so the phone server can resolve a
    # route's chosen STT/TTS provider: provider_name (→ registry class), the
    # per-language voices, advanced (endpointing / model_id), and the decrypted
    # API key. A route with NULL stt/tts_provider_id falls back to the
    # default-for-calls ids below. Only call-enabled providers are shipped.
    providers = {
        str(p["id"]): {
            "id": p["id"],
            "provider_type": p["provider_type"],
            "provider_name": p["provider_name"],
            "voices": p.get("voices") or {},
            "advanced": p.get("advanced") or {},
            "api_key": _provider_api_key(p),
        }
        for p in audio_provider_store.get_all_providers()
        if p.get("enabled_for_calls")
    }

    # Turn classifier Groq credentials (BYO key → Groq directly; hosted relay sub
    # → minted system token + relay base_url). See direct_llm_groq_credentials;
    # ``classifier=False`` leaves them to the async builder.
    groq_api_key, groq_base_url = direct_llm_groq_credentials() if classifier else ("", "")

    return {
        "version": int(time.time()),
        "routes": routes,
        "providers": providers,
        "servers": servers,
        "default_stt_provider_id": stt_default["id"] if stt_default else None,
        "default_tts_provider_id": tts_default["id"] if tts_default else None,
        "credentials": {
            "groq_api_key": groq_api_key,
            "groq_base_url": groq_base_url,
            "ami_secret": ami_secret,
            "register_secrets": register_secrets,
        },
        "settings": settings,
    }


def config_for_daemon(config_data: dict, caps: dict | None) -> dict:
    """The push as one daemon may read it: an outbound route's PIN goes only
    to a daemon that advertised ``outbound_pin``. An older daemon dials
    without the gate and must not hold a secret nobody asks for; the one
    that advertises the capability later gets the full push then."""
    from adapters.phone import is_outbound

    if (caps or {}).get(OUTBOUND_PIN_CAPABILITY):
        return config_data
    routes = config_data.get("routes") or []
    if not any(is_outbound(r.get("direction")) and r.get("pin") for r in routes):
        return config_data
    return {
        **config_data,
        "routes": [
            {k: v for k, v in r.items() if k != "pin"} if is_outbound(r.get("direction")) else r
            for r in routes
        ],
    }


def management_capabilities(ws) -> dict:
    """What a connected daemon advertised (``{}`` before its frame)."""
    return _management_meta.get(ws) or {}


async def notify_phone_config_changed() -> None:
    """Broadcast updated config to all connected phone servers.

    Called after any phone-related DB mutation (route CRUD,
    settings change, credential change). The assembly and the sends run
    under ``push_lock`` with the socket handler's own pushes.
    """
    async with push_lock():
        try:
            config_data = await build_phone_config()
        except Exception as e:  # noqa: BLE001  # a config that cannot be built is not pushed, the daemon keeps its own
            logger.error("Failed to assemble phone config: %s", e)
            return

        dead: list = []

        for ws in list(_management_clients):
            try:
                await ws.send_text(json.dumps({
                    "type": "config_update",
                    "data": config_for_daemon(config_data, management_capabilities(ws)),
                }))
            except Exception:  # noqa: BLE001  # the ASGI server's send raises by implementation once the socket is gone
                dead.append(ws)

        for ws in dead:
            _management_clients.discard(ws)

    if _management_clients:
        logger.info(
            "Phone config pushed to %s server(s) "
            "(version=%s)", len(_management_clients), config_data['version']
        )
