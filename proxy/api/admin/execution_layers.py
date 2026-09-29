"""Execution Layer Management REST API.

Admin endpoints for managing subscriptions, API keys, and models per
execution layer.  User endpoints for connecting personal subscriptions.
"""

from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import config
from auth.providers import get_current_user, require_auth, require_admin, UserContext
from storage.billing import subscription_status, subscription_store
from core.execution_layer import PROVIDER_LOCAL, LayerCapabilities, provider_entry
from core.session.session_manager import (
    capabilities_for_path, get_all_capabilities, get_all_layers, get_layer_capabilities,
)
from services.engines import subscription_pool
from auth import roles

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class AddSubscriptionRequest(BaseModel):
    provider: str             # 'anthropic' | 'openai' | 'groq' | 'ollama' | 'openai_compatible'
    auth_type: str            # 'api_key' | 'local_endpoint'
    label: str = ""
    api_key: str | None = None
    endpoint_url: str | None = None
    # Scope flags. None = "use the endpoint default" (admin add → both TRUE;
    # user add → use_personal TRUE, contribute_platform forced FALSE unless admin).
    use_personal: bool | None = None
    contribute_platform: bool | None = None


class UpdateSubscriptionRequest(BaseModel):
    label: str | None = None
    status: str | None = None
    use_personal: bool | None = None
    contribute_platform: bool | None = None


class AddModelRequest(BaseModel):
    model_id: str
    display_name: str
    provider: str = ""
    context_window: int = 0
    pricing_input: float = 0         # $ per 1M tokens
    pricing_output: float = 0
    pricing_cache_write: float = 0
    pricing_cache_read: float = 0
    supports_reasoning: bool = False
    supports_xhigh: bool = False
    tier: int | None = None          # 1 (frontier) .. 4 (fast); None = untiered
    good_at: str = ""


class BulkAddModelsRequest(BaseModel):
    models: list[dict]     # [{"model_id": str, "display_name": str}]
    provider: str
    # Engines to add the rows to (a shared local endpoint adds its discovered
    # models to every engine it is enabled for). Default: the URL's layer.
    layers: list[str] | None = None


class DiscoverModelsRequest(BaseModel):
    subscription_id: str


class AddLocalEndpointRequest(BaseModel):
    provider: str             # 'ollama' | 'openai_compatible'
    endpoint_url: str
    label: str = ""
    api_key: str | None = None
    layers: list[str]         # engines the endpoint is enabled for


class SetLocalEndpointEngineRequest(BaseModel):
    layer: str
    enabled: bool


class UpdateModelRequest(BaseModel):
    enabled: bool | None = None
    context_window: int | None = None
    pricing_input: float | None = None
    pricing_output: float | None = None
    pricing_cache_write: float | None = None
    pricing_cache_read: float | None = None
    supports_reasoning: bool | None = None
    supports_xhigh: bool | None = None
    # `tier: null` sent explicitly clears the tier (model_fields_set tells
    # an explicit null from an absent field); custom rows only.
    tier: int | None = None
    good_at: str | None = None


def _validate_tier_fields(tier: int | None, good_at: str | None) -> None:
    if tier is not None and tier not in config.MODEL_TIER_LABELS:
        raise HTTPException(400, "tier must be 1 (frontier) to 4 (fast)")
    if good_at is not None and len(good_at) > config.MODEL_GOOD_AT_MAX_CHARS:
        raise HTTPException(
            400, f"good_at must be at most {config.MODEL_GOOD_AT_MAX_CHARS} characters")


class SetPlatformAuthRequest(BaseModel):
    allowed: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_admin(user: UserContext | None) -> None:
    # Delegates to the shared guard: 401 if unauthenticated (user is None),
    # then 403 if not admin. Without the None-guard, an unauthenticated request
    # dereferenced `user.role` → AttributeError → 500 instead of 401.
    require_admin(user)


def _valid_layers() -> set[str]:
    """The registered engine ids — read per call, never frozen at import.

    Was a module-level literal that had to be kept in step with
    ``session_manager._LAYERS`` by hand; registering a layer now makes its id
    valid here by construction.
    """
    from core.session.session_manager import valid_execution_paths
    return valid_execution_paths()


# Local providers reach the operator's own network — unavailable on hosted
# OtoDock (no operator LAN), where the descriptors do not declare them at
# all; a provider no engine declares there gets this message (a typo gets
# it too — stated).
_CLOUD_LOCAL_MSG = (
    "Local model endpoints are unavailable on hosted OtoDock — "
    "they would need access to your own network."
)


# What a row on an engine may name is the ENGINE's declaration, read from its
# descriptor (``LayerCapabilities.auth`` / ``providers`` / ``identity``) —
# these used to be three module-level maps that had to be kept in step with
# the layers by hand, and validated a provider against the union across
# every engine, so an ``openai`` key on ``claude-code-cli`` was stored and
# later handed out as ``ANTHROPIC_API_KEY``.

def _layer_providers(caps: LayerCapabilities) -> list[str]:
    """The providers a subscription row on this engine may name: the
    declared provider list of a multi-provider engine, else its one vendor."""
    if caps.providers:
        return [p["id"] for p in caps.providers]
    return [caps.identity.vendor_id] if caps.identity.vendor_id else []


def _user_key_provider(layer: str) -> str:
    """The vendor a user's OWN API key on ``layer`` belongs to, or "" when
    the engine takes none: it has no vendor to hold a key for (Direct LLM is
    multi-provider — its keys are admin infrastructure) or ``api_key`` is
    not among its auth types. Tolerant of an unknown id (→ "")."""
    caps = get_layer_capabilities(layer)
    if caps is None or "api_key" not in caps.auth.auth_types:
        return ""
    return caps.identity.vendor_id


def _layers_with_auth_type(auth_type: str) -> list[str]:
    """The registered engines whose rows may carry ``auth_type``, in
    registry order."""
    return [
        path for path, layer in get_all_layers().items()
        if auth_type in layer.capabilities.auth.auth_types
    ]


async def _subscriptions_changed(*layers: str) -> None:
    """Tell each engine its subscription set changed (Direct LLM pushes the
    phone config, whose Groq classifier reuses its Groq key). Tolerant: a
    row stored on an engine that is no longer registered has nothing to
    notify, and a mutation of an existing row must not start raising."""
    registered = get_all_layers()
    for path in dict.fromkeys(layers):
        layer = registered.get(path)
        if layer is not None:
            await layer.on_subscriptions_changed()


# ---------------------------------------------------------------------------
# Admin: Layer overview
# ---------------------------------------------------------------------------

def _attach_windows(subs: list[dict]) -> None:
    """Each OAuth row's effective 5-hour / weekly reading (``windows``), for
    the bars on both AI Engines cards. Percentages only, so shared-by-another-
    admin rows carry it too. Absent while the platform setting is off."""
    from services.engines import subscription_windows
    if not subscription_windows.is_enabled():
        return
    oauth = [s for s in subs if s.get("auth_type") == "oauth" and s.get("id")]
    readings = subscription_windows.latest(oauth) if oauth else {}
    for s in subs:
        if s.get("auth_type") != "oauth":
            continue
        w = readings.get(s.get("id"))
        s["windows"] = subscription_windows.to_public(w) if w else None


@router.get("/v1/admin/execution-layers")
async def admin_list_layers(user: UserContext = Depends(get_current_user)):
    """List all execution layers with subscriptions, models, and pool stats."""
    _require_admin(user)

    capabilities = get_all_capabilities()
    layers = []

    for path, caps in capabilities.items():
        # The admin tab manages the platform pool + owner-less infra (relay / migrated
        # shared keys). list_admin_managed keeps owner-less subs visible even with
        # 'Agent pool' off, so toggling it can't make them vanish. is_mine drives which
        # rows show edit controls (the caller's own accounts). Local endpoints are
        # listed ONCE across the engines in ``local_endpoints`` below, not per layer.
        platform_subs = [
            s for s in subscription_store.list_admin_managed(layer=path)
            if s.get("auth_type") != "local_endpoint"
        ]
        for s in platform_subs:
            s["is_mine"] = bool(s.get("owner_sub")) and s.get("owner_sub") == user.sub
        _attach_windows(platform_subs)
        # Count personal accounts (without exposing details)
        personal_subs = subscription_store.list_subscriptions(
            layer=path, use_personal=True, include_disabled=True,
        )
        # Get models (sync builtins first)
        subscription_store.sync_builtin_models(path, caps.get("models", []))
        models = subscription_store.list_models(layer=path)
        # Pool stats
        pool = subscription_store.get_pool_stats(path)

        layers.append({
            "name": path,
            "display_name": caps.get("display_name", path),
            "capabilities": caps,
            "subscriptions": {
                "platform": platform_subs,
                "user_count": len(personal_subs),
            },
            "models": models,
            "pool_stats": pool,
        })

    local_endpoints = [
        _annotate_local_group(g, user)
        for g in subscription_store.list_local_endpoint_groups()
    ]
    return {"layers": layers, "local_endpoints": local_endpoints}


# ---------------------------------------------------------------------------
# Admin: Subscriptions CRUD
# ---------------------------------------------------------------------------

@router.get("/v1/admin/execution-layers/{layer}/subscriptions")
async def admin_list_subscriptions(
    layer: str,
    user: UserContext = Depends(get_current_user),
):
    _require_admin(user)
    if layer not in _valid_layers():
        raise HTTPException(400, f"Invalid layer: {layer}")
    subs = subscription_store.list_admin_managed(layer=layer)
    for s in subs:
        s["is_mine"] = bool(s.get("owner_sub")) and s.get("owner_sub") == user.sub
    _attach_windows(subs)
    return {"subscriptions": subs}


@router.post("/v1/admin/execution-layers/{layer}/subscriptions")
async def admin_add_subscription(
    layer: str,
    req: AddSubscriptionRequest,
    user: UserContext = Depends(get_current_user),
):
    _require_admin(user)
    if layer not in _valid_layers():
        raise HTTPException(400, f"Invalid layer: {layer}")
    caps = capabilities_for_path(layer)
    entry = provider_entry(caps, req.provider)
    if entry is None and caps.providers:
        # Hosted OtoDock declares no local provider: the message says why.
        if config.OTODOCK_CLOUD:
            raise HTTPException(400, _CLOUD_LOCAL_MSG)
        raise HTTPException(400, f"Invalid provider for {layer}: {req.provider}")
    if req.provider not in _layer_providers(caps):
        raise HTTPException(400, f"Invalid provider for {layer}: {req.provider}")
    if req.auth_type == "oauth":
        # An OAuth row carries a vendor login; only its login flow (the
        # /v1/oauth/* exchange endpoints) can create one. Accepting it here
        # stored a row with an EMPTY credential.
        raise HTTPException(400, "Connect an account through its login flow, not as a key")
    if req.auth_type not in caps.auth.auth_types:
        raise HTTPException(400, f"Invalid auth_type for {layer}: {req.auth_type}")

    # Hosted Direct-LLM: a credential-less platform sub that routes this provider's
    # LLM calls through the OtoDock relay (credit-metered; the token is minted per
    # session/user at resolve time). Only a vendor the engine declares a
    # ``relay_path`` for, on an engine that declares ``relay`` among its auth
    # types (checked above).
    # Idempotent — one relay sub per provider (re-enable returns the existing one).
    if req.auth_type == "relay":
        if not (entry or {}).get("relay_path"):
            raise HTTPException(400, f"hosted relay not available for provider: {req.provider}")
        for s in subscription_store.list_subscriptions(
            layer=layer, contribute_platform=True, include_disabled=True,
        ):
            if s.get("provider") == req.provider and s.get("auth_type") == "relay":
                return s
        sub = subscription_store.add_subscription(
            layer=layer, provider=req.provider, auth_type="relay",
            owner_sub="", use_personal=False, contribute_platform=True,
            label=req.label or "OtoDock Hosted", credential_data={},
        )
        await _subscriptions_changed(layer)
        return sub

    # Build credential data
    cred_data = {}
    if req.auth_type == "api_key":
        if not req.api_key:
            raise HTTPException(400, "api_key required for api_key auth type")
        cred_data["api_key"] = req.api_key
    elif req.auth_type == "local_endpoint":
        if not req.endpoint_url:
            raise HTTPException(400, "endpoint_url required for local_endpoint auth type")
        cred_data["endpoint_url"] = req.endpoint_url
        # Optional bearer for a key-protected local server (llama.cpp
        # --api-key, a LiteLLM master key, …). Blank = keyless.
        if req.api_key:
            cred_data["api_key"] = req.api_key

    try:
        sub = subscription_store.add_subscription(
            layer=layer,
            provider=req.provider,
            auth_type=req.auth_type,
            owner_sub=user.sub,
            # Admin-added accounts default to BOTH personal use and pool contribution.
            use_personal=True if req.use_personal is None else req.use_personal,
            contribute_platform=True if req.contribute_platform is None else req.contribute_platform,
            label=req.label,
            credential_data=cred_data,
        )
    except subscription_store.SubscriptionExists:
        raise HTTPException(409, "A credential of this kind already exists on this engine")
    subscription_pool.schedule_rebind("admin subscription add")
    await _subscriptions_changed(layer)
    return sub


@router.put("/v1/admin/execution-layers/{layer}/subscriptions/{sub_id}")
async def admin_update_subscription(
    layer: str,
    sub_id: str,
    req: UpdateSubscriptionRequest,
    user: UserContext = Depends(get_current_user),
):
    _require_admin(user)
    # Owner-or-infra only: an admin manages their OWN accounts (and owner-less
    # platform infra like the relay) — never another admin's connected account.
    existing = subscription_store.get_subscription(sub_id)
    if not existing:
        raise HTTPException(404, "Subscription not found")
    if existing.get("owner_sub") not in ("", user.sub):
        raise HTTPException(403, "Not your subscription")
    if req.status is not None and req.status not in subscription_status.STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(subscription_status.STATUSES)}")
    result = subscription_store.update_subscription(
        sub_id,
        label=req.label,
        status=req.status,
        use_personal=req.use_personal,
        contribute_platform=req.contribute_platform,
    )
    if not result:
        raise HTTPException(404, "Subscription not found")
    # Live sessions follow the selection: a scope/status change here may have
    # delisted this account for its bound sessions — re-home them now.
    subscription_pool.schedule_rebind("admin subscription update")
    await _subscriptions_changed(layer)
    return result


@router.delete("/v1/admin/execution-layers/{layer}/subscriptions/{sub_id}")
async def admin_delete_subscription(
    layer: str,
    sub_id: str,
    force: bool = False,
    user: UserContext = Depends(get_current_user),
):
    _require_admin(user)
    sub = subscription_store.get_subscription(sub_id)
    if not sub:
        raise HTTPException(404, "Subscription not found")
    # Owner-or-infra only (see admin_update_subscription).
    if sub.get("owner_sub") not in ("", user.sub):
        raise HTTPException(403, "Not your subscription")
    # In use? Judged on the LIVE bindings, not the stored counter: the
    # counter reads stale after an abandoned spawn, and a stale number used
    # to refuse the delete until a restart (public issue #3). The reconcile
    # writes the honest value back. Live sessions still block — unless the
    # admin forces it, in which case the rebind fan-out re-homes them onto
    # the remaining selection (or blocks them until one is connected).
    _stored, live = subscription_pool.reconcile_active_sessions(sub_id)
    if live > 0 and not force:
        raise HTTPException(
            409,
            f"Subscription has {live} live session(s). Wait for them to "
            "close, or delete with force=true to move them to another "
            "subscription.",
        )
    deleted = subscription_store.delete_subscription(sub_id)
    if not deleted:
        raise HTTPException(404, "Subscription not found")
    subscription_pool.schedule_rebind("admin subscription delete")
    await _subscriptions_changed(layer)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Admin: Local endpoints (one server, listed once across the engines)
# ---------------------------------------------------------------------------

def _local_group(group: str) -> dict:
    for g in subscription_store.list_local_endpoint_groups():
        if g["group"] == group:
            return g
    raise HTTPException(404, "Local endpoint not found")


def _require_group_owner(g: dict, user: UserContext) -> None:
    # Owner-or-infra, like every subscription mutation: sibling rows carry
    # the connecting admin's sub (an admin never edits another admin's).
    for eng in g["engines"].values():
        if eng.get("owner_sub") not in ("", user.sub):
            raise HTTPException(403, "Not your endpoint")


def _annotate_local_group(g: dict, user: UserContext) -> dict:
    out = dict(g)
    out["engines"] = {
        layer: {
            "id": eng["id"], "status": eng["status"],
            "active_sessions": eng.get("active_sessions", 0),
            "is_mine": eng.get("owner_sub") in ("", user.sub),
        }
        for layer, eng in g["engines"].items()
    }
    return out


async def _local_endpoint_changed(layers) -> None:
    subscription_pool.schedule_rebind("local endpoint change")
    await _subscriptions_changed(*layers)


@router.post("/v1/admin/execution-layers/local-endpoints")
async def admin_add_local_endpoint(
    req: AddLocalEndpointRequest,
    user: UserContext = Depends(get_current_user),
):
    """Connect a self-hosted OpenAI-compatible server to one or both engines:
    one ``local_endpoint`` subscription row per engine, same credential."""
    _require_admin(user)
    # The local-endpoint route: refused outright on hosted OtoDock, where
    # no engine declares a local provider.
    if config.OTODOCK_CLOUD:
        raise HTTPException(400, _CLOUD_LOCAL_MSG)
    layers = [layer for layer in dict.fromkeys(req.layers) if layer]
    if not layers:
        raise HTTPException(400, "Pick at least one engine for the endpoint")
    bad = [layer for layer in layers if layer not in _layers_with_auth_type("local_endpoint")]
    if bad:
        raise HTTPException(400, f"Local endpoints cannot serve: {', '.join(bad)}")
    # Every named engine must declare the provider as a LOCAL one (a
    # contract test keeps one id's kind and api_path equal across engines).
    entries = [provider_entry(capabilities_for_path(layer), req.provider) for layer in layers]
    if any(e is None or e.get("kind") != PROVIDER_LOCAL for e in entries):
        raise HTTPException(400, f"Invalid local provider: {req.provider}")
    url = subscription_store.normalize_endpoint_url(req.endpoint_url)
    if not url:
        raise HTTPException(400, "endpoint_url required")
    api_path = entries[0].get("api_path") or ""
    if api_path and not url.lower().endswith(api_path):
        # The provider's OpenAI-compatible API answers under this path (Ollama:
        # /v1 — the server root answers only the native API; Discover strips
        # the suffix again to reach /api/tags).
        url += api_path
    key = subscription_store.local_endpoint_group_key(req.provider, url)
    if any(g["group"] == key for g in subscription_store.list_local_endpoint_groups()):
        raise HTTPException(409, "This endpoint is already connected — use its engine checkboxes")
    cred_data: dict = {"endpoint_url": url}
    if req.api_key:
        cred_data["api_key"] = req.api_key
    for layer in layers:
        subscription_store.add_subscription(
            layer=layer, provider=req.provider, auth_type="local_endpoint",
            owner_sub=user.sub, use_personal=True, contribute_platform=True,
            label=req.label, credential_data=cred_data,
        )
    await _local_endpoint_changed(layers)
    return _annotate_local_group(_local_group(key), user)


@router.put("/v1/admin/execution-layers/local-endpoints/{group}")
async def admin_set_local_endpoint_engine(
    group: str,
    req: SetLocalEndpointEngineRequest,
    user: UserContext = Depends(get_current_user),
):
    """Enable or disable the endpoint for one engine. Enabling creates the
    missing sibling row (copying the credential) or re-activates a disabled
    one; disabling sets that engine's row ``disabled`` — its models stay,
    hidden from the pickers while no active row serves their provider."""
    _require_admin(user)
    if req.layer not in _layers_with_auth_type("local_endpoint"):
        raise HTTPException(400, f"Local endpoints cannot serve: {req.layer}")
    g = _local_group(group)
    _require_group_owner(g, user)
    eng = g["engines"].get(req.layer)
    if req.enabled:
        if eng is None:
            sibling = next(iter(g["engines"].values()))
            cred_data = subscription_store.get_credential_data(sibling["id"])
            subscription_store.add_subscription(
                layer=req.layer, provider=g["provider"], auth_type="local_endpoint",
                owner_sub=user.sub, use_personal=True, contribute_platform=True,
                label=g["label"], credential_data=cred_data,
            )
        elif eng["status"] != subscription_status.ACTIVE:
            subscription_store.update_subscription(eng["id"], status=subscription_status.ACTIVE)
    elif eng is not None and eng["status"] == subscription_status.ACTIVE:
        subscription_store.update_subscription(eng["id"], status=subscription_status.DISABLED)
    await _local_endpoint_changed([req.layer])
    return _annotate_local_group(_local_group(group), user)


@router.delete("/v1/admin/execution-layers/local-endpoints/{group}")
async def admin_delete_local_endpoint(
    group: str,
    force: bool = False,
    user: UserContext = Depends(get_current_user),
):
    _require_admin(user)
    g = _local_group(group)
    _require_group_owner(g, user)
    # Same rule as the subscription delete: live bindings decide, the stored
    # counter is reconciled first, and `force` deletes past live sessions
    # (the rebind fan-out re-homes them).
    busy = []
    for layer, e in g["engines"].items():
        _stored, live = subscription_pool.reconcile_active_sessions(e["id"])
        if live > 0:
            busy.append(f"{layer} ({live})")
    if busy and not force:
        raise HTTPException(
            409, f"Endpoint has live sessions on {', '.join(busy)}. Wait for "
            "them to close, or delete with force=true to move them to another "
            "subscription.",
        )
    for e in g["engines"].values():
        subscription_store.delete_subscription(e["id"])
    await _local_endpoint_changed(list(g["engines"]))
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Admin: Models CRUD
# ---------------------------------------------------------------------------

@router.get("/v1/admin/execution-layers/{layer}/models")
async def admin_list_models(
    layer: str,
    user: UserContext = Depends(get_current_user),
):
    _require_admin(user)
    if layer not in _valid_layers():
        raise HTTPException(400, f"Invalid layer: {layer}")
    # Sync builtins from capabilities
    caps = get_all_capabilities().get(layer)
    if caps:
        subscription_store.sync_builtin_models(layer, caps.get("models", []))
    return {"models": subscription_store.list_models(layer=layer)}


@router.post("/v1/admin/execution-layers/{layer}/models")
async def admin_add_model(
    layer: str,
    req: AddModelRequest,
    user: UserContext = Depends(get_current_user),
):
    _require_admin(user)
    if layer not in _valid_layers():
        raise HTTPException(400, f"Invalid layer: {layer}")
    if not req.model_id or not req.display_name:
        raise HTTPException(400, "model_id and display_name required")
    _validate_tier_fields(req.tier, req.good_at)
    model = subscription_store.add_model(
        layer=layer,
        model_id=req.model_id,
        display_name=req.display_name,
        provider=req.provider,
        is_builtin=False,
        context_window=req.context_window,
        pricing_input=req.pricing_input,
        pricing_output=req.pricing_output,
        pricing_cache_write=req.pricing_cache_write,
        pricing_cache_read=req.pricing_cache_read,
        supports_reasoning=req.supports_reasoning,
        supports_xhigh=req.supports_xhigh,
        tier=req.tier,
        good_at=req.good_at,
    )
    return model


@router.post("/v1/admin/execution-layers/{layer}/discover-models")
async def admin_discover_models(
    layer: str,
    req: DiscoverModelsRequest,
    user: UserContext = Depends(get_current_user),
):
    """Discover available models from a provider using subscription credentials."""
    _require_admin(user)
    if layer not in _valid_layers():
        raise HTTPException(400, f"Invalid layer: {layer}")

    sub = subscription_store.get_subscription(req.subscription_id)
    if not sub:
        raise HTTPException(404, "Subscription not found")
    if sub["layer"] != layer:
        raise HTTPException(400, "Subscription does not belong to this layer")

    creds = subscription_store.get_credential_data(req.subscription_id)
    provider = sub["provider"]

    from core.layers.providers import get_adapter
    adapter = get_adapter(provider)

    try:
        models = await adapter.list_available_models(
            api_key=creds.get("api_key", ""),
            endpoint_url=creds.get("endpoint_url"),
        )
    except Exception as e:
        logger.error(f"Model discovery failed for {provider}: {e}")
        raise HTTPException(502, f"Failed to fetch models from {provider}: {e}")

    return {"models": models, "provider": provider}


@router.post("/v1/admin/execution-layers/{layer}/models/bulk")
async def admin_bulk_add_models(
    layer: str,
    req: BulkAddModelsRequest,
    user: UserContext = Depends(get_current_user),
):
    """Add multiple models at once (from discover-models flow)."""
    _require_admin(user)
    if layer not in _valid_layers():
        raise HTTPException(400, f"Invalid layer: {layer}")
    targets = [t for t in dict.fromkeys(req.layers or [layer]) if t]
    bad = [t for t in targets if t not in _valid_layers()]
    if bad:
        raise HTTPException(400, f"Invalid layer: {', '.join(bad)}")

    added = []
    for target in targets:
        for m in req.models:
            model = subscription_store.add_model(
                layer=target,
                model_id=m["model_id"],
                display_name=m["display_name"],
                provider=req.provider,
                is_builtin=False,
            )
            added.append(model)

    return {"models": added, "count": len(added)}


@router.put("/v1/admin/execution-layers/{layer}/models/{model_id}")
async def admin_toggle_model(
    layer: str,
    model_id: int,
    req: UpdateModelRequest,
    user: UserContext = Depends(get_current_user),
):
    _require_admin(user)
    tier_sent = "tier" in req.model_fields_set
    if tier_sent or req.good_at is not None:
        # The registry owns a builtin's tier: the next sync would overwrite
        # an admin edit, so refuse it instead of accepting it silently.
        row = subscription_store.get_model(model_id)
        if not row:
            raise HTTPException(404, "Model not found")
        if row.get("is_builtin"):
            raise HTTPException(
                400, "A builtin model's tier comes with the platform; tag custom models only")
        _validate_tier_fields(req.tier, req.good_at)
    result = subscription_store.update_model(
        model_id,
        enabled=req.enabled,
        context_window=req.context_window,
        pricing_input=req.pricing_input,
        pricing_output=req.pricing_output,
        pricing_cache_write=req.pricing_cache_write,
        pricing_cache_read=req.pricing_cache_read,
        supports_reasoning=req.supports_reasoning,
        supports_xhigh=req.supports_xhigh,
        tier=req.tier if tier_sent else None,
        clear_tier=tier_sent and req.tier is None,
        good_at=req.good_at,
    )
    if not result:
        raise HTTPException(404, "Model not found")
    return result


@router.delete("/v1/admin/execution-layers/{layer}/models/{model_id}")
async def admin_delete_model(
    layer: str,
    model_id: int,
    user: UserContext = Depends(get_current_user),
):
    _require_admin(user)
    deleted = subscription_store.delete_model(model_id)
    if not deleted:
        raise HTTPException(404, "Model not found or is a builtin model")
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Admin: Pool status
# ---------------------------------------------------------------------------

@router.get("/v1/admin/execution-layers/pool-status")
async def admin_pool_status(user: UserContext = Depends(get_current_user)):
    _require_admin(user)
    capabilities = get_all_capabilities()
    return {
        path: subscription_store.get_pool_stats(path)
        for path in capabilities
    }


# ---------------------------------------------------------------------------
# Admin: User platform auth toggle
# ---------------------------------------------------------------------------

@router.put("/v1/admin/users/{user_sub}/platform-auth")
async def admin_set_platform_auth(
    user_sub: str,
    req: SetPlatformAuthRequest,
    user: UserContext = Depends(get_current_user),
):
    _require_admin(user)
    subscription_store.set_user_allow_platform_auth(user_sub, req.allowed)
    subscription_pool.schedule_rebind("platform-auth toggle")
    return {"user_sub": user_sub, "allow_platform_auth": req.allowed}


# ---------------------------------------------------------------------------
# User: Personal subscriptions
# ---------------------------------------------------------------------------

@router.get("/v1/users/me/execution-layers")
async def user_list_layers(user: UserContext | None = Depends(get_current_user)):
    """List execution layers with user's own subscriptions and platform availability."""
    user = require_auth(user)
    capabilities = get_all_capabilities()
    allow_platform = subscription_store.get_user_allow_platform_auth(user.sub)
    layers = []

    for path, caps in capabilities.items():
        user_subs = subscription_store.list_subscriptions(
            layer=path, owner_sub=user.sub, include_disabled=True,
        )
        _attach_windows(user_subs)
        # "Platform available" = the user may borrow a platform API credential here
        # (Platform Auth on AND a borrowable admin sub exists — NOT admin OAuth).
        platform_available = subscription_pool.borrowable_pool_available(path, user.sub)

        layers.append({
            "name": path,
            "display_name": caps.get("display_name", path),
            # The engine's descriptor rides with its row, as it does on the
            # admin tab: the user card reads its vendor, account label, auth
            # types and login flow here instead of comparing the engine id.
            "capabilities": caps,
            "user_subscriptions": user_subs,
            "platform_available": platform_available,
            "allow_platform_auth": allow_platform,
            # Server-computed "can this user run this engine" — the single
            # predicate behind the chat-page engine/model filtering and the
            # cross-engine switch options. Never re-derive client-side.
            "can_run": subscription_pool.user_can_run(path, user.sub),
        })

    return {"layers": layers}


@router.post("/v1/users/me/execution-layers/{layer}/subscriptions")
async def user_add_subscription(
    layer: str,
    req: AddSubscriptionRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """A user's own API key on an engine that has a vendor to hold it for
    (the key is the vendor's). Local endpoints and the multi-provider Direct
    LLM engine are admin surfaces; an OAuth account arrives through its own
    exchange endpoint."""
    user = require_auth(user)
    provider = _user_key_provider(layer)
    if not provider:
        eligible = [p for p in _layers_with_auth_type("api_key") if _user_key_provider(p)]
        raise HTTPException(
            400, f"API keys can be added on {', '.join(eligible) or 'no engine'}",
        )
    if req.auth_type != "api_key":
        raise HTTPException(400, "Only api_key is supported for user subscriptions")
    if req.provider != provider:
        raise HTTPException(400, f"provider must be {provider} for {layer}")
    api_key = (req.api_key or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key is required")

    try:
        sub = subscription_store.add_subscription(
            layer=layer,
            provider=provider,
            auth_type="api_key",
            owner_sub=user.sub,
            use_personal=True if req.use_personal is None else req.use_personal,
            # Only admins may contribute a personal account to the shared platform
            # pool — and for an admin it DEFAULTS ON (so agent-scoped tasks work
            # without the admin knowing to tick it); they can untick to opt out.
            contribute_platform=roles.is_admin(user.role) and (
                True if req.contribute_platform is None else bool(req.contribute_platform)
            ),
            label=req.label,
            credential_data={"api_key": api_key},
        )
    except subscription_store.SubscriptionExists:
        raise HTTPException(409, "You already have an API key on this engine")
    subscription_pool.schedule_rebind("user subscription add")
    return sub


@router.put("/v1/users/me/execution-layers/{layer}/subscriptions/{sub_id}")
async def user_update_subscription(
    layer: str,
    sub_id: str,
    req: UpdateSubscriptionRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Owner-scoped subscription update — every role, own rows only.

    Any owner may rename and toggle
    ``use_personal`` — the per-account "bench this subscription from my own
    sessions for a while" switch (multi-account owners flip between plans
    without disconnecting). ``contribute_platform`` stays ADMIN-only: the
    shared agent pool is an admin surface, mirroring the connect-time rule
    (non-admin connects can never contribute).
    """
    user = require_auth(user)
    sub = subscription_store.get_subscription(sub_id)
    if not sub or sub.get("owner_sub") != user.sub:
        raise HTTPException(404, "Subscription not found")
    if req.contribute_platform is not None and not roles.is_admin(user.role):
        raise HTTPException(403, "Only admins can change agent-pool contribution")
    updated = subscription_store.update_subscription(
        sub_id,
        label=req.label,
        use_personal=req.use_personal,
        contribute_platform=req.contribute_platform,
    )
    # Live sessions follow the checkbox: benching this account re-homes its
    # bound sessions onto the remaining selection right away.
    subscription_pool.schedule_rebind("user subscription update")
    return updated


@router.delete("/v1/users/me/execution-layers/{layer}/subscriptions/{sub_id}")
async def user_delete_subscription(
    layer: str,
    sub_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    user = require_auth(user)
    # Verify ownership
    sub = subscription_store.get_subscription(sub_id)
    if not sub or sub.get("owner_sub") != user.sub:
        raise HTTPException(404, "Subscription not found")
    subscription_store.delete_subscription(sub_id)
    subscription_pool.schedule_rebind("user subscription delete")
    return {"deleted": True}
