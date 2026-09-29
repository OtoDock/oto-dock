"""Folder apps on an external link (APPS.md "External links").

Load-bearing: the link's page addresses the document by the release hash
behind the unlock cookie and the assets by the hash alone; the page mints
an external viewer claim behind the same gate; the app's API and socket
under the link take that claim and nothing else; the platform methods
and the writes stay closed to it; a revoked link stops serving before the
claim expires.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

import config
from api.apps import app_proxy
from app import app
from auth.password import hash_password
from auth.providers import UserContext, get_current_user
from services.apps import app_supervisor, app_tokens, releases
from storage import database as task_store

client = TestClient(app)

AGENT = "ext-app-agent"
OWNER = "ext-owner"
ACCOUNT_PW = "owner-pass-123"
ORIGIN = {"Origin": "http://testserver"}


def _user(sub: str = OWNER, is_api_key: bool = False) -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member", agents=[AGENT],
                       agent_roles={AGENT: "manager"}, is_api_key=is_api_key)


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _people(tmp_path, monkeypatch):
    from api.apps import apps as apps_api
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", tmp_path.resolve())
    task_store.upsert_user(OWNER, f"{OWNER}@test.com", "Owner", "member")
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET password_hash=%s, username=%s WHERE sub=%s",
                     (hash_password(ACCOUNT_PW), "owner", OWNER))
        conn.commit()
    task_store.add_user_agent(OWNER, AGENT, "manager", "test")
    from auth import rate_limiter
    rate_limiter._attempts.clear()
    apps_api._fire_rate.clear()
    app_proxy._buckets.clear()
    app_proxy._ws_counts.clear()
    client.cookies.clear()
    _as(_user())
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()


class _Echo(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = json.dumps({"path": self.path,
                              "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


@pytest.fixture
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def _folder_app() -> tuple[dict, str]:
    row = task_store.upsert_app(AGENT, "owner", OWNER, "board", title="Board",
                                rel_path="users/owner/workspace/apps/board", kind="folder")
    src = config.AGENTS_DIR / AGENT / row["rel_path"]
    for rel, text in {"app.json": "{}", "client/index.html": "<p>link board</p><script src=\"app.js\"></script>",
                      "client/app.js": "console.log('link')", "client/page.html": "<p>no</p>",
                      "server/index.ts": "export {}"}.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    rel, sha, _n = releases.cut_folder_release(row, src)
    return task_store.set_app_release(row["id"], rel, sha), sha


def _install(row: dict, port: int) -> app_supervisor.Instance:
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=config.AGENTS_DIR, data_dir=config.AGENTS_DIR,
                                   host_port=port, state="up", entry="server/index.ts")
    inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH, {"sub": "x"}, 3600)
    app_supervisor._instances[(row["id"], "live")] = inst
    return inst


def _link(app_id: str) -> tuple[dict, str]:
    r = client.post("/v1/shares", json={"target_kind": "app", "target_id": app_id,
                                        "scope": "external", "password": ACCOUNT_PW})
    assert r.status_code == 200, r.text
    out = r.json()
    return out, out["link"].rsplit("/s/", 1)[1]


def _unlock(token: str, password: str):
    return client.post(f"/s/{token}/unlock", json={"password": password}, headers=ORIGIN)


def test_the_link_serves_the_folder_document_assets_and_api_behind_its_own_claim(upstream):
    row, sha = _folder_app()
    _install(row, upstream.server_port)
    out, token = _link(row["id"])
    share_id = out["share"]["id"]
    # The page names the folder; its hash (which alone opens the release's
    # files) waits for the password, like the document.
    page = client.get(f"/s/{token}").text
    assert '"folder": true' in page and sha not in page
    assert client.get(f"/s/{token}/client/{sha}/").status_code == 401
    assert client.post(f"/s/{token}/viewer-token").status_code == 401
    unlocked = _unlock(token, out["password"])
    assert unlocked.status_code == 200 and unlocked.json()["release_sha"] == sha
    assert sha in client.get(f"/s/{token}").text
    r = client.get(f"/s/{token}/client/{sha}/")
    assert r.status_code == 200 and "<p>link board</p>" in r.text and "otodock.fetch" in r.text
    csp = r.headers["content-security-policy"]
    assert f"connect-src http://testserver/s/{token}/api/" in csp
    assert f"ws://testserver/s/{token}/ws/" in csp and "/platform/" not in csp
    assert "<p>link board</p>" in client.get(f"/s/{token}/html").text
    assert client.get(f"/s/{token}/client/{'0' * 64}/").status_code == 404
    # The claim: external, this share, the app's audience.
    vt = client.post(f"/s/{token}/viewer-token").json()
    claims = app_tokens.verify(vt["token"], row["id"], app_tokens.PURPOSE_VIEWER)
    assert claims["external"] is True and claims["grant"] == share_id and claims["sub"] == ""
    # From here the frame sends no cookie: the assets ride the hash, the API
    # the claim.
    client.cookies.clear()
    r = client.get(f"/s/{token}/client/{sha}/app.js")
    assert r.status_code == 200 and r.text == "console.log('link')"
    assert r.headers["content-disposition"].startswith("attachment")
    assert client.get(f"/s/{token}/client/{sha}/page.html").status_code == 404
    assert client.get(f"/s/{token}/client/{sha}/").status_code == 401
    bearer = {"Authorization": f"Bearer {vt['token']}"}
    r = client.get(f"/s/{token}/api/cards?x=1", headers={**bearer, "Cookie": "session=abc"})
    assert r.status_code == 200, r.text
    seen = r.json()
    assert seen["path"] == "/cards?x=1" and seen["headers"]["x-otodock-basis"] == "external"
    assert seen["headers"]["x-otodock-viewer"] == vt["token"] and "cookie" not in seen["headers"]
    assert client.get(f"/s/{token}/api/_health", headers=bearer).status_code == 404
    assert client.options(f"/s/{token}/api/cards").status_code == 204
    # Nothing else opens the link's API: no claim, a dashboard viewer's
    # claim, a launch token. And the claim opens nothing beyond the API:
    # no platform methods, no writes.
    assert client.get(f"/s/{token}/api/cards").status_code == 401
    dashboard_claim = client.post(f"/v1/apps/{row['id']}/viewer-token").json()["token"]
    assert client.get(f"/s/{token}/api/cards",
                      headers={"Authorization": f"Bearer {dashboard_claim}"}).status_code == 401
    assert client.get(f"/s/{token}/api/cards",
                      headers={"Authorization": f"Bearer {app_supervisor.get(row['id']).token}"}).status_code == 401
    assert client.post(f"/v1/apps/{row['id']}/platform/viewer.me", json={}, headers=bearer).status_code == 403
    assert client.patch(f"/v1/apps/{row['id']}/state", json={"patch": {}}, headers=bearer).status_code == 403
    # A revoked link stops serving at once, claim or not.
    _as(_user())
    assert client.patch(f"/v1/shares/{share_id}", json={"revoke": True}).status_code == 200
    assert client.get(f"/s/{token}/api/cards", headers=bearer).status_code == 404
    assert client.get(f"/s/{token}/client/{sha}/app.js").status_code == 404


def test_a_dormant_owners_link_answers_404_whoever_made_it(upstream):
    """A personal app whose owner left the agent is dormant on
    its links too, whether the owner or a platform admin made the link (the
    link is judged on its creator, and a dormant app counts nobody's
    authority); a re-attach brings the link back."""
    from api.apps import manifest as _mf
    from storage.pg import get_conn
    row, _sha = _folder_app()
    _install(row, upstream.server_port)
    out, token = _link(row["id"])
    assert _unlock(token, out["password"]).status_code == 200
    bearer = {"Authorization": f"Bearer {client.post(f'/s/{token}/viewer-token').json()['token']}"}
    assert client.get(f"/s/{token}/api/cards", headers=bearer).status_code == 200
    task_store.upsert_user("ext-admin", "ext-admin@test.com", "Admin", "admin")

    def made_by(sub: str) -> None:
        with get_conn() as conn:
            conn.execute("UPDATE shares SET created_by=%s WHERE id=%s", (sub, out["share"]["id"]))
            conn.commit()

    def membership(on: bool) -> None:
        if on:
            task_store.add_user_agent(OWNER, AGENT, "manager", "test")
            return
        with get_conn() as conn:
            conn.execute("DELETE FROM user_agents WHERE sub=%s AND agent=%s", (OWNER, AGENT))
            conn.commit()

    membership(False)
    for creator in (OWNER, "ext-admin"):
        made_by(creator)
        assert not _mf.sub_can_approve_surface(creator, row), creator
        assert client.get(f"/s/{token}/api/cards", headers=bearer).status_code == 404, creator
        assert client.get(f"/s/{token}").status_code == 404, creator
    made_by(OWNER)
    membership(True)
    assert _mf.sub_can_approve_surface(OWNER, row) and _mf.sub_can_approve_surface("ext-admin", row)
    assert client.get(f"/s/{token}/api/cards", headers=bearer).status_code == 200


@pytest.fixture
def ws_upstream():
    import websockets
    loop = asyncio.new_event_loop()
    box: dict = {}
    ready = threading.Event()

    async def handler(conn):
        hdrs = {k.lower(): v for k, v in conn.request.headers.items()}
        await conn.send(json.dumps({"headers": hdrs, "path": conn.request.path}))
        async for msg in conn:
            await conn.send("echo:" + (msg if isinstance(msg, str) else msg.decode()))

    async def main():
        box["stop"] = loop.create_future()
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            box["port"] = server.sockets[0].getsockname()[1]
            ready.set()
            await box["stop"]

    t = threading.Thread(target=lambda: loop.run_until_complete(main()), daemon=True)
    t.start()
    ready.wait(5)
    yield box["port"]
    loop.call_soon_threadsafe(box["stop"].set_result, None)
    t.join(5)


def test_the_link_socket_takes_the_external_claim_only(ws_upstream):
    from starlette.websockets import WebSocketDisconnect
    row, _sha = _folder_app()
    _install(row, ws_upstream)
    out, token = _link(row["id"])
    _unlock(token, out["password"])
    vt = client.post(f"/s/{token}/viewer-token").json()["token"]
    dashboard_claim = client.post(f"/v1/apps/{row['id']}/viewer-token").json()["token"]
    client.cookies.clear()
    with client.websocket_connect(f"/s/{token}/ws/live") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": vt}))
        hello = json.loads(ws.receive_text())
        assert hello["path"] == "/live" and hello["headers"]["x-otodock-basis"] == "external"
        ws.send_text("ping")
        assert ws.receive_text() == "echo:ping"
    with client.websocket_connect(f"/s/{token}/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": dashboard_claim}))
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    assert e.value.code == 4401
