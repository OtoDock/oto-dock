"""Centralized spawn authorization for delegated workers.

Both delegate surfaces — background task runs and first-class worker chats —
call :func:`authorize_spawn` BEFORE any task/chat/run row is created, so a
policy denial always precedes the resource-admission (RAM) veto and the
scope × role × target-mode matrix lives in exactly one place.

Identity rules mirror ``api/tasks/tasks._resolve_creator_identity`` (token-
authoritative: the caller's cookie/JWT attributes, client headers never do).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException

from auth.providers import UserContext
from core.session.visibility import SHARED_CHAT_OWNER_PREFIX, available_scopes_for
from storage import agent_store, mcp_store
from storage import database as task_store

logger = logging.getLogger("claude-proxy.delegation")

# Per-creator ceiling on concurrently active delegated workers. One admin-set
# value (``mcp_config_values['delegation-mcp'].MAX_PARALLEL_SPAWNS``); the
# manifest declares the same default so the admin config editor shows it.
DEFAULT_MAX_PARALLEL_SPAWNS = 4


@dataclass
class SpawnAuthz:
    """The resolved authorization for one worker spawn."""

    created_by: str          # attribution: real user sub, or the source agent slug
    acting_sub: str | None   # the real user; None for service / master-key callers
    scope: str               # final scope after clamping to the target's mode
    scope_note: str          # non-empty when the requested scope was clamped
    chat_owner: str          # chats.user_sub for a surface="chat" worker
    source_agent: str        # token-authoritative delegating agent


def _max_parallel_spawns() -> int:
    raw = mcp_store.get_mcp_config_values("delegation-mcp").get("MAX_PARALLEL_SPAWNS")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PARALLEL_SPAWNS
    return value if value > 0 else DEFAULT_MAX_PARALLEL_SPAWNS


def authorize_spawn(
    user: UserContext,
    *,
    target_agent: str,
    requested_scope: str,
    source_agent: str | None = None,
    surface: str = "task",
    x_agent_name: str | None = None,
) -> SpawnAuthz:
    """Authorize one delegated-worker spawn; raises HTTPException on denial.

    Order matters: platform kill-switch first, then target/roster/access,
    then scope clamping to the target's mode, then identity + role for the
    FINAL scope, and the per-creator cap last — all before any row exists.
    """
    # 1. Platform kill-switch. The mcp_state row exists only where the
    #    delegation-mcp manifest was scanned — the public cut ships without
    #    the folder, so these endpoints are dormant there by construction.
    state = mcp_store.get_mcp_state("delegation-mcp")
    if not state or not state.get("enabled"):
        raise HTTPException(
            status_code=403,
            detail="Delegation is disabled on this platform (delegation-mcp is turned off).",
        )

    if requested_scope not in ("user", "agent"):
        raise HTTPException(status_code=400, detail=f"Invalid scope: {requested_scope!r}")

    target_row = agent_store.get_agent(target_agent)
    if not target_row:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {target_agent}")

    # 2. Delegating agent + roster. The source is token-authoritative: a
    #    session caller's JWT names the agent it was minted for; the master
    #    key falls back to X-Agent-Name; a dashboard cookie has no delegating
    #    agent (self-delegation to the target).
    source = user.agent or x_agent_name or source_agent or target_agent
    if source != target_agent:
        allowed = agent_store.get_delegation_targets(source)
        if target_agent not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{source}' cannot delegate to '{target_agent}' — "
                       "not in its delegation targets.",
            )
    # A real user must additionally have access to the target agent; service
    # callers are covered by the roster (agent-to-agent policy).
    acting = user.acting_sub
    if acting is not None and not user.can_access_agent(target_agent):
        raise HTTPException(
            status_code=403,
            detail=f"You do not have access to agent '{target_agent}'.",
        )

    # 3. Clamp the requested scope to what the target's visibility mode
    #    offers. Machine-to-machine: a silent clamp with a note beats a 400
    #    that wastes the delegating agent's turn.
    avail = available_scopes_for(
        bool(target_row.get("collaborative", True)),
        target_row.get("default_scope") or "user",
    )
    scope, scope_note = requested_scope, ""
    if requested_scope not in avail:
        clamped = avail[0]
        if clamped == "user" and acting is None:
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{target_agent}' only offers user-scoped sessions "
                       "and this caller has no user identity.",
            )
        scope = clamped
        scope_note = (
            f"Note: scope '{requested_scope}' is not offered by "
            f"'{target_agent}' — clamped to '{scope}'."
        )

    # 4. Identity for the FINAL scope (parity with _resolve_creator_identity).
    if acting is not None:
        created_by = acting
    elif scope == "user":
        if user.is_no_user_session:
            raise HTTPException(
                status_code=403,
                detail="This session has no user identity and cannot spawn "
                       "user-scoped workers.",
            )
        raise HTTPException(
            status_code=400,
            detail="User-scoped workers cannot be spawned with the master API "
                   "key; they must come from a user session.",
        )
    else:
        created_by = source

    # 5. Agent-scope work from a real user is gated at the editor tier —
    #    the same bar as agent-scope tasks (viewers are read-only).
    if scope == "agent" and acting is not None and not user.can_edit_agent(target_agent):
        raise HTTPException(
            status_code=403,
            detail="Agent-scoped workers require editor, manager, or admin "
                   "role for this agent.",
        )

    # 6. Worker-chat owner. Shared-only targets pool into the synthetic
    #    per-agent history; a real creator owns their worker; agent-scope
    #    service spawns on a collaborative target also pool (no per-user dir
    #    to hang them on — visible via list_sessions/peek).
    if avail == ("agent",) or acting is None:
        chat_owner = f"{SHARED_CHAT_OWNER_PREFIX}{target_agent}"
    else:
        chat_owner = acting

    # 7. Per-creator cap — policy "no" beats the RAM-gate veto, so this runs
    #    before any task/chat/run row exists.
    cap = _max_parallel_spawns()
    active = task_store.count_active_delegate_runs(created_by)
    if active + 1 > cap:
        raise HTTPException(
            status_code=403,
            detail=f"Delegation limit reached: {active} worker(s) already "
                   f"active (max {cap}). Wait for one to finish, or ask an "
                   "admin to raise MAX_PARALLEL_SPAWNS for delegation-mcp.",
        )

    return SpawnAuthz(
        created_by=created_by,
        acting_sub=acting,
        scope=scope,
        scope_note=scope_note,
        chat_owner=chat_owner,
        source_agent=source,
    )


def validate_spawn_overrides(
    target_agent: str, layer: str | None, model: str | None,
) -> None:
    """Reject a spawn override outside the target agent's configured
    envelope — a clear 400, never a silent downgrade. The implied execution
    TARGET is untouched by design: overrides pick the layer/model/mode ON
    the agent's configured target; they can never reroute a worker onto a
    machine the delegating context couldn't use."""
    if not (layer or model):
        return
    info = agent_store.get_agent(target_agent) or {}
    if layer:
        # Same parse as the agents API: primary execution_path + the
        # execution_paths JSON-string extras — never treat the raw column
        # string as a sequence (`in` would degrade to substring matching).
        from api.agents._common import _get_execution_paths
        allowed = _get_execution_paths(info)
        if layer not in allowed:
            raise HTTPException(
                400,
                f"Execution layer '{layer}' is not enabled for agent "
                f"'{target_agent}' (enabled: {', '.join(allowed) or 'none'}).",
            )
    if model:
        exec_path = layer or info.get("execution_path", "")
        # Same source of truth as the dashboard's cross-layer model guard
        # (ws/dashboard._model_allowed_for_path): a registry error must not
        # brick spawns — this guards cross-layer poison, not the registry.
        # Enabled-only: the delegation roster advertises enabled models and
        # the validator must accept exactly that set (an admin-disabled
        # model is disabled for spawn overrides too).
        try:
            from storage import subscription_store
            ok = any(
                (m.get("model_id") or "") == model and m.get("enabled")
                for m in subscription_store.list_models(exec_path)
            )
        except Exception:
            ok = True
        if not ok:
            raise HTTPException(
                400,
                f"Model '{model}' is not available on execution layer "
                f"'{exec_path}'.",
            )
