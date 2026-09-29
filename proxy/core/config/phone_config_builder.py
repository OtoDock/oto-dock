"""Phone-call agent config builder.

Mirrors ``core/config/config_builder.py`` but tailored to phone (call) sessions:
no dashboard adapter, no file-display context. Adds the call context
(PhoneAdapter) + per-route override before any dynamic_context blocks.

Who the caller IS comes from the route (``services/phone/phone_identity.py``):

- ``caller`` / ``shared`` — an EXTERNAL principal (a phone caller who is not
  a platform user): the agent-scope mount plus, with a caller tree, their
  own space at /caller; the shared dirs are read-only (every external
  principal is a viewer — ``phone_identity.EXTERNAL_ROUTE_ROLE``); no shared
  memory, no platform-management MCPs, no shell; the session token dies at
  hangup. Built here.
- ``user`` — the call is the tied platform user's own session: built by the
  dashboard builder (``config_builder.build_agent_config``) with the phone
  pass-throughs, then the call context is appended. The user's per-agent
  role governs, capped at manager for a phone line.

This is the entry point for ``trigger_payload`` enrichment on the phone
path. When a phone route declares ``trigger_slug``, the warmup handler
resolves the trigger row, builds a normalised payload, and threads it through
here to ``get_dynamic_contexts`` so manifest ``agent_context`` blocks resolve
``${trigger.*}`` tokens.

NOTE: ``client_type="phone"`` and the ``phone_mode`` flag below are the
session-type discriminator — they must stay in lockstep with the adapter
``PhoneAdapter.name`` and the manifests' ``exclude_from: ["phone"]``.
"""

import asyncio
import logging

import config
from storage.agents import agent_store
from services.mcp import mcp_registry
from services.mcp import dynamic_context
from services.engines import subscription_pool
from auth.path_policy import SecurityContext, build_permission_context
from core.execution_layer import AgentConfig
from core.sandbox.session_config_dir import ensure_persistent_agent_dir
from core.session import external_identity
from services.phone.phone_identity import EXTERNAL_ROUTE_ROLE, RouteIdentity
from core.execution_layer import DEFAULT_EXECUTION_PATH
from core.session import session_kind

logger = logging.getLogger("claude-proxy")


def resolve_phone_execution_target(
    agent_name: str, *, role: str = "", user_sub: str | None = None,
) -> str:
    """Resolve where a phone session runs (user override for a user-tied
    route > agent default > local, offline fallback honored). ``role`` is the
    session's role — viewer for external calls, the tied user's capped role
    for a user route (a viewer forces the local target on admin-paired
    machines, exactly like a viewer chat)."""
    from storage import remote_store
    return remote_store.resolve_execution_target(
        agent_name, user_sub, role or EXTERNAL_ROUTE_ROLE,
    )[0]


async def build_phone_agent_config(
    agent_name: str,
    *,
    call_type: str = "inbound",
    phone_context_override: str = "",
    phone_mode: bool = True,
    trigger_payload: dict | None = None,
    route_identity: RouteIdentity | None = None,
    session_id: str = "",
) -> AgentConfig:
    """Build an ``AgentConfig`` for a phone (call) session.

    Args:
        agent_name: agent slug servicing this call.
        call_type: ``"inbound"`` or ``"outbound"`` — drives which call
            context block (and TTS phrasing) the PhoneAdapter returns.
        phone_context_override: per-route extra instructions appended
            after the base call context (this is the ``phone_routes``
            ``phone_context_override`` column, name preserved).
        phone_mode: passed through to ``build_session_mcp_config`` for
            call-only MCP filtering (the ``"phone"`` client-type).
        trigger_payload: when the inbound route declares a
            ``trigger_slug``, the warmup handler resolves the trigger and
            assembles ``{source, route, phone, did, email, body}``. ``None``
            for routes without a trigger or for outbound calls.
        route_identity: the call's resolved identity
            (``phone_identity.resolve_route_identity``). ``None`` builds the
            id-less external principal in the shared space (a legacy caller
            with no route information).
        session_id: the session being warmed — the dashboard builder bakes
            it into ``OTO_SESSION_ID`` / the session token for a user route.
    """
    if route_identity is not None and route_identity.mode == "user":
        return await _build_user_route_config(
            agent_name, route_identity, call_type=call_type,
            phone_context_override=phone_context_override,
            trigger_payload=trigger_payload, session_id=session_id,
        )
    return await _build_external_config(
        agent_name, route_identity, call_type=call_type,
        phone_context_override=phone_context_override, phone_mode=phone_mode,
        trigger_payload=trigger_payload,
    )


def _phone_tail(prompt: str, *, call_type: str, phone_context_override: str) -> str:
    """The call context (TTS-friendly rules) + the per-route override."""
    from adapters.phone import PhoneAdapter
    prompt = (prompt or "") + "\n\n" + PhoneAdapter.get_phone_context(call_type=call_type)
    if phone_context_override:
        prompt += "\n" + phone_context_override
    return prompt


async def _build_user_route_config(
    agent_name: str,
    identity: RouteIdentity,
    *,
    call_type: str,
    phone_context_override: str,
    trigger_payload: dict | None,
    session_id: str,
) -> AgentConfig:
    """A route tied to a platform user: their own session, built by the
    dashboard builder, plus the call context."""
    from core.config.config_builder import build_agent_config
    user = identity.user or {}
    cfg = await build_agent_config(
        agent_name, user, user.get("sub", ""), identity.user_role,
        permission_mode="auto",
        client_type=session_kind.PHONE.name,
        session_id=session_id,
        phone_mode=True,
        trigger_payload=trigger_payload,
        subscription_pool_fallback=True,
        external_claim=identity.caller_claim,
    )
    cfg.system_prompt = _phone_tail(
        cfg.system_prompt, call_type=call_type,
        phone_context_override=phone_context_override,
    )
    return cfg


async def _build_external_config(
    agent_name: str,
    identity: RouteIdentity | None,
    *,
    call_type: str,
    phone_context_override: str,
    phone_mode: bool,
    trigger_payload: dict | None,
) -> AgentConfig:
    """An EXTERNAL principal — the caller is not a platform user. Always a
    viewer of the shared space, whatever a legacy route row says."""
    ident = (
        identity.external if identity is not None and identity.external is not None
        else external_identity.resolve(external_identity.PHONE, "", session_id="", shared=True)
    )
    role = EXTERNAL_ROUTE_ROLE
    is_admin_only = agent_store.is_admin_only(agent_name)
    agent_info = agent_store.get_agent(agent_name)
    agent_dir = config.get_agent_dir(agent_name)

    # Resolve target metadata for the SecurityContext (drives the
    # # Execution Environment prompt block + admin-tier bash gating). An
    # external call is agent-scope: the viewer role, no user override.
    from storage import remote_store as _remote_store
    phone_target_value = await asyncio.to_thread(
        resolve_phone_execution_target, agent_name, role=role,
    )
    # The resolved placement from one read of the machine row — the kind,
    # the path facts (without them the Pass-1 path gate fail-closes every
    # file access when the call runs on a remote target), the grants, the
    # display.
    target = await asyncio.to_thread(
        _remote_store.placement_of, phone_target_value, None, agent_name,
    )
    is_remote = target.is_remote
    target_browser = await asyncio.to_thread(
        _remote_store.get_target_browser_settings, target,
    )

    # The caller's own tree (/caller): only with a tree-bearing identity, and
    # only on a LOCAL target — satellites never sync externals/, so a remote
    # call keeps the agent-scope mount (caller memory still works: the
    # memory API keys on the token, proxy-side). Defensive today: an
    # external identity is a viewer and resolve_execution_target forces every
    # non-owner role to local, so a tree-bearing call never resolves remote.
    external_home = ""
    if ident.has_tree:
        if is_remote:
            logger.warning(
                f"Phone call on agent {agent_name}: caller tree not mounted on "
                f"remote target {target.label or phone_target_value} "
                "(externals/ never syncs) — agent-scope mount, caller memory "
                "stays proxy-side"
            )
        else:
            home = external_identity.external_home(agent_dir, ident)
            external_home = str(home) if home is not None else ""

    phone_security = SecurityContext(
        role=role,
        username="",
        agent=agent_name,
        is_admin_agent=is_admin_only,
        placement=target,
        session_scope="agent",
        config_visible=False,
        # Callers read /knowledge and never see /config.
        knowledge_rw=False,
        principal="external",
        external_channel=ident.channel,
        external_id=ident.id,
        external_home=external_home,
        external_ephemeral=ident.ephemeral,
        external_verified=ident.verified,
        external_claim=ident.claim,
    )

    # Resolve execution path early — it picks the MCP config format (Codex
    # reads TOML), gates the permission-context layer mentions (Bash +
    # plans dir) and selects the subscription pool below.
    execution_path = ((agent_info or {}).get("execution_path")
                      or DEFAULT_EXECUTION_PATH)
    from core.session.session_manager import capabilities_for_path
    _caps = capabilities_for_path(execution_path)
    mcp_format = _caps.mcp_config_format

    # MCP config — phone mode filters out tools that don't apply mid-call;
    # the external rule drops the platform-management MCPs.
    mcp_config_path, credential_env, excluded_mcps, secret_bundles, _ = await asyncio.to_thread(
        mcp_registry.build_session_mcp_config,
        agent_name, None, phone_mode=phone_mode,
        placement=target,
        target_browser=target_browser,
        external=True,
        mcp_config_format=mcp_format,
    )
    if mcp_format == "toml" and mcp_config_path and credential_env:
        # Codex spawns MCPs from the TOML env tables only (no daemon-env
        # inheritance): bake the resolved credential env in, as the dashboard
        # builder does (bash-only env_injection keys never reach the file).
        mcp_config_path = await asyncio.to_thread(
            mcp_registry.inject_credential_env_into_toml,
            mcp_config_path, credential_env,
            exclude_keys=mcp_registry.bash_only_env_keys(agent_name),
        )

    # Dynamic context blocks — including builder blocks that read
    # ${trigger.*} tokens. A call is agent-scope so ``user_sub`` stays empty
    # and credential resolution falls back to bound service accounts. Only
    # the MCPs that actually attach contribute blocks (and scope nouns in the
    # permission context): an excluded MCP must not describe itself.
    assigned_mcp_names = [
        m.name for m in (mcp_registry.get_agent_mcps(agent_name, placement=target) or [])
        if m.name not in (excluded_mcps or {})
    ]
    dynamic_contexts = await dynamic_context.get_dynamic_contexts(
        agent_name, assigned_mcp_names,
        user_sub="",
        user_role=role,
        trigger_payload=trigger_payload,
    )

    # Compose the system prompt:
    #   base agent prompt (incl. dynamic_contexts, caller memory + context)
    #   + permission context (identity / scope / folders / permissions)
    #   + base call context (TTS-friendly response rules)
    #   + optional per-route override (extra instructions)
    agent_prompt = config.build_agent_prompt(
        agent_name,
        username=None,
        role=role,
        excluded_mcps=excluded_mcps or None,
        dynamic_contexts=dynamic_contexts or None,
        sandboxed=True,
        client_type=session_kind.PHONE.name,
        placement=target,
        execution_path=execution_path or "",
        # An engine that keeps a call on stdio MCPs (Direct LLM's
        # direct_mcp_policy) never connects the sidecar HTTP MCPs — keep
        # their catalog rows and skills out of the prompt. The CLI engines
        # connect them on a call themselves, so the flag is per engine.
        skip_http_mcps=not _caps.behaviour.phone_http_mcps,
        external=True,
        external_home=external_home,
    ) or ""
    agent_prompt += build_permission_context(
        phone_security,
        assigned_mcp_names=tuple(assigned_mcp_names),
        execution_path=execution_path or "",
    )
    agent_prompt = _phone_tail(
        agent_prompt, call_type=call_type,
        phone_context_override=phone_context_override,
    )

    # Persistent CLI config dir: the caller's tree when there is one, else
    # the agent scope. ``no_shell`` writes the external tool denials into
    # the CLI settings (one of the three layers of that rule).
    host_claude_dir = await asyncio.to_thread(
        ensure_persistent_agent_dir,
        agent_name,
        execution_path=execution_path,
        username="",
        scope="agent",
        external_home=external_home or None,
        no_shell=True,
    )

    resolved_model = config.get_cli_model(agent_name)

    # Subscription pool — an external call has no user identity, so the
    # platform pool answers. Surfaced via extra_env; provider-switching key
    # (``_USER_SUB``) left empty for Direct LLM (no per-user routing on calls).
    extra_env: dict[str, str] = {}
    subscription_id = ""
    try:
        subscription_id, sub_env = await asyncio.to_thread(
            subscription_pool.resolve_subscription_env,
            execution_path, None,
            model=resolved_model,
            agent_info=agent_info,
        )
        extra_env.update(sub_env)
        if execution_path == "direct-llm":
            extra_env["_USER_SUB"] = ""
    except subscription_pool.NoSubscriptionError:
        # The pool refused the spawn (its subscription cap): the call must not
        # start on whatever credential file the agent dir still holds.
        raise
    except Exception as e:
        logger.warning(f"Phone subscription acquisition error: {e}")

    # ``model`` MUST land on the AgentConfig or the CLI/Codex/Direct-LLM
    # layer sends an empty ``--model`` (Anthropic 400s on it).
    resolved_effort = config.get_cli_effort(agent_name)

    return AgentConfig(
        agent_name=agent_name,
        system_prompt=agent_prompt,
        mcp_config_path=str(mcp_config_path) if mcp_config_path else "",
        permission_mode="auto",
        client_type=session_kind.PHONE.name,
        model=resolved_model,
        effort=resolved_effort,
        security_context=phone_security,
        sandbox_host_claude_dir=str(host_claude_dir),
        extra_env=extra_env,
        subscription_id=subscription_id,
        subscription_user_sub="",
        credential_env=credential_env or {},
        mcp_secret_bundles=secret_bundles or {},
        execution_target=phone_target_value,
        execution_path=execution_path,
    )
