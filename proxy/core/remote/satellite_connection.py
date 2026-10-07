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
from core import placement
from dataclasses import dataclass, field

from fastapi import WebSocket
from auth import roles

logger = logging.getLogger("claude-proxy.satellite")


# Send queue capacity per connection. Matches satellite-side queue size so
# back-pressure behavior is symmetric. Drops oldest on overflow.
_SEND_QUEUE_SIZE = 10_000

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
        self._tickets: "deque[tuple[int, asyncio.Future]]" = deque()
        self._failed: CreditFailed | None = None
        self._acked: "deque[tuple[float, int]]" = deque()
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

# Terminal message persisted when the grace window lapses without a reconnect
# — a visible marker beats a silent truncation that looks
# like a clean finish.

# The engines a satellite that reports no ``engines`` capability can run:
# every satellite released before the field existed dispatches exactly these
# two, so an ABSENT field must read as the pair — never as empty, or the whole
# fielded fleet would stop spawning. The one place the pair is spelled on the
# proxy; a newer satellite's own list replaces it.
_LEGACY_SATELLITE_ENGINES = frozenset({"claude-code-cli", "codex-cli"})
_ENGINES_MAX = 16          # a bounded list of short ids — the field is satellite-supplied
_ENGINE_ID_MAX_LEN = 64

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

# Minimum SATELLITE_VERSION that handles the headless-Codex thread ops
# (`codex_compact` / `codex_steer`). Older satellites would silently drop the
# frame and the ack would only burn its timeout — gate BEFORE sending so the
# caller degrades instantly (compact → "not supported", steer → queue).
_CODEX_THREAD_OPS_MIN_VERSION = (0, 5, 98)

# Sync-ignore rule tables (auth_result policy ``sync_ignore_rules``): the
# satellite validates + applies the table to every sync walk from 0.5.110.
# The proxy's per-machine manifest walk applies the SAME table only for
# supporting machines (effective_ignore_rules below) — asymmetric walks
# would misattribute whole excluded trees as deletes.
_SYNC_IGNORE_RULES_MIN_VERSION = (0, 5, 110)
_PAGED_MANIFEST_MIN_VERSION = (0, 5, 130)

# Minimum SATELLITE_VERSION that honours the config-driven sync file cap
# delivered in the auth_result policy handshake (``sync_max_file_bytes``).
# Older satellites hard-reject inbound files above the legacy 100MB in
# apply_file_push — the proxy must never send them one (effective_sync_cap).
_CONFIG_SYNC_CAP_MIN_VERSION = (0, 5, 103)

# Minimum SATELLITE_VERSION that handles `codex_bg_terminals` (the remote
# bg-command drain's pull of `thread/backgroundTerminals/list`). Older
# satellites would silently drop the frame and every drain poll would burn
# its ack timeout — gate BEFORE sending so the drain stays a no-op there
# (the router's OOB hook still resolves live completions; only the
# loss-window backstop is missing).
_CODEX_BG_TERMINALS_MIN_VERSION = (0, 5, 105)

# Minimum SATELLITE_VERSION that writes the ``local_model_provider`` start-payload
# field into its Codex config.toml (the ``[model_providers.oto_local]`` block for
# a local OpenAI-compatible endpoint). An older satellite would run the local
# model name against Codex's built-in OpenAI provider, so the remote layer
# refuses a local-endpoint Codex spawn below this instead of forwarding it.
_LOCAL_MODEL_PROVIDER_MIN_VERSION = (0, 5, 116)

# Minimum SATELLITE_VERSION whose Codex writers honour the ``stream_idle_timeout_ms``
# and ``catalog_json`` fields of ``local_model_provider`` (the per-session model
# catalog that makes Codex defer its MCP tools) and whose warm gate waits for
# every MCP server. Additive: an older satellite ignores the fields and runs a
# local-model session the 0.5.116 way (every tool on every request, Codex's
# 5-minute idle timeout), so this only decides whether the proxy sends them and
# warns — the hard gate above stays at 0.5.116 and auto-update brings the
# satellite to 0.5.117 at its next reconnect.
_LOCAL_MODEL_CATALOG_MIN_VERSION = (0, 5, 117)

# Minimum SATELLITE_VERSION whose Codex app-server sessions honour the
# ``codex_hooks_floor`` start-payload field: ``[features] hooks = true``,
# thread-level ``bypass_hook_trust`` and the deny-only / no-forward hook env,
# so an UNATTENDED remote Codex session (a kind ``session_kind.attended()``
# refuses) runs ``permission_gate.py`` as its PreToolUse hook exactly like
# the local layer does. Additive: an older satellite ignores the field and
# gates by the prompt rules only (today's behaviour), so the proxy only warns
# and auto-update brings the satellite here at its next reconnect.
_CODEX_HOOKS_FLOOR_MIN_VERSION = (0, 5, 118)
# The satellite's post-turn stdout forwarder keeps running up to the
# background-work ceiling instead of stopping after ten minutes, so a
# background command's completion frame reaches the proxy however long the
# command takes; the remote reaper's background-command leash relies on it.
_LONG_BG_DRAIN_MIN_VERSION = (0, 5, 120)
# Minimum SATELLITE_VERSION with hook parity (proxy docs/architecture/HOOKS.md):
# the Claude writer names the ``Stop`` hook, the Codex writer names ``Stop``
# and ``SubagentStop``, the Claude PTY spawn sets ``OTO_INTERACTIVE``, and
# the tunnel allowlist admits ``/v1/hooks/stop`` and
# ``/v1/hooks/codex-question``. Additive: an older satellite ignores the
# shipped ``stop_tracker.py``, delivers no Stop signal (the hook's POST fails
# silently) and answers a remote Codex question empty; the proxy warns once
# per session start and auto-update brings the satellite here.
_HOOK_PARITY_MIN_VERSION = (0, 5, 121)
# Minimum SATELLITE_VERSION that runs app steps (proxy APPS.md "Steps"):
# the ``step_run`` frame, the private steps directory with the hash check,
# ``step_output`` streaming, the tunnel allowlist line for
# ``/v1/apps/<id>/actions/<id>``. NOT additive: an older satellite drops the
# frame silently, so the proxy never sends it — the delivery dies with
# "this machine's satellite is too old for steps" until the satellite
# updates at its next reconnect.
_STEPS_MIN_VERSION = (0, 5, 122)
# Minimum SATELLITE_VERSION for checks on a machine (proxy CHECKS.md): the
# ``step_run`` frame's ``payload_env`` and ``cwd_absolute`` fields, and a
# Codex judge in the default collaboration mode (plan iff the platform
# mode is plan). Additive: below it a check script reads the step's payload
# name, a check outside the synced tree is an ``error`` verdict, and a Codex
# judge gets the ordinary sandbox with the gate alone enforcing.
_CHECKS_MIN_VERSION = (0, 5, 123)
# The satellite-local credential gateway (CREDENTIALS.md "The credential
# gateway"): the machine holds a session's vendor tokens in memory and adds
# them on the way out; below it a vendor entry keeps the inline shape.
_MCP_GATEWAY_MIN_VERSION = (0, 5, 132)
# Paced transfers (0.5.137): the satellite's pulls ride a bounded bulk lane
# purged on a drop, and ``file_pull_cancel`` stops one the proxy gave up on.
_PACED_TRANSFERS_MIN_VERSION = (0, 5, 137)
# Minimum SATELLITE_VERSION that handles `steer_turn` (a user frame written
# into a running headless Claude CLI turn's stdin — the satellite twin of
# PersistentSession.steer). NOT additive: an older satellite drops the frame
# silently and the ack would only burn its timeout, so the Claude remote
# adapter refuses before sending and the caller keeps its queue fallback
# (stop-and-send).
_STEER_TURN_MIN_VERSION = (0, 5, 128)
# Reads Codex's ``willRetry`` at the error notification's top level, so a
# retried error no longer ends its Codex turn (SATELLITE_VERSION 0.5.131).
_CODEX_RETRY_READ_MIN_VERSION = (0, 5, 131)

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
_PERSIST_LANE_ORDER = ("status", "caps", "paused", "update_result")


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
    capabilities: dict = field(default_factory=dict)
    # Reported SATELLITE_VERSION from the auth message. Drives the feature
    # gates (``satellite_supports_*``) for frames an older satellite would
    # silently drop.
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
    # multi-chunk file push delays a command ack by at most one chunk. The
    # protocol's ping and pong bypass both lanes and queue behind every byte
    # already handed to the transport: ``bulk_credit`` bounds those bytes.
    # ``send_wakeup`` is set by enqueue_send to wake the writer
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
    # The file bytes in flight on this connection (push_file) and the time
    # of its latest inbound transfer frame (an ack of a bulk frame or any
    # file_content, monotonic): a transfer waiting behind another one on a
    # moving link is not stalled, and a satellite below 0.5.137 whose
    # heartbeat queues behind a pull is still alive.
    bulk_credit: _BulkCredit = field(default_factory=_BulkCredit)
    last_transfer_at: float = 0.0

    async def enqueue_send(self, msg: dict, *, bulk: bool = False) -> None:
        """Queue a message for the writer task. Drops oldest on overflow.

        ``bulk=True`` routes the message to the BULK lane (file_push data
        chunks) instead of the CONTROL lane; the writer always drains control
        first, so a large transfer can never delay command acks. Safe to call
        from any coroutine. Returns immediately — the actual ws.send happens
        inside `_writer_loop`.
        """
        self.enqueue_send_nowait(msg, bulk=bulk)

    def enqueue_send_nowait(self, msg: dict, *, bulk: bool = False) -> None:
        """``enqueue_send`` for a caller that cannot await (a cleanup path)."""
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
    return [u["sub"] for u in task_store.list_users() if roles.is_admin(u.get("role"))]


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

    from storage.pg import run_db

    machine = await run_db(remote_store.get_remote_machine, machine_id)
    if not placement.machine_is_admin_paired(machine):
        return

    machine_name = machine.get("name") or machine_id[:8]
    admin_subs = await run_db(_list_admin_subs)
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
from core.remote.satellite_file_transfer import (  # noqa: F401
    SatelliteFileTransferMixin, _FileChangedLane, _PullStream, evict_file_changed_stamps,
)


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
        # Per-command install progress callbacks. mcp_sync registers one
        # before issuing sync_mcps and removes it after the ack arrives.
        self._install_progress_cbs: dict[str, object] = {}
        # Per-command step output callbacks (APPS.md "Steps"): app_steps
        # registers one around its step_run send_command so the script's
        # lines reach the app log as they arrive.
        self._step_output_cbs: dict[str, object] = {}
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

    # --- Off-loop persistence (status / caps / paused / update_result) ---

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
                name=f"sat-persist-{machine_id[:8]}",
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
                    except Exception as e:
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
            "next request", machine_id[:8], lane, type(err).__name__, err,
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

    async def _refresh_machine_cache(self, machine_id: str, conn) -> None:
        """Read the admin ``max_sessions`` override + machine name off-loop
        and cache them on the connection (only while it is still the live
        one). Fail-open on any error: the caches stay None/"" and the
        capacity pre-check falls back to the satellite's own recommendation
        exactly as before."""
        from storage import remote_store
        from storage.pg import run_db
        try:
            m = await run_db(remote_store.get_remote_machine, machine_id)
        except Exception:
            logger.debug("machine cache refresh failed for %s", machine_id[:8], exc_info=True)
            return
        if not m or self._connections.get(machine_id) is not conn:
            return
        raw = m.get("max_sessions")
        try:
            conn.max_sessions = int(raw) if raw else None
        except (TypeError, ValueError):
            conn.max_sessions = None
        conn.name = m.get("name") or ""

    def note_max_sessions(self, machine_id: str, value: int | None) -> None:
        """Admin endpoint hook: refresh the cached override for a connected
        machine. Loop-thread only (callers on a worker thread use
        ``loop.call_soon_threadsafe``)."""
        conn = self._connections.get(machine_id)
        if conn is not None:
            conn.max_sessions = int(value) if value else None

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
                token = msg.pop("_xfer", None)
                if token is not None and token.cancelled:
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
                with contextlib.suppress(Exception):
                    await old.ws.close(code=4002, reason="Replaced by new connection")
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
                # What waits on the old socket can never be answered: no await
                # between this and the swap, so nothing lands on it unseen.
                self._reject_connection_waiters(old, f"Satellite {machine_id[:8]} replaced by a new connection")
            self._connections[machine_id] = conn
            self._reset_file_changed_lane(machine_id)

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
                "payload (%s bytes) — not persisted", machine_id[:8], caps_size,
            )
        logger.info(
            "Satellite %s connected (capabilities: %s)",
            machine_id[:8], list(capabilities.get("installed_clis", [])),
        )

        # Cache the admin max_sessions override + name off-loop (fail-open
        # until it lands — same as an unknown cap today).
        asyncio.create_task(self._refresh_machine_cache(machine_id, conn))

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
                            RuntimeError(f"Satellite {machine_id[:8]} disconnected")
                        )
            credit = getattr(conn, "bulk_credit", None)
            if credit is not None:
                credit.fail_all(f"Satellite {machine_id[:8]} disconnected")

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
                            RuntimeError(f"Satellite {machine_id[:8]} disconnected")
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
        cap = getattr(conn, "max_sessions", None)
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
            out.append({
                "machine_id": mid,
                "name": getattr(conn, "name", "") or mid[:8],
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

    def satellite_engines(self, machine_id: str) -> frozenset[str]:
        """The engine ids (``execution_path`` values) the connected satellite's
        CODE can dispatch — its advertised ``engines`` capability, or the
        legacy pair when the field is absent or malformed (every satellite
        released before the field ran exactly those two). Binary presence is
        a different fact (``installed_clis`` / ``cli_status``): the pin
        reconcile may install a pinned binary after auth, so a spawn is never
        gated on it. Empty for a machine that is not connected."""
        conn = self._connections.get(machine_id)
        if not conn:
            return frozenset()
        raw = conn.capabilities.get("engines")
        if not isinstance(raw, list):
            return _LEGACY_SATELLITE_ENGINES
        engines = {
            e for e in raw[:_ENGINES_MAX]
            if isinstance(e, str) and 0 < len(e) <= _ENGINE_ID_MAX_LEN
        }
        return frozenset(engines) if engines else _LEGACY_SATELLITE_ENGINES

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

    def supports_codex_thread_ops(self, machine_id: str) -> bool:
        """True if the connected satellite handles ``codex_compact`` /
        ``codex_steer`` (SATELLITE_VERSION 0.5.98). When False the remote
        layer returns the unsupported result immediately instead of burning
        an ack timeout on a silently-dropped frame."""
        return self._satellite_at_least(machine_id, _CODEX_THREAD_OPS_MIN_VERSION)

    def satellite_supports_config_sync_cap(self, machine_id: str) -> bool:
        """True if the connected satellite honours the handshake-delivered
        sync file cap (SATELLITE_VERSION 0.5.103)."""
        return self._satellite_at_least(machine_id, _CONFIG_SYNC_CAP_MIN_VERSION)

    def supports_codex_bg_terminals(self, machine_id: str) -> bool:
        """True if the connected satellite handles ``codex_bg_terminals``
        (SATELLITE_VERSION 0.5.105). When False the remote bg-command drain
        stays a no-op instead of burning an ack timeout on a silently-dropped
        frame — the router's OOB hook covers live completions either way."""
        return self._satellite_at_least(machine_id, _CODEX_BG_TERMINALS_MIN_VERSION)

    def satellite_supports_local_model_provider(self, machine_id: str) -> bool:
        """True if the connected satellite writes the ``local_model_provider``
        start-payload field into its Codex config.toml (SATELLITE_VERSION
        0.5.116). When False the remote layer refuses a local-endpoint Codex
        spawn, naming the satellite's version, rather than letting the local
        model name run against Codex's built-in OpenAI provider."""
        return self._satellite_at_least(machine_id, _LOCAL_MODEL_PROVIDER_MIN_VERSION)

    def satellite_supports_local_model_catalog(self, machine_id: str) -> bool:
        """True if the connected satellite writes ``local_model_provider``'s
        ``stream_idle_timeout_ms`` and ``catalog_json`` (SATELLITE_VERSION
        0.5.117). When False the remote layer sends the provider without them
        and logs it — the session still runs, the 0.5.116 way."""
        return self._satellite_at_least(machine_id, _LOCAL_MODEL_CATALOG_MIN_VERSION)

    def satellite_supports_codex_hooks_floor(self, machine_id: str) -> bool:
        """True if the connected satellite runs the PreToolUse permission
        floor for unattended Codex app-server sessions when the start payload
        carries ``codex_hooks_floor`` (SATELLITE_VERSION 0.5.118). When False
        the remote layer still sends the field and logs that the session
        runs prompt-gated until the satellite updates."""
        return self._satellite_at_least(machine_id, _CODEX_HOOKS_FLOOR_MIN_VERSION)

    def satellite_supports_hook_parity(self, machine_id: str) -> bool:
        """True if the connected satellite carries the full hook set for
        both engines (SATELLITE_VERSION 0.5.121): the Stop hook, the Codex
        SubagentStop hook, the interactive flag on the Claude PTY and the
        two extra tunnel paths. When False the session still runs; it sends
        no Stop signal and a remote Codex question gets empty answers."""
        return self._satellite_at_least(machine_id, _HOOK_PARITY_MIN_VERSION)

    def satellite_supports_steps(self, machine_id: str) -> bool:
        """True if the connected satellite runs app steps (SATELLITE_VERSION
        0.5.122 and the ``steps`` capability in its handshake). When False
        the proxy never sends ``step_run`` — the frame would be dropped
        silently — and the delivery dies with a reason naming the version."""
        conn = self._connections.get(machine_id)
        if not conn or not (conn.capabilities or {}).get("steps"):
            return False
        return self._satellite_at_least(machine_id, _STEPS_MIN_VERSION)

    def satellite_supports_mcp_gateway(self, machine_id: str) -> bool:
        """True if the connected satellite runs the credential gateway for
        this session's vendor MCPs (SATELLITE_VERSION 0.5.132 and the
        ``mcp_gateway`` capability in its handshake). When False a vendor
        entry keeps today's inline shape until the machine updates."""
        conn = self._connections.get(machine_id)
        if not conn or not (conn.capabilities or {}).get("mcp_gateway"):
            return False
        return self._satellite_at_least(machine_id, _MCP_GATEWAY_MIN_VERSION)

    def satellite_supports_paced_transfers(self, machine_id: str) -> bool:
        """True if the connected satellite paces its pulls and handles
        ``file_pull_cancel`` (SATELLITE_VERSION 0.5.137 and the
        ``paced_transfers`` capability in its handshake). When False the
        proxy never sends the cancel: an older satellite would drop it."""
        conn = self._connections.get(machine_id)
        if not conn or not (getattr(conn, "capabilities", None) or {}).get("paced_transfers"):
            return False
        return self._satellite_at_least(machine_id, _PACED_TRANSFERS_MIN_VERSION)

    def satellite_supports_checks(self, machine_id: str) -> bool:
        """True if the connected satellite carries the checks additions
        (SATELLITE_VERSION 0.5.123): the two ``step_run`` fields a check
        script needs and the Codex collaboration mode from the platform
        mode. Everything degrades below it (CHECKS.md "Satellite 0.5.123")."""
        return self._satellite_at_least(machine_id, _CHECKS_MIN_VERSION)

    def satellite_supports_steer_turn(self, machine_id: str) -> bool:
        """True if the connected satellite handles ``steer_turn`` (a user
        frame into a running headless Claude CLI turn, SATELLITE_VERSION
        0.5.128). When False the Claude remote adapter refuses the steer
        before sending and the caller queues the message as before."""
        return self._satellite_at_least(machine_id, _STEER_TURN_MIN_VERSION)

    def satellite_supports_codex_retry_read(self, machine_id: str) -> bool:
        """True if the connected satellite keeps a Codex turn running through
        an error Codex retries (SATELLITE_VERSION 0.5.131). When False its
        turn ends on that notification, so the proxy keeps the ERROR for it
        instead of letting the turn end silently."""
        return self._satellite_at_least(machine_id, _CODEX_RETRY_READ_MIN_VERSION)

    def satellite_supports_long_bg_drain(self, machine_id: str) -> bool:
        """True if the connected satellite forwards post-turn stdout up to
        the background-work ceiling (SATELLITE_VERSION 0.5.120). When False
        the remote reaper keeps today's behaviour for background commands
        (subagents are still spared: their Stop hook is not stdout)."""
        return self._satellite_at_least(machine_id, _LONG_BG_DRAIN_MIN_VERSION)

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
        return name or machine_id[:8]

    def effective_sync_cap(self, machine_id: str) -> int:
        """Largest file (bytes) the proxy may send this satellite: the
        config-driven cap for 0.5.103+ machines, else the legacy 100MB their
        ``apply_file_push`` hard-rejects above. Used by ``push_file``'s send
        gate and as ``compute_manifest(max_file_size=...)`` so an old
        machine's merge plan never contains a file it would reject."""
        from core.remote.file_sync import LEGACY_SYNC_MAX_FILE_BYTES, MAX_FILE_SIZE
        if self.satellite_supports_config_sync_cap(machine_id):
            return MAX_FILE_SIZE
        return min(MAX_FILE_SIZE, LEGACY_SYNC_MAX_FILE_BYTES)

    def satellite_supports_sync_ignore_rules(self, machine_id: str) -> bool:
        """True if the connected satellite validates + applies the
        ``sync_ignore_rules`` policy table (SATELLITE_VERSION 0.5.110)."""
        return self._satellite_at_least(machine_id, _SYNC_IGNORE_RULES_MIN_VERSION)

    def effective_ignore_rules(self, machine_id: str) -> dict | None:
        """The VALIDATED sync-ignore rule table to use for THIS machine's
        manifest walk, or None (legacy walk). Rules apply only when the
        satellite ACKed support — an old satellite walks legacy-style, so
        the proxy must too or the two manifests disagree and the diff
        misattributes whole build trees as deletes."""
        if not self.satellite_supports_sync_ignore_rules(machine_id):
            return None
        import config as app_config
        from core.remote.file_sync import validate_ignore_rules
        return validate_ignore_rules(app_config.SYNC_IGNORE_RULES)

    # A satellite from this version answers ``request_manifest`` in pages
    # when asked; the joined reply is bounded to this many entries and this
    # much serialized text (a page is about 2 MiB, a frame at most the
    # socket's cap).
    _MANIFEST_MAX_ENTRIES = 500_000
    _MANIFEST_MAX_BYTES = 256 * 1024 * 1024

    def satellite_supports_paged_manifest(self, machine_id: str) -> bool:
        """True if the connected satellite pages a manifest reply on request
        (SATELLITE_VERSION 0.5.130); an older one answers in one frame and
        must not be asked (it would ignore the key anyway)."""
        return self._satellite_at_least(machine_id, _PAGED_MANIFEST_MIN_VERSION)

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
            future.set_exception(RuntimeError(f"manifest from {machine_id[:8]} {why}"))

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

    def _satellite_at_least(self, machine_id: str, version: tuple) -> bool:
        conn = self._connections.get(machine_id)
        sat_version = getattr(conn, "satellite_version", "") if conn else ""
        if not sat_version:
            return False
        try:
            parts = tuple(int(p) for p in sat_version.split(".")[:3])
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
        self._pending_acks[command_id] = (machine_id, future, conn)
        # A paged manifest's pages end with the wait, however it ends.
        future.add_done_callback(lambda _f, _cid=command_id: self._manifest_pages.pop(_cid, None))

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
            self._manifest_pages.pop(command_id, None)

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
            raise RuntimeError(f"Satellite {machine_id[:8]} not connected")
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

    async def handle_message(self, machine_id: str, msg: dict) -> None:
        """Route an incoming message from a satellite."""
        msg_type = msg.get("type", "")

        if msg_type == "ack":
            command_id = msg.get("command_id", "")
            entry = self._pending_acks.get(command_id)
            if entry:
                future = entry[1]
                if not future.done():
                    future.set_result(msg)

        elif msg_type == "pause":
            # Deliberate pause (tray Pause). Persist so the sustained-outage
            # evaluator skips this machine (no false admin alert). The
            # satellite closes the WS right after this; the next successful
            # auth (tray Resume / reboot) clears the flag — through the same
            # lane, so the two writes keep their order.
            self._request_persist(machine_id, "paused", True)

        elif msg_type == "cli_status":
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

        elif msg_type == "heartbeat":
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
                _active = msg.get("active_sessions")
                if isinstance(_active, int):
                    conn.reported_sessions = _active
                # ``online`` + ``last_seen`` → the persister, coalesced to one
                # write per machine per HEARTBEAT_PERSIST_INTERVAL_S (or at
                # once on a transition). NEVER a synchronous commit here: this
                # branch runs every 20 s per satellite on the event loop.
                self._maybe_persist_heartbeat(conn)
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
            # state, and (0.5.138) every other headless session the satellite
            # holds (``other_sessions``; None when an older satellite sends no
            # such list). Routed to the run-recovery callback (registered at
            # startup) so a restarted proxy re-adopts running turns instead
            # of failing them blind (Mode C). Absent callback → old behavior.
            cb = self._sessions_alive_callback
            if cb is not None:
                others = msg.get("other_sessions")
                asyncio.create_task(
                    cb(machine_id, msg.get("sessions") or [],
                       others if isinstance(others, list) else None),
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
                        # Dropping the marker stalls the turn until the drain
                        # timeout — leave a trace so it isn't a silent hang.
                        logger.warning(
                            "turn_ended marker dropped (queue full) for "
                            "session=%s machine=%s", session_id, machine_id,
                        )

        elif msg_type == "codex_thread_id":
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

        elif msg_type == "file_changed":
            # Apply the change to the platform's agent_dir so the dashboard
            # workspace listing reflects what the satellite agent did.
            # Large files (>1 MB) are sent without `content_b64` — the
            # satellite already wrote them; we issue a pull to fetch the body.
            # Admitted, bounded and shed in the mixin; never one task per
            # frame with nothing above it.
            self._dispatch_file_changed(machine_id, msg)

        elif msg_type == "file_manifest":
            self._on_manifest_frame(machine_id, msg)

        elif msg_type == "file_content":
            conn = self._connections.get(machine_id)
            if conn is not None:
                conn.last_transfer_at = time.monotonic()
            request_id = msg.get("request_id", "")
            st = self._pending_pulls.get(request_id)
            if st is not None:
                self._on_pull_chunk(st, msg)

        elif msg_type == "mcp_install_progress":
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

        elif msg_type == "step_output":
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

        elif msg_type == "http_abort":
            # 0.5.115+: the satellite's local client (hook script / MCP
            # client) went away mid-stream — close our upstream now instead
            # of holding it until the idle sweep.
            from core.remote.satellite_http_tunnel import get_dispatcher
            get_dispatcher().abort_stream(machine_id, str(msg.get("stream_id", "")))

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
        - 90s no heartbeat (nor transfer frame) -> DB status='disconnected'
        - 5min no heartbeat (nor transfer frame) -> kill all sessions and close the WS
        - reconcile admin offline/online notifications against sustained
          reachability (see ``_evaluate_admin_machine_alerts``).
        """
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            stale: list[str] = []

            from storage import remote_store
            for machine_id, conn in list(self._connections.items()):
                elapsed = now - _last_contact(conn)
                if elapsed > 300:
                    stale.append(machine_id)
                elif elapsed > 90:
                    # Silent-but-open socket: mark it down (off-loop, once —
                    # the next heartbeat flips it back to online at once via
                    # the transition rule) with the exact last contact so the
                    # admin offline-alert grace clock stays honest.
                    if getattr(conn, "persisted_status", "") != remote_store.STATUS_DISCONNECTED:
                        self._persist_status(
                            conn, remote_store.STATUS_DISCONNECTED,
                            getattr(conn, "last_seen_iso", "") or None,
                        )

            for machine_id in stale:
                logger.warning(
                    "Satellite %s: no heartbeat for 5min, disconnecting",
                    machine_id[:8],
                )
                conn = self._connections.get(machine_id)
                if conn:
                    with contextlib.suppress(Exception):
                        await conn.ws.close(
                            code=4003, reason="Heartbeat timeout"
                        )
                await self.deregister(machine_id)

            # Reconcile sustained-outage admin notifications. Isolated in a
            # try/except so a transient DB hiccup can't kill the monitor.
            try:
                await self._evaluate_admin_machine_alerts()
            except Exception:
                logger.exception("admin machine-alert evaluation failed")


def _last_contact(conn) -> float:
    """When a connection was last heard from (monotonic): its heartbeat, or a
    transfer frame, since a satellite below 0.5.137 queues a whole pull ahead
    of its heartbeats."""
    return max(conn.last_heartbeat, getattr(conn, "last_transfer_at", 0.0))


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
