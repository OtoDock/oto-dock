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

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from api.events import webhook_body

from storage.automation import trigger_store
from storage import database as task_store
from storage.automation import notification_store
from storage.identity import api_key_store
from storage.pg import run_db
from services.scheduler import trigger_manager
from services.infra import api_key_manager
from auth.providers import UserContext, get_current_user, require_auth
from core.session.visibility import nouser_read_targets
from auth import roles
from core.session import visibility as _vis

logger = logging.getLogger("claude-proxy.triggers")
router = APIRouter()


def _webhook_throttle(key: str, bucket: str = "webhook") -> None:
    """Apply a rate-limit bucket to ``key`` → 429 if over. The ``webhook`` bucket
    (generous) caps a single trigger's fire rate; ``webhook_auth`` (strict) caps
    per-address key brute-forcing."""
    from auth import rate_limiter
    ok, retry_after = rate_limiter.hit(bucket, key)
    if not ok:
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={"Retry-After": str(retry_after)},
        )


def _webhook_auth_failed(request: Request) -> None:
    """Record + throttle a failed webhook auth by source address (strict
    bucket, keyed by ``auth_bucket_key``), then raise 403 (or 429 once the
    address trips the limit) — so a leaked-URL brute force is rate-limited
    instead of unbounded."""
    from auth.lan_check import auth_bucket_key
    _webhook_throttle(f"ip:{auth_bucket_key(request)}", bucket="webhook_auth")
    raise HTTPException(403, "Forbidden")


# ---------------------------------------------------------------------------
# The fire key's verification: a bcrypt and a
# store read, so never on the event loop. A key verified recently is served
# from a cache of SHA-256 digests (a legitimate burst costs one bcrypt, not
# one per fire), re-checked against its row so a revocation stays immediate;
# a miss is gated per key prefix before any bcrypt, and runs in a worker
# thread behind a small semaphore with a bounded queue (503 past it).
# ---------------------------------------------------------------------------

_FIRE_KEY_TTL_S = 300.0
_FIRE_KEY_CACHE_MAX = 1024
# (kind, owner segment, sha256 of the token) → (key id, stored hash, until).
_verified_keys: "OrderedDict[tuple[str, str, str], tuple[str, str, float]]" = OrderedDict()


class _VerifyGate:
    """The semaphore, bound to the running loop and rebuilt when it changes
    (the test suite runs several loops)."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.sem: asyncio.Semaphore | None = None
        self.waiting = 0
        # One verification per key at a time: the fires of a burst that
        # present the same key wait for it instead of queueing their own.
        self.inflight: dict[tuple[str, str, str], asyncio.Future] = {}

    def current(self) -> "_VerifyGate":
        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            import config
            self.loop = loop
            self.sem = asyncio.Semaphore(config.WEBHOOK_KEY_VERIFY_CONCURRENCY)
            self.waiting = 0
            self.inflight = {}
        return self


_verify_gate = _VerifyGate()


def _fire_key_prefix(authorization: str) -> str | None:
    """The key's index prefix, or None when the header is not a well-formed
    fire key (the master key included). No I/O."""
    import config
    token = api_key_manager._strip_bearer(authorization)
    if not token or config.is_master_key(token):
        return None
    if not token.startswith(api_key_manager.KEY_PUBLIC_PREFIX):
        return None
    body = token[len(api_key_manager.KEY_PUBLIC_PREFIX):]
    if len(body) < api_key_manager.KEY_INDEX_PREFIX_LEN:
        return None
    return body[:api_key_manager.KEY_INDEX_PREFIX_LEN]


def _cached_key_row(kind: str, owner: str, key_id: str, key_hash: str) -> dict | None:
    """The key row a cache entry names, when it still authorizes this fire:
    not revoked, the same stored hash, the triggers permission, this agent or
    one of the users the username segment names. Runs on the DB executor."""
    if kind == _vis.SCOPE_AGENT:
        row = api_key_store.get_agent_api_key(key_id)
        owner_ok = bool(row) and row.get("agent") == owner
    else:
        row = api_key_store.get_user_api_key(key_id)
        owner_ok = bool(row) and row.get("user_sub") in notification_store.resolve_username_candidates(owner)
    if (not owner_ok or row.get("revoked_at") or row.get("key_hash") != key_hash
            or not api_key_store.has_permission(row, "triggers")):
        return None
    if kind == _vis.SCOPE_AGENT:
        api_key_store.update_agent_key_last_used(key_id)
    else:
        api_key_store.update_user_key_last_used(key_id)
    return row


def _remember_key(cache_key: tuple[str, str, str], row: dict) -> None:
    _verified_keys[cache_key] = (row["id"], row["key_hash"], time.monotonic() + _FIRE_KEY_TTL_S)
    _verified_keys.move_to_end(cache_key)
    while len(_verified_keys) > _FIRE_KEY_CACHE_MAX:
        _verified_keys.popitem(last=False)


async def _verify_fire_key(request: Request, kind: str, owner: str, slug: str) -> dict:
    """The key row authorizing this fire, or the refusal (403, or 429 on the
    per-address or per-prefix throttle, or 503 when the verification queue is
    full)."""
    import config
    from auth import rate_limiter
    auth = request.headers.get("authorization") or ""
    prefix = _fire_key_prefix(auth)
    if prefix is None:
        logger.info(f"Webhook auth failed {kind}={owner} slug={slug} code=format")
        _webhook_auth_failed(request)

    digest = hashlib.sha256(auth.split(" ", 1)[1].strip().encode()).hexdigest()
    cache_key = (kind, owner, digest)
    cached = _verified_keys.get(cache_key)
    if cached and cached[2] > time.monotonic():
        row = await run_db(_cached_key_row, kind, owner, cached[0], cached[1])
        if row is not None:
            _remember_key(cache_key, row)
            return row
    _verified_keys.pop(cache_key, None)

    gate = _verify_gate.current()
    leader = gate.inflight.get(cache_key)
    if leader is not None:
        row = await asyncio.shield(leader)
        if row is None:
            _webhook_auth_failed(request)
        return row

    ok, retry_after = rate_limiter.check_rate_limit("webhook_prefix", prefix)
    if not ok:
        raise HTTPException(429, "Too many requests", headers={"Retry-After": str(retry_after)})
    if gate.sem.locked() and gate.waiting >= config.WEBHOOK_KEY_VERIFY_MAX_WAITERS:
        raise HTTPException(503, "Busy", headers={"Retry-After": "5", "Connection": "close"})

    future: asyncio.Future = asyncio.get_running_loop().create_future()
    gate.inflight[cache_key] = future
    row = None
    try:
        gate.waiting += 1
        try:
            await gate.sem.acquire()
        finally:
            gate.waiting -= 1
        try:
            if kind == _vis.SCOPE_AGENT:
                row = await asyncio.to_thread(
                    api_key_manager.verify_bearer_for_agent, auth, agent=owner,
                    required_permission="triggers")
            else:
                row = await asyncio.to_thread(
                    api_key_manager.verify_bearer_for_user, auth, username=owner,
                    required_permission="triggers")
        except api_key_manager.KeyMismatch as e:
            # All failures → 403 (don't distinguish auth-format from missing-key
            # to attackers). Log the code for ops debugging; throttle the source address.
            logger.info(f"Webhook auth failed {kind}={owner} slug={slug} code={e.code}")
            if e.code == "unknown":
                rate_limiter.record_attempt("webhook_prefix", prefix)
        finally:
            gate.sem.release()
    finally:
        gate.inflight.pop(cache_key, None)
        if not future.done():
            future.set_result(row)
    if row is None:
        _webhook_auth_failed(request)
    _remember_key(cache_key, row)
    return row


# =====================================================================
# Permission helpers
# =====================================================================


def _can_manage_trigger(trigger: dict, user: UserContext) -> bool:
    """Return True if the user can mutate (edit/pause/resume/delete) this trigger.

    3-tier model:
      - Agent-scoped: manager (any) or editor (own only).
      - User-scoped: creator only (or admin).

    Only the master key bypasses (``is_service``). A session JWT is
    api-key-shaped but carries a real (or no-user) identity: any session
    reaches ONLY its own agent's triggers (writes never cross agents, as at
    create), a user-backed one then resolves the 3-tier model like a cookie
    caller, and a no-user one may manage the agent-scope triggers.
    """
    if user.is_session and trigger.get("agent") != user.agent:
        return False
    if user.is_admin or user.is_service:
        return True
    if user.is_no_user_session:
        return trigger.get("scope") == _vis.SCOPE_AGENT
    scope = trigger.get("scope")
    if scope == _vis.SCOPE_AGENT:
        if user.can_manage_agent(trigger["agent"]):
            return True  # owner: any
        if user.can_edit_agent(trigger["agent"]) and trigger.get("created_by") == user.sub:
            return True  # editor: only own
        return False
    if scope == _vis.SCOPE_USER:
        return trigger.get("created_by") == user.sub
    return False


def _can_view_trigger(trigger: dict, user: UserContext) -> bool:
    """Return True if the user can see this trigger (lists, detail).

    Keyed on ``is_service``, not ``is_api_key`` — a session JWT must stay
    inside its identity's reach (keying on ``is_api_key`` let any agent
    session read AND test-fire any trigger by id). A NO-USER session
    additionally sees the AGENT-SCOPE triggers of its delegation targets —
    but test-fire takes the edit authority and pins every session to its
    own agent (firing makes the target's agent RUN; the edge is read-only)."""
    if user.is_admin or user.is_service:
        return True
    if not user.can_access_agent(trigger["agent"]):
        if not (trigger.get("scope") == _vis.SCOPE_AGENT
                and trigger["agent"] in nouser_read_targets(user)):
            return False
    scope = trigger.get("scope")
    if scope == _vis.SCOPE_AGENT:
        return True
    if scope == _vis.SCOPE_USER:
        return trigger.get("created_by") == user.sub
    return False


def _check_trigger_mutation_authority(trigger: dict, user: UserContext) -> None:
    """Enforce mutation rights for api-key callers using the AUTHENTICATED
    identity (never a client header).

      - master key: full service-to-service access.
      - any session: its OWN agent's triggers only (writes never cross
        agents, as at create).
      - no-user session (phone/agent service): DENIED on user-scoped triggers
        (no identity); may manage agent-scope triggers on its agent.
      - real-user-backed session token: user-scope → only the creator;
        agent-scope → admin/manager any, editor only own.
    """
    if user.is_session and trigger.get("agent") != user.agent:
        raise HTTPException(
            403,
            "This session can only manage its own agent's triggers "
            "(to change another agent, delegate to it).",
        )
    acting = user.acting_sub
    if acting is None:
        if user.is_service:
            return  # master key: full s2s
        # No-user session: agent-scope management on ITS OWN agent only.
        if trigger.get("scope") == _vis.SCOPE_USER:
            raise HTTPException(
                403,
                "This session has no user identity and cannot manage "
                "user-scoped triggers.",
            )
        return
    scope = trigger.get("scope")
    if scope == _vis.SCOPE_USER:
        if trigger.get("created_by") != acting:
            raise HTTPException(
                403, "Cannot manage another user's trigger",
            )
    elif scope == _vis.SCOPE_AGENT:
        acting_user = task_store.get_user(acting) or {}
        role = roles.effective_role(
            acting_user.get("role"), task_store.get_user_agent_roles(acting), trigger["agent"])
        if roles.is_admin(role):
            return  # platform admin: any
        if roles.may_mutate_shared(role, own=trigger.get("created_by") == acting):
            return  # owner: any; editor: only own
        raise HTTPException(
            403,
            f"User lacks manager role on agent '{trigger['agent']}' "
            f"(or editor on a trigger they created)",
        )
    else:
        # A scope the vocabulary does not know never falls through to allow.
        raise HTTPException(403, f"Unknown trigger scope {scope!r}")


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
    from storage.agents import agent_store as _as
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
    if scope == _vis.SCOPE_AGENT:
        if acting is None:
            # master key OR no-user (phone/agent) session: system-owned.
            return agent
        if not user.can_edit_agent(agent):
            raise HTTPException(
                403,
                "Agent-scoped triggers require editor, manager, or admin role for this agent",
            )
        return acting
    if scope == _vis.SCOPE_USER:
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
    # The app action (APPS.md "Handlers"): the app by slug in the trigger's
    # scope (never an id from the client) and one of its on_trigger names.
    app_slug: str | None = None
    handler: str | None = None


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
    app_slug: str | None = None
    handler: str | None = None
    # The vendor subscription a trigger fires from can be re-bound later
    # (the same scope rule as at creation; '' unbinds): an app's trigger is
    # usually created before the agent's subscription exists.
    subscription_id: str | None = None


_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9:_.-]{1,128}$")


def _event_id(request: Request) -> str:
    """``X-OtoDock-Event-Id``: the caller's idempotency key for an app
    trigger's delivery (APPS.md "Handlers"); task and notify actions ignore
    it in this version."""
    raw = (request.headers.get("x-otodock-event-id") or "").strip()
    if not raw:
        return ""
    if not _EVENT_ID_RE.match(raw):
        raise HTTPException(400, "X-OtoDock-Event-Id: 1 to 128 of A-Z a-z 0-9 : _ . -")
    return raw


def _resolve_app_target(agent: str, scope: str, created_by: str, app_slug: str) -> str:
    """The app row a slug names in the trigger's scope: the agent's shared
    app for agent scope, the creator's personal app for user scope."""
    username = ""
    if scope == _vis.SCOPE_USER:
        u = task_store.get_user(created_by) or {}
        username = u.get("username") or ""
        if not username:
            raise HTTPException(400, "a user-scoped app trigger needs a user with a username")
    row = task_store.get_app_by_slug(agent, username, app_slug.strip())
    if not row or row.get("hidden"):
        raise HTTPException(404, f"no app '{app_slug}' in this scope")
    return row["id"]


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
    await _verify_fire_key(request, _vis.SCOPE_AGENT, agent, slug)

    trigger = await run_db(trigger_store.get_trigger_by_slug, scope="agent", owner=agent, slug=slug)
    if not trigger or not trigger.get("enabled"):
        raise HTTPException(404, "Trigger not found or disabled")

    # Cap the fire rate per trigger so a leaked key can't burn credits / DoS.
    _webhook_throttle(f"trig:agent:{agent}/{slug}")
    event_id = _event_id(request)
    body = await _keyed_body(request, f"trigger:agent/{agent}/{slug}")
    return await trigger_manager.fire_trigger(
        trigger, body, trigger_source=f"agent:{agent}/{slug}", event_id=event_id,
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
    key = await _verify_fire_key(request, _vis.SCOPE_USER, username, slug)

    # The key proved which user the address names (a segment can match one
    # user by username and another by display name).
    trigger = await run_db(
        trigger_store.get_trigger_by_slug, scope="user", owner=key.get("user_sub") or "", slug=slug,
    )
    if not trigger or not trigger.get("enabled"):
        raise HTTPException(404, "Trigger not found or disabled")

    # Cap the fire rate per trigger so a leaked key can't burn credits / DoS.
    _webhook_throttle(f"trig:user:{username}/{slug}")
    event_id = _event_id(request)
    body = await _keyed_body(request, f"trigger:user/{username}/{slug}")
    return await trigger_manager.fire_trigger(
        trigger, body, trigger_source=f"user:{username}/{slug}", event_id=event_id,
    )


async def _json_object(request: Request) -> dict:
    """A dashboard test fire's JSON body (the middleware's JSON tier bounds
    it); anything but an object fires with ``{}``."""
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def _keyed_body(request: Request, source: str) -> dict:
    """The fire's JSON body, read once its key verified: up to the keyed cap
    (``webhook_body.keyed_cap``), under the read deadlines and the in-flight
    bounds. A body that is not a JSON object fires with ``{}``."""
    cap = webhook_body.keyed_cap()
    webhook_body.lift(request, cap)
    try:
        raw = await webhook_body.read(request, cap=cap, source=source)
    except webhook_body.TooLarge:
        raise HTTPException(413, "Body too large") from None
    except webhook_body.Busy:
        raise HTTPException(503, "Busy", headers={"Retry-After": "5"}) from None
    except TimeoutError:
        raise HTTPException(408, "Body timeout") from None
    # A body the middleware cut arrives as a disconnect, which propagates:
    # the caller already holds the 413, so the fire must not go on with {}.
    try:
        body = await webhook_body.parse_json(raw)
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


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
    app_id = (_resolve_app_target(req.agent, req.scope, created_by, req.app_slug)
              if req.app_slug else None)
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
            app_id=app_id,
            handler=req.handler,
            caller_is_admin=bool(u.is_admin or u.is_service),
        )
    except trigger_manager.TriggerConflict as e:
        raise HTTPException(409, str(e))
    except trigger_manager.TriggerValidationError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        # Likely UniqueViolation on (scope, owner, slug). Surface as 400.
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(400, "Trigger slug already exists in this scope")
        raise
    response = {"status": "created",
                "trigger": _decorate_for_user(row, u, await run_db(_decoration_maps, [row]))}
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
            t for t in await run_db(trigger_store.list_triggers,
                                    subscription_id=req.subscription_id)
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
    def _job() -> tuple[list[dict], dict]:
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
                or (r.get("scope") == _vis.SCOPE_AGENT and r["agent"] in edge_reach)
            ]
        return rows, _decoration_maps(rows)

    # The rows and everything their decoration reads (the linked tasks, the
    # transfers, the creators' names, the apps): one job, four batched reads
    # and one task read per distinct linked task, however many rows.
    rows, maps = await run_db(_job)
    return {"triggers": [_decorate_for_user(r, u, maps) for r in rows]}


@router.get("/v1/triggers/{trigger_id}")
async def get_trigger_endpoint(
    trigger_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)

    def _job():
        row = trigger_store.get_trigger(trigger_id)
        return row, (_decoration_maps([row]) if row and _can_view_trigger(row, u) else None)

    row, maps = await run_db(_job)
    if not row:
        raise HTTPException(404, "Trigger not found")
    if maps is None:
        raise HTTPException(403, "Forbidden")
    return _decorate_for_user(row, u, maps)


def _moved_from(rows: list[dict]) -> dict[str, str]:
    from services.agents import offboarding_transfer
    return offboarding_transfer.transferred_from_names(
        [r.get("transferred_from") or "" for r in rows])


async def _edit_impl(
    trigger_id: str, req: EditTriggerRequest, user: UserContext,
):
    row = await run_db(trigger_store.get_trigger, trigger_id)
    if not row:
        raise HTTPException(404, "Trigger not found")
    if not _can_manage_trigger(row, user):
        raise HTTPException(403, "Forbidden")
    if user.is_api_key:
        _check_trigger_mutation_authority(row, user)

    fields = req.model_dump(exclude_unset=True)
    if "app_slug" in fields:
        slug = fields.pop("app_slug")
        if slug:
            fields["app_id"] = _resolve_app_target(row["agent"], row["scope"], row["created_by"], slug)
        else:
            fields["app_id"] = None
            fields["handler"] = None
    if not fields:
        raise HTTPException(400, "At least one editable field must be provided")
    try:
        ok, err = trigger_manager.update_trigger(
            trigger_id, fields, caller_is_admin=bool(user.is_admin or user.is_service))
    except trigger_manager.TriggerConflict as e:
        raise HTTPException(409, str(e))
    if err:
        raise HTTPException(409 if "approve" in err else 400, err)
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
    row = await run_db(trigger_store.get_trigger, trigger_id)
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
    row = await run_db(trigger_store.get_trigger, trigger_id)
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
    row = await run_db(trigger_store.get_trigger, trigger_id)
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
    """Internal fire test (no Bearer required — session auth + edit permission).

    Reads JSON body for placeholder substitution, fires the same path as
    webhook calls. Useful for "test fire" buttons in dashboard.
    """
    u = require_auth(user)
    row = await run_db(trigger_store.get_trigger, trigger_id)
    if not row:
        raise HTTPException(404, "Trigger not found")
    if not _can_view_trigger(row, u):
        raise HTTPException(403, "Forbidden")
    # Edge visibility is READ-ONLY — firing makes the target's agent
    # run (rule 2: writes never cross agents), so a session may fire only
    # its own agent's triggers no matter what it can see.
    if u.is_session and row.get("agent") != u.agent:
        raise HTTPException(
            403,
            "Cross-agent visibility is read-only — this session cannot fire "
            "another agent's triggers (delegate to that agent instead).",
        )
    # A fire runs the linked task as the agent and sends the notify with the
    # caller's body in its placeholders: the authority an edit needs, as
    # running a task by id does.
    if not _can_manage_trigger(row, u):
        raise HTTPException(403, "Firing a trigger needs the right to edit it")
    if u.is_api_key:
        _check_trigger_mutation_authority(row, u)
    if not row.get("enabled"):
        raise HTTPException(400, "Trigger is paused")
    body = await _json_object(request)
    return await trigger_manager.fire_trigger(
        row, body, trigger_source=f"test:{u.sub[:8]}",
    )


# =====================================================================
# Decoration helpers (add UI permission flags)
# =====================================================================


def trigger_webhook_path(row: dict, usernames: dict[str, str] | None = None) -> str | None:
    """Webhook URL relative path (frontend prepends host). ``usernames`` is
    the pre-resolved ``{sub: username}`` of a listing (``_decoration_maps``);
    without it the one row's creator is read here. Lives under
    /v1/webhooks/ — same prefix as vendor-subscribed webhooks so a
    single reverse-proxy auth-gate bypass (`^/v1/webhooks/`) covers
    both inbound surfaces. Vendor triggers (subscription_id set) don't
    carry a generic webhook URL; their events arrive at
    /v1/webhooks/{provider}/{subscription_id}."""
    if row.get("subscription_id"):
        return None
    if row.get("scope") == _vis.SCOPE_AGENT:
        return f"/v1/webhooks/agent/{row['agent']}/{row['slug']}"
    if row.get("scope") == _vis.SCOPE_USER:
        if usernames is None:
            username = notification_store.resolve_sub_to_username(row["created_by"])
        else:
            username = usernames.get(row["created_by"])
        return f"/v1/webhooks/user/{username}/{row['slug']}" if username else None
    return None


_LINKED_TASK_EMPTY = {
    "task_name": None, "task_effective_model": "", "task_override_model": "",
    "task_effective_execution_path": "", "task_effective_model_source": "",
    "task_effective_model_tier": None, "task_tier_label": "",
}


def _linked_task_fields(row: dict, tasks_by_id: dict[str, dict] | None) -> dict:
    """The linked task's name and effective model for one trigger row.
    ``tasks_by_id`` is the pre-resolved batch (``_linked_tasks_for``) so a
    listing costs one pass over the task rows, not one lookup per trigger."""
    task_id = row.get("task_id")
    if not task_id:
        return dict(_LINKED_TASK_EMPTY)
    if tasks_by_id is None:
        tasks_by_id = _linked_tasks_for([row])
    return tasks_by_id.get(task_id) or dict(_LINKED_TASK_EMPTY)


def _linked_tasks_for(rows: list[dict]) -> dict[str, dict]:
    """Resolve the linked task fields for every trigger row in one batch
    (sync DB reads: call off the event loop)."""
    from api.tasks.tasks import _make_model_resolver
    from services.scheduler import scheduler
    out: dict[str, dict] = {}
    resolve = _make_model_resolver()
    for task_id in {r.get("task_id") for r in rows if r.get("task_id")}:
        task = task_store.get_dynamic_task(task_id)
        if not task:
            out[task_id] = dict(_LINKED_TASK_EMPTY)
            continue
        model = resolve(scheduler._row_to_task(task))
        out[task_id] = {
            "task_name": task["name"],
            "task_effective_model": model["effective_model"],
            "task_override_model": task.get("override_model") or "",
            "task_effective_execution_path": model["effective_execution_path"],
            "task_effective_model_source": model["effective_model_source"],
            "task_effective_model_tier": model["effective_model_tier"],
            "task_tier_label": model["tier_label"],
        }
    return out


def _decoration_maps(rows: list[dict]) -> dict:
    """Everything ``_decorate_for_user`` reads for a set of rows, in a few
    queries (sync store reads: call off the loop): the linked tasks, the
    transfers' names, the creators' usernames and display names (the
    user-scope rows), and the apps."""
    user_rows = [r for r in rows if r.get("scope") == _vis.SCOPE_USER]
    creators = [r.get("created_by") or "" for r in user_rows]
    return {
        "tasks": _linked_tasks_for(rows),
        "moved": _moved_from(rows),
        "usernames": notification_store.resolve_subs_to_usernames(creators),
        "names": notification_store.resolve_subs_to_display_names(creators),
        "apps": task_store.get_apps_by_ids([r.get("app_id") or "" for r in rows]),
    }


def _decorate_for_user(row: dict, user: UserContext, maps: dict | None = None) -> dict:
    """Add can_pause / can_resume / can_delete / can_edit / can_fire flags
    + linked task name and model + webhook URL hint + the name of whoever
    the offboarding transfer moved it from, all from ``maps``
    (``_decoration_maps``, read off the loop by every route): with them
    nothing here reads the store; a direct caller without them reads for
    its one row.
    """
    if maps is None:
        maps = _decoration_maps([row])
    out = dict(row)
    out["transferred_from_name"] = maps["moved"].get(row.get("transferred_from") or "", "")
    can_manage = _can_manage_trigger(row, user)
    is_enabled = bool(row.get("enabled", True))

    out["can_edit"] = can_manage
    out["can_delete"] = can_manage
    out["can_pause"] = can_manage and is_enabled
    out["can_resume"] = can_manage and not is_enabled
    # Fire takes the edit authority (a session only on its own agent, which
    # ``_can_manage_trigger`` already pins), so the flag must not lie.
    out["can_fire"] = can_manage and _can_view_trigger(row, user)

    out["webhook_path"] = trigger_webhook_path(row, maps["usernames"])
    if row.get("scope") == _vis.SCOPE_USER:
        out["created_by_name"] = maps["names"].get(row["created_by"])

    # Linked task: its name and what it runs on (a trigger has no model of
    # its own; the linked task's pins or its agent's default decide).
    out.update(_linked_task_fields(row, maps["tasks"]))
    # The app target in words (APPS.md "Handlers").
    if row.get("app_id"):
        app_row = maps["apps"].get(row["app_id"])
        out["app_slug"] = (app_row or {}).get("slug")
        out["app_title"] = (app_row or {}).get("title") or (app_row or {}).get("slug")
    else:
        out["app_slug"] = None
        out["app_title"] = None

    # Permissions JSONB → Python list (for any embedded api-key data; not
    # currently needed but keeps shape consistent).

    return out
