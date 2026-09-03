"""WebSocket client for satellite daemon.

Connects to the platform, authenticates, manages message loop with
auto-reconnect, writer-task send queue (coroutine-safe ws.send), TCP
keepalive, and clock-jump detection (laptop sleep/wake recovery).
"""

import asyncio
import contextlib
import ipaddress
import json
import logging
import random
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosed

from ..config import otodock_dir
from .lifecycle_update import (
    _finalize_post_update_state,
    _self_uninstall_and_exit,
    _self_update_and_restart,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ..config import SatelliteConfig
    from ..sessions.session_manager import SessionManager

logger = logging.getLogger("satellite")


def _classify_host(host: str) -> str:
    """Classify a host as 'local' (loopback / RFC1918 / link-local), 'public',
    or 'unknown' (unresolvable). A hostname is resolved and judged by ALL of its
    addresses — any public address makes it public."""
    if not host:
        return "unknown"
    if host == "localhost":
        return "local"
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            ips = [ipaddress.ip_address(i[4][0]) for i in socket.getaddrinfo(host, None)]
        except (OSError, ValueError):
            return "unknown"
    if not ips:
        return "unknown"
    if all(ip.is_loopback or ip.is_private or ip.is_link_local for ip in ips):
        return "local"
    return "public"


class InsecureTransportRefused(RuntimeError):
    """platform_url is plaintext ws:// to a publicly-resolving host and the
    operator has not opted in via ``allow_insecure_transport = true``."""


def assert_transport_secure(url: str, *, allow_insecure: bool = False) -> None:
    """Refuse plaintext ``ws://`` to a publicly-resolving host unless the
    operator explicitly opted in.

    ``wss://`` is silent (the websockets library validates the server cert
    against the system CA bundle by default). Plain ``ws://`` to a loopback /
    LAN host is a mild note. ``ws://`` to a host that resolves PUBLIC is
    **refused** (``InsecureTransportRefused``): this channel carries the
    machine secret AND executable code updates, so a MITM on it is full code
    execution on the satellite — a log line on a headless daemon is not a
    control. DNS classification can misfire (split-horizon DNS, a DDNS name
    used on the LAN, a VPN/overlay where the name resolves publicly but is
    reached privately), so those setups set ``allow_insecure_transport =
    true`` in ``satellite.conf`` once — with the opt-in the old error-level
    warning is logged and the connection proceeds.
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme == "wss":
        return
    host = parsed.hostname or "?"
    if scheme != "ws":
        logger.warning(
            "platform_url scheme is %r (expected wss://); the connection may fail.",
            scheme,
        )
        return
    if _classify_host(host) == "public":
        if not allow_insecure:
            raise InsecureTransportRefused(
                f"platform_url uses plaintext ws:// to a publicly-resolving "
                f"host ({host}) — the machine secret and code updates would "
                f"travel in the clear on a MITM-able channel. Re-pair with a "
                f"wss:// URL, or — if this host is only ever reached over a "
                f"trusted LAN/VPN path despite resolving publicly — set "
                f"`allow_insecure_transport = true` under [satellite] in "
                f"satellite.conf to opt in."
            )
        logger.error(
            "SECURITY: platform_url uses plaintext ws:// to a publicly-resolving "
            "host (%s) with allow_insecure_transport=true — the machine secret "
            "and session tokens travel in the clear and the channel is "
            "MITM-able. Re-pair with a wss:// URL unless this host is only "
            "ever reached over a trusted (LAN/VPN) path.",
            host,
        )
        return
    logger.warning(
        "platform_url uses plaintext ws:// (host=%s). Acceptable on a trusted LAN "
        "or same host, but use wss:// for anything crossing an untrusted network.",
        host,
    )


# WS ping/timeout values, symmetric with proxy/app.py uvicorn config.
# INTERVAL stays below the 60s nginx/Authentik proxy_read_timeout default so quiet
# sockets stay alive through reverse proxies. TIMEOUT is generous (30s, not 10s) so
# a slow/busy host — e.g. a laptop satellite mid-spawn (codex close + CLI start + a
# large MCP stream) that briefly starves its event loop past a pong — isn't killed
# with "1011 keepalive ping timeout"; a transient stall recovers instead of
# dropping the in-flight turn/warmup.
_WS_PING_INTERVAL = 20
_WS_PING_TIMEOUT = 30

# Heartbeat interval (application-level). Aligned with ping_interval so a
# missed heartbeat is also a missed ping.
_HEARTBEAT_INTERVAL = 20

# How often (seconds) the heartbeat recomputes the per-agent STAT fingerprints.
# Well above the 20s heartbeat — the proxy's idle-sync sweep is itself
# 60s, so recomputing the stat-walk faster is wasted work; the cached value rides
# every heartbeat in between.
_FINGERPRINT_INTERVAL = 60

# Mirror the platform's keep-15 cap on the SATELLITE's copy of each workspace's
# hidden ``.screenshots`` dir. camoufox runs on the platform and the relocation
# pushes each shot here add-only (the dir is excluded from the bidirectional sync,
# so platform deletes never propagate) — without this it would grow unbounded.
_SCREENSHOTS_KEEP = 15


def _sweep_satellite_screenshots(agents_dir: "Path", keep: int) -> None:
    """Trim every ``<agent>/.../workspace/.screenshots`` dir to its newest ``keep``
    files. Targeted globs (not a full tree walk) so it's cheap on a large tree."""
    from pathlib import Path as _P
    base = _P(agents_dir)
    dirs = list(base.glob("*/users/*/workspace/.screenshots")) \
        + list(base.glob("*/workspace/.screenshots"))
    for d in dirs:
        try:
            files = sorted(
                (p for p in d.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
        except OSError:
            continue
        for p in files[keep:]:
            with contextlib.suppress(OSError):
                p.unlink()

# Threshold beyond which a monotonic gap signals system sleep, not a slow
# scheduler. 3× the heartbeat interval gives plenty of slack on busy hosts
# while catching real sleep events.
_CLOCK_JUMP_THRESHOLD = 60

# Reconnect backoff: exponential from BASE, capped at CAP, with ±50% jitter
# applied per attempt. The cap is deliberately low (not 60s) so the satellite
# recovers within ~30s of a proxy restart / brief outage instead of idling up
# to a minute. The jitter de-synchronizes a fleet's reconnects so a recovering
# proxy isn't hit by every satellite in lockstep — lockstep reconnect storms
# can themselves prolong an outage (nginx 502s while the proxy is overwhelmed).
_RECONNECT_BACKOFF_BASE = 1.0
_RECONNECT_BACKOFF_CAP = 30.0

# Send queue capacity. Same as previous _send_buffer cap.
_SEND_QUEUE_SIZE = 10_000

# Capacity of the dedicated LOSSLESS interactive-PTY output lane (frames;
# each ≤ one 64 KB PTY read → ~16 MB max buffered). Unlike _send_queue this lane
# BLOCKS the producer when full (the PTY reader pauses → the TUI's write() blocks)
# rather than dropping the oldest — a dropped byte would corrupt the xterm stream.
_PTY_LANE_SIZE = 256


def _get_system_load() -> dict:
    """Current CPU% + memory% for this host, cross-platform via psutil.

    Replaces the old /proc-only reader (which returned zeros on Windows/macOS).
    ``cpu_percent(interval=None)`` is non-blocking — it diffs against the previous
    call, so the first heartbeat reads ~0% and subsequent ones are accurate, which
    is exactly right for the 20s heartbeat cadence (never blocks the loop)."""
    try:
        import psutil
        return {
            "cpu_pct": round(psutil.cpu_percent(interval=None), 1),
            "mem_pct": round(psutil.virtual_memory().percent, 1),
        }
    except Exception:
        return {"cpu_pct": 0.0, "mem_pct": 0.0}


def _set_keepalive(sock: socket.socket) -> None:
    """Enable TCP_KEEPALIVE on a connected socket.

    Defends against half-open TCP after NAT timeout or laptop sleep. The
    WS-level pings detect liveness when the socket is healthy; this is the
    backstop for cases where the OS reports the socket as alive but the
    underlying TCP connection is dead (NAT entry expired, peer power-off).

    Linux: 60s idle → 30s probe interval × 3 → ~150s detection.
    macOS: 60s idle (no interval/count tunables in the public API).
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):  # Linux
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 30)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        elif hasattr(socket, "TCP_KEEPALIVE"):  # macOS
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 60)
    except OSError as e:
        logger.warning("Failed to set TCP keepalive: %s", e)


class SatelliteWSClient:
    """WebSocket client that connects to the platform and routes messages.

    Uses a writer-task send queue so multiple coroutines can safely enqueue
    messages without racing on ws.send() (which is not coroutine-safe and
    can interleave frames). The queue persists across reconnects, so
    messages enqueued during a disconnect window deliver after auth succeeds.
    """

    def __init__(self, config: "SatelliteConfig", session_manager: "SessionManager"):
        self.config = config
        self.sm = session_manager
        self._ws = None
        self._send_queue: asyncio.Queue = asyncio.Queue(maxsize=_SEND_QUEUE_SIZE)
        # The lossless, backpressured interactive-PTY output lane, drained
        # CONTROL-FIRST by _writer_loop. _send_wakeup lets the writer serve both
        # lanes without blocking on a single queue's get().
        self._pty_send_queue: asyncio.Queue = asyncio.Queue(maxsize=_PTY_LANE_SIZE)
        self._send_wakeup: asyncio.Event = asyncio.Event()
        self._authenticated = False
        # Set by __main__.py post-construction; the WS routes inbound
        # http_response/http_response_chunk frames to this. Optional so
        # tests that don't need the tunnel don't have to construct one.
        self.tunnel = None  # LocalTunnelServer | None
        # Pause gate (tray Pause/Resume). Ephemeral: a reboot clears it.
        self._paused = False
        self._resume_event = asyncio.Event()
        # otodock-CLI: in-flight local_session_open requests (satellite→proxy
        # request/response, keyed by request_id) → resolved by the matching
        # local_session_opened / local_session_error frame in _message_loop.
        self._local_session_futures: dict = {}
        # Optional status sink (the Windows tray). Set via
        # set_status_callback; None = headless, _set_status is a no-op.
        self._status_cb = None  # Callable[[str], None] | None

    def set_status_callback(self, cb) -> None:
        """Register a sink for status transitions (the tray's set_status).

        Called once from __main__ when a tray exists. The callback runs on
        the asyncio loop thread (from _set_status); the tray side is
        responsible for being thread-safe.
        """
        self._status_cb = cb

    def _set_status(self, status: str) -> None:
        """Push a status transition to the tray, if any. Never raises."""
        cb = self._status_cb
        if cb is None:
            return
        try:
            cb(status)
        except Exception:
            logger.debug("status callback failed", exc_info=True)

    async def pause(self) -> None:
        """Disconnect the WS but keep the process running (tray Pause).

        Idempotent. Sends ``{type: "pause"}`` so the proxy records a
        deliberate pause (suppressing the admin offline alert) and then
        closes the live WS so the proxy deregisters promptly. The
        connect_forever loop parks on the resume gate. Pause is ephemeral —
        a reboot / Quit+autostart returns to connecting.
        """
        if self._paused:
            return
        self._paused = True
        self._resume_event.clear()
        self._set_status("paused")
        ws = self._ws
        if ws is not None:
            try:
                await ws.send(json.dumps({"type": "pause"}))
            except Exception as e:
                logger.debug("pause notify send: %s", e)
            with contextlib.suppress(Exception):
                await ws.close(code=1000, reason="paused")

    async def resume(self) -> None:
        """Resume connecting after a pause (tray Resume). Idempotent."""
        if not self._paused:
            return
        self._paused = False
        self._resume_event.set()
        self._set_status("reconnecting")

    async def connect_forever(self) -> None:
        """Reconnect loop with jittered exponential backoff (1s base → 30s cap)."""
        # Refuse plaintext ws:// to a public host (unless the operator opted
        # in via allow_insecure_transport) — checked once, before the loop.
        assert_transport_secure(
            self.config.platform_url,
            allow_insecure=getattr(self.config, "allow_insecure_transport", False),
        )
        backoff = _RECONNECT_BACKOFF_BASE
        while True:
            # Pause gate: park here until resume() (or process restart).
            if self._paused:
                self._set_status("paused")
                await self._resume_event.wait()
                self._resume_event.clear()
                backoff = _RECONNECT_BACKOFF_BASE
            writer_task: asyncio.Task | None = None
            heartbeat_task: asyncio.Task | None = None
            try:
                logger.info(f"Connecting to {self.config.platform_url}...")
                async with websockets.connect(
                    self.config.platform_url,
                    ping_interval=_WS_PING_INTERVAL,
                    ping_timeout=_WS_PING_TIMEOUT,
                    max_size=16 * 1024 * 1024,  # 16MB for file content
                ) as ws:
                    self._ws = ws
                    self._authenticated = False
                    backoff = _RECONNECT_BACKOFF_BASE

                    # Apply TCP keepalive on the underlying socket. The
                    # websockets library exposes the transport's socket via
                    # get_extra_info("socket"). Works for ws:// and wss://.
                    try:
                        sock = ws.transport.get_extra_info("socket")
                        if sock is not None:
                            _set_keepalive(sock)
                    except (AttributeError, OSError) as e:
                        logger.warning("TCP keepalive setup skipped: %s", e)

                    # Direct-send auth BEFORE starting the writer task.
                    # The writer must not run until the platform has accepted
                    # us (otherwise queued messages would race the auth frame).
                    await self._authenticate()
                    logger.info("Authenticated with platform")
                    self._set_status("connected")

                    # Start writer + heartbeat tasks.
                    writer_task = asyncio.create_task(
                        self._writer_loop(), name="ws-writer"
                    )
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat_loop(), name="ws-heartbeat"
                    )

                    # Drive the inbound message dispatcher. Returns on
                    # disconnect or unhandled exception.
                    await self._message_loop()

            except ConnectionClosed as e:
                logger.warning(f"WS connection closed: {e.code} {e.reason}")
                # Close code 4006 (machine_deleted) → run the uninstall script +
                # exit instead of looping forever.
                if e.code == 4006:
                    logger.info(
                        "Machine deleted from platform (4006). "
                        "Running self-uninstall.",
                    )
                    await _self_uninstall_and_exit()
                    return
                # Close code 4007 (update_in_progress) → the
                # proxy just pushed an update_required message and is
                # closing this WS. Our `_self_update_and_restart` task
                # (kicked off by the message handler above) is already
                # extracting + scheduling the systemd restart. Don't
                # immediately reconnect — let systemd respawn us with
                # the new code. If the update task crashes before
                # triggering restart, the next reconnect attempt will
                # just see the same mismatch and re-receive the update.
                if e.code == 4007:
                    logger.info(
                        "Update in progress (4007). "
                        "Awaiting systemd restart with new code."
                    )
                    await asyncio.sleep(5.0)
                    # Fall through to normal reconnect loop in case the
                    # update task failed; the proxy will retry the push.
            except (ConnectionError, OSError) as e:
                logger.warning(f"WS connection error: {e}")
            except Exception:
                logger.exception("Unexpected error in WS loop")
            finally:
                self._ws = None
                self._authenticated = False
                for task in (writer_task, heartbeat_task):
                    if task is not None and not task.done():
                        task.cancel()
                # Drain cancelled tasks to avoid "Task was destroyed but it is
                # pending!" warnings.
                for task in (writer_task, heartbeat_task):
                    if task is None:
                        continue
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                # Fail any in-flight tunneled HTTP streams so subprocess
                # hooks return 502 immediately instead of waiting for the
                # 7-day permission timeout.
                if self.tunnel is not None:
                    try:
                        self.tunnel.fail_all_streams("tunnel-disconnected")
                    except Exception:
                        logger.exception("fail_all_streams failed")
                # Purge the PTY mirror lane (after the writer is torn down so
                # its requeued in-flight frame is included). Frees any reader
                # parked on a full lane within ms of disconnect detection —
                # see enqueue_pty — and prevents a reconnect from replaying a
                # stale mirror burst. The control lane is NOT purged (acks/
                # transcript/pty_exit stay lossless across the drop).
                purged = 0
                while True:
                    try:
                        self._pty_send_queue.get_nowait()
                        purged += 1
                    except asyncio.QueueEmpty:
                        break
                if purged:
                    logger.info("Purged %d buffered pty frame(s) on disconnect", purged)

            # If we were paused (the WS close above was deliberate), skip
            # the backoff sleep and let the top-of-loop gate park us.
            if self._paused:
                continue

            # Jittered sleep: 50–100% of the current backoff ceiling. The
            # jitter de-synchronizes a fleet's reconnects after a shared
            # proxy outage; the low cap keeps recovery snappy.
            delay = backoff * (0.5 + random.random() * 0.5)
            logger.info(f"Reconnecting in {delay:.0f}s...")
            self._set_status("reconnecting")
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, _RECONNECT_BACKOFF_CAP)

    async def enqueue_send(self, msg: dict) -> None:
        """Queue a message for the writer task. Drops oldest on overflow.

        Safe to call from any coroutine. Returns immediately (no await on
        the network). If the queue is full (sustained disconnect with high
        send volume), drops the oldest message and logs a WARNING.
        """
        try:
            self._send_queue.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                dropped = self._send_queue.get_nowait()
                logger.warning(
                    "Send queue full (%d), dropped oldest: %s",
                    _SEND_QUEUE_SIZE,
                    dropped.get("type") if isinstance(dropped, dict) else "?",
                )
            except asyncio.QueueEmpty:
                pass
            try:
                self._send_queue.put_nowait(msg)
            except asyncio.QueueFull:
                logger.error(
                    "Send queue still full after drop; message lost: %s",
                    msg.get("type") if isinstance(msg, dict) else "?",
                )
        # Wake the control-first writer: it serves two lanes via an
        # event rather than blocking on a single queue's get().
        self._send_wakeup.set()

    async def request_local_session(self, args: dict, timeout: float = 180.0) -> dict:
        """otodock-CLI: ask the proxy to open an interactive session for a local
        `otodock` terminal. Sends ``local_session_open`` and awaits the matching
        ``local_session_opened`` / ``local_session_error``. Returns the response
        dict; raises ``TimeoutError`` if the proxy doesn't answer (e.g. mid-restart)
        so the client never hangs forever.

        The timeout is generous (180 s, vs 30 s for the cheap LIST): the FIRST run
        of an agent on a machine SYNCS/INSTALLS its MCP servers before the spawn (a
        pip install over the tunnel can take 30 s+), which a 30 s timeout would cut
        off mid-install — the session then spawns but the client has already given
        up ("the platform did not respond"). Subsequent runs are fast."""
        request_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._local_session_futures[request_id] = fut
        await self.enqueue_send({
            "type": "local_session_open",
            "request_id": request_id,
            **args,
        })
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._local_session_futures.pop(request_id, None)

    async def request_local_list(self, args: dict, timeout: float = 30.0) -> dict:
        """otodock-CLI --resume: ask the proxy for the owner's resumable chats.
        Sends ``local_session_list`` and awaits ``local_session_listed`` (or
        ``local_session_error``). Raises ``TimeoutError`` if unanswered."""
        request_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._local_session_futures[request_id] = fut
        await self.enqueue_send({
            "type": "local_session_list",
            "request_id": request_id,
            **args,
        })
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._local_session_futures.pop(request_id, None)

    async def send_local_detached(self, session_id: str) -> None:
        """Tell the proxy a local `otodock` terminal disconnected from ``session_id``
        (drops the idle-reaper exemption; the PTY itself stays alive)."""
        if session_id:
            await self.enqueue_send({
                "type": "local_session_detached",
                "session_id": session_id,
            })

    async def enqueue_pty(self, msg: dict) -> None:
        """Queue an interactive-PTY output frame on the LOSSLESS lane.

        Unlike enqueue_send this BLOCKS the caller when the lane is full instead of
        dropping the oldest — that backpressure pauses the PTY reader (the Unix
        reader task awaits this; the Windows reader thread blocks on it via
        run_coroutine_threadsafe), so the TUI's own write() stalls rather than us
        corrupting the xterm byte stream.

        DISCONNECTED, however, the frame is dropped outright (and the lane is
        purged in connect_forever's finally). Mirror frames have no viewer while
        the proxy is away, and parking the PTY reader on a dead lane turns the
        raw live tee to a local `otodock` terminal into a BURST stream — each
        burst carries many CSI 6n queries the real terminal mass-answers, which
        re-ignites the 59fe52b query/answer storm and floods the terminal until
        reconnect. Dropping keeps local delivery smooth 1:1 through the outage;
        the dashboard mirror recovers via the attach-time scrollback replay.
        """
        if self._ws is None or not self._authenticated:
            return
        await self._pty_send_queue.put(msg)
        self._send_wakeup.set()

    async def _writer_loop(self) -> None:
        """Single coroutine that owns ws.send(), draining TWO lanes CONTROL-FIRST:
        the drop-oldest control queue (acks, session/transcript/pty
        lifecycle events) and the LOSSLESS, backpressured pty-output lane. Control
        is drained fully before each pty frame, so a burst of terminal output can
        never delay an ack/heartbeat behind it (which would risk a false
        disconnect). When both lanes are empty the writer clears + re-checks
        _send_wakeup (closing the lost-wakeup window) then awaits it.

        On send failure: requeue to the SAME lane (slight reorder accepted over
        loss) then exit — the outer connect_forever loop reconnects and a fresh
        writer drains the persisted lanes.
        """
        try:
            while True:
                # Control-first: prefer a ready control frame, else one pty frame,
                # else sleep until a producer signals via _send_wakeup.
                lane = self._send_queue
                try:
                    msg = lane.get_nowait()
                except asyncio.QueueEmpty:
                    lane = self._pty_send_queue
                    try:
                        msg = lane.get_nowait()
                    except asyncio.QueueEmpty:
                        self._send_wakeup.clear()
                        # Re-check after clear so a producer that enqueued in the
                        # gap isn't missed; one that enqueues later re-sets the
                        # event we await. No lost wakeup, no busy-wait.
                        if not (self._send_queue.empty() and self._pty_send_queue.empty()):
                            continue
                        await self._send_wakeup.wait()
                        continue
                if not isinstance(msg, dict):
                    continue
                try:
                    await self._ws.send(json.dumps(msg))
                except (ConnectionClosed, ConnectionError) as e:
                    logger.warning(
                        "writer_loop send failed (%s); requeueing %s",
                        e, msg.get("type"),
                    )
                    try:
                        lane.put_nowait(msg)
                    except asyncio.QueueFull:
                        logger.warning(
                            "Could not requeue %s after send failure",
                            msg.get("type"),
                        )
                    return
                except Exception:
                    logger.exception(
                        "writer_loop unexpected error sending %s",
                        msg.get("type"),
                    )
                    return
        except asyncio.CancelledError:
            return

    async def _authenticate(self) -> None:
        """Send auth message directly, wait for auth_result (5s timeout)."""
        from ..config import SATELLITE_VERSION
        caps = self.sm.detect_capabilities()
        auth_msg = {
            "type": "auth",
            "machine_id": self.config.machine_id,
            "machine_secret": self.config.machine_secret,
            "capabilities": caps,
            "satellite_version": SATELLITE_VERSION,
        }
        # Direct send (writer task not started yet).
        await self._ws.send(json.dumps(auth_msg))

        raw = await asyncio.wait_for(self._ws.recv(), timeout=5.0)
        resp = json.loads(raw)
        # The proxy may send `update_required` INSTEAD
        # of `auth_result: ok` when our reported version is below the
        # platform's `SATELLITE_VERSION_LATEST` and `auto_update_enabled`
        # is True on this machine. Recognize that here — the message
        # loop hasn't started yet at this point, so without handling
        # update_required during auth the connection would hard-fail.
        if resp.get("type") == "update_required":
            logger.info(
                "Received update_required during auth %s → %s — applying",
                resp.get("previous_version", "?"),
                resp.get("target_version", "?"),
            )
            # Run synchronously: the os.replace + systemctl restart need
            # to complete before we hand back control. If extract fails,
            # _self_update_and_restart logs + returns; outer loop reconnects.
            await _self_update_and_restart(resp)
            # If _self_update_and_restart didn't sys.exit (extract or
            # hash check failed), raise so the outer loop reconnects
            # and the proxy can retry the push next time.
            raise ConnectionError("update_required handler returned without restart")
        if resp.get("status") != "ok":
            # Server-driven self-uninstall: when the platform rejects auth
            # with action="uninstall" (machine deleted from dashboard /
            # user-settings), run the uninstaller + exit instead of looping
            # forever. The 4006 close code path covers the case where the
            # server closes WITHOUT a prior auth_result, but the normal
            # delete flow sends auth_result first and the rejected
            # ConnectionError previously short-circuited before code 4006
            # was observed.
            if resp.get("action") == "uninstall":
                logger.warning(
                    "Auth rejected by platform (reason=%s) with action=uninstall. "
                    "Running self-uninstall.",
                    resp.get("reason", "?"),
                )
                await _self_uninstall_and_exit()
                # _self_uninstall_and_exit calls os._exit(0); never returns.
                return
            raise ConnectionError(f"Auth rejected: {resp}")
        self._authenticated = True

        # Report our live interactive PTY session ids so the proxy can
        # reconcile on reconnect — re-adopt survivors, exit the dead, reap orphans.
        # Always sent (empty on a fresh connect). Queued now; the writer task
        # (started after auth) drains it AFTER any output buffered during the drop.
        try:
            await self.enqueue_send({
                "type": "pty_alive",
                "session_ids": list(self.sm.pty_sessions.keys()),
            })
        except Exception:
            logger.debug("pty_alive enqueue failed", exc_info=True)

        # Headless twin of pty_alive: live CLI sessions + their in-flight
        # turn state, so a RESTARTED proxy can re-adopt running turns
        # (Mode C) instead of failing them blind at startup.
        try:
            await self.enqueue_send({
                "type": "sessions_alive",
                "sessions": self.sm.headless_sessions_alive(),
            })
        except Exception:
            logger.debug("sessions_alive enqueue failed", exc_info=True)

        # Cache the satellite-host policy locally so
        # we can re-validate satellite_host file writes/reads in
        # _apply_satellite_host_push / file_pull. Defense in depth — if
        # the proxy is compromised and sends us a write to /etc/sudoers,
        # we reject locally based on our own copy of allow_full_fs even
        # if the proxy's policy_v2 module would have admitted it. The
        # policy ALSO updates live via the `policy_update` WS message
        # (handled in _message_loop) when admin toggles allow_full_fs
        # mid-session.
        policy = resp.get("policy") or {}
        from ..host.satellite_policy import set_policy
        _policy_update = {
            "allow_full_fs": bool(policy.get("allow_full_fs", False)),
            # Device-control capabilities the owner has granted this machine
            # (computer / browser / app control). Cached
            # only (no satellite-side consumer yet — enforcement is proxy-side);
            # policy_update (live toggle) lands via the full-dict path in
            # _message_loop. set_policy validates/normalizes the list.
            "device_grants": policy.get("device_grants") or [],
        }
        # Universal per-file sync cap (0.5.103+ proxies). Only forward when
        # present — an older proxy sends none, and set_policy's partial-update
        # semantics must leave the key unset (legacy 100MB fallback), not
        # clobber it with None.
        if "sync_max_file_bytes" in policy:
            _policy_update["sync_max_file_bytes"] = policy["sync_max_file_bytes"]
        # Sync-ignore rule table (1.5+ proxies) — ALWAYS forwarded, absent →
        # None → set_policy CLEARS the cached table. Auth is a full policy
        # statement: a proxy downgrade must drop us to legacy exclusions in
        # lockstep, or the two manifests disagree and the diff misattributes
        # the whole excluded tree as satellite deletes. (Deliberately different
        # from the cap above — stale rules are dangerous, a stale cap is not.)
        _policy_update["sync_ignore_rules"] = policy.get("sync_ignore_rules")
        set_policy(_policy_update)

        # Auth succeeded — finalize a post-update state, or surface a
        # swap that SILENTLY FAILED (we're still on the old version). Made
        # version-aware so a blocked runner.ps1 swap no longer logs a false
        # "Update finalized" and loops forever (the slow-Windows-laptop bug).
        _finalize_post_update_state(otodock_dir(), SATELLITE_VERSION)

        # Pin-and-freeze: reconcile the installed claude/codex CLIs to the
        # platform pins (VERSIONS.md, shipped as cli_pins). Off-thread + fail-open
        # so a slow/absent npm never blocks the WS auth; verify-first, installs
        # only when no binary satisfies the pin. The CLIs' own auto-update is
        # disabled, so this is the sole thing that changes the installed
        # version → drift-proof fleet.
        #
        # set_pins runs HERE, synchronously: the message loop (the only source
        # of spawn commands) starts after _authenticate returns, so spawn-time
        # resolution provably sees the pins from this connection's first
        # session — the off-thread reconcile below can take minutes on a cold
        # npm install and must not be what spawns wait on.
        cli_pins = resp.get("cli_pins") or {}
        from ..host.cli_versions import reconcile_cli_versions, set_pins
        set_pins(cli_pins, {
            "claude": self.config.claude_bin,
            "codex": self.config.codex_bin,
        })
        if cli_pins:
            # cli_status trampoline: the reconcile runs in a worker thread;
            # hop its report back to the loop and onto the persistent control
            # queue — the queue (and this singleton client) survive
            # reconnects, so a report finishing after a drop delivers on the
            # next connection. Exceptions (loop closed at shutdown) are
            # swallowed by the reconcile's own report guard.
            loop = asyncio.get_running_loop()

            def _report_cli_status(clis: dict) -> None:
                asyncio.run_coroutine_threadsafe(
                    self.enqueue_send({"type": "cli_status", "clis": clis}),
                    loop,
                )

            asyncio.create_task(asyncio.to_thread(
                reconcile_cli_versions, cli_pins, _report_cli_status,
            ))

    async def _message_loop(self) -> None:
        """Route incoming platform messages to session manager."""
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON from platform: {raw[:100]}")
                continue

            msg_type = msg.get("type", "")
            try:
                if msg_type == "start_session":
                    asyncio.create_task(self.sm.start_session(msg, self))
                elif msg_type == "send_message":
                    asyncio.create_task(self.sm.send_message(msg, self))
                elif msg_type == "abort":
                    asyncio.create_task(self.sm.abort(msg, self))
                elif msg_type == "interrupt_turn":
                    # Soft headless-CLI abort: stop the generation, keep the
                    # process + MCP sidecars alive (proxy ≥ gates on 0.5.89).
                    asyncio.create_task(self.sm.interrupt_turn(msg))
                elif msg_type == "close_session":
                    asyncio.create_task(self.sm.close_session(msg, self))
                # Interactive PTY.
                elif msg_type == "pty_open":
                    asyncio.create_task(self.sm.pty_open(msg, self))
                elif msg_type == "pty_input":
                    asyncio.create_task(self.sm.pty_input(msg))
                elif msg_type == "pty_resize":
                    asyncio.create_task(self.sm.pty_resize(msg))
                elif msg_type == "pty_close":
                    asyncio.create_task(self.sm.pty_close(msg))
                elif msg_type == "pty_inject":
                    # Server-prompt injection into an otodock-attached CLI's
                    # stdin (the delegation-delivery PTY rung); always answers
                    # with a pty_inject_result frame.
                    asyncio.create_task(self.sm.pty_inject(msg, self))
                elif msg_type == "pty_local_detach":
                    # Dual-control: the dashboard took over — detach the local
                    # `otodock` terminal but keep the PTY alive.
                    asyncio.create_task(self.sm.evict_local_conn(msg, self))
                elif msg_type == "stop_turn":
                    asyncio.create_task(self.sm.stop_turn(msg))
                elif msg_type == "control_response":
                    # Native-permission answer from proxy → CLI stdin
                    asyncio.create_task(self.sm.control_response(msg))
                elif msg_type == "control_request":
                    asyncio.create_task(self.sm.control_request(msg))
                elif msg_type == "credentials_update":
                    # Rotation fan-out: rewrite a scope dir's CLI credential
                    # file (the live CLIs re-read it — no process touched).
                    asyncio.create_task(self.sm.credentials_update(msg, self))
                elif msg_type == "policy_update":
                    # Live policy refresh (admin / user toggled allow_full_fs or
                    # device_grants from the dashboard). Stores into the same
                    # local cache used at auth time so subsequent satellite_host
                    # writes/reads + device-tool calls re-check the new policy.
                    from ..host.satellite_policy import set_policy
                    set_policy(msg.get("policy") or {})
                    logger.info("Policy updated: %s", msg.get("policy"))
                elif msg_type == "check_session_resumable":
                    asyncio.create_task(
                        self.sm.check_session_resumable(msg, self)
                    )
                elif msg_type == "codex_compact":
                    # Manual thread compaction on a headless Codex session
                    # (remote twin of the local layer.compact()); always acks.
                    asyncio.create_task(self.sm.codex_compact(msg, self))
                elif msg_type == "codex_steer":
                    # Mid-turn steering into a running headless Codex turn
                    # (remote twin of layer.steer()); acks {steered}.
                    asyncio.create_task(self.sm.codex_steer(msg, self))
                elif msg_type == "codex_bg_terminals":
                    # Background-terminal list pull for the proxy's bg-command
                    # drain (remote twin of its list RPC); always acks.
                    asyncio.create_task(self.sm.codex_bg_terminals(msg, self))
                elif msg_type == "check_session_process":
                    asyncio.create_task(
                        self.sm.check_session_process(msg, self)
                    )
                elif msg_type == "resume_session_stream":
                    asyncio.create_task(
                        self.sm.resume_session_stream(msg, self)
                    )
                elif msg_type == "request_manifest":
                    asyncio.create_task(self.sm.request_manifest(msg, self))
                elif msg_type == "file_push":
                    asyncio.create_task(self.sm.file_push(msg, self))
                elif msg_type == "file_pull":
                    asyncio.create_task(self.sm.file_pull(msg, self))
                elif msg_type == "file_stat":
                    # Cheap cache-revalidation probe (0.5.95+) — replies as
                    # a command ack; validation mirrors file_pull.
                    asyncio.create_task(self.sm.file_stat(msg, self))
                elif msg_type == "sync_mcps":
                    asyncio.create_task(self.sm.sync_mcps(msg, self))
                elif msg_type == "sync_mcps_verify":
                    asyncio.create_task(self.sm.sync_mcps_verify(msg, self))
                elif msg_type == "uninstall":
                    # Platform asked us to self-uninstall (admin/user
                    # deleted the remote machine from the dashboard). Run
                    # the uninstall script and exit cleanly.
                    logger.info("Received uninstall command — self-cleaning")
                    asyncio.create_task(_self_uninstall_and_exit())
                elif msg_type == "update_required":
                    # Auto-update: platform pushed a fresh
                    # satellite tarball. Apply it atomically, restart
                    # via systemd, the new code reconnects on the new
                    # version. WS close 4007 is sent right after this
                    # message by the proxy.
                    logger.info(
                        "Received update_required %s → %s — applying",
                        msg.get("previous_version", "?"),
                        msg.get("target_version", "?"),
                    )
                    asyncio.create_task(_self_update_and_restart(msg))
                elif msg_type in ("local_session_opened", "local_session_error",
                                  "local_session_listed"):
                    # otodock-CLI: response to our local_session_open / _list request.
                    fut = self._local_session_futures.pop(msg.get("request_id", ""), None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                elif msg_type == "http_response":
                    # Tunneled HTTP response — synchronous dispatch into
                    # the matching pending stream's queue. Fast; no upstream
                    # call from the satellite side.
                    if self.tunnel is not None:
                        self.tunnel.dispatch_response(msg)
                elif msg_type == "http_response_chunk":
                    if self.tunnel is not None:
                        self.tunnel.dispatch_response(msg)
                elif msg_type == "pong":
                    pass
                else:
                    logger.debug(f"Unknown message type: {msg_type}")
            except Exception:
                logger.exception(f"Error handling {msg_type}")

    async def _heartbeat_loop(self) -> None:
        """Send heartbeat every 20s with system load info.

        Tracks monotonic clock between iterations. A large gap means the
        OS slept (laptop closed) — the WS is now almost certainly dead
        even though no error fired yet, so we close it explicitly to
        trigger the reconnect path.
        """
        last_tick = time.monotonic()
        last_fp_compute = 0.0
        agent_fingerprints: dict[str, str] = {}
        while True:
            try:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
            except asyncio.CancelledError:
                return
            now = time.monotonic()
            elapsed = now - last_tick
            if elapsed > _CLOCK_JUMP_THRESHOLD:
                logger.warning(
                    "Clock jump detected (%.1fs gap, expected ~%ds) — "
                    "system likely slept; forcing reconnect",
                    elapsed, _HEARTBEAT_INTERVAL,
                )
                try:
                    if self._ws is not None:
                        await self._ws.close(code=4004, reason="clock-jump")
                except Exception:
                    pass
                return
            last_tick = now
            # Recompute per-agent STAT fingerprints at most every
            # _FINGERPRINT_INTERVAL — off-thread so a large tree never blocks the
            # loop; the cached value rides intervening heartbeats. The proxy gates
            # its idle-sync sweep on these. Best-effort.
            if now - last_fp_compute >= _FINGERPRINT_INTERVAL:
                try:
                    agent_fingerprints = await asyncio.to_thread(
                        self.sm.compute_agent_fingerprints
                    )
                    last_fp_compute = now
                except Exception:
                    logger.debug("fingerprint compute failed", exc_info=True)
                try:
                    await asyncio.to_thread(
                        _sweep_satellite_screenshots,
                        self.sm.config.agents_dir, _SCREENSHOTS_KEEP,
                    )
                except Exception:
                    logger.debug("screenshots sweep failed", exc_info=True)
            try:
                await self.enqueue_send({
                    "type": "heartbeat",
                    "load": _get_system_load(),
                    # All live agent sessions: headless (-p/codex) + interactive PTY
                    # (the latter also holds native-CLI/otodock sessions). The old
                    # count missed pty_sessions → undercounted interactive boxes.
                    "active_sessions": len(self.sm.sessions) + len(self.sm.pty_sessions),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "agent_fingerprints": agent_fingerprints,
                })
            except Exception:
                logger.exception("heartbeat enqueue failed")
