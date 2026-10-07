"""The credential gateway route: a local session's HTTP MCP traffic, with
the credential added on the way out.

``/v1/mcp-gateway/{mcp}/{rest}`` takes the session token as the bearer and
nothing else (a cookie, the master key and every other principal are
refused), binds the token's session to the MCP the path names through the
broker store, confines the forward to the MCP's declared endpoint, resolves
the credential per request and streams the answer back without holding a
body on the loop. It answers on the internal listener only (the sandbox
splice and the Direct layer land there) when the app bound one. A refusal
is a JSON-RPC error the MCP client reads, never a 401 (CREDENTIALS.md "The
credential gateway").
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

import config
from auth import lan_check, token_holder
from auth.request_path import has_traversal
from auth.session_token import validate_session_token
from core.credentials import mcp_gateway
from core.session.session_state import session_token_refusal

logger = logging.getLogger("claude-proxy.mcp-gateway")
router = APIRouter()

_DISCONNECT_POLL_S = 5.0


def _refuse(method: str, body: bytes, message: str) -> Response:
    status, headers, payload = mcp_gateway.refusal_shape(method, body, message)
    return Response(content=payload, status_code=status, headers=headers)


def _session_of(request: Request) -> dict:
    """The validated session token's payload, or the 401 it earns: exactly
    one bearer, a session token, a live session, a current holder."""
    auths = request.headers.getlist("authorization")
    if len(auths) != 1:
        raise HTTPException(status_code=401, detail="A session token is required")
    scheme, _, token = auths[0].strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="A session token is required")
    payload = validate_session_token(token.strip())
    if payload is None:
        raise HTTPException(status_code=401, detail="A session token is required")
    if session_token_refusal(payload):
        raise HTTPException(status_code=401, detail="Session is no longer active")
    return payload


async def _read_body(request: Request) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > mcp_gateway.MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.api_route("/v1/mcp-gateway/{mcp}/{rest:path}", methods=["POST", "GET", "DELETE"],
                  include_in_schema=False)
async def gateway(mcp: str, rest: str, request: Request):
    if config.INTERNAL_LISTENER_PORT and not lan_check.resolve(request.scope).internal:
        raise HTTPException(status_code=404, detail="Not found")
    payload = _session_of(request)
    if not await token_holder.holder_ok(payload):
        raise HTTPException(status_code=401, detail="Session is no longer active")
    session_id = str(payload.get("sid") or "")
    method = request.method
    rest = "/" + rest
    if has_traversal(rest):
        raise HTTPException(status_code=404, detail="Not found")
    body = await _read_body(request)
    if body is None:
        raise HTTPException(status_code=413, detail="Request body too large")
    prepared = await mcp_gateway.prepare_forward(
        session_id, mcp, method, rest, request.scope.get("query_string") or b"",
        request.headers.items(),
    )
    if isinstance(prepared, mcp_gateway.Refusal):
        logger.info("mcp-gateway: %s refused for session %s mcp %r", prepared.reason,
                    session_id[:8], mcp)
        return _refuse(method, body, prepared.detail)
    req = prepared.client.build_request(method, prepared.url, headers=prepared.headers,
                                        content=body)
    send = asyncio.ensure_future(prepared.client.send(req, stream=True))
    try:
        while True:
            done, _ = await asyncio.wait({send}, timeout=_DISCONNECT_POLL_S)
            if done:
                break
            if await request.is_disconnected():
                send.cancel()
                return Response(status_code=499)
        upstream = send.result()
    except httpx.HTTPError as e:
        logger.warning("mcp-gateway: upstream error for mcp %r: %s", mcp, type(e).__name__)
        return _refuse(method, body, "The MCP server did not answer.")
    if 300 <= upstream.status_code < 400:
        await upstream.aclose()
        return _refuse(method, body, mcp_gateway._REFUSAL_REDIRECT)
    if upstream.status_code == 401:
        await upstream.aclose()
        logger.info("mcp-gateway: the server refused the credential for session %s mcp %r",
                    session_id[:8], mcp)
        return _refuse(method, body, mcp_gateway._REFUSAL_VENDOR_401)
    headers = dict(mcp_gateway.forward_response_headers(upstream.headers.multi_items()))
    return StreamingResponse(
        upstream.aiter_raw(), status_code=upstream.status_code, headers=headers,
        background=BackgroundTask(upstream.aclose),
    )
