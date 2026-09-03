"""Public entrance for Twilio call signaling — proxy → phone-daemon relay.

Twilio must reach the phone daemon's `/v1/twilio/*` surface over public
HTTPS, but the daemon's port stays private: the PROXY (already publicly
fronted as the dashboard's `DASHBOARD_PUBLIC_URL`) exposes the same paths
and forwards to `PHONE_SERVER_URL`. One hostname, one TLS front, nothing
extra for the admin to publish — and the only shape that works on a hosted
cloud tier where the daemon is never reachable directly.

These routes carry NO platform auth (Twilio cannot send bearer tokens); the
daemon validates `X-Twilio-Signature` on every request, fail-closed. The
relay's job is byte-fidelity, not policy:

* webhook/status POSTs forward the RAW body (parsing + re-encoding could
  reorder form fields and break the signature), the query string, and the
  signature + content-type headers;
* the media WebSocket bridges text frames 1:1 after forwarding the upgrade's
  signature header — the daemon reconstructs the signed URL from the SAME
  pushed `public_base_url`, so path preservation keeps the math identical.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx
import websockets
from fastapi import APIRouter, Request, Response, WebSocket

import config

logger = logging.getLogger("claude-proxy.twilio-relay")

router = APIRouter()

_FORWARD_TIMEOUT_S = 15.0
_WS_MAX_MSG = 1024 * 1024
# Real Twilio webhooks are a few KB of form fields; this unauthenticated
# route must not buffer arbitrary bodies into proxy RAM (Starlette has no
# default body cap) before the daemon's own 1 MB limit ever sees them.
_MAX_BODY_BYTES = 256 * 1024

_SERVER_ID_OK = str.isdigit


def _daemon_base() -> str:
    return config.PHONE_SERVER_URL.rstrip("/")


def _daemon_ws_base() -> str:
    base = _daemon_base()
    return "ws" + base[len("http"):] if base.startswith("http") else base


async def _forward_post(request: Request, path: str) -> Response:
    """Relay one signed Twilio POST to the daemon, byte-for-byte."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
        return Response(status_code=413)
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > _MAX_BODY_BYTES:
            return Response(status_code=413)
        chunks.append(chunk)
    body = b"".join(chunks)
    url = f"{_daemon_base()}{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = {
        k: v for k, v in (
            ("X-Twilio-Signature", request.headers.get("X-Twilio-Signature", "")),
            ("Content-Type", request.headers.get("Content-Type", "")),
        ) if v
    }
    try:
        async with httpx.AsyncClient(timeout=_FORWARD_TIMEOUT_S) as client:
            upstream = await client.post(url, content=body, headers=headers)
    except httpx.HTTPError as e:
        logger.warning(f"twilio relay: daemon unreachable ({e})")
        return Response(status_code=502, content="phone service unavailable")
    return Response(
        status_code=upstream.status_code,
        content=upstream.content,
        media_type=upstream.headers.get("content-type", "application/xml"),
    )


@router.post("/v1/twilio/inbound/{server_id}")
async def relay_twilio_inbound(server_id: str, request: Request) -> Response:
    if not _SERVER_ID_OK(server_id):
        return Response(status_code=404)
    return await _forward_post(request, f"/v1/twilio/inbound/{server_id}")


@router.post("/v1/twilio/status/{server_id}")
async def relay_twilio_status(server_id: str, request: Request) -> Response:
    if not _SERVER_ID_OK(server_id):
        return Response(status_code=404)
    return await _forward_post(request, f"/v1/twilio/status/{server_id}")


async def ws_twilio_media_relay(websocket: WebSocket) -> None:
    """Bridge Twilio's media WebSocket to the daemon, frame for frame.

    The daemon owns authentication: the upgrade's ``X-Twilio-Signature``
    rides the dial-out, and a refusal (handshake failure) closes Twilio's
    side before any media flows. Media Streams speaks text frames only;
    anything else is dropped.
    """
    server_id = websocket.path_params.get("server_id", "")
    if not _SERVER_ID_OK(server_id):
        await websocket.close(code=1008)
        return
    signature = websocket.headers.get("X-Twilio-Signature", "")
    await websocket.accept()
    try:
        daemon_ws = await websockets.connect(
            f"{_daemon_ws_base()}/v1/twilio/media/{server_id}",
            additional_headers=(
                {"X-Twilio-Signature": signature} if signature else {}),
            max_size=_WS_MAX_MSG,
            close_timeout=5,
        )
    except Exception as e:
        logger.warning(f"twilio relay: media dial to daemon failed ({e})")
        await websocket.close(code=1011)
        return

    async def _twilio_to_daemon() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            text = message.get("text")
            if text is not None:
                await daemon_ws.send(text)

    async def _daemon_to_twilio() -> None:
        async for frame in daemon_ws:
            if isinstance(frame, str):
                await websocket.send_text(frame)

    pump_up = asyncio.create_task(_twilio_to_daemon())
    pump_down = asyncio.create_task(_daemon_to_twilio())
    try:
        # Either side ending (hangup, daemon refusal after start, network
        # drop) tears down both — the transport anchors call lifetime to its
        # socket, so the relay must never outlive one leg.
        done, pending = await asyncio.wait(
            {pump_up, pump_down}, return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for task in done:
            with contextlib.suppress(Exception):
                task.result()
    finally:
        with contextlib.suppress(Exception):
            await daemon_ws.close()
        with contextlib.suppress(Exception):
            await websocket.close()
