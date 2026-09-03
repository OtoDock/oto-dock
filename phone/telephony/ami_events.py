"""Persistent AMI event listeners — signalled DTMF for AudioSocket calls.

AudioSocket has no digit TLV, and the in-band Goertzel detector cannot see
RFC2833/SIP-INFO digits (they travel as signalling, never as audio). Asterisk
itself emits AMI ``DTMFEnd`` events for every signalled digit, so the daemon
keeps one persistent AMI connection per AMI-capable server and forwards
matched digits into the live call's transport via ``inject_dtmf``.

Correlation: the inbound dialplan's register curl carries ``${UNIQUEID}`` in
``dial_event``; ``main._handle_connection`` registers ``(server_key,
uniqueid) → connection`` here for the call's duration. Uniqueids are only
unique per Asterisk instance, so events match registrations for their own
server only.

The event connection doubles as the actions surface for the future
transfer feature (AMI ``Redirect`` rides ``send_action`` on the same live
socket; the registration map keeps each call's channel for exactly that).

Digit VALUES are PIN material — never logged (grep-guarded by
``tests/test_pin_log_hygiene.py``); only counts and sources are.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from typing import Callable

from telephony.ami_client import AMIError, format_action, read_packet

logger = logging.getLogger("ami_events")

#: Keepalive cadence; the read timeout allows two missed pings before the
#: connection is declared dead.
PING_INTERVAL_S = 30.0
READ_TIMEOUT_S = 75.0
#: Reconnect backoff envelope. A connection that survived STABLE_AFTER_S
#: resets the backoff (a PBX reboot must self-heal quickly; a PBX that
#: drops us right after login must not be hammered).
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0
STABLE_AFTER_S = 30.0
#: Per-action response budget (matches the origination client's spirit).
ACTION_TIMEOUT_S = 10.0

_VALID_DIGITS = frozenset("0123456789*#ABCD")

#: Reserved manager key for the flat single-Asterisk legacy config (cannot
#: collide with numeric server ids).
FLAT_SERVER_KEY = "_flat"

#: on_dtmf(server_key, uniqueid, digit, duration_ms)
DtmfHandler = Callable[[str, str, str, int], None]


class AmiEventClient:
    """One persistent AMI connection: event demux + request/response actions.

    ``run()`` loops forever (connect → login → subscribe → read) with
    exponential backoff; ``close()`` is the only way out. Digit dispatch is
    fully synchronous inside the read loop (parse → handler, no await in
    between) so teardown can never race a half-delivered event.
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        secret: str,
        *,
        server_key: str,
        on_dtmf: DtmfHandler,
        ping_interval_s: float = PING_INTERVAL_S,
        read_timeout_s: float = READ_TIMEOUT_S,
        backoff_min_s: float = BACKOFF_MIN_S,
        backoff_max_s: float = BACKOFF_MAX_S,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self.server_key = server_key
        self.on_dtmf = on_dtmf
        self.ping_interval_s = ping_interval_s
        self.read_timeout_s = read_timeout_s
        self.backoff_min_s = backoff_min_s
        self.backoff_max_s = backoff_max_s

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._action_seq = itertools.count(1)
        self._connected = False
        self._closing = False
        self._ping_task: asyncio.Task | None = None
        #: Events whose Uniqueid had no registration — live-diagnosis counter
        #: (e.g. a Local-channel ``;2``-leg mismatch shows up here).
        self.unmatched_events = 0

    # -- public ---------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    async def run(self) -> None:
        """Connect/reconnect loop; returns only via ``close()``."""
        backoff = self.backoff_min_s
        while not self._closing:
            started = time.monotonic()
            try:
                await self._connect_and_read()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._closing:
                    break
                logger.warning(
                    f"AMI events [{self.server_key}] connection lost: {e}"
                )
            finally:
                await self._teardown_connection()
            if self._closing:
                break
            if time.monotonic() - started >= STABLE_AFTER_S:
                backoff = self.backoff_min_s
            logger.info(
                f"AMI events [{self.server_key}] reconnecting in {backoff:.0f}s"
            )
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, self.backoff_max_s)

    async def send_action(self, action: dict) -> dict:
        """Send one action, await its response (10 s budget).

        Raises ``AMIError`` immediately when not connected; pending futures
        are failed on connection death (old ActionIDs are unanswerable on a
        new connection). Multi-packet responses (``Response: Follows``,
        event-list actions) are unsupported.
        """
        if not self._connected or not self._writer:
            raise AMIError("AMI events connection not established")
        action = dict(action)
        action_id = f"ev{next(self._action_seq)}"
        action["ActionID"] = action_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[action_id] = fut
        try:
            self._writer.write(format_action(action))
            await self._writer.drain()
            return await asyncio.wait_for(fut, timeout=ACTION_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise AMIError(f"AMI action {action.get('Action')!r} timed out")
        finally:
            self._pending.pop(action_id, None)

    async def close(self) -> None:
        self._closing = True
        await self._teardown_connection()

    # -- connection lifecycle -------------------------------------------------

    async def _connect_and_read(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=10.0,
        )
        banner = await asyncio.wait_for(self._reader.readline(), timeout=5.0)
        if not banner:
            raise AMIError("AMI closed before banner")

        # Login with events off, then subscribe narrowly. The effective mask
        # is read-perms ∩ EventMask — the manager user needs the ``dtmf``
        # read class (provisioning template grants it; see
        # PHONE-PROVISIONING.md for pre-existing installs).
        login = await self._request_during_setup({
            "Action": "Login",
            "Username": self.username,
            "Secret": self.secret,
            "Events": "off",
        })
        if "Success" not in login.get("Response", ""):
            raise AMIError(
                f"AMI login failed: {login.get('Message', 'unknown')}"
            )
        sub = await self._request_during_setup(
            {"Action": "Events", "EventMask": "call,dtmf"})
        # Modern shape: "Response: Success". Legacy (pre-12): "Response:
        # Events On". Either counts; on genuine failure fall back to the
        # full mask — client-side filtering runs regardless, and no events
        # at all still degrades to the in-band detector.
        resp = sub.get("Response", "").lower()
        if "success" not in resp and "events" not in resp:
            with contextlib.suppress(Exception):
                await self._request_during_setup(
                    {"Action": "Events", "EventMask": "on"})

        self._connected = True
        logger.info(f"AMI events [{self.server_key}] connected + subscribed")
        self._ping_task = asyncio.create_task(self._ping_loop())
        await self._read_loop()

    async def _request_during_setup(self, action: dict) -> dict:
        """Sequential request/response before the demux loop starts.

        Skips interleaved events (FullyBooted arrives even with
        ``Events: off``) by matching on ActionID.
        """
        action = dict(action)
        action_id = f"setup{next(self._action_seq)}"
        action["ActionID"] = action_id
        self._writer.write(format_action(action))
        await self._writer.drain()
        for _ in range(20):
            packet = await read_packet(self._reader, line_timeout=10.0)
            if not packet:
                continue
            if packet.get("ActionID") == action_id:
                return packet
        raise AMIError("no matching AMI response during setup")

    async def _read_loop(self) -> None:
        while True:
            packet = await read_packet(
                self._reader, line_timeout=self.read_timeout_s)
            if not packet:
                continue
            action_id = packet.get("ActionID", "")
            fut = self._pending.get(action_id)
            if fut is not None:
                if not fut.done():
                    fut.set_result(packet)
                continue
            if "Event" in packet:
                self._handle_event(packet)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ping_interval_s)
            try:
                await self.send_action({"Action": "Ping"})
            except asyncio.CancelledError:
                raise
            except Exception:
                # Dead socket: force the read loop out of its timeout wait.
                if self._writer is not None:
                    with contextlib.suppress(Exception):
                        self._writer.close()
                return

    async def _teardown_connection(self) -> None:
        self._connected = False
        if self._ping_task is not None:
            self._ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ping_task
            self._ping_task = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AMIError("AMI events connection lost"))
        self._pending.clear()
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    # -- events ---------------------------------------------------------------

    def _handle_event(self, packet: dict) -> None:
        if packet.get("Event") != "DTMFEnd":
            return
        if packet.get("Direction", "Received") != "Received":
            return
        digit = packet.get("Digit", "")
        if len(digit) != 1 or digit not in _VALID_DIGITS:
            return
        uniqueid = packet.get("Uniqueid", "")
        if not uniqueid:
            return
        try:
            duration_ms = int(float(packet.get("DurationMs", 0)))
        except (TypeError, ValueError):
            duration_ms = 0
        self.on_dtmf(self.server_key, uniqueid, digit, duration_ms)


class AmiListenerManager:
    """Listener lifecycle (config-diffed) + the live-call registration map.

    ``apply(cfg)`` is serialized by an internal lock — config pushes arrive
    per DB mutation and the sync WS callback schedules applies as tasks, so
    two applies must never interleave their start/stop work.
    """

    def __init__(self, client_factory: Callable[..., AmiEventClient] | None = None):
        self._client_factory = client_factory or AmiEventClient
        #: key → (coords tuple, client, run task)
        self._clients: dict[str, tuple[tuple, AmiEventClient, asyncio.Task]] = {}
        #: (server_key, uniqueid) → (conn, ami_channel)
        self._calls: dict[tuple[str, str], tuple[object, str]] = {}
        self._lock = asyncio.Lock()
        self._stopped = False

    # -- lifecycle ------------------------------------------------------------

    async def apply(self, cfg) -> None:
        """Reconcile listeners with the pushed config (start/stop/restart)."""
        async with self._lock:
            if self._stopped:
                return
            desired = self._desired_servers(cfg)
            # Stop removed/changed listeners first (a credential rotation
            # must not briefly run two connections as the same user).
            for key in list(self._clients):
                if desired.get(key) == self._clients[key][0]:
                    continue
                await self._stop_client(key)
            for key, coords in desired.items():
                if key in self._clients:
                    continue
                host, port, username, secret = coords
                client = self._client_factory(
                    host, port, username, secret,
                    server_key=key, on_dtmf=self._on_dtmf,
                )
                task = asyncio.create_task(client.run())
                self._clients[key] = (coords, client, task)
                logger.info(f"AMI events [{key}] listener started")

    async def stop(self) -> None:
        async with self._lock:
            self._stopped = True
            for key in list(self._clients):
                await self._stop_client(key)
            self._calls.clear()

    async def _stop_client(self, key: str) -> None:
        coords, client, task = self._clients.pop(key)
        await client.close()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        logger.info(f"AMI events [{key}] listener stopped")

    def _desired_servers(self, cfg) -> dict[str, tuple]:
        """AMI-capable servers as ``key → (host, port, username, secret)``.

        The proxy mirrors ``ami_host`` from the server row's plain host for
        every non-Twilio server, so ``ami_host`` alone means nothing — a
        listener starts only with full credentials (username AND secret);
        anything less would hammer failed logins against port 5038 until
        the PBX's fail2ban bans the daemon IP, taking the register curl and
        outbound Originate down with it.

        The flat single-Asterisk legacy settings are used ONLY when the
        servers map has no AMI-capable entry — on current proxies the flat
        values always mirror the default server row, and connecting to both
        would double-inject every digit.
        """
        if cfg.ami_dtmf_listener == "off":
            return {}
        desired: dict[str, tuple] = {}
        for key, entry in cfg.ami_event_servers().items():
            desired[key] = (
                entry["host"], entry["port"],
                entry["username"], entry["secret"],
            )
        if not desired and cfg.ami_host and cfg.ami_username and cfg.ami_secret:
            desired[FLAT_SERVER_KEY] = (
                cfg.ami_host, cfg.ami_port, cfg.ami_username, cfg.ami_secret,
            )
        return desired

    # -- live-call registration ----------------------------------------------

    def resolve_server_key(self, phone_server_id) -> str | None:
        """The listener key covering a route's server, or None when no
        listener covers it (unconfigured AMI / kill switch / Twilio)."""
        if phone_server_id is not None:
            key = str(phone_server_id)
            if key in self._clients:
                return key
        if FLAT_SERVER_KEY in self._clients:
            return FLAT_SERVER_KEY
        return None

    def register_call(
        self, server_key: str, uniqueid: str, conn, ami_channel: str = "",
    ) -> None:
        key = (server_key, uniqueid)
        existing = self._calls.get(key)
        if existing is not None and existing[0] is not conn:
            # Static per-route AudioSocket UUIDs: simultaneous calls to the
            # same DID can cross-pair (pre-existing limitation, documented
            # in AUDIOSOCKET.md). Count-only — no identifiers.
            logger.warning(
                f"AMI events [{server_key}] overwriting a live DTMF "
                f"registration (simultaneous same-DID calls?)"
            )
        self._calls[key] = (conn, ami_channel)

    def unregister_call(self, server_key: str, uniqueid: str) -> None:
        """Safe no-op after stop() and for never-registered calls."""
        self._calls.pop((server_key, uniqueid), None)

    def call_channel(self, server_key: str, uniqueid: str) -> str:
        """The registered Asterisk channel (future Redirect target)."""
        entry = self._calls.get((server_key, uniqueid))
        return entry[1] if entry else ""

    # -- dispatch -------------------------------------------------------------

    def _on_dtmf(
        self, server_key: str, uniqueid: str, digit: str, duration_ms: int,
    ) -> None:
        entry = self._calls.get((server_key, uniqueid))
        if entry is None:
            client_entry = self._clients.get(server_key)
            if client_entry is not None:
                client_entry[1].unmatched_events += 1
            logger.debug(
                f"AMI events [{server_key}] DTMF for unregistered call"
            )
            return
        conn = entry[0]
        inject = getattr(conn, "inject_dtmf", None)
        if inject is None:
            return
        inject(digit, duration_ms)
        logger.info(f"AMI events [{server_key}] DTMF digit received")
