"""Meeting rooms REST API endpoints."""

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from auth.providers import UserContext, get_current_user, require_auth
from storage import database as task_store
from storage import agent_store

router = APIRouter()


def _require_meeting_access(meeting: dict, u: UserContext) -> None:
    """Authorize per-meeting access for object-level handlers.

    A caller may touch a meeting if they are the master key, a platform admin,
    the real-user creator, or can access at least one participant agent. Without
    this, any authenticated user could read/start/end/leave an arbitrary meeting
    by guessing its id (object-level IDOR).
    """
    if u.is_service or u.is_admin:
        return
    if u.acting_sub is not None and meeting.get("created_by") == u.acting_sub:
        return
    try:
        participants = json.loads(meeting.get("participants", "[]"))
    except (json.JSONDecodeError, TypeError):
        participants = []
    if any(u.can_access_agent(a) for a in participants):
        return
    raise HTTPException(403, "Not authorized to access this meeting")


# ---------------------------------------------------------------------------
# List meetings
# ---------------------------------------------------------------------------

@router.get("/v1/meetings")
async def list_meetings_endpoint(
    agent: str | None = Query(None),
    status: str | None = Query(None),
    created_by: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: UserContext | None = Depends(get_current_user),
):
    """List meetings with scope-based filtering."""
    u = require_auth(user)

    # Only admin / master key can use the created_by filter
    if created_by and not u.is_service and not u.is_admin:
        created_by = None

    # Scope filtering (same logic as _scope_filter_sub in tasks.py)
    scope_sub: str | None = None
    if not u.is_service:
        if not (u.is_admin and not agent):
            scope_sub = u.acting_sub

    meetings = await asyncio.to_thread(
        task_store.list_meetings, limit, offset, agent, status,
        scope_user_sub=scope_sub, created_by=created_by,
    )

    # Post-filter: user must be able to access at least one participant agent
    if not u.is_service:
        filtered = []
        for m in meetings:
            try:
                participants = json.loads(m.get("participants", "[]"))
            except (json.JSONDecodeError, TypeError):
                participants = []
            if u.is_admin or any(u.can_access_agent(a) for a in participants):
                filtered.append(m)
        meetings = filtered

    total = await asyncio.to_thread(
        task_store.get_meeting_count, agent, status,
        scope_user_sub=scope_sub, created_by=created_by,
    )

    return {"meetings": meetings, "total": total, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# Create meeting
# ---------------------------------------------------------------------------

class CreateMeetingRequest(BaseModel):
    topic: str
    agents: list[str]
    max_turns: int = 30
    strategy: str = "round_robin"
    parent_chat_id: str | None = None
    parent_session_id: str | None = None
    parent_run_id: str | None = None
    scope: str = "user"


@router.post("/v1/meetings")
async def create_meeting(
    req: CreateMeetingRequest,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Create a meeting record."""
    u = require_auth(user)

    # Validate at least 2 agents
    if len(req.agents) < 2:
        raise HTTPException(400, "Meetings require at least 2 agents")

    # Validate all agents exist and the caller is authorized. A participant runs
    # AGENT scope — writing the shared ``/workspace/`` with manager capability —
    # when the meeting itself is agent-scoped OR the agent is Shared-only (which
    # is always agent-scope). Agent-scope participation requires editor+ on that
    # agent, mirroring ``_enforce_task_scope`` in api/tasks/tasks.py: a real-human viewer
    # must NOT be able to convene a meeting that performs agent-scope writes they
    # couldn't do via a task. User-scope participants run as the caller's OWN
    # per-agent role (self-limiting), so read access suffices. Service / api-key /
    # no-user sessions keep their existing access-only check.
    from core.session.visibility import is_shared_only
    # A no-user session (agent-scope task/trigger, phone) has no roles to
    # derive access from — its participant reach is its agent's DELEGATION
    # ROSTER ∪ itself (rule 3: autonomy needs explicit wiring; the roster is
    # that wiring). Token-authoritative: the source agent comes from the
    # session JWT, never the X-Agent-Name header.
    nouser_allowed: set[str] | None = None
    if u.is_no_user_session:
        source = u.agent or ""
        nouser_allowed = {source} | set(
            agent_store.get_delegation_targets(source) if source else [])
    for slug in req.agents:
        agent = agent_store.get_agent(slug)
        if not agent:
            raise HTTPException(400, f"Agent '{slug}' not found")
        if nouser_allowed is not None:
            if slug not in nouser_allowed:
                raise HTTPException(
                    403,
                    f"This session has no user identity; it can only meet its "
                    f"own agent's delegation targets — '{slug}' is not wired.",
                )
            continue
        if u.is_service:
            continue
        runs_agent_scope = req.scope == "agent" or is_shared_only(slug)
        if runs_agent_scope and u.acting_sub is not None:
            if not u.can_edit_agent(slug):
                raise HTTPException(
                    403,
                    f"Agent-scoped participation in '{slug}' requires editor, "
                    f"manager, or admin role for this agent",
                )
        elif not u.can_access_agent(slug):
            raise HTTPException(403, f"No access to agent '{slug}'")

    # Check no active meeting on parent chat
    if req.parent_chat_id:
        existing = await asyncio.to_thread(
            task_store.get_active_meeting_for_chat, req.parent_chat_id,
        )
        if existing:
            raise HTTPException(409, "An active meeting already exists on this chat")

    # Resolve moderator (the calling agent)
    moderator = x_agent_name or req.agents[0]

    # Visibility-modes: reject a scope the moderator agent's mode doesn't offer
    # (Personal-only → no "agent"; Shared-only → no "user"). Defense-in-depth —
    # the meetings-mcp already resolves scope from the (clamped) default.
    if req.scope in ("user", "agent"):
        from core.session.visibility import available_scopes_for
        from storage import agent_store as _as
        _row = _as.get_agent(moderator) or {}
        _avail = available_scopes_for(
            bool(_row.get("collaborative", True)), _row.get("default_scope") or "user",
        )
        if req.scope not in _avail:
            raise HTTPException(
                400,
                f"This agent does not support {req.scope!r}-scoped meetings "
                f"(mode offers: {', '.join(_avail)})",
            )

    # Resolve created_by token-authoritatively (mirrors the task-create path).
    # Identity comes from the session token, NEVER a client-supplied created_by:
    # a no-user (phone/agent) session has no identity and cannot create
    # user-scoped meetings; the master key can't either; a real user is always
    # attributed to self; an agent-scope meeting from a service session is
    # attributed to the agent.
    acting = u.acting_sub
    if req.scope == "user":
        if acting is None:
            if u.is_no_user_session:
                raise HTTPException(
                    403,
                    "This session has no user identity and cannot create "
                    "user-scoped meetings.",
                )
            raise HTTPException(
                400,
                "User-scoped meetings cannot be created with the master API key; "
                "they must be created from a user session.",
            )
        created_by = acting
    else:
        created_by = acting if acting is not None else (x_agent_name or u.agent or "api")

    # Platform kill-switch + per-creator participant cap. The CREATE endpoint
    # is where these actually bite: meetings-mcp reaches sessions via the
    # extra_mcps force-inject, which bypasses mcp_state at config build. A
    # MISSING state row means enabled (unscanned fresh install) — only an
    # explicit admin disable blocks.
    from storage import mcp_store
    state = await asyncio.to_thread(mcp_store.get_mcp_state, "meetings-mcp")
    if state is not None and not state.get("enabled"):
        raise HTTPException(
            403, "Meetings are disabled on this platform (meetings-mcp is turned off).")
    cap_raw = (await asyncio.to_thread(
        mcp_store.get_mcp_config_values, "meetings-mcp")).get("MAX_PARALLEL_SPAWNS")
    try:
        cap = int(cap_raw)
    except (TypeError, ValueError):
        cap = 4
    if cap <= 0:
        cap = 4
    active = await asyncio.to_thread(
        task_store.count_active_meeting_participants, created_by)
    if active + len(req.agents) > cap:
        raise HTTPException(
            403,
            f"Meeting limit reached: {active} participant(s) already active "
            f"in your meetings (max {cap} total). End a meeting first, or ask "
            f"an admin to raise MAX_PARALLEL_SPAWNS for meetings-mcp.",
        )

    meeting_id = f"mtg-{uuid.uuid4().hex[:12]}"
    participants_json = json.dumps(req.agents)

    meeting = await asyncio.to_thread(
        task_store.create_meeting,
        meeting_id, req.topic, participants_json, moderator,
        req.strategy, req.max_turns,
        req.parent_chat_id or "", req.parent_session_id,
        req.parent_run_id, req.scope, created_by,
    )
    return {"meeting_id": meeting_id, "status": "pending", "meeting": meeting}


@router.post("/v1/meetings/{meeting_id}/start")
async def start_meeting_endpoint(
    meeting_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Trigger the meeting orchestrator."""
    u = require_auth(user)

    meeting = await asyncio.to_thread(task_store.get_meeting, meeting_id)
    if not meeting:
        raise HTTPException(404, "Meeting not found")
    _require_meeting_access(meeting, u)
    if meeting["status"] != "pending":
        raise HTTPException(400, f"Meeting status is '{meeting['status']}', expected 'pending'")

    from services.meetings import meeting_orchestrator
    asyncio.create_task(meeting_orchestrator.start_meeting(meeting_id))
    return {"status": "starting", "meeting_id": meeting_id}


@router.get("/v1/meetings/{meeting_id}")
async def get_meeting_endpoint(
    meeting_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Get meeting status and details."""
    u = require_auth(user)
    meeting = await asyncio.to_thread(task_store.get_meeting, meeting_id)
    if not meeting:
        raise HTTPException(404, "Meeting not found")
    _require_meeting_access(meeting, u)
    return meeting


@router.post("/v1/meetings/{meeting_id}/end")
async def end_meeting_endpoint(
    meeting_id: str,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """End a meeting. Only the moderator (agent), creator, or an admin can end it."""
    u = require_auth(user)
    meeting = await asyncio.to_thread(task_store.get_meeting, meeting_id)
    if not meeting:
        raise HTTPException(404, "Meeting not found")
    _require_meeting_access(meeting, u)
    if meeting["status"] not in ("active", "concluding"):
        raise HTTPException(400, "Meeting not active")
    # A meeting concludes ONLY at the moderator agent's request, or when a
    # human creator / admin force-ends it. A missing X-Agent-Name is never
    # treated as moderator authority (it would let any participant-accessor end
    # the meeting); the human path must clear the creator/admin bar explicitly.
    if x_agent_name:
        if x_agent_name != meeting["moderator"]:
            raise HTTPException(403, "Only the moderator can end the meeting")
    elif not (
        u.is_service or u.is_admin
        or (u.acting_sub is not None and meeting.get("created_by") == u.acting_sub)
    ):
        raise HTTPException(
            403, "Only the moderator, the creator, or an admin can end the meeting",
        )

    from services.meetings import meeting_orchestrator
    result = await meeting_orchestrator.end_meeting(meeting_id, x_agent_name)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


class LeaveMeetingRequest(BaseModel):
    reason: str = ""


@router.post("/v1/meetings/{meeting_id}/leave")
async def leave_meeting_endpoint(
    meeting_id: str,
    req: LeaveMeetingRequest = LeaveMeetingRequest(),
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Leave a meeting. Any participant can leave (on behalf of its own agent)."""
    u = require_auth(user)
    meeting = await asyncio.to_thread(task_store.get_meeting, meeting_id)
    if not meeting:
        raise HTTPException(404, "Meeting not found")
    _require_meeting_access(meeting, u)
    if meeting["status"] != "active":
        raise HTTPException(400, "Meeting not active")

    agent = x_agent_name
    if not agent:
        raise HTTPException(400, "X-Agent-Name header required")

    # A caller may only pull out the agent it can actually act for — otherwise
    # any meeting-accessor could eject an arbitrary participant by name.
    if not (u.is_service or u.can_access_agent(agent)):
        raise HTTPException(403, f"No access to agent '{agent}'")

    active = json.loads(meeting["active_participants"])
    if agent not in active:
        raise HTTPException(400, f"Agent '{agent}' not in meeting")

    from services.meetings import meeting_orchestrator
    result = await meeting_orchestrator.leave_meeting(meeting_id, agent, req.reason)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/v1/meetings/{meeting_id}/propose-conclude")
async def propose_conclude_endpoint(
    meeting_id: str,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Propose concluding the meeting. Pauses and lets moderator decide."""
    u = require_auth(user)
    meeting = await asyncio.to_thread(task_store.get_meeting, meeting_id)
    if not meeting:
        raise HTTPException(404, "Meeting not found")
    _require_meeting_access(meeting, u)
    if meeting["status"] != "active":
        raise HTTPException(400, "Meeting not active")
    # Mark as paused so orchestrator knows
    await asyncio.to_thread(task_store.update_meeting, meeting_id, status="paused")
    return {"status": "paused", "meeting_id": meeting_id, "proposed_by": x_agent_name}


@router.get("/v1/meetings/{meeting_id}/transcript")
async def get_transcript_endpoint(
    meeting_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Get meeting transcript."""
    u = require_auth(user)
    meeting = await asyncio.to_thread(task_store.get_meeting, meeting_id)
    if not meeting:
        raise HTTPException(404, "Meeting not found")
    _require_meeting_access(meeting, u)
    turns = await asyncio.to_thread(task_store.get_meeting_turns, meeting_id)
    return {"meeting_id": meeting_id, "turns": turns}
