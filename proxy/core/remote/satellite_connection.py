"""Satellite connection manager — tracks connected satellite daemons.

Each satellite maintains a persistent WebSocket connection to the proxy.
The SatelliteConnectionManager routes commands to the correct satellite
and manages per-session event queues for RemoteExecutionLayer consumption.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from fastapi import WebSocket

logger = logging.getLogger("claude-proxy.satellite")


# Send queue capacity per connection. Matches satellite-side queue size so
# back-pressure behavior is symmetric. Drops oldest on overflow.
_SEND_QUEUE_SIZE = 10_000

# Bounded in-flight window for chunked file pushes. push_file sends at most this
# many 512KB chunks onto the bulk lane before awaiting a satellite ack (a
# "flush"), then continues. This (a) caps transient memory + bulk-queue depth
# regardless of file size, (b) paces to the satellite's apply rate
# (backpressure — the bulk lane can no longer overflow + silently drop a chunk),
# (c) gives a per-window timeout instead of one timeout for the whole file (a
# large file over a slow link no longer fails the single 30s ack), and (d) lets
# a failed/timed-out window abort the transfer early instead of blasting the
# rest. 16 × 512KB = 8MB in flight. The satellite acks any chunk carrying a
# command_id and commits only on the final chunk's hash, so windowing needs no
# satellite protocol change.
PUSH_WINDOW_CHUNKS = 16

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

# Terminal message persisted when the grace window lapses without a reconnect
# — a visible marker beats a silent truncation that looks
# like a clean finish.

# Minimum SATELLITE_VERSION (as a version tuple) that forwards Codex background
# sub-agent thread events past the main turn, enabling proxy-side remote bg
# supervision. Older satellites degrade to sweep-at-turn-end. See
# satellite_supports_bg + RemoteExecutionLayer.start_session.
_REMOTE_CODEX_BG_MIN_VERSION = (0, 5, 18)

# Minimum SATELLITE_VERSION that handles `pty_inject` (server-prompt stdin
# injection into otodock-attached sessions — the delegation-delivery PTY rung).
# Older satellites would silently drop the frame; the proxy holds the prompt
# queued instead (see interactive_session._try_satellite_inject).
_PTY_INJECT_MIN_VERSION = (0, 5, 83)

# Minimum SATELLITE_VERSION that handles `interrupt_turn` (soft headless-CLI
# abort: control_request{interrupt} into the CLI's stdin, process + MCP
# sidecars survive). Older satellites would silently drop the frame, so the
# proxy keeps the hard abort (tree-kill + re-warm) for them.
_SOFT_INTERRUPT_MIN_VERSION = (0, 5, 89)

# Minimum SATELLITE_VERSION that handles `file_stat` (cheap pull-through cache
# revalidation: size+mtime probe instead of a full re-transfer). Older
# satellites would silently drop the frame — the proxy then skips the probe
# and re-pulls unconditionally (today's behavior, no regression).
_FILE_STAT_MIN_VERSION = (0, 5, 95)


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
    capabilities: dict = field(default_factory=dict)
    # Reported SATELLITE_VERSION from the auth message. Drives feature gates like
    # remote Codex bg-sub-agent supervision (see satellite_supports_bg).
    satellite_version: str = ""
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
    # Defaults hold until the first heartbeat arrives.
    load: dict = field(default_factory=dict)
    reported_sessions: int = 0
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
    # multi-chunk file push can never delay a command ack or the keepalive past
    # a single chunk. ``send_wakeup`` is set by enqueue_send to wake the writer
    # when either lane gains an item (lets it serve two lanes without cancelling
    # a blocking Queue.get()).
    send_queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=_SEND_QUEUE_SIZE)
    )
    bulk_queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=_SEND_QUEUE_SIZE)
    )
    send_wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    writer_task: asyncio.Task | None = None

    async def enqueue_send(self, msg: dict, *, bulk: bool = False) -> None:
        """Queue a message for the writer task. Drops oldest on overflow.

        ``bulk=True`` routes the message to the BULK lane (file_push data
        chunks) instead of the CONTROL lane; the writer always drains control
        first, so a large transfer can never delay command acks / the
        keepalive. Safe to call from any coroutine. Returns immediately — the
        actual ws.send happens inside `_writer_loop`.
        """
        queue = self.bulk_queue if bulk else self.send_queue
        label = "bulk" if bulk else "control"
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                dropped = queue.get_nowait()
                logger.warning(
                    "Satellite %s %s send queue full (%d), dropped oldest: %s",
                    self.machine_id[:8],
                    label,
                    _SEND_QUEUE_SIZE,
                    dropped.get("type") if isinstance(dropped, dict) else "?",
                )
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:
                logger.error(
                    "Satellite %s %s send queue still full after drop; "
                    "message lost: %s",
                    self.machine_id[:8],
                    label,
                    msg.get("type") if isinstance(msg, dict) else "?",
                )
        finally:
            # Wake the writer whichever lane received the item.
            self.send_wakeup.set()


def _list_admin_subs() -> list[str]:
    """All platform admins. Used as the notify-target for admin-paired
    satellite up/down events."""
    from storage import database as task_store
    return [u["sub"] for u in task_store.list_users() if u.get("role") == "admin"]


async def _notify_admins_machine_state_change(
    machine_id: str, *, online: bool,
) -> None:
    """Fire a notification to every platform admin when an admin-paired
    satellite changes connection state. No-op for user-paired machines
    (their owner already sees the soft-fallback banner).

    `online=True` fires the back-online notice; `online=False` fires the
    just-went-offline notice. Skips silently when the machine row is
    missing or its `pairing_scope` is not `'admin'`.
    """
    from storage import remote_store
    from services.notifications import notification_manager

    machine = await asyncio.to_thread(remote_store.get_remote_machine, machine_id)
    if not machine or (machine.get("pairing_scope") or "") != "admin":
        return

    machine_name = machine.get("name") or machine_id[:8]
    admin_subs = await asyncio.to_thread(_list_admin_subs)
    if not admin_subs:
        return

    if online:
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


from core.remote.satellite_grace import SatelliteGraceMixin
from core.remote.satellite_admin_alerts import SatelliteAdminAlertsMixin
from core.remote.satellite_file_transfer import SatelliteFileTransferMixin, _PullStream  # noqa: F401


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
        # command_id -> (machine_id, future) so we can reject all pending acks
        # for a machine on deregister instead of letting them wait 30s timeout.
        self._pending_acks: dict[str, tuple[str, asyncio.Future]] = {}
        # request_id -> _PullStream for streaming file pulls. Kept separate
        # from _pending_acks (which is push acks): file_content chunks are
        # written straight to a .partial on disk and the future resolves on
        # the final chunk. Rejected + cleaned up on deregister.
        self._pending_pulls: dict[str, _PullStream] = {}
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
        # Per-command install progress callbacks. mcp_sync registers one
        # before issuing sync_mcps and removes it after the ack arrives.
        self._install_progress_cbs: dict[str, object] = {}
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
        self._lock = asyncio.Lock()

    def set_sessions_alive_callback(self, cb) -> None:
        """Register the run-recovery handler for `sessions_alive` reports."""
        self._sessions_alive_callback = cb

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
        arrived, False on timeout (older satellites that don't ack, or a lost
        message — the caller proceeds anyway after draining). Pops the event.

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
        command ack / the keepalive pong by at most one chunk, never the whole
        file. When both lanes are empty the writer clears + re-checks
        ``send_wakeup`` (closing the lost-wakeup window) then awaits it, so it
        never busy-waits and never cancels a blocking Queue.get().

        On send failure: re-queue the message to its own lane (slight
        reordering accepted over losing it) then trigger deregister so callers
        wake up immediately instead of waiting for the heartbeat monitor.
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
                if not isinstance(msg, dict):
                    continue
                try:
                    await conn.ws.send_text(json.dumps(msg))
                except Exception as e:
                    logger.warning(
                        "Satellite %s writer send failed: %s; requeueing %s",
                        conn.machine_id[:8], e,
                        msg.get("type"),
                    )
                    try:
                        lane.put_nowait(msg)
                    except asyncio.QueueFull:
                        logger.warning(
                            "Satellite %s could not requeue %s after "
                            "send failure (queue full)",
                            conn.machine_id[:8], msg.get("type"),
                        )
                    # Fire-and-forget deregister: don't await ourselves.
                    # Identity-guarded — a send failure on a connection that
                    # a duplicate reconnect already replaced must not tear
                    # down the fresh one.
                    asyncio.create_task(
                        self.deregister(conn.machine_id, expected=conn)
                    )
                    return
        except asyncio.CancelledError:
            return

    # --- Connection lifecycle ---

    async def register(
        self, machine_id: str, ws: WebSocket, capabilities: dict,
        satellite_version: str = "",
    ) -> SatelliteConnection:
        """Register a new satellite connection. Closes old connection if duplicate."""
        async with self._lock:
            old = self._connections.get(machine_id)
            if old:
                logger.warning(
                    "Satellite %s: duplicate connection, closing old", machine_id[:8]
                )
                try:
                    await old.ws.close(code=4002, reason="Replaced by new connection")
                except Exception:
                    pass
                # Stop the old writer now — its handler's deregister is a
                # no-op once we replace the dict entry (identity guard), so
                # nobody else will cancel it.
                old_writer = getattr(old, "writer_task", None)
                if old_writer is not None and not old_writer.done():
                    old_writer.cancel()

            conn = SatelliteConnection(
                machine_id=machine_id,
                ws=ws,
                capabilities=capabilities,
                satellite_version=satellite_version,
            )
            if old:
                # Duplicate-connection replacement: carry the old connection's
                # in-flight session queues straight onto the new one (the
                # grace area below only covers a clean drop→reconnect, where
                # deregister ran first). The SAME queue objects move over, so
                # producers' `info.event_queue` references stay valid and the
                # satellite's buffered events replay into them.
                conn.session_queues.update(old.session_queues)
                conn.session_execution_paths.update(old.session_execution_paths)
            self._connections[machine_id] = conn

            # Re-adopt any sessions held from a recent drop
            # of THIS machine so their in-flight turns continue on the new
            # connection. The SAME queue objects are restored → the producers'
            # `info.event_queue` references stay valid; the satellite replays
            # its buffered events into them automatically (no resume request
            # needed). Inside the lock → mutually exclusive with deregister /
            # _expire_grace; whichever pops `_grace_sessions` first wins.
            held = self._grace_sessions.pop(machine_id, None)
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
                    "reconnect (grace)", machine_id[:8], len(held),
                )

        # Start the writer task BEFORE updating DB status so any send
        # triggered during the rest of register() has the consumer running.
        # Auth has already succeeded by the time we get here (handler runs
        # auth then calls register), so the writer can safely begin sending.
        conn.writer_task = asyncio.create_task(
            self._writer_loop(conn),
            name=f"ws-writer-{machine_id[:8]}",
        )

        # Mark online + refresh last_seen. Admin offline/online
        # notifications are deliberately NOT fired from this connect edge —
        # they're owned by the sustained-state evaluator
        # (_evaluate_admin_machine_alerts), which clears any outstanding
        # `offline_alerted` flag and fires the "back online" notice on its
        # next tick. Keeping it out of the hot connect path is what makes a
        # reconnect after a proxy restart / auto-update / blip silent.
        from storage import remote_store
        remote_store.update_machine_status(
            machine_id, "online", last_seen=_iso_now()
        )
        remote_store.update_machine_capabilities(machine_id, capabilities)
        logger.info(
            "Satellite %s connected (capabilities: %s)",
            machine_id[:8], list(capabilities.get("installed_clis", [])),
        )

        # Kick off a background MCP verify pass. If any MCP on the satellite
        # is marked unhealthy (mid-install marker present) or its version_hash
        # no longer matches what the proxy would install, the next session
        # warmup will see it in the diff and re-install. No action needed
        # here — sync_mcps_for_session always re-verifies before install.
        async def _kick_verify():
            try:
                await self.send_command(
                    machine_id,
                    {"type": "sync_mcps_verify"},
                    timeout=15.0,
                )
            except Exception as e:
                logger.debug("reconcile verify on reconnect failed: %s", e)
        asyncio.create_task(_kick_verify())

        # Catch up every agent on this machine to the platform NOW — applying
        # deletes (tombstones) + drift that landed while it was offline, without
        # waiting for a session warmup. Best-effort, background, per-(machine,
        # agent)-locked against any concurrent warmup.
        async def _kick_workspace_sync():
            try:
                from core.session.session_manager import _get_remote_layer
                layer = _get_remote_layer()
                if layer is not None:
                    await layer.sync_all_agents_on_reconnect(machine_id)
            except Exception:
                logger.debug(
                    "reconnect workspace sync kick failed for %s", machine_id[:8],
                )
        asyncio.create_task(_kick_workspace_sync())

        return conn

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
        async with self._lock:
            current = self._connections.get(machine_id)
            if expected is not None and current is not expected:
                logger.info(
                    "Satellite %s: stale deregister ignored (connection "
                    "already replaced)", machine_id[:8],
                )
                return
            conn = self._connections.pop(machine_id, None)
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
                    name=f"pty-grace-{machine_id[:8]}",
                )
                logger.info(
                    "Satellite %s: holding interactive PTY(s) in reconnect "
                    "grace (%.0fs)", machine_id[:8], _GRACE_WINDOW_S,
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
                    name=f"grace-{machine_id[:8]}",
                )
                logger.info(
                    "Satellite %s: holding %d in-flight session(s) in "
                    "reconnect grace (%.0fs)",
                    machine_id[:8], len(conn.session_queues), _GRACE_WINDOW_S,
                )

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
                    "tunnel stream cleanup failed for %s", machine_id[:8],
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
                cid for cid, (mid, _) in self._pending_acks.items()
                if mid == machine_id
            ]
            for cid in to_reject:
                entry = self._pending_acks.pop(cid, None)
                if entry:
                    _, future = entry
                    if not future.done():
                        future.set_exception(
                            RuntimeError(f"Satellite {machine_id[:8]} disconnected")
                        )

            # Reject + clean up any streaming pulls for this machine so the
            # caller wakes immediately and no .partial is left on disk.
            pulls_to_reject = [
                rid for rid, st in self._pending_pulls.items()
                if st.machine_id == machine_id
            ]
            for rid in pulls_to_reject:
                st = self._pending_pulls.pop(rid, None)
                if st is not None:
                    self._cleanup_pull_stream(st)
                    if not st.future.done():
                        st.future.set_exception(
                            RuntimeError(f"Satellite {machine_id[:8]} disconnected")
                        )

            # Drop the per-machine install lock only if it's not currently
            # held (held lock → a waiter will take it and discover the
            # disconnect on its next send_command).
            lock = self._install_locks.get(machine_id)
            if lock is not None and not lock.locked():
                self._install_locks.pop(machine_id, None)

            from storage import remote_store
            remote_store.update_machine_status(machine_id, "disconnected")
            logger.info("Satellite %s disconnected", machine_id[:8])
            # No admin notification on this disconnect edge: the
            # sustained-state evaluator (_evaluate_admin_machine_alerts)
            # fires "offline" only once the machine stays unreachable past
            # the grace window, so a transient drop followed by a fast
            # reconnect (restart / auto-update / network blip) never pages
            # admins.


    # --- Connection queries ---

    def is_connected(self, machine_id: str) -> bool:
        return machine_id in self._connections

    def machine_at_capacity(self, machine_id: str) -> bool:
        """Soft pre-check: True when the satellite reports it's at/over its
        effective session ceiling — the admin override (remote_machines.max_sessions)
        if set, else the satellite's own reported recommendation. Fail-OPEN on
        unknown (no connection / no cap / no count); the satellite's hard reject in
        session_manager is the real backstop, so a stale or zero count never blocks."""
        conn = self._connections.get(machine_id)
        if not conn:
            return False
        cap = None
        try:
            from storage import remote_store
            m = remote_store.get_remote_machine(machine_id) or {}
            if m.get("max_sessions"):
                cap = int(m["max_sessions"])
        except Exception:
            cap = None
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
        from storage import remote_store
        out: list[dict] = []
        for mid, conn in list(self._connections.items()):
            caps = conn.capabilities or {}
            try:
                m = remote_store.get_remote_machine(mid) or {}
            except Exception:
                m = {}
            rec = caps.get("recommended_max_sessions")
            db_max = m.get("max_sessions")
            out.append({
                "machine_id": mid,
                "name": m.get("name") or mid[:8],
                "online": True,
                "active_sessions": conn.reported_sessions,
                "max_sessions": int(db_max) if db_max else (int(rec) if rec else None),
                "cpu_pct": (conn.load or {}).get("cpu_pct", 0.0),
                "mem_pct": (conn.load or {}).get("mem_pct", 0.0),
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
        ``pty_close`` and streams ``pty_output``. When False (older
        satellite), remote interactive falls back to headless ``-p`` instead of
        hanging on a ``pty_open`` the satellite never answers."""
        conn = self._connections.get(machine_id)
        return bool(conn and conn.capabilities.get("interactive_pty"))

    def satellite_os(self, machine_id: str) -> str:
        """The connected satellite's OS as it reports in capabilities
        (``platform.system().lower()`` → ``"windows"`` / ``"linux"`` / ``"darwin"``),
        or ``""`` if unknown/offline. Used to scope OS-specific interactive-PTY
        workarounds (e.g. the Windows-ConPTY cold-submit backstop)."""
        conn = self._connections.get(machine_id)
        return str(conn.capabilities.get("os", "")) if conn else ""

    def satellite_supports_bg(self, machine_id: str) -> bool:
        """True if the connected satellite is new enough to forward background
        sub-agent thread events AFTER the main turn (the satellite-side change in
        SATELLITE_VERSION 0.5.18). When False — older satellite, or unknown
        version — the proxy leaves remote Codex bg supervision off and the
        translator sweeps bg subs at turn end (today's behavior, no regression)."""
        return self._satellite_at_least(machine_id, _REMOTE_CODEX_BG_MIN_VERSION)

    def satellite_supports_pty_inject(self, machine_id: str) -> bool:
        """True if the connected satellite handles ``pty_inject`` (server-prompt
        stdin injection for otodock-attached sessions, SATELLITE_VERSION 0.5.83).
        When False the proxy keeps the prompt queued rather than sending a frame
        the satellite would silently drop."""
        return self._satellite_at_least(machine_id, _PTY_INJECT_MIN_VERSION)

    def satellite_supports_soft_interrupt(self, machine_id: str) -> bool:
        """True if the connected satellite handles ``interrupt_turn`` (soft
        headless-CLI abort, SATELLITE_VERSION 0.5.89). When False the proxy
        keeps the hard abort path — the frame would be silently dropped and
        the turn would never close."""
        return self._satellite_at_least(machine_id, _SOFT_INTERRUPT_MIN_VERSION)

    def satellite_supports_file_stat(self, machine_id: str) -> bool:
        """True if the connected satellite handles ``file_stat`` (pull-through
        cache revalidation, SATELLITE_VERSION 0.5.95). When False the proxy
        skips the probe and re-pulls unconditionally — the frame would be
        silently dropped and the probe would only burn its timeout."""
        return self._satellite_at_least(machine_id, _FILE_STAT_MIN_VERSION)

    def _satellite_at_least(self, machine_id: str, version: tuple) -> bool:
        conn = self._connections.get(machine_id)
        if not conn or not conn.satellite_version:
            return False
        try:
            parts = tuple(int(p) for p in conn.satellite_version.split(".")[:3])
        except (ValueError, AttributeError):
            return False
        return parts >= version

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
            raise RuntimeError(f"Satellite {machine_id[:8]} not connected")

        if command_id is None:
            command_id = str(uuid.uuid4())
        msg["command_id"] = command_id

        # Create ack future keyed by (machine_id, future) so deregister can
        # cancel them en masse on disconnect.
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_acks[command_id] = (machine_id, future)

        try:
            # Enqueue rather than ws.send_text(). The writer task does the
            # actual send. If the WS is dead, the writer triggers deregister
            # which rejects this future with RuntimeError immediately —
            # caller wakes up without waiting for the timeout.
            await conn.enqueue_send(msg)
            ack = await asyncio.wait_for(future, timeout=timeout)
            if ack.get("status") == "error":
                raise RuntimeError(
                    f"Satellite command error: {ack.get('error', 'unknown')}"
                )
            return ack
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Satellite {machine_id[:8]} command timeout ({timeout}s)"
            )
        finally:
            self._pending_acks.pop(command_id, None)

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
        except otodock_session.OtodockSessionError as e:
            await self.send_fire_and_forget(machine_id, {
                "type": "local_session_error",
                "request_id": request_id,
                "reason": str(e),
            })
        except Exception:
            logger.exception(
                "otodock local_session_open failed (machine=%s)", machine_id[:8]
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
                "otodock local_session_list failed (machine=%s)", machine_id[:8]
            )
            await self.send_fire_and_forget(machine_id, {
                "type": "local_session_error",
                "request_id": request_id,
                "reason": "internal error listing sessions",
            })


    # --- Session event routing ---

    def create_session_queue(
        self, machine_id: str, session_id: str, execution_path: str,
        *, maxsize: int = 1000,
    ) -> asyncio.Queue:
        """Create an event queue for a remote session.

        ``maxsize`` is raised by the run-recovery adoption path (the replay
        of a retained turn buffer arrives as one burst larger than the
        default cap; session_event dispatch silently drops on full)."""
        conn = self._connections.get(machine_id)
        if not conn:
            raise RuntimeError(f"Satellite {machine_id[:8]} not connected")
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

    async def handle_message(self, machine_id: str, msg: dict) -> None:
        """Route an incoming message from a satellite."""
        msg_type = msg.get("type", "")

        if msg_type == "ack":
            command_id = msg.get("command_id", "")
            entry = self._pending_acks.get(command_id)
            if entry:
                _, future = entry
                if not future.done():
                    future.set_result(msg)

        elif msg_type == "pause":
            # Deliberate pause (tray Pause). Persist so the sustained-outage
            # evaluator skips this machine (no false admin alert). The
            # satellite closes the WS right after this; the next successful
            # auth (tray Resume / reboot) clears the flag.
            from storage import remote_store
            await asyncio.to_thread(remote_store.set_paused, machine_id, True)

        elif msg_type == "heartbeat":
            conn = self._connections.get(machine_id)
            if conn:
                conn.last_heartbeat = time.monotonic()
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
                _active = msg.get("active_sessions")
                if isinstance(_active, int):
                    conn.reported_sessions = _active
            from storage import remote_store
            remote_store.update_machine_status(
                machine_id, "online", last_seen=_iso_now()
            )
            # Respond with pong
            await self.send_fire_and_forget(machine_id, {"type": "pong"})

        elif msg_type == "session_event":
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
                            machine_id[:8], session_id[:8],
                        )

        elif msg_type == "pty_output":
            # Interactive remote PTY: rendered bytes from
            # the satellite-hosted TUI → the RemotePtyProcess, which fans them out
            # to the dashboard viewer + scrollback. Routed by (machine_id,
            # session_id) — NOT the lossy session_event queue (a dropped byte
            # would corrupt the xterm). base64 keeps raw control bytes intact.
            import base64 as _b64
            from core.remote import remote_pty
            try:
                _data = _b64.b64decode(msg.get("data_b64", "") or "")
            except Exception:
                _data = b""
            if _data:
                remote_pty.feed_output(machine_id, msg.get("session_id", ""), _data)

        elif msg_type == "pty_exit":
            # The satellite-hosted interactive PTY process ended → fire the
            # InteractiveSession's on_exit (dashboard reverts to the rich view +
            # primes a resume).
            from core.remote import remote_pty
            remote_pty.feed_exit(
                machine_id, msg.get("session_id", ""), msg.get("code"),
            )

        elif msg_type == "pty_alive":
            # Right after (re)auth the satellite reports the session ids
            # of its still-live interactive PTYs (empty on a fresh connect).
            # Reconcile against our grace-held handles — re-adopt the survivors,
            # exit the ones whose child died, and close the orphans we no longer
            # want (kills the reverse-orphan, also covers a proxy restart).
            await self._reconcile_ptys(
                machine_id, msg.get("session_ids", []) or [],
            )

        elif msg_type == "sessions_alive":
            # Headless twin of pty_alive: live CLI sessions + in-flight turn
            # state. Routed to the run-recovery callback (registered at
            # startup) so a restarted proxy re-adopts running turns instead
            # of failing them blind (Mode C). Absent callback → old behavior.
            cb = self._sessions_alive_callback
            if cb is not None:
                asyncio.create_task(
                    cb(machine_id, msg.get("sessions") or []),
                )

        elif msg_type == "transcript_lines":
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
                _isess.feed_transcript_lines(msg.get("lines") or [])
            elif _isess is not None:
                logger.warning(
                    "Dropping transcript_lines for session %s from machine %s "
                    "(owned by %s)",
                    (msg.get("session_id") or "")[:8], machine_id[:8],
                    (_isess.target or "")[:8],
                )

        elif msg_type == "pty_inject_result":
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
                    (msg.get("session_id") or "")[:8], machine_id[:8],
                    (_isess.target or "")[:8],
                )

        elif msg_type == "session_started":
            session_id = msg.get("session_id", "")
            logger.info(
                "Satellite %s: session %s started (pid=%s, path=%s)",
                machine_id[:8], session_id[:8],
                msg.get("pid"), msg.get("execution_path"),
            )

        elif msg_type == "session_ended":
            session_id = msg.get("session_id", "")
            conn = self._connections.get(machine_id)
            if conn:
                queue = conn.session_queues.get(session_id)
                if queue:
                    queue.put_nowait(None)  # sentinel
            logger.info(
                "Satellite %s: session %s ended (exit=%s)",
                machine_id[:8], session_id[:8], msg.get("exit_code"),
            )

        elif msg_type == "session_aborted":
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
                machine_id[:8], session_id[:8],
                (f"drained {drained} stale events" if armed
                 else "graceful — queue left to the live producer"),
            )

        elif msg_type == "turn_ended":
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
                        })
                    except asyncio.QueueFull:
                        pass

        elif msg_type == "codex_thread_id":
            session_id = msg.get("session_id", "")
            thread_id = msg.get("thread_id", "")
            if session_id and thread_id:
                conn = self._connections.get(machine_id)
                if conn:
                    queue = conn.session_queues.get(session_id)
                    if queue:
                        queue.put_nowait({
                            "type": "_codex_thread_id",
                            "thread_id": thread_id,
                        })

        elif msg_type == "file_changed":
            # Apply the change to the platform's agent_dir so the dashboard
            # workspace listing reflects what the satellite agent did.
            # Large files (>1 MB) are sent without `content_b64` — the
            # satellite already wrote them; we issue a pull to fetch the body.
            asyncio.create_task(self._apply_file_changed(machine_id, msg))

        elif msg_type == "file_manifest":
            command_id = msg.get("command_id", "")
            entry = self._pending_acks.get(command_id)
            if entry:
                _, future = entry
                if not future.done():
                    future.set_result(msg)

        elif msg_type == "file_content":
            request_id = msg.get("request_id", "")
            st = self._pending_pulls.get(request_id)
            if st is not None:
                self._on_pull_chunk(st, msg)

        elif msg_type == "mcp_install_progress":
            # Satellite streaming install progress during a sync_mcps batch.
            # Delivered to any registered per-command callback (mcp_sync
            # keeps one keyed by command_id) so the pump can surface these
            # as SYSTEM CommonEvents with `subtype="mcp_installation_progress"`.
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

        elif msg_type == "http_request":
            # HTTP-over-WS tunnel: subprocess on satellite wants to call a
            # platform-internal endpoint. Dispatch via httpx loopback.
            from core.remote.satellite_http_tunnel import get_dispatcher
            await get_dispatcher().handle_request_frame(self, machine_id, msg)

        elif msg_type == "http_request_chunk":
            # Continuation of a streamed request body for an open tunnel
            # stream. Synchronous queue put (no upstream call).
            from core.remote.satellite_http_tunnel import get_dispatcher
            get_dispatcher().handle_request_chunk(machine_id, msg)

        elif msg_type == "local_session_open":
            # otodock-CLI: a satellite-initiated request to open an interactive
            # session (the user ran `otodock` on the machine). Identity is
            # re-derived from THIS machine's owner binding — never the frame.
            asyncio.create_task(self._handle_local_session_open(machine_id, msg))

        elif msg_type == "local_session_list":
            # otodock-CLI --resume: list the owner's resumable chats for an agent.
            asyncio.create_task(self._handle_local_session_list(machine_id, msg))

        elif msg_type == "local_session_detached":
            # The local `otodock` terminal disconnected — drop the idle-reaper
            # exemption (the PTY stays alive; the chat is dashboard-resumable).
            from core.session import otodock_session
            otodock_session.detach_local_session(msg.get("session_id", ""))

    # --- Heartbeat monitor ---

    async def heartbeat_monitor(self) -> None:
        """Background task: detect stale connections + drive admin alerts.

        Each 30s tick:
        - 90s no heartbeat  -> DB status='disconnected'
        - 5min no heartbeat -> kill all sessions and close the WS
        - reconcile admin offline/online notifications against sustained
          reachability (see ``_evaluate_admin_machine_alerts``).
        """
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            stale: list[str] = []

            for machine_id, conn in list(self._connections.items()):
                elapsed = now - conn.last_heartbeat
                if elapsed > 300:
                    stale.append(machine_id)
                elif elapsed > 90:
                    from storage import remote_store
                    remote_store.update_machine_status(
                        machine_id, "disconnected"
                    )

            for machine_id in stale:
                logger.warning(
                    "Satellite %s: no heartbeat for 5min, disconnecting",
                    machine_id[:8],
                )
                conn = self._connections.get(machine_id)
                if conn:
                    try:
                        await conn.ws.close(
                            code=4003, reason="Heartbeat timeout"
                        )
                    except Exception:
                        pass
                await self.deregister(machine_id)

            # Reconcile sustained-outage admin notifications. Isolated in a
            # try/except so a transient DB hiccup can't kill the monitor.
            try:
                await self._evaluate_admin_machine_alerts()
            except Exception:
                logger.exception("admin machine-alert evaluation failed")


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


# Module-level singleton (initialized in app.py lifespan)
_connection_manager: SatelliteConnectionManager | None = None


def get_connection_manager() -> SatelliteConnectionManager:
    """Return the singleton connection manager."""
    global _connection_manager
    if _connection_manager is None:
        _connection_manager = SatelliteConnectionManager()
    return _connection_manager
