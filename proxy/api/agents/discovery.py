"""Agent discovery + configuration endpoints.

Listing, metadata/info, remote target status, post-install setup
completion, delegation targets, the per-agent users overview, and
browser-origin allow-lists. Attaches to the shared package router."""

import asyncio
import logging

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel

import config
from auth.providers import UserContext, get_current_user, require_agent_access, require_auth
from storage import agent_store
from storage import database as task_store
from storage import trigger_store

from api.agents._common import _get_agent_dir, _get_execution_paths
from api.agents._router import router

logger = logging.getLogger("claude-proxy.agents")


def _get_mcp_info(name: str) -> tuple[int, list[str]]:
    """Return (count, names) of MCPs this agent is CONFIGURED with.

    Configuration view (visible AND manager-enabled AND platform-enabled),
    INCLUDING device-local MCPs regardless of placement — the overview badge
    reflects what the admin assigned, not the local-session subset. Per-session
    runtime sets use ``get_agent_mcps`` with a resolved target.
    """
    from services.mcp import mcp_registry
    manifests = mcp_registry.get_agent_mcps_all_placements(name)
    # Standalone skill packages aren't MCPs from the caller's perspective
    # (discovery cards + delegation surfaces) — they carry no tools.
    names = sorted(m.name for m in manifests if m.category != "skill")
    return len(names), names


def _safe_model(name: str) -> str:
    """Resolve agent's default model for display, returning '' on failure.

    `config.resolve_agent_model` raises RuntimeError when no enabled model
    is available. For list-agents display we'd rather show an empty model
    cell than 500 the whole page. Real session-starting paths (warmup, tasks,
    phone) let the exception propagate so the user sees a meaningful error.
    """
    try:
        return config.get_cli_model(name)
    except RuntimeError:
        return ""


class CompleteSetupBody(BaseModel):
    summary: str = ""


class DelegationTargetsRequest(BaseModel):
    targets: list[str]


class BrowserOriginsRequest(BaseModel):
    origins: list[str]


@router.get("/v1/execution-layers")
async def list_execution_layers():
    """Return capabilities for all registered execution layers.

    Used by the frontend to dynamically populate model selectors,
    permission mode dropdowns, and execution path options.

    Merges admin-managed custom models (enabled only) into each layer's
    model list. Custom models appear after builtin models.
    """
    from core.session.session_manager import get_all_capabilities
    from storage import subscription_store

    caps = get_all_capabilities()
    result = {}
    for path, layer_dict in caps.items():
        layer = dict(layer_dict)
        # Merge enabled custom models from admin DB
        subscription_store.sync_builtin_models(path, layer.get("models", []))
        db_models = subscription_store.list_models(layer=path)

        # Determine which providers are available platform-wide (the agent pool).
        # This is a global selector — per-user personal availability lives in the
        # user execution-layers endpoint; here we reflect contribute_platform subs
        # so one user's personal-only provider doesn't advertise models to everyone.
        active_subs = subscription_store.list_subscriptions(layer=path, contribute_platform=True)
        active_providers = {s["provider"] for s in active_subs if s.get("status") == "active"}

        # Build merged model list: builtin models + enabled custom models
        builtin_ids = {m["value"] for m in layer.get("models", [])}
        merged = list(layer.get("models", []))
        for m in db_models:
            if not m["is_builtin"] and m["enabled"] and m["model_id"] not in builtin_ids:
                merged.append({
                    "value": m["model_id"],
                    "label": m["display_name"],
                    "provider": m.get("provider", ""),
                    # supports_xhigh flows through so the AgentConfig effort
                    # dropdown can hide the "XHigh" option on custom models
                    # that the admin hasn't explicitly flagged.
                    "supports_xhigh": bool(m.get("supports_xhigh", False)),
                })
            # Respect admin disable on builtin models
            if m["is_builtin"] and not m["enabled"]:
                merged = [x for x in merged if x["value"] != m["model_id"]]

        # Filter: only show models whose provider has an active subscription.
        # "System Default" (value="") always passes. CLI layer models pass
        # (provider filtering only applies to direct-llm).
        if active_providers and path == "direct-llm":
            merged = [
                m for m in merged
                if not m.get("value")  # "System Default"
                or m.get("provider", "anthropic") in active_providers
            ]

        layer["models"] = merged
        result[path] = layer
    return result


@router.get("/v1/agents")
async def list_agents(
    all: bool = Query(False, alias="all"),
    user: UserContext | None = Depends(get_current_user),
):
    """List all registered agents with summary metadata.

    Admins + API key: all agents. Others: only assigned agents.
    Pass ?all=true to skip admin checkbox filtering (for admin pages).
    """
    u = require_auth(user)

    # Bulk DB counts (avoids N queries-per-agent in the loop below). The
    # schedule/trigger numbers must reflect what THIS caller can see — agent-scoped
    # (shared) + their OWN user-scoped — and never leak other users' private items,
    # so they match the Scheduled Tasks / Triggers tabs for the same user. An
    # API-key caller is agent-scope (sees the agent's full set), like those tabs.
    if u.is_api_key:
        task_counts = task_store.count_dynamic_tasks_by_agent()
        trigger_counts = trigger_store.count_triggers_by_agent()
    else:
        task_counts = task_store.count_user_visible_dynamic_tasks_by_agent(u.sub)
        trigger_counts = trigger_store.count_user_visible_triggers_by_agent(u.sub)

    agents = []
    for name in sorted(agent_store.get_agent_slugs()):
        # Admin: respect agent checkboxes for UI (show only assigned agents)
        # unless ?all=true is passed (admin pages need platform-wide view).
        if u.is_admin:
            if not all and u.agents and name not in u.agents:
                continue
        elif not u.can_access_agent(name):
            continue
        agent_dir = config.get_agent_dir(name)
        mcp_count, mcp_names = _get_mcp_info(name)
        schedule_count = task_counts.get(name, 0)
        trigger_count = trigger_counts.get(name, 0)
        has_workspace = (agent_dir / "workspace").is_dir()
        agent_data = agent_store.get_agent(name)

        agents.append({
            "name": name,
            "display_name": agent_data["display_name"] if agent_data else name,
            "execution_path": agent_data["execution_path"] if agent_data else "claude-code-cli",
            "execution_paths": _get_execution_paths(agent_data),
            "execution_target": agent_data.get("execution_target", "local") if agent_data else "local",
            # Display-only: swallow "no model available" exceptions so the
            # agent grid still renders. Frontend shows empty model text when
            # admin hasn't enabled any models yet for this agent's layer.
            "default_model": _safe_model(name),
            "default_scope": (agent_data.get("default_scope") if agent_data else "user") or "user",
            "default_execution_mode": (agent_data.get("default_execution_mode", "") if agent_data else ""),
            "collaborative": bool(agent_data.get("collaborative", True)) if agent_data else True,
            "color": agent_data.get("color", "") if agent_data else "",
            "description": agent_data.get("description", "") if agent_data else "",
            "mcp_count": mcp_count,
            "mcp_names": mcp_names,
            "schedule_count": schedule_count,
            "trigger_count": trigger_count,
            "has_workspace": has_workspace,
        })

    return {"agents": agents}


@router.get("/v1/agents/{name}/info")
async def get_agent_info(name: str, user: UserContext | None = Depends(get_current_user)):
    """Return detailed metadata for a single agent."""
    u = require_auth(user)
    require_agent_access(u, name)

    agent_dir = _get_agent_dir(name)
    _, mcp_names = _get_mcp_info(name)
    has_workspace = (agent_dir / "workspace").is_dir()
    agent_data = agent_store.get_agent(name)

    return {
        "name": name,
        "display_name": agent_data["display_name"] if agent_data else name,
        "admin_only": agent_store.is_admin_only(name),
        "execution_path": agent_data["execution_path"] if agent_data else "claude-code-cli",
        "execution_paths": _get_execution_paths(agent_data),
        "execution_target": agent_data.get("execution_target", "local") if agent_data else "local",
        "default_model": agent_data["default_model"] if agent_data else "",
        "default_effort": agent_data["default_effort"] if agent_data else "",
        "default_scope": (agent_data.get("default_scope") if agent_data else "user") or "user",
        "collaborative": bool(agent_data.get("collaborative", True)) if agent_data else True,
        "color": agent_data.get("color", "") if agent_data else "",
        "description": agent_data.get("description", "") if agent_data else "",
        "mcps": mcp_names,
        "has_workspace": has_workspace,
        "delegation_targets": agent_store.get_delegation_targets(name),
        "community_template": agent_data.get("community_template") if agent_data else None,
        "community_template_version": agent_data.get("community_template_version") if agent_data else None,
        "setup_completed_at": agent_data.get("setup_completed_at") if agent_data else None,
        "default_for_new_users_role": (
            agent_data.get("default_for_new_users_role", "") if agent_data else ""
        ),
        # Per-agent default execution mode:
        # "" (unset) | "interactive" | "-p". Drives the AgentConfig "Default
        # Session Mode" control (CLI-layer agents only).
        "default_execution_mode": (
            agent_data.get("default_execution_mode", "") if agent_data else ""
        ),
    }


@router.get("/v1/agents/{name}/target-status")
async def get_agent_target_status(
    name: str, user: UserContext | None = Depends(get_current_user),
):
    """Live status of the agent's effective remote execution target.

    Drives the connection dot next to the agent name in the chat / task
    TopBar (polled every 15s). Resolves the caller's INTENDED target with
    the same priority the session uses — user override > agent default —
    and reports that machine's live reachability WITHOUT the offline→local
    fallback, so an offline target still lights the dot.

    Response (200) shapes:
        {"state": null} — agent runs locally for this caller.
        {"state": "online"|"stale"|"disconnected"|"never_connected",
         "scope": "admin"|"user", "machine_name": str,
         "last_heartbeat_age_s": int|null, "last_seen_iso": str}

    `scope` lets the frontend pick severity: an admin-paired target that's
    down blocks everyone on the agent (red dot); a user's own machine
    that's down only soft-falls-back to local for that user (amber dot).
    Any user with access to the agent may call this — the admin target
    blocks all of them, and the user-override branch is naturally scoped
    to the caller's own `user_remote_targets` row.
    """
    from services.remote.remote_status import get_live_machine_status
    from storage import remote_store

    u = require_auth(user)
    require_agent_access(u, name)

    def _status_payload(target: str, scope: str) -> dict:
        machine = remote_store.get_remote_machine(target) or {}
        status = get_live_machine_status(target)
        return {
            "state": status["state"],
            "scope": scope,
            "machine_name": machine.get("name") or "",
            "last_heartbeat_age_s": status["last_heartbeat_age_s"],
            "last_seen_iso": status["last_seen_iso"],
        }

    # Priority 1: the caller's own per-agent override (their machine).
    # Honored for every role — the user owns the hardware — and reported
    # as the 'user' scope so the dot is amber when offline (the session
    # soft-falls-back to local for this user, it doesn't hard-block).
    user_target = remote_store.get_user_remote_target(u.sub, name)
    if user_target and user_target.get("machine_id"):
        return _status_payload(user_target["machine_id"], "user")

    # Priority 2: the agent's admin-paired default target. Shown to every
    # user on the agent — when it's offline it blocks all of them.
    agent_data = agent_store.get_agent(name) or {}
    target = agent_data.get("execution_target", "local") or "local"
    if not target or target == "local":
        return {"state": None}
    machine = remote_store.get_remote_machine(target) or {}
    if (machine.get("pairing_scope") or "") != "admin":
        # A user-paired machine set as an agent default shouldn't happen
        # (the admin assign endpoint refuses it), but guard anyway.
        return {"state": None}
    return _status_payload(target, "admin")


@router.post("/v1/agents/{name}/complete-setup")
async def complete_agent_setup(
    name: str,
    body: CompleteSetupBody,
    user: UserContext | None = Depends(get_current_user),
):
    """Mark an agent's post-install setup complete.

    Three side effects, all idempotent:

    1. **Delete ``config/context/setup.md``** from the agent's folder if present
       — so it stops auto-loading into context on future turns. Done from the
       proxy process (which has direct host-FS access), not from the
       sandboxed MCP. Re-running this endpoint after the file is already
       gone is a no-op (no error). Even when the agent's row already shows
       ``setup_completed_at`` set, we still attempt the file delete so a
       stale on-disk ``setup.md`` (e.g. left over after a manual stamp via
       SQL, or after a partial earlier call) gets cleaned up.
    2. **Stamp ``agents.setup_completed_at``** on the row if not already set.
    3. **Notify the installer** (``created_by``) — only on the first
       transition from "incomplete" → "complete" (not on already-complete
       re-runs, to avoid notification spam).

    Authorization: any user with access to the agent. The chat-side guard
    is the MCP's own permission matrix (viewers see no tools).
    """
    u = require_auth(user)
    require_agent_access(u, name)
    agent = agent_store.get_agent(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")

    # File delete — runs whether or not the stamp is already set, so the
    # tool stays useful for cleaning up a leftover setup.md on a stale agent.
    setup_path = _get_agent_dir(name) / "config" / "context" / "setup.md"
    setup_md_removed = False
    if setup_path.is_file():
        try:
            await asyncio.to_thread(setup_path.unlink)
            setup_md_removed = True
        except Exception:
            logger.exception("complete-setup: failed to remove setup.md for %s", name)

    if agent.get("setup_completed_at"):
        return {
            "status": "already_complete",
            "setup_completed_at": agent["setup_completed_at"],
            "setup_md_removed": setup_md_removed,
        }

    updated = await asyncio.to_thread(agent_store.mark_setup_completed, name)
    # Notify the installer (created_by) if present. If the installer's user
    # account no longer exists (deleted long after the install, edge case),
    # fall back to fanning out to every admin so the notification doesn't
    # become an orphan in the deliveries table.
    try:
        from services.notifications import notification_manager
        display = agent.get("display_name") or name
        body_text = f"`{name}` has confirmed post-install setup is complete."
        if (body.summary or "").strip():
            body_text += f"\n\nSummary: {body.summary.strip()}"

        installer_sub = agent.get("created_by") or ""
        targets: list[str] = []
        if installer_sub:
            installer_user = await asyncio.to_thread(task_store.get_user, installer_sub)
            if installer_user:
                targets = [installer_sub]
        if not targets:
            # Fallback: every admin. Rare path — only fires when the
            # installer user has been deleted since the agent install.
            from storage.pg import get_conn

            def _admin_subs() -> list[str]:
                with get_conn() as conn:
                    rows = conn.execute(
                        "SELECT sub FROM users WHERE role='admin'",
                    ).fetchall()
                    return [r["sub"] for r in rows]

            targets = await asyncio.to_thread(_admin_subs)

        for target in targets:
            await notification_manager.fire_notification(
                title=f"Setup complete for {display}",
                body=body_text,
                severity="info",
                scope="user",
                target=target,
                source="community_agent",
                source_id=name,
            )
    except Exception:
        logger.exception("complete-setup notification failed for %s", name)
    return {
        "status": "completed",
        "agent": updated,
        "setup_md_removed": setup_md_removed,
    }


@router.get("/v1/agents/{name}/delegation-targets")
async def get_delegation_targets(name: str, user: UserContext | None = Depends(get_current_user)):
    """List delegation targets for an agent + available agents to set as targets."""
    u = require_auth(user)
    require_agent_access(u, name)
    if not u.can_manage_agent(name):
        raise HTTPException(403, "Manager role required for this agent")

    targets = await asyncio.to_thread(agent_store.get_delegation_targets, name)

    # Build available list based on user's role
    all_slugs = sorted(agent_store.get_agent_slugs())
    available = []
    for slug in all_slugs:
        if slug == name:
            continue
        # Managers can only add agents they have access to
        if not u.is_admin and not u.can_access_agent(slug):
            continue
        agent_data = agent_store.get_agent(slug)
        available.append({
            "name": slug,
            "display_name": agent_data["display_name"] if agent_data else slug,
            "color": agent_data.get("color", "") if agent_data else "",
        })

    return {"targets": targets, "available": available}


@router.get("/v1/agents/{name}/users")
async def list_agent_users(name: str, user: UserContext | None = Depends(get_current_user)):
    """List the users attached to this agent with their per-agent role, for the
    agent-settings Users overview. Manager/admin of the agent only."""
    u = require_auth(user)
    require_agent_access(u, name)
    if not u.can_manage_agent(name):
        raise HTTPException(403, "Manager role required for this agent")
    rows = await asyncio.to_thread(task_store.get_agent_users_with_profile, name)
    users = [
        {
            "sub": r["sub"],
            "name": (r["display_name"] or r["name"] or r["username"]
                     or r["email"] or r["sub"]),
            "email": r["email"],
            "role": r["agent_role"],
        }
        for r in rows
    ]
    return {"users": users}


@router.put("/v1/agents/{name}/delegation-targets")
async def set_delegation_targets(
    name: str, req: DelegationTargetsRequest, user: UserContext | None = Depends(get_current_user),
):
    """Set delegation targets for an agent (replace all)."""
    u = require_auth(user)
    require_agent_access(u, name)
    if not u.can_manage_agent(name):
        raise HTTPException(403, "Manager role required for this agent")

    all_slugs = set(agent_store.get_agent_slugs())
    for target in req.targets:
        if target == name:
            raise HTTPException(400, "Agent cannot be its own delegation target")
        if target not in all_slugs:
            raise HTTPException(400, f"Target agent '{target}' not found")
        # Managers can only set targets they have access to
        if not u.is_admin and not u.can_access_agent(target):
            raise HTTPException(403, f"No access to agent '{target}'")

    await asyncio.to_thread(agent_store.set_delegation_targets, name, req.targets)
    return {"status": "saved", "agent": name, "targets": req.targets}


@router.get("/v1/agents/{name}/browser-origins")
async def get_browser_origins(name: str, user: UserContext | None = Depends(get_current_user)):
    """List the per-agent allowed browser origins (browser-control MCP)."""
    u = require_auth(user)
    require_agent_access(u, name)
    if not u.can_manage_agent(name):
        raise HTTPException(403, "Manager role required for this agent")
    origins = await asyncio.to_thread(agent_store.get_browser_allowed_origins, name)
    return {"origins": origins}


@router.put("/v1/agents/{name}/browser-origins")
async def set_browser_origins(
    name: str, req: BrowserOriginsRequest, user: UserContext | None = Depends(get_current_user),
):
    """Replace the per-agent allowed browser origins (replace all).

    Empty list = no allow-list (any origin not caught by the manifest's
    blocked-origins default is reachable). Each entry is a @playwright/mcp
    origin pattern, e.g. ``https://example.com``, ``https://example.com:8443``,
    or ``http://localhost:*``. This is a
    network-request scope, NOT a hard security boundary (the dedicated profile
    + the per-machine Browser-control grant are the real boundary).
    """
    u = require_auth(user)
    require_agent_access(u, name)
    if not u.can_manage_agent(name):
        raise HTTPException(403, "Manager role required for this agent")
    cleaned: list[str] = []
    for raw in req.origins:
        origin = raw.strip()
        if not origin:
            continue
        if ";" in origin or len(origin) > 200:
            raise HTTPException(400, f"Invalid origin {origin!r} (no ';' separator, max 200 chars)")
        if origin not in cleaned:
            cleaned.append(origin)
    await asyncio.to_thread(agent_store.set_browser_allowed_origins, name, cleaned)
    return {"status": "saved", "agent": name, "origins": cleaned}
