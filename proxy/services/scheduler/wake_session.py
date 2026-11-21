"""The session a finished task's result wakes: what its config is built from (the
chat's pin, credentials, delegation targets, MCPs, the prompt), its spawn, and the
re-warm of an interactive chat the wake lands in.
"""

import asyncio
import contextlib
import functools
import logging
from pathlib import Path  # a string annotation's name
from typing import Any, cast

import config
from auth import roles
from auth.path_policy import SecurityContext  # a string annotation's name
from core import placement
from core.execution_layer import AgentConfig, ExecutionLayer
from core.session import session_kind
from core.session.visibility import VisibilityResolution  # a string annotation's name
from services.scheduler import interactive, wake_fresh
from services.scheduler.delivery_identity import (
    _standing_of,
    _wake_refused,
    _wake_visibility,
)
from services.scheduler.lanes import _run_echo_turn_pumped
from storage import database as task_store
from storage.remote_store import BrowserTargetSettings  # a string annotation's name

logger = logging.getLogger("otodock.scheduler")


def _chat_pin(chat: dict | None, user_sub: str | None, *, as_chat: bool = False) -> str:
    """Where a chat woken as a person runs when it is that person's own chat
    or a Shared-only chat: the chat row's own placement, as the dashboard
    resumes it (``pinned_target``), since the person's own resolution (their
    machine, the tier rule for an admin-paired one) may name a machine the
    chat never ran on. "" for every other delivery, which resolves from its
    person and role. ``as_chat``: a session built as the person's warm-up
    builds it (a key's turn) pins every chat the person drives the same way.
    Synchronous: call it on the DB executor."""
    from core.session.chat_rebuild import persons_chat
    from core.session.visibility import shared_chat_owner
    agent = (chat or {}).get("agent") or ""
    if agent and session_kind.is_task_chat_id((chat or {}).get("id") or ""):
        # A task run's chat runs where its row is pinned (where the run ran,
        # or where the agent's own move put it), whoever the wake runs as.
        pin = cast("dict", chat).get("execution_target") or ""
        if not pin:
            return ""
        if placement.is_local(pin):
            return placement.LOCAL
        from services.remote.remote_status import is_reachable
        return pin if is_reachable(pin) else placement.offline_sentinel(pin)
    if not (as_chat and chat and user_sub) and not persons_chat(chat, user_sub):
        return ""
    chat = cast("dict", chat)  # persons_chat read the agent from it
    if chat.get("user_sub") != shared_chat_owner(agent):
        # A chat with no recorded placement resolves from its person, as
        # the dashboard resumes it.
        pin = chat.get("execution_target") or ""
        if not pin:
            return ""
    else:
        pin = chat.get("execution_target") or placement.LOCAL
    if placement.is_local(pin):
        return placement.LOCAL
    from services.remote.remote_status import is_reachable
    return pin if is_reachable(pin) else placement.offline_sentinel(pin)


def _wake_credential_env(agent: str, session_id: str, *, user_sub: str | None, role: str,
                         vis: "VisibilityResolution", where: placement.PlacementCapabilities,
                         mcp_config: "Path | None", flat_env: dict[str, str] | None,
                         bash_env_keys: set, mcp_format: str | None, task_type: str = "",
                         ) -> "tuple[Path | None, dict[str, str], dict[str, str], int]":
    """The credential environment of a respawned session, as the chat builder
    (``core/config/config_builder.py``) assembles it: the MCP build's flat env,
    each manifest's ``path_env`` at the mount, the ``OTO_*`` set and a session
    token for the delivery's person (a Shared-only chat mounts no person, so
    the layers' own token would name nobody; the credential environment is
    applied last by every spawn path). A TOML config gets it injected (Codex
    stdio MCPs inherit nothing from the parent), the bash-only keys excluded.
    Returns ``(mcp_config, credential_env, multi_value_envs, minted_at)``, the
    last the token's ``iat`` (the session's floor). Synchronous: call it off
    the loop."""
    from auth.session_token import mint_session_env
    from core.sandbox import oto_env
    from services import path_roles
    from services.mcp import mcp_registry
    env = dict(flat_env or {})
    multi_value_envs: dict[str, str] = {}
    for manifest in (mcp_registry.get_agent_mcps(agent, placement=where) or []):
        for env_var, decl in (manifest.path_env or {}).items():
            try:
                env[env_var] = path_roles.resolve_path_env_entry(
                    decl, username=vis.mount_username, user_role=role or "",
                )
            except ValueError as e:
                logger.warning("path_env injection failed for %s.%s: %s",
                               manifest.name, env_var, e)
                continue
            if decl.is_multi:
                multi_value_envs[env_var] = decl.join
    platform_role = ((task_store.get_user(user_sub) or {}).get("role") or "") if user_sub else ""
    env.update(oto_env.build_oto_env(
        agent_name=agent,
        username=vis.mount_username,
        user_sub=user_sub or "",
        user_role=role or "",
        platform_role=platform_role,
        session_id=session_id,
        memory_user_enabled=vis.memory_user_enabled,
        memory_agent_enabled=vis.memory_agent_enabled,
        default_scope=vis.effective_default_scope,
        # A task kind's wake says so to its tools: the self-config tool
        # lists its two session tools alone there.
        task_type=task_type,
        available_scopes=vis.available_scopes,
        force_config=vis.config_visible,
    ))
    minted_at = mint_session_env(env, session_id, agent, user_sub or "", external=None, role=role)
    multi_value_envs.update(oto_env.OTO_MULTI_VALUE_ENVS)
    mcp_config = mcp_registry.inject_toml_credentials(
        mcp_config, env, mcp_format=mcp_format, exclude_keys=bash_env_keys,
    )
    return mcp_config, env, multi_value_envs, minted_at


def _wake_delegation_targets(agent: str, user_sub: str | None, role: str) -> list[str]:
    """The agents a respawned session may delegate to, as the chat builder
    resolves them: the agent's configured targets, those of a person's wake
    narrowed to the agents the person holds (a platform admin keeps them
    all), and the agent itself first. Synchronous: call it on the DB executor."""
    from storage.agents import agent_store
    targets = agent_store.get_delegation_targets(agent)
    if user_sub and not roles.is_admin(role) and not roles.is_admin(
            (task_store.get_user(user_sub) or {}).get("role")):
        held = set(task_store.get_user_agents(user_sub))
        targets = [t for t in targets if t in held]
    return targets if agent in targets else [agent] + targets


def _wake_mcp_build(
    agent: str, user_sub: str | None, *, username: str, role: str,
    vis: "VisibilityResolution", chat_id: str, where: placement.PlacementCapabilities,
    browser: "BrowserTargetSettings", mcp_format: str | None, session_id: str,
    targets: list[str], kind: str = "", personal_owner: str | None = None,
    account_choices: dict[str, str] | None = None, limits: dict | None = None,
) -> tuple:
    """The MCP build of a respawned session for the delivery's person, as the
    chat builder makes it (the delegation targets included): a task's set,
    or a dashboard chat's when ``kind`` says so (the resume after the
    agent's own move continues the chat as its person's turn would), with
    the owner filter of a personal machine when ``personal_owner`` is set
    (``config_builder.personal_owner_of``). Returns the build's five values. Synchronous: it reads agent files and platform
    settings, so call it off the loop."""
    from services.mcp import mcp_registry
    owner_kw: dict[str, Any] = {"personal_owner": personal_owner} if personal_owner is not None else {}
    if account_choices:
        owner_kw["account_choices"] = account_choices
    if limits:
        owner_kw["limits"] = limits
    # Nobody drives a respawned session: a dashboard-kind wake is not the
    # person's own chat for the MCPs that load only where a person is.
    return mcp_registry.build_session_mcp_config(
        agent, user_sub or None, task_mode=kind != session_kind.DASHBOARD.name,
        task_scope=vis.mount_scope, shared_chat=True,
        username=username or "", user_role=role or "", chat_id=chat_id or "",
        placement=where, target_browser=browser, mcp_config_format=mcp_format,
        session_id=session_id, delegation_targets=targets,
        **owner_kw,
    )


def _wake_prompt(
    agent: str, *, role: str, vis: "VisibilityResolution",
    where: placement.PlacementCapabilities, exec_path: str, security: "SecurityContext",
    excluded: dict, dynamic_contexts: list | None, kind: str = "", prompt_limits: tuple = (),
    session_id: str = "",
) -> str:
    """The system prompt of a respawned session, as the chat builder makes
    it: from the same visibility and role as the config (the mount's
    username, folders and user context, the role's view of the workspace),
    with the dynamic MCP context, followed by the identity, scope and folder
    sections of the session's security context. Synchronous: it reads agent
    files and platform settings, so call it off the loop."""
    from auth.path_policy import build_permission_context
    from services.mcp import mcp_registry
    prompt = config.build_agent_prompt(
        agent, username=vis.mount_username, role=role,
        excluded_mcps=excluded or None, dynamic_contexts=dynamic_contexts or None,
        client_type=(kind if kind == session_kind.DASHBOARD.name else session_kind.TASK.name),
        placement=where,
        mount_shared=vis.mount_shared, execution_path=exec_path or "",
        shared_chat=True,
        tool_limits=prompt_limits or None,
        session_id=session_id,
    )
    assigned = [m.name for m in (mcp_registry.get_agent_mcps(agent, placement=where) or [])
                if m.name not in excluded]
    return (prompt or "") + build_permission_context(
        security, assigned_mcp_names=tuple(assigned), execution_path=exec_path or "",
    )


# A respawned session's dynamic MCP context asks remote MCPs: bounded, so a
# slow one never holds the wake's admission slot.
_WAKE_CONTEXT_TIMEOUT_S = 30.0


async def _wake_dynamic_contexts(
    agent: str, excluded: dict, *, user_sub: str | None, role: str,
    targets: list[str], where: placement.PlacementCapabilities,
    account_choices: dict[str, str] | None = None,
    switchable: bool = False, exec_path: str = "", choice_scope: str = "user",
    kind: str = "",
) -> list | None:
    """The dynamic MCP context of a respawned session, as the chat builder
    asks for it (the attached MCPs, the delegation roster, the meetings
    access), with the accounts block: whose accounts the session runs on
    when they are a person's and, for a turn of a dashboard chat that
    chooses (``switchable``, ``choice_scope`` the chat's), the accounts it
    may switch between; None when it cannot be had in time."""
    from services.mcp import account_context, dynamic_context, mcp_registry
    assigned = [m.name for m in (await asyncio.to_thread(
        functools.partial(mcp_registry.get_agent_mcps, agent, placement=where)) or [])
                if m.name not in (excluded or {})]

    async def _contexts() -> list:
        out = await dynamic_context.get_dynamic_contexts(
            agent, assigned, user_sub=user_sub or "", user_role=role,
            trigger_payload=None, delegation_targets=targets,
            delegation_roster=await asyncio.to_thread(
                dynamic_context.build_delegation_roster, targets, user_sub or ""),
            meetings_access=await asyncio.to_thread(
                dynamic_context.build_meetings_access, user_sub or "", role or ""),
            nouser_reads=not user_sub, placement=where,
            **({"account_choices": account_choices} if account_choices else {}),
        )
        block = await asyncio.to_thread(functools.partial(
            account_context.account_lines, agent, assigned, user_sub=user_sub or "",
            account_choices=account_choices, scope=choice_scope if switchable else "",
            run=kind not in ("", session_kind.DASHBOARD.name)))
        if block:
            out = [*out, (account_context.AGENT_CONFIG_MCP, block)]
        return out

    try:
        return await asyncio.wait_for(_contexts(), _WAKE_CONTEXT_TIMEOUT_S)
    except Exception:  # noqa: BLE001  # the wake's MCP context is optional, the respawn goes on without it
        logger.warning("Wake respawn: the dynamic MCP context of agent '%s' was not built",
                       agent, exc_info=True)
        return None


def _wake_superseded(session_id: str, chat_id: str) -> bool:
    """Someone else is starting or using this chat's session: a live
    interactive session under the id, a live terminal or a warmup on the
    chat, a turn streaming on it, or another turn starter holding it (a
    claim on the queue, a key's turn), or its delete is under way. A
    background wake then spawns nothing
    (its wake is stored for the chat's next turn instead, and that turn's end
    replays it)."""
    from core.events import input_queue
    from core.events.stream_pump import _active_pumps
    from core.session import interactive_session, key_rows, warmup_registry

    live = interactive_session.get(session_id) if session_id else None
    if live is not None and live.alive:
        return True
    if chat_id:
        if interactive_session.find_live_for_chat(chat_id) is not None:
            return True
        if warmup_registry.get(chat_id) is not None:
            return True
        pump = _active_pumps.get(chat_id)
        if pump is not None and not pump.is_done:
            return True
        if (input_queue.is_claimed(chat_id) or input_queue.deleting(chat_id)
                or key_rows.taken_by_another(chat_id)):
            return True
    return False


def _release_wake_slot(session_id: str, chat_id: str) -> None:
    """A wake that reserved and gave up before its session lived frees the
    slot, unless a person took the chat over meanwhile: their warmup may
    have adopted the reservation by acquiring the same id, and its own
    lifecycle (or the reconciler) releases it then."""
    from core import concurrency
    if not _wake_superseded(session_id, chat_id):
        concurrency.release_unless_live(session_id)


async def _role_after_wait(agent: str, user_sub: str, role: str, target: str,
                           session_id: str, *, agent_state: bool = False,
                           pinned: bool = False, as_chat: bool = False) -> str | None:
    """The role a person's wake spawns at once it holds its slot (it may have
    waited minutes): their agent row now, or their effective role when the
    wake runs from the agent's own state (``agent_state``: a Shared-only
    chat). ``as_chat`` (a session built as the person's own warm-up builds
    it): the role the dashboard reads for them (``acting_role_of``). None
    when they no longer hold the agent (below the editor tier counts as gone
    from a Shared-only chat), or when the new role would place the session
    elsewhere (a ``pinned`` chat's placement is its own)."""
    from services.remote import remote_status
    from storage.pg import run_db
    standing, row = await run_db(_standing_of, user_sub, agent)
    if _wake_refused(standing, shared=agent_state):
        logger.info(
            "Delegate one-shot delivery dropped: %s no longer holds "
            "agent '%s': session=%s", user_sub[:8], agent, session_id[:8]
        )
        return None
    from auth.providers import acting_role_of
    if as_chat or not agent_state:
        # The role the dashboard reads for the person (a platform admin is
        # admin on every agent), as the team account's floor reads it.
        fresh = await run_db(acting_role_of, user_sub, agent)
    else:
        fresh = standing
    if fresh != role and not pinned:
        fresh_target, _ = await run_db(
            remote_status.resolve_execution_target, agent, user_sub, fresh)
        if fresh_target != target:
            logger.info(
                "Delegate one-shot delivery deferred: the role change moves session "
                "%s to another machine", session_id[:8]
            )
            return None
    return fresh


async def _chat_built_config(
    session_id: str, agent: str, *, user_sub: str | None, role: str, chat_id: str,
    chat_row: dict | None, exec_path: str, target: str, fresh: bool,
    permission_mode: str, client_type: str, compute: dict | None = None,
) -> "AgentConfig | None":
    """The config of a session built as the person's own warm-up of the chat
    builds it (``build_agent_config``): their tools, accounts and prompt as
    stored now, at ``role``, on the chat's own place and engine, a task
    run's chat in the run's stored identity. None when the person is gone or
    has no engine to run it on (nothing starts). The placement guard and the
    agent-state floor raise as they do for a warm-up. ``compute`` is the
    compute machine the delivery took the slot on: the build prefers it.
    The person pays, as their own warm-up would: a resume of their cut turn
    is theirs (``chat_rebuild.nobody_typed``)."""
    from core.config.config_builder import build_agent_config
    from core.config.task_config_builder import rewarm_task_identity
    from services.engines import subscription_pool
    from storage.pg import run_db
    user = await run_db(task_store.get_user, user_sub) if user_sub else None
    if not user:
        logger.warning("Session rebuild skipped: no person to build it for "
                       "(agent=%s, session=%s)", agent, session_id[:8])
        return None
    task_identity = (await run_db(rewarm_task_identity, chat_id, agent)
                     if session_kind.is_task_chat_id(chat_id) else None)
    try:
        return await build_agent_config(
            agent_name=agent, user=user, user_sub=cast("str", user_sub), user_role=role,
            permission_mode=permission_mode, client_type=client_type, resume=not fresh,
            model=(chat_row or {}).get("model") or "", execution_path=exec_path,
            resume_handle="" if fresh else (chat_row or {}).get("codex_thread_id") or "",
            chat_id=chat_id, session_id=session_id, task_identity=task_identity,
            # The place the rung resolved: the layer and the config agree.
            pinned_target=target, compute_prefer=str((compute or {}).get("id") or ""),
        )
    except subscription_pool.NoSubscriptionError as e:
        logger.warning("Session rebuild skipped: no credentials (agent=%s, session=%s): %s",
                       agent, session_id[:8], e)
        return None


async def _wake_built_config(
    session_id: str, agent: str, *, user_sub: str | None, role: str, chat_id: str,
    chat_row: dict | None, exec_path: str, target: str, username: str, fresh: bool,
    permission_mode: str, client_type: str, woken: bool, compute: dict | None = None,
) -> "AgentConfig | None":
    """The config of a session a delivery starts for whoever it names: the
    person on the delivery (their own tree on a personal chat, the agent
    scope on a Shared-only one) or nobody at ``role``. None when no
    credentials can pay for it (nothing starts)."""
    from core.config.task_config_builder import build_delivery_security_context
    from services.remote import machine_away
    from storage import remote_store
    from storage.pg import run_db
    # The placement of this (possibly remote) one-shot. `target` is already
    # resolved above (local | machine_id; offline returned earlier); the
    # object threads the display and the grants so a remote agent's device
    # MCPs aren't dropped from the resume config + prompt.
    try:
        _os_placement = await machine_away.settled_placement(target, user_sub, agent, compute=compute)
    except machine_away.MachineAway as e:
        # Still in its update past the wait: kept for later, as for a
        # machine that is offline.
        logger.info("Session rebuild skipped: %s (agent=%s, session=%s)", e, agent, session_id[:8])
        return None
    _os_browser = await run_db(remote_store.get_target_browser_settings, _os_placement)
    # Who the session is: the person on the delivery (their own tree on a
    # personal chat, the agent scope on a Shared-only one) or nobody.
    vis = await run_db(_wake_visibility, agent, username, role, user_sub)
    # Close/reap dropped the session's security context (JWT-replay defense):
    # rebuilt for the resumed turn, or every hook fail-closes with "Session
    # is no longer active" and the callback runs tool-dead. The prompt's
    # identity sections are rendered from it.
    # The agent's tool limits for the session's kind, and a task kind's own
    # limit on the self-config tool (``tool_limits``).
    from services.mcp import mcp_registry, tool_limits
    task_kind = client_type != session_kind.DASHBOARD.name
    limits = await run_db(functools.partial(
        tool_limits.load, agent, tool_limits.governs_of(external=False, person=user_sub)))
    # In the engine's own format (Codex reads TOML), as every config builder
    # does, for the delivery's person at the session's mount scope: at agent
    # scope the MCP accounts are the agent's service bindings, at user scope
    # the person's own. All five values are kept: the credential environment
    # and the per-MCP secret bundles reach the session through its config.
    from core.session.session_manager import capabilities_for_path
    _mcp_format = capabilities_for_path(exec_path).mcp_config_format
    targets = await run_db(_wake_delegation_targets, agent, user_sub, role)
    # The resume of a shared chat the agent moved to its person's computer
    # carries what is that person's there, as the chat's own turn does.
    from core.config.config_builder import personal_machine_bundles, personal_owner_of
    from services.mcp import account_context
    _agents_accounts = (await run_db(functools.partial(
        account_context.credential_identity, agent, user_sub,
        mount_scope=vis.mount_scope, role=role or ""))).agents
    _owner = personal_owner_of(_os_placement, _agents_accounts, user_sub, _os_browser)
    _on_personal = _os_placement.user_paired and _agents_accounts
    # The chat's own choice of account per tool: a person's own chat's in
    # their own tree, a chat on the agent's accounts among the agent's own,
    # none for any other chat (``account_context.choice_scope``).
    # A run's wake reads the run's own choice (``chat_reload.schedule_for_run``).
    run_chat = client_type == session_kind.TASK.name
    choice_scope = ""
    if chat_id and (user_sub or not vis.mount_username):
        choice_scope = await run_db(functools.partial(
            account_context.choice_scope, chat_id, mount_scope=vis.mount_scope, role=role or "",
            task=run_chat))
    choices: dict[str, str] = {}
    if choice_scope:
        choices = await run_db(functools.partial(
            account_context.session_choices, chat_id, user_sub or "",
            mount_scope=vis.mount_scope, role=role or "", task=run_chat))
    mcp_config, flat_env, excluded, secret_bundles, bash_env_keys = await asyncio.to_thread(
        functools.partial(
            _wake_mcp_build, agent, user_sub, username=username, role=role, vis=vis,
            chat_id=chat_id, where=_os_placement, browser=_os_browser,
            mcp_format=_mcp_format, session_id=session_id, targets=targets, kind=client_type,
            personal_owner=_owner if _on_personal else None,
            account_choices=choices, limits=limits,
        ))
    secret_bundles = personal_machine_bundles(secret_bundles or {}, _os_placement,
                                              _agents_accounts, agent, who="wake", owner=_owner)
    limit_pairs = manager_pairs = tool_limits.grants(limits, [
        m for m in (await run_db(mcp_registry.get_agent_mcps, agent, placement=_os_placement) or [])
        if m.name not in (excluded or {})])
    if task_kind:
        limit_pairs = tool_limits.merged(limit_pairs, tool_limits.TASK_SESSION_LIMIT)
    # After the MCP build: the context carries the limits of the servers
    # that attached.
    oneshot_security = await build_delivery_security_context(
        agent, user_sub=user_sub, role=role, target=target, compute=compute or None,
        tool_limits=limit_pairs,
    )
    contexts = await _wake_dynamic_contexts(
        agent, excluded or {}, user_sub=user_sub, role=role, targets=targets,
        where=_os_placement, account_choices=choices,
        # The switch is offered where the chat, or the run, chooses.
        switchable=bool(choice_scope),
        exec_path=exec_path, choice_scope=choice_scope, kind=client_type)
    agent_prompt = await asyncio.to_thread(functools.partial(
        _wake_prompt, agent, role=role, vis=vis, where=_os_placement, exec_path=exec_path,
        security=oneshot_security, excluded=excluded or {}, dynamic_contexts=contexts,
        kind=client_type, prompt_limits=manager_pairs, session_id=session_id,
    ))
    mcp_config, credential_env, multi_value_envs, token_minted_at = await asyncio.to_thread(
        functools.partial(
            _wake_credential_env, agent, session_id,
            user_sub=user_sub, role=role, vis=vis, where=_os_placement,
            mcp_config=mcp_config, flat_env=flat_env,
            bash_env_keys=bash_env_keys, mcp_format=_mcp_format,
            task_type=session_kind.TASK.name if task_kind else "",
        ),
    )
    # The SAME persistent config dir the session was built with (its mount:
    # the person's tree for a personal chat, else agent/workspace), on every
    # machine, as the chat builder resolves it: a compute machine's spawn is
    # prepared there and --resume finds its conversation there, and on a
    # paired computer (whose satellite owns its own copy of the config) the
    # folder still names the session's account key and its home.
    from core.sandbox.sandbox import ensure_persistent_agent_dir
    from core.sandbox.session_config_dir import session_home_for
    oneshot_claude_dir = str(await asyncio.to_thread(
        ensure_persistent_agent_dir, agent,
        execution_path=exec_path,
        username=vis.mount_username,
        scope=vis.mount_scope,
    ))
    home = session_home_for(oneshot_claude_dir, session_id, execution_path=exec_path, ctx=oneshot_security)
    # The CHAT's pinned model: an empty model falls back to the AGENT
    # default, which belongs to the agent's default LAYER; on a chat
    # pinned to the other CLI that yields an unusable/empty model
    # ("API Error: 400 model: String should have at least 1 character").
    _os_model = (chat_row or {}).get("model") or ""
    # The resumed session draws on a subscription like every other spawn,
    # through the one owner (``core/config/spender.py``): a lane's chat pays
    # as its newest round, a task chat as its run, any other chat as the
    # person the delivery names (the sender pays on a Shared-only chat too,
    # as their own turn there does), the platform pool with nobody. The
    # layer binds the seat at start and the session's close releases it;
    # every exit of the spawn that starts nothing gives it back. Without
    # usable credentials nothing spawns (the wake is stored for the chat's
    # next turn, which fails visibly if it has none either).
    from core.config import spender
    from services.engines import subscription_pool
    from storage.agents import agent_store
    payer = await run_db(functools.partial(
        spender.chat_spender, chat_row or {"id": chat_id, "agent": agent}, user_sub or None, woken=woken))
    try:
        spent = await asyncio.to_thread(functools.partial(
            spender.spend_env, payer, exec_path,
            model=_os_model, agent_info=await run_db(agent_store.get_agent, agent),
            sticky_scope=subscription_pool.scope_key_for(target, oneshot_claude_dir, home),
            agent=agent))
    except subscription_pool.NoSubscriptionError as e:
        logger.warning(
            "Delegate one-shot delivery skipped: no credentials for the wake "
            "(agent=%s, session=%s): %s", agent, session_id[:8], e
        )
        return None
    subscription_id, sub_env = spent.subscription_id, spent.env
    return AgentConfig(
        agent_name=agent,
        user_sub=user_sub or "",
        system_prompt=agent_prompt or "",
        mcp_config_path=str(mcp_config) if mcp_config else "",
        credential_env=credential_env,
        mcp_secret_bundles=secret_bundles or {},
        excluded_mcps=frozenset(excluded or ()),
        multi_value_envs=multi_value_envs,
        permission_mode=permission_mode,
        client_type=client_type,
        resume=not fresh,
        security_context=oneshot_security,
        execution_target=target,
        execution_path=exec_path,
        sandbox_host_claude_dir=oneshot_claude_dir,
        session_home=home,
        model=_os_model,
        # The chat's own thread: without it a Codex resume starts a new one
        # (Claude resumes by the session id and stores none).
        resume_handle="" if fresh else (chat_row or {}).get("codex_thread_id") or "",
        token_minted_at=token_minted_at,
        extra_env=sub_env,
        subscription_id=subscription_id,
        subscription_user_sub=spent.bound_sub,
        account_choices=choices,
    )


async def _spawn_wake_session(
    layer: "ExecutionLayer", session_id: str, agent: str, result_prompt: str, *,
    user_sub: str | None, role: str, chat_id: str, chat_row: dict | None,
    exec_path: str, target: str, username: str, fresh_from: str = "", fresh: bool = False,
    permission_mode: str, client_type: str, build: str, compute: dict | None = None,
    driven_by: str = "",
) -> tuple[bool, str | None]:
    """Build the resumed session's config and start it (headless, or the
    chat's own terminal). Returns whether a session was started and the rung's
    answer; after a start the session's lifecycle owns the slot. With
    ``fresh`` the session starts fresh under ``session_id`` and runs the wake
    through the fresh-spawn rung (``wake_fresh``): ``fresh_from`` the chat's
    session that left no conversation, or empty for a chat that had none.
    ``permission_mode``, ``client_type`` and ``build`` as
    ``_deliver_via_oneshot`` takes them, with no default
    (``chat_rebuild.CHAT_OWN_MODE``: the chat's mode read now): ``build`` picks the
    builder (``core/session/chat_rebuild.py``), and everything after the
    config is the same for both."""
    from core.config.config_builder import release_config_seat
    from core.events.common_events import TEXT
    from core.session import chat_rebuild
    from storage.pg import run_db
    if permission_mode == chat_rebuild.CHAT_OWN_MODE:
        # A wake of a person's chat runs in the chat's mode as it is now,
        # after the slot's wait: a mode lowered meanwhile is the one taken.
        fresh_row = await run_db(task_store.get_chat, chat_id) if chat_id else None
        if not fresh_row:
            return False, None
        chat_row = fresh_row
        permission_mode = await run_db(chat_rebuild.own_mode, fresh_row)
    if build == chat_rebuild.AS_CHAT:
        oneshot_cfg = await _chat_built_config(
            session_id, agent, user_sub=user_sub, role=role, chat_id=chat_id,
            chat_row=chat_row, exec_path=exec_path, target=target, fresh=fresh,
            permission_mode=permission_mode, client_type=client_type,
            **({"compute": compute} if compute else {}))
    else:
        # Who pays reads the gate's own rule: a key's message is its
        # person's, anything else built as a wake is nobody's typing, which
        # a lane's chat pays as its newest round.
        oneshot_cfg = await _wake_built_config(
            session_id, agent, user_sub=user_sub, role=role, chat_id=chat_id,
            chat_row=chat_row, exec_path=exec_path, target=target, username=username,
            fresh=fresh, permission_mode=permission_mode, client_type=client_type,
            woken=chat_rebuild.nobody_typed(driven_by, build),
            **({"compute": compute} if compute else {}))
    if oneshot_cfg is None:
        return False, None
    # A chat pinned interactive gets its wake in ITS OWN MODE: re-warm the
    # terminal session, inject the result, await the orchestrator's turn,
    # close (unless a viewer attached meanwhile). The headless echo below
    # would run the wake invisibly outside the terminal the chat lives in
    # (previously: "interactive resume/delegation is future work"). On any
    # failure return None — the caller's path="none" stores the wake for
    # durable replay at the chat's next warmup.
    from core import execution_mode as _exec_mode
    # Until the layer binds the seat at start (or the re-warm takes it over),
    # every exit gives it back: a superseded wake, a raise, a cancel.
    handed_over = False
    try:
        if _wake_superseded(session_id, chat_id):
            logger.info(
                "Delegate one-shot delivery deferred (superseded): session=%s, "
                "chat=%s",
                session_id[:8], chat_id[:8] if chat_id else '-'
            )
            return False, None
        interactive = bool(chat_row) and await run_db(functools.partial(
            _exec_mode.is_interactive, chat_override=cast("dict", chat_row).get("execution_mode") or None,
        ))
        if interactive and fresh:
            return False, None   # a terminal chat's fresh start is its next warmup's
        if interactive:
            # The re-warm owns the seat from here: a failed one closes any
            # terminal it started (the close releases) and returns the seat
            # of one it never started.
            handed_over = True
            out = await _rewarm_interactive_and_wake(
                layer, session_id, agent, result_prompt,
                chat_id=chat_id, base_cfg=oneshot_cfg, chat_row=cast("dict", chat_row),  # interactive needs a row
                ends_with_turn=_ends_with_its_turn(build, chat_row),
            )
            return out is not None, out
        await layer.start_session(session_id, oneshot_cfg)
        handed_over = True
        if build == chat_rebuild.AS_CHAT and user_sub:
            # The per-turn time line reads the session's zone, and no
            # client is here to send one: the person's last known.
            from core.session.session_state import get_user_tz, set_session_user_tz
            zone = get_user_tz(user_sub)
            if zone:
                set_session_user_tz(session_id, zone)
    finally:
        if not handed_over:
            release_config_seat(session_id, oneshot_cfg)
    if fresh or chat_id:
        try:
            if fresh:
                answer = await wake_fresh.run_echo(
                    layer, session_id, fresh_from, chat_id, agent, result_prompt,
                    person=user_sub or "", source=driven_by)
            else:
                answer = await _run_echo_turn_pumped(
                    layer, session_id, chat_id, agent, result_prompt, person=user_sub or "",
                    source=driven_by,
                )
        finally:
            # Background work the turn started keeps the session, as a chat's
            # own turn keeps it: the reaper spares it while the work runs.
            from core.session import background_leash
            if (_ends_with_its_turn(build, chat_row)
                    and not background_leash.background_pending_count(session_id)):
                await layer.close_session(session_id)
        return True, answer
    parts: list[str] = []
    async with layer.session_lock(session_id):
        async for event in layer.send_message(session_id, result_prompt):
            if event.type == TEXT:
                content = event.data.get("content", "")
                if content:
                    parts.append(content)
    return True, "".join(parts) if parts else ""


def _woken_turn_over(isess) -> bool:
    """The woken turn ended by the terminal's own facts: its turn is closed
    and no question parks it. A turn that ends inside the minimum turn age
    never calls back (``_maybe_fire_turn_complete``), so the wait reads this."""
    return not isess.turn_open and not isess.question_parked


async def _end_woken_terminal(session_id: str, isess) -> None:
    """Close a terminal a wake built for nobody, unless the work its turn
    started runs on: the reaper spares it then, as for a chat's own turn, and
    it stays read-only to everyone (``runs_for_nobody``)."""
    from core.session import interactive_registry, interactive_session
    if interactive_registry.background_reported(isess):
        logger.info("Delegate wake turn complete: session=%s keeps its background work, "
                    "left to the idle reaper", session_id[:8])
        return
    await interactive_session.close_session(session_id, reason="wake_turn_done")


def _ends_with_its_turn(build: str, chat_row: dict | None) -> bool:
    """A session a wake builds the unattended way on the agent's shared chat,
    which people open: it closes when its wake's turn ends, so a person's
    next message warms their own session."""
    from core.session import chat_rebuild
    from core.session.visibility import is_shared_chat_owner
    return build == chat_rebuild.AS_WAKE and is_shared_chat_owner((chat_row or {}).get("user_sub"))


def _arm_chat_queue(chat_id: str) -> None:
    from core.events import input_queue
    if input_queue.peek(chat_id):
        input_queue.arm(chat_id)


# Turn-open verification window for interactive wakes: covers CLI spawn →
# readiness gate → paste flush → one transcript-tail poll of lag. Two windows
# run back to back (initial submit, then the bare-Enter nudge).
_WAKE_TURN_OPEN_S = 45.0


async def _wait_turn_open(isess, timeout: float) -> bool:
    """Poll until the session's transcript-derived turn-open flag flips."""
    waited = 0.0
    while waited < timeout:
        if isess.turn_open:
            return True
        if not isess.alive:
            return False
        await asyncio.sleep(1.0)
        waited += 1.0
    return bool(isess.turn_open)


#: How long a woken terminal's turn waits on the question its engine asked
#: on screen (the hook handed the call to it, nothing ran since) before its
#: person is told.
_WAKE_QUIET_S = 60.0
#: How often the re-warm looks at its woken turn.
_WAKE_POLL_S = 5.0


def _waits_on_person(isess) -> bool:
    """A woken terminal's turn that stopped at the engine's own question:
    open, nobody viewing, the question its last hook and ``_WAKE_QUIET_S``
    old. The screen is no signal: the engine redraws it while it asks."""
    import time

    from core.session.session_state import terminal_ask_since
    if not isess.turn_open or isess.has_viewer:
        return False
    asked = terminal_ask_since(isess.session_id)
    return bool(asked) and time.monotonic() - asked >= _WAKE_QUIET_S


async def _rewarm_interactive_and_wake(
    layer, session_id: str, agent: str, result_prompt: str,
    *, chat_id: str, base_cfg, chat_row: dict, ends_with_turn: bool = False,
) -> str | None:
    """Re-warm a DEAD interactive-pinned chat and inject the delegate wake.

    The interactive sibling of the headless one-shot: resume the chat's
    session as a PTY (``interactive=True``, chat-bound, the chat's own
    permission mode + baked TUI theme), submit the wake with the settle
    Enter, VERIFY the turn opened (transcript-derived — an unverified submit
    silently loses the payload), await the orchestrator's continuation turn
    (``interactive._run_interactive_task`` shape: turn-end callback + liveness poll + max
    backstop), then leave the terminal to the idle reaper so a user opening
    the chat shortly after still attaches to it. The chat-bound tailer
    persists the whole exchange to chat history as it runs.

    A ``warmup_registry`` entry brackets the spawn so a racing delivery's
    ``_oneshot_blocked`` defers to this re-warm and a dashboard opening the
    chat mid-wake attaches instead of double-spawning.

    Returns "" on success (the tailer persisted the turn — no echo row to
    save) or None on any failure (the caller stores the wake for durable
    replay)."""
    from core.config.config_builder import release_config_seat
    from core.session import interactive_session, warmup_registry

    cfg = base_cfg
    cfg.interactive = True
    # The chat binding is LOAD-BEARING: the InteractiveSession registers with
    # cfg.chat_id, and transcript tailing → chat_messages, the turn-open /
    # turn-complete signals, the streaming-status broadcasts AND the
    # dashboard's pty_attach chat guard all gate on it. Without it the wake
    # turn runs invisibly and unpersisted, and an open chat refuses to attach
    # to the live terminal (the original wake-recovery shipped that way).
    cfg.chat_id = chat_id
    # The wake turn keeps base_cfg's mode: the chat's own on a person's chat
    # (a step its mode asks about waits on the terminal's question, and the
    # person is told once, below), the unattended word on any other.
    cfg.interactive_theme = chat_row.get("tui_theme") or ""

    registered = False
    spawned = False
    try:
        try:
            await warmup_registry.register(chat_id, cfg.user_sub or "", agent)
            registered = True
        except RuntimeError:
            pass  # registry is an optimization, never a spawn blocker

        await layer.start_session(session_id, cfg)
        spawned = True
        isess = interactive_session.get(session_id)
        if isess is None:
            logger.warning(
                "Delegate interactive re-warm: no PTY registered for "
                "session=%s (chat=%s)", session_id[:8], chat_id[:8]
            )
            return None

        done = asyncio.Event()
        isess.on_turn_complete = lambda _msg: done.set()
        isess.chat_terminal_rewarmed = True
        # Cold wake prompt: buffered behind the readiness gate, flushed with
        # the settle Enter (large pastes were losing the fixed-gap Enter). Its
        # row is the ladder's (the result's or the wake's event row), so the
        # text is noted first and the transcript tailer writes no person's
        # row for it; a write that did not happen takes the note back.
        from core.session.transcript_tool_events import (
            drop_sent_prompt,
            note_sent_prompt,
        )
        note_sent_prompt(chat_id, result_prompt, owner=session_id)
        if not isess.submit_prompt(result_prompt, settle=True):
            drop_sent_prompt(chat_id, result_prompt, owner=session_id)
        logger.info(
            "Delegate wake re-warmed interactive: session=%s, "
            "chat=%s, agent=%s", session_id[:8], chat_id[:8], agent
        )
        if registered:
            # Spawn is live — release the registry so the queued-prompt drain
            # and dashboard attach gates stop deferring to it, and what a
            # person sent while it got ready goes out (input_queue).
            await warmup_registry.unregister(chat_id)
            registered = False
            _arm_chat_queue(chat_id)

        # Verify the turn actually OPENED before counting the wake delivered.
        # turn_open is transcript-derived (both CLIs write the user line at
        # submit), so a paste that never submitted leaves it False while the
        # PTY idles — previously that "delivery" silently lost the payload.
        # One bare-Enter nudge covers a paste that ate its settle Enter.
        if not await _wait_turn_open(isess, _WAKE_TURN_OPEN_S):
            if isess.alive:
                isess.submit_prompt("", settle=True)
            if not await _wait_turn_open(isess, _WAKE_TURN_OPEN_S):
                logger.warning(
                    "Delegate wake never opened a turn (paste lost?): "
                    "session=%s, chat=%s — storing "
                    "for durable replay", session_id[:8], chat_id[:8]
                )
                if isess.alive and not isess.has_viewer:
                    await interactive_session.close_session(
                        session_id, reason="delegate_wake_unsubmitted",
                    )
                return None

        waited = 0.0
        _STEP = _WAKE_POLL_S
        told = False
        over_polls = 0
        while True:
            try:
                await asyncio.wait_for(done.wait(), timeout=_STEP)
                break  # orchestrator's continuation turn completed
            except asyncio.TimeoutError:
                if isess.has_viewer:
                    told = False  # a later question after the person left is told again
                if not told and _waits_on_person(isess):
                    told = True
                    from services.scheduler.lanes import tell_chat_waits
                    await tell_chat_waits(agent, chat_id, cfg.user_sub or "", chat_row)
                if not isess.alive:
                    logger.warning(
                        "Delegate wake interactive session died mid-turn: "
                        "session=%s (chat=%s)",
                        session_id[:8], chat_id[:8]
                    )
                    # The turn opened; the tailer persisted whatever ran.
                    # Treat as delivered — a re-store would double-wake.
                    return ""
                # Two looks in a row with the turn over: it ended without a
                # callback (a turn faster than the minimum turn age).
                over_polls = over_polls + 1 if _woken_turn_over(isess) else 0
                if over_polls >= 2:
                    break
                waited += _STEP
                if waited >= interactive.INTERACTIVE_TASK_MAX_S:
                    logger.warning(
                        "Delegate wake turn exceeded %ss "
                        "backstop: session=%s — leaving session to "
                        "the idle reaper", int(interactive.INTERACTIVE_TASK_MAX_S), session_id[:8]
                    )
                    if ends_with_turn:
                        await _end_woken_terminal(session_id, isess)
                    return ""

        # Turn done. A person's chat keeps the terminal ALIVE: the idle reaper
        # owns its end, and a user opening the chat within the idle window
        # attaches to the real terminal with the wake turn on screen.
        if ends_with_turn:
            # Built for nobody on a chat people open: it ends with its turn,
            # whoever watches (``_ends_with_its_turn``).
            await _end_woken_terminal(session_id, isess)
            return ""
        logger.info(
            "Delegate wake turn complete: session=%s, "
            "viewer=%s — terminal left "
            "to the idle reaper", session_id[:8], 'yes' if isess.has_viewer else 'no'
        )
        return ""
    except asyncio.CancelledError:
        # Cancelled before the terminal started: the seat the config holds
        # goes back (a started terminal's close releases its own).
        if not spawned:
            release_config_seat(session_id, cfg)
        raise
    except Exception:
        logger.exception(
            "Delegate interactive re-warm failed: session=%s, "
            "chat=%s", session_id[:8], chat_id[:8]
        )
        if spawned:
            try:
                isess = interactive_session.get(session_id)
                if isess is not None and not isess.has_viewer:
                    await interactive_session.close_session(
                        session_id, reason="delegate_wake_failed",
                    )
            except Exception:  # noqa: BLE001  # closing a re-warmed terminal nobody watches after a failed wake
                pass
        else:
            # Nothing started: the seat the config holds goes back.
            release_config_seat(session_id, cfg)
        return None
    finally:
        if registered:
            with contextlib.suppress(Exception):
                await warmup_registry.unregister(chat_id)
            _arm_chat_queue(chat_id)
