"""RemoteExecutionLayer — routes CLI and Codex sessions to satellite daemons.

This layer implements the ExecutionLayer ABC by delegating subprocess management
to a connected satellite daemon over WebSocket. Raw events from the satellite
are translated to CommonEvent using the same translators as local sessions.

The layer is composed from sibling mixins, one per concern:
``remote_session_start`` (capacity, provisioning, sync, spawn),
``remote_start_payload`` (the satellite's start payload), ``remote_turn``
(send, stream, settle, stop), ``remote_abort`` (soft interrupt, watchdog,
hard kill), ``remote_resume`` (adopt, resume, liveness),
``remote_bg_commands`` (between-turns queue reads) and
``remote_workspace_sync``. ``remote_session_info`` holds the per-session
record and ``remote_reaper`` the idle reaper. This module keeps the control
surface (close, permissions, model and mode changes, steer, compact, the
liveness and lock accessors) and re-exports the names importers and tests
take from here.

The layer is a PLACEMENT, not an engine: a session here still runs one of
the registered engines on the satellite, and everything engine-specific —
the payload keys, the translators, the frames, the resume probe — is that
engine's ``RemoteEngineAdapter`` (``core/layers/<x>/remote.py``), reached
through ``_adapter(info)``. Nothing in this package names an engine.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from core import placement
from core.execution_layer import ExecutionLayer, LayerCapabilities
from core.remote.satellite_connection import SatelliteConnectionManager
from core.session.session_state import (
    set_session_mode,
    resolve_permission,
    cleanup_session_permission_state,
    mark_closing, clear_starting,
)

logger = logging.getLogger("remote-layer")


def get_remote_layer():
    """Accessor for other modules that need the RemoteExecutionLayer
    singleton without triggering a circular import. Delegates to
    session_manager's lazy initializer.
    """
    from core.session.session_manager import _get_remote_layer
    return _get_remote_layer()


from core.remote.remote_session_info import RemoteSessionInfo  # noqa: E402,F401
from core.remote.remote_session_start import (  # noqa: E402,F401
    RemoteSessionStartMixin,
    _collect_session_files,
    _machine_sync_role,
)
from core.remote.remote_start_payload import RemoteStartPayloadMixin  # noqa: E402
from core.remote.remote_turn import RemoteTurnMixin  # noqa: E402
from core.remote.remote_abort import RemoteAbortMixin  # noqa: E402
from core.remote.remote_resume import RemoteResumeMixin  # noqa: E402
from core.remote.remote_bg_commands import RemoteBgCommandsMixin  # noqa: E402
from core.remote.remote_workspace_sync import (  # noqa: E402,F401
    RemoteWorkspaceSyncMixin,
    _partition_deferred_pulls,
    _DEFER_PULL_MIN_BYTES,
)
from core.remote.remote_reaper import (  # noqa: E402,F401
    _idle_session_has_pending_work,
    reap_idle_remote_sessions,
)


class RemoteExecutionLayer(
    RemoteSessionStartMixin,
    RemoteStartPayloadMixin,
    RemoteTurnMixin,
    RemoteAbortMixin,
    RemoteResumeMixin,
    RemoteBgCommandsMixin,
    RemoteWorkspaceSyncMixin,
    ExecutionLayer,
):
    """Routes sessions to satellite daemons via WebSocket.

    Every engine with a remote adapter (``runtime.supports_remote_execution``)
    runs here; Direct LLM is always handled locally and never routes through
    this layer.
    """

    def __init__(self, connection_manager: SatelliteConnectionManager):
        self._cm = connection_manager
        self._sessions: dict[str, RemoteSessionInfo] = {}
        # Session ids a start is spawning now: a connect report naming one
        # (the satellite's previous process of that id) leaves it to the start.
        self._spawning: set[str] = set()
        # Background deferred-pull tasks: held so create_task'd coroutines
        # aren't GC'd mid-flight; each removes itself on completion.
        self._deferred_sync_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _adapter(info: RemoteSessionInfo):
        """The remote adapter of the engine ``info``'s session runs — every
        per-engine question about a session goes through it."""
        from core.session.session_manager import get_layer_by_path
        adapter = get_layer_by_path(info.execution_path).remote_adapter()
        if adapter is None:
            raise RuntimeError(f"{info.execution_path} has no remote adapter")
        return adapter

    def is_spawning(self, session_id: str) -> bool:
        return session_id in self._spawning

    async def _insert_session(self, info: RemoteSessionInfo) -> None:
        """Hold ``info`` as its session's record. A record already held under
        the id (a re-warm of a live session, a session adopted while this
        start ran) has its engine state ended first: its router reads a queue
        nothing feeds any more."""
        prior = self._sessions.get(info.session_id)
        if prior is not None and prior is not info:
            try:
                await self._adapter(prior).close_state(prior)
            except Exception:
                logger.exception("Insert of %s: the replaced record's state did not close",
                                 info.session_id[:8])
        self._sessions[info.session_id] = info

    # --- ExecutionLayer interface: control surface ---

    async def close_unadopted(self, machine_id: str, session_id: str,
                              incarnation: str = "") -> None:
        """End a process a satellite reported after a restart that the
        platform does not take back (closed on purpose, or placed on another
        machine). Only the satellite's process: nothing of the session's
        state here is touched, since it may belong to the session on its own
        machine. ``incarnation`` (the report's) makes the satellite close only
        the object it reported, never one a later start put under the id."""
        frame = {"type": "close_session", "session_id": session_id}
        if incarnation:
            frame["incarnation"] = incarnation
        try:
            await self._cm.send_command(machine_id, frame, timeout=15.0)
        except Exception as e:
            logger.warning("Remote close of unadopted %s failed: %s", session_id[:8], e)

    async def close_session(self, session_id: str) -> None:
        info = self._sessions.pop(session_id, None)
        if not info:
            return
        mark_closing(session_id)
        clear_starting(session_id)
        info.alive = False
        # The engine's per-session state goes first — BEFORE the event queue
        # is removed (a Codex router is that queue's sole consumer), so nothing
        # is left waiting on an orphaned queue and every still-pending
        # background sub-agent and terminal is resolved.
        await self._adapter(info).close_state(info)
        try:
            await self._cm.send_command(info.machine_id, {
                "type": "close_session",
                "session_id": session_id,
            }, timeout=15.0)
        except Exception as e:
            logger.warning("Remote close failed: %s", e)
        # Keep the cached heartbeat count roughly honest between heartbeats —
        # the capacity pre-check + eviction loop read it (see
        # debit_reported_session).
        self._cm.debit_reported_session(info.machine_id)
        self._cm.remove_session_queue(info.machine_id, session_id)
        cleanup_session_permission_state(session_id)

        # Purge the remote-file-flow cache for this session (per-session dir
        # under AGENTS_DIR/.remote-host-cache/). Must come AFTER _sessions.pop so
        # is_remote_session() on any in-flight hook returns False.
        try:
            from core.remote import remote_file_flow
            remote_file_flow.cleanup_session(session_id)
        except Exception as e:
            logger.warning("remote_file_flow cleanup failed: %s", e)

        # Purge any per-session `outputs` subdirectories (e.g. camoufox
        # screenshots under workspace/.screenshots/{session_id}/).
        try:
            from core.session import session_state
            from services.mcp import mcp_output_relocation
            ctx = session_state.get_session_security(session_id)
            if ctx is not None:
                mcp_output_relocation.cleanup_session(
                    session_id, ctx.agent, ctx.username,
                )
        except Exception as e:
            logger.warning("mcp_output_relocation cleanup failed: %s", e)

        # Release subscription + concurrency slot
        from services.engines.subscription_pool import release_subscription
        release_subscription(session_id)
        from core.concurrency import release_chat_slot
        release_chat_slot(session_id)

    async def respond_permission(
        self, session_id: str, request_id: str, approved: bool,
    ) -> None:
        """Answer a permission prompt.

        Hook-based permissions (default): the hook script on the satellite
        is blocking on an HTTP call to the proxy; resolve_permission unblocks
        it locally — no satellite round-trip needed.

        Native CLI permissions (use_native_permissions=True): the CLI
        emitted a ``control_request.can_use_tool`` on its stdout and is
        waiting for a ``control_response`` on stdin. We forward it through
        the satellite.
        """
        # Hook-based path always works (harmless if no hook is waiting).
        resolve_permission(request_id, approved)

        # Native-permission path: forward control_response to satellite stdin.
        info = self._sessions.get(session_id)
        if info and info.use_native_permissions:
            try:
                await self._cm.send_fire_and_forget(info.machine_id, {
                    "type": "control_response",
                    "session_id": session_id,
                    "request_id": request_id,
                    "approved": approved,
                })
            except Exception as e:
                logger.warning(
                    "Remote native permission forward failed for session %s: %s",
                    session_id[:8], e,
                )

    async def change_model(self, session_id: str, model: str) -> None:
        """The model change reaches the satellite in the engine's shape
        (``RemoteEngineAdapter.control_request``): Claude's stdin control
        frame, Codex's per-turn override on the next ``turn/start``."""
        info = self._sessions.get(session_id)
        if not info:
            return
        info.model = model
        await self._adapter(info).control_request(
            info, self._cm, "set_model", model=model,
        )

    async def change_mode(self, session_id: str, mode: str) -> None:
        """The proxy-side gate verdict follows the new mode at once
        (``set_session_mode``); the engine's own mode reaches the satellite
        in the engine's shape (Claude's stdin control frame; Codex's sandbox
        mode, which its satellite twin re-derives approvalPolicy from and
        rebuilds the per-turn sandboxPolicy with)."""
        info = self._sessions.get(session_id)
        if not info:
            return
        info.mode = mode
        set_session_mode(session_id, mode)
        await self._adapter(info).control_request(
            info, self._cm, "set_permission_mode", mode=mode,
        )

    async def send_control_request(
        self, session_id: str, subtype: str, **kwargs,
    ) -> dict:
        """A queued control request flushed after a streaming turn. The two
        subtypes the dashboard queues go through ``change_model`` /
        ``change_mode`` so the session record (and the proxy-side mode the
        plan card and the gate read) stay in step; anything else is the
        engine's to forward or ignore."""
        info = self._sessions.get(session_id)
        if not info:
            return {}
        if subtype == "set_model":
            await self.change_model(session_id, kwargs.get("model", ""))
        elif subtype == "set_permission_mode":
            await self.change_mode(session_id, kwargs.get("mode", ""))
        else:
            await self._adapter(info).control_request(info, self._cm, subtype, **kwargs)
        return {}

    @property
    def capabilities(self) -> LayerCapabilities:
        """The PLACEMENT's descriptor, not an engine's: remote is where a
        session runs, the engine is still claude-code-cli or codex-cli on the
        satellite. Every flat flag reads True ("the placement forbids
        nothing"); the typed groups are defaults. Shared code that has a
        session in hand asks ``capabilities_for(session_id)`` and gets the
        engine's real descriptor — never these flags for an engine's facts."""
        return LayerCapabilities(
            name="remote",
            display_name="Remote Execution",
            supports_resume=True,
            supports_permissions=True,
            supports_plan_mode=True,
            supports_todos=True,
            supports_subagents=True,
            supports_control_commands=True,
            supports_mcps=True,
        )

    def capabilities_for(self, session_id: str) -> LayerCapabilities:
        """The descriptor of the engine ``session_id`` runs on its satellite:
        a headless session's from its record, a remote INTERACTIVE session's
        (no record — it lives in ``interactive_session``) from that registry;
        the placement descriptor for a session this layer does not hold."""
        from core.session.session_manager import capabilities_for_path
        info = self._sessions.get(session_id)
        if info is not None:
            return capabilities_for_path(info.execution_path)
        from core.session import interactive_session
        isess = interactive_session.get(session_id) if session_id else None
        if isess is not None and not placement.is_local(isess.target):
            return capabilities_for_path(isess.execution_path)
        return self.capabilities

    def owns_session(self, session_id: str) -> bool:
        return session_id in self._sessions

    def canonical_tool_name(self, session_id: str, native: str) -> str:
        """The engine behind the session answers (a placement has no tool
        names of its own); identity for a session this layer does not hold."""
        caps = self.capabilities_for(session_id)
        if caps is self.capabilities:
            return native
        from core.session.session_manager import get_layer_by_path
        return get_layer_by_path(caps.name).canonical_tool_name(session_id, native)

    def local_session_ids(self) -> list[str]:
        """Remote sessions are the satellite's processes, not ours — but the
        session RECORDS are held here, and every caller that asks "which
        sessions does this layer know about" means those records (liveness
        gates, the token-confinement check, the lane wedge sweep)."""
        return list(self._sessions)

    def session_ids_on(self, machine_id: str) -> list[str]:
        """The session records this layer holds on one machine."""
        return [sid for sid, info in self._sessions.items() if info.machine_id == machine_id]

    async def get_session(self, session_id: str):
        return self._sessions.get(session_id)

    async def is_session_alive(self, session_id: str) -> bool:
        info = self._sessions.get(session_id)
        if not info:
            return False
        # cli_dead → the persistent CLI subprocess crashed/was aborted but the
        # entry isn't reaped yet; report not-alive so callers don't reuse it.
        return (
            info.alive
            and not info.cli_dead
            and self._cm.is_connected(info.machine_id)
        )

    @asynccontextmanager
    async def session_lock(self, session_id: str):
        info = self._sessions.get(session_id)
        if info:
            async with info.lock:
                yield
        else:
            yield

    async def steer(self, session_id: str, text: str) -> bool:
        """Mid-turn steering for a REMOTE headless session — the engine's
        satellite frame (``RemoteEngineAdapter.steer``: Codex's
        ``codex_steer`` since satellite 0.5.98, Claude's ``steer_turn`` since
        0.5.128, each refused before the send on an older satellite). False
        takes the caller's queue fallback; the accept is strict
        (exactly-once)."""
        info = self._sessions.get(session_id)
        if not info or not text:
            return False
        return await self._adapter(info).steer(info, self._cm, text)

    async def compact(self, session_id: str) -> dict | None:
        """Manual context compaction for a REMOTE headless session — the
        engine's satellite frame, when it has one (``RemoteEngineAdapter.compact``)."""
        info = self._sessions.get(session_id)
        if not info:
            return None
        return await self._adapter(info).compact(info, self._cm)


# The MCP config-rewriting functions live in remote_mcp_rewrite.py; the payload
# builders in remote_start_payload.py call them by bare name, and they are
# re-exported here so tests that import them from core.remote.remote_execution
# keep working. Tests that MONKEYPATCH the internally-called ones
# (_resolve_satellite_mcp_path_info / _rewrite_stdio_paths / _rewrite_env_for_remote)
# must patch core.remote.remote_mcp_rewrite instead.
from core.remote.remote_mcp_rewrite import (  # noqa: F401
    _REMOTE_MCP_STARTUP_FLOOR,
    _REMOTE_MCP_STARTUP_OVERRIDES,
    _strip_toml_mcp_sections,
    _resolve_satellite_mcp_path_info,
    _rewrite_mcp_json_for_remote,
    _rewrite_mcp_toml_for_remote,
    _rewrite_stdio_paths,
    _rewrite_env_for_remote,
)
