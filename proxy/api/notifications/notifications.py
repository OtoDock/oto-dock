"""Notification REST API.

Consumed by the React dashboard and the notifications-mcp server.
Auth: API key (server-to-server) OR OAuth2 session cookie (dashboard users).
"""

import asyncio
import logging
import zoneinfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

import config
from storage import notification_store
from services.notifications import notification_manager
from core.session.session_state import get_user_tz
from auth.providers import (
    UserContext,
    get_current_user,
    require_auth,
)

logger = logging.getLogger("claude-proxy.notification-api")
router = APIRouter()


# --- Request models ---


class CreateNotificationRequest(BaseModel):
    title: str
    body: str
    severity: str = "info"
    scope: str = "user"
    target: str | None = None
    notification_type: str = "one_time"  # one_time | recurring
    schedule: str | None = None           # cron expression for recurring
    run_at: str | None = None             # ISO datetime for one_time
    # Recurring every N seconds. Mutually exclusive with schedule and run_at.
    interval_seconds: int | None = None
    source: str = "mcp"
    agent_slug: str | None = None         # originating agent (for deep linking)
    chat_id: str | None = None            # originating chat/task (for deep linking)
    # IANA timezone snapshot. Usually proxy-resolved from the calling session
    # (browser-detected via client_info). Callers may override explicitly.
    user_tz: str | None = None


class PushSubscribeRequest(BaseModel):
    platform: str  # "web" | "android"
    subscription_data: str  # JSON string (Web Push) or FCM token


def _validate_user_tz(value: str | None) -> str | None:
    if not value:
        return value
    try:
        zoneinfo.ZoneInfo(value)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid user_tz: {e}")
    return value


def _resolve_user_tz(
    explicit: str | None, user: UserContext, on_behalf: str | None,
) -> str | None:
    if explicit:
        return _validate_user_tz(explicit)
    if on_behalf:
        return get_user_tz(on_behalf)
    if user and not user.is_api_key:
        return get_user_tz(user.sub)
    return None


# --- Scope enforcement ---


def _enforce_scope(user: UserContext, scope: str, agent: str | None = None) -> None:
    """Check that the user has permission to create notifications with this scope.

    Editor + manager + admin can create agent-scope notifications
    (collaborative workspace tier). Mutation rules in PATCH/DELETE handlers
    below restrict editors to their own creations.
    """
    # Visibility-modes: reject a session scope the agent's mode doesn't offer
    # (Personal-only → no "agent"; Shared-only → no "user"). "global" is an
    # admin broadcast, not a mode scope — it passes through to its own check.
    if scope in ("user", "agent") and agent:
        from core.session.visibility import available_scopes_for
        from storage import agent_store as _as
        _row = _as.get_agent(agent) or {}
        _avail = available_scopes_for(
            bool(_row.get("collaborative", True)), _row.get("default_scope") or "user",
        )
        if scope not in _avail:
            raise HTTPException(
                status_code=400,
                detail=f"This agent does not support {scope!r}-scoped "
                       f"notifications (mode offers: {', '.join(_avail)})",
            )
    if scope == "user":
        return  # any authenticated user
    elif scope == "agent":
        # A no-user / service session legitimately creates agent-scope work for
        # its own agent; a real user (cookie or USER_SESSION) is gated by role.
        if user.is_no_user_session or user.is_service:
            return
        if agent and not user.can_edit_agent(agent):
            raise HTTPException(
                status_code=403,
                detail="Agent-scoped notifications require editor, manager, or admin role for this agent",
            )
        elif not agent and not user.can_manage_tasks():
            raise HTTPException(
                status_code=403,
                detail="Agent-scoped notifications require manager or admin role",
            )
    elif scope == "global":
        if not user.is_admin and not user.is_service:
            raise HTTPException(
                status_code=403,
                detail="Global notifications require admin role",
            )
    else:
        raise HTTPException(status_code=400, detail=f"Invalid scope: {scope}")


# --- Notification CRUD ---


@router.post("/v1/notifications")
async def create_notification(
    req: CreateNotificationRequest,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="X-Agent-Name"),
):
    u = require_auth(user)
    _enforce_scope(u, req.scope)

    acting = u.acting_sub

    # User-scope recipient is token-authoritative. A no-user session
    # (phone/agent service) has no identity and cannot create user-scoped
    # notifications. An agent acting as a real user can only notify THAT user
    # (client-supplied target ignored). The master key must name a user.
    target = req.target
    if req.scope == "user":
        if u.is_no_user_session:
            raise HTTPException(
                status_code=403,
                detail="This session has no user identity and cannot create "
                       "user-scoped notifications.",
            )
        if acting is not None:
            if u.is_api_key:
                target = acting  # real-user session → self; ignore client target
            elif not target:
                target = acting  # dashboard user default
            elif not u.is_admin:
                # Cross-user targeting is admin-only: a non-admin dashboard
                # user naming another user would otherwise aim (e.g. a
                # danger-severity recurring) notification at them.
                target = acting
        elif not target:
            raise HTTPException(
                status_code=400,
                detail="User-scoped notifications via the master key must specify target",
            )

    # Resolve username to sub if target looks like a username (not a long hash)
    if target and req.scope == "user" and len(target) < 30:
        resolved = await asyncio.to_thread(notification_store.resolve_username_to_sub, target)
        if not resolved:
            raise HTTPException(status_code=404, detail=f"User '{target}' not found")
        target = resolved

    # Validate type-specific fields
    if req.notification_type == "recurring":
        if not req.schedule and req.interval_seconds is None:
            raise HTTPException(
                status_code=400,
                detail="Recurring notifications require schedule or interval_seconds",
            )
        if req.schedule and req.interval_seconds is not None:
            raise HTTPException(
                status_code=400,
                detail="schedule and interval_seconds are mutually exclusive",
            )
    if req.notification_type == "one_time" and not req.run_at and req.source != "mcp":
        # MCP-created one-time without run_at = fire immediately
        pass
    if req.interval_seconds is not None:
        from services.scheduler.scheduler import _validate_interval_seconds
        err = _validate_interval_seconds(req.interval_seconds)
        if err:
            raise HTTPException(status_code=400, detail=err)

    user_tz = _resolve_user_tz(req.user_tz, u, acting)
    notif = await asyncio.to_thread(
        notification_store.create_notification,
        title=req.title,
        body=req.body,
        severity=req.severity,
        scope=req.scope,
        target=target,
        source=req.source,
        notification_type=req.notification_type,
        schedule=req.schedule,
        run_at=req.run_at,
        interval_seconds=req.interval_seconds,
        created_by=acting if acting is not None else (x_agent_name or u.agent or "api"),
        agent_slug=req.agent_slug,
        chat_id=req.chat_id,
        user_tz=user_tz,
    )

    # If it has a schedule, register with APScheduler
    if req.schedule or req.run_at or req.interval_seconds is not None:
        notification_manager.schedule_new_notification(notif)
    else:
        # Fire immediately — include deep link context for navigation
        await notification_manager.fire_notification(
            title=req.title,
            body=req.body,
            severity=req.severity,
            scope=req.scope,
            target=target,
            source=req.source,
            notification_id=notif["id"],
            agent_slug=req.agent_slug,
            chat_id=req.chat_id,
        )

    return {"status": "created", "notification": notif}


@router.get("/v1/notifications")
async def list_notifications(
    scope: str | None = Query(None),
    source: str | None = Query(None),
    agent: str | None = Query(None),
    audit: bool = Query(False),
    view: str | None = Query(None),
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="X-Agent-Name"),
):
    u = require_auth(user)

    # Definitions view: list notification definitions (for agent settings page).
    # enabled_only=False so paused notifications appear in the list (they
    # can be resumed). The default has been flipped, but we pass it explicitly
    # to make the intent clear.
    if view == "definitions" or u.is_api_key:
        notifications = await asyncio.to_thread(
            notification_store.list_notifications,
            scope=scope,
            source=source,
            enabled_only=False,
        )

        # USER-VIEW vs ADMIN-AUDIT (consistent with /v1/tasks, /v1/triggers,
        # /v1/tasks/runs). Agent-scoped + global notifications are shared; a
        # user's user-scoped notifications are private to them. The admin AUDIT
        # surface (``audit=true``, admin only — the admin Notifications page) sees
        # every user's notifications so it can audit; every other caller —
        # INCLUDING an admin on an agent's settings tab — gets the user-view (own
        # user-scoped + agent-scoped + global). ``agent`` narrows the agent-scoped
        # items in user-view.
        audit_view = audit and u.is_admin
        if not u.is_api_key and not audit_view:
            filtered = []
            for n in notifications:
                if n["scope"] == "user" and n.get("target") == u.sub:
                    filtered.append(n)
                elif n["scope"] == "agent" and (
                    n.get("target") == agent if agent
                    else u.can_access_agent(n.get("target", ""))
                ):
                    filtered.append(n)
                elif n["scope"] == "global":
                    filtered.append(n)
            notifications = filtered

        # Hide one-time notifications that have already fired
        notifications = [
            n for n in notifications
            if not (n["notification_type"] == "one_time" and n.get("fired_count", 0) > 0)
        ]

        # Add metadata: permissions + human-readable target names.
        # 3-tier model:
        #   - User-scope notif: own creator can mutate.
        #   - Agent-scope notif: owner (manager/admin) can mutate any;
        #     editor can mutate only their own (created_by == self).
        for n in notifications:
            is_own_target = n.get("target") == u.sub  # user-scope ownership
            is_own_creator = n.get("created_by") == u.sub
            is_static = n.get("source") == "static"
            # Per-agent: resolve can_manage / can_edit based on the
            # notification's agent (only relevant for agent-scope).
            n_agent = n.get("target") if n["scope"] == "agent" else None
            can_manage = u.is_admin or (n_agent and u.can_manage_agent(n_agent))
            can_edit = u.is_admin or (n_agent and u.can_edit_agent(n_agent))
            can_mutate_agent_scope = can_manage or (can_edit and is_own_creator)
            can_mutate = (
                is_own_target  # user-scope: own
                or (n["scope"] == "agent" and can_mutate_agent_scope)
            )
            n["can_delete"] = not is_static and can_mutate and not u.is_api_key
            n["can_fire"] = can_mutate or u.is_api_key
            # Pause/resume share the same authority as delete (only dynamic
            # notifications); the available action depends on enabled state.
            is_enabled = bool(n.get("enabled", True))
            n["can_pause"] = n["can_delete"] and is_enabled
            n["can_resume"] = n["can_delete"] and not is_enabled
            # Resolve target sub to username for display
            if n.get("target") and n["scope"] == "user":
                resolved_name = notification_store.resolve_sub_to_username(n["target"])
                n["target_name"] = resolved_name or n["target"][:12]
            else:
                n["target_name"] = n.get("target")

        return {"notifications": notifications}

    # Default: list user's deliveries (inbox)
    deliveries = await asyncio.to_thread(
        notification_store.list_deliveries,
        user_sub=u.sub,
    )
    return {"deliveries": deliveries}


@router.get("/v1/notifications/unread-count")
async def get_unread_count(
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    count = await asyncio.to_thread(notification_store.get_unread_count, u.sub)
    return {"count": count}


@router.post("/v1/notifications/{notification_id}/fire")
async def fire_notification_now(
    notification_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Fire an existing notification immediately (for testing)."""
    u = require_auth(user)

    notif = await asyncio.to_thread(
        notification_store.get_notification, notification_id
    )
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")

    # Permission check: own creator OR agent owner (manager/admin) OR
    # agent editor on own creation. API key bypasses.
    n_agent = notif.get("target") if notif["scope"] == "agent" else None
    is_own = notif.get("created_by") == u.sub
    can_manage = u.is_admin or (n_agent and u.can_manage_agent(n_agent))
    can_edit_own = (
        n_agent and u.can_edit_agent(n_agent) and is_own
    )
    if not (is_own or can_manage or can_edit_own or u.is_api_key):
        raise HTTPException(status_code=403, detail="Not authorized")

    deliveries = await notification_manager.fire_notification(
        title=notif["title"],
        body=notif["body"],
        severity=notif["severity"],
        scope=notif["scope"],
        target=notif.get("target"),
        source=notif["source"],
        notification_id=notification_id,
    )
    return {"status": "fired", "targets": len(deliveries)}


def _check_notification_authority(notif: dict, user: UserContext) -> None:
    """Permission gate shared by pause/resume/delete (3-tier model):
    creator, admin, agent manager (any), agent editor (only own), or API key.
    """
    n_agent = notif.get("target") if notif["scope"] == "agent" else None
    is_own = notif.get("created_by") == user.sub
    can_manage = user.is_admin or (n_agent and user.can_manage_agent(n_agent))
    can_edit_own = n_agent and user.can_edit_agent(n_agent) and is_own
    if not (is_own or can_manage or can_edit_own or user.is_api_key):
        raise HTTPException(status_code=403, detail="Not authorized for this notification")


@router.post("/v1/notifications/{notification_id}/pause")
async def pause_notification(
    notification_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Pause: set enabled=FALSE and unregister the APScheduler job.

    The notification stays in the system and can be resumed later. For
    permanent removal, use DELETE.
    """
    u = require_auth(user)
    notif = await asyncio.to_thread(
        notification_store.get_notification, notification_id,
    )
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    _check_notification_authority(notif, u)
    await notification_manager.pause_notification(notification_id)
    return {"status": "paused", "notification_id": notification_id}


@router.post("/v1/notifications/{notification_id}/resume")
async def resume_notification(
    notification_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Resume: set enabled=TRUE and re-register the APScheduler job.

    For one-time notifications whose ``run_at`` has passed, the row stays
    enabled but no job is scheduled — the user can press 'Fire Now' to
    fire manually.
    """
    u = require_auth(user)
    notif = await asyncio.to_thread(
        notification_store.get_notification, notification_id,
    )
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    _check_notification_authority(notif, u)
    await notification_manager.resume_notification(notification_id)
    return {"status": "resumed", "notification_id": notification_id}


class EditNotificationRequest(BaseModel):
    title: str | None = None
    body: str | None = None
    severity: str | None = None
    schedule: str | None = None
    run_at: str | None = None
    interval_seconds: int | None = None
    notification_type: str | None = None
    user_tz: str | None = None


async def _edit_notification_impl(
    notification_id: str, req: EditNotificationRequest, user: UserContext,
):
    notif = await asyncio.to_thread(
        notification_store.get_notification, notification_id,
    )
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    _check_notification_authority(notif, user)

    fields = req.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(
            status_code=400,
            detail="At least one editable field must be provided",
        )
    non_null_timing = sum(
        1 for k in ("schedule", "run_at", "interval_seconds") if fields.get(k)
    )
    if non_null_timing > 1:
        raise HTTPException(
            status_code=400,
            detail="schedule, run_at, and interval_seconds are mutually exclusive — set only one",
        )

    ok, err = await notification_manager.update_notification(
        notification_id, fields,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    if not ok:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"status": "updated", "notification_id": notification_id}


@router.patch("/v1/notifications/{notification_id}")
async def edit_notification(
    notification_id: str,
    req: EditNotificationRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Partial update of a notification definition. See ``EditNotificationRequest`` for editable fields."""
    u = require_auth(user)
    return await _edit_notification_impl(notification_id, req, u)


@router.post("/v1/notifications/{notification_id}/edit")
async def edit_notification_post(
    notification_id: str,
    req: EditNotificationRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """POST alias for PATCH (avoids IPS rules that block PATCH)."""
    u = require_auth(user)
    return await _edit_notification_impl(notification_id, req, u)


@router.delete("/v1/notifications/{notification_id}")
async def delete_notification(
    notification_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Hard-delete a notification definition. Cannot be undone."""
    u = require_auth(user)

    notif = await asyncio.to_thread(
        notification_store.get_notification, notification_id
    )
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")

    _check_notification_authority(notif, u)

    deleted = await notification_manager.delete_notification(notification_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"status": "deleted"}


# --- Delivery management (inbox) ---


@router.patch("/v1/notifications/deliveries/{delivery_id}/read")
async def mark_delivery_read(
    delivery_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    ok = await asyncio.to_thread(notification_store.mark_read, delivery_id, u.sub)
    if not ok:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return {"status": "read"}


@router.patch("/v1/notifications/deliveries/{delivery_id}/dismiss")
async def dismiss_delivery(
    delivery_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    ok = await asyncio.to_thread(notification_store.dismiss, delivery_id, u.sub)
    if not ok:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return {"status": "dismissed"}


@router.post("/v1/notifications/mark-all-read")
async def mark_all_read(
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    count = await asyncio.to_thread(notification_store.mark_all_read, u.sub)
    return {"status": "ok", "count": count}


@router.post("/v1/notifications/dismiss-all")
async def dismiss_all(
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    count = await asyncio.to_thread(notification_store.dismiss_all, u.sub)
    return {"status": "ok", "count": count}


# --- Push subscription management ---


@router.post("/v1/push/subscribe")
async def push_subscribe(
    req: PushSubscribeRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    if req.platform not in ("web", "android"):
        raise HTTPException(status_code=400, detail="Platform must be 'web' or 'android'")

    sub = await asyncio.to_thread(
        notification_store.save_push_subscription,
        user_sub=u.sub,
        platform=req.platform,
        subscription_data=req.subscription_data,
    )
    return {"status": "subscribed", "subscription": sub}


@router.get("/v1/app/identity")
async def app_identity(user: UserContext | None = Depends(get_current_user)):
    """This proxy's stable install id (the relay identity), used by the Android
    app to bind each installation and route multi-installation notifications +
    OAuth deep-links to the matching server. Auth-gated; returns nothing else."""
    require_auth(user)
    from services.billing.relay_client import get_install_id
    return {"install_id": get_install_id()}


@router.delete("/v1/push/unsubscribe")
async def push_unsubscribe(
    subscription_data: str = Query(...),
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    ok = await asyncio.to_thread(
        notification_store.delete_push_subscription_by_data, subscription_data, u.sub
    )
    return {"status": "unsubscribed" if ok else "not_found"}


@router.get("/v1/push/vapid-public-key")
async def get_vapid_public_key(
    user: UserContext | None = Depends(get_current_user),
):
    require_auth(user)
    key = getattr(config, "VAPID_PUBLIC_KEY", "")
    if not key:
        raise HTTPException(status_code=404, detail="VAPID not configured")
    return {"key": key}
