"""Phone Server — AsyncIO TCP server for Asterisk AudioSocket.

Listens for AudioSocket connections from Asterisk (FreePBX),
creates a call pipeline per call: VAD → STT → LLM → TTS.

Also runs an HTTP API for outbound call management (AMI origination).

On startup, connects to the proxy's management WebSocket to receive
configuration (routes, credentials, settings). All config is pushed
from the proxy DB — no local .env needed except PROXY_URL/PROXY_API_KEY.
"""

import asyncio
import contextlib
import ipaddress
import logging
import logging.handlers
import re
import signal
import socket
import sys
import time
from datetime import datetime, timezone

from telephony.ami_events import AmiListenerManager
from telephony.audio_socket import AudioSocketConnection, AudioSocketError
from calls import pin_failures
from calls.call_manager import CallManager, CallStatus
from calls.call_registry import registry as call_registry
from config_manager import ConfigManager
from calls.http_api import OutboundCallAPI
from proxy.client import fetch_pin_failures, report_call
from proxy.management_ws import ManagementWSClient
from pipeline import CallPipeline
from pipeline.providers import prewarm_fillers
import config

# Track active calls
_active_calls: dict[str, asyncio.Task] = {}

# Connections that have not yet sent their identifying UUID frame are
# bounded before anything is read from them: at most PENDING_MAX across the
# daemon and PENDING_PER_PEER_MAX per peer (``_peer_key``), closed at once
# past either ceiling, and released as soon as the frame is read (a resolved
# call is ``_active_calls``'s from then on). The top PENDING_PBX_RESERVE of
# PENDING_MAX is usable only by the configured PBX addresses, so peers that
# hold the rest cannot close the PBX's own connects. Asterisk sends the
# frame immediately, so the read waits two seconds at most. The soft
# descriptor limit is raised at start so a burst of connections cannot
# exhaust it first.
PENDING_MAX = 64
PENDING_PER_PEER_MAX = 8
PENDING_PBX_RESERVE = 16
UUID_READ_TIMEOUT_S = 2.0
PBX_RESOLVE_TIMEOUT_S = 10.0
_pending_total = 0
_pending_by_peer: dict[str, int] = {}


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address ``text`` names, without its zone and with an IPv4-mapped
    IPv6 address unwrapped to IPv4; None when it is not an address literal."""
    try:
        addr = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


class _PbxPeers:
    """The addresses the configured PBX hosts resolve to.

    Refreshed after each config push and never on the accept path, which
    only reads ``addresses``. A literal address skips DNS. A host that fails
    to resolve, or that the resolver refuses as malformed, keeps the
    addresses it last resolved to and is logged once until it resolves
    again. Refreshes run one at a time in push order.
    """

    def __init__(self) -> None:
        self.addresses: frozenset[str] = frozenset()
        self._by_host: dict[str, frozenset[str]] = {}
        self._failing: set[str] = set()
        self._lock = asyncio.Lock()

    async def refresh(self, hosts: list[str], logger: logging.Logger) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            by_host: dict[str, frozenset[str]] = {}
            for host in hosts:
                literal = _ip(host)
                if literal is not None:
                    by_host[host] = frozenset((str(literal),))
                    continue
                try:
                    infos = await asyncio.wait_for(
                        loop.getaddrinfo(host, None, type=socket.SOCK_STREAM),
                        PBX_RESOLVE_TIMEOUT_S,
                    )
                except (OSError, ValueError, asyncio.TimeoutError) as e:
                    if host not in self._failing:
                        self._failing.add(host)
                        logger.warning(
                            f"PBX host {host} did not resolve, its last addresses "
                            f"keep the accept reserve: {e!r}")
                    if host in self._by_host:
                        by_host[host] = self._by_host[host]
                    continue
                self._failing.discard(host)
                by_host[host] = frozenset(
                    str(a) for a in (_ip(str(info[4][0])) for info in infos) if a)
            self._failing &= set(hosts)
            self._by_host = by_host
            self.addresses = frozenset().union(*by_host.values())


_pbx_peers = _PbxPeers()
# The scheduled refreshes, held until done so none is collected mid-run.
_pbx_refresh_tasks: set[asyncio.Task] = set()


def _pbx_refresh_done(task: asyncio.Task) -> None:
    _pbx_refresh_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logging.getLogger("phone-server").warning(
            f"PBX address refresh failed: {task.exception()!r}")


def _schedule_pbx_refresh(cfg: ConfigManager, logger: logging.Logger) -> None:
    """Refresh the PBX addresses for the config just loaded, off the caller."""
    task = asyncio.create_task(_pbx_peers.refresh(cfg.pbx_hosts(), logger))
    _pbx_refresh_tasks.add(task)
    task.add_done_callback(_pbx_refresh_done)


def _peer_key(host: str) -> str:
    """The key a peer's pending connections are counted under: a configured
    PBX address and any IPv4 address by itself, any other IPv6 address by
    its /64 (one host commonly holds a whole /64)."""
    addr = _ip(host)
    if addr is None:
        return host
    key = str(addr)
    if isinstance(addr, ipaddress.IPv4Address) or key in _pbx_peers.addresses:
        return key
    return str(ipaddress.IPv6Network((addr, 64), strict=False))


class _RateLimitedLog:
    """One line per reason per window, carrying the count it held back."""

    def __init__(self, window_s: float = 5.0):
        self._window = window_s
        self._last: dict[str, float] = {}
        self._held: dict[str, int] = {}

    def warning(self, log: logging.Logger, reason: str, message: str) -> None:
        now = time.monotonic()
        last = self._last.get(reason)
        if last is not None and now - last < self._window:
            self._held[reason] = self._held.get(reason, 0) + 1
            return
        held = self._held.pop(reason, 0)
        self._last[reason] = now
        if held:
            message += f" ({held} more in the last {self._window:.0f}s)"
        log.warning(message)


_reject_log = _RateLimitedLog()


def _raise_nofile_limit(log: logging.Logger) -> None:
    """Lift the soft descriptor limit to the hard one (at most 65536): the
    daemon holds a socket per call plus its provider connections, and a
    container's default soft limit of 1024 is what a connection burst fills."""
    import resource
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = 65536 if hard == resource.RLIM_INFINITY else min(hard, 65536)
        if target > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            log.info(f"RLIMIT_NOFILE soft limit raised {soft} -> {target}")
    except (ValueError, OSError) as e:
        log.warning(f"RLIMIT_NOFILE soft limit not raised: {e}")

# Asterisk uniqueid shape (``epoch.seq``, optional systemname prefix) — the
# register payload's dial_event is caller-supplied JSON, so the value is
# validated before it can key the AMI DTMF registration map.
_UNIQUEID_RE = re.compile(r"[\w.\-/]{1,64}\Z")

# Shared call manager for outbound calls
_call_manager = CallManager()

# Inbound caller metadata is sourced from the generic ``call_registry``
# (populated by HTTP push from any phone driver — Asterisk dialplan
# ``System(curl ...)``, 3CX webhook adapter, Twilio webhook adapter, etc.).
# For outbound calls we build the entry inline from ``OutboundCall``.


def _resolve_route(call_uuid: str, cfg: ConfigManager):
    """Resolve the phone route from the call UUID.

    Returns (PhoneRoute | None, is_outbound).
    Checks outbound call registry first, then looks up inbound routes.
    Unknown UUIDs return (None, False).
    """
    outbound_call = _call_manager.get_call_by_uuid(call_uuid)
    if outbound_call:
        # Every origination named its route; an unknown id resolves to None.
        return (cfg.get_outbound_route(outbound_call.route_id), True)

    route = cfg.resolve_inbound_route(call_uuid)
    return (route, False)


# The history asked for: the lockout window plus room for a slow gate (a
# refused call is stamped when it ended, the window is judged here).
_PIN_HISTORY_S = int(pin_failures.WINDOW_S) + 600
_pin_seed_task: asyncio.Task | None = None


async def _seed_pin_lockout(logger: logging.Logger) -> None:
    """Rebuild the PIN gate's lockout windows from the proxy's call log, once
    per process: a restart would otherwise clear every cooldown."""
    if pin_failures.store.seeded:
        return
    calls = await fetch_pin_failures(_PIN_HISTORY_S)
    if calls is None or pin_failures.store.seeded:
        return
    replayed = pin_failures.store.seed(calls)
    logger.info(f"PIN lockout windows restored ({replayed} recent failures)")


def _start_pin_seed(logger: logging.Logger) -> asyncio.Task | None:
    """One seed at a time; none once a seed landed."""
    global _pin_seed_task
    if pin_failures.store.seeded or (_pin_seed_task and not _pin_seed_task.done()):
        return _pin_seed_task
    _pin_seed_task = asyncio.create_task(_seed_pin_lockout(logger))
    return _pin_seed_task


def _spawn_call_report(payload: dict) -> None:
    """Fire-and-forget call-log row (proxy.client.report_call swallows all
    errors). Guarded so a sync/test context without a loop is a no-op."""
    with contextlib.suppress(RuntimeError):
        asyncio.get_running_loop().create_task(report_call(payload))


def _inbound_report(
    conn, resolution_uuid: str, route, caller_info: dict | None,
    *, outcome: str, pin_attempts: int, started_at: str, duration_s: int,
    session_id: str = "",
) -> dict:
    """``session_id`` names the warmed proxy session (empty when the call
    never reached the agent) — the proxy joins the call's identity and the
    tools it ran onto the log row from it."""
    caller_info = caller_info or {}
    return {
        "route_id": route.id or "",
        "phone_server_id": route.phone_server_id,
        "agent": route.agent or "",
        "direction": "inbound",
        "from_number": caller_info.get("phone", "") or "",
        "to_number": caller_info.get("did", "") or route.did or "",
        "transport": getattr(conn, "transport_name", "audiosocket"),
        "call_uuid": conn.call_uuid or resolution_uuid,
        "outcome": outcome,
        "pin_attempts": pin_attempts,
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": duration_s,
        "session_id": session_id or "",
    }


async def _run_call(
    conn,
    resolution_uuid: str,
    route,
    is_outbound: bool,
    caller_info: dict | None,
    cfg: ConfigManager,
    logger: logging.Logger,
) -> None:
    """Shared per-call body for every media transport (AudioSocket, Twilio).

    ``resolution_uuid`` is the key the call was RESOLVED by — the AudioSocket
    first-frame UUID / outbound ``audio_uuid`` / Twilio session token — not
    the transport's own ``call_uuid`` correlation label (Twilio: callSid).
    """
    started_at = datetime.now(timezone.utc).isoformat()
    if len(_active_calls) >= cfg.max_live_calls:
        logger.warning(
            f"Rejecting {conn.peer_addr} — at capacity "
            f"({len(_active_calls)}/{cfg.max_live_calls} active calls)"
        )
        # The operator wants rejected callers visible in the route's call
        # log (outbound rejects surface via the CallManager watchdog).
        if not is_outbound:
            _spawn_call_report(_inbound_report(
                conn, resolution_uuid, route, caller_info,
                outcome="rejected_capacity", pin_attempts=0,
                started_at=started_at, duration_s=0,
            ))
        await conn.close()
        return

    if is_outbound:
        outbound_call = _call_manager.get_call_by_uuid(resolution_uuid)
        if outbound_call is None:
            logger.warning(
                f"Call {resolution_uuid}: outbound record vanished — rejecting"
            )
            await conn.close()
            return
        _call_manager.update_status(outbound_call.call_id, CallStatus.CONNECTED)
        # phone-mcp passed phone_number when it POSTed /api/calls earlier;
        # we read it off the OutboundCall and forward it as caller_info.
        # ``${trigger.phone}`` then resolves to the CALLEE on outbound calls
        # (semantic mirror of inbound, where it's the caller).
        outbound_caller_info = {
            "phone": outbound_call.phone_number or "",
            "did": route.ami_caller_id or "",
            "source": "phone-outbound",
            "dial_event": {
                "task_description": outbound_call.task_description or "",
                "call_id": outbound_call.call_id,
            },
        }
        pipeline = CallPipeline(
            conn,
            route=route,
            cfg=cfg,
            call_manager=_call_manager,
            outbound_call_id=outbound_call.call_id,
            audiosocket_uuid=resolution_uuid,
            caller_info=outbound_caller_info,
        )
    else:
        pipeline = CallPipeline(
            conn, route=route, cfg=cfg,
            audiosocket_uuid=resolution_uuid,
            caller_info=caller_info,
        )

    # Track this call
    task = asyncio.current_task()
    _active_calls[resolution_uuid] = task

    t0 = time.monotonic()
    outcome_override: str | None = None
    try:
        await asyncio.wait_for(
            pipeline.run(),
            timeout=cfg.call_max_duration_s,
        )
    except asyncio.TimeoutError:
        logger.warning(f"Call {resolution_uuid} exceeded max duration ({cfg.call_max_duration_s}s)")
    except Exception as e:
        logger.error(f"Call {resolution_uuid} error: {e}", exc_info=True)
        outcome_override = "error"
    finally:
        _active_calls.pop(resolution_uuid, None)
        await conn.close()
        # Inbound rows come from here (also covers PIN-refused calls that
        # never warmed up); outbound rows come from CallManager's terminal
        # edge — never both, so one call is one row.
        if not is_outbound:
            # ``pipeline.llm`` is None when the call never warmed a session
            # (PIN refused, early hangup).
            _spawn_call_report(_inbound_report(
                conn, resolution_uuid, route, caller_info,
                outcome=outcome_override or pipeline.state.call_outcome,
                pin_attempts=pipeline.state.pin_attempts,
                started_at=started_at,
                duration_s=int(time.monotonic() - t0),
                session_id=getattr(pipeline.llm, "session_id", "") or "",
            ))
        logger.info(f"Call {resolution_uuid} ended ({len(_active_calls)} active calls)")


async def _handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    cfg: ConfigManager,
    logger: logging.Logger,
    ami_manager: AmiListenerManager | None = None,
) -> None:
    """Handle a single AudioSocket connection from Asterisk."""
    global _pending_total
    peername = writer.get_extra_info("peername")
    peer_host = str(peername[0]) if peername else "unknown"
    peer_key = _peer_key(peer_host)
    # A key equal to a PBX address is that PBX: other IPv6 peers key by /64.
    ceiling = (PENDING_MAX if peer_key in _pbx_peers.addresses
               else PENDING_MAX - PENDING_PBX_RESERVE)
    if (_pending_total >= ceiling
            or _pending_by_peer.get(peer_key, 0) >= PENDING_PER_PEER_MAX):
        _reject_log.warning(
            logger, "pending",
            f"Rejecting {peer_host}: too many connections awaiting their UUID "
            f"frame ({_pending_total} pending)",
        )
        writer.close()
        return
    conn = AudioSocketConnection(reader, writer)
    if len(_active_calls) >= cfg.max_live_calls:
        logger.warning(
            f"Rejecting {conn.peer_addr} — at capacity "
            f"({len(_active_calls)}/{cfg.max_live_calls} active calls)"
        )
        await conn.close()
        return
    logger.info(f"New connection from {conn.peer_addr}")

    _pending_total += 1
    _pending_by_peer[peer_key] = _pending_by_peer.get(peer_key, 0) + 1
    try:
        # First frame must be UUID
        call_uuid = await asyncio.wait_for(conn.read_uuid(), timeout=UUID_READ_TIMEOUT_S)
    except (AudioSocketError, asyncio.TimeoutError) as e:
        _reject_log.warning(
            logger, "uuid", f"Failed to read UUID from {conn.peer_addr}: {e}")
        await conn.close()
        return
    finally:
        _pending_total -= 1
        left = _pending_by_peer.get(peer_key, 0) - 1
        if left > 0:
            _pending_by_peer[peer_key] = left
        else:
            _pending_by_peer.pop(peer_key, None)

    route, is_outbound = _resolve_route(call_uuid, cfg)

    if route is None:
        _reject_log.warning(
            logger, "unknown-uuid",
            f"Unknown UUID {call_uuid} from {conn.peer_addr}; rejecting connection",
        )
        await conn.close()
        return

    logger.info(
        f"Call {call_uuid} from {conn.peer_addr} → "
        f"agent={route.agent}, mode={route.llm_mode}, outbound={is_outbound}"
    )

    caller_info = None
    if not is_outbound:
        # Caller metadata comes from the HTTP-push call registry. The phone
        # driver MUST pre-register the call via ``POST /v1/calls/register``
        # BEFORE its AudioSocket() invocation (Asterisk: ``System(curl ...)``
        # line right before ``AudioSocket()``). Cache miss = no enrichment —
        # the call still goes through; the proxy resolves the route + bound
        # trigger but ``${trigger.phone}`` resolves empty and any
        # ``requires: ["trigger.phone"]`` block skips silently.
        caller_info = call_registry.pop_for_uuid(call_uuid)
        if caller_info:
            logger.info(
                f"Call {call_uuid}: caller phone={caller_info.get('phone', '')!r}, "
                f"did={caller_info.get('did', '')!r} "
                f"(from call_registry, source={caller_info.get('source', '')!r})"
            )
        else:
            logger.warning(
                f"Call {call_uuid}: no call_registry entry. The phone driver "
                f"must POST to /v1/calls/register before AudioSocket() — e.g. "
                f"a System(curl ...) dialplan line right before AudioSocket()."
            )

    # Bind the call to its server's AMI event listener so signalled DTMF
    # (RFC2833/SIP INFO — invisible to the in-band detector) reaches the PIN
    # gate. Registration lives ONLY on this AudioSocket path — the Twilio
    # path enters _run_call directly and must never register.
    ami_reg: tuple[str, str] | None = None
    if not is_outbound and ami_manager is not None:
        uniqueid = str(
            (caller_info or {}).get("dial_event", {}).get("uniqueid", "") or "")
        server_key = ami_manager.resolve_server_key(route.phone_server_id)
        if server_key is not None and _UNIQUEID_RE.fullmatch(uniqueid):
            channel = str(
                (caller_info or {}).get("dial_event", {}).get("channel", "") or "")
            ami_manager.register_call(server_key, uniqueid, conn, channel)
            ami_reg = (server_key, uniqueid)
        elif route.pin:
            logger.info(
                f"Call {call_uuid}: AMI DTMF unavailable (no listener for the "
                f"server, or the dialplan register curl predates the "
                f"dial_event uniqueid field) — in-band digit detection only."
            )

    try:
        await _run_call(
            conn, call_uuid, route, is_outbound, caller_info, cfg, logger)
    finally:
        if ami_reg is not None and ami_manager is not None:
            ami_manager.unregister_call(*ami_reg)


async def _run_server() -> None:
    """Start the management WS, HTTP API, and TCP server."""
    # Set up logging with defaults — will be reconfigured after config arrives
    _log_fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=_log_fmt)
    # Self-rotating: 10 MB x (1 live + 3 backups) = 40 MB hard cap (the log
    # grew unbounded before; no external logrotate dependency).
    _file_handler = logging.handlers.RotatingFileHandler(
        str(config.BASE_DIR / "phone-server.log"),
        maxBytes=10 * 1024 * 1024, backupCount=3,
    )
    _file_handler.setLevel(logging.INFO)
    _file_handler.setFormatter(logging.Formatter(_log_fmt))
    logging.getLogger().addHandler(_file_handler)

    logger = logging.getLogger("phone-server")
    _raise_nofile_limit(logger)

    # Suppress harmless Deepgram SDK cancel noise
    logging.getLogger("deepgram.clients.common.v1.abstract_async_websocket").setLevel(
        logging.CRITICAL
    )

    logger.info("Starting Phone Server")
    logger.info(f"  Proxy: {config.PROXY_URL}")

    # 1. Connect to proxy management WebSocket
    mgmt = ManagementWSClient(config.PROXY_URL, config.PROXY_API_KEY)
    from duplex import capabilities_frame
    mgmt.capabilities_frame = capabilities_frame()
    mgmt_task = asyncio.create_task(mgmt.connect())

    logger.info("Waiting for config from proxy...")
    try:
        config_data = await asyncio.wait_for(mgmt.wait_for_config(), timeout=30.0)
    except asyncio.TimeoutError:
        logger.error("Timeout waiting for config from proxy — exiting")
        mgmt.stop()
        return

    # 2. Initialize ConfigManager
    cfg = ConfigManager()
    cfg.load(config_data)

    # The PIN lockout windows, before the first call (bounded: a slow proxy
    # never holds the start; a config push retries until one lands).
    seed = _start_pin_seed(logger)
    if seed is not None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(seed), timeout=10.0)

    # Pre-warm each enabled route's filler clips so the first call never pays the
    # synth latency. Background — never blocks server startup; the call
    # path self-heals if a call lands first.
    asyncio.create_task(prewarm_fillers(cfg))

    # Persistent AMI event listeners (signalled DTMF for AudioSocket calls).
    # apply() is Lock-serialized internally — scheduling it as a task from
    # the sync WS callback can never interleave two reconciles.
    ami_manager = AmiListenerManager()
    asyncio.create_task(ami_manager.apply(cfg))
    _schedule_pbx_refresh(cfg, logger)

    # 3. Register config update callback: reload config, then refresh filler clips
    # for any combos whose voice/provider/phrases changed (content-keyed → only
    # changed combos re-synth; unused combos are pruned).
    def _on_config_changed(data: dict) -> None:
        cfg.load(data)
        _start_pin_seed(logger)
        asyncio.create_task(prewarm_fillers(cfg))
        asyncio.create_task(ami_manager.apply(cfg))
        _schedule_pbx_refresh(cfg, logger)

    mgmt.on_config_changed = _on_config_changed

    # Dashboard duplex sessions: the proxy opens them over the management
    # socket; the manager dials back the per-session engine socket.
    from duplex import DuplexManager
    duplex_manager = DuplexManager(cfg)
    mgmt.on_duplex_open = duplex_manager.handle_open

    # Reconfigure log level from config
    log_level = getattr(logging, cfg.log_level, logging.INFO)
    logging.getLogger().setLevel(log_level)
    _file_handler.setLevel(log_level)

    # 4. Start HTTP API for outbound calls + the call-registry registration
    # endpoint (/v1/calls/register) + the Twilio signaling surface (inbound
    # webhook, media WS, status callbacks). The same aiohttp app serves all.
    api = OutboundCallAPI(_call_manager, cfg)
    from calls.twilio_http import TwilioCallAPI
    twilio_api = TwilioCallAPI(
        cfg, _call_manager,
        run_call=lambda conn, uuid, route, is_outbound, caller_info: _run_call(
            conn, uuid, route, is_outbound, caller_info, cfg, logger),
        active_count=lambda: len(_active_calls),
        release_warmup=api._close_warmup_client,
    )
    api.attach_twilio(twilio_api)
    await api.start(port=cfg.http_api_port)

    # 5. Start AudioSocket TCP server
    server = await asyncio.start_server(
        lambda r, w: _handle_connection(r, w, cfg, logger, ami_manager),
        host=cfg.audiosocket_host,
        port=cfg.audiosocket_port,
    )

    addrs = [str(s.getsockname()) for s in server.sockets]
    logger.info(f"Phone Server listening on {', '.join(addrs)}")
    logger.info(f"  HTTP API: port {cfg.http_api_port}")
    logger.info(f"  Idle timeout: {cfg.idle_timeout_s}s")

    # Periodically reclaim terminal outbound-call records (phone-mcp polls
    # with wait=False, so they're never cleaned up on the request path).
    async def _gc_loop() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                _call_manager.gc_terminal()
            except Exception as e:
                logger.warning(f"call GC error: {e}")

    asyncio.create_task(_gc_loop())

    # Handle graceful shutdown
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    async with server:
        await stop.wait()

    # Stop management WS
    mgmt.stop()
    mgmt_task.cancel()

    # Stop HTTP API
    await api.stop()

    # Cancel active calls
    logger.info(f"Shutting down {len(_active_calls)} active call(s)...")
    for uuid, task in _active_calls.items():
        task.cancel()
    if _active_calls:
        await asyncio.gather(*_active_calls.values(), return_exceptions=True)

    # After the call gather — each call's finally still unregisters cleanly
    # (unregister_call is a no-op on a stopped manager either way).
    await ami_manager.stop()

    logger.info("Phone Server stopped")


def main() -> None:
    refused = config.plaintext_proxy_refusal(config.PROXY_URL)
    if refused:
        sys.stderr.write(f"Phone Server not started: {refused}\n")
        raise SystemExit(2)
    asyncio.run(_run_server())


if __name__ == "__main__":
    main()
