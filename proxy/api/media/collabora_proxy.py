"""Reverse-proxy `/collabora/*` HTTP and WebSocket traffic to the Collabora
container when the platform is configured for **sub-path mode** (Collabora
mounted under ${DASHBOARD_PUBLIC_URL}/collabora). This lets the platform proxy
be the single external entry point: user reverse proxies / Cloudflare Tunnel /
k8s ingress point at one upstream, and we split `/collabora/*` traffic
internally to the Collabora backend.

In **subdomain mode** (COLLABORA_URL on a different host) and
**central-cloud mode** the helper guard returns False and these endpoints
return 404 — the user's external setup routes the Collabora FQDN directly to
its container and the platform proxy is uninvolved.
"""

import asyncio
import logging
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

import config

logger = logging.getLogger("claude-proxy")
router = APIRouter()

# RFC 7230 hop-by-hop headers — must not be forwarded across a proxy.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

# Response headers we must strip because httpx auto-decompresses the upstream
# body before handing it to us. If we forward `Content-Encoding: gzip` with
# already-decompressed bytes, the browser tries to gunzip plain bytes and
# fails with ERR_CONTENT_DECODING_FAILED (asset 200s but body unusable —
# JS never executes, Collabora iframe stays blank). Same reason
# `Content-Length` is stripped: the decompressed length doesn't match the
# upstream's compressed-byte count.
_STRIP_RESPONSE_HEADERS = _HOP_BY_HOP | {"content-encoding", "content-length"}


def _is_subpath_mode() -> bool:
    """True iff COLLABORA_URL is on the same host as DASHBOARD_PUBLIC_URL.

    When True, the platform proxy is responsible for routing /collabora/* to
    the Collabora backend. When False (subdomain / central-cloud / unset),
    these endpoints return 404 / close the WS — the user's external setup
    routes the Collabora FQDN directly.
    """
    if not config.COLLABORA_URL or not config.DASHBOARD_PUBLIC_URL:
        return False
    try:
        return (urlparse(config.COLLABORA_URL).hostname
                == urlparse(config.DASHBOARD_PUBLIC_URL).hostname)
    except Exception:
        return False


def _is_blocked_subpath(encoded_path: str) -> bool:
    """Collabora's admin console / metrics must NEVER be reachable through the
    public proxy: the admin WebSocket can manage every open document and the
    metrics endpoint leaks operational data. Hard-404 them regardless of auth.

    The check is on the DECODED path because the backend URL-decodes before
    routing — otherwise ``/cool/%61dminws`` (or double-encoded forms) would slip
    past a match on the raw encoded string and still reach ``adminws``."""
    p = encoded_path
    for _ in range(3):  # collapse any double/triple percent-encoding
        decoded = unquote(p)
        if decoded == p:
            break
        p = decoded
    p = p.lower().lstrip("/")
    return (
        "adminws" in p
        or "getmetrics" in p
        or "admin.html" in p
        or p.startswith("cool/admin")
    )


def _query_param(scope: dict, name: str) -> str:
    raw = scope.get("query_string", b"")
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "replace")
    return parse_qs(raw).get(name, [""])[0]


def _collabora_authorized(scope: dict, cookies: dict) -> bool:
    """Gate the proxy so it isn't an open relay to the Collabora backend.

    In sub-path mode the editor is same-origin with the dashboard, so a browser
    request carries the ``session`` cookie; the editing WS / WOPI-bearing
    requests additionally carry a WOPI ``access_token``. Either proves a real
    user, so we allow on the FIRST that validates and reject otherwise.
    """
    from auth.providers import validate_session_jwt
    sc = cookies.get("session")
    if sc and validate_session_jwt(sc):
        return True
    from api.media.wopi import validate_wopi_token
    tok = _query_param(scope, "access_token")
    if tok and validate_wopi_token(tok):
        return True
    return False


def _raw_subpath(scope: dict) -> str:
    """Extract the encoded path AFTER `/collabora/` from the ASGI scope.

    FastAPI's `{path:path}` URL-decodes its capture, which breaks Collabora's
    `/cool/<url-encoded-WOPISrc>/ws` pattern: once decoded, the embedded
    `http://...?access_token=...` looks like multiple path segments + an
    embedded query string, and Collabora rejects with "Bad URI syntax".
    Using `raw_path` (bytes, undecoded) preserves the encoding end-to-end.
    """
    raw = scope.get("raw_path") or scope.get("path", "").encode("ascii", "replace")
    if isinstance(raw, bytes):
        raw = raw.decode("ascii")
    prefix = "/collabora/"
    if raw.startswith(prefix):
        return raw[len(prefix):]
    return raw.lstrip("/")


def _backend_http_url(encoded_path: str) -> str:
    """Compose the backend HTTP URL preserving the `/collabora/` prefix.

    Collabora's `--o:net.service_root=/collabora` makes it generate links
    with the prefix and expect requests at the prefixed path; the reverse
    proxy must NOT strip the prefix.
    """
    return f"{config.COLLABORA_BACKEND_URL.rstrip('/')}/collabora/{encoded_path}"


def _backend_ws_url(encoded_path: str, query: str) -> str:
    base = config.COLLABORA_BACKEND_URL.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    url = f"{base}/collabora/{encoded_path}"
    if query:
        url += f"?{query}"
    return url


def _forwarded_headers() -> dict[str, str]:
    """`X-Forwarded-Proto`/`-Host` derived from `DASHBOARD_PUBLIC_URL`.

    Collabora runs with `--o:net.proxy_prefix=true`, so it builds its own
    self-referential URLs (the discovery `urlsrc`, the editor WS endpoint, asset
    paths) from these headers. Without them it falls back to its `server_name` +
    `ssl.termination` config and advertises e.g. `https://localhost` — the wrong
    scheme/port behind a plain-http or non-default-port proxy, which makes the
    iframe's editor try to connect to `https://localhost:443` ("refused to
    connect"). Sending them makes Collabora's self-URLs track the dashboard's
    real public scheme+host for ANY deployment (http-localhost or https-FQDN).
    """
    pub = urlparse(config.DASHBOARD_PUBLIC_URL or "")
    fwd: dict[str, str] = {}
    if pub.scheme:
        fwd["X-Forwarded-Proto"] = pub.scheme
    if pub.netloc:
        fwd["X-Forwarded-Host"] = pub.netloc
    return fwd


@router.api_route(
    "/collabora/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def proxy_collabora_http(path: str, request: Request):
    if not _is_subpath_mode():
        raise HTTPException(status_code=404, detail="Not found")

    encoded_path = _raw_subpath(request.scope)
    if _is_blocked_subpath(encoded_path):
        raise HTTPException(status_code=404, detail="Not found")
    # Same-origin CORS preflight carries neither cookie nor token; it returns no
    # data, so let it through (and the admin block above still applies).
    if request.method != "OPTIONS" and not _collabora_authorized(request.scope, request.cookies):
        raise HTTPException(status_code=403, detail="Forbidden")

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "host"
    }
    headers.update(_forwarded_headers())

    raw_query = request.scope.get("query_string", b"")
    if isinstance(raw_query, bytes):
        raw_query = raw_query.decode("ascii", "replace")

    target_url = _backend_http_url(encoded_path)
    if raw_query:
        target_url += f"?{raw_query}"

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
        try:
            upstream = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=await request.body(),
            )
        except httpx.RequestError as e:
            logger.warning(f"Collabora backend unreachable ({path}): {e}")
            raise HTTPException(status_code=502, detail="Collabora backend unavailable")

    response_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _STRIP_RESPONSE_HEADERS
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


def _upstream_connect_kwargs(
    additional_headers: list | None,
    forwarded_origin: str | None,
    chosen_subproto: str | None,
) -> dict:
    """kwargs for the upstream Collabora websocket connect.

    Tolerant keepalive: the library default (~20s ping timeout) tears the
    tunnel down when Collabora stalls on a CPU-heavy recalculation (large
    spreadsheets), killing the user's live preview. The tunnel must be the
    tolerant link; the cost is slower detection of a genuinely dead upstream
    (~145s worst case).
    """
    kwargs: dict = {
        "max_size": None,
        "additional_headers": additional_headers or None,
        "ping_interval": 25,
        "ping_timeout": 120,
    }
    if forwarded_origin:
        kwargs["origin"] = forwarded_origin
    if chosen_subproto:
        kwargs["subprotocols"] = [chosen_subproto]
    return kwargs


@router.websocket("/collabora/{path:path}")
async def proxy_collabora_ws(websocket: WebSocket, path: str):
    if not _is_subpath_mode():
        # Don't accept; close with policy violation so client surfaces a real error.
        await websocket.close(code=1008, reason="Collabora sub-path mode not configured")
        return

    encoded_path = _raw_subpath(websocket.scope)
    # Block the admin WS + reject an unauthenticated relay before accepting.
    if _is_blocked_subpath(encoded_path) or not _collabora_authorized(
        websocket.scope, websocket.cookies,
    ):
        await websocket.close(code=1008, reason="unauthorized")
        return

    raw_query = websocket.scope.get("query_string", b"")
    if isinstance(raw_query, bytes):
        raw_query = raw_query.decode("ascii", "replace")
    upstream_url = _backend_ws_url(encoded_path, raw_query)

    # Subprotocol passthrough — Collabora uses bare WS without subprotocols
    # in current versions, but be defensive in case that changes.
    subprotos = websocket.scope.get("subprotocols") or []
    chosen_subproto = subprotos[0] if subprotos else None
    if chosen_subproto:
        await websocket.accept(subprotocol=chosen_subproto)
    else:
        await websocket.accept()

    # Forward request headers Collabora requires/uses for origin validation,
    # cookies (Authentik passthrough), and any future custom headers. The WS
    # library sets Host / Sec-WebSocket-* itself; we must not override those.
    req_headers = {k.lower(): v for k, v in websocket.headers.items()}
    forwarded_origin = req_headers.get("origin")
    forwarded_cookie = req_headers.get("cookie")
    additional_headers: list[tuple[str, str]] = []
    if forwarded_cookie:
        additional_headers.append(("Cookie", forwarded_cookie))
    # Same proxy-prefix scheme/host signal as the HTTP forwarder (so the WS
    # handshake's self-URLs match the dashboard's public origin).
    for _k, _v in _forwarded_headers().items():
        additional_headers.append((_k, _v))

    try:
        connect_kwargs = _upstream_connect_kwargs(
            additional_headers, forwarded_origin, chosen_subproto
        )
        async with websockets.connect(upstream_url, **connect_kwargs) as upstream:
            async def client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            await upstream.close()
                            return
                        if msg.get("text") is not None:
                            await upstream.send(msg["text"])
                        elif msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                except WebSocketDisconnect:
                    try:
                        await upstream.close()
                    except Exception:
                        pass

            async def upstream_to_client():
                try:
                    async for frame in upstream:
                        if isinstance(frame, str):
                            await websocket.send_text(frame)
                        else:
                            await websocket.send_bytes(frame)
                except websockets.ConnectionClosed:
                    pass

            await asyncio.gather(
                client_to_upstream(),
                upstream_to_client(),
                return_exceptions=True,
            )
            # Field diagnosis for "preview died by itself": record WHY the
            # upstream document socket ended (1000/1001 = orderly; 1006/None =
            # dropped, e.g. missed pongs under load or a container restart).
            if upstream.close_code not in (1000, 1001):
                logger.warning(
                    f"Collabora WS upstream closed abnormally ({path}): "
                    f"code={upstream.close_code} reason={upstream.close_reason!r}"
                )
    except websockets.WebSocketException as e:
        logger.warning(f"Collabora WS upstream error ({path}): {e}")
    except Exception:
        logger.exception(f"Collabora WS proxy error ({path})")
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass
