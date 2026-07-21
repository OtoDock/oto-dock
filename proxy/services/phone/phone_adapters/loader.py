"""Adapter factory — maps ``adapter_type`` → adapter class and wires deps.

``load_adapter`` is cheap and does a small synchronous settings read (resolving
the media endpoint), so callers invoke it via ``asyncio.to_thread`` to keep the
event loop clean. The returned adapter's async methods do the provider I/O.
"""

from __future__ import annotations

import logging

import config

from storage import credential_store
from storage import database as task_store

from .asterisk_freepbx import AsteriskFreePBXAdapter
from .base import PhoneAdapterError, PhoneServerAdapter
from .manual_asterisk import ManualAsteriskAdapter
from .stubs import ThreeCxStubAdapter, TwilioStubAdapter

# adapter_type → class. Asterisk (manual + FreePBX over AMI) are real;
# Twilio/3CX are graceful stubs until their automation ships.
logger = logging.getLogger("claude-proxy.phone.adapters")

ADAPTER_CLASSES: dict[str, type[PhoneServerAdapter]] = {
    "asterisk_manual": ManualAsteriskAdapter,
    "asterisk_freepbx": AsteriskFreePBXAdapter,
    "twilio": TwilioStubAdapter,
    "three_cx": ThreeCxStubAdapter,
}

# AudioSocket-based local-PBX adapter types (the Asterisk family). Gated OFF when
# config.LOCAL_PBX_ENABLED is false (OtoDock cloud) — see config.py. Twilio/3CX are
# cloud-reachable and stay available regardless.
LOCAL_PBX_ADAPTERS = frozenset({"asterisk_manual", "asterisk_freepbx"})


def available_adapter_types() -> list[str]:
    """Adapter types an admin may create on THIS install — ``ADAPTER_CLASSES`` minus
    the local-PBX (Asterisk/FreePBX) ones when ``config.LOCAL_PBX_ENABLED`` is false."""
    if config.LOCAL_PBX_ENABLED:
        return list(ADAPTER_CLASSES)
    return [t for t in ADAPTER_CLASSES if t not in LOCAL_PBX_ADAPTERS]


_DEFAULT_AUDIOSOCKET_PORT = "9092"
_DEFAULT_HTTP_API_PORT = "9093"


def load_adapter(server_row: dict) -> PhoneServerAdapter:
    """Build the control-plane adapter for a ``phone_servers`` row."""
    adapter_type = server_row.get("adapter_type", "")
    cls = ADAPTER_CLASSES.get(adapter_type)
    if cls is None:
        raise PhoneAdapterError(
            f"Unknown adapter type {adapter_type!r}", status_code=400,
        )
    return cls(
        server_row,
        credential_resolver=_make_credential_resolver(server_row["id"]),
        media_endpoint=_resolve_media_endpoint(server_row),
        register_endpoint=_resolve_register_endpoint(server_row),
    )


def _make_credential_resolver(server_id):
    """A per-server resolver: ``resolve("ami-secret")`` → the decrypted infra
    credential dict stored under ``phone-server-{id}-ami-secret``."""

    def resolve(suffix: str) -> dict:
        return credential_store.get_infra_credentials(
            f"phone-server-{server_id}-{suffix}",
        )

    return resolve


def _resolve_media_endpoint(server_row: dict) -> str:
    """The host:port Asterisk dials for AudioSocket — the *phone server's*
    reachable address (NOT the PBX, NOT the 0.0.0.0 listener bind).

    Host is auto-resolved at startup (``config.AUDIOSOCKET_PUBLIC_HOST`` — the
    machine's outbound IP, or ``OTO_AUDIOSOCKET_PUBLIC_HOST`` when the
    installer/compose sets it). Port is the admin-set ``phone_audiosocket_port``.
    A per-server ``config.audiosocket_endpoint`` (rarely needed; e.g. a NAT'd PBX
    VM) overrides both — no admin needs to type an IP in the common case."""
    cfg = server_row.get("config") or {}
    explicit = (cfg.get("audiosocket_endpoint") or "").strip()
    if explicit:
        return explicit
    _warn_if_container_autodetect(server_row)
    settings = task_store.get_all_platform_settings()
    port = (settings.get("phone_audiosocket_port") or _DEFAULT_AUDIOSOCKET_PORT).strip()
    return f"{config.AUDIOSOCKET_PUBLIC_HOST}:{port}"


def _warn_if_container_autodetect(server_row: dict) -> None:
    """An autodetected AUDIOSOCKET_PUBLIC_HOST inside a container is the
    container's own bridge IP — the PBX can't dial it, so the generated
    dialplan is silently broken. Warn loudly at render time; the fix is
    ``OTO_AUDIOSOCKET_PUBLIC_HOST=<docker host LAN IP>`` in the env file (or a
    per-server ``audiosocket_endpoint`` override)."""
    import os
    if not config.AUDIOSOCKET_PUBLIC_HOST_AUTODETECTED:
        return
    if not os.path.exists("/.dockerenv"):
        return
    logger.warning(
        "phone server %s: AudioSocket endpoint autodetected as %s INSIDE a "
        "container — the PBX cannot reach a container-internal IP. Set "
        "OTO_AUDIOSOCKET_PUBLIC_HOST to the Docker host's LAN IP in the env "
        "file and regenerate the dialplan snippet.",
        server_row.get("id"), config.AUDIOSOCKET_PUBLIC_HOST,
    )


def _resolve_register_endpoint(server_row: dict) -> str:
    """The ``host:port/v1/calls/register`` (no scheme — the template wraps
    ``http://``) the inbound dialplan curls to attach caller metadata before
    AudioSocket connects.

    Same reachable phone-server host as the media endpoint, but the phone
    daemon's HTTP API port (default 9093) — NOT the AudioSocket media port. A
    per-server ``config.http_api_endpoint`` (``host:port``) overrides, for a
    NAT'd / custom-port deployment; any scheme/path an admin pastes is stripped
    so the rendered URL is always exactly ``http://<host:port>/v1/calls/register``."""
    cfg = server_row.get("config") or {}
    explicit = (cfg.get("http_api_endpoint") or "").strip()
    if explicit:
        # bare host:port only — drop any scheme or path the admin pasted.
        hostport = explicit.split("://", 1)[-1].split("/", 1)[0]
    else:
        settings = task_store.get_all_platform_settings()
        port = (settings.get("phone_http_api_port") or _DEFAULT_HTTP_API_PORT).strip()
        hostport = f"{config.AUDIOSOCKET_PUBLIC_HOST}:{port}"
    return f"{hostport}/v1/calls/register"
