"""Task Management REST API.

Consumed by the React dashboard and the schedules-mcp and delegation-mcp servers.
Auth: API key (server-to-server) OR OAuth2 session cookie (dashboard users).
Mutating endpoints gate token callers on the calling agent — the master key
names it in ``X-Agent-Name``, a session token IS its ``agent`` claim — and
dashboard users on their role.
"""

import asyncio
import json
import logging
import uuid
import zoneinfo
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config
from api.tasks import run_stream, task_quota
from services.scheduler import schedule_text, scheduler, task_kinds
from storage import database as task_store
from storage.agents import agent_store
from storage.automation import run_status, trigger_store
from storage.pg import run_db
from core.session.session_state import get_user_tz
from core.session.visibility import nouser_read_targets
from auth.providers import UserContext, get_current_user, require_agent_access, require_auth, require_user
from auth import roles
from core.session import visibility as _vis
from ws import wire_events as wire

logger = logging.getLogger("claude-proxy.task-api")
# No route here takes an anonymous caller (auth.providers.require_user).
router = APIRouter(dependencies=[Depends(require_user)])


# --- Auth helpers ---


def _caller_agent(user: UserContext, x_agent_name: str | None) -> str | None:
    """The agent a token caller acts as. A session token's is its own
    ``agent`` claim — the header is not read, so a session cannot name
    another agent (a phone or agent-scope run reaches its own agent and
    its wired targets, like a trigger or a notification); the master key
    names its agent in ``X-Agent-Name``."""
    return user.agent if user.is_session else x_agent_name


def _check_agent_access_s2s(body_agent: str, x_agent_name: str | None) -> None:
    """Validate that the calling agent (``_caller_agent``) can manage this
    agent's tasks: itself, or a wired delegation target."""
    if not x_agent_name:
        raise HTTPException(status_code=403, detail="X-Agent-Name header required")
    if x_agent_name == body_agent:
        return  # self-management always allowed
    # Cross-agent: check delegation targets
    allowed = agent_store.get_delegation_targets(x_agent_name)
    if body_agent in allowed:
        return
    raise HTTPException(
        status_code=403,
        detail=f"Agent '{x_agent_name}' cannot manage tasks for agent '{body_agent}'",
    )


# --- Request models ---


class CreateScheduledTaskRequest(BaseModel):
    name: str
    agent: str
    prompt: str
    # Recurring: provide EXACTLY ONE of schedule (cron, for wall-clock times)
    # or interval_seconds (every N seconds, for a fixed cadence).
    schedule: str | None = None
    interval_seconds: int | None = None
    llm_mode: str = "cli"
    timeout_seconds: int = 600
    scope: str = "user"
    # REQUIRED — no default. The creating agent picks how the task notifies on
    # completion (auto/manual/none). The task agent's prompt is auto-injected
    # with mode-specific guidance so the two never disagree.
    notification_mode: Literal["auto", "manual", "none"]
    notify_severity: str = "info"
    # IANA timezone snapshot. Usually proxy-resolved from the calling session
    # (browser-detected via client_info). Callers may override explicitly.
    user_tz: str | None = None
    # Per-task execution overrides — pin THIS task's runs to a specific model
    # / execution layer instead of the agent's default (the token lever: a
    # cheap default with one expensive recurring task, or vice versa).
    # Validated against the agent's envelope by the same validator that
    # guards delegate spawns. Omit both to inherit.
    model: str | None = None
    layer: str | None = None
    # Checks (CHECKS.md): the agent's offered checks and the caller's own,
    # by name, run at the end of each run's turn; the agent's mandatory
    # checks run without being named.
    checks: list[str] | None = None


class CreateOneTimeTaskRequest(BaseModel):
    name: str
    agent: str
    prompt: str
    run_at: str | None = None
    delay_seconds: int | None = None
    llm_mode: str = "cli"
    timeout_seconds: int = 600
    # on_complete_* are only used by create_task_and_wait (via PATCH after task starts)
    on_complete_agent: str | None = None
    on_complete_prompt: str | None = None
    on_complete_session_id: str | None = None
    # Multi-turn delegation: continue a previous task's session. Must be a
    # UUID string (or null) — boolean-as-string ("false"/"true") would later
    # land in `--resume` and break the CLI. Pydantic accepts str | None;
    # `_validate_continue_session` rejects non-UUID strings at the boundary.
    continue_session: str | None = None
    use_persistent: bool = False
    scope: str = "user"
    # REQUIRED — see CreateScheduledTaskRequest.
    notification_mode: Literal["auto", "manual", "none"]
    notify_severity: str = "info"
    source_agent: str | None = None
    user_tz: str | None = None
    # 'one_time' (default — needs run_at OR delay_seconds), 'trigger'
    # (no schedule, fired only via trigger webhook)
    task_type: str | None = None
    # Per-task execution overrides — see CreateScheduledTaskRequest.
    model: str | None = None
    layer: str | None = None
    checks: list[str] | None = None


def _validate_user_tz(value: str | None) -> str | None:
    """Reject malformed IANA names. Returns the value (or None) on success."""
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
    """Default-resolve user_tz from the calling session when not provided.

    Order: explicit (from request) → on_behalf user's last reported TZ →
    dashboard caller's TZ → None (row stays NULL → falls back to platform).
    """
    if explicit:
        return _validate_user_tz(explicit)
    if on_behalf:
        return get_user_tz(on_behalf)
    if user and not user.is_api_key:
        return get_user_tz(user.sub)
    return None


async def _validate_task_overrides(
    agent: str, layer: str | None, model: str | None,
) -> None:
    """Reject a per-task model/engine pin outside the agent's envelope.

    Delegates to the SAME validator the delegate-spawn path uses
    (``spawn_authz.validate_spawn_overrides``) so a schedule and a delegation
    can never advertise different sets: layer must be one of the agent's
    enabled execution paths, model must be enabled on that layer. Raises 400.
    Call after the agent-access check and before any row write.
    """
    if not (layer or model):
        return
    from services.delegation.spawn_authz import validate_spawn_overrides
    await asyncio.to_thread(validate_spawn_overrides, agent, layer, model)


# --- Scope + permission helpers ---


def _enforce_task_scope(user: UserContext, scope: str, agent: str) -> None:
    """Check that the user has permission to create tasks with this scope.

    Editor + manager + admin can create agent-scope tasks
    (collaborative workspace tier). Each editor's tasks are visible to
    the whole team but only the creator (or an owner) can mutate them
    — enforced in ``_check_task_permission`` for PATCH/DELETE.
    """
    # Visibility-modes: the agent's mode determines which scopes it offers.
    # Personal-only → "user" only; Shared-only (incl. internal/caller) →
    # "agent" only; collaborative → both. Defense-in-depth on top of the
    # schedules-mcp scope-enum filter.
    from core.session.visibility import available_scopes_for
    _row = agent_store.get_agent(agent) or {}
    _avail = available_scopes_for(
        bool(_row.get("collaborative", True)), _row.get("default_scope") or "user",
    )
    if scope not in _avail:
        raise HTTPException(
            status_code=400,
            detail=f"This agent does not support {scope!r}-scoped tasks "
                   f"(mode offers: {', '.join(_avail)})",
        )
    if scope == _vis.SCOPE_USER:
        return  # any authenticated user
    elif scope == _vis.SCOPE_AGENT:
        # No-user / service sessions create agent-scope work for their own
        # agent (the agent gate, ``_check_agent_access_s2s`` on the token's
        # agent, keeps them to it and its wired targets); a real user (cookie
        # or USER_SESSION) is gated by editor role.
        if not user.can_edit_agent(agent) and not (
            user.is_no_user_session or user.is_service
        ):
            raise HTTPException(
                status_code=403,
                detail="Agent-scoped tasks require editor, manager, or admin role for this agent",
            )
    else:
        raise HTTPException(status_code=400, detail=f"Invalid scope: {scope}")


def _resolve_creator_identity(
    user: UserContext, scope: str, x_agent_name: str | None,
) -> tuple[str, str | None]:
    """Authoritatively resolve ``(created_by, acting_sub)`` for a new task from
    the AUTHENTICATED identity only. Any client-supplied ``created_by`` /
    ``X-On-Behalf-Of`` is ignored — the server attributes.

    - Real user (dashboard cookie / real-user-backed session token):
      ``created_by = acting_sub = user.sub``.
    - No-user session (phone/agent service): user-scope → 403 (no identity);
      agent-scope → ``created_by = user.agent`` (``x_agent_name`` is the
      token's agent here, ``_caller_agent``), no acting_sub.
    - Master key (s2s): user-scope → 400 (must come from a real session);
      agent-scope → ``created_by = x_agent_name or "api"``, no acting_sub.
    """
    acting = user.acting_sub
    if acting is not None:
        return acting, acting
    # No real user identity (master key or no-user service session).
    if scope == _vis.SCOPE_USER:
        if user.is_no_user_session:
            raise HTTPException(
                status_code=403,
                detail="This session has no user identity and cannot create "
                       "user-scoped tasks.",
            )
        raise HTTPException(
            status_code=400,
            detail="User-scoped tasks cannot be created with the master API key; "
                   "they must be created from a user session.",
        )
    return (x_agent_name or user.agent or "api"), None


def _check_task_permission(task_data: dict, user: UserContext) -> None:
    """Enforce user-level permissions for task mutation using the
    AUTHENTICATED identity only (never a client header).

      - master key: full service-to-service access.
      - no-user session (phone/agent service): DENIED on user-scoped tasks
        (it has no identity); may manage agent-scope tasks on its own agent
        (agent access is gated separately by ``_check_agent_access_s2s``).
      - real user (dashboard cookie / real-user-backed session token):
        user-scope → only the creator; agent-scope → admin/manager any,
        editor only own, viewer none.
    """
    acting = user.acting_sub
    if acting is None:
        # Master key OR no-user session.
        if user.is_no_user_session and task_data.get("scope", _vis.SCOPE_USER) == _vis.SCOPE_USER:
            raise HTTPException(
                403,
                "This session has no user identity and cannot modify "
                "user-scoped tasks.",
            )
        return  # master key: full s2s; no-user: agent-scope management allowed

    task_scope = task_data.get("scope", _vis.SCOPE_USER)
    if task_scope == _vis.SCOPE_USER:
        if task_data.get("created_by") != acting:
            raise HTTPException(403, "Cannot modify another user's task")
    elif task_scope == _vis.SCOPE_AGENT:
        acting_user = task_store.get_user(acting)
        if not acting_user:
            raise HTTPException(403, "Unknown user")
        role = roles.effective_role(
            acting_user["role"], task_store.get_user_agent_roles(acting), task_data.get("agent", ""))
        if roles.is_admin(role):
            return  # platform admin: full mutation rights
        if roles.may_mutate_shared(role, own=task_data.get("created_by") == acting):
            return  # owner tier: any agent-scope task; editor: their own
        if role == roles.EDITOR:
            raise HTTPException(
                403,
                "Editors can mutate only their own agent-scope tasks "
                "(manager required to mutate another collaborator's task)",
            )
        # Viewer / unknown role: denied
        raise HTTPException(
            403, "Agent-scoped tasks require editor, manager, or admin role for this agent",
        )
    else:
        # A scope the vocabulary does not know never falls through to allow.
        raise HTTPException(403, f"Unknown task scope {task_scope!r}")


def _check_run_access(run: dict, user: UserContext) -> None:
    """Check if user can access a task run based on scope/creator.

    Rules:
    - master key (s2s): full access
    - Agent-scoped runs (or legacy runs without scope): any user with agent access
    - User-scoped runs: only the creator (or admin)

    Only the master key bypasses — a session token (interactive USER_SESSION or a
    no-user AGENT_SESSION) is held to the same agent-access + ownership checks as
    a dashboard cookie, so it cannot read another user's run by id.
    """
    if user.is_service:
        return
    if not user.can_access_agent(run["agent"]):
        # A delegation edge lets a NO-USER session read the target's
        # AGENT-SCOPE runs by id (user-scope stays out — rule 1).
        if not (run.get("scope", _vis.SCOPE_AGENT) != _vis.SCOPE_USER
                and run["agent"] in nouser_read_targets(user)):
            raise HTTPException(403, "Not authorized to access this agent's runs")
    run_scope = run.get("scope", "agent")
    if run_scope == _vis.SCOPE_USER and run.get("created_by") != user.sub:
        if not user.is_admin:
            raise HTTPException(403, "Not authorized to access this run")


async def _check_callback_anchor(
    user: UserContext, agent: str | None, session_id: str | None, chat_id: str | None,
) -> None:
    """An on-complete callback hands the run's result to a live session as a
    prompt, so a caller names only its own: its session (the token's
    ``sid``), that session's chat and its agent — the anchor
    ``spawn_delegate`` takes. A cookie has no session and names none. The
    master key is trusted s2s. Raises 403."""
    if user.is_service:
        return
    if agent and agent != user.agent:
        raise HTTPException(403, "An on-complete callback returns only to the calling agent")
    if session_id and session_id != user.session_id:
        raise HTTPException(403, "An on-complete callback returns only to the calling session")
    if chat_id:
        own = (await asyncio.to_thread(task_store.get_chat_by_session, user.session_id)
               if user.session_id else None)
        if not own or own["id"] != chat_id:
            raise HTTPException(
                403, "An on-complete callback returns only to the calling session's chat",
            )


def _scope_filter_sub(user: UserContext, agent: str | None, audit: bool = False) -> str | None:
    """Return user.sub for scope filtering, or None to skip filtering.

    Skipped for the master key and the admin AUDIT surface (``audit=true`` — the
    admin Task History page) so it sees every user's runs. An admin on an agent's
    settings tab (``audit=false``) still gets the user-view, like any other user.
    ``agent`` stays a plain filter in both modes.
    """
    if user.is_service:
        return None  # master key (service-to-service) — unfiltered
    if user.is_admin and audit:
        return None  # admin audit page: show all runs
    # Real user (cookie / USER_SESSION) → own runs. A no-user session owns no
    # user-scope run: its synthetic ``session:<sid>`` sub matches no creator,
    # so it reads agent-scope runs only, as list_tasks shows it.
    return user.acting_sub if user.acting_sub is not None else user.sub


# --- Endpoints ---


@router.get("/v1/session/current")
async def get_current_session(
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Resolve the CALLER'S OWN session for callback routing — NOT identity.

    Returns only ``{session_id, chat_id, last_active}`` — the anchors an MCP
    needs to route a background-task callback back to the originating chat.
    It deliberately carries NO identity (``user_sub`` / ``user_name`` /
    ``username``): the proxy attributes identity from the caller's signed
    session token, so an MCP must never learn "who the user is" from here.

    Resolution keys off the caller's OWN ``session_id`` (from its session JWT),
    never an agent-name + recency scan. That is the structural fix for the
    identity-bleed bug — two concurrent sessions on the same agent each resolve
    ONLY their own session, so identities can never be mixed.
    """
    u = require_auth(user)

    from core.session.session_state import _sessions

    # Session-JWT caller (every stdio MCP) → resolve ITS OWN session by sid.
    if u.session_id:
        meta = _sessions.get(u.session_id)
        chat = await asyncio.to_thread(task_store.get_chat_by_session, u.session_id)
        return {
            "session_id": u.session_id,
            "chat_id": chat["id"] if chat else None,
            "last_active": meta.get("last_active") if meta else None,
        }

    # No session_id in the token. A dashboard cookie has no single "current
    # agent session" to resolve here; only a trusted master-key s2s caller
    # gets the explicit X-Agent-Name most-recently-active lookup.
    if not u.is_service or not x_agent_name:
        return {"session_id": None, "chat_id": None, "last_active": None}

    from core.session.session_state import _dashboard_notify_queues
    # Exclude task sessions (is_task=True) — they share the agent name but must
    # never be returned as the "current" interactive session.
    agent_sessions = [
        (sid, meta) for sid, meta in _sessions.items()
        if meta.get("agent") == x_agent_name and not meta.get("is_task")
    ]
    if not agent_sessions:
        return {"session_id": None, "chat_id": None, "last_active": None}
    # Prefer sessions with an active dashboard WS (can receive live delivery).
    dashboard_sessions = [
        (sid, meta) for sid, meta in agent_sessions if sid in _dashboard_notify_queues
    ]
    pool = dashboard_sessions if dashboard_sessions else agent_sessions
    latest_sid, latest_meta = max(pool, key=lambda x: x[1].get("last_active", ""))
    chat = await asyncio.to_thread(task_store.get_chat_by_session, latest_sid)
    # Prefer a candidate that actually has a chat over an empty pre-warmed one.
    if not chat and len(pool) > 1:
        for sid, meta in sorted(pool, key=lambda x: x[1].get("last_active", ""), reverse=True):
            if sid == latest_sid:
                continue
            alt_chat = await asyncio.to_thread(task_store.get_chat_by_session, sid)
            if alt_chat:
                latest_sid, latest_meta, chat = sid, meta, alt_chat
                break
    return {
        "session_id": latest_sid,
        "chat_id": chat["id"] if chat else None,
        "last_active": latest_meta.get("last_active"),
    }


@router.post("/v1/tasks/scheduled")
async def create_scheduled_task(
    req: CreateScheduledTaskRequest,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    u = require_auth(user)
    _enforce_task_scope(u, req.scope, req.agent)
    if u.is_api_key:
        _check_agent_access_s2s(req.agent, _caller_agent(u, x_agent_name))
        if u.acting_sub is not None:
            # A delegation edge is the agent's reach, not the person's: a
            # session minted for a person reaches only the agents they can.
            require_agent_access(u, req.agent)
    else:
        # No platform-level require_write(u): _enforce_task_scope already gates
        # by scope (agent-scope needs per-agent editor+; user-scope any user)
        # and require_agent_access gates agent visibility — so a platform
        # "member" who is a per-agent manager/editor can still schedule tasks.
        require_agent_access(u, req.agent)

    # Exactly one timing primitive must be set for a recurring task.
    if req.schedule and req.interval_seconds is not None:
        raise HTTPException(
            400,
            "schedule and interval_seconds are mutually exclusive — pick one",
        )
    if not req.schedule and req.interval_seconds is None:
        raise HTTPException(
            400,
            "Recurring tasks require either schedule (cron) or interval_seconds",
        )
    if req.interval_seconds is not None:
        err = scheduler._validate_interval_seconds(req.interval_seconds)
        if err:
            raise HTTPException(400, err)
    await _validate_task_overrides(req.agent, req.layer, req.model)

    created_by, acting_sub = _resolve_creator_identity(u, req.scope, _caller_agent(u, x_agent_name))
    checks = await _validate_checks(req.agent, req.checks, u)
    task_id = f"dyn-{uuid.uuid4().hex[:8]}"
    user_tz = _resolve_user_tz(req.user_tz, u, acting_sub)
    task = scheduler.TaskDefinition(
        id=task_id,
        name=req.name,
        agent=req.agent,
        prompt=req.prompt,
        schedule=req.schedule or "",
        interval_seconds=req.interval_seconds,
        llm_mode=req.llm_mode,
        timeout_seconds=req.timeout_seconds,
        source="dynamic",
        created_by=created_by,
        scope=req.scope,
        notification_mode=req.notification_mode,
        notify_severity=req.notify_severity,
        user_tz=user_tz,
        override_model=req.model or None,
        override_execution_path=req.layer or None,
        checks=checks,
    )
    async with task_quota.creating():
        await task_quota.enforce_task_caps(u, req.agent, created_by, req.scope)
        await scheduler.add_dynamic_task(task)
    timing_desc = (
        f"schedule={req.schedule!r}" if req.schedule
        else f"interval={req.interval_seconds}s"
    )
    logger.info(
        f"Created scheduled task: {task_id} scope={req.scope} {timing_desc} "
        f"by {created_by} tz={user_tz or '-'} notify={req.notification_mode}"
    )
    return {"task_id": task_id, "status": "created"}


@router.post("/v1/tasks/one-time")
async def create_one_time_task(
    req: CreateOneTimeTaskRequest,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    u = require_auth(user)
    _enforce_task_scope(u, req.scope, req.agent)
    if u.is_api_key:
        _check_agent_access_s2s(req.agent, _caller_agent(u, x_agent_name))
        if u.acting_sub is not None:
            require_agent_access(u, req.agent)  # see create_scheduled_task
    else:
        # No platform-level require_write(u): _enforce_task_scope already gates
        # by scope (agent-scope needs per-agent editor+; user-scope any user)
        # and require_agent_access gates agent visibility — so a platform
        # "member" who is a per-agent manager/editor can still schedule tasks.
        require_agent_access(u, req.agent)
    await _check_callback_anchor(
        u, req.on_complete_agent, req.on_complete_session_id, None,
    )

    # Cross-agent delegation validation
    if req.source_agent and req.source_agent != req.agent:
        allowed = await asyncio.to_thread(agent_store.get_delegation_targets, req.source_agent)
        if req.agent not in allowed:
            raise HTTPException(
                403,
                f"Agent '{req.source_agent}' is not allowed to delegate to '{req.agent}'. "
                f"Configure delegation targets in agent settings.",
            )
    await _validate_task_overrides(req.agent, req.layer, req.model)

    created_by, acting_sub = _resolve_creator_identity(u, req.scope, _caller_agent(u, x_agent_name))
    checks = await _validate_checks(req.agent, req.checks, u)
    task_id = f"dyn-{uuid.uuid4().hex[:8]}"
    user_tz = _resolve_user_tz(req.user_tz, u, acting_sub)

    # Resolve task_type: explicit value (e.g. 'trigger' for trigger-only
    # tasks) takes precedence; this route creates the kinds that need no
    # clock (task_kinds.unscheduled: one_time, trigger).
    task_type = req.task_type or task_kinds.ONE_TIME
    kind = task_kinds.of_word(task_type)
    if kind is None or not kind.unscheduled:
        raise HTTPException(400, f"Invalid task_type: {task_type!r}")
    # Trigger-only tasks must NOT have run_at / delay_seconds — they only
    # fire when a webhook trigger fires them.
    if task_type == task_kinds.TRIGGER and (req.run_at or req.delay_seconds is not None):
        raise HTTPException(
            400,
            "task_type='trigger' tasks cannot have run_at or delay_seconds; "
            "they fire only via webhook triggers.",
        )

    task = scheduler.TaskDefinition(
        id=task_id,
        name=req.name,
        agent=req.agent,
        prompt=req.prompt,
        run_at=req.run_at,
        delay_seconds=req.delay_seconds,
        llm_mode=req.llm_mode,
        timeout_seconds=req.timeout_seconds,
        source="dynamic",
        created_by=created_by,
        on_complete_agent=req.on_complete_agent,
        on_complete_prompt=req.on_complete_prompt,
        on_complete_session_id=req.on_complete_session_id,
        continue_session=req.continue_session,
        use_persistent=req.use_persistent,
        scope=req.scope,
        notification_mode=req.notification_mode,
        notify_severity=req.notify_severity,
        user_tz=user_tz,
        task_type=task_type,
        override_model=req.model or None,
        override_execution_path=req.layer or None,
        checks=checks,
    )
    async with task_quota.creating():
        if task_type in task_quota.COUNTED_KINDS:
            await task_quota.enforce_task_caps(u, req.agent, created_by, req.scope)
        await scheduler.add_dynamic_task(task)
    logger.info(
        f"Created task: {task_id} type={task_type} scope={req.scope} "
        f"by {created_by} tz={user_tz or '-'} notify={req.notification_mode}"
    )
    return {"task_id": task_id, "status": "created"}


@router.get("/v1/tasks")
async def list_tasks(
    agent: str | None = Query(None),
    audit: bool = Query(False),
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    all_tasks = await run_db(scheduler.get_all_task_definitions)
    # Exclude delegate tasks (background tasks with callback, not real schedules)
    all_tasks = [t for t in all_tasks if not t.use_persistent]
    if agent:
        all_tasks = [t for t in all_tasks if t.agent == agent]
    # Filter by accessible agents for everyone except the master key.
    # Session JWTs are api-key-shaped but carry a real (or no-user)
    # identity — they must NOT see agents outside their reach (H2: keying
    # this on is_api_key let any agent session list every agent's tasks).
    # Delegation edges extend a NO-USER session's reach to the targets'
    # AGENT-SCOPE definitions only (user-scope also dies below — no acting
    # sub); user-backed sessions never consult the roster.
    if not u.is_service:
        edge_reach = await asyncio.to_thread(nouser_read_targets, u)
        all_tasks = [
            t for t in all_tasks
            if u.can_access_agent(t.agent)
            or (t.scope == _vis.SCOPE_AGENT and t.agent in edge_reach)
        ]

    # Scope-aware filtering. Agent-scoped items are shared; a user's user-scoped
    # items are private to them. The admin AUDIT surface (``audit=true``, admin
    # only — the admin Scheduled Tasks page) sees every user's items so it can
    # audit; every other caller — INCLUDING an admin on an agent's settings tab —
    # gets the user-view (own user-scoped + agent-scoped). ``agent`` stays a plain
    # filter in both modes. Keyed on is_service like _scope_filter_sub (H1:
    # keying on is_api_key exempted agent-session callers, so an agent could
    # list OTHER users' user-scoped task definitions — prompts included — on
    # the same agent; the runs endpoint already filtered correctly).
    audit_view = audit and u.is_admin
    if not u.is_service and not audit_view:
        filtered = []
        for t in all_tasks:
            if t.scope == _vis.SCOPE_AGENT:
                filtered.append(t)
            elif (t.scope == _vis.SCOPE_USER and u.acting_sub is not None
                    and t.created_by == u.acting_sub):
                filtered.append(t)
        all_tasks = filtered

    # The next fires, one map (the in-memory jobs; standalone computes once).
    next_fires = await run_db(scheduler.next_run_times)
    resolve = _make_model_resolver()
    # The run counts, one grouped query (see _run_count_for).
    counts = await run_db(task_store.count_runs_by_task, [t.id for t in all_tasks])
    platform_tz = config.get_platform_timezone()
    from services.agents import offboarding_transfer
    moved_from = await run_db(offboarding_transfer.transferred_from_names,
                              [t.transferred_from for t in all_tasks])
    views = []
    for t in all_tasks:
        job = {"next_run_time": next_fires[t.id]} if t.id in next_fires else None
        d = _task_view(t, u, resolve, job, platform_tz, moved_from=moved_from)
        d["run_count"] = _run_count_for(t, counts.get(t.id, 0))
        views.append(d)
    return {"tasks": views}


def _run_count_for(t, history_count: int) -> int:
    """How many times a task ran, as the task views report it. A recurring
    continuation keeps the row counter ``dynamic_tasks.run_count`` — its
    ``max_runs`` bound, which also counts dead-chat skips; every other kind
    reads the run history, because the row counter is never incremented for
    them (it read 0 on a task with hundreds of morning runs, 2026-09-17)."""
    if getattr(t, "task_type", "") == task_kinds.CONTINUATION:
        return max(int(getattr(t, "run_count", 0) or 0), history_count)
    return history_count


def _make_model_resolver():
    """What a task will ACTUALLY run on: its own pins, else the agent's
    default resolved WITH the effective layer (the runner's rule: an agent
    default the pinned engine does not serve is skipped for that engine's
    own first choice). Memoized per (agent, layer): the admin listing spans
    agents and every resolution hits the DB. An install with no enabled
    model for the layer resolves to "" = unresolved, which the UI renders
    as nothing rather than a wrong claim."""
    from api.agents._common import _get_execution_paths
    from storage.billing import subscription_store

    agent_rows: dict[str, dict] = {}
    defaults: dict[tuple[str, str], str] = {}
    layer_rows: dict[str, list[dict]] = {}

    def _agent(slug: str) -> dict:
        if slug not in agent_rows:
            agent_rows[slug] = agent_store.get_agent(slug) or {}
        return agent_rows[slug]

    def _models(layer: str) -> list[dict]:
        if layer not in layer_rows:
            try:
                layer_rows[layer] = subscription_store.list_models(layer)
            except Exception:
                layer_rows[layer] = []
        return layer_rows[layer]

    def resolve(t) -> dict:
        # getattr, not attribute access: the listing also renders duck-typed
        # task defs (the scoping tests' minimal stubs), and task_config_builder
        # reads the same fields the same way.
        override_model = getattr(t, "override_model", None) or ""
        override_path = getattr(t, "override_execution_path", None) or ""
        path = override_path or (_agent(t.agent).get("execution_path") or "")
        key = (t.agent, path)
        if key not in defaults:
            try:
                defaults[key] = config.resolve_agent_model(t.agent, layer=path or None)
            except Exception:
                defaults[key] = ""
        model = override_model or defaults[key]
        if override_model:
            source = "pinned"
        elif override_path:
            source = "layer default"
        else:
            source = "agent default"
        tier, tier_label, _good_at = config.get_model_tier(model) if model else (None, "", "")
        warnings: list[str] = []
        if override_path and override_path not in _get_execution_paths(_agent(t.agent)):
            warnings.append(f"the pinned engine '{override_path}' is not enabled for this agent")
        if override_model:
            rows = [m for m in _models(path) if m.get("model_id") == override_model]
            if not rows:
                warnings.append(f"the pinned model '{override_model}' is not offered on '{path}'")
            elif not any(m.get("enabled") for m in rows):
                warnings.append(f"the pinned model '{override_model}' is disabled")
        return {
            "effective_model": model,
            "effective_execution_path": path,
            "effective_model_source": source,
            "effective_model_tier": tier,
            "tier_label": tier_label,
            "pin_warnings": warnings,
        }

    return resolve


def _task_view(t, u: UserContext, resolve, job: dict | None,
               platform_tz: str | None = None, *, moved_from: dict | None = None) -> dict:
    """One task row as both schedules pages and the schedules MCP read it:
    the definition, what it runs on, the zone it fires in, its schedule in
    words, who it was transferred from, and the caller's permission flags.
    ``moved_from`` is ``offboarding_transfer.transferred_from_names`` for the
    rows being shown."""
    d = t.model_dump()
    d["transferred_from_name"] = (moved_from or {}).get(d.get("transferred_from") or "", "")
    d["next_run_time"] = job["next_run_time"] if job else None
    # The zone the cron and a naive run_at are read in: the row's own, else
    # the platform's — no non-admin endpoint carries the platform zone, and
    # a tab showing "Daily at 08:00" beside a browser-zone "Next" needs it.
    # getattr: the listing also renders duck-typed rows (the scoping tests).
    d["effective_tz"] = getattr(t, "user_tz", None) or platform_tz or config.get_platform_timezone()
    d["schedule_text"] = schedule_text.describe(d)
    if getattr(t, "task_type", "") == task_kinds.APP:
        # An app handler row runs no LLM turn: no model claim on it.
        d.update({"effective_model": "", "effective_execution_path": "",
                  "effective_model_source": "", "effective_model_tier": None,
                  "tier_label": "", "pin_warnings": []})
    else:
        d.update(resolve(t))

    # Permission metadata for UI (3-tier per-agent model).
    # owner = manager + admin; can mutate any task on the agent.
    # editor = workspace collaborator; can mutate ONLY own tasks.
    # viewer = read-only.
    can_manage = u.is_service or u.can_manage_agent(t.agent)
    can_edit = u.is_service or u.can_edit_agent(t.agent)
    is_own = t.created_by == u.sub
    is_own_user_scope = t.scope == _vis.SCOPE_USER and is_own
    is_own_agent_scope = t.scope == _vis.SCOPE_AGENT and is_own
    d["can_run"] = (
        u.is_service or is_own_user_scope or can_manage
        or (is_own_agent_scope and can_edit)
    )
    d["can_delete"] = (
        u.is_service or is_own_user_scope
        or (t.scope == _vis.SCOPE_AGENT and can_manage)
        or (is_own_agent_scope and can_edit)
    )
    # Pause/resume share the same authority as delete; the available
    # action depends on current enabled state.
    d["can_pause"] = d["can_delete"] and t.enabled
    d["can_resume"] = d["can_delete"] and not t.enabled
    return d


async def _delete_task_impl(task_id: str, user: UserContext, x_agent_name: str | None):
    """Shared delete logic for DELETE and POST endpoints."""
    dyn = await asyncio.to_thread(task_store.get_dynamic_task, task_id)
    if not dyn:
        raise HTTPException(status_code=404, detail="Task not found")

    if user.is_api_key:
        _check_agent_access_s2s(dyn["agent"], _caller_agent(user, x_agent_name))
        _check_task_permission(dyn, user)
    else:
        require_agent_access(user, dyn["agent"])
        # Dashboard users: check ownership.
        # Agent-scope: manager can delete any; editor can delete own.
        task_scope = dyn.get("scope", "user")
        if task_scope == _vis.SCOPE_USER and dyn.get("created_by") != user.sub:
            raise HTTPException(403, "Cannot delete another user's task")
        if task_scope == _vis.SCOPE_AGENT:
            if user.can_manage_agent(dyn["agent"]):
                pass  # owner: any
            elif user.can_edit_agent(dyn["agent"]) and dyn.get("created_by") == user.sub:
                pass  # editor: only own
            else:
                raise HTTPException(
                    403,
                    "Agent-scoped tasks can be deleted by their creator (editor+) "
                    "or any manager/admin",
                )

    deleted = await scheduler.remove_dynamic_task(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "deleted", "task_id": task_id}


@router.delete("/v1/tasks/{task_id}")
async def delete_task(
    task_id: str,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    u = require_auth(user)
    return await _delete_task_impl(task_id, u, x_agent_name)


@router.post("/v1/tasks/{task_id}/delete")
async def delete_task_post(
    task_id: str,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """POST-based delete — avoids IPS rules that block HTTP DELETE."""
    u = require_auth(user)
    return await _delete_task_impl(task_id, u, x_agent_name)


async def _resolve_pause_resume_target(
    task_id: str, user: UserContext, x_agent_name: str | None,
) -> dict:
    """Shared lookup + permission check for pause/resume.

    Returns the dynamic_task row. Raises 404 for missing.
    """
    dyn = await asyncio.to_thread(task_store.get_dynamic_task, task_id)
    if not dyn:
        raise HTTPException(status_code=404, detail="Task not found")
    if user.is_api_key:
        _check_agent_access_s2s(dyn["agent"], _caller_agent(user, x_agent_name))
    else:
        require_agent_access(user, dyn["agent"])
    _check_task_permission(dyn, user)
    return dyn


@router.post("/v1/tasks/{task_id}/pause")
async def pause_task(
    task_id: str,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Pause a dynamic task: flip enabled=FALSE and unregister APScheduler job."""
    u = require_auth(user)
    await _resolve_pause_resume_target(task_id, u, x_agent_name)
    await scheduler.pause_dynamic_task(task_id)
    return {"status": "paused", "task_id": task_id}


@router.post("/v1/tasks/{task_id}/resume")
async def resume_task(
    task_id: str,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Resume a paused dynamic task: flip enabled=TRUE and re-register job.

    For one-time tasks whose ``run_at`` has passed, the row stays enabled but
    no APScheduler job is registered — the user can fire manually via the Run
    button.
    """
    u = require_auth(user)
    dyn = await _resolve_pause_resume_target(task_id, u, x_agent_name)
    async with task_quota.creating():
        # A resumed row counts again: the same cap a create meets.
        await task_quota.enforce_newly_counted(u, dyn, {**dyn, "enabled": True})
        await scheduler.resume_dynamic_task(task_id)
    return {"status": "resumed", "task_id": task_id}


class EditTaskRequest(BaseModel):
    name: str | None = None
    prompt: str | None = None
    schedule: str | None = None
    run_at: str | None = None
    interval_seconds: int | None = None
    timeout_seconds: int | None = None
    # Optional on edit (None = leave unchanged). Same enum as create.
    notification_mode: Literal["auto", "manual", "none"] | None = None
    notify_severity: str | None = None
    user_tz: str | None = None
    # Per-task execution overrides. "" (or null) CLEARS the pin — the task
    # goes back to the agent's default on its next run; a non-empty value is
    # validated against the agent's envelope like it is on create. Omit the
    # field entirely to leave the current pin untouched.
    model: str | None = None
    layer: str | None = None
    # Checks (CHECKS.md): the whole list; [] clears it. Omit to leave it.
    checks: list[str] | None = None


# The edit fields with no null state: omitted leaves them, null is refused.
_EDIT_REQUIRED = ("name", "prompt", "timeout_seconds", "notification_mode", "notify_severity")


async def _validate_checks(agent: str, names: list[str] | None, u: UserContext) -> list[str]:
    """The refs of the named checks (CHECKS.md): each must be one of the
    agent's checks or one of the caller's own; a mandatory one needs no
    naming and is dropped."""
    if not names:
        return []
    from services.checks import documents as _checks
    username = ""
    if u.acting_sub:
        username = await asyncio.to_thread(task_store.get_username_by_sub, u.acting_sub) or ""
    refs: list[str] = []
    for raw in names:
        if not isinstance(raw, str) or not raw.strip():
            continue
        it = await asyncio.to_thread(_checks.resolve_ref, agent, raw.strip(), username)
        if it is None:
            raise HTTPException(400, f"No check named {raw!r} on this agent (or among your own)")
        if it.mandatory:
            continue
        if it.ref not in refs:
            refs.append(it.ref)
    if len(refs) > 16:
        raise HTTPException(400, "At most 16 checks on one task")
    return refs


def _manages_agent(user: UserContext, agent: str) -> bool:
    """Whether the acting person holds the manager tier on ``agent``
    (a platform admin does everywhere). Synchronous: the DB executor."""
    acting = user.acting_sub
    if not acting:
        return False
    row = task_store.get_user(acting)
    if not row:
        return False
    return roles.can_manage(
        roles.effective_role(row["role"], task_store.get_user_agent_roles(acting), agent))


async def _edit_task_impl(
    task_id: str, req: EditTaskRequest, user: UserContext,
    x_agent_name: str | None,
):
    """Shared implementation for PATCH and POST /edit endpoints."""
    dyn = await asyncio.to_thread(task_store.get_dynamic_task, task_id)
    if not dyn:
        raise HTTPException(status_code=404, detail="Task not found")
    if user.is_api_key:
        _check_agent_access_s2s(dyn["agent"], _caller_agent(user, x_agent_name))
    else:
        require_agent_access(user, dyn["agent"])
    _check_task_permission(dyn, user)

    # exclude_unset: only fields the caller explicitly supplied flow through.
    # That's how we tell "leave field alone" apart from "explicitly set to None".
    fields = req.model_dump(exclude_unset=True)
    # A NULL in a column the task definition requires is a row no listing,
    # fire or scheduler start can load — refused, never written.
    nulled = [k for k in _EDIT_REQUIRED if k in fields and fields[k] is None]
    if nulled:
        raise HTTPException(status_code=400, detail=f"{', '.join(nulled)} cannot be null")
    # schedule / run_at / interval_seconds can be explicitly None (mode switch
    # signal) — the service layer handles cross-field NULL-ing. But at least
    # one editable field must be present.
    if not fields:
        raise HTTPException(
            status_code=400,
            detail="At least one editable field must be provided",
        )
    # Reject contradictory: caller can't set more than one timing primitive
    # to a non-null value in the same request.
    non_null_timing = sum(
        1 for k in ("schedule", "run_at", "interval_seconds") if fields.get(k)
    )
    if non_null_timing > 1:
        raise HTTPException(
            status_code=400,
            detail="schedule, run_at, and interval_seconds are mutually exclusive — set only one",
        )

    # Per-task execution overrides: request names (model/layer, matching
    # delegate()) → column names. Empty/None clears the pin back to the
    # agent's default; "" rather than NULL keeps every row's inherit-state
    # spelled the same way. Validate against the EFFECTIVE layer — a
    # model-only edit on a task that already pins a layer must be checked
    # against that layer, not the agent's primary one.
    if "model" in fields or "layer" in fields:
        new_layer = (fields.get("layer") if "layer" in fields
                     else dyn.get("override_execution_path")) or None
        new_model = (fields.get("model") if "model" in fields
                     else dyn.get("override_model")) or None
        await _validate_task_overrides(dyn["agent"], new_layer, new_model)
        if "model" in fields:
            fields["override_model"] = fields.pop("model") or ""
        if "layer" in fields:
            fields["override_execution_path"] = fields.pop("layer") or ""
    if "checks" in fields:
        refs = await _validate_checks(dyn["agent"], fields.pop("checks") or [], user)
        fields["checks"] = json.dumps(refs) if refs else ""
    adopter = ""
    if "prompt" in fields and (dyn.get("transferred_from") or ""):
        # A manager of the agent (or an admin) who rewrites a transferred
        # task's prompt adopts it: the row becomes theirs, so their own
        # provenance decides the knowledge-write grant and their own loss of
        # standing moves it on. An editor's edit keeps the record.
        if await run_db(_manages_agent, user, dyn["agent"]):
            adopter = user.acting_sub or ""
            fields["transferred_from"] = ""
            fields["created_by"] = adopter

    timed = any(fields.get(k) for k in ("schedule", "run_at", "interval_seconds"))
    if timed and (dyn.get("task_type") or "") not in task_quota.COUNTED_KINDS + (task_kinds.TRIGGER,):
        # A timing field re-derives the kind: a self-continuation, a delegate
        # worker or a judge run would silently become a scheduled task.
        raise HTTPException(
            status_code=400,
            detail="This task's timing cannot be edited; schedule a new task instead",
        )
    kind = task_kinds.of_word(dyn.get("task_type"))
    if timed and dyn.get("community_template") and kind is not None and not kind.clocked:
        # A seeded row counts for nobody because its clock is the
        # installer's decision. A clock given by an edit is the editor's:
        # the row stops being the template's (its item slug stays, so the
        # template updater still finds it) and counts from now on.
        fields["community_template"] = None
    # A timing edit can make a row count toward the caps (a trigger task
    # given an interval): judged as a create would be, under the same lock.
    after = {**dyn, **fields}
    if fields.get("schedule") or fields.get("interval_seconds"):
        after["task_type"] = task_kinds.SCHEDULED
    elif fields.get("run_at"):
        after["task_type"] = task_kinds.ONE_TIME
    async with task_quota.creating():
        await task_quota.enforce_newly_counted(user, dyn, after)
        ok, err = await scheduler.update_dynamic_task(task_id, fields)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if not ok:
        raise HTTPException(status_code=404, detail="Task not found")
    if adopter and (dyn.get("task_type") or "") == task_kinds.TRIGGER:
        # A trigger and the task it fires keep one creator.
        await run_db(trigger_store.adopt_linked, task_id, dyn.get("created_by") or "", adopter)
    if (fields.get("name") or "").strip():
        # Task-history rows AND the Active-now widget label task rows by the
        # task's NAME — re-label the chats of live (running|pending) runs
        # everywhere now via title_updated; idle rows refresh on the next
        # list poll. Best-effort, sanitized like a chat rename (the frame
        # reaches OTHER users' sidebars).
        try:
            from api.agents.chats import _sanitize_title
            new_name = _sanitize_title(str(fields["name"]))
            chat_ids = await asyncio.to_thread(
                task_store.list_live_run_chats, task_id) if new_name else []
            if chat_ids:
                from services.notifications import notification_manager
                for cid in chat_ids:
                    row = await asyncio.to_thread(task_store.get_chat, cid)
                    if row:
                        notification_manager.broadcast_chat_title(
                            row.get("user_sub") or "", cid, new_name,
                            agent=row.get("agent") or "",
                        )
        except Exception:
            logger.debug("task-rename title broadcast failed for %s",
                         task_id, exc_info=True)
    return {"status": "updated", "task_id": task_id}


@router.patch("/v1/tasks/{task_id}")
async def edit_task(
    task_id: str,
    req: EditTaskRequest,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Partial update of a dynamic task. See ``EditTaskRequest`` for editable fields."""
    u = require_auth(user)
    return await _edit_task_impl(task_id, req, u, x_agent_name)


@router.post("/v1/tasks/{task_id}/edit")
async def edit_task_post(
    task_id: str,
    req: EditTaskRequest,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """POST alias for PATCH (avoids IPS rules that block PATCH)."""
    u = require_auth(user)
    return await _edit_task_impl(task_id, req, u, x_agent_name)


class UpdateOnCompleteRequest(BaseModel):
    on_complete_agent: str | None = None
    on_complete_prompt: str | None = None
    on_complete_session_id: str | None = None
    on_complete_chat_id: str | None = None


@router.patch("/v1/tasks/{task_id}/on-complete")
async def update_task_on_complete(
    task_id: str,
    req: UpdateOnCompleteRequest,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Register or clear a callback on an already-running task.

    Used by create_task_and_wait when the inline SSE wait times out —
    it registers the fallback callback so the agent is still notified.
    """
    u = require_auth(user)
    dyn = await asyncio.to_thread(task_store.get_dynamic_task, task_id)
    if not dyn:
        raise HTTPException(status_code=404, detail="Task not found")
    # Registering an on-complete callback mutates the task, so it takes the same
    # gate as DELETE/edit: a token caller passes the s2s agent check, everyone
    # gets the scope-aware ownership check (user-scope → creator only).
    if u.is_api_key:
        _check_agent_access_s2s(dyn["agent"], _caller_agent(u, x_agent_name))
    else:
        require_agent_access(u, dyn["agent"])
    _check_task_permission(dyn, u)
    await _check_callback_anchor(
        u, req.on_complete_agent, req.on_complete_session_id, req.on_complete_chat_id,
    )
    # Auto-populate chat_id from session_id if not provided.
    # This is the persistent anchor — works even if the browser closes,
    # the WS disconnects, or the proxy restarts.
    chat_id = req.on_complete_chat_id
    if not chat_id and req.on_complete_session_id:
        chat = await asyncio.to_thread(
            task_store.get_chat_by_session, req.on_complete_session_id,
        )
        if chat:
            chat_id = chat["id"]

    await asyncio.to_thread(
        task_store.update_dynamic_task_on_complete,
        task_id,
        req.on_complete_agent,
        req.on_complete_prompt,
        req.on_complete_session_id,
        chat_id,
    )
    return {"status": "updated", "task_id": task_id, "chat_id": chat_id}


@router.post("/v1/tasks/{task_id}/run")
async def run_task_now(
    task_id: str,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    u = require_auth(user)

    dyn = await asyncio.to_thread(task_store.get_dynamic_task, task_id)
    if not dyn:
        raise HTTPException(status_code=404, detail="Task not found")
    task_def = scheduler._row_to_task(dyn)

    if u.is_api_key:
        # dyn is guaranteed non-None (404 raised above), so attribution is
        # token-authoritative via _check_task_permission. A no-user session
        # is denied user-scoped runs; agent-scope runs are allowed on the
        # agents the caller reaches.
        _check_agent_access_s2s(task_def.agent, _caller_agent(u, x_agent_name))
        if u.acting_sub is not None:
            require_agent_access(u, task_def.agent)  # see create_scheduled_task
        _check_task_permission(dyn, u)
    else:
        require_agent_access(u, task_def.agent)
        # Dashboard user permission check (per-agent role).
        # User-scope: only the creator can run.
        # Agent-scope: owner (manager/admin) can run any; editor can run own.
        task_scope = task_def.scope
        is_own = task_def.created_by == u.sub
        is_own_user_scope = task_scope == _vis.SCOPE_USER and is_own
        is_own_agent_scope = task_scope == _vis.SCOPE_AGENT and is_own
        if (
            not is_own_user_scope
            and not u.can_manage_agent(task_def.agent)
            and not (is_own_agent_scope and u.can_edit_agent(task_def.agent))
        ):
            raise HTTPException(403, "Not authorized to run this task")

    run_id = await scheduler.trigger_task_now(
        task_def, trigger_type=task_kinds.TRIGGER_MANUAL,
        trigger_source=_caller_agent(u, x_agent_name),
    )

    # Delegated tasks register an on-complete callback to an originating chat.
    # Emit a delegate_spawn to that chat's live pump now that the task has
    # actually started — keyed by the stable task_id, so a rejected delegation
    # never strands a badge. Routed through the pump so it persists in the turn +
    # live-state (push_pump_event is live-only). Falls back to a direct event row
    # if there's no live pump (rare — the delegating agent's turn is normally
    # still streaming when its delegate_task MCP call hits this endpoint).
    on_complete_chat_id = dyn.get("on_complete_chat_id")
    if on_complete_chat_id:
        from core.events.common_events import CommonEvent, DELEGATE_SPAWN
        from core.session.session_state import inject_pump_event
        spawn_data = {
            # "type" rides along so the event-row fallback below persists the
            # same shape the pump stores (the dashboard's history reload keys
            # blocks on event_data["type"], not the event_type column).
            "type": wire.DELEGATE_SPAWN,
            "task_id": task_id,
            "task_name": task_def.name,
            "agent": task_def.agent,
            "prompt_preview": (task_def.prompt or "")[:100],
            # Full prompt — the dashboard's delegate pill expands to it (the
            # preview stays for the collapsed line + older rows).
            "prompt": task_def.prompt or "",
        }
        if not inject_pump_event(
            on_complete_chat_id, CommonEvent(type=DELEGATE_SPAWN, data=spawn_data),
        ):
            await asyncio.to_thread(
                task_store.add_chat_message, on_complete_chat_id, "event", "",
                event_type=wire.DELEGATE_SPAWN, event_data=json.dumps(spawn_data),
            )

    return {"run_id": run_id, "task_id": task_id}


@router.get("/v1/tasks/runs")
async def list_runs(
    agent: str | None = Query(None),
    status: str | None = Query(None),
    task_id: str | None = Query(None),
    session_id: str | None = Query(None),
    created_by: str | None = Query(None),
    audit: bool = Query(False),
    include_delegates: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext | None = Depends(get_current_user),
):
    u = require_auth(user)
    # Only admin / master key can use the created_by filter (admin user dropdown)
    if created_by and not u.is_service and not u.is_admin:
        created_by = None
    scope_sub = _scope_filter_sub(u, agent, audit)
    # The DEFAULT listing excludes delegate runs (surface=chat delegations
    # live in the chat history; API consumers usually want schedules). A
    # direct task_id query is explicit (delegate continue flows resolve
    # output by task id); schedules-mcp passes include_delegates —
    # task-surface background jobs are documented as "visible in the agent's
    # History", and the Active-now widget's task rows click through there
    # expecting to find them.
    exclude = None if (include_delegates or task_id) else "delegate"
    # Accessible-agents filter IN SQL, not a Python post-filter (H2 — session
    # JWTs are api-key-shaped but must stay inside their identity's reach): a
    # post-filter after SQL LIMIT/COUNT returned short pages and an inflated
    # ``total`` on cross-agent listings. Admin passes can_access_agent
    # everywhere, so only non-admin callers are constrained.
    agents_filter: list[str] | None = None
    edge_filter: list[str] | None = None
    if not u.is_service and not u.is_admin:
        reach = set(u.agents)
        if u.agent:
            reach.add(u.agent)
        agents_filter = sorted(reach)
        # Delegation edges add the targets' AGENT-SCOPE runs for a no-user
        # caller — a separate SQL disjunct, NOT a widened agents_filter (the
        # own-agent branch deliberately includes user-scope runs, edge
        # targets must not).
        edge_filter = sorted(
            await asyncio.to_thread(nouser_read_targets, u) - reach
        ) or None
    runs = await asyncio.to_thread(
        task_store.list_runs, limit, offset, agent, status, task_id, session_id,
        scope_user_sub=scope_sub, created_by=created_by,
        exclude_task_type=exclude, agents_filter=agents_filter,
        edge_agents_filter=edge_filter,
    )
    total = await asyncio.to_thread(
        task_store.get_run_count, agent, status, scope_user_sub=scope_sub,
        created_by=created_by, exclude_task_type=exclude,
        agents_filter=agents_filter, edge_agents_filter=edge_filter,
    )
    return {"runs": runs, "total": total, "limit": limit, "offset": offset}


async def _enrich_run_with_delegator(run: dict) -> dict:
    """Add delegating agent info to run dict if this is a delegate task."""
    dyn = await asyncio.to_thread(task_store.get_dynamic_task, run["task_id"])
    if dyn and dyn.get("on_complete_agent"):
        slug = dyn["on_complete_agent"]
        agent_data = await asyncio.to_thread(agent_store.get_agent, slug)
        run["delegating_agent"] = slug
        run["delegating_agent_display_name"] = agent_data["display_name"] if agent_data else slug
        run["delegating_agent_color"] = agent_data.get("color", "") if agent_data else ""
    return run


async def _enrich_run_with_session_cost(run: dict) -> dict:
    """Add session-level cost totals for multi-turn tasks."""
    sid = run.get("session_id")
    if sid:
        total_cost, turn_count = await asyncio.to_thread(task_store.get_session_cost, sid)
        if turn_count > 1:
            run["session_cost_usd"] = total_cost
            run["session_turn_count"] = turn_count
    return run


@router.get("/v1/tasks/runs/{run_id}")
async def get_run(run_id: str, user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    run = await asyncio.to_thread(task_store.get_run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    _check_run_access(run, u)
    run = await _enrich_run_with_delegator(run)
    run = await _enrich_run_with_session_cost(run)
    return run


@router.get("/v1/tasks/runs/by-chat/{chat_id}")
async def get_run_by_chat(chat_id: str, user: UserContext | None = Depends(get_current_user)):
    """Look up a task run by its chat_id (for AgentChat to detect task chats)."""
    u = require_auth(user)
    runs = await asyncio.to_thread(task_store.list_runs, limit=1, chat_id=chat_id)
    if not runs:
        raise HTTPException(status_code=404, detail="No run found for this chat")
    _check_run_access(runs[0], u)
    run = await _enrich_run_with_delegator(runs[0])
    run = await _enrich_run_with_session_cost(run)
    return run


@router.post("/v1/tasks/runs/{run_id}/cancel")
async def cancel_run(run_id: str, user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    # No platform-level require_write(u): _check_run_access + the per-agent role
    # checks below gate cancellation, so a per-agent editor/manager who is a
    # platform "member" can cancel runs they're entitled to.
    run = await asyncio.to_thread(task_store.get_run, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    _check_run_access(run, u)
    if (not u.is_service and u.acting_sub is None
            and run["agent"] != u.agent and run.get("created_by") != u.agent):
        # A no-user session cancels its own agent's runs and the work it
        # started on a wired target: the edge that let _check_run_access
        # read the target's other runs grants no write.
        raise HTTPException(403, "Cancelling another agent's runs is not allowed")
    # Agent-scoped runs: owner can cancel any; editor can cancel
    # only their own runs — a session token held to its person's role like
    # a cookie. _check_run_access already gates on user-scope
    # "created_by == self".
    if u.acting_sub is not None and run.get("scope") != _vis.SCOPE_USER:
        is_own = run.get("created_by") == u.acting_sub
        if u.can_manage_agent(run["agent"]):
            pass  # owner: any
        elif u.can_edit_agent(run["agent"]) and is_own:
            pass  # editor: only own
        else:
            raise HTTPException(
                403,
                "Cancelling agent-scoped runs requires manager role, "
                "or editor role on a run you created",
            )
    cancelled = await scheduler.cancel_run(run_id)
    return {"status": "cancelled" if cancelled else "not_running", "run_id": run_id}


@router.get("/v1/tasks/stats")
async def get_stats(user: UserContext | None = Depends(get_current_user)):
    require_auth(user)
    stats = await run_db(task_store.get_stats)
    stats["scheduled_tasks"] = len(await run_db(scheduler.next_run_times))
    stats["running_tasks"] = len(scheduler.get_running_tasks())
    return stats


@router.get("/v1/tasks/{task_id}")
async def get_task(
    task_id: str,
    audit: bool = Query(False),
    user: UserContext | None = Depends(get_current_user),
):
    """One task's full definition, prompt included, what it runs on and the
    triggers pointing at it. Registered AFTER the static /v1/tasks/runs and
    /v1/tasks/stats routes (Starlette matches in registration order). The
    read gate is the listing's, applied to one row: a row outside the
    caller's reach, another user's user-scope row and a delegate row all
    answer 404, as the listing would simply not show them."""
    u = require_auth(user)
    dyn = await asyncio.to_thread(task_store.get_dynamic_task, task_id)
    task_def = scheduler._row_to_task(dyn) if dyn else None
    if task_def is None or task_def.use_persistent:
        raise HTTPException(404, "Task not found")
    if not u.is_service:
        edge_reach = await asyncio.to_thread(nouser_read_targets, u)
        if not (u.can_access_agent(task_def.agent)
                or (task_def.scope == _vis.SCOPE_AGENT and task_def.agent in edge_reach)):
            raise HTTPException(404, "Task not found")
        if (task_def.scope == _vis.SCOPE_USER and not (audit and u.is_admin)
                and task_def.created_by != u.acting_sub):
            raise HTTPException(404, "Task not found")

    def _build() -> dict:
        from services.agents import offboarding_transfer
        next_fire = scheduler.next_run_time(task_id)
        d = _task_view(task_def, u, _make_model_resolver(),
                       {"next_run_time": next_fire} if next_fire else None,
                       moved_from=offboarding_transfer.transferred_from_names(
                           [dyn.get("transferred_from") or ""]))
        d["transferred_from"] = dyn.get("transferred_from") or ""
        d["transferred_at"] = dyn.get("transferred_at") or ""
        history = task_store.count_runs_by_task([task_id]).get(task_id, 0)
        if getattr(task_def, "task_type", "") == task_kinds.CONTINUATION:
            d["run_count"] = max(int(dyn.get("run_count", 0) or 0), history)
        else:
            d["run_count"] = history
        d["fired"] = bool(dyn.get("fired", False))
        d["on_complete_chat_id"] = dyn.get("on_complete_chat_id")
        triggers: list[dict] = []
        if task_def.task_type == task_kinds.TRIGGER:
            from api.events.triggers import trigger_webhook_path
            from storage.automation import trigger_store
            # The linkage invariant (a trigger shares its task's scope,
            # agent and creator) makes the task gate above cover its
            # triggers.
            for row in trigger_store.list_triggers(task_id=task_id):
                triggers.append({
                    "id": row["id"],
                    "name": row.get("name") or row.get("slug"),
                    "slug": row.get("slug"),
                    "scope": row.get("scope"),
                    "enabled": bool(row.get("enabled", True)),
                    "subscription_id": row.get("subscription_id"),
                    "webhook_path": trigger_webhook_path(row),
                    "fired_count": row.get("fired_count", 0),
                    "last_fired_at": row.get("last_fired_at"),
                })
        d["triggers"] = triggers
        return d

    return await run_db(_build)


@router.get("/v1/tasks/{task_id}/session")
async def get_task_session(task_id: str, user: UserContext | None = Depends(get_current_user)):
    """Return the session_id from the most recent completed run of a task."""
    u = require_auth(user)
    dyn = await asyncio.to_thread(task_store.get_dynamic_task, task_id)
    task_def = scheduler._row_to_task(dyn) if dyn else None
    if task_def is None and not (u.is_service or u.is_admin):
        # A one-time or delegate row retires with its run: with no row left
        # to check the caller against, the session is not handed out.
        raise HTTPException(404, "Task not found")
    if task_def and not u.is_service:
        require_agent_access(u, task_def.agent)
        if task_def.scope == _vis.SCOPE_USER and task_def.created_by != u.sub:
            raise HTTPException(403, "Not authorized to access this task")
    session_id = await asyncio.to_thread(task_store.get_task_session, task_id)
    return {"task_id": task_id, "session_id": session_id}


@router.get("/v1/schedules")
async def list_schedules(user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    jobs = await run_db(scheduler.get_scheduled_jobs)
    if u.is_service:
        return {"schedules": jobs}
    # Filter to schedules the caller may actually see: the job's task must be on
    # an accessible agent, and a user-scoped task is private to its creator.
    # Jobs with no matching task definition (system/maintenance jobs) are hidden
    # from non-service callers — they previously leaked every user's schedule.
    defs_by_id = {t.id: t for t in await run_db(scheduler.get_all_task_definitions)}
    visible = []
    for j in jobs:
        t = defs_by_id.get(j.get("task_id"))
        if t is None or not u.can_access_agent(t.agent):
            continue
        if t.scope == _vis.SCOPE_USER and t.created_by != u.sub:
            continue
        visible.append(j)
    return {"schedules": visible}


@router.get("/v1/tasks/runs/{run_id}/stream")
async def stream_run_output(
    run_id: str,
    request: Request,
):
    """SSE endpoint for live run output. Used by the schedules-mcp ``run_task(wait=true)``
    tool so spawned tasks can stream output back to the calling agent.

    Auth: the session token in the Authorization header, or the dashboard
    session cookie. A key in the query string is not read (it would land
    in access logs)."""
    user = require_auth(await get_current_user(request))

    # Permission check before starting stream
    pre_run = await asyncio.to_thread(task_store.get_run, run_id)
    if pre_run:
        _check_run_access(pre_run, user)

    async def _event_gen():
        # Check if already completed — send full output and done event
        run = await asyncio.to_thread(task_store.get_run, run_id)
        if not run:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Run not found'})}\n\n"
            return

        if run_status.is_terminal(run["status"]):
            if run.get("output_text"):
                yield run_stream.sse(run_stream.text_frame(run["output_text"]))
            yield run_stream.sse(run_stream.done_frame(run["status"]))
            return

        # Tell the subscriber what state it is joining — a parked run
        # ("pending", waiting on the memory-admission slot) otherwise streams
        # nothing but keep-alives for minutes with no explanation.
        yield f"data: {json.dumps({'type': 'status', 'status': run['status']})}\n\n"

        # Subscribe to live updates from scheduler. The runner's ``done``
        # (sent after the row is stamped) is the normal end; the row is the
        # backstop — read once after the subscribe (the run may have ended in
        # between, its frame already popped with its buffer) and on every
        # keep-alive tick (a writer with no scheduler task behind it: a
        # dashboard-resumed turn's flip, a recovery after a restart).
        q = await scheduler.subscribe_run(run_id)
        saw_text = False
        try:
            ended = await run_stream.row_end(run_id)
            while True:
                if ended is None:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=run_stream.KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        # Keep-alive comment to prevent proxy timeout
                        yield ": keep-alive\n\n"
                        ended = await run_stream.row_end(run_id)
                        continue
                else:
                    # The row has ended: hand over what the queue still holds
                    # (the subscribe's replay), then the row's own verdict —
                    # unless a ``done`` was queued, which is the same verdict.
                    try:
                        event = q.get_nowait()
                    except asyncio.QueueEmpty:
                        if not saw_text and ended.get("output_text"):
                            yield run_stream.sse(run_stream.text_frame(ended["output_text"]))
                        yield run_stream.sse(run_stream.done_frame(ended["status"]))
                        break
                yield run_stream.sse(event)
                saw_text = saw_text or run_stream.is_text(event)
                if run_stream.is_done(event):
                    break
        finally:
            scheduler.unsubscribe_run(run_id, q)

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
