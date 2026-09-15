"""RemoteExecutionLayer — routes CLI and Codex sessions to satellite daemons.

This layer implements the ExecutionLayer ABC by delegating subprocess management
to a connected satellite daemon over WebSocket. Raw events from the satellite
are translated to CommonEvent using the same translators as local sessions.

The layer is composed from sibling mixins, one per concern:
``remote_session_start`` (capacity, provisioning, sync, spawn),
``remote_start_payload`` (the satellite's start payload), ``remote_turn``
(send, stream, settle, stop), ``remote_abort`` (soft interrupt, watchdog,
hard kill), ``remote_resume`` (adopt, resume, liveness),
``remote_bg_commands`` (between-turns queue reads), ``remote_workspace_sync``
and ``remote_bg_subagent``. ``remote_session_info`` holds the per-session
record and ``remote_reaper`` the idle reaper. This module keeps the control
surface (close, permissions, model and mode changes, steer, compact, the
liveness and lock accessors) and re-exports the names importers and tests
take from here.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from core.execution_layer import ExecutionLayer, LayerCapabilities
from core.remote.satellite_connection import SatelliteConnectionManager
from core.session.session_state import (
    set_session_mode,
    resolve_permission,
    cleanup_session_permission_state,
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
from core.remote.remote_bg_subagent import RemoteBgSubagentMixin  # noqa: E402
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
    RemoteBgSubagentMixin,
    ExecutionLayer,
):
    """Routes sessions to satellite daemons via WebSocket.

    Supports both CLI and Codex execution paths. Direct LLM is always
    handled locally and never routes through this layer.
    """

    def __init__(self, connection_manager: SatelliteConnectionManager):
        self._cm = connection_manager
        self._sessions: dict[str, RemoteSessionInfo] = {}
        # Background deferred-pull tasks: held so create_task'd coroutines
        # aren't GC'd mid-flight; each removes itself on completion.
        self._deferred_sync_tasks: set[asyncio.Task] = set()

    # --- ExecutionLayer interface: control surface ---

    async def close_session(self, session_id: str) -> None:
        info = self._sessions.pop(session_id, None)
        if not info:
            return
        info.alive = False
        # Cancel the bg router + supervisors BEFORE removing the event queue
        # (the router is its sole consumer) so nothing is left waiting on an
        # orphaned queue, and resolve any still-pending bg sub-agents.
        if info.bg_supervised:
            await self._teardown_remote_bg(info)
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
        info = self._sessions.get(session_id)
        if not info:
            return
        info.model = model
        if info.execution_path == "claude-code-cli":
            # Subtype must match what Claude Code CLI expects on its stdin
            # control channel (same as local CLIExecutionLayer.change_model).
            await self._cm.send_fire_and_forget(info.machine_id, {
                "type": "control_request",
                "session_id": session_id,
                "subtype": "set_model",
                "kwargs": {"model": model},
            })
        # Codex: model change applies on next turn automatically

    async def change_mode(self, session_id: str, mode: str) -> None:
        info = self._sessions.get(session_id)
        if not info:
            return
        info.mode = mode
        set_session_mode(session_id, mode)
        if info.execution_path == "claude-code-cli":
            # Subtype must match what Claude Code CLI expects on its stdin
            # control channel (same as local CLIExecutionLayer.change_mode).
            await self._cm.send_fire_and_forget(info.machine_id, {
                "type": "control_request",
                "session_id": session_id,
                "subtype": "set_permission_mode",
                "kwargs": {"mode": mode},
            })
        elif info.execution_path == "codex-cli":
            # Codex app-server gates at the sandbox boundary: the satellite
            # re-derives approvalPolicy from the sandbox mode + rebuilds the
            # per-turn sandboxPolicy, so the new escape behaviour takes effect on
            # the next turn. (The proxy-side gate verdict already follows the new
            # mode via set_session_mode above.)
            from core.layers.codex.helpers import permission_to_sandbox
            await self._cm.send_fire_and_forget(info.machine_id, {
                "type": "control_request",
                "session_id": session_id,
                "subtype": "set_permission_mode",
                "kwargs": {"sandbox_mode": permission_to_sandbox(
                    mode, allow_full_fs=info.allow_full_fs,
                )},
            })

    async def send_control_request(
        self, session_id: str, subtype: str, **kwargs,
    ) -> dict:
        info = self._sessions.get(session_id)
        if not info:
            return {}
        if info.execution_path == "claude-code-cli":
            await self._cm.send_fire_and_forget(info.machine_id, {
                "type": "control_request",
                "session_id": session_id,
                "subtype": subtype,
                "kwargs": kwargs,
            })
        return {}

    @property
    def capabilities(self) -> LayerCapabilities:
        # Remote layer inherits capabilities from the underlying execution path
        # The dashboard should check the agent's execution_path for specific capabilities
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

    async def session_self_wakes(self, session_id: str) -> bool:
        """Wake-grace eligibility (see pump_bg_monitors._wake_grace_covers):
        only claude sessions self-wake; a remote codex session must not pay
        the grace window."""
        info = self._sessions.get(session_id)
        return bool(info) and info.execution_path == "claude-code-cli"

    async def steer(self, session_id: str, text: str) -> bool:
        """Mid-turn steering for a REMOTE headless Codex session — tunnel twin
        of the local layer's ``turn/steer``. Version-gated: an old satellite
        would silently drop the frame, so return False immediately and let the
        caller take today's queue fallback. ``steered`` in the ack is strict
        (True only on daemon accept) — the exactly-once contract the
        steer-vs-queue branch in dashboard_chat depends on."""
        info = self._sessions.get(session_id)
        if not info or info.execution_path != "codex-cli" or not text:
            return False
        if not self._cm.supports_codex_thread_ops(info.machine_id):
            logger.info(
                "remote steer unsupported by satellite %s (< 0.5.98) — "
                "falling back to queue", info.machine_id[:8],
            )
            return False
        try:
            ack = await self._cm.send_command(info.machine_id, {
                "type": "codex_steer",
                "session_id": session_id,
                "text": text,
            }, timeout=15.0)
        except RuntimeError as e:
            logger.warning("remote steer RPC failed for %s: %s", session_id[:8], e)
            return False
        return bool(ack.get("steered"))

    async def compact(self, session_id: str) -> dict | None:
        """Manual context compaction for a REMOTE headless Codex session —
        tunnel twin of the local layer's ``thread/compact/start`` flow. The
        satellite runs the whole wait loop (up to ~125s) and acks
        ``{ok, post_tokens|reason}``; version-gated like ``steer``."""
        info = self._sessions.get(session_id)
        if not info or info.execution_path != "codex-cli":
            return None
        if not self._cm.supports_codex_thread_ops(info.machine_id):
            logger.info(
                "remote compact unsupported by satellite %s (< 0.5.98)",
                info.machine_id[:8],
            )
            return None
        try:
            ack = await self._cm.send_command(info.machine_id, {
                "type": "codex_compact",
                "session_id": session_id,
            }, timeout=150.0)
        except RuntimeError as e:
            logger.warning(
                "remote compact RPC failed for %s: %s", session_id[:8], e,
            )
            return None
        if not ack.get("ok"):
            logger.info(
                "remote compact refused for %s: %s",
                session_id[:8], ack.get("reason", "unknown"),
            )
            return None
        return {"post_tokens": ack.get("post_tokens")}


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
    _translate_venv_for_windows,
    _rewrite_env_for_remote,
)
