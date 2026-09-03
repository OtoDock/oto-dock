"""Triggers REST API.

Two surfaces:

1. **Webhook fire** — external systems POST to scoped URLs with a Bearer
   key. These live under ``/v1/webhooks/`` (NOT ``/v1/triggers/``) so they
   share the same reverse-proxy bypass as vendor-subscribed webhooks
   (one prefix, one auth-gate bypass):

   - ``POST /v1/webhooks/agent/{agent}/{slug}``  — agent-scoped, requires
     ``agent_api_keys`` row matching the URL's agent.
   - ``POST /v1/webhooks/user/{username}/{slug}`` — user-scoped, requires
     ``user_api_keys`` row matching the URL's user.

   Master ``PROXY_API_KEY`` is REJECTED — see services/infra/api_key_manager.py.

2. **Internal CRUD** — session/API-key authenticated:

   - ``POST /v1/triggers``                     — create
   - ``GET  /v1/triggers``                     — list (scope-filtered)
   - ``GET  /v1/triggers/{id}``                — detail
   - ``PATCH /v1/triggers/{id}``               — edit
   - ``POST /v1/triggers/{id}/edit``           — POST alias for PATCH
   - ``DELETE /v1/triggers/{id}``              — hard delete
   - ``POST /v1/triggers/{id}/pause``          — flip enabled=FALSE
   - ``POST /v1/triggers/{id}/resume``         — flip enabled=TRUE
   - ``POST /v1/triggers/{id}/fire``           — internal fire test (no Bearer)

All triggers live in the DB. Provenance (dashboard vs MCP) is no longer
tracked at the column level — it has no UX value and the absence/presence
of ``subscription_id`` is the meaningful provenance signal (vendor vs
generic webhook).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from storage import trigger_store
from storage import database as task_store
from storage import notification_store
from services.scheduler import trigger_manager
from services.infra import api_key_manager
from auth.providers import UserContext, get_current_user, require_auth
from core.session.visibility import nouser_read_targets

logger = logging.getLogger("claude-proxy.triggers")
router = APIRouter()


def _webhook_throttle(key: str, bucket: str = "webhook") -> None:
    """Apply a rate-limit bucket to ``key`` → 429 if over. The ``webhook`` bucket
    (generous) caps a single trigger's fire rate; ``webhook_auth`` (strict) caps
    per-IP key brute-forcing."""
    from auth import rate_limiter
    ok, retry_after = rate_limiter.hit(bucket, key)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={"Retry-After": str(retry_after)},
        )


def _webhook_auth_failed(request: Request) -> None:
    """Record + throttle a failed webhook auth by source IP (strict bucket), then
    raise 403 (or 429 once the IP trips the limit) — so a leaked-URL brute force
    is rate-limited instead of unbounded."""
    from auth.lan_check import get_client_ip
    _webhook_throttle(f"ip:{get_client_ip(request)}", bucket="webhook_auth")
    raise HTTPException(403, "Forbidden")


# =====================================================================
# Permission helpers
# =====================================================================


def _can_manage_trigger(trigger: dict, user: UserContext) -> bool:
    """Return True if the user can mutate (edit/pause/resume/delete) this trigger.

    3-tier model:
      - Agent-scoped: manager (any) or editor (own only).
      - User-scoped: creator only (or admin).

    Only the master key bypasses (``is_service``). A session JWT is
    api-key-shaped but carries a real (or no-user) identity: a user-backed
    session resolves the 3-tier model like a cookie caller, and a no-user
    session may manage ONLY its own agent's agent-scope triggers.
    """
    if user.is_admin or user.is_service:
        return True
    if user.is_no_user_session:
        return (trigger.get("scope") == "agent"
                and trigger.get("agent") == user.agent)
    scope = trigger.get("scope")
    if scope == "agent":
        if user.can_manage_agent(trigger["agent"]):
            return True  # owner: any
        if user.can_edit_agent(trigger["agent"]) and trigger.get("created_by") == user.sub:
            return True  # editor: only own
        return False
    if scope == "user":
        return trigger.get("created_by") == user.sub
    return False


def _can_view_trigger(trigger: dict, user: UserContext) -> bool:
    """Return True if the user can see this trigger (lists, detail).

    Keyed on ``is_service``, not ``is_api_key`` — a session JWT must stay
    inside its identity's reach (keying on ``is_api_key`` let any agent
    session read AND test-fire any trigger by id). A NO-USER session
    additionally sees the AGENT-SCOPE triggers of its delegation targets —
    but test-fire has its own no-user own-agent pin (firing makes the
    target's agent RUN; the edge is read-only)."""
    if user.is_admin or user.is_service:
        return True
    if not user.can_access_agent(trigger["agent"]):
        if not (trigger.get("scope") == "agent"
                and trigger["agent"] in nouser_read_targets(user)):
            return False
    scope = trigger.get("scope")
    if scope == "agent":
        return True
    if scope == "user":
        return trigger.get("created_by") == user.sub
    return False


def _check_trigger_mutation_authority(trigger: dict, user: UserContext) -> None:
    """Enforce mutation rights for api-key callers using the AUTHENTICATED
    identity (never a client header).

      - master key: full service-to-service access.
      - no-user session (phone/agent service): DENIED on user-scoped triggers
        (no identity); may manage agent-scope triggers on its agent.
      - real-user-backed session token: user-scope → only the creator;
        agent-scope → admin/manager any, editor only own.
    """
    acting = user.acting_sub
    if acting is None:
        if user.is_service:
            return  # master key: full s2s
        # No-user session: agent-scope management on ITS OWN agent only.
        if trigger.get("scope") == "user":
            raise HTTPException(
                403,
                "This session has no user identity and cannot manage "
                "user-scoped triggers.",
            )
        if trigger.get("agent") != user.agent:
            raise HTTPException(
                403,
                "This session can only manage its own agent's triggers "
                "(to change another agent, delegate to it).",
            )
        return
    scope = trigger.get("scope")
    if scope == "user":
        if trigger.get("created_by") != acting:
            raise HTTPException(
                403, "Cannot manage another user's trigger",
            )
    elif scope == "agent":
        acting_user = task_store.get_user(acting)
        if acting_user and acting_user.get("role") == "admin":
            return  # platform admin: any
        roles = task_store.get_user_agent_roles(acting) or {}
        per_agent = roles.get(trigger["agent"], "viewer")
        if per_agent == "manager":
            return  # owner: any
        if per_agent == "editor" and trigger.get("created_by") == acting:
            return  # editor: only own
        raise HTTPException(
            403,
            f"User lacks manager role on agent '{trigger['agent']}' "
            f"(or editor on a trigger they created)",
        )


def _enforce_create_permission(
    *, scope: str, agent: str, user: UserContext,
) -> str:
    """Validate the caller can create a trigger of this scope on this agent and
    return the token-authoritative ``created_by``. Identity comes from the
    session token ONLY — never a client X-On-Behalf-Of header.

    Editor + manager + admin can create agent-scope triggers
    (collaborative).
    """
    # Visibility-modes: reject a scope the agent's mode doesn't offer
    # (Personal-only → no "agent"; Shared-only → no "user").
    from core.session.visibility import available_scopes_for
    from storage import agent_store as _as
    _row = _as.get_agent(agent) or {}
    _avail = available_scopes_for(
        bool(_row.get("collaborative", True)), _row.get("default_scope") or "user",
    )
    if scope not in _avail:
        raise HTTPException(
            400,
            f"This agent does not support {scope!r}-scoped triggers "
            f"(mode offers: {', '.join(_avail)})",
        )
    # Writes never cross agents: an agent session (user-backed or not) creates
    # triggers only on ITS OWN agent — changing another agent goes through
    # delegation. Cookie users and the master key keep their role-based reach.
    if user.is_session and agent != user.agent:
        raise HTTPException(
            403,
            "Agent sessions can create triggers only on their own agent "
            "(to change another agent, delegate to it).",
        )
    acting = user.acting_sub
    if scope == "agent":
        if acting is None:
            # master key OR no-user (phone/agent) session: system-owned.
            return agent
        if not user.can_edit_agent(agent):
            raise HTTPException(
                403,
                "Agent-scoped triggers require editor, manager, or admin role for this agent",
            )
        return acting
    if scope == "user":
        if acting is None:
            if user.is_no_user_session:
                raise HTTPException(
                    403,
                    "This session has no user identity and cannot create "
                    "user-scoped triggers.",
                )
            raise HTTPException(
                400,
                "User-scoped triggers cannot be created with the master API key; "
                "they must be created from a user session.",
            )
        # Real user — must have agent access.
        if not user.can_access_agent(agent):
            raise HTTPException(403, f"Access denied for agent '{agent}'")
        return acting
    raise HTTPException(400, f"Invalid scope: {scope}")


# =====================================================================
# Pydantic request models
# =====================================================================


class NotifyConfig(BaseModel):
    enabled: bool = False
    severity: str = "info"
    title: str | None = None
    body: str | None = None
    target_scope: str | None = None  # 'user' | 'agent' | 'global'
    target: str | None = None        # username, agent name, or NULL


class CreateTriggerRequest(BaseModel):
    name: str
    scope: str = "user"               # 'user' | 'agent'
    agent: str
    slug: str | None = None
    task_id: str | None = None
    notify: NotifyConfig | None = None
    debounce_seconds: int = 0
    enabled: bool = True
    # Vendor-subscription linkage. When subscription_id is set,
    # the trigger fires when the linked subscription receives a matching
    # event. event_filter is an equality dict (see event_normalizer).
    subscription_id: str | None = None
    event_filter: dict | None = None


class EditTriggerRequest(BaseModel):
    name: str | None = None
    task_id: str | None = None
    notify_enabled: bool | None = None
    notify_severity: str | None = None
    notify_title: str | None = None
    notify_body: str | None = None
    notify_target_scope: str | None = None
    notify_target: str | None = None
    debounce_seconds: int | None = None
    event_filter: dict | None = None


# =====================================================================
# Webhook fire endpoints
# =====================================================================


@router.post("/v1/webhooks/agent/{agent}/{slug}")
async def fire_agent_trigger(
    agent: str,
    slug: str,
    request: Request,
):
    """Fire an agent-scoped trigger via webhook.

    Auth: Bearer ``otok_…`` matching an ``agent_api_keys`` row for ``agent``
    with the ``triggers`` permission. Master PROXY_API_KEY rejected.
    """
    auth = request.headers.get("authorization") or ""
    try:
        api_key_manager.verify_bearer_for_agent(
            auth, agent=agent, required_permission="triggers",
        )
    except api_key_manager.KeyMismatch as e:
        # All failures → 403 (don't distinguish auth-format from missing-key
        # to attackers). Log the code for ops debugging; throttle the source IP.
        logger.info(f"Webhook auth failed agent={agent} slug={slug} code={e.code}")
        _webhook_auth_failed(request)

    trigger = trigger_store.get_trigger_by_slug(scope="agent", owner=agent, slug=slug)
    if not trigger or not trigger.get("enabled"):
        raise HTTPException(404, "Trigger not found or disabled")

    # Cap the fire rate per trigger so a leaked key can't burn credits / DoS.
    _webhook_throttle(f"trig:agent:{agent}/{slug}")
    body = await _safe_json(request)
    return await trigger_manager.fire_trigger(
        trigger, body, trigger_source=f"agent:{agent}/{slug}",
    )


@router.post("/v1/webhooks/user/{username}/{slug}")
async def fire_user_trigger(
    username: str,
    slug: str,
    request: Request,
):
    """Fire a user-scoped trigger via webhook.

    Auth: Bearer ``otok_…`` matching a ``user_api_keys`` row for the user
    identified by ``username`` with the ``triggers`` permission.
    """
    auth = request.headers.get("authorization") or ""
    try:
        api_key_manager.verify_bearer_for_user(
            auth, username=username, required_permission="triggers",
        )
    except api_key_manager.KeyMismatch as e:
        logger.info(f"Webhook auth failed user={username} slug={slug} code={e.code}")
        _webhook_auth_failed(request)

    user_sub = notification_store.resolve_username_to_sub(username)
    # verify_bearer_for_user already confirmed it; this is just for the lookup.
    trigger = trigger_store.get_trigger_by_slug(
        scope="user", owner=user_sub or "", slug=slug,
    )
    if not trigger or not trigger.get("enabled"):
        raise HTTPException(404, "Trigger not found or disabled")

    # Cap the fire rate per trigger so a leaked key can't burn credits / DoS.
    _webhook_throttle(f"trig:user:{username}/{slug}")
    body = await _safe_json(request)
    return await trigger_manager.fire_trigger(
        trigger, body, trigger_source=f"user:{username}/{slug}",
    )


async def _safe_json(request: Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


# =====================================================================
# CRUD endpoints
# =====================================================================


@router.post("/v1/triggers")
async def create_trigger_endpoint(
    req: CreateTriggerRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    created_by = _enforce_create_permission(
        scope=req.scope, agent=req.agent, user=u,
    )
    notify = req.notify or NotifyConfig()
    try:
        row = trigger_manager.register_trigger(
            name=req.name,
            scope=req.scope,
            agent=req.agent,
            created_by=created_by,
            slug=req.slug,
            task_id=req.task_id,
            notify_enabled=notify.enabled,
            notify_severity=notify.severity,
            notify_title=notify.title,
            notify_body=notify.body,
            notify_target_scope=notify.target_scope,
            notify_target=notify.target,
            debounce_seconds=req.debounce_seconds,
            enabled=req.enabled,
            subscription_id=req.subscription_id,
            event_filter=req.event_filter,
        )
    except trigger_manager.TriggerValidationError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        # Likely UniqueViolation on (scope, owner, slug). Surface as 400.
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(400, "Trigger slug already exists in this scope")
        raise
    response = {"status": "created", "trigger": _decorate_for_user(row, u)}
    # Soft-warn on functionally-duplicate vendor-subscribed triggers.
    # The unique-index only catches exact slug collisions, but
    # (subscription_id + event_filter) ties are functionally identical —
    # every matching event will fire BOTH, so the user gets multiple
    # notifications / task runs they probably didn't intend. We don't
    # BLOCK because multiple intents per subscription is legitimate
    # (one trigger fires a task, another fires a notify to a different
    # target). We just surface it loudly so the agent / dashboard can
    # report it back.
    if req.subscription_id:
        siblings = [
            t for t in trigger_store.list_triggers(subscription_id=req.subscription_id)
            if t["id"] != row["id"] and t.get("event_filter") == row.get("event_filter")
        ]
        if siblings:
            response["warnings"] = [
                f"Another trigger ('{s['slug']}') on this subscription has an "
                f"identical event_filter — both will fire on every matching event. "
                f"Consider deleting one if this wasn't intended."
                for s in siblings
            ]
    return response


@router.get("/v1/triggers")
async def list_triggers_endpoint(
    agent: str | None = Query(None),
    scope: str | None = Query(None),
    audit: bool = Query(False),
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    # The master key + the admin AUDIT surface (``audit=true`` — the admin
    # Triggers page) see every user's triggers so they can audit; everyone
    # else — INCLUDING an admin on an agent's settings tab — gets the
    # user-view (own user-scoped + agent-scoped). ``agent`` stays a plain
    # filter in both modes. Keyed on is_service like /v1/tasks (H1/H2): a
    # session JWT is api-key-shaped but must get the user-view + the
    # accessible-agents filter exactly like a cookie caller.
    if u.is_service or (audit and u.is_admin):
        rows = trigger_store.list_triggers(agent=agent, scope=scope)
    else:
        rows = trigger_store.list_triggers_for_user_view(
            user_sub=u.sub, agent=agent,
        )
        if scope:
            rows = [r for r in rows if r.get("scope") == scope]
    # Filter by accessible agents for non-admin. Delegation edges add the
    # targets' AGENT-SCOPE triggers to a no-user caller's view (the store's
    # user-view already dropped foreign user-scope rows).
    if not (u.is_admin or u.is_service):
        edge_reach = nouser_read_targets(u)
        rows = [
            r for r in rows
            if u.can_access_agent(r["agent"])
            or (r.get("scope") == "agent" and r["agent"] in edge_reach)
        ]
    return {"triggers": [_decorate_for_user(r, u) for r in rows]}


@router.get("/v1/triggers/{trigger_id}")
async def get_trigger_endpoint(
    trigger_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    row = trigger_store.get_trigger(trigger_id)
    if not row:
        raise HTTPException(404, "Trigger not found")
    if not _can_view_trigger(row, u):
        raise HTTPException(403, "Forbidden")
    return _decorate_for_user(row, u)


async def _edit_impl(
    trigger_id: str, req: EditTriggerRequest, user: UserContext,
):
    row = trigger_store.get_trigger(trigger_id)
    if not row:
        raise HTTPException(404, "Trigger not found")
    if not _can_manage_trigger(row, user):
        raise HTTPException(403, "Forbidden")
    if user.is_api_key:
        _check_trigger_mutation_authority(row, user)

    fields = req.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "At least one editable field must be provided")
    ok, err = trigger_manager.update_trigger(trigger_id, fields)
    if err:
        raise HTTPException(400, err)
    if not ok:
        raise HTTPException(404, "Trigger not found")
    return {"status": "updated", "trigger_id": trigger_id}


@router.patch("/v1/triggers/{trigger_id}")
async def edit_trigger_endpoint(
    trigger_id: str,
    req: EditTriggerRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    return await _edit_impl(trigger_id, req, u)


@router.post("/v1/triggers/{trigger_id}/edit")
async def edit_trigger_post(
    trigger_id: str,
    req: EditTriggerRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    return await _edit_impl(trigger_id, req, u)


@router.delete("/v1/triggers/{trigger_id}")
async def delete_trigger_endpoint(
    trigger_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    row = trigger_store.get_trigger(trigger_id)
    if not row:
        raise HTTPException(404, "Trigger not found")
    if not _can_manage_trigger(row, u):
        raise HTTPException(403, "Forbidden")
    if u.is_api_key:
        _check_trigger_mutation_authority(row, u)
    ok, err = trigger_manager.delete_trigger(trigger_id)
    if err:
        raise HTTPException(403, err)
    if not ok:
        raise HTTPException(404, "Trigger not found")
    return {"status": "deleted", "trigger_id": trigger_id}


@router.post("/v1/triggers/{trigger_id}/pause")
async def pause_trigger_endpoint(
    trigger_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    row = trigger_store.get_trigger(trigger_id)
    if not row:
        raise HTTPException(404, "Trigger not found")
    if not _can_manage_trigger(row, u):
        raise HTTPException(403, "Forbidden")
    if u.is_api_key:
        _check_trigger_mutation_authority(row, u)
    ok, err = trigger_manager.pause_trigger(trigger_id)
    if err:
        raise HTTPException(403, err)
    if not ok:
        raise HTTPException(404, "Trigger not found")
    return {"status": "paused", "trigger_id": trigger_id}


@router.post("/v1/triggers/{trigger_id}/resume")
async def resume_trigger_endpoint(
    trigger_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    row = trigger_store.get_trigger(trigger_id)
    if not row:
        raise HTTPException(404, "Trigger not found")
    if not _can_manage_trigger(row, u):
        raise HTTPException(403, "Forbidden")
    if u.is_api_key:
        _check_trigger_mutation_authority(row, u)
    ok, err = trigger_manager.resume_trigger(trigger_id)
    if err:
        raise HTTPException(403, err)
    if not ok:
        raise HTTPException(404, "Trigger not found")
    return {"status": "resumed", "trigger_id": trigger_id}


@router.post("/v1/triggers/{trigger_id}/fire")
async def fire_test_endpoint(
    trigger_id: str,
    request: Request,
    user: UserContext | None = Depends(get_current_user),
):
    """Internal fire test (no Bearer required — session auth + view permission).

    Reads JSON body for placeholder substitution, fires the same path as
    webhook calls. Useful for "test fire" buttons in dashboard.
    """
    u = require_auth(user)
    row = trigger_store.get_trigger(trigger_id)
    if not row:
        raise HTTPException(404, "Trigger not found")
    if not _can_view_trigger(row, u):
        raise HTTPException(403, "Forbidden")
    # Edge visibility is READ-ONLY — firing makes the target's agent
    # run (rule 2: writes never cross agents), so a no-user session may fire
    # only its own agent's triggers no matter what it can see.
    if u.is_no_user_session and row.get("agent") != u.agent:
        raise HTTPException(
            403,
            "Cross-agent visibility is read-only — this session cannot fire "
            "another agent's triggers (delegate to that agent instead).",
        )
    if not row.get("enabled"):
        raise HTTPException(400, "Trigger is paused")
    body = await _safe_json(request)
    return await trigger_manager.fire_trigger(
        row, body, trigger_source=f"test:{u.sub[:8]}",
    )


# =====================================================================
# Decoration helpers (add UI permission flags)
# =====================================================================


def _decorate_for_user(row: dict, user: UserContext) -> dict:
    """Add can_pause / can_resume / can_delete / can_edit / can_fire flags
    + linked task name + webhook URL hint.
    """
    out = dict(row)
    can_manage = _can_manage_trigger(row, user)
    is_enabled = bool(row.get("enabled", True))

    out["can_edit"] = can_manage
    out["can_delete"] = can_manage
    out["can_pause"] = can_manage and is_enabled
    out["can_resume"] = can_manage and not is_enabled
    # Fire is view-level EXCEPT for no-user callers on edge-visible rows —
    # the fire endpoint pins them to their own agent, so the flag must not lie.
    out["can_fire"] = _can_view_trigger(row, user) and not (
        user.is_no_user_session and row.get("agent") != user.agent
    )

    # Webhook URL relative path (frontend prepends host). Lives under
    # /v1/webhooks/ — same prefix as vendor-subscribed webhooks so a
    # single reverse-proxy auth-gate bypass (`^/v1/webhooks/`) covers
    # both inbound surfaces. Vendor triggers (subscription_id set) don't
    # carry a generic webhook URL; their events arrive at
    # /v1/webhooks/{provider}/{subscription_id}.
    if row.get("subscription_id"):
        out["webhook_path"] = None  # vendor-source — no generic URL
    elif row.get("scope") == "agent":
        out["webhook_path"] = f"/v1/webhooks/agent/{row['agent']}/{row['slug']}"
    elif row.get("scope") == "user":
        username = notification_store.resolve_sub_to_username(row["created_by"])
        out["webhook_path"] = (
            f"/v1/webhooks/user/{username}/{row['slug']}" if username else None
        )
        out["created_by_name"] = username

    # Linked task name for display.
    if row.get("task_id"):
        task = task_store.get_dynamic_task(row["task_id"])
        out["task_name"] = task["name"] if task else None
    else:
        out["task_name"] = None

    # Permissions JSONB → Python list (for any embedded api-key data; not
    # currently needed but keeps shape consistent).

    return out
