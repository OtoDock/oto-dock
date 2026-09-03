"""Task-specific agent config builder.

Builds AgentConfig for task execution — handles scope-based credentials,
task system prompt suffix, and task environment variables.

Extracted from services/scheduler/scheduler.py to centralize config building.
"""

import asyncio
import logging
from typing import NamedTuple

import config
from storage import database as task_store
from storage import agent_store
from storage import remote_store
from services.mcp import mcp_registry
from services.mcp import dynamic_context
from services.engines import subscription_pool
from auth.path_policy import SecurityContext, build_permission_context
from core.execution_layer import AgentConfig
from core.session.visibility import is_shared_only

logger = logging.getLogger("claude-proxy")


class TaskIdentity(NamedTuple):
    """The scope + identity a task session runs as.

    Single source of truth for task-scope resolution, shared by the
    scheduler (``build_task_agent_config``) AND the dashboard WS re-warm
    path (``ws/dashboard.py``) so a continued task ALWAYS keeps its
    original scope/identity — never the identity of whoever re-opened it.
    """

    username: str             # "" for agent scope; creator's username for user scope
    role: str                 # admin | manager | viewer
    scope: str                # "agent" | "user"
    creds_user_sub: str | None  # None for agent scope; creator's sub for user scope
    # Manager-provenance knowledge writes (agent scope only): True when the
    # dynamic task's creator resolves to a platform admin or a per-agent
    # MANAGER of this agent AND the caller opted in (``allow_knowledge_rw``).
    # Re-resolved at every fire, so a demoted/deleted creator loses it at the
    # next run. Always False for user-scope identities (their username drives
    # the ordinary owner-tier knowledge path).
    knowledge_rw: bool = False


# Dynamic-task fire shapes eligible for manager-provenance knowledge writes.
# ``delegate`` is included (operator decision 2026-08-24): a delegate
# worker's ``created_by`` is the REAL delegating user (spawn_authz uses the
# acting sub; no-user delegations record the source agent slug, which the
# provenance check fails closed on), so an agent-scope worker delegated by
# a manager of the TARGET agent runs with the same knowledge reach that
# user's own chat on the target already has — user-scope delegate workers
# on personal-capable targets always had it via the ordinary role path.
# Meetings / headless apps / phone never carry a task_type and stay False
# by default.
_KNOWLEDGE_RW_TASK_TYPES = frozenset(
    {"scheduled", "one_time", "trigger", "delegate"})


def task_allows_knowledge_rw(task_type: str | None) -> bool:
    """Whether this task shape may opt in to manager-provenance knowledge
    writes (``resolve_task_identity(allow_knowledge_rw=...)``). Unknown /
    empty / ``continuation`` types fail closed."""
    return (task_type or "") in _KNOWLEDGE_RW_TASK_TYPES


def _creator_grants_knowledge_rw(agent_name: str, created_by: str | None) -> bool:
    """Provenance check for the agent-scope branch: the ``created_by`` column
    is mixed-type (user sub | agent slug | ``"api"``), so the creator must
    resolve to a REAL platform user — an agent self-scheduling a task can
    never self-escalate. Grant only for a platform admin or a per-agent
    MANAGER of THIS agent (per-agent roles only; editors stay False)."""
    if not created_by:
        return False
    creator = task_store.get_user(created_by)
    if not creator:
        return False
    if (creator.get("role") or "") == "admin":
        return True
    per_agent = task_store.get_user_agent_roles(created_by).get(agent_name, "")
    return per_agent == "manager"


def resolve_task_identity(
    agent_name: str, scope: str, created_by: str | None,
    *, allow_knowledge_rw: bool = False,
) -> TaskIdentity:
    """Resolve the (username, role, scope, creds_user_sub) a task runs as.

    - **User-scoped** task → the CREATOR's username + per-agent role (admin
      if the creator is a platform admin), creds = the creator. This is what
      keeps a user-scope task running as its owner even when an admin
      re-warms someone else's run.
    - **Agent-scoped** task → NO user (``username=""``, creds ``None``); the
      role is intrinsic to the agent (``admin`` for admin-only agents, else
      ``manager``). The empty username is what drives the agent-scope sandbox
      mount (``/workspace`` RW + ``/knowledge`` RO, no ``/config``, no
      ``/users``) regardless of the role label — see
      ``core/sandbox/sandbox.py:_workspace_mounts``.
    - **Shared-only** agents ALWAYS run agent-scoped (shared workspace), no
      matter how the run row stored ``scope`` — they have no per-user scope.
      Forcing it here (not just clamping the mount downstream) is what keeps the
      task's CREDENTIALS agent-scope: ``creds_user_sub`` stays ``None`` so the
      session draws on the platform pool, never the creator's subscription.
    - **knowledge_rw** (agent scope only): computed from ``created_by``
      provenance when ``allow_knowledge_rw`` is passed — ONLY the scheduler's
      scheduled / one-time / trigger fire paths (and the WS re-warm of such a
      run, which rebuilds the same fire) opt in; every other construction
      site keeps the default False. Baked at spawn: a creator demoted
      mid-run keeps knowledge RW until that run ends (next fire re-resolves).
    """
    if scope == "user" and created_by and not is_shared_only(agent_name):
        username = task_store.get_username_by_sub(created_by) or ""
        creator = task_store.get_user(created_by)
        if creator and (creator.get("role") or "") == "admin":
            role = "admin"
        elif creator:
            role = task_store.get_user_agent_roles(created_by).get(agent_name, "viewer")
        else:
            role = "viewer"
        return TaskIdentity(
            username=username, role=role, scope="user", creds_user_sub=created_by,
        )
    knowledge_rw = (
        allow_knowledge_rw
        and _creator_grants_knowledge_rw(agent_name, created_by)
    )
    if agent_store.is_admin_only(agent_name):
        return TaskIdentity(username="", role="admin", scope="agent",
                            creds_user_sub=None, knowledge_rw=knowledge_rw)
    return TaskIdentity(username="", role="manager", scope="agent",
                        creds_user_sub=None, knowledge_rw=knowledge_rw)


# Background task rules. The subagent paragraph only ships on layers that
# actually expose the Agent tool (Claude Code CLI + Codex CLI); Direct LLM
# has no Agent tool, so receiving instructions to "use subagents" would be
# noise + confusion. Completion is detected deterministically (CLI: the
# turn's `result` event plus the SubagentRegistry's all-done state) — there
# is no agent-emitted completion marker.
_TASK_AGENT_PREAMBLE = """

---

## Background Task Agent Rules

You are running as a background task agent (no interactive user).
"""

_TASK_AGENT_SUBAGENT_RULES = """
### Subagent Rules
- For complex tasks, you SHOULD use the Agent tool to spawn subagents for parallel research/work.
- You can spawn multiple foreground agents in a single message — they run concurrently in parallel.
- You MAY use background agents (`run_in_background: true`) when you want to continue working while agents run. The system automatically detects when they finish and prompts you to review their results — just end your turn normally when you've done what you can; no completion marker is needed.
"""


def _build_task_agent_suffix(execution_path: str) -> str:
    """Assemble the task suffix for a given execution layer.

    Subagents (the Agent tool) only exist on Claude Code CLI + Codex CLI.
    Direct LLM has no equivalent, so the subagent paragraph is dropped to
    avoid telling the model to call a tool that isn't there.
    """
    parts = [_TASK_AGENT_PREAMBLE]
    if execution_path in ("claude-code-cli", "codex-cli"):
        parts.append(_TASK_AGENT_SUBAGENT_RULES)
    return "".join(parts)


def _notification_policy_block(mode: str) -> str:
    """Mode-specific instructions appended to the task agent's system prompt.

    The creating LLM picks ``notification_mode`` on the task row; the task
    agent never sees that field directly. This block translates the choice
    into a single clear rule — eliminating the old ambiguity where both the
    system AND the agent could fire a "task complete" notification.
    """
    if mode == "auto":
        return (
            "\n\n## Notification Policy\n"
            "A completion notification will be sent automatically by the system "
            "when this task finishes (success or failure). **Do NOT call "
            "`mcp__notifications-mcp__create_notification` yourself** — it would "
            "duplicate the automatic notification."
        )
    if mode == "manual":
        return (
            "\n\n## Notification Policy\n"
            "When you finish your work, call "
            "`mcp__notifications-mcp__create_notification` with a title and body "
            "summarizing what you did. Pick a severity matching the outcome "
            "(`info` / `success` / `warning` / `danger`). Send exactly **one** "
            "final notification — the system handles failure notifications for "
            "you if you crash, so don't notify on errors yourself."
        )
    if mode == "none":
        return (
            "\n\n## Notification Policy\n"
            "This task runs silently. **Do NOT call "
            "`mcp__notifications-mcp__create_notification`.** The user does not "
            "expect to be notified about this run."
        )
    return ""


async def build_task_agent_config(
    agent_name: str,
    task,
    session_id: str,
    trigger_payload: dict | None = None,
) -> AgentConfig:
    """Build an AgentConfig for task execution.

    Handles scope-based credential resolution, task suffix, and env vars.

    Args:
        agent_name: Agent slug.
        task: TaskDefinition with scope, created_by, etc.
        session_id: Session ID for the task execution.
        trigger_payload: when this task was fired by a webhook
            trigger, the normalised payload (source, route, phone, email,
            did, body) is threaded through to ``get_dynamic_contexts`` so
            manifest ``agent_context`` blocks resolve ``${trigger.*}``
            tokens. ``None`` for scheduled / manual fires.
    """
    # Resolve the scope + identity this task session runs as. Single source
    # of truth (shared with the dashboard WS re-warm path) so a continued
    # task keeps its original scope — see ``resolve_task_identity``. Computed
    # BEFORE the MCP config build so instance-config transforms (e.g.
    # ssh_hosts) get session context for scope-aware allowlists. The
    # knowledge-RW opt-in keys on the task SHAPE: scheduled / one-time /
    # trigger fires only (delegate workers ride this same funnel and must
    # stay out — see ``task_allows_knowledge_rw``).
    identity = resolve_task_identity(
        agent_name, task.scope, task.created_by,
        allow_knowledge_rw=task_allows_knowledge_rw(
            getattr(task, "task_type", "") or ""),
    )
    task_username = identity.username
    task_role = identity.role
    user_sub_for_creds = identity.creds_user_sub  # None for agent scope
    # Platform role of the task's creds user; "" for agent-scope runs.
    task_platform_role = ""
    if user_sub_for_creds:
        task_platform_role = (task_store.get_user(user_sub_for_creds) or {}).get("role") or ""

    # Resolve delegation targets: user-scope filters to the creator's
    # accessible agents; agent-scope keeps all configured targets.
    db_targets = agent_store.get_delegation_targets(agent_name)
    if identity.scope == "user" and user_sub_for_creds:
        user_agents = set(task_store.get_user_agents(user_sub_for_creds))
        resolved_targets = [t for t in db_targets if t in user_agents]
    else:
        resolved_targets = db_targets

    # Self-delegation is always permitted — ensure the agent's own slug appears in
    # the delegate_task roster + DELEGATION_TARGETS env (the tool allows self via
    # its `target_agent != AGENT` bypass; _meeting_context excludes self).
    if agent_name not in resolved_targets:
        resolved_targets = [agent_name] + resolved_targets

    # Per-session ctx for ${session.*} resolution in MCP agent_env declarations
    session_task_owner = user_sub_for_creds if identity.scope == "user" else ""
    session_task_username = task_username if (session_task_owner and task_username) else ""

    # Resolve the execution target + placement facts BEFORE the MCP config /
    # prompt / path_env below, so device-local MCPs (computer / browser / app
    # control) attach only on a satellite that grants the capability. A task
    # can drive an unattended satellite, so this path must thread the flags too.
    # (target_kind/target_label also feed the SecurityContext + AgentConfig.)
    _target_user_sub = user_sub_for_creds
    task_resolved_target = await asyncio.to_thread(
        remote_store.resolve_execution_target, agent_name,
        _target_user_sub, task_role,
    )
    task_target_value = task_resolved_target[0]
    task_target_kind, task_target_label = await asyncio.to_thread(
        remote_store.get_target_metadata, task_target_value,
        _target_user_sub, agent_name,
    )
    is_remote = task_target_kind in ("admin_remote", "user_remote")
    target_has_display = await asyncio.to_thread(
        remote_store.get_target_has_display, task_target_kind, task_target_value,
    )
    target_device_grants = await asyncio.to_thread(
        remote_store.get_target_device_grants, task_target_kind, task_target_value,
    )
    # Satellite path-policy fields for the SecurityContext — without them the
    # Pass-1 path gate treats every satellite-absolute path as outside the
    # synced tree and (with no home_dir and allow_full_fs=False) fail-closes
    # every file access on a remote-targeted run.
    target_path_policy = await asyncio.to_thread(
        remote_store.get_target_path_policy, task_target_kind, task_target_value,
    )

    # Resolve the agent's execution layer BEFORE building the MCP config — Codex
    # needs the MCP config in TOML format (config.toml `[mcp_servers.*]`), NOT the
    # Claude JSON format. Without this the JSON blob was written into config.toml →
    # Codex's strict TOML parser hit it ("invalid statement") → exit 1 / blank
    # terminal. Mirrors core/config/config_builder.py (the chat path).
    agent_info = agent_store.get_agent(agent_name)
    # Delegate-spawn per-lane layer override beats the agent default
    # (validated against the agent's enabled layers at spawn).
    execution_path = (getattr(task, "override_execution_path", None)
                      or (agent_info or {}).get("execution_path", "claude-code-cli"))
    mcp_format = "toml" if execution_path == "codex-cli" else "json"

    mcp_config, credential_env, excluded_mcps, secret_bundles, bash_env_keys = (
        await asyncio.to_thread(
            mcp_registry.build_session_mcp_config,
            agent_name,
            user_sub_for_creds,
            task_mode=True,
            task_scope=identity.scope,
            delegation_targets=resolved_targets,
            mcp_config_format=mcp_format,
            username=task_username,
            user_role=task_role,
            task_owner=session_task_owner,
            task_username=session_task_username,
            is_remote=is_remote,
            target_has_display=target_has_display,
            target_device_grants=target_device_grants,
            target_admin_paired=(task_target_kind == "admin_remote"),
        )
    )

    is_admin_only = agent_store.is_admin_only(agent_name)
    # Shared-only agents always run agent-scoped — already reflected in
    # ``identity.scope`` (resolve_task_identity forces it).

    # Resolve the agent's visibility mode for this task. ``scope_override`` is
    # the run's stored scope; the resolver clamps it to the agent's mode
    # (Shared-only → agent, Personal-only → user) and owns mount scope/username,
    # config visibility, available scopes and memory availability. A task is an
    # unattended session: config_visible only when a real owner-tier creator is
    # the user (preserves user-scope manager tasks; agent-scope tasks get no
    # /config — the admin-only-task guard).
    from core.session.visibility import resolve_visibility
    vis = resolve_visibility(
        agent_name,
        username=task_username or "",
        user_role=task_role or "",
        user_sub=user_sub_for_creds or "",
        scope_override=identity.scope,
    )

    # Inject manifest-declared path_env values. The framework auto-resolves
    # each role to a sandbox-style virtual path based on user/agent scope
    # AND access level (viewer/manager/admin). Multi-value entries
    # (allowlist-style env vars) are joined here; the satellite is told
    # via `multi_value_envs` to split-translate-rejoin.
    # See proxy/services/path_roles.py for full role semantics.
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
                credential_env[env_var] = path_roles.resolve_path_env_entry(
                    decl, username=vis.mount_username, user_role=task_role or "",
                )
            except ValueError as e:
                logger.warning(
                    "path_env injection failed for %s.%s: %s",
                    manifest.name, env_var, e,
                )
                continue
            if decl.is_multi:
                multi_value_envs[env_var] = decl.join

    # Standard OTO_* env vars — same set env_builder injects into the
    # parent process. Baked into credential_env so Codex TOML stdio MCPs
    # get them too (Codex doesn't inherit parent env to MCPs).
    from core.sandbox import oto_env
    credential_env.update(oto_env.build_oto_env(
        agent_name=agent_name,
        username=vis.mount_username,        # MOUNT username ("" for agent scope)
        user_sub=user_sub_for_creds or "",  # REAL creator sub — attribution
        user_role=task_role or "",
        platform_role=task_platform_role,
        session_id=session_id or "",
        memory_user_enabled=vis.memory_user_enabled,
        memory_agent_enabled=vis.memory_agent_enabled,
        default_scope=vis.effective_default_scope,
        # Propagates task.task_type → OTO_TASK_TYPE (generic session
        # metadata any MCP may read to distinguish task shapes).
        task_type=getattr(task, "task_type", "") or "",
        available_scopes=vis.available_scopes,
        force_config=vis.config_visible,
    ))
    multi_value_envs.update(oto_env.OTO_MULTI_VALUE_ENVS)

    # For TOML (Codex): inject the resolved credential env INTO the config.toml
    # env sections (Codex doesn't inherit parent env for MCP servers). Done after
    # path_env + OTO_* are folded into credential_env so they all land in the file.
    # bash-only env_injection (GH_TOKEN/GIT_CONFIG_*) is excluded — it stays in the
    # daemon env for bash, never on disk. Mirrors core/config/config_builder.py.
    if mcp_format == "toml" and mcp_config and credential_env:
        mcp_config = await asyncio.to_thread(
            mcp_registry.inject_credential_env_into_toml,
            mcp_config, credential_env,
            exclude_keys=bash_env_keys,
        )

    # Attached knowledge libraries — same bake-at-build contract as
    # config_builder (fail-safe empty: policy denies mirror writes).
    try:
        from storage import db_knowledge_libraries as _db_kl
        _kl_rows = await asyncio.to_thread(
            _db_kl.attachments_for_consumer, agent_name)
        _knowledge_libraries = tuple(
            (a["source_agent"], a["subdir"] or "", bool(a["writable"]))
            for a in _kl_rows)
    except Exception:
        _knowledge_libraries = ()
    task_security = SecurityContext(
        role=task_role,
        username=task_username,             # REAL creator (attribution/identity)
        agent=agent_name,
        is_admin_agent=is_admin_only,
        target_kind=task_target_kind,
        target_label=task_target_label,
        target_agents_dir=target_path_policy["agents_dir"],
        target_machine_id=target_path_policy["machine_id"],
        target_home_dir=target_path_policy["home_dir"],
        target_allow_full_fs=target_path_policy["allow_full_fs"],
        target_claude_runtime_root=target_path_policy.get("claude_runtime_root", ""),
        target_os_user=target_path_policy["os_user"],
        target_user_dirs=target_path_policy["user_dirs"],
        target_device_grants=target_device_grants,
        session_scope=vis.mount_scope,
        config_visible=vis.config_visible,
        available_scopes=vis.available_scopes,
        knowledge_libraries=_knowledge_libraries,
        knowledge_rw=identity.knowledge_rw,
    )

    # Resolve dynamic MCP context. Pass user_sub + user_role so manifest
    # agent_context blocks can resolve ${account.*}, ${credential.*},
    # ${user.*} tokens for user-scope tasks. Agent-scope tasks pass an
    # empty user_sub and the resolver falls back to each MCP's service
    # account row. trigger_payload threads from
    # trigger_manager → scheduler so webhook-fired tasks resolve
    # ${trigger.*} tokens for builder blocks.
    assigned_mcp_names = [m.name for m in (mcp_registry.get_agent_mcps(
        agent_name, is_remote=is_remote, target_has_display=target_has_display,
        target_device_grants=target_device_grants,
    ) or [])]
    dynamic_contexts = await dynamic_context.get_dynamic_contexts(
        agent_name, assigned_mcp_names,
        user_sub=user_sub_for_creds or "",
        user_role=task_role or "",
        delegation_targets=resolved_targets,
        # Pre-resolved off-loop: the delegation + meetings providers are no-I/O.
        delegation_roster=await asyncio.to_thread(
            dynamic_context.build_delegation_roster, resolved_targets,
        ),
        meetings_access=await asyncio.to_thread(
            dynamic_context.build_meetings_access,
            user_sub_for_creds or "", task_role or "",
        ),
        # Agent-scope runs are no-user sessions — their read reach is the
        # delegation roster (user-scope runs follow the user).
        nouser_reads=not user_sub_for_creds,
        trigger_payload=trigger_payload,
        is_remote=is_remote,
        target_admin_paired=(task_target_kind == "admin_remote"),
        target_os=await asyncio.to_thread(
            remote_store.get_target_os, task_target_kind, task_target_value,
        ),
    )

    # Build system prompt. MOUNT username drives the tree + user-context
    # sections; the real creator identity is rendered from task_security.
    agent_prompt = config.build_agent_prompt(
        agent_name,
        username=vis.mount_username,
        role=task_role,
        excluded_mcps=excluded_mcps or None,
        dynamic_contexts=dynamic_contexts or None,
        sandboxed=True,
        client_type="task",
        is_remote=is_remote,
        target_has_display=target_has_display,
        target_device_grants=target_device_grants,
        mount_shared=vis.mount_shared,
    )
    agent_prompt = (agent_prompt or "") + build_permission_context(
        task_security,
        assigned_mcp_names=tuple(assigned_mcp_names),
        execution_path=execution_path or "",
    )
    agent_prompt = (agent_prompt or "") + _build_task_agent_suffix(execution_path or "")
    agent_prompt += _notification_policy_block(getattr(task, "notification_mode", "manual"))

    # Resolve model and effort from the agent's configured defaults — a
    # delegate-spawn per-lane model override beats the default (validated
    # against the layer's model registry at spawn). Layer-aware: when the
    # run's layer differs from the agent's engine (override_execution_path),
    # the agent-default model may be foreign to it and must not be stamped
    # (the provider 400s every turn).
    resolved_model = (getattr(task, "override_model", None)
                      or config.get_cli_model(agent_name, layer=execution_path))
    resolved_effort = config.get_cli_effort(agent_name)

    # PROXY_TASK_OWNER/USERNAME/SCOPE are now delivered to MCPs via manifest
    # agent_env declarations using ${session.*} tokens (resolved at config-
    # build time above). No direct injection here — declared in:
    #   meetings-mcp, notifications-mcp, schedules-mcp, delegation-mcp manifest.json.
    task_extra_env: dict[str, str] = {}

    # Acquire execution layer subscription (API key, OAuth, or local endpoint)
    # (execution_path already resolved above for sandbox rewrite gating)
    subscription_id = ""
    # User-scoped tasks prefer the creator's subscription; agent-scoped use platform pool
    sub_user = user_sub_for_creds

    # Prepare the persistent config dir — .codex/ for Codex, .claude/ otherwise.
    # The codex layer reads ``config.sandbox_host_claude_dir`` AS its CODEX_HOME;
    # a Codex task whose config landed in .claude/ ran against a missing .codex
    # config and hung at init. Single source of truth (can't drift across the
    # session-config builders): core.sandbox.sandbox.ensure_persistent_agent_dir. MOUNT
    # scope + username from the resolver (agent scope for Shared-only/internal).
    # Computed BEFORE the subscription resolve — the dir keys the scope-sticky
    # acquisition below.
    from core.sandbox.sandbox import ensure_persistent_agent_dir
    host_claude_dir = await asyncio.to_thread(
        ensure_persistent_agent_dir,
        agent_name,
        execution_path=execution_path,
        username=vis.mount_username,
        scope=vis.mount_scope,
    )

    try:
        subscription_id, sub_env = await asyncio.to_thread(
            subscription_pool.resolve_subscription_env,
            execution_path, sub_user,
            model=resolved_model, agent_info=agent_info,
            # Scope-sticky: sessions sharing this credential dir must run on
            # ONE account (same key the layer stamps at bind_session).
            sticky_scope=subscription_pool.credential_scope_key(
                task_target_value, str(host_claude_dir)),
        )
        task_extra_env.update(sub_env)
    except subscription_pool.NoSubscriptionError:
        raise
    except Exception as e:
        logger.warning(f"Subscription pool error for task on {agent_name}: {e}")
    # User-scoped task with no resolved credentials → surface a clean block (the
    # task runner marks the run failed with this reason) rather than a cryptic
    # mid-run provider error. Agent-scoped tasks use the platform pool as before.
    if sub_user and not subscription_id:
        raise subscription_pool.NoSubscriptionError(
            subscription_pool.user_scope_block_reason(execution_path, sub_user)
        )

    # Execution-mode resolver: an autonomous task runs
    # as the native interactive TUI — instead of headless -p — when the global
    # kill-switch is on AND the agent's default mode is interactive AND it's a
    # Claude/Codex session. Anything else → -p. Meetings never reach here (forced
    # -p separately). The scheduler clears this for a continue_session resume
    # (interactive resume/delegation) and drives the prompt injection +
    # completion watcher; the frontend (TaskRunView) already renders it.
    #
    # Remote interactive tasks: a task can run interactive on a satellite too
    # (Claude via transcript forwarding, Codex via rollout forwarding) — but ONLY
    # when that satellite advertises the interactive_pty capability (else the
    # pty_open would hang), mirroring the chat gate
    # (ws/dashboard._resolve_session_interactive).
    # The user_remote (user-scoped only) / admin_remote (all scopes) routing was
    # ALREADY decided by resolve_execution_target above (task_target_value); this
    # only chooses interactive-vs-headless on that resolved target.
    from core import execution_mode
    agent_default_mode = (agent_info or {}).get("default_execution_mode", "") or ""
    _mode_override = getattr(task, "override_execution_mode", None)
    if _mode_override:
        # Delegate-spawn per-lane mode override — a one-line substitution
        # into the existing viewer-less interactive worker path; the CLI-only
        # gate and the remote PTY-support gate below still apply.
        task_interactive = (
            _mode_override == "interactive"
            and (execution_path or "") in ("claude-code-cli", "codex-cli")
        )
    else:
        task_interactive = (
            execution_mode.is_interactive(agent_default=agent_default_mode)
            and (execution_path or "") in ("claude-code-cli", "codex-cli")
        )
    if task_interactive and is_remote:
        from core.remote.satellite_connection import get_connection_manager
        task_interactive = get_connection_manager().satellite_supports_pty(
            task_target_value
        )

    return AgentConfig(
        agent_name=agent_name,
        # Task creator — drives MCP-install progress delivery to their
        # TaskRunView (install_registry participant). "" for system tasks.
        user_sub=task.created_by or "",
        system_prompt=agent_prompt,
        mcp_config_path=str(mcp_config) if mcp_config else "",
        credential_env=credential_env or {},
        mcp_secret_bundles=secret_bundles or {},
        permission_mode="auto",
        client_type="task",
        model=resolved_model,
        effort=resolved_effort,
        resume=False,  # Only True for continue_session (set by scheduler)
        extra_env=task_extra_env,
        security_context=task_security,
        subscription_id=subscription_id,
        subscription_user_sub=sub_user or "",
        sandbox_host_claude_dir=str(host_claude_dir),
        multi_value_envs=multi_value_envs,
        execution_target=task_target_value,
        execution_path=execution_path,
        interactive=task_interactive,
        default_execution_mode=agent_default_mode,
    )


async def build_delivery_security_context(
    agent_name: str, *, user_sub: str | None, role: str, target: str,
) -> SecurityContext:
    """SecurityContext for a server-driven one-shot resume of an existing
    chat session (delegate-result delivery).

    Close/reap deliberately drops a session's persisted security context
    (JWT-replay defense), so a one-shot resume without a rebuilt context
    would have every hook fail closed — the whole callback turn runs with
    "Session is no longer active" tool denials. Mirrors the target +
    visibility resolution the task/chat config builders perform for the
    delivering identity (the delegating chat's owner, or the shared agent
    identity for agent-scope callbacks).
    """
    from storage.db_users import get_username_by_sub
    username = (get_username_by_sub(user_sub) or "") if user_sub else ""
    target_kind, target_label = await asyncio.to_thread(
        remote_store.get_target_metadata, target, user_sub, agent_name,
    )
    target_device_grants = await asyncio.to_thread(
        remote_store.get_target_device_grants, target_kind, target,
    )
    target_path_policy = await asyncio.to_thread(
        remote_store.get_target_path_policy, target_kind, target,
    )
    from core.session.visibility import resolve_visibility
    vis = resolve_visibility(
        agent_name,
        username=username,
        user_role=role or "",
        user_sub=user_sub or "",
        scope_override="user" if user_sub else "agent",
    )
    try:
        from storage import db_knowledge_libraries as _db_kl
        _kl_rows = await asyncio.to_thread(
            _db_kl.attachments_for_consumer, agent_name)
        _knowledge_libraries = tuple(
            (a["source_agent"], a["subdir"] or "", bool(a["writable"]))
            for a in _kl_rows)
    except Exception:
        _knowledge_libraries = ()
    return SecurityContext(
        role=role or "manager",
        username=username,
        agent=agent_name,
        is_admin_agent=agent_store.is_admin_only(agent_name),
        target_kind=target_kind,
        target_label=target_label,
        target_agents_dir=target_path_policy["agents_dir"],
        target_machine_id=target_path_policy["machine_id"],
        target_home_dir=target_path_policy["home_dir"],
        target_allow_full_fs=target_path_policy["allow_full_fs"],
        target_claude_runtime_root=target_path_policy.get("claude_runtime_root", ""),
        target_os_user=target_path_policy["os_user"],
        target_user_dirs=target_path_policy["user_dirs"],
        target_device_grants=target_device_grants,
        session_scope=vis.mount_scope,
        config_visible=vis.config_visible,
        available_scopes=vis.available_scopes,
        knowledge_libraries=_knowledge_libraries,
    )
