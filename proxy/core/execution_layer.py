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
from typing import AsyncIterator

from core.events.common_events import CommonEvent


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
    client_type: str = ""              # dashboard | phone | task | sse
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
    codex_thread_id: str = ""          # Codex: pre-populate thread_id for resume
    chat_id: str = ""                  # Direct LLM: rebuild history from this chat on restart
    # Multi-value path_env env names → join separator. Sent to the remote
    # satellite in start_session payload so its path translator knows which
    # env values to split-translate-rejoin (e.g. ALLOWED_FILE_DIRS=":").
    # Built by config_builder/task_config_builder from manifest path_env
    # decls + the platform's own multi-value env vars (OTO_ALLOWED_ROOTS).
    multi_value_envs: dict = field(default_factory=dict)
    execution_target: str = "local"    # "local" or machine_id for remote execution
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

@dataclass
class LayerCapabilities:
    """Describes what an execution layer supports.

    Returned by ExecutionLayer.capabilities. Used by the WS handler,
    API endpoints, and frontend to adapt behavior per layer.
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

    # Provider support (for multi-provider layers like Codex)
    providers: list[dict] | None = None

    def to_dict(self) -> dict:
        """Serialize for API responses."""
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

    @abstractmethod
    async def send_message(
        self, session_id: str, message: str, **kwargs,
    ) -> AsyncIterator[CommonEvent]:
        """Send a message and yield response events.

        Kwargs are layer-specific (e.g. inject_time for CLI, barge_in_chars
        for Direct LLM).
        """

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
        the post-turn queue. Default: unsupported (Claude stream-json has no
        steering upstream; Direct rebuilds per-turn; satellite PTY injection
        is turn-end by design).
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
        """Read a session's stdout to resolve background-command completions
        between turns; return True if any were resolved. Default no-op — only the
        CLI layer backgrounds bash commands (Codex/remote/direct don't), and the
        bg-command monitor only arms for sessions with pending commands."""
        return False

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
