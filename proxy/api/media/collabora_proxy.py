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

The module also reads Collabora's discovery for the WOPI proof keys
(``read_discovery``, used by ``wopi_proof``).
"""

import asyncio
import contextlib
import ipaddress
import logging
import re
from collections.abc import Mapping
from urllib.parse import unquote, urlparse, urlsplit

import httpx
import websockets
from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)

import config
from services.infra import pinned_http

logger = logging.getLogger("otodock")
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

# The platform's own cookies stop at the relay: Collabora needs none of them
# (the gate above has judged the request) and its start-up trace logs request
# headers whole. A cookie of something in front of the install passes.
_PLATFORM_COOKIES = frozenset({"session", "otodock_render", "pk_handoff", "__Host-pk_handoff"})
_PLATFORM_COOKIE_PREFIXES = ("share_session_",)


def _upstream_cookie(raw: str | None) -> str | None:
    """The ``Cookie`` header to forward: ``raw`` without the platform's
    cookies, None when nothing is left."""
    kept = []
    for part in (raw or "").split(";"):
        part = part.strip()
        name = part.split("=", 1)[0].strip()
        if not part or name in _PLATFORM_COOKIES or name.startswith(_PLATFORM_COOKIE_PREFIXES):
            continue
        kept.append(part)
    return "; ".join(kept) or None


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
    except ValueError:
        return False


def _decoded(text: str) -> str:
    """``text`` with any double or triple percent-encoding collapsed, the
    way the backend decodes before routing."""
    for _ in range(3):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


# Collabora endpoints no editor in a browser uses, each a direct way to a
# file or to the server: the conversions and document readers (which take
# any file they are handed and answer the relay's private address), the
# legacy and WASM prefixes, Collabora's own file server, remote links (off).
_BLOCKED_TOP = frozenset({"lool", "wasm", "wopi"})
_BLOCKED_COOL = frozenset({
    "convert-to", "extract-link-targets", "extract-document-structure",
    "transform-document-structure", "get-thumbnail", "render-search-result", "links",
})


def _is_blocked_subpath(encoded_path: str) -> bool:
    """Collabora's admin console / metrics must NEVER be reachable through the
    public proxy: the admin WebSocket can manage every open document and the
    metrics endpoint leaks operational data. Hard-404 them regardless of auth,
    with the direct-file endpoints of ``_BLOCKED_TOP`` and ``_BLOCKED_COOL``.

    The check is on the DECODED path because the backend URL-decodes before
    routing — otherwise ``/cool/%61dminws`` (or double-encoded forms) would slip
    past a match on the raw encoded string and still reach ``adminws``."""
    p = _decoded(encoded_path).lower().lstrip("/")
    segments = [seg for seg in p.split("/") if seg]
    first = segments[0] if segments else ""
    second = segments[1] if len(segments) > 1 else ""
    return (
        "adminws" in p
        or "getmetrics" in p
        or "admin.html" in p
        or p.startswith("cool/admin")
        # 26.04's raw-bytes socket and its fetch/put (the mobile and WASM
        # builds of the client use them, the browser's never does) and the
        # integrators' WOPI reachability test.
        or p.startswith("co/collab")
        or "wopiaccesscheck" in p
        or first in _BLOCKED_TOP
        or (first == "cool" and second in _BLOCKED_COOL)
    )


# The paths Collabora 26.04 serves and its editor in a browser uses, judged
# on the RAW path's segments (the document's WOPISrc rides as ONE encoded
# segment of ``cool/<src>/…``). Recorded on a walk of every editor feature
# (open, the welcome dialog, insert image and media, compare documents,
# export as PDF and as the document's type, print, comments, copy and
# paste): the static client under ``browser/``, the socket, an upload
# (``insertfile``), a download (export, print), media playback and its
# captions. Anything else answers 404, ahead of the deny list. Kept though
# the walk did not reach them: the health probes (``livez``, ``readyz``) and
# a browser's own fetches (``favicon.ico``, ``robots.txt``); ``hosting/
# capabilities`` and ``discovery``, which the client reads to learn the
# server's features; ``cool/clipboard``, the rich-content copy a browser
# with the clipboard API fetches; ``cool/signature``, electronic signing;
# ``avatar``, a commenter's picture; ``browser-logging``, the client's log
# when its socket is down; ``cool/<src>/ws``, the socket of an older client.
_STATIC_ROOT = "browser"
_DOCUMENT_SCHEMES = frozenset({"http", "https"})
_ALLOWED_TOP = frozenset({"livez", "readyz", "favicon.ico", "robots.txt"})
_ALLOWED_HOSTING = frozenset({"discovery", "capabilities"})
_ALLOWED_COOL = frozenset({"clipboard", "media", "mediavtt", "signature"})
# cool/<src>/<action>: how many segments may follow the action (download
# names its id, avatar may name a user).
_ALLOWED_DOC_ACTIONS = {"insertfile": (0, 0), "download": (1, 3), "avatar": (0, 2),
                        "browser-logging": (0, 0)}


def _names_a_document(segment: str) -> bool:
    """``segment`` (one raw segment) decodes to a WOPISrc: an http(s) URL."""
    src = urlsplit(_decoded(segment))
    return src.scheme in _DOCUMENT_SCHEMES and bool(src.netloc)


def _is_allowed_subpath(encoded_path: str, *, socket: bool = False) -> bool:
    """A path the editor uses: the HTTP set, or with ``socket`` the editor's
    socket (``cool/ws``, and ``cool/<src>/ws`` of the older client)."""
    # A dot segment, raw or percent-encoded once or more, is refused before
    # the lists: the client below the relay folds it (``browser/../cool/x``
    # reaches ``cool/x``), so the lists would judge one path and forward
    # another. No path the editor uses has one, its WOPISrc included.
    if any(part in (".", "..") for part in _decoded(encoded_path).split("/")):
        return False
    segs = encoded_path.split("/")
    low = [seg.lower() for seg in segs]
    if socket:
        return low == ["cool", "ws"] or (
            len(segs) == 3 and low[0] == "cool" and low[2] == "ws" and _names_a_document(segs[1]))
    if low[0] == _STATIC_ROOT:
        return len(segs) >= 2
    if len(segs) == 1:
        return low[0] in _ALLOWED_TOP
    if low[0] == "hosting":
        return len(segs) == 2 and low[1] in _ALLOWED_HOSTING
    if low[0] != "cool":
        return False
    if len(segs) == 2:
        return low[1] in _ALLOWED_COOL
    bounds = _ALLOWED_DOC_ACTIONS.get(low[2])
    return (bounds is not None and _names_a_document(segs[1])
            and bounds[0] <= len(segs) - 3 <= bounds[1])


# Every access_token a request or a frame carries, wherever it sits: the
# query, the path of the legacy socket URL (/cool/<WOPISrc?access_token=…>/ws)
# and the query inside a WOPISrc value, read after the decoding.
_TOKEN_RE = re.compile(r"access_token=([^&/?#\s]*)")


def _tokens_in(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(_decoded(text)) if t]


def _request_tokens(scope: Mapping) -> list[str]:
    raw = scope.get("query_string", b"")
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "replace")
    return _tokens_in(_raw_subpath(scope)) + _tokens_in(raw)


def _token_names(token: str, sub: str) -> bool:
    """Whether ``token`` is a valid WOPI token minted for the person ``sub``."""
    from api.media.wopi import validate_wopi_token
    claims = validate_wopi_token(token)
    return claims is not None and claims.get("user_sub") == sub


async def _collabora_authorized(scope: Mapping, cookies: dict) -> str | None:
    """Gate the proxy so it isn't an open relay to the Collabora backend: the
    person the request's ``session`` cookie names, or None.

    In sub-path mode the editor is same-origin with the dashboard (the
    Android app's WebView loads the server's own address too), so every
    request of the frame carries the ``session`` cookie. It passes when it
    validates AND its person still holds on the HTTP routes' own rules
    (``wopi.person_holds``: the users row, the token epoch a sign out
    everywhere or a password set moves, the forced password change and the
    2FA enrolment), read once per person and issue time per 10 s, so an
    editor's burst of asset requests costs one read. A WOPI token alone
    admits nothing: Collabora signs every call it makes for a token it is
    handed, so a token copied from its log would otherwise open the
    document through here. Every token the request carries (the query, the
    legacy socket path, a WOPISrc value; ``_request_tokens``) must name the
    cookie's person, and a socket must carry one.
    """
    from api.media.wopi import person_holds
    from auth.providers import validate_session_jwt
    sc = cookies.get("session")
    if not sc:
        return None
    payload = validate_session_jwt(sc)
    sub = (payload or {}).get("sub") or ""
    if not sub or not await person_holds(sub, payload.get("iat") if payload else None):
        return None
    found = _request_tokens(scope)
    if scope.get("type") == "websocket" and not found:
        return None
    if not all(_token_names(tok, sub) for tok in found):
        return None
    return sub


def _load_frame_ok(frame: str | bytes, sub: str) -> bool:
    """A client frame the relay passes upstream, text or binary: every WOPI
    token a ``load`` frame carries must be minted for the socket's person
    (the frame, not the socket's URL, names the document Collabora opens)."""
    head = frame[:16].lstrip()
    if not (head.startswith(b"load") if isinstance(head, bytes) else head.startswith("load")):
        return True
    text = frame.decode("utf-8", "replace") if isinstance(frame, bytes) else frame
    return all(_token_names(tok, sub) for tok in _tokens_in(text))


# An open editor socket judges the credential that admitted it again at
# this interval (the dashboard socket's): a sign out everywhere, a password
# change or the forced change closes it within the window.
_RELAY_REVALIDATE_S = 60.0


async def _socket_still_authorized(scope: Mapping, cookies: dict) -> bool:
    """``_collabora_authorized`` for an open socket: the handshake's cookie
    is judged without its expiry (the sliding refresh renews only the
    browser's copy, the sign-in is what counts): its signature, its purpose,
    the revocation of its sign-in, then its person. A store that could not
    be read keeps the socket (as the dashboard socket's revalidation does):
    only a person known to no longer hold closes it."""
    from api.media.wopi import person_standing
    from auth import session_revocation, tokens

    sc = cookies.get("session")
    if not sc:
        return False
    payload = tokens.verify(sc, tokens.SIGN_IN, verify_exp=False)
    return bool(
        payload
        and not session_revocation.is_revoked(session_revocation.session_cookie_id(payload))
        and await person_standing(payload.get("sub") or "", payload.get("iat")) is not False)


async def _close_when_unauthorized(websocket: WebSocket, upstream, scope: Mapping,
                                   cookies: dict, path: str) -> None:
    """Re-judge an open editor socket every ``_RELAY_REVALIDATE_S``; when it
    no longer holds, close both ends (the browser's with 1008: Collabora's
    reconnect is then refused at the handshake and it says it cannot
    connect)."""
    while True:
        await asyncio.sleep(_RELAY_REVALIDATE_S)
        if await _socket_still_authorized(scope, cookies):
            continue
        logger.info("Collabora WS closed: its sign-in no longer holds (%s)", path[:60])
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await websocket.close(code=1008, reason="sign-in ended")
        return


def _raw_subpath(scope: Mapping) -> str:
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


# Collabora's discovery is about 100 KB; a backend answering more is not
# Collabora.
_DISCOVERY_MAX_BYTES = 2 * 1024 * 1024
_DISCOVERY_TIMEOUT_S = 10.0


_plain_discovery_warned: set[str] = set()


def _warn_plain_discovery(url: str) -> None:
    """Once per address: Collabora's keys read over plain http from a host
    that is not this machine or the LAN can be swapped on the way."""
    parts = urlparse(url)
    host = parts.hostname or ""
    if parts.scheme != "http" or host in _plain_discovery_warned or host == "localhost":
        return
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private:
            return
    except ValueError:
        pass
    _plain_discovery_warned.add(host)
    logger.warning("Collabora's discovery (its signing key) is read over plain http from %s: "
                   "serve COLLABORA_URL over https", host)


async def read_discovery() -> str | None:
    """Collabora's ``/hosting/discovery`` document, for the WOPI proof keys
    (``wopi_proof``), or None when preview is off or the read failed.

    Sub-path mode reads the backend (``COLLABORA_BACKEND_URL``) with the
    public ``Host``, so its editor addresses are the ones a browser gets;
    otherwise ``COLLABORA_URL``. The dial is checked
    (``pinned_http.model_server``: this machine, the LAN or the internet,
    never link-local or a metadata address) and never goes through an
    environment proxy: the backend is on this host or the compose network.
    """
    if not config.COLLABORA_URL:
        return None
    headers: dict[str, str] = {}
    if _is_subpath_mode():
        url = _backend_http_url("hosting/discovery")
        if public := _public_host():
            headers["host"] = public
    else:
        url = f"{config.COLLABORA_URL.rstrip('/')}/hosting/discovery"
        _warn_plain_discovery(url)
    try:
        async with httpx.AsyncClient(
                transport=pinned_http.CheckedTransport(pinned_http.model_server),
                trust_env=False, timeout=_DISCOVERY_TIMEOUT_S, follow_redirects=False) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code != 200:
                    logger.warning("Collabora discovery answered %s", resp.status_code)
                    return None
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > _DISCOVERY_MAX_BYTES:
                        logger.warning("Collabora discovery is larger than %d bytes",
                                       _DISCOVERY_MAX_BYTES)
                        return None
    except (httpx.HTTPError, OSError) as e:
        logger.warning("Collabora discovery could not be read: %s", type(e).__name__)
        return None
    return body.decode("utf-8", "replace")


# The backend schemes that speak TLS.
_TLS_SCHEMES = frozenset({"https", "wss"})
# Request headers the relay never forwards: its own Host stands in for the
# client's, the platform's cookies are filtered apart, and the client's
# address claims are its to make (an older Collabora may read them).
_DROPPED_REQUEST_HEADERS = frozenset({"host", "cookie", "proxyprefix", "forwarded", "x-real-ip"})


def _public_host() -> str:
    """The dashboard's public host and port (``DASHBOARD_PUBLIC_URL``'s
    netloc), sent upstream as ``Host``: with no ``server_name`` set,
    Collabora builds its editor address, its frame-ancestors list and its
    socket origin check from the request's Host (26.04 reads no
    ``X-Forwarded-*`` header), so a changed public URL needs only the
    proxy's restart."""
    try:
        return urlparse(config.DASHBOARD_PUBLIC_URL or "").netloc
    except ValueError:
        return ""


def _backend_ws_target(encoded_path: str, query: str) -> tuple[str, dict]:
    """The upstream socket: a URI whose host is the public one (the
    handshake's ``Host``) with the backend's scheme, and the connect
    arguments that dial the backend itself, with no environment proxy (an
    ``HTTP(S)_PROXY`` meant for the internet never carries the editor's
    socket)."""
    backend = urlparse(config.COLLABORA_BACKEND_URL.rstrip("/"))
    secure = backend.scheme in _TLS_SCHEMES
    host = _public_host() or backend.netloc
    uri = f"{'wss' if secure else 'ws'}://{host}{backend.path}/collabora/{encoded_path}"
    if query:
        uri += f"?{query}"
    dial: dict = {"host": backend.hostname, "port": backend.port or (443 if secure else 80),
                  "proxy": None}
    if secure:
        dial["server_hostname"] = backend.hostname
    return uri, dial


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
    # The editor is same-origin with the dashboard: a CORS preflight has
    # nothing to ask, so it is answered here and never reaches Collabora.
    if request.method == "OPTIONS":
        return Response(status_code=204)
    if not await _collabora_authorized(request.scope, request.cookies):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not _is_allowed_subpath(encoded_path):
        raise HTTPException(status_code=404, detail="Not found")

    # A client's ProxyPrefix header would make Collabora build its addresses
    # from it (and 26.04 refuses it with proxy_prefix off), and a client's
    # forwarding headers would speak for the relay: they stop here.
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in _DROPPED_REQUEST_HEADERS
        and not k.lower().startswith("x-forwarded-")
    }
    if cookie := _upstream_cookie(request.headers.get("cookie")):
        headers["cookie"] = cookie
    if public := _public_host():
        headers["host"] = public

    raw_query = request.scope.get("query_string", b"")
    if isinstance(raw_query, bytes):
        raw_query = raw_query.decode("ascii", "replace")

    target_url = _backend_http_url(encoded_path)
    if raw_query:
        target_url += f"?{raw_query}"

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=False, trust_env=False) as client:
        try:
            upstream = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=await request.body(),
            )
        except httpx.RequestError as e:
            logger.warning("Collabora backend unreachable (%s): %s", path, e)
            raise HTTPException(status_code=502, detail="Collabora backend unavailable") from e

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


# Collabora 25.04's Viewing mode (26.04 saves on the switch by itself; this
# stays for an older Collabora under WOPI_ACCEPT_UNSIGNED) (the tick that
# ends editing on a phone, the Editing/Viewing switch on a desktop) tells
# the document process to refuse edits from that view, and the process then
# refuses the view's saves too:
# every save of the unsaved edits, the idle save included, waits for an
# answer that never comes, five times, and Collabora stops storing the file.
# The relay asks for the save on the same socket right before the switch, so
# the document process saves while the view still may (its frames are
# handled in order), and the switch finds nothing unsaved.
_VIEW_EDITING = "setviewreadonly value=false"
_VIEW_VIEWING = "setviewreadonly value=true"
_SAVE_BEFORE_VIEWING = "save dontTerminateEdit=1 dontSaveIfUnmodified=1"


class _EditingWatch:
    """The frames one client frame becomes upstream: itself, after a save
    when the view leaves Editing. A view starts in Viewing on a phone, so
    only a switch after an Editing frame can have edits to save."""

    def __init__(self) -> None:
        self.editing = False

    def upstream_frames(self, text: str) -> list[str]:
        if text == _VIEW_EDITING:
            self.editing = True
        elif text == _VIEW_VIEWING and self.editing:
            self.editing = False
            return [_SAVE_BEFORE_VIEWING, text]
        return [text]


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
    # Only the editor's socket, never the admin one, and never for a request
    # without a signed-in person: refused before accepting.
    usable = _is_allowed_subpath(encoded_path, socket=True) and not _is_blocked_subpath(encoded_path)
    person = await _collabora_authorized(websocket.scope, websocket.cookies) if usable else None
    if not person:
        await websocket.close(code=1008, reason="unauthorized")
        return

    raw_query = websocket.scope.get("query_string", b"")
    if isinstance(raw_query, bytes):
        raw_query = raw_query.decode("ascii", "replace")
    upstream_url, dial = _backend_ws_target(encoded_path, raw_query)

    # Subprotocol passthrough — Collabora uses bare WS without subprotocols
    # in current versions, but be defensive in case that changes.
    subprotos = websocket.scope.get("subprotocols") or []
    chosen_subproto = subprotos[0] if subprotos else None
    if chosen_subproto:
        await websocket.accept(subprotocol=chosen_subproto)
    else:
        await websocket.accept()

    # Forward the Origin Collabora's origin check reads and the cookies (a
    # gateway's passthrough, never the platform's own). The WS library sets
    # Host (the URI's: the public one) and Sec-WebSocket-* itself.
    req_headers = {k.lower(): v for k, v in websocket.headers.items()}
    forwarded_origin = req_headers.get("origin")
    forwarded_cookie = _upstream_cookie(req_headers.get("cookie"))
    additional_headers: list[tuple[str, str]] = []
    if forwarded_cookie:
        additional_headers.append(("Cookie", forwarded_cookie))

    try:
        connect_kwargs = _upstream_connect_kwargs(
            additional_headers, forwarded_origin, chosen_subproto
        )
        async with websockets.connect(upstream_url, **connect_kwargs, **dial) as upstream:
            async def client_to_upstream():
                watch = _EditingWatch()
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            await upstream.close()
                            return
                        data = msg.get("text") if msg.get("text") is not None else msg.get("bytes")
                        if data is not None and not _load_frame_ok(data, person):
                            logger.info("Collabora WS closed: a document token of "
                                        "another person (%s)", path[:60])
                            await upstream.close()
                            await websocket.close(code=1008, reason="unauthorized")
                            return
                        if msg.get("text") is not None:
                            for frame in watch.upstream_frames(msg["text"]):
                                await upstream.send(frame)
                        elif msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                except WebSocketDisconnect:
                    with contextlib.suppress(Exception):
                        await upstream.close()

            async def upstream_to_client():
                try:
                    async for frame in upstream:
                        if isinstance(frame, str):
                            await websocket.send_text(frame)
                        else:
                            await websocket.send_bytes(frame)
                except websockets.ConnectionClosed:
                    pass

            watch = asyncio.create_task(_close_when_unauthorized(
                websocket, upstream, websocket.scope, dict(websocket.cookies), path))
            try:
                await asyncio.gather(
                    client_to_upstream(),
                    upstream_to_client(),
                    return_exceptions=True,
                )
            finally:
                watch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watch
            # Field diagnosis for "preview died by itself": record WHY the
            # upstream document socket ended (1000/1001 = orderly; 1006/None =
            # dropped, e.g. missed pongs under load or a container restart).
            if upstream.close_code not in (1000, 1001):
                logger.warning(
                    "Collabora WS upstream closed abnormally (%s): "
                    "code=%s reason=%r",
                    path, upstream.close_code, upstream.close_reason
                )
    except websockets.WebSocketException as e:
        logger.warning("Collabora WS upstream error (%s): %s", path, e)
    except Exception:
        logger.exception("Collabora WS proxy error (%s)", path)
    finally:
        with contextlib.suppress(RuntimeError):
            await websocket.close()
