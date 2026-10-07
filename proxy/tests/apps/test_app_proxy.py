"""The proxied routes of a folder app (APPS.md "Proxied routes", "Client
files", "Viewer identity", "Agents call apps").

Load-bearing: the routes resolve their caller from the header alone (a
cookie-only request is refused), the viewer claim reaches the app and the
cookie never does, every unsafe header and path is stripped or refused,
the client files are addressed by content and never rendered as a
document, an agent session reaches only its own apps, the socket bridge
authenticates before it wakes anything.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.providers import UserContext, get_current_user
from auth.session_token import create_session_token
from tests.conftest import live_session_token
from api.apps import app_proxy
from services.apps import app_supervisor, app_tokens, releases
from storage import database as task_store

client = TestClient(app)

AGENT = "proxy-agent"
OTHER_AGENT = "proxy-other"


def _user(sub: str = "alice-sub", role: str = "member",
          agent_roles: dict[str, str] | None = None, is_api_key: bool = False) -> UserContext:
    roles = {AGENT: "manager"} if agent_roles is None else agent_roles
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role=role,
                       agents=list(roles.keys()), is_api_key=is_api_key, agent_roles=roles)


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


ALICE = _user()
BOB = _user("bob-sub", agent_roles={AGENT: "viewer"})
CAROL = _user("carol-sub", agent_roles={})


@pytest.fixture(autouse=True)
def _clean():
    from api.apps import apps as apps_api
    _as(ALICE)
    apps_api._fire_rate.clear()
    app_proxy._buckets.clear()
    app_proxy._ws_counts.clear()
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()
    app_proxy._buckets.clear()
    app_proxy._ws_counts.clear()


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    for a in (AGENT, OTHER_AGENT):
        (agents_root / a / "users" / "alice" / "workspace").mkdir(parents=True)
        (agents_root / a / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    for sub, name in (("alice-sub", "alice"), ("bob-sub", "bob"), ("carol-sub", "carol")):
        task_store.upsert_user(sub, f"{sub}@test.com", name.title(), "member")
    from storage.pg import get_conn
    with get_conn() as conn:
        for sub, name in (("alice-sub", "alice"), ("bob-sub", "bob"), ("carol-sub", "carol")):
            conn.execute("UPDATE users SET username=%s WHERE sub=%s", (name, sub))
        conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    task_store.add_user_agent("bob-sub", AGENT, "viewer", "test")
    return agents_root / AGENT


# ── a fake app server ───────────────────────────────────────────────────────


class _Echo(BaseHTTPRequestHandler):
    def _reply(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path.startswith("/redir-evil"):
            self.send_response(302)
            self.send_header("Location", "https://evil.example/steal")
            self.end_headers()
            return
        if self.path.startswith("/redir-ok"):
            self.send_response(302)
            self.send_header("Location", f"/v1/apps/{self.server.app_id}/api/ok")
            self.end_headers()
            return
        payload = json.dumps({
            "method": self.command, "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body.decode("utf-8", "replace"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Set-Cookie", "evil=1; Path=/")
        self.send_header("X-App-Custom", "yes")
        self.send_header("Content-Security-Policy", "default-src *")
        self.send_header("Access-Control-Allow-Origin", "https://evil.example")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _reply

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    srv.app_id = ""
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def _folder_row(slug: str = "board", username: str = "alice", owner: str | None = "alice-sub",
                agent: str = AGENT) -> dict:
    root = f"users/{username}/workspace" if username else "workspace"
    return task_store.upsert_app(agent, username, owner, slug, title=slug.title(),
                                 rel_path=f"{root}/apps/{slug}", kind="folder")


def _install(row: dict, port: int, state: str = "up") -> app_supervisor.Instance:
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=config.get_agent_dir(row["agent"]),
                                   data_dir=config.get_agent_dir(row["agent"]),
                                   host_port=port, state=state, entry="server/index.ts")
    inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH,
                                 {"sub": f"app:{row['id']}"}, 3600)
    app_supervisor._instances[(row["id"], "live")] = inst
    return inst


def _viewer_token(app_id: str) -> str:
    return client.post(f"/v1/apps/{app_id}/viewer-token").json()["token"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── the viewer token ────────────────────────────────────────────────────────


def test_viewer_token_is_for_humans_who_may_see_the_row(agent_tree):
    row = _folder_row()
    r = client.post(f"/v1/apps/{row['id']}/viewer-token")
    assert r.status_code == 200
    claims = app_tokens.verify(r.json()["token"], row["id"], app_tokens.PURPOSE_VIEWER)
    assert claims["sub"] == "alice-sub" and claims["username"] == "alice"
    assert claims["role"] == "manager" and claims["grant"] == "" and claims["external"] is False
    assert claims["visibility"] == "personal"
    # Six a minute per (app, user).
    assert client.post(f"/v1/apps/{row['id']}/viewer-token").status_code == 429
    # A viewer of a shared row gets the viewer role; an outsider gets 404;
    # a bearer principal (an agent session) never mints one.
    shared = _folder_row("team", "", None)
    _as(BOB)
    claims = app_tokens.verify(_viewer_token(shared["id"]), shared["id"], app_tokens.PURPOSE_VIEWER)
    assert claims["role"] == "viewer" and claims["visibility"] == "shared"
    _as(CAROL)
    assert client.post(f"/v1/apps/{shared['id']}/viewer-token").status_code == 404
    _as(_user(is_api_key=True))
    assert client.post(f"/v1/apps/{shared['id']}/viewer-token").status_code == 403


# ── the proxied API ─────────────────────────────────────────────────────────


def test_api_refuses_a_cookie_only_request_and_forwards_the_claim(agent_tree, upstream):
    row = _folder_row()
    upstream.app_id = row["id"]
    _install(row, upstream.server_port)
    # The cookie principal alone (the dependency override stands in for the
    # cookie) is refused: the routes read the header only.
    r = client.get(f"/v1/apps/{row['id']}/api/hello")
    assert r.status_code == 401 and r.headers["access-control-allow-origin"] == "*"
    tok = _viewer_token(row["id"])
    r = client.post(f"/v1/apps/{row['id']}/api/items/1?x=1&y=%2F", json={"a": 1},
                    headers={**_bearer(tok), "X-My-Header": "kept", "X-OtoDock-Evil": "no",
                             "Cookie": "session=abc", "X-Forwarded-For": "9.9.9.9",
                             "Origin": "null"})
    assert r.status_code == 200, r.text
    seen = r.json()
    assert seen["method"] == "POST" and seen["path"] == "/items/1?x=1&y=%2F"
    assert json.loads(seen["body"]) == {"a": 1}
    h = seen["headers"]
    assert h["x-otodock-viewer"] == tok and h["x-otodock-basis"] == "viewer"
    assert h["x-my-header"] == "kept" and "x-otodock-evil" not in h
    assert "cookie" not in h and "authorization" not in h and "origin" not in h
    assert h["x-forwarded-for"] != "9.9.9.9" and "x-forwarded-proto" in h
    # Response hygiene: the app's cookie, CSP and CORS never reach the
    # browser; its custom header does; ours are the CORS answer.
    assert "set-cookie" not in r.headers and r.headers["x-app-custom"] == "yes"
    assert r.headers["access-control-allow-origin"] == "*"
    assert "evil.example" not in str(r.headers)
    # The app's own CSP is dropped; the platform's API default takes its place.
    assert "default-src *" not in (r.headers.get("content-security-policy") or "")
    # The app's Set-Cookie above never comes back to it — nor to any other
    # app on the same loopback host: the forwarding client keeps no jar.
    r = client.get(f"/v1/apps/{row['id']}/api/items/2", headers=_bearer(tok))
    assert r.status_code == 200 and "cookie" not in r.json()["headers"]
    # Preflight: no credentials, the two headers, no auth needed.
    p = client.options(f"/v1/apps/{row['id']}/api/hello")
    assert p.status_code == 204 and "authorization" in p.headers["access-control-allow-headers"]
    assert "access-control-allow-credentials" not in p.headers
    # HEAD goes through without a body.
    assert client.head(f"/v1/apps/{row['id']}/api/hello", headers=_bearer(tok)).status_code == 200


def test_api_paths_bodies_and_redirects_are_bounded(agent_tree, upstream, monkeypatch):
    row = _folder_row()
    upstream.app_id = row["id"]
    _install(row, upstream.server_port)
    tok = _viewer_token(row["id"])
    # (httpx, like a browser, collapses a plain `..` before sending; the
    # encoded forms reach the server as written and the check refuses them.)
    for bad in ("_health", "_handler/x", "/_handler/x", "//_handler/x", "%2e%2e/x", "a%2fb", "a%5cb"):
        assert client.get(f"/v1/apps/{row['id']}/api/{bad}", headers=_bearer(tok)).status_code == 404, bad
    with pytest.raises(Exception):
        app_proxy.check_app_path("a/../b")
    with pytest.raises(Exception):
        app_proxy.check_app_path("/_handler/x", "//_handler/x")
    monkeypatch.setattr(app_proxy, "MAX_BODY_BYTES", 16)
    r = client.post(f"/v1/apps/{row['id']}/api/big", content=b"x" * 17, headers=_bearer(tok))
    assert r.status_code == 413
    assert client.post(f"/v1/apps/{row['id']}/api/ok", content=b"x" * 16,
                       headers=_bearer(tok)).status_code == 200
    # A redirect away from the app's own routes is a 502; one under them passes.
    assert client.get(f"/v1/apps/{row['id']}/api/redir-evil", headers=_bearer(tok),
                      follow_redirects=False).status_code == 502
    r = client.get(f"/v1/apps/{row['id']}/api/redir-ok", headers=_bearer(tok), follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/v1/apps/{row['id']}/api/ok"


def test_api_tokens_of_other_apps_expired_claims_and_launch_tokens(agent_tree, upstream):
    row = _folder_row()
    other = _folder_row("other")
    upstream.app_id = row["id"]
    inst = _install(row, upstream.server_port)
    foreign = _viewer_token(other["id"])
    assert client.get(f"/v1/apps/{row['id']}/api/x", headers=_bearer(foreign)).status_code == 401
    expired = app_tokens.mint(row["id"], app_tokens.PURPOSE_VIEWER, {"sub": "alice-sub"}, -1)
    assert client.get(f"/v1/apps/{row['id']}/api/x", headers=_bearer(expired)).status_code == 401
    # The running instance's launch token is basis `app`; a fresh one for the
    # same app that no instance holds is not.
    r = client.get(f"/v1/apps/{row['id']}/api/x", headers=_bearer(inst.token))
    assert r.status_code == 200 and r.json()["headers"]["x-otodock-basis"] == "app"
    assert "x-otodock-viewer" not in r.json()["headers"]
    stale = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH, {"sub": "x"}, 60)
    assert client.get(f"/v1/apps/{row['id']}/api/x", headers=_bearer(stale)).status_code == 401
    # The master key and garbage are refused; a file app has no API.
    assert client.get(f"/v1/apps/{row['id']}/api/x", headers=_bearer("garbage")).status_code == 401
    file_row = task_store.upsert_app(AGENT, "alice", "alice-sub", "single", rel_path="x.html")
    assert client.get(f"/v1/apps/{file_row['id']}/api/x", headers=_bearer(tok := _viewer_token(file_row["id"]))).status_code == 404
    assert tok


def test_agent_basis_reaches_its_own_apps_only(agent_tree, upstream):
    shared = _folder_row("team", "", None)
    personal = _folder_row("mine")
    upstream.app_id = shared["id"]
    _install(shared, upstream.server_port)
    _install(personal, upstream.server_port)
    # The session agent's own shared app: allowed, the claim names the session.
    tok = live_session_token("sess-1", AGENT, "")
    r = client.get(f"/v1/apps/{shared['id']}/api/cards", headers=_bearer(tok))
    assert r.status_code == 200, r.text
    claim = app_tokens.verify(r.json()["headers"]["x-otodock-viewer"], shared["id"],
                              app_tokens.PURPOSE_CALLER)
    assert claim["principal"] == "agent" and claim["sub"] == "session:sess-1"
    assert claim["role"] == "agent" and claim["agent"] == AGENT
    assert r.json()["headers"]["x-otodock-basis"] == "agent"
    # Another agent's session: 404. A no-user session never owns a personal row.
    assert client.get(f"/v1/apps/{shared['id']}/api/cards",
                      headers=_bearer(live_session_token("s2", OTHER_AGENT, ""))).status_code == 404
    assert client.get(f"/v1/apps/{personal['id']}/api/cards", headers=_bearer(tok)).status_code == 404
    # The owner's session reaches the personal row with the owner's role;
    # another user's session does not; an external session never.
    mine = live_session_token("s3", AGENT, "alice-sub")
    r = client.get(f"/v1/apps/{personal['id']}/api/cards", headers=_bearer(mine))
    assert r.status_code == 200
    claim = app_tokens.verify(r.json()["headers"]["x-otodock-viewer"], personal["id"],
                              app_tokens.PURPOSE_CALLER)
    assert claim["sub"] == "alice-sub" and claim["username"] == "alice" and claim["role"] == "manager"
    assert client.get(f"/v1/apps/{personal['id']}/api/cards",
                      headers=_bearer(live_session_token("s4", AGENT, "bob-sub"))).status_code == 404
    # The owner's session on ANOTHER agent does not reach it either: a
    # personal app lives in its agent's tree, and a session never crosses one.
    assert client.get(f"/v1/apps/{personal['id']}/api/cards",
                      headers=_bearer(live_session_token("s6", OTHER_AGENT, "alice-sub"))).status_code == 404
    # A live external session is confined to its allowlist by the middleware
    # (403) before the app proxy's own refusal; a dead one is 401 everywhere.
    ext = live_session_token("s5", AGENT, "", external="phone:+15550001")
    assert client.get(f"/v1/apps/{shared['id']}/api/cards", headers=_bearer(ext)).status_code == 403
    dead_ext = create_session_token("s5-dead", AGENT, "", external="phone:+15550001")
    assert client.get(f"/v1/apps/{shared['id']}/api/cards", headers=_bearer(dead_ext)).status_code == 401


def test_api_answers_503_in_backoff_404_for_static_and_429_on_bursts(agent_tree, upstream,
                                                                     monkeypatch):
    row = _folder_row()
    inst = _install(row, upstream.server_port, state="backoff")
    inst.last_error = "exited with code 3"
    inst.next_start_at = time.monotonic() + 30
    tok = _viewer_token(row["id"])
    r = client.get(f"/v1/apps/{row['id']}/api/x", headers=_bearer(tok))
    assert r.status_code == 503 and int(r.headers["retry-after"]) >= 1
    assert r.json()["state"] == "backoff" and r.headers["x-otodock-server"] == "backoff"
    inst.state = "static"
    assert client.get(f"/v1/apps/{row['id']}/api/x", headers=_bearer(tok)).status_code == 404
    inst.state = "up"
    app_proxy._buckets.clear()
    monkeypatch.setattr(app_proxy, "RATE_PER_ACTOR", 5.0)
    codes = [client.get(f"/v1/apps/{row['id']}/api/x", headers=_bearer(tok)).status_code
             for _ in range(12)]
    assert 429 in codes and codes[0] == 200


# ── the socket bridge ───────────────────────────────────────────────────────


@pytest.fixture
def ws_upstream():
    """An echo socket server on its own loop: the first frame it sends is
    the handshake headers it saw."""
    import websockets

    loop = asyncio.new_event_loop()
    port_box: dict = {}
    ready = threading.Event()

    async def handler(conn):
        hdrs = {k.lower(): v for k, v in conn.request.headers.items()}
        await conn.send(json.dumps({"headers": hdrs, "path": conn.request.path}))
        async for msg in conn:
            await conn.send("echo:" + (msg if isinstance(msg, str) else msg.decode()))

    async def main():
        port_box["stop"] = loop.create_future()
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port_box["port"] = server.sockets[0].getsockname()[1]
            ready.set()
            await port_box["stop"]

    t = threading.Thread(target=lambda: loop.run_until_complete(main()), daemon=True)
    t.start()
    ready.wait(5)
    yield port_box["port"]
    loop.call_soon_threadsafe(port_box["stop"].set_result, None)
    t.join(5)


def test_ws_bridge_authenticates_first_then_forwards_both_ways(agent_tree, ws_upstream):
    row = _folder_row()
    _install(row, ws_upstream)
    tok = _viewer_token(row["id"])
    with client.websocket_connect(f"/v1/apps/{row['id']}/ws/chat?room=1") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": tok}))
        hello = json.loads(ws.receive_text())
        assert hello["path"] == "/chat?room=1"
        assert hello["headers"]["x-otodock-viewer"] == tok
        assert hello["headers"]["x-otodock-basis"] == "viewer"
        assert "cookie" not in hello["headers"]
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"
        # A later auth frame rotates the token and is not forwarded.
        ws.send_text(json.dumps({"type": "auth", "token": _viewer_token(row["id"])
                                 if False else tok}))
        ws.send_text("again")
        assert ws.receive_text() == "echo:again"
        assert app_supervisor.get(row["id"]).ws_count == 1
    assert app_supervisor.get(row["id"]).ws_count == 0
    # A rotation must name the same viewer: another person's claim, or a
    # link's claim for the same app, closes the socket instead of keeping
    # the first identity alive past its own expiry.
    from starlette.websockets import WebSocketDisconnect
    link_claim = app_tokens.mint(row["id"], app_tokens.PURPOSE_VIEWER, {
        "principal": "external", "sub": "", "role": "viewer", "grant": "share-1",
        "external": True}, 3600)
    other_claim = app_tokens.mint(row["id"], app_tokens.PURPOSE_VIEWER, {
        "principal": "viewer", "sub": "bob-sub", "role": "viewer", "external": False}, 3600)
    for foreign in (link_claim, other_claim):
        with client.websocket_connect(f"/v1/apps/{row['id']}/ws/chat") as ws:
            ws.send_text(json.dumps({"type": "auth", "token": tok}))
            ws.receive_text()
            ws.send_text(json.dumps({"type": "auth", "token": foreign}))
            ws.send_text("after")
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_text()
        assert e.value.code == 4401
    # No auth frame → 4400; a bad token → 4401; nothing woke the app either
    # time (the state stays as installed).
    from starlette.websockets import WebSocketDisconnect
    with client.websocket_connect(f"/v1/apps/{row['id']}/ws") as ws:
        ws.send_text("not auth")
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    assert e.value.code == 4400
    with client.websocket_connect(f"/v1/apps/{row['id']}/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": "nope"}))
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    assert e.value.code == 4401
    # Two sockets per viewer; the third is refused — and COUNTED: a page
    # that reconnects the runtime's socket itself lands here, and its author
    # has no browser console to read 4429 in (app_logs reports the count).
    from api.apps import app_proxy as _ap
    before = _ap.refused_sockets(row["id"])
    with client.websocket_connect(f"/v1/apps/{row['id']}/ws") as a, \
            client.websocket_connect(f"/v1/apps/{row['id']}/ws") as b:
        for s in (a, b):
            s.send_text(json.dumps({"type": "auth", "token": tok}))
            s.receive_text()
        with client.websocket_connect(f"/v1/apps/{row['id']}/ws") as c:
            c.send_text(json.dumps({"type": "auth", "token": tok}))
            with pytest.raises(WebSocketDisconnect) as e:
                c.receive_text()
        assert e.value.code == 4429
    assert _ap.refused_sockets(row["id"]) == before + 1


def test_ws_bridge_of_a_check_claim_watches_the_check_instance(agent_tree, ws_upstream):
    # A render's page opens the app's socket with a claim that names the
    # check instance; the bridge must watch THAT process for its lifetime,
    # not the live one (which need not exist), or it closes 1012 at once
    # and the page reconnects every second while the browser captures it.
    row = _folder_row()
    inst = app_supervisor.Instance(row_id=row["id"], name="check", row=row,
                                   release_dir=config.get_agent_dir(row["agent"]),
                                   data_dir=config.get_agent_dir(row["agent"]),
                                   host_port=ws_upstream, state="up", entry="server/index.ts")
    app_supervisor._instances[(row["id"], "check")] = inst
    app_supervisor._instances.pop((row["id"], "live"), None)
    claim = app_tokens.mint(row["id"], app_tokens.PURPOSE_VIEWER, {
        "principal": "viewer", "sub": "render:x", "username": "", "role": "viewer",
        "grant": "", "agent": row["agent"], "visibility": "shared", "external": False,
        "instance": "check", "render": "j1",
    }, 60)
    with client.websocket_connect(f"/v1/apps/{row['id']}/ws/live") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": claim}))
        hello = json.loads(ws.receive_text())
        assert hello["path"] == "/live"
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"
        time.sleep(1.2)
        ws.send_text("still here")
        assert ws.receive_text() == "echo:still here"


# ── client files ────────────────────────────────────────────────────────────


def _release(agent_tree, row: dict, files: dict[str, str]) -> tuple[dict, str]:
    src = agent_tree / row["rel_path"]
    for rel, text in files.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    rel, sha, _n = releases.cut_folder_release(row, src)
    return task_store.set_app_release(row["id"], rel, sha), sha


def test_client_document_behind_the_cookie_and_assets_by_content_address(agent_tree):
    row = _folder_row()
    row, sha = _release(agent_tree, row, {
        "app.json": "{}", "client/index.html": "<p>board</p><script src=\"app.js\"></script>",
        "client/app.js": "console.log('app')", "client/style.css": "p{}",
        "client/.secret.js": "x", "client/page.html": "<p>no</p>",
        "client/data.json": "{}", "server/index.ts": "export {}",
    })
    base = f"/v1/apps/{row['id']}/client/{sha}"
    r = client.get(f"{base}/")
    assert r.status_code == 200 and "<p>board</p>" in r.text and "otodock.fetch" in r.text
    csp = r.headers["content-security-policy"]
    assert f"connect-src http://testserver/v1/apps/{row['id']}/api/" in csp
    assert f"ws://testserver/v1/apps/{row['id']}/ws/" in csp and "sandbox allow-scripts" in csp
    assert client.get(f"{base}/index.html").status_code == 200
    # A stranger and a signed-out visitor get nothing from the document.
    _as(CAROL)
    assert client.get(f"{base}/").status_code == 404
    _as(None)
    assert client.get(f"{base}/").status_code == 401
    assert client.get(f"{base}/index.html").status_code == 401
    # The assets need no principal at all — the frame sends no cookie.
    r = client.get(f"{base}/app.js")
    assert r.status_code == 200 and r.text == "console.log('app')"
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"].startswith("attachment")
    assert "immutable" in r.headers["cache-control"]
    assert r.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert client.get(f"{base}/style.css").headers["content-type"].startswith("text/css")
    assert client.get(f"{base}/data.json").headers["content-type"].startswith("application/json")
    # Never a document, never a dotfile, never a traversal, never a wrong hash.
    for bad in ("page.html", ".secret.js", "%2e%2e/app.json", "_x.js", "missing.js", "a%2f..%2fapp.js"):
        assert client.get(f"{base}/{bad}").status_code == 404, bad
    assert client.get(f"/v1/apps/{row['id']}/client/{'0' * 64}/app.js").status_code == 404
    # A file changed on disk no longer matches the manifest.
    (agent_tree / row["release_path"] / "client" / "app.js").write_text("tampered")
    assert client.get(f"{base}/app.js").status_code == 404
    # A document served through the legacy /html route is the same document.
    _as(ALICE)
    assert "<p>board</p>" in client.get(f"/v1/apps/{row['id']}/html").text
    # The row shape carries what the frame needs.
    shaped = client.get(f"/v1/apps/{row['id']}").json()
    assert shaped["kind"] == "folder" and shaped["release_sha"] == sha
    assert shaped["has_server"] is True and shaped["server"] == "stopped"
    assert shaped["deploy_state"] == "idle" and shaped["deploying"] is False


def test_preview_copy_serves_to_the_owner_only(agent_tree):
    row = _folder_row()
    row, sha = _release(agent_tree, row, {"app.json": "{}", "client/index.html": "<p>live</p>"})
    pd = releases.preview_dir(row)
    (pd / "client").mkdir(parents=True)
    (pd / "app.json").write_text("{}")
    (pd / "client" / "index.html").write_text("<p>draft</p>")
    entries = {"app.json": {"sha256": releases._sha256(b"{}"), "size": 2},
               "client/index.html": {"sha256": releases._sha256(b"<p>draft</p>"), "size": 12}}
    (pd / "manifest.json").write_text(releases.manifest_text(entries))
    psha = releases.tree_sha(pd)
    assert "<p>draft</p>" in client.get(f"/v1/apps/{row['id']}/client/{psha}/?preview=1").text
    assert client.get(f"/v1/apps/{row['id']}/client/{psha}/").status_code == 404
    assert "<p>draft</p>" in client.get(f"/v1/apps/{row['id']}/html?preview=1").text
    assert client.get(f"/v1/apps/{row['id']}").json()["preview_sha"] == psha
    shared = _folder_row("team", "", None)
    shared, ssha = _release(agent_tree, shared, {"app.json": "{}", "client/index.html": "<p>t</p>"})
    _as(BOB)
    assert "<p>t</p>" in client.get(f"/v1/apps/{shared['id']}/html?preview=1").text
    assert client.get(f"/v1/apps/{shared['id']}").json()["preview_sha"] == ""


# ── logs and the REST twins ─────────────────────────────────────────────────


def test_logs_are_for_managers_and_the_twins_take_agents_and_apps(agent_tree, upstream):
    row = _folder_row("team", "", None)
    inst = _install(row, upstream.server_port)
    releases.log_path(row).parent.mkdir(parents=True, exist_ok=True)
    releases.log_path(row).write_text("line one\nline two\n")
    r = client.get(f"/v1/apps/{row['id']}/logs?tail=1")
    assert r.status_code == 200 and r.json()["log"] == "line two" and r.json()["server"] == "up"
    _as(BOB)
    assert client.get(f"/v1/apps/{row['id']}/logs").status_code == 403
    _as(ALICE)
    # The page (a viewer claim) never writes; the agent and the app do.
    viewer = _viewer_token(row["id"])
    r = client.patch(f"/v1/apps/{row['id']}/state", json={"patch": {"a": 1}}, headers=_bearer(viewer))
    assert r.status_code == 403
    agent_tok = live_session_token("sess-9", AGENT, "")
    r = client.patch(f"/v1/apps/{row['id']}/state", json={"patch": {"a": 1}}, headers=_bearer(agent_tok))
    assert r.status_code == 200 and r.json()["rev"] == 1
    from api.apps import apps as apps_api
    apps_api._fire_rate.clear()  # the row's two-writes-a-second rule
    r = client.patch(f"/v1/apps/{row['id']}/state", json={"doc": {"b": 2}}, headers=_bearer(inst.token))
    assert r.status_code == 200 and r.json()["rev"] == 2
    doc, rev = task_store.get_app_state(row["id"])
    assert doc == {"b": 2} and rev == 2
    r = client.post(f"/v1/apps/{row['id']}/push", json={"payload": {"hi": 1}}, headers=_bearer(inst.token))
    assert r.status_code == 200 and r.json()["screens"] == 0
    # A cookie-only request is refused on the twins too.
    assert client.patch(f"/v1/apps/{row['id']}/state", json={"patch": {}}).status_code == 401
    # Another agent's session cannot write this row.
    assert client.patch(f"/v1/apps/{row['id']}/state", json={"patch": {}},
                        headers=_bearer(live_session_token("s", OTHER_AGENT, ""))).status_code == 404


def _person(sub: str, name: str, platform: str = "member", agent_role: str = "") -> None:
    task_store.upsert_user(sub, f"{sub}@test.com", name.title(), platform)
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", (name, sub))
        conn.commit()
    if agent_role:
        task_store.add_user_agent(sub, AGENT, agent_role, "test")


def test_rest_twins_carry_the_pin_authority(agent_tree, upstream):
    """The push and state twins apply the pin authority of the
    session hooks: a shared row takes a human session at editor or above
    (or a platform admin), a no-user session and the app's own live launch
    token; the owner of a personal row keeps it."""
    from api.apps import apps as apps_api
    shared = _folder_row("team", "", None)
    inst = _install(shared, upstream.server_port)
    _person("pm-sub", "pm", agent_role="contributor")
    _person("ed-sub", "ed", agent_role="editor")
    _person("root-sub", "root", platform="admin")

    def write(token: str) -> tuple[int, int]:
        apps_api._fire_rate.clear()
        state = client.patch(f"/v1/apps/{shared['id']}/state", json={"patch": {"c": 1}},
                             headers=_bearer(token)).status_code
        apps_api._fire_rate.clear()
        push = client.post(f"/v1/apps/{shared['id']}/push", json={"payload": {}},
                           headers=_bearer(token)).status_code
        return state, push

    assert write(live_session_token("s-pm", AGENT, "pm-sub")) == (403, 403)
    assert write(live_session_token("s-bob", AGENT, "bob-sub")) == (403, 403)
    assert write(live_session_token("s-ed", AGENT, "ed-sub")) == (200, 200)
    assert write(live_session_token("s-root", AGENT, "root-sub")) == (200, 200)
    assert write(live_session_token("s-svc", AGENT, "")) == (200, 200)
    assert write(inst.token) == (200, 200)
    # The refused writes wrote nothing: four accepted writers, four revisions.
    assert task_store.get_app_state(shared["id"])[1] == 4
    # The contributor's own personal app stays theirs to write.
    mine = _folder_row("pm-board", "pm", "pm-sub")
    apps_api._fire_rate.clear()
    r = client.patch(f"/v1/apps/{mine['id']}/state", json={"patch": {"c": 1}},
                     headers=_bearer(live_session_token("s-pm2", AGENT, "pm-sub")))
    assert r.status_code == 200, r.text


def test_the_preview_launch_token_never_writes_the_live_app(agent_tree, upstream):
    """The preview copy's launch token names its instance,
    reaches the preview's own server, and never writes the live state
    document, pushes to the live frames or notifies the members."""
    from api.apps import apps as apps_api
    from api.apps import manifest as _mf
    shared = _folder_row("team", "", None)
    actions_json, err = _mf.validate_actions(
        [{"id": "n", "label": "N", "type": "platform", "method": "notifications.create"}], AGENT, shared=True)
    assert actions_json is not None, err
    shared = task_store.upsert_app(AGENT, "", None, "team", actions_json=actions_json)
    task_store.approve_app_actions(shared["id"], task_store.manifest_sig(shared), "alice-sub")
    shared = task_store.get_app(shared["id"])
    upstream.app_id = shared["id"]
    live = _install(shared, upstream.server_port)
    preview = app_supervisor.Instance(row_id=shared["id"], name="preview", row=shared,
                                      release_dir=config.get_agent_dir(AGENT),
                                      data_dir=config.get_agent_dir(AGENT),
                                      host_port=upstream.server_port, state="up", entry="server/index.ts")
    preview.token = app_tokens.mint(shared["id"], app_tokens.PURPOSE_LAUNCH,
                                    {"sub": f"app:{shared['id']}", "instance": "preview"}, 3600)
    app_supervisor._instances[(shared["id"], "preview")] = preview
    caller = asyncio.run(app_proxy.caller_from_token(preview.token, shared))
    assert caller.basis == "app" and caller.instance == "preview"
    assert asyncio.run(app_proxy.caller_from_token(live.token, shared)).instance == "live"
    hdr = _bearer(preview.token)
    apps_api._fire_rate.clear()
    assert client.patch(f"/v1/apps/{shared['id']}/state", json={"patch": {"a": 1}}, headers=hdr).status_code == 403
    assert client.post(f"/v1/apps/{shared['id']}/push", json={"payload": {}}, headers=hdr).status_code == 403
    r = client.post(f"/v1/apps/{shared['id']}/platform/notifications.create",
                    json={"args": {"title": "x"}}, headers=hdr)
    assert r.status_code == 403, r.text
    apps_api._fire_rate.clear()
    assert client.patch(f"/v1/apps/{shared['id']}/state", json={"patch": {"a": 1}},
                        headers=_bearer(live.token)).status_code == 200


def test_the_agent_caller_judges_the_tokens_holder(agent_tree, upstream):
    """A session token whose person is gone, or was minted before
    their last password change, reaches no app; a living person's token and
    a no-user token do."""
    from datetime import datetime, timedelta, timezone
    from storage.pg import get_conn
    shared = _folder_row("team", "", None)
    upstream.app_id = shared["id"]
    _install(shared, upstream.server_port)
    url = f"/v1/apps/{shared['id']}/api/cards"
    assert client.get(url, headers=_bearer(live_session_token("s-g", AGENT, "ghost-sub"))).status_code == 401
    assert client.get(url, headers=_bearer(live_session_token("s-a", AGENT, "alice-sub"))).status_code == 200
    assert client.get(url, headers=_bearer(live_session_token("s-n", AGENT, ""))).status_code == 200
    stale = live_session_token("s-b", AGENT, "bob-sub")
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_changed_at=%s WHERE sub=%s", (later, "bob-sub"))
        conn.commit()
    assert client.get(url, headers=_bearer(stale)).status_code == 401


@pytest.fixture
def owner_standing(agent_tree, upstream):
    """pm, a contributor, owns an approved personal folder app declaring the
    ``sessions`` feed and ``viewer.me``, served by a fake live instance; the
    apps' offboarding subscriber is registered for the test."""
    from api.apps import manifest as _mf
    from services.agents import offboarding
    from services.apps import app_lifecycle
    from storage.agents import agent_store
    agent_store.create_agent(AGENT, AGENT)
    _person("pm-sub", "pm", agent_role="contributor")
    _person("root-sub", "root", platform="admin")
    actions = [{"id": "s", "label": "Sessions", "type": "data_feed", "feed": "sessions"},
               {"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}]
    actions_json, err = _mf.validate_actions(actions, AGENT, shared=False)
    assert actions_json is not None, err
    row = task_store.upsert_app(AGENT, "pm", "pm-sub", "mine", title="Mine",
                                rel_path="users/pm/workspace/apps/mine", kind="folder",
                                actions_json=actions_json)
    task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), "pm-sub")
    row = task_store.get_app(row["id"])
    upstream.app_id = row["id"]
    inst = _install(row, upstream.server_port)
    app_lifecycle.register()
    yield row, inst
    offboarding.unsubscribe(app_lifecycle.SUBSCRIBER)


PM = _user("pm-sub", agent_roles={AGENT: "contributor"})
ROOT = _user("root-sub", role="admin", agent_roles={})


def _set_agents(sub: str, agents: dict[str, str]):
    _as(ROOT)
    r = client.put(f"/v1/admin/users/{sub}/agents",
                   json={"agents": list(agents), "agent_roles": agents})
    assert r.status_code == 200, r.text


def test_apps_owner_standing(owner_standing, monkeypatch):
    """A personal app is dormant once its owner lost the agent:
    every surface answers as for a missing app, its server stops at the
    removal, nothing is deleted, and a re-attach brings it back; an admin
    still sees the row."""
    from api.apps import apps as apps_api
    from starlette.websockets import WebSocketDisconnect
    row, inst = owner_standing
    _as(PM)
    claim = _viewer_token(row["id"])
    assert client.get(f"/v1/apps/{row['id']}/api/x", headers=_bearer(claim)).status_code == 200
    session = live_session_token("s-pm", AGENT, "pm-sub")
    # The server's open subscription socket loses its feeds at its next
    # frame (the row is read afresh per frame).
    socket_cm = client.websocket_connect(f"/v1/apps/{row['id']}/platform/ws")
    open_socket = socket_cm.__enter__()
    open_socket.send_text(json.dumps({"type": "auth", "token": inst.token}))
    open_socket.send_text(json.dumps({"type": "catalog_subscribe", "feed": "sessions"}))
    assert open_socket.receive_json()["ok"] is True
    woken: list[str] = []

    async def _no_wake(r, name="live"):
        woken.append(name)
        raise app_supervisor.AppUnavailable("down", 5)

    from services.apps import app_lifecycle
    forgotten: list = []

    async def _forget(rows):
        forgotten.extend(rows)

    monkeypatch.setattr(app_lifecycle, "forget_rows", _forget)
    _set_agents("pm-sub", {})
    # The subscriber stopped the server; nothing else changed (the row, its
    # approval, its handler schedules and triggers stay for a re-attach).
    assert app_supervisor.get(row["id"]) is None and forgotten == []
    open_socket.send_text(json.dumps({"type": "catalog_subscribe", "feed": "sessions"}))
    assert open_socket.receive_json() == {"type": "catalog_ack", "feed": "sessions", "ok": False}
    socket_cm.__exit__(None, None, None)
    kept = task_store.get_app(row["id"])
    assert kept and not kept["hidden"] and task_store.app_actions_approved(kept)
    monkeypatch.setattr(app_supervisor, "ensure_up", _no_wake)
    inst = _install(row, inst.host_port)  # a straggler the sweep would stop
    apps_api._fire_rate.clear()
    _as(PM)
    assert client.post(f"/v1/apps/{row['id']}/viewer-token").status_code == 404
    assert client.get(f"/v1/apps/{row['id']}/api/x", headers=_bearer(claim)).status_code == 404
    assert client.get(f"/v1/apps/{row['id']}/api/x", headers=_bearer(session)).status_code == 404
    assert client.post(f"/v1/apps/{row['id']}/platform/viewer.me", json={},
                       headers=_bearer(inst.token)).status_code == 404
    assert client.post(f"/v1/apps/{row['id']}/egress/api.example.com/x",
                       headers=_bearer(inst.token)).status_code == 404
    for token in (session, inst.token):
        assert client.patch(f"/v1/apps/{row['id']}/state", json={"patch": {"a": 1}},
                            headers=_bearer(token)).status_code == 404
        assert client.post(f"/v1/apps/{row['id']}/push", json={"payload": {}},
                           headers=_bearer(token)).status_code == 404
    with client.websocket_connect(f"/v1/apps/{row['id']}/platform/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    assert e.value.code == 1008
    assert woken == []
    # An admin still reads the row.
    _as(ROOT)
    assert client.get(f"/v1/apps/{row['id']}").status_code == 200
    # A re-attach brings the app back.
    _set_agents("pm-sub", {AGENT: "contributor"})
    apps_api._fire_rate.clear()
    _as(PM)
    assert client.post(f"/v1/apps/{row['id']}/viewer-token").status_code == 200


def test_the_owner_standing_follows_the_row_not_the_event(owner_standing):
    """The subscriber re-reads the standing: a demotion that keeps access
    stops nothing, and a person added back before the chain runs keeps the
    app running."""
    from services.agents import offboarding
    from services.apps import app_lifecycle
    row, _inst = owner_standing
    _set_agents("pm-sub", {AGENT: "viewer"})
    assert app_supervisor.get(row["id"]) is not None
    event = offboarding.OffboardEvent(
        sub="pm-sub", reason=offboarding.REMOVED, actor_sub="root-sub",
        agents=(offboarding.AgentLoss(AGENT, "viewer", "", "viewer", ""),))
    assert asyncio.run(app_lifecycle.on_offboard(event)) == 0
    assert app_supervisor.get(row["id"]) is not None


def test_a_claims_instance_word_is_one_of_the_three_else_live(agent_tree):
    """The viewer claim names the process it reaches; a word the platform
    never mints reads as the live release. A launch caller's actor carries
    its instance unless it is the live one (the preview spends its own
    buckets)."""
    row = _folder_row()
    for word, want in (("preview", app_supervisor.PREVIEW), ("check", app_supervisor.CHECK),
                       ("live", app_supervisor.LIVE), ("staging", app_supervisor.LIVE),
                       ("", app_supervisor.LIVE)):
        claim = app_tokens.mint(row["id"], app_tokens.PURPOSE_VIEWER, {
            "principal": "viewer", "sub": "alice-sub", "role": "manager", "external": False,
            "instance": word}, 60)
        assert asyncio.run(app_proxy.caller_from_token(claim, row)).instance == want, word
    assert app_supervisor.INSTANCES == (app_supervisor.LIVE, app_supervisor.PREVIEW, app_supervisor.CHECK)
    live = app_proxy.Caller(basis="app", sub=row["id"], instance=app_supervisor.LIVE)
    preview = app_proxy.Caller(basis="app", sub=row["id"], instance=app_supervisor.PREVIEW)
    assert live.actor == f"app:{row['id']}" and preview.actor == f"app:{row['id']}:preview"
    # A check instance runs unapproved code without the approval gate: its
    # launch token is never the app identity on a route that takes one.
    check = app_supervisor.Instance(row_id=row["id"], name=app_supervisor.CHECK, row=row,
                                    release_dir=config.get_agent_dir(AGENT),
                                    data_dir=config.get_agent_dir(AGENT), host_port=1, state="up")
    check.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH,
                                  {"sub": f"app:{row['id']}", "instance": app_supervisor.CHECK}, 3600)
    app_supervisor._instances[(row["id"], app_supervisor.CHECK)] = check
    r = client.get(f"/v1/apps/{row['id']}/egress/api.example.com/x", headers=_bearer(check.token))
    assert r.status_code == 401, r.text
    r = client.post(f"/v1/apps/{row['id']}/platform/viewer.me", json={}, headers=_bearer(check.token))
    assert r.status_code == 401, r.text


def test_free_functions():
    assert app_proxy._redirect_ok("", "x") and app_proxy._redirect_ok("ok", "x")
    assert app_proxy._redirect_ok("/v1/apps/x/api/y", "x")
    assert not app_proxy._redirect_ok("https://evil", "x")
    assert not app_proxy._redirect_ok("/v1/apps/x/approve", "x")
    assert not app_proxy._redirect_ok("../y", "x")
    assert app_proxy._asset_path_ok("a/b.js") and not app_proxy._asset_path_ok("a/.b.js")
    assert not app_proxy._asset_path_ok("_x/y.js") and not app_proxy._asset_path_ok("x.html")
    assert app_proxy.connect_sources("https://h", "id")[2].startswith("wss://h/")
    s = socket.socket()
    s.close()


# ── the runtime's own socket guard ──────────────────────────────────────────

_needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@_needs_node
def test_the_runtime_hands_back_one_socket_per_path():
    """Two calls for the same live path give the SAME wrapper and open ONE
    socket. The wrapper reconnects with its own backoff, so a page that
    wrapped a reconnect around it would double the sockets on every close
    until the per-viewer cap refused them all and the app looked
    permanently offline (found live on the internal install, 2026-09-13).
    """
    from api.apps.apps import APP_RUNTIME
    body = APP_RUNTIME.replace("<script>", "", 1)
    body = body[: body.rindex("</script>")]
    harness = """
      var opened = 0, listeners = [];
      globalThis.window = globalThis;
      globalThis.otodock = {};
      globalThis.location = {protocol: 'https:', host: 'h', pathname:
        '/v1/apps/00000000-0000-0000-0000-000000000000/client/abc/'};
      globalThis.parent = {postMessage: function(){}};
      globalThis.addEventListener = function(t, cb){ listeners.push([t, cb]); };
      globalThis.dispatchEvent = function(){};
      globalThis.CustomEvent = function(){};
      globalThis.document = {documentElement: {scrollHeight: 1}, body: {scrollHeight: 1}};
      globalThis.setInterval = function(){};
      globalThis.WebSocket = function(){ opened++; this.readyState = 0; this.send = function(){}; };
      globalThis.Headers = function(){ this.set = function(){}; };
      globalThis.fetch = function(){ return Promise.resolve({status: 200}); };
    """
    tail = """
      // The token the wrapper waits for, delivered the way the host does.
      for (var i = 0; i < listeners.length; i++) {
        if (listeners[i][0] === 'message') {
          listeners[i][1]({data: {source: 'otodock-host', type: 'viewer_token',
                                  token: 't', exp: 9e9}});
        }
      }
      var a = window.otodock.ws('/live');
      var b = window.otodock.ws('/live');
      var c = window.otodock.ws('/other');
      setTimeout(function(){
        console.log(JSON.stringify({same: a === b, other: c !== a, opened: opened}));
      }, 20);
    """
    out = subprocess.run(["node", "-e", harness + body + tail],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-800:]
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen == {"same": True, "other": True, "opened": 2}


# ── a placed app and the receiving agent's sessions (SHARING.md) ────────────

THIRD_AGENT = "proxy-third"

EXPORTS = {"methods": {
    "status": {"description": "the register's status"},
    "update-project": {"description": "write a project's status", "min_role": "editor"},
    "purge-cache": {"description": "drop the cache", "min_role": "manager"},
}}


def _exporting_row(slug: str = "register", *, username: str = "", owner: str | None = None,
                   approve: bool = True) -> dict:
    root = f"users/{username}/workspace" if username else "workspace"
    row = task_store.upsert_app(AGENT, username, owner, slug, title=slug.title(),
                                rel_path=f"{root}/apps/{slug}", kind="folder", actions_json="[]",
                                blocks={"exports": task_store.canonical_actions_json(EXPORTS)})
    if approve:
        task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), "alice-sub")
    return task_store.get_app(row["id"])


def _place(row: dict, agent: str = OTHER_AGENT, cap: str = "editor") -> dict:
    from storage.sharing import share_store
    return share_store.create_internal_share(
        target_kind="app", target_id=row["id"], created_by="alice-sub",
        grantee_kind=share_store.AGENT, grantee_agent=agent, role_cap=cap,
        decision=share_store.ACCEPTED, decided_by="alice-sub")


def _caller(token: str, row: dict) -> app_proxy.Caller:
    return asyncio.run(app_proxy._agent_caller(app_proxy.validate_session_token(token), row))


@pytest.fixture
def contexts():
    """Register a session's security context for the scope rule; popped after."""
    from auth.path_policy import SecurityContext
    from core.session import session_state
    sids: list[str] = []

    def _register(sid: str, agent: str, username: str, scope: str) -> None:
        session_state.set_session_security(sid, SecurityContext(
            role="viewer", username=username, agent=agent, is_admin_agent=False,
            session_scope=scope))
        sids.append(sid)

    yield _register
    for sid in sids:
        session_state._session_security.pop(sid, None)


def test_a_placement_admits_the_receiving_agents_sessions_on_its_exports(agent_tree, upstream):
    """SHARING.md "Agents use a placed app": an agent share lets every
    session of the receiving agent call the app's signed exports at the
    person's row there capped by the share, and nothing else of the app."""
    from storage.sharing import share_store
    row = _exporting_row()
    upstream.app_id = row["id"]
    _install(row, upstream.server_port)
    url = f"/v1/apps/{row['id']}/api"
    nobody = live_session_token("s-x", OTHER_AGENT, "")
    assert client.get(f"{url}/status", headers=_bearer(nobody)).status_code == 404
    share = _place(row, cap="editor")
    # A session with no person carries the no-user word, which ranks below
    # every floor: the methods without one answer, the floored ones refuse.
    r = client.get(f"{url}/status", headers=_bearer(nobody))
    assert r.status_code == 200 and r.json()["path"] == "/api/status", r.text
    assert client.post(f"{url}/update-project", headers=_bearer(nobody)).status_code == 403
    c = _caller(nobody, row)
    assert c.basis == "agent" and c.role == "agent" and c.sub == "session:s-x"
    marker = {"kind": "agent", "share_id": share["id"], "from_agent": AGENT,
              "agent": OTHER_AGENT, "role_cap": "editor"}
    assert c.extra["placement"] == marker
    # What the app sees: the placement principal and basis (never the word
    # its own agent's sessions carry), the calling agent, the capped role
    # and the placement naming the app's own agent.
    assert r.json()["headers"]["x-otodock-basis"] == "placement"
    claim = app_tokens.verify(r.json()["headers"]["x-otodock-viewer"], row["id"],
                              app_tokens.PURPOSE_CALLER)
    assert claim["principal"] == "placement" and claim["placement"] == marker
    assert claim["agent"] == OTHER_AGENT and claim["role"] == "agent" and claim["sub"] == "session:s-x"
    assert claim["session"] == "s-x" and claim["external"] is False
    # bob, a viewer of the home agent and an editor of the receiving one:
    # editor there, capped at the share's editor; the manager floor refuses.
    task_store.add_user_agent("bob-sub", OTHER_AGENT, "editor", "test")
    bob = live_session_token("s-bob", OTHER_AGENT, "bob-sub")
    r = client.post(f"{url}/update-project", headers=_bearer(bob))
    assert r.status_code == 200
    claim = app_tokens.verify(r.json()["headers"]["x-otodock-viewer"], row["id"],
                              app_tokens.PURPOSE_CALLER)
    assert (claim["principal"], claim["sub"], claim["username"], claim["role"]) == \
        ("placement", "bob-sub", "bob", "editor")
    r = client.post(f"{url}/purge-cache", headers=_bearer(bob))
    assert r.status_code == 403 and "manager" in r.text
    assert _caller(bob, row).role == "editor"
    # carol holds no row on the receiving agent: a viewer, capped.
    carol = live_session_token("s-carol", OTHER_AGENT, "carol-sub")
    assert client.get(f"{url}/status", headers=_bearer(carol)).status_code == 200
    assert client.post(f"{url}/update-project", headers=_bearer(carol)).status_code == 403
    # A platform admin with no row acts at viewer capped, never at their standing.
    task_store.upsert_user("root-sub", "root@test.com", "Root", "admin")
    root = live_session_token("s-root", OTHER_AGENT, "root-sub")
    assert client.post(f"{url}/update-project", headers=_bearer(root)).status_code == 403
    # The exported methods alone: an unexported path, an empty first segment
    # and the app's own endpoints are refused; the home agent's own session
    # keeps every path and carries no marker.
    r = client.get(f"{url}/raw", headers=_bearer(bob))
    assert r.status_code == 404 and "not exported" in r.text
    assert client.get(f"{url}//status", headers=_bearer(bob)).status_code == 404
    assert client.get(f"{url}/_handler/x", headers=_bearer(bob)).status_code == 404
    own = live_session_token("s-own", AGENT, "")
    assert client.get(f"{url}/raw", headers=_bearer(own)).status_code == 200
    assert "placement" not in _caller(own, row).extra
    # A third agent's session: 404. The cap lowered to viewer: the editor
    # floor refuses at once. The share revoked: 404 at once, no cache.
    assert client.get(f"{url}/status",
                      headers=_bearer(live_session_token("s-3", THIRD_AGENT, ""))).status_code == 404
    share_store.revoke_share(share["id"])
    _place(row, cap="viewer")
    assert client.get(f"{url}/status", headers=_bearer(bob)).status_code == 200
    assert client.post(f"{url}/update-project", headers=_bearer(bob)).status_code == 403
    for s in share_store.list_target_shares("app", row["id"]):
        share_store.revoke_share(s["id"])
    assert client.get(f"{url}/status", headers=_bearer(bob)).status_code == 404
    # An unapproved manifest answers nothing to a placed session, as the
    # broker answers; the approval opens it.
    draft = _exporting_row("draft", approve=False)
    _install(draft, upstream.server_port)
    _place(draft)
    assert client.get(f"/v1/apps/{draft['id']}/api/status", headers=_bearer(bob)).status_code == 404
    task_store.approve_app_actions(draft["id"], task_store.manifest_sig(draft), "alice-sub")
    assert client.get(f"/v1/apps/{draft['id']}/api/status", headers=_bearer(bob)).status_code == 200


def test_a_placed_call_lands_on_the_route_a_binding_reaches(agent_tree, upstream):
    """An exported method is served once, at ``/api/<method>``, where the
    broker forwards a binding's call (``app_bindings.broker``): a placed
    agent's session calling ``/v1/apps/<id>/api/<method>`` lands there too,
    while the home agent's own session keeps the path it sent, as a page
    does."""
    row = _exporting_row()
    upstream.app_id = row["id"]
    _install(row, upstream.server_port)
    _place(row, cap="editor")
    url = f"/v1/apps/{row['id']}/api"
    placed = live_session_token("s-pl", OTHER_AGENT, "")
    r = client.get(f"{url}/status?week=3", headers=_bearer(placed))
    assert r.status_code == 200 and r.json()["path"] == "/api/status?week=3", r.text
    own = live_session_token("s-own", AGENT, "")
    r = client.get(f"{url}/status", headers=_bearer(own))
    assert r.status_code == 200 and r.json()["path"] == "/status", r.text


def test_a_person_placement_admits_their_own_user_scope_sessions_alone(agent_tree, upstream, contexts):
    """A person's accepted share placed in an agent admits that person's
    own sessions mounting the user scope there, at the share's cap uncapped
    by their row; a Shared-only chat (the agent scope), a session with no
    context, a no-user session, another person and another agent are out."""
    from core.session import visibility
    from storage.sharing import share_store
    row = _exporting_row()
    _install(row, upstream.server_port)
    url = f"/v1/apps/{row['id']}/api"
    task_store.add_user_agent("carol-sub", OTHER_AGENT, "viewer", "test")
    task_store.add_user_agent("bob-sub", OTHER_AGENT, "editor", "test")
    mine = share_store.create_internal_share(target_kind="app", target_id=row["id"],
                                             created_by="alice-sub", grantee_sub="carol-sub",
                                             role_cap="editor")
    share_store.set_decision(mine["id"], share_store.ACCEPTED, "carol-sub", placed_agent=OTHER_AGENT)
    contexts("s-carol-user", OTHER_AGENT, "carol", visibility.SCOPE_USER)
    carol = live_session_token("s-carol-user", OTHER_AGENT, "carol-sub")
    assert client.post(f"{url}/update-project", headers=_bearer(carol)).status_code == 200
    assert client.post(f"{url}/purge-cache", headers=_bearer(carol)).status_code == 403
    c = _caller(carol, row)
    assert c.role == "editor" and c.extra["placement"]["kind"] == "person"
    assert c.extra["placement"]["share_id"] == mine["id"]
    contexts("s-carol-shared", OTHER_AGENT, "carol", visibility.SCOPE_AGENT)
    contexts("s-carol-third", THIRD_AGENT, "carol", visibility.SCOPE_USER)
    for tok in (live_session_token("s-carol-shared", OTHER_AGENT, "carol-sub"),
                live_session_token("s-carol-none", OTHER_AGENT, "carol-sub"),
                live_session_token("s-nobody", OTHER_AGENT, ""),
                live_session_token("s-bob", OTHER_AGENT, "bob-sub"),
                live_session_token("s-carol-third", THIRD_AGENT, "carol-sub")):
        assert client.get(f"{url}/status", headers=_bearer(tok)).status_code == 404
    # Beside an agent share at manager, her own cap wins over her capped row
    # (a viewer's row capped at manager is still a viewer); bob acts at his
    # row and a no-user session at the no-user word.
    _place(row, cap="manager")
    c = _caller(carol, row)
    assert c.role == "editor" and c.extra["placement"]["kind"] == "person"
    assert _caller(live_session_token("s-bob", OTHER_AGENT, "bob-sub"), row).role == "editor"
    assert _caller(live_session_token("s-nobody", OTHER_AGENT, ""), row).role == "agent"
    # A placed PERSONAL row answers its grantee's sessions on the receiving
    # agent; the owner's own session there holds no placement.
    personal = _exporting_row("mine", username="alice", owner="alice-sub")
    _install(personal, upstream.server_port)
    theirs = share_store.create_internal_share(target_kind="app", target_id=personal["id"],
                                               created_by="alice-sub", grantee_sub="carol-sub",
                                               role_cap="viewer")
    share_store.set_decision(theirs["id"], share_store.ACCEPTED, "carol-sub", placed_agent=OTHER_AGENT)
    assert client.get(f"/v1/apps/{personal['id']}/api/status", headers=_bearer(carol)).status_code == 200
    assert client.get(f"/v1/apps/{personal['id']}/api/status",
                      headers=_bearer(live_session_token("s-alice-o", OTHER_AGENT, "alice-sub"))).status_code == 404


def test_a_placed_session_reaches_the_exports_and_nothing_else(agent_tree, ws_upstream):
    """Push, state, the platform route and the socket refuse a placed
    agent's session, with or without a person, and a placed session's token
    never keeps alive a socket the home agent's own session opened."""
    from starlette.websockets import WebSocketDisconnect
    from api.apps import apps as apps_api
    row = _exporting_row()
    _install(row, ws_upstream)
    _place(row, cap="manager")
    task_store.add_user_agent("bob-sub", OTHER_AGENT, "manager", "test")
    bob = live_session_token("s-bob", OTHER_AGENT, "bob-sub")
    for tok in (bob, live_session_token("s-x", OTHER_AGENT, "")):
        apps_api._fire_rate.clear()
        assert client.patch(f"/v1/apps/{row['id']}/state", json={"patch": {"a": 1}},
                            headers=_bearer(tok)).status_code == 403
        apps_api._fire_rate.clear()
        assert client.post(f"/v1/apps/{row['id']}/push", json={"payload": {}},
                           headers=_bearer(tok)).status_code == 403
        assert client.post(f"/v1/apps/{row['id']}/platform/viewer.me", json={},
                           headers=_bearer(tok)).status_code == 403
        with client.websocket_connect(f"/v1/apps/{row['id']}/ws/live") as ws:
            ws.send_text(json.dumps({"type": "auth", "token": tok}))
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_text()
        assert e.value.code == 1008
    assert task_store.get_app_state(row["id"])[1] == 0
    own = live_session_token("s-own", AGENT, "bob-sub")
    with client.websocket_connect(f"/v1/apps/{row['id']}/ws/chat") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": own}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "auth", "token": bob}))
        ws.send_text("after")
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    assert e.value.code == 4401
