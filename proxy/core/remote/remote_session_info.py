"""Proxy-side state of one remote session (the ``RemoteSessionInfo`` record).

A leaf of the remote package: every piece of ``RemoteExecutionLayer`` builds
or annotates this record, so it lives on its own and imports nothing but the
standard library. The ENGINE's own per-session state (translators, settle
controller, routers) lives on ``engine_state`` as the engine's remote
adapter's dataclass — this module names no engine.
"""

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class RemoteSessionInfo:
    """Tracks a remote session's state on the proxy side."""
    session_id: str
    machine_id: str
    agent_name: str
    execution_path: str  # the engine's wire id (claude-code-cli / codex-cli)
    event_queue: asyncio.Queue
    alive: bool = True
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # The engine's resume token beyond the session id (Codex: the thread id
    # the satellite reports at start), recorded by
    # ``remote_turn.record_resume_handle`` and persisted on
    # ``chats.codex_thread_id`` by the pump from the adapter's first-turn
    # METADATA event; "" until reported / for an engine that resumes by
    # session id alone.
    resume_handle: str = ""
    # The engine's remote adapter's own per-session record
    # (``RemoteEngineAdapter.init_session`` / ``adopt_state`` create it,
    # ``close_state`` releases it). Read by that adapter only.
    engine_state: object | None = None
    # Permission-mode tracking (native vs hook-based permissions)
    use_native_permissions: bool = False
    # Machine pairing's full-filesystem grant — feeds Codex sandbox-mode
    # re-resolution on mode toggles (must match the spawn-time resolution).
    allow_full_fs: bool = False
    model: str = ""
    mode: str = "default"
    last_activity: float = field(default_factory=time.monotonic)
    # Set of MCP names this session has in its shipped config. Consumed by
    # mcp_sync to compute the union across active sessions on the same
    # machine (deferred-uninstall guard).
    used_mcps: set[str] = field(default_factory=set)
    # Fallback reason surfaced in warmup_ready for UI badges. None means
    # the session is running on the user/admin-intended target.
    fallback_reason: str | None = None
    # command_id of the in-flight `send_message` command. Used to filter
    # stale `_turn_ended` events from a previous turn whose satellite-side
    # file-scan ran past the proxy's drain timeout — without this match,
    # a late turn_ended would terminate the NEW turn with zero events.
    current_send_command_id: str = ""
    # Set when the satellite's CLI subprocess is known to have died (user
    # abort on the first turn, satellite-reported CLI crash). Drives
    # ``is_session_process_dead`` so the dashboard's auto-resume path
    # spawns a fresh session on the next user message — mirrors local
    # ``PersistentSession`` detecting a dead proc via ``returncode``. Both
    # engines are CLIs on the satellite ("CLI process not running" is its
    # own wording), so the name is the satellite's, not one engine's.
    cli_dead: bool = False
    # True while a send_message turn is streaming. The idle reaper must not
    # close a session mid-turn on event silence alone — a network stall on
    # the satellite box leaves the CLI alive and working with zero stream
    # events for many minutes (the Mode D incident).
    turn_active: bool = False
