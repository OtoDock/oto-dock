"""Agent discovery + configuration endpoints.

Listing, metadata/info, remote target status, post-install setup
completion, delegation targets, the per-agent users overview, and
browser-origin allow-lists. Attaches to the shared package router."""

import asyncio
import logging

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel

import config
from core import placement
from auth.providers import UserContext, get_current_user, require_agent_access, require_auth, require_human
from storage.agents import agent_store
from storage import database as task_store
from storage.automation import trigger_store
from storage.pg import run_db

from api.agents._common import _get_agent_dir, _get_execution_paths
from api.agents._router import router
from core.execution_layer import DEFAULT_EXECUTION_PATH, PROVIDER_LOCAL
from core import layout

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


def _mcp_names_by_agent(slugs: list[str]) -> dict[str, list[str]]:
    """``_get_mcp_info``'s names for many agents from bulk reads: the
    manager-enabled sets, the platform state and the explicit-instance
    visibility once each, the manifests from the registry's in-memory map,
    and every capability probe once per capability token. The rule mirrors
    ``mcp_registry._agent_base_manifests`` drop for drop (not manager-enabled,
    not platform-enabled, unknown manifest, explicit and not visible,
    capability unavailable) plus the skill filter of ``_get_mcp_info``;
    the listing's parity test holds the two together."""
    from services.mcp import mcp_registry
    from storage.mcp import mcp_store
    enabled = mcp_store.get_all_manager_enabled_mcps()
    state = mcp_store.get_all_mcp_states()
    visible = mcp_store.get_visible_explicit_mcps_by_agent(slugs)
    available: dict[str, bool] = {}
    out: dict[str, list[str]] = {}
    for slug in slugs:
        names: list[str] = []
        for name in enabled.get(slug, ()):
            if not state.get(name, False):
                continue
            manifest = mcp_registry.get_manifest(name)
            if not manifest:
                continue
            if manifest.assignment_mode == "explicit" and name not in visible.get(slug, ()):
                continue
            cap = manifest.requires_capability or ""
            if cap not in available:
                available[cap] = mcp_registry.manifest_capability_available(manifest)
            if not available[cap] or manifest.category == "skill":
                continue
            names.append(name)
        out[slug] = sorted(names)
    return out


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
    # "agent" | "user" | None. None resolves from which setup file applies:
    # exactly one → that one; both → 400 asking for the argument; neither →
    # agent (legacy no-op semantics).
    scope: str | None = None


class DelegationTargetsRequest(BaseModel):
    targets: list[str]


class BrowserOriginsRequest(BaseModel):
    origins: list[str]


@router.get("/v1/execution-layers")
async def list_execution_layers(user: UserContext = Depends(get_current_user)):
    """Return capabilities for all registered execution layers.

    Used by the frontend to dynamically populate model selectors,
    permission mode dropdowns, and execution path options.

    Merges admin-managed custom models (enabled only) into each layer's
    model list. Custom models appear after builtin models.

    Authenticated (cookie or session token): the per-layer ``configured``
    flag reveals which AI vendors the platform has subscriptions for — every
    consumer (dashboard pages, config-MCP, satellite tunnel) already carries
    credentials, so nothing legitimate is anonymous here.
    """
    require_auth(user)
    return await run_db(_execution_layers_sync)


def _execution_layers_sync() -> dict:
    """The catalog as ONE executor job. It only reads: the builtin model
    rows are synced at boot (startup.py) and by the admin execution-layer
    pages, never by this page-load GET (the sync's writes and commit held
    the loop on every new-chat page)."""
    from core.session.session_manager import get_all_capabilities
    from services.engines import subscription_pool
    from storage.billing import subscription_status, subscription_store

    caps = get_all_capabilities()
    result = {}
    for path, layer_dict in caps.items():
        layer = dict(layer_dict)
        # Platform-level availability: drives the AgentConfig engine-card
        # enable gate (mirror of the PATCH /v1/agents server gate).
        layer["configured"] = subscription_pool.layer_platform_configured(path)
        # Merge enabled custom models from admin DB
        db_models = subscription_store.list_models(layer=path)

        # Determine which providers are available platform-wide (the agent pool).
        # This is a global selector — per-user personal availability lives in the
        # user execution-layers endpoint; here we reflect contribute_platform subs
        # so one user's personal-only provider doesn't advertise models to everyone.
        active_subs = subscription_store.list_subscriptions(layer=path, contribute_platform=True)
        active_providers = {s["provider"] for s in active_subs
                            if s.get("status") == subscription_status.ACTIVE}

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
                    # An admin-tagged tier reaches the pickers the same way
                    # a builtin's registry tier does.
                    "tier": m.get("tier"),
                    "tier_label": config.MODEL_TIER_LABELS.get(m.get("tier") or 0, ""),
                    "good_at": m.get("good_at") or "",
                })
            # Respect admin disable on builtin models
            if m["is_builtin"] and not m["enabled"]:
                merged = [x for x in merged if x["value"] != m["model_id"]]

        # Filter: only show models whose provider has an active subscription.
        # "System Default" (value="") always passes. The engine's
        # model_filter_policy says how: "all" filters every provider,
        # "local_providers" hides only the local endpoints without a row (a
        # user on a personal vendor account must keep seeing the vendor's
        # builtins even when the platform pool has no vendor row), "none"
        # never filters.
        policy = (layer.get("model_policy") or {}).get("model_filter_policy", "none")
        if policy == "all" and active_providers:
            merged = [
                m for m in merged
                if not m.get("value")  # "System Default"
                or m.get("provider", "anthropic") in active_providers
            ]
        elif policy == "local_providers":
            # Self-hosted providers (the descriptor's local kind) — their
            # models exist only while an endpoint row is active on the layer.
            local_ids = {p["id"] for p in (layer.get("providers") or []) if p.get("kind") == PROVIDER_LOCAL}
            merged = [
                m for m in merged
                if not m.get("value")
                or m.get("provider", "openai") not in local_ids
                or m.get("provider") in active_providers
            ]

        layer["models"] = merged
        # What "Auto" runs on this engine right now: the resolver's own answer
        # for an unpinned agent (declared default → fall-down, restricted to
        # what the platform pool serves), with the row's display name so the
        # dashboard never has to find it in ``models`` — whose provider filter
        # is NOT the resolver's (it counts every active contribute_platform
        # row; the resolver counts the pool's, i.e. owner-less or admin-owned).
        # "" when nothing is enabled on the engine.
        try:
            auto = config.resolve_layer_default_model(path)
        except RuntimeError:
            auto = ""
        layer["auto_model"] = auto
        layer["auto_model_label"] = next(
            ((m.get("display_name") or auto) for m in db_models if m.get("model_id") == auto),
            auto,
        ) if auto else ""
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
    return await run_db(_list_agents_sync, u, all)


def _list_agents_sync(u: UserContext, all: bool) -> dict:
    """The listing as ONE executor job: the counts, the bulk MCP view and
    one model resolution per engine, so its cost no longer grows with the
    agent count on the loop (every visible dashboard polls it)."""
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

    visible: list[str] = []
    for name in sorted(agent_store.get_agent_slugs()):
        # Admin: respect agent checkboxes for UI (show only assigned agents)
        # unless ?all=true is passed (admin pages need platform-wide view).
        if u.is_admin:
            if not all and u.agents and name not in u.agents:
                continue
        elif not u.can_access_agent(name):
            continue
        visible.append(name)
    mcp_names_by_agent = _mcp_names_by_agent(visible)
    # What "Auto" runs on an engine is the same for every unpinned agent on
    # it: resolved once per engine per request. Display-only: an engine with
    # nothing enabled shows an empty model cell instead of a 500 (the
    # _safe_model contract); a pinned agent shows its pin, unchecked, as
    # resolve_agent_model answers with no layer argument.
    layer_default: dict[str, str] = {}

    def _model(agent_data: dict | None) -> str:
        pinned = (agent_data or {}).get("default_model") or ""
        if pinned:
            return pinned
        path = (agent_data or {}).get("execution_path") or DEFAULT_EXECUTION_PATH
        if path not in layer_default:
            try:
                layer_default[path] = config.resolve_layer_default_model(path)
            except RuntimeError:
                layer_default[path] = ""
        return layer_default[path]

    agents = []
    for name in visible:
        agent_dir = config.get_agent_dir(name)
        mcp_names = mcp_names_by_agent.get(name, [])
        schedule_count = task_counts.get(name, 0)
        trigger_count = trigger_counts.get(name, 0)
        has_workspace = (agent_dir / layout.WORKSPACE).is_dir()
        agent_data = agent_store.get_agent(name)

        agents.append({
            "name": name,
            "display_name": agent_data["display_name"] if agent_data else name,
            "execution_path": agent_data["execution_path"] if agent_data else DEFAULT_EXECUTION_PATH,
            "execution_paths": _get_execution_paths(agent_data),
            "execution_target": (agent_data.get("execution_target") or placement.LOCAL) if agent_data else placement.LOCAL,
            "default_model": _model(agent_data),
            "default_scope": (agent_data.get("default_scope") if agent_data else "user") or "user",
            "default_execution_mode": (agent_data.get("default_execution_mode", "") if agent_data else ""),
            "collaborative": bool(agent_data.get("collaborative", True)) if agent_data else True,
            "color": agent_data.get("color", "") if agent_data else "",
            "description": agent_data.get("description", "") if agent_data else "",
            "mcp_count": len(mcp_names),
            "mcp_names": mcp_names,
            "schedule_count": schedule_count,
            "trigger_count": trigger_count,
            "has_workspace": has_workspace,
            "department_id": (agent_data.get("department_id", "") if agent_data else "") or "",
            "department_level_id": (agent_data.get("department_level_id", "") if agent_data else "") or "",
        })

    return {"agents": agents}


@router.get("/v1/agents/{name}/info")
async def get_agent_info(name: str, user: UserContext | None = Depends(get_current_user)):
    """Return detailed metadata for a single agent."""
    u = require_auth(user)
    require_agent_access(u, name)

    def _read() -> tuple[list[str], bool, dict | None, bool, list[str]]:
        agent_dir = _get_agent_dir(name)
        _, mcp_names = _get_mcp_info(name)
        return (mcp_names, (agent_dir / layout.WORKSPACE).is_dir(),
                agent_store.get_agent(name), agent_store.is_admin_only(name),
                agent_store.get_delegation_targets(name))

    # The store reads as one executor job; the registry fetch below is an
    # awaited HTTP call and stays on the loop.
    mcp_names, has_workspace, agent_data, admin_only, delegation_targets = await run_db(_read)

    # A template install's catalog state for the Config tab's banner
    # (COMMUNITY-AGENTS-REGISTRY.md "Updates"); the registry is cached, an
    # unreachable one leaves the field out rather than slowing the page.
    template_update = None
    provenance = str((agent_data or {}).get("community_template") or "")
    if provenance and not provenance.startswith("local:"):
        try:
            from services.community import community_agent_updater, community_agents_catalog
            registry = await community_agents_catalog.fetch_registry()
            entry = next((e for e in registry.get("agents", []) if e.get("slug") == provenance), None)
            template_update = community_agent_updater.detect(agent_data, entry)
        except Exception:
            template_update = None

    return {
        "name": name,
        "display_name": agent_data["display_name"] if agent_data else name,
        "admin_only": admin_only,
        "execution_path": agent_data["execution_path"] if agent_data else DEFAULT_EXECUTION_PATH,
        "execution_paths": _get_execution_paths(agent_data),
        "execution_target": (agent_data.get("execution_target") or placement.LOCAL) if agent_data else placement.LOCAL,
        "default_model": agent_data["default_model"] if agent_data else "",
        "default_effort": agent_data["default_effort"] if agent_data else "",
        "default_scope": (agent_data.get("default_scope") if agent_data else "user") or "user",
        "collaborative": bool(agent_data.get("collaborative", True)) if agent_data else True,
        "color": agent_data.get("color", "") if agent_data else "",
        "description": agent_data.get("description", "") if agent_data else "",
        "mcps": mcp_names,
        "has_workspace": has_workspace,
        "delegation_targets": delegation_targets,
        "community_template": agent_data.get("community_template") if agent_data else None,
        "community_template_version": agent_data.get("community_template_version") if agent_data else None,
        "template_update": template_update,
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
        "department_id": (agent_data.get("department_id", "") if agent_data else "") or "",
        "department_level_id": (agent_data.get("department_level_id", "") if agent_data else "") or "",
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
        {"state": "online"|"stale"|"paused"|"disconnected"|"never_connected",
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
    target = agent_data.get("execution_target") or placement.LOCAL
    if placement.is_local(target):
        return {"state": None}
    machine = remote_store.get_remote_machine(target) or {}
    if not placement.machine_is_admin_paired(machine):
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
    """Mark an agent's post-install setup complete — agent-wide or per-user.

    Two scopes, resolved from ``body.scope`` or from which setup file applies
    (exactly one present → that one; both → 400 asking for the argument;
    neither → agent, preserving the legacy idempotent no-op):

    **scope=agent** — the original semantics, manager/admin (or the agent
    principal) only: delete ``config/context/setup.md`` if present, stamp
    ``agents.setup_completed_at`` if not already set, notify the installer on
    the first incomplete→complete transition. Re-runs return
    ``already_complete`` — never an error.

    **scope=user** — per-user onboarding: delete the CALLER's own
    ``users/{u}/context/user-setup.md``. The username comes from the session
    identity, never from the request body — a user cannot complete another
    user's setup. Any attached user may call it (viewers included: the seeded
    onboarding targets exactly the default-attach audience). File already
    absent → idempotent success.

    All file deletes carry tombstone + git + fan-out bookkeeping — a bare
    unlink would be resurrected by the next satellite sync. The work is
    ``services/agents/setup_state.py``, which the platform methods an app
    declares (``setup.status`` / ``setup.complete``) share.
    """
    from services.agents import setup_state
    u = require_auth(user)
    require_agent_access(u, name)
    agent = agent_store.get_agent(name)
    if not agent:
        raise HTTPException(404, f"Agent '{name}' not found")
    if body.scope not in (None, "agent", "user"):
        raise HTTPException(400, "scope must be 'agent' or 'user'")

    username = ""
    if u.acting_sub is not None:
        username = task_store.get_username_by_sub(u.sub) or ""

    scope = body.scope
    if scope is None:
        agent_file = setup_state.agent_setup_path(name).is_file()
        own = setup_state.user_setup_path(name, username)
        user_file = own is not None and own.is_file()
        if agent_file and user_file:
            raise HTTPException(
                400,
                "Both agent-wide setup.md and your user-setup.md are present "
                "— pass scope='agent' or scope='user' to say which one is "
                "complete.",
            )
        scope = "user" if user_file else "agent"

    if scope == "user":
        if not username:
            raise HTTPException(
                400,
                "scope='user' requires a user session (agent-scoped sessions "
                "have no per-user setup).",
            )
        return await setup_state.complete_user_setup(name, username, u.sub)

    # scope == "agent" — completing setup FOR THE WHOLE AGENT is a
    # config-level act: manager/admin, or the agent principal (agent-scoped
    # sessions act as the agent itself). Viewers/editors could previously
    # reach this by direct POST; the seeded per-user flow is scope=user.
    if not (u.is_admin or u.can_manage_agent(name) or u.acting_sub is None):
        raise HTTPException(
            403, "Completing agent-wide setup requires manager access.",
        )
    return await setup_state.complete_agent_setup(name, agent, summary=body.summary or "")


@router.get("/v1/agents/{name}/delegation-targets")
async def get_delegation_targets(name: str, user: UserContext | None = Depends(get_current_user)):
    """List delegation targets for an agent + available agents to set as targets."""
    u = require_auth(user)
    require_agent_access(u, name)
    if not u.can_manage_agent(name):
        raise HTTPException(403, "Manager role required for this agent")

    # ``targets`` = the MANUAL set (what the checkbox UI owns). Department-
    # compiled edges are reported separately so the UI can render them
    # checked-but-locked ("via <department>") and keep them OUT of the PUT
    # payload — the compiler owns those rows.
    detailed = await asyncio.to_thread(
        agent_store.get_delegation_targets_detailed, name
    )
    targets = [d["target"] for d in detailed if d["source"] == "manual"]
    compiled_targets = [d["target"] for d in detailed if d["source"] == "department"]
    compiled = []
    if compiled_targets:
        from storage.agents import db_departments
        agent_row = agent_store.get_agent(name) or {}
        dept = None
        if agent_row.get("department_id"):
            dept = await asyncio.to_thread(
                db_departments.get_department, agent_row["department_id"]
            )
        compiled = [
            {
                "target": t,
                "department_id": (dept or {}).get("id", ""),
                "department_name": (dept or {}).get("name", ""),
            }
            for t in compiled_targets
        ]

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

    return {
        "targets": targets,
        "compiled": compiled,
        "available": available,
    }


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
            # An admin acts as admin whatever the row says.
            "platform_role": r["platform_role"],
        }
        for r in rows
    ]
    return {"users": users}


@router.put("/v1/agents/{name}/delegation-targets")
async def set_delegation_targets(
    name: str, req: DelegationTargetsRequest, user: UserContext | None = Depends(get_current_user),
):
    """Set delegation targets for an agent (replace all).

    A wired edge also grants this agent's NO-USER sessions read visibility
    into the target's agent-scope activity (merged observe semantics), so
    the per-target reach check below doubles as the read-grant authority —
    a manager never grants more than they can see themselves. Human-only
    (``require_human``): an edge also lets the agent's apps call the
    target agent's apps (APPS.md "Bindings"), so a prompt in a manager's
    session must not be able to wire one.
    """
    u = require_human(user)
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
    # Un-checking a manual edge that a department also wants must let the
    # compiler immediately re-assert it as source='department'.
    from services.departments import edge_compiler
    await asyncio.to_thread(edge_compiler.recompile)
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
