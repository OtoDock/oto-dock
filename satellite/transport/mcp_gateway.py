"""The satellite-local credential gateway: a machine session's vendor MCP
traffic leaves the machine with the credential added here, from memory.

The proxy pushes each token (``mcp_gateway_token``: the session id, the
hash of the session's own token, the MCP key, the header, the value, a
lease in seconds, the upstream URL) before the session spawns and renews
it under the lease; a wipe (``mcp_gateway_wipe``) removes a session's
entries; an entry past its lease is swept. The session's configuration
names ``/v1/mcp-gateway/<mcp>/<path>`` on the loopback tunnel server with
the session's own token as the bearer: the table is keyed by the hash of
that bearer and the MCP, so no session index is needed and a stale close or
a respawn of the same session id can never wipe a replacement's push. The
forward is confined to the pushed upstream's path, the session's own
headers never leave the machine, redirects are never followed, a refusal is
a JSON-RPC error the MCP client reads (never a 401), and a response streams
through with the client's transport polled so a vanished reader ends the
upstream read.

Memory only: nothing here is written to disk, and a satellite restart
starts empty (the proxy pushes again at the next session start).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

logger = logging.getLogger("satellite.mcp-gateway")

GATEWAY_PATH_PREFIX = "/v1/mcp-gateway"
LEASE_MAX_S = 15 * 60
# MCP JSON-RPC, never a file upload (the tunnel keeps the 128 MB upload cap;
# this path carries tool calls and results).
MAX_BODY_BYTES = 16 * 1024 * 1024
_SWEEP_S = 60.0
_PUSH_WAIT_S = 3.0
_CLIENT_POLL_S = 5.0
_CONNECT_TIMEOUT_S = 10.0

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "trailers", "transfer-encoding", "upgrade",
})
_REQUEST_DROP = _HOP_BY_HOP | frozenset({
    "host", "content-length", "authorization", "cookie", "forwarded", "x-real-ip",
})
_RESPONSE_DROP = _HOP_BY_HOP | frozenset({
    "set-cookie", "www-authenticate", "location", "content-length",
})

_REFUSAL_NO_TOKEN = (
    "No credential has reached this machine for this session and MCP; the platform "
    "pushes it at the session's start and renews it while the session runs."
)
_REFUSAL_PATH = "The gateway forwards only to the MCP's declared endpoint."
_REFUSAL_REDIRECT = "The MCP server answered with a redirect to another address; the gateway follows none."
_REFUSAL_VENDOR_401 = (
    "The MCP server refused the credential; reconnect the account or re-enter the key "
    "in Settings > Integrations."
)
_REFUSAL_NO_ANSWER = "The MCP server did not answer."


@dataclass
class _Token:
    session_id: str
    header: str
    value: str
    upstream: str
    path: str
    lease_until: float


def token_hash_of(bearer: str) -> str:
    return hashlib.sha256(bearer.encode()).hexdigest()


def refusal_shape(method: str, body: bytes, message: str, *, code: int = -32001) -> tuple[int, dict, bytes]:
    """The answer a refused request gets: a JSON-RPC error with the
    request's id for a POST that carries one, 202 for a notification, 405
    for a GET, 200 for a DELETE (the proxy's gateway answers the same)."""
    if method == "GET":
        return 405, {"Allow": "POST, DELETE"}, b""
    if method == "DELETE":
        return 200, {}, b""
    rid = None
    if body and len(body) <= 256 * 1024:
        try:
            doc = json.loads(body)
        except ValueError:
            doc = None
        if isinstance(doc, dict) and doc.get("id") is not None:
            rid = doc["id"]
    if rid is None:
        return 202, {}, b""
    payload = {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
    return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode()


def _path_matches(declared: str, requested: str) -> bool:
    if not declared or not requested:
        return False
    return declared.rstrip("/") == requested.rstrip("/")


class GatewayTable:
    """The pushed tokens, keyed by ``(token hash, mcp)``."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _Token] = {}
        self._changed = asyncio.Event()

    def push(self, msg: dict) -> bool:
        """Store a pushed token; False for a frame missing a field."""
        token_hash = str(msg.get("token_hash") or "")
        mcp = str(msg.get("mcp") or "")
        header = str(msg.get("header") or "")
        value = str(msg.get("value") or "")
        upstream = str(msg.get("upstream") or "")
        if not (token_hash and mcp and header and value and upstream):
            return False
        try:
            lease = float(msg.get("expires_in", LEASE_MAX_S))
        except (TypeError, ValueError):
            lease = LEASE_MAX_S
        lease = max(0.0, min(lease, LEASE_MAX_S))
        parts = urlsplit(upstream)
        self._entries[(token_hash, mcp)] = _Token(
            session_id=str(msg.get("session_id") or ""), header=header, value=value,
            upstream=f"{parts.scheme}://{parts.netloc}", path=parts.path or "/",
            lease_until=time.monotonic() + lease,
        )
        self._changed.set()
        self._changed = asyncio.Event()
        return True

    def wipe(self, msg: dict) -> int:
        """Remove every entry of the session the frame names (by its token
        hash, by its session id when the hash is absent); the count."""
        token_hash = str(msg.get("token_hash") or "")
        session_id = str(msg.get("session_id") or "")
        keys = [
            k for k, t in self._entries.items()
            if (token_hash and k[0] == token_hash) or (not token_hash and session_id and t.session_id == session_id)
        ]
        for k in keys:
            self._entries.pop(k, None)
        return len(keys)

    def sweep(self, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        keys = [k for k, t in self._entries.items() if t.lease_until <= now]
        for k in keys:
            self._entries.pop(k, None)
        return len(keys)

    def get(self, token_hash: str, mcp: str) -> _Token | None:
        t = self._entries.get((token_hash, mcp))
        if t is None or t.lease_until <= time.monotonic():
            return None
        return t

    async def wait_for(self, token_hash: str, mcp: str, timeout: float) -> _Token | None:
        """The entry, waiting up to ``timeout`` for a push in flight (a
        session's first request can race the push's ack)."""
        deadline = time.monotonic() + timeout
        while True:
            t = self.get(token_hash, mcp)
            if t is not None:
                return t
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._changed.wait()), timeout=min(remaining, 0.25))

    def __len__(self) -> int:
        return len(self._entries)


class LocalMcpGateway:
    """The handler the loopback tunnel server mounts under
    ``/v1/mcp-gateway/``."""

    def __init__(self) -> None:
        self.table = GatewayTable()
        self._client: aiohttp.ClientSession | None = None
        self._sweeper: asyncio.Task | None = None

    def start(self) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(_SWEEP_S)
            n = self.table.sweep()
            if n:
                logger.info("mcp-gateway: %d token(s) past their lease swept", n)

    def _session(self) -> aiohttp.ClientSession:
        if self._client is None or self._client.closed:
            # Raw bytes pass through (no decompression: the client's own
            # Accept-Encoding travelled); no redirects; no read timeout (a
            # standing stream); a bounded connect.
            self._client = aiohttp.ClientSession(
                auto_decompress=False,
                timeout=aiohttp.ClientTimeout(total=None, connect=_CONNECT_TIMEOUT_S, sock_read=None),
            )
        return self._client

    @staticmethod
    def _bearer_of(request: web.Request) -> str:
        auths = request.headers.getall("Authorization", [])
        if len(auths) != 1:
            return ""
        scheme, _, token = auths[0].strip().partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return ""
        return token.strip()

    @staticmethod
    def _refuse(method: str, body: bytes, message: str) -> web.Response:
        status, headers, payload = refusal_shape(method, body, message)
        return web.Response(status=status, headers=headers, body=payload)

    async def _read_body(self, request: web.Request) -> bytes | None:
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.content.iter_any():
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    async def handle(self, request: web.Request, mcp: str, rest: str) -> web.StreamResponse:
        method = request.method
        if method not in ("POST", "GET", "DELETE"):
            return web.Response(status=405, headers={"Allow": "POST, GET, DELETE"})
        bearer = self._bearer_of(request)
        if not bearer:
            return web.json_response({"error": "session-token-required"}, status=401)
        body = await self._read_body(request)
        if body is None:
            return web.json_response({"error": "request-body-too-large"}, status=413)
        token = await self.table.wait_for(token_hash_of(bearer), mcp, _PUSH_WAIT_S)
        if token is None:
            logger.info("mcp-gateway: no token for mcp %s (session unknown or lease ended)", mcp)
            return self._refuse(method, body, _REFUSAL_NO_TOKEN)
        if not _path_matches(token.path, rest):
            return self._refuse(method, body, _REFUSAL_PATH)
        headers = {}
        for k, v in request.headers.items():
            lk = k.lower()
            if lk in _REQUEST_DROP or lk.startswith("x-forwarded-"):
                continue
            headers[k] = v
        if not any(k.lower() == "accept-encoding" for k in headers):
            headers["Accept-Encoding"] = "identity"
        headers[token.header] = token.value
        url = f"{token.upstream}{token.path}"
        session = self._session()
        try:
            upstream = await self._send_watching_client(
                request, session.request(method, url, headers=headers, data=body, allow_redirects=False),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("mcp-gateway: upstream error for mcp %s: %s", mcp, type(e).__name__)
            return self._refuse(method, body, _REFUSAL_NO_ANSWER)
        if upstream is None:
            return web.Response(status=499)
        try:
            if 300 <= upstream.status < 400:
                return self._refuse(method, body, _REFUSAL_REDIRECT)
            if upstream.status == 401:
                logger.info("mcp-gateway: the server refused the credential for mcp %s", mcp)
                return self._refuse(method, body, _REFUSAL_VENDOR_401)
            response = web.StreamResponse(status=upstream.status)
            for k, v in upstream.headers.items():
                if k.lower() not in _RESPONSE_DROP:
                    response.headers.add(k, v)
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                if request.transport is None or request.transport.is_closing():
                    break
                await response.write(chunk)
            with contextlib.suppress(Exception):
                await response.write_eof()
            return response
        finally:
            upstream.release()

    @staticmethod
    async def _send_watching_client(request: web.Request, coro):
        """Await the upstream's answer while polling the client's transport
        every few seconds: a client that left ends the wait (None)."""
        task = asyncio.ensure_future(coro)
        while True:
            done, _ = await asyncio.wait({task}, timeout=_CLIENT_POLL_S)
            if done:
                return task.result()
            if request.transport is None or request.transport.is_closing():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                return None
