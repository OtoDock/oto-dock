"""Phone-call agent config builder.

Mirrors ``core/config/config_builder.py`` but tailored to phone (call) sessions: no
dashboard adapter, no user dirs (a call is agent-scope by definition), no
file-display context. Adds the call context (PhoneAdapter) + per-route
override before any dynamic_context blocks.

This is the entry point for ``trigger_payload`` enrichment on
the phone path. When a phone route declares ``trigger_slug``, the warmup
handler resolves the trigger row, builds a normalised payload, and threads
it through here to ``get_dynamic_contexts`` so manifest ``agent_context``
blocks resolve ``${trigger.*}`` tokens.

NOTE: ``client_type="phone"`` and the ``phone_mode`` flag below are the
session-type discriminator — they must stay in lockstep with the adapter
``PhoneAdapter.name`` and the manifests' ``exclude_from: ["phone"]``.
"""

import asyncio
import logging

import config
from storage import agent_store
from services.mcp import mcp_registry
from services.mcp import dynamic_context
from services.engines import subscription_pool
from auth.path_policy import SecurityContext, build_permission_context
from core.execution_layer import AgentConfig
from core.sandbox.sandbox import ensure_persistent_claude_dir

logger = logging.getLogger("claude-proxy")


def _resolve_phone_role(agent_name: str) -> str:
    """Phone session role: admin for admin-only agents, else the per-agent or
    platform-default phone role. Shared so the config builder and the phone WS
    handler resolve the execution target identically."""
    if agent_store.is_admin_only(agent_name):
        return "admin"
    return config.PHONE_AGENT_ROLES.get(agent_name, config.PHONE_DEFAULT_ROLE)


def resolve_phone_execution_target(agent_name: str) -> str:
    """Resolve where a phone session runs (agent default > local, offline
    fallback honored). A call is agent-scope, so there is no user override."""
    from storage import remote_store
    return remote_store.resolve_execution_target(
        agent_name, None, _resolve_phone_role(agent_name),
    )[0]


async def build_phone_agent_config(
    agent_name: str,
    *,
    call_type: str = "inbound",
    phone_context_override: str = "",
    phone_mode: bool = True,
    trigger_payload: dict | None = None,
) -> AgentConfig:
    """Build an ``AgentConfig`` for a phone (call) session.

    Phone sessions are always agent-scope (no per-user identity at the
    proxy boundary — the caller is identified by phone/DID inside the
    trigger payload, not by an authenticated user_sub).

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
    """
    is_admin_only = agent_store.is_admin_only(agent_name)
    phone_role = _resolve_phone_role(agent_name)
    agent_info = agent_store.get_agent(agent_name)

    # Resolve target metadata for the SecurityContext (drives the
    # # Execution Environment prompt block + admin-tier bash gating).
    # Phone sessions are agent-scope (no user_sub) — the agent-level
    # default applies.
    from storage import remote_store as _remote_store
    # Resolve via the shared helper so the SecurityContext metadata,
    # AgentConfig.execution_target, AND the phone WS handler's layer selection
    # all agree. get_target_metadata expects the RESOLVED target.
    phone_target_value = await asyncio.to_thread(
        resolve_phone_execution_target, agent_name,
    )
    phone_target_kind, phone_target_label = await asyncio.to_thread(
        _remote_store.get_target_metadata, phone_target_value, None, agent_name,
    )
    # Placement facts for device-local MCP filtering.
    # Computer-control is call-excluded via exclude_from, but other device
    # MCPs (browser / app) still need the gate threaded consistently.
    is_remote = phone_target_kind in ("admin_remote", "user_remote")
    target_has_display = await asyncio.to_thread(
        _remote_store.get_target_has_display, phone_target_kind, phone_target_value,
    )
    target_device_grants = await asyncio.to_thread(
        _remote_store.get_target_device_grants, phone_target_kind, phone_target_value,
    )
    # Satellite path-policy fields — without them the Pass-1 path gate
    # fail-closes every file access when the call runs on a remote target.
    target_path_policy = await asyncio.to_thread(
        _remote_store.get_target_path_policy, phone_target_kind, phone_target_value,
    )

    phone_security = SecurityContext(
        role=phone_role,
        username="",
        agent=agent_name,
        is_admin_agent=is_admin_only,
        target_kind=phone_target_kind,
        target_label=phone_target_label,
        target_agents_dir=target_path_policy["agents_dir"],
        target_machine_id=target_path_policy["machine_id"],
        target_home_dir=target_path_policy["home_dir"],
        target_allow_full_fs=target_path_policy["allow_full_fs"],
        target_claude_runtime_root=target_path_policy.get("claude_runtime_root", ""),
        target_os_user=target_path_policy["os_user"],
        target_user_dirs=target_path_policy["user_dirs"],
        target_device_grants=target_device_grants,
        # PINNED False: phone calls carry no schedule provenance — the
        # knowledge_rw opt-in is scheduler-fire-only.
        knowledge_rw=False,
    )

    # MCP config — phone mode filters out tools that don't apply mid-call.
    mcp_config_path, credential_env, excluded_mcps, secret_bundles, _ = await asyncio.to_thread(
        mcp_registry.build_session_mcp_config,
        agent_name, None, phone_mode=phone_mode,
        is_remote=is_remote, target_has_display=target_has_display,
        target_device_grants=target_device_grants,
    )

    # Dynamic context blocks — including builder blocks that read
    # ${trigger.*} tokens. A call is agent-scope so ``user_sub`` stays empty
    # and credential resolution falls back to bound service accounts.
    assigned_mcp_names = [m.name for m in (mcp_registry.get_agent_mcps(
        agent_name, is_remote=is_remote, target_has_display=target_has_display,
        target_device_grants=target_device_grants,
    ) or [])]
    dynamic_contexts = await dynamic_context.get_dynamic_contexts(
        agent_name, assigned_mcp_names,
        user_sub="",
        user_role=phone_role or "",
        trigger_payload=trigger_payload,
    )

    # Resolve execution path early — needed for permission-context layer
    # gating (Bash + plans dir mentions) and the subscription pool below.
    execution_path = (agent_info or {}).get("execution_path", "claude-code-cli")

    # Compose the system prompt:
    #   base agent prompt (incl. dynamic_contexts)
    #   + permission context (file/path constraints)
    #   + base call context (TTS-friendly response rules)
    #   + optional per-route override (extra instructions)
    agent_prompt = config.build_agent_prompt(
        agent_name,
        username=None,
        role=phone_role,
        excluded_mcps=excluded_mcps or None,
        dynamic_contexts=dynamic_contexts or None,
        sandboxed=True,
        client_type="phone",
        is_remote=is_remote,
        target_has_display=target_has_display,
        target_device_grants=target_device_grants,
    ) or ""
    agent_prompt += build_permission_context(
        phone_security,
        assigned_mcp_names=tuple(assigned_mcp_names),
        execution_path=execution_path or "",
    )

    from adapters.phone import PhoneAdapter
    agent_prompt += "\n\n" + PhoneAdapter.get_phone_context(call_type=call_type)
    if phone_context_override:
        agent_prompt += "\n" + phone_context_override

    # Persistent .claude/ dir lives at the agent-scope (not per-user).
    host_claude_dir = await asyncio.to_thread(
        ensure_persistent_claude_dir,
        agent_name,
        username="",
        scope="agent",
    )

    resolved_model = config.get_cli_model(agent_name)

    # Subscription pool — a call has no user identity, so the platform pool
    # answers. Surfaced via extra_env; provider-switching key (``_USER_SUB``)
    # left empty for Direct LLM (no per-user routing on phone calls).
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
    except Exception as e:
        logger.warning(f"Phone subscription acquisition error: {e}")

    # Resolve effort from agent's configured default. ``model`` was already
    # resolved above for the subscription pool call — it MUST also land on
    # the AgentConfig or the CLI/Codex/Direct-LLM layer sends an empty
    # ``--model`` and Anthropic returns 400 "model: String should have at
    # least 1 character". (The legacy inline warmup path had the
    # same latent bug but never surfaced because nobody had spoken to the
    # phone agent past the greeting.)
    resolved_effort = config.get_cli_effort(agent_name)

    return AgentConfig(
        agent_name=agent_name,
        system_prompt=agent_prompt,
        mcp_config_path=str(mcp_config_path) if mcp_config_path else "",
        permission_mode="auto",
        client_type="phone",
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
