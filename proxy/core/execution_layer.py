"""ExecutionLayer ABC — abstract interface for all execution backends.

Each backend (Claude CLI, Direct LLM API, Codex CLI, etc.) implements
this interface. The ChatStreamPump and SessionManager interact with
execution layers exclusively through this contract.
"""

import asyncio
import contextlib
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Iterator

from core.events.common_events import CommonEvent
from core import placement

#: The engine an agent runs on when nothing says otherwise — a missing
#: ``agents.execution_path``, a chat row from before the column existed, a
#: caller with no opinion. ONE constant: it used to be typed inline at a dozen
#: sites, so "the platform's default engine" could be neither read nor changed
#: in one place. Lives here, with the contract, because this module is a leaf
#: (the registry imports it, not the other way round) so every config builder
#: can read it without an import cycle.
DEFAULT_EXECUTION_PATH = "claude-code-cli"

#: The permission modes a person can pick for a chat — what a descriptor's
#: ``permission_modes`` may declare (the dashboard's menu is the declared
#: subset). ``auto`` (a task's silent mode) and ``judge`` (a check's
#: read-only profile) are stored on a chat too but never offered. Values
#: are ``chats.permission_mode`` vocabulary — frozen.
PERMISSION_MODES: tuple[str, ...] = ("default", "acceptEdits", "plan", "dontAsk")
PLAN_MODE = "plan"


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
    system_prompt: str = ""
    mcp_config_path: str = ""          # path to generated mcp-config.json
    credential_env: dict = field(default_factory=dict)  # per-user MCP creds
    # Per-(session, mcp) secret bundles for the credential broker,
    # keyed by the mcpServers / [mcp_servers.*] key (server_name or mcp name).
    # The execution layer provisions these into core.credentials.mcp_broker at
    # start_session and injects each stdio MCP's OTO_MCP_FETCH_TOKEN; the
    # interceptor fetches them at spawn so secrets reach each MCP per-server
    # instead of being baked into config files. Direct-LLM ignores this field
    # (it provisions from its own build_session_mcp_config call).
    mcp_secret_bundles: dict = field(default_factory=dict)
    permission_mode: str = "default"   # auto | default | plan | acceptEdits
    client_type: str = ""              # the session kind's name (core/session/session_kind.py); "" = unrecorded
    model: str = ""                    # override model (empty = agent default)
    effort: str = ""                   # low | medium | high | max
    resume: bool = False               # CLI: --resume existing session
    use_native_permissions: bool = False  # CLI: use native perm mode vs hook
    extra_env: dict = field(default_factory=dict)
    security_context: object = None    # SecurityContext for path-based access control
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
# "unsupported" — they exist because the REMOTE layer must know without being
# able to call the local method — and a contract test binds each flag to its
# method so the two cannot drift.


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
    an in-flight remote turn survives a proxy restart and is re-adopted, so
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
    race swallows the first. ``installed_name`` is the name a satellite's
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
    binary: str = ""
    pin_key: str = ""
    config_dir_name: str = ""
    self_wakes: bool = False
    event_queue_depth: int = 0
    interactive_submit_backstop: bool = False
    installed_name: str = ""

    def to_dict(self) -> dict:
        return {
            "has_os_process": self.has_os_process,
            "hard_abort_kills_process": self.hard_abort_kills_process,
            "supports_remote_execution": self.supports_remote_execution,
            "supports_interactive_pty": self.supports_interactive_pty,
            "interactive_first_prompt_via_argv": self.interactive_first_prompt_via_argv,
            "supports_reattach_after_restart": self.supports_reattach_after_restart,
            "binary": self.binary,
            "pin_key": self.pin_key,
            "config_dir_name": self.config_dir_name,
            "installed_name": self.installed_name,
            "self_wakes": self.self_wakes,
            "event_queue_depth": self.event_queue_depth,
            "interactive_submit_backstop": self.interactive_submit_backstop,
        }


@dataclass
class BehaviourProfile:
    """What the engine does with a conversation, tools and files.

    ``rebuilds_history_from_db``: the engine keeps no history of its own and
    rebuilds every turn from ``chat_messages`` — so it never needs a history
    seed and can drop an aborted turn cleanly. ``attach_images_inline``:
    chat-attached photos go to the model as native content blocks (else as
    sandbox paths its Read tool opens). ``phone_http_mcps``: a PHONE session
    on this engine connects the HTTP sidecar MCPs (an in-process engine keeps
    a call on stdio only). ``skills_delivery``: ``materialized_dir`` (the
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
    tool's name on the card). ``question_tool_holds_turn``: the engine's
    question tool RUNS and holds the turn open for the platform's answers
    (Codex: ``request_user_input`` → ``ask_user_question``); False when the
    platform must deny the call, surface the card and let the answer arrive
    as the next message (Claude headless).
    """
    rebuilds_history_from_db: bool = False
    attach_images_inline: bool = False
    phone_http_mcps: bool = True
    skills_delivery: str = "materialized_dir"   # "materialized_dir" | "prompt_catalog"
    supports_bash: bool = False
    supports_plans_dir: bool = False
    builtin_file_tools: bool = False
    has_shell_on_external_route: bool = True
    provider_pinned_per_session: bool = False
    supports_steer: bool = False
    supports_compact: bool = False
    supports_interrupt_for_queued: bool = False
    tools: dict[str, tuple[str, ...]] = field(default_factory=dict)
    question_tool_holds_turn: bool = False

    def to_dict(self) -> dict:
        return {
            "rebuilds_history_from_db": self.rebuilds_history_from_db,
            "attach_images_inline": self.attach_images_inline,
            "phone_http_mcps": self.phone_http_mcps,
            "skills_delivery": self.skills_delivery,
            "supports_bash": self.supports_bash,
            "supports_plans_dir": self.supports_plans_dir,
            "builtin_file_tools": self.builtin_file_tools,
            "has_shell_on_external_route": self.has_shell_on_external_route,
            "provider_pinned_per_session": self.provider_pinned_per_session,
            "supports_steer": self.supports_steer,
            "supports_compact": self.supports_compact,
            "supports_interrupt_for_queued": self.supports_interrupt_for_queued,
            "tools": {role: list(names) for role, names in self.tools.items()},
            "question_tool_holds_turn": self.question_tool_holds_turn,
        }


@dataclass
class ModelPolicy:
    """Which model an agent on this engine runs when nobody chose one.

    ``default_model`` is DECLARED, not derived: the operator's rule is the
    best tier the engine serves with tier 1 offered but never defaulted, so
    a frontier model is always a deliberate choice. The resolver honours it
    when its row is enabled and the platform pool can serve its provider;
    otherwise it falls DOWN the tiers from it, never up (config.py
    ``resolve_agent_model``). ``model_filter_policy`` is how the engine's
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
    """
    auth_types: tuple[str, ...] = ()
    credential_file: CredentialFileSpec | None = None
    oauth_flow: str = ""                 # "code_paste" | "device_code" | ""

    def to_dict(self) -> dict:
        return {
            "auth_types": list(self.auth_types),
            "credential_file": self.credential_file.to_dict() if self.credential_file else None,
            "oauth_flow": self.oauth_flow,
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


@dataclass(frozen=True)
class WindowSpec:
    """One rate-limit window the engine's vendor reports on a subscription.

    ``key`` is the window's name in the listing JSON AND the sample table's
    column prefix (``five_hour`` / ``seven_day`` are column-backed; any other
    key is stored in the sample's JSONB ``data``). ``length_s`` is how long a
    reading of it stays meaningful; ``role`` is ``session`` or ``quota``;
    ``label`` is what the owner reads in an alert ("weekly limit reached").
    What the engine does NOT declare is the margin the pool routes by — that
    is platform policy, keyed by role (``services/engines/subscription_windows``).
    """
    key: str
    length_s: int
    role: str
    label: str


@dataclass
class UsageProfile:
    """Which windows the engine's vendor reports on an OAuth subscription.

    Empty for an engine whose vendor reports none (Direct LLM: BYO keys are
    metered per token, never capped by a window) — such an engine is never
    polled and its accounts never read as exhausted.
    """
    windows: tuple[WindowSpec, ...] = ()

    def to_dict(self) -> dict:
        return {"windows": [
            {"key": w.key, "length_s": w.length_s, "role": w.role, "label": w.label}
            for w in self.windows
        ]}


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
        """The key of the declared quota window, "" when none is declared."""
        for key, spec in self.specs.items():
            if spec.role == "quota":
                return key
        return ""


@dataclass
class RemoteStartPlan:
    """What building a satellite ``start_session`` payload for an engine
    produced, beyond the payload itself: ``credential_file_delivered`` says
    the engine wrote its login into the payload (so the session has a
    credential file on the satellite to rewrite on rotation — the shared
    question the subscription bind asks; the payload KEY that carries it is
    the engine's own vocabulary), ``start_timeout_s`` is the ack budget the
    engine needs for this spawn (a Codex session on a local model waits out
    its MCP warm gate before acking)."""
    payload: dict
    credential_file_delivered: bool = False
    start_timeout_s: float = 60.0


@dataclass
class RemoteStartContext:
    """What the shared remote start-payload builder computed before handing
    the payload to the engine's adapter: the session and machine, the
    ``AgentConfig`` (the adapter reads resume / interactive / the resume
    handle / the MCP config path / the security context — none are in the
    base payload), the satellite's tunnel port and OS (the MCP config
    rewrite is OS-aware), the scope root the session works in
    (``users/<u>`` or ``workspace`` — the adapter names its config dir under
    it), the MUTABLE session env (the adapter pops its private carriers and
    may add), the per-session JWT and the two bundle-key sets the MCP
    rewriters need, and the connection manager (``cm``, typed loosely: the
    adapter calls the satellite version gates and names it already uses)."""
    session_id: str
    machine_id: str
    sat_port: int
    target_os: str
    config: AgentConfig
    scope_root: str
    env: dict
    proxy_api_key: str
    secret_bundle_keys: set
    bearer_swap_keys: set
    cm: object


class RemoteEngineAdapter(ABC):
    """The satellite-protocol half of an engine: how the proxy's remote
    placement (``RemoteExecutionLayer``) drives THIS engine's session on a
    satellite. One per engine package (``core/layers/<x>/remote.py``),
    returned by ``ExecutionLayer.remote_adapter()``; the remote layer keeps
    everything shared (capacity, provisioning, sync, the brokers, the
    queues, the bind, the abort watchdog, grace and liveness) and asks the
    adapter for the engine's part. Frames named inside an adapter are that
    engine's satellite wire vocabulary — frozen with the satellite release
    that handles them, and never descriptor fields.

    ``info`` throughout is the remote layer's per-session record
    (``core.remote.remote_session_info.RemoteSessionInfo``); the adapter
    keeps its own per-session state on ``info.engine_state`` (its own
    dataclass — translators, settle, routers) and is the only code that
    reads it. ``cm`` is the satellite connection manager.
    """

    #: Env keys that are this engine's PRIVATE carriers (credential blobs, a
    #: local endpoint, the model rows) and must never ride the satellite env.
    #: The shared builder strips the UNION over every registered adapter after
    #: the engine's own ``start_payload`` consumed its own — so a session of
    #: one engine can never ship another engine's variable.
    private_env_keys: frozenset = frozenset()

    #: True when a router owned by this adapter is the sole consumer of
    #: ``info.event_queue`` (it demuxes per thread and registers a fresh
    #: consumer per turn); False when the turn stream reads the queue itself
    #: and the shared layer drains stale frames before each turn.
    owns_event_queue: bool = False

    # --- spawn ---------------------------------------------------------------

    @abstractmethod
    def start_payload(self, base: dict, ctx: RemoteStartContext) -> RemoteStartPlan:
        """Extend the shared ``start_session`` payload with this engine's own
        keys (its config dir, its credential file, its MCP config in its
        format, its resume handle, its effort mapping) and say what the
        caller needs to know (``RemoteStartPlan``)."""

    @abstractmethod
    def mcp_names(self, payload: dict) -> set[str]:
        """The MANIFEST names of the MCPs the payload's config will launch —
        what the satellite MCP sync reconciles against."""

    @abstractmethod
    def without_mcps(self, payload: dict, excluded: set[str]) -> dict:
        """The payload with the named (manifest-name) MCPs removed from its
        config — an MCP the satellite could not install must not be spawned."""

    # --- per-session state ---------------------------------------------------

    @abstractmethod
    def init_session(self, info, config: AgentConfig, cm) -> None:
        """A session just started on the satellite: create the adapter's
        per-session record on ``info.engine_state`` (and any consumer task)."""

    def adopt_state(self, info) -> None:
        """A proxy restart re-adopts a session the satellite kept alive
        (``runtime.supports_reattach_after_restart``): rebuild the per-turn
        state the replay streams through. Default: the engine has no live
        re-adopt."""
        raise NotImplementedError(
            f"{type(self).__name__}: this engine has no live re-adopt"
        )

    async def close_state(self, info) -> None:
        """The session is closing or being replaced: cancel the adapter's
        tasks and resolve what they tracked. Runs BEFORE the shared layer
        removes the session's queue. Default: nothing to tear down."""
        return None

    # --- turns ---------------------------------------------------------------

    @abstractmethod
    async def begin_turn(self, info, settle_after_result: int) -> None:
        """Fresh per-turn state before the ``send_message`` frame goes out
        (a per-turn translator and settle controller, the registry prunes, a
        fresh main-turn consumer when the adapter owns the queue)."""

    @abstractmethod
    def stream_turn(self, info, cm) -> AsyncIterator[CommonEvent]:
        """One turn's events from the satellite, translated to CommonEvents,
        ending with DONE — this engine's turn-end decision included."""

    async def drain_bg_commands(self, info, cm, *, budget: float) -> bool:
        """Between turns: resolve background-command completions the way
        this engine surfaces them; True when any resolved. Default: none."""
        return False

    # --- control -------------------------------------------------------------

    @abstractmethod
    def soft_interrupt_frame(self) -> str:
        """The satellite frame that closes this engine's live turn gracefully
        (the process survives for the next prompt)."""

    @abstractmethod
    async def control_request(self, info, cm, subtype: str, **kwargs) -> bool:
        """Forward a control request (``set_model`` / ``set_permission_mode``
        / an engine-specific subtype) to the satellite in this engine's
        shape; True when a frame was sent."""

    async def steer(self, info, cm, text: str) -> bool:
        """Inject user input into the RUNNING remote turn; True only on the
        engine's accept (exactly-once — the caller must not also queue it).
        Default: no satellite frame does this for the engine."""
        return False

    async def compact(self, info, cm) -> dict | None:
        """Compact the remote session's context between turns; the local
        method's contract. Default: no channel."""
        return None

    # --- resume --------------------------------------------------------------

    @abstractmethod
    async def can_resume(
        self, cm, *, machine_id: str, session_id: str, agent_name: str,
        username: str, resume_handle: str,
    ) -> bool:
        """Whether a dead or lost remote session of this engine can be resumed
        on ``machine_id`` — by asking the satellite, or from the handle."""


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
    mcp_delivery: str = "external_config"   # "external_config" | "proxy_managed"
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


# ---------------------------------------------------------------------------
# ExecutionLayer ABC
# ---------------------------------------------------------------------------

class ExecutionLayer(ABC):
    """Abstract interface for all execution backends.

    Implementations:
    - CLIExecutionLayer: wraps PersistentSession (Claude Code CLI subprocess)
    - DirectLLMExecutionLayer: wraps DirectSession (Anthropic API)
    - CodexCLIExecutionLayer: future (Codex CLI subprocess)
    """

    @abstractmethod
    async def start_session(
        self, session_id: str, config: AgentConfig,
    ) -> None:
        """Initialize a session (spawn process, create API client, etc.)."""

    @property
    def engine_name(self) -> str:
        """The engine's short name (``claude`` / ``codex`` / ``direct``) as
        ``core/session/session_events`` reports it — read from the
        descriptor, so a layer declares it once in ``EngineIdentity``. The
        remote layer answers per session from its execution path instead
        (``engine_for``)."""
        return self.capabilities.identity.short_name

    def engine_for(self, session_id: str) -> str:
        return self.engine_name

    def capabilities_for(self, session_id: str) -> LayerCapabilities:
        """The descriptor of the ENGINE behind ``session_id``.

        For a local layer that is its own descriptor. The remote layer is a
        placement, not an engine — its ``capabilities`` describe the
        placement and it answers this per session with the descriptor of
        the engine the session runs (claude-code-cli or codex-cli on the
        satellite), so shared code that has a session in hand asks this and
        never reads a placement's flags for an engine's facts.
        """
        return self.capabilities

    def remote_adapter(self) -> "RemoteEngineAdapter | None":
        """This engine's satellite-protocol half (``RemoteEngineAdapter``),
        or None for an engine that never runs on a satellite (a contract
        test binds this to ``runtime.supports_remote_execution``)."""
        return None

    def canonical_tool_name(self, session_id: str, native: str) -> str:
        """The platform's name for a tool this engine presented as ``native``
        (``core/events/tool_roles``): what the permission authority, the
        pump and the tailers key on. Identity by default — Claude's names
        ARE the canonical set; Codex maps its app-server item types, its
        rollout names and its sanitised MCP server keys here. ``session_id``
        lets an engine answer from the session (the MCP servers it
        declared); the remote placement delegates to the engine behind the
        session. Never raises: an unknown native name comes back as is."""
        return native

    def chat_session_files(self, home: Path, session_id: str, resume_handle: str) -> list[Path]:
        """The on-disk session files of ONE chat under a scope home
        (``users/<u>`` / ``workspace`` / an external caller's tree): what the
        retention sweeps delete when the chat ages out. ``session_id`` and
        ``resume_handle`` are the chat row's; an engine reads the one it
        keys its files by. Default: none — an engine that rebuilds from the
        DB keeps no files."""
        return []

    def iter_session_files(self, home: Path) -> Iterator[tuple[Path, str]]:
        """Every session file of this engine under a scope home, with the
        session id or resume handle it belongs to (raw — the caller keeps its
        own id shape and liveness guards): the orphan sweep. Default: none."""
        return iter(())

    def transcript_tailer(self):
        """The module that persists this engine's INTERACTIVE transcript
        into ``chat_messages`` — ``core.session.transcript_tailer`` for the
        Claude TUI's session JSONL, ``codex_rollout_tailer`` for the Codex
        rollout; both expose ``resolve_and_tail(session_id, chat_id)`` and
        ``forget``. None for an engine with no TUI (a contract test binds
        this to ``runtime.supports_interactive_pty``)."""
        return None

    def pinned_cli_version(self) -> str:
        """The exact CLI version the platform pins for this engine
        (``VERSIONS.md``), or "" for an engine with no binary.

        A METHOD read at call time, not a descriptor field: the descriptors
        are module constants, so a field would freeze ``config.PINNED_*`` at
        import — and the satellite handshake, the machine cards and their
        tests read the live value. Layers with a binary override this.
        """
        return ""

    def cli_binary_path(self) -> str:
        """The CLI executable this host runs for the engine — the configured
        path (``config.CLAUDE_BIN`` / ``config.CODEX_BIN``: an explicit
        override, else ``PATH``, else the user npm prefix), which may sit
        off ``PATH``; "" for an engine with no binary. A lazy method for
        the same reason ``pinned_cli_version`` is one. The boot preflight
        reads it for every engine with a ``runtime.binary``."""
        return ""

    def prepare_config_dir(
        self, agent_name: str, *, username: str = "", scope: str = "user",
        external_home=None, no_shell: bool = False, read_only: bool = False,
    ) -> Path:
        """Create or refresh the persistent config dir a LOCAL session of this
        engine mounts (``.claude`` / ``.codex`` under the scope's root — the
        settings, the hook scripts, the skills dir) and return its host
        path. ``scope`` is ``user`` (``users/<username>/``) or ``agent``
        (``workspace/``); ``external_home`` is an external caller's private
        tree; ``no_shell`` and ``read_only`` are the external-session and
        judge restrictions an engine writes into its settings where it can.

        Every REGISTERED engine overrides this (a contract test binds it):
        the default raises, so an engine that forgets fails CI, never a
        first spawn. An engine with no CLI config still answers — Direct
        LLM keeps its plans dir and its in-process MCP config home in the
        ``.claude`` tree the sandbox mounts.
        """
        raise NotImplementedError(
            f"{type(self).__name__} declares no config dir builder "
            "(ExecutionLayer.prepare_config_dir)"
        )

    def resumes_interactive(self, config: AgentConfig) -> bool:
        """Whether a fresh INTERACTIVE spawn of ``config`` resumes an earlier
        conversation. The warmup's ``config.resume`` flag by default — Claude
        resumes its session id with ``--resume``, so the flag stands. An
        engine that resumes by a handle (Codex: the thread id, whose rollout
        outlives the in-memory session and the flag) answers from the
        handle; for a non-interactive config every engine answers the flag.
        """
        return config.resume

    async def on_subscriptions_changed(self) -> None:
        """The set of subscriptions on this engine changed — a row added,
        updated, disabled or deleted through the admin API.

        Default: nothing. An engine whose credentials feed another platform
        surface overrides it (Direct LLM pushes the phone config, whose Groq
        turn classifier reuses this engine's Groq key). Async because the
        one implementation awaits a WebSocket broadcast; the admin endpoints
        await it after their store write.
        """
        return None

    # --- Credentials: how an acquired subscription reaches a session -------
    #
    # SYNCHRONOUS, all of them: the pool runs on ``to_thread`` workers and its
    # fan-out callback fires on a worker thread for local writes and on the
    # event loop for satellite acks — an async method could be awaited from
    # neither. Vendor knowledge lives here; policy (which account, when to
    # rotate, the lock, the backoff, the fan-out) stays in the pool.

    def subscription_env(self, handle: SubscriptionHandle) -> dict[str, str]:
        """The env entries that carry ``handle``'s credential into a session
        of this engine — the layer's own names, which its ``start_session``
        (and the remote start payload) consume. A key rides the env; an
        OAuth login rides as the credential FILE payload the layer writes
        (``credential_file_from_env`` pops it) — never as an env token, which
        is frozen at exec and would defeat rotation fan-out. A minted relay
        token arrives as ``api_key`` + ``endpoint_url`` like any BYO key.

        Every REGISTERED engine overrides this (a contract test binds it):
        the default raises rather than returning nothing, so an engine that
        forgets cannot spawn with no credentials and fail on its first turn.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not map a subscription to a session env"
        )

    def credential_file_payload(
        self, access_token: str, expires_at_ms: int, stored: dict,
    ) -> dict | None:
        """The full content of this engine's credential file for an OAuth
        ``access_token`` — what the pool writes under a live process on every
        rotation and what the satellite receives verbatim. ``stored`` is the
        decrypted ``credential_data`` (grant metadata, vendor blobs); the
        refresh token is never in the payload (the pool is the sole rotator).
        None for an engine without a credential file, or when ``stored``
        lacks what the file needs. ``expires_at_ms`` is the token's expiry,
        passed by the caller so the file and the caller's own runway snapshot
        cannot disagree."""
        return None

    def credential_file_from_env(self, env: dict) -> dict | None:
        """Pop this engine's credential-file payload out of a session env
        (the consumer half of ``subscription_env``) — the local layer writes
        it, the remote start payload ships it. None when the env carries no
        file (an API-key or local-endpoint session)."""
        return None

    def refresh_oauth(self, refresh_token: str, stored: dict) -> OAuthRefresh:
        """One refresh against the vendor's token endpoint (the vendor call
        only — the lock, the backoff, the terminal verdict, the store write
        and the fan-out are the pool's). ``stored`` is the decrypted
        ``credential_data`` the vendor-shaped parts are rebuilt from. An
        engine that takes a login (``"oauth"`` among its auth types)
        overrides this; the pool treats the default — and any adapter
        exception — as a transient failure, never a dead grant."""
        raise NotImplementedError(
            f"{type(self).__name__} takes no OAuth login to refresh"
        )

    # --- Usage windows: how the vendor reports an account's rate limits -----

    def usage_request(self, stored: dict) -> tuple[str, dict] | None:
        """The vendor's usage endpoint and the headers a poll of ``stored``'s
        account needs (the poller adds the platform ``User-Agent``), or None
        when the engine declares no windows or the credential lacks what the
        vendor needs. An engine with ``usage.windows`` overrides this."""
        return None

    def parse_usage(self, payload) -> Windows | None:
        """The engine's reading of its vendor's usage payload against its
        declared windows, or None on a shape it does not recognise."""
        return None

    def usage_scope_key(self, model: str) -> str:
        """The per-model window a spawn of ``model`` counts against on this
        engine (the ``Scoped.key`` its parser files that window under — Claude
        scopes weekly windows by model family), or "" when the vendor has no
        per-model windows or the model falls under none."""
        return ""

    def record_usage_event(self, session_id: str, payload) -> None:
        """An in-band usage event from a live session of this engine (the
        headless stream's rate-limit info, the app-server's rate-limit
        notification) — parsed by the engine and recorded on the session's
        account, off the event loop. Shared code (the remote turn) calls this
        so it never names an engine's parser; the engine's own session and
        translator may call their sibling usage module directly. Default:
        nothing to record."""
        return None

    async def send_message(
        self, session_id: str, message: str, **kwargs,
    ) -> AsyncIterator[CommonEvent]:
        """Send a message and yield response events.

        Kwargs are layer-specific (e.g. inject_time for CLI, barge_in_chars
        for Direct LLM). One user message is one or more TURNS of the
        layer's ``_send_turn``: after each turn the platform's ``turn_end``
        (``session_events``) may answer "continue with this reason", and the
        reason runs as a follow-up turn on the same session — the
        proxy-driven twin of a ``Stop`` hook block (HOOKS.md "Shapes per
        event"). A follow-up turn keeps the settle window and drops the
        per-message extras (attached images, barge-in, the time prefix).
        """
        from core.session import session_events
        keep = {k: v for k, v in kwargs.items() if k == "settle_after_result"}
        first = True

        async def _turn(text: str):
            nonlocal first
            extra = kwargs if first else keep
            first = False
            async for ev in self._send_turn(session_id, text, **extra):
                yield ev

        async for ev in session_events.drive_turns(
            session_id, self.engine_for(session_id), message, _turn,
        ):
            yield ev

    @abstractmethod
    async def _send_turn(
        self, session_id: str, message: str, **kwargs,
    ) -> AsyncIterator[CommonEvent]:
        """Run ONE turn: send the message, yield its events, return at the
        turn's end. ``send_message`` wraps it with the turn-end loop."""

    @abstractmethod
    async def abort(self, session_id: str) -> bool:
        """Abort the current response.

        Returns True when the abort was GRACEFUL — the engine's own history
        kept the partial turn (Claude control_request interrupt, Codex
        turn/interrupt) and the turn's producer/pump must be left running to
        persist it; the caller then skips the cancelled-context injection.
        False = the process/stream was killed (caller cancels the producer
        and injects cancelled context on the next turn, as before).
        """

    async def steer(self, session_id: str, text: str) -> bool:
        """Inject user input into the RUNNING turn (mid-turn steering).

        Returns True when the engine accepted the input into the live turn
        (delivered exactly-once — the caller must NOT also queue it). False =
        unsupported by this engine, no live turn, or the engine rejected it
        (review/compaction turn, turn just ended) — the caller falls back to
        the post-turn queue. Default: unsupported (Direct rebuilds per-turn;
        satellite PTY injection is turn-end by design). Codex steers through
        ``turn/steer``; the Claude CLI layer writes the user frame into the
        live turn's stdin, which the CLI consumes at its next tool boundary.
        """
        return False

    async def interrupt_for_queued(self, session_id: str) -> bool:
        """Stop-and-send: gracefully stop the in-flight turn so a just-queued
        USER message becomes the next turn (the producer's queue drain
        delivers it within seconds instead of after the whole turn).

        GRACEFUL-ONLY — True only when the engine accepted an interrupt that
        keeps the partial turn in its own history (Claude control_request
        interrupt); the process survives, so background bash commands and
        background subagents keep running. False = no live turn /
        unsupported / dead pipe — the caller leaves the message in the
        post-turn queue (today's behavior). Unlike ``abort`` there is NO
        killpg fallback and NO watchdog: a turn that ignores the interrupt
        simply drains at its natural end. (Known exception: the interrupt
        arms the CLI's #63943 wedge detector, which kills a session whose
        NEXT turn hits the thinking-signature 400 — the correct repair for a
        genuinely wedged process, same as the Stop button.) Default:
        unsupported — an engine with ``steer`` never reaches the queue
        branch while a turn is live, Direct's abort is erase-style,
        interactive PTYs never reach the queue branch.
        """
        return False

    async def compact(self, session_id: str) -> dict | None:
        """Manually compact the session's context, between turns.

        Returns ``{"post_tokens": int | None}`` on success, None when the
        engine has no compaction channel or the compaction failed. Default:
        unsupported (Claude stream-json does not execute /compact from user
        frames — tested on 2.1.201; Direct rebuilds history per-turn). Codex
        implements it via ``thread/compact/start``.
        """
        return None

    @abstractmethod
    async def close_session(self, session_id: str) -> None:
        """Clean up session resources."""

    @abstractmethod
    async def respond_permission(
        self, session_id: str, request_id: str, approved: bool,
    ) -> None:
        """Answer a permission prompt."""

    @abstractmethod
    async def change_model(
        self, session_id: str, model: str,
    ) -> None:
        """Change model mid-session (if supported)."""

    @abstractmethod
    async def change_mode(
        self, session_id: str, mode: str,
    ) -> None:
        """Change permission mode mid-session (if supported)."""

    @abstractmethod
    async def send_control_request(
        self, session_id: str, subtype: str, **kwargs,
    ) -> dict:
        """Send a control request (model change, mode change, thinking tokens).

        Returns the response dict. Only meaningful for CLI path.
        For other layers, may be a no-op returning {}.
        """

    # --- Capabilities ---

    @property
    @abstractmethod
    def capabilities(self) -> LayerCapabilities:
        """Return the capability descriptor for this execution layer."""

    # Convenience accessors (backwards-compatible with old boolean properties)
    @property
    def supports_resume(self) -> bool:
        return self.capabilities.supports_resume

    @property
    def supports_permissions(self) -> bool:
        return self.capabilities.supports_permissions

    @property
    def supports_plan_mode(self) -> bool:
        return self.capabilities.supports_plan_mode

    # --- Session ownership (the registry's view of who holds a session) ---

    @abstractmethod
    def owns_session(self, session_id: str) -> bool:
        """True while this layer holds ``session_id`` in its own registry.

        SYNCHRONOUS and cheap — a dict membership test, no I/O, no lock. It
        answers "whose layer is this session on", never "is it healthy": a
        session whose process died is still OWNED until something pops it.
        Callers that need liveness follow up with ``is_session_alive`` /
        ``is_session_process_dead``.

        This is what lets shared code stop importing each layer's private
        pool dict (``_persistent_sessions``, ``_codex_sessions``,
        ``_direct_sessions``, the remote layer's ``_sessions``) to answer a
        question the layer can answer itself.
        """

    def local_session_ids(self) -> list[str]:
        """Every session id this layer currently holds.

        Used by shutdown, the idle sweeps and the concurrency reconcile so
        they can iterate ``get_all_layers()`` instead of importing each pool
        by name. Default: empty — a layer with no local pool (the remote
        layer's sessions live on satellites) overrides or leaves it.
        """
        return []

    # --- Session access (for layers that manage internal state) ---

    @abstractmethod
    async def get_session(self, session_id: str):
        """Return the underlying session object (PersistentSession, DirectSession, etc.).

        Used by callers that need layer-specific access (e.g. checking if
        process is alive). Returns None if session doesn't exist.
        """

    @abstractmethod
    async def is_session_alive(self, session_id: str) -> bool:
        """Check if a session is still usable (process alive, not closed)."""

    # --- Background sub-agents ---

    async def wait_for_bg_subagents(
        self, session_id: str, *, timeout: float = 120.0,
    ) -> int:
        """Block until this session's background sub-agents finish; return how
        many were pending (0 if none).

        Layer-agnostic: both the CLI (``task_started`` + SubagentStop hook) and
        Codex (the per-thread bg supervisor) feed the same per-session
        ``SubagentRegistry``, and Direct LLM has none — so this single registry
        wait serves every layer. Used by the task producer to honor the
        delegation contract (a delegated agent's result returns only after its bg
        sub-agents finished + it synthesized) and as the bounded wait backstop.
        On timeout it returns the pending count rather than raising, so callers
        nudge anyway (a lost terminal can't hang a task forever)."""
        from core.session.session_state import get_subagent_registry
        reg = get_subagent_registry(session_id)
        n = reg.pending_count
        if n == 0:
            return 0
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(reg.wait_all_done(), timeout=timeout)
        return n

    async def drain_bg_commands(self, session_id: str, *, budget: float = 2.0) -> bool:
        """Resolve background-command completions between turns; return True if
        any were resolved. The CLI layer reads stdout for task_updated frames;
        the Codex layers reconcile against thread/backgroundTerminals/list —
        locally by direct RPC, remotely through the satellite's
        codex_bg_terminals RPC (≥0.5.105; a legacy satellite's translator
        resolves still-open terminals at turn end instead). Direct has no
        background primitive. Default no-op — the bg-command monitor only arms
        for sessions with pending commands."""
        return False

    async def session_self_wakes(self, session_id: str) -> bool:
        """Whether ``session_id`` may run a turn of its own after a
        background completion (``runtime.self_wakes`` of the engine behind
        the session — read per session, so the remote placement answers for
        the engine it runs), so the bg monitors give that wake a grace window
        before nudging (``pump_bg_monitors._wake_grace_covers``). False for
        a session this layer does not hold."""
        return (
            self.capabilities_for(session_id).runtime.self_wakes
            and self.owns_session(session_id)
        )

    # --- Session lock + lifecycle for multi-turn producers ---

    @abstractmethod
    @asynccontextmanager
    async def session_lock(self, session_id: str):
        """Async context manager wrapping the underlying session lock.

        Used by producers to hold the lock across multiple send_message()
        calls (multi-turn queue processing).
        """
        yield  # pragma: no cover

    @abstractmethod
    async def is_session_process_dead(self, session_id: str) -> bool:
        """Check if a session exists but its process has died.

        CLI: session in pool but proc.returncode is not None.
        Direct LLM: always False (no process to die).
        """

    async def probe_session_process_dead(self, session_id: str) -> bool:
        """Actively verify process death before an irreversible reap.

        Local layers already know the truth cheaply (subprocess returncode),
        so the default delegates. The remote layer overrides with a satellite
        RPC — its ``is_session_process_dead`` is a cached flag that reads
        False during a network stall, exactly when the stall-reap needs a
        real answer. Must fail toward ALIVE on uncertainty: a probe failure
        must never cause a reap (severance/grace covers unreachable
        satellites)."""
        return await self.is_session_process_dead(session_id)

    def session_idle_seconds(self, session_id: str) -> float | None:
        """Seconds since this session last produced a real event, or None if
        unknown / not applicable. Used to detect a turn whose event stream was
        severed (e.g. a remote satellite reconnect orphaned the session queue),
        leaving its producer parked with no activity. Only the remote layer
        tracks this — local CLI / Codex / Direct sessions have no such wedge, so
        the default returns None (never reaped on staleness)."""
        return None

    def remote_stream_severed(self, session_id: str) -> bool:
        """True if this session's event stream is known-severed — a remote
        satellite reconnect orphaned its event queue, so an in-flight turn's
        producer no longer receives events (wedged). Lets the dashboard reap a
        zombie pump immediately on resume instead of waiting out the staleness
        window. Local CLI / Codex / Direct sessions are never severed → False."""
        return False

    @abstractmethod
    async def prepare_resume(self, session_id: str) -> None:
        """Prepare for session resume after process death.

        CLI: removes dead session from pool so start_session(resume=True) works.
        Direct LLM: no-op.
        """

    @abstractmethod
    async def can_resume_session(
        self, session_id: str, *, agent_name: str = "", username: str = "",
    ) -> bool:
        """Check if a dead session has conversation data and can be resumed.

        CLI: checks session .jsonl file for user messages.  After proxy
        restart the in-memory session→claude-dir mapping is lost, so
        *agent_name* and *username* are used to derive the .claude/ path.
        Direct LLM: always False (in-memory history lost on death;
        recovery handled via chat_id in AgentConfig instead).
        """
