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
from auth.providers import UserContext, get_current_user, require_auth, require_write
from storage import agent_store
from storage import database as task_store

from api.agents._common import _get_execution_paths
from api.agents._router import router

logger = logging.getLogger("claude-proxy.agents")

# Section route modules — imported for their ``@router`` registrations. ORDER
# MATTERS: discovery -> files -> user-context, then the CRUD routes below, to
# reproduce the original route-declaration order.
from api.agents import discovery as _discovery
from api.agents import files as _files
from api.agents import user_context as _user_context

# Backwards-compatible re-exports: api.media.* and the test-suite import these
# helpers/handlers from ``api.agents.agents``.
safe_agent_path = _files.safe_agent_path
_check_file_role = _files._check_file_role
_build_tree = _files._build_tree
RecoverRestoreRequest = _files.RecoverRestoreRequest
restore_recover_bin = _files.restore_recover_bin
discard_recover_bin = _files.discard_recover_bin

# Reference the side-effect-only modules so linters see the imports as used
# (importing them is what registers the discovery/user-context routes).
_SECTION_MODULES = (_discovery, _files, _user_context)


def _require_admin(user: UserContext | None) -> UserContext:
    if not user or user.role != "admin":
        raise HTTPException(403, "Admin only")
    return user


def _require_manage(user: UserContext | None) -> UserContext:
    if not user or user.role not in ("admin", "creator"):
        raise HTTPException(403, "Admin or creator only")
    return user


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
    execution_target: str | None = None  # "local" or machine_id
    default_scope: str | None = None  # "user" | "agent"
    collaborative: bool | None = None  # visibility-modes second axis
    # Per-agent default execution mode: "" (unset) | "interactive" | "-p".
    # Only valid when the agent's default model is a CLI execution layer.
    default_execution_mode: str | None = None


class DeleteAgentRequest(BaseModel):
    confirm_slug: str


class SetDefaultForNewUsersBody(BaseModel):
    enabled: bool
    role: str | None = None  # required when enabled=True; one of viewer/editor/manager


@router.post("/v1/agents")
async def create_agent(req: CreateAgentRequest, user: UserContext = Depends(get_current_user)):
    """Create a new agent with folder structure and DB record."""
    u = _require_manage(user)

    if req.admin_only and u.role != "admin":
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
    valid_paths = {"claude-code-cli", "direct-llm", "codex-cli"}
    if req.execution_path:
        if req.execution_path not in valid_paths:
            raise HTTPException(400, f"Invalid execution_path. Valid: {valid_paths}")
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
        (agent_dir / "config" / "context").mkdir(parents=True, exist_ok=True)
        (agent_dir / "workspace").mkdir(parents=True, exist_ok=True)
        (agent_dir / "users").mkdir(parents=True, exist_ok=True)

        prompt_file = agent_dir / "config" / "prompt.md"
        prompt_file.write_text(f"# {req.display_name}\n\n")
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
    from storage import mcp_store
    from services.mcp import mcp_registry
    core_mcps = [
        name for name, m in mcp_registry.get_all_manifests().items()
        if m.category == "core"
        and m.assignment_mode != "explicit"
    ]
    if core_mcps:
        try:
            await asyncio.to_thread(mcp_store.set_manager_enabled_mcps, slug, core_mcps)
            # Seed skill entries for assigned MCPs
            for mcp_name in core_mcps:
                m = mcp_registry.get_manifest(mcp_name)
                if m:
                    for skill in m.skills:
                        await asyncio.to_thread(
                            mcp_store.ensure_agent_skill, slug, skill.id,
                            default_enabled=True,
                            default_exclude_from=skill.default_exclude_from,
                        )
        except Exception as exc:
            # Non-critical
            logger.debug(f"Seeding core MCPs/skills for new agent {slug} failed: {exc}")

    # Assign creator as manager of the new agent (preserve existing assignments)
    from storage import database as task_store
    try:
        current_roles = await asyncio.to_thread(task_store.get_user_agent_roles, u.sub)
        if slug not in current_roles:
            current_roles[slug] = "manager"
            await asyncio.to_thread(
                task_store.set_user_agents, u.sub, list(current_roles.keys()), u.sub,
                agent_roles=current_roles,
            )
    except Exception as exc:
        # Non-critical
        logger.debug(f"Assigning creator as manager of new agent {slug} failed: {exc}")

    return agent


@router.patch("/v1/agents/{name}")
async def update_agent(name: str, req: UpdateAgentRequest, user: UserContext = Depends(get_current_user)):
    """Update agent configuration."""
    # Editing an agent's config requires manager authority OVER THIS AGENT, not
    # merely platform creator-tier + any access — otherwise a creator who is only
    # a viewer of someone else's agent could rewrite its configuration.
    u = _require_manage(user)
    if not u.can_manage_agent(name):
        raise HTTPException(403, "Manager or admin access required for this agent")

    if not agent_store.agent_exists(name):
        raise HTTPException(404, "Agent not found")

    if req.admin_only and u.role != "admin":
        raise HTTPException(403, "Only admins can set admin-only flag")

    valid_paths = {"claude-code-cli", "direct-llm", "codex-cli"}

    if req.execution_paths is not None:
        for p in req.execution_paths:
            if p not in valid_paths:
                raise HTTPException(400, f"Invalid execution path: {p}. Valid: {valid_paths}")
        if len(req.execution_paths) == 0:
            raise HTTPException(400, "At least one execution path required")

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
            _layers = config.get_model_layers(_dm) if _dm else []
            if _layers and not ({"claude-code-cli", "codex-cli"} & set(_layers)):
                raise HTTPException(
                    400,
                    "default_execution_mode can only be set when the agent's default "
                    "model runs on a CLI execution layer (claude-code-cli or codex-cli).",
                )

    # Validate execution_target
    if req.execution_target is not None and req.execution_target != "local":
        # Per-user satellite isolation: setting an agent's default
        # execution target is admin-only AND the target machine must be
        # admin-owned. Non-admin users (managers) cannot point an agent at
        # any remote machine via this endpoint; they may set a personal
        # override via PUT /v1/users/me/remote-target instead.
        if u.role != "admin":
            raise HTTPException(
                403,
                "Only admins can set an agent's default remote execution target. "
                "Use User Settings → Remote Machines to set a personal override instead.",
            )
        from storage import remote_store
        machine = remote_store.get_remote_machine(req.execution_target)
        if not machine:
            raise HTTPException(400, "Remote machine not found")
        if machine.get("pairing_scope", "") != "admin":
            raise HTTPException(
                403,
                "Agent default execution targets must be admin-paired machines. "
                "Machine '" + (machine.get("name") or req.execution_target) + "' "
                "is user-paired; user-paired machines are for "
                "personal session overrides only.",
            )
        # Direct LLM cannot run remotely
        agent = agent_store.get_agent(name)
        ep = req.execution_paths[0] if req.execution_paths else (agent or {}).get("execution_path", "")
        if ep == "direct-llm":
            raise HTTPException(400, "Direct LLM agents always run locally")

    fields = req.model_dump(exclude_none=True)
    # Keep the PRIMARY execution layer consistent with the default model whenever
    # EITHER is updated. A no-picker session/task uses (primary layer + default
    # model), and a model only runs on its OWN layer (e.g. gpt-5.6-sol is codex-cli-
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
        primary = paths[0] if paths else (existing.get("execution_path") or "claude-code-cli")
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
        current = (agent_store.get_agent(name) or {}).get("execution_target", "local")
        if _target != current:
            if _target == "local":
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
    _require_admin(user)
    if not agent_store.agent_exists(name):
        raise HTTPException(404, "Agent not found")
    if body.enabled:
        if body.role not in ("viewer", "editor", "manager"):
            raise HTTPException(
                400,
                "When enabled=True, role must be one of 'viewer', 'editor', 'manager'",
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
    _require_admin(user)

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

    # Delete from DB (all related tables)
    deleted = await asyncio.to_thread(agent_store.delete_agent, name)
    if not deleted:
        raise HTTPException(404, "Agent not found")

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
        from storage import recover_bin_store
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

    # The Conversations tab is for EXTERNAL conversations (phone today, more
    # sources later). Dashboard chats (source_type='chat') live on the
    # chat-history page — exclude them here so the tab works for every agent.
    conversations = await asyncio.to_thread(
        task_store.get_agent_conversations, name,
        source_type=source_type, exclude_sources=("chat",), offset=offset, limit=limit,
    )
    total = await asyncio.to_thread(
        task_store.count_agent_conversations, name,
        source_type=source_type, exclude_sources=("chat",),
    )
    return {"conversations": conversations, "total": total}


@router.get("/v1/chats/{chat_id}/detail")
async def get_chat_detail(
    chat_id: str,
    user: UserContext = Depends(get_current_user),
):
    """Get chat metadata with access control.

    Accessible to: chat owner, admins, or managers of Shared-only/phone agents.
    """
    u = require_auth(user)
    chat = await asyncio.to_thread(task_store.get_chat, chat_id)
    if not chat:
        raise HTTPException(404, "Chat not found")

    # Access check: owner, admin, or an assigned user of a Shared-only agent's
    # agent-scoped chat (phone conversations, tasks, meetings, shared history).
    if chat["user_sub"] != u.sub and u.role != "admin":
        from core.session.visibility import is_shared_chat_owner, is_shared_only
        agent = chat.get("agent", "")
        is_assigned = u.can_access_agent(agent)
        is_agent_scoped = (
            is_shared_only(agent)
            or chat.get("source_type") == "phone"
            or is_shared_chat_owner(chat.get("user_sub", ""))
        )
        if not (is_assigned and is_agent_scoped):
            raise HTTPException(403, "Access denied")

    return chat
