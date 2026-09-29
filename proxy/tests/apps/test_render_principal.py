"""The render principal (auth/render_principal.py, APPS.md "Security
model"): a live render token in its own cookie is a viewer of one app and
nothing else — the allowlist answers 403 everywhere else, the token dies
with its job, a session validator never takes it, a proxied peer never
gets it — and the viewer claim it mints names the check instance, which is
routed to only while a render job runs it.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth import render_principal as rp
from auth.providers import validate_session_jwt
from services.apps import app_supervisor, app_tokens
from storage import database as task_store

client = TestClient(app)
AGENT = "render-agent"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    from api.apps import app_proxy
    rp._live.clear()
    client.cookies.clear()
    monkeypatch.setattr(config, "TRUSTED_PROXIES", [])
    app_proxy._buckets.clear()
    app_proxy._ws_counts.clear()
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    yield
    rp._live.clear()
    client.cookies.clear()
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()


def _row(slug: str = "board") -> dict:
    return task_store.upsert_app(AGENT, "", None, slug, title=slug.title(),
                                 rel_path=f"workspace/apps/{slug}", kind="folder")


def _as_render(row: dict) -> tuple[str, str]:
    token, jti = rp.mint(row["id"], rp.synthetic_sub(row["id"]), AGENT)
    client.cookies.set(rp.COOKIE_NAME, token)
    return token, jti


def test_a_render_token_is_its_own_kind_and_dies_with_its_job():
    token, jti = rp.mint("app-x", "render:app-x", AGENT)
    claims = rp.verify(token)
    assert claims and claims["app"] == "app-x" and claims["jti"] == jti
    assert claims["exp"] - claims["iat"] == rp.TTL_S
    # Never a session: the dashboard socket and the cookie readers refuse it.
    assert validate_session_jwt(token) is None
    # Released: dead, whatever its expiry says.
    rp.release(jti)
    assert rp.verify(token) is None
    # A live jti for ANOTHER app does not revive it.
    rp._live[jti] = ("app-y", time.time() + 60)
    assert rp.verify(token) is None
    assert rp.verify("") is None and rp.verify("not.a.jwt") is None


def test_the_allowlist_is_bound_to_the_app_and_closed_everywhere_else():
    a = "01234567-89ab-0123-0123-0123456789ab"
    b = "abcdef01-2345-4567-4567-abcdef012345"
    for method, path in [
        ("GET", "/auth/me"), ("GET", "/auth/config"), ("GET", f"/v1/apps/{a}"),
        ("GET", f"/v1/apps/{a}/state"), ("GET", f"/v1/apps/{a}/html"),
        ("GET", f"/v1/apps/{a}/client/{'f' * 64}/"), ("GET", f"/v1/apps/{a}/client/{'f' * 64}/x/y.js"),
        ("GET", f"/v1/apps/{a}/deploy/status"), ("POST", f"/v1/apps/{a}/viewer-token"),
        ("GET", f"/v1/apps/{a}/catalog/sessions"), ("POST", f"/v1/apps/{a}/catalog/viewer.me"),
        ("POST", f"/v1/apps/{a}/catalog/files.read?x=1"), ("GET", "/v1/users/me/audio-prefs"),
    ]:
        assert rp.is_allowed(method, path, a), (method, path)
    for method, path in [
        ("POST", f"/v1/apps/{a}/actions/batch"), ("POST", f"/v1/apps/{a}/actions/go"),
        ("POST", f"/v1/apps/{a}/warm"), ("POST", f"/v1/apps/{a}/approve"),
        ("POST", f"/v1/apps/{a}/catalog/notifications.create"), ("POST", f"/v1/apps/{a}/catalog/files.write"),
        ("GET", f"/v1/apps/{b}"), ("GET", f"/v1/apps/{b}/client/{'f' * 64}/"),
        ("GET", "/v1/agents"), ("GET", "/v1/chats"), ("POST", "/v1/shares"),
        ("GET", "/ws/dashboard"), ("DELETE", f"/v1/apps/{a}"),
        ("GET", f"/v1/apps/{a}/client/{'f' * 64}/%2e%2e/x"), ("GET", f"/v1/apps/{a}/../{b}"),
    ]:
        assert not rp.is_allowed(method, path, a), (method, path)
    # The shell and its files are not judged at all.
    for path in ("/", "/apps/x", "/assets/index-abc.js", "/ui-kit/tailwind.js", "/sounds/ping.wav"):
        assert rp.is_allowed("GET", path, a)
    assert not rp.is_allowed("GET", "/auth/me", "not-an-id")


def test_the_cookie_is_a_viewer_of_one_app_and_403_everywhere_else():
    row, other = _row(), _row("other")
    token, jti = _as_render(row)
    me = client.get("/auth/me")
    assert me.status_code == 200, me.text
    user = me.json()["user"]
    assert user["render"] is True and user["platform_configured"] is True
    assert user["agents"] == [AGENT] and user["must_change_password"] is False
    r = client.get(f"/v1/apps/{row['id']}")
    assert r.status_code == 200 and r.json()["kind"] == "folder"
    assert client.get(f"/v1/apps/{row['id']}/state").status_code == 200
    blocked = rp.BLOCKED_DETAIL
    for method, path in [
        ("POST", f"/v1/apps/{row['id']}/actions/batch"), ("POST", f"/v1/apps/{row['id']}/warm"),
        ("GET", "/v1/agents"), ("GET", f"/v1/apps/{other['id']}"), ("POST", "/v1/shares"),
    ]:
        r = client.request(method, path)
        assert r.status_code == 403 and r.json()["detail"] == blocked, (method, path, r.text)
    # file-tools' question: live, then not.
    assert client.get("/v1/internal/render/verify", headers={"Authorization": f"Bearer {token}"}).status_code == 204
    rp.release(jti)
    assert client.get("/v1/internal/render/verify", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert client.get(f"/v1/apps/{row['id']}").status_code == 403


def test_a_render_cookie_beside_a_bearer_or_session_is_nobody():
    """The confinement judges the cookie only when no bearer and no session
    ride with it; the resolver takes it under the same condition, so a junk
    header can never turn it into an unconfined principal."""
    row = _row()
    _as_render(row)
    for headers in ({"Authorization": "Bearer junk"}, {"Authorization": "Basic eDp5"}):
        r = client.get("/v1/agents", headers=headers)
        assert r.status_code == 401, (headers, r.text)
    client.cookies.set("session", "junk")
    assert client.get("/v1/agents").status_code == 401
    client.cookies.delete("session")
    assert client.get(f"/v1/apps/{row['id']}").status_code == 200


def test_a_render_cookie_through_a_proxy_is_nobody(monkeypatch):
    row = _row()
    _as_render(row)
    # The confinement lets the path through; the resolver refuses the peer.
    r = client.get("/auth/me", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 401
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ["testclient"])
    monkeypatch.setattr("auth.lan_check._ip_in_trusted", lambda ip: True)
    assert client.get("/auth/me").status_code == 401


def test_the_render_mints_a_short_claim_for_the_check_instance_with_its_own_pace():
    row = _row()
    token, jti = _as_render(row)
    r = client.post(f"/v1/apps/{row['id']}/viewer-token")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ttl"] == 120
    claims = app_tokens.verify(body["token"], row["id"], app_tokens.PURPOSE_VIEWER)
    assert claims and claims["instance"] == "check" and claims["render"] == jti
    assert claims["sub"] == rp.synthetic_sub(row["id"]) and claims["role"] == "viewer"
    # No two-second pace for a render: the next load mints again at once.
    assert client.post(f"/v1/apps/{row['id']}/viewer-token").status_code == 200


def test_a_check_claim_reaches_the_check_instance_only_while_it_runs(tmp_path):
    row = _row()
    claim = app_tokens.mint(row["id"], app_tokens.PURPOSE_VIEWER, {
        "principal": "viewer", "sub": "render:x", "username": "", "role": "viewer",
        "grant": "", "agent": AGENT, "visibility": "shared", "external": False,
        "instance": "check", "render": "j1",
    }, 60)
    headers = {"Authorization": f"Bearer {claim}"}
    r = client.get(f"/v1/apps/{row['id']}/api/hello", headers=headers)
    assert r.status_code == 503 and r.headers.get("x-otodock-server") == "stopped", r.text

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"from the check instance")

        def log_message(self, *a):  # noqa: D102
            return
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        inst = app_supervisor.Instance(row_id=row["id"], name="check", row=row,
                                       release_dir=tmp_path, data_dir=tmp_path,
                                       host_port=srv.server_port, state="up")
        app_supervisor._instances[(row["id"], "check")] = inst
        r = client.get(f"/v1/apps/{row['id']}/api/hello", headers=headers)
        assert r.status_code == 200 and r.text == "from the check instance"
        # A live claim never reaches the check instance: it names the live process.
        live = app_tokens.mint(row["id"], app_tokens.PURPOSE_VIEWER, {
            "principal": "viewer", "sub": "u1", "username": "u1", "role": "viewer",
            "grant": "", "agent": AGENT, "visibility": "shared", "external": False,
        }, 60)
        r = client.get(f"/v1/apps/{row['id']}/api/hello", headers={"Authorization": f"Bearer {live}"})
        assert r.status_code == 503 and r.headers.get("x-otodock-server") != "up"
    finally:
        srv.shutdown()
