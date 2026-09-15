"""Proxy-side state of one remote session (the ``RemoteSessionInfo`` record).

A leaf of the remote package: every piece of ``RemoteExecutionLayer`` builds
or annotates this record, so it lives on its own and imports only the
translator and settle types. Split out of remote_execution.py, which
re-exports it.
"""

import asyncio
import time
from dataclasses import dataclass, field

from core.layers.cli.settle import SettleController
from core.layers.cli.translator import ClaudeCLIEventTranslator
from core.layers.codex.layer import CodexEventTranslator


@dataclass
class RemoteSessionInfo:
    """Tracks a remote session's state on the proxy side."""
    session_id: str
    machine_id: str
    agent_name: str
    execution_path: str  # "claude-code-cli" or "codex-cli"
    event_queue: asyncio.Queue
    alive: bool = True
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Codex per-session state (translator persists cumulative token deltas)
    codex_translator: CodexEventTranslator | None = None
    codex_thread_id: str = ""
    # CLI per-session state (created fresh per turn in send_message)
    cli_translator: ClaudeCLIEventTranslator | None = None
    cli_settle: SettleController | None = None
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
    # ``PersistentSession`` detecting a dead proc via ``returncode``.
    cli_dead: bool = False
    # True while a send_message turn is streaming. The idle reaper must not
    # close a session mid-turn on event silence alone — a network stall on
    # the satellite box leaves the CLI alive and working with zero stream
    # events for many minutes (the Mode D incident).
    turn_active: bool = False
    # --- Remote Codex background sub-agent demux (mirrors the LOCAL session's
    # router/supervisor in core/layers/codex/session.py, but consumes the
    # WS-forwarded session_event stream instead of the daemon's notif_queue).
    # Only active for codex-cli on a satellite new enough to forward bg-thread
    # events past the main turn (version-gated; see start_session). When off,
    # _stream_codex_turn reads event_queue directly = today's behavior. ---
    bg_supervised: bool = False
    router_task: asyncio.Task | None = None
    default_consumer: asyncio.Queue | None = None      # active main turn (router → here)
    thread_consumers: dict[str, asyncio.Queue] = field(default_factory=dict)  # sub_tid → buffer
    bg_supervisors: dict[str, asyncio.Task] = field(default_factory=dict)     # sub_tid → supervisor
    # Self-pacing for the codex bg-command drain: at most one satellite
    # codex_bg_terminals RPC per second (the monitor retries every 0.3s).
    _last_bg_terminals_list: float = 0.0
