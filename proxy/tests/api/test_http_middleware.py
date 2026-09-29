"""The platform's HTTP middleware (``middleware.py``): one pure-ASGI layer.

The request body cap counts the streamed bytes, so a chunked body with no
Content-Length is capped like any other: 64 KB for the sign-in routes and for
a request with no valid credential, 2 MB for the webhook receivers, 8 MB by
default, and each large-upload route its own bound. The security headers,
the three confinements and the 500 guard keep their answers in the one
layer.

Run: cd proxy && venv/bin/pytest tests/api/test_http_middleware.py -v
"""

from __future__ import annotations

import asyncio
import time
import uuid

import jwt
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import middleware
from app import app
from auth.providers import create_session_jwt
from auth.session_token import create_session_token
from storage import pg

CHUNK = 64 * 1024


def _cookie(sub: str = "user-admin", role: str = "admin") -> str:
    return f"session={create_session_jwt(sub, f'{sub}@t.com', sub, role)}"


def _drive(method: str, path: str, *, chunks: int = 0, chunk: int = CHUNK,
           headers: list[tuple[str, str]] = (), query: str = "",
           declared: int | None = None, target=app):
    """Send ``chunks`` body chunks over raw ASGI (no Content-Length unless
    ``declared``). Returns (status, response headers, bytes handed to the
    app)."""
    sent = {"bytes": 0, "left": chunks}
    out = {"status": None, "headers": {}}

    async def receive():
        if sent["left"] > 0:
            sent["left"] -= 1
            sent["bytes"] += chunk
            return {"type": "http.request", "body": b"x" * chunk, "more_body": sent["left"] > 0}
        if chunks == 0 and sent["bytes"] == 0:
            sent["bytes"] = -1
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(3600)

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in message["headers"]}

    raw = [(k.lower().encode(), v.encode()) for k, v in headers]
    raw.append((b"content-type", b"application/json"))
    if declared is not None:
        raw.append((b"content-length", str(declared).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": query.encode(), "root_path": "",
             "headers": raw, "client": ("127.0.0.1", 5000), "server": ("127.0.0.1", 8400),
             "state": {}}

    async def run():
        await asyncio.wait_for(target(scope, receive, send), 30)

    asyncio.run(run())
    return out["status"], out["headers"], max(sent["bytes"], 0)


# ── the tiers ───────────────────────────────────────────────────────────────


def test_one_middleware_besides_the_database_guard():
    names = [m.cls.__name__ for m in app.user_middleware]
    assert names == ["_DatabaseUnavailableGuard", "PlatformHttpMiddleware"]


@pytest.mark.parametrize("path, cap", [
    ("/auth/login/local", config.MAX_AUTH_BODY_BYTES),
    ("/v1/triggers", config.MAX_UNAUTH_BODY_BYTES),
    ("/v1/webhooks/relay/slack", config.MAX_WEBHOOK_BODY_BYTES),
])
def test_a_chunked_body_is_cut_at_its_tier(path, cap):
    status, headers, handed = _drive("POST", path, chunks=64)
    assert status == 413
    assert headers.get("connection") == "close"
    assert handed <= cap + CHUNK


def test_a_declared_length_over_the_tier_is_refused_before_the_app_reads():
    status, headers, handed = _drive("POST", "/auth/login/local", declared=10 * 1024 * 1024)
    assert status == 413 and headers.get("connection") == "close"
    assert handed == 0


def test_an_authenticated_body_is_cut_at_the_default():
    status, _, handed = _drive("POST", "/v1/triggers", chunks=150, headers=[("cookie", _cookie())])
    assert status == 413
    assert handed <= config.MAX_JSON_BODY_BYTES + CHUNK


def test_a_credential_lifts_the_unauthenticated_tier():
    status, _, _ = _drive("POST", "/v1/triggers", chunks=4, headers=[("cookie", _cookie())])
    assert status not in (401, 413)
    token = create_session_token(str(uuid.uuid4()), "some-agent", "user-admin")
    status, _, _ = _drive("POST", "/v1/triggers", chunks=4,
                          headers=[("authorization", f"Bearer {token}")])
    assert status not in (401, 413)


def test_a_header_that_merely_exists_is_not_a_credential():
    status, _, _ = _drive("POST", "/v1/triggers", chunks=4, headers=[("authorization", "Bearer x")])
    assert status == 401
    status, _, _ = _drive("POST", "/v1/triggers", chunks=4)
    assert status == 413


def test_an_expired_cookie_on_a_large_body_is_a_sign_in_problem():
    now = int(time.time())
    expired = jwt.encode({"purpose": "session", "sub": "user-admin", "iat": now - 7200,
                          "exp": now - 60}, config.JWT_SECRET, algorithm="HS256")
    status, _, handed = _drive("POST", "/v1/upload", declared=10 * 1024 * 1024,
                               headers=[("cookie", f"session={expired}")])
    assert status == 401 and handed == 0


def test_a_small_unauthenticated_body_still_reaches_the_route():
    status, _, _ = _drive("POST", "/auth/forgot-password", chunks=0)
    assert status not in (401, 413)


def test_a_wopi_token_lifts_the_tier_on_wopi_only():
    # WOPI PutFile carries only its access_token. The declared length is
    # judged before the app runs (the route itself reads no body without a
    # valid token).
    from api.media.wopi import create_wopi_token, encode_file_id
    rel = "agent-x/workspace/doc.docx"
    token, _ = create_wopi_token(rel, "user-admin", "Admin", "edit", "agent-x")
    path = f"/wopi/files/{encode_file_id(rel)}/contents"
    status, _, _ = _drive("POST", path, chunks=4, query=f"access_token={token}")
    assert status not in (401, 413)
    status, _, _ = _drive("POST", path, declared=4 * CHUNK)
    assert status == 413
    status, _, _ = _drive("POST", path, declared=4 * CHUNK, query="access_token=forged")
    assert status == 401
    status, _, _ = _drive("POST", "/v1/triggers", chunks=4, query=f"access_token={token}")
    assert status == 413


def test_the_app_action_routes_take_a_megabyte_without_a_platform_credential():
    status, _, _ = _drive("POST", "/v1/apps/some-app/actions/refresh", chunks=8)
    assert status != 413
    status, _, handed = _drive("POST", "/v1/apps/some-app/actions/refresh", chunks=32)
    assert status == 413 and handed <= 1024 * 1024 + CHUNK


def test_the_app_proxy_reads_its_own_body():
    status, _, _ = _drive("POST", "/v1/apps/some-app/api/save", chunks=8)
    assert status != 413


def test_the_per_route_caps():
    backstop = config.MAX_REQUEST_BODY_BYTES
    assert middleware.body_cap("POST", "/v1/upload") == (backstop, True)
    assert middleware.body_cap("PUT", "/v1/upload/chunked/AbC_-1/3") == (
        config.UPLOAD_CHUNK_BYTES + 64 * 1024, True)
    assert middleware.body_cap("POST", "/v1/hooks/images") == (64 * 1024 * 1024, True)
    assert middleware.body_cap("POST", "/wopi/files/abc/contents") == (backstop, True)
    assert middleware.body_cap("POST", "/v1/webhooks/github/x")[1] is False
    assert middleware.body_cap("GET", "/v1/upload") == (config.MAX_JSON_BODY_BYTES, True)
    save = middleware.body_cap("PUT", "/v1/agents/a/files/workspace/notes.md")[0]
    assert save == 6 * config.INLINE_TEXT_MAX_BYTES + 64 * 1024


def test_the_zip_install_cap_covers_the_handlers_own(monkeypatch):
    from api.mcp import mcps
    cap, _ = middleware.body_cap("POST", "/v1/admin/mcps/install")
    assert cap > mcps._MAX_UPLOAD_SIZE


def test_the_file_save_cap_follows_an_unlimited_inline_setting(monkeypatch):
    monkeypatch.setattr(config, "INLINE_TEXT_MAX_BYTES", 0)
    cap, _ = middleware.body_cap("PUT", "/v1/agents/a/files/workspace/big.txt")
    assert cap == config.MAX_REQUEST_BODY_BYTES


def test_a_tier_set_to_zero_is_off(monkeypatch):
    monkeypatch.setattr(config, "MAX_JSON_BODY_BYTES", 0)
    assert middleware.body_cap("POST", "/v1/triggers")[0] == config.MAX_REQUEST_BODY_BYTES


# ── the body deadline ───────────────────────────────────────────────────────


def _trickle(path: str, gaps: list[float], *, headers: list[tuple[str, str]] = (),
             target=app, after_body: float = 0.0, limit: float = 5.0):
    """Send one 8-byte chunk after each wait in ``gaps`` (the last one ends
    the body; an empty list sends a first chunk and then nothing). Returns
    (status, response headers, response body, the messages the app read)."""
    out = {"status": None, "headers": {}, "body": b""}
    read: list[str] = []
    state = {"i": 0}

    async def receive():
        i = state["i"]
        if not gaps and i == 0:
            state["i"] = 1
            return {"type": "http.request", "body": b"{" * 8, "more_body": True}
        if i < len(gaps):
            await asyncio.sleep(gaps[i])
            state["i"] = i + 1
            return {"type": "http.request", "body": b"{" * 8, "more_body": i + 1 < len(gaps)}
        await asyncio.sleep(after_body or 3600)
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    raw = [(k.lower().encode(), v.encode()) for k, v in headers]
    raw.append((b"content-type", b"application/json"))
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": "POST", "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": raw, "client": ("127.0.0.1", 5000), "server": ("127.0.0.1", 8400),
             "state": {}}

    async def recording_receive():
        message = await receive()
        read.append(message["type"])
        return message

    async def run():
        await asyncio.wait_for(target(scope, recording_receive, send), limit)

    asyncio.run(run())
    return out["status"], out["headers"], out["body"], read


@pytest.fixture
def short_body_deadline(monkeypatch):
    monkeypatch.setattr(middleware, "_BODY_GAP_S", 0.2)
    monkeypatch.setattr(middleware, "_BODY_WHOLE_S", 0.6)


def test_a_stalled_body_without_a_credential_is_answered_408(short_body_deadline):
    status, headers, body, _ = _trickle("/auth/login/local", [])
    assert status == 408
    assert headers.get("connection") == "close"
    assert body == b'{"detail":"Request body timeout"}'


def test_a_body_that_trickles_past_the_whole_body_limit_is_answered_408(short_body_deadline):
    started = time.monotonic()
    status, headers, _, _ = _trickle("/auth/login/local", [0.1] * 40)
    assert status == 408 and headers.get("connection") == "close"
    assert time.monotonic() - started < 2.0


def test_a_slow_body_with_a_valid_credential_is_not_timed(short_body_deadline):
    status, _, _, _ = _trickle("/v1/triggers", [0.0, 0.4, 0.4], headers=[("cookie", _cookie())])
    assert status not in (401, 408, 413)


def test_a_presented_but_invalid_credential_is_timed(short_body_deadline):
    status, _, _, _ = _trickle("/v1/triggers", [], headers=[("authorization", "Bearer x")])
    assert status == 408


def test_the_webhook_receivers_keep_their_own_read_bounds(short_body_deadline):
    from api.events import webhooks
    assert not middleware._times_body("/v1/webhooks/github/sub-1")
    assert not middleware._times_body("/v1/webhooks/relay/slack")
    assert middleware._times_body("/auth/login/local")
    assert webhooks._CHUNK_GAP_S <= 10.0 and webhooks._BODY_S <= 30.0


def test_waiting_for_the_disconnect_after_the_body_is_not_timed(short_body_deadline):
    async def listener(scope, receive, send):
        message = await receive()
        assert message["type"] == "http.request" and message["more_body"] is False
        # A streaming response listens for the disconnect after the body.
        assert (await receive())["type"] == "http.disconnect"
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    wrapped = middleware.PlatformHttpMiddleware(listener)
    status, _, _, read = _trickle("/v1/stream", [0.0], target=wrapped, after_body=0.5)
    assert status == 204
    assert read == ["http.request", "http.disconnect"]


# ── the response side and the confinements ──────────────────────────────────


def test_security_headers_on_routes_refusals_and_confinements():
    client = TestClient(app)
    for resp in (
        client.get("/auth/config"),
        client.post("/auth/login/local", content=b"x" * 70_000,
                    headers={"content-type": "application/json"}),
        client.get("/v1/tasks", headers={"Authorization": f"Bearer {config.API_KEY}"}),
    ):
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["referrer-policy"] == "same-origin"


def test_the_collabora_subtree_may_be_framed():
    status, headers, _ = _drive("GET", "/collabora/hosting/discovery")
    assert "x-frame-options" not in headers
    assert headers.get("x-content-type-options") == "nosniff"


def test_the_service_key_is_still_confined():
    r = TestClient(app).get("/v1/tasks", headers={"Authorization": f"Bearer {config.API_KEY}"})
    assert r.status_code == 403
    assert r.json()["detail"] == "This endpoint is not available to the service key"


# ── the 500 guard ───────────────────────────────────────────────────────────


def _mini(exc: BaseException) -> TestClient:
    from app import install_db_unavailable_guards
    mini = FastAPI()
    middleware.register_middlewares(mini)

    @mini.get("/v1/agents/boom")
    async def _boom():
        raise exc

    install_db_unavailable_guards(mini)
    return TestClient(mini, raise_server_exceptions=False)


@pytest.mark.parametrize("exc", [
    RuntimeError("a bug"),
    psycopg.errors.QueryCanceled("canceling statement due to statement timeout"),
])
def test_a_route_error_is_a_json_500(exc):
    r = _mini(exc).get("/v1/agents/boom")
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal Server Error"}
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("exc", [
    pg.DatabaseUnavailable("breaker open"),
    ExceptionGroup("g", [pg.DatabaseUnavailable("breaker open")]),
])
def test_a_database_outage_passes_through_to_the_503(exc):
    r = _mini(exc).get("/v1/agents/boom")
    assert r.status_code == 503


@pytest.mark.parametrize("exc", [
    pg.DatabaseUnavailable("x"), pg.DatabaseUnresponsive("x"),
    psycopg.OperationalError("x"), psycopg.errors.AdminShutdown("x"),
    psycopg.errors.QueryCanceled("x"), psycopg.errors.DeadlockDetected("x"),
    RuntimeError("x"), ExceptionGroup("g", [psycopg.OperationalError("x")]),
    ExceptionGroup("g", [RuntimeError("x"), psycopg.OperationalError("x")]),
])
def test_the_outage_predicate_matches_the_apps(exc):
    import app as app_module
    assert middleware.db_unavailable(exc) == app_module._db_unavailable(exc)


# ── no 500 before auth ──────────────────────

_PERIM_1_ROUTES = [
    ("POST", "/v1/account/connect/start"),
    ("POST", "/v1/account/relay/enable"),
    ("POST", "/v1/account/relay/disable"),
    ("POST", "/v1/account/disconnect"),
    ("GET", "/v1/mcp-credential-schema"),
    ("GET", "/v1/users/me/integrations"),
    ("PUT", "/v1/users/me/integrations/x"),
    ("DELETE", "/v1/users/me/integrations/x"),
    ("PUT", "/v1/users/me/integrations/x/default-account"),
    ("PUT", "/v1/users/me/integrations/x/agent-binding"),
    ("DELETE", "/v1/users/me/integrations/x/agent-binding/y"),
    ("GET", "/v1/agents/x/mcps/y/service-account-options"),
    ("PUT", "/v1/agents/x/mcps/y/service-binding"),
    ("DELETE", "/v1/agents/x/mcps/y/service-binding"),
    ("GET", "/v1/admin/integrations"),
    ("PUT", "/v1/admin/integrations/infra/x"),
    ("DELETE", "/v1/admin/integrations/infra/x"),
    ("PUT", "/v1/admin/integrations/email-server-config"),
    ("GET", "/v1/admin/oauth-bearer-allowlist"),
    ("POST", "/v1/admin/oauth-bearer-allowlist"),
    ("DELETE", "/v1/admin/oauth-bearer-allowlist/1"),
    ("POST", "/v1/admin/oauth-bearer-allowlist/restore-defaults"),
]


@pytest.mark.parametrize("method,path", _PERIM_1_ROUTES)
def test_an_unauthenticated_call_is_refused_401_not_500(method, path):
    from fastapi.testclient import TestClient

    from app import app
    r = TestClient(app, raise_server_exceptions=False).request(method, path, json={})
    assert r.status_code in (401, 422), (method, path, r.status_code)
