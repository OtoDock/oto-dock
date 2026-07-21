"""Webhook subscriptions REST API.

CRUD over ``webhook_subscriptions`` rows + vendor-target catalog lookup.
The receive surface lives in ``api/events/webhooks.py``; this module is the
*management* surface used by the dashboard's Subscribe-to-events modal,
admin pages, and the future MCP read-only list_subscriptions tool.

Permission model:
  * User-scope subscriptions: caller must == ``owner`` (subscription
    creator); admin can manage anyone's.
  * Service-scope subscriptions: caller must have manager role on the
    bound agent; admin can manage all.
  * ``X-On-Behalf-Of`` is rejected with 400 — subscriptions are user- or
    manager-driven, never agent-driven (no agent SHOULD be creating
    vendor-side state behind a human's back in v1).
"""

from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

import config
from auth.providers import UserContext, get_current_user, require_auth
from services.mcp import mcp_registry
from services.webhooks import subscription_manager
from storage import webhook_subscription_store

logger = logging.getLogger("claude-proxy.api.subscriptions")
router = APIRouter()


# =====================================================================
# Pydantic models
# =====================================================================


class CreateSubscriptionRequest(BaseModel):
    scope: str  # 'user' | 'service'
    mcp_name: str
    account_label: str
    vendor_target: str
    selected_events: list[str]
    selected_subevents: dict[str, list[str]] | None = None
    agent: str | None = None  # required when scope='service'


# =====================================================================
# Permission helpers
# =====================================================================


def _reject_on_behalf(on_behalf: str | None) -> None:
    if on_behalf:
        raise HTTPException(
            400,
            "Subscriptions cannot use X-On-Behalf-Of — they are user- or "
            "manager-driven (no agent should be creating vendor-side state).",
        )


def _can_manage_subscription(row: dict, user: UserContext) -> bool:
    if user.is_admin:
        return True
    if user.is_service:
        # The trusted master key (service-to-service) is admin-equivalent here.
        # A session token is NOT — it must resolve a real scope/owner below.
        return True
    scope = row.get("scope")
    if scope == "user":
        return row.get("owner") == user.sub
    if scope == "service":
        return user.can_manage_agent(row.get("agent", ""))
    return False


def _can_view_subscription(row: dict, user: UserContext) -> bool:
    if user.is_admin or user.is_service:
        return True
    scope = row.get("scope")
    if scope == "user":
        return row.get("owner") == user.sub
    if scope == "service":
        agent = row.get("agent", "")
        return user.can_access_agent(agent) if agent else False
    return False


def _enforce_create_permission(
    *, scope: str, agent: str | None, user: UserContext,
) -> str:
    """Return the resolved ``owner`` user_sub for the new row."""
    if scope == "user":
        if not user.sub:
            raise HTTPException(403, "User-scope subscriptions require a logged-in user")
        return user.sub
    if scope == "service":
        if not agent:
            raise HTTPException(
                400, "Service-scope subscriptions require an `agent` field"
            )
        if not user.can_manage_agent(agent):
            raise HTTPException(
                403,
                "Service-scope subscriptions require manager+ role on the agent",
            )
        # Visibility-mode gate (mirrors api/events/triggers.py + api/tasks/tasks.py): a
        # service-scope subscription runs in the agent's AGENT scope, so the
        # agent's mode must offer it. Personal-only agents (user-scope only)
        # cannot have agent-scope subscriptions; Shared-only / agent-default
        # agents can. Closes the orphan-subscription gap.
        from core.session.visibility import available_scopes_for
        from storage import agent_store as _as
        _row = _as.get_agent(agent) or {}
        _avail = available_scopes_for(
            bool(_row.get("collaborative", True)), _row.get("default_scope") or "user",
        )
        if "agent" not in _avail:
            raise HTTPException(
                400,
                f"Agent {agent!r} does not support agent-scope subscriptions "
                f"(mode offers: {', '.join(_avail)})",
            )
        return user.sub  # creator's sub stored in created_by; owner='' for service rows
    raise HTTPException(400, f"Invalid scope: {scope!r}")


# =====================================================================
# Routes
# =====================================================================


@router.post("/v1/subscriptions")
async def create_subscription(
    body: CreateSubscriptionRequest,
    user: UserContext | None = Depends(get_current_user),
    x_on_behalf_of: str | None = Header(default=None),
):
    """Create + auto-register a vendor webhook subscription."""
    u = require_auth(user)
    _reject_on_behalf(x_on_behalf_of)
    creator_sub = _enforce_create_permission(
        scope=body.scope, agent=body.agent, user=u,
    )
    try:
        row = await subscription_manager.create_subscription(
            user_sub=creator_sub,
            scope=body.scope,
            agent=body.agent,
            mcp_name=body.mcp_name,
            account_label=body.account_label,
            vendor_target=body.vendor_target,
            selected_events=body.selected_events,
            selected_subevents=body.selected_subevents or {},
            caller_is_admin=bool(u.is_admin or u.is_api_key),
        )
    except subscription_manager.SubscriptionScopeError as e:
        raise HTTPException(
            e.status,
            detail={
                "error": "missing_scopes",
                "message": str(e),
                "required": e.required_scopes,
                "action": "reconnect",
                **(e.detail or {}),
            },
        )
    except subscription_manager.SubscriptionError as e:
        raise HTTPException(e.status, detail={"message": str(e), **(e.detail or {})})
    return row


@router.get("/v1/subscriptions")
async def list_subscriptions(
    scope: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    mcp_name: str | None = Query(default=None),
    provider_id: str | None = Query(default=None),
    account_label: str | None = Query(default=None),
    user: UserContext | None = Depends(get_current_user),
):
    """List subscriptions visible to the caller.

    Filters: scope (user|service), agent, mcp_name, provider_id, account_label.
    Admin sees everything; non-admin sees own user-scope + service-scope on
    accessible agents.
    """
    u = require_auth(user)
    if u.is_admin or u.is_api_key:
        rows = webhook_subscription_store.list_subscriptions(
            scope=scope, agent=agent, mcp_name=mcp_name, provider_id=provider_id,
            account_label=account_label,
        )
    else:
        rows = webhook_subscription_store.list_subscriptions_for_user_view(
            user_sub=u.sub, agent=agent,
        )
        if scope:
            rows = [r for r in rows if r.get("scope") == scope]
        if mcp_name:
            rows = [r for r in rows if r.get("mcp_name") == mcp_name]
        if provider_id:
            rows = [r for r in rows if r.get("provider_id") == provider_id]
        if account_label is not None:
            rows = [r for r in rows if r.get("account_label") == account_label]
        # Filter service-scope rows by manageable agents (view ≠ manage; we
        # still SHOW agent-scope rows for any agent the user can access).
        rows = [r for r in rows if _can_view_subscription(r, u)]
    return {"subscriptions": rows}


@router.get("/v1/subscriptions/{subscription_id}")
async def get_subscription(
    subscription_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    row = webhook_subscription_store.get_subscription(subscription_id)
    if not row:
        raise HTTPException(404, "Subscription not found")
    if not _can_view_subscription(row, u):
        raise HTTPException(403, "Access denied")
    return row


@router.delete("/v1/subscriptions/{subscription_id}")
async def delete_subscription(
    subscription_id: str,
    user: UserContext | None = Depends(get_current_user),
    x_on_behalf_of: str | None = Header(default=None),
):
    u = require_auth(user)
    _reject_on_behalf(x_on_behalf_of)
    row = webhook_subscription_store.get_subscription(subscription_id)
    if not row:
        raise HTTPException(404, "Subscription not found")
    if not _can_manage_subscription(row, u):
        raise HTTPException(403, "Access denied")
    deleted = await subscription_manager.delete_subscription(
        subscription_id=subscription_id,
    )
    return {"deleted": bool(deleted)}


@router.post("/v1/subscriptions/{subscription_id}/renew")
async def renew_subscription(
    subscription_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    row = webhook_subscription_store.get_subscription(subscription_id)
    if not row:
        raise HTTPException(404, "Subscription not found")
    if not _can_manage_subscription(row, u):
        raise HTTPException(403, "Access denied")
    try:
        updated = await subscription_manager.renew_subscription(subscription_id)
    except subscription_manager.SubscriptionError as e:
        raise HTTPException(e.status, detail={"message": str(e), **(e.detail or {})})
    return updated or {"error": "not_renewable"}


@router.get("/v1/mcps/{mcp_name}/webhook-event-catalog")
async def get_event_catalog(
    mcp_name: str,
    account_label: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    user: UserContext | None = Depends(get_current_user),
):
    """Return the manifest's ``event_catalog`` for the Subscribe modal.

    ``admin_only`` entries are filtered server-side for non-admins (the
    modal needs no admin awareness); ``delivery`` rides through for a badge.
    With ``account_label`` + ``scope`` the response also carries the
    EFFECTIVE registration mode (relay / auto / manual — relay when hosted
    event delivery applies to that account) and a ``vendor_target_prefill``
    from the bound account's extra (``vendor_target_spec.account_extra_key``,
    slack: ``team_id``).
    """
    u = require_auth(user)
    manifest = mcp_registry.get_manifest(mcp_name)
    if not manifest:
        raise HTTPException(404, f"MCP not found: {mcp_name}")
    webhooks = (manifest.credentials.webhooks or {}) if manifest.credentials else {}
    if not webhooks.get("available", False):
        raise HTTPException(404, f"MCP {mcp_name!r} does not support webhooks")
    catalog = webhooks.get("event_catalog", [])
    if not (u.is_admin or u.is_api_key):
        catalog = [e for e in catalog if not e.get("admin_only")]

    reg_block = webhooks.get("registration", {}) or {}
    effective_mode = reg_block.get("mode", "manual")
    vendor_target_prefill = ""
    if account_label and scope in ("user", "service"):
        owner = (u.sub or "") if scope == "user" else ""
        with contextlib.suppress(Exception):
            effective_mode = subscription_manager.resolve_effective_registration_mode(
                mcp_name=mcp_name, scope=scope, owner=owner, agent=agent,
                account_label=account_label,
            )
        extra_key = (webhooks.get("vendor_target_spec") or {}).get(
            "account_extra_key", "")
        if extra_key:
            try:
                extra = subscription_manager._resolve_account_extra(
                    provider_id=webhooks.get("provider_id", ""), scope=scope,
                    owner=owner, agent=agent, mcp_name=mcp_name,
                    account_label=account_label,
                )
                vendor_target_prefill = str(extra.get(extra_key, "") or "")
            except Exception:
                vendor_target_prefill = ""

    return {
        "provider_id": webhooks.get("provider_id", ""),
        "event_catalog": catalog,
        "vendor_target_spec": webhooks.get("vendor_target_spec", {}),
        "webhook_base": (config.DASHBOARD_PUBLIC_URL or "").rstrip("/"),
        "registration": {
            "mode": effective_mode,
            "manual_instructions_url": reg_block.get(
                "manual_instructions_url", ""),
        },
        "per_subscription_secret": bool(
            (webhooks.get("signature") or {}).get(
                "per_subscription_secret", False)),
        "vendor_target_prefill": vendor_target_prefill,
    }


@router.get("/v1/subscriptions/{subscription_id}/signing-secret")
async def get_subscription_signing_secret(
    subscription_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Reveal the PER-SUBSCRIPTION signing secret (manual-mode vendors need
    it pasted into the vendor console next to the webhook URL). Manage-gated;
    404 for manifests using a platform-wide secret (nothing to reveal)."""
    u = require_auth(user)
    row = webhook_subscription_store.get_subscription(subscription_id)
    if not row:
        raise HTTPException(404, "Subscription not found")
    if not _can_manage_subscription(row, u):
        raise HTTPException(403, "Access denied")
    manifest = mcp_registry.get_manifest(row["mcp_name"])
    webhooks = (
        (manifest.credentials.webhooks or {})
        if manifest and manifest.credentials else {}
    )
    if not (webhooks.get("signature") or {}).get("per_subscription_secret", False):
        raise HTTPException(404, "This vendor uses a platform-wide secret")
    secret = webhook_subscription_store.get_signing_secret(subscription_id)
    return {"signing_secret": secret or ""}
