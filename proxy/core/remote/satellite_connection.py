"""Satellite connection manager — tracks connected satellite daemons.

Each satellite maintains a persistent WebSocket connection to the proxy.
The SatelliteConnectionManager routes commands to the correct satellite
and manages per-session event queues for RemoteExecutionLayer consumption.
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
from fastapi import WebSocket

from auth import roles
from core import placement
from core.background import spawn
from core.remote import satellite_link, tunnel_state

logger = logging.getLogger("otodock.satellite")

#: The word a compute machine beside the platform claims for a tools folder
#: of its own (``compute_tools``).
_TOOLS_OWN = "own"


# What a connection may hold for its writer: this many frames, and
# ``config.SAT_SEND_QUEUE_MAX_BYTES`` of their strings. A machine whose queue
# stays above either for ``_SLOW_READER_GRACE_S``, or passes four times
# either at any moment, reads too slowly: it is dropped (``_drop_slow_reader``)
# and catches up at its reconnect. No frame is dropped while it is kept. A
# burst a healthy machine drains (a scrub's deletes) trips nothing.
_SEND_QUEUE_SIZE = 10_000
_SLOW_READER_GRACE_S = 10.0
_SLOW_READER_HARD_FACTOR = 4
_FRAME_OVERHEAD_BYTES = 64
# A producer that can wait (a tunneled response's chunks) waits while the
# connection holds this much: the upstream is read as fast as the machine
# takes it.
ROOM_BYTES = 8 * 1024 * 1024


def _frame_cost(obj, depth: int = 0) -> int:
    """The bytes a frame's strings hold, at any depth (a ``sync_mcps``
    frame's tarballs sit three levels down)."""
    if isinstance(obj, str):
        return len(obj)
    if depth >= 8:
        return 0
    if isinstance(obj, dict):
        return sum(_frame_cost(v, depth + 1) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return sum(_frame_cost(v, depth + 1) for v in obj)
    return 0

# The bulk credit's measurement: a connection is slow when its credit has been
# busy for this long and the acks of that window carried less than
# _SLOW_ACK_BYTES of file bytes (push_file then sends smaller chunks).
_RATE_WINDOW_S = 2.0
_SLOW_ACK_BYTES = 2 * 1024 * 1024


class CreditFailed(RuntimeError):
    """The connection's bulk credit is gone: the connection was replaced or
    dropped, and nothing may be sent on it any more."""


class _BulkCredit:
    """The file bytes of bulk frames handed to one connection and not yet
    answered by the satellite, bounded by ``cap``.

    The protocol's own ping and pong queue behind every byte already handed
    to the transport (uvloop, the kernel, a reverse proxy, the router), so
    without a bound a large transfer on a slow uplink holds a pong past its
    30 s deadline and the keepalive closes a healthy socket. With the credit
    no more than ``cap`` of file bytes (a third more on the wire) sits ahead
    of a pong. A frame goes alone when nothing is in flight, so a frame
    above the cap is never stuck.

    Admission is first come, first served: a frame waits behind every
    earlier waiter, a release wakes the head only, and a cancelled waiter
    leaves its place to the next, so no push starves another. Every frame's
    bytes come back exactly once, by the done callback of its ack future
    (``push_file``)."""

    def __init__(self, cap: int | None = None):
        if cap is None:
            import config
            cap = int(getattr(config, "BULK_CREDIT_BYTES", 1024 * 1024))
        self.cap = max(1, cap)
        self.inflight = 0
        self.slow = False
        self._tickets: deque[tuple[int, asyncio.Future]] = deque()
        self._failed: CreditFailed | None = None
        self._acked: deque[tuple[float, int]] = deque()
        self._busy_since: float | None = None

    def _fits(self, n: int) -> bool:
        return self.inflight == 0 or self.inflight + n <= self.cap

    def _grant(self, n: int) -> None:
        if self.inflight == 0:
            self._busy_since = time.monotonic()
        self.inflight += n

    def try_take(self, n: int) -> bool:
        """Take ``n`` bytes at once when nobody waits and they fit. Raises
        ``CreditFailed`` once the connection is gone."""
        if self._failed is not None:
            raise self._failed
        if not self._tickets and self._fits(n):
            self._grant(n)
            return True
        return False

    def reserve(self, n: int) -> "asyncio.Future | None":
        """Take ``n`` bytes at once (None), or queue a ticket now and return
        the future its grant resolves. Raises ``CreditFailed`` once the
        connection is gone. A caller that stops waiting hands the future to
        ``abandon``."""
        if self.try_take(n):
            return None
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._tickets.append((n, fut))
        return fut

    def abandon(self, n: int, fut: asyncio.Future) -> None:
        """The holder of ``reserve``'s future stopped waiting: a grant comes
        back, a waiting ticket leaves its place to the next."""
        if fut.done() and not fut.cancelled() and fut.exception() is None:
            self.release(n, acked=False)
            return
        if not fut.done():
            fut.cancel()
        with contextlib.suppress(ValueError):
            self._tickets.remove((n, fut))
        if self._failed is None:
            self._wake()

    async def take(self, n: int) -> None:
        """Wait until ``n`` bytes may go. Raises ``CreditFailed`` once the
        connection is gone."""
        fut = self.reserve(n)
        if fut is None:
            return
        try:
            await asyncio.shield(fut)
        except BaseException:
            self.abandon(n, fut)
            raise

    def _wake(self) -> None:
        while self._tickets:
            n, fut = self._tickets[0]
            if fut.done():
                self._tickets.popleft()
                continue
            if not self._fits(n):
                return
            self._tickets.popleft()
            self._grant(n)
            fut.set_result(None)

    def release(self, n: int, *, acked: bool) -> None:
        """Return ``n`` bytes. ``acked``: the satellite applied them (they
        count towards the rate)."""
        self.inflight = max(0, self.inflight - n)
        now = time.monotonic()
        if acked:
            self._acked.append((now, n))
            self._measure(now)
        if self.inflight == 0:
            self._busy_since = None
        if self._failed is None:
            self._wake()

    def _measure(self, now: float) -> None:
        while self._acked and now - self._acked[0][0] > _RATE_WINDOW_S:
            self._acked.popleft()
        if self._busy_since is not None and now - self._busy_since >= _RATE_WINDOW_S:
            self.slow = sum(n for _, n in self._acked) < _SLOW_ACK_BYTES

    def is_slow(self) -> bool:
        """The last measurement: the link moved less than _SLOW_ACK_BYTES
        per _RATE_WINDOW_S while busy (kept until the next one)."""
        self._measure(time.monotonic())
        return self.slow

    def fail_all(self, why: str = "connection closed") -> None:
        """The connection is gone: every waiter and every later ``take``
        raises."""
        self._failed = CreditFailed(why)
        while self._tickets:
            _n, fut = self._tickets.popleft()
            if not fut.done():
                fut.set_exception(self._failed)


class _XferToken:
    """Rides a bulk frame on the lane: the writer drops the frame unsent
    once its push cancelled it."""
    __slots__ = ("cancelled",)

    def __init__(self) -> None:
        self.cancelled = False

# How long an admin-paired satellite must be CONTINUOUSLY unreachable
# before we fire the "offline" notification to admins. This grace window
# is the whole anti-noise mechanism: proxy restarts, satellite
# auto-updates, and brief network blips all reconnect within seconds (the
# satellite's reconnect backoff caps at 60s), so they never cross this
# threshold and never notify. Only a genuine sustained outage — a machine
# that FAILS to reconnect — reaches admins, and exactly once thanks to the
# persisted `offline_alerted` flag. Evaluated every heartbeat-monitor tick
# (30s), so detection lands within grace + ~30s. Because the decision is
# derived from persisted state (`last_seen` + `offline_alerted`) rather
# than in-memory connect/disconnect edges, it is fully restart-safe.
_OFFLINE_ALERT_GRACE_S = 120.0

# WS-drop resilience: when a satellite WS drops, its
# in-flight session event queues are HELD for this many seconds instead of
# being terminated immediately. A reconnect within the window re-adopts them
# (the satellite keeps the CLI/Codex process alive + buffers/replays its
# outbound events), so the turn continues seamlessly. On expiry the held
# sessions are terminated with a visible ⚠-incomplete marker. Keep it BELOW
# the Codex per-turn 300s read timeout (else the producer self-aborts first);
# it is independent of the dashboard reap (session_idle_seconds returns None
# while a session is held → a refresh mid-grace shows "reconnecting", no reap).
_GRACE_WINDOW_S = 90.0

# The heartbeat monitor: how often it looks, and how long a machine may stay
# silent (satellites send a heartbeat every 20 s).
_MONITOR_TICK_S = 15.0
_SILENT_DROP_S = 75.0

# Terminal message persisted when the grace window lapses without a reconnect
# — a visible marker beats a silent truncation that looks
# like a clean finish.

_ENGINES_MAX = 16          # a bounded list of short ids — the field is satellite-supplied
_ENGINE_ID_MAX_LEN = 64

# Heartbeat persistence cadence. The heartbeat handler refreshes the in-memory
# ``last_heartbeat`` / ``last_seen_iso`` on EVERY heartbeat (20 s) but hands a
# ``last_seen`` write to the persister at most this often per machine — plus
# immediately on a status TRANSITION (disconnected → online). One COMMIT per
# machine per minute instead of three, and never on the event loop (the 09-03
# stall: a slow disk made every heartbeat's synchronous commit freeze the
# whole proxy). The live badge never reads the DB for a connected machine
# (services/remote/remote_status.py), so the only visible effect is the admin
# list's "last seen" text lagging ≤ this many seconds.
HEARTBEAT_PERSIST_INTERVAL_S = 60.0

# Persist lanes, in flush order. Each lane holds the NEWEST requested value per
# machine; a single flusher task per machine writes them off-loop in this order
# (status before capabilities so a fresh row never shows stale caps with a new
# status). Superseded values are never written.
_PERSIST_LANE_ORDER = ("status", "caps", "paused", "update_result", "pending_update")


def _persist_write(lane: str, machine_id: str, value) -> None:
    """Execute one lane's write. Resolves the store function at CALL time so
    test patches on ``storage.remote_store`` take effect."""
    from storage import remote_store
    if lane == "status":
        status, last_seen = value
        remote_store.update_machine_status(
            machine_id, status, last_seen=last_seen or None,
        )
    elif lane == "caps":
        remote_store.update_machine_capabilities(machine_id, value)
    elif lane == "paused":
        remote_store.set_paused(machine_id, bool(value))
    elif lane == "update_result":
        remote_store.record_update_result(machine_id, **value)
    elif lane == "pending_update":
        remote_store.set_pending_update(machine_id, bool(value))
    else:  # pragma: no cover — programming error
        raise ValueError(f"unknown persist lane {lane!r}")


@dataclass
class _ManifestJoin:
    """The pages of one paged manifest reply joined so far: the entries,
    their serialized size, and the index the next page must carry."""
    entries: list = field(default_factory=list)
    size: int = 0
    next_page: int = 0


@dataclass
class SatelliteConnection:
    """Per-machine connection state."""
    machine_id: str
    ws: WebSocket
    connected_at: float = field(default_factory=time.monotonic)
    last_heartbeat: float = field(default_factory=time.monotonic)
    # Signed clock offset (proxy_utc − satellite_utc, seconds), measured from each
    # heartbeat's UTC ``timestamp``. Used by the file-sync merge to adjust the
    # satellite's epoch mtimes into the proxy clock before ordering a divergence.
    # None until the first heartbeat arrives → the merge treats divergences as
    # un-orderable (platform-wins). See ``core/remote/file_sync.py``.
    clock_offset: float | None = None
    # The platform sent this machine its own outgoing proxy at the sign-in
    # (``mcp_egress.exit_upstream_block``): its exit reaches the world through
    # it, known before the machine's first heartbeat says so.
    exit_upstream_sent: bool = False
    capabilities: dict = field(default_factory=dict)
    # Reported SATELLITE_VERSION from the auth message.
    satellite_version: str = ""
    # Wall-clock ISO of the last contact (auth or heartbeat). The DB's
    # ``last_seen`` is a COALESCED copy of this (HEARTBEAT_PERSIST_INTERVAL_S);
    # deregister / the heartbeat monitor persist the exact value on the way
    # down so the admin offline-alert downtime clock starts at the true last
    # contact.
    last_seen_iso: str = ""
    # Last status handed to the persister for this connection ("" = none yet)
    # + when the last ``last_seen`` write was requested (monotonic). Drive the
    # heartbeat coalescing in ``_maybe_persist_heartbeat``.
    persisted_status: str = ""
    last_seen_persisted_at: float = 0.0
    # Cached ``remote_machines`` columns read off-loop at register (and
    # refreshed by the admin endpoint via ``note_max_sessions``) so the
    # capacity pre-check and the admin stats never touch the DB on the loop.
    # ``max_sessions`` None = no admin override (fail-open, as before).
    max_sessions: int | None = None
    name: str = ""
    # Idle-sync: latest cheap STAT fingerprint per agent slug reported on the
    # heartbeat (`agent_fingerprints`), and the fingerprint as of the last COMPLETED
    # idle merge (`synced_fingerprints`). The periodic sweep runs the merge for a
    # connected-IDLE (machine, agent) only when latest != synced. `synced` seeds =
    # the first reported value (NO merge — initial catch-up is reconnect-sync /
    # session-start's job), so a proxy restart never triggers a merge burst.
    agent_fingerprints: dict[str, str] = field(default_factory=dict)
    synced_fingerprints: dict[str, str] = field(default_factory=dict)
    # Per-satellite budget telemetry from the heartbeat. ``load`` =
    # {"cpu_pct","mem_pct"} of the satellite host; ``reported_sessions`` = its live
    # session count (headless + interactive PTY + native-CLI/otodock). This is the
    # AUTHORITATIVE source for the admin per-satellite display + the soft capacity
    # pre-check — the proxy's own session_queues are blind to interactive/native-CLI.
    # The sign-in's own count (0.5.171) holds until the first heartbeat;
    # ``sessions_reported`` is False while neither gave one (the admin display
    # reads that as not known, never as 0).
    load: dict = field(default_factory=dict)
    reported_sessions: int = 0
    sessions_reported: bool = False
    # A compute machine's memory (0.5.173: at its sign-in and in every
    # heartbeat) and when it came (monotonic): the admission gates read it
    # for the sessions it runs (``core/concurrency``).
    memory: dict = field(default_factory=dict)
    memory_at: float = 0.0
    # The machine's memory guard (0.5.177): True while it runs, "stalled",
    # or None when the machine says nothing (an older satellite, a Mac).
    memory_guard: object = None
    # Per-session event queues: session_id -> asyncio.Queue
    session_queues: dict[str, asyncio.Queue] = field(default_factory=dict)
    # Track execution_path per session for event translation
    session_execution_paths: dict[str, str] = field(default_factory=dict)
    # Outbound send lanes — the writer task is the ONLY coroutine that touches
    # ws.send_text(). Multiple producers enqueue concurrently without racing on
    # the wire (WS frames are not coroutine-atomic). TWO lanes: ``send_queue``
    # carries CONTROL frames (commands, acks, pong, deletes, tunnel responses)
    # and ``bulk_queue`` carries file_push DATA chunks. The writer always drains
    # control first and re-checks it between every bulk frame, so a large
    # multi-chunk file push delays a command ack by at most one chunk. The
    # protocol's ping and pong bypass both lanes and queue behind every byte
    # already handed to the transport: ``bulk_credit`` bounds those bytes.
    # ``send_wakeup`` is set by enqueue_send to wake the writer
    # when either lane gains an item (lets it serve two lanes without cancelling
    # a blocking Queue.get()).
    send_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    bulk_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    send_wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    writer_task: asyncio.Task | None = None
    # What both lanes hold for the writer, counted at the enqueue and given
    # back when the writer takes a frame; when the count last went over the
    # bound (monotonic, None under it); the manager's hook for a reader
    # judged too slow, called once; and ``room``, set while the connection
    # holds less than ``ROOM_BYTES`` (a producer that can wait waits on it).
    queued_frames: int = 0
    queued_bytes: int = 0
    _costs: tuple[deque, deque] = field(default_factory=lambda: (deque(), deque()))
    over_since: float | None = None
    on_slow_reader: "Callable[[SatelliteConnection, str], None] | None" = None
    slow_dropped: bool = False
    room: asyncio.Event = field(default_factory=asyncio.Event)
    # The file bytes in flight on this connection (push_file) and the time
    # of its latest inbound transfer frame (an ack of a bulk frame or any
    # file_content, monotonic): a transfer waiting behind another one on a
    # moving link is not stalled.
    bulk_credit: _BulkCredit = field(default_factory=_BulkCredit)
    last_transfer_at: float = 0.0
    last_frame_at: float = 0.0
    # The machine's local link (the loopback tunnel its sessions call the
    # platform through), as the satellite reports it at auth and in each
    # heartbeat (``core/remote/tunnel_state.py``). ``tunnel_down_since`` is
    # monotonic.
    tunnel_ok: bool = True
    tunnel_rebinds: int = 0
    tunnel_error: str = ""
    tunnel_down_since: float | None = None
    # The platform's own machine (``remote_machines.pairing_scope`` compute),
    # set at its sign-in: one beside the platform shares its folder, so no
    # file flow and no fan-out ever names it, and the tunnel serves it the
    # gateway. ``own_copy``: a compute machine on another host, which works on
    # its own copy of the folders (``placement.compute_own_copy``).
    # ``own_tools``: one beside the platform that claimed a tools folder of
    # its own (``compute_tools: own``): it builds the community tools there.
    compute: bool = False
    own_copy: bool = False
    own_tools: bool = False
    compute_address: str = ""

    def __post_init__(self) -> None:
        self.room.set()

    async def enqueue_send(self, msg: dict, *, bulk: bool = False) -> None:
        """Queue a message for the writer task.

        ``bulk=True`` routes the message to the BULK lane (file_push data
        chunks) instead of the CONTROL lane; the writer always drains control
        first, so a large transfer can never delay command acks. Safe to call
        from any coroutine. Returns immediately — the actual ws.send happens
        inside `_writer_loop`.
        """
        self.enqueue_send_nowait(msg, bulk=bulk)

    def enqueue_send_nowait(self, msg: dict, *, bulk: bool = False) -> None:
        """``enqueue_send`` for a caller that cannot await (a cleanup path).
        Nothing is queued for a connection already dropped as a slow reader."""
        if self.slow_dropped:
            return
        queue = self.bulk_queue if bulk else self.send_queue
        cost = _FRAME_OVERHEAD_BYTES + _frame_cost(msg)
        queue.put_nowait(msg)
        self._costs[bulk].append(cost)
        self.queued_frames += 1
        self.queued_bytes += cost
        if self.queued_bytes >= ROOM_BYTES:
            self.room.clear()
        self._judge()
        # Wake the writer whichever lane received the item.
        self.send_wakeup.set()

    def _judge(self) -> None:
        import config
        max_bytes = int(getattr(config, "SAT_SEND_QUEUE_MAX_BYTES", 64 * 1024 * 1024))
        if self.queued_bytes <= max_bytes and self.queued_frames <= _SEND_QUEUE_SIZE:
            self.over_since = None
            return
        now = time.monotonic()
        if self.over_since is None:
            self.over_since = now
        hard = (self.queued_bytes > _SLOW_READER_HARD_FACTOR * max_bytes
                or self.queued_frames > _SLOW_READER_HARD_FACTOR * _SEND_QUEUE_SIZE)
        if hard or now - self.over_since >= _SLOW_READER_GRACE_S:
            self._slow_reader(
                f"{self.queued_frames} frame(s), {self.queued_bytes // 1024} KiB waiting"
                + ("" if hard else f" for {now - self.over_since:.0f} s"))

    def _slow_reader(self, why: str) -> None:
        if self.slow_dropped:
            return
        self.slow_dropped = True
        self.room.set()
        if self.on_slow_reader is not None:
            self.on_slow_reader(self, why)

    def frame_taken(self, bulk: bool) -> int:
        """The writer took a frame off a lane: its bytes come back. Returns
        the cost (a requeue after a failed send hands it to ``frame_back``)."""
        costs = self._costs[bulk]
        cost = costs.popleft() if costs else 0
        self.queued_frames = max(0, self.queued_frames - 1)
        self.queued_bytes = max(0, self.queued_bytes - cost)
        if self.queued_bytes < ROOM_BYTES:
            self.room.set()
        if self.over_since is not None:
            self._judge()
        return cost

    def frame_back(self, bulk: bool, msg: dict, cost: int) -> None:
        """A frame whose send failed goes back to its lane, counted again."""
        (self.bulk_queue if bulk else self.send_queue).put_nowait(msg)
        self._costs[bulk].append(cost)
        self.queued_frames += 1
        self.queued_bytes += cost


def _list_admin_subs() -> list[str]:
    """All platform admins. Used as the notify-target for admin-paired
    satellite up/down events."""
    from storage import database as task_store
    return [u["sub"] for u in task_store.list_users() if roles.is_admin(u.get("role"))]


async def _notify_admins_machine_state_change(
    machine_id: str, *, online: bool,
) -> None:
    """Fire a notification to every platform admin when an admin-paired
    satellite or a compute machine changes connection state. No-op for
    user-paired machines (their owner already sees the soft-fallback banner).

    `online=True` fires the back-online notice; `online=False` fires the
    just-went-offline notice. Skips silently when the machine row is
    missing or is a person's own.
    """
    from services.notifications import notification_manager
    from storage import remote_store
    from storage.pg import run_db

    machine = await run_db(remote_store.get_remote_machine, machine_id)
    compute = placement.machine_is_compute(machine)
    if not (compute or placement.machine_is_admin_paired(machine)):
        return

    machine_name = machine.get("name") or placement.log_id(machine_id)
    admin_subs = await run_db(_list_admin_subs)
    if not admin_subs:
        return

    if compute and online:
        title = f"Compute machine online: {machine_name}"
        body = f"The compute machine '{machine_name}' is connected again. Chats, tasks, calls and apps run on it."
        severity = "info"
    elif compute:
        title = f"Compute machine offline: {machine_name}"
        body = (
            f"The compute machine '{machine_name}' is not connected. Chats, tasks, calls "
            f"and apps wait or are refused until it is back. Its log says why."
        )
        severity = "warning"
    elif online:
        title = f"Remote machine online: {machine_name}"
        body = f"The platform remote machine '{machine_name}' has reconnected."
        severity = "info"
    else:
        title = f"Remote machine offline: {machine_name}"
        body = (
            f"The platform remote machine '{machine_name}' has gone offline. "
            f"Agents pinned to this machine will not run until it reconnects."
        )
        severity = "warning"

    for sub in admin_subs:
        try:
            await notification_manager.fire_notification(
                title=title, body=body, severity=severity,
                scope="user", target=sub,
                source="satellite", source_id=machine_id,
            )
        except Exception:
            logger.exception(
                "Failed to fire admin satellite-state notification to %s",
                sub[:16],
            )


from core.remote import machine_memory
from core.remote.satellite_admin_alerts import SatelliteAdminAlertsMixin
from core.remote.satellite_file_transfer import (
    SatelliteFileTransferMixin,
    _FileChangedLane,
    _PullStream,
    evict_file_changed_stamps,
)
from core.remote.satellite_grace import HeldLines, SatelliteGraceMixin


def _note_memory(conn: SatelliteConnection, memory: object, guard: object = None) -> None:
    """Keep a machine's memory report (whole numbers, its cap and known flags,
    nothing else), when it came, and its guard's state. A compute machine
    sends one since 0.5.173, a Linux machine whose guard runs since 0.5.177; a
    report that says it read nothing is not kept."""
    if guard is True or guard == "stalled":
        conn.memory_guard = guard
    if not isinstance(memory, dict) or memory.get("known") is False:
        return
    kept = {k: int(memory[k]) for k in ("total_bytes", "available_bytes", "swap_free_bytes")
            if isinstance(memory.get(k), int) and not isinstance(memory.get(k), bool) and memory[k] >= 0}
    if "total_bytes" not in kept or "available_bytes" not in kept:
        return
    kept["capped"] = memory.get("capped") is True
    conn.memory = kept
    conn.memory_at = time.monotonic()
    if not getattr(conn, "compute", False):
        from core import concurrency
        concurrency.machine_report(conn.machine_id)


#: Why a machine says it ended a turn itself: its memory guard ended the
#: engine (0.5.177), it dropped a Codex daemon that did not take the
#: interrupt this side asked for, or the engine's process exited in the turn.
TURN_END_REASONS = frozenset({"memory", "interrupted", "exited"})
#: Why a machine says a session's engine is gone with no turn reading it
#: (``session_ended``): the process exited, or its memory guard ended it.
ENGINE_GONE_REASONS = frozenset({"memory", "exited"})
#: The most of an exited engine's last line the turn's reader is handed.
_LAST_LINE_MAX = 300


def turn_end_of(msg: dict) -> dict:
    """What a machine's ``turn_ended`` frame says of the end, as the turn's
    reader takes it: a reason this side knows and, for an engine that
    exited, its exit code (a whole number) and the last line it said (one
    bounded line). Nothing else of the frame."""
    reason = msg.get("reason")
    if not isinstance(reason, str) or reason not in TURN_END_REASONS:
        return {}
    out: dict = {"reason": reason}
    if reason == "exited":
        code, line = msg.get("exit_code"), msg.get("last_line")
        if isinstance(code, int) and not isinstance(code, bool):
            out["exit_code"] = code
        if isinstance(line, str) and line.strip():
            out["last_line"] = " ".join(line.split())[:_LAST_LINE_MAX]
    return out


class SatelliteConnectionManager(
    SatelliteFileTransferMixin,
    SatelliteGraceMixin,
    SatelliteAdminAlertsMixin,
):
    """Singleton that manages all satellite WebSocket connections.

    Provides:
    - Connection registration/deregistration
    - Command sending with ack/nack futures
    - Per-session event queue routing
    - Heartbeat monitoring
    """

    def __init__(self):
        self._connections: dict[str, SatelliteConnection] = {}  # machine_id -> conn
        # command_id -> (machine_id, future, connection the frame went out on)
        # so deregister rejects every pending ack of a machine and a
        # replacement those of the replaced connection, instead of letting
        # them wait out their timeout.
        self._pending_acks: dict[str, tuple[str, asyncio.Future, SatelliteConnection | None]] = {}
        # machine_id -> the wait for its ``update_received`` after an online
        # "Update now" push (api/remote/remote_machines.py).
        self._update_acks: dict[str, asyncio.Future] = {}
        # The pages of a paged manifest reply, by command id, until the last
        # frame resolves the wait (cleared with the ack on every other end).
        self._manifest_pages: dict[str, _ManifestJoin] = {}
        # request_id -> _PullStream for streaming file pulls. Kept separate
        # from _pending_acks (which is push acks): file_content chunks are
        # written straight to a .partial on disk and the future resolves on
        # the final chunk. Rejected + cleaned up on deregister.
        self._pending_pulls: dict[str, _PullStream] = {}
        # file_changed appliers: one bounded lane per machine inside one
        # global bound (SatelliteFileTransferMixin._dispatch_file_changed).
        import config as _cfg
        self._fc_lanes: dict[str, _FileChangedLane] = {}
        self._fc_global_sem = asyncio.Semaphore(_cfg.SAT_FILE_CHANGED_CONCURRENCY_GLOBAL)
        # Per-machine lock for MCP install/sync orchestration. Concurrent
        # warmups on the same machine serialize so they don't race on
        # in-flight installs. Acquired by sync_mcps_for_session; not held
        # during routine session work.
        self._install_locks: dict[str, asyncio.Lock] = {}
        # Per-(machine, agent) workspace-sync lock so two concurrent warmups of
        # the same machine+agent don't double-apply / race the merge base. The
        # per-FILE serialization (vs live write-backs) is the separate global path
        # lock in remote_file_flow, taken per file inside the sync.
        self._sync_locks: dict[tuple[str, str], asyncio.Lock] = {}
        # When each machine's connection last went away (monotonic), cleared
        # at its next register: a resume probe waits for a machine that
        # dropped within the grace window and refuses at once for one away
        # longer (``machine_dropped_recently``).
        self._last_drop_at: dict[str, float] = {}
        self._held_lines: dict[str, HeldLines] = {}
        # Per-command install progress callbacks. mcp_sync registers one
        # before issuing sync_mcps and removes it after the ack arrives.
        self._install_progress_cbs: dict[str, Callable[[dict], Any]] = {}
        # Per-command step output callbacks (APPS.md "Steps"): app_steps
        # registers one around its step_run send_command so the script's
        # lines reach the app log as they arrive.
        self._step_output_cbs: dict[str, Callable[[str], Any]] = {}
        # Per-machine reconnect-grace holding area. On a WS
        # drop we move the connection's in-flight session_queues here
        # (machine_id -> {session_id -> (queue, execution_path)}) + start a
        # grace timer, instead of terminating them; register() re-adopts them
        # on reconnect. See deregister / register / _expire_grace.
        self._grace_sessions: dict[str, dict[str, tuple[asyncio.Queue, str]]] = {}
        self._grace_timers: dict[str, asyncio.Task] = {}
        # Interactive PTYs survive a WS blip the same way. On a
        # drop we hold the machine's RemotePtyProcess handles (kept in place in
        # core.remote.remote_pty's registry — NOT moved) + start a per-machine timer
        # here, instead of killing them; the satellite's post-auth `pty_alive`
        # reconciles them on reconnect. See deregister / _reconcile_ptys /
        # _expire_pty_grace.
        self._pty_grace_timers: dict[str, asyncio.Task] = {}
        # Abort-ack events keyed by (machine_id, session_id). Armed (cleared)
        # by the layer before it sends an abort; set when the satellite replies
        # with `session_aborted`. The auto-resume path awaits this (with a
        # timeout) so it doesn't race the still-dying CLI subprocess.
        self._abort_acked_events: dict[tuple[str, str], asyncio.Event] = {}
        # Run-recovery hook: async fn(machine_id, sessions) fed by the
        # satellite's post-auth `sessions_alive` report (Mode C re-adopt).
        # Registered at startup by core.remote.run_recovery.
        self._sessions_alive_callback = None
        # A machine connected or left: fn(machine_id), on the loop with no
        # await, so it only schedules its work. Set at startup by ws.satellite
        # (the pages' frame).
        self._link_callback: Callable[[str], None] | None = None
        # A session's line reached its queue: fn(session_id), on the loop with
        # no await. Set by the remote layer (its one reader between turns).
        self._line_listener: Callable[[str], None] | None = None
        # Per-machine link state: the reconnect steps' windows, the machine
        # row's cache and the alive report of each sign-in. There is no lock
        # around register / deregister / the grace expiries: each of their
        # sections runs with no await, so it is atomic on the loop.
        self._links = satellite_link.MachineLinks(self)
        # Off-loop persistence of per-machine DB state (status/last_seen,
        # capabilities, paused, update result). ``_persist_pending`` holds the
        # NEWEST requested value per machine per lane and is touched only from
        # the loop thread; ``_persist_tasks`` holds at most ONE flusher task per
        # machine, which drains the lanes in order on the dedicated DB executor
        # (storage.pg.run_db). Newest-wins gives ordering (a fast reconnect's
        # ``online`` can never be overtaken by the preceding ``disconnected``)
        # and coalescing (a burst of heartbeats is one write). See
        # ``_request_persist`` / ``_persist_flusher``.
        self._persist_pending: dict[str, dict[str, object]] = {}
        self._persist_tasks: dict[str, asyncio.Task] = {}
        self._persist_warned_at: dict[tuple[str, str], float] = {}

    def set_sessions_alive_callback(self, cb) -> None:
        """Register the run-recovery handler for `sessions_alive` reports."""
        self._sessions_alive_callback = cb

    def set_line_listener(self, cb: Callable[[str], None] | None) -> None:
        """Register what hears a session's line reach its queue."""
        self._line_listener = cb

    def set_link_callback(self, cb: Callable[[str], None] | None) -> None:
        """Register what hears a machine's connect and leave."""
        self._link_callback = cb

    def _link_changed(self, machine_id: str) -> None:
        cb = self._link_callback
        if cb is None:
            return
        try:
            cb(machine_id)
        except Exception:  # a listener's fault must not undo a connect or a drop
            logger.exception("Satellite %s: link callback failed", placement.log_id(machine_id))

    # --- Off-loop persistence (status / caps / paused / update_result / pending_update) ---

    def persist_lane(self, machine_id: str, lane: str, value) -> None:
        """Public entry for other modules (the auth handshake in
        ``ws/satellite.py``): queue a newest-wins off-loop write."""
        self._request_persist(machine_id, lane, value)

    def _request_persist(self, machine_id: str, lane: str, value) -> None:
        """Record the newest desired ``value`` for ``lane`` and make sure a
        flusher is running for this machine. Loop-thread only; returns at
        once — the DB write happens on the DB executor."""
        if lane not in _PERSIST_LANE_ORDER:
            raise ValueError(f"unknown persist lane {lane!r}")
        self._persist_pending.setdefault(machine_id, {})[lane] = value
        task = self._persist_tasks.get(machine_id)
        if task is None or task.done():
            self._persist_tasks[machine_id] = asyncio.create_task(
                self._persist_flusher(machine_id),
                name=f"sat-persist-{placement.log_id(machine_id)}",
            )

    async def _persist_flusher(self, machine_id: str) -> None:
        """Drain this machine's pending lanes until none are left. A failed
        write keeps its value pending (a NEWER value requested meanwhile
        wins) and is retried by the next request — the heartbeat cadence
        guarantees one within HEARTBEAT_PERSIST_INTERVAL_S — never by a tight
        loop against a sick database. Loop-closed / cancellation are silent
        (shutdown, tests)."""
        from storage.pg import run_db
        try:
            while True:
                pending = self._persist_pending.pop(machine_id, None)
                if not pending:
                    return
                failed: dict[str, object] = {}
                for lane in _PERSIST_LANE_ORDER:
                    if lane not in pending:
                        continue
                    value = pending[lane]
                    try:
                        await run_db(_persist_write, lane, machine_id, value)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001  # the persist flusher's per-lane write: one lane must not drop the rest
                        self._warn_persist_failure(machine_id, lane, e)
                        failed[lane] = value
                if failed:
                    # Re-park the failed values under anything newer that
                    # arrived during the write; do NOT loop on them.
                    fresh = self._persist_pending.get(machine_id)
                    if fresh is None:
                        self._persist_pending[machine_id] = failed
                        return
                    for lane, value in failed.items():
                        fresh.setdefault(lane, value)
                    return
        except asyncio.CancelledError:
            return
        except RuntimeError:
            # Event loop closed underneath us (process exit / test teardown).
            return
        finally:
            if self._persist_tasks.get(machine_id) is asyncio.current_task():
                self._persist_tasks.pop(machine_id, None)

    def _warn_persist_failure(self, machine_id: str, lane: str, err: Exception) -> None:
        now = time.monotonic()
        key = (machine_id, lane)
        if now - self._persist_warned_at.get(key, 0.0) < 60.0:
            return
        self._persist_warned_at[key] = now
        logger.warning(
            "Satellite %s: persisting %s failed (%s: %s); will retry on the "
            "next request", placement.log_id(machine_id), lane, type(err).__name__, err,
        )

    async def drain_persists(self, timeout: float = 5.0) -> bool:
        """Await every in-flight flusher (tests, shutdown). True when all
        landed within ``timeout``; False if some are still running (a stalled
        DB) — the caller decides whether to wait longer."""
        tasks = [t for t in self._persist_tasks.values() if not t.done()]
        if not tasks:
            return True
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        return not pending

    def _persist_status(self, conn, status: str, last_seen: str | None) -> None:
        """Hand a status (+ optional last_seen) write to the persister and
        record what we asked for on the connection. ``conn`` may be a bare
        test stand-in — attribute access is defensive."""
        machine_id = getattr(conn, "machine_id", None)
        if not machine_id:
            return
        try:
            conn.persisted_status = status
            if last_seen:
                conn.last_seen_persisted_at = time.monotonic()
        except AttributeError:  # pragma: no cover — frozen stand-ins
            pass
        self._request_persist(machine_id, "status", (status, last_seen))

    def _maybe_persist_heartbeat(self, conn) -> None:
        """Coalesced heartbeat persistence: write ``online`` + ``last_seen``
        on a status transition or when the last write is older than
        HEARTBEAT_PERSIST_INTERVAL_S; otherwise the in-memory refresh is all
        that happens."""
        from storage import remote_store
        now = time.monotonic()
        transition = getattr(conn, "persisted_status", "") != remote_store.STATUS_ONLINE
        stale = (
            now - float(getattr(conn, "last_seen_persisted_at", 0.0) or 0.0)
            >= HEARTBEAT_PERSIST_INTERVAL_S
        )
        if transition or stale:
            self._persist_status(conn, remote_store.STATUS_ONLINE,
                                 getattr(conn, "last_seen_iso", "") or None)

    async def _refresh_machine_cache(self, machine_id: str, conn: SatelliteConnection) -> None:
        """Read the admin ``max_sessions`` override + machine name off-loop
        and cache them on the connection (only while it is still the live
        one). Fail-open on any error: the caches stay None/"" and the
        capacity pre-check falls back to the satellite's own recommendation
        exactly as before."""
        from storage import remote_store
        from storage.pg import run_db
        try:
            m = await run_db(remote_store.get_remote_machine, machine_id)
        except psycopg.Error:
            logger.debug("machine cache refresh failed for %s", placement.log_id(machine_id), exc_info=True)
            return
        if not m or self._connections.get(machine_id) is not conn:
            return
        raw = m.get("max_sessions")
        try:
            conn.max_sessions = int(raw) if raw else None
        except (TypeError, ValueError):
            conn.max_sessions = None
        conn.name = m.get("name") or ""
        link = self._links.get(machine_id)
        link.max_sessions, link.name = conn.max_sessions, conn.name

    def note_max_sessions(self, machine_id: str, value: int | None) -> None:
        """Admin endpoint hook: refresh the cached override for a connected
        machine. Loop-thread only (callers on a worker thread use
        ``loop.call_soon_threadsafe``)."""
        conn = self._connections.get(machine_id)
        if conn is not None:
            conn.max_sessions = int(value) if value else None
            self._links.get(machine_id).max_sessions = conn.max_sessions

    def get_sync_lock(self, machine_id: str, agent_slug: str) -> asyncio.Lock:
        """Return the per-(machine, agent) workspace-sync lock (lazily created).
        Single-threaded event loop → the get-or-create is race-free (no await)."""
        key = (machine_id, agent_slug)
        lock = self._sync_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._sync_locks[key] = lock
        return lock

    def get_clock_offset(self, machine_id: str) -> float | None:
        """Signed clock offset (proxy_utc − satellite_utc, seconds) for a machine,
        or None if no heartbeat has measured it yet. Fed to the file-sync merge."""
        conn = self._connections.get(machine_id)
        return conn.clock_offset if conn is not None else None

    @property
    def links(self) -> satellite_link.MachineLinks:
        """Every machine's link state (``satellite_link``)."""
        return self._links

    def get_connection(self, machine_id: str) -> "SatelliteConnection | None":
        """The live connection object for a machine, or None. Used by the
        idle-sync sweep to read per-agent stat-fingerprints + advance the synced
        baseline on the connection."""
        return self._connections.get(machine_id)

    def arm_abort_acked(self, machine_id: str, session_id: str) -> None:
        """Reset the abort-ack event before sending an abort (call once per abort)."""
        self._abort_acked_events[(machine_id, session_id)] = asyncio.Event()

    def _signal_abort_acked(self, machine_id: str, session_id: str) -> None:
        ev = self._abort_acked_events.get((machine_id, session_id))
        if ev is not None:
            ev.set()

    async def wait_abort_acked(
        self, machine_id: str, session_id: str, timeout: float = 16.0,
    ) -> bool:
        """Wait for the satellite's `session_aborted` ack. Returns True if it
        arrived, False on timeout (a lost message: the caller proceeds anyway
        after draining). Pops the event.

        Timeout covers the satellite's worst-case graceful kill (≈10s signal
        wait + ≈5s tree-kill) before it sends the ack."""
        ev = self._abort_acked_events.get((machine_id, session_id))
        if ev is None:
            return False
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._abort_acked_events.pop((machine_id, session_id), None)


    def register_install_progress(self, command_id: str, cb) -> None:
        """Register a callback for mcp_install_progress events matching
        the given command_id. Called by mcp_sync around send_command.
        """
        self._install_progress_cbs[command_id] = cb

    def unregister_install_progress(self, command_id: str) -> None:
        self._install_progress_cbs.pop(command_id, None)

    def register_step_output(self, command_id: str, cb) -> None:
        """A callback for the ``step_output`` frames of one ``step_run``
        command (APPS.md "Steps"); ``cb(text)`` may be a coroutine."""
        self._step_output_cbs[command_id] = cb

    def unregister_step_output(self, command_id: str) -> None:
        self._step_output_cbs.pop(command_id, None)

    def get_install_lock(self, machine_id: str) -> asyncio.Lock:
        """Return the per-machine install lock, creating if needed.

        Used by mcp_sync to serialize concurrent sync_mcps invocations from
        multiple warmups on the same satellite.
        """
        lock = self._install_locks.get(machine_id)
        if lock is None:
            lock = asyncio.Lock()
            self._install_locks[machine_id] = lock
        return lock

    # --- Writer task (coroutine-safe ws.send) ---

    async def _writer_loop(self, conn: SatelliteConnection) -> None:
        """Single coroutine that owns conn.ws.send_text() for one connection.

        Multiple producers (send_command, file_push, fire-and-forget, HTTP
        tunnel frames) all enqueue via conn.enqueue_send(). This task dequeues
        and writes serially so concurrent producers never interleave WS frames.

        TWO lanes, drained CONTROL-FIRST: a control frame is sent whenever one
        is ready, and the lane is re-checked between every bulk (file_push)
        frame — so a large multi-chunk transfer on the bulk lane can delay a
        command ack by at most one chunk, never the whole file. A bulk frame
        whose push was abandoned (its ``_xfer`` token cancelled) is dropped
        unsent. When both lanes are empty the writer clears + re-checks
        ``send_wakeup`` (closing the lost-wakeup window) then awaits it, so it
        never busy-waits and never cancels a blocking Queue.get().

        On send failure: re-queue the message to its own lane (slight
        reordering accepted over losing it) then trigger deregister so callers
        wake up immediately instead of waiting for the heartbeat monitor. A
        frame that cannot be encoded is not a send failure: it is dropped and
        its waiter failed (``_drop_unencodable``), the machine kept.
        """
        try:
            while True:
                # Control-first selection: prefer a ready control frame, else
                # take a single bulk frame, else sleep until a producer signals.
                lane = conn.send_queue
                try:
                    msg = lane.get_nowait()
                except asyncio.QueueEmpty:
                    lane = conn.bulk_queue
                    try:
                        msg = lane.get_nowait()
                    except asyncio.QueueEmpty:
                        conn.send_wakeup.clear()
                        # Re-check after clear: a producer that enqueued between
                        # the get_nowait misses above and the clear is caught
                        # here; one that enqueues after the clear re-sets the
                        # event we await below. No lost wakeup, no busy-wait.
                        if not (conn.send_queue.empty() and conn.bulk_queue.empty()):
                            continue
                        await conn.send_wakeup.wait()
                        continue
                cost = conn.frame_taken(lane is conn.bulk_queue)
                if not isinstance(msg, dict):
                    continue
                token = msg.pop("_xfer", None)
                if token is not None and token.cancelled:
                    continue
                try:
                    text = json.dumps(msg)
                except (TypeError, ValueError) as e:
                    # A frame that cannot be encoded is this frame's fault,
                    # never the socket's: drop it, keep the machine.
                    self._drop_unencodable(conn, msg, e)
                    continue
                try:
                    await conn.ws.send_text(text)
                except Exception as e:  # noqa: BLE001  # the writer loop's send: a raise would end the writer silently
                    logger.warning(
                        "Satellite %s writer send failed: %s; requeueing %s",
                        conn.machine_id[:8], e,
                        msg.get("type"),
                    )
                    conn.frame_back(lane is conn.bulk_queue, msg, cost)
                    # Fire-and-forget deregister: don't await ourselves.
                    # Identity-guarded — a send failure on a connection that
                    # a duplicate reconnect already replaced must not tear
                    # down the fresh one.
                    spawn(self.deregister(conn.machine_id, expected=conn),
                          name="deregister", owner="satellite_connection")
                    return
        except asyncio.CancelledError:
            return

    def _drop_unencodable(self, conn: SatelliteConnection, msg: dict, error: Exception) -> None:
        """Log a frame the writer could not encode and fail its waiter at once
        (a command's ack by ``command_id``, a file pull by ``request_id``)
        instead of leaving it to its timeout. A frame with no waiter here (a
        tunnel frame) is only logged; an online update push's ack (keyed by
        machine) is left to its timeout, the push being strings only."""
        kind = msg.get("type") or "frame"
        logger.error("Satellite %s: dropped a %s frame that could not be encoded: %s",
                     conn.machine_id[:8], kind, error)
        why = f"Satellite {conn.machine_id[:8]}: the {kind} request could not be encoded"
        entry = self._pending_acks.get(str(msg.get("command_id") or ""))
        if entry is not None and not entry[1].done():
            entry[1].set_exception(RuntimeError(why))
        st = self._pending_pulls.pop(str(msg.get("request_id") or ""), None)
        if st is not None:
            self._cleanup_pull_stream(st)
            if not st.future.done():
                st.future.set_exception(RuntimeError(why))

    # --- Connection lifecycle ---

    async def register(
        self, machine_id: str, ws: WebSocket, capabilities: dict,
        satellite_version: str = "", *, report_id: str = "", compute: bool = False,
        name: str = "", compute_address: str = "",
    ) -> SatelliteConnection:
        """Register a new satellite connection. Closes old connection if duplicate.

        The swap (the old entry replaced, its sessions and the ones held in
        the reconnect window carried over, its waiters failed) runs with no
        await: it is atomic on the loop, with ``deregister`` and the grace
        expiries, and no machine waits on another's registration. The old
        socket is closed after it. ``report_id`` names this sign-in's alive
        report (``satellite_link``); ``compute`` the machine's kind and
        ``name`` the row's name, known before the sign-in's steps read them."""
        old = self._connections.get(machine_id)
        conn = SatelliteConnection(
            machine_id=machine_id,
            ws=ws,
            capabilities=capabilities,
            satellite_version=satellite_version,
        )
        conn.compute = compute
        conn.compute_address = compute_address if compute else ""
        conn.own_copy = bool(conn.compute_address)
        conn.own_tools = bool(compute and not conn.own_copy
                              and (capabilities or {}).get("compute_tools") == _TOOLS_OWN)
        _note_memory(conn, (capabilities or {}).get("memory"), (capabilities or {}).get("memory_guard"))
        _signed_in_with = (capabilities or {}).get("active_sessions")
        if isinstance(_signed_in_with, int):
            conn.reported_sessions, conn.sessions_reported = _signed_in_with, True
        tunnel_state.seed(conn, capabilities)
        conn.on_slow_reader = self._on_slow_reader
        link = self._links.get(machine_id)
        # The machine row's cache carries over until its refresh lands.
        conn.max_sessions, conn.name = link.max_sessions, name or link.name
        if old:
            logger.warning(
                "Satellite %s: duplicate connection, closing old (silent for %.0f s)",
                placement.log_id(machine_id), time.monotonic() - last_contact(old),
            )
            # Duplicate-connection replacement: carry the old connection's
            # in-flight session queues straight onto the new one (the
            # grace area below only covers a clean drop→reconnect, where
            # deregister ran first). The SAME queue objects move over, so
            # producers' `info.event_queue` references stay valid and the
            # satellite's buffered events replay into them.
            conn.session_queues.update(old.session_queues)
            conn.session_execution_paths.update(old.session_execution_paths)
            # What waits on the old socket can never be answered.
            self._reject_connection_waiters(old, f"Satellite {placement.log_id(machine_id)} replaced by a new connection")
            self._links.end_connection(machine_id, old)
            # The machine closed its relayed tools when that link went, and
            # the old handler's deregister is a no-op once the entry is replaced.
            from core.remote import tool_relay
            tool_relay.machine_dropped(machine_id)
        self._connections[machine_id] = conn
        self._reset_file_changed_lane(machine_id)
        self.start_held_lines(machine_id)

        # Re-adopt any sessions held from a recent drop
        # of THIS machine so their in-flight turns continue on the new
        # connection. The SAME queue objects are restored → the producers'
        # `info.event_queue` references stay valid; the satellite replays
        # its buffered events into them automatically (no resume request
        # needed). Whichever of this, deregister and _expire_grace pops
        # `_grace_sessions` first wins: none of them awaits in between.
        held = self._grace_sessions.pop(machine_id, None)
        self._last_drop_at.pop(machine_id, None)
        prior_timer = self._grace_timers.pop(machine_id, None)
        if prior_timer is not None and not prior_timer.done():
            prior_timer.cancel()
        if held:
            for _sid, (_q, _path) in held.items():
                conn.session_queues[_sid] = _q
                if _path:
                    conn.session_execution_paths[_sid] = _path
            logger.info(
                "Satellite %s: re-adopted %d in-flight session(s) on "
                "reconnect (grace)", placement.log_id(machine_id), len(held),
            )
        self._links.begin_report(machine_id, conn, report_id)

        # Start the writer task BEFORE updating DB status so any send
        # triggered during the rest of register() has the consumer running.
        # Auth has already succeeded by the time we get here (handler runs
        # auth then calls register), so the writer can safely begin sending.
        conn.writer_task = asyncio.create_task(
            self._writer_loop(conn),
            name=f"ws-writer-{placement.log_id(machine_id)}",
        )
        # The sign-in a renewal could not push while the machine was away:
        # started here, ahead of everything this sign-in sends, so it is the
        # first command the new connection carries. Every sign-in asks, no
        # window: a machine that missed nothing costs one lookup.
        from services.engines import token_fanout
        spawn(token_fanout.machine_signed_in(machine_id), name="sign_in_catch_up", owner="satellite_connection")

        if old:
            # The old socket's tunnel streams can never be answered: the
            # satellite failed its side of them when that socket went, and
            # a reply would reach the new connection under a stream id it
            # no longer knows. Until the sweep they would count against
            # the machine's stream cap (deregister, which cancels them on
            # a drop, is a no-op once the entry is replaced).
            try:
                from core.remote.satellite_http_tunnel import get_dispatcher
                await get_dispatcher().cancel_machine_streams(self, machine_id)
            except Exception:
                logger.exception("tunnel stream cleanup failed for %s", placement.log_id(machine_id))
            # Stop the old writer now — its handler's deregister is a
            # no-op once we replace the dict entry (identity guard), so
            # nobody else will cancel it.
            old_writer = getattr(old, "writer_task", None)
            if old_writer is not None and not old_writer.done():
                old_writer.cancel()
            with contextlib.suppress(Exception):
                await old.ws.close(code=4002, reason="Replaced by new connection")

        # Mark online + refresh last_seen — OFF the loop, through the
        # per-machine persister (newest-wins, so a stale ``disconnected`` from
        # the connection this one replaces can never land after it). Admin
        # offline/online notifications are deliberately NOT fired from this
        # connect edge — they're owned by the sustained-state evaluator
        # (_evaluate_admin_machine_alerts), which clears any outstanding
        # `offline_alerted` flag and fires the "back online" notice on its
        # next tick. Keeping it out of the hot connect path is what makes a
        # reconnect after a proxy restart / auto-update / blip silent.
        from storage import remote_store
        conn.last_seen_iso = _iso_now()
        self._persist_status(conn, remote_store.STATUS_ONLINE, conn.last_seen_iso)
        # Bounded persist — same rule as the cli_status handler: an
        # authenticated-but-compromised satellite must not park arbitrary
        # payloads in the capabilities column (it is parsed and returned to
        # admin/owner dashboards verbatim). The caps lane is shared with the
        # cli_status re-report so the two writes can never land out of order.
        try:
            caps_size = len(json.dumps(capabilities))
        except (TypeError, ValueError):
            caps_size = -1
        if 0 <= caps_size <= 64 * 1024:
            self._request_persist(machine_id, "caps", dict(capabilities))
        else:
            logger.warning(
                "Satellite %s sent an oversized/unserializable capabilities "
                "payload (%s bytes) — not persisted", placement.log_id(machine_id), caps_size,
            )
        logger.info(
            "Satellite %s connected (capabilities: %s)",
            placement.log_id(machine_id), list(capabilities.get("installed_clis", [])),
        )
        self._kick_reconnect_steps(machine_id)
        # What the machine was owed while it was away goes out now.
        from core.remote import machine_push_lane
        machine_push_lane.machine_connected(machine_id)
        self._link_changed(machine_id)
        return conn

    def _on_slow_reader(self, conn: SatelliteConnection, why: str) -> None:
        spawn(self._drop_slow_reader(conn, why), name="drop_slow_reader",
              owner="satellite_connection")

    async def _drop_slow_reader(self, conn: SatelliteConnection, why: str) -> None:
        """A machine that reads slower than its queue fills is dropped, never
        buffered without end: its acks fail now, its sessions are held in
        the reconnect window, and what it missed is made up at its return
        (the names its lane owes it, its credentials, its sessions' frames).
        A socket that still drains sees close code 4008. One that is paused
        is cut without it."""
        logger.warning(
            "Satellite %s reads too slowly (%s); dropping it, it reconnects and catches up",
            conn.machine_id[:8], why,
        )
        await self.deregister(conn.machine_id, expected=conn)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(conn.ws.close(code=4008, reason="slow reader"), 5.0)

    def _kick_reconnect_steps(self, machine_id: str) -> None:
        """The steps a sign-in asks for beside its alive report. Each runs at
        most once per window per machine (``satellite_link.STEP_WINDOW_S``),
        at the window's end when a machine signs in again inside it, and
        only while the machine is connected then: one that reconnects every
        second costs its sign-in and its report, nothing more."""

        def _still(step):
            def start() -> None:
                if machine_id in self._connections:
                    step()
            return start

        def _machine_row() -> None:
            # The admin max_sessions override + name, off the loop (the
            # values of the last connection stand until it lands).
            spawn(self._refresh_machine_cache(machine_id, self._connections[machine_id]),
                  name="refresh_machine_cache", owner="satellite_connection")

        # What the machine lacks of its tools is installed ahead of its next
        # chat (``tools_ahead.after_signin``: spread over a minute, a few
        # machines at a time; its sync verifies the machine's tools first).
        async def _kick_ahead():
            from services.remote import tools_ahead
            await tools_ahead.after_signin(machine_id)

        # Catch up every agent on this machine to the platform NOW — applying
        # deletes (tombstones) + drift that landed while it was offline, without
        # waiting for a session warmup. Best-effort, background, per-(machine,
        # agent)-locked against any concurrent warmup. The walk coalesces and
        # keeps a window of its own (``remote_workspace_sync``).
        async def _kick_workspace_sync():
            try:
                from core.session.session_manager import _get_remote_layer
                layer = _get_remote_layer()
                if layer is not None:
                    await layer.sync_all_agents_on_reconnect(machine_id)
            except Exception:  # noqa: BLE001  # reconnect catch-up spans db, disk and satellite: a reconnect retries
                logger.debug(
                    "reconnect workspace sync kick failed for %s", placement.log_id(machine_id),
                )

        self._links.kick(machine_id, "machine_row", _still(_machine_row))
        if self.is_compute(machine_id):
            # A compute machine beside the platform reads the platform's
            # folders itself: no workspace catch-up goes to it (it would
            # refuse it). It gets its tools ahead when it keeps a tools folder
            # of its own, as one on another host does, whose copy is synced
            # only with a session's identity, at that session's start.
            if self.takes_tools(machine_id):
                self._links.kick(machine_id, "verify", _still(
                    lambda: spawn(_kick_ahead(), name="kick_ahead", owner="satellite_connection")))
            return
        self._links.kick(machine_id, "verify", _still(
            lambda: spawn(_kick_ahead(), name="kick_ahead", owner="satellite_connection")))
        spawn(_kick_workspace_sync(), name="kick_workspace_sync", owner="satellite_connection")

    def broadcast_fire_and_forget(self, msg: dict) -> int:
        """Queue ``msg`` to every connected machine (a copy each). Returns how
        many were queued. Never waits and never raises."""
        sent = 0
        for conn in list(self._connections.values()):
            try:
                conn.enqueue_send_nowait(dict(msg))
                sent += 1
            except Exception:  # noqa: BLE001  # one machine's queue must not stop the broadcast
                logger.debug("broadcast to %s failed", conn.machine_id[:8], exc_info=True)
        return sent

    def _reject_connection_waiters(self, conn: SatelliteConnection, why: str) -> None:
        """Fail every ack and pull waiting on ``conn`` and its bulk credit
        (a replaced connection). A pull that already holds every byte is
        left to its waiter, which commits it."""
        for cid, entry in list(self._pending_acks.items()):
            if entry[2] is not conn:
                continue
            self._pending_acks.pop(cid, None)
            self._manifest_pages.pop(cid, None)
            if not entry[1].done():
                entry[1].set_exception(RuntimeError(why))
        for rid, st in list(self._pending_pulls.items()):
            if st.conn is not conn or st.future.done():
                continue
            self._pending_pulls.pop(rid, None)
            self._cleanup_pull_stream(st)
            st.future.set_exception(RuntimeError(why))
        credit = getattr(conn, "bulk_credit", None)
        if credit is not None:
            credit.fail_all(why)

    async def deregister(
        self, machine_id: str, expected: "SatelliteConnection | None" = None,
    ) -> None:
        """Remove a satellite connection and clean up session queues.

        ``expected`` is the connection the CALLER owns (its ws handler /
        writer). When the registry already holds a DIFFERENT connection for
        this machine — a duplicate reconnect replaced us while our socket was
        still draining — the deregister is a stale no-op: popping the entry
        here would unregister the LIVE connection and mark the machine
        offline while the satellite still holds its (healthy) new socket.
        That exact race took the whole fleet "down" on the proxy after a
        simultaneous reconnect storm. ``None`` keeps the unconditional pop
        for callers that operate on "whatever is current" (tests, admin
        forced-drop paths).
        """
        # Everything up to the writer's cancel runs with no await (atomic on
        # the loop with register and the grace expiries).
        current = self._connections.get(machine_id)
        if expected is not None and current is not expected:
            logger.info(
                "Satellite %s: stale deregister ignored (connection "
                "already replaced)", placement.log_id(machine_id),
            )
            return
        conn = self._connections.pop(machine_id, None)
        if conn is not None:
            self._last_drop_at[machine_id] = time.monotonic()
            self._links.end_connection(machine_id, conn)
            held = self._held_lines.pop(machine_id, None)
            if held is not None:
                held.end()
            room = getattr(conn, "room", None)
            if room is not None:
                room.set()     # a producer waiting for room sees the connection gone
            from core.remote import machine_push_lane, tool_relay
            machine_push_lane.machine_dropped(machine_id)
            tool_relay.machine_dropped(machine_id)
            self._link_changed(machine_id)
        # Interactive remote PTYs: the satellite keeps the
        # PTY + its child process alive across a WS drop, so do NOT kill them
        # here. Hold them in a reconnect-grace window + a timer; the
        # satellite's post-auth `pty_alive` reconciles them on reconnect
        # (re-adopt / exit-if-died / close-orphan), and `_expire_pty_grace`
        # tears them down only if the window lapses. Tell any viewer it's
        # reconnecting so the terminal shows a banner + pauses input.
        from core.remote import remote_pty
        if remote_pty.has_machine_ptys(machine_id):
            for _sid in remote_pty.machine_pty_session_ids(machine_id):
                self._notify_pty_status(_sid, "reconnecting")
            prior_pty = self._pty_grace_timers.pop(machine_id, None)
            if prior_pty is not None and not prior_pty.done():
                prior_pty.cancel()
            self._pty_grace_timers[machine_id] = asyncio.create_task(
                self._expire_pty_grace(machine_id),
                name=f"pty-grace-{placement.log_id(machine_id)}",
            )
            logger.info(
                "Satellite %s: holding interactive PTY(s) in reconnect "
                "grace (%.0fs)", placement.log_id(machine_id), _GRACE_WINDOW_S,
            )
        if conn is not None and conn.session_queues:
            # Do NOT terminate in-flight sessions on a WS
            # drop. The satellite keeps the CLI/Codex process alive and
            # buffers its outbound events (`ws_client._send_queue` persists
            # across reconnects → it replays them on reconnect), so the turn
            # can resume losslessly. Hold the in-flight session queues in a
            # per-machine grace area + start a grace timer; register()
            # re-adopts them on reconnect, _expire_grace() terminates them
            # (with a ⚠ marker) if the window passes. Done INSIDE the lock
            # so a racing register() / _expire_grace() sees a consistent
            # grace state. The SAME queue objects are held → the producers'
            # `info.event_queue` references stay valid on re-adoption.
            self._grace_sessions[machine_id] = {
                sid: (q, conn.session_execution_paths.get(sid, ""))
                for sid, q in conn.session_queues.items()
            }
            prior = self._grace_timers.pop(machine_id, None)
            if prior is not None and not prior.done():
                prior.cancel()
            self._grace_timers[machine_id] = asyncio.create_task(
                self._expire_grace(machine_id),
                name=f"grace-{placement.log_id(machine_id)}",
            )
            logger.info(
                "Satellite %s: holding %d in-flight session(s) in "
                "reconnect grace (%.0fs)",
                placement.log_id(machine_id), len(conn.session_queues), _GRACE_WINDOW_S,
            )

        # The machine's file_changed lane restarts with its next connection;
        # its quiet-window stamps go with the connection unless sessions are
        # held in the reconnect grace (the satellite replays their frames).
        self._reset_file_changed_lane(machine_id)
        if machine_id not in self._grace_sessions:
            evict_file_changed_stamps(machine_id)

        if conn:
            # Cancel the writer task first so it stops trying to send.
            # If it's already done (returned due to send failure), this is
            # a harmless no-op. Use getattr so test mocks without a
            # writer_task attribute remain compatible.
            writer_task = getattr(conn, "writer_task", None)
            if writer_task is not None and not writer_task.done():
                writer_task.cancel()

            # Cancel every in-flight tunneled HTTP stream for this machine
            # so subprocess hooks return cleanly instead of hanging until
            # timeout. The satellite-side LocalTunnelServer also self-fails
            # its streams when the WS closes.
            try:
                from core.remote.satellite_http_tunnel import get_dispatcher
                await get_dispatcher().cancel_machine_streams(self, machine_id)
            except Exception:
                logger.exception(
                    "tunnel stream cleanup failed for %s", placement.log_id(machine_id),
                )

            # NOTE: in-flight session queues are NOT terminated here
            # anymore — they were moved to the reconnect-grace area above (still
            # inside the lock). The grace timer terminates them (with a ⚠
            # marker) only if no reconnect arrives within _GRACE_WINDOW_S.

            # Reject any pending acks for this machine so callers wake up
            # immediately rather than waiting out the 30s send_command
            # timeout. Critical for mid-install disconnects where sync_mcps
            # tarballs are in flight.
            to_reject = [
                cid for cid, entry in self._pending_acks.items()
                if entry[0] == machine_id
            ]
            for cid in to_reject:
                entry = self._pending_acks.pop(cid, None)
                self._manifest_pages.pop(cid, None)
                if entry:
                    future = entry[1]
                    if not future.done():
                        future.set_exception(
                            RuntimeError(f"Satellite {placement.log_id(machine_id)} disconnected")
                        )
            credit = getattr(conn, "bulk_credit", None)
            if credit is not None:
                credit.fail_all(f"Satellite {placement.log_id(machine_id)} disconnected")

            # Reject + clean up any streaming pulls for this machine so the
            # caller wakes immediately and no .partial is left on disk. A
            # pull that already holds every byte is left to its waiter.
            pulls_to_reject = [
                rid for rid, st in self._pending_pulls.items()
                if st.machine_id == machine_id and not st.future.done()
            ]
            for rid in pulls_to_reject:
                st = self._pending_pulls.pop(rid, None)
                if st is not None:
                    self._cleanup_pull_stream(st)
                    if not st.future.done():
                        st.future.set_exception(
                            RuntimeError(f"Satellite {placement.log_id(machine_id)} disconnected")
                        )

            # Drop the per-machine install lock only if it's not currently
            # held (held lock → a waiter will take it and discover the
            # disconnect on its next send_command).
            lock = self._install_locks.get(machine_id)
            if lock is not None and not lock.locked():
                self._install_locks.pop(machine_id, None)

            # Off-loop, newest-wins; carries the EXACT last contact so the
            # admin offline-alert downtime clock is unaffected by the
            # coalesced heartbeat writes.
            from storage import remote_store
            self._persist_status(
                conn, remote_store.STATUS_DISCONNECTED, getattr(conn, "last_seen_iso", "") or None,
            )
            logger.info("Satellite %s disconnected", placement.log_id(machine_id))
            # No admin notification on this disconnect edge: the
            # sustained-state evaluator (_evaluate_admin_machine_alerts)
            # fires "offline" only once the machine stays unreachable past
            # the grace window, so a transient drop followed by a fast
            # reconnect (restart / auto-update / network blip) never pages
            # admins.


    # --- Connection queries ---

    def tunnel_ok(self, machine_id: str) -> bool:
        """False while the machine reports its local link down; True for a
        machine not connected."""
        conn = self._connections.get(machine_id)
        return True if conn is None else bool(getattr(conn, "tunnel_ok", True))

    def tunnel_port(self, machine_id: str) -> int:
        """The port the machine's local link listens on now (0 when not
        connected or never reported): the next sessions' ``PROXY_URL``."""
        conn = self._connections.get(machine_id)
        if conn is None:
            return 0
        return tunnel_state.valid_port((conn.capabilities or {}).get("local_tunnel_port"))

    def exit_state(self, machine_id: str) -> tuple[int, bool]:
        """The machine's credential exit as its live connection reports it:
        the listener's port and whether it answers (``(0, False)`` when not
        connected or never reported). The one input of
        ``mcp_session_config.exit_machine_refusal`` for a connected machine."""
        conn = self._connections.get(machine_id)
        if conn is None:
            return 0, False
        caps = conn.capabilities or {}
        port = tunnel_state.valid_port(caps.get("exit_port"))
        return port, bool(port and caps.get("exit_ok") is True)

    def exit_report(self, machine_id: str) -> dict:
        """What the machine's live connection says beside its exit's port and
        answer: why it does not answer (``exit_why``, "" while it does) and
        whether it reaches the world through an outgoing proxy
        (``exit_upstream``: the machine's own report, or the platform's own
        egress names sent at its sign-in, ``conn.exit_upstream_sent``).
        Empty when not connected."""
        conn = self._connections.get(machine_id)
        if conn is None:
            return {}
        caps = conn.capabilities or {}
        return {"why": str(caps.get("exit_why") or ""),
                "upstream": caps.get("exit_upstream") is True or bool(getattr(conn, "exit_upstream_sent", False))}

    def _retire_moved_port_sessions(self, machine_id: str) -> None:
        """Close the idle sessions on a machine started on a local link port it
        no longer serves (the remote layer's eviction path: each resumes at its
        next message). Loop-side and cheap: a scan of the remote sessions."""
        from core.session.session_manager import _get_remote_layer
        layer = _get_remote_layer()
        if layer.moved_port_idle_sessions(machine_id):
            spawn(layer.retire_moved_port_sessions(machine_id),
                  name="retire_moved_port_sessions", owner="satellite_connection")

    def is_connected(self, machine_id: str) -> bool:
        """A socket to send on: the transport's question. Whether the
        machine is there for a person or for routing is
        ``remote_status.machine_state`` (``is_reachable``)."""
        return machine_id in self._connections

    def is_compute(self, machine_id: str) -> bool:
        """Whether the connected machine is a compute machine (set at its
        sign-in from its row); False for a machine that is not connected."""
        conn = self._connections.get(machine_id)
        return bool(conn is not None and conn.compute)

    def compute_machine_ids(self) -> frozenset[str]:
        """The connected compute machines. Over a copy of the table: the
        balancer reads it from a worker thread while the loop registers and
        drops connections."""
        return frozenset(mid for mid, c in list(self._connections.items()) if c.compute)

    def shares_folders(self, machine_id: str) -> bool:
        """Whether the connected machine is a compute machine that works on
        the platform's own folders (one beside the platform): nothing syncs
        to it and nothing it reports changed is applied."""
        conn = self._connections.get(machine_id)
        return bool(conn is not None and conn.compute and not conn.own_copy)

    def compute_address(self, machine_id: str) -> str:
        """The address a connected compute machine on another server signed
        in from, ``""`` for one beside the platform (``placement.compute_own_copy``
        reads it)."""
        conn = self._connections.get(machine_id)
        return conn.compute_address if conn is not None else ""

    def owns_copy(self, machine_id: str) -> bool:
        """Whether the connected machine is a compute machine on another host,
        with its own copy of the folders (``placement.compute_own_copy``)."""
        conn = self._connections.get(machine_id)
        return bool(conn is not None and conn.compute and conn.own_copy)

    def takes_tools(self, machine_id: str) -> bool:
        """Whether the platform installs tools on the connected machine: a
        paired computer, a compute machine on another host, and one beside
        the platform that claimed a tools folder of its own. One that shares
        the platform's folders and claims none is sent no tool."""
        conn = self._connections.get(machine_id)
        return bool(conn is not None and (not conn.compute or conn.own_copy or conn.own_tools))

    def builds_own_tools(self, machine_id: str) -> bool:
        """Whether the connected machine is the compute machine beside the
        platform that keeps the community tools in a folder of its own: the
        platform sends it those tools alone, never a file."""
        conn = self._connections.get(machine_id)
        return bool(conn is not None and conn.compute and conn.own_tools)

    def building_machine(self) -> str:
        """The connected compute machine an install's build runs on: the one
        beside the platform that builds its own tools, else one on another
        host (an install with no machine of its own), ``""`` when none is
        connected. Over a copy of the table (``compute_machine_ids``)."""
        conns = list(self._connections.items())
        for machine_id, conn in conns:
            if conn.compute and conn.own_tools:
                return machine_id
        return next((machine_id for machine_id, conn in conns if conn.compute and conn.own_copy), "")

    def machine_memory_report(self, machine_id: str, *, compute: bool = False) -> tuple[dict, float] | None:
        """A connected machine's last memory report and its age in seconds;
        None when it is not connected or sent none, and with ``compute`` when
        it is not a compute machine."""
        conn = self._connections.get(machine_id)
        if conn is None or not conn.memory or (compute and not conn.compute):
            return None
        return conn.memory, time.monotonic() - conn.memory_at

    def debit_reported_session(self, machine_id: str) -> None:
        """Optimistically decrement the cached heartbeat session count after
        the proxy closes a session on this machine — the capacity pre-check
        would otherwise read the STALE count until the next heartbeat and
        (a) refuse a spawn whose room eviction just reclaimed, or (b) keep
        evicting past the needed headroom. Approximate by design: the next
        heartbeat overwrites with the satellite's true count."""
        conn = self._connections.get(machine_id)
        if conn is not None and conn.reported_sessions > 0:
            conn.reported_sessions -= 1

    def machine_at_capacity(self, machine_id: str) -> bool:
        """Soft pre-check: True when the satellite reports it's at/over its
        effective session ceiling — the admin override (remote_machines.max_sessions)
        if set, else the satellite's own reported recommendation. Fail-OPEN on
        unknown (no connection / no cap / no count); the satellite's hard reject in
        session_manager is the real backstop, so a stale or zero count never blocks."""
        conn = self._connections.get(machine_id)
        if not conn:
            return False
        # Admin override from the per-connection cache (filled off-loop at
        # register, refreshed by note_max_sessions) — never a DB read here.
        # Without one, a machine that reports its memory starts by memory
        # (``core/concurrency_paired``): the count limits only one that does not.
        cap = getattr(conn, "max_sessions", None)
        if cap is None and conn.memory:
            return False
        if cap is None:
            rec = (conn.capabilities or {}).get("recommended_max_sessions")
            cap = int(rec) if rec else None
        if not cap:
            return False
        return conn.reported_sessions >= cap

    def concurrency_stats(self) -> list[dict]:
        """Per-satellite live session counts + load for the admin dashboard.
        Sourced from the heartbeat (authoritative — includes interactive +
        native-CLI/otodock sessions), NOT proxy-side session_queues (which are
        blind to those). Consumed by core.concurrency.get_stats()['satellites']."""
        out: list[dict] = []
        for mid, conn in list(self._connections.items()):
            caps = conn.capabilities or {}
            rec = caps.get("recommended_max_sessions")
            # Cached at register (off-loop); no DB read on the loop.
            db_max = getattr(conn, "max_sessions", None)
            by_memory = bool(conn.memory) and not db_max
            out.append({
                "machine_id": mid,
                "name": getattr(conn, "name", "") or mid[:8],
                "online": True,
                "active_sessions": conn.reported_sessions if conn.sessions_reported else None,
                "max_sessions": int(db_max) if db_max else (int(rec) if rec and not by_memory else None),
                "cpu_pct": (conn.load or {}).get("cpu_pct", 0.0),
                "mem_pct": (conn.load or {}).get("mem_pct", 0.0),
                **machine_memory.stats_fields(conn),
            })
        return out

    def is_session_stream_attached(self, machine_id: str, session_id: str) -> bool:
        """Whether the session's event queue is reachable — attached to the
        current connection OR held in the reconnect-grace area (a WS
        drop that may still reconnect). Grace-held counts as attached so the
        dashboard reap treats it as 'reconnecting', not a severed zombie. False
        here means the stream is genuinely severed (grace expired or never
        held) — used to reap a wedged pump on resume."""
        conn = self._connections.get(machine_id)
        if conn and session_id in conn.session_queues:
            return True
        return self.is_session_in_grace(machine_id, session_id)

    def get_connected_machines(self) -> list[str]:
        return list(self._connections.keys())

    def satellite_supports_pty(self, machine_id: str) -> bool:
        """True if the connected satellite advertises the interactive-PTY
        capability — it handles ``pty_open``/``pty_input``/``pty_resize``/
        ``pty_close`` and streams ``pty_output``. When False (a Windows
        machine without pywinpty), remote interactive falls back to headless
        ``-p`` instead of hanging on a ``pty_open`` the satellite never
        answers."""
        conn = self._connections.get(machine_id)
        return bool(conn and conn.capabilities.get("interactive_pty"))

    def satellite_session_home_dirs(self, machine_id: str) -> frozenset[str]:
        """The engine folders where the connected satellite makes a session a
        home of its own when the start asks (``placement.home_dirs`` of its
        report): an older one runs every session of a scope from its shared
        folder."""
        conn = self._connections.get(machine_id)
        return placement.home_dirs(conn.capabilities) if conn else frozenset()

    def satellite_reports_listed(self, machine_id: str) -> bool:
        """True if the connected satellite's stand-ins report when the engine
        has a tool's list (``tools_listed``): only then may a terminal there
        hold its first message for them (an older one never reports)."""
        conn = self._connections.get(machine_id)
        return bool(conn and conn.capabilities.get("tools_listed"))

    def satellite_takes_start_context(self, machine_id: str) -> bool:
        """True if the connected satellite writes the terminal's start-hook
        entries the payload asks for (``start_context_hook``): an older one
        writes none, so its terminal gets the start's riding part in its
        text (``core/session/start_context.py``)."""
        conn = self._connections.get(machine_id)
        return bool(conn and conn.capabilities.get("start_context_hook"))

    def satellite_streams_between_turns(self, machine_id: str) -> bool:
        """True if the connected satellite sends a Claude session's output
        between turns for the life of its process when ``stop_turn`` asks
        (``stream_between_turns``), and takes a steer into a turn the engine
        started by itself: ``between_turns_stream`` in its capabilities. An
        older one forwards only on ``drain_bg``, for ``drain_bg_s``."""
        conn = self._connections.get(machine_id)
        return bool(conn and conn.capabilities.get("between_turns_stream"))

    def satellite_os(self, machine_id: str) -> str:
        """The connected satellite's OS as it reports in capabilities
        (``platform.system().lower()`` → ``"windows"`` / ``"linux"`` / ``"darwin"``),
        or ``""`` if unknown/offline. Used to scope OS-specific interactive-PTY
        workarounds (e.g. the Windows-ConPTY cold-submit backstop)."""
        conn = self._connections.get(machine_id)
        return str(conn.capabilities.get("os", "")) if conn else ""

    def satellite_engines(self, machine_id: str) -> frozenset[str]:
        """The engine ids (``execution_path`` values) the connected satellite's
        CODE can dispatch — its advertised ``engines`` capability, empty when
        the field is missing or malformed. Binary presence is a different fact
        (``installed_clis`` / ``cli_status``): the pin reconcile may install a
        pinned binary after auth, so a spawn is never gated on it. Empty for a
        machine that is not connected."""
        conn = self._connections.get(machine_id)
        raw = conn.capabilities.get("engines") if conn else None
        if not isinstance(raw, list):
            return frozenset()
        return frozenset(
            e for e in raw[:_ENGINES_MAX]
            if isinstance(e, str) and 0 < len(e) <= _ENGINE_ID_MAX_LEN
        )

    def expect_update_ack(self, machine_id: str) -> asyncio.Future:
        """The future an online update push waits on: resolved with the
        target version when the satellite says it read the push."""
        fut = asyncio.get_running_loop().create_future()
        old = self._update_acks.get(machine_id)
        if old is not None and not old.done():
            old.cancel()
        self._update_acks[machine_id] = fut
        return fut

    def satellite_version(self, machine_id: str) -> str:
        """The connected satellite's reported SATELLITE_VERSION, or ``""`` when
        it is offline or never reported one (user-facing messages)."""
        conn = self._connections.get(machine_id)
        return str(getattr(conn, "satellite_version", "") or "") if conn else ""

    def satellite_name(self, machine_id: str) -> str:
        """The connected satellite's display name, falling back to the short
        machine id (user-facing messages)."""
        conn = self._connections.get(machine_id)
        name = str(getattr(conn, "name", "") or "") if conn else ""
        return name or placement.log_id(machine_id)

    # A satellite answers ``request_manifest`` in pages when asked. The
    # joined reply is bounded to this many entries and this much serialized
    # text (a page is about 2 MiB, a frame at most the socket's cap).
    _MANIFEST_MAX_ENTRIES = 500_000
    _MANIFEST_MAX_BYTES = 256 * 1024 * 1024

    def satellite_restores_holds(self, machine_id: str) -> bool:
        """True for the satellite that decides a ``windows-hash-hold``: a
        Windows one, which hashes every file and lists what it cannot read."""
        from core import host_os
        row = host_os.of(self.satellite_os(machine_id))
        return row is not None and row.name == host_os.WINDOWS

    def _on_manifest_frame(self, machine_id: str, msg: dict) -> None:
        """One ``file_manifest`` frame, only from the machine the command
        went to. A frame with no ``page`` and no ``more`` is a whole reply
        and resolves the wait at once. A paged reply's frames carry
        ``page`` 0, 1, 2 and so on, ``more`` on all but the last: they are
        joined in that order and the last resolves the wait. A page out of
        order (one lost on the way, or repeated) and a joined reply past the
        entry or size ceiling fail the wait, so no caller mistakes a partial
        manifest for a whole one."""
        command_id = msg.get("command_id", "")
        entry = self._pending_acks.get(command_id)
        if not entry:
            self._manifest_pages.pop(command_id, None)
            return
        mid, future = entry[0], entry[1]
        if mid != machine_id or future.done():
            return
        files = msg.get("files")
        files = files if isinstance(files, list) else []
        page = msg.get("page")
        more = bool(msg.get("more"))
        if page is None and not more and command_id not in self._manifest_pages:
            future.set_result(msg)
            return

        def _fail(why: str) -> None:
            self._manifest_pages.pop(command_id, None)
            future.set_exception(RuntimeError(f"manifest from {placement.log_id(machine_id)} {why}"))

        join = self._manifest_pages.setdefault(command_id, _ManifestJoin())
        if not isinstance(page, int) or isinstance(page, bool) or page != join.next_page:
            _fail(f"sent page {page!r} where page {join.next_page} was due")
            return
        join.next_page += 1
        join.entries.extend(files)
        join.size += len(json.dumps(files, separators=(",", ":")))
        if len(join.entries) > self._MANIFEST_MAX_ENTRIES:
            _fail(f"exceeds {self._MANIFEST_MAX_ENTRIES} entries")
            return
        if join.size > self._MANIFEST_MAX_BYTES:
            _fail(f"exceeds {self._MANIFEST_MAX_BYTES} bytes")
            return
        if more:
            return
        self._manifest_pages.pop(command_id, None)
        future.set_result({**msg, "files": join.entries})

    # --- Command sending ---

    async def send_command(
        self,
        machine_id: str,
        msg: dict,
        *,
        timeout: float = 30.0,
        command_id: str | None = None,
    ) -> dict:
        """Send a command to a satellite and wait for its ack.

        Returns the ack message dict. Raises RuntimeError on timeout or error.

        ``command_id`` lets the caller pre-generate the id so it can correlate
        late-arriving session events (specifically ``turn_ended``) back to the
        originating command. When omitted a fresh UUID is generated.
        """
        conn = self._connections.get(machine_id)
        if not conn:
            raise RuntimeError(f"Satellite {placement.log_id(machine_id)} not connected")

        if command_id is None:
            command_id = str(uuid.uuid4())
        msg["command_id"] = command_id

        # Create ack future keyed by (machine_id, future) so deregister can
        # cancel them en masse on disconnect.
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_acks[command_id] = (machine_id, future, conn)
        # A paged manifest's pages end with the wait, however it ends.
        future.add_done_callback(lambda _f, _cid=command_id: self._manifest_pages.pop(_cid, None))  # type: ignore[misc]  # mypy infers no type for a lambda with a default argument

        try:
            # Enqueue rather than ws.send_text(). The writer task does the
            # actual send. If the WS is dead, the writer triggers deregister
            # which rejects this future with RuntimeError immediately —
            # caller wakes up without waiting for the timeout.
            await conn.enqueue_send(msg)
            ack = await asyncio.wait_for(future, timeout=timeout)
            if ack.get("status") == "error":
                if ack.get("reason") == "machine_memory":
                    # The machine's own last check (0.5.177): its sentence alone.
                    raise RuntimeError(str(ack.get("error") or "This computer is low on memory."))
                raise RuntimeError(
                    f"Satellite command error: {ack.get('error', 'unknown')}"
                )
            return ack
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Satellite {placement.log_id(machine_id)} command timeout ({timeout}s)"
            ) from exc
        finally:
            self._pending_acks.pop(command_id, None)
            self._manifest_pages.pop(command_id, None)

    async def begin_command(self, machine_id: str, msg: dict) -> tuple[str, asyncio.Future]:
        """Send a command and hand its ack back as a future the caller waits
        on at its own pace: a probe that stays outstanding while its caller
        waits in rounds (the resume probe) is sent once, not once per round.
        The future resolves with the ack dict (its ``status`` unjudged) and
        fails with ``RuntimeError`` when the machine drops. The caller ends
        the wait with ``forget_command`` when it gives up. Raises
        ``RuntimeError`` when the machine is not connected."""
        conn = self._connections.get(machine_id)
        if not conn:
            raise RuntimeError(f"Satellite {placement.log_id(machine_id)} not connected")
        command_id = str(uuid.uuid4())
        msg["command_id"] = command_id
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_acks[command_id] = (machine_id, future, conn)
        future.add_done_callback(lambda _f, _cid=command_id: self._pending_acks.pop(_cid, None))  # type: ignore[misc]  # mypy infers no type for a lambda with a default argument
        try:
            await conn.enqueue_send(msg)
        except BaseException:
            # A frame that never left waits for no ack.
            self._pending_acks.pop(command_id, None)
            if not future.done():
                future.cancel()
            raise
        return command_id, future

    def forget_command(self, command_id: str) -> None:
        """Stop waiting for a ``begin_command`` ack (a late one is dropped)."""
        entry = self._pending_acks.pop(command_id, None)
        if entry is not None and not entry[1].done():
            entry[1].cancel()

    def machine_dropped_recently(self, machine_id: str) -> bool:
        """True while a machine that is not connected may still be coming
        back: it dropped within the reconnect grace, or it holds sessions or
        terminals in it. False for a machine away longer, or never seen."""
        if machine_id in self._grace_timers or machine_id in self._pty_grace_timers:
            return True
        dropped = self._last_drop_at.get(machine_id)
        return dropped is not None and time.monotonic() - dropped < _GRACE_WINDOW_S

    async def send_fire_and_forget(self, machine_id: str, msg: dict) -> None:
        """Send a message without waiting for ack.

        Enqueues on the writer queue. The writer task handles dead-WS
        detection (triggers deregister on send failure), so this method
        doesn't need its own error handling.
        """
        conn = self._connections.get(machine_id)
        if conn:
            await conn.enqueue_send(msg)

    async def send_local_detach(self, machine_id: str, session_id: str, reason: str = "") -> None:
        """dual-control: ask the satellite to detach the local `otodock`
        terminal from ``session_id`` (the dashboard is taking over) WITHOUT killing
        the PTY. The satellite evicts the local conn, tells the client to exit with
        ``reason``, and confirms back via ``local_session_detached`` (which flips
        ``otodock_attached`` off so the dashboard takes control)."""
        await self.send_fire_and_forget(machine_id, {
            "type": "pty_local_detach",
            "session_id": session_id,
            "reason": reason,
        })

    async def _handle_local_session_open(self, machine_id: str, msg: dict) -> None:
        """otodock-CLI: open a satellite-initiated interactive session and
        reply with the session/chat ids (or a user-facing error reason). Identity
        is re-derived from the machine owner inside ``open_local_session`` — the
        frame's fields (agent/cwd/model) are treated as untrusted input."""
        from core.session import otodock_session
        request_id = msg.get("request_id", "")
        try:
            result = await otodock_session.open_local_session(machine_id, msg)
            await self.send_fire_and_forget(machine_id, {
                "type": "local_session_opened",
                "request_id": request_id,
                "session_id": result["session_id"],
                "chat_id": result["chat_id"],
                # dual-control: True = ATTACH to an already-live PTY (the
                # satellite must already hold it in pty_sessions, else error the
                # client instead of waiting for a pty_open that won't come). False/
                # absent = a fresh spawn whose pty_open is on its way.
                "attach": bool(result.get("attach")),
            })
        except (otodock_session.OtodockSessionError, tunnel_state.TunnelDown) as e:
            await self.send_fire_and_forget(machine_id, {
                "type": "local_session_error",
                "request_id": request_id,
                "reason": str(e),
            })
        except Exception:
            logger.exception(
                "otodock local_session_open failed (machine=%s)", placement.log_id(machine_id)
            )
            await self.send_fire_and_forget(machine_id, {
                "type": "local_session_error",
                "request_id": request_id,
                "reason": "internal error opening the session",
            })

    async def _handle_local_session_list(self, machine_id: str, msg: dict) -> None:
        """otodock-CLI --resume: reply with the owner's resumable chats (or a
        user-facing error)."""
        from core.session import otodock_session
        request_id = msg.get("request_id", "")
        try:
            result = await otodock_session.list_local_sessions(machine_id, msg)
            await self.send_fire_and_forget(machine_id, {
                "type": "local_session_listed",
                "request_id": request_id,
                "chats": result["chats"],
            })
        except otodock_session.OtodockSessionError as e:
            await self.send_fire_and_forget(machine_id, {
                "type": "local_session_error",
                "request_id": request_id,
                "reason": str(e),
            })
        except Exception:
            logger.exception(
                "otodock local_session_list failed (machine=%s)", placement.log_id(machine_id)
            )
            await self.send_fire_and_forget(machine_id, {
                "type": "local_session_error",
                "request_id": request_id,
                "reason": "internal error listing sessions",
            })


    # --- Session event routing ---

    def create_session_queue(
        self, machine_id: str, session_id: str, execution_path: str,
        *, maxsize: int | None = None,
    ) -> asyncio.Queue:
        """Create an event queue for a remote session.

        ``maxsize`` is raised by the run-recovery adoption path (the replay
        of a retained turn buffer arrives as one burst larger than the
        default cap; session_event dispatch silently drops on full). The
        default is the ENGINE's declared ``runtime.event_queue_depth``: an
        engine whose between-turns events are routed out of band (Codex's
        bg terminals) declares a deeper one, because there a QueueFull drop
        is correctness-bearing (a lost item/completed leaves a badge only the
        0.5.105 drain backstop can clear)."""
        conn = self._connections.get(machine_id)
        if not conn:
            raise RuntimeError(f"Satellite {placement.log_id(machine_id)} not connected")
        if maxsize is None:
            from core.session.session_manager import capabilities_for_path
            maxsize = capabilities_for_path(execution_path).runtime.event_queue_depth
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        conn.session_queues[session_id] = queue
        conn.session_execution_paths[session_id] = execution_path
        return queue

    def remove_session_queue(self, machine_id: str, session_id: str) -> None:
        """Remove a session's event queue."""
        conn = self._connections.get(machine_id)
        if conn:
            conn.session_queues.pop(session_id, None)
            conn.session_execution_paths.pop(session_id, None)
        # Also drop any grace-held copy (close during grace) so the
        # grace timer can't terminate an already-closed session and a reconnect
        # won't re-adopt it.
        self.drop_grace_session(machine_id, session_id)

    # --- Incoming message handling ---

    # One method per frame type the satellite sends; a type not named here
    # is ignored.
    _FRAME_HANDLERS: dict[str, str] = {
        "ack": "_frame_ack",
        "update_received": "_frame_update_received",
        "pause": "_frame_pause",
        "cli_status": "_frame_cli_status",
        "heartbeat": "_frame_heartbeat",
        "session_event": "_frame_session_event",
        "pty_output": "_frame_pty_output",
        "pty_exit": "_frame_pty_exit",
        "pty_alive": "_frame_pty_alive",
        "sessions_alive": "_frame_sessions_alive",
        "transcript_lines": "_frame_transcript_lines",
        "pty_inject_result": "_frame_pty_inject_result",
        "session_started": "_frame_session_started",
        "session_ended": "_frame_session_ended",
        "session_aborted": "_frame_session_aborted",
        "turn_ended": "_frame_turn_ended",
        "memory_stop": "_frame_memory_stop",
        "codex_thread_id": "_frame_codex_thread_id",
        "file_changed": "_frame_file_changed",
        "file_manifest": "_frame_file_manifest",
        "file_content": "_frame_file_content",
        "mcp_install_progress": "_frame_mcp_install_progress",
        "step_output": "_frame_step_output",
        "tool_out": "_frame_tool_out",
        "tool_exit": "_frame_tool_exit",
        "app_server_output": "_frame_app_server",
        "app_server_exit": "_frame_app_server",
        "http_request": "_frame_http_request",
        "http_request_chunk": "_frame_http_request_chunk",
        "http_abort": "_frame_http_abort",
        "local_session_open": "_frame_local_session_open",
        "local_session_list": "_frame_local_session_list",
        "local_session_detached": "_frame_local_session_detached",
    }

    async def handle_message(self, machine_id: str, msg: dict) -> None:
        """Route an incoming message from a satellite."""
        msg_type = msg.get("type", "")
        # A type that is not a string is ignored before the lookup (an
        # unhashable one would raise there).
        if not isinstance(msg_type, str):
            return
        handler = self._FRAME_HANDLERS.get(msg_type)
        if handler is not None:
            await getattr(self, handler)(machine_id, msg)

    async def _frame_ack(self, machine_id: str, msg: dict) -> None:
        command_id = msg.get("command_id", "")
        entry = self._pending_acks.get(command_id)
        # Only the machine a command went to answers it.
        if entry and entry[0] == machine_id:
            future = entry[1]
            if not future.done():
                future.set_result(msg)

    async def _frame_update_received(self, machine_id: str, msg: dict) -> None:
        # A satellite read an online "Update now" push (0.5.140; the
        # auth-time push reads its acknowledgement on its own socket).
        fut = self._update_acks.pop(machine_id, None)
        if fut is not None and not fut.done():
            fut.set_result(str(msg.get("target_version") or ""))

    async def _frame_pause(self, machine_id: str, msg: dict) -> None:
        # Deliberate pause (tray Pause). Persist so the sustained-outage
        # evaluator skips this machine (no false admin alert). The
        # satellite closes the WS right after this; the next successful
        # auth (tray Resume / reboot) clears the flag — through the same
        # lane, so the two writes keep their order.
        self._request_persist(machine_id, "paused", True)

    async def _frame_cli_status(self, machine_id: str, msg: dict) -> None:
        # Per-CLI {version, path} from the satellite's pin reconcile
        # (0.5.107+), sent after every reconcile pass. Data only — pin
        # mismatch is judged against config.PINNED_* by consumers, never
        # asserted by the satellite. Merged into capabilities for the
        # machines API; auth REPLACES capabilities wholesale on reconnect
        # and the reconcile re-reports seconds later, so this
        # self-refreshes every connect. Input is whitelisted + bounded:
        # an authenticated-but-compromised satellite must not park
        # arbitrary payloads in the capabilities column.
        conn = self._connections.get(machine_id)
        if conn:
            clis_raw = msg.get("clis")
            clis: dict = {}
            if isinstance(clis_raw, dict):
                # The whitelist is the registered engines' binaries
                # (``runtime.binary``) — the keys the satellite's pin
                # reconcile reports under.
                from core.session.session_manager import get_all_layers
                for name in sorted({
                    lay.capabilities.runtime.binary
                    for lay in get_all_layers().values()
                    if lay.capabilities.runtime.binary
                }):
                    entry = clis_raw.get(name)
                    if not isinstance(entry, dict):
                        continue
                    ver = entry.get("version")
                    path = entry.get("path")
                    clis[name] = {
                        "version": ver[:300] if isinstance(ver, str) else None,
                        "path": path[:300] if isinstance(path, str) else None,
                    }
            conn.capabilities["cli_status"] = clis
            # Persist a SNAPSHOT through the caps lane: json.dumps on
            # the worker thread must never iterate a dict the event loop
            # can still mutate, and the lane's newest-wins rule means a
            # register() snapshot and this re-report can never land out
            # of order (the old to_thread pair could).
            self._request_persist(machine_id, "caps", dict(conn.capabilities))

    async def _frame_heartbeat(self, machine_id: str, msg: dict) -> None:
        conn = self._connections.get(machine_id)
        if conn:
            conn.last_heartbeat = time.monotonic()
            conn.last_seen_iso = _iso_now()
            # Measure the signed clock offset (proxy_utc − satellite_utc) so the
            # file-sync merge can adjust the satellite's epoch mtimes into the
            # proxy clock before ordering a divergence. Best-effort; epoch mtime
            # is timezone-immune, so this only corrects genuine wall-clock skew.
            _ts = msg.get("timestamp")
            if _ts:
                try:
                    from datetime import datetime, timezone
                    _sat = datetime.fromisoformat(_ts)
                    if _sat.tzinfo is None:
                        _sat = _sat.replace(tzinfo=timezone.utc)
                    conn.clock_offset = (
                        datetime.now(timezone.utc) - _sat
                    ).total_seconds()
                except (ValueError, TypeError):
                    pass
            # Record the latest per-agent STAT fingerprints. Seed the
            # synced baseline on FIRST sight (setdefault) WITHOUT a merge —
            # initial catch-up is reconnect-sync / session-start's job; the
            # periodic sweep only fires on SUBSEQUENT changes. After a proxy
            # restart the baseline re-seeds here, so there's no merge burst.
            _fps = msg.get("agent_fingerprints")
            if isinstance(_fps, dict):
                for _slug, _fp in _fps.items():
                    if isinstance(_slug, str) and isinstance(_fp, str):
                        conn.agent_fingerprints[_slug] = _fp
                        conn.synced_fingerprints.setdefault(_slug, _fp)
            # Store per-satellite load + live session count (the proxy
            # otherwise discards these). Authoritative for the admin display +
            # the soft capacity pre-check in RemoteExecutionLayer.start_session.
            _load = msg.get("load")
            if isinstance(_load, dict):
                conn.load = _load
            _note_memory(conn, msg.get("memory"), msg.get("memory_guard"))
            _active = msg.get("active_sessions")
            if isinstance(_active, int):
                conn.reported_sessions, conn.sessions_reported = _active, True
                if _active == 0:
                    self._links.idle(machine_id)
            # The local link (0.5.168): a changed port, a link down or back.
            # A snapshot through the caps lane, as for cli_status.
            if tunnel_state.apply(conn, msg):
                self._request_persist(machine_id, "caps", dict(conn.capabilities))
            self._retire_moved_port_sessions(machine_id)
            # ``online`` + ``last_seen`` → the persister, coalesced to one
            # write per machine per HEARTBEAT_PERSIST_INTERVAL_S (or at
            # once on a transition). NEVER a synchronous commit here: this
            # branch runs every 20 s per satellite on the event loop.
            self._maybe_persist_heartbeat(conn)
        # Respond with pong
        await self.send_fire_and_forget(machine_id, {"type": "pong"})

    async def _frame_session_event(self, machine_id: str, msg: dict) -> None:
        session_id = msg.get("session_id", "")
        conn = self._connections.get(machine_id)
        if conn:
            queue = conn.session_queues.get(session_id)
            if queue:
                try:
                    queue.put_nowait(msg.get("event", {}))
                except asyncio.QueueFull:
                    logger.warning(
                        "Satellite %s session %s queue full",
                        placement.log_id(machine_id), session_id[:8],
                    )
                    return
                if self._line_listener is not None:
                    self._line_listener(session_id)
            else:
                # Read by the satellite while the platform restarted: kept
                # for the adoption that takes the session back.
                self.hold_session_line(machine_id, session_id, msg.get("event", {}))

    async def _frame_pty_output(self, machine_id: str, msg: dict) -> None:
        # Interactive remote PTY: rendered bytes from
        # the satellite-hosted TUI → the RemotePtyProcess, which fans them out
        # to the dashboard viewer + scrollback. Routed by (machine_id,
        # session_id) — NOT the lossy session_event queue (a dropped byte
        # would corrupt the xterm). base64 keeps raw control bytes intact.
        import base64 as _b64

        from core.remote import remote_pty
        try:
            _data = _b64.b64decode(msg.get("data_b64", "") or "")
        except (ValueError, TypeError):
            _data = b""
        if _data:
            remote_pty.feed_output(machine_id, msg.get("session_id", ""), _data)

    async def _frame_pty_exit(self, machine_id: str, msg: dict) -> None:
        # The satellite-hosted interactive PTY process ended → fire the
        # InteractiveSession's on_exit (dashboard reverts to the rich view +
        # primes a resume).
        from core.remote import remote_pty
        remote_pty.feed_exit(
            machine_id, msg.get("session_id", ""), msg.get("code"),
            str(msg.get("open_token") or ""), reason=str(msg.get("reason") or ""),
        )

    async def _frame_pty_alive(self, machine_id: str, msg: dict) -> None:
        # Right after (re)auth the satellite reports the session ids
        # of its still-live interactive PTYs (empty on a fresh connect).
        # Reconcile against our grace-held handles — re-adopt the survivors,
        # exit the ones whose child died, and close the orphans nobody
        # claimed (kills the reverse-orphan, also covers a proxy restart).
        # Then the terminals' half of this sign-in's alive report.
        conn = self._connections.get(machine_id)
        if conn is not None and self._links.older_report(machine_id, conn, msg):
            return
        tokens = msg.get("open_tokens")
        await self._reconcile_ptys(
            machine_id, msg.get("session_ids", []) or [],
            tokens if isinstance(tokens, dict) else {},
        )
        if conn is not None:
            self._links.note_terminals(machine_id, conn, msg)

    async def _frame_sessions_alive(self, machine_id: str, msg: dict) -> None:
        # Headless twin of pty_alive: live CLI sessions + in-flight turn
        # state, and every other headless session the satellite holds
        # (``other_sessions``; None when the satellite could not build the
        # list, its ``partial``). The run-recovery callback (registered at startup) is
        # awaited first, so a restarted proxy re-adopts running turns
        # instead of failing them blind (Mode C), then the alive report's
        # hooks: one task per machine, under no lock (``satellite_link``).
        conn = self._connections.get(machine_id)
        if conn is not None and not self._links.older_report(machine_id, conn, msg):
            self._links.note_sessions(machine_id, conn, msg)

    async def _frame_transcript_lines(self, machine_id: str, msg: dict) -> None:
        # Interactive remote PTY: the satellite
        # tailed this session's transcript JSONL and forwarded new lines → the
        # InteractiveSession persists them to chat_messages via the SAME tailer
        # parser used for local sessions (no on-disk mirror). Routed by
        # session_id (the proxy's InteractiveSession id), not the machine.
        from core.session import interactive_session
        _isess = interactive_session.get(msg.get("session_id", "") or "")
        # Only the satellite that actually owns this interactive session may
        # feed its transcript — otherwise a compromised satellite could
        # inject forged chat lines into another machine's session by id.
        if _isess is not None and _isess.target == machine_id:
            # The lines up to ``offset`` are acked once persisted
            # (``ack_transcript_lines``).
            offset = msg.get("offset")
            _isess.feed_transcript_lines(
                msg.get("lines") or [],
                ack_offset=offset if isinstance(offset, int) and not isinstance(offset, bool) else None)
        elif _isess is None:
            # A terminal the platform holds no entry for yet: kept until this
            # sign-in's terminal report, for the claim that may take it back.
            self.hold_transcript_lines(machine_id, msg.get("session_id", "") or "", msg.get("lines") or [])
        elif _isess is not None:
            logger.warning(
                "Dropping transcript_lines for session %s from machine %s "
                "(owned by %s)",
                (msg.get("session_id") or "")[:8], placement.log_id(machine_id),
                (_isess.target or "")[:8],
            )

    async def ack_transcript_lines(self, machine_id: str, session_id: str, offset: int,
                                   open_token: str = "") -> None:
        """A terminal's transcript lines up to ``offset`` are persisted: the
        machine sends only the lines past it again at its next sign-in."""
        ack: dict = {"type": "transcript_ack", "session_id": session_id, "offset": offset}
        if open_token:
            ack["open_token"] = open_token
        await self.send_fire_and_forget(machine_id, ack)

    async def _frame_pty_inject_result(self, machine_id: str, msg: dict) -> None:
        # Server-prompt injection ACK/NACK from the satellite (the
        # delegation-delivery PTY rung for otodock-attached sessions). Same
        # owner check as transcript_lines — only the owning satellite may
        # resolve an in-flight injection.
        from core.session import interactive_session
        _isess = interactive_session.get(msg.get("session_id", "") or "")
        if _isess is not None and _isess.target == machine_id:
            _isess.handle_inject_result(
                str(msg.get("inject_id", "")),
                bool(msg.get("ok")),
                str(msg.get("reason", "") or ""),
            )
        elif _isess is not None:
            logger.warning(
                "Dropping pty_inject_result for session %s from machine %s "
                "(owned by %s)",
                (msg.get("session_id") or "")[:8], placement.log_id(machine_id),
                (_isess.target or "")[:8],
            )

    async def _frame_session_started(self, machine_id: str, msg: dict) -> None:
        session_id = msg.get("session_id", "")
        logger.info(
            "Satellite %s: session %s started (pid=%s, path=%s)",
            placement.log_id(machine_id), session_id[:8],
            msg.get("pid"), msg.get("execution_path"),
        )

    async def _frame_session_ended(self, machine_id: str, msg: dict) -> None:
        session_id = msg.get("session_id", "")
        gone = turn_end_of(msg)
        if gone.get("reason") in ENGINE_GONE_REASONS:
            # The engine ended by itself with no turn reading it on the
            # machine. No sentinel: the next turn would meet it.
            from core.session.session_manager import _get_remote_layer
            said = msg.get("incarnation")
            held = _get_remote_layer().note_engine_gone(
                machine_id, session_id, gone, said if isinstance(said, str) else "")
            logger.warning(
                "Satellite %s: the engine of session %s is gone (%s, exit=%s)%s",
                placement.log_id(machine_id), session_id[:8], gone["reason"], gone.get("exit_code"),
                "" if held else ", a session it does not hold here or an earlier one of the id")
            return
        conn = self._connections.get(machine_id)
        if conn:
            queue = conn.session_queues.get(session_id)
            if queue:
                queue.put_nowait(None)  # sentinel
        logger.info(
            "Satellite %s: session %s ended (exit=%s)",
            placement.log_id(machine_id), session_id[:8], msg.get("exit_code"),
        )

    async def _frame_session_aborted(self, machine_id: str, msg: dict) -> None:
        # Satellite confirms the abort is fully complete. Only a HARD
        # abort (which armed the ack via arm_abort_acked before sending)
        # drains here: its producer is being cancelled, so events the
        # dying CLI flushed during the kill window would otherwise leak
        # into the next (auto-resumed) turn. A GRACEFUL codex abort also
        # acks (the satellite always acks ``abort``), but its producer is
        # ALIVE and consuming the closing turn's tail — draining would
        # steal the terminal turn event and strand it, so skip. (A late
        # hard-abort ack past wait_abort_acked's timeout also skips; the
        # next turn's start-of-turn drain in send_message covers it.)
        session_id = msg.get("session_id", "")
        armed = (machine_id, session_id) in self._abort_acked_events
        drained = 0
        if armed:
            conn = self._connections.get(machine_id)
            if conn:
                queue = conn.session_queues.get(session_id)
                if queue:
                    while True:
                        try:
                            queue.get_nowait()
                            drained += 1
                        except asyncio.QueueEmpty:
                            break
        self._signal_abort_acked(machine_id, session_id)
        logger.info(
            "Satellite %s: session %s abort confirmed (%s)",
            placement.log_id(machine_id), session_id[:8],
            (f"drained {drained} stale events" if armed
             else "graceful — queue left to the live producer"),
        )

    async def _frame_memory_stop(self, machine_id: str, msg: dict) -> None:
        machine_memory.note_stop(machine_id, msg)

    async def _frame_turn_ended(self, machine_id: str, msg: dict) -> None:
        # Satellite finished the current turn (stdout read loop exited
        # normally or stop_turn command was processed). Inject a marker
        # so RemoteExecutionLayer.send_message can yield DONE and return.
        # ``command_id`` is echoed by the satellite so the layer can
        # filter out a turn_ended from a previous turn whose late
        # file-scan delivered after the proxy's drain timeout (otherwise
        # the stale marker would terminate the next turn prematurely).
        session_id = msg.get("session_id", "")
        conn = self._connections.get(machine_id)
        if conn:
            queue = conn.session_queues.get(session_id)
            if queue:
                try:
                    queue.put_nowait({
                        "type": "_turn_ended",
                        "command_id": msg.get("command_id", ""),
                        **turn_end_of(msg),
                    })
                except asyncio.QueueFull:
                    # Dropping the marker stalls the turn until the drain
                    # timeout — leave a trace so it isn't a silent hang.
                    logger.warning(
                        "turn_ended marker dropped (queue full) for "
                        "session=%s machine=%s", session_id, machine_id,
                    )

    async def _frame_codex_thread_id(self, machine_id: str, msg: dict) -> None:
        # The satellite reported the engine's RESUME HANDLE for a session
        # (the frame type is Codex's own wire vocabulary — frozen; the
        # marker it becomes on the session queue is the generic one every
        # engine's remote adapter records through
        # ``remote_turn.record_resume_handle`` and persists on its first
        # turn).
        session_id = msg.get("session_id", "")
        handle = msg.get("thread_id", "")
        if session_id and handle:
            conn = self._connections.get(machine_id)
            if conn:
                queue = conn.session_queues.get(session_id)
                if queue:
                    queue.put_nowait({
                        "type": "_resume_handle",
                        "handle": handle,
                    })

    async def _frame_file_changed(self, machine_id: str, msg: dict) -> None:
        # Apply the change to the platform's agent_dir so the dashboard
        # workspace listing reflects what the satellite agent did.
        # Large files (>1 MB) are sent without `content_b64` — the
        # satellite already wrote them; we issue a pull to fetch the body.
        # Admitted, bounded and shed in the mixin; never one task per
        # frame with nothing above it.
        self._dispatch_file_changed(machine_id, msg)

    async def _frame_file_manifest(self, machine_id: str, msg: dict) -> None:
        self._on_manifest_frame(machine_id, msg)

    async def _frame_file_content(self, machine_id: str, msg: dict) -> None:
        conn = self._connections.get(machine_id)
        if conn is not None:
            conn.last_transfer_at = time.monotonic()
        request_id = msg.get("request_id", "")
        st = self._pending_pulls.get(request_id)
        # Only the machine a pull went to answers it.
        if st is not None and st.machine_id == machine_id:
            self._on_pull_chunk(st, msg)

    async def _frame_mcp_install_progress(self, machine_id: str, msg: dict) -> None:
        # Satellite streaming install progress during a sync_mcps batch.
        # Delivered to any registered per-command callback (mcp_sync
        # keeps one keyed by command_id); remote_session_start turns them
        # into the dashboard's install_progress frames.
        command_id = msg.get("command_id", "")
        cb = self._install_progress_cbs.get(command_id)
        logger.info(
            "satellite mcp_install_progress RECEIVED: cmd=%s mcp=%s phase=%s pct=%s cb_match=%s registered_cmds=%d",
            command_id[:8], msg.get("mcp", "-"), msg.get("phase", "-"),
            msg.get("pct", "-"), "YES" if cb is not None else "NO",
            len(self._install_progress_cbs),
        )
        if cb is not None:
            try:
                r = cb(msg)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                logger.exception("install progress cb raised")

    async def _frame_step_output(self, machine_id: str, msg: dict) -> None:
        # A running app step's output (APPS.md "Steps"): to the callback
        # app_steps registered for the command; nothing else keeps it.
        cb = self._step_output_cbs.get(msg.get("command_id", ""))
        if cb is not None:
            try:
                r = cb(str(msg.get("text") or ""))
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                logger.exception("step output cb raised")

    async def _frame_tool_out(self, machine_id: str, msg: dict) -> None:
        # Direct LLM's tool processes on a compute machine (core/remote/tool_relay.py).
        from core.remote import tool_relay
        tool_relay.on_out(machine_id, msg)

    async def _frame_tool_exit(self, machine_id: str, msg: dict) -> None:
        from core.remote import tool_relay
        tool_relay.on_exit(machine_id, msg)

    async def _frame_app_server(self, machine_id: str, msg: dict) -> None:
        # An app server on a compute machine: its output and its exit, to the
        # process the supervisor holds for it.
        from services.apps import app_compute
        app_compute.on_frame(machine_id, msg)

    async def _frame_http_request(self, machine_id: str, msg: dict) -> None:
        # HTTP-over-WS tunnel: subprocess on satellite wants to call a
        # platform-internal endpoint. Dispatch via httpx loopback.
        from core.remote.satellite_http_tunnel import get_dispatcher
        await get_dispatcher().handle_request_frame(self, machine_id, msg)

    async def _frame_http_request_chunk(self, machine_id: str, msg: dict) -> None:
        # Continuation of a streamed request body for an open tunnel
        # stream. Synchronous queue put (no upstream call).
        from core.remote.satellite_http_tunnel import get_dispatcher
        get_dispatcher().handle_request_chunk(machine_id, msg)

    async def _frame_http_abort(self, machine_id: str, msg: dict) -> None:
        # 0.5.115+: the satellite's local client (hook script / MCP
        # client) went away mid-stream — close our upstream now instead
        # of holding it until the idle sweep.
        from core.remote.satellite_http_tunnel import get_dispatcher
        get_dispatcher().abort_stream(machine_id, str(msg.get("stream_id", "")))

    async def _frame_local_session_open(self, machine_id: str, msg: dict) -> None:
        # otodock-CLI: a satellite-initiated request to open an interactive
        # session (the user ran `otodock` on the machine). Identity is
        # re-derived from THIS machine's owner binding — never the frame.
        spawn(self._handle_local_session_open(machine_id, msg),
              name="handle_local_session_open", owner="satellite_connection")

    async def _frame_local_session_list(self, machine_id: str, msg: dict) -> None:
        # otodock-CLI --resume: list the owner's resumable chats for an agent.
        spawn(self._handle_local_session_list(machine_id, msg),
              name="handle_local_session_list", owner="satellite_connection")

    async def _frame_local_session_detached(self, machine_id: str, msg: dict) -> None:
        # The local `otodock` terminal disconnected — drop the idle-reaper
        # exemption (the PTY stays alive; the chat is dashboard-resumable).
        from core.session import otodock_session
        otodock_session.detach_local_session(msg.get("session_id", ""))


    # --- Heartbeat monitor ---

    async def heartbeat_monitor(self) -> None:
        """Background task: detect stale connections + drive admin alerts.

        Each tick (``_MONITOR_TICK_S``):
        - a machine not heard from for ``_SILENT_DROP_S`` (no heartbeat, no
          transfer frame) is dropped: its socket closed, the connection the
          monitor judged deregistered. The server's keepalive closes a
          silent socket's transport sooner but tells the application
          nothing while bytes still wait in it, so this is the bound for a
          stopped machine with frames queued. The ping rules on both sides
          end a silent link in 50 to 70 s and a satellite ends its own link
          when a heartbeat tick is over 60 s late, so no healthy machine is
          silent this long.
        - every other tick: reconcile admin offline/online notifications
          against sustained reachability (``_evaluate_admin_machine_alerts``).
        """
        tick = 0
        while True:
            await asyncio.sleep(_MONITOR_TICK_S)
            tick += 1
            now = time.monotonic()
            stale = [
                (machine_id, conn) for machine_id, conn in list(self._connections.items())
                if now - last_contact(conn) > _SILENT_DROP_S
            ]
            for machine_id, gone in stale:
                logger.warning(
                    "Satellite %s: nothing heard for %.0f s, disconnecting",
                    placement.log_id(machine_id), now - last_contact(gone),
                )
                # Deregistered first, with the connection that was judged: a
                # machine that signed in again meanwhile keeps its new one.
                await self.deregister(machine_id, expected=gone)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        gone.ws.close(code=4003, reason="Heartbeat timeout"), 5.0)

            self._links.prune(set(self._connections), self.machine_dropped_recently)
            from core.remote import machine_push_lane
            machine_push_lane.prune(set(self._connections))
            if tick % 2:
                continue
            # Reconcile sustained-outage admin notifications. Isolated in a
            # try/except so a transient DB hiccup can't kill the monitor.
            try:
                await self._evaluate_admin_machine_alerts()
            except Exception:
                logger.exception("admin machine-alert evaluation failed")


def last_contact(conn) -> float:
    """When a connection was last heard from (monotonic): any frame it sent.
    A heartbeat waits its turn on the machine's control lane behind manifest
    pages, inline file changes and tunnel request chunks, so a machine on a slow
    uplink can be sending for minutes with no heartbeat through. A stopped
    process sends nothing at all."""
    return max(conn.last_heartbeat, getattr(conn, "last_transfer_at", 0.0),
               getattr(conn, "last_frame_at", 0.0))


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _seconds_since_iso(iso: str | None) -> float | None:
    """Seconds elapsed since an ISO-8601 timestamp, or None if unparseable.

    Used by the sustained-outage evaluator to measure how long a machine
    has been out of contact (``now - last_seen``). Tolerates both
    timezone-aware and naive timestamps (naive is treated as UTC, matching
    ``_iso_now``). Negative results (clock skew) are clamped to 0.
    """
    if not iso:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    return max(0.0, delta)


# Module-level singleton (first asked for in startup.py's lifespan)
_connection_manager: SatelliteConnectionManager | None = None


def get_connection_manager() -> SatelliteConnectionManager:
    """Return the singleton connection manager."""
    global _connection_manager
    if _connection_manager is None:
        _connection_manager = SatelliteConnectionManager()
    return _connection_manager


# The placement leaf reads a machine's live exit state through this reader
# (it imports nothing of the remote side).
placement.register_live_exit_state(lambda machine_id: get_connection_manager().exit_state(machine_id))
def _live_exit_report(machine_id: str) -> dict:
    """The live connection's exit report; a stand-in manager that knows no
    report answers none, and the row's copy answers then."""
    report = getattr(get_connection_manager(), "exit_report", None)
    return report(machine_id) if report is not None else {}


placement.register_live_exit_report(_live_exit_report)
