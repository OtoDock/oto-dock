"""Accounts kept by the host (APPS.md "External links", SHARING.md).

Load-bearing: the ``external`` block is validated, signed and carried; the
link's page keeps the app's own session token in a cookie of the link's
through the proxy — HttpOnly, bound to the share, scoped to the link's
path, alive for the manifest's days — and every claim minted while it
holds one carries ``session`` for the app to read; a clear drops it; the
mint is paced per link and client IP; a link's viewers are actors by
their session, else by their IP, never one actor per link; a link's claim
is refused on the dashboard prefix; a challenged path takes a passed
token once and never forwards it; the runtime's ``otodock.session`` and
``otodock.challenge`` resolve on the host's answers.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

import config
from api.apps import app_proxy
from api.apps import apps as apps_api
from api.apps import manifest as _mf
from app import app
from auth.password import hash_password
from auth.providers import UserContext, get_current_user
from services.apps import app_deploy, app_supervisor, app_tokens, releases
from services.infra import turnstile
from storage import database as task_store

client = TestClient(app)

AGENT = "link-sess-agent"
OWNER = "link-sess-owner"
ACCOUNT_PW = "owner-pass-abc"
ORIGIN = {"Origin": "http://testserver"}
CANON = task_store.canonical_actions_json
_needs_node = pytest.mark.skipif(not shutil.which("node"), reason="requires node")


def _user(sub: str = OWNER, is_api_key: bool = False) -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member", agents=[AGENT],
                       agent_roles={AGENT: "manager"}, is_api_key=is_api_key)


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _people(tmp_path, monkeypatch):
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
    app_proxy._buckets.clear()
    app_proxy._ws_counts.clear()


class _Echo(BaseHTTPRequestHandler):
    def _reply(self):
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        payload = json.dumps({"path": self.path, "method": self.command,
                              "headers": {k.lower(): v for k, v in self.headers.items()}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _reply

    def log_message(self, *a):
        pass


@pytest.fixture
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def _folder_app(external: dict | None = None, approve: bool = True) -> tuple[dict, str]:
    blocks = {"external": CANON(external) if external else ""}
    row = task_store.upsert_app(AGENT, "owner", OWNER, "shop", title="Shop",
                                rel_path="users/owner/workspace/apps/shop", kind="folder", blocks=blocks)
    src = config.AGENTS_DIR / AGENT / row["rel_path"]
    for rel, text in {"app.json": "{}", "client/index.html": "<p>link shop</p>",
                      "server/index.ts": "export {}"}.items():
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    rel, sha, _n = releases.cut_folder_release(row, src)
    row = task_store.set_app_release(row["id"], rel, sha)
    if approve and task_store.canonical_manifest(row) != "[]":
        task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), OWNER)
        row = task_store.get_app(row["id"])
    return row, sha


def _install(row: dict, port: int) -> app_supervisor.Instance:
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=config.AGENTS_DIR, data_dir=config.AGENTS_DIR,
                                   host_port=port, state="up", entry="server/index.ts")
    inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH, {"sub": "x"}, 3600)
    app_supervisor._instances[(row["id"], "live")] = inst
    return inst


def _link(app_id: str, public: bool = False) -> tuple[dict, str]:
    body = {"target_kind": "app", "target_id": app_id, "scope": "external", "password": ACCOUNT_PW}
    if public:
        body["public"] = True
    r = client.post("/v1/shares", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    return out, out["link"].rsplit("/s/", 1)[1]


def _unlock(token: str, password: str):
    return client.post(f"/s/{token}/unlock", json={"password": password}, headers=ORIGIN)


def _mint(token: str):
    apps_api._fire_rate.clear()
    r = client.post(f"/s/{token}/viewer-token")
    assert r.status_code == 200, r.text
    return r.json()


# ── the block ───────────────────────────────────────────────────────────────


def test_the_external_block_is_validated_signed_and_carried():
    v = lambda doc: app_deploy.validate_app_json(doc, AGENT, False)  # noqa: E731
    m = v({"title": "Shop", "external": {"links": ["Checkout.Stripe.com", "checkout.stripe.com"],
                                          "challenge": ["/register", "/register/", "/login"],
                                          "session_days": 7}})
    assert json.loads(m.external_json) == {"links": ["checkout.stripe.com"],
                                           "challenge": ["/register", "/login"], "session_days": 7}
    assert v({"external": {}}).external_json == "" and v({}).external_json == ""
    for bad in (
        {"external": ["x"]},
        {"external": {"extra": 1}},
        {"external": {"links": ["*.stripe.com"]}},
        {"external": {"links": ["10.0.0.1"]}},
        {"external": {"links": ["localhost"]}},
        {"external": {"links": [f"h{i}.example.com" for i in range(17)]}},
        {"external": {"challenge": ["register"]}},
        {"external": {"challenge": ["/a/../b"]}},
        {"external": {"challenge": ["/x?y"]}},
        {"external": {"challenge": [f"/p{i}" for i in range(17)]}},
        {"external": {"session_days": 0}},
        {"external": {"session_days": 366}},
        {"external": {"session_days": True}},
        {"external": {"session_days": "7"}},
    ):
        with pytest.raises(app_deploy.DeployError):
            v(bad)
    row, _sha = _folder_app({"links": ["checkout.stripe.com"], "challenge": ["/register"], "session_days": 7})
    assert _mf.parse_external(row) == {"links": ["checkout.stripe.com"], "challenge": ["/register"],
                                       "session_days": 7}
    assert _mf.parse_external({"external": ""}) == {"links": [], "challenge": [], "session_days": 30}
    assert _mf.challenge_path(row, "/register") and _mf.challenge_path(row, "/register/?x=1")
    assert _mf.challenge_path(row, "/register/step-two") and not _mf.challenge_path(row, "/registered")
    assert not _mf.challenge_path(row, "/login")
    shaped = apps_api.shape_app_rows([row], _user())[0]
    assert shaped["external"] == {"links": ["checkout.stripe.com"], "challenge": ["/register"],
                                  "session_days": 7}
    before = task_store.manifest_sig(row)
    row = task_store.upsert_app(AGENT, "owner", OWNER, "shop", blocks={"external": ""})
    assert task_store.manifest_sig(row) != before
    assert apps_api.shape_app_rows([row], _user())[0]["external"] == {"links": [], "challenge": [],
                                                                       "session_days": 30}


# ── the session ─────────────────────────────────────────────────────────────


def test_the_host_keeps_the_session_and_every_claim_carries_it(upstream, monkeypatch):
    row, sha = _folder_app({"session_days": 7, "links": ["checkout.stripe.com"], "challenge": ["/register"]})
    _install(row, upstream.server_port)
    out, token = _link(row["id"])
    share_id = out["share"]["id"]
    # The page config says what the link may do once approved.
    page = client.get(f"/s/{token}").text
    assert '"external_links": ["checkout.stripe.com"]' in page and '"session_days": 7' in page
    # Before the unlock nothing: the session route is behind the gate.
    assert client.post(f"/s/{token}/session", json={"token": "abc"}, headers=ORIGIN).status_code == 401
    assert _unlock(token, out["password"]).status_code == 200
    # No cookie: the claim has no session and the answer says so.
    vt = _mint(token)
    assert vt["session"] is None
    assert "session" not in app_tokens.verify(vt["token"], row["id"], app_tokens.PURPOSE_VIEWER)
    # A set: JSON and same-origin only, a printable token of at most 512.
    assert client.post(f"/s/{token}/session", json={"token": "abc"}).status_code == 403
    assert client.post(f"/s/{token}/session", content="token=abc", headers=ORIGIN).status_code in (403, 422)
    assert client.post(f"/s/{token}/session", json={"token": ""}, headers=ORIGIN).status_code == 400
    assert client.post(f"/s/{token}/session", json={"token": "a\nb"}, headers=ORIGIN).status_code == 400
    assert client.post(f"/s/{token}/session", json={"token": "x" * 513}, headers=ORIGIN).status_code == 400
    r = client.post(f"/s/{token}/session", json={"token": "sess-alpha"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    cookie = r.headers["set-cookie"]
    assert f"share_session_{share_id}=" in cookie and f"Path=/s/{token}" in cookie
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert f"Max-Age={7 * 86400}" in cookie
    assert "sess-alpha" not in cookie      # an envelope, never the token in clear
    assert r.json()["session"] == "sess-alpha"
    claims = app_tokens.verify(r.json()["token"], row["id"], app_tokens.PURPOSE_VIEWER)
    assert claims["session"] == "sess-alpha" and claims["external"] is True
    # Every later mint carries it; the app reads it from the verified header.
    vt = _mint(token)
    assert vt["session"] == "sess-alpha"
    bearer = {"Authorization": f"Bearer {vt['token']}"}
    r = client.get(f"/s/{token}/api/me", headers=bearer)
    assert r.status_code == 200, r.text
    seen = r.json()["headers"]
    assert app_tokens.verify(seen["x-otodock-viewer"], row["id"], app_tokens.PURPOSE_VIEWER)["session"] == "sess-alpha"
    assert seen["x-otodock-basis"] == "external" and "cookie" not in seen
    # A link's claim is for the link's own routes: the dashboard prefix
    # refuses it (a revoke must bite everywhere).
    assert client.get(f"/v1/apps/{row['id']}/api/me", headers=bearer).status_code == 401
    from starlette.websockets import WebSocketDisconnect
    with client.websocket_connect(f"/v1/apps/{row['id']}/ws/live") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": vt["token"]}))
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    assert e.value.code == 4401
    # The actor is the session: its own bucket, apart from an anonymous
    # viewer's IP bucket and from another session's.
    monkeypatch.setattr(app_proxy, "RATE_PER_ACTOR", 2.0)
    app_proxy._buckets.clear()
    assert client.get(f"/s/{token}/api/a", headers=bearer).status_code == 200
    assert client.get(f"/s/{token}/api/b", headers=bearer).status_code == 200
    r = client.get(f"/s/{token}/api/c", headers=bearer)
    assert r.status_code == 429 and r.headers.get("retry-after") == "1"
    anonymous = {"Authorization": f"Bearer {_external(share_id, row, '')}"}
    assert client.get(f"/s/{token}/api/a", headers=anonymous).status_code == 200
    other = {"Authorization": f"Bearer {_external(share_id, row, 'sess-beta')}"}
    assert client.get(f"/s/{token}/api/a", headers=other).status_code == 200
    monkeypatch.setattr(app_proxy, "RATE_PER_ACTOR", 20.0)
    app_proxy._buckets.clear()   # the small buckets above refill at the old burst
    # The mint is paced per link and client IP: a second within two
    # seconds waits (the host retries), the next after a clear is fresh.
    apps_api._fire_rate.clear()
    assert client.post(f"/s/{token}/viewer-token").status_code == 200
    assert client.post(f"/s/{token}/viewer-token").status_code == 429
    # A clear: same-origin, the cookie goes, the claim loses the session.
    assert client.delete(f"/s/{token}/session").status_code == 403
    r = client.delete(f"/s/{token}/session", headers=ORIGIN)
    assert r.status_code == 200 and r.json()["session"] is None
    assert "Max-Age=0" in r.headers["set-cookie"] or "max-age=0" in r.headers["set-cookie"].lower() \
        or "expires=" in r.headers["set-cookie"].lower()
    assert _mint(token)["session"] is None
    # A challenged path: while Turnstile is configured a non-GET call from
    # the link carries a passed token, verified once and never forwarded;
    # GET and other paths need none; not configured, nothing is asked.
    monkeypatch.setattr(turnstile, "load_config",
                        lambda settings=None: turnstile.TurnstileConfig("site", "secret", False))

    async def fake_verify(data):
        return {"success": data.get("response") == "good-token"}

    monkeypatch.setattr(turnstile, "_post_siteverify", fake_verify)
    vt = _mint(token)
    bearer = {"Authorization": f"Bearer {vt['token']}"}
    r = client.post(f"/s/{token}/api/register", headers=bearer, json={})
    assert r.status_code == 403 and "challenge required" in r.text
    r = client.post(f"/s/{token}/api/register", headers={**bearer, "X-OtoDock-Challenge": "bad"}, json={})
    assert r.status_code == 403 and "challenge failed" in r.text
    r = client.post(f"/s/{token}/api/register", headers={**bearer, "X-OtoDock-Challenge": "good-token"}, json={})
    assert r.status_code == 200, r.text
    assert "x-otodock-challenge" not in r.json()["headers"]
    assert client.get(f"/s/{token}/api/register", headers=bearer).status_code == 200
    assert client.post(f"/s/{token}/api/other", headers=bearer, json={}).status_code == 200
    monkeypatch.setattr(turnstile, "load_config",
                        lambda settings=None: turnstile.TurnstileConfig("", "", False))
    assert client.post(f"/s/{token}/api/register", headers=bearer, json={}).status_code == 200
    assert "x-otodock-challenge" in app_proxy._CORS["Access-Control-Allow-Headers"]


def _external(share_id: str, row: dict, session: str) -> str:
    claims = {"principal": "external", "sub": "", "username": "", "role": "viewer",
              "grant": share_id, "agent": AGENT, "visibility": "personal", "external": True}
    if session:
        claims["session"] = session
    return app_tokens.mint(row["id"], app_tokens.PURPOSE_VIEWER, claims, 600)


def test_socket_slots_follow_the_actor():
    app_proxy._ws_counts.clear()
    assert all(app_proxy._ws_take("app", "share:s:s:abcd") for _ in range(2))
    assert not app_proxy._ws_take("app", "share:s:s:abcd")
    assert all(app_proxy._ws_take("app", "share:s:ip:203.0.113.9") for _ in range(8))
    assert not app_proxy._ws_take("app", "share:s:ip:203.0.113.9")
    assert app_proxy._ws_take("app", "share:s:s:other")
    app_proxy._ws_counts.clear()


@_needs_node
def test_the_runtime_asks_the_host_for_the_session_and_the_challenge():
    from api.apps.apps import APP_RUNTIME
    body = APP_RUNTIME.replace("<script>", "", 1)
    body = body[: body.rindex("</script>")]
    harness = """
      var posted = [], listeners = [];
      globalThis.window = globalThis;
      globalThis.otodock = {};
      globalThis.location = {protocol: 'https:', host: 'h', pathname: '/s/tok/client/abc/'};
      globalThis.parent = {postMessage: function(m){ posted.push(m); }};
      globalThis.addEventListener = function(t, cb){ listeners.push([t, cb]); };
      globalThis.dispatchEvent = function(){};
      globalThis.CustomEvent = function(){};
      globalThis.document = {documentElement: {scrollHeight: 1}, body: {scrollHeight: 1}};
      globalThis.setInterval = function(){};
      globalThis.WebSocket = function(){ this.readyState = 0; this.send = function(){}; };
      globalThis.Headers = function(){ this.set = function(){}; };
      globalThis.fetch = function(){ return Promise.resolve({status: 200}); };
    """
    tail = """
      function deliver(data){
        for (var i = 0; i < listeners.length; i++) if (listeners[i][0] === 'message') listeners[i][1]({data: data});
      }
      var out = {before: window.otodock.session.token};
      var p = window.otodock.session.set('sess-abc');
      out.asked = posted.filter(function(m){ return m.type === 'session_set'; }).map(function(m){ return m.token; });
      deliver({source: 'otodock-host', type: 'app_session', token: 'sess-abc', status: 'ok'});
      p.then(function(r){
        out.set = r; out.after = window.otodock.session.token;
        var c = window.otodock.challenge();
        var ask = posted.filter(function(m){ return m.type === 'challenge'; })[0];
        deliver({source: 'otodock-host', type: 'challenge_result', call_id: ask.call_id, token: 'ts-token', status: 'ok'});
        return c;
      }).then(function(t){
        out.challenge = t;
        var q = window.otodock.session.clear();
        deliver({source: 'otodock-host', type: 'app_session', token: null, status: 'cleared'});
        return q;
      }).then(function(r){
        out.cleared = r; out.final = window.otodock.session.token;
        // The host-mediated open: a call id the ack echoes; the link's
        // hosts and query ride shared_link.
        var o = window.otodock.openExternal('https://checkout.stripe.com/pay/x');
        var oask = posted.filter(function(m){ return m.type === 'open_url'; })[0];
        out.open_asked = {url: oask.url, has_id: !!oask.call_id};
        deliver({source: 'otodock-host', type: 'open_url_ack', call_id: oask.call_id, status: 'opened'});
        deliver({source: 'otodock-host', type: 'shared_link', actions: false, links: ['checkout.stripe.com', 7],
                 url: 'https://dock.example/s/tok', query: 'paid=1'});
        out.shared = window.otodock.sharedLink;
        return o;
      }).then(function(r){
        out.opened = r;
        console.log(JSON.stringify(out));
        process.exit(0);   // the runtime's own safety timers would keep node alive
      });
    """
    out = subprocess.run(["node", "-e", harness + body + tail], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-800:]
    seen = json.loads(out.stdout.strip().splitlines()[-1])
    assert seen == {"before": None, "asked": ["sess-abc"], "set": {"token": "sess-abc", "status": "ok"},
                    "after": "sess-abc", "challenge": "ts-token",
                    "cleared": {"token": None, "status": "cleared"}, "final": None,
                    "open_asked": {"url": "https://checkout.stripe.com/pay/x", "has_id": True},
                    "shared": {"actions": False, "links": ["checkout.stripe.com"],
                               "url": "https://dock.example/s/tok", "query": "paid=1"},
                    "opened": {"status": "opened", "reason": ""}}
