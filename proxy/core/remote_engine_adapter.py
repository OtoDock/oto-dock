"""What an engine provides to run on a paired machine: the adapter the remote
layer calls for everything engine-specific, with its start context and plan.
Re-exported by ``core.execution_layer``.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

from core.engine_records import AgentConfig, ResumeVerdict, SpawnPlan
from core.events.common_events import CommonEvent


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
    may add), the per-session JWT, the bundle-key set and the credential
    gateway's facts the MCP rewriters need (``gateway`` by mcpServers key),
    and the connection manager (``cm``, typed loosely: the adapter calls the
    satellite version gates and names it already uses). ``spawn`` is set
    for a session on a compute machine: the engine's layer prepared the
    files in the shared folder and planned the spawn, so the adapter
    ships the plan and the words the machine reads, nothing to write."""
    session_id: str
    machine_id: str
    sat_port: int
    target_os: str
    config: AgentConfig
    scope_root: str
    env: dict
    proxy_api_key: str
    secret_bundle_keys: set
    gateway: dict
    cm: object
    spawn: SpawnPlan | None = None


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

    async def reenter_review(self, info, cm) -> bool:
        """Re-enter the engine's own review after its decline limit (see
        ``ExecutionLayer.reenter_review``); False for an engine with none."""
        return False

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

    # The payload fields that carry the session's prompt text.
    prompt_payload_keys: tuple[str, ...] = ("system_prompt",)

    def with_unavailable_mcps(self, payload: dict, reasons: dict[str, str]) -> dict:
        """The payload's prompt told the truth about the MCPs the satellite
        could not install (``without_mcps`` took them out of the config):
        they leave the prompt's catalog and are listed, with their reason,
        as unavailable (``config.fold_unavailable_mcps``)."""
        from config import fold_unavailable_mcps
        for key in self.prompt_payload_keys:
            if isinstance(payload.get(key), str):
                payload[key] = fold_unavailable_mcps(payload[key], reasons)
        return payload

    # --- per-session state ---------------------------------------------------

    @abstractmethod
    def init_session(self, info, config: AgentConfig, cm) -> None:
        """A session just started on the satellite: create the adapter's
        per-session record on ``info.engine_state`` (and any consumer task)."""

    def adopt_state(self, info, cm) -> None:
        """A proxy restart re-adopts a session the satellite kept alive
        (``runtime.supports_reattach_after_restart`` for a turn in flight,
        ``runtime.readopts_idle_session`` for an idle one): rebuild the state
        a replay or the next turn streams through, from ``info``. Default:
        the engine has no live re-adopt."""
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

    def note_interrupt(self, info) -> None:
        """That frame went out for the live turn: an engine whose turn then
        ends in an error result marks the result as the interrupt's.
        Default: nothing."""
        return

    def turn_settled(self, info) -> bool:
        """The engine's turn is over (its result was read) and only the wait
        for background work holds the stream open: a soft interrupt would
        find nothing to interrupt, so a Stop takes the hard path at once.
        Default: the engine's stream has no such wait."""
        return False

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

    def probe_future(self, cm, *, machine_id: str, session_id: str) -> "asyncio.Future | None":
        """The outstanding resume probe's answer for ``session_id`` on
        ``machine_id`` (``can_resume`` left it out), None when none is out.
        Default: an engine that probes nothing has none."""
        return None

    def forget_probe(self, cm, *, machine_id: str, session_id: str) -> None:
        """Drop the outstanding resume probe for ``session_id`` on
        ``machine_id`` (a late answer is dropped with it). Default: nothing."""
        return

    @abstractmethod
    async def can_resume(
        self, cm, *, machine_id: str, session_id: str, agent_name: str,
        username: str, resume_handle: str, external_home: str = "",
    ) -> ResumeVerdict:
        """Whether a dead or lost remote session of this engine can be resumed
        on ``machine_id`` — by asking the satellite, or from the handle. An
        engine that asks answers ``UNKNOWN`` while the machine is away or
        silent (``core/session/resume_probe.py`` waits on it)."""
