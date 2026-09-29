"""Centralized agent config builder for session creation.

Consolidates the prompt/MCP/security/model config building that was
duplicated across dashboard warmup, pre-warmup, and plan implementation.
"""

import asyncio
import logging

import config
from storage.agents import agent_store
from storage import database as task_store
from core import placement
from storage import remote_store
from services.mcp import mcp_registry
from services.mcp import dynamic_context
from services.engines import subscription_pool
from auth.path_policy import SecurityContext, build_permission_context
from core.execution_layer import AgentConfig
from core.config.task_config_builder import TaskIdentity
from core.execution_layer import DEFAULT_EXECUTION_PATH
from core.session import session_kind
from auth import roles


def release_config_seat(session_id: str, cfg, *, keep_binding: bool = False) -> None:
    """Return the pool seat a built ``AgentConfig`` holds when the spawn is
    abandoned before the layer's ``start_session`` bound it — a skipped
    pre-warm, a slot denial, an offline target, a failed start. The seat was
    taken inside :func:`build_agent_config` (or a sibling builder); nothing
    else gives it back, and a seat leaked here is the "stale
    ``active_sessions``" an admin could not delete past. Same scope key the
    layers stamp at bind, so the acquire-window claim is dropped with it.

    A binding already under ``session_id`` predates this spawn — the layers
    never fail after binding, so it belongs to the dead session this spawn
    was re-warming and nothing else will release it: released here too,
    unless ``keep_binding`` says the id is a LIVE session's (a task round
    riding an already-warm session, a phone reconnect reusing its pre-warmed
    session). No-op without a subscription.
    """
    sub_id = getattr(cfg, "subscription_id", "") or ""
    if not sub_id:
        return
    if not keep_binding and subscription_pool.session_bound(session_id):
        subscription_pool.release_subscription(session_id)
    subscription_pool.release_unbound_seat(
        sub_id,
        subscription_pool.credential_scope_key(
            getattr(cfg, "execution_target", "") or placement.LOCAL,
            getattr(cfg, "sandbox_host_claude_dir", "") or "",
        ),
    )


def _resolve_target_fields(resolved: tuple[str, str | None]) -> dict:
    """Unpack resolve_execution_target tuple for AgentConfig spread.

    When the resolver returns the offline sentinel (hard fail because
    fallback is disabled and the target is unreachable), we leave
    execution_target set to the sentinel. The warmup handler is expected to
    detect it (``placement.is_offline_sentinel``) and emit an error event
    to the client rather than calling start_session.
    """
    target, reason = resolved
    return {
        "execution_target": target,
        "fallback_reason": reason,
    }


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
    resume_handle: str = "",
    chat_id: str = "",
    session_id: str = "",
    task_identity: TaskIdentity | None = None,
    pinned_target: str = "",
    work_cwd: str = "",
    is_otodock: bool = False,
    term: str = "",
    phone_mode: bool = False,
    trigger_payload: dict | None = None,
    subscription_pool_fallback: bool = False,
    external_claim: str = "",
) -> AgentConfig:
    """Build an AgentConfig from agent DB + user context.

    The phone ``user`` identity mode (a route tied to a platform user —
    ``services/phone/phone_identity.py``) reuses this builder so the call is
    that user's own session: ``phone_mode`` applies the call-only MCP
    exclusions, ``trigger_payload`` feeds the ``${trigger.*}`` context
    blocks, ``subscription_pool_fallback`` lets a user without a subscription
    ride the platform pool (today's phone behaviour), and ``external_claim``
    rides the session token so it dies at hangup and the call log can name
    the caller — the SecurityContext stays a USER principal.

    Centralizes prompt building, MCP config, credential resolution,
    security context, and model/effort resolution.

    Args:
        agent_name: Agent slug.
        user: User dict from DB (has username, display_name, email, role).
        user_sub: User sub (OAuth subject identifier).
        user_role: the effective per-agent role (auth/roles.EFFECTIVE_ROLES; acting_role_of on the WS side).
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
        # Platform role follows the task's creds user, not the viewer who
        # reopened it; empty for agent-scope tasks (no human identity).
        _platform_role = ""
        if task_identity.creds_user_sub:
            _creds_row = task_store.get_user(task_identity.creds_user_sub) or {}
            _platform_role = _creds_row.get("role") or ""
    else:
        username = user.get("username") or None
        creds_sub = user_sub
        _display_name = user.get("display_name", "")
        _email = user.get("email", "")
        _scope_override = None
        _platform_role = user.get("role") or ""

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
    elif roles.is_admin(user_role):
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
    execution_path = (execution_path
                      or _agent_info_early.get("execution_path")
                      or DEFAULT_EXECUTION_PATH)
    # The engine's descriptor answers the engine questions below; a stored
    # id no layer claims fails here, closed, with the registry's message.
    from core.session.session_manager import capabilities_for_path
    _caps = capabilities_for_path(execution_path)
    mcp_format = _caps.mcp_config_format

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
    # Below the editor tier nobody runs from the agent's own CLI state (a
    # Shared-only chat, a task re-warm, a phone call as that person):
    # refused here, before a pool seat is taken, with the person's message.
    from core.sandbox.session_config_dir import refuse_agent_state_below_editor
    refuse_agent_state_below_editor(vis.mount_scope, user_role or "")

    # Resolve the execution target + its placement facts BEFORE building the
    # MCP config / prompt / skills / path_env below. Device-local MCPs
    # (computer / browser / app control) attach ONLY when the session runs on
    # a satellite, so every one of those builders needs to know whether this
    # session is remote and whether that machine has an interactive display.
    # (The placement also feeds the SecurityContext.)
    if not _caps.runtime.supports_remote_execution:
        # An engine that cannot run on a satellite (Direct LLM: session_manager
        # routes it local, remote_session_start refuses it): neither a pin nor a
        # default machine may shape the placement prompt, the device-MCP set
        # or the security context. The resolver already answers local for a
        # direct-llm AGENT; this covers a chat whose execution_path override
        # picked the engine on an agent that defaults to another layer, and a
        # chat row that stored a machine pin before the fix (re-pinned local
        # on this warmup).
        if not placement.is_local(pinned_target):
            logger.info(
                "build_agent_config: %s pinned to %s runs on the Direct LLM "
                "engine — placed local", agent_name, pinned_target[:8],
            )
        resolved_target = (placement.LOCAL, None)
    elif pinned_target:
        # RESUME pins to the chat/run's ORIGIN target — never
        # re-resolve. Re-resolving would silently fall back to local when the
        # bound machine is offline, losing the on-disk session/context. 'local'
        # always stays local; a machine_id must be reachable, else emit the
        # offline sentinel so the warmup handler raises the tailored offline
        # error (no fallback) — recovers automatically when the machine returns.
        if placement.is_local(pinned_target):
            resolved_target = (placement.LOCAL, None)
        else:
            from services.remote.remote_status import is_reachable as _is_reachable
            reachable = await asyncio.to_thread(_is_reachable, pinned_target)
            resolved_target = (
                (pinned_target, None) if reachable
                else (placement.offline_sentinel(pinned_target), "pinned-target-offline")
            )
    else:
        resolved_target = await asyncio.to_thread(
            remote_store.resolve_execution_target, agent_name, creds_sub, user_role,
        )
    target_value = resolved_target[0]
    # The resolved placement — the kind (local / admin-paired / user-paired)
    # and the machine's facts (paths, OS, the pairing flag, the grants, the
    # display), from one read of its row. It feeds the SecurityContext, the
    # MCP placement / consent / display gates, the prompt and the dynamic
    # contexts below.
    target = await asyncio.to_thread(
        remote_store.placement_of, target_value, creds_sub, agent_name,
    )
    is_remote = target.is_remote
    # Browser-control mode + extension token: a targeted read (the token
    # column never rides the machine row nor the persisted context),
    # dedicated/no-token when local.
    target_browser = await asyncio.to_thread(
        remote_store.get_target_browser_settings, target,
    ) if is_remote else None

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
            phone_mode=phone_mode,
            placement=target,
            target_browser=target_browser,
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
    for manifest in (mcp_registry.get_agent_mcps(agent_name, placement=target) or []):
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
        platform_role=_platform_role,
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
        external=external_claim or "",
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
    assigned_mcp_names = [
        m.name for m in (mcp_registry.get_agent_mcps(agent_name, placement=target) or [])
        # Only the MCPs that actually attach describe themselves (the phone
        # exclusions drop e.g. display-mcp on a call).
        if m.name not in (excluded_mcps or {})
    ]
    dynamic_contexts = await dynamic_context.get_dynamic_contexts(
        agent_name, assigned_mcp_names,
        user_sub=creds_sub or "", user_role=user_role,
        trigger_payload=trigger_payload,
        delegation_targets=resolved_targets,
        # Pre-resolved off-loop: the delegation + meetings providers are no-I/O.
        delegation_roster=await asyncio.to_thread(
            dynamic_context.build_delegation_roster, resolved_targets,
        ),
        meetings_access=await asyncio.to_thread(
            dynamic_context.build_meetings_access, creds_sub or "", user_role or "",
        ),
        # Only a NO-USER session reads its delegation targets (the edge is
        # the read grant) — user-backed sessions follow the user (the
        # server enforces from the token either way; this is prompt).
        nouser_reads=not creds_sub,
        placement=target,
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
        placement=target,
        mount_shared=vis.mount_shared,
        execution_path=execution_path or "",
        # A phone session on an engine that keeps a call on stdio MCPs never
        # connects the sidecar HTTP MCPs (Direct LLM's direct_mcp_policy).
        skip_http_mcps=bool(phone_mode and not _caps.behaviour.phone_http_mcps),
    )

    # Append client-specific context (dashboard adapter injects file display
    # instructions, task delivery format, etc.). This builder serves dashboard
    # human chats only; a Shared-only agent chatted here renders the agent-scope
    # blocks via the visibility resolver below.
    if client_type == session_kind.DASHBOARD.name:
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
    # Attached knowledge libraries — baked into the ctx like the mounts
    # (attach/detach lands at the next session build). Fail-safe empty:
    # mirror writes then deny at the policy layer, nothing widens.
    try:
        from storage.knowledge import db_knowledge_libraries as _db_kl
        _kl_rows = await asyncio.to_thread(
            _db_kl.attachments_for_consumer, agent_name)
        _knowledge_libraries = tuple(
            (a["source_agent"], a["subdir"] or "", bool(a["writable"]))
            for a in _kl_rows)
    except Exception:
        _knowledge_libraries = ()
    security_ctx = SecurityContext(
        role=user_role,
        username=username or "",
        agent=agent_name,
        is_admin_agent=is_admin_only,
        display_name=_display_name,
        email=_email,
        placement=target,
        session_allowed_roots=session_allowed_roots,
        work_cwd=work_cwd or "",
        # Visibility-modes: mount scope (≠ the REAL username above), /config
        # gating, and the agent's mode scopes — drive the prompt's scope/folder
        # variants + the sandbox mount decouple.
        session_scope=vis.mount_scope,
        config_visible=vis.config_visible,
        available_scopes=vis.available_scopes,
        knowledge_libraries=_knowledge_libraries,
        # Manager-provenance knowledge writes ride ONLY a task re-warm's
        # resolved identity (the rebuild of a scheduled/one-time/trigger
        # fire). Human chat sessions stay on the bool(username)+role path.
        knowledge_rw=(task_identity.knowledge_rw
                      if task_identity is not None else False),
        # A phone route tied to this user: the claim rides the session token
        # (liveness + call-log audit); the principal stays the user.
        external_claim=external_claim or "",
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

    # Resolve model and effort. Layer-aware: on a session whose layer
    # differs from the agent's engine (task/delegate overrides, engine-
    # switched chats warming with model="") the agent-default model may be
    # foreign to the layer — never stamp it (the provider 400s every turn).
    resolved_model = model or config.get_cli_model(agent_name, layer=execution_path)
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
        if not subscription_pool_fallback:
            raise
    except Exception as e:
        logger.warning(f"Subscription pool error for {agent_name}: {e}")
    # A phone route tied to a user without a subscription of their own rides
    # the platform pool (today's phone behaviour) instead of failing the call.
    if creds_sub and not subscription_id and subscription_pool_fallback:
        try:
            subscription_id, sub_env = await asyncio.to_thread(
                subscription_pool.resolve_subscription_env,
                execution_path, None, model=resolved_model, agent_info=agent_info,
                sticky_scope=subscription_pool.credential_scope_key(
                    target_value, str(host_claude_dir)),
            )
            extra_env.update(sub_env)
            if execution_path == "direct-llm":
                extra_env["_USER_SUB"] = ""
            logger.info(
                f"Phone route user session for {agent_name}: no subscription for "
                f"the tied user — using the platform pool"
            )
        except subscription_pool.NoSubscriptionError:
            raise
        except Exception as e:
            logger.warning(f"Subscription pool fallback error for {agent_name}: {e}")
    # User-scoped work with no resolved credentials → surface a clean, actionable
    # block (the warmup handler turns this into a dashboard message) instead of
    # letting the layer start with an empty key and fail cryptically mid-turn.
    # Agent-scoped work (creds_sub falsy) uses the platform pool and is left as-is.
    if creds_sub and not subscription_id:
        raise subscription_pool.NoSubscriptionError(
            subscription_pool.user_scope_block_reason(execution_path, creds_sub)
        )

    # Prompt-size line (≈ tokens = chars / 4): the always-loaded skill text
    # is the bulk of it — this is the number the skills card/guide split
    # (Plan B) is measured against, on every engine.
    _prompt_chars = len(agent_prompt or "")
    logger.info(
        f"Prompt built for {agent_name}: client={client_type or '-'} "
        f"layer={execution_path or '-'} ≈{_prompt_chars // 4} tokens "
        f"({_prompt_chars} chars)"
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
        resume_handle=resume_handle,
        chat_id=chat_id,
        multi_value_envs=multi_value_envs,
        **_resolve_target_fields(resolved_target),
        execution_path=execution_path,
        default_execution_mode=(agent_info or {}).get("default_execution_mode", "") or "",
        work_cwd=work_cwd or "",
        term=term or "",
    )
