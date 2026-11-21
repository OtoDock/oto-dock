"""CLIExecutionLayer: the Claude Code engine's facts.

What a session of this engine starts with on a machine (``prepare_spawn``),
its files, credentials and usage windows, and the translation of a
ClaudeStreamChunk to CommonEvents. The platform holds no session of it:
the remote layer does, through this engine's adapter.
"""

import asyncio
import contextlib
import json
import logging
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import config as app_config
from core import file_commit, layout, placement
from core.engine import Engine
from core.events import tool_roles
from core.events.common_events import (
    BG_COMMAND_END,
    BG_COMMAND_START,
    CONTEXT_COMPACT,
    DELEGATE_SPAWN,
    DONE,
    ERROR,
    METADATA,
    PERMISSION_REQUEST,
    PLAN_MODE,
    SUBAGENT_END,
    SUBAGENT_START,
    SYSTEM,
    TEXT,
    THINKING,
    TODO_UPDATE,
    TOOL_INPUT,
    TOOL_RESULT,
    TOOL_USE,
    WORKFLOW_END,
    WORKFLOW_PROGRESS,
    WORKFLOW_START,
    CommonEvent,
)
from core.execution_layer import (
    PERMISSION_MODES,
    AgentConfig,
    AuthProfile,
    BehaviourProfile,
    CredentialFileSpec,
    EngineIdentity,
    InBandSpec,
    LayerCapabilities,
    ModelPolicy,
    OAuthRefresh,
    ResumeVerdict,
    RuntimeProfile,
    SessionHomeSpec,
    SpawnPlan,
    SubscriptionHandle,
    UsageProfile,
    Windows,
    WindowSpec,
)
from core.layers.cli import oauth as claude_oauth, usage as claude_usage
from core.layers.cli.helpers import ClaudeStreamChunk
from core.layers.cli.spawn import ClaudeSpawn
from core.permission_modes import CLI_AUTO_WORD
from core.session.external_identity import external_home_of, is_external_ctx
from storage.pg import run_db

if TYPE_CHECKING:
    from auth.path_policy import SecurityContext
    from core.sandbox.sandbox import SandboxBuilder

logger = logging.getLogger("otodock")


# ---------------------------------------------------------------------------
# ClaudeStreamChunk → CommonEvent translator
# ---------------------------------------------------------------------------


def cli_chunk_to_events(chunk: ClaudeStreamChunk) -> list[CommonEvent]:
    """Translate a single ClaudeStreamChunk into one or more CommonEvents.

    A chunk can carry both an event_type AND is_done/is_error flags,
    so we may yield multiple events from a single chunk.
    """
    events: list[CommonEvent] = []

    et = chunk.event_type

    # CLI-synthesized interrupt noise: on a graceful interrupt the CLI can
    # emit a "[ede_diagnostic] result_type=… stop_reason=…" marker — as
    # assistant text (invisible before N1 — the hard kill never persisted
    # partial turns), or as an is_error RESULT when the interrupt lands
    # mid-tool-use (stop_reason=tool_use; live-hit 2026-08-11: a duplex
    # barge-in during a web search rendered "Error: [ede_diagnostic] …" in
    # the chat). Never user-facing content; drop it on BOTH paths.
    _ede_noise = bool(chunk.text) and chunk.text.lstrip().startswith("[ede_diagnostic]")

    if et == "text" and (chunk.text or chunk.event_data.get("note")):
        # A note chunk with no text is the note mark (the translator's).
        if not _ede_noise:
            events.append(CommonEvent(type=TEXT, data={"content": chunk.text, **(
                {"note": True} if chunk.event_data.get("note") else {})}))

    elif et == "thinking":
        events.append(CommonEvent(type=THINKING, data=chunk.event_data))

    elif et == "tool_start":
        events.append(CommonEvent(type=TOOL_USE, data=chunk.event_data))

    elif et == "tool_info":
        events.append(CommonEvent(type=TOOL_INPUT, data=chunk.event_data))
        # Emit TODO_UPDATE for TodoWrite tool (cross-layer abstraction)
        if chunk.event_data.get("name") == "TodoWrite":
            ti = chunk.event_data.get("tool_input") or {}
            if "todos" in ti:
                events.append(CommonEvent(type=TODO_UPDATE, data={"todos": ti["todos"]}))

    elif et == "todo_update":
        # Translator-maintained checklist snapshot (the TaskCreate/TaskUpdate
        # harness that replaced TodoWrite — see translator._cc_tasks).
        events.append(CommonEvent(type=TODO_UPDATE, data=chunk.event_data))

    elif et == "tool_end":
        events.append(CommonEvent(type=TOOL_RESULT, data=chunk.event_data))

    elif et == "task_spawn":
        events.append(CommonEvent(type=SUBAGENT_START, data=chunk.event_data))

    elif et == "subagent_end":
        events.append(CommonEvent(type=SUBAGENT_END, data=chunk.event_data))

    elif et == "bg_command_start":
        events.append(CommonEvent(type=BG_COMMAND_START, data=chunk.event_data))

    elif et == "bg_command_end":
        events.append(CommonEvent(type=BG_COMMAND_END, data=chunk.event_data))

    elif et == "workflow_started":
        events.append(CommonEvent(type=WORKFLOW_START, data=chunk.event_data))

    elif et == "workflow_progress":
        events.append(CommonEvent(type=WORKFLOW_PROGRESS, data=chunk.event_data))

    elif et == "workflow_ended":
        events.append(CommonEvent(type=WORKFLOW_END, data=chunk.event_data))

    elif et == "delegate_spawn":
        events.append(CommonEvent(type=DELEGATE_SPAWN, data=chunk.event_data))

    elif et == "plan_mode":
        events.append(CommonEvent(type=PLAN_MODE, data=chunk.event_data))

    elif et == "permission_prompt":
        # The raising session's id: a meeting participant's request rides
        # the meeting's pump, whose own id names no session.
        events.append(CommonEvent(type=PERMISSION_REQUEST, data={
            **chunk.event_data, "session_id": chunk.session_id or ""}))

    elif et == "system":
        subtype = chunk.event_data.get("subtype", "")
        # Translate compaction system events to first-class CONTEXT_COMPACT
        if subtype == "compacting":
            events.append(CommonEvent(type=CONTEXT_COMPACT, data={
                "phase": "started", "trigger": "auto",
            }))
        elif subtype == "compact_boundary":
            meta = chunk.event_data.get("compact_metadata") or {}
            events.append(CommonEvent(type=CONTEXT_COMPACT, data={
                "phase": "completed",
                "trigger": meta.get("trigger", "auto"),
                "pre_tokens": meta.get("pre_tokens"),
                "post_tokens": None,
                "messages_summarized": meta.get("messages_summarized"),
            }))
        else:
            events.append(CommonEvent(type=SYSTEM, data=chunk.event_data))

    elif et == "metadata":
        events.append(CommonEvent(type=METADATA, data=chunk.event_data))

    # Error flag — can coexist with any event type. A typed ending (a decline
    # or a usage limit) rides its ERROR with no TEXT twin: the pump persists
    # its line once.
    if chunk.is_error and chunk.text and not _ede_noise:
        data: dict = {"message": chunk.text}
        if et == "turn_ending":
            data["ending"] = dict(chunk.event_data)
        events.append(CommonEvent(type=ERROR, data=data))

    # Done flag — turn boundary, always last
    if chunk.is_done:
        events.append(CommonEvent(type=DONE, data={}))

    return events


# ---------------------------------------------------------------------------
# CLI layer capabilities
# ---------------------------------------------------------------------------

_CLI_CAPABILITIES = LayerCapabilities(
    name="claude-code-cli",
    display_name="Claude Code CLI",
    supports_resume=True,
    supports_permissions=True,
    supports_plan_mode=True,
    supports_todos=True,
    supports_subagents=True,
    supports_context_compression=True,
    supports_control_commands=True,
    supports_mcps=True,
    permission_modes=list(PERMISSION_MODES),
    control_commands=["set_model", "set_permission_mode"],
    models=app_config.get_layer_models("claude-code-cli"),
    effort_levels=["low", "medium", "high", "xhigh", "max"],
    effort_changeable_mid_session=False,
    compression_threshold_pct=83,
    mcp_delivery="external_config",
    mcp_config_format="json",
    providers=[
        # `claude --effort` takes the platform ladder as is (spawn.py,
        # ClaudeSpawn._build_persistent_cmd); xhigh only on the models whose row says so,
        # else the CLI gets max.
        {"id": "anthropic", "label": "Anthropic", "requires_key": True,
         "kind": "vendor", "relay_path": "", "api_path": "",
         "effort_scale": ["low", "medium", "high", "xhigh", "max"],
         "effort_per_model": ["xhigh"]},
    ],
    identity=EngineIdentity(
        short_name="claude",
        vendor_id="anthropic",
        vendor_label="Anthropic",
        account_label="Claude",
        role="coding",
        sort_order=10,
    ),
    runtime=RuntimeProfile(
        has_os_process=True,                  # one `claude -p` process per session
        hard_abort_kills_process=True,        # SIGTERM the process group; the session goes cli_dead
        supports_remote_execution=True,
        supports_interactive_pty=True,        # the native Ink TUI under a PTY
        interactive_first_prompt_via_argv=False,   # cold prompt is a PTY write + Enter
        supports_reattach_after_restart=True, # Mode C: a satellite keeps the in-flight turn
        readopts_idle_session=True,           # an idle session the satellite kept is taken back
        binary="claude",
        pin_key="claude_code",                # cli_pins wire key — frozen
        config_dir_name=".claude",            # CLAUDE_CONFIG_DIR under the session's scope root
        self_wakes=True,                      # ≥ 2.1.243 runs a turn of its own after a bg completion
        event_queue_depth=1000,               # a remote session's proxy-side event queue
        interactive_submit_backstop=True,     # the Ink TUI under ConPTY swallows the first Enter
        interactive_reports_model=True,       # a /model command, then the next turn's answer names it
        installed_name="claude-code",         # installed_clis wire value — frozen
    ),
    behaviour=BehaviourProfile(
        rebuilds_history_from_db=False,       # the CLI owns its JSONL history (--resume)
        attach_images_inline=False,           # photos ride as sandbox paths its Read tool opens
        skills_delivery="materialized_dir",   # the Skill tool indexes .claude/skills/
        supports_bash=True,
        supports_plans_dir=True,              # ~/.claude/plans/ (EnterPlanMode)
        builtin_file_tools=False,             # its own Read/Write/Edit, not the platform's
        has_shell_on_external_route=True,     # the hook floor, the argv and the settings deny carve the shell tools off per session
        hides_denied_tools=True,              # the functions a limit leaves out are not offered (tool_limits)
        provider_pinned_per_session=False,    # single provider
        supports_steer=True,                  # user frame into the live turn's stdin
        supports_compact=False,               # stream-json runs no /compact (tested on 2.1.201)
        supports_interrupt_for_queued=True,   # control_request {interrupt}
        supports_background_commands=True,  # Bash run_in_background
        supports_step_descriptions=True,  # the Bash and Task description
        # The CLI writes every cache entry for one hour (ephemeral_1h; its status line
        # reports ttl "1h"), so an hour idle is a cold start on any account.
        prompt_cache_s=3600,
        # The CLI's own tool names, by role — Claude's ARE the platform's
        # canonical names (core/events/tool_roles), so canonical_tool_name is
        # identity here. The shell and write rows are what an external
        # session and a judge session may not have (two layers: the hook
        # floor by role and --disallowedTools; never the scope's shared
        # settings.json, see config_dir.build_settings).
        tools={
            "shell": ("Bash", "Monitor", "PowerShell"),
            "read": ("Read",),
            "glob": ("Glob",),
            "search": ("Grep",),
            "write": ("Write", "Edit", "MultiEdit", "NotebookEdit"),
            "web_fetch": ("WebFetch",),
            "web_search": ("WebSearch",),
            "subagent": ("Agent", "Task"),
            "todo": ("TodoWrite", "TodoRead"),
            "task_read": ("TaskGet", "TaskList", "TaskOutput"),
            "task_write": ("TaskCreate", "TaskUpdate", "TaskStop"),
            "discovery": ("ToolSearch",),
            "skill": ("Skill",),
            "workflow": ("Workflow",),
            "plan_enter": ("EnterPlanMode",),
            "plan_exit": ("ExitPlanMode",),
            "question": ("AskUserQuestion",),
        },
        # Under --setting-sources user the CLI reads no CLAUDE.md or AGENTS.md
        # of a repository below its working folder (probed on 2.1.289).
        repository_instruction_files=("CLAUDE.md", "AGENTS.md"),
        start_context_door="message",
    ),
    model_policy=ModelPolicy(
        default_model="claude-opus-5-5",      # tier 2: the frontier model is a deliberate choice
        model_filter_policy="none",           # a personal Claude account sees every builtin
        pricing_editable=False,               # registry-priced; the vendor bills the subscription
    ),
    auth=AuthProfile(
        auth_types=("oauth", "api_key"),      # a Claude login, or an Anthropic key
        credential_file=CredentialFileSpec(   # the satellite clamp's row — frozen
            wire_kind="claude", dirname=".claude", filename=".credentials.json",
        ),
        oauth_flow="code_paste",              # the popup shows a code the user pastes back
        sign_in_in_place=True,                # a rewritten .credentials.json is read at the next call
        session_home=SessionHomeSpec(
            # The conversations, the plans and the store's skills stay the
            # scope's (the platform's readers and a resume find them there);
            # the sign-in, the account record and the settings are the
            # session's. Proved on 2.1.322: two live sessions of one store
            # on two accounts, and a third home resuming the first's chat.
            links=("projects", "tasks", "todos", "file-history", "plans", "paste-cache",
                   "skills", "commands", "agents", "output-styles", "plugins"),
            own_files=(".credentials.json", ".claude.json", "settings.json"),
            seed=((".claude.json", ("oauthAccount",)),),
        ),
    ),
    usage=UsageProfile(
        windows=(
            # Anthropic's consumer windows, as /api/oauth/usage names them.
            WindowSpec(key="five_hour", length_s=5 * 3600, role="session", label="session"),
            WindowSpec(key="seven_day", length_s=7 * 86400, role="quota", label="weekly"),
            # The weekly window per model family (Fable, Opus, ...): readings
            # file each under its family (usage_scope_key).
            WindowSpec(key="seven_day_model", length_s=7 * 86400, role="quota",
                       label="weekly for one model", scope="model"),
        ),
        in_band=(
            # The headless stream's rate_limit_event, read from the CLI's own
            # output after every answer of the model.
            InBandSpec(kind="headless", source="claude_event", trusted=True),
            # A terminal's status line command (hooks/usage_report.py), which
            # the agent could run itself: a hint.
            InBandSpec(kind="terminal", source="claude_statusline", trusted=False),
        ),
    ),
)


def _real_dir(path: Path) -> bool:
    """A folder itself, never a link to one."""
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# CLIExecutionLayer
# ---------------------------------------------------------------------------


@dataclass
class _Prepared:
    """What a start readies before its spawn: the parts of the plan a
    machine spawns from (``prepare_spawn``)."""
    sandbox_builder: "SandboxBuilder"
    mcp_path: Path | None
    extra_env: dict[str, str]
    disallowed_tools: list[str] | None
    credential_file: bool
    mcp_names: tuple[str, ...]


#: A store's settings are copied into a home whole, at most this much.
_STORE_SETTINGS_MAX_BYTES = 1024 * 1024


def _store_settings(store: Path, config) -> bytes:
    """The scope's settings bytes a home copies: read beneath the agent's
    real folder with no link followed (``core.agent_folder``); a store in no
    agent's folder (a test's) is read as named."""
    from core import agent_folder
    from services.infra import safe_fs
    path = store / "settings.json"
    try:
        root, rel = agent_folder.beneath_agent(path, getattr(config, "agent_name", "") or "")
    except safe_fs.EscapeRefused:
        return path.read_bytes()
    return safe_fs.read_bytes_beneath(root, rel, max_size=_STORE_SETTINGS_MAX_BYTES)


class CLIExecutionLayer(Engine):
    """The Claude Code CLI as an engine: its facts, and the spawn plan a
    machine starts a session of it from."""


    async def _prepare(self, session_id: str, config: AgentConfig) -> "_Prepared":
        """Everything a start readies before its spawn: the egress and
        mount resolve, the credential file, the token files, the MCP
        config copy, the SSH keys, the broker."""
        # Fail CLOSED: a session of the platform's place MUST run sandboxed +
        # network-isolated. An empty sandbox dir means the config builder
        # omitted it — refuse rather than plan an un-sandboxed start.
        if not config.sandbox_host_claude_dir:
            raise RuntimeError(
                f"refusing to plan a Claude Code session for agent "
                f"'{config.agent_name}' without a sandbox dir: every session "
                f"runs sandboxed and network-isolated."
            )
        # Below the editor tier a session never runs from the agent's own CLI
        # state (its sandbox masks it: the CLI would start with no hooks).
        from core.sandbox.session_config_dir import refuse_session_on_agent_state
        refuse_session_on_agent_state(config.security_context)
        mcp_path = Path(config.mcp_config_path) if config.mcp_config_path else None

        # The sandbox's description (every session is sandboxed).
        extra_env = dict(config.extra_env) if config.extra_env else {}
        sandbox_builder = None

        from core.sandbox.sandbox import (
            SandboxBuilder,
            mcp_sandbox_mounts,
            own_engine_home,
            resolve_sandbox_config,
        )
        from core.sandbox.sandbox_render import cli_install_ro_binds
        from services.mcp import mcp_registry as mcp_reg

        ctx = cast("SecurityContext | None", config.security_context)

        # The MCPs the session's build attached (the assigned set minus what
        # it left out, ``AgentConfig.excluded_mcps``): their manifest mounts
        # and their network routes, nothing of a tool the session was
        # refused. is_remote=False (fail-closed default, explicit here): this
        # is the sandbox's mount path, so satellite_only device MCPs must
        # never appear — they run native on a paired computer.
        attached = [
            m for m in (await run_db(mcp_reg.get_agent_mcps, config.agent_name,
                                     placement=placement.LOCAL_PLACEMENT) or [])
            if m.name not in (config.excluded_mcps or ())
        ]
        mcp_mounts = mcp_sandbox_mounts(attached)

        sandbox_cfg = await run_db(
            resolve_sandbox_config,
            role=ctx.role if ctx else "viewer",
            username=ctx.mount_username if ctx else "",
            agent_name=config.agent_name,
            is_admin_agent=ctx.is_admin_agent if ctx else False,
            host_claude_dir=Path(config.sandbox_host_claude_dir),
            user_sub=config.user_sub,
            mcp_sandbox_mounts=mcp_mounts,
            account_choices=config.account_choices or None,
            # A claude installed outside the system mounts (user-prefix npm,
            # e.g. ~/.npm-global on a native install) must be mounted into the
            # sandbox or bwrap can't exec it — mirrors the Codex layer's
            # long-standing treatment of CODEX_BIN.
            extra_ro_binds=cli_install_ro_binds(app_config.CLAUDE_BIN),
            config_visible=ctx.config_visible if ctx else None,
            mount_shared=ctx.mount_shared if ctx else True,
            knowledge_rw=bool(getattr(ctx, "knowledge_rw", False)) if ctx else False,
            # The PRE-sandbox build file (sessions/) still carries host
            # mcps/ paths — scanned for the per-MCP dir binds.
            mcp_config_path=config.mcp_config_path,
            # External caller (not a platform user): shared memory masked,
            # their own tree at /caller when the route gave them one.
            external=is_external_ctx(ctx),
            external_home=external_home_of(ctx),
            read_only=bool(getattr(ctx, "read_only", False)),
            attached_mcps=[m.name for m in attached],
            outside_files_write=bool(getattr(ctx, "outside_files_write", False)),
            outside_knowledge=tuple(getattr(ctx, "outside_knowledge", ()) or ()),
            # The wrap-up after a conversation: the person's tree, and its
            # engine state in the run's own scratch home (the config dir's).
            wrapup_home=getattr(ctx, "wrapup_home", "") if getattr(ctx, "wrapup", "") else None,
            engine_home=own_engine_home(ctx, config.sandbox_host_claude_dir),
            # The session's own home, bound in over the masked homes
            # (``session_config_dir.session_home_for``).
            session_home=config.session_home,
            # A compute machine on another server opens no tool's local
            # address: its network is not the platform's.
            own_copy=bool(getattr(getattr(ctx, "placement", None), "own_copy", False)),
        )
        sandbox_builder = SandboxBuilder(sandbox_cfg)
        if config.session_home:
            await asyncio.to_thread(self._make_home, session_id, config)
        # External sessions never get a shell — the CLI argv is one of the
        # two layers (the hook floor by role is the other; the names are
        # this engine's ``behaviour.tools``). A judge session (CHECKS.md)
        # gets no write tools the same two ways.
        # Off the loop: the first read of the tools' labels loads the table.
        disallowed_tools = await asyncio.to_thread(
            self.session_denied_tools,
            external=is_external_ctx(ctx), read_only=bool(getattr(ctx, "read_only", False)),
            ctx=ctx,
        ) or None

        # Get the sandbox-internal .claude/ path from env overrides
        sandbox_claude_dir = sandbox_builder.get_env_overrides().get(
            "CLAUDE_CONFIG_DIR", "/workspace/.claude"
        )

        # OAuth file delivery: write the session's .credentials.json into the
        # scope config dir (CLAUDE_CONFIG_DIR). The CLI re-reads this file
        # (mtime-watch + 401-recovery), which is what lets the pool rotate the
        # token under a LIVE process — env delivery is frozen at exec and
        # outranks the file, so the payload must never reach the child env
        # (credential_file_from_env pops it).
        credentials = self.credential_file_from_env(extra_env)
        if credentials is not None:
            from services.engines.token_fanout import write_credential_file
            write_credential_file(
                Path(config.session_home or config.sandbox_host_claude_dir),
                cast("CredentialFileSpec", self.capabilities.auth.credential_file).filename, credentials,
            )

        # Copy MCP config into .claude/ dir (avoids mounting proxy/sessions/)
        if mcp_path:
            from core.sandbox.sandbox import prepare_mcp_config_for_sandbox
            sandbox_mcp_path = prepare_mcp_config_for_sandbox(
                mcp_path, config.sandbox_host_claude_dir,
                sandbox_config_dir=sandbox_claude_dir,
                session_id=session_id,
                secret_bundles=config.mcp_secret_bundles,
                agent_name=config.agent_name,
            )
            mcp_path = Path(sandbox_mcp_path) if sandbox_mcp_path else None

        # ssh-hosts: provision the agent's authorized SSH keys into
        # <.claude>/ssh so the prompt's ready-to-run `ssh -i
        # "$OTO_SSH_KEY_DIR/…"` lines work from bash. Independent of
        # mcp_path: the keys are the session's, not the config file's. A
        # session that takes no keys is left none from an earlier one (the
        # helper's rule).
        from core.sandbox.session_config_dir import provision_ssh_keys_for_sandbox
        ssh_dir = await provision_ssh_keys_for_sandbox(
            ctx, config.agent_name, config.sandbox_host_claude_dir, sandbox_claude_dir,
        )
        if ssh_dir:
            extra_env["OTO_SSH_KEY_DIR"] = ssh_dir

        # The credential exit and the SSH agent socket: the
        # exit's variables and trust bundle for the session's clients and
        # its port on the description; the signer's socket on the
        # description and in the env. On a compute machine the exit is that
        # machine's own (``machine=``), and the satellite opens the agent
        # socket itself from the description's name.
        from core.credentials import credential_exit, ssh_signer
        machine = placement.compute_of(getattr(ctx, "placement", None))
        await credential_exit.attach_sandbox(session_id, sandbox_builder, extra_env,
                                             config.sandbox_host_claude_dir,
                                             sandbox_config_dir=sandbox_claude_dir,
                                             machine=machine)
        await ssh_signer.attach_sandbox(session_id, ctx, sandbox_builder, extra_env, compute=machine is not None)

        # Credential broker: populate the per-session secret store so
        # the wrapped stdio MCPs can fetch their secrets at spawn. Must precede
        # the CLI launch, which spawns the MCP children.
        from core.credentials import mcp_broker
        mcp_broker.provision(session_id, config.mcp_secret_bundles or {})
        return _Prepared(
            sandbox_builder=sandbox_builder, mcp_path=mcp_path, extra_env=extra_env,
            disallowed_tools=disallowed_tools, credential_file=credentials is not None,
            mcp_names=tuple(m.name for m in attached),
        )

    def _make_home(self, session_id: str, config: AgentConfig) -> None:
        """The session's home in its scope's folder on the platform
        (``session_home``), which a compute machine beside it shares: the
        links to the scope's folders, the account record seeded, and the
        scope's settings with the same bytes (the hooks they name stay in
        the scope's folder). A compute machine with its own copy gets the
        home's files and makes the links itself."""
        from core.sandbox import session_config_dir
        spec = cast("SessionHomeSpec", self.capabilities.auth.session_home)
        store = Path(config.sandbox_host_claude_dir)
        home = session_config_dir.make_session_home(store, session_id, spec)
        with contextlib.suppress(FileNotFoundError):
            session_config_dir.write_if_changed(home / "settings.json", _store_settings(store, config))

    async def prepare_spawn(self, session_id: str, config: AgentConfig) -> SpawnPlan:
        """The session's spawn for a machine: the preparation, then the
        argv and the environment ``build_spawn_command`` assembles. The
        machine renders the sandbox around them itself, from the
        description. The host config dir is recorded here: the machine's
        session writes its files there through the shared folder."""
        ready = await self._prepare(session_id, config)
        builder = self._persistent_builder(session_id, config, ready, interactive=config.interactive)
        argv, proc_env, _cwd = await run_db(builder.build_spawn_command)
        if config.session_home:
            # The engine reads its home; everything else (the hooks, the
            # prompt, the tools' config) stays named by the scope's folder.
            from core.sandbox import session_home
            proc_env["CLAUDE_CONFIG_DIR"] = (
                f"{proc_env['CLAUDE_CONFIG_DIR']}/{session_home.relative(session_id)}")
        if config.interactive:
            await file_commit.run(self._seed_interactive, config, ready.sandbox_builder)
        desc = ready.sandbox_builder.describe()
        # The binary by its bare name: the machine resolves its own pinned install.
        argv[0] = Path(argv[0]).name
        from core.sandbox.session_config_dir import agent_relative
        from core.session.session_state import set_session_claude_dir
        set_session_claude_dir(session_id, config.sandbox_host_claude_dir)
        return SpawnPlan(
            sandbox=desc.to_dict(), argv=argv, env=proc_env, cwd=desc.cwd,
            config_dir=config.sandbox_host_claude_dir,
            config_dir_relative=agent_relative(config.agent_name, config.sandbox_host_claude_dir),
            prompt_file=str(builder.prompt_file) if builder.prompt_file else "",
            credential_file=ready.credential_file,
            mcp_names=frozenset(ready.mcp_names),
        )

    @staticmethod
    def _persistent_builder(session_id: str, config: AgentConfig, ready: "_Prepared", *,
                            interactive: bool) -> ClaudeSpawn:
        """What assembles the session's argv and env for the spawn plan
        (a headless session's and a terminal's)."""
        return ClaudeSpawn(
            session_id=session_id,
            agent_prompt=config.system_prompt,
            mcp_config_path=ready.mcp_path,
            permission_mode=config.permission_mode,
            client_type=config.client_type,
            resume=config.resume,
            use_native_permissions=config.use_native_permissions,
            model=config.model,
            effort=config.effort,
            extra_env=ready.extra_env or None,
            credential_env=config.credential_env or None,
            sandbox_builder=ready.sandbox_builder,
            agent_name=config.agent_name,
            interactive=interactive,
            disallowed_tools=ready.disallowed_tools,
        )

    @staticmethod
    def _seed_interactive(config: AgentConfig, sandbox_builder: "SandboxBuilder") -> None:
        """Seed the CLI config so the TUI launches past the first-run wizard
        (theme picker, login, trust) and authenticates through the env OAuth
        token like headless; ``CLAUDE_CONFIG_DIR`` maps to the host dir."""
        if sandbox_builder and config.sandbox_host_claude_dir:
            from services.engines.cli_settings_manager import (
                seed_interactive_cli_config,
            )
            seed_interactive_cli_config(
                config.session_home or config.sandbox_host_claude_dir,
                sandbox_builder.get_cwd(),
                theme=config.interactive_theme or "dark",
            )


    def pinned_cli_version(self) -> str:
        return app_config.PINNED_CLAUDE_CODE_VERSION

    def session_denied_tools(self, *, external: bool, read_only: bool, ctx=None) -> list[str]:
        """The CLI tools a session may not have, from the descriptor: for an
        outside session (a caller who is not a platform user) the shell,
        sub-agents and workflows always, the web tools unless the web was
        picked for outside people and the write tools unless the person's
        files were picked Read and write (``ctx``, the session's security
        context); the functions a tool limit leaves out, for every session;
        the shell and the web of a context that says ``no_shell`` /
        ``no_web``; the write tools of a check's judge. The argv's list; the
        hook floor refuses the same by role and by limit."""
        tools = self.capabilities.behaviour.tools
        denied: list[str] = []
        if external:
            for role in ("shell", "subagent", "workflow"):
                denied.extend(tools.get(role, ()))
            denied.extend(sorted(tool_roles.OUTSIDE_NEVER_NAMES))
            if not getattr(ctx, "outside_web", False):
                denied.extend(tools.get("web_fetch", ()) + tools.get("web_search", ()))
            if not getattr(ctx, "outside_files_write", False):
                denied.extend(tools.get("write", ()))
            # The functions of an attached server the pick leaves out, so the
            # model is not offered what the floor would refuse.
            from services.mcp import mcp_permissions
            for key, names in getattr(ctx, "outside_grants", ()) or ():
                if names is None:
                    continue
                denied.extend(f"mcp__{key}__{fn}" for fn in mcp_permissions.known_tools(key)
                              if fn not in names)
        # The agent's tool limits (services/mcp/tool_limits): the functions
        # of a limited server the session may not use, for every session kind.
        from services.mcp import mcp_permissions
        for key, names in getattr(ctx, "tool_limits", ()) or ():
            denied.extend(f"mcp__{key}__{fn}" for fn in mcp_permissions.known_tools(key) if fn not in names)
        if getattr(ctx, "no_shell", False):
            # A session with no person to ask (the wrap-up after a
            # conversation, the agent's own phone line): no shell, and none
            # of the tools that could carry one or reach another session.
            for role in ("shell", "subagent", "workflow"):
                denied.extend(tools.get(role, ()))
            denied.extend(sorted(tool_roles.OUTSIDE_NEVER_NAMES))
        if getattr(ctx, "no_web", False):
            denied.extend(tools.get("web_fetch", ()) + tools.get("web_search", ()))
        if read_only:
            denied.extend(tools.get("write", ()))
        return list(dict.fromkeys(denied))

    def prepare_config_dir(
        self, agent_name: str, *, username: str = "", scope: str = "user",
        external_home=None, no_shell: bool = False, read_only: bool = False,
        outside_picks=None, kind_rule=None,
    ) -> Path:
        """The ``.claude`` dir: settings.json, the hook scripts, the skills
        dir. ``no_shell`` and ``read_only`` are not written here: the dir is
        the scope's, shared by every session of it and reloaded live by the
        CLI, so a session's own denials ride its argv
        (``session_denied_tools``) and the hook floor. The builder
        (``core/layers/cli/config_dir``) is reached through its module at
        call time (tests patch it there)."""
        from core.layers.cli import config_dir as _cd
        extra: dict = {}
        if external_home:
            extra["external_home"] = external_home
            extra["outside_picks"] = outside_picks
            extra["kind_rule"] = kind_rule
        return _cd.ensure_persistent_claude_dir(
            agent_name, username=username, scope=scope, **extra)

    def chat_session_files(self, home: Path, session_id: str, resume_handle: str) -> list[Path]:
        """The session JSONL of one chat: ``<home>/.claude/projects/*/<sid>.jsonl``
        (every project dir — the munged-cwd name varies, and
        satellite-migrated copies sit in local homes too; found beneath the
        agent's real folder with no link below it,
        ``transcript_tailer.transcript_files``)
        plus the legacy pre-sandbox location under the proxy's own home. The
        caller keeps the id-shape and liveness guards."""
        if not session_id:
            return []
        from core.session import transcript_tailer
        try:
            files = [Path(p) for p in transcript_tailer.transcript_files(str(home / ".claude"), session_id)]
        except OSError:
            files = []
        legacy = (Path.home() / ".claude" / "projects"
                  / str(app_config.AGENTS_DIR).replace("/", "-") / f"{session_id}.jsonl")
        if legacy.is_file():
            files.append(legacy)
        return files

    def iter_session_files(self, home: Path):
        projects = home / ".claude" / "projects"
        for f in projects.glob("*/*.jsonl"):
            yield f, f.stem
        # The ``<id>/`` folder (subagents, tool results) of a transcript an
        # older sweep removed alone.
        for d in projects.glob("*/*"):
            if not d.suffix and not d.with_suffix(".jsonl").exists() and _real_dir(d):
                yield d, d.name

    def session_file_companions(self, path: Path) -> list[Path]:
        """The ``<id>/`` folder beside ``<id>.jsonl``
        (``core/remote/session_files.py`` moves it with the file)."""
        return [path.with_suffix("")] if path.suffix == ".jsonl" else []

    # --- Credentials -------------------------------------------------------

    #: The env entry the pool's login payload rides in from the config builder
    #: to ``start_session`` (popped there — it never reaches the child).
    _CREDENTIALS_ENV = "_CLAUDE_CREDS_BLOB"

    def subscription_env(self, handle: SubscriptionHandle) -> dict[str, str]:
        env: dict[str, str] = {}
        if handle.api_key:
            env["ANTHROPIC_API_KEY"] = handle.api_key
        if handle.oauth_access_token:
            payload = self.credential_file_payload(
                handle.oauth_access_token, handle.oauth_expires_at_ms, handle.credential,
            )
            if payload is not None:
                env[self._CREDENTIALS_ENV] = json.dumps(payload)
        return env

    def credential_file_payload(
        self, access_token: str, expires_at_ms: int, stored: dict,
    ) -> dict | None:
        return claude_oauth.credentials_file(access_token, expires_at_ms, stored)

    def credential_file_from_env(self, env: dict) -> dict | None:
        raw = env.pop(self._CREDENTIALS_ENV, "")
        return json.loads(raw) if raw else None

    def refresh_oauth(self, refresh_token: str, stored: dict) -> OAuthRefresh:
        return claude_oauth.refresh(refresh_token, stored)

    # --- Usage windows -----------------------------------------------------

    def _window_specs(self) -> dict[str, WindowSpec]:
        return {s.key: s for s in self.capabilities.usage.windows}

    def usage_request(self, stored: dict) -> tuple[str, dict] | None:
        return claude_usage.usage_request(stored)

    def parse_usage(self, payload) -> Windows | None:
        return claude_usage.from_usage(payload, self._window_specs())

    def usage_scope_key(self, model: str) -> str:
        return claude_usage.model_family(model)

    def record_usage_event(self, session_id: str, payload) -> None:
        claude_usage.record_event_async(session_id, payload)

    def parse_usage_report(self, payload) -> Windows | None:
        return claude_usage.from_statusline(payload, self._window_specs())

    def terminal_reports_mode(self) -> bool:
        return True

    def hook_can_ask(self) -> bool:
        return True

    def engine_reviews(self, session_id: str, live_mode: str) -> bool:
        """Claude Code's classifier judges a call the hook leaves undecided
        exactly when the CLI reports its auto mode with the call (any
        placement: the hook payload says what the CLI runs now)."""
        return live_mode == CLI_AUTO_WORD

    # --- Capabilities ---

    @property
    def capabilities(self) -> LayerCapabilities:
        return _CLI_CAPABILITIES

    def transcript_tailer(self):
        """The Ink TUI writes its session JSONL under
        ``<CLAUDE_CONFIG_DIR>/projects/``; ``transcript_tailer`` persists it."""
        from core.session import transcript_tailer
        return transcript_tailer

    _remote_adapter = None

    def remote_adapter(self):
        """This engine on a satellite — ``core/layers/cli/remote.py``
        (imported here, not at module level: it needs this module's
        ``cli_chunk_to_events``)."""
        if self._remote_adapter is None:
            from core.layers.cli.remote import ClaudeRemoteAdapter
            self._remote_adapter = ClaudeRemoteAdapter(self)
        return self._remote_adapter

    async def can_resume_session(
        self, session_id: str, *, agent_name: str = "", username: str = "",
        external_home: str = "",
    ) -> ResumeVerdict:
        """Check if session file has conversation data for --resume.

        CLI stores sessions at <CLAUDE_CONFIG_DIR>/projects/<project-hash>/<session_id>.jsonl.
        The file must contain at least one user message to be resumable.

        Checks the session's persistent .claude/ dir first.  After a proxy
        restart the in-memory mapping is lost, so *agent_name* + *username*
        (or an external caller's *external_home*) are used to derive the
        .claude/ path (same logic as ``config_dir.ensure_persistent_claude_dir``).
        Falls back to ~/.claude/ for legacy pre-sandbox sessions. A file
        the disk could not read is ``UNKNOWN`` (the probe helper retries it
        once); a missing or empty one is ``FRESH``.
        """
        from core.session.session_state import get_session_claude_dir

        # 1. In-memory mapping (works when proxy hasn't restarted)
        claude_dir = get_session_claude_dir(session_id) or ""

        # 2. Restart fallback: derive from agent + username / caller tree
        if not claude_dir and agent_name:
            agent_dir = app_config.get_agent_dir(agent_name)
            if external_home:
                claude_dir = str(Path(external_home) / ".claude")
            elif username:
                claude_dir = str(layout.user_dir(agent_dir, username) / ".claude")
            else:
                claude_dir = str(agent_dir / layout.WORKSPACE / ".claude")
        return await file_commit.run_in("session-probes", self._probe_on_disk, claude_dir, session_id,
                                        agent_name)

    @classmethod
    def _probe_on_disk(cls, claude_dir: str, session_id: str, agent: str = "") -> ResumeVerdict:
        """``can_resume_session``'s disk work, in a worker thread: the
        transcript in the session's engine folder, found and read beneath
        the real folder of ``agent`` (``transcript_tailer``), then the legacy file in the
        proxy account's own home. A link or a pipe where the transcript
        should be reads as no transcript; any other disk fault is
        ``UNKNOWN``."""
        from core.session import transcript_tailer
        if claude_dir:
            try:
                session_file = transcript_tailer.find_transcript(claude_dir, session_id, agent)
            except OSError:
                logger.warning("resume probe of %s: its engine folder could not be read",
                               session_id[:8], exc_info=True)
                return ResumeVerdict.UNKNOWN
            if session_file:
                return cls._session_file_has_user_message(Path(session_file), agent)

        # 3. Legacy fallback: ~/.claude/ path (pre-sandbox sessions). No
        # session writes the proxy account's own home.
        project_hash = str(app_config.AGENTS_DIR).replace("/", "-")
        legacy = Path.home() / ".claude" / "projects" / project_hash / f"{session_id}.jsonl"
        if legacy.exists():
            return cls._legacy_file_has_user_message(legacy)

        return ResumeVerdict.FRESH

    @staticmethod
    def _session_file_has_user_message(session_file: Path, agent: str = "") -> ResumeVerdict:
        """Whether a session JSONL file in an engine folder holds at least
        one user message (``transcript_tailer.holds_a_prompt``: record by
        record beneath the agent's real folder, stopping at the first user
        record; a torn last record is no fault). A file that vanished, holds
        no user record or is refused (a link, a pipe, in no agent's folder) is
        ``FRESH``; one the disk could not read is ``UNKNOWN`` (a local disk
        fault, retried once by the probe helper). Runs in a worker thread."""
        from core.session import transcript_tailer
        try:
            held = transcript_tailer.holds_a_prompt(str(session_file), agent)
        except OSError:
            return ResumeVerdict.UNKNOWN
        return ResumeVerdict.RESUMABLE if held else ResumeVerdict.FRESH

    @staticmethod
    def _legacy_file_has_user_message(session_file: Path) -> ResumeVerdict:
        """The same question of the legacy file in the proxy account's own
        home, read line by line."""
        try:
            with session_file.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"type":"user"' in line or '"type": "user"' in line:
                        return ResumeVerdict.RESUMABLE
        except FileNotFoundError:
            return ResumeVerdict.FRESH
        except OSError:
            return ResumeVerdict.UNKNOWN
        return ResumeVerdict.FRESH


def expect_terminal_tools(session_id: str, config: AgentConfig) -> None:
    """The stand-ins a Claude Code terminal's first message waits for: the
    servers of the session's tool config behind a stand-in
    (``tool_listing.expect_listed``). Claude Code sends a first message
    without the servers still connecting; Codex waits for its own."""
    if not config.interactive or not config.mcp_config_path:
        return
    from services.mcp import tool_listing
    try:
        servers = json.loads(Path(config.mcp_config_path).read_text()).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        return
    tool_listing.expect_listed(session_id, tool_listing.standin_servers_of(servers))
