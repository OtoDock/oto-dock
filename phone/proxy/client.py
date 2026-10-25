"""WebSocket client for the Claude Code Proxy API.

Uses a persistent WebSocket connection to the proxy's /ws/phone endpoint
for the entire call duration. Warmup, chat turns, and close all go through
the same connection — no per-turn HTTP overhead.

There is no other transport: a call whose socket fails to open or closes
under it is marked ``proxy_lost`` and the pipeline ends it with the apology
phrase (nothing else may answer it). The standalone reporters at the end of
the module post over HTTP on their own short-lived clients.
"""

import asyncio
import contextlib
import json
import logging

import httpx
import websockets

import config

logger = logging.getLogger("proxy_client")

# Sentinel yielded when Claude starts a tool call.
# Consumers should treat this as a "flush current TTS segment" signal.
TOOL_USE_SIGNAL = object()


class ProxyClient:
    """Client for the Claude Code Proxy, using WebSocket transport.

    Supports two LLM modes:
    - "proxy" (default): CLI subprocess path
    - "direct": Anthropic API path (lower latency)

    The WebSocket stays open for the entire call; without it the call ends.
    """

    def __init__(self, model: str = "unified", llm_mode: str = "proxy",
                 phone_mode: bool = False, call_type: str = "inbound",
                 phone_context_override: str = "",
                 audiosocket_uuid: str = "",
                 phone_route_id: str = "",
                 caller_phone: str = "",
                 caller_did: str = "",
                 dial_event: dict | None = None,
                 pin_verified: bool = False,
                 attach: dict | None = None,
                 transfer_available: bool = True):
        self.model = model
        self.llm_mode = llm_mode
        self.phone_mode = phone_mode
        self.call_type = call_type
        self.phone_context_override = phone_context_override
        # Route → trigger resolution at warmup. Empty values degrade
        # gracefully — the proxy skips trigger enrichment and the call
        # proceeds with the base prompt.
        # ``phone_route_id`` is the authoritative lookup key — works for
        # both inbound (where ``audiosocket_uuid`` would also work) AND
        # outbound (where the audiosocket UUID is per-call ephemeral and
        # doesn't match any stored route). Phone server passes it from
        # ``route.id``.
        self.audiosocket_uuid = audiosocket_uuid
        self.phone_route_id = phone_route_id
        self.caller_phone = caller_phone
        self.caller_did = caller_did
        self.dial_event = dial_event or {}
        # The daemon's PIN gate passed before this client was created. The
        # proxy honours it only on routes that actually have a PIN — it
        # upgrades the caller's identity label (call log) and marks the
        # caller's private tree as verified.
        self.pin_verified = bool(pin_verified)
        # A call that runs inside the caller's chat: ``{session_id, grant}``
        # from the proxy's relay. The warmup attaches to that session.
        self.attach = dict(attach) if attach else None
        # The proxy is gone (the socket never opened, or closed): the
        # pipeline ends the call, since nothing else may answer it. Set by
        # ``connect`` and the receive loop's end; ``on_proxy_lost`` is the
        # pipeline's handler, called once from the loop's end.
        self.proxy_lost: bool = False
        self.on_proxy_lost = None
        self._closing: bool = False
        # Whether this line can hand the caller over (a registered channel, or
        # Twilio): the warmup says so, and the prompt lists targets only then.
        self.transfer_available = bool(transfer_available)
        self.session_id: str | None = None
        self.messages: list[dict[str, str]] = []
        self._ws: websockets.ClientConnection | None = None
        self._ws_connected: bool = False
        # Barge-in tracking
        self._last_spoken_chars: int = 0
        self._barge_in_pending: bool = False
        # True while a WS turn is open (no done/error/close seen) — gates
        # _cancel_tts's upstream abort so a post-turn commit can't fire a
        # session-scoped abort at an unrelated queued turn.
        self.turn_in_flight: bool = False
        # Turn correlation: one receiver task routes frames to the current
        # turn's queue; frames from abandoned/aborted turns are dropped there
        # (see _recv_loop). Control frames (warmup) have no turn id.
        self._turn_seq: int = 0
        self._current_turn: int = 0
        self._turn_queue: asyncio.Queue | None = None
        self._control_queue: asyncio.Queue = asyncio.Queue()
        self._recv_task: asyncio.Task | None = None
        # A call tool the agent called (the proxy's ``call_control`` frame):
        # handed to the pipeline synchronously whatever its turn, never
        # dropped. Without a handler the frame is refused at once.
        self.on_call_control = None

    @property
    def current_turn(self) -> int:
        """The turn id of the open or last engine turn (the proxy echoes it)."""
        return self._current_turn

    async def connect(self) -> bool:
        """Establish persistent WebSocket to proxy.

        Returns True if connected; False marks the proxy lost, and the
        pipeline ends the call.
        """
        # Key travels in the Authorization header, never the URL — the proxy's
        # access log records the full path+query of every WS handshake.
        ws_url = (
            f"{config.PROXY_WS_SCHEME}://{config.PROXY_WS_HOST_PORT}"
            f"/ws/phone"
        )
        try:
            self._ws = await websockets.connect(
                ws_url,
                additional_headers={"Authorization": f"Bearer {config.PROXY_API_KEY}"},
                close_timeout=5,
                max_size=10 * 1024 * 1024,  # 10MB for large tool results
            )
            self._ws_connected = True
            self._recv_task = asyncio.create_task(self._recv_loop())
            logger.info("WebSocket connected to proxy at %s", config.PROXY_WS_HOST_PORT)
            return True
        except Exception as e:  # noqa: BLE001  # the call learns the proxy is unreachable whatever the connect raised
            self._ws_connected = False
            self.proxy_lost = True
            logger.warning("WebSocket connection failed, the call cannot be served: %s", e)
            return False

    async def _recv_loop(self) -> None:
        """Single WS reader: route frames to the current turn or control queue.

        Frames from stale turns (abandoned on barge-in, or aborted) are
        dropped here — this is what keeps an interrupted turn's tail from
        being spoken as the answer to the caller's next utterance. Frames
        without a ``turn`` (warmup replies, out-of-turn errors) go to the
        control queue.
        """
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "call_control":
                    self._dispatch_call_control(msg)
                    continue
                turn = msg.get("turn")
                if turn is None:
                    self._control_queue.put_nowait(msg)
                elif turn == self._current_turn and self._turn_queue is not None:
                    self._turn_queue.put_nowait(msg)
                else:
                    logger.debug(
                        "Dropping stale frame turn=%s "
                        "(current=%s): %s",
                        turn, self._current_turn, msg.get('type')
                    )
        except websockets.ConnectionClosed as e:
            logger.warning("WS connection closed: %s", e)
        except Exception as e:  # noqa: BLE001  # the receive loop's last line: the call sees the proxy lost
            logger.error("WS receive loop error: %s", e)
        finally:
            self._ws_connected = False
            closed = {"type": "_closed"}
            self._control_queue.put_nowait(closed)
            if self._turn_queue is not None:
                self._turn_queue.put_nowait(closed)
            # The daemon's own close at hangup is not a loss.
            if not self._closing:
                self.proxy_lost = True
                handler = self.on_proxy_lost
                if handler is not None:
                    try:
                        handler()
                    except Exception as e:  # noqa: BLE001  # the proxy-lost handler is the call's callback, the loop ends anyway
                        logger.error("proxy lost handler error: %s", e)

    def _dispatch_call_control(self, msg: dict) -> None:
        handler = self.on_call_control
        if handler is None:
            request_id = str((msg.get("data") or {}).get("request_id") or "")
            asyncio.get_running_loop().create_task(self.send_call_control_result(
                request_id, False, "The call is not live yet."))
            return
        try:
            handler(msg)
        except Exception as e:  # noqa: BLE001  # a call-control handler is the call's callback, the socket goes on
            logger.error("call control handler error: %s", e)

    async def send_call_control_result(self, request_id: str, ok: bool, message: str) -> None:
        """Answer one ``call_control`` frame (the tool's result text)."""
        if not (self._ws_connected and self._ws):
            return
        try:
            await self._ws.send(json.dumps({
                "type": "call_control_result",
                "data": {"request_id": request_id, "ok": bool(ok), "message": message},
            }))
        except Exception as e:  # noqa: BLE001  # the proxy socket's send raises by its state and transport
            logger.warning("WS call control result send failed: %s", e)

    def discard_turn(self) -> None:
        """Stop reading the open turn: the line closes as the farewell drains
        and whatever the engine still produces is the proxy's transcript. The
        receiver drops the turn's remaining frames."""
        self._turn_queue = None
        self.turn_in_flight = False

    async def warmup_session(self) -> str | None:
        """Pre-create a persistent session on the proxy over the socket.
        Returns the session_id, or None on failure (no socket included).
        """
        if not (self._ws_connected and self._ws):
            return None
        try:
            msg = {
                "type": "warmup",
                "model": self.model,
                "llm_mode": self.llm_mode,
                "phone_mode": self.phone_mode,
                "call_type": self.call_type,
                "phone_context_override": self.phone_context_override,
                # Route → trigger payload resolution on the proxy.
                # Empty / missing fields leave trigger_payload=None → no
                # enrichment, base prompt unchanged. Phone driver populates
                # these from its AMI / dial-event source (FreePBX, Twilio,
                # 3CX).
                "audiosocket_uuid": self.audiosocket_uuid,
                "phone_route_id": self.phone_route_id,
                "caller_phone": self.caller_phone,
                "caller_did": self.caller_did,
                "dial_event": self.dial_event,
                "pin_verified": self.pin_verified,
                # This daemon acts on ``call_control`` frames: the proxy may
                # give the session the call tools instead of the markers alone.
                "call_control": True,
                # Whether a transfer is possible on this line: the prompt
                # lists the route's targets only when it is.
                "transfer": self.transfer_available,
            }
            if self.attach:
                # The call runs inside the caller's chat: borrow that session.
                msg["attach_session_id"] = self.attach.get("session_id", "")
                msg["attach_grant"] = self.attach.get("grant", "")
            if self.session_id:
                # Include pre-warmed session_id so proxy can reuse existing session
                msg["session_id"] = self.session_id
            await self._ws.send(json.dumps(msg))
            msg = await asyncio.wait_for(self._control_queue.get(), timeout=120)
            if msg.get("type") == "warmup_ready":
                self.session_id = msg["data"]["session_id"]
                logger.info(
                    "WS warmup ready: %s "
                    "(mode=%s)",
                    self.session_id, msg['data'].get('llm_mode', self.llm_mode)
                )
                return self.session_id
            elif msg.get("type") == "error":
                logger.warning("WS warmup error: %s", msg['data'].get('message'))
                return None
            else:
                logger.warning("WS warmup unexpected response: %s", msg)
                return None
        except Exception as e:  # noqa: BLE001  # a failed warmup reads as none, the first turn warms the session
            logger.warning("WS warmup failed: %s", e)
            return None

    def set_session_id(self, session_id: str) -> None:
        """Inject an externally-obtained session_id (e.g. from pre-warmup)."""
        self.session_id = session_id
        self.messages.append({
            "role": "assistant",
            "content": f"[//]: # (session:{session_id})",
        })
        logger.info("Session ID injected: %s", session_id)

    def mark_spoken(self, char_count: int) -> None:
        """Track how many chars of the last response were played via TTS."""
        self._last_spoken_chars = char_count

    def annotate_interrupted_response(self) -> None:
        """Flag that the next send_message should include barge-in info."""
        if self._last_spoken_chars > 0:
            self._barge_in_pending = True

    async def send_system_note(self, text: str, *, resume: bool = False) -> bool:
        """A daemon-composed turn whose reply is not awaited: the resume note
        at an in-session call's end (``resume`` says the chat is its own
        again). The frame goes out in order before ``close``; the proxy's
        producer runs it after the call's last turn."""
        if not (self._ws_connected and self._ws):
            return False
        self._turn_seq += 1
        self._current_turn = self._turn_seq
        self._turn_queue = asyncio.Queue()
        frame = {"type": "chat", "prompt": text, "turn": self._turn_seq,
                 "barge_in_chars": None, "system": True}
        if resume:
            frame["resume"] = True
        try:
            await self._ws.send(json.dumps(frame))
        except Exception as e:  # noqa: BLE001  # the proxy socket's send raises by its state and transport
            logger.warning("WS system note failed: %s", e)
            return False
        return True

    async def send_message(self, text: str, *, system: bool = False):
        """Send a user message and stream back assistant text tokens.

        Yields text chunks as they arrive from the LLM. Without the socket
        it yields nothing and marks the proxy lost: the pipeline ends the
        call with the apology phrase instead of speaking an error.
        ``system`` marks a prompt the daemon composed (an opening, a hold or
        resume note): a call that runs inside a person's chat writes no user
        row for it.
        """
        if not (self._ws_connected and self._ws):
            self.proxy_lost = True
            return
        async for chunk in self._ws_send_message(text, system=system):
            yield chunk

    async def _ws_send_message(self, text: str, *, system: bool = False):
        """Send message via WebSocket and yield text chunks."""
        barge_in_chars = None
        if self._barge_in_pending and self._last_spoken_chars > 0:
            barge_in_chars = self._last_spoken_chars
            self._barge_in_pending = False
            self._last_spoken_chars = 0

        self._turn_seq += 1
        turn = self._turn_seq
        self._current_turn = turn
        # Fresh queue per turn: frames from abandoned/aborted turns die with
        # their old queue instead of bleeding into this one.
        self._turn_queue = asyncio.Queue()

        try:
            frame = {
                "type": "chat",
                "prompt": text,
                "turn": turn,
                "barge_in_chars": barge_in_chars,
            }
            if system:
                frame["system"] = True
            await self._ws.send(json.dumps(frame))
        except Exception as e:  # noqa: BLE001  # the proxy socket's send raises by its state and transport
            # The socket is broken: the receive loop ends it for the
            # pipeline; this turn says nothing.
            logger.error("WS send failed: %s", e)
            self.proxy_lost = True
            return

        full_response = []
        queue = self._turn_queue
        # Turn-liveness flag for _cancel_tts's upstream-abort gate: a commit
        # landing after the engine finished must not fire a session-scoped
        # abort that could kill an unrelated queued turn.
        self.turn_in_flight = True
        while True:
            msg = await queue.get()
            msg_type = msg.get("type", "")

            if msg_type == "_closed":
                # The receive loop marked the proxy lost: the pipeline ends
                # the call after this stream, no error speech.
                logger.error("WS connection closed during chat")
                self.turn_in_flight = False
                break
            elif msg_type == "text":
                content = msg.get("data", {}).get("content", "")
                if content:
                    full_response.append(content)
                    yield content
            elif msg_type in ("tool_start", "tool_end"):
                # EITHER tool boundary finalizes the current TTS segment.
                # Some layers have no start moment — Codex reports tool calls
                # only on completion, interactive row feeds land post-hoc —
                # so tool_end is the only boundary they ever send; without it
                # the pre-tool sentence sits unfinalized in the TTS context
                # until the turn ends. Repeats are safe: an empty buffer with
                # nothing sent since the last flush makes the signal a no-op
                # downstream.
                yield TOOL_USE_SIGNAL
            elif msg_type == "session":
                self.session_id = msg.get("data", {}).get("session_id")
            elif msg_type == "done":
                self.turn_in_flight = False
                break
            elif msg_type == "error":
                error_msg = msg.get("data", {}).get("message", "Unknown error")
                logger.error("WS chat error: %s", error_msg)
                self.turn_in_flight = False
                yield "Sorry, I encountered an error."
                break
            # Silently ignore: tool_input, thinking, metadata

        self.turn_in_flight = False
        # Reset barge-in tracking for this new response
        self._last_spoken_chars = 0

        assistant_text = "".join(full_response)
        logger.info(
            "WS response: %s chars, turn=%s, "
            "session=%s", len(assistant_text), turn, self.session_id
        )

    @property
    def supports_abort(self) -> bool:
        """Whether an in-flight turn can be cancelled server-side: over the
        socket, in both modes. Direct aborts cleanly (the API stream unwinds
        and the session pops the un-answered user message). Proxy mode runs
        the same graceful per-layer interrupt the duplex chat attach uses
        (CLI stdin control_request, Codex interrupt), keeping the partial
        turn; the daemon's turn-id filter drops the tail frames.
        """
        return self._ws_connected

    @property
    def abort_erases_turn(self) -> bool:
        """Direct-layer abort semantics: the un-answered user message is
        popped server-side — the turn "never happened" — so the pipeline
        refolds its transcript into the next dispatch. Proxy-mode aborts
        merely INTERRUPT the turn (like the duplex chat attach: the user row
        and partial reply stay in chat history with an interruption note),
        so this stays False there and the pipeline dispatches only the NEW
        speech."""
        return self.llm_mode == "direct"

    async def abort_turn(self) -> None:
        """Abort the in-flight turn (barge-in / continuation on Direct): the
        proxy cancels the turn's producer — the API stream and any in-flight
        tool unwind, and the direct session pops the un-answered user message
        so the pipeline can resend it batched with the caller's new speech.
        Callers gate on supports_abort.
        """
        if not (self._ws_connected and self._ws):
            return
        try:
            await self._ws.send(json.dumps({
                "type": "abort", "turn": self._current_turn,
            }))
        except Exception as e:  # noqa: BLE001  # the proxy socket's send raises by its state and transport
            logger.warning("WS abort send failed: %s", e)

    async def close_session(self) -> bool:
        """Close the persistent session on the proxy (call on hangup)."""
        if not self.session_id or not (self._ws_connected and self._ws):
            return False
        try:
            await self._ws.send(json.dumps({"type": "close"}))
            logger.info("WS close sent: session=%s", self.session_id)
            return True
        except Exception as e:  # noqa: BLE001  # the proxy socket's send raises by its state and transport
            logger.warning("WS close failed: %s", e)
            return False

    async def close(self) -> None:
        """Close the persistent session and transport."""
        self._closing = True
        await self.close_session()
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._recv_task
            self._recv_task = None
        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
            self._ws_connected = False


async def report_call(payload: dict) -> None:
    """Report one call's outcome row for the proxy's admin call log
    (``POST /v1/phone/calls/report`` → ``phone_call_log`` table).

    Best-effort, fire-and-forget, same contract as
    ``report_turn_classifier_usage`` below: short-lived client, tight
    timeouts, every error swallowed — a dead proxy must never disturb call
    teardown. This is the only daemon→proxy call-lifecycle report; it also
    fires for calls that never warmed up (PIN-refused, capacity-rejected),
    which is precisely what the admin wants to see."""
    try:
        async with httpx.AsyncClient(
            base_url=config.PROXY_URL,
            timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0),
        ) as http:
            await http.post(
                "/v1/phone/calls/report",
                headers={"Authorization": f"Bearer {config.PROXY_API_KEY}"},
                json=payload,
            )
    except Exception as e:  # noqa: BLE001  # call reports are best effort, the call's teardown must not see them
        logger.warning("Call report failed: %s", e)


async def report_transfer_outcome(call_uuid: str, outcome: str) -> None:
    """A transfer's Dial ended without the colleague after the call's row was
    reported as ``transferred``: the row takes ``outcome``
    (``POST /v1/phone/calls/transfer-outcome``). The same contract as
    ``report_call``: best effort, short timeouts, every error swallowed."""
    try:
        async with httpx.AsyncClient(
            base_url=config.PROXY_URL,
            timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0),
        ) as http:
            await http.post(
                "/v1/phone/calls/transfer-outcome",
                headers={"Authorization": f"Bearer {config.PROXY_API_KEY}"},
                json={"call_uuid": call_uuid, "outcome": outcome},
            )
    except Exception as e:  # noqa: BLE001  # call reports are best effort, the call's teardown must not see them
        logger.warning("Transfer outcome report failed: %s", e)


async def fetch_pin_failures(window_s: int) -> list[dict] | None:
    """The calls of the last ``window_s`` seconds that entered the PIN gate
    (``GET /v1/phone/pin-failures``: inbound, and outbound on a user route,
    keyed by the number the gate counted), for the lockout windows after
    a restart; None when the proxy did not answer (the caller tries again
    later). Same short-lived client and auth as ``report_call``."""
    try:
        async with httpx.AsyncClient(
            base_url=config.PROXY_URL,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        ) as http:
            resp = await http.get(
                "/v1/phone/pin-failures",
                params={"window_s": window_s},
                headers={"Authorization": f"Bearer {config.PROXY_API_KEY}"},
            )
            resp.raise_for_status()
            calls = resp.json().get("calls")
            return calls if isinstance(calls, list) else None
    except Exception as e:  # noqa: BLE001  # the PIN history is best effort, a dead proxy must not stop the call
        logger.warning("PIN failure history unavailable: %s", e)
        return None


async def report_relisten_usage(*, agent: str, provider: str, seconds: float, session_id: str = "") -> None:
    """Report one call's second-listen seconds (the STT provider's batch
    endpoint, on top of the stream) to the proxy for the usage page. Best
    effort, as ``report_turn_classifier_usage``."""
    if seconds <= 0:
        return
    try:
        async with httpx.AsyncClient(
            base_url=config.PROXY_URL,
            timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0),
        ) as http:
            await http.post(
                "/v1/phone/usage/relisten",
                headers={"Authorization": f"Bearer {config.PROXY_API_KEY}"},
                json={"agent": agent, "provider": provider, "seconds": round(float(seconds), 2),
                      "session_id": session_id or ""},
            )
    except Exception as e:  # noqa: BLE001  # usage reports are best effort, the call's teardown must not see them
        logger.warning("Second-listen usage report failed: %s", e)


async def report_turn_classifier_usage(
    *, agent: str, model: str, input_tokens: int, output_tokens: int,
    session_id: str = "",
) -> None:
    """Report one phone call's Groq turn-classifier token usage to the proxy for local
    per-agent cost tracking (/admin/usage).

    Best-effort, fire-and-forget: owns a short-lived HTTP client with a tight timeout and
    swallows every error, so it can never disturb call teardown. Uses the same internal
    Bearer auth as the socket (``config.PROXY_API_KEY`` == the proxy master key).
    Standalone (not a ProxyClient method) because the call's client speaks only its
    socket, and that is being torn down at report time.

    The hosted relay bills the classifier independently (non-streaming → buffered usage);
    this records the BASE price locally for display — a separate ledger."""
    if input_tokens <= 0 and output_tokens <= 0:
        return
    try:
        async with httpx.AsyncClient(
            base_url=config.PROXY_URL,
            timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0),
        ) as http:
            await http.post(
                "/v1/phone/usage/turn-classifier",
                headers={"Authorization": f"Bearer {config.PROXY_API_KEY}"},
                json={
                    "agent": agent,
                    "model": model,
                    "input_tokens": int(input_tokens),
                    "output_tokens": int(output_tokens),
                    "session_id": session_id or "",
                },
            )
    except Exception as e:  # noqa: BLE001  # usage reports are best effort, the call's teardown must not see them
        logger.warning("Turn-classifier usage report failed: %s", e)
