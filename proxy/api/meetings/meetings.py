"""Meeting rooms REST API endpoints.

Who may read a meeting: the master key, a platform admin, a user-scope
meeting's creator, and, for an agent-scope meeting, a caller who can read
EVERY participant agent (a person's agents; a no-user session's own agent
and its delegation targets). Access to one participant never opens a
meeting with others: the record names the host chat and the transcript
carries every agent's turns, thinking and tool inputs. Who may act on one:
its creator, an admin, the master key, or a session acting for one of its
agents (the session token's own agent, never a header a cookie caller
sends). A caller who may neither read nor act gets 404, so ids never leak.
"""

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from auth.providers import UserContext, get_current_user, require_auth
from core.session.visibility import SCOPE_USER, nouser_read_targets
from storage import database as task_store
from storage.agents import agent_store
from storage.chat import meeting_status
from storage.pg import run_db

router = APIRouter()

# The routes' own answer words beside the meeting statuses they echo:
# ``starting`` (the orchestrator was kicked) and ``left`` (a participant
# left) are results, never a row's status.
ANSWER_STARTING = "starting"


def _participants(meeting: dict) -> list[str]:
    try:
        return list(json.loads(meeting.get("participants") or "[]"))
    except (json.JSONDecodeError, TypeError):
        return []


def _readable_agents(u: UserContext) -> set[str] | None:
    """The agents whose meetings the caller may read (None: unfiltered).
    Synchronous: the no-user branch reads the delegation roster."""
    if u.is_service or u.is_admin:
        return None
    if u.is_no_user_session:
        return ({u.agent} if u.agent else set()) | nouser_read_targets(u)
    reach = set(u.agents)
    if u.agent:
        reach.add(u.agent)
    return reach


def _can_read(meeting: dict, u: UserContext, readable: set[str] | None) -> bool:
    if readable is None:
        return True
    if (meeting.get("scope") or SCOPE_USER) == SCOPE_USER:
        return u.acting_sub is not None and meeting.get("created_by") == u.acting_sub
    participants = _participants(meeting)
    return bool(participants) and set(participants) <= readable


def _is_creator(meeting: dict, u: UserContext) -> bool:
    return u.acting_sub is not None and meeting.get("created_by") == u.acting_sub


def _acting_agent(u: UserContext, x_agent_name: str | None) -> str:
    """The agent a caller acts for: a session token's own agent (the
    header is not read: a session names another agent no more than it
    names another person), the header's for the master key, none for a
    cookie."""
    if u.is_service:
        return x_agent_name or ""
    if u.is_session:
        return u.agent or ""
    return ""


async def _meeting_for(meeting_id: str, u: UserContext,
                       x_agent_name: str | None = None) -> tuple[dict, str, bool]:
    """The row, the agent the caller acts for, and whether the caller may
    read it. 404 when the caller may neither read it nor act in it."""
    meeting = await run_db(task_store.get_meeting, meeting_id)
    if not meeting:
        raise HTTPException(404, "Meeting not found")
    readable = await run_db(_readable_agents, u)
    can_read = _can_read(meeting, u, readable)
    acting = _acting_agent(u, x_agent_name)
    can_act = (u.is_service or u.is_admin or _is_creator(meeting, u)
               or (bool(acting) and acting in _participants(meeting)))
    if not (can_read or can_act):
        raise HTTPException(404, "Meeting not found")
    return meeting, acting, can_read


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
    """List the meetings the caller may read (the module docstring); the
    page and its total come from one predicate."""
    u = require_auth(user)

    # Only admin / master key can use the created_by filter
    if created_by and not u.is_service and not u.is_admin:
        created_by = None

    # Scope filtering (same logic as _scope_filter_sub in tasks.py). A
    # no-user session has no creator identity: "" matches no user-scope row.
    scope_sub: str | None = None
    if not u.is_service and not (u.is_admin and not agent):
        scope_sub = u.acting_sub if u.acting_sub is not None else ""
    readable = await run_db(_readable_agents, u)
    readable_list = sorted(readable) if readable is not None else None

    meetings = await run_db(
        task_store.list_meetings, limit, offset, agent, status,
        scope_user_sub=scope_sub, created_by=created_by, readable_agents=readable_list,
    )
    total = await run_db(
        task_store.get_meeting_count, agent, status,
        scope_user_sub=scope_sub, created_by=created_by, readable_agents=readable_list,
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
    parent_run_id: str | None = None
    scope: str = "user"


def _check_participants(u: UserContext, req: CreateMeetingRequest) -> None:
    """Validate that every participant exists and the caller may convene
    it. Synchronous: call it on the DB executor (the agent rows, the
    Shared-only reads and a no-user session's delegation roster).

    A participant runs AGENT scope (writing the shared ``/workspace/`` with
    manager capability) when the meeting itself is agent-scoped OR the agent
    is Shared-only (which is always agent-scope). Agent-scope participation
    requires editor+ on that agent, mirroring ``_enforce_task_scope`` in
    api/tasks/tasks.py: a real-human viewer must NOT be able to convene a
    meeting that performs agent-scope writes they couldn't do via a task.
    User-scope participants run as the caller's OWN per-agent role
    (self-limiting), so read access suffices. A no-user session (agent-scope
    task/trigger, phone) has no roles to derive access from: its participant
    reach is its agent's DELEGATION ROSTER and itself (autonomy needs
    explicit wiring; the roster is that wiring), the source agent read from
    the session JWT, never the X-Agent-Name header."""
    from core.session.visibility import is_shared_only
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
                    f"own agent's delegation targets: '{slug}' is not wired.",
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


def _moderator_scopes(moderator: str) -> list[str]:
    """The meeting scopes the moderator agent's visibility mode offers.
    Synchronous: call it on the DB executor."""
    from core.session.visibility import available_scopes_for
    row = agent_store.get_agent(moderator) or {}
    return available_scopes_for(
        bool(row.get("collaborative", True)), row.get("default_scope") or "user",
    )


def _host(u: UserContext, chat_id: str) -> str | None:
    """The session a meeting hosted in ``chat_id`` inherits its permission
    mode from, read off the chat's row once the caller may host there: the
    meeting's pump takes over the chat, its turns are the chat's rows, and
    that session's viewers answer its prompts. A session hosts only in its
    own chat; a person needs to open and drive the chat (the drive gate of
    ``duplex_service.chat_access_denied_reason``); the master key names
    any chat. With no host chat, a session keeps its own session and
    anyone else none. Synchronous: call it on the DB executor."""
    if not chat_id:
        return u.session_id or None
    chat = task_store.get_chat(chat_id)
    if not chat:
        raise HTTPException(404, "Chat not found")
    if u.is_session:
        if not u.session_id or (chat.get("session_id") or "") != u.session_id:
            raise HTTPException(403, "A session can host a meeting only in its own chat")
    elif not u.is_service:
        from services.media.duplex_service import chat_access_denied_reason
        refusal = chat_access_denied_reason(chat_id, u)
        if refusal is not None:
            raise HTTPException(403, refusal)
    return chat.get("session_id") or None


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

    await run_db(_check_participants, u, req)

    parent_chat_id = req.parent_chat_id or ""
    parent_session_id = await run_db(_host, u, parent_chat_id)

    # Check no active meeting on parent chat
    if parent_chat_id:
        existing = await run_db(
            task_store.get_active_meeting_for_chat, parent_chat_id,
        )
        if existing:
            raise HTTPException(409, "An active meeting already exists on this chat")

    # The moderator: a session's own agent (its header is not read), the
    # master key's header, a person's pick; the first participant otherwise.
    # Always a participant: the round loop queues the moderator first.
    if u.is_session or u.is_service:
        moderator = _acting_agent(u, x_agent_name) or req.agents[0]
    else:
        moderator = x_agent_name or req.agents[0]
    if moderator not in req.agents:
        raise HTTPException(400, f"The moderator '{moderator}' must be one of the participants")

    # Visibility-modes: reject a scope the moderator agent's mode doesn't offer
    # (Personal-only → no "agent"; Shared-only → no "user"). Defense-in-depth —
    # the meetings-mcp already resolves scope from the (clamped) default.
    if req.scope in ("user", "agent"):
        _avail = await run_db(_moderator_scopes, moderator)
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
        created_by = acting if acting is not None else (_acting_agent(u, x_agent_name) or "api")

    # Platform kill-switch + per-creator participant cap. The CREATE endpoint
    # is where these actually bite: meetings-mcp reaches sessions via the
    # extra_mcps force-inject, which bypasses mcp_state at config build. A
    # MISSING state row means enabled (unscanned fresh install) — only an
    # explicit admin disable blocks.
    from storage.mcp import mcp_store
    state = await run_db(mcp_store.get_mcp_state, "meetings-mcp")
    if state is not None and not state.get("enabled"):
        raise HTTPException(
            403, "Meetings are disabled on this platform (meetings-mcp is turned off).")
    cap_raw = (await run_db(
        mcp_store.get_mcp_config_values, "meetings-mcp")).get("MAX_PARALLEL_SPAWNS")
    try:
        cap = int(cap_raw)
    except (TypeError, ValueError):
        cap = 4
    if cap <= 0:
        cap = 4
    active = await run_db(
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

    meeting = await run_db(
        task_store.create_meeting,
        meeting_id, req.topic, participants_json, moderator,
        req.strategy, req.max_turns,
        parent_chat_id, parent_session_id,
        req.parent_run_id, req.scope, created_by,
    )
    return {"meeting_id": meeting_id, "status": meeting_status.PENDING, "meeting": meeting}


@router.post("/v1/meetings/{meeting_id}/start")
async def start_meeting_endpoint(
    meeting_id: str,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Trigger the meeting orchestrator: the creator, an admin, the master
    key, or the moderator agent's own session."""
    u = require_auth(user)
    meeting, acting, _ = await _meeting_for(meeting_id, u, x_agent_name)
    if not (u.is_service or u.is_admin or _is_creator(meeting, u)
            or (acting and acting == meeting["moderator"])):
        raise HTTPException(403, "Only the creator, an admin or the moderator can start the meeting")
    if meeting["status"] != meeting_status.PENDING:
        raise HTTPException(400, f"Meeting status is '{meeting['status']}', expected 'pending'")

    from services.meetings import meeting_orchestrator
    asyncio.create_task(meeting_orchestrator.start_meeting(meeting_id))
    return {"status": ANSWER_STARTING, "meeting_id": meeting_id}


@router.get("/v1/meetings/{meeting_id}")
async def get_meeting_endpoint(
    meeting_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Get meeting status and details, for a reader of every participant."""
    u = require_auth(user)
    meeting, _, can_read = await _meeting_for(meeting_id, u)
    if not can_read:
        raise HTTPException(404, "Meeting not found")
    return meeting


@router.post("/v1/meetings/{meeting_id}/end")
async def end_meeting_endpoint(
    meeting_id: str,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """End a meeting: the moderator agent's own session, the creator, an
    admin or the master key. A header alone grants nothing to a cookie."""
    u = require_auth(user)
    meeting, acting, _ = await _meeting_for(meeting_id, u, x_agent_name)
    if meeting["status"] not in meeting_status.REQUEST_ENDABLE:
        raise HTTPException(400, "Meeting not active")
    if acting and acting != meeting["moderator"] and not (
            u.is_service or u.is_admin or _is_creator(meeting, u)):
        raise HTTPException(403, "Only the moderator can end the meeting")
    if not acting and not (u.is_service or u.is_admin or _is_creator(meeting, u)):
        raise HTTPException(
            403, "Only the moderator, the creator, or an admin can end the meeting",
        )

    from services.meetings import meeting_orchestrator
    result = await meeting_orchestrator.end_meeting(
        meeting_id, acting if acting == meeting["moderator"] else None)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


class LeaveMeetingRequest(BaseModel):
    reason: str = ""


def _participant_named(meeting: dict, u: UserContext, acting: str,
                       x_agent_name: str | None) -> str:
    """The participant a leave or a pause request is about: a session's own
    agent; for the creator, an admin or the master key, the header's agent.
    403 for anyone else, 400 without a name or for a stranger to the meeting."""
    if acting:
        agent = acting
    elif u.is_service or u.is_admin or _is_creator(meeting, u):
        agent = x_agent_name or ""
    else:
        raise HTTPException(403, "Only a participant's own session, the creator or an admin may")
    if not agent:
        raise HTTPException(400, "X-Agent-Name header required")
    active = json.loads(meeting["active_participants"])
    if agent not in active:
        raise HTTPException(400, f"Agent '{agent}' not in meeting")
    return agent


@router.post("/v1/meetings/{meeting_id}/leave")
async def leave_meeting_endpoint(
    meeting_id: str,
    req: LeaveMeetingRequest = LeaveMeetingRequest(),
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Leave a meeting: a participant's own session pulls its agent out."""
    u = require_auth(user)
    meeting, acting, _ = await _meeting_for(meeting_id, u, x_agent_name)
    if meeting["status"] not in meeting_status.REQUEST_LEAVABLE:
        raise HTTPException(400, "Meeting not active")
    agent = _participant_named(meeting, u, acting, x_agent_name)

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
    """Propose concluding the meeting (a participant's own session): pauses
    and lets the moderator decide."""
    u = require_auth(user)
    meeting, acting, _ = await _meeting_for(meeting_id, u, x_agent_name)
    if meeting["status"] not in meeting_status.PROPOSABLE:
        raise HTTPException(400, "Meeting not active")
    agent = _participant_named(meeting, u, acting, x_agent_name)
    if not await run_db(task_store.update_meeting_if, meeting_id, meeting_status.PROPOSABLE,
                        status=meeting_status.PAUSED):
        raise HTTPException(400, "Meeting not active")
    return {"status": meeting_status.PAUSED, "meeting_id": meeting_id, "proposed_by": agent}


@router.get("/v1/meetings/{meeting_id}/transcript")
async def get_transcript_endpoint(
    meeting_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Get meeting transcript, for a reader of every participant."""
    u = require_auth(user)
    meeting, _, can_read = await _meeting_for(meeting_id, u)
    if not can_read:
        raise HTTPException(404, "Meeting not found")
    turns = await run_db(task_store.get_meeting_turns, meeting_id)
    return {"meeting_id": meeting_id, "turns": turns}
