"""Tests for the platform-proxy `/collabora/*` reverse-proxy router.

We avoid spinning up the full app (which has heavy lifespan startup —
DB schema init, MCP scan, scheduler, satellite heartbeat). Instead we
mount just `collabora_proxy.router` on a minimal FastAPI app and exercise
the route logic directly.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_router():
    from api.media import collabora_proxy
    app = FastAPI()
    app.include_router(collabora_proxy.router)
    return app


@pytest.fixture
def subpath_mode(monkeypatch):
    """Configure for sub-path mode: COLLABORA_URL on the dashboard host."""
    import config
    monkeypatch.setattr(config, "COLLABORA_URL", "https://example.com/collabora")
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://example.com")
    monkeypatch.setattr(config, "COLLABORA_BACKEND_URL", "http://backend:9980")


@pytest.fixture
def subdomain_mode(monkeypatch):
    """Configure for subdomain mode: COLLABORA_URL on a different host."""
    import config
    monkeypatch.setattr(config, "COLLABORA_URL", "https://collabora.example.com")
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://example.com")


@pytest.fixture
def authed(monkeypatch):
    """Bypass the proxy auth gate so the proxying-behaviour tests don't have to
    mint a real session cookie — the gate itself is covered separately below."""
    from api.media import collabora_proxy

    async def _yes(scope, cookies):
        return True
    monkeypatch.setattr(collabora_proxy, "_collabora_authorized", _yes)


# ---------------------------------------------------------------------------
# _is_subpath_mode helper
# ---------------------------------------------------------------------------


def test_is_subpath_mode_true_for_same_host(subpath_mode):
    from api.media.collabora_proxy import _is_subpath_mode
    assert _is_subpath_mode() is True


def test_is_subpath_mode_false_for_different_host(subdomain_mode):
    from api.media.collabora_proxy import _is_subpath_mode
    assert _is_subpath_mode() is False


def test_is_subpath_mode_false_when_collabora_url_empty(monkeypatch):
    import config
    monkeypatch.setattr(config, "COLLABORA_URL", "")
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://example.com")
    from api.media.collabora_proxy import _is_subpath_mode
    assert _is_subpath_mode() is False


# ---------------------------------------------------------------------------
# HTTP forward
# ---------------------------------------------------------------------------


def test_http_forwards_to_backend(app_with_router, subpath_mode, authed):
    """In sub-path mode, GET /collabora/<path> forwards to backend with prefix preserved."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"<html>collabora</html>"
    mock_resp.headers = {"content-type": "text/html", "x-frame-options": "SAMEORIGIN"}

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(return_value=mock_resp)

    with patch("api.media.collabora_proxy.httpx.AsyncClient", return_value=mock_client):
        with TestClient(app_with_router) as client:
            r = client.get("/collabora/browser/dist/cool.html?foo=bar")

    assert r.status_code == 200
    assert r.content == b"<html>collabora</html>"

    call = mock_client.request.call_args
    assert call.kwargs["method"] == "GET"
    # Backend URL keeps the /collabora/ prefix because Collabora's service_root expects it.
    # Query string passes through verbatim from the raw ASGI query_string (preserves
    # URL-encoding for Collabora's WOPISrc/access_token path-segment patterns).
    assert call.kwargs["url"] == "http://backend:9980/collabora/browser/dist/cool.html?foo=bar"


def test_http_strips_hop_by_hop_request_headers(app_with_router, subpath_mode, authed):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b""
    mock_resp.headers = {}

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(return_value=mock_resp)

    with patch("api.media.collabora_proxy.httpx.AsyncClient", return_value=mock_client):
        with TestClient(app_with_router) as client:
            client.get(
                "/collabora/browser/dist/foo.js",
                headers={
                    "Connection": "keep-alive",
                    "Upgrade": "websocket",
                    "X-Custom": "passthrough",
                },
            )

    forwarded = {k.lower(): v for k, v in mock_client.request.call_args.kwargs["headers"].items()}
    assert "connection" not in forwarded
    assert "upgrade" not in forwarded
    assert forwarded.get("host") == "example.com"  # the public host, never the client's
    assert forwarded.get("x-custom") == "passthrough"


def test_upstream_cookie_drops_the_platforms_and_keeps_the_rest():
    from api.media.collabora_proxy import _upstream_cookie

    raw = ("session=eyJ.sign.in; share_session_ab12=x; otodock_render=r; "
           "__Host-pk_handoff=n; pk_handoff=n; gateway=keep; other=1")
    assert _upstream_cookie(raw) == "gateway=keep; other=1"
    assert _upstream_cookie("session=eyJ.sign.in") is None
    assert _upstream_cookie("") is None
    assert _upstream_cookie(None) is None


def test_http_forwards_no_platform_cookie(app_with_router, subpath_mode, authed):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b""
    mock_resp.headers = {}
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(return_value=mock_resp)

    with patch("api.media.collabora_proxy.httpx.AsyncClient", return_value=mock_client):
        with TestClient(app_with_router) as client:
            client.get("/collabora/browser/dist/foo.js", headers={"Cookie": "session=eyJ.a.b; gateway=keep"})
            client.get("/collabora/browser/dist/foo.js", headers={"Cookie": "session=eyJ.a.b"})

    first, second = (
        {k.lower(): v for k, v in c.kwargs["headers"].items()}
        for c in mock_client.request.call_args_list
    )
    assert first.get("cookie") == "gateway=keep"
    assert "cookie" not in second


def test_http_strips_content_encoding_from_response(app_with_router, subpath_mode, authed):
    """Regression: httpx auto-decompresses the upstream body, so forwarding
    the upstream's `Content-Encoding: gzip` header would make the browser
    try to gunzip already-decompressed bytes and fail with
    ERR_CONTENT_DECODING_FAILED. The proxy must strip it.

    Without this fix, the Collabora iframe's bundle.js/bundle.css 200s in
    DevTools but the body is unusable → JS never executes → no WS to
    coolwsd → blank iframe (the May 2026 Greek-filename preview bug)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"console.log('decompressed');"  # already plain bytes
    mock_resp.headers = {
        "content-type": "application/javascript",
        "content-encoding": "gzip",  # ← misleading; body is NOT gzipped here
        "content-length": "999",     # ← also stale (decompressed size differs)
        "cache-control": "max-age=3600",
    }

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(return_value=mock_resp)

    with patch("api.media.collabora_proxy.httpx.AsyncClient", return_value=mock_client):
        with TestClient(app_with_router) as client:
            r = client.get("/collabora/browser/0/bundle.js")

    assert r.status_code == 200
    assert r.content == b"console.log('decompressed');"
    # Forbidden: would cause ERR_CONTENT_DECODING_FAILED in the browser.
    lower_headers = {k.lower() for k in r.headers}
    assert "content-encoding" not in lower_headers, (
        "must strip Content-Encoding (httpx already decompressed)"
    )
    # Content-Length is re-computed by Starlette to match the decompressed
    # body size — should NOT be the stale upstream value (999).
    assert r.headers["content-length"] == str(len(r.content))
    # Other headers should pass through.
    assert r.headers["cache-control"] == "max-age=3600"


def test_http_strips_content_encoding_case_insensitive(app_with_router, subpath_mode, authed):
    """Header names are case-insensitive — strip regardless of casing."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"x"
    mock_resp.headers = {
        "content-type": "text/plain",
        "Content-Encoding": "br",  # capital — must also be stripped
    }
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(return_value=mock_resp)

    with patch("api.media.collabora_proxy.httpx.AsyncClient", return_value=mock_client):
        with TestClient(app_with_router) as client:
            r = client.get("/collabora/browser/dist/anything.css")

    assert "content-encoding" not in {k.lower() for k in r.headers}


def test_http_returns_502_when_backend_unreachable(app_with_router, subpath_mode, authed):
    import httpx

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(side_effect=httpx.ConnectError("backend down"))

    with patch("api.media.collabora_proxy.httpx.AsyncClient", return_value=mock_client):
        with TestClient(app_with_router) as client:
            r = client.get("/collabora/browser/dist/foo.js")

    assert r.status_code == 502
    assert "Collabora backend unavailable" in r.json()["detail"]


def test_http_returns_404_in_subdomain_mode(app_with_router, subdomain_mode):
    """In subdomain mode the platform proxy must NOT intercept /collabora/*."""
    with TestClient(app_with_router) as client:
        r = client.get("/collabora/browser/dist/cool.html")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# WebSocket forward — verify routing without spinning up a real WS server
# ---------------------------------------------------------------------------


def test_ws_returns_close_in_subdomain_mode(app_with_router, subdomain_mode):
    """Subdomain mode: WS endpoint closes immediately with policy violation."""
    from starlette.websockets import WebSocketDisconnect

    with TestClient(app_with_router) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/collabora/cool/abc/ws"):
                pass
    # 1008 = policy violation
    assert exc_info.value.code == 1008


def test_ws_target_dials_the_backend_with_the_public_host(monkeypatch):
    """The socket's URI carries the public host (the handshake's Host, from
    which Collabora builds its addresses and its origin check) with the
    backend's scheme; the dial goes to the backend, never through an
    environment proxy, and TLS checks the backend's name."""
    import config
    from api.media.collabora_proxy import _backend_ws_target

    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "https://docs.example:8443")
    monkeypatch.setattr(config, "COLLABORA_BACKEND_URL", "http://backend:9980")
    uri, dial = _backend_ws_target("cool/ws", "WOPISrc=foo")
    assert uri == "ws://docs.example:8443/collabora/cool/ws?WOPISrc=foo"
    assert dial == {"host": "backend", "port": 9980, "proxy": None}
    monkeypatch.setattr(config, "COLLABORA_BACKEND_URL", "https://backend/")
    uri, dial = _backend_ws_target("x", "")
    assert uri == "wss://docs.example:8443/collabora/x"
    assert dial == {"host": "backend", "port": 443, "proxy": None, "server_hostname": "backend"}
    monkeypatch.setattr(config, "DASHBOARD_PUBLIC_URL", "")
    assert _backend_ws_target("x", "")[0] == "wss://backend/collabora/x"


def test_http_forward_sends_the_public_host_and_no_proxy_prefix(app_with_router, subpath_mode, authed):
    mock_resp = MagicMock(status_code=200, content=b"", headers={})
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(return_value=mock_resp)
    with patch("api.media.collabora_proxy.httpx.AsyncClient", return_value=mock_client) as cls:
        with TestClient(app_with_router) as client:
            client.get("/collabora/browser/dist/foo.js", headers={"ProxyPrefix": "https://evil.example/x/"})
    forwarded = {k.lower(): v for k, v in mock_client.request.call_args.kwargs["headers"].items()}
    assert forwarded["host"] == "example.com"
    assert "proxyprefix" not in forwarded
    with patch("api.media.collabora_proxy.httpx.AsyncClient", return_value=mock_client):
        with TestClient(app_with_router) as client:
            client.get("/collabora/browser/dist/foo.js", headers={
                "X-Forwarded-For": "1.2.3.4", "X-Forwarded-Host": "evil.example",
                "Forwarded": "for=1.2.3.4", "X-Real-IP": "1.2.3.4"})
    forwarded = {k.lower(): v for k, v in mock_client.request.call_args.kwargs["headers"].items()}
    assert not any(k.startswith("x-forwarded") for k in forwarded)
    assert cls.call_args.kwargs["trust_env"] is False


# ---------------------------------------------------------------------------
# Auth gate + admin/metrics hard-block (security hardening)
# ---------------------------------------------------------------------------


def test_http_403_without_session_or_wopi_token(app_with_router, subpath_mode):
    """An unauthenticated request (no session cookie, no WOPI access_token) must
    be rejected — the proxy is not an open relay to the Collabora backend."""
    with TestClient(app_with_router) as client:
        r = client.get("/collabora/browser/dist/cool.html")
    assert r.status_code == 403


def _cookie(sub, *, iat=None, exp_in=3600, jti="sign-in-1"):
    import time as _time

    import jwt

    import config
    now = int(_time.time())
    iat = now if iat is None else iat
    return jwt.encode({"purpose": "session", "sub": sub, "iat": iat, "exp": now + exp_in,
                       "jti": jti}, config.JWT_SECRET, algorithm="HS256")


@pytest.fixture
def relay_people(temp_db, monkeypatch):
    """Two local accounts: one free, one held by the forced password change."""
    from api.media import wopi
    from storage import database as task_store
    wopi._holder_answers.clear()
    free = task_store.create_local_user("free@t.com", "Free", "Free", "member", "x")
    held = task_store.create_local_user("held@t.com", "Held", "Held", "member", "x",
                                        must_change_password=True)
    yield free, held
    wopi._holder_answers.clear()


def test_the_gate_judges_the_person_behind_the_cookie(app_with_router, subpath_mode, relay_people, monkeypatch):
    """A cookie whose signature holds is not enough: a sign out everywhere
    (the person's token epoch), a person held by the forced password change
    and a person who is gone are refused, as on every HTTP route."""
    import time as _time
    from unittest.mock import AsyncMock

    from api.media import wopi
    from storage import database as task_store
    free, held = relay_people
    upstream = MagicMock(status_code=200, content=b"cool", headers={"content-type": "text/html"})
    monkeypatch.setattr("httpx.AsyncClient.request", AsyncMock(return_value=upstream))
    with TestClient(app_with_router) as client:
        def get(sub, **kw):
            client.cookies.set("session", _cookie(sub, **kw))
            try:
                return client.get("/collabora/browser/dist/cool.html").status_code
            finally:
                client.cookies.clear()
        assert get(free, iat=int(_time.time()) - 3) == 200
        assert get(held) == 403
        assert get("gone-sub") == 403
        task_store.bump_token_epoch(free)
        wopi._holder_answers.clear()
        assert get(free, iat=int(_time.time()) - 3) == 403


def test_a_wopi_token_alone_opens_nothing_and_must_name_the_cookies_person(
        app_with_router, subpath_mode, relay_people, monkeypatch):
    """Collabora signs every call it makes for a token it is handed, so a
    token copied from its log must not open the editor through the relay:
    no token admits a request without the session cookie, and a token in the
    query must be minted for the cookie's person."""
    from unittest.mock import AsyncMock

    from api.media import wopi
    free, held = relay_people
    upstream = MagicMock(status_code=200, content=b"x", headers={})
    monkeypatch.setattr("httpx.AsyncClient.request", AsyncMock(return_value=upstream))
    with TestClient(app_with_router) as client:
        for sub in (free, held, "agent"):
            token, _ = wopi.create_wopi_token("a/workspace/x.docx", sub, "N", "edit", "a")
            assert client.get(f"/collabora/cool/clipboard?access_token={token}").status_code == 403, sub
        own, _ = wopi.create_wopi_token("a/workspace/x.docx", free, "N", "edit", "a")
        other, _ = wopi.create_wopi_token("a/workspace/x.docx", "someone-else", "N", "edit", "a")
        client.cookies.set("session", _cookie(free))
        assert client.get(f"/collabora/cool/clipboard?access_token={own}").status_code == 200
        assert client.get(f"/collabora/cool/clipboard?access_token={other}").status_code == 403
        assert client.get("/collabora/cool/clipboard?access_token=forged").status_code == 403
        assert client.get("/collabora/browser/dist/cool.html").status_code == 200


def test_a_load_frame_must_carry_the_sockets_persons_token_in_any_form():
    from urllib.parse import quote

    from api.media import wopi
    from api.media.collabora_proxy import _load_frame_ok
    own, _ = wopi.create_wopi_token("a/workspace/x.docx", "pat", "N", "edit", "a")
    other, _ = wopi.create_wopi_token("a/workspace/x.docx", "kim", "N", "edit", "a")
    src = "http://otodock-proxy:8400/wopi/files/YQ"

    def load(token):
        return f"load url={quote(f'{src}?access_token={token}&access_token_ttl=0', safe='')} lang=en-US"

    assert _load_frame_ok(load(own), "pat")
    assert not _load_frame_ok(load(other), "pat")
    assert not _load_frame_ok(load("forged"), "pat")
    assert _load_frame_ok(f"load url={quote(src, safe='')} lang=en-US", "pat")
    assert _load_frame_ok("setviewreadonly value=true", "pat")
    assert not _load_frame_ok(" " + load(other), "pat")
    assert not _load_frame_ok(load(other).encode(), "pat")
    assert _load_frame_ok(load(own).encode(), "pat")
    two = f"load url={quote(f'{src}?access_token={own}', safe='')} url={quote(f'{src}?access_token={other}', safe='')}"
    assert not _load_frame_ok(two, "pat")
    assert _load_frame_ok(b"\x89PNG binary paste", "pat")


@pytest.mark.asyncio
async def test_an_open_socket_is_rejudged_without_the_cookies_expiry(relay_people, monkeypatch):
    """The open editor socket keeps the handshake's cookie, which the
    sliding refresh never renews: its expiry does not end the socket, its
    revocation, its person's epoch or the forced change do."""
    import time as _time

    from api.media import collabora_proxy, wopi
    from auth import session_revocation
    from storage import database as task_store
    free, held = relay_people
    now = int(_time.time())
    expired = _cookie(free, iat=now - 3, exp_in=-60, jti="sign-in-old")
    assert await collabora_proxy._socket_still_authorized({}, {"session": expired}) is True
    assert await collabora_proxy._socket_still_authorized({}, {"session": _cookie(held)}) is False
    session_revocation.revoke({"jti": "sign-in-old", "sub": free, "iat": now - 3, "exp": now + 60},
                              lifetime_s=120)
    try:
        assert await collabora_proxy._socket_still_authorized({}, {"session": expired}) is False
    finally:
        session_revocation.reset_for_tests()
    task_store.bump_token_epoch(free)
    wopi._holder_answers.clear()
    assert await collabora_proxy._socket_still_authorized(
        {}, {"session": _cookie(free, iat=now - 3, jti="other")}) is False


@pytest.mark.asyncio
async def test_a_store_that_cannot_be_read_keeps_an_open_socket(relay_people, monkeypatch):
    """A store blip at the 60 s recheck must not drop every live editor: only
    a person known to no longer hold closes the socket (a new request is
    refused meanwhile)."""
    from api.media import collabora_proxy, wopi
    free, _held = relay_people

    def _down(sub, iat):
        raise psycopg.OperationalError("store down")

    monkeypatch.setattr(wopi, "_person_holds_now", _down)
    cookie = {"session": _cookie(free)}
    assert await collabora_proxy._socket_still_authorized({}, cookie) is True
    assert await collabora_proxy._collabora_authorized({}, cookie) is None


@pytest.mark.asyncio
async def test_a_socket_whose_sign_in_ended_is_closed_at_its_recheck(monkeypatch):
    from unittest.mock import AsyncMock

    from api.media import collabora_proxy
    answers = iter([True, False])

    async def _judge(scope, cookies):
        return next(answers)

    monkeypatch.setattr(collabora_proxy, "_RELAY_REVALIDATE_S", 0.01)
    monkeypatch.setattr(collabora_proxy, "_socket_still_authorized", _judge)
    ws, upstream = MagicMock(), MagicMock()
    ws.close, upstream.close = AsyncMock(), AsyncMock()
    await collabora_proxy._close_when_unauthorized(ws, upstream, {}, {}, "cool/x/ws")
    upstream.close.assert_awaited_once()
    ws.close.assert_awaited_once_with(code=1008, reason="sign-in ended")


def test_http_404_admin_and_metrics_endpoints(app_with_router, subpath_mode, authed):
    """The Collabora admin console / metrics are hard-404'd even WITH auth."""
    with TestClient(app_with_router) as client:
        for path in (
            "/collabora/cool/adminws",
            "/collabora/cool/getMetrics",
            "/collabora/cool/admin",
            "/collabora/browser/abc/admin.html",
            "/collabora/co/collab",
            "/collabora/co/collab/fetch?WOPISrc=x",
            "/collabora/co/collab/put",
            "/collabora/hosting/wopiAccessCheck",
            "/collabora/cool/convert-to/pdf",
            "/collabora/cool/%63onvert-to",
            "/collabora/cool/get-thumbnail",
            "/collabora/cool/extract-link-targets",
            "/collabora/cool/extract-document-structure",
            "/collabora/cool/transform-document-structure",
            "/collabora/cool/render-search-result",
            "/collabora/cool/links",
            "/collabora/lool/convert-to",
            "/collabora/wasm/x",
            "/collabora/wopi/files/x",
        ):
            assert client.get(path).status_code == 404, path


def test_ws_admin_endpoint_rejected(app_with_router, subpath_mode, authed):
    """The admin WebSocket is closed (1008) before any relay, even authed."""
    from starlette.websockets import WebSocketDisconnect

    with TestClient(app_with_router) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/collabora/cool/adminws"):
                pass
    assert exc_info.value.code == 1008


# ---------------------------------------------------------------------------
# Upstream websocket keepalive
# ---------------------------------------------------------------------------


def test_ws_upstream_keepalive_is_tolerant():
    """The tunnel must outlast a Collabora stall (heavy XLSX recalculation):
    explicit ping kwargs, never the library's ~20s default teardown."""
    from api.media.collabora_proxy import _upstream_connect_kwargs

    kwargs = _upstream_connect_kwargs(None, None, None)
    assert kwargs["ping_interval"] == 25
    assert kwargs["ping_timeout"] >= 60
    assert kwargs["max_size"] is None
    assert "origin" not in kwargs and "subprotocols" not in kwargs


def test_ws_upstream_kwargs_carry_origin_and_subprotocol():
    from api.media.collabora_proxy import _upstream_connect_kwargs

    headers = [("Cookie", "a=b")]
    kwargs = _upstream_connect_kwargs(headers, "https://example.com", "cool")
    assert kwargs["additional_headers"] == headers
    assert kwargs["origin"] == "https://example.com"
    assert kwargs["subprotocols"] == ["cool"]


# ---------------------------------------------------------------------------
# A save before a view leaves Editing
# ---------------------------------------------------------------------------

def test_a_view_leaving_editing_is_saved_first():
    from api.media.collabora_proxy import _EditingWatch

    watch = _EditingWatch()
    assert watch.upstream_frames("setviewreadonly value=false") == ["setviewreadonly value=false"]
    assert watch.upstream_frames("setviewreadonly value=true") == [
        "save dontTerminateEdit=1 dontSaveIfUnmodified=1",
        "setviewreadonly value=true",
    ]


def test_a_view_that_starts_in_viewing_has_nothing_to_save():
    from api.media.collabora_proxy import _EditingWatch

    watch = _EditingWatch()
    assert watch.upstream_frames("setviewreadonly value=true") == ["setviewreadonly value=true"]
    # Editing, then Viewing twice: the second switch follows no edit.
    watch.upstream_frames("setviewreadonly value=false")
    assert len(watch.upstream_frames("setviewreadonly value=true")) == 2
    assert watch.upstream_frames("setviewreadonly value=true") == ["setviewreadonly value=true"]


def test_other_frames_pass_through_unchanged():
    from api.media.collabora_proxy import _EditingWatch

    watch = _EditingWatch()
    watch.upstream_frames("setviewreadonly value=false")
    for text in ("key type=input char=97 key=0", "save dontTerminateEdit=1 dontSaveIfUnmodified=1", ""):
        assert watch.upstream_frames(text) == [text]
    assert watch.editing



def test_a_preflight_is_answered_here(app_with_router, subpath_mode):
    """The editor is same-origin: an OPTIONS never reaches Collabora."""
    with patch("api.media.collabora_proxy.httpx.AsyncClient") as cls:
        with TestClient(app_with_router) as client:
            assert client.options("/collabora/cool/x").status_code == 204
    cls.assert_not_called()


class _FakeUpstream:
    """Collabora's end of the editor socket: records what the relay sends."""

    def __init__(self):
        import asyncio
        self.sent: list = []
        self.close_code = 1000
        self.close_reason = ""
        self._closed = asyncio.Event()

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self._closed.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._closed.wait()
        raise StopAsyncIteration

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _ws_rig(monkeypatch, app_with_router):
    from api.media import collabora_proxy
    upstream = _FakeUpstream()
    dials: list = []

    def _connect(uri, **kwargs):
        dials.append(uri)
        return upstream

    monkeypatch.setattr(collabora_proxy.websockets, "connect", _connect)
    return TestClient(app_with_router), upstream, dials


def test_the_editor_socket_takes_only_the_persons_own_token(app_with_router, subpath_mode,
                                                            relay_people, monkeypatch):
    """Every form a socket can carry a token in (the compact query, the
    legacy path, a WOPISrc value) must name the signed-in person, a socket
    must carry one, and a load frame of another person's token closes it."""
    from urllib.parse import quote

    from starlette.websockets import WebSocketDisconnect

    from api.media import wopi
    free, _held = relay_people
    own, _ = wopi.create_wopi_token("a/workspace/x.docx", free, "N", "edit", "a")
    other, _ = wopi.create_wopi_token("a/workspace/x.docx", "someone-else", "N", "edit", "a")
    src = "http://otodock-proxy:8400/wopi/files/YQ"
    client, upstream, dials = _ws_rig(monkeypatch, app_with_router)
    client.cookies.set("session", _cookie(free))

    def refused(url):
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(url) as ws:
                ws.receive_text()
        return exc_info.value.code == 1008

    assert refused(f"/collabora/cool/ws?WOPISrc={quote(src, safe='')}")  # no token at all
    assert refused(f"/collabora/cool/ws?WOPISrc={quote(src, safe='')}&access_token={other}")
    assert refused(f"/collabora/cool/{quote(f'{src}?access_token={other}&access_token_ttl=0', safe='')}/ws")
    assert refused(f"/collabora/cool/ws?WOPISrc={quote(f'{src}?access_token={other}', safe='')}"
                   f"&access_token={own}")
    assert not dials

    def load(token):
        return f"load url={quote(f'{src}?access_token={token}&access_token_ttl=0', safe='')} lang=en-US"

    legacy = f"/collabora/cool/{quote(f'{src}?access_token={own}&access_token_ttl=0', safe='')}/ws"
    with client.websocket_connect(legacy) as ws:
        ws.send_text(load(own))
        ws.send_text(" " + load(other))
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
    assert exc_info.value.code == 1008
    assert upstream.sent == [load(own)]
    upstream2 = _FakeUpstream()
    monkeypatch.setattr("api.media.collabora_proxy.websockets.connect", lambda uri, **kw: upstream2)
    with client.websocket_connect(f"/collabora/cool/ws?WOPISrc={quote(src, safe='')}&access_token={own}") as ws:
        ws.send_bytes(load(other).encode())
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
    assert exc_info.value.code == 1008 and upstream2.sent == []
