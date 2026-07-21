"""Centralized agent config builder for session creation.

Consolidates the prompt/MCP/security/model config building that was
duplicated across dashboard warmup, pre-warmup, and plan implementation.
"""

import asyncio
import logging

import config
from storage import agent_store
from storage import database as task_store
from storage import remote_store
from services.mcp import mcp_registry
from services.mcp import dynamic_context
from services.engines import subscription_pool
from auth.path_policy import SecurityContext, build_permission_context
from core.execution_layer import AgentConfig
from core.config.task_config_builder import TaskIdentity


def _resolve_target_fields(resolved: tuple[str, str | None]) -> dict:
    """Unpack resolve_execution_target tuple for AgentConfig spread.

    When the resolver returns the '__offline__:<machine_id>' sentinel (hard
    fail because fallback is disabled and the target is unreachable), we
    leave execution_target set to the sentinel. The warmup handler is
    expected to detect a sentinel-formatted target (see
    `is_hard_fail_target()`) and emit an error event to the client rather
    than calling start_session.
    """
    target, reason = resolved
    return {
        "execution_target": target,
        "fallback_reason": reason,
    }


def is_hard_fail_target(target: str) -> bool:
    """True iff execution_target is the offline sentinel from the resolver."""
    return target.startswith("__offline__:")


def extract_offline_machine(target: str) -> str:
    """Extract the machine_id from the '__offline__:<id>' sentinel."""
    return target.split(":", 1)[1] if ":" in target else ""

logger = logging.getLogger("claude-proxy")


async def build_agent_config(
    agent_name: str,
    user: dict,
    user_sub: str,
    user_role: str,
    permission_mode: str = "default",
    client_type: str = "dashboard",
    resume: bool = False,
    model: str = "",
    execution_path: str = "",
    codex_thread_id: str = "",
    chat_id: str = "",
    session_id: str = "",
    task_identity: TaskIdentity | None = None,
    pinned_target: str = "",
    work_cwd: str = "",
    is_otodock: bool = False,
    term: str = "",
) -> AgentConfig:
    """Build an AgentConfig from agent DB + user context.

    Centralizes prompt building, MCP config, credential resolution,
    security context, and model/effort resolution.

    Args:
        agent_name: Agent slug.
        user: User dict from DB (has username, display_name, email, role).
        user_sub: User sub (OAuth subject identifier).
        user_role: User role string (admin, manager, viewer).
        permission_mode: Hook permission mode (auto, default, plan, acceptEdits).
        client_type: Client identifier (dashboard, phone, task, sse).
        resume: Whether to resume an existing CLI session.
        model: Override model (empty = agent default).
        execution_path: Override execution path (empty = agent default from DB).
        session_id: Current session id — baked into ``OTO_SESSION_ID`` so
            Codex stdio MCPs (which receive env via TOML) get the right
            value. CLI/Direct LLM stdio MCPs also get it via env_builder
            but having it here keeps the credential_env consistent.
        task_identity: When re-warming a TASK chat (``task-{run_id}``), the
            scope/identity resolved from the run row (see
            ``resolve_task_identity``). Overrides the viewer-derived
            username/role/scope AND the ``user_sub`` used for credential /
            target / subscription / MCP-path resolution, so a continued task
            always rebuilds in ITS OWN scope — never the identity of whoever
            re-opened it. ``None`` for ordinary chats.
    """
    # Task re-warm overrides the viewer's identity with the task's stored
    # scope/identity. Without this, a dead agent-scoped task reopened by an
    # admin would rebuild with the admin's full-FS mounts (/users, /config
    # RW) instead of the agent-scope sandbox (/workspace RW, /knowledge RO).
    if task_identity is not None:
        username = task_identity.username or None
        user_role = task_identity.role
        creds_sub = task_identity.creds_user_sub  # None for agent scope
        _display_name = ""
        _email = ""
        _scope_override: str | None = task_identity.scope
    else:
        username = user.get("username") or None
        creds_sub = user_sub
        _display_name = user.get("display_name", "")
        _email = user.get("email", "")
        _scope_override = None

    # Resolve delegation targets: DB targets ∩ the session identity's agents.
    # Task re-warm mirrors the scheduler (user-scope → creator's agents;
    # agent-scope → all configured targets); chats use the viewer (admin →
    # all; else the user's own accessible agents).
    db_targets = agent_store.get_delegation_targets(agent_name)
    if task_identity is not None:
        if task_identity.scope == "user" and creds_sub:
            user_agents = set(task_store.get_user_agents(creds_sub))
            resolved_targets = [t for t in db_targets if t in user_agents]
        else:
            resolved_targets = db_targets
    elif user_role == "admin":
        resolved_targets = db_targets
    else:
        user_agents = set(task_store.get_user_agents(creds_sub))
        resolved_targets = [t for t in db_targets if t in user_agents]

    # Self-delegation is always permitted, so the agent's own slug must appear in
    # the delegate roster + DELEGATION_TARGETS env. The delegation-mcp tool already
    # allows self via its `target_agent != AGENT` bypass; this just makes it
    # visible. (_delegation_mcp_context renders self separately + skips it in the peer
    # loop; _meeting_context excludes self.)
    if agent_name not in resolved_targets:
        resolved_targets = [agent_name] + resolved_targets

    # Resolve sandbox + execution path early (needed for MCP config)
    _agent_info_early = agent_store.get_agent(agent_name) or {}
    execution_path = execution_path or _agent_info_early.get("execution_path", "claude-code-cli")
    mcp_format = "toml" if execution_path == "codex-cli" else "json"

    agent_info = _agent_info_early
    is_admin_only = agent_store.is_admin_only(agent_name)

    # Resolve the agent's visibility mode ONCE — the single owner of mount
    # scope/username, config visibility, available scopes, memory availability
    # and the chat-history owner. ``username`` passed here is the REAL human
    # (attribution); the resolver derives the agent-scope MOUNT username ("")
    # for Shared-only chats and service sessions. ``_scope_override`` carries a
    # continued task's stored scope.
    from core.session.visibility import resolve_visibility
    vis = resolve_visibility(
        agent_name,
        username=username or "",
        user_role=user_role or "",
        user_sub=creds_sub or "",
        scope_override=_scope_override,
    )

    # Resolve the execution target + its placement facts BEFORE building the
    # MCP config / prompt / skills / path_env below. Device-local MCPs
    # (computer / browser / app control) attach ONLY when the session runs on
    # a satellite, so every one of those builders needs to know whether this
    # session is remote and whether that machine has an interactive display.
    # (target_kind/target_label also feed the SecurityContext + AgentConfig.)
    if pinned_target:
        # RESUME pins to the chat/run's ORIGIN target — never
        # re-resolve. Re-resolving would silently fall back to local when the
        # bound machine is offline, losing the on-disk session/context. 'local'
        # always stays local; a machine_id must be reachable, else emit the
        # offline sentinel so the warmup handler raises the tailored offline
        # error (no fallback) — recovers automatically when the machine returns.
        if pinned_target == "local":
            resolved_target = ("local", None)
        else:
            from services.remote.remote_status import is_reachable as _is_reachable
            reachable = await asyncio.to_thread(_is_reachable, pinned_target)
            resolved_target = (
                (pinned_target, None) if reachable
                else (f"__offline__:{pinned_target}", "pinned-target-offline")
            )
    else:
        resolved_target = await asyncio.to_thread(
            remote_store.resolve_execution_target, agent_name, creds_sub, user_role,
        )
    target_value = resolved_target[0]
    target_kind, target_label = await asyncio.to_thread(
        remote_store.get_target_metadata, target_value, creds_sub, agent_name,
    )
    is_remote = target_kind in ("admin_remote", "user_remote")

    # For remote targets, look up the satellite's last-reported capabilities:
    #   - agents_dir / home_dir / os_user / user_dirs / allow_full_fs feed the
    #     path-policy gate + SecurityContext.
    #   - display.has_display gates requires_display device MCPs, read
    #     inline from the same caps dict (task/meeting/phone, which don't read
    #     caps, use remote_store.get_target_has_display instead).
    target_agents_dir = ""
    target_machine_id = ""
    target_home_dir = ""
    target_allow_full_fs = False
    target_os_user = ""
    target_claude_runtime_root = ""
    target_user_dirs: dict = {}
    target_has_display: bool | None = None
    target_device_grants: set = set()
    target_os = ""
    if is_remote and target_value:
        try:
            machine = await asyncio.to_thread(
                remote_store.get_remote_machine, target_value,
            )
            if machine:
                import json as _json
                caps_raw = machine.get("capabilities") or "{}"
                caps = _json.loads(caps_raw) if isinstance(caps_raw, str) else (caps_raw or {})
                target_agents_dir = caps.get("agents_dir", "") or ""
                target_machine_id = machine.get("id", "") or ""
                target_home_dir = caps.get("home_dir", "") or ""
                target_allow_full_fs = bool(machine.get("allow_full_fs") or False)
                target_os_user = caps.get("os_user", "") or ""
                target_os = str(caps.get("os", "") or "")
                target_claude_runtime_root = caps.get("claude_runtime_root", "") or ""
                target_user_dirs = caps.get("user_dirs", {}) or {}
                _display = caps.get("display")
                if isinstance(_display, dict) and "has_display" in _display:
                    target_has_display = bool(_display["has_display"])
                # device_grants is a top-level JSON-array column (NOT in caps);
                # parse it inline from the row we already read, via the store's
                # own parser so the format lives in one place.
                target_device_grants = remote_store._parse_device_grants(
                    machine.get("device_grants")
                )
        except Exception:
            # Best effort — empty target_agents_dir means the policy
            # will treat ALL satellite paths as "outside synced tree".
            # That combined with allow_full_fs=False (the
            # default) and no home_dir produces a fail-closed reject.
            target_agents_dir = ""
            target_machine_id = ""
            target_home_dir = ""
            target_allow_full_fs = False
            target_os_user = ""
            target_os = ""
            target_user_dirs = {}
            target_has_display = None
            target_device_grants = set()

    # Resolve per-user MCP credentials + config.
    # For TOML (Codex), credential env is injected into the TOML env sections
    # (Codex doesn't inherit parent env for MCP servers). We must rewrite
    # sandbox paths FIRST so the TOML gets the correct sandbox-internal paths.
    mcp_config, credential_env, excluded_mcps, secret_bundles, bash_env_keys = (
        await asyncio.to_thread(
            mcp_registry.build_session_mcp_config, agent_name, creds_sub,
            delegation_targets=resolved_targets,
            mcp_config_format=mcp_format,
            username=username or "",
            user_role=user_role or "",
            # Credentials follow the MOUNT scope: a Shared-only chat mounts the
            # AGENT scope, so its per-user MCPs resolve via the agent's bound
            # SERVICE account (knowledge/.credentials) — never the engaging
            # human's own accounts. Defaulting to "user" here resolved the
            # ACCOUNT as the human while every path surface (mounts, path_env,
            # writeback, satellite delivery) used the agent scope — the token
            # landed where the MCP never looked.
            task_scope=vis.mount_scope,
            interactive_local=is_otodock,
            is_remote=is_remote,
            target_has_display=target_has_display,
            target_device_grants=target_device_grants,
            target_admin_paired=(target_kind == "admin_remote"),
        )
    )

    # Inject manifest-declared path_env values. The framework auto-resolves
    # each role to a sandbox-style virtual path based on user/agent scope
    # AND access level (viewer/manager/admin) — so the same manifest entry
    # can yield different paths for different sessions of the same MCP.
    # Local sandbox: bwrap maps the virtual path to the real host path.
    # Remote satellite: the satellite's path translator rewrites it to
    # `{satellite_agent_dir}/...` before subprocess spawn. Multi-value
    # entries (allowlist-style env vars) are joined here and the satellite
    # is told via `multi_value_envs` to split-translate-rejoin.
    # Session-scoped roles emit the literal `{session_id}` token; both
    # translators expand it at process-spawn time. See path_roles.py.
    from services import path_roles
    multi_value_envs: dict[str, str] = {}
    if credential_env is None:
        credential_env = {}
    for manifest in (mcp_registry.get_agent_mcps(
        agent_name, is_remote=is_remote, target_has_display=target_has_display,
        target_device_grants=target_device_grants,
    ) or []):
        if not manifest.path_env:
            continue
        for env_var, decl in manifest.path_env.items():
            try:
                # MOUNT username — "" for Shared-only/service so MCP path_env
                # resolves to the agent scope, not the human's user dir.
                credential_env[env_var] = path_roles.resolve_path_env_entry(
                    decl, username=vis.mount_username, user_role=user_role or "",
                )
            except ValueError as e:
                logger.warning(
                    "path_env injection failed for %s.%s: %s",
                    manifest.name, env_var, e,
                )
                continue
            if decl.is_multi:
                multi_value_envs[env_var] = decl.join

    # Standard OTO_* env vars — same set the env_builder injects into the
    # parent process. We also bake them into credential_env so the Codex
    # TOML injector ships them inside each stdio MCP's [env] section
    # (Codex doesn't inherit parent env to MCPs). For CLI/Direct LLM stdio
    # MCPs they arrive via the parent env, but it's harmless to include
    # them here too — env_builder dedupes naturally on dict update order.
    from core.sandbox import oto_env
    credential_env.update(oto_env.build_oto_env(
        agent_name=agent_name,
        username=vis.mount_username,        # MOUNT username ("" for agent scope)
        user_sub=creds_sub or "",          # REAL sub — attribution
        user_role=user_role or "",
        session_id=session_id or "",
        memory_user_enabled=vis.memory_user_enabled,
        memory_agent_enabled=vis.memory_agent_enabled,
        default_scope=vis.effective_default_scope,
        task_type="",  # config_builder is for chat / non-task sessions
        available_scopes=vis.available_scopes,
        force_config=vis.config_visible,
    ))

    # Mint the session JWT with the REAL session identity (creds_sub) and bake
    # it into credential_env so it OVERRIDES env_builder's token. env_builder
    # derives the token's user_sub from the MOUNT username, which is "" for a
    # Shared-only human chat — that token would mis-attribute as a service
    # session and let a Shared-only viewer bypass the agent-memory role gate
    # (the memory API role check keys on the JWT's user). credential_env is
    # applied LAST in build_session_env, so this is the authoritative token for
    # the agent process AND every stdio MCP it spawns.
    from auth.session_token import create_session_token
    credential_env["PROXY_API_KEY"] = create_session_token(
        session_id or "", agent_name, creds_sub or "",
    )
    multi_value_envs.update(oto_env.OTO_MULTI_VALUE_ENVS)

    # For TOML format: regenerate with rewritten credential env injected.
    # bash-only env_injection (GH_TOKEN/GIT_CONFIG_*) is EXCLUDED from the file —
    # it stays in `credential_env` (→ the Codex daemon env, below) for bash, but
    # is never written to the config.toml on disk.
    if mcp_format == "toml" and mcp_config and credential_env:
        mcp_config = await asyncio.to_thread(
            mcp_registry.inject_credential_env_into_toml,
            mcp_config, credential_env,
            exclude_keys=bash_env_keys,
        )

    # Resolve dynamic MCP context (e.g., delegation targets with descriptions).
    # Async — builder blocks invoke remote MCP tools.
    # Chat sessions never carry a trigger_payload; ``${trigger.*}`` tokens
    # resolve empty and any trigger-gated blocks skip naturally.
    assigned_mcp_names = [m.name for m in (mcp_registry.get_agent_mcps(
        agent_name, is_remote=is_remote, target_has_display=target_has_display,
        target_device_grants=target_device_grants,
    ) or [])]
    dynamic_contexts = await dynamic_context.get_dynamic_contexts(
        agent_name, assigned_mcp_names,
        user_sub=creds_sub or "", user_role=user_role,
        delegation_targets=resolved_targets,
        # Pre-resolved off-loop: the delegation provider is no-I/O.
        delegation_roster=await asyncio.to_thread(
            dynamic_context.build_delegation_roster, resolved_targets,
        ),
        is_remote=is_remote,
        target_admin_paired=(target_kind == "admin_remote"),
        target_os=target_os,
    )

    # Build agent system prompt. MOUNT username drives the workspace tree +
    # user-context sections (agent-scope for Shared-only); the REAL human
    # identity is rendered by build_permission_context from the SecurityContext.
    agent_prompt = config.build_agent_prompt(
        agent_name, username=vis.mount_username, role=user_role,
        excluded_mcps=excluded_mcps or None,
        dynamic_contexts=dynamic_contexts or None,
        sandboxed=True,
        client_type=client_type or "",
        is_remote=is_remote,
        target_has_display=target_has_display,
        target_device_grants=target_device_grants,
        mount_shared=vis.mount_shared,
    )

    # Append client-specific context (dashboard adapter injects file display
    # instructions, task delivery format, etc.). This builder serves dashboard
    # human chats only; a Shared-only agent chatted here renders the agent-scope
    # blocks via the visibility resolver below.
    if client_type == "dashboard":
        from adapters.dashboard import DashboardAdapter
        adapter = DashboardAdapter()
        client_context = adapter.build_client_context(mcp_config)
        if client_context:
            agent_prompt = (
                agent_prompt + "\n\n" + client_context
                if agent_prompt else client_context
            )

    # Append permission context (tells agent its file access constraints).
    # otodock-CLI: on a dashboard RESUME of an otodock chat the caller doesn't
    # pass work_cwd — recover it from the persisted chat row so the session
    # re-spawns in the SAME folder (else it would default to the in-tree workspace
    # and Codex rollout / Claude project-hash resume would miss). Fresh otodock
    # starts pass work_cwd explicitly, so this read is skipped there.
    if not work_cwd and chat_id:
        from storage import database as _db_cb
        _chat_row = await asyncio.to_thread(_db_cb.get_chat, chat_id)
        if _chat_row:
            work_cwd = _chat_row.get("work_cwd", "") or ""
    # Admit the session's own arbitrary cwd subtree as a per-session allowed root.
    # work_cwd arrives already realpath-normalized from the otodock client (it runs
    # on the satellite, so it resolves its own $PWD). An under-home work_cwd is
    # harmless here — the home branch would admit it anyway; this just admits it
    # slightly earlier. Empty for every normal session.
    session_allowed_roots: tuple = (work_cwd,) if work_cwd else ()
    security_ctx = SecurityContext(
        role=user_role,
        username=username or "",
        agent=agent_name,
        is_admin_agent=is_admin_only,
        display_name=_display_name,
        email=_email,
        target_kind=target_kind,
        target_label=target_label,
        target_agents_dir=target_agents_dir,
        target_machine_id=target_machine_id,
        target_home_dir=target_home_dir,
        target_allow_full_fs=target_allow_full_fs,
        session_allowed_roots=session_allowed_roots,
        work_cwd=work_cwd or "",
        target_claude_runtime_root=target_claude_runtime_root,
        target_os_user=target_os_user,
        target_user_dirs=target_user_dirs,
        target_device_grants=target_device_grants,
        # Visibility-modes: mount scope (≠ the REAL username above), /config
        # gating, and the agent's mode scopes — drive the prompt's scope/folder
        # variants + the sandbox mount decouple.
        session_scope=vis.mount_scope,
        config_visible=vis.config_visible,
        available_scopes=vis.available_scopes,
    )
    perm_ctx = build_permission_context(
        security_ctx,
        assigned_mcp_names=tuple(assigned_mcp_names),
        execution_path=execution_path or "",
    )
    agent_prompt = agent_prompt + perm_ctx if agent_prompt else perm_ctx

    # Mount scope + username from the visibility resolver: handles service /
    # internal (→ agent), Shared-only (→ agent even with a human), Personal-only
    # (→ user), and a task re-warm's stored scope. The persistent .claude/.codex
    # dir lives under users/{u}/ (user) or workspace/ (agent) accordingly.
    scope = vis.mount_scope

    # Prepare the persistent config dir — .codex/ for Codex, .claude/ otherwise.
    # Single source of truth so the session-config builders can't drift (see
    # core.sandbox.sandbox.ensure_persistent_agent_dir).
    from core.sandbox.sandbox import ensure_persistent_agent_dir
    host_claude_dir = await asyncio.to_thread(
        ensure_persistent_agent_dir,
        agent_name,
        execution_path=execution_path,
        username=vis.mount_username,
        scope=scope,
    )

    # Resolve model and effort
    resolved_model = model or config.get_cli_model(agent_name)
    resolved_effort = config.get_cli_effort(agent_name)
    extra_env: dict[str, str] = {}
    subscription_id = ""

    try:
        subscription_id, sub_env = await asyncio.to_thread(
            subscription_pool.resolve_subscription_env,
            execution_path, creds_sub,
            model=resolved_model, agent_info=agent_info,
            # Scope-sticky: sessions sharing this credential dir must run on
            # ONE account (same key the layer stamps at bind_session).
            sticky_scope=subscription_pool.credential_scope_key(
                target_value, str(host_claude_dir)),
        )
        extra_env.update(sub_env)
        # Direct LLM needs caller identity for mid-session provider switching
        if execution_path == "direct-llm":
            extra_env["_USER_SUB"] = creds_sub or ""
    except subscription_pool.NoSubscriptionError:
        raise
    except Exception as e:
        logger.warning(f"Subscription pool error for {agent_name}: {e}")
    # User-scoped work with no resolved credentials → surface a clean, actionable
    # block (the warmup handler turns this into a dashboard message) instead of
    # letting the layer start with an empty key and fail cryptically mid-turn.
    # Agent-scoped work (creds_sub falsy) uses the platform pool and is left as-is.
    if creds_sub and not subscription_id:
        raise subscription_pool.NoSubscriptionError(
            subscription_pool.user_scope_block_reason(execution_path, creds_sub)
        )

    return AgentConfig(
        agent_name=agent_name,
        user_sub=user_sub or "",
        system_prompt=agent_prompt or "",
        mcp_config_path=str(mcp_config) if mcp_config else "",
        credential_env=credential_env or {},
        mcp_secret_bundles=secret_bundles or {},
        permission_mode=permission_mode,
        client_type=client_type,
        model=resolved_model,
        effort=resolved_effort,
        resume=resume,
        use_native_permissions=False,  # dashboard uses hook-based gating
        extra_env=extra_env,
        security_context=security_ctx,
        subscription_id=subscription_id,
        subscription_user_sub=creds_sub or "",
        sandbox_host_claude_dir=str(host_claude_dir),
        codex_thread_id=codex_thread_id,
        chat_id=chat_id,
        multi_value_envs=multi_value_envs,
        **_resolve_target_fields(resolved_target),
        execution_path=execution_path,
        default_execution_mode=(agent_info or {}).get("default_execution_mode", "") or "",
        work_cwd=work_cwd or "",
        term=term or "",
    )
