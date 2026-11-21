"""Centralized agent config builder for session creation.

Consolidates the prompt/MCP/security/model config building that was
duplicated across dashboard warmup, pre-warmup, and plan implementation.
"""

import asyncio
import functools
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import psycopg

import config
from auth import roles
from auth.path_policy import SecurityContext, build_permission_context
from core import placement
from core.config.task_config_builder import TaskIdentity
from core.execution_layer import AgentConfig, LayerCapabilities
from core.permission_modes import ASK_MODE
from core.session import session_kind
from services.engines import subscription_pool
from services.mcp import account_context, dynamic_context, mcp_registry, tool_limits
from services.remote import remote_status
from storage import database as task_store, remote_store
from storage.agents import agent_store
from storage.agents.schema import DEFAULT_EXECUTION_PATH

if TYPE_CHECKING:
    from core.session.visibility import VisibilityResolution


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
    if not keep_binding:
        # The stand-ins a terminal's first message would have waited for:
        # the start registered them before it could fail. The build's open
        # start goes too (``start_context.forget``).
        from core.session import start_context
        from services.mcp import tool_listing
        tool_listing.drop_listed(session_id)
        start_context.forget(session_id)
    sub_id = getattr(cfg, "subscription_id", "") or ""
    if not sub_id:
        return
    if not keep_binding and subscription_pool.session_bound(session_id):
        subscription_pool.release_subscription(session_id)
    subscription_pool.release_unbound_seat(
        sub_id,
        subscription_pool.scope_key_for(
            getattr(cfg, "execution_target", "") or placement.LOCAL,
            getattr(cfg, "sandbox_host_claude_dir", "") or "",
            getattr(cfg, "session_home", "") or "",
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


logger = logging.getLogger("otodock")


@dataclass(frozen=True)
class _SessionIdentity:
    """What :func:`_session_identity` read for one build."""
    username: str | None
    user_role: str
    creds_sub: str | None
    display_name: str
    email: str
    platform_role: str
    resolved_targets: list[str]
    agent_info: dict
    execution_path: str
    caps: LayerCapabilities
    is_admin_only: bool
    vis: "VisibilityResolution"
    shared_only: bool


def _session_identity(
    agent_name: str,
    user: dict,
    user_sub: str,
    user_role: str,
    task_identity: TaskIdentity | None,
    execution_path: str,
) -> _SessionIdentity:
    """The identity a chat session builds in, resolved from the store (a
    worker-thread job of :func:`build_agent_config`)."""
    # Task re-warm overrides the viewer's identity with the task's stored
    # scope/identity. Without this, a dead agent-scoped task reopened by an
    # admin would rebuild with the admin's full-FS mounts (/users, /config
    # RW) instead of the agent-scope sandbox (/workspace RW, /knowledge RO).
    username: str | None
    creds_sub: str | None
    scope_override: str | None
    if task_identity is not None:
        username = task_identity.username or None
        user_role = task_identity.role
        creds_sub = task_identity.creds_user_sub  # None for agent scope
        display_name = ""
        email = ""
        scope_override = task_identity.scope
        # Platform role follows the task's creds user, not the viewer who
        # reopened it; empty for agent-scope tasks (no human identity).
        platform_role = ""
        if task_identity.creds_user_sub:
            creds_row = task_store.get_user(task_identity.creds_user_sub) or {}
            platform_role = creds_row.get("role") or ""
    else:
        username = user.get("username") or None
        creds_sub = user_sub
        display_name = user.get("display_name", "")
        email = user.get("email", "")
        scope_override = None
        platform_role = user.get("role") or ""

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
        user_agents = set(task_store.get_user_agents(cast("str", creds_sub)))
        resolved_targets = [t for t in db_targets if t in user_agents]

    # Self-delegation is always permitted, so the agent's own slug must appear in
    # the delegate roster + DELEGATION_TARGETS env. The delegation-mcp tool already
    # allows self via its `target_agent != AGENT` bypass; this just makes it
    # visible. (_delegation_mcp_context renders self separately + skips it in the peer
    # loop; _meeting_context excludes self.)
    if agent_name not in resolved_targets:
        resolved_targets = [agent_name] + resolved_targets

    # Resolve sandbox + execution path early (needed for MCP config)
    agent_info = agent_store.get_agent(agent_name) or {}
    execution_path = (execution_path
                      or agent_info.get("execution_path")
                      or DEFAULT_EXECUTION_PATH)
    # The engine's descriptor answers the engine questions of the build; a
    # stored id no layer claims fails here, closed, with the registry's
    # message, before the visibility read below.
    from core.session.session_manager import capabilities_for_path
    caps = capabilities_for_path(execution_path)
    is_admin_only = agent_store.is_admin_only(agent_name)

    # Resolve the agent's visibility mode ONCE — the single owner of mount
    # scope/username, config visibility, available scopes, memory availability
    # and the chat-history owner. ``username`` passed here is the REAL human
    # (attribution); the resolver derives the agent-scope MOUNT username ("")
    # for Shared-only chats and service sessions. ``scope_override`` carries a
    # continued task's stored scope.
    from core.session.visibility import is_shared_only, resolve_visibility
    vis = resolve_visibility(
        agent_name,
        username=username or "",
        user_role=user_role or "",
        user_sub=creds_sub or "",
        scope_override=scope_override,
    )
    return _SessionIdentity(
        username=username, user_role=user_role, creds_sub=creds_sub,
        display_name=display_name, email=email, platform_role=platform_role,
        resolved_targets=resolved_targets, agent_info=agent_info,
        execution_path=execution_path, caps=caps, is_admin_only=is_admin_only,
        vis=vis, shared_only=is_shared_only(agent_name),
    )


def _model_and_effort(agent_name: str, model: str, execution_path: str) -> tuple[str, str]:
    """The session's model (the caller's, else the agent's default on this
    layer) and effort, read from the store (a worker-thread job)."""
    return (model or config.get_cli_model(agent_name, layer=execution_path),
            config.get_cli_effort(agent_name))


def _moved_pin(chat_id: str) -> str:
    """The pin a person chose for the chat (its latest move row that did not
    fail: a move, a summarised move, a new chat's pick), the platform or a
    reachable machine; ``""`` otherwise. Asked only on a fresh spawn, when
    there is no session to resume (a summary, an engine switch, a cleared
    context). Synchronous."""
    from storage import move_schema
    chat = task_store.get_chat(chat_id)
    if not chat:
        return ""
    pin = chat.get("execution_target") or ""
    last = remote_store.latest_chat_move(chat_id)
    if not last or last.get("state") == move_schema.FAILED:
        return ""
    if placement.is_local(pin):
        # A chat moved to (or started on) the platform stays there, even when
        # the person's default is a machine.
        return placement.LOCAL if placement.is_local(last.get("target")) else ""
    if last.get("target") != pin:
        return ""
    if not remote_status.is_reachable(pin):
        logger.info("build_agent_config: chat %s was moved to %s, which is offline: "
                    "the agent's default takes it", chat_id[:8], pin[:8])
        return ""
    return pin


def personal_owner_of(target: placement.PlacementCapabilities, agents_accounts: bool,
                      person_sub: str | None, browser) -> str:
    """The person whose own things a session on the agent's accounts
    (``agents_accounts``: ``account_context.credential_identity`` answered
    the agent's, as it does for a Shared-only chat, a chat worker and a team
    account's chat) may carry to a personal machine: its person when that
    person owns the machine (``browser`` is the target's
    ``remote_store.BrowserTargetSettings``, whose ``owner_sub`` is the row's
    owner, read in the same SELECT as the token), ``""`` otherwise. Every
    builder of such a session asks it once, and its answer is the owner
    filter of the build (``build_session_mcp_config``'s ``personal_owner``)
    and the bundles' owner (``personal_machine_bundles``)."""
    if not (target.user_paired and agents_accounts and person_sub and browser is not None):
        return ""
    return person_sub if getattr(browser, "owner_sub", "") == person_sub else ""


def personal_machine_bundles(secret_bundles: dict, target: placement.PlacementCapabilities,
                             agents_accounts: bool, agent_name: str, *, who: str,
                             owner: str = "") -> dict:
    """The secret bundles a session may carry to ``target``. A session on
    the agent's accounts (``agents_accounts``, as ``personal_owner_of``) on a
    person's own machine carries only what is that person's, and only when
    it runs as them (``owner``, from ``personal_owner_of``): the machine's
    own browser token, and the bundles of MCPs whose credential is one
    person's account (``credential_resolver.carries_to_personal_machine``),
    which the build's owner filter resolved from that person's bindings
    alone. Everything else (the agent's other accounts, an admin's instance
    settings, a key) stays on the platform, logged by name. Every build of
    such a session asks it (the chat's, a wake's, a chat worker's)."""
    if not (secret_bundles and target.user_paired and agents_accounts):
        return secret_bundles
    from core.credentials.mcp_broker import SecretBundle
    from services.oauth.credential_resolver import carries_to_personal_machine
    kept: dict = {}
    for key, bundle in secret_bundles.items() if owner else ():
        if key == mcp_registry.BROWSER_SERVER_KEY:
            token = bundle.env.get(mcp_registry.BROWSER_TOKEN_ENV)
            if token:
                kept[key] = SecretBundle(env={mcp_registry.BROWSER_TOKEN_ENV: token})
            continue
        manifest = mcp_registry.get_manifest_by_config_key(key)
        renews = getattr(getattr(bundle, "token", None), "renews", False)
        if manifest is not None and carries_to_personal_machine(manifest) and not renews:
            kept[key] = bundle
    withheld = sorted(set(secret_bundles) - set(kept))
    if withheld:
        logger.warning("%s: %s runs agent-scoped on the personal machine %s: secret files "
                       "withheld for %s", who, agent_name, target.machine_id[:8],
                       ", ".join(withheld))
    return kept


def _placement_refusal(target: str, run_sub: str | None, role: str | None,
                       agent_default: str | None) -> str:
    """The sentence when a session of this person may not run on ``target``
    (``placement.may_run_on``, ``agent_default`` the agent's own default
    place); ``""`` when it may. Synchronous."""
    machine = remote_store.get_remote_machine(target)
    if machine is None:
        return ""
    allowed_user = task_store.get_platform_setting("allow_user_paired_machines") != "0"
    if placement.may_run_on(machine, run_sub=run_sub, run_can_manage=roles.can_manage(role),
                            user_paired_allowed=allowed_user, agent_default=agent_default):
        return ""
    name = str(machine.get("name") or target[:8])
    if placement.machine_is_compute(machine):
        return f"This chat is on {name}, a compute machine. Chats do not run there yet."
    if placement.machine_is_personal(machine):
        from services.remote.chat_targets import owner_name
        return (f"This chat is on {owner_name(machine.get('registered_by') or '')}'s computer "
                f"({name}). Move it to one of your places from the Runs on menu.")
    return (f"This chat is on {name}, which your role on this agent can't use. "
            "Move it from the Runs on menu.")


async def _with_accounts_block(
    contexts: list, agent_name: str, assigned: list[str], person: str,
    account_choices: dict[str, str], *, switchable: bool, scope: str = "user",
    run: bool = False,
) -> list:
    """``contexts`` with the accounts block (``account_context.account_lines``):
    whose accounts the session runs on when they are a person's, and, for a
    session that can switch (``switchable``, ``scope`` the chat's
    ``choice_scope``), the accounts it may switch between; ``run`` for a
    task's session."""
    block = await asyncio.to_thread(
        account_context.account_lines, agent_name, assigned, user_sub=person,
        account_choices=account_choices, scope=scope if switchable else "", run=run)
    return [*(contexts or []), (account_context.AGENT_CONFIG_MCP, block)] if block else contexts


def _own_engine_dir(security_ctx, agent_name: str, session_id: str, kind_rule) -> dict:
    """The config dir arguments of a session that runs from its own scratch
    engine home: the agent's own phone line, keyed on the call, never the agent
    scope's own, which holds every agent-scope session's transcripts (pruned at
    the call's end with the other scratch homes; its skills its rule's). ``{}``
    for every other session. Makes the scratch home's folder: call it in a
    worker thread (``_persistent_dir``), never as an argument evaluated on
    the loop."""
    if not (security_ctx.open_line and session_id):
        return {}
    from core.session import external_identity
    home = external_identity.scratch_home(config.get_agent_dir(agent_name), external_identity.PHONE, session_id)
    return {"external_home": str(home), "kind_rule": kind_rule}


def _persistent_dir(agent_name: str, execution_path: str, username: str, scope: str,
                    security_ctx, session_id: str, kind_rule):
    """The session's persistent config dir, its scratch home included, in the
    worker thread that calls it."""
    from core.sandbox.sandbox import ensure_persistent_agent_dir
    return ensure_persistent_agent_dir(
        agent_name, execution_path=execution_path, username=username, scope=scope,
        **_own_engine_dir(security_ctx, agent_name, session_id, kind_rule))


async def _compute_row(target: str, execution_path: str, chat_id: str, user_role: str,
                       phone_mode: bool, prefer: str = "", *, agent: str = "") -> dict | None:
    """The compute machine a session resolved to ``target`` is built on
    (``remote_status.compute_row_for``: None on a paired computer, the
    refusal when the platform's place has no machine). ``prefer`` is the
    machine a caller already took the session's slot on, so the build and
    the slot agree; else the chat's place (``remote_status.chat_place``,
    read only while a machine on another host is connected): the machine
    its engine record lives on, or a shared one when the record is in the
    platform's folder; an outside person's session needs the platform's own
    folder (theirs never syncs), and so does a call (a spoken turn does not
    wait for a sync). ``agent`` the session's: a tool of it that reaches a
    local address keeps it beside the platform, unless its chat stays on
    its record's machine (``ChatPlace.agent_for``)."""
    if not placement.is_local(target):
        return None
    place = remote_status.ChatPlace("", False)
    if chat_id and remote_status.any_own_copy():
        place = remote_status.chat_place(await asyncio.to_thread(task_store.get_chat, chat_id))
    return await asyncio.to_thread(functools.partial(
        remote_status.compute_row_for, target, execution_path, prefer=prefer or place.machine,
        shared_folder=place.shared or phone_mode or (user_role or "") == roles.EXTERNAL,
        agent=place.agent_for(agent)))


async def build_agent_config(
    agent_name: str,
    user: dict,
    user_sub: str,
    user_role: str,
    permission_mode: str = ASK_MODE,
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
    extra_mcps: list[str] | None = None,
    call_ask_back: bool = False,
    kind_rule=None,
    no_shell: bool = False,
    compute_prefer: str = "",
) -> AgentConfig:
    """Build an AgentConfig from agent DB + user context.

    ``compute_prefer`` is the compute machine a caller already took the
    session's slot on (a wake): the build lands there while that machine
    still serves, so the slot and the session agree.

    ``kind_rule`` and ``no_shell`` are the agent's own phone line's (the
    phone builder): its hard set of management tools through the one rule,
    and no shell, since with the session's agent token a shell could make
    any call the hard set keeps off.

    ``extra_mcps`` and ``call_ask_back`` are the phone builder's: the
    call-control MCP rides a user route's call regardless of assignment, and
    an outbound call another agent placed offers the ask tool.

    The phone ``user`` identity mode (a route tied to a platform user —
    ``services/phone/phone_identity.py``) reuses this builder so the call is
    that user's own session: ``phone_mode`` applies the call-only MCP
    exclusions, ``trigger_payload`` feeds the ``${trigger.*}`` context
    blocks, ``subscription_pool_fallback`` lets a user without a subscription
    ride the platform pool, bound to nobody, and ``external_claim``
    rides the session token so it dies at hangup and the call log can name
    the caller — the SecurityContext stays a USER principal.

    Centralizes prompt building, MCP config, credential resolution,
    security context, and model/effort resolution.

    Args:
        agent_name: Agent slug.
        user: User dict from DB (has username, display_name, email, role).
        user_sub: User sub (OAuth subject identifier).
        user_role: the effective per-agent role (auth/roles.EFFECTIVE_ROLES; acting_role_of on the WS side).
        permission_mode: the chat's mode (core/permission_modes; ``auto`` for an unattended run).
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
    # The reads before the placement — the task re-warm's creds row, the
    # delegation targets, the agent row, the visibility mode and memory
    # toggles — in one worker-thread job, in their order.
    ident = await asyncio.to_thread(
        _session_identity, agent_name, user, user_sub, user_role, task_identity, execution_path,
    )
    username, user_role, creds_sub = ident.username, ident.user_role, ident.creds_sub
    _display_name, _email, _platform_role = ident.display_name, ident.email, ident.platform_role
    resolved_targets, execution_path = ident.resolved_targets, ident.execution_path
    _caps = ident.caps
    mcp_format = _caps.mcp_config_format
    agent_info = ident.agent_info
    is_admin_only = ident.is_admin_only
    vis = ident.vis
    # Below the editor tier nobody runs from the agent's own CLI state (a
    # Shared-only chat, a task re-warm, a phone call as that person):
    # refused here, before a pool seat is taken, with the person's message.
    from core.sandbox.session_config_dir import refuse_agent_state_below_editor
    refuse_agent_state_below_editor(vis.mount_scope, user_role or "",
                                    shared_only=ident.shared_only)

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
        resolved_target: tuple[str, str | None] = (placement.LOCAL, None)
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
            from services.remote import machine_away
            from services.remote.remote_status import is_reachable as _is_reachable
            # A machine that drops off for its update's restart is back in
            # seconds: the start waits for it as for a machine finishing it.
            await machine_away.update_done(placement.machine_of(pinned_target))
            reachable = await asyncio.to_thread(_is_reachable, pinned_target)
            resolved_target = (
                (pinned_target, None) if reachable
                else (placement.offline_sentinel(pinned_target), "pinned-target-offline")
            )
    else:
        moved = await asyncio.to_thread(_moved_pin, chat_id) if chat_id else ""
        if moved:
            # A person chose the chat's place and it has no session to resume
            # there: the fresh spawn goes there, not to the agent's default.
            resolved_target = (moved, None)
        else:
            resolved_target = await asyncio.to_thread(
                remote_status.resolve_execution_target, agent_name, creds_sub, user_role,
            )
    target_value = resolved_target[0]
    if not placement.is_local(target_value) and not placement.is_offline_sentinel(target_value):
        # The placement guard: never a session on a place its person may not
        # use (a shared chat another person placed on their computer, an
        # admin machine below the owner tier unless its every-role switch
        # admits the agent's chats). Sent with the sentence that says where
        # the chat is.
        refusal = await asyncio.to_thread(_placement_refusal, target_value, creds_sub, user_role,
                                          agent_info.get("execution_target"))
        if refusal:
            raise placement.PlacementRefused(refusal)
    # The resolved placement — the kind (local / admin-paired / user-paired)
    # and the machine's facts (paths, OS, the pairing flag, the grants, the
    # display), from one read of its row. It feeds the SecurityContext, the
    # MCP placement / consent / display gates, the prompt and the dynamic
    # contexts below.
    # The compute machine that serves the platform's place now, read once
    # here and carried in the placement (``remote_status.compute_row_for``):
    # the layer is resolved from it, and with none the session is refused.
    # A call on a person's route goes to one that shares the platform's
    # folders (the phone socket reads its layer off this build).
    compute_row = await _compute_row(target_value, execution_path, chat_id, user_role,
                                     phone_mode, compute_prefer, agent=agent_name)
    # A machine still in its update is waited for, a bounded time, before
    # its tools are built; past the wait the start is refused.
    from services.remote import machine_away
    target = await machine_away.settled_placement(target_value, creds_sub, agent_name, compute=compute_row)
    # Browser-control mode + extension token: a targeted read (the token
    # column never rides the machine row nor the persisted context),
    # dedicated/no-token where no device tool is offered (the platform's
    # own places).
    target_browser = await asyncio.to_thread(
        remote_store.get_target_browser_settings, target,
    ) if target.offers_device_tools else None
    # The agent's MCP set on this placement, read once and used by the
    # call-control check, the path_env values and the assigned names below.
    agent_mcps = await asyncio.to_thread(
        mcp_registry.get_agent_mcps, agent_name, placement=target,
    ) or []

    # Resolve per-user MCP credentials + config.
    # For TOML (Codex), credential env is injected into the TOML env sections
    # (Codex doesn't inherit parent env for MCP servers). We must rewrite
    # sandbox paths FIRST so the TOML gets the correct sandbox-internal paths.
    # A dashboard session of an agent with phone-mcp carries the call-control
    # MCP too: a call it places may run inside this very chat
    # (ws/phone_attach.py), and the tools end or transfer that call.
    if client_type == session_kind.DASHBOARD.name and not phone_mode:
        from services.phone.call_control import CALL_CONTROL_MCP, PHONE_MCP
        _names = {m.name for m in agent_mcps}
        if PHONE_MCP in _names and CALL_CONTROL_MCP not in (extra_mcps or []):
            extra_mcps = [*(extra_mcps or []), CALL_CONTROL_MCP]
    # A chat that is not the person's own (``mcp_registry.SHARED_CHAT_CONTEXT``):
    # no person, a task chat reopened in the dashboard, a
    # Shared-only agent's chat, or a chat row another person owns (an admin
    # driving a member's chat, a delegated worker's chat). The MCP set and
    # the prompt both read the fact, passed only when it holds.
    from core.session.visibility import SCOPE_AGENT
    shared_chat = (
        not creds_sub or task_identity is not None or ident.shared_only
        or vis.mount_scope == SCOPE_AGENT
    )
    if not shared_chat and chat_id:
        _owner_row = await asyncio.to_thread(task_store.get_chat, chat_id)
        if _owner_row:
            shared_chat = (_owner_row.get("user_sub") or "") not in ("", creds_sub)
    # Passed only when it holds: a build of the person's own chat calls the
    # builders with exactly the keywords their goldens record. The two
    # builders are read at call time, as their test stubs expect.

    # Whose credentials the session resolves (``account_context.credential_identity``,
    # the session build asks the same). On the agent's accounts on a personal
    # machine: the machine's owner when they are the person at the chat
    # (``personal_owner_of``), else nobody.
    agents_accounts = (await asyncio.to_thread(
        account_context.credential_identity, agent_name, creds_sub,
        mount_scope=vis.mount_scope, role=user_role or "")).agents
    personal_owner = personal_owner_of(target, agents_accounts, creds_sub, target_browser)
    on_personal = target.user_paired and agents_accounts

    # The chat's own choice of account per tool (``credential_resolver.pick_account``):
    # a person's own chat's among their accounts, a chat on the agent's
    # accounts among the agent's own, none for any other chat
    # (``account_context.choice_scope``).
    choice_scope = ""
    if chat_id and creds_sub and task_identity is None:
        choice_scope = await asyncio.to_thread(
            account_context.choice_scope, chat_id, mount_scope=vis.mount_scope,
            role=user_role or "")
    account_choices: dict[str, str] = {}
    if choice_scope:
        account_choices = await asyncio.to_thread(
            account_context.session_choices, chat_id, creds_sub or "",
            mount_scope=vis.mount_scope, role=user_role or "")

    # The agent's tool limits for this session's kind (``tool_limits``): a
    # person's identity behind the session takes the chats column (a task
    # chat re-warmed, its run's person in every mode: ``TaskIdentity.person``),
    # none the runs column (the agent's own line, the agent's own run).
    limits = await asyncio.to_thread(
        tool_limits.load, agent_name, tool_limits.governs_of(
            external=False, person=task_identity.person if task_identity is not None else creds_sub))

    def _mcp_build(*args, **kwargs):
        if shared_chat:
            kwargs["shared_chat"] = True
        kwargs["kind_rule"] = kind_rule
        if on_personal:
            kwargs["personal_owner"] = personal_owner
        if account_choices:
            kwargs["account_choices"] = account_choices
        kwargs["limits"] = limits or None
        return mcp_registry.build_session_mcp_config(*args, **kwargs)

    def _prompt_build(*args, **kwargs):
        if shared_chat:
            kwargs["shared_chat"] = True
        kwargs["kind_rule"] = kind_rule
        kwargs["tool_limits"] = limit_pairs or None
        return config.build_agent_prompt(*args, **kwargs)

    mcp_config, credential_env, excluded_mcps, secret_bundles, bash_env_keys = (
        await asyncio.to_thread(
            _mcp_build, agent_name, creds_sub,
            session_id=session_id,
            chat_id=chat_id,
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
            call=client_type == session_kind.PHONE.name,
            placement=target,
            target_browser=target_browser,
            extra_mcps=extra_mcps,
            call_ask_back=call_ask_back,
        )
    )
    secret_bundles = personal_machine_bundles(secret_bundles, target, agents_accounts,
                                              agent_name, who="build_agent_config",
                                              owner=personal_owner)
    # The limits' grants, per attached server, as the hook and the engines
    # read them (``SecurityContext.tool_limits``).
    limit_pairs = tool_limits.grants(
        limits, [m for m in agent_mcps if m.name not in (excluded_mcps or {})])

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
    for manifest in agent_mcps:
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
    from auth.session_token import mint_session_env
    token_minted_at = mint_session_env(
        credential_env, session_id or "", agent_name, creds_sub or "",
        external=external_claim or "", role=user_role,
    )
    multi_value_envs.update(oto_env.OTO_MULTI_VALUE_ENVS)

    # A Codex config gets the credential environment written in; the
    # bash-only git entries stay out of the file (they belong to the shell,
    # through the daemon env below).
    mcp_config = await asyncio.to_thread(
        mcp_registry.inject_toml_credentials, mcp_config, credential_env,
        mcp_format=mcp_format, exclude_keys=bash_env_keys,
    )

    # Resolve dynamic MCP context (e.g., delegation targets with descriptions).
    # Async — builder blocks invoke remote MCP tools.
    # Chat sessions never carry a trigger_payload; ``${trigger.*}`` tokens
    # resolve empty and any trigger-gated blocks skip naturally.
    assigned_mcp_names = [
        m.name for m in agent_mcps
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
            dynamic_context.build_delegation_roster, resolved_targets, creds_sub or "",
        ),
        meetings_access=await asyncio.to_thread(
            dynamic_context.build_meetings_access, creds_sub or "", user_role or "",
        ),
        # Only a NO-USER session reads its delegation targets (the edge is
        # the read grant) — user-backed sessions follow the user (the
        # server enforces from the token either way; this is prompt).
        nouser_reads=not creds_sub,
        placement=target,
        **({"account_choices": account_choices} if account_choices else {}),
    )
    # The accounts the chat may switch between, for a dashboard chat that
    # chooses: the switch is refused everywhere else (a call, a task's chat).
    dynamic_contexts = await _with_accounts_block(
        dynamic_contexts, agent_name, assigned_mcp_names, creds_sub or "", account_choices,
        switchable=bool(choice_scope) and client_type == session_kind.DASHBOARD.name
        and not phone_mode,
        scope=choice_scope)

    # Build agent system prompt. MOUNT username drives the workspace tree +
    # user-context sections (agent-scope for Shared-only); the REAL human
    # identity is rendered by build_permission_context from the SecurityContext.
    # In a worker thread: the build reads the agent's files, the person's
    # context and memory and walks the workspace tree (tens of milliseconds
    # on a large agent), which the loop must not wait for.
    agent_prompt = await asyncio.to_thread(
        _prompt_build,
        agent_name, username=vis.mount_username, role=user_role,
        excluded_mcps=excluded_mcps or None,
        dynamic_contexts=dynamic_contexts or None,
        sandboxed=True,
        client_type=client_type or "",
        placement=target,
        mount_shared=vis.mount_shared,
        execution_path=execution_path or "",
        # The person's role filters the skills by audience (a chat has one).
        user_role=user_role or None,
        session_id=session_id,
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
    except (ImportError, psycopg.Error):
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
        no_shell=no_shell,
        open_line=bool(getattr(kind_rule, "open_line", False)),
        tool_limits=limit_pairs,
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
    from core.sandbox.session_config_dir import session_home_for
    host_claude_dir = await asyncio.to_thread(
        _persistent_dir, agent_name, execution_path, vis.mount_username, scope,
        security_ctx, session_id, kind_rule)
    home = session_home_for(host_claude_dir, session_id, execution_path=execution_path, ctx=security_ctx)

    # Resolve model and effort. Layer-aware: on a session whose layer
    # differs from the agent's engine (task/delegate overrides, engine-
    # switched chats warming with model="") the agent-default model may be
    # foreign to the layer — never stamp it (the provider 400s every turn).
    resolved_model, resolved_effort = await asyncio.to_thread(
        _model_and_effort, agent_name, model, execution_path,
    )
    # Who pays (``core/config/spender.py``): decided by the session being
    # built and its person, a task chat by its run. A person who resolves nothing is refused with the reason;
    # a call to a person's route alone falls back to the shared
    # subscriptions, bound to nobody.
    from core.config import spender
    payer = await asyncio.to_thread(functools.partial(
        spender.build_spender, chat_id, agent_name, creds_sub,
        call=phone_mode, route=subscription_pool_fallback))
    spent = await asyncio.to_thread(functools.partial(
        spender.spend_env, payer, execution_path, model=resolved_model, agent_info=agent_info,
        # Scope-sticky: sessions sharing this credential dir must run on
        # ONE account (same key the layer stamps at bind_session).
        sticky_scope=subscription_pool.scope_key_for(target_value, str(host_claude_dir), home),
        agent=agent_name, call_fallback=subscription_pool_fallback))
    subscription_id = spent.subscription_id
    extra_env: dict[str, str] = dict(spent.env)

    # Prompt-size line (≈ tokens = chars / 4): the always-loaded skill text
    # is the bulk of it — this is the number the skills card/guide split
    # (Plan B) is measured against, on every engine.
    _prompt_chars = len(agent_prompt or "")
    logger.info(
        "Prompt built for %s: client=%s "
        "layer=%s ≈%s tokens "
        "(%s chars)",
        agent_name, client_type or '-', execution_path or '-', _prompt_chars // 4, _prompt_chars
    )

    return AgentConfig(
        agent_name=agent_name,
        user_sub=user_sub or "",
        system_prompt=agent_prompt or "",
        mcp_config_path=str(mcp_config) if mcp_config else "",
        credential_env=credential_env or {},
        mcp_secret_bundles=secret_bundles or {},
        excluded_mcps=frozenset(excluded_mcps or ()),
        permission_mode=permission_mode,
        client_type=client_type,
        model=resolved_model,
        effort=resolved_effort,
        resume=resume,
        use_native_permissions=False,  # dashboard uses hook-based gating
        extra_env=extra_env,
        security_context=security_ctx,
        subscription_id=subscription_id,
        subscription_user_sub=spent.bound_sub,
        sandbox_host_claude_dir=str(host_claude_dir),
        session_home=home,
        resume_handle=resume_handle,
        chat_id=chat_id,
        multi_value_envs=multi_value_envs,
        **_resolve_target_fields(resolved_target),
        execution_path=execution_path,
        default_execution_mode=(agent_info or {}).get("default_execution_mode", "") or "",
        work_cwd=work_cwd or "",
        term=term or "",
        token_minted_at=token_minted_at,
        account_choices=account_choices,
    )
