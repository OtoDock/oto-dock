"""App secrets (APPS.md "Secrets").

Load-bearing: the ``secrets`` block is validated, signed and carried (names,
whether required, where the platform sends each — never a value); a value
is set by a person with the approval authority and never by a bearer
principal or the render principal; no route returns a value and the
listing never decrypts; a required secret without a value parks a release
and the approve route refuses with the words; a live or preview instance
receives the ``env`` values and a check instance never does; every value
the child was handed is scrubbed from the log, the tail and the smoke; the
egress route adds the declared headers to a call to a declared host and
nothing else; the smoke of an unapproved manifest runs with no hosts.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

import config
from api.apps import app_egress, app_proxy
from api.apps import apps as apps_api
from app import app
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from core.session import session_state
from services.apps import app_deploy, app_sandbox, app_secrets, app_supervisor, app_tokens, releases
from storage import database as task_store
from storage import db_app_secrets
from storage.identity.credential_store import decrypt_secret

client = TestClient(app)

AGENT = "secrets-agent"
SID = "sess-secrets-alice"
CANON = task_store.canonical_actions_json

EGRESS = ["api.example.test"]
SECRETS = [
    {"name": "STRIPE_SECRET_KEY", "required": True, "description": "a restricted key",
     "sends_to": {"host": "api.example.test", "header": "Authorization", "prefix": "Bearer "}},
    {"name": "STRIPE_WEBHOOK_SECRET", "required": True},
    {"name": "SMTP_PASSWORD", "required": False, "env": True},
]
KEY_VALUE = "sk-test-alpha-beta-gamma-delta"
PW_VALUE = "mail-pass-epsilon-zeta"


def _user(sub: str = "alice-sub", agent_roles: dict[str, str] | None = None,
          is_api_key: bool = False, render_app: str = "") -> UserContext:
    roles = {AGENT: "manager"} if agent_roles is None else agent_roles
    u = UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                    agents=list(roles.keys()), is_api_key=is_api_key, agent_roles=roles)
    if render_app:
        u.render_app = render_app
    return u


ALICE = _user()
BOB = _user("bob-sub", {AGENT: "viewer"})
EDITOR = _user("erin-sub", {AGENT: "editor"})


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _clean():
    _as(ALICE)
    apps_api._fire_rate.clear()
    app_proxy._buckets.clear()
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()
    app_proxy._buckets.clear()
    app_egress._client = None


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    (agents_root / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    from storage.pg import get_conn
    for sub, name in (("alice-sub", "alice"), ("bob-sub", "bob"), ("erin-sub", "erin")):
        task_store.upsert_user(sub, f"{sub}@test.com", name.title(), "member")
        with get_conn() as conn:
            conn.execute("UPDATE users SET username=%s WHERE sub=%s", (name, sub))
            conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    task_store.add_user_agent("bob-sub", AGENT, "viewer", "test")
    task_store.add_user_agent("erin-sub", AGENT, "editor", "test")
    session_state.set_session_security(SID, SecurityContext(
        role="manager", username="alice", agent=AGENT, is_admin_agent=False))
    yield agents_root / AGENT
    session_state._session_security.pop(SID, None)


def _hook(op: str, payload: dict, sid: str = SID):
    payload.setdefault("session_id", sid)
    with patch("api.hooks.app_deploy.verify_session_match_async"), \
            patch("api.hooks.pins.verify_session_match_async"):
        return client.post(f"/v1/hooks/apps/{op}", json=payload,
                           headers={"Authorization": "Bearer dummy"})


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _folder(agent_tree: Path, slug: str = "shop", *, manifest: dict, server: str | None = None) -> Path:
    root = agent_tree / "users/alice/workspace" / "apps" / slug
    files = {"app.json": json.dumps(manifest), "client/index.html": "<p>shop</p>"}
    if server is not None:
        files["server/index.ts"] = server
    return _write(root, files)


def _row(slug: str = "shop", *, secrets: list[dict] | None = None, egress: list[str] | None = None,
         username: str = "alice", owner: str | None = "alice-sub") -> dict:
    root = f"users/{username}/workspace" if username else "workspace"
    blocks = {"secrets": CANON(secrets) if secrets else "", "egress": CANON(egress) if egress else ""}
    return task_store.upsert_app(AGENT, username, owner, slug, title=slug.title(),
                                 rel_path=f"{root}/apps/{slug}", kind="folder", blocks=blocks)


def _approve(row: dict) -> dict:
    task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), "alice-sub")
    return task_store.get_app(row["id"])


# ── the block ───────────────────────────────────────────────────────────────


def test_the_secrets_block_is_validated_signed_and_carried(agent_tree):
    v = lambda doc: app_deploy.validate_app_json(doc, AGENT, False)  # noqa: E731
    m = v({"title": "Shop", "egress": EGRESS, "secrets": SECRETS})
    got = json.loads(m.secrets_json)
    assert [s["name"] for s in got] == ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "SMTP_PASSWORD"]
    assert got[0]["sends_to"] == {"host": "api.example.test", "header": "Authorization",
                                  "prefix": "Bearer "}
    assert got[1] == {"name": "STRIPE_WEBHOOK_SECRET", "required": True}
    assert got[2]["env"] is True and got[2]["required"] is False
    assert m.blocks()["secrets"] == m.secrets_json
    assert v({"secrets": []}).secrets_json == ""
    for bad in (
        {"secrets": {"name": "X"}},
        {"secrets": [{"name": "stripe_key"}]},                       # lower case
        {"secrets": [{"name": "X"}]},                                # too short
        {"secrets": [{"name": "PATH"}]},                             # the runtime's
        {"secrets": [{"name": "OTODOCK_APP_TOKEN"}]},
        {"secrets": [{"name": "BUN_INSTALL"}]},
        {"secrets": [{"name": "LD_PRELOAD", "env": True}]},          # the host-side launcher's
        {"secrets": [{"name": "PYTHONPATH", "env": True}]},
        {"secrets": [{"name": "BASH_ENV", "env": True}]},
        {"secrets": [{"name": "A_KEY"}, {"name": "A_KEY"}]},          # twice
        {"secrets": [{"name": "A_KEY", "required": "yes"}]},
        {"secrets": [{"name": "A_KEY", "description": "x" * 201}]},
        {"secrets": [{"name": "A_KEY", "value": "never"}]},           # a value never
        {"secrets": [{"name": "A_KEY", "sends_to": {"host": "api.example.test", "header": "X-Key"}}]},
        {"egress": EGRESS, "secrets": [{"name": "A_KEY", "sends_to": {"host": "other.example.test",
                                                                       "header": "X-Key"}}]},
        {"egress": EGRESS, "secrets": [{"name": "A_KEY", "sends_to": {"host": "api.example.test",
                                                                       "header": "bad header"}}]},
        {"egress": EGRESS, "secrets": [{"name": "A_KEY", "sends_to": {"host": "api.example.test",
                                                                       "header": "Host"}}]},
        {"egress": EGRESS, "secrets": [{"name": "A_KEY", "sends_to": {"host": "api.example.test",
                                                                       "header": "X-Key",
                                                                       "prefix": "p" * 33}}]},
        {"egress": EGRESS, "secrets": [{"name": "A_KEY", "env": True,
                                        "sends_to": {"host": "api.example.test", "header": "X-Key"}}]},
        {"secrets": [{"name": f"KEY_{i}"} for i in range(17)]},
    ):
        with pytest.raises(app_deploy.DeployError):
            v(bad)
    # Deployed: the block lands on the row, is signed (a new name voids the
    # approval), and parses back; the values table is empty.
    _folder(agent_tree, manifest={"title": "Shop", "egress": EGRESS, "secrets": SECRETS[1:2]})
    body = _hook("deploy", {"slug": "shop"}).json()
    assert body["status"] == "pending approval", body
    row = task_store.get_app(body["app_id"])
    assert [s["name"] for s in json.loads(row["secrets"])] == ["STRIPE_WEBHOOK_SECRET"]
    from api.apps import manifest as _mf
    assert _mf.parse_secrets(row)[0]["required"] is True
    sig_one = task_store.manifest_sig(row)
    row = task_store.upsert_app(AGENT, "alice", "alice-sub", "shop",
                                blocks={"secrets": CANON(SECRETS), "egress": CANON(EGRESS)})
    assert task_store.manifest_sig(row) != sig_one
    assert db_app_secrets.list_set(row["id"]) == {}
    # The check hook names the block among the parsed ones.
    assert _hook("check", {"slug": "shop"}).json()["manifest"]["secrets"] is True


# ── the routes ──────────────────────────────────────────────────────────────


def test_values_are_set_by_people_with_authority_and_never_returned(agent_tree):
    _folder(agent_tree, manifest={"title": "Shop", "egress": EGRESS, "secrets": SECRETS})
    body = _hook("deploy", {"slug": "shop"}).json()
    assert body["status"] == "pending approval"
    assert body["waiting"] == "STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET are not set"
    row = task_store.get_app(body["app_id"])
    url = f"/v1/apps/{row['id']}/secrets"
    # The listing: names, required, use, set — for the owner; a viewer sees
    # no personal row; an editor of the agent is not the owner.
    r = client.get(url)
    assert r.status_code == 200, r.text
    listed = {s["name"]: s for s in r.json()["secrets"]}
    assert listed["STRIPE_SECRET_KEY"]["set"] is False and listed["STRIPE_SECRET_KEY"]["required"]
    assert listed["STRIPE_SECRET_KEY"]["sends_to"]["host"] == "api.example.test"
    assert listed["SMTP_PASSWORD"]["env"] is True
    assert r.json()["waiting"] == "STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET are not set"
    _as(BOB)
    assert client.get(url).status_code == 404
    _as(EDITOR)
    assert client.get(url).status_code == 404       # a personal app: the owner alone
    # A bearer principal (the agent's own session) never sets a value; the
    # render principal neither.
    _as(_user(is_api_key=True))
    assert client.put(f"{url}/STRIPE_SECRET_KEY", json={"value": KEY_VALUE}).status_code == 403
    _as(_user("render-sub", render_app=row["id"], agent_roles={}))
    assert client.put(f"{url}/STRIPE_SECRET_KEY", json={"value": KEY_VALUE}).status_code in (403, 404)
    _as(ALICE)
    # Only a declared name, only a real value.
    assert client.put(f"{url}/NOT_DECLARED", json={"value": KEY_VALUE}).status_code == 400
    assert client.put(f"{url}/lower", json={"value": KEY_VALUE}).status_code == 400
    assert client.put(f"{url}/STRIPE_SECRET_KEY", json={"value": "  "}).status_code == 400
    assert client.put(f"{url}/STRIPE_SECRET_KEY", json={"value": "x" * 8193}).status_code == 400
    assert client.put(f"{url}/STRIPE_SECRET_KEY", json={"value": "a\x00b"}).status_code == 400
    # The approve route refuses while a required value is missing.
    r = client.post(f"/v1/apps/{row['id']}/deploy/approve")
    assert r.status_code == 409 and "STRIPE_SECRET_KEY" in r.json()["detail"]
    # Set: the answer carries no value; the store holds ciphertext; the
    # listing says set and by whom.
    r = client.put(f"{url}/STRIPE_SECRET_KEY", json={"value": KEY_VALUE})
    assert r.status_code == 200 and KEY_VALUE not in r.text
    r = client.put(f"{url}/STRIPE_WEBHOOK_SECRET", json={"value": "whsec-eta-theta-iota"})
    assert r.status_code == 200
    from storage.pg import get_conn
    with get_conn() as conn:
        enc = conn.execute("SELECT value_enc FROM app_secrets WHERE app_id=%s AND name=%s",
                           (row["id"], "STRIPE_SECRET_KEY")).fetchone()["value_enc"]
    assert KEY_VALUE not in enc and decrypt_secret(enc) == KEY_VALUE
    listed = {s["name"]: s for s in client.get(url).json()["secrets"]}
    assert listed["STRIPE_SECRET_KEY"]["set"] is True
    assert listed["STRIPE_SECRET_KEY"]["set_by"] == "alice-sub"
    assert "value" not in listed["STRIPE_SECRET_KEY"]
    assert client.get(url).json()["waiting"] == ""
    st = client.get(f"/v1/apps/{row['id']}/deploy/status").json()
    assert st["waiting"] == "" and {s["name"]: s["set"] for s in st["secrets"]} == {
        "STRIPE_SECRET_KEY": True, "STRIPE_WEBHOOK_SECRET": True, "SMTP_PASSWORD": False}
    assert not any("value" in s for s in st["secrets"])
    # The card's row: names and set flags, never a value; the status hook
    # and the list hook say the same to the agent.
    shaped = apps_api.shape_app_rows([task_store.get_app(row["id"])], ALICE)[0]
    assert {s["name"]: s["set"] for s in shaped["secrets"]}["SMTP_PASSWORD"] is False
    assert KEY_VALUE not in json.dumps(shaped)
    hooked = _hook("status", {"slug": "shop"}).json()
    assert hooked["waiting"] == "" and KEY_VALUE not in json.dumps(hooked)
    listed_apps = _hook("list", {"slug": ""}).json()["apps"]
    assert all("waiting" not in a for a in listed_apps if a["slug"] == "shop")
    # Now the approve goes through (no server: a static release).
    r = client.post(f"/v1/apps/{row['id']}/deploy/approve")
    assert r.status_code == 200, r.text
    row = task_store.get_app(row["id"])
    assert task_store.app_actions_approved(row) and releases.current_number(row) == 1
    # Remove a required value: the listing says so, the list hook names it,
    # a new release parks for it.
    r = client.delete(f"{url}/STRIPE_WEBHOOK_SECRET")
    assert r.status_code == 200 and r.json()["removed"] is True
    assert client.get(url).json()["waiting"] == "STRIPE_WEBHOOK_SECRET is not set"
    listed_apps = _hook("list", {"slug": ""}).json()["apps"]
    assert [a["waiting"] for a in listed_apps if a["slug"] == "shop"] == ["STRIPE_WEBHOOK_SECRET is not set"]
    (agent_tree / row["rel_path"] / "client" / "index.html").write_text("<p>v2</p>")
    body = _hook("deploy", {"slug": "shop"}).json()
    assert body["status"] == "pending approval" and body["manifest_changed"] is False
    assert body["waiting"] == "STRIPE_WEBHOOK_SECRET is not set"
    assert client.post(f"/v1/apps/{row['id']}/deploy/approve").status_code == 409
    # A stored name the manifest no longer declares is listed as such and
    # can be removed; a purge takes the values with the row.
    db_app_secrets.set_value(row["id"], "OLD_KEY", "old-value-kappa", "alice-sub")
    listed = {s["name"]: s for s in client.get(url).json()["secrets"]}
    assert listed["OLD_KEY"]["declared"] is False and listed["OLD_KEY"]["set"] is True
    assert client.delete(f"{url}/OLD_KEY").json()["removed"] is True
    from storage.pg import get_conn as _gc
    with _gc() as conn:
        conn.execute("DELETE FROM pinned_apps WHERE id=%s", (row["id"],))
        conn.commit()
    assert db_app_secrets.list_set(row["id"]) == {}


def test_a_shared_app_takes_an_editor_or_a_manager(agent_tree):
    row = _row("team", secrets=SECRETS[1:2], username="", owner=None)
    url = f"/v1/apps/{row['id']}/secrets"
    _as(BOB)
    assert client.get(url).status_code == 403
    assert client.put(f"{url}/STRIPE_WEBHOOK_SECRET", json={"value": "whsec-lambda-mu"}).status_code == 403
    _as(EDITOR)
    assert client.put(f"{url}/STRIPE_WEBHOOK_SECRET", json={"value": "whsec-lambda-mu"}).status_code == 200
    _as(ALICE)
    assert client.delete(f"{url}/STRIPE_WEBHOOK_SECRET").json()["removed"] is True


# ── the launch ──────────────────────────────────────────────────────────────


def test_env_values_reach_live_only(agent_tree, tmp_path, monkeypatch):
    """The ``env`` values reach the live instance only; a
    preview (unapproved code an editor or a session can start at once) and a
    check start with none, whatever is passed."""
    row = _row(secrets=SECRETS, egress=EGRESS)
    env = app_sandbox.build_env(row, "tok", "pub", secrets={"SMTP_PASSWORD": PW_VALUE, "PATH": "x"})
    assert env["SMTP_PASSWORD"] == PW_VALUE and env["PATH"] != "x"
    assert "SMTP_PASSWORD" not in app_sandbox.build_env(row, "tok", "pub")
    db_app_secrets.set_value(row["id"], "SMTP_PASSWORD", PW_VALUE, "alice-sub")
    db_app_secrets.set_value(row["id"], "STRIPE_SECRET_KEY", KEY_VALUE, "alice-sub")
    # Only the env secrets reach a launch; a sends_to value never does.
    assert app_secrets.env_values(row) == {"SMTP_PASSWORD": PW_VALUE}
    # The launcher: the env it builds per instance kind.
    seen: dict[str, dict] = {}

    def fake_build_env(r, token, pub, *, port=3000, preview=False, secrets=None):
        seen[("preview" if preview else "live")] = dict(secrets or {})
        return {"PATH": "/bin"}

    async def fake_exec(*argv, **kw):
        raise app_sandbox.AppStartError("not really")

    monkeypatch.setattr(app_sandbox, "build_env", fake_build_env)
    monkeypatch.setattr(app_sandbox, "build_command", lambda *a, **k: ["true"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    for name in ("live", "preview", "check"):
        inst = app_supervisor.Instance(row_id=row["id"], name=name, row=row,
                                       release_dir=tmp_path, data_dir=tmp_path, entry="server/index.ts")
        with pytest.raises(app_sandbox.AppStartError):
            asyncio.run(app_supervisor._launch(inst, preview=name == "preview", allow_hosts=[],
                                               secrets={"SMTP_PASSWORD": PW_VALUE}))
        kind = "preview" if name == "preview" else "live"
        assert seen.pop(kind) == ({"SMTP_PASSWORD": PW_VALUE} if name == "live" else {}), name
    # start() hands the preview nothing either, and a required value that is
    # not set keeps the live server down but never the preview.
    launched: dict[str, dict] = {}

    async def fake_launch(inst, *, preview, allow_hosts=None, secrets=None):
        launched[inst.name] = dict(secrets or {})
        raise app_sandbox.AppStartError("not really")

    monkeypatch.setattr(app_supervisor, "_launch", fake_launch)
    row = _approve(row)
    db_app_secrets.set_value(row["id"], "STRIPE_WEBHOOK_SECRET", "whsec-test-value", "alice-sub")
    (tmp_path / "server").mkdir(exist_ok=True)
    (tmp_path / "server" / "index.ts").write_text("export {}")
    for name in ("live", "preview"):
        with pytest.raises(app_sandbox.AppStartError):
            asyncio.run(app_supervisor.start(row, name, release_dir=tmp_path, data_dir=tmp_path / name))
        app_supervisor._instances.clear()
    assert launched == {"live": {"SMTP_PASSWORD": PW_VALUE}, "preview": {}}
    db_app_secrets.delete_value(row["id"], "STRIPE_WEBHOOK_SECRET")
    with pytest.raises(app_supervisor.AppUnavailable) as e:
        asyncio.run(app_supervisor.start(row, "live", release_dir=tmp_path, data_dir=tmp_path / "l2"))
    assert e.value.state == "secrets"
    with pytest.raises(app_sandbox.AppStartError):
        asyncio.run(app_supervisor.start(row, "preview", release_dir=tmp_path, data_dir=tmp_path / "p2"))


def test_a_missing_required_value_keeps_the_server_down(agent_tree, tmp_path, monkeypatch):
    fake_bun = tmp_path / "bun"
    fake_bun.write_text("#!/bin/sh\n")
    fake_bun.chmod(0o755)
    monkeypatch.setattr(config, "BUN_BIN", str(fake_bun))
    row = _approve(_row(secrets=SECRETS[1:2], egress=EGRESS))
    src = _write(agent_tree / row["rel_path"], {"app.json": "{}", "client/index.html": "<p>x</p>",
                                                "server/index.ts": "export {}"})
    rel, sha, _n = releases.cut_folder_release(row, src)
    row = task_store.set_app_release(row["id"], rel, sha)
    with pytest.raises(app_supervisor.AppUnavailable) as e:
        asyncio.run(app_supervisor.start(row))
    assert e.value.state == "secrets" and "STRIPE_WEBHOOK_SECRET" in e.value.reason
    # A check instance needs no value: the refusal never fires for it (the
    # launch itself fails here because the fake Bun exits at once).
    with pytest.raises(app_sandbox.AppStartError):
        asyncio.run(app_supervisor.start(row, "check", release_dir=src, data_dir=tmp_path / "d",
                                         allow_hosts=[]))


def test_the_pump_scrubs_every_value_the_child_was_handed(agent_tree, tmp_path):
    row = _row(secrets=SECRETS, egress=EGRESS)
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=tmp_path, data_dir=tmp_path, entry="server/index.ts")
    inst.token = "launch-token-nu-xi-omicron-pi"
    inst.scrub = [inst.token, PW_VALUE]

    class _Proc:
        def __init__(self):
            self.stdout = asyncio.StreamReader()

    async def run():
        proc = _Proc()
        inst.proc = proc
        proc.stdout.feed_data(f"env has {PW_VALUE} and {inst.token}\nclean line\n".encode())
        proc.stdout.feed_eof()
        await app_supervisor._pump(inst)

    asyncio.run(run())
    assert inst._tail == ["env has [redacted] and [redacted]", "clean line"]
    logged = releases.log_path(row).read_text()
    assert PW_VALUE not in logged and inst.token not in logged and "[redacted]" in logged
    # The smoke of an unapproved manifest runs with no hosts; an approved
    # one with the row's.
    seen: list = []

    async def fake_start(r, name="live", *, release_dir=None, data_dir=None, allow_hosts=None):
        seen.append(allow_hosts)
        raise app_sandbox.AppStartError("no")

    with patch.object(app_supervisor, "start", fake_start):
        tree = _write(tmp_path / "tree", {"server/index.ts": "export {}"})
        asyncio.run(app_supervisor.smoke(row, tree))
        asyncio.run(app_supervisor.smoke(_approve(row), tree))
    assert seen == [[], None]


# ── the egress route ────────────────────────────────────────────────────────


def test_the_egress_route_adds_the_declared_headers_and_nothing_else(agent_tree, monkeypatch):
    row = _approve(_row(secrets=SECRETS, egress=EGRESS))
    db_app_secrets.set_value(row["id"], "STRIPE_SECRET_KEY", KEY_VALUE, "alice-sub")
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=agent_tree, data_dir=agent_tree, host_port=1,
                                   state="up", entry="server/index.ts")
    inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH, {"sub": f"app:{row['id']}"}, 3600)
    app_supervisor._instances[(row["id"], "live")] = inst
    calls: list[httpx.Request] = []

    def vendor(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "https://elsewhere.example/"},
                                  stream=httpx.ByteStream(b""))
        if request.url.path == "/cached":
            return httpx.Response(304, headers={"ETag": '"v1"'}, stream=httpx.ByteStream(b""))
        body = json.dumps({"path": request.url.raw_path.decode().split("?")[0],
                           "q": request.url.query.decode(),
                           "auth": request.headers.get("authorization", ""),
                           "custom": request.headers.get("x-app-custom", ""),
                           "leaked": request.headers.get("x-otodock-viewer", "")}).encode()
        # A streamed body (a response built from `json=` is read at once and
        # the route streams it a second time).
        return httpx.Response(200, headers={"Content-Type": "application/json", "Set-Cookie": "v=1",
                                            "X-Vendor": "yes",
                                            "Access-Control-Allow-Origin": "https://evil.example"},
                              stream=httpx.ByteStream(body))

    app_egress._client = httpx.AsyncClient(transport=httpx.MockTransport(vendor))
    # The mock vendor is not in DNS: the resolved-address check is its own test below.
    monkeypatch.setattr(app_egress, "validate_outbound_url", lambda url, **kw: None)
    base = f"/v1/apps/{row['id']}/egress/api.example.test"
    bearer = {"Authorization": f"Bearer {inst.token}"}
    # The launch token alone; a viewer's claim is refused; a foreign host is
    # unknown; a traversing path too.
    viewer = client.post(f"/v1/apps/{row['id']}/viewer-token").json()["token"]
    assert client.post(f"{base}/v1/charges", headers={"Authorization": f"Bearer {viewer}"}).status_code == 403
    assert client.post(f"{base}/v1/charges").status_code == 401
    assert client.post(f"/v1/apps/{row['id']}/egress/other.example.test/x", headers=bearer).status_code == 404
    assert client.post(f"{base}/v1/%2e%2e/x", headers=bearer).status_code == 404
    # The call: the declared header rides, the app's own X-OtoDock-* and
    # Authorization do not, the query survives, the vendor's cookie and
    # CORS headers are dropped.
    r = client.post(f"{base}/v1/charges?amount=5", headers={**bearer, "X-App-Custom": "yes",
                                                             "X-OtoDock-Viewer": "forged"},
                    content=b"amount=5")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["auth"] == f"Bearer {KEY_VALUE}" and got["custom"] == "yes" and got["leaked"] == ""
    assert got["path"] == "/v1/charges" and got["q"] == "amount=5"
    assert calls[-1].url.scheme == "https" and calls[-1].content == b"amount=5"
    assert "set-cookie" not in r.headers and r.headers.get("x-vendor") == "yes"
    assert r.headers.get("access-control-allow-origin") is None
    assert r.json()["auth"] not in r.headers.values()
    # A redirect is never followed; a burst is refused with a retry hint.
    assert client.get(f"{base}/redirect", headers=bearer).status_code == 502
    app_proxy._buckets[f"{row['id']}|egress"] = (0.0, __import__("time").monotonic())
    r = client.get(f"{base}/v1/charges", headers=bearer)
    assert r.status_code == 429 and r.headers.get("retry-after") == "1"
    # A host with no set value: the call goes out with no injected header.
    db_app_secrets.delete_value(row["id"], "STRIPE_SECRET_KEY")
    app_proxy._buckets.clear()
    r = client.get(f"{base}/v1/charges", headers=bearer)
    assert r.status_code == 200 and r.json()["auth"] == ""
    # A 304 is an answer, not a redirect; the path reaches the vendor as the
    # server wrote it, and an encoded separator is refused, not decoded.
    assert client.get(f"{base}/cached", headers=bearer).status_code == 304
    assert client.get(f"{base}/files/a%3Fb", headers=bearer).json()["path"] == "/files/a%3Fb"
    assert client.get(f"{base}/repos/group%2Fproj", headers=bearer).status_code == 404
    # A changed manifest waiting on the card (the deploy writes it onto the
    # row first) re-points the key at a new host: nothing goes out, to that
    # host or to the one the live release had, until a person approves.
    db_app_secrets.set_value(row["id"], "STRIPE_SECRET_KEY", KEY_VALUE, "alice-sub")
    moved = [dict(SECRETS[0], sends_to={"host": "evil.example.test", "header": "Authorization"}),
             *SECRETS[1:]]
    _row(secrets=moved, egress=[*EGRESS, "evil.example.test"])
    sent = len(calls)
    for target in ("evil.example.test", "api.example.test"):
        r = client.post(f"/v1/apps/{row['id']}/egress/{target}/steal", headers=bearer)
        assert r.status_code == 403 and "approval" in r.text, r.text
    assert len(calls) == sent


def test_the_preview_cannot_spend_the_apps_keys(agent_tree, monkeypatch):
    """The preview copy's launch token is refused at the
    egress route (no vendor call, no key added), while the live one's goes
    out."""
    row = _approve(_row(secrets=SECRETS, egress=EGRESS))
    db_app_secrets.set_value(row["id"], "STRIPE_SECRET_KEY", KEY_VALUE, "alice-sub")
    tokens = {}
    for name in ("live", "preview"):
        inst = app_supervisor.Instance(row_id=row["id"], name=name, row=row,
                                       release_dir=agent_tree, data_dir=agent_tree, host_port=1,
                                       state="up", entry="server/index.ts")
        inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH,
                                     {"sub": f"app:{row['id']}", "instance": name}, 3600)
        app_supervisor._instances[(row["id"], name)] = inst
        tokens[name] = inst.token
    calls: list[httpx.Request] = []

    def vendor(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

    app_egress._client = httpx.AsyncClient(transport=httpx.MockTransport(vendor))
    monkeypatch.setattr(app_egress, "validate_outbound_url", lambda url, **kw: None)
    url = f"/v1/apps/{row['id']}/egress/api.example.test/v1/refunds"
    r = client.post(url, headers={"Authorization": f"Bearer {tokens['preview']}"})
    assert r.status_code == 403 and not calls, r.text
    r = client.post(url, headers={"Authorization": f"Bearer {tokens['live']}"})
    assert r.status_code == 200 and calls[-1].headers["authorization"] == f"Bearer {KEY_VALUE}"


def test_the_egress_route_refuses_a_host_that_answers_with_a_private_address(agent_tree, monkeypatch):
    """The request is made by the proxy process: an approved host whose record
    answers a private address is refused with the address in the reason
    (``services.infra.outbound_url``), after the allowlist and the rate bucket."""
    row = _approve(_row(secrets=SECRETS, egress=EGRESS))
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=agent_tree, data_dir=agent_tree, host_port=1,
                                   state="up", entry="server/index.ts")
    inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH, {"sub": f"app:{row['id']}"}, 3600)
    app_supervisor._instances[(row["id"], "live")] = inst
    seen: list[tuple[str, dict]] = []

    def refuse(url, **kw):
        seen.append((url, kw))
        return "the host 'api.example.test' resolves to a private or internal address (10.0.0.5)"

    monkeypatch.setattr(app_egress, "validate_outbound_url", refuse)
    app_egress._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    r = client.post(f"/v1/apps/{row['id']}/egress/api.example.test/v1/charges",
                    headers={"Authorization": f"Bearer {inst.token}"})
    assert r.status_code == 403
    assert "10.0.0.5" in r.text
    assert seen == [("https://api.example.test/", {"require_https": True})]
    # A host outside the app's egress list never reaches the resolver.
    assert client.post(f"/v1/apps/{row['id']}/egress/other.example.test/x",
                       headers={"Authorization": f"Bearer {inst.token}"}).status_code == 404
    assert len(seen) == 1
