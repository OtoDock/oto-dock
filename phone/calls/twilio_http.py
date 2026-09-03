"""Twilio call signaling on the phone daemon's HTTP API.

Three endpoints, all fail-closed on X-Twilio-Signature (the bearer
middleware exempts ``/v1/twilio/`` — these authenticate as Twilio, not as
platform callers):

* ``POST /v1/twilio/inbound/{server_id}`` — the number's VoiceUrl. Validates
  the signature, resolves the route by (server, To DID), mints a single-use
  session token, and answers TwiML ``<Connect><Stream>`` pointing Twilio's
  media WSS at us with the token as a custom ``<Parameter>`` (the stream URL
  itself cannot carry query strings).
* ``GET /v1/twilio/media/{server_id}`` — the media WebSocket Twilio opens.
  Validates the signature on the upgrade request, reads the ``start``
  handshake, resolves the session token (outbound registry first, then the
  pending-inbound store), and runs the shared per-call body over a
  ``TwilioMediaStreamConnection``.
* ``POST /v1/twilio/status/{server_id}`` — outbound call-status callbacks
  (``?u={audio_uuid}`` correlates; the query string is part of what Twilio
  signs). Terminal states apply only while the call is still DIALING.

The daemon sits behind the operator's TLS reverse proxy; Twilio signed the
PUBLIC URL, so validation reconstructs it from the server's configured
``public_base_url`` + the request's path/query — the reverse proxy must pass
paths through verbatim.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from aiohttp import web

from calls.call_manager import CallStatus
from config_manager import normalize_did
from proxy.client import report_call
from telephony.twilio_media import (
    TwilioMediaError,
    TwilioMediaStreamConnection,
    read_stream_start,
)

logger = logging.getLogger("twilio_http")

#: TTL for a minted inbound session token — TwiML executes immediately and
#: the media WS lands within ~1-2 s; 60 s is generous.
_PENDING_TTL_S = 60.0
#: CallSid replay-dedupe window (Twilio signatures carry no timestamp).
_CALLSID_TTL_S = 300.0
#: Claimed outbound uuids — a duplicate media WS for the same call must be
#: refused. Expiry only bounds the set's size; DIALING-state checks do the
#: real gating.
_CLAIM_TTL_S = 700.0

_TWIML_STREAM = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Response><Connect>'
    '<Stream url="{stream_url}">'
    '<Parameter name="session" value="{token}"/>'
    "</Stream></Connect></Response>"
)
_TWIML_REJECT = (
    '<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>'
)
_TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response/>'


def normalize_public_base_url(raw: str) -> str:
    """Canonical ``https://host[:port]`` or ``""`` when unusable.

    Path components are rejected: the daemon reconstructs signed URLs as
    ``base + request.path``, which only holds when the reverse proxy passes
    paths through verbatim with no prefix. (The proxy-side adapter applies
    the same rule — the VoiceUrl it writes and the URL validated here must
    be byte-identical.)
    """
    raw = (raw or "").strip().rstrip("/")
    if not raw.lower().startswith("https://"):
        return ""
    parts = urlsplit(raw)
    if not parts.netloc or parts.path or parts.query or parts.fragment:
        return ""
    return f"https://{parts.netloc}"


def compute_signature(auth_token: str, url: str, params: dict) -> str:
    """The X-Twilio-Signature scheme: HMAC-SHA1 over the full URL with the
    alphabetically-sorted POST params concatenated (name+value, no
    delimiter), base64-encoded."""
    payload = url + "".join(k + v for k, v in sorted(params.items()))
    digest = hmac.new(
        auth_token.encode(), payload.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _signature_matches(auth_token: str, header: str, candidates: list[str],
                       params: dict) -> bool:
    if not auth_token or not header:
        return False
    return any(
        hmac.compare_digest(compute_signature(auth_token, url, params), header)
        for url in candidates
    )


def validate_webhook_signature(request: web.Request, form: dict,
                               server_cfg: dict) -> bool:
    base = normalize_public_base_url(server_cfg.get("public_base_url", ""))
    if not base:
        return False
    header = request.headers.get("X-Twilio-Signature", "")
    netloc = urlsplit(base).netloc
    return _signature_matches(
        server_cfg.get("auth_token", ""), header,
        [f"https://{netloc}{request.path_qs}"], form,
    )


def validate_upgrade_signature(request: web.Request, server_cfg: dict) -> bool:
    """The media WS upgrade GET is signed too. The docs leave the exact
    scheme/trailing-slash form under-specified, so all four variants are
    tried (constant-time each, no params on a GET)."""
    base = normalize_public_base_url(server_cfg.get("public_base_url", ""))
    if not base:
        return False
    header = request.headers.get("X-Twilio-Signature", "")
    netloc = urlsplit(base).netloc
    candidates = [
        f"{scheme}://{netloc}{request.path_qs}{suffix}"
        for scheme in ("wss", "https")
        for suffix in ("", "/")
    ]
    return _signature_matches(
        server_cfg.get("auth_token", ""), header, candidates, {},
    )


class _TtlStore:
    """Tiny TTL map (event-loop only, swept on access)."""

    def __init__(self, ttl_s: float):
        self._ttl = ttl_s
        self._items: dict[str, tuple[float, object]] = {}

    def _sweep(self) -> None:
        now = time.monotonic()
        for key in [k for k, (exp, _) in self._items.items() if exp <= now]:
            del self._items[key]

    def put(self, key: str, value: object = None) -> None:
        self._sweep()
        self._items[key] = (time.monotonic() + self._ttl, value)

    def pop(self, key: str):
        """Single-use take; None when absent/expired."""
        self._sweep()
        entry = self._items.pop(key, None)
        return entry[1] if entry else None

    def seen(self, key: str) -> bool:
        """Register-if-new; True when the key was already present."""
        self._sweep()
        if key in self._items:
            return True
        self._items[key] = (time.monotonic() + self._ttl, None)
        return False


def build_stream_twiml(public_base_url: str, server_id, token: str) -> str:
    netloc = urlsplit(normalize_public_base_url(public_base_url)).netloc
    return _TWIML_STREAM.format(
        stream_url=f"wss://{netloc}/v1/twilio/media/{server_id}",
        token=token,
    )


def _twiml(text: str, status: int = 200) -> web.Response:
    return web.Response(text=text, status=status, content_type="text/xml")


class TwilioCallAPI:
    """The Twilio signaling handlers, wired onto the daemon's aiohttp app.

    Injected collaborators keep this module cycle-free: ``run_call`` is the
    shared per-call body (main.py), ``active_count`` reads the live-call
    registry, ``release_warmup`` is OutboundCallAPI's warmup-client teardown.
    """

    def __init__(self, cfg, call_manager, *, run_call, active_count,
                 release_warmup):
        self.cfg = cfg
        self.call_manager = call_manager
        self._run_call = run_call
        self._active_count = active_count
        self._release_warmup = release_warmup
        self._pending = _TtlStore(_PENDING_TTL_S)
        self._recent_call_sids = _TtlStore(_CALLSID_TTL_S)
        self._claimed_uuids = _TtlStore(_CLAIM_TTL_S)
        # Webhook + status share one budget; the media WS upgrade is exempt
        # (signature-authenticated, bounded by max_live_calls) so a callback
        # burst can never 429 a live call's media socket.
        from calls.http_api import _RateLimiter
        self._limiter = _RateLimiter(cfg.rate_limit_twilio_per_min, 60.0)

    def register_routes(self, app: web.Application) -> None:
        app.router.add_post("/v1/twilio/inbound/{server_id}", self.inbound)
        app.router.add_get("/v1/twilio/media/{server_id}", self.media)
        app.router.add_post("/v1/twilio/status/{server_id}", self.status)

    # -- inbound webhook ------------------------------------------------------

    async def inbound(self, request: web.Request) -> web.Response:
        server_id = request.match_info["server_id"]
        server_cfg = self.cfg.twilio_server(server_id)
        if not server_cfg:
            return web.json_response({"error": "unknown server"}, status=403)
        form = {k: v for k, v in (await request.post()).items()
                if isinstance(v, str)}
        if not validate_webhook_signature(request, form, server_cfg):
            logger.warning(f"twilio inbound: bad signature (server {server_id})")
            return web.json_response({"error": "bad signature"}, status=403)
        # AFTER the signature check: the limiter budget is shared and
        # fixed-window, so unsigned garbage consuming it first would let an
        # internet flood starve genuine signed Twilio webhooks into 429s
        # (all inbound calls fail while the flood runs). An HMAC per bogus
        # request is the cheaper denial to absorb.
        if not self._limiter.allow():
            return web.json_response({"error": "rate limit exceeded"}, status=429)

        call_sid = form.get("CallSid", "")
        if call_sid and self._recent_call_sids.seen(call_sid):
            # Replayed signed POST (signatures carry no timestamp) — one
            # TwiML answer per CallSid.
            logger.warning(f"twilio inbound: replayed CallSid {call_sid[-8:]}")
            return _twiml(_TWIML_REJECT)

        to = form.get("To", "")
        route = self.cfg.resolve_inbound_route_by_did(server_id, to)
        if route is None:
            logger.warning(
                f"twilio inbound: no route for {normalize_did(to)!r} "
                f"on server {server_id} — rejecting"
            )
            return _twiml(_TWIML_REJECT)
        if self._active_count() >= self.cfg.max_live_calls:
            logger.warning("twilio inbound: at capacity — rejecting")
            # Rejected callers still get a call-log row (route + caller are
            # both known here; the media stream never opens).
            now = datetime.now(timezone.utc).isoformat()
            asyncio.get_running_loop().create_task(report_call({
                "route_id": route.id or "",
                "phone_server_id": route.phone_server_id,
                "agent": route.agent or "",
                "direction": "inbound",
                "from_number": form.get("From", ""),
                "to_number": form.get("To", ""),
                "transport": "twilio",
                "call_uuid": call_sid,
                "outcome": "rejected_capacity",
                "pin_attempts": 0,
                "started_at": now,
                "ended_at": now,
                "duration_s": 0,
            }))
            return _twiml(_TWIML_REJECT)

        token = secrets.token_urlsafe(24)
        caller_info = {
            "phone": form.get("From", ""),
            "did": form.get("To", ""),
            "source": "phone-twilio",
            "dial_event": {
                k.lower(): form[k]
                for k in ("CallSid", "AccountSid", "FromCity", "FromState",
                          "FromCountry", "CallerName")
                if form.get(k)
            },
        }
        self._pending.put(token, (route, caller_info))
        logger.info(
            f"twilio inbound: call {call_sid[-8:]} → route {route.id} "
            f"(agent={route.agent})"
        )
        return _twiml(build_stream_twiml(
            server_cfg["public_base_url"], server_id, token,
        ))

    # -- media websocket ------------------------------------------------------

    async def media(self, request: web.Request) -> web.StreamResponse:
        server_id = request.match_info["server_id"]
        server_cfg = self.cfg.twilio_server(server_id)
        if not server_cfg or not validate_upgrade_signature(request, server_cfg):
            logger.warning(f"twilio media: bad upgrade signature (server {server_id})")
            return web.json_response({"error": "bad signature"}, status=403)

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        try:
            start = await read_stream_start(ws)
        except TwilioMediaError as e:
            logger.warning(f"twilio media: handshake failed: {e}")
            await ws.close()
            return ws

        fmt = start.get("mediaFormat") or {}
        if fmt and ("mulaw" not in str(fmt.get("encoding", "mulaw"))
                    or int(fmt.get("sampleRate") or 8000) != 8000):
            logger.error(f"twilio media: unexpected mediaFormat {fmt} — closing")
            await ws.close()
            return ws

        token = str((start.get("customParameters") or {}).get("session", ""))
        stream_sid = str(start.get("streamSid", ""))
        call_sid = str(start.get("callSid", ""))
        route = None
        is_outbound = False
        caller_info: dict = {}

        outbound_call = self.call_manager.get_call_by_uuid(token) if token else None
        if outbound_call is not None:
            # Claimed single-use: a duplicate WS for the same call, or one
            # arriving after the watchdog failed it, must not attach a
            # second/ghost pipeline.
            if (outbound_call.status != CallStatus.DIALING
                    or self._claimed_uuids.seen(token)):
                logger.warning(
                    f"twilio media: refusing outbound attach "
                    f"(status={outbound_call.status.value})"
                )
                await ws.close()
                return ws
            is_outbound = True
            if outbound_call.route_id:
                route = self.cfg.get_outbound_route(outbound_call.route_id)
            else:
                route = self.cfg.get_default_outbound_route()
        elif token:
            entry = self._pending.pop(token)
            if entry:
                route, caller_info = entry

        if route is None:
            logger.warning("twilio media: unknown/expired session token — closing")
            await ws.close()
            return ws

        conn = TwilioMediaStreamConnection(
            ws, stream_sid=stream_sid, call_sid=call_sid,
        )
        # The handler must not return until the call ends — aiohttp tears the
        # websocket down when the handler returns.
        await self._run_call(conn, token, route, is_outbound, caller_info)
        return ws

    # -- status callbacks (outbound) ------------------------------------------

    async def status(self, request: web.Request) -> web.Response:
        server_id = request.match_info["server_id"]
        server_cfg = self.cfg.twilio_server(server_id)
        if not server_cfg:
            return web.json_response({"error": "unknown server"}, status=403)
        form = {k: v for k, v in (await request.post()).items()
                if isinstance(v, str)}
        if not validate_webhook_signature(request, form, server_cfg):
            logger.warning(f"twilio status: bad signature (server {server_id})")
            return web.json_response({"error": "bad signature"}, status=403)
        # Limiter AFTER the signature check — see inbound() for the rationale.
        if not self._limiter.allow():
            return web.json_response({"error": "rate limit exceeded"}, status=429)

        uuid = request.query.get("u", "")
        call = self.call_manager.get_call_by_uuid(uuid) if uuid else None
        call_status = form.get("CallStatus", "")
        # Terminal transitions apply ONLY while still DIALING: a late or
        # out-of-order callback must never fail a live CONNECTED call, and
        # anything after the pipeline attached is the pipeline's to finalize.
        if call is not None and call.status == CallStatus.DIALING:
            if call_status in ("busy", "no-answer", "failed", "canceled"):
                logger.info(f"twilio status: {call.call_id} → {call_status}")
                self.call_manager.update_status(
                    call.call_id, CallStatus.FAILED, error=call_status,
                )
                await self._release_warmup(call)
            elif call_status == "completed":
                # Answered-and-ended before the media WS ever connected.
                logger.info(
                    f"twilio status: {call.call_id} completed before media"
                )
                self.call_manager.update_status(
                    call.call_id, CallStatus.FAILED,
                    error="call ended before media connected",
                )
                await self._release_warmup(call)
        return _twiml(_TWIML_EMPTY)


async def originate_twilio_call(cfg, route, call, server_cfg) -> None:
    """Place an outbound call via the Twilio REST API (the AMI Originate
    twin). The inline TwiML routes the answered call straight into our media
    WSS with the call's ``audio_uuid`` as the session Parameter; status
    callbacks correlate through ``?u={audio_uuid}``. Raises on any vendor
    refusal — the caller maps it onto the standard FAILED path."""
    from telephony.twilio_rest import TwilioRestClient

    base = normalize_public_base_url(server_cfg.get("public_base_url", ""))
    if not base:
        raise ValueError("Twilio server has no usable public_base_url")
    server_id = server_cfg.get("id") or route.phone_server_id
    client = TwilioRestClient(
        server_cfg["account_sid"], server_cfg["auth_token"],
    )
    await client.create_call(
        to=call.phone_number,
        from_=route.ami_caller_id or "",
        twiml=build_stream_twiml(base, server_id, call.audio_uuid),
        # Ring bound: Twilio adds ~5 s to this and the clock starts after
        # REST + carrier setup — the Twilio-path dial watchdog runs at 75 s
        # so it stays the fallback, never the first to fire.
        timeout_s=45,
        status_callback=f"{base}/v1/twilio/status/{server_id}?u={call.audio_uuid}",
    )
