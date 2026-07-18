"""Tests for the platform-proxy `/collabora/*` reverse-proxy router.

We avoid spinning up the full app (which has heavy lifespan startup —
DB schema init, MCP scan, scheduler, satellite heartbeat). Instead we
mount just `collabora_proxy.router` on a minimal FastAPI app and exercise
the route logic directly.
"""

from unittest.mock import AsyncMock, MagicMock, patch

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
    monkeypatch.setattr(
        collabora_proxy, "_collabora_authorized", lambda scope, cookies: True,
    )


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
                "/collabora/foo",
                headers={
                    "Connection": "keep-alive",
                    "Upgrade": "websocket",
                    "X-Custom": "passthrough",
                },
            )

    forwarded = {k.lower(): v for k, v in mock_client.request.call_args.kwargs["headers"].items()}
    assert "connection" not in forwarded
    assert "upgrade" not in forwarded
    assert "host" not in forwarded
    assert forwarded.get("x-custom") == "passthrough"


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
            r = client.get("/collabora/anything")

    assert "content-encoding" not in {k.lower() for k in r.headers}


def test_http_returns_502_when_backend_unreachable(app_with_router, subpath_mode, authed):
    import httpx

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(side_effect=httpx.ConnectError("backend down"))

    with patch("api.media.collabora_proxy.httpx.AsyncClient", return_value=mock_client):
        with TestClient(app_with_router) as client:
            r = client.get("/collabora/foo")

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


def test_ws_url_composition():
    """The WS upstream URL converts http→ws and preserves the /collabora/ prefix + query."""
    import config
    from api.media.collabora_proxy import _backend_ws_url

    original = config.COLLABORA_BACKEND_URL
    try:
        config.COLLABORA_BACKEND_URL = "http://backend:9980"
        assert _backend_ws_url("cool/UUID/ws", "WOPISrc=foo") == \
            "ws://backend:9980/collabora/cool/UUID/ws?WOPISrc=foo"

        config.COLLABORA_BACKEND_URL = "https://backend:9980/"
        assert _backend_ws_url("x", "") == "wss://backend:9980/collabora/x"
    finally:
        config.COLLABORA_BACKEND_URL = original


# ---------------------------------------------------------------------------
# Auth gate + admin/metrics hard-block (security hardening)
# ---------------------------------------------------------------------------


def test_http_403_without_session_or_wopi_token(app_with_router, subpath_mode):
    """An unauthenticated request (no session cookie, no WOPI access_token) must
    be rejected — the proxy is not an open relay to the Collabora backend."""
    with TestClient(app_with_router) as client:
        r = client.get("/collabora/browser/dist/cool.html")
    assert r.status_code == 403


def test_http_404_admin_and_metrics_endpoints(app_with_router, subpath_mode, authed):
    """The Collabora admin console / metrics are hard-404'd even WITH auth."""
    with TestClient(app_with_router) as client:
        for path in (
            "/collabora/cool/adminws",
            "/collabora/cool/getMetrics",
            "/collabora/cool/admin",
            "/collabora/browser/abc/admin.html",
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
