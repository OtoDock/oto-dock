"""Phone server configuration assembly and push.

Builds the full config JSON that the phone server needs from:
  - platform_settings (audio_* + phone_* keys)
  - phone_routes table
  - infra_credentials (encrypted API keys)

Broadcasts config updates to all connected phone servers via the
management WebSocket.
"""

import json
import logging
import secrets
import time

from storage import audio_provider_store
from storage import credential_store
from storage import database as task_store
from storage import phone_route_store
from storage import phone_server_store

logger = logging.getLogger("phone_config")

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


def duplex_engine_available() -> bool:
    """A connected phone daemon advertising duplex support. This is the
    engine half of the duplex capability gate — provider/policy checks live
    in ``audio_service.duplex_capability``."""
    return pick_duplex_client() is not None


def assemble_duplex_session_config(user_sub: str) -> dict:
    """Per-session config for one duplex session (rides ``duplex_open``).

    Identity-and-provider scope ONLY: the chat-context STT/TTS provider rows
    with decrypted keys, the chat endpointing, and the user's dictation
    language. Behaviour knobs (``phone_tts_buffer_chars``, ``phone_bargein_*``,
    the turn-classifier language map + Groq credentials) ride the GLOBAL
    config push — one source per knob, never duplicated here.
    """
    from storage import user_audio_prefs_store

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
        "language": prefs.get("stt_language") or "",
    }


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
    proxy.
    """
    from storage import subscription_store

    # Pool view (contribute_platform + active + owner-is-admin); reading the store
    # directly here bypasses acquire_subscription, so list_platform_pool is what
    # applies the owner-is-admin guard.
    groq_subs = subscription_store.list_platform_pool(layer="direct-llm", provider="groq")

    # BYO-wins: a stored key beats the hosted relay (lower latency, no credits).
    for sub in groq_subs:
        key = subscription_store.get_credential_data(sub["id"]).get("api_key", "")
        if key:
            return key, ""

    # Hosted: a credential-less relay sub → mint a system token + relay endpoint.
    if any(s.get("auth_type") == "relay" for s in groq_subs):
        from services.engines import subscription_pool
        creds = subscription_pool.relay_llm_credentials("groq", "")
        if creds:
            return creds  # (minted_token, "{RELAY}/v1/relay/groq/v1")

    return "", ""


def groq_classifier_configured() -> bool:
    """True if the turn classifier has a Groq path configured — a BYO key **or** a
    hosted relay sub — **without minting** a token. Backs the read-only admin
    "active" indicator (a GET shouldn't trigger a relay round-trip); whether a
    hosted call actually succeeds still depends on credits at call time.
    """
    from storage import subscription_store
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


def assemble_phone_config() -> dict:
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

    # Phone routes. Inbound PINs ride the push in plaintext for local
    # enforcement — the established contract for daemon secrets on the
    # master-key-authenticated management WS (Twilio auth token, AMI secret,
    # provider API keys below). The PIN never exists as a route column, so
    # the raw rows themselves stay clean.
    routes = phone_route_store.get_all_routes()
    for r in routes:
        if r.get("direction") == "inbound":
            pin = phone_route_store.get_route_pin(r["id"])
            if pin:
                r["pin"] = pin

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
    # → minted system token + relay base_url). See direct_llm_groq_credentials.
    groq_api_key, groq_base_url = direct_llm_groq_credentials()

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


async def notify_phone_config_changed() -> None:
    """Broadcast updated config to all connected phone servers.

    Called after any phone-related DB mutation (route CRUD,
    settings change, credential change).
    """
    try:
        config_data = assemble_phone_config()
    except Exception as e:
        logger.error(f"Failed to assemble phone config: {e}")
        return

    msg = json.dumps({"type": "config_update", "data": config_data})
    dead: list = []

    for ws in list(_management_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)

    for ws in dead:
        _management_clients.discard(ws)

    if _management_clients:
        logger.info(
            f"Phone config pushed to {len(_management_clients)} server(s) "
            f"(version={config_data['version']})"
        )
