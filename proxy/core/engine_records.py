"""The records that cross the boundary between an engine and the rest of the
platform: a session's config, the profiles an engine declares, its subscriptions
and usage windows, and the capabilities a session resolves to. A leaf like
``core.execution_layer``, which re-exports them.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from core import placement
from core.permission_modes import ASK_MODE as ASK_MODE

#: The platform's effort levels, lowest first — what ``agents.default_effort``
#: may hold and the order every engine's ``effort_levels`` and every
#: provider's ``effort_scale`` follow (each is a sub-sequence of it).
EFFORT_LADDER: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max", "ultra")


class ResumeVerdict(Enum):
    """What ``can_resume_session`` knows about a dead session's conversation.

    ``RESUMABLE``: the engine's files hold it, ``start_session(resume=True)``
    continues it. ``FRESH``: they definitely do not (missing, empty, a name
    nothing can load), the chat takes a fresh session and the history digest.
    ``UNKNOWN``: the question could not be answered now (a machine away or
    not answering, a read the host could not finish): the caller waits or
    refuses the turn, and nothing of the chat changes. The fresh branch is
    irreversible, so this is never a bool: a caller that reads it as one
    fails at its first run instead of resuming fresh on a slow machine.
    """

    RESUMABLE = "resumable"
    FRESH = "fresh"
    UNKNOWN = "unknown"

    def __bool__(self) -> bool:
        raise TypeError("a ResumeVerdict is compared to its members, never read as a bool")


class MachineReach(Enum):
    """Where a session's machine stands when its resume probe went
    unanswered: the platform itself (``LOCAL``), connected but silent
    (``CONNECTED``), dropped within its reconnect grace (``AWAY_RECENT``, worth
    a wait), or away past it or paused (``AWAY``, refused at once)."""

    LOCAL = "local"
    CONNECTED = "connected"
    AWAY_RECENT = "away_recent"
    AWAY = "away"


def fold_effort(level: str, scale: list[str] | tuple[str, ...]) -> str:
    """``level`` on a provider's declared ``scale``: itself when the scale
    has it (or declares nothing), else the highest scale level below it on
    the ladder, else the scale's lowest. A level off the ladder passes
    through (the adapter's own map decides)."""
    if not scale or level in scale or level not in EFFORT_LADDER:
        return level
    rank = EFFORT_LADDER.index(level)
    below = [s for s in scale if s in EFFORT_LADDER and EFFORT_LADDER.index(s) < rank]
    if below:
        return max(below, key=EFFORT_LADDER.index)
    return min(scale, key=lambda s: EFFORT_LADDER.index(s) if s in EFFORT_LADDER else len(EFFORT_LADDER))


# ---------------------------------------------------------------------------
# AgentConfig — bundles what each execution layer needs to start a session
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Configuration bundle passed to ExecutionLayer.start_session().

    Gathered from DB (agent_store) + runtime context (user, MCP config, etc.).
    """

    agent_name: str
    user_sub: str = ""                 # logged-in user driving this session; "" for
                                       # user-less phone paths. Used to route MCP-install
                                       # progress to exactly the engaging user's dashboard
                                       # tabs (install_registry participants).
    # The person the placement guard judges a machine for, when it is not
    # ``user_sub`` (a task: its creds person or a chat worker's delegator,
    # "" for a run of no person). None = ``user_sub``.
    placement_sub: str | None = None
    system_prompt: str = ""
    mcp_config_path: str = ""          # path to generated mcp-config.json
    credential_env: dict = field(default_factory=dict)  # per-user MCP creds
    # Per-(session, mcp) secret bundles for the credential broker,
    # keyed by the mcpServers / [mcp_servers.*] key (server_name or mcp name).
    # The execution layer provisions these into core.credentials.mcp_broker at
    # start_session and injects each stdio MCP's OTO_MCP_FETCH_TOKEN; the
    # interceptor fetches them at spawn so secrets reach each MCP per-server
    # instead of being baked into config files. Direct LLM's tool manager
    # provisions them and merges them into each stdio server's environment.
    mcp_secret_bundles: dict = field(default_factory=dict)
    # The MCPs the session's builder left out, by manifest name (its kind's
    # ``exclude_from``, the external set, an audience or credential
    # refusal). No engine reads them from the config file the builder wrote;
    # the prompt lists them as unavailable.
    excluded_mcps: frozenset[str] = frozenset()
    permission_mode: str = ASK_MODE    # a PERMISSION_MODES word, or the unattended / judge word
    client_type: str = ""              # the session kind's name (core/session/session_kind.py); "" = unrecorded
    model: str = ""                    # override model (empty = agent default)
    effort: str = ""                   # low | medium | high | max
    resume: bool = False               # CLI: --resume existing session
    use_native_permissions: bool = False  # CLI: use native perm mode vs hook
    extra_env: dict = field(default_factory=dict)
    # SecurityContext for path-based access control (typed object here; a
    # layer casts its reads, None when the session has none).
    security_context: object = None
    subscription_id: str = ""          # tracks pool subscription for cleanup on session close
    # The ACQUISITION-scope user sub subscription_id was resolved with — the
    # exact user_sub the config builder passed to the pool ("" = agent-scope /
    # platform pool). Distinct from user_sub above (a user can DRIVE an
    # agent-scoped session). Layers forward it to bind_session so a selection
    # change can re-evaluate the session against the same candidate lists
    # (subscription_pool.rebind_delisted_sessions). None = unstamped → the
    # session is excluded from selection-change rebinds.
    subscription_user_sub: str | None = None
    sandbox_host_claude_dir: str = ""  # host path to persistent .claude/ dir for this session
    # The session's own engine folder, a home in that dir
    # (``core.sandbox.session_home``), as a host path: its sign-in file, its
    # account record and its settings live there, so the pool picks its
    # account alone. "" runs it from the dir above, shared by its scope.
    session_home: str = ""
    # The engine's resume token beyond the session id — what its resume
    # command takes (Codex: the thread id `codex resume <tid>` / `thread/resume`
    # continue). Persisted on ``chats.codex_thread_id`` (the column keeps the
    # name of the engine that first needed one; read-through, no migration);
    # "" for an engine that resumes by session id alone (Claude's --resume).
    resume_handle: str = ""
    chat_id: str = ""                  # Direct LLM: rebuild history from this chat on restart
    # Multi-value path_env env names → join separator. Sent to the remote
    # satellite in start_session payload so its path translator knows which
    # env values to split-translate-rejoin (e.g. ALLOWED_FILE_DIRS=":").
    # Built by config_builder/task_config_builder from manifest path_env
    # decls + the platform's own multi-value env vars (OTO_ALLOWED_ROOTS).
    multi_value_envs: dict = field(default_factory=dict)
    execution_target: str = placement.LOCAL    # the sandbox or a machine id (core/placement.py)
    execution_path: str = ""           # "claude-code-cli" | "codex-cli" | "direct-llm" (for remote layer)
    # Set by the target resolver when the effective target differs from the
    # user- or admin-configured intent (e.g. user's machine offline, viewer
    # routed to local). Surfaced in warmup_ready so the dashboard can show
    # a badge and/or toast. None means no fallback occurred.
    fallback_reason: str | None = None
    # Interactive CLI: spawn the native TUI under a PTY, registered
    # via core.session.interactive_session, instead of the headless -p stream. The
    # session is NOT pump-driven.
    interactive: bool = False
    # Dashboard light/dark mode ("dark"|"light") at spawn time — seeds Claude's
    # TUI theme so it matches the dashboard (and the xterm background). Only used
    # for interactive spawns.
    interactive_theme: str = ""
    # Per-agent default execution mode: ""
    # (unset) | "interactive" | "-p". Read from the agent record; the resolver
    # consults it AFTER the per-chat override and BEFORE the platform default.
    # Only honoured for CLI execution layers (ignored for direct-llm).
    default_execution_mode: str = ""
    # Codex interactive: the cold first prompt, delivered as the ``codex`` launch
    # arg (the TUI auto-runs it after MCP warm — deterministic first-turn submit).
    # Set by the dashboard spawn funnel for a FRESH codex interactive session
    # only; empty otherwise (Claude + resume use the PTY input flush instead).
    interactive_first_prompt: str = ""
    # otodock-CLI / arbitrary-folder sessions: an
    # ABSOLUTE satellite-host working directory OUTSIDE the agent tree (the
    # user's real project folder). Empty = today's behavior (cwd derived from
    # cwd_relative inside agent_dir). When set, the satellite spawns the PTY
    # here (config dirs + username stay agent_dir-rooted via cwd_relative), and
    # the proxy admits this subtree as a per-session allowed root (see
    # SecurityContext.session_allowed_roots).
    work_cwd: str = ""

    # otodock-CLI: the local terminal's $TERM, forwarded so the remote PTY
    # renders to match the user's actual terminal. Empty (dashboard/headless and
    # any non-otodock session) → the satellite keeps its xterm-256color default.
    term: str = ""
    # The ``iat`` of the session token a builder minted into credential_env
    # BEFORE the layer registers the session (the config builder's, a
    # wake's): the session's token floor (session_state.register_session_state).
    # 0 when the builder minted nothing and the layer's spawn-time mint is
    # the process's token.
    token_minted_at: int = 0
    # The chat's choice of account per tool (``{mcp_name: account_label}``,
    # ``credential_resolver.pick_account``); empty for every session without
    # one. The start-time readers of a session's account take it from here.
    account_choices: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# LayerCapabilities — rich capability descriptor for each execution layer
# ---------------------------------------------------------------------------
#
# The descriptor is composed of typed GROUPS. The nineteen fields that
# sit flat on LayerCapabilities are the public JSON contract the dashboard,
# the agent-config MCP and the tests read at the top level, so they stay
# flat and FROZEN; every capability added since lives in one of the groups
# below and serializes under the group's key. A layer's declaration then
# reads as a checklist per concern, and shared code asks the descriptor the
# question it used to answer by comparing an engine-id string.
#
# The rule for what is a capability at all: a FACT shared code needs about
# an engine — data. Anything that is BEHAVIOUR is a method on ExecutionLayer.
# Three flags below (supports_steer / supports_compact /
# supports_interrupt_for_queued) shadow methods the ABC defaults to
# "unsupported" — they exist because the REMOTE layer must know what an
# engine's adapter can do without calling it — and a contract test binds each
# flag to the method that carries it (the layer's own for Direct LLM, the
# remote adapter's for an engine on a machine) so the two cannot drift.


@dataclass
class EngineIdentity:
    """Who the engine is, for labels and ordering.

    ``short_name`` is the engine's wire-independent short id (``claude`` /
    ``codex`` / ``direct``) that ``core/session/session_events`` reports and
    the interactive transcript kinds are named after. ``vendor`` is the
    company whose models the engine runs (a multi-provider engine such as
    Direct LLM has none); ``account_label`` is what a subscription to it is
    called in the UI ("Claude", "ChatGPT"). ``role`` separates the coding
    engines the setup banner counts from a supporting one; ``sort_order``
    is the AI Engines page order.
    """
    short_name: str = ""
    vendor_id: str = ""            # "anthropic" | "openai" | "" (multi-provider)
    vendor_label: str = ""         # "Anthropic" | "OpenAI" | ""
    account_label: str = ""        # "Claude" | "ChatGPT" | ""
    role: str = "supporting"       # "coding" | "supporting"
    sort_order: int = 100

    def to_dict(self) -> dict:
        return {
            "short_name": self.short_name,
            "vendor_id": self.vendor_id,
            "vendor_label": self.vendor_label,
            "account_label": self.account_label,
            "role": self.role,
            "sort_order": self.sort_order,
        }


@dataclass
class RuntimeProfile:
    """How the engine runs on this host — process, placement, binary.

    ``has_os_process``: a LOCAL session has a subprocess (the liveness gate
    skips engines without one; the concurrency RAM class is HEAVY only with
    one). ``hard_abort_kills_process``: a hard abort kills that process (the
    session goes dead and its background work with it) rather than
    interrupting a daemon that stays warm. ``supports_remote_execution``: the
    engine can run on a satellite at all (an in-process engine never can).
    ``supports_interactive_pty``: the engine has a native TUI the platform
    can spawn under a PTY. ``interactive_first_prompt_via_argv``: a fresh
    interactive spawn takes its cold prompt as a launch argument (the TUI
    auto-runs it) instead of a PTY write. ``supports_reattach_after_restart``:
    an in-flight remote turn survives a proxy restart and is re-adopted.
    ``readopts_idle_session``: an idle remote session the satellite kept
    across a proxy restart is taken back (its adapter's ``adopt_state``), so
    shutdown leaves it open. ``binary`` is the CLI executable's name on PATH
    ("" for an in-process engine); ``pin_key`` is the WIRE key the satellite
    reads the pinned version under — frozen vocabulary (``claude_code`` /
    ``codex``), never derived from the binary.

    ``config_dir_name`` is the engine's per-scope config dir under a
    session's root (``.claude`` / ``.codex``; "" for an engine with none) —
    the dir a remote start payload names and the satellite's file sync
    whitelists; it coincides with the credential file's ``dirname`` where
    both exist, and a contract test says so, but neither reads the other.
    ``self_wakes``: the engine runs a turn of its own after a background
    completion (Claude ≥ 2.1.243), so the bg monitors grant it a grace
    window before nudging. ``event_queue_depth`` sizes the proxy-side event
    queue of a REMOTE session (an engine whose between-turns events are
    routed out of band — Codex's bg terminals — needs a deeper one; 0 for an
    engine that never runs remotely). ``interactive_submit_backstop``: the
    engine's TUI needs a second Enter under a Windows ConPTY, whose render
    race swallows the first. ``interactive_reports_model``: the engine's
    terminal names the model a person picks inside it, and the engine's
    transcript tailer reports that pick as a batch's ``model_chosen`` by its
    own rule (Claude Code: the first answer of the turn after a ``/model``
    command; Codex: a main turn on another model than the one before, in the
    same collaboration mode), which the terminal writes to the chat's row
    (``InteractiveTurnMixin._note_model_chosen``). ``installed_name`` is the name a satellite's
    ``installed_clis`` capability reports for this engine's CLI
    (``claude-code`` / ``codex``) — frozen satellite wire, the ``ENGINES``
    row's ``installed_name``; the machine cards map it to ``binary`` to read
    ``cli_status``.
    """
    has_os_process: bool = False
    hard_abort_kills_process: bool = False
    supports_remote_execution: bool = False
    supports_interactive_pty: bool = False
    interactive_first_prompt_via_argv: bool = False
    supports_reattach_after_restart: bool = False
    readopts_idle_session: bool = False
    binary: str = ""
    pin_key: str = ""
    config_dir_name: str = ""
    self_wakes: bool = False
    event_queue_depth: int = 0
    interactive_submit_backstop: bool = False
    installed_name: str = ""
    interactive_reports_model: bool = False

    def to_dict(self) -> dict:
        return {
            "has_os_process": self.has_os_process,
            "hard_abort_kills_process": self.hard_abort_kills_process,
            "supports_remote_execution": self.supports_remote_execution,
            "supports_interactive_pty": self.supports_interactive_pty,
            "interactive_first_prompt_via_argv": self.interactive_first_prompt_via_argv,
            "supports_reattach_after_restart": self.supports_reattach_after_restart,
            "readopts_idle_session": self.readopts_idle_session,
            "binary": self.binary,
            "pin_key": self.pin_key,
            "config_dir_name": self.config_dir_name,
            "installed_name": self.installed_name,
            "self_wakes": self.self_wakes,
            "event_queue_depth": self.event_queue_depth,
            "interactive_submit_backstop": self.interactive_submit_backstop,
            "interactive_reports_model": self.interactive_reports_model,
        }


@dataclass
class BehaviourProfile:
    """What the engine does with a conversation, tools and files.

    ``rebuilds_history_from_db``: the engine keeps no history of its own and
    rebuilds every turn from ``chat_messages`` — so it never needs a history
    seed and can drop an aborted turn cleanly. ``attach_images_inline``:
    chat-attached photos go to the model as native content blocks (else as
    sandbox paths its Read tool opens). ``skills_delivery``: ``materialized_dir`` (the
    CLI's Skill tool indexes a directory) or ``prompt_catalog`` (the prompt
    lists on-demand skills the engine's own Skill builtin loads).
    ``supports_bash`` / ``supports_plans_dir`` / ``builtin_file_tools`` /
    ``has_shell_on_external_route`` drive the permission-context prompt — the
    last one is only meaningful when ``supports_bash`` (an engine with no
    shell at all has nothing to lose on the external route).
    ``provider_pinned_per_session``: the model provider is fixed at spawn, so
    a mid-chat model change across providers is refused. The last three flags
    shadow ABC methods — see the contract test that binds them.

    ``tools`` is the engine's NATIVE tool vocabulary by role
    (``core/events/tool_roles``): role → the names this engine's hooks,
    stream or rollout present for it. Claude's are the canonical names;
    Codex's ``commandExecution`` / ``exec_command`` / ``fileChange`` /
    ``update_plan`` map into the canonical set through
    ``ExecutionLayer.canonical_tool_name`` — a descriptor test binds every
    declared name to a canonical name of its declared role. Read by the
    engine's own config-dir and argv builders (the shell and write tools a
    session may not have) and by the codex-question route (the question
    tool's name on the card). ``nonblocking_question_tools``: the question
    tools that open no dialog holding the turn in the engine's own terminal
    (Codex's ``request_user_input_async``): the terminal does not park on
    them. Headless, every question tool is denied with the card shown and
    the answer arriving as the next message.

    ``repository_instruction_files``: the instruction files of the
    repositories in a person's workspace that the platform lifts into the
    session's prompt, in the engine's own order (its own file first, the
    other only when a repository lacks it); ``()`` when the engine needs
    none. An engine sets it because it does not load a nested repository's
    files where the platform runs it (``services/agents/repository_instructions``).

    ``supports_background_commands``: a command can outlive the step that
    started it, and the chat shows its start and its end (``bg_command_*``).
    ``supports_step_descriptions``: the model writes a sentence for each step
    and the chat shows it. Read by the test rig's live layer, which asks an
    engine only for what it declares (``rig/live/``).

    ``start_context_door``: how a headless start takes the data that changes
    at each start (``core/session/start_context.py``), in a form the
    engine's own screen never draws when the chat moves to a terminal:
    ``"message"`` its own message marked as not typed (Claude Code's
    ``isSynthetic``), ``"hook"`` the engine's ``SessionStart`` hook, which it
    runs headless too (Codex), ``"turn"`` ahead of the turn's text (an
    engine with no terminal).

    ``hides_denied_tools``: the engine takes, at its start on the platform, a
    per-server list of the functions a session may use (``tool_limits``), so
    a function a limit leaves out is not offered to the model. The hook
    refuses it on every engine regardless; this says the model is spared the
    attempt.
    """
    rebuilds_history_from_db: bool = False
    attach_images_inline: bool = False
    skills_delivery: str = "materialized_dir"   # "materialized_dir" | "prompt_catalog"
    supports_bash: bool = False
    supports_plans_dir: bool = False
    builtin_file_tools: bool = False
    has_shell_on_external_route: bool = True
    provider_pinned_per_session: bool = False
    supports_steer: bool = False
    supports_compact: bool = False
    supports_interrupt_for_queued: bool = False
    supports_background_commands: bool = False
    supports_step_descriptions: bool = False
    hides_denied_tools: bool = False
    tools: dict[str, tuple[str, ...]] = field(default_factory=dict)
    nonblocking_question_tools: tuple[str, ...] = ()
    repository_instruction_files: tuple[str, ...] = ()
    start_context_door: str = "turn"   # "turn" | "message" | "hook"
    # How long the vendor keeps a conversation's prompt cache, in seconds (0:
    # not declared). A session idle longer than this starts cold on any
    # account, so the pool may move it to the account it would pick now.
    prompt_cache_s: int = 0

    def to_dict(self) -> dict:
        return {
            "prompt_cache_s": self.prompt_cache_s,
            "rebuilds_history_from_db": self.rebuilds_history_from_db,
            "attach_images_inline": self.attach_images_inline,
            "skills_delivery": self.skills_delivery,
            "supports_bash": self.supports_bash,
            "supports_plans_dir": self.supports_plans_dir,
            "builtin_file_tools": self.builtin_file_tools,
            "has_shell_on_external_route": self.has_shell_on_external_route,
            "hides_denied_tools": self.hides_denied_tools,
            "provider_pinned_per_session": self.provider_pinned_per_session,
            "supports_steer": self.supports_steer,
            "supports_compact": self.supports_compact,
            "supports_interrupt_for_queued": self.supports_interrupt_for_queued,
            "supports_background_commands": self.supports_background_commands,
            "supports_step_descriptions": self.supports_step_descriptions,
            "tools": {role: list(names) for role, names in self.tools.items()},
            "nonblocking_question_tools": list(self.nonblocking_question_tools),
            "repository_instruction_files": list(self.repository_instruction_files),
            "start_context_door": self.start_context_door,
        }


@dataclass
class ModelPolicy:
    """Which model an agent on this engine runs when nobody chose one.

    ``default_model`` is DECLARED, not derived: the operator's rule is the
    best tier the engine serves with tier 1 offered but never defaulted, so
    a frontier model is always a deliberate choice. Direct LLM is the one
    exception: it declares Sonnet 5.5 (tier 3, the operator's call, phone
    calls included) though it serves GPT-6.2 Sol at tier 2 and the same
    price per token, until the operator decides again. The resolver honours it
    when its row is enabled and the platform pool can serve its provider;
    otherwise it falls DOWN the tiers from it, never up
    (``configuration/models.py`` ``resolve_agent_model``). ``model_filter_policy`` is how the engine's
    model list is trimmed to the providers with an active subscription:
    ``all`` (every provider filtered), ``local_providers`` (only local
    endpoints are hidden without a row — a personal vendor account must keep
    seeing the vendor's builtins), ``none``. ``pricing_editable``: the admin
    page offers the per-row price editor (an engine whose vendor prices per
    subscription has nothing to edit).
    """
    default_model: str = ""
    model_filter_policy: str = "none"   # "all" | "local_providers" | "none"
    pricing_editable: bool = False

    def to_dict(self) -> dict:
        return {
            "default_model": self.default_model,
            "model_filter_policy": self.model_filter_policy,
            "pricing_editable": self.pricing_editable,
        }


#: The ``auth_type`` values a subscription row may carry — the storage
#: vocabulary of ``execution_layer_subscriptions.auth_type``.
AUTH_TYPES: tuple[str, ...] = ("oauth", "api_key", "local_endpoint", "relay")
#: The ``auth_type`` of a self-hosted model server's row (an agent that
#: allows only local models on an engine takes these rows alone).
AUTH_LOCAL = "local_endpoint"

#: What a ``providers[]`` entry's ``kind`` may be (see ``LayerCapabilities``).
PROVIDER_KINDS: tuple[str, ...] = ("vendor", "local")
PROVIDER_VENDOR = "vendor"
PROVIDER_LOCAL = "local"


def provider_entry(caps: "LayerCapabilities", provider_id: str) -> dict | None:
    """The engine's ``providers[]`` entry for ``provider_id``, or None when
    the engine does not declare it (a local provider on hosted OtoDock, a
    typo, another engine's vendor)."""
    for entry in caps.providers or ():
        if entry.get("id") == provider_id:
            return entry
    return None


@dataclass(frozen=True)
class CredentialFileSpec:
    """The credential FILE a CLI engine reads its login from, as the platform
    and the satellites know it.

    Three strings a released satellite clamps as one row keyed by the wire
    kind (``satellite/sessions/session_manager.py`` ``credentials_update``:
    ``kind`` must be one it knows, ``dir_relative`` must end in that kind's
    ``dirname``, and it derives the ``filename`` from the kind) — so they are
    FROZEN vocabulary, declared once here and never derived from each other.
    ``wire_kind`` is ``credentials_update.kind`` on the satellite wire;
    ``dirname`` the scope config dir the file lives in; ``filename`` the
    file. The ``start_session`` key that carries the file to a satellite at
    spawn is NOT here: it is the engine's own payload vocabulary, written by
    its remote adapter and read by its own satellite session classes.
    """
    wire_kind: str
    dirname: str
    filename: str

    def to_dict(self) -> dict:
        return {
            "wire_kind": self.wire_kind,
            "dirname": self.dirname,
            "filename": self.filename,
        }


@dataclass(frozen=True)
class SessionHomeSpec:
    """What a session of this engine keeps in a home of its own
    (``core.sandbox.session_home``), so two sessions of one scope run on two
    accounts at once. ``links`` are the store's folders the home reaches by
    a link (the conversations, the plans, the skills: what the platform's
    readers and a resume find in the store); ``own_files`` the files that are
    the session's (the sign-in file among them); ``seed`` the own files a new
    home copies from the store's, each without the named top-level keys (the
    account record without the account it names). An engine that declares
    none keeps one account per scope folder."""
    links: tuple[str, ...]
    own_files: tuple[str, ...]
    seed: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def seed_map(self) -> dict[str, tuple[str, ...]]:
        return dict(self.seed)

    def to_dict(self) -> dict:
        return {"links": list(self.links), "own_files": list(self.own_files),
                "seed": {name: list(keys) for name, keys in self.seed}}


@dataclass
class AuthProfile:
    """How a subscription to this engine is held and how it reaches a session.

    ``auth_types`` are the ``auth_type`` values a row on this engine may
    carry (a subset of :data:`AUTH_TYPES`): an engine with ``oauth`` takes a
    vendor login, ``api_key`` a key for its vendor, ``local_endpoint`` a
    self-hosted OpenAI-compatible server, ``relay`` the hosted relay. Shared
    code asks this instead of comparing the engine id — which engines a
    local endpoint may serve, which pools carry OAuth accounts to cap, which
    engine takes a user's own API key (one that has a vendor and ``api_key``).
    An OAuth row is only ever created by its vendor's login flow, never by
    the generic admin add.

    ``credential_file`` is set for an engine whose login reaches a session as
    a FILE in its scope config dir — the file the pool rewrites under a live
    process on every rotation, and the reason same-scope sessions must run
    on one account (the sticky scope). None for an engine whose credentials
    are injected per session env and never touch disk.

    ``oauth_flow`` is the SHAPE of the login the dashboard renders for an
    engine with ``oauth`` among its auth types — ``code_paste`` (a popup
    whose page shows a code the user pastes back) or ``device_code`` (a
    verification URL and a one-time code the page polls on, RFC 8628) — and
    "" for an engine without a login. It names the UI, not the grant: the
    vendor routes (``/v1/oauth/<vendor>/…``) stay bound to the engine by its
    vendor id; the flow picks which of the two forms the page shows.

    ``sign_in_in_place``: a live session of this engine takes ANOTHER
    account's sign-in file at its next call, so the pool may move it by
    rewriting that file. An engine that refuses another account's file under
    a live process (Codex: "signed in to another account" at its next 401)
    leaves it False, and its live sessions are never moved that way.

    ``resume_refusal_codes``: the vendor's error codes that say a conversation
    resumed on another account cannot be read there (Codex sends the earlier
    reasoning back encrypted for the first account). A turn that fails with
    one starts the chat once more, fresh from its history.

    ``session_home``: what a session keeps in a home of its own
    (``SessionHomeSpec``); None keeps one account per scope folder.
    """
    auth_types: tuple[str, ...] = ()
    credential_file: CredentialFileSpec | None = None
    oauth_flow: str = ""                 # "code_paste" | "device_code" | ""
    sign_in_in_place: bool = False
    resume_refusal_codes: tuple[str, ...] = ()
    session_home: SessionHomeSpec | None = None

    def to_dict(self) -> dict:
        return {
            "auth_types": list(self.auth_types),
            "credential_file": self.credential_file.to_dict() if self.credential_file else None,
            "oauth_flow": self.oauth_flow,
            "sign_in_in_place": self.sign_in_in_place,
            "resume_refusal_codes": list(self.resume_refusal_codes),
            "session_home": self.session_home.to_dict() if self.session_home else None,
        }


@dataclass
class SubscriptionHandle:
    """What the pool hands a layer when it acquires a subscription for a
    session: the row's identity, the credential pieces every engine reads
    (a key, an OAuth access token, an endpoint) and the decrypted stored
    ``credential_data`` for the vendor-shaped parts only that engine's
    adapter understands (``ExecutionLayer.subscription_env``).

    ``oauth_access_token`` / ``oauth_expires_at_ms`` are the token ACTUALLY
    issued for this spawn — the rotated one after a refresh, the aging stored
    one on a fail-soft — which is what the per-session runway tracking
    snapshots (``subscription_pool.bind_session``). 0 when the credential has
    no expiry.
    """
    subscription_id: str
    layer: str
    provider: str
    auth_type: str                  # 'oauth' | 'api_key' | 'local_endpoint' | 'relay'
    api_key: str | None
    oauth_access_token: str | None
    endpoint_url: str | None
    oauth_expires_at_ms: int = 0
    credential: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OAuthRefresh:
    """What an engine's ``refresh_oauth`` hands back to the pool.

    On success ``oauth_token`` is the vendor-shaped new token record
    (``accessToken``, ``refreshToken``, ``expiresAt`` and the vendor's own
    extras — Claude's scopes and plan tier), ``extra`` any other top-level
    ``credential_data`` keys the rotation changed (Codex's ``codex_auth_blob``
    with its tokens brought in step), and ``refresh_token_expires_in`` the
    grant lifetime the vendor reported, when it did. The POOL persists the
    record and carries the platform's own keys across the rotation
    (``refreshTokenExpiresAt``, the health-alert stamps, the account uuid).

    On failure ``oauth_token`` is None and ``status`` / ``body`` (the PARSED
    JSON body, or None) / ``error`` carry what the pool classifies: a
    provider-confirmed ``invalid_grant`` is terminal, everything else is
    transient and backed off. An adapter never raises.
    """
    oauth_token: dict | None = None
    extra: dict = field(default_factory=dict)
    refresh_token_expires_in: int | None = None
    status: int = 0
    body: dict | None = None
    error: str = ""


#: The roles a vendor's rate-limit window can play: the short ``session``
#: window that turns over several times a day, and the ``quota`` window — the
#: one the pool's drain-first sort, the day cap and the owner alerts key off.
WINDOW_ROLES: tuple[str, ...] = ("session", "quota")

#: What a window limits: the whole ``account`` (every model stops at its
#: limit), or one ``model`` (the account keeps serving the others).
WINDOW_SCOPES: tuple[str, ...] = ("account", "model")

#: The kinds of session that can report an account's windows while they run.
IN_BAND_KINDS: tuple[str, ...] = ("headless", "terminal")


@dataclass(frozen=True)
class WindowSpec:
    """One rate-limit window the engine's vendor reports on a subscription.

    ``key`` is the window's name in the listing JSON AND the sample table's
    column prefix (``five_hour`` / ``seven_day`` are column-backed; any other
    key is stored in the sample's JSONB ``data``). ``length_s`` is how long a
    reading of it stays meaningful; ``role`` is ``session`` or ``quota``;
    ``label`` is what the owner reads in an alert ("weekly limit reached").
    ``scope`` is ``account`` or ``model``: a model's window is one per model
    family (the readings' ``Scoped`` entries, matched to a spawn by the
    engine's ``usage_scope_key``), so its ``key`` names the declaration only
    and no reading is filed under it. Without one, an engine's readings carry
    no model windows. What the engine does NOT declare is the margin the pool
    routes by — that is platform policy, keyed by role
    (``services/engines/subscription_windows``).
    """
    key: str
    length_s: int
    role: str
    label: str
    scope: str = "account"


@dataclass(frozen=True)
class InBandSpec:
    """Where a live session of the engine reports its account's windows.

    ``kind`` is ``headless`` or ``terminal``; ``source`` is the name its
    readings are stored under. ``trusted``: the platform reads the report
    from the engine's own output, which the agent cannot write (a headless
    stream). Otherwise the report is a session's claim, a HINT: the agent can
    write it (a terminal's status line command, a rollout file in the
    engine's folder). A hint is stored and shown. It asks for one confirming
    poll of the account at most, and never moves a session, rests an account
    or ends a rest (``subscription_windows.record_for_session``)."""
    kind: str
    source: str
    trusted: bool


@dataclass
class UsageProfile:
    """Which windows the engine's vendor reports on an OAuth subscription,
    and where a live session reports them (``in_band``).

    Empty for an engine whose vendor reports none (Direct LLM: BYO keys are
    metered per token, never capped by a window) — such an engine is never
    polled and its accounts never read as exhausted.
    """
    windows: tuple[WindowSpec, ...] = ()
    in_band: tuple[InBandSpec, ...] = ()

    def to_dict(self) -> dict:
        return {
            "windows": [
                {"key": w.key, "length_s": w.length_s, "role": w.role, "label": w.label,
                 "scope": w.scope}
                for w in self.windows
            ],
            "in_band": [
                {"kind": b.kind, "source": b.source, "trusted": b.trusted}
                for b in self.in_band
            ],
        }


# --- The usage READING an engine's parser produces and the pool consumes ----

@dataclass
class Window:
    pct: float                       # 0..100, above 100 when the vendor says so
    resets_at: datetime | None       # aware UTC; None when the vendor gave none


@dataclass
class Scoped:
    """A per-model quota window (Claude reports one per model family)."""
    key: str                         # model family: fable | opus | sonnet | …
    label: str                       # the vendor's display name
    pct: float
    resets_at: datetime | None
    active: bool = False             # the vendor says this window is limiting now


@dataclass(kw_only=True)
class Windows:
    """One reading of an account's windows, carrying the specs it was read
    against (``LayerCapabilities.usage.windows`` of the account's engine) so
    every consumer settles, compares and serializes each window by ITS
    declaration and never by attribute name."""
    specs: dict[str, WindowSpec]
    windows: dict[str, Window] = field(default_factory=dict)
    scoped: list[Scoped] = field(default_factory=list)
    reached: str = ""                # "" | <window key> | scoped:<key>
    plan: str = ""
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = ""

    def scoped_for(self, key: str) -> Scoped | None:
        for s in self.scoped:
            if s.key == key:
                return s
        return None

    @property
    def quota_key(self) -> str:
        """The key of the first declared whole-account quota window, "" when
        none is declared."""
        for key, spec in self.specs.items():
            if spec.role == "quota" and spec.scope == "account":
                return key
        return ""

    @property
    def model_spec(self) -> WindowSpec | None:
        """The declared window per model, None when the engine declares none
        (its readings then carry no model windows)."""
        for spec in self.specs.values():
            if spec.scope == "model":
                return spec
        return None



#: An engine whose tools the proxy's own client drives (Direct LLM), as against
#: one handed a config for its own process to read.
PROXY_MANAGED = "proxy_managed"

@dataclass
class LayerCapabilities:
    """Describes what an execution layer supports.

    Returned by ExecutionLayer.capabilities. Used by the WS handler,
    API endpoints, and frontend to adapt behavior per layer. The flat fields
    are frozen (see the module comment above); new capabilities go in the
    typed groups at the bottom.
    """

    # Identity
    name: str                          # "claude-code-cli" | "codex-cli" | "direct-llm"
    display_name: str                  # "Claude Code CLI" | "OpenAI Codex" | "Direct LLM API"

    # Feature flags
    supports_resume: bool = False
    supports_permissions: bool = False
    supports_plan_mode: bool = False
    supports_todos: bool = False
    supports_subagents: bool = False
    supports_context_compression: bool = False
    supports_control_commands: bool = False
    supports_mcps: bool = True

    # Per-layer configuration
    permission_modes: list[str] = field(default_factory=list)
    control_commands: list[str] = field(default_factory=list)
    models: list[dict] = field(default_factory=list)
    effort_levels: list[str] = field(default_factory=list)
    effort_changeable_mid_session: bool = False
    compression_threshold_pct: int | None = None

    # MCP delivery strategy
    mcp_delivery: str = "external_config"   # "external_config" | PROXY_MANAGED
    mcp_config_format: str | None = "json"  # "json" | "toml" | None

    # The providers the engine's models come from — every registered engine
    # declares at least its vendor; None only on a synthetic descriptor (the
    # remote placement's), whose one provider is then identity.vendor_id.
    # Each entry: {id, label, requires_key, effort_scale, effort_per_model,
    # kind, relay_path, api_path}. requires_key is False for a local endpoint
    # (Ollama, an OpenAI-compatible server) and True for a vendor key.
    # effort_scale is the platform effort levels the provider's API ACCEPTS,
    # in ladder order (a level the engine offers but the scale omits is
    # folded by the adapter onto the nearest level it has); effort_per_model
    # is the subset a model row's supports_<level> flag gates (the adapter
    # folds it away when the flag is off, the dashboard offers it only when
    # the row says so). kind is PROVIDER_KINDS: "vendor" (a company's API,
    # keyed or logged in) or "local" (a self-hosted server on the operator's
    # network — never offered on hosted OtoDock). relay_path is the hosted
    # relay's base path for a vendor the relay fronts ("" otherwise; only an
    # engine whose auth_types include "relay" may set it). api_path is the
    # path under a bare endpoint where a local provider's OpenAI-compatible
    # API answers ("/v1" for Ollama, "" when the root is it). Bound by:
    # tests/execution/test_engine_descriptor.py.
    providers: list[dict] | None = None

    # Typed groups — every capability added since 2026-09-20 lives here.
    # Defaults on every group so a descriptor built with the flat fields alone
    # (the remote layer's synthetic one, until phase 4) keeps constructing.
    identity: EngineIdentity = field(default_factory=EngineIdentity)
    runtime: RuntimeProfile = field(default_factory=RuntimeProfile)
    behaviour: BehaviourProfile = field(default_factory=BehaviourProfile)
    model_policy: ModelPolicy = field(default_factory=ModelPolicy)
    auth: AuthProfile = field(default_factory=AuthProfile)
    usage: UsageProfile = field(default_factory=UsageProfile)

    def to_dict(self) -> dict:
        """Serialize for API responses: the frozen flat fields, then one
        nested object per group."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "supports_resume": self.supports_resume,
            "supports_permissions": self.supports_permissions,
            "supports_plan_mode": self.supports_plan_mode,
            "supports_todos": self.supports_todos,
            "supports_subagents": self.supports_subagents,
            "supports_context_compression": self.supports_context_compression,
            "supports_control_commands": self.supports_control_commands,
            "supports_mcps": self.supports_mcps,
            "permission_modes": self.permission_modes,
            "control_commands": self.control_commands,
            "models": self.models,
            "effort_levels": self.effort_levels,
            "effort_changeable_mid_session": self.effort_changeable_mid_session,
            "compression_threshold_pct": self.compression_threshold_pct,
            "mcp_delivery": self.mcp_delivery,
            "mcp_config_format": self.mcp_config_format,
            "providers": self.providers,
            "identity": self.identity.to_dict(),
            "runtime": self.runtime.to_dict(),
            "behaviour": self.behaviour.to_dict(),
            "model_policy": self.model_policy.to_dict(),
            "auth": self.auth.to_dict(),
            "usage": self.usage.to_dict(),
        }


@dataclass
class SpawnPlan:
    """A session's spawn with nothing of the host that renders it, built by
    the engine (``Engine.prepare_spawn``) when the session
    runs on a compute machine: the sandbox description
    (``sandbox_render.SandboxDescription.to_dict()``), the inner argv with
    the engine's binary by its bare name (the machine resolves its own
    pinned install), the environment the process runs with (exactly: the
    machine adds nothing to it), the sandbox-side working directory, the
    engine's config dir on the platform (the files the machine's session
    reads through the shared folder) and the same relative to the agent's
    folder (the satellite's transcript tail), the prompt file the proxy
    removes at the close, whether the engine's login was written as a file
    (the rotation fan-out's target), the MCPs the config launches (their
    manifest names) and the same servers by the names the config gives them
    (what the engine reports a server's start by: a Codex warm gate waits
    for these), and whether the engine dials a local model server (its tools
    warm up slower, so the machine's ack takes longer)."""
    sandbox: dict
    argv: list[str]
    env: dict[str, str]
    cwd: str
    config_dir: str
    config_dir_relative: str
    prompt_file: str = ""
    credential_file: bool = False
    mcp_names: frozenset[str] = frozenset()
    mcp_server_names: tuple[str, ...] = ()
    local_model: bool = False
