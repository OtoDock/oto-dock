"""Phone API — telephony servers, routes, call prompts, and call-only settings.

Admin-only. Phone servers are the telephony adapters (Asterisk/FreePBX today;
Twilio/3CX later) that routes are provisioned against; the adapter bootstrap +
provisioning logic layers on top of the CRUD here. AMI secrets are stored
encrypted in ``infra_credentials`` keyed ``phone-server-{id}-ami-secret`` — never
in the server row. STT/TTS providers + chat audio live in ``api/audio/audio.py``.

All mutations push fresh config to connected phone servers via the management
WebSocket (``notify_phone_config_changed``).
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import config
from auth.providers import UserContext, get_current_user, mask_email, require_admin
from services.phone import phone_adapters
from storage import credential_store
from storage import database as task_store
from storage import phone_call_log_store
from storage import phone_route_store
from storage import phone_server_store
from storage import trigger_store
from services.phone.phone_config import ensure_ami_user, ensure_register_secret, notify_phone_config_changed

logger = logging.getLogger("claude-proxy")
router = APIRouter()

# Call-only platform settings (``phone_`` prefix stripped on the wire).
_PHONE_SETTING_PREFIX = "phone_"

# Secret storage helpers (shared with the config push via the store).
AMI_SECRET_KEY = phone_server_store.AMI_SECRET_KEY
TWILIO_AUTH_TOKEN_KEY = phone_server_store.TWILIO_AUTH_TOKEN_KEY
_ami_cred_name = phone_server_store.ami_cred_name
_register_cred_name = phone_server_store.register_cred_name
_twilio_cred_name = phone_server_store.twilio_cred_name


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class PhoneRouteCreate(BaseModel):
    direction: str  # "inbound" | "outbound"
    name: str = ""
    agent: str = "personal-assistant"
    language: str = "en"
    llm_mode: str = "proxy"
    phone_server_id: int | None = None
    stt_provider_id: int | None = None
    tts_provider_id: int | None = None
    greeting: str = ""
    phone_context_override: str = ""
    backchannel_mode: str = "on"       # per-route filler toggle: on | off
    thinking_filler_mode: str = "on"   # per-route filler toggle: on | off
    background_sound: str = "off"  # ambience bed: off | call_center | office | city | nature
    enabled: bool = True
    audiosocket_uuid: str | None = None
    did: str = ""  # inbound DID the PBX maps to this route (provisioned on the adapter)
    ami_caller_id: str = ""
    ami_outbound_context: str = ""
    dial_prefix: str = ""  # outbound: prepended to the number → selects the FreePBX outbound route/trunk
    # Optional bound trigger (scope='agent', matching agent). When
    # set, the warmup handler builds a ``trigger_payload`` from inbound call
    # context and threads it into ``get_dynamic_contexts`` so manifest
    # ``agent_context`` blocks resolve ``${trigger.*}`` tokens.
    trigger_slug: str | None = None


class PhoneRouteUpdate(BaseModel):
    direction: str | None = None
    name: str | None = None
    agent: str | None = None
    language: str | None = None
    llm_mode: str | None = None
    phone_server_id: int | None = None
    stt_provider_id: int | None = None
    tts_provider_id: int | None = None
    greeting: str | None = None
    phone_context_override: str | None = None
    backchannel_mode: str | None = None
    thinking_filler_mode: str | None = None
    background_sound: str | None = None
    enabled: bool | None = None
    audiosocket_uuid: str | None = None
    did: str | None = None
    ami_caller_id: str | None = None
    ami_outbound_context: str | None = None
    dial_prefix: str | None = None
    # Empty string clears the binding; null = field not supplied (exclude_unset).
    trigger_slug: str | None = None


class PhoneServerCreate(BaseModel):
    name: str
    adapter_type: str = "asterisk_manual"
    host: str = ""
    config: dict = {}
    credentials: dict = {}
    is_default: bool = False
    ami_secret: str | None = None  # stored encrypted, not on the row
    twilio_auth_token: str | None = None  # stored encrypted, not on the row


class PhoneServerUpdate(BaseModel):
    name: str | None = None
    adapter_type: str | None = None
    host: str | None = None
    config: dict | None = None
    credentials: dict | None = None


class SecretSet(BaseModel):
    value: str


# ---------------------------------------------------------------------------
# Adapter cascade helpers (provisioning routes on the phone server)
# ---------------------------------------------------------------------------

async def _load_adapter(server: dict):
    """Build the control-plane adapter for a server row (off the event loop)."""
    return await asyncio.to_thread(phone_adapters.load_adapter, server)


def _adapter_http_error(e: phone_adapters.PhoneAdapterError) -> HTTPException:
    """Map a PhoneAdapterError onto the HTTP envelope (502 vendor / 400 / 504)."""
    detail = e.message
    if e.vendor_status is not None:
        detail = f"{e.message} (provider returned {e.vendor_status})"
    return HTTPException(status_code=e.status_code, detail=detail)


async def _resolve_verified_server(phone_server_id: int | None) -> dict:
    """Resolve the target server for a route and require it be bootstrap-verified.

    Falls back to the default server when none is supplied (direct-API
    convenience; the dashboard always sends one).
    """
    if phone_server_id is None:
        server = await asyncio.to_thread(phone_server_store.get_default_server)
        if not server:
            raise HTTPException(status_code=400, detail="No phone server configured.")
    else:
        server = await asyncio.to_thread(phone_server_store.get_server, phone_server_id)
        if not server:
            raise HTTPException(status_code=404, detail="Phone server not found")
    if server.get("bootstrap_status") != "verified":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Phone server {server['name']!r} is not verified "
                f"(status: {server.get('bootstrap_status')}). Complete bootstrap first."
            ),
        )
    return server


def _bootstrap_log_append(existing: str, line: str) -> str:
    """Append a timestamped line to a server's bootstrap_log (newest last, capped)."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    combined = ((existing or "") + f"\n[{stamp}] {line}").strip()
    return combined[-4000:]  # bounded audit trail


async def _assert_did_available(
    server_id: int, did: str, direction: str, exclude_route_id: str | None = None,
) -> None:
    """409 if an inbound DID is already routed on this server (the DB has a
    unique index on it — pre-checking gives a clean message instead of a 500)."""
    if direction != "inbound" or not did:
        return
    routes = await asyncio.to_thread(phone_route_store.get_all_routes)
    for r in routes:
        if (
            r.get("phone_server_id") == server_id
            and r.get("direction") == "inbound"
            and r.get("did") == did
            and r.get("id") != exclude_route_id
        ):
            raise HTTPException(
                status_code=409,
                detail=f"DID {did} is already routed on this phone server.",
            )


# ---------------------------------------------------------------------------
# Routes CRUD
# ---------------------------------------------------------------------------

def _decorate_routes_with_pin(routes: list[dict]) -> list[dict]:
    """Boolean-flag masking (the ``_decorate_server`` pattern): the PIN value
    itself never appears in any API response."""
    return [
        {
            **r,
            "pin_configured": (
                r["direction"] == "inbound"
                and bool(phone_route_store.get_route_pin(r["id"]))
            ),
        }
        for r in routes
    ]


@router.get("/v1/admin/phone/routes")
async def list_phone_routes(user: UserContext | None = Depends(get_current_user)):
    """List all phone routes."""
    require_admin(user)
    routes = await asyncio.to_thread(phone_route_store.get_all_routes)
    routes = await asyncio.to_thread(_decorate_routes_with_pin, routes)
    return {"routes": routes}


# Ambience templates shipped with the phone daemon (phone/ambience.py).
# Validated here so a typo 400s instead of silently playing nothing.
_BACKGROUND_SOUNDS = {"off", "call_center", "office", "city", "nature"}


def _validate_background_sound(value: str) -> None:
    if value not in _BACKGROUND_SOUNDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown background_sound {value!r} — expected one of "
                f"{sorted(_BACKGROUND_SOUNDS)}"
            ),
        )


def _validate_trigger_slug(slug: str, agent: str) -> None:
    """Defence-in-depth: the bound trigger must exist for this agent in agent
    scope. Frontend already enforces this on the dropdown — but admin-only
    surfaces still validate so a stale slug or a direct curl can't slip through
    and produce silent NULL lookups at warmup time.
    """
    row = trigger_store.get_trigger_by_slug(
        scope="agent", owner=agent, slug=slug,
    )
    if not row:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Trigger {slug!r} not found for agent {agent!r} in agent "
                "scope. Triggers must exist before they can be bound to a "
                "phone route."
            ),
        )


@router.post("/v1/admin/phone/routes")
async def create_phone_route(
    req: PhoneRouteCreate,
    user: UserContext | None = Depends(get_current_user),
):
    """Create a phone route and provision it on its phone server.

    Gates on a bootstrap-verified server, allocates an AudioSocket UUID for
    inbound, inserts the row, then asks the adapter to provision it. A provision
    failure rolls the row back and surfaces the adapter's status (502/400/504).
    """
    u = require_admin(user)
    data = req.model_dump()
    _validate_background_sound(data.get("background_sound", "off"))
    if data.get("trigger_slug"):
        await asyncio.to_thread(
            _validate_trigger_slug, data["trigger_slug"], data["agent"],
        )

    server = await _resolve_verified_server(data.get("phone_server_id"))
    data["phone_server_id"] = server["id"]
    await _assert_did_available(server["id"], data.get("did") or "", data.get("direction", "inbound"))
    # Inbound routes need a stable AudioSocket UUID — it's baked into the PBX
    # DID→UUID mapping. Allocate one when the caller didn't supply it.
    if data.get("direction", "inbound") == "inbound" and not data.get("audiosocket_uuid"):
        data["audiosocket_uuid"] = str(uuid.uuid4())

    route = await asyncio.to_thread(phone_route_store.create_route, data)

    adapter = await _load_adapter(server)
    try:
        handle = await adapter.provision_route(route)
    except phone_adapters.PhoneAdapterError as e:
        # Roll the row back so a failed provision leaves no orphan.
        await asyncio.to_thread(phone_route_store.delete_route, route["id"])
        logger.warning(
            "Provision failed for route %s on server %s: %s",
            route["id"], server["id"], e,
        )
        raise _adapter_http_error(e)

    updated = await asyncio.to_thread(
        phone_route_store.set_adapter_data,
        route["id"],
        adapter_data=handle.adapter_data,
        audiosocket_uuid=handle.audiosocket_uuid,
    )
    route = updated or route
    logger.info(f"Admin {mask_email(u.email)} created phone route: {route['id']} ({route['name']})")
    await notify_phone_config_changed()
    # ``provisioning_instructions`` is non-persisted human follow-up (e.g. the
    # manual adapter's AstDB command); the dashboard shows it after create.
    return {**route, "provisioning_instructions": handle.instructions}


@router.put("/v1/admin/phone/routes/{route_id}")
async def update_phone_route(
    route_id: str,
    req: PhoneRouteUpdate,
    user: UserContext | None = Depends(get_current_user),
):
    """Update a phone route.

    Edits that don't change the *provisioning identity* (server / DID / direction)
    are a plain DB update. When that identity changes the route is re-provisioned:
    provision on the (possibly new) server FIRST — so a failure leaves the DB and
    the old provisioning untouched — then best-effort tear down the old one.
    """
    u = require_admin(user)
    data = req.model_dump(exclude_unset=True)
    if data.get("background_sound") is not None:
        _validate_background_sound(data["background_sound"])
    existing = await asyncio.to_thread(phone_route_store.get_route, route_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Route not found")
    if (data.get("direction") == "outbound"
            and existing["direction"] == "inbound"
            and await asyncio.to_thread(phone_route_store.get_route_pin, route_id)):
        # A silent flip would strand the encrypted credential AND disarm the
        # PIN without anyone noticing — make the admin remove it first.
        raise HTTPException(
            status_code=409,
            detail=(
                "This route has a PIN configured. Remove the PIN before "
                "changing the route to outbound."
            ),
        )
    if data.get("trigger_slug"):
        # Resolve effective agent (post-edit) before validating the trigger
        # belongs to it — the same PUT may rebind both fields.
        target_agent = data.get("agent") or existing["agent"]
        await asyncio.to_thread(
            _validate_trigger_slug, data["trigger_slug"], target_agent,
        )

    identity_changed = any(
        f in data and data[f] != existing.get(f)
        for f in ("phone_server_id", "did", "direction")
    )

    if identity_changed:
        merged = {**existing, **data}
        server = await _resolve_verified_server(merged.get("phone_server_id"))
        merged["phone_server_id"] = server["id"]
        data["phone_server_id"] = server["id"]
        await _assert_did_available(
            server["id"], merged.get("did") or "",
            merged.get("direction", "inbound"), exclude_route_id=route_id,
        )
        if merged.get("direction", "inbound") == "inbound" and not merged.get("audiosocket_uuid"):
            merged["audiosocket_uuid"] = str(uuid.uuid4())
        # Provision on the target server FIRST — DB stays untouched on failure.
        new_adapter = await _load_adapter(server)
        try:
            handle = await new_adapter.provision_route(merged)
        except phone_adapters.PhoneAdapterError as e:
            logger.warning("Re-provision failed for route %s: %s", route_id, e)
            raise _adapter_http_error(e)
        # Best-effort tear-down of the old provisioning (never blocks the edit).
        old_server = await asyncio.to_thread(
            phone_server_store.get_server, existing.get("phone_server_id"),
        )
        if old_server:
            try:
                old_adapter = await _load_adapter(old_server)
                await old_adapter.deprovision_route(existing)
            except phone_adapters.PhoneAdapterError as e:
                logger.warning("Old deprovision failed for route %s (continuing): %s", route_id, e)
        await asyncio.to_thread(phone_route_store.update_route, route_id, data)
        route = await asyncio.to_thread(
            phone_route_store.set_adapter_data,
            route_id,
            adapter_data=handle.adapter_data,
            audiosocket_uuid=handle.audiosocket_uuid or merged.get("audiosocket_uuid"),
        )
    else:
        route = await asyncio.to_thread(phone_route_store.update_route, route_id, data)

    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    logger.info(f"Admin {mask_email(u.email)} updated phone route: {route_id}")
    await notify_phone_config_changed()
    return route


@router.delete("/v1/admin/phone/routes/{route_id}")
async def delete_phone_route(
    route_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Delete a phone route, de-provisioning it on the phone server first.

    De-provision is best-effort: if the PBX call fails the row is still removed
    (so the dashboard never gets stuck) and the response carries a ``warning``
    so the admin can clean the PBX side manually.
    """
    u = require_admin(user)
    route = await asyncio.to_thread(phone_route_store.get_route, route_id)
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    warning = ""
    server = None
    if route.get("phone_server_id"):
        server = await asyncio.to_thread(
            phone_server_store.get_server, route["phone_server_id"],
        )
    if server:
        try:
            adapter = await _load_adapter(server)
            await adapter.deprovision_route(route)
        except phone_adapters.PhoneAdapterError as e:
            warning = (
                "Route removed, but de-provisioning on the phone server failed: "
                f"{e.message}"
            )
            logger.warning("Deprovision failed for route %s (deleting anyway): %s", route_id, e)

    await asyncio.to_thread(phone_route_store.delete_route, route_id)
    # Best-effort: drop the route's encrypted PIN credential with it.
    try:
        await asyncio.to_thread(phone_route_store.delete_route_pin, route_id)
    except Exception:
        logger.warning("PIN credential cleanup failed for route %s", route_id)
    logger.info(f"Admin {mask_email(u.email)} deleted phone route: {route_id}")
    await notify_phone_config_changed()
    resp = {"status": "deleted"}
    if warning:
        resp["warning"] = warning
    return resp


# ---------------------------------------------------------------------------
# Route PIN (inbound access code) — write-only secret sub-resource, the
# phone-server ami-secret/twilio-auth-token shape. Encrypted at rest
# (infra_credentials sidecar), surfaced only as ``pin_configured``.
# ---------------------------------------------------------------------------

_PIN_RE = re.compile(r"^\d{4,6}$")


@router.put("/v1/admin/phone/routes/{route_id}/pin")
async def set_phone_route_pin(
    route_id: str,
    req: SecretSet,
    user: UserContext | None = Depends(get_current_user),
):
    """Set the route's inbound PIN (4-6 digits). The value is never echoed
    back, never logged, and never appears in route responses."""
    u = require_admin(user)
    route = await asyncio.to_thread(phone_route_store.get_route, route_id)
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    if route["direction"] != "inbound":
        raise HTTPException(
            status_code=400, detail="PIN protection is for inbound routes only")
    if not _PIN_RE.fullmatch(req.value or ""):
        raise HTTPException(
            status_code=400, detail="The PIN must be 4 to 6 digits")
    await asyncio.to_thread(phone_route_store.set_route_pin, route_id, req.value)
    logger.info(f"Admin {mask_email(u.email)} set the PIN on phone route {route_id}")
    await notify_phone_config_changed()
    return {"status": "ok", "pin_configured": True}


@router.delete("/v1/admin/phone/routes/{route_id}/pin")
async def delete_phone_route_pin(
    route_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    route = await asyncio.to_thread(phone_route_store.get_route, route_id)
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")
    await asyncio.to_thread(phone_route_store.delete_route_pin, route_id)
    logger.info(f"Admin {mask_email(u.email)} removed the PIN on phone route {route_id}")
    await notify_phone_config_changed()
    return {"status": "ok", "pin_configured": False}


# ---------------------------------------------------------------------------
# Call log (per-route detail view)
# ---------------------------------------------------------------------------

@router.get("/v1/admin/phone/call-log")
async def get_phone_call_log(
    route_id: str | None = None,
    offset: int = 0,
    limit: int = 50,
    user: UserContext | None = Depends(get_current_user),
):
    """Newest-first call rows (optionally one route's) with outcomes —
    including PIN failures/cooldowns and capacity rejects."""
    require_admin(user)
    limit = max(1, min(int(limit), config.MAX_PAGE_SIZE))
    offset = max(0, int(offset))
    calls, total = await asyncio.to_thread(
        phone_call_log_store.list_calls, route_id or None,
        offset=offset, limit=limit,
    )
    return {"calls": calls, "total": total}


# ---------------------------------------------------------------------------
# Phone servers CRUD
# ---------------------------------------------------------------------------

def _decorate_server(server: dict) -> dict:
    """Attach the secret-configured flags the pill UI renders."""
    creds = credential_store.get_infra_credentials(_ami_cred_name(server["id"]))
    twilio_creds = credential_store.get_infra_credentials(
        _twilio_cred_name(server["id"]))
    return {
        **server,
        "ami_secret_configured": bool(creds.get(AMI_SECRET_KEY, "")),
        "twilio_auth_token_configured": bool(
            twilio_creds.get(TWILIO_AUTH_TOKEN_KEY, "")),
    }


@router.get("/v1/admin/phone-servers")
async def list_phone_servers(user: UserContext | None = Depends(get_current_user)):
    require_admin(user)
    servers = await asyncio.to_thread(phone_server_store.get_all_servers)
    return {"servers": [_decorate_server(s) for s in servers]}


@router.post("/v1/admin/phone-servers")
async def create_phone_server(
    req: PhoneServerCreate, user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    if req.adapter_type not in phone_adapters.ADAPTER_CLASSES:
        raise HTTPException(status_code=400, detail="Unknown adapter_type")
    if req.adapter_type not in phone_adapters.available_adapter_types():
        # Local PBX (Asterisk/FreePBX) is disabled on this install (e.g. OtoDock
        # cloud) — see config.LOCAL_PBX_ENABLED.
        raise HTTPException(
            status_code=400,
            detail=f"Adapter type {req.adapter_type!r} is disabled on this install",
        )
    data = req.model_dump(exclude={"ami_secret", "twilio_auth_token"})
    try:
        server = await asyncio.to_thread(phone_server_store.create_server, data)
    except Exception as e:  # unique name collision, etc.
        raise HTTPException(status_code=400, detail=f"Could not create server: {e}")
    if req.ami_secret:
        await asyncio.to_thread(
            credential_store.set_infra_credentials,
            _ami_cred_name(server["id"]), {AMI_SECRET_KEY: req.ami_secret},
        )
    if req.twilio_auth_token:
        await asyncio.to_thread(
            credential_store.set_infra_credentials,
            _twilio_cred_name(server["id"]),
            {TWILIO_AUTH_TOKEN_KEY: req.twilio_auth_token},
        )
    logger.info(f"Admin {mask_email(u.email)} created phone server: {server['name']} ({server['adapter_type']})")
    await notify_phone_config_changed()
    return _decorate_server(server)


@router.put("/v1/admin/phone-servers/{server_id}")
async def update_phone_server(
    server_id: int, req: PhoneServerUpdate,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    server = await asyncio.to_thread(
        phone_server_store.update_server, server_id, req.model_dump(exclude_unset=True),
    )
    if not server:
        raise HTTPException(status_code=404, detail="Phone server not found")
    logger.info(f"Admin {mask_email(u.email)} updated phone server: {server_id}")
    await notify_phone_config_changed()
    return _decorate_server(server)


@router.delete("/v1/admin/phone-servers/{server_id}")
async def delete_phone_server(
    server_id: int, user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    in_use = await asyncio.to_thread(phone_server_store.routes_using_server, server_id)
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"Server is used by {len(in_use)} route(s): {', '.join(in_use)}. "
                   "Re-assign or delete those routes first.",
        )
    deleted = await asyncio.to_thread(phone_server_store.delete_server, server_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Phone server not found")
    await asyncio.to_thread(credential_store.delete_infra_credentials, _ami_cred_name(server_id))
    await asyncio.to_thread(credential_store.delete_infra_credentials, _register_cred_name(server_id))
    await asyncio.to_thread(credential_store.delete_infra_credentials, _twilio_cred_name(server_id))
    logger.info(f"Admin {mask_email(u.email)} deleted phone server: {server_id}")
    await notify_phone_config_changed()
    return {"status": "deleted"}


@router.put("/v1/admin/phone-servers/{server_id}/default")
async def set_phone_server_default(
    server_id: int, user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    try:
        server = await asyncio.to_thread(phone_server_store.set_default, server_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    logger.info(f"Admin {mask_email(u.email)} set default phone server: {server_id}")
    await notify_phone_config_changed()
    return _decorate_server(server)


@router.put("/v1/admin/phone-servers/{server_id}/ami-secret")
async def set_phone_server_ami_secret(
    server_id: int, req: SecretSet,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    server = await asyncio.to_thread(phone_server_store.get_server, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Phone server not found")
    await asyncio.to_thread(
        credential_store.set_infra_credentials,
        _ami_cred_name(server_id), {AMI_SECRET_KEY: req.value},
    )
    logger.info(f"Admin {mask_email(u.email)} set AMI secret for phone server: {server_id}")
    await notify_phone_config_changed()
    return {"status": "saved"}


@router.delete("/v1/admin/phone-servers/{server_id}/ami-secret")
async def delete_phone_server_ami_secret(
    server_id: int, user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    await asyncio.to_thread(credential_store.delete_infra_credentials, _ami_cred_name(server_id))
    logger.info(f"Admin {mask_email(u.email)} deleted AMI secret for phone server: {server_id}")
    await notify_phone_config_changed()
    return {"status": "deleted"}


@router.put("/v1/admin/phone-servers/{server_id}/twilio-auth-token")
async def set_phone_server_twilio_auth_token(
    server_id: int, req: SecretSet,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    server = await asyncio.to_thread(phone_server_store.get_server, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Phone server not found")
    await asyncio.to_thread(
        credential_store.set_infra_credentials,
        _twilio_cred_name(server_id), {TWILIO_AUTH_TOKEN_KEY: req.value},
    )
    logger.info(f"Admin {mask_email(u.email)} set Twilio auth token for phone server: {server_id}")
    await notify_phone_config_changed()
    return {"status": "saved"}


@router.delete("/v1/admin/phone-servers/{server_id}/twilio-auth-token")
async def delete_phone_server_twilio_auth_token(
    server_id: int, user: UserContext | None = Depends(get_current_user),
):
    u = require_admin(user)
    await asyncio.to_thread(
        credential_store.delete_infra_credentials, _twilio_cred_name(server_id))
    logger.info(f"Admin {mask_email(u.email)} deleted Twilio auth token for phone server: {server_id}")
    await notify_phone_config_changed()
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Bootstrap + health (adapter-driven)
# ---------------------------------------------------------------------------

async def _get_server_or_404(server_id: int) -> dict:
    server = await asyncio.to_thread(phone_server_store.get_server, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Phone server not found")
    return server


@router.get("/v1/admin/phone-servers/{server_id}/bootstrap")
async def get_phone_server_bootstrap(
    server_id: int, user: UserContext | None = Depends(get_current_user),
):
    """The one-time setup snippet + current bootstrap state for a server."""
    require_admin(user)
    server = await _get_server_or_404(server_id)
    # Mint this server's register secret (idempotent) before the adapter renders
    # the snippet, so the embedded Bearer token is populated on first view.
    await asyncio.to_thread(ensure_register_secret, server_id)
    # AMI adapters also get a generated manager-user snippet: mint the user +
    # secret (idempotent, admin-set values win) BEFORE loading the adapter so
    # the row config carries the username and Verify works with zero typing.
    ami_username = ami_secret = ""
    ami_file = getattr(phone_adapters.ADAPTER_CLASSES.get(
        server.get("adapter_type", "")), "ami_snippet_file", None)
    if ami_file:
        ami_username, ami_secret = await asyncio.to_thread(ensure_ami_user, server_id)
        server = await _get_server_or_404(server_id)  # re-read: config may have gained the username
    adapter = await _load_adapter(server)
    try:
        snippet = await adapter.get_bootstrap_snippet()
    except phone_adapters.PhoneAdapterError as e:
        raise _adapter_http_error(e)
    return {
        "status": server["bootstrap_status"],
        "log": server["bootstrap_log"],
        "snippet": snippet,
        "ami_snippet": (
            adapter.render_ami_user_snippet(ami_username, ami_secret)
            if ami_file else None
        ),
        "ami_snippet_file": ami_file,
        "ami_username": ami_username or None,
        "requires_bootstrap": adapter.requires_bootstrap,
        "supports_sftp": adapter.supports_sftp_bootstrap,
    }


@router.post("/v1/admin/phone-servers/{server_id}/bootstrap/verify")
async def verify_phone_server_bootstrap(
    server_id: int, user: UserContext | None = Depends(get_current_user),
):
    """Verify the one-time bootstrap (adapter-defined) and persist the result."""
    u = require_admin(user)
    server = await _get_server_or_404(server_id)
    adapter = await _load_adapter(server)
    try:
        result = await adapter.verify_bootstrap()
    except phone_adapters.PhoneAdapterError as e:
        raise _adapter_http_error(e)
    log = _bootstrap_log_append(
        server["bootstrap_log"], f"verify → {result.status}: {result.detail}",
    )
    server = await asyncio.to_thread(
        phone_server_store.update_server, server_id,
        {"bootstrap_status": result.status, "bootstrap_log": log},
    )
    logger.info(f"Admin {mask_email(u.email)} verified phone server {server_id}: {result.status}")
    await notify_phone_config_changed()
    return _decorate_server(server)


@router.post("/v1/admin/phone-servers/{server_id}/bootstrap/apply")
async def apply_phone_server_bootstrap(
    server_id: int, req: dict,
    user: UserContext | None = Depends(get_current_user),
):
    """Install the bootstrap over SSH/SFTP (FreePBX). SFTP creds are
    one-shot — used for this call and never persisted."""
    u = require_admin(user)
    server = await _get_server_or_404(server_id)
    adapter = await _load_adapter(server)
    try:
        result = await adapter.apply_bootstrap(req or {})
    except phone_adapters.PhoneAdapterError as e:
        log = _bootstrap_log_append(server["bootstrap_log"], f"apply failed: {e.message}")
        await asyncio.to_thread(
            phone_server_store.update_server, server_id, {"bootstrap_log": log},
        )
        raise _adapter_http_error(e)
    log = _bootstrap_log_append(
        server["bootstrap_log"], f"apply → {result.status}: {result.detail}",
    )
    server = await asyncio.to_thread(
        phone_server_store.update_server, server_id,
        {"bootstrap_status": result.status, "bootstrap_log": log},
    )
    logger.info(f"Admin {mask_email(u.email)} applied bootstrap on phone server {server_id}: {result.status}")
    await notify_phone_config_changed()
    return _decorate_server(server)


@router.post("/v1/admin/phone-servers/{server_id}/health")
async def check_phone_server_health(
    server_id: int, user: UserContext | None = Depends(get_current_user),
):
    """Probe the server now and persist the health columns."""
    require_admin(user)
    server = await _get_server_or_404(server_id)
    adapter = await _load_adapter(server)
    try:
        status = await adapter.health_check()
    except phone_adapters.PhoneAdapterError as e:
        status = phone_adapters.HealthStatus(healthy=False, detail=e.message)
    server = await asyncio.to_thread(
        phone_server_store.update_server, server_id,
        {
            "last_health_check": datetime.now(timezone.utc).isoformat(),
            "last_health_status": "healthy" if status.healthy else "unhealthy",
            "last_health_detail": status.detail,
        },
    )
    return _decorate_server(server)


# ---------------------------------------------------------------------------
# Call-only settings (phone_* keys; prompts, languages, fillers, timeouts, …)
# ---------------------------------------------------------------------------

@router.get("/v1/admin/phone/settings")
async def get_phone_settings(user: UserContext | None = Depends(get_current_user)):
    """Phone (call-only) settings with the ``phone_`` prefix stripped."""
    require_admin(user)
    all_settings = await asyncio.to_thread(task_store.get_all_platform_settings)
    return {
        k.removeprefix(_PHONE_SETTING_PREFIX): v
        for k, v in all_settings.items()
        if k.startswith(_PHONE_SETTING_PREFIX)
    }


@router.put("/v1/admin/phone/settings")
async def update_phone_settings(
    req: dict, user: UserContext | None = Depends(get_current_user),
):
    """Partial update of phone_* settings. Keys arrive prefix-stripped."""
    u = require_admin(user)
    for key, value in req.items():
        if value is not None:
            await asyncio.to_thread(
                task_store.set_platform_setting, f"{_PHONE_SETTING_PREFIX}{key}", str(value),
            )
    logger.info(f"Admin {mask_email(u.email)} updated phone settings: {list(req.keys())}")
    await notify_phone_config_changed()
    return {"status": "updated"}
