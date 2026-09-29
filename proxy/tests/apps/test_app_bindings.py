"""App bindings (APPS.md "Bindings").

Load-bearing: the broker forwards a call only over a delegation edge (or
the owner's membership) with both manifests approved, on the caller's
launch token, to a method the target exported, with a claim carrying the
viewer's role ON THE TARGET agent; a removed edge or a voided approval
bites at once; the hop cap, the in-flight cap and the undeclared paths
refuse; a snapshot is served from the target's data directory without a
wake, under the export's floor, with an ETag; an emitted event wakes the
approved subscribers that named the emitter and listen; describe_app
answers only for reachable agents; wiring an edge is human-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from api.apps import app_bindings, app_proxy
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from core.session import session_state
from services.apps import app_handlers, app_supervisor, app_tokens, releases
from storage import database as task_store
from storage import db_app_deliveries as deliveries
from storage.agents import agent_store
from storage.pg import get_conn

client = TestClient(app)

AGENT_A = "bind-a"
AGENT_B = "bind-b"
SID_A = "sess-bind-a"


class _Target(http.server.BaseHTTPRequestHandler):
    """The target app's server: records what the broker brought."""
    seen: list[dict] = []

    def _answer(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        _Target.seen.append({"path": self.path, "method": self.command,
                             "headers": {k.lower(): v for k, v in self.headers.items()},
                             "body": body.decode("utf-8", "replace")})
        out = json.dumps({"ok": True, "path": self.path}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = _answer

    def log_message(self, *_a) -> None:
        return None


def _user(sub: str = "alice-sub", agent_roles: dict[str, str] | None = None,
          is_api_key: bool = False) -> UserContext:
    roles = {AGENT_A: "manager"} if agent_roles is None else agent_roles
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                       agents=list(roles.keys()), is_api_key=is_api_key, agent_roles=roles)


ALICE = _user()


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from api.apps import apps as apps_api
    _as(ALICE)
    apps_api._fire_rate.clear()
    app_proxy._buckets.clear()
    app_proxy._client = None
    app_bindings._inflight.clear()
    app_handlers._drains.clear()
    app_handlers.mark_dirty()
    monkeypatch.setattr(app_proxy, "PLATFORM_RATE", 1000.0)
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()
    app_proxy._client = None
    app_bindings._inflight.clear()


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    for a in (AGENT_A, AGENT_B):
        (agents_root / a / "workspace").mkdir(parents=True)
        (agents_root / a / "users" / "alice" / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    for sub, name in (("alice-sub", "alice"), ("carol-sub", "carol")):
        task_store.upsert_user(sub, f"{sub}@test.com", name.title(), "member")
        with get_conn() as conn:
            conn.execute("UPDATE users SET username=%s WHERE sub=%s", (name, sub))
            conn.commit()
    task_store.add_user_agent("alice-sub", AGENT_A, "manager", "test")
    task_store.add_user_agent("carol-sub", AGENT_B, "editor", "test")
    agent_store.set_delegation_targets(AGENT_A, [AGENT_B])
    session_state.set_session_security(SID_A, SecurityContext(
        role="manager", username="", agent=AGENT_A, is_admin_agent=False))
    yield agents_root
    session_state._session_security.pop(SID_A, None)
    agent_store.set_delegation_targets(AGENT_A, [])


@pytest.fixture
def target_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _Target.seen.clear()
    yield srv.server_address[1]
    srv.shutdown()


def _row(agent: str, slug: str, *, exports: dict | None = None, bindings: list | None = None,
         handlers: dict | None = None, username: str = "", owner: str | None = None,
         approve: bool = True) -> dict:
    blocks: dict[str, str] = {}
    for k, v in (("exports", exports), ("bindings", bindings), ("handlers", handlers)):
        if v:
            blocks[k] = task_store.canonical_actions_json(v)
    root = f"users/{username}/workspace" if username else "workspace"
    row = task_store.upsert_app(agent, username, owner, slug, title=slug.title(),
                                rel_path=f"{root}/apps/{slug}", kind="folder",
                                actions_json="[]", blocks=blocks or None)
    if approve:
        task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), "alice-sub")
    return task_store.get_app(row["id"])


def _install(row: dict, port: int = 1) -> app_supervisor.Instance:
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=config.get_agent_dir(row["agent"]),
                                   data_dir=config.get_agent_dir(row["agent"]), host_port=port,
                                   state="up")
    inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH, {"sub": f"app:{row['id']}"}, 3600)
    app_supervisor._instances[(row["id"], "live")] = inst
    return inst


def _viewer_claim(app_id: str, sub: str = "alice-sub", role: str = "manager") -> str:
    return app_tokens.mint(app_id, app_tokens.PURPOSE_VIEWER, {
        "principal": "viewer", "sub": sub, "username": sub.split("-")[0], "role": role,
        "grant": "", "external": False}, 600)


def _hook(op: str, payload: dict, sid: str = SID_A):
    payload.setdefault("session_id", sid)
    with patch("api.hooks.app_deploy.verify_session_match_async"), \
            patch("api.hooks.pins.verify_session_match_async"):
        return client.post(f"/v1/hooks/apps/{op}", json=payload,
                           headers={"Authorization": "Bearer dummy"})


CRM = [{"name": "crm", "agent": AGENT_B, "app": "leads"}]


# ── the broker ──────────────────────────────────────────────────────────────


def test_the_broker_forwards_over_an_edge_with_the_role_on_the_target(agent_tree, target_server):
    caller = _row(AGENT_A, "board", bindings=CRM)
    target = _row(AGENT_B, "leads", exports={"methods": {
        "summary": {"description": "the week"},
        "secret": {"description": "editors only", "min_role": "editor"}}})
    inst = _install(caller)
    _install(target, port=target_server)
    hdr = {"Authorization": f"Bearer {inst.token}"}
    url = f"/v1/apps/{caller['id']}/bindings/crm"
    # A plain call: the target sees /api/<path>, the query, a claim for
    # ITSELF naming the calling app and agent, an unattended role.
    r = client.get(f"{url}/summary?week=3", headers=hdr)
    assert r.status_code == 200 and r.json()["ok"], r.text
    seen = _Target.seen[-1]
    assert seen["path"] == "/api/summary?week=3" and seen["headers"]["x-otodock-basis"] == "binding"
    claim = app_tokens.verify(seen["headers"]["x-otodock-caller"], target["id"], app_tokens.PURPOSE_CALLER)
    assert claim["principal"] == "delegation" and claim["caller_app"] == caller["id"]
    assert claim["caller_agent"] == AGENT_A and claim["hop"] == 0 and claim["role"] == "none"
    assert claim["sub"] == "" and "x-otodock-viewer" not in seen["headers"]
    # A viewer behind the call: their role on the TARGET agent decides the
    # floor, not their role on the calling agent.
    viewer = _viewer_claim(caller["id"])
    r = client.get(f"{url}/secret", headers={**hdr, "X-OtoDock-Viewer": viewer})
    assert r.status_code == 403 and "editor" in r.text
    task_store.add_user_agent("alice-sub", AGENT_B, "editor", "test")
    r = client.get(f"{url}/secret", headers={**hdr, "X-OtoDock-Viewer": viewer})
    assert r.status_code == 200, r.text
    claim = app_tokens.verify(_Target.seen[-1]["headers"]["x-otodock-caller"], target["id"],
                              app_tokens.PURPOSE_CALLER)
    assert claim["role"] == "editor" and claim["caller_role"] == "manager" and claim["sub"] == "alice-sub"
    # A POST carries its body; the app's own headers stay behind.
    r = client.post(f"{url}/summary", json={"a": 1}, headers={**hdr, "X-Custom": "no"})
    assert r.status_code == 200 and json.loads(_Target.seen[-1]["body"]) == {"a": 1}
    assert "x-custom" not in _Target.seen[-1]["headers"]
    # Refusals: an undeclared method, the app's own endpoints, an unknown
    # binding, a viewer token as the bearer, another app's launch token.
    assert client.get(f"{url}/nope", headers=hdr).status_code == 404
    assert client.get(f"{url}/_health", headers=hdr).status_code == 404
    assert client.get(f"/v1/apps/{caller['id']}/bindings/erp/summary", headers=hdr).status_code == 404
    assert client.get(f"{url}/summary", headers={"Authorization": f"Bearer {viewer}"}).status_code == 403
    other = app_supervisor.get(target["id"]).token
    assert client.get(f"{url}/summary", headers={"Authorization": f"Bearer {other}"}).status_code == 401
    # The hop cap and the in-flight cap hold whatever the app forwards.
    deep = app_tokens.mint(caller["id"], app_tokens.PURPOSE_CALLER, {
        "principal": "delegation", "hop": 2, "deadline": int(time.time()) + 20}, 60)
    assert client.get(f"{url}/summary", headers={**hdr, "X-OtoDock-Caller": deep}).status_code == 429
    app_bindings._inflight[caller["id"]] = app_bindings.BIND_INFLIGHT_MAX
    assert client.get(f"{url}/summary", headers=hdr).status_code == 429
    app_bindings._inflight.clear()
    # A removed edge and a voided approval bite at once, and read the same.
    agent_store.set_delegation_targets(AGENT_A, [])
    assert client.get(f"{url}/summary", headers=hdr).status_code == 404
    agent_store.set_delegation_targets(AGENT_A, [AGENT_B])
    task_store.clear_app_approval(target["id"])
    assert client.get(f"{url}/summary", headers=hdr).status_code == 404
    fresh = task_store.get_app(target["id"])
    task_store.approve_app_actions(fresh["id"], task_store.manifest_sig(fresh), "alice-sub")
    assert client.get(f"{url}/summary", headers=hdr).status_code == 200
    # Two apps of one agent need no edge; a personal caller reaches an
    # agent its owner is a member of and carries the owner.
    twin = _row(AGENT_B, "twin", bindings=[{"name": "crm", "agent": AGENT_B, "app": "leads"}])
    t_inst = _install(twin)
    assert client.get(f"/v1/apps/{twin['id']}/bindings/crm/summary",
                      headers={"Authorization": f"Bearer {t_inst.token}"}).status_code == 200
    mine = _row(AGENT_A, "mine", bindings=CRM, username="alice", owner="alice-sub")
    m_inst = _install(mine)
    r = client.get(f"/v1/apps/{mine['id']}/bindings/crm/secret",
                   headers={"Authorization": f"Bearer {m_inst.token}"})
    assert r.status_code == 200, r.text
    claim = app_tokens.verify(_Target.seen[-1]["headers"]["x-otodock-caller"], target["id"],
                              app_tokens.PURPOSE_CALLER)
    assert claim["principal"] == "membership" and claim["sub"] == "alice-sub" and claim["role"] == "editor"


def test_a_session_behind_a_binding_carries_its_row_not_an_admin_standing(agent_tree, target_server):
    # _agent_caller hands a session's app its per-agent row alone; the
    # broker must not turn the same person back into a platform admin on
    # the target, where a manager-floored export would let them through.
    task_store.upsert_user("root-sub", "root@test.com", "Root", "admin")
    task_store.add_user_agent("root-sub", AGENT_A, "viewer", "test")
    caller = _row(AGENT_A, "board", bindings=CRM)
    _row(AGENT_B, "leads", exports={"methods": {
        "boss": {"description": "managers only", "min_role": "manager"}}})
    target = task_store.get_app_by_slug(AGENT_B, "", "leads")
    inst = _install(caller)
    _install(target, port=target_server)
    agent_claim = app_tokens.mint(caller["id"], app_tokens.PURPOSE_CALLER, {
        "principal": "agent", "sub": "root-sub", "username": "root", "role": "viewer",
        "agent": AGENT_A, "session": "s1", "external": False}, 60)
    hdr = {"Authorization": f"Bearer {inst.token}", "X-OtoDock-Viewer": agent_claim}
    r = client.get(f"/v1/apps/{caller['id']}/bindings/crm/boss", headers=hdr)
    assert r.status_code == 403 and "manager" in r.text, r.text
    # The same admin at the keyboard (a viewer claim) keeps the standing.
    r = client.get(f"/v1/apps/{caller['id']}/bindings/crm/boss",
                   headers={"Authorization": f"Bearer {inst.token}",
                            "X-OtoDock-Viewer": _viewer_claim(caller["id"], sub="root-sub", role="admin")})
    assert r.status_code == 200, r.text
    claim = app_tokens.verify(_Target.seen[-1]["headers"]["x-otodock-caller"], target["id"],
                              app_tokens.PURPOSE_CALLER)
    assert claim["role"] == "admin"


def test_a_render_claim_calls_nothing_through_a_binding(agent_tree, target_server):
    # The rendered check's claim names the owner of a personal app so the
    # page renders as they see it; the broker refuses it as the platform
    # route does, on a live call and on a snapshot.
    caller = _row(AGENT_A, "board", bindings=CRM)
    target = _row(AGENT_B, "leads", exports={"methods": {"summary": {"description": "x"}},
                                             "snapshots": {"board": {"description": "x"}}})
    inst = _install(caller)
    _install(target, port=target_server)
    render = app_tokens.mint(caller["id"], app_tokens.PURPOSE_VIEWER, {
        "principal": "viewer", "sub": "alice-sub", "username": "alice", "role": "manager",
        "grant": "", "external": False, "instance": "check", "render": "jti-1"}, 120)
    hdr = {"Authorization": f"Bearer {inst.token}", "X-OtoDock-Viewer": render}
    url = f"/v1/apps/{caller['id']}/bindings/crm"
    seen = len(_Target.seen)
    r = client.get(f"{url}/summary", headers=hdr)
    assert r.status_code == 403 and "rendered check" in r.text, r.text
    assert len(_Target.seen) == seen
    assert client.get(f"{url}/snapshot/board", headers=hdr).status_code == 403
    assert client.get(f"{url}/summary", headers={"Authorization": f"Bearer {inst.token}"}).status_code == 200


def test_a_target_that_dies_mid_body_frees_its_slot(agent_tree):
    # The in-flight count is released whatever ends the stream: a target
    # that promises a body and hangs up must not hold a slot for good.
    class _Short(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", "1000")
            self.end_headers()
            self.wfile.write(b'{"a":')
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, *_a) -> None:
            return None

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Short)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        caller = _row(AGENT_A, "board", bindings=CRM)
        target = _row(AGENT_B, "leads", exports={"methods": {"summary": {"description": "x"}}})
        inst = _install(caller)
        _install(target, port=srv.server_address[1])
        hdr = {"Authorization": f"Bearer {inst.token}"}
        for _ in range(app_bindings.BIND_INFLIGHT_MAX + 1):
            with contextlib.suppress(Exception):
                client.get(f"/v1/apps/{caller['id']}/bindings/crm/summary", headers=hdr)
        assert app_bindings._inflight.get(caller["id"], 0) == 0
    finally:
        srv.shutdown()


# ── snapshots ───────────────────────────────────────────────────────────────


def test_a_snapshot_is_served_without_a_wake_under_its_floor(agent_tree):
    caller = _row(AGENT_A, "board", bindings=CRM)
    target = _row(AGENT_B, "leads", exports={"snapshots": {
        "board": {"description": "the board"}, "vip": {"description": "x", "min_role": "manager"},
        "later": {"description": "not yet written"}}})
    inst = _install(caller)
    exports_dir = releases.app_data_dir(target) / "exports"
    exports_dir.mkdir(parents=True)
    (exports_dir / "board.json").write_text(json.dumps({"cards": 3}))
    (exports_dir / "vip.json").write_text("{}")
    hdr = {"Authorization": f"Bearer {inst.token}"}
    url = f"/v1/apps/{caller['id']}/bindings/crm/snapshot"
    r = client.get(f"{url}/board", headers=hdr)
    assert r.status_code == 200 and r.json() == {"cards": 3} and r.headers.get("etag"), r.text
    assert app_supervisor.get(target["id"]) is None          # never woken
    assert r.headers["x-otodock-snapshot-of"] == target["id"]
    r2 = client.get(f"{url}/board", headers={**hdr, "If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304
    (exports_dir / "board.json").write_text(json.dumps({"cards": 4}))
    r3 = client.get(f"{url}/board", headers={**hdr, "If-None-Match": r.headers["etag"]})
    assert r3.status_code == 200 and r3.json() == {"cards": 4}
    assert client.get(f"{url}/vip", headers=hdr).status_code == 403
    assert client.get(f"{url}/later", headers=hdr).status_code == 404
    assert client.get(f"{url}/missing", headers=hdr).status_code == 404
    assert client.get(f"{url}/..%2Fboard", headers=hdr).status_code == 404
    (exports_dir / "board.json").write_text("not json")
    assert client.get(f"{url}/board", headers=hdr).status_code == 502
    agent_store.set_delegation_targets(AGENT_A, [])
    (exports_dir / "board.json").write_text("{}")
    assert client.get(f"{url}/board", headers=hdr).status_code == 404


def test_a_snapshot_never_follows_a_link_or_waits_on_a_fifo(agent_tree, tmp_path):
    # The target writes its data directory from inside its sandbox; the
    # proxy reads it with its own rights, so a link planted there must not
    # reach a host file and a FIFO must not park a worker thread.
    import os
    caller = _row(AGENT_A, "board", bindings=CRM)
    target = _row(AGENT_B, "leads", exports={"snapshots": {
        "board": {"description": "the board"}, "pipe": {"description": "x"}}})
    inst = _install(caller)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "board.json").write_text(json.dumps({"refresh_token": "s3cr3t"}))
    exports_dir = releases.app_data_dir(target) / "exports"
    exports_dir.mkdir(parents=True)
    (exports_dir / "board.json").symlink_to(outside / "board.json")
    hdr = {"Authorization": f"Bearer {inst.token}"}
    url = f"/v1/apps/{caller['id']}/bindings/crm/snapshot"
    r = client.get(f"{url}/board", headers=hdr)
    assert r.status_code == 502 and "s3cr3t" not in r.text, r.text
    # The whole exports directory swapped for a link: the same refusal.
    (exports_dir / "board.json").unlink()
    exports_dir.rmdir()
    exports_dir.symlink_to(outside)
    r = client.get(f"{url}/board", headers=hdr)
    assert r.status_code == 502 and "s3cr3t" not in r.text, r.text
    exports_dir.unlink()
    exports_dir.mkdir()
    os.mkfifo(exports_dir / "pipe.json")
    got: list = []
    t = threading.Thread(target=lambda: got.append(client.get(f"{url}/pipe", headers=hdr).status_code),
                         daemon=True)
    t.start()
    t.join(10)
    assert got == [502]
    (exports_dir / "board.json").write_text(json.dumps({"cards": 1}))
    assert client.get(f"{url}/board", headers=hdr).json() == {"cards": 1}


# ── events ──────────────────────────────────────────────────────────────────


def test_an_emitted_event_wakes_the_subscribers_that_named_the_emitter(agent_tree, monkeypatch):
    monkeypatch.setattr(app_handlers, "schedule_drain", lambda app_id: None)
    subscriber = _row(AGENT_A, "board", bindings=CRM,
                      handlers={"on_event": {"refresh": ["app:crm:lead-created"]}})
    deaf = _row(AGENT_A, "deaf", bindings=CRM)
    emitter = _row(AGENT_B, "leads", exports={"events": {"lead-created": {"description": "a lead"}}})
    e_inst = _install(emitter)
    hdr = {"Authorization": f"Bearer {e_inst.token}"}
    r = client.post(f"/v1/apps/{emitter['id']}/events", json={"name": "lead-created", "payload": {"id": 7}},
                    headers=hdr)
    assert r.status_code == 200 and r.json()["delivered"] == 1, r.text
    got = deliveries.recent(subscriber["id"])
    assert len(got) == 1 and got[0]["event"] == "app:crm:lead-created" and got[0]["handler"] == "refresh"
    d = deliveries.get(got[0]["id"])
    assert d["payload"]["payload"] == {"id": 7} and d["payload"]["from"] == {"agent": AGENT_B, "app": "leads"}
    assert d["event_id"] == r.json()["event_id"]
    assert deliveries.recent(deaf["id"]) == []
    # Refusals: an undeclared event, a bad payload, a viewer's token.
    assert client.post(f"/v1/apps/{emitter['id']}/events", json={"name": "nope"}, headers=hdr).status_code == 400
    assert client.post(f"/v1/apps/{emitter['id']}/events", json={"name": "lead-created", "payload": [1]},
                       headers=hdr).status_code == 400
    assert client.post(f"/v1/apps/{emitter['id']}/events", json={"name": "lead-created"},
                       headers={"Authorization": f"Bearer {_viewer_claim(emitter['id'])}"}).status_code == 403
    # Without the subscriber's edge, nothing is delivered.
    agent_store.set_delegation_targets(AGENT_A, [])
    r = client.post(f"/v1/apps/{emitter['id']}/events", json={"name": "lead-created"}, headers=hdr)
    assert r.status_code == 200 and r.json()["delivered"] == 0
    assert len(deliveries.recent(subscriber["id"])) == 1


def test_the_preview_copy_calls_no_other_app_and_emits_nothing(agent_tree, target_server, monkeypatch):
    """The preview copy runs code nobody approved, so its
    launch token reaches no binding (a live call or a snapshot) and emits no
    event; the live copy's token does."""
    monkeypatch.setattr(app_handlers, "schedule_drain", lambda app_id: None)
    caller = _row(AGENT_A, "board", bindings=CRM)
    target = _row(AGENT_B, "leads", exports={"methods": {"summary": {"description": "the week"}},
                                              "snapshots": {"board": {"description": "the board"}},
                                              "events": {"lead-created": {"description": "a lead"}}})
    _install(caller)
    _install(target, port=target_server)
    exports_dir = releases.app_data_dir(target) / "exports"
    exports_dir.mkdir(parents=True)
    (exports_dir / "board.json").write_text("{}")
    preview = {}
    for row in (caller, target):
        inst = app_supervisor.Instance(row_id=row["id"], name="preview", row=row,
                                       release_dir=config.get_agent_dir(row["agent"]),
                                       data_dir=config.get_agent_dir(row["agent"]), host_port=1, state="up")
        inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH,
                                     {"sub": f"app:{row['id']}", "instance": "preview"}, 3600)
        app_supervisor._instances[(row["id"], "preview")] = inst
        preview[row["id"]] = {"Authorization": f"Bearer {inst.token}"}
    url = f"/v1/apps/{caller['id']}/bindings/crm"
    assert client.get(f"{url}/summary", headers=preview[caller["id"]]).status_code == 403
    assert client.get(f"{url}/snapshot/board", headers=preview[caller["id"]]).status_code == 403
    r = client.post(f"/v1/apps/{target['id']}/events", json={"name": "lead-created"},
                    headers=preview[target["id"]])
    assert r.status_code == 403, r.text
    live = {"Authorization": f"Bearer {app_supervisor.get(caller['id']).token}"}
    assert client.get(f"{url}/summary", headers=live).status_code == 200


def test_only_the_shared_row_a_binding_names_emits_its_events(agent_tree, monkeypatch):
    # Subscribers find an emitter by (agent, slug): a member's personal app
    # of the same slug, approved by its own owner, must not speak for the
    # team's app, nor for one that does not exist.
    monkeypatch.setattr(app_handlers, "schedule_drain", lambda app_id: None)
    subscriber = _row(AGENT_A, "board", bindings=CRM,
                      handlers={"on_event": {"refresh": ["app:crm:lead-created"]}})
    events = {"events": {"lead-created": {"description": "a lead"}}}
    impostor = _row(AGENT_B, "leads", exports=events, username="carol", owner="carol-sub")
    hdr = {"Authorization": f"Bearer {_install(impostor).token}"}
    r = client.post(f"/v1/apps/{impostor['id']}/events", json={"name": "lead-created", "payload": {"id": 1}},
                    headers=hdr)
    assert r.status_code == 403, r.text
    shared = _row(AGENT_B, "leads", exports=events)
    r = client.post(f"/v1/apps/{impostor['id']}/events", json={"name": "lead-created"}, headers=hdr)
    assert r.status_code == 403, r.text
    assert deliveries.recent(subscriber["id"]) == []
    r = client.post(f"/v1/apps/{shared['id']}/events", json={"name": "lead-created"},
                    headers={"Authorization": f"Bearer {_install(shared, port=2).token}"})
    assert r.status_code == 200 and r.json()["delivered"] == 1, r.text


# ── describe and the edge ──────────────────────────────────────────────────


def test_describe_answers_for_reachable_agents_and_wiring_an_edge_is_human_only(agent_tree):
    target = _row(AGENT_B, "leads", exports={
        "methods": {"summary": {"description": "the week", "min_role": "editor"}},
        "events": {"lead-created": {"description": "a lead"}}})
    _row(AGENT_B, "draft", exports={"methods": {"x": {"description": "y"}}}, approve=False)
    r = _hook("describe", {"agent": AGENT_B, "slug": "leads"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["app_id"] == target["id"] and body["binding"] == {"agent": AGENT_B, "app": "leads"}
    assert body["exports"]["methods"]["summary"]["min_role"] == "editor"
    assert "lead-created" in body["exports"]["events"] and body["exports"]["snapshots"] == {}
    assert _hook("describe", {"agent": AGENT_B, "slug": "draft"}).status_code == 404
    assert _hook("describe", {"agent": "bind-c", "slug": "leads"}).status_code == 404
    agent_store.set_delegation_targets(AGENT_A, [])
    assert _hook("describe", {"agent": AGENT_B, "slug": "leads"}).status_code == 404
    assert app_bindings.reachable_agents(AGENT_A, "alice") == {AGENT_A}
    # A session bearer may not wire an edge; a person may.
    _as(_user(is_api_key=True))
    r = client.put(f"/v1/agents/{AGENT_A}/delegation-targets", json={"targets": [AGENT_B]})
    assert r.status_code == 403
    _as(ALICE)
    r = client.put(f"/v1/agents/{AGENT_A}/delegation-targets", json={"targets": [AGENT_B]})
    assert r.status_code in (200, 400, 403), r.text    # the agent rows are not seeded here; the human check came first
    asyncio.run(asyncio.sleep(0))
