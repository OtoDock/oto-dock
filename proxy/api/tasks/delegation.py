"""Delegation (Projects) API — worker spawn.

``POST /v1/delegation/spawn`` replaces the old MCP-side create→patch→run
dance with ONE atomic call: authorization (``services/delegation/spawn_authz``
— kill-switch, roster, scope clamp, identity, per-creator cap), worker-chat
creation for ``surface="chat"``, the dynamic-task row with its on-complete
callback registered BEFORE the fire, and the ``delegate_spawn`` badge event
on the delegating chat.
"""

import asyncio
import json
import logging
import re
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

import config
from auth.providers import UserContext, get_current_user, require_auth
from core.session.visibility import chat_history_owner, nouser_read_targets
from services.delegation.spawn_authz import (
    authorize_spawn, check_delegation_chain, validate_spawn_overrides,
)
from services.delegation import file_transfer, lane_status
from services.scheduler import scheduler
from storage import database as task_store
from storage import mcp_store

logger = logging.getLogger("claude-proxy.delegation-api")
router = APIRouter()

_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Server-owned callback template — the scheduler's delivery path substitutes
# the {{...}} tokens at terminal time (see _deliver_task_result).
_ON_COMPLETE_PROMPT = (
    "[BACKGROUND_TASK_RESULT]\n"
    "Task '{{task_name}}' (id: {{task_id}}, worker chat: {{chat_id}}) "
    "finished with status={{status}}.\n\n"
    "Output:\n{{output}}\n\n"
    "Review the result and respond to the user appropriately. If status is "
    "user_interrupted, the user stopped or redirected the worker and the "
    "worker has ALREADY served them directly in its own chat — this callback "
    "was deferred until that settled. The [User interjected] lines above are "
    "their instructions and the turns after them are the worker's replies; "
    "fold that into your plan instead of re-delegating or assuming the lane "
    "died.\n\n"
    "This result is the worker's report and is final for its run. Do not "
    "answer it with another delegation just to acknowledge, confirm or thank "
    "— delegate again (continue_id) only when the user asked for more work "
    "or the worker is blocked on something only you can supply. Otherwise "
    "report to the user and let them decide what happens next."
)


class SpawnDelegateRequest(BaseModel):
    name: str
    prompt: str
    surface: Literal["chat", "task"]
    # Required for a fresh delegation; optional on continue_id (the continued
    # worker's own agent is authoritative — a mismatching value is rejected).
    agent: str = ""
    source_agent: str | None = None
    # Multi-turn: a prior worker chat id, a task-run chat id, or a prior
    # delegate task id. The worker retains its chat AND its context.
    continue_id: str | None = None
    project_id: str | None = None
    scope: str = "user"
    timeout_seconds: int = 1800
    # Optional per-lane execution overrides — for task-specific needs only;
    # omitted = the worker inherits the agent's configured defaults exactly
    # as before. Validated against the agent's envelope (enabled layers,
    # layer's model registry); IGNORED on continue_id — the existing worker
    # keeps its configuration.
    model: str | None = None
    layer: str | None = None
    mode: Literal["interactive", "non-interactive"] | None = None


def _resolve_continued_chat(continue_id: str) -> dict | None:
    """The chat row a continue_id points at.

    Accepts a worker chat id (surface="chat"), a task-run chat id
    (``task-run-…``), or a delegate task id (``dyn-…`` — resolved through its
    last completed run's session, since delegate task rows auto-clean after
    success). Every continuation flows through the chat row so the new run
    appends to the SAME chat and runs on the SAME agent.
    """
    row = task_store.get_chat(continue_id)
    if row:
        return row
    session_id = task_store.get_task_session(continue_id)
    if session_id:
        return task_store.get_chat_by_session(session_id)
    return None


def _model_served_by_layer(model: str, layer: str) -> bool:
    """True iff the execution layer serves this model. Fail-OPEN on lookup
    errors — this guards cross-layer poison, it is not the registry's
    gatekeeper (twin of ws/dashboard._model_allowed_for_path)."""
    try:
        from storage import subscription_store
        return any(
            (m.get("model_id") or "") == model
            for m in subscription_store.list_models(layer)
        )
    except Exception:
        return True


def _model_is_cross_layer_poison(model: str, layer: str) -> bool:
    """POSITIVE cross-layer evidence only: the model is NOT served by
    ``layer`` but IS served by some other layer (the 2026-08 stamp bug shape
    — a Claude model on a codex lane). A model unknown to the registry
    everywhere is NOT poison (custom/retired ids keep their pin; spawn
    validation owns that case). Fail-open (False) on lookup errors."""
    try:
        if _model_served_by_layer(model, layer):
            return False
        from storage import subscription_store
        return any(
            (m.get("model_id") or "") == model
            and (m.get("layer") or "") != layer
            for m in subscription_store.list_models()
        )
    except Exception:
        return False


@router.post("/v1/delegation/spawn")
async def spawn_delegate(
    req: SpawnDelegateRequest,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    u = require_auth(user)
    # A continuation derives its agent from the continued worker itself — the
    # caller may omit `agent`, and a mismatching value is rejected instead of
    # silently running the continuation on the wrong agent (a --resume under
    # another agent's config dir never finds the conversation: the CLI answers
    # "No conversation found with session ID: …" and that becomes the output).
    continued_chat: dict | None = None
    if req.continue_id:
        continued_chat = await asyncio.to_thread(
            _resolve_continued_chat, req.continue_id,
        )
        if not continued_chat:
            raise HTTPException(
                404,
                f"No worker chat or delegate task '{req.continue_id}' to continue.",
            )
        if req.agent and req.agent != continued_chat.get("agent"):
            raise HTTPException(
                400,
                f"continue_id '{req.continue_id}' belongs to agent "
                f"'{continued_chat.get('agent')}' — omit `agent` on continues "
                "or pass that exact value.",
            )
    effective_agent = (continued_chat or {}).get("agent") or req.agent
    if not effective_agent:
        raise HTTPException(400, "agent is required for a fresh delegation.")

    authz = await asyncio.to_thread(
        lambda: authorize_spawn(
            u,
            target_agent=effective_agent,
            requested_scope=req.scope,
            source_agent=req.source_agent,
            surface=req.surface,
            x_agent_name=x_agent_name,
        )
    )

    project_id = (req.project_id or "").strip()
    if project_id and not _PROJECT_ID_RE.match(project_id):
        raise HTTPException(
            400,
            "project_id must be a short slug: lowercase letters, digits, "
            "'-' or '_' (max 64 chars).",
        )

    # Per-lane execution overrides: continue keeps the existing worker's
    # configuration (fresh spawns validate against the agent's envelope).
    ov_model, ov_layer, ov_mode = (
        (None, None, None) if req.continue_id
        else (req.model, req.layer, req.mode)
    )
    if ov_model or ov_layer:
        await asyncio.to_thread(
            validate_spawn_overrides, effective_agent, ov_layer, ov_model,
        )

    # Callback anchors come from the caller's OWN session token (the
    # delegating session). A cookie / master-key caller has no session —
    # the worker still runs, results live in its chat / run history.
    parent_session_id = u.session_id or ""
    parent_chat = None
    if parent_session_id:
        parent_chat = await asyncio.to_thread(
            task_store.get_chat_by_session, parent_session_id,
        )
    parent_chat_id = parent_chat["id"] if parent_chat else ""

    # Chain guard (fresh spawns only): cycles and over-deep chains 403 with a
    # message telling the agent what to do instead. Continues extend a lane
    # that already passed this at spawn time. Introduced with the symmetric
    # department topology (2026-09-02) — see spawn_authz.check_delegation_chain.
    if continued_chat is None:
        await asyncio.to_thread(
            check_delegation_chain,
            parent_chat_id, authz.source_agent, effective_agent,
        )

    # Resolve the worker chat / continuation session. Continues run inside
    # the continued chat regardless of surface — a continued task-run chat
    # behaves exactly like a worker chat (same chat, same agent, same pins).
    continue_session: str | None = None
    worker_chat_id = ""
    if continued_chat is not None:
        # Continuation authority: a worker of THIS chat, or a chat in the
        # caller's own history pool — never an arbitrary chat id.
        if (continued_chat.get("parent_chat_id") != parent_chat_id
                and continued_chat.get("user_sub") != authz.chat_owner):
            raise HTTPException(
                403,
                "continue_id does not refer to a worker of this chat or "
                "one of your own chats.",
            )
        worker_chat_id = continued_chat["id"]
        # The chat's CURRENT session — re-read here, not a stale anchor.
        continue_session = continued_chat.get("session_id") or None
        # The existing worker keeps its configuration: re-derive the
        # per-lane overrides from the chat row's pins so a continued
        # lane can't flip to agent defaults changed since the spawn.
        # An empty mode pin is indistinguishable from "default" — only
        # an explicit pin re-pins.
        ov_model = continued_chat.get("model") or None
        ov_layer = continued_chat.get("execution_path") or None
        # Lenient re-derivation: a pre-fix lane whose chat row was stamped
        # with a model belonging to a DIFFERENT layer (the 2026-08
        # cross-engine poison) must keep continuing — drop the model pin
        # instead of converting the bad stamp into an explicit override
        # that the create-time validation would reject. Positive evidence
        # only: a registry-unknown model keeps its pin.
        if ov_model and ov_layer and _model_is_cross_layer_poison(ov_model, ov_layer):
            ov_model = None
        ov_mode = (continued_chat.get("execution_mode")
                   if continued_chat.get("execution_mode") in
                   ("interactive", "non-interactive") else None)
    elif req.surface == "chat":
        worker_chat_id = str(uuid.uuid4())
        # Best-effort: the run resolves its real model itself (override
        # included); the chat row's model/layer/mode only seed the
        # dashboard header and the continue re-derivation above.
        # Layer-aware: with a per-lane layer override, the agent-default
        # model may be foreign to that engine.
        try:
            worker_model = ov_model or config.get_cli_model(
                effective_agent, layer=ov_layer)
        except Exception:
            worker_model = ov_model or ""
        await asyncio.to_thread(
            task_store.create_chat,
            worker_chat_id, authz.chat_owner, effective_agent, "auto",
            model=worker_model, origin="delegated",
            execution_path=ov_layer or "", execution_mode=ov_mode or "",
            parent_chat_id=parent_chat_id, project_id=project_id,
            delegate_role="worker", title=req.name,
        )

    # The delegating chat is an orchestrator from its first delegation —
    # project or not (the dock and the sidebar accent key on the role).
    # project_id only rides along when one was passed: an unconditional
    # update would wipe an existing project linkage.
    if parent_chat_id:
        orchestrator_stamp: dict = {"delegate_role": "orchestrator"}
        if project_id:
            orchestrator_stamp["project_id"] = project_id
        await asyncio.to_thread(
            task_store.update_chat, parent_chat_id, **orchestrator_stamp,
        )
        _broadcast_orchestrator_stamp(
            parent_chat_id,
            project_id or (parent_chat or {}).get("project_id") or "",
        )

    # The task row. Delegates always run with notification_mode="none" — the
    # result returns via the on-complete callback; a separate "Task Complete"
    # notification would be redundant noise.
    task_id = f"dyn-{uuid.uuid4().hex[:8]}"
    has_callback = bool(parent_session_id)
    task = scheduler.TaskDefinition(
        id=task_id,
        name=req.name,
        agent=effective_agent,
        prompt=req.prompt,
        timeout_seconds=req.timeout_seconds,
        created_by=authz.created_by,
        scope=authz.scope,
        on_complete_agent=authz.source_agent if has_callback else None,
        on_complete_prompt=_ON_COMPLETE_PROMPT if has_callback else None,
        on_complete_session_id=parent_session_id or None,
        continue_session=continue_session,
        use_persistent=True,
        notification_mode="none",
        # Explicit marker — run classification, the spawn cap, and the
        # runs-listing split all key on it (no use_persistent derivation).
        # Auto-cleanup after the run still applies (keys on schedule/trigger).
        task_type="delegate",
        target_chat_id=worker_chat_id or None,
        parent_chat_id=parent_chat_id or None,
        project_id=project_id or None,
        override_model=ov_model,
        override_execution_path=ov_layer,
        override_execution_mode=ov_mode,
    )
    await scheduler.add_dynamic_task(task)
    # The persistent callback anchor (survives browser close / proxy restart)
    # rides the row, not the TaskDefinition — registered BEFORE the fire.
    if has_callback:
        await asyncio.to_thread(
            task_store.update_dynamic_task_on_complete,
            task_id, authz.source_agent, _ON_COMPLETE_PROMPT,
            parent_session_id, parent_chat_id or None,
        )

    run_id = await scheduler.trigger_task_now(
        task, trigger_type="manual", trigger_source=authz.source_agent,
    )

    # Delegate badge on the delegating chat — emitted by the PROXY once the
    # worker actually exists, keyed by the stable task_id (a rejected
    # delegation never strands a badge). Live pump when streaming (the normal
    # case — the delegating turn is still open), event-row fallback otherwise.
    if parent_chat_id:
        from core.events.common_events import CommonEvent, DELEGATE_SPAWN
        from core.session.session_state import inject_pump_event
        spawn_data = {
            # "type" rides along so the event-row fallback below persists the
            # same shape the pump stores (the dashboard's history reload keys
            # blocks on event_data["type"], not the event_type column).
            "type": "delegate_spawn",
            "task_id": task_id,
            "task_name": req.name,
            "agent": effective_agent,
            "surface": req.surface,
            "chat_id": worker_chat_id or None,
            "prompt_preview": (req.prompt or "")[:100],
            "prompt": req.prompt or "",
        }
        if not inject_pump_event(
            parent_chat_id, CommonEvent(type=DELEGATE_SPAWN, data=spawn_data),
        ):
            await asyncio.to_thread(
                task_store.add_chat_message, parent_chat_id, "event", "",
                event_type="delegate_spawn", event_data=json.dumps(spawn_data),
            )

    logger.info(
        f"Delegated: task={task_id} run={run_id} surface={req.surface} "
        f"agent={effective_agent} scope={authz.scope} by={authz.created_by} "
        f"worker_chat={worker_chat_id or '-'} project={project_id or '-'} "
        f"continued={req.continue_id or '-'}"
    )
    return {
        "task_id": task_id,
        "run_id": run_id,
        "chat_id": worker_chat_id or None,
        "agent": effective_agent,
        "scope": authz.scope,
        "scope_note": authz.scope_note or None,
        "project_id": project_id or None,
    }


def _broadcast_orchestrator_stamp(chat_id: str, project_id: str) -> None:
    """Live ``chat_meta`` frame for an orchestrator stamp — the sidebar accent
    and the Dock gate key on delegate_role/project_id from the chats list,
    which otherwise refreshes only on its 30s poll / turn-end refetch."""
    from core.session.session_state import broadcast_chat_frame
    broadcast_chat_frame(chat_id, {
        "type": "chat_meta",
        "chat_id": chat_id,
        "delegate_role": "orchestrator",
        "project_id": project_id,
    })


class SendFilesRequest(BaseModel):
    target_agent: str
    paths: list[str]
    dest_dir: str = ""
    note: str = ""
    source_agent: str | None = None
    scope: str = "user"


@router.post("/v1/delegation/send_files")
async def send_files(
    req: SendFilesRequest,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Copy workspace files into another agent's ``workspace/inbox/<source>/``.

    The proxy mediates (sandboxes cannot see each other); the roster edge
    is the consent — gates mirror ``spawn_authz`` via
    ``services/delegation/file_transfer``. Passive by design: no turn is
    spawned on the target and there is no active notice — its sessions
    see ``workspace/inbox/<source>/…`` in their workspace listing, and
    context travels IN the files (README pattern, taught by the skill)."""
    u = require_auth(user)
    if not req.paths:
        raise HTTPException(400, "`paths` must name at least one file or directory.")
    authz = await asyncio.to_thread(
        lambda: file_transfer.authorize_send_files(
            u,
            target_agent=req.target_agent,
            requested_scope=req.scope,
            source_agent=req.source_agent,
            x_agent_name=x_agent_name,
        )
    )
    result = await asyncio.to_thread(
        lambda: file_transfer.perform_send_files(
            authz, paths=req.paths, dest_dir=req.dest_dir, note=req.note,
        )
    )
    _schedule_transfer_fanout(
        authz.target_agent, result.landed, origin_user_sub=authz.owner_sub,
    )
    return {
        "transfer_id": result.transfer_id,
        "target_agent": authz.target_agent,
        "files": result.landed,
        "total_bytes": result.total_bytes,
        "scope": authz.dest_scope,
        "scope_note": authz.scope_note or None,
        "skipped": result.skipped,
    }


def _schedule_transfer_fanout(
    target_agent: str, rel_paths: list[str], *, origin_user_sub: str,
) -> None:
    """Background satellite push per landed inbox file — best-effort, never
    blocks the response (uploads precedent: a target agent living on a
    remote machine must see the inbox too). ``include_idle`` so a connected
    satellite holding the agent receives it even with no live session."""
    try:
        from services.remote import workspace_fanout
    except Exception:
        return
    agent_dir = config.get_agent_dir(target_agent)
    for rel in rel_paths:
        if not workspace_fanout.has_fanout_candidates(
            target_agent, rel, include_idle=True,
        ):
            continue

        async def _push(rel: str = rel) -> None:
            try:
                await workspace_fanout.fan_out_write(
                    target_agent, rel, agent_dir / rel,
                    include_idle=True, origin_user_sub=origin_user_sub,
                )
            except Exception:
                logger.exception("send_files fan-out failed: %s", rel)

        asyncio.create_task(_push())


class AdoptProjectRequest(BaseModel):
    project_id: str


@router.post("/v1/delegation/adopt")
async def adopt_project(
    req: AdoptProjectRequest,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Take over an existing delegation project as its orchestrator.

    The caller's own chat (resolved from its session token) is stamped
    ``delegate_role='orchestrator'`` + the project slug, so a session that
    picks a project up from its board/handoff gets the Dock and the sidebar
    accent without having to spawn a lane first. The previous orchestrator
    keeps its historical stamp — the lane graph merges by project_id.
    Idempotent; visibility mirrors the peek rule (own history pool, own
    workers, or admin)."""
    u = require_auth(user)
    await asyncio.to_thread(_require_delegation_enabled)
    project_id = (req.project_id or "").strip()
    if not _PROJECT_ID_RE.match(project_id):
        raise HTTPException(
            400,
            "project_id must be a short slug: lowercase letters, digits, "
            "'-' or '_' (max 64 chars).",
        )
    _, owner, caller_chat_id = await asyncio.to_thread(
        _caller_visibility, u, x_agent_name,
    )
    if not caller_chat_id:
        raise HTTPException(
            400,
            "Adoption needs a session-bound chat — call this from the "
            "session that should take over the project.",
        )
    rows = await asyncio.to_thread(task_store.list_chats_by_project, project_id)
    lanes = [
        r for r in rows
        if r["id"] != caller_chat_id
        and (u.is_admin or r.get("user_sub") == owner
             or r.get("parent_chat_id") == caller_chat_id)
    ]
    if not lanes:
        raise HTTPException(
            404,
            f"No delegation project '{project_id}' is visible to you — "
            "adopt an existing project's slug, or start one by passing "
            "project_id on a delegate call.",
        )
    await asyncio.to_thread(
        task_store.update_chat, caller_chat_id,
        delegate_role="orchestrator", project_id=project_id,
    )
    _broadcast_orchestrator_stamp(caller_chat_id, project_id)
    logger.info(
        f"Project adopted: project={project_id} chat={caller_chat_id} "
        f"by={u.sub or u.agent or '-'} lanes={len(lanes)}"
    )
    return {
        "ok": True,
        "project_id": project_id,
        "chat_id": caller_chat_id,
        "lanes": [
            {
                "id": r["id"],
                "title": r.get("title") or "",
                "agent": r.get("agent", ""),
                "delegate_role": r.get("delegate_role") or "",
            }
            for r in lanes[:20]
        ],
    }


# --- visibility: list / peek ------------------------------------------------

_PEEK_CONTENT_CAP = 4000  # per-message char cap — peeks inform, not replay


def _require_delegation_enabled() -> None:
    state = mcp_store.get_mcp_state("delegation-mcp")
    if not state or not state.get("enabled"):
        raise HTTPException(
            status_code=403,
            detail="Delegation is disabled on this platform (delegation-mcp is turned off).",
        )


def _caller_visibility(u: UserContext, x_agent_name: str | None) -> tuple[str, str, str]:
    """(source_agent, owner, caller_chat_id) — the caller's visibility anchors.

    ``owner`` is the chat-history pool the caller's identity owns (real sub,
    or the synthetic ``agent::<slug>`` for shared-only agents / no-user
    service callers). ``caller_chat_id`` resolves from the caller's OWN
    session token — lineage authority for its spawned workers."""
    source_agent = u.agent or x_agent_name or ""
    acting = u.acting_sub
    if acting is not None:
        owner = chat_history_owner(source_agent, acting) if source_agent else acting
    elif source_agent:
        owner = f"agent::{source_agent}"
    else:
        owner = ""
    caller_chat_id = ""
    if u.session_id:
        parent = task_store.get_chat_by_session(u.session_id)
        caller_chat_id = parent["id"] if parent else ""
    return source_agent, owner, caller_chat_id


def _nouser_edge_peek(u: UserContext, chat: dict) -> bool:
    """May a NO-USER session peek this chat via a delegation edge?

    Agent-shape only — the target's shared ``agent::`` pool or its
    AGENT-SCOPE task-run chats (run-row rule; a user-scope run has a creator
    to protect and no user on this side to match it). Per-user chat pools
    are never reachable: there is no user to follow (rule 1).
    """
    chat_agent = chat.get("agent") or ""
    if not chat_agent or chat_agent not in nouser_read_targets(u):
        return False
    owner = chat.get("user_sub") or ""
    if owner == f"agent::{chat_agent}":
        return True
    if owner.startswith("task::") or (chat.get("id") or "").startswith("task-"):
        from api.agents.chats import _run_for_chat
        run = _run_for_chat(chat)
        if run is not None:
            return (run.get("scope") or "agent") != "user"
    return False


def _session_row(chat: dict, caller_chat_id: str) -> dict:
    return {
        "chat_id": chat["id"],
        "title": chat.get("title") or "",
        "status": lane_status.chat_status(chat["id"]),
        "agent": chat.get("agent") or "",
        "updated_at": chat.get("updated_at") or "",
        "origin": chat.get("origin") or "",
        "parent_chat_id": chat.get("parent_chat_id") or "",
        "project_id": chat.get("project_id") or "",
        "is_current": chat["id"] == caller_chat_id,
    }


@router.get("/v1/delegation/sessions")
async def list_delegation_sessions(
    limit: int = 30,
    agent: str | None = None,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """The caller's visibility set. Live status derived per row.

    Default (no ``agent``): its identity's own chats on its own agent plus
    workers its chat spawned (any agent) — the pre-C2 set, unchanged.

    ``agent=<slug>`` / ``agent=all`` (C2, user-backed callers): reads
    follow the user — the pools the acting user could open in the dashboard
    on that agent (their per-user chats, the shared ``agent::`` pool of a
    Shared-only agent) PLUS its task-run chats under the run-row rule
    (agent-scope runs for anyone with agent access, user-scope runs only for
    their creator). A no-user session's cross-agent reach is its DELEGATION
    roster instead: agent-shape pools only — the target's shared
    ``agent::`` pool + its agent-scope task-run chats, never per-user chats
    (there is no user to follow). No edge → 403."""
    u = require_auth(user)
    await asyncio.to_thread(_require_delegation_enabled)
    limit = max(1, min(limit, 100))
    source_agent, owner, caller_chat_id = await asyncio.to_thread(
        _caller_visibility, u, x_agent_name,
    )

    if agent:
        acting = u.acting_sub
        nouser_slugs: list[str] | None = None
        if acting is None:
            edge_reach = await asyncio.to_thread(nouser_read_targets, u)
            if agent == "all":
                nouser_slugs = sorted(edge_reach | ({u.agent} if u.agent else set()))
            elif agent == u.agent or agent in edge_reach:
                nouser_slugs = [agent]
            else:
                raise HTTPException(
                    403,
                    "This session has no user identity; its cross-agent "
                    f"visibility is its delegation targets — '{agent}' "
                    "is not on this agent's roster.",
                )
        elif agent != "all" and not u.can_access_agent(agent):
            raise HTTPException(403, f"No access to agent '{agent}'")

        def _gather_cross() -> list[dict]:
            from storage import agent_store as _agent_store
            if nouser_slugs is not None:
                slugs = nouser_slugs
            elif agent == "all":
                slugs = (_agent_store.get_agent_slugs() if u.is_admin
                         else sorted(set(u.agents) | ({u.agent} if u.agent else set())))
            else:
                slugs = [agent]
            rows: dict[str, dict] = {}
            for slug in slugs:
                # No-user callers read agent-shape pools (rule 1: no user
                # to follow); user-backed callers read the acting user's
                # dashboard pools.
                pool_owner = (f"agent::{slug}" if acting is None
                              else chat_history_owner(slug, acting))
                for c in task_store.list_chats(pool_owner, agent=slug, limit=limit):
                    rows[c["id"]] = c
                for c in task_store.list_task_chats(slug, acting or "", limit=limit):
                    rows[c["id"]] = c
            merged = sorted(rows.values(), key=lambda c: c.get("updated_at") or "",
                            reverse=True)[:limit]
            return [_session_row(c, caller_chat_id) for c in merged]

        return {"sessions": await asyncio.to_thread(_gather_cross)}

    def _gather() -> list[dict]:
        rows: dict[str, dict] = {}
        if owner:
            for c in task_store.list_chats(owner, agent=source_agent or None, limit=limit):
                rows[c["id"]] = c
        if caller_chat_id:
            for c in task_store.list_chats_by_parent(caller_chat_id, limit=limit):
                rows[c["id"]] = c
        merged = sorted(rows.values(), key=lambda c: c.get("updated_at") or "",
                        reverse=True)[:limit]
        return [_session_row(c, caller_chat_id) for c in merged]

    return {"sessions": await asyncio.to_thread(_gather)}


@router.get("/v1/delegation/sessions/{chat_id}/peek")
async def peek_delegation_session(
    chat_id: str,
    depth: int = 0,
    user: UserContext | None = Depends(get_current_user),
    x_agent_name: str | None = Header(None, alias="x-agent-name"),
):
    """Recent turns of one visible session, live-inclusive.

    DB rows carry the finished turns. A HEADLESS chat mid-turn additionally
    returns the in-progress turn from the live pump state (partial text +
    tool activity, marked ``in_progress``); interactive chats persist
    incrementally at message granularity, so their mid-turn content is
    already in the DB rows as it lands. Default: the last user + last
    assistant message; ``depth`` returns the last N user/assistant rows."""
    u = require_auth(user)
    await asyncio.to_thread(_require_delegation_enabled)
    _, owner, caller_chat_id = await asyncio.to_thread(
        _caller_visibility, u, x_agent_name,
    )

    chat = await asyncio.to_thread(task_store.get_chat, chat_id)
    if not chat:
        raise HTTPException(404, f"No chat '{chat_id}'.")
    # Own history pool (any agent — covers task-surface worker chats the
    # caller's identity owns) or a worker this chat spawned. Beyond that (C2):
    # reads follow the user — anything the ACTING USER could open in the
    # dashboard is peekable (cross-agent per-user pools, shared ``agent::``
    # pools, task-run chats via their run row). ``can_access_chat`` IS the
    # dashboard's own rule, so the two surfaces can never disagree. No-user
    # callers get the owner/lineage rule plus their DELEGATION edges
    # (agent-shape only) — never the user-derived fallback.
    if not ((owner and chat.get("user_sub") == owner)
            or (caller_chat_id and chat.get("parent_chat_id") == caller_chat_id)):
        if u.acting_sub is not None:
            from api.agents.chats import can_access_chat
            allowed = await asyncio.to_thread(can_access_chat, u, chat)
        else:
            allowed = await asyncio.to_thread(_nouser_edge_peek, u, chat)
        if not allowed:
            raise HTTPException(403, "This session is not in your visibility set.")

    # Live in-progress turn (headless pump): snapshot SYNCHRONOUSLY on the
    # event loop — the pump mutates these dicts in place on this same loop,
    # so no await may sit between the read and the copy, and raw refs must
    # never cross into a worker thread. ``_db_msg_cutoff_id`` fences the
    # persist/live seam: DB rows above the snapshot's cutoff belong to a
    # turn that persisted mid-request and would duplicate the live copy.
    from copy import deepcopy
    from core.events.stream_pump import _active_pumps
    from core.session.session_state import _chat_streaming_state
    live_snapshot: list[dict] = []
    cutoff: int | None = None
    _pump = _active_pumps.get(chat_id)
    if _pump is not None and not _pump.is_done:
        cutoff = _pump._db_msg_cutoff_id
        _live = _chat_streaming_state.get(chat_id)
        if _live and _live.get("live_blocks"):
            live_snapshot = deepcopy(_live["live_blocks"])

    def _render_live(blocks: list[dict]) -> str:
        parts: list[str] = []
        for b in blocks:
            bt = b.get("type")
            if bt == "text" and b.get("content"):
                parts.append(str(b["content"]))
            elif bt == "tool":
                label = b.get("summary") or b.get("name") or "tool"
                state = "running" if b.get("active") else "done"
                parts.append(f"[tool {state}: {label}]")
        return "\n".join(parts)

    def _peek() -> dict:
        rows = task_store.get_chat_messages(chat_id)
        if cutoff is not None:
            rows = [m for m in rows if int(m.get("id") or 0) <= cutoff]
        turns = [
            m for m in rows
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        if depth > 0:
            picked = turns[-min(depth, 50):]
        else:
            picked = [m for m in (
                next((m for m in reversed(turns) if m["role"] == "user"), None),
                next((m for m in reversed(turns) if m["role"] == "assistant"), None),
            ) if m]
            picked.sort(key=lambda m: m.get("id") or 0)
        messages = []
        for m in picked:
            content = m["content"]
            if len(content) > _PEEK_CONTENT_CAP:
                content = content[:_PEEK_CONTENT_CAP] + "\n… [truncated]"
            messages.append({"role": m["role"], "content": content})
        if live_snapshot:
            partial = _render_live(live_snapshot)
            if partial:
                if len(partial) > _PEEK_CONTENT_CAP:
                    partial = partial[:_PEEK_CONTENT_CAP] + "\n… [truncated]"
                messages.append({
                    "role": "assistant", "content": partial,
                    "in_progress": True,
                })
        return {
            "chat_id": chat_id,
            "title": chat.get("title") or "",
            "agent": chat.get("agent") or "",
            "status": lane_status.chat_status(chat_id),
            "messages": messages,
            "truncated": len(turns) > len(picked),
        }

    return await asyncio.to_thread(_peek)
