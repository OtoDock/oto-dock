"""App-level error handling in app.py: the 422 body, the retired API docs
routes, and the 503 a database outage answers with.

FastAPI's default 422 body echoes the whole request back (``input``) once
per validation error, rendered by a pure-Python walk on the event loop: one
unauthenticated 1 MB body freezes the loop for about 0.4 s, 8 MB for 3 s.
"""

from __future__ import annotations

import json

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from storage import pg


@pytest.fixture(scope="module")
def client():
    from app import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# 422
# ---------------------------------------------------------------------------

def test_422_never_echoes_the_body(client):
    # A list where an object is due, under the 64 KB cap of an
    # unauthenticated body (a larger one is refused before any parse).
    body = json.dumps([0] * 20_000)
    r = client.post("/auth/login/local", content=body,
                    headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert len(r.content) < 1024
    for err in r.json()["detail"]:
        assert set(err) == {"type", "loc", "msg"}


def test_422_caps_the_number_of_errors(client):
    body = {"session_id": "s", "items": [0] * 5000}  # one error per item
    r = client.post("/v1/hooks/resolve-tool-arg-paths", json=body)
    assert r.status_code == 422
    data = r.json()
    assert len(data["detail"]) == 20
    assert data["truncated"] > 4000
    assert len(r.content) < 4096


def test_422_cuts_long_loc_parts(client):
    # The integrations router refuses an anonymous body 401 before it is
    # validated (require_user); the loc shape is what this test is about, so
    # the router guard is overridden for the call.
    from auth.providers import require_user
    key = "k" * 60_000  # under the 64 KB cap of an unauthenticated body
    client.app.dependency_overrides[require_user] = lambda: None
    try:
        r = client.put("/v1/users/me/integrations/some-mcp",
                       json={"credentials": {key: 1}})
    finally:
        client.app.dependency_overrides.pop(require_user, None)
    assert r.status_code == 422
    assert len(r.content) < 2048
    locs = [part for err in r.json()["detail"] for part in err["loc"]]
    assert all(len(str(p)) <= 64 for p in locs)


# ---------------------------------------------------------------------------
# Retired docs routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"])
def test_api_docs_routes_are_not_served(client, path):
    r = client.get(path)
    assert r.status_code == 404
    assert "openapi" not in r.text.lower()


# ---------------------------------------------------------------------------
# 503 on a database outage
# ---------------------------------------------------------------------------

def _mini_app() -> FastAPI:
    """An app with the same guards as the real one, a route and a middleware
    that raise on demand (the real middlewares raise from on-loop store
    calls, e.g. the sliding cookie refresh)."""
    from app import install_db_unavailable_guards

    mini = FastAPI()
    state = {"route": None, "middleware": None}

    @mini.middleware("http")
    async def _swallowing_mw(request: Request, call_next):
        # The shape of middleware.log_dashboard_requests: any exception from
        # the route becomes its own 500.
        try:
            return await call_next(request)
        except Exception:
            from starlette.responses import JSONResponse
            return JSONResponse({"detail": "Internal Server Error"}, status_code=500)

    # Registered last = outermost, like middleware.refresh_session_cookie.
    @mini.middleware("http")
    async def _mw(request: Request, call_next):
        response = await call_next(request)
        if state["middleware"] is not None:
            raise state["middleware"]
        return response

    @mini.get("/x")
    async def _x():
        if state["route"] is not None:
            raise state["route"]
        return {"ok": True}

    install_db_unavailable_guards(mini)
    mini.state.raise_on = state
    return mini


@pytest.mark.parametrize("exc", [
    pg.DatabaseUnavailable("breaker open"),
    pg.DatabaseUnresponsive("no answer"),
    psycopg.OperationalError("server closed the connection unexpectedly"),
    psycopg.errors.AdminShutdown("terminating connection"),
])
def test_a_database_outage_answers_503_from_a_route(exc):
    mini = _mini_app()
    mini.state.raise_on["route"] = exc
    r = TestClient(mini, raise_server_exceptions=False).get("/x")
    assert r.status_code == 503
    assert r.json() == {"detail": "Database temporarily unavailable"}
    assert r.headers.get("retry-after") == "5"


def test_a_database_outage_answers_503_from_a_middleware():
    mini = _mini_app()
    mini.state.raise_on["middleware"] = pg.DatabaseUnavailable("breaker open")
    r = TestClient(mini, raise_server_exceptions=False).get("/x")
    assert r.status_code == 503


@pytest.mark.parametrize("exc", [
    psycopg.errors.QueryCanceled("canceling statement due to statement timeout"),
    psycopg.errors.DeadlockDetected("deadlock"),
    RuntimeError("a bug"),
])
def test_other_errors_stay_500(exc):
    mini = _mini_app()
    mini.state.raise_on["route"] = exc
    r = TestClient(mini, raise_server_exceptions=False).get("/x")
    assert r.status_code == 500


def test_the_real_app_answers_503_when_the_store_is_down(client, monkeypatch):
    from api.auth import identity

    def _down(*a, **kw):
        raise pg.DatabaseUnavailable("breaker open")

    monkeypatch.setattr(identity.task_store, "count_users", _down)
    r = client.get("/auth/config")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# The client address and the two listeners
# ---------------------------------------------------------------------------

async def _echo_client(scope, receive, send):
    """A bare ASGI app answering with the client the shim left in the scope."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    body = json.dumps({"client": scope["client"][0], "peer": scope.get("otodock.peer")}).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


@pytest.fixture
def _bare_metal(monkeypatch):
    import config
    from auth import lan_check
    monkeypatch.setattr(config, "RUNNING_IN_DOCKER", False)
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [])
    monkeypatch.setattr(config, "INTERNAL_LISTENER_PORT", 0)
    lan_check.reset_state()
    yield
    lan_check.reset_state()


def test_the_shim_resolves_the_client_and_keeps_the_peer(_bare_metal):
    import asyncio

    from app import _ClientAddressShim
    seen = {}

    async def inner(scope, receive, send):
        seen.update(scope)

    shim = _ClientAddressShim(inner)
    scope = {"type": "http", "client": ("127.0.0.1", 5000), "server": ("127.0.0.1", 8400),
             "headers": [(b"x-forwarded-for", b"203.0.113.7")]}
    asyncio.run(shim(scope, None, None))
    assert seen["client"] == ("203.0.113.7", 5000) and seen["otodock.peer"] == "127.0.0.1"
    # A lifespan scope passes through untouched.
    lifespan = {"type": "lifespan"}
    asyncio.run(shim(lifespan, None, None))
    assert "otodock.peer" not in lifespan


def test_the_server_is_built_with_the_shim_and_two_listeners(_bare_metal, monkeypatch):
    import config
    from app import _BoundedWebSocketProtocol, _ClientAddressShim, _build_server
    monkeypatch.setattr(config, "HOST", "127.0.0.1")
    monkeypatch.setattr(config, "PORT", 0)
    server, sockets = _build_server(_echo_client)
    try:
        cfg = server.config
        assert cfg.proxy_headers is False and cfg.lifespan == "on" and cfg.interface == "asgi3"
        assert isinstance(cfg.app, _ClientAddressShim)
        assert cfg.log_config is None and cfg.ws is _BoundedWebSocketProtocol
        assert cfg.timeout_keep_alive == 2 and cfg.timeout_graceful_shutdown == 10
        assert cfg.http.__name__ == "_HeaderDeadlineProtocol"
        assert len(sockets) == 2 and not any(s.get_inheritable() for s in sockets)
        assert sockets[1].getsockname() == ("127.0.0.1", config.INTERNAL_LISTENER_PORT)
        assert config.INTERNAL_LISTENER_PORT > 0
    finally:
        for s in sockets:
            s.close()


def test_a_forwarded_header_counts_on_the_main_listener_only(_bare_metal, monkeypatch):
    import threading
    import time
    import urllib.request

    import config
    from app import _build_server
    monkeypatch.setattr(config, "HOST", "127.0.0.1")
    monkeypatch.setattr(config, "PORT", 0)
    server, sockets = _build_server(_echo_client)
    main_port = sockets[0].getsockname()[1]
    internal_port = sockets[1].getsockname()[1]
    thread = threading.Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started

        def client_seen(port):
            req = urllib.request.Request(f"http://127.0.0.1:{port}/",
                                         headers={"X-Forwarded-For": "203.0.113.7"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())["client"]

        # Loopback is a hop on bare metal: a same-host edge's header counts on
        # the main listener; the internal listener never reads it.
        assert client_seen(main_port) == "203.0.113.7"
        assert client_seen(internal_port) == "127.0.0.1"
    finally:
        server.should_exit = True
        thread.join(10)
