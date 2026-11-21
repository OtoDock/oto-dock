"""ExecutionLayer ABC: the session methods of a layer that holds sessions.

The holders are the remote layer (every Claude Code and Codex session, on
machines) and Direct LLM's layer. An engine's facts are ``core/engine.py``.
The ChatStreamPump and SessionManager interact with a holder exclusively
through this contract.
"""

import asyncio
import contextlib
from abc import abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from core.engine import Engine
from core.engine_records import (  # noqa: F401
    AUTH_LOCAL,
    AUTH_TYPES,
    EFFORT_LADDER,
    IN_BAND_KINDS,
    PROVIDER_KINDS,
    PROVIDER_LOCAL,
    PROVIDER_VENDOR,
    WINDOW_ROLES,
    WINDOW_SCOPES,
    AgentConfig,
    AuthProfile,
    BehaviourProfile,
    CredentialFileSpec,
    EngineIdentity,
    InBandSpec,
    LayerCapabilities,
    MachineReach,
    ModelPolicy,
    OAuthRefresh,
    ResumeVerdict,
    RuntimeProfile,
    Scoped,
    SessionHomeSpec,
    SpawnPlan,
    SubscriptionHandle,
    UsageProfile,
    Window,
    Windows,
    WindowSpec,
    fold_effort,
    provider_entry,
)
from core.events.common_events import CommonEvent
from core.permission_modes import (
    ASK_MODE as ASK_MODE,
    AUTO_REVIEW_MODE as AUTO_REVIEW_MODE,
    DONT_ASK_MODE as DONT_ASK_MODE,
    JUDGE_MODE as JUDGE_MODE,
    PERMISSION_MODES as PERMISSION_MODES,
    PLAN_MODE as PLAN_MODE,
    STORED_ONLY_MODES as STORED_ONLY_MODES,
    UNATTENDED_MODE as UNATTENDED_MODE,
    cli_mode as cli_mode,
    entry_mode as entry_mode,
    known_mode as known_mode,
    offered_mode as offered_mode,
    stored_mode as stored_mode,
    terminal_mode as terminal_mode,
)
from core.remote_engine_adapter import (  # noqa: F401
    RemoteEngineAdapter,
    RemoteStartContext,
    RemoteStartPlan,
)

# ---------------------------------------------------------------------------
# ExecutionLayer ABC
# ---------------------------------------------------------------------------

# How long a turn start waits for a machine in its reconnect grace before it
# reads the session as dead: a satellite is back about a second after a
# network blip, and a person's send must not wait for the whole grace.
RECONNECT_WAIT_S = 10.0


class ExecutionLayer(Engine):
    """A layer that holds sessions: the session methods, on top of an
    engine's facts (``Engine``).

    Implementations:
    - RemoteExecutionLayer: the sessions of the process engines, on machines
    - DirectLLMExecutionLayer: wraps DirectSession (Anthropic API)
    """

    @abstractmethod
    async def start_session(
        self, session_id: str, config: AgentConfig,
    ) -> None:
        """Initialize a session (spawn process, create API client, etc.)."""

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

    async def reenter_review(self, session_id: str) -> bool:
        """Re-enter the engine's own review after its decline limit paused
        it, leaving the platform's mode alone: only while the session is
        still in Auto when the engine can take the change (a person's change
        made meanwhile wins). False when nothing was sent (the default: an
        engine with no such pause)."""
        return False

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
    def _send_turn(
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

    async def answer_engine_prompt(
        self, session_id: str, request_id: str, approved: bool, tool_input: dict,
    ) -> None:
        """Answer a permission request the engine raised on its own stdio
        channel (``core/session/engine_prompts``). Only the Claude CLI raises
        them; every other layer has nothing to write."""
        return

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
        pool dict (``_direct_sessions``, the remote layer's ``_sessions``)
        to answer a question the layer can answer itself.
        """

    def local_session_ids(self) -> list[str]:
        """Every session id this layer currently holds.

        Every record the layer holds: Direct LLM's sessions in this
        process, the remote layer's records of the sessions on machines.
        Used by shutdown, the idle sweeps and the concurrency reconcile so
        they can iterate the holders instead of importing each pool by name.
        """
        return []

    # --- Session access (for layers that manage internal state) ---

    @abstractmethod
    async def get_session(self, session_id: str):
        """Return the underlying session object (a machine session's record, DirectSession).

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
        codex_bg_terminals RPC. Direct has no
        background primitive. Default no-op — the bg-command monitor only arms
        for sessions with pending commands."""
        return False

    async def record_waiting_turns(self, session_id: str) -> None:
        """Record what the session's engine produced by itself that nobody
        read yet, before a new turn writes the person's message, so its own
        turn lands above that message, as it happened. Default no-op: only a
        session read through a queue between turns can hold such output."""
        return None

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

        A layer that holds its sessions in the proxy knows the truth
        cheaply, so the default delegates. The remote layer overrides with a satellite
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
        tracks this — a Direct LLM session has no such wedge, so the default
        returns None (never reaped on staleness)."""
        return None

    def remote_stream_severed(self, session_id: str) -> bool:
        """True if this session's event stream is known-severed — a remote
        satellite reconnect orphaned its event queue, so an in-flight turn's
        producer no longer receives events (wedged). Lets the dashboard reap a
        zombie pump immediately on resume instead of waiting out the staleness
        window. Local CLI / Codex / Direct sessions are never severed → False."""
        return False

    async def wait_session_reconnect(self, session_id: str, *,
                                     timeout: float = RECONNECT_WAIT_S) -> bool:
        """Wait, at most ``timeout`` seconds, for a session whose machine is
        reconnecting; True when it is alive again. Every place a turn start
        decides a session is dead asks this first, so a short network blip
        does not take the destructive dead path (record popped, background
        registries dropped, a fresh session in place of the live one). Local
        sessions have no machine → False at once."""
        return False

    def is_session_grace_held(self, session_id: str) -> bool:
        """True while the session's machine dropped and its reconnect grace
        still holds the session: reconnecting, not dead. Local sessions have
        no machine → False."""
        return False

    @abstractmethod
    async def prepare_resume(self, session_id: str) -> None:
        """Prepare for session resume after process death.

        A machine's session: forgets the dead record so start_session(resume=True) works.
        Direct LLM: drops a session whose tools on a compute machine were lost; its slot stays the caller's.
        """

    def resume_probe_future(
        self, session_id: str, *, agent_name: str = "", username: str = "",
    ) -> "asyncio.Future | None":
        """The answer of the resume probe still outstanding for a session
        whose machine has not answered (``settle_resume`` kept it): a future
        a deferred turn's watcher waits on, None when none is out. A local
        session has no probe out."""
        return None

    def forget_resume_probe(
        self, session_id: str, *, agent_name: str = "", username: str = "",
    ) -> None:
        """Stop waiting for the session's outstanding resume probe (a late
        answer is dropped). Nothing for a holder that sends no probe."""
        return

    def resume_machine_reach(
        self, session_id: str, *, agent_name: str = "", username: str = "",
    ) -> MachineReach:
        """Where the session's machine stands, for a resume probe that went
        unanswered (``resume_probe.settle_resume`` decides whether to wait).
        A layer that runs on the platform answers ``LOCAL``; the remote layer
        answers from its connection manager."""
        return MachineReach.LOCAL
