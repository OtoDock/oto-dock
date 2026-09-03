"""WebSocket client for the Claude Code Proxy API.

Uses a persistent WebSocket connection to the proxy's /ws/phone endpoint
for the entire call duration. Warmup, chat turns, and close all go through
the same connection — no per-turn HTTP overhead.

Falls back to HTTP SSE if WebSocket connection fails.
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

    The WebSocket stays open for the entire call. If the WebSocket
    connection fails, falls back to HTTP SSE for that operation.
    """

    def __init__(self, model: str = "unified", llm_mode: str = "proxy",
                 phone_mode: bool = False, call_type: str = "inbound",
                 phone_context_override: str = "",
                 audiosocket_uuid: str = "",
                 phone_route_id: str = "",
                 caller_phone: str = "",
                 caller_did: str = "",
                 dial_event: dict | None = None):
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
        self.session_id: str | None = None
        self.messages: list[dict[str, str]] = []
        self._ws: websockets.ClientConnection | None = None
        self._ws_connected: bool = False
        # HTTP fallback client
        self._http: httpx.AsyncClient | None = None
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

    async def connect(self) -> bool:
        """Establish persistent WebSocket to proxy.

        Returns True if connected, False if fell back to HTTP.
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
            logger.info(f"WebSocket connected to proxy at {config.PROXY_WS_HOST_PORT}")
            return True
        except Exception as e:
            logger.warning(f"WebSocket connection failed, will use HTTP fallback: {e}")
            self._ws_connected = False
            self._http = httpx.AsyncClient(
                base_url=config.PROXY_URL,
                timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0),
            )
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
                turn = msg.get("turn")
                if turn is None:
                    self._control_queue.put_nowait(msg)
                elif turn == self._current_turn and self._turn_queue is not None:
                    self._turn_queue.put_nowait(msg)
                else:
                    logger.debug(
                        f"Dropping stale frame turn={turn} "
                        f"(current={self._current_turn}): {msg.get('type')}"
                    )
        except websockets.ConnectionClosed as e:
            logger.warning(f"WS connection closed: {e}")
        except Exception as e:
            logger.error(f"WS receive loop error: {e}")
        finally:
            self._ws_connected = False
            closed = {"type": "_closed"}
            self._control_queue.put_nowait(closed)
            if self._turn_queue is not None:
                self._turn_queue.put_nowait(closed)

    async def warmup_session(self) -> str | None:
        """Pre-create a persistent session on the proxy.

        Uses WebSocket if connected, otherwise HTTP fallback.
        Returns the session_id, or None on failure.
        """
        if self._ws_connected and self._ws:
            return await self._ws_warmup()
        # HTTP fallback: if session already pre-warmed, skip creating a new one
        if self.session_id:
            return self.session_id
        return await self._http_warmup()

    async def _ws_warmup(self) -> str | None:
        """Warmup via WebSocket."""
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
            }
            if self.session_id:
                # Include pre-warmed session_id so proxy can reuse existing session
                msg["session_id"] = self.session_id
            await self._ws.send(json.dumps(msg))
            msg = await asyncio.wait_for(self._control_queue.get(), timeout=120)
            if msg.get("type") == "warmup_ready":
                self.session_id = msg["data"]["session_id"]
                logger.info(
                    f"WS warmup ready: {self.session_id} "
                    f"(mode={msg['data'].get('llm_mode', self.llm_mode)})"
                )
                return self.session_id
            elif msg.get("type") == "error":
                logger.warning(f"WS warmup error: {msg['data'].get('message')}")
                return None
            else:
                logger.warning(f"WS warmup unexpected response: {msg}")
                return None
        except Exception as e:
            logger.warning(f"WS warmup failed: {e}")
            return None

    async def _http_warmup(self) -> str | None:
        """Warmup via HTTP (fallback)."""
        if not self._http:
            return None
        try:
            body = {
                "model": self.model,
                "permission_mode": "auto",
                "llm_mode": self.llm_mode,
                "phone_mode": self.phone_mode,
                "call_type": self.call_type,
                "phone_context_override": self.phone_context_override,
                # Same trigger-enrichment fields as the WS path — HTTP
                # fallback also enables enrichment when the proxy supports it.
                "audiosocket_uuid": self.audiosocket_uuid,
                "phone_route_id": self.phone_route_id,
                "caller_phone": self.caller_phone,
                "caller_did": self.caller_did,
                "dial_event": self.dial_event,
            }
            resp = await self._http.post(
                "/v1/sessions/warmup",
                headers={"Authorization": f"Bearer {config.PROXY_API_KEY}"},
                json=body,
            )
            if resp.status_code == 200:
                data = resp.json()
                self.session_id = data.get("session_id")
                if self.session_id:
                    self.messages.append({
                        "role": "assistant",
                        "content": f"[//]: # (session:{self.session_id})",
                    })
                logger.info(
                    f"HTTP warmup ready: {self.session_id} "
                    f"(mode={data.get('llm_mode', self.llm_mode)})"
                )
                return self.session_id
            else:
                logger.warning(f"HTTP warmup failed: {resp.status_code} {resp.text}")
                return None
        except Exception as e:
            logger.warning(f"HTTP warmup error: {e}")
            return None

    def set_session_id(self, session_id: str) -> None:
        """Inject an externally-obtained session_id (e.g. from pre-warmup)."""
        self.session_id = session_id
        self.messages.append({
            "role": "assistant",
            "content": f"[//]: # (session:{session_id})",
        })
        logger.info(f"Session ID injected: {session_id}")

    def mark_spoken(self, char_count: int) -> None:
        """Track how many chars of the last response were played via TTS."""
        self._last_spoken_chars = char_count

    def annotate_interrupted_response(self) -> None:
        """Flag that the next send_message should include barge-in info."""
        if self._last_spoken_chars > 0:
            self._barge_in_pending = True

    async def send_message(self, text: str):
        """Send a user message and stream back assistant text tokens.

        Yields text chunks as they arrive from the LLM.
        Uses WebSocket if connected, otherwise HTTP SSE fallback.
        """
        if self._ws_connected and self._ws:
            async for chunk in self._ws_send_message(text):
                yield chunk
        else:
            async for chunk in self._http_send_message(text):
                yield chunk

    async def _ws_send_message(self, text: str):
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
            await self._ws.send(json.dumps({
                "type": "chat",
                "prompt": text,
                "turn": turn,
                "barge_in_chars": barge_in_chars,
            }))
        except Exception as e:
            logger.error(f"WS send failed: {e}")
            yield "Sorry, I couldn't reach the AI service."
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
                logger.error("WS connection closed during chat")
                self.turn_in_flight = False
                if not full_response:
                    yield "Sorry, the connection was lost."
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
                # until the turn ends. Repeats are safe: an empty text buffer
                # makes the signal a no-op downstream.
                yield TOOL_USE_SIGNAL
            elif msg_type == "session":
                self.session_id = msg.get("data", {}).get("session_id")
            elif msg_type == "done":
                self.turn_in_flight = False
                break
            elif msg_type == "error":
                error_msg = msg.get("data", {}).get("message", "Unknown error")
                logger.error(f"WS chat error: {error_msg}")
                self.turn_in_flight = False
                yield "Sorry, I encountered an error."
                break
            # Silently ignore: tool_input, thinking, metadata

        self.turn_in_flight = False
        # Reset barge-in tracking for this new response
        self._last_spoken_chars = 0

        assistant_text = "".join(full_response)
        logger.info(
            f"WS response: {len(assistant_text)} chars, turn={turn}, "
            f"session={self.session_id}"
        )

    @property
    def supports_abort(self) -> bool:
        """Whether an in-flight turn can be cancelled server-side.

        Direct always aborts cleanly (the API stream unwinds and the session
        pops the un-answered user message). Proxy mode aborts over the WS
        channel — the proxy runs the same graceful per-layer interrupt the
        duplex chat attach uses (CLI stdin control_request, Codex interrupt),
        keeping the partial turn; the daemon's turn-id filter drops the tail
        frames. The HTTP-SSE fallback has no signalling channel, so a
        dead-WS proxy client degrades to run-to-completion (barge-in
        fidelity still rides the X-Claude-Barge-In-Chars header).
        """
        return self.llm_mode == "direct" or (
            self.llm_mode == "proxy" and self._ws_connected)

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
        """Abort the in-flight turn (barge-in / continuation on Direct).

        WS path: the proxy cancels the turn's producer — the API stream and
        any in-flight tool unwind, and the direct session pops the
        un-answered user message so the pipeline can resend it batched with
        the caller's new speech. HTTP path: abandoning the SSE iterator
        already closed the request; drop the un-answered user message from
        local history for the same reason. Callers gate on supports_abort.
        """
        if not self.supports_abort:
            return
        if self._ws_connected and self._ws:
            try:
                await self._ws.send(json.dumps({
                    "type": "abort", "turn": self._current_turn,
                }))
            except Exception as e:
                logger.warning(f"WS abort send failed: {e}")
            return
        if self.messages and self.messages[-1].get("role") == "user":
            self.messages.pop()

    async def _http_send_message(self, text: str):
        """Send message via HTTP SSE (fallback)."""
        if not self._http:
            yield "Sorry, no connection available."
            return

        self.messages.append({"role": "user", "content": text})
        messages_to_send = [{"role": m["role"], "content": m["content"]} for m in self.messages]

        body = {
            "model": self.model,
            "messages": messages_to_send,
            "stream": True,
        }

        headers = {
            "Authorization": f"Bearer {config.PROXY_API_KEY}",
            "Content-Type": "application/json",
            "X-Claude-Permission-Mode": "auto",
        }

        if self.llm_mode == "direct":
            headers["X-Claude-LLM-Mode"] = "direct"

        if self._barge_in_pending and self._last_spoken_chars > 0:
            headers["X-Claude-Barge-In-Chars"] = str(self._last_spoken_chars)
            self._barge_in_pending = False
            self._last_spoken_chars = 0

        full_response = []

        try:
            async with self._http.stream(
                "POST", "/v1/chat/completions",
                json=body, headers=headers,
            ) as response:
                if response.status_code != 200:
                    error = await response.aread()
                    logger.error(f"HTTP error {response.status_code}: {error.decode()}")
                    yield "Sorry, I encountered an error."
                    return

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    event_type = data.get("type", "")
                    event_data = data.get("data", {})

                    if event_type == "text":
                        content = event_data.get("content", "")
                        if content:
                            full_response.append(content)
                            yield content
                    elif event_type in ("tool_start", "tool_end"):
                        # Same both-boundaries rule as the WS path above.
                        yield TOOL_USE_SIGNAL
                    elif event_type == "session":
                        self.session_id = event_data.get("session_id")
                    elif event_type == "done":
                        break
        except httpx.HTTPError as e:
            logger.error(f"HTTP error: {e}")
            yield "Sorry, I couldn't reach the AI service."
            return

        assistant_text = "".join(full_response)
        if assistant_text:
            content = assistant_text
            if self.session_id:
                content += f"\n\n[//]: # (session:{self.session_id})"
            self.messages.append({"role": "assistant", "content": content})

        self._last_spoken_chars = 0
        logger.info(f"HTTP response: {len(assistant_text)} chars, session={self.session_id}")

    async def close_session(self) -> bool:
        """Close the persistent session on the proxy (call on hangup)."""
        if not self.session_id:
            return False

        if self._ws_connected and self._ws:
            try:
                await self._ws.send(json.dumps({"type": "close"}))
                logger.info(f"WS close sent: session={self.session_id}")
                return True
            except Exception as e:
                logger.warning(f"WS close failed: {e}")
                return False

        # HTTP fallback
        if self._http:
            try:
                resp = await self._http.delete(
                    f"/v1/sessions/{self.session_id}",
                    headers={"Authorization": f"Bearer {config.PROXY_API_KEY}"},
                )
                data = resp.json()
                closed = data.get("status") == "closed"
                logger.info(f"HTTP close session {self.session_id}: {data.get('status')}")
                return closed
            except Exception as e:
                logger.warning(f"HTTP close failed: {e}")
                return False

        return False

    async def close(self) -> None:
        """Close the persistent session and transport."""
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
        if self._http:
            await self._http.aclose()
            self._http = None


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
    except Exception as e:
        logger.warning(f"Call report failed: {e}")


async def report_turn_classifier_usage(
    *, agent: str, model: str, input_tokens: int, output_tokens: int,
    session_id: str = "",
) -> None:
    """Report one phone call's Groq turn-classifier token usage to the proxy for local
    per-agent cost tracking (/admin/usage).

    Best-effort, fire-and-forget: owns a short-lived HTTP client with a tight timeout and
    swallows every error, so it can never disturb call teardown. Uses the same internal
    Bearer auth as the warmup path (``config.PROXY_API_KEY`` == the proxy master key).
    Standalone (not a ProxyClient method) because ``ProxyClient._http`` is None on the
    WebSocket-happy path and the call's client is being torn down at report time.

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
    except Exception as e:
        logger.warning(f"Turn-classifier usage report failed: {e}")
