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
from typing import Any

from core.permission_modes import ASK_MODE


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
    # The object the machine started under the id (its start's ack, 2.0): a
    # machine's word about an earlier object of the id is told apart. "" for
    # a record taken back from a report, or a machine that names none.
    incarnation: str = ""
    # The engine's remote adapter's own per-session record
    # (``RemoteEngineAdapter.init_session`` / ``adopt_state`` create it,
    # ``close_state`` releases it). Read by that adapter only.
    engine_state: object | None = None  # the engine's own state; its adapter casts the reads
    # Permission-mode tracking (native vs hook-based permissions)
    use_native_permissions: bool = False
    # Machine pairing's full-filesystem grant — feeds Codex sandbox-mode
    # re-resolution on mode toggles (must match the spawn-time resolution).
    allow_full_fs: bool = False
    model: str = ""
    mode: str = ASK_MODE
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
    # spawns a fresh session on the next user message. Both
    # engines are CLIs on the satellite ("CLI process not running" is its
    # own wording), so the name is the satellite's, not one engine's.
    cli_dead: bool = False
    # An interrupt was sent and no clean result has followed: the next
    # error result is read for the unsigned thinking block the CLI keeps
    # after an interrupt in mid-thinking (claude-code#63943), repaired by
    # ending the process. Across turns, as the local session's watch.
    post_interrupt_watch: bool = False
    # True while a send_message turn is streaming. The idle reaper must not
    # close a session mid-turn on event silence alone — a network stall on
    # the satellite box leaves the CLI alive and working with zero stream
    # events for many minutes (the Mode D incident).
    turn_active: bool = False
    # Frames the end of a turn read that open the session's next turn (the
    # engine's own, or a steer it took late): the readers between turns
    # take them before the queue (``remote_bg_commands.next_carried``).
    carried: list[dict] = field(default_factory=list)
    # The one reader between turns while it runs (``remote_bg_commands.
    # wake_reader``), and the turn the engine started by itself while it is
    # the chat's live turn (``remote_bg_commands.EngineTurn``).
    reader: asyncio.Task | None = None
    engine_turn: Any = None
    # The turn's own silence clock: the last stream event of THIS turn
    # (``last_activity`` also moves on a steer, which must not restart it),
    # and whether the turn is a task run's (its ceiling is the turn
    # ceiling). Read by ``core/events/turn_life`` through the adapters.
    last_event_at: float = field(default_factory=time.monotonic)
    task_turn: bool = False
    # This turn sent the start's data as the engine's own message (Claude Code
    # on a satellite that takes it): its empty result is expected.
    own_message_turn: bool = False
    # The proxy ended the CLI itself this turn (a Stop's hard path, the
    # watchdog, a typed ending's kill): the session end that follows is
    # that kill, not a lost machine.
    proxy_killed: bool = False
    # The machine's local link port the session was started with (its
    # ``PROXY_URL`` and MCP addresses hold it): a session whose machine moved
    # the link to another port is dead to the platform (``tunnel_state``).
    # 0 = unknown, never judged.
    tunnel_port: int = 0
    # Whether the session's files were copied to its machine (a paired
    # computer, a compute machine on another host) or the machine reads the
    # platform's folder itself (a compute machine beside the platform): the
    # sync choke points and the readers key on it. ``compute``: the machine
    # is a compute machine of either kind (the platform admits, counts and
    # drains its sessions).
    files_synced: bool = True
    compute: bool = False
    # A compute session's prompt file on the platform, removed at the close
    # (the proxy wrote it; the machine's session only read it).
    prompt_file: str = ""
