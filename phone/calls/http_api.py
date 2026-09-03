"""HTTP API for outbound call management.

Lightweight aiohttp server that exposes endpoints for registering
and monitoring outbound AI calls. Runs alongside the AudioSocket
TCP server.
"""

import asyncio
import contextlib
import hmac
import logging
import math
import re
import time

from aiohttp import web

import config
from telephony.ami_client import AMIClient, AMIError
from calls.call_manager import CallManager, CallStatus
from calls.call_registry import registry as call_registry
from config_manager import ConfigManager
from validation import ValidationError, validate_outbound_call, validate_register

logger = logging.getLogger("http_api")


class _RateLimiter:
    """Fixed-window rate limiter (per endpoint, process-wide).

    Runs entirely in the aiohttp event loop, so no locking is needed. The hit
    list is trimmed on every check and never grows past the limit.
    """

    def __init__(self, max_per_window: int, window_s: float):
        self.max = max_per_window
        self.window = window_s
        self._hits: list[float] = []

    def allow(self) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        self._hits = [t for t in self._hits if t > cutoff]
        if len(self._hits) >= self.max:
            return False
        self._hits.append(now)
        return True


def _bearer_token(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.startswith("Bearer ") else ""


def _auth_middleware_factory(cfg: ConfigManager):
    """Build the auth middleware, closed over the live ``ConfigManager``.

    Two surfaces, both FAIL-CLOSED (a missing/empty secret → 401, never open):
      * ``/v1/calls/register`` — the Asterisk dialplan. Authenticated against the
        per-server ``cfg.register_secrets`` set (any match revokes-on-delete via
        the proxy dropping it from the pushed list). ``cfg`` is the live manager
        whose ``load()`` replaces ``cfg._data`` in place, so an added/rotated/
        revoked secret takes effect on the next request with no re-wiring.
      * everything else (``/api/calls`` + its sub-paths) — phone-mcp.
        Authenticated against the telephony-wide ``config.PHONE_API_SECRET``.
    ``/health`` is the only open endpoint.
    """

    @web.middleware
    async def _auth_middleware(request: web.Request, handler):
        if request.path == "/health":
            return await handler(request)
        if request.path.startswith("/v1/twilio/"):
            # Twilio cannot send platform bearer tokens; these handlers
            # authenticate every request themselves via X-Twilio-Signature
            # (fail-closed — see calls/twilio_http.py).
            return await handler(request)
        token = _bearer_token(request)
        if request.path == "/v1/calls/register":
            ok = bool(token) and any(
                hmac.compare_digest(token, s) for s in cfg.register_secrets
            )
        else:
            expected = config.PHONE_API_SECRET
            ok = bool(token) and bool(expected) and hmac.compare_digest(token, expected)
        if not ok:
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    return _auth_middleware


class OutboundCallAPI:
    """HTTP API for outbound call management."""

    def __init__(self, call_manager: CallManager, cfg: ConfigManager):
        self.call_manager = call_manager
        self.cfg = cfg
        self._calls_limiter = _RateLimiter(cfg.rate_limit_calls_per_min, 60.0)
        self._register_limiter = _RateLimiter(cfg.rate_limit_register_per_min, 60.0)
        self._app = web.Application(middlewares=[_auth_middleware_factory(cfg)])
        self._app.router.add_post("/api/calls", self._create_call)
        self._app.router.add_get("/api/calls/{call_id}", self._get_call)
        self._app.router.add_get("/api/calls/{call_id}/wait", self._wait_for_event)
        self._app.router.add_post("/api/calls/{call_id}/answer", self._post_answer)
        # Generic call-metadata registration. Phone drivers (Asterisk
        # dialplan via System(curl), 3CX webhook adapter, Twilio webhook
        # adapter, etc.) POST here BEFORE the AudioSocket TCP connection
        # lands. The phone server then looks up the cache by
        # audiosocket_uuid and forwards phone/did/dial_event into the
        # proxy warmup so ``${trigger.*}`` tokens resolve.
        self._app.router.add_post("/v1/calls/register", self._register_call)
        self._app.router.add_get("/health", self._health)
        self._runner: web.AppRunner | None = None

    def attach_twilio(self, twilio_api) -> None:
        """Register the Twilio signaling routes (webhook + media WS + status)
        on this app. Must run before ``start()``."""
        twilio_api.register_routes(self._app)

    async def start(self, port: int | None = None) -> None:
        """Start the HTTP API server."""
        port = port or self.cfg.http_api_port
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.cfg.http_api_host, port)
        await site.start()
        logger.info(f"HTTP API listening on port {port}")
        # Both call surfaces are fail-closed: a missing secret rejects (401), it
        # does not open the endpoint. Warn loudly so a misprovisioned install is
        # visible rather than silently rejecting every call.
        if not config.PHONE_API_SECRET:
            logger.warning(
                "PHONE_API_SECRET is not set — /api/calls will reject all "
                "requests (401). Restart the proxy to generate it."
            )
        if not self.cfg.register_secrets:
            logger.warning(
                "No register secrets configured — /v1/calls/register will reject "
                "all requests (401) until a phone server is provisioned."
            )

    async def stop(self) -> None:
        """Stop the HTTP API server."""
        if self._runner:
            await self._runner.cleanup()

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "phone-server"})

    async def _register_call(self, request: web.Request) -> web.Response:
        """POST /v1/calls/register — pre-register inbound call metadata.

        Body:
            {
              "audiosocket_uuid": "<uuid>",   REQUIRED
              "phone": "<caller E.164>",      optional
              "did":   "<dialed number>",     optional
              "source": "phone-freepbx",      optional, defaults "phone"
              "dial_event": { ... }           optional, raw driver payload
            }

        Returns 200 ``{"status": "registered"}`` on success, 400 if the
        UUID is missing. Idempotent — re-registering overwrites. Requires the
        caller's per-server register secret as a ``Bearer`` token (the dashboard
        snippet embeds it); the auth middleware rejects a missing/unknown token
        with 401 before this handler runs.
        """
        if not self._register_limiter.allow():
            return web.json_response({"error": "rate limit exceeded"}, status=429)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        try:
            fields = validate_register(body)
        except ValidationError as e:
            return web.json_response({"error": str(e)}, status=400)

        call_registry.register(
            fields["audiosocket_uuid"],
            phone=fields["phone"],
            did=fields["did"],
            source=fields["source"],
            dial_event=fields["dial_event"],
        )
        return web.json_response({"status": "registered"})

    async def _create_call(self, request: web.Request) -> web.Response:
        """POST /api/calls — Register and originate an outbound call.

        Body: {
            "phone_number": "+30...",
            "task_description": "Reserve table for 4...",
            "instructions": "Optional extra instructions",
            "route_id": "optional-route-uuid",
            "wait": true  // block until call completes
        }
        """
        if not self._calls_limiter.allow():
            return web.json_response({"error": "rate limit exceeded"}, status=429)

        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "invalid JSON"}, status=400,
            )

        try:
            fields = validate_outbound_call(body)
        except ValidationError as e:
            return web.json_response({"error": str(e)}, status=400)

        phone_number = fields["phone_number"]
        task_description = fields["task_description"]
        instructions = fields["instructions"]
        route_id = fields["route_id"]
        wait = bool(body.get("wait", False))

        # Resolve outbound route
        if route_id:
            route = self.cfg.get_outbound_route(route_id)
        else:
            route = self.cfg.get_default_outbound_route()

        if not route:
            return web.json_response(
                {"error": "no outbound route configured"}, status=400,
            )

        if self.call_manager.active_count() >= self.cfg.max_outbound_calls:
            return web.json_response(
                {"error": "too many concurrent outbound calls"}, status=429,
            )

        # Register the call
        call = self.call_manager.register_call(
            phone_number=phone_number,
            task_description=task_description,
            instructions=instructions,
        )
        # Attach route_id for resolution when AudioSocket connects
        call.route_id = route_id

        # Pre-warm the caller agent session on the proxy
        warmup_task = asyncio.create_task(
            self._warmup_caller_session(call.call_id, route)
        )

        # Originate: Twilio REST when the route's server is a Twilio row,
        # else AMI. The Twilio ring timeout is 45 s (+ Twilio's ~5 s buffer,
        # clock starts after REST + carrier setup), so its watchdog runs at
        # 75 s — status callbacks are the primary no-answer path there and
        # the watchdog is only the fallback.
        twilio_server = self.cfg.twilio_server(
            getattr(route, "phone_server_id", None))
        # One grep answers "which route/adapter did this call use" — the
        # 2026-08-14 live test burned an hour proving a misconfigured MCP
        # instance had picked the FreePBX route instead of the Twilio one.
        logger.info(
            f"Call {call.call_id}: route {route.id or '(default)'} "
            f"(server {getattr(route, 'phone_server_id', None)}, "
            f"{'twilio' if twilio_server else 'ami'})"
        )
        try:
            if twilio_server:
                from calls.twilio_http import originate_twilio_call
                await originate_twilio_call(self.cfg, route, call, twilio_server)
                self.call_manager.update_status(call.call_id, CallStatus.DIALING)
            else:
                await self._originate_call(call.call_id, route)
        except Exception as e:
            logger.error(f"Originate failed for {call.call_id}: {e}")
            self.call_manager.update_status(
                call.call_id, CallStatus.FAILED, error=str(e),
            )
            # Let warmup unwind (its finally closes an unstored client), then
            # release the client if it was already stored — no leak either way.
            warmup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await warmup_task
            await self._close_warmup_client(call)
            return web.json_response(call.to_dict(), status=502)

        # Start dial timeout watchdog
        asyncio.create_task(
            self._dial_timeout_watchdog(
                call.call_id, timeout=75.0 if twilio_server else 45.0,
            )
        )

        if wait:
            # Block until call completes or times out
            call = await self.call_manager.wait_for_completion(
                call.call_id,
                timeout=self.cfg.outbound_call_timeout_s,
            )
            result = call.to_dict()
            # Clean up after returning
            self.call_manager.cleanup_call(call.call_id)
            return web.json_response(result)
        else:
            return web.json_response(call.to_dict(), status=202)

    async def _get_call(self, request: web.Request) -> web.Response:
        """GET /api/calls/{call_id} — Get call status and transcript."""
        call_id = request.match_info["call_id"]
        call = self.call_manager.get_call(call_id)
        if not call:
            return web.json_response(
                {"error": "call not found"}, status=404,
            )
        return web.json_response(call.to_dict())

    async def _wait_for_event(self, request: web.Request) -> web.Response:
        """GET /api/calls/{call_id}/wait?timeout=300 — Long-poll for events."""
        call_id = request.match_info["call_id"]
        call = self.call_manager.get_call(call_id)
        if not call:
            return web.json_response({"error": "call not found"}, status=404)

        try:
            timeout = float(request.query.get("timeout", "300"))
            if math.isnan(timeout) or timeout < 0:
                timeout = 300.0
        except (TypeError, ValueError):
            timeout = 300.0
        timeout = min(timeout, 360.0)
        result = await self.call_manager.wait_for_event(call_id, timeout=timeout)
        return web.json_response(result)

    async def _post_answer(self, request: web.Request) -> web.Response:
        """POST /api/calls/{call_id}/answer — Send an answer to a pending question."""
        call_id = request.match_info["call_id"]
        call = self.call_manager.get_call(call_id)
        if not call:
            return web.json_response({"error": "call not found"}, status=404)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        answer = body.get("answer")
        if not answer:
            return web.json_response({"error": "answer is required"}, status=400)

        if not self.call_manager.post_answer(call_id, answer):
            return web.json_response(
                {"error": "no pending question for this call"}, status=409,
            )

        return web.json_response({"status": "answer_delivered", "call_id": call_id})

    async def _originate_call(self, call_id: str, route) -> None:
        """Connect to AMI and originate the call."""
        call = self.call_manager.get_call(call_id)
        if not call:
            raise AMIError(f"Unknown call_id: {call_id}")

        # AMI coordinates: the route's own server (mixed-provider installs,
        # via the pushed per-server map) beats the flat default-server
        # settings (single-Asterisk compat — the flat keys are what released
        # daemons already read).
        per_server = self.cfg.server_ami(getattr(route, "phone_server_id", None))
        ami = AMIClient(
            host=per_server["host"] if per_server else self.cfg.ami_host,
            port=per_server["port"] if per_server else self.cfg.ami_port,
            username=per_server["username"] if per_server else self.cfg.ami_username,
            secret=per_server["secret"] if per_server else self.cfg.ami_secret,
        )
        try:
            await ami.connect()
            await ami.login()
            caller_id = route.ami_caller_id or ""
            context = route.ami_outbound_context or "oto-audiosocket-outbound"
            response = await ami.originate(
                phone_number=call.phone_number,
                audio_uuid=call.audio_uuid,
                caller_id=caller_id,
                context=context,
                dial_prefix=route.dial_prefix or "",
                timeout_ms=30000,
            )
            if "Success" not in response.get("Response", ""):
                raise AMIError(
                    f"Originate rejected: {response.get('Message', 'unknown')}"
                )
            self.call_manager.update_status(call_id, CallStatus.DIALING)
        finally:
            await ami.close()

    async def _warmup_caller_session(self, call_id: str, route) -> None:
        """Pre-warm the caller agent and pre-generate the opening via WebSocket.

        Uses a temporary ProxyClient WS connection to:
        1. Create a session (warmup)
        2. Stream the opening text from the LLM

        Runs during AMI origination (10-30s ringing = plenty of time).
        The session_id and opening text are stored on the OutboundCall
        object for the pipeline to pick up when AudioSocket connects.
        """
        from proxy.client import ProxyClient

        call = self.call_manager.get_call(call_id)
        if not call:
            return

        # Connection that pre-warms the caller session AND generates the opening.
        # On success we KEEP it (call.warmup_client) so the live pipeline reuses
        # this exact connection: the opening + task context live in this proxy
        # session, and tearing it down is what made answered outbound calls
        # forget why they called. caller_info also threads trigger_slug /
        # ${trigger.*} enrichment into the OPENING, not just later turns.
        client = ProxyClient(
            model=route.agent,
            call_type="outbound",
            phone_context_override=route.phone_context_override or "",
            phone_route_id=route.id or "",
            caller_phone=call.phone_number or "",
            caller_did=route.ami_caller_id or "",
            dial_event={
                "task_description": call.task_description or "",
                "call_id": call.call_id,
                "phase": "prewarmup",
            },
        )
        stored = False  # True once the client is handed to the call for reuse
        try:
            ws_ok = await client.connect()
            if not ws_ok:
                logger.warning(f"Caller pre-warmup: WS connect failed for call {call_id}")
                return

            # Step 1: Warmup session
            session_id = await client.warmup_session()
            if not session_id:
                logger.warning(f"Caller pre-warmup: warmup failed for call {call_id}")
                return

            call.warmup_session_id = session_id
            logger.info(
                f"Caller session pre-warmed for call {call_id} "
                f"(session={session_id})"
            )

            # Step 2: Pre-generate the opening text via LLM
            task_desc = call.task_description
            instructions = call.instructions or ""
            extra = f" Instructions: {instructions}" if instructions else ""

            opening_prompt = (
                f"Task: {task_desc}.{extra}\n"
                f"The person just answered the phone. "
                f"Introduce yourself briefly and begin working on the task."
            )

            full_response: list[str] = []
            async for chunk in client.send_message(opening_prompt):
                if chunk is not None and isinstance(chunk, str):
                    full_response.append(chunk)

            raw_text = "".join(full_response)

            # If the agent ended the call in its first turn (a "say X then hang
            # up" task), the opening carries [CALL_COMPLETE]. Record that BEFORE
            # stripping so the pipeline hangs up after playing the opening.
            call.opening_completes_call = bool(
                re.search(r"\[CALL_COMPLETE\]", raw_text)
            )

            # Clean for TTS: strip session markers and [CALL_COMPLETE]
            clean = re.sub(
                r"\[//\]: # \(session:[a-f0-9-]+\)", "", raw_text,
            )
            clean = re.sub(r"\[CALL_COMPLETE\]", "", clean).strip()

            if clean:
                call.opening_text = clean
                call.opening_prompt = opening_prompt
                # Hand the live connection (session + opening) to the call for
                # the pipeline to reuse; do NOT close it here.
                call.warmup_client = client
                stored = True
                logger.debug(
                    f"Pre-generated opening for call {call_id} "
                    f"({len(clean)} chars): {clean[:100]}..."
                )
            else:
                logger.warning(
                    f"Pre-generated opening was empty for call {call_id}"
                )

        except Exception as e:
            logger.warning(f"Caller warmup/opening error: {e}")
        finally:
            # If the client wasn't handed to the call (failure / empty opening /
            # cancellation), close it so the proxy session + socket don't leak.
            if not stored:
                with contextlib.suppress(Exception):
                    await client.close()
            call._opening_ready.set()

    async def _close_warmup_client(self, call) -> None:
        """Tear down a pre-warmed caller client that no live pipeline adopted.

        The pipeline nulls ``call.warmup_client`` when it adopts the connection,
        so this only fires for calls that were never answered / failed.
        """
        client = getattr(call, "warmup_client", None)
        if client is not None:
            call.warmup_client = None
            with contextlib.suppress(Exception):
                await client.close()

    async def _dial_timeout_watchdog(
        self,
        call_id: str,
        timeout: float = 45.0,
    ) -> None:
        """Mark call as FAILED if no AudioSocket connects within timeout."""
        await asyncio.sleep(timeout)
        call = self.call_manager.get_call(call_id)
        if call and call.status == CallStatus.DIALING:
            logger.warning(f"Call {call_id} dial timeout ({timeout}s) — no answer")
            self.call_manager.update_status(
                call_id, CallStatus.FAILED,
                error="no answer (dial timeout)",
            )
            # No live call will adopt the pre-warmed session — release it.
            await self._close_warmup_client(call)
