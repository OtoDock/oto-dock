"""Agent management REST API — CRUD + package router assembly.

Create / update / delete agents and the agent-conversation views. The
file-tree, discovery, and user-context endpoints live in sibling modules
(``files`` / ``discovery`` / ``user_context``) and attach to the shared
``api.agents._router.router``, imported below so their routes register in
the original order ahead of the CRUD routes defined here.

Auth: API key (server-to-server) OR OAuth2 session cookie (dashboard users)."""

import asyncio
import logging
import shutil

from fastapi import Depends, HTTPException
from pydantic import BaseModel

import config
from core import placement
from auth.providers import (
    UserContext, get_current_user, require_admin, require_auth, require_creator, require_write,
    session_bound_to,
)
from storage.agents import agent_store
from storage import database as task_store

from api.agents._common import _get_execution_paths
from api.agents._router import router
from core.session import session_kind
from auth import roles
from core import layout

logger = logging.getLogger("claude-proxy.agents")



def _engine_label(execution_path: str) -> str:
    """The engine's display name for the enablement gate's 403s — read from
    the registry, so a fourth engine names itself."""
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities(execution_path)
    return caps.display_name if caps else execution_path

# Section route modules — imported for their ``@router`` registrations. ORDER
# MATTERS: discovery -> files -> user-context, then the CRUD routes below, to
# reproduce the original route-declaration order.
from api.agents import discovery as _discovery
from api.agents import files as _files
from api.agents import user_context as _user_context
from api.agents import activity as _activity
from api.agents import knowledge_libraries as _knowledge_libraries
from core.execution_layer import DEFAULT_EXECUTION_PATH

# Backwards-compatible re-exports: api.media.* and the test-suite import these
# helpers/handlers from ``api.agents.agents``.
safe_agent_path = _files.safe_agent_path
_check_file_role = _files._check_file_role
_build_tree = _files._build_tree
RecoverRestoreRequest = _files.RecoverRestoreRequest
restore_recover_bin = _files.restore_recover_bin
discard_recover_bin = _files.discard_recover_bin

# Reference the side-effect-only modules so linters see the imports as used
# (importing them is what registers the discovery/user-context/knowledge-
# library routes). EVERY section module MUST appear here: an import missing
# from this tuple reads as unused to ruff, and a `--fix` silently deletes it
# — which unregistered all knowledge-library routes on 2026-08-28 (404s in
# the agent config's Shared Knowledge card). tests/api/test_route_registry.py
# pins the route set against exactly that.
_SECTION_MODULES = (_discovery, _files, _user_context, _activity,
                    _knowledge_libraries)


class CreateAgentRequest(BaseModel):
    display_name: str
    slug: str = ""
    admin_only: bool = False
    # Empty = auto-select the first available engine for the creator (the
    # dashboard create flow never pins one). See create_agent() below.
    execution_path: str = ""
    default_model: str = ""
    default_effort: str = ""
    color: str = ""
    description: str = ""
    # v3: per-agent default scope for memory / tasks / notifications /
    # triggers / meetings. Must be "user" or "agent".
    default_scope: str = "user"
    # Visibility-modes second axis. With default_scope → the 4 modes
    # (Personal+shared / Shared+personal / Personal only / Shared only).
    collaborative: bool = True


class UpdateAgentRequest(BaseModel):
    display_name: str | None = None
    admin_only: bool | None = None
    execution_paths: list[str] | None = None  # first = primary execution_path
    default_model: str | None = None
    default_effort: str | None = None
    color: str | None = None
    description: str | None = None
    execution_target: str | None = None  # placement.LOCAL or a machine id
    default_scope: str | None = None  # "user" | "agent"
    collaborative: bool | None = None  # visibility-modes second axis
    # Per-agent default execution mode: "" (unset) | "interactive" | "-p".
    # Only valid when the agent's default model is a CLI execution layer.
    default_execution_mode: str | None = None
    # Departments (agents map): ONE department per agent; set BOTH to assign,
    # both "" to clear. Cookie admins + creators only (field-level gate in
    # update_agent — assigning wires delegation edges into other teams).
    department_id: str | None = None
    department_level_id: str | None = None


class DeleteAgentRequest(BaseModel):
    confirm_slug: str


class SetDefaultForNewUsersBody(BaseModel):
    enabled: bool
    role: str | None = None  # required when enabled=True; one of viewer/editor/manager


@router.post("/v1/agents")
async def create_agent(req: CreateAgentRequest, user: UserContext = Depends(get_current_user)):
    """Create a new agent with folder structure and DB record."""
    u = require_creator(user)

    if req.admin_only and not u.is_admin:
        raise HTTPException(403, "Only admins can create admin-only agents")

    # Generate/validate slug
    slug = req.slug.strip() if req.slug else agent_store.sanitize_slug(req.display_name)
    if not slug or len(slug) < 2:
        raise HTTPException(400, "Slug must be at least 2 characters")
    # Re-sanitize even if provided
    slug = agent_store.sanitize_slug(slug)

    # Check uniqueness
    if agent_store.agent_exists(slug):
        raise HTTPException(409, f"Agent '{slug}' already exists")

    agent_dir = config.AGENTS_DIR / slug
    if agent_dir.exists():
        raise HTTPException(409, f"Agent directory '{slug}' already exists on disk")

    # Agent-count limit. Gates two cases: cloud free tier (1 agent)
    # and a self-hosted license expired > 30 days (stage-2 graceful downgrade).
    # Self-hosted community/licensed-valid installs are unlimited (allowed=True).
    from auth.license import check_agent_count_limit
    allowed, current, max_agents = await asyncio.to_thread(check_agent_count_limit)
    if not allowed:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Agent limit reached ({current}/{max_agents}). "
                "Upgrade your plan or renew your license to add more agents."
            ),
        )

    # Resolve the execution layer (AI engine). When the client doesn't pin one
    # (the dashboard create flow never does), auto-enable the first engine that's
    # connected on BOTH the platform and the creator's own account — claude-code-cli,
    # then codex-cli, never direct-llm — so the agent works zero-config. Falls back
    # to claude-code-cli. See subscription_pool.default_execution_layer_for_creator.
    from core.session.session_manager import valid_execution_paths
    valid_paths = valid_execution_paths()
    if req.execution_path:
        if req.execution_path not in valid_paths:
            raise HTTPException(
                400, f"Invalid execution_path. Valid: {sorted(valid_paths)}")
        # Same platform-configured gate as PATCH (cookie-admin bypass; see
        # update_agent). The no-picker auto-default below is deliberately NOT
        # gated — it may fall back to claude-code-cli on a bare platform so a
        # fresh install can always create its first agent.
        if not (u.is_admin and not u.is_api_key):
            from services.engines import subscription_pool
            if not await asyncio.to_thread(
                subscription_pool.layer_platform_configured, req.execution_path
            ):
                raise HTTPException(
                    403,
                    f"No {_engine_label(req.execution_path)} "
                    "subscription is connected on this platform",
                )
        execution_path = req.execution_path
    else:
        from services.engines import subscription_pool
        execution_path = await asyncio.to_thread(
            subscription_pool.default_execution_layer_for_creator, u.sub
        )

    # Validate default_scope
    if req.default_scope not in ("user", "agent"):
        raise HTTPException(400, "default_scope must be 'user' or 'agent'")

    # Create folder structure
    try:
        (agent_dir / layout.CONFIG / layout.CONTEXT).mkdir(parents=True, exist_ok=True)
        (agent_dir / layout.WORKSPACE).mkdir(parents=True, exist_ok=True)
        (agent_dir / layout.USERS).mkdir(parents=True, exist_ok=True)

        persona_file = agent_dir / layout.CONFIG / "agent.md"
        persona_file.write_text(f"# {req.display_name}\n\n")
    except Exception as e:
        # Clean up on failure
        if agent_dir.exists():
            shutil.rmtree(agent_dir, ignore_errors=True)
        raise HTTPException(500, f"Failed to create agent directory: {e}")

    # Insert DB record
    try:
        agent = await asyncio.to_thread(
            agent_store.create_agent,
            slug, req.display_name,
            admin_only=req.admin_only,
            execution_path=execution_path,
            default_model=req.default_model,
            default_effort=req.default_effort,
            created_by=u.sub,
            color=req.color,
            description=req.description,
            default_scope=req.default_scope,
            collaborative=req.collaborative,
        )
    except Exception as e:
        shutil.rmtree(agent_dir, ignore_errors=True)
        raise HTTPException(500, f"Failed to create agent record: {e}")

    # Auto-assign core MCPs. ``exclude_from`` values (``phone``, ``task``) are
    # session-time filters and don't affect assignment — they're handled in
    # mcp_registry.get_agent_mcps() at session config time.
    from services.mcp import mcp_registry
    try:
        await asyncio.to_thread(mcp_registry.assign_core_mcps, slug)
    except Exception as exc:
        # Non-critical
        logger.debug(f"Seeding core MCPs/skills for new agent {slug} failed: {exc}")

    # The creator manages the new agent. One additive insert: rewriting the
    # person's whole role list here would undo an admin's concurrent change.
    from storage import database as task_store
    try:
        await asyncio.to_thread(task_store.add_user_agent, u.sub, slug, roles.MANAGER, u.sub)
        from services.notifications.notification_manager import invalidate_audience
        invalidate_audience(slug)
    except Exception as exc:
        # Non-critical
        logger.debug(f"Assigning creator as manager of new agent {slug} failed: {exc}")

    return agent


@router.patch("/v1/agents/{name}", dependencies=[Depends(session_bound_to("name"))])
async def update_agent(name: str, req: UpdateAgentRequest, user: UserContext = Depends(get_current_user)):
    """Update agent configuration."""
    # Editing an agent's config requires manager authority OVER THIS AGENT and
    # nothing more: the platform role is deliberately not consulted — a platform
    # member who manages this agent may configure it, while a platform creator
    # who is only a viewer of someone else's agent may not.
    u = require_auth(user)
    if not u.can_manage_agent(name):
        raise HTTPException(403, "Manager or admin access required for this agent")

    if not agent_store.agent_exists(name):
        raise HTTPException(404, "Agent not found")

    if req.admin_only and not u.is_admin:
        raise HTTPException(403, "Only admins can set admin-only flag")

    from core.session.session_manager import valid_execution_paths
    valid_paths = valid_execution_paths()

    if req.execution_paths is not None:
        for p in req.execution_paths:
            if p not in valid_paths:
                raise HTTPException(
                    400,
                    f"Invalid execution path: {p}. Valid: {sorted(valid_paths)}")
        if len(req.execution_paths) == 0:
            raise HTTPException(400, "At least one execution path required")
        # Platform-configured gate on NEWLY-ADDED engines only (set difference
        # vs the stored set — reorders and removals always pass, and an engine
        # whose platform subscription has since vanished stays grandfathered so
        # a saved config can never become unsavable). Bypass is COOKIE admins
        # only: the config-MCP authenticates with a session token carrying the
        # session owner's sub, so an admin-created task/trigger session would
        # otherwise hand the bypass to a prompt-injectable LLM turn.
        if not (u.is_admin and not u.is_api_key):
            from services.engines import subscription_pool
            _stored = agent_store.get_agent(name) or {}
            _current_set = set(_get_execution_paths(_stored))
            for p in req.execution_paths:
                if p in _current_set:
                    continue
                if not await asyncio.to_thread(
                    subscription_pool.layer_platform_configured, p
                ):
                    raise HTTPException(
                        403,
                        f"No {_engine_label(p)} subscription is "
                        "connected on this platform",
                    )

    # ``getattr`` defends against test mocks that don't declare every
    # optional field on their ``_Req`` subclass.
    _req_default_scope = getattr(req, "default_scope", None)
    if _req_default_scope is not None and _req_default_scope not in ("user", "agent"):
        raise HTTPException(400, "default_scope must be 'user' or 'agent'")

    # Per-agent default execution mode. Only meaningful for CLI execution
    # layers — reject when the agent's default model only runs on direct-llm
    # (which can't run the interactive TUI).
    _req_exec_mode = getattr(req, "default_execution_mode", None)
    if _req_exec_mode is not None:
        if _req_exec_mode not in ("", "interactive", "-p"):
            raise HTTPException(400, "default_execution_mode must be '', 'interactive', or '-p'")
        if _req_exec_mode in ("interactive", "-p"):
            _existing = agent_store.get_agent(name) or {}
            _dm = req.default_model or _existing.get("default_model", "")
            _layers = set(config.get_model_layers(_dm)) if _dm else set()
            # The CLI layer must be one THIS AGENT uses: a local model served
            # by both Direct LLM and Codex resolves to codex-cli too, which
            # would let a Direct-LLM-only agent store an interactive default
            # it can never run (live-hit 2026-09-07).
            _paths = set(
                req.execution_paths if req.execution_paths is not None
                else _get_execution_paths(_existing)
            )
            from core.session.session_manager import get_all_layers
            _pty_layers = {
                p for p, l in get_all_layers().items()
                if l.capabilities.runtime.supports_interactive_pty
            }
            if _layers and not (_pty_layers & _layers & _paths):
                raise HTTPException(
                    400,
                    "default_execution_mode can only be set when the agent's default "
                    "model runs on an engine with an interactive terminal "
                    f"({' or '.join(sorted(_pty_layers))}) that this agent uses.",
                )

    # Validate execution_target
    if req.execution_target is not None and not placement.is_local(req.execution_target):
        # Per-user satellite isolation: setting an agent's default
        # execution target is admin-only AND the target machine must be
        # admin-owned. Non-admin users (managers) cannot point an agent at
        # any remote machine via this endpoint; they may set a personal
        # override via PUT /v1/users/me/remote-target instead.
        if not u.is_admin:
            raise HTTPException(
                403,
                "Only admins can set an agent's default remote execution target. "
                "Use User Settings → Remote Machines to set a personal override instead.",
            )
        from storage import remote_store
        machine = remote_store.get_remote_machine(req.execution_target)
        if not machine:
            raise HTTPException(400, "Remote machine not found")
        if not placement.machine_is_admin_paired(machine):
            raise HTTPException(
                403,
                "Agent default execution targets must be admin-paired machines. "
                "Machine '" + (machine.get("name") or req.execution_target) + "' "
                "is user-paired; user-paired machines are for "
                "personal session overrides only.",
            )
        # An engine that cannot run on a satellite refuses a remote target
        agent = agent_store.get_agent(name)
        ep = req.execution_paths[0] if req.execution_paths else (agent or {}).get("execution_path", "")
        from core.session.session_manager import get_layer_capabilities
        _ec = get_layer_capabilities(ep)
        if _ec is not None and not _ec.runtime.supports_remote_execution:
            raise HTTPException(400, f"{_ec.display_name} agents always run locally")

    # Department assignment (agents map). The endpoint gate above
    # (can_manage_agent) still applies — this field gate NARROWS within it,
    # same layering as execution_target: a manager reaches the endpoint and is
    # rejected only on this field. Wiring an agent into a department creates
    # delegation edges into OTHER teams, so it is platform-role territory.
    # WIDENED 2026-08-15 (operator decision, shared-libraries design
    # § D-MCP) from cookie-only to
    # ``acting_sub``-backed principals: a REAL-USER session token with
    # admin/creator platform role may now assign departments too — the
    # agent-config-mcp ``set_department`` tool rides this, sitting in the
    # CRITICAL permission tier — always prompts, in EVERY mode incl.
    # dontAsk, and is denied outright in no-human contexts — so a human
    # approves each call in chat (the mitigation for the prompt-injection
    # vector the old cookie-only predicate defended against). Master key +
    # no-user sessions stay out.
    _req_dept = getattr(req, "department_id", None)
    _req_dept_level = getattr(req, "department_level_id", None)
    if _req_dept is not None or _req_dept_level is not None:
        if u.acting_sub is None or not roles.is_creator_or_above(u.role):
            raise HTTPException(
                403,
                "Only admins and creators can change an agent's department.",
            )
        _dept = (_req_dept or "").strip()
        _level = (_req_dept_level or "").strip()
        if bool(_dept) != bool(_level):
            raise HTTPException(
                400,
                "department_id and department_level_id must be set together "
                "(or both empty to clear the assignment).",
            )
        if _dept:
            from storage.agents import db_departments
            dept_row = await asyncio.to_thread(
                db_departments.get_department, _dept
            )
            if not dept_row:
                raise HTTPException(400, "Department not found")
            if _level not in {lv["id"] for lv in dept_row["levels"]}:
                raise HTTPException(
                    400, "Level does not belong to that department"
                )

    fields = req.model_dump(exclude_none=True)
    # Keep the PRIMARY execution layer consistent with the default model whenever
    # EITHER is updated. A no-picker session/task uses (primary layer + default
    # model), and a model only runs on its OWN layer (e.g. gpt-6-sol is codex-cli-
    # only), so a mismatch makes delegated/no-picker runs hard-reject the model
    # (the personal-assistant delegate bug). resolve_execution_path reads the
    # scalar `execution_path`, so reconciling it here is what fixes those runs.
    # We MUST handle a default_model-only change too: the UI sends just
    # default_model when the layer checkboxes didn't change, which would
    # otherwise leave a stale primary that can't run the new model.
    if "execution_paths" in fields or "default_model" in fields:
        import json
        existing = agent_store.get_agent(name) or {}
        if "execution_paths" in fields:
            paths = list(fields.pop("execution_paths"))
        else:
            paths = _get_execution_paths(existing)  # current enabled set [primary, …]
        primary = paths[0] if paths else (
            existing.get("execution_path") or DEFAULT_EXECUTION_PATH)
        dm = fields.get("default_model") or existing.get("default_model", "")
        if dm:
            dm_layers = config.get_model_layers(dm)
            # The frontend sends execution_paths in checkbox order, which need not
            # put the default model's layer first — so if the primary can't run
            # the default model, promote the model's layer (if it's enabled).
            if dm_layers and primary not in dm_layers:
                promoted = next((p for p in paths if p in dm_layers), "")
                if promoted:
                    primary = promoted
        if paths:
            fields["execution_path"] = primary
            additional = [p for p in paths if p != primary]
            fields["execution_paths"] = json.dumps(additional) if additional else ""

    # Route execution-target changes through remote_store so BOTH stores stay
    # in lockstep: `agents.execution_target` (what the resolver reads) AND
    # `agent_remote_targets` (what the admin Remote Machines page lists).
    # Writing only the column here left the machine's agent list blind to
    # assignments made from Agent Settings — the same agent could then be
    # "added again" from the machine page. The store helpers write both.
    if "execution_target" in fields:
        from storage import remote_store
        _target = fields.pop("execution_target")
        current = (agent_store.get_agent(name) or {}).get("execution_target") or placement.LOCAL
        if _target != current:
            if placement.is_local(_target):
                await asyncio.to_thread(remote_store.remove_agent_remote_target, name)
            else:
                await asyncio.to_thread(
                    remote_store.set_agent_remote_target, name, _target, u.sub,
                )
        if not fields:
            return agent_store.get_agent(name)
    if not fields:
        return agent_store.get_agent(name)

    result = await asyncio.to_thread(agent_store.update_agent, name, **fields)
    if not result:
        raise HTTPException(404, "Agent not found")
    # Membership changed → the department edge set changed with it.
    if "department_id" in fields or "department_level_id" in fields:
        from services.departments import edge_compiler
        await asyncio.to_thread(edge_compiler.recompile)
    # If this update put the agent into Shared-only mode (one shared chat history
    # across all users), drop any user→personal-machine overrides for it: a
    # shared-only agent may not run on a personal machine. Admin defaults (admin
    # machines) are untouched; reverting to any other mode re-allows personal
    # overrides. Computed from the returned row (no stale cache).
    if "default_scope" in fields or "collaborative" in fields:
        now_shared_only = (
            not result.get("collaborative", True)
            and (result.get("default_scope") or "user") == "agent"
        )
        if now_shared_only:
            from storage import remote_store
            removed = await asyncio.to_thread(
                remote_store.clear_user_remote_targets_for_agent, name
            )
            if removed:
                logger.info(
                    "agent %s → shared-only: cleared %d personal-machine remote "
                    "override(s)", name, removed,
                )
    return result


@router.put("/v1/admin/agents/{name}/default-for-new-users")
async def admin_set_default_for_new_users(
    name: str,
    body: SetDefaultForNewUsersBody,
    user: UserContext | None = Depends(get_current_user),
):
    """Toggle whether every new user signing up is auto-attached to this agent.

    Admin-only — agent managers cannot make their agent the default for
    every platform user. When ``enabled=False`` the role column is cleared
    to the empty string (auto-attach disabled). When ``enabled=True`` the
    role must be one of ``viewer`` / ``editor`` / ``manager`` and is
    persisted to ``agents.default_for_new_users_role``.

    Does NOT backfill existing users — only NEW user creations trigger
    the attach pass. Admin can manually attach existing users via the
    user settings page.
    """
    require_admin(user)
    if not agent_store.agent_exists(name):
        raise HTTPException(404, "Agent not found")
    if body.enabled:
        if body.role not in roles.AGENT_ROLES:
            raise HTTPException(
                400,
                "When enabled=True, role must be one of "
                + ", ".join(repr(r) for r in roles.AGENT_ROLES),
            )
        new_role = body.role
    else:
        new_role = ""
    await asyncio.to_thread(
        agent_store.set_default_for_new_users_role, name, new_role,
    )
    logger.info(
        "Admin updated default-for-new-users for %s: enabled=%s role=%r",
        name, body.enabled, new_role,
    )
    return {
        "agent_slug": name,
        "enabled": bool(new_role),
        "default_for_new_users_role": new_role,
    }


@router.delete("/v1/agents/{name}")
async def delete_agent(name: str, req: DeleteAgentRequest, user: UserContext = Depends(get_current_user)):
    """Delete an agent permanently. Admin only, requires slug confirmation."""
    require_admin(user)

    if req.confirm_slug != name:
        raise HTTPException(400, "Slug confirmation does not match")

    if not agent_store.agent_exists(name):
        raise HTTPException(404, "Agent not found")

    # Best-effort vendor DELETE for any service-scope webhook
    # subscriptions bound to this agent. Must run BEFORE agent_store.delete_agent
    # so service account OAuth tokens (used to call vendor delete APIs)
    # are still readable.
    try:
        from services.webhooks import subscription_manager
        await subscription_manager.cleanup_agent_subscriptions(name)
    except Exception:
        logger.exception(
            "Subscription cleanup raised for agent %s (continuing with agent delete)",
            name,
        )

    # App servers stop BEFORE the tree goes (a process on an unlinked
    # release would linger until its idle stop; APPS.md "Lifecycle").
    try:
        from services.apps import app_lifecycle
        await app_lifecycle.stop_agent_apps(name)
    except Exception:
        logger.exception("App servers of agent %s did not stop cleanly (continuing)", name)

    # Knowledge-library consumers must be read BEFORE the DB cascade wipes
    # the attachment rows — their mirror dirs live in OTHER agents' trees,
    # which the rmtree below never reaches.
    from storage.knowledge import db_knowledge_libraries
    _lib_consumers = [
        a["consumer_agent"] for a in await asyncio.to_thread(
            db_knowledge_libraries.consumers_of, name)
    ]

    # Delete from DB (all related tables)
    deleted = await asyncio.to_thread(agent_store.delete_agent, name)
    if not deleted:
        raise HTTPException(404, "Agent not found")

    if _lib_consumers:
        from services.knowledge import library_projector
        asyncio.create_task(
            library_projector.teardown_source(name, consumers=_lib_consumers))
        logger.info("knowledge-library teardown on agent delete: %s → %d consumers",
                    name, len(_lib_consumers))

    # Delete filesystem: the agent folder (all user/agent workspace, knowledge,
    # config + Claude/Codex session files) AND the recover-bin tree, which lives
    # OUTSIDE the agent folder (under RECOVER_BIN_DIR/<slug>/) so the rmtree below
    # doesn't reach it. delete_agent already dropped the recover_bin metadata rows.
    agent_dir = config.AGENTS_DIR / name
    if agent_dir.exists():
        try:
            shutil.rmtree(agent_dir)
        except Exception as e:
            logger.warning("Failed to remove agent directory %s: %s", name, e)
    try:
        from storage.files import recover_bin_store
        await asyncio.to_thread(recover_bin_store.remove_agent_files, name)
    except Exception as e:
        logger.warning("Failed to remove recover-bin files for %s: %s", name, e)

    return {"status": "deleted", "slug": name}


@router.get("/v1/agents/{name}/conversations")
async def list_agent_conversations(
    name: str,
    source_type: str = "",
    offset: int = 0,
    limit: int = 50,
    user: UserContext = Depends(get_current_user),
):
    """List all conversations for an agent. Managers + admins only."""
    u = require_auth(user)
    limit = max(1, min(limit, config.MAX_PAGE_SIZE))  # cap page size
    # Operators only (manager + admin). Phone transcripts are agent-scope and
    # shared across the agent's managers/admins BY DESIGN (phone is not per-user),
    # so there is no per-user filter — but viewers + editors must not see them.
    # Matches the frontend tab gate (canManage, AgentLayout.tsx).
    require_write(u, name)

    # The Conversations tab is for EXTERNAL conversations — the kinds an
    # out-of-band driver owns (phone today; a new one declares itself in
    # core/session/session_kind). Dashboard chats and task runs live in the
    # sidebar, so the tab works for every agent.
    conversations = await asyncio.to_thread(
        task_store.get_agent_conversations, name,
        source_type=source_type, source_types=session_kind.EXTERNAL_DRIVEN_SOURCE_TYPES,
        offset=offset, limit=limit,
    )
    total = await asyncio.to_thread(
        task_store.count_agent_conversations, name,
        source_type=source_type, source_types=session_kind.EXTERNAL_DRIVEN_SOURCE_TYPES,
    )
    return {"conversations": conversations, "total": total}


def can_open_chat(u: UserContext, chat: dict) -> bool:
    """May this user open this chat BY ID — the rule the detail route and
    the dashboard's ``resume_chat`` share. The REST rule
    (``api/agents/chats.py::can_access_chat``: the owner, an admin, a
    Shared-only agent's ``agent::`` pool for its assigned users, a task
    run's chat by its run) plus phone conversations for the agent's
    managers, which is who the conversations list serves. The chat's OWNER
    decides, never the agent's current mode: a per-user chat from before an
    agent turned Shared-only stays its owner's."""
    from api.agents.chats import can_access_chat
    from core.session.visibility import is_phone_chat_owner
    if session_kind.of_chat(chat) is session_kind.PHONE \
            or is_phone_chat_owner(chat.get("user_sub")):
        return u.is_admin or u.can_manage_agent(chat.get("agent", ""))
    return can_access_chat(u, chat)


@router.get("/v1/chats/{chat_id}/detail")
async def get_chat_detail(
    chat_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Get chat metadata with access control (``can_open_chat``): the
    owner, an admin, an assigned user for a Shared-only agent's shared
    history or an agent-scope task run, a manager for a phone conversation.
    """
    u = require_auth(user)
    chat = await asyncio.to_thread(task_store.get_chat, chat_id)
    if not chat:
        raise HTTPException(404, "Chat not found")
    if not await asyncio.to_thread(can_open_chat, u, chat):
        raise HTTPException(403, "Access denied")
    return chat
