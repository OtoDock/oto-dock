"""Pinned mini-apps: pin/unpin/list hooks + cookie-authed serve route +
CRUD/approval/exec endpoints + registry cleanup.

The security-load-bearing assertions live here: the fixed slug-derived save
path (no caller path input), the sandbox CSP on EVERY serve branch, denied ==
missing on serve/CRUD (no oracle), the approval sig lifecycle (stale sig →
409; manifest mutation silently kills approval), the approver-authority rule
(approval delegates a task run to every viewer, so the approver must pass the
``/v1/tasks/{id}/run`` rule — re-checked with their CURRENT role at exec),
fire_task target restrictions (scheduled/trigger only, same agent, shared →
agent-scope), and the verbatim fire (no page-controlled prompt_override).
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from api.apps import manifest as _mf
from app import app
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from core.session import session_state
from storage import database as task_store

client = TestClient(app)

SID = "sess-app-1"
SID_SHARED = "sess-app-shared"
AGENT = "apps-agent"


def _user(sub: str = "alice-sub", role: str = "member",
          agents: tuple[str, ...] = (AGENT,),
          agent_roles: dict[str, str] | None = None) -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role=role,
                       agents=list(agents),
                       agent_roles=agent_roles or {AGENT: "manager"})


@pytest.fixture(autouse=True)
def authed():
    app.dependency_overrides[get_current_user] = _user
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _reset_fire_rate():
    from api.apps import apps as apps_api
    apps_api._fire_rate.clear()
    yield
    apps_api._fire_rate.clear()


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    """Temp agents tree with alice (personal scope, SID) and an agent-scope
    session (SID_SHARED) registered, plus the matching users rows the
    owner_sub FK and the authority reconstruction need."""
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    (agents_root / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    task_store.upsert_user("alice-sub", "alice@test.com", "Alice", "member")
    # upsert_user derives its own username slug; the session ctx says
    # "alice", so align the row (ownership resolves via
    # get_user_sub_by_username).
    _set_username("alice-sub", "alice")
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    session_state.set_session_security(SID, SecurityContext(
        role="manager", username="alice", agent=AGENT, is_admin_agent=False))
    session_state.set_session_security(SID_SHARED, SecurityContext(
        role="manager", username="", agent=AGENT, is_admin_agent=False))
    yield agents_root / AGENT
    for sid in (SID, SID_SHARED):
        session_state._session_security.pop(sid, None)


def _set_username(sub: str, username: str) -> None:
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", (username, sub))
        conn.commit()


def _pin(payload: dict, sid: str = SID) -> object:
    payload.setdefault("session_id", sid)
    with patch("api.hooks.hooks.verify_session_match"):
        return client.post("/v1/hooks/apps/pin", json=payload,
                           headers={"Authorization": "Bearer dummy"})


def _hook(op: str, payload: dict, sid: str = SID) -> object:
    payload.setdefault("session_id", sid)
    with patch("api.hooks.hooks.verify_session_match"):
        return client.post(f"/v1/hooks/apps/{op}", json=payload,
                           headers={"Authorization": "Bearer dummy"})


def _mk_task(task_type: str = "trigger", scope: str = "agent",
             created_by: str = "alice-sub", agent: str = AGENT) -> str:
    task_id = str(uuid.uuid4())
    task_store.create_dynamic_task(
        task_id, agent, f"task-{task_type}", "do the thing", "auto",
        task_type, "0 9 * * *" if task_type == "scheduled" else None,
        None, None, 300, created_by, scope=scope, notification_mode="none",
    )
    return task_id


# ───────────────────────── POST /v1/hooks/apps/pin ──────────────────────────


def test_pin_validation(agent_tree):
    assert _pin({"slug": "Bad Slug", "html": "<p>x</p>"}).status_code == 400
    assert _pin({"slug": "-bad", "html": "<p>x</p>"}).status_code == 400
    assert _pin({"slug": "a" * 41, "html": "<p>x</p>"}).status_code == 400
    assert _pin({"slug": "ok", "html": "x" * (2 * 1024 * 1024 + 1)}).status_code == 400
    assert _pin({"slug": "ok", "html": "<p>x</p>", "title": "t" * 201}).status_code == 400
    # First pin requires html.
    r = _pin({"slug": "no-html"})
    assert r.status_code == 400 and "html is required" in r.json()["detail"]
    r = _pin({"slug": "ok", "html": "<p>x</p>", "session_id": "nope"})
    assert r.status_code == 400 and "unknown session" in r.json()["detail"]


def test_pin_personal_writes_fixed_path_and_registers(agent_tree):
    r = _pin({"slug": "brief", "title": "Morning brief", "html": "<p>hi</p>"})
    assert r.status_code == 200
    body = r.json()
    # Fixed slug-derived location in the CALLER's scope — never caller input.
    assert body["path"] == "/users/alice/workspace/apps/brief.html"
    assert body["scope"] == "personal"
    assert body["approval"] == "none"
    saved = agent_tree / "users/alice/workspace/apps/brief.html"
    assert saved.read_text() == "<p>hi</p>"  # raw content, wrapped at serve time

    row = task_store.get_app(body["app_id"])
    assert row["agent"] == AGENT and row["slug"] == "brief"
    assert row["username"] == "alice" and row["owner_sub"] == "alice-sub"
    assert row["rel_path"] == "users/alice/workspace/apps/brief.html"


def test_pin_agent_scope_session_is_shared(agent_tree):
    r = _pin({"slug": "board", "html": "<p>x</p>"}, sid=SID_SHARED)
    assert r.status_code == 200
    assert r.json()["scope"] == "shared"
    row = task_store.get_app(r.json()["app_id"])
    assert row["username"] == "" and row["owner_sub"] is None
    assert row["rel_path"] == "workspace/apps/board.html"


def test_pin_upsert_and_metadata_only_update(agent_tree):
    first = _pin({"slug": "brief", "html": "<p>v1</p>", "title": "V1"}).json()
    again = _pin({"slug": "brief", "html": "<p>v2</p>"}).json()
    assert again["app_id"] == first["app_id"]
    saved = agent_tree / "users/alice/workspace/apps/brief.html"
    assert saved.read_text() == "<p>v2</p>"
    # Metadata-only update (no html) keeps the file.
    meta = _pin({"slug": "brief", "title": "V2"})
    assert meta.status_code == 200
    assert saved.read_text() == "<p>v2</p>"
    row = task_store.get_app(first["app_id"])
    assert row["title"] == "V2"


def test_pin_broadcasts_file_updated_on_html_write(agent_tree, monkeypatch):
    calls = []

    async def fake_broadcast(agent_slug, rel_path, **kw):
        calls.append((agent_slug, rel_path))

    monkeypatch.setattr(
        "services.notifications.notification_manager.broadcast_file_updated",
        fake_broadcast,
    )
    _pin({"slug": "brief", "html": "<p>x</p>"})
    assert calls == [(AGENT, "users/alice/workspace/apps/brief.html")]
    calls.clear()
    # Metadata-only updates don't rewrite the file but STILL broadcast — the
    # overlay refreshes its registry view on any apps/*.html file_updated
    # (that is how a revived soft-unpinned row reappears in open tabs).
    _pin({"slug": "brief", "title": "rename only"})
    assert calls == [(AGENT, "users/alice/workspace/apps/brief.html")]


def test_pin_actions_validation_matrix(agent_tree):
    trig = _mk_task("trigger", scope="agent")
    one_time = _mk_task("one_time", scope="agent")
    foreign = _mk_task("trigger", agent="other-agent")

    def pin_with(actions, sid=SID):
        return _pin({"slug": "b", "html": "<p>x</p>", "actions": actions}, sid=sid)

    ok = pin_with([{"id": "go", "label": "Go", "type": "fire_task", "task_id": trig}])
    assert ok.status_code == 200 and ok.json()["approval"] == "pending user approval"

    r = pin_with([{"id": "go", "label": "Go", "type": "fire_task", "task_id": one_time}])
    assert r.status_code == 400 and "scheduled or trigger" in r.json()["detail"]
    r = pin_with([{"id": "go", "label": "Go", "type": "fire_task", "task_id": foreign}])
    assert r.status_code == 400 and "another agent" in r.json()["detail"]
    r = pin_with([{"id": "go", "label": "Go", "type": "fire_task", "task_id": "nope"}])
    assert r.status_code == 400 and "not found" in r.json()["detail"]

    # A SHARED app cannot fire a user-scoped task.
    user_task = _mk_task("trigger", scope="user")
    r = pin_with(
        [{"id": "go", "label": "Go", "type": "fire_task", "task_id": user_task}],
        sid=SID_SHARED,
    )
    assert r.status_code == 400 and "agent-scoped" in r.json()["detail"]

    r = pin_with([{"id": "p", "label": "P", "type": "send_prompt", "prompt": ""}])
    assert r.status_code == 400
    r = pin_with([{"id": "p", "label": "P", "type": "nope"}])
    assert r.status_code == 400
    r = pin_with([{"id": "dup", "label": "A", "type": "send_prompt", "prompt": "x"},
                  {"id": "dup", "label": "B", "type": "send_prompt", "prompt": "y"}])
    assert r.status_code == 400 and "duplicate" in r.json()["detail"]


def test_pin_cap_per_scope(agent_tree, monkeypatch):
    monkeypatch.setattr(task_store, "MAX_APPS_PER_SCOPE", 2)
    monkeypatch.setattr("storage.db_apps.MAX_APPS_PER_SCOPE", 2)
    assert _pin({"slug": "a1", "html": "<p>1</p>"}).status_code == 200
    assert _pin({"slug": "a2", "html": "<p>2</p>"}).status_code == 200
    r = _pin({"slug": "a3", "html": "<p>3</p>"})
    assert r.status_code == 400 and "limit" in r.json()["detail"]
    # Updates still allowed at the cap; other scopes unaffected.
    assert _pin({"slug": "a1", "html": "<p>1b</p>"}).status_code == 200
    assert _pin({"slug": "s1", "html": "<p>s</p>"}, sid=SID_SHARED).status_code == 200


def test_make_default_renumbers_within_scope_only(agent_tree):
    a = _pin({"slug": "a", "html": "<p>a</p>"}).json()["app_id"]
    b = _pin({"slug": "b", "html": "<p>b</p>"}).json()["app_id"]
    s = _pin({"slug": "s", "html": "<p>s</p>"}, sid=SID_SHARED).json()["app_id"]
    _pin({"slug": "b", "make_default": True})
    rows = {r["id"]: r for r in task_store.list_apps(AGENT, "alice")}
    assert rows[b]["position"] == 0 and rows[a]["position"] == 1
    assert rows[s]["position"] == 0  # the shared list was never touched


def test_unpin_keeps_file(agent_tree):
    body = _pin({"slug": "brief", "html": "<p>x</p>"}).json()
    r = _hook("unpin", {"slug": "brief"})
    assert r.status_code == 200
    assert task_store.get_app(body["app_id"]) is None
    assert (agent_tree / "users/alice/workspace/apps/brief.html").exists()
    assert _hook("unpin", {"slug": "brief"}).status_code == 404


def test_list_hook_shows_merged_scope(agent_tree):
    _pin({"slug": "shared-app", "html": "<p>s</p>"}, sid=SID_SHARED)
    _pin({"slug": "mine", "html": "<p>m</p>"})
    r = _hook("list", {})
    slugs = [a["slug"] for a in r.json()["apps"]]
    assert slugs == ["shared-app", "mine"]  # shared group first
    r = _hook("list", {}, sid=SID_SHARED)
    assert [a["slug"] for a in r.json()["apps"]] == ["shared-app"]


# ───────────────────────── GET /v1/apps/{id}/html ───────────────────────────


def _csp_ok(resp) -> None:
    csp = resp.headers["content-security-policy"]
    assert "sandbox allow-scripts" in csp
    assert "script-src http://testserver 'unsafe-inline'" in csp
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"


def test_serve_app_wraps_with_app_runtime(agent_tree):
    app_id = _pin({"slug": "brief", "html": "<p>hi</p>"}).json()["app_id"]
    r = client.get(f"/v1/apps/{app_id}/html")
    assert r.status_code == 200
    _csp_ok(r)
    assert "<p>hi</p>" in r.text
    assert "otodock-tokens.css" in r.text
    assert "window.otodock = { send:" in r.text          # base runtime
    assert "window.otodock.action = function" in r.text  # app extension


def test_serve_app_full_document_verbatim(agent_tree):
    doc = "<!doctype html><html><body>raw</body></html>"
    app_id = _pin({"slug": "raw", "html": doc}).json()["app_id"]
    r = client.get(f"/v1/apps/{app_id}/html")
    assert r.status_code == 200 and r.text == doc
    _csp_ok(r)


def test_serve_app_auth_and_404_parity(agent_tree):
    app_id = _pin({"slug": "brief", "html": "<p>x</p>"}).json()["app_id"]
    _as(None)
    r = client.get(f"/v1/apps/{app_id}/html")
    assert r.status_code == 401
    _csp_ok(r)  # the sandbox headers hold on EVERY branch

    # A different user cannot serve alice's personal app — and the body is
    # byte-identical to a missing id (no liveness oracle).
    _as(_user(sub="bob-sub", agent_roles={AGENT: "viewer"}))
    denied = client.get(f"/v1/apps/{app_id}/html")
    missing = client.get(f"/v1/apps/{uuid.uuid4()}/html")
    assert denied.status_code == missing.status_code == 404
    assert denied.text == missing.text
    _csp_ok(denied)


def test_serve_shared_app_any_agent_user(agent_tree):
    app_id = _pin({"slug": "board", "html": "<p>b</p>"}, sid=SID_SHARED).json()["app_id"]
    _as(_user(sub="bob-sub", agent_roles={AGENT: "viewer"}))
    assert client.get(f"/v1/apps/{app_id}/html").status_code == 200
    # No agent access → the same 404.
    _as(_user(sub="eve-sub", agents=(), agent_roles={}))
    assert client.get(f"/v1/apps/{app_id}/html").status_code == 404


def test_serve_app_missing_file(agent_tree):
    app_id = _pin({"slug": "brief", "html": "<p>x</p>"}).json()["app_id"]
    (agent_tree / "users/alice/workspace/apps/brief.html").unlink()
    r = client.get(f"/v1/apps/{app_id}/html")
    assert r.status_code == 404 and "deleted from the workspace" in r.text
    _csp_ok(r)


# ───────────────────────── CRUD: list / approve / exec ──────────────────────


def test_list_api_scoping_and_order(agent_tree):
    _pin({"slug": "shared-app", "html": "<p>s</p>"}, sid=SID_SHARED)
    _pin({"slug": "mine", "html": "<p>m</p>"})
    # Another user's personal app must not leak into alice's list.
    task_store.upsert_user("bob-sub", "bob@test.com", "Bob", "member")
    _set_username("bob-sub", "bob")
    task_store.upsert_app(AGENT, "bob", "bob-sub", "bobs",
                          title="B", rel_path="users/bob/workspace/apps/bobs.html")
    r = client.get(f"/v1/apps?agent={AGENT}")
    apps = r.json()["apps"]
    assert [(a["slug"], a["scope"]) for a in apps] == [
        ("shared-app", "shared"), ("mine", "personal"),
    ]
    assert all(a["can_manage"] for a in apps)  # alice is manager + owner


def test_approve_sig_lifecycle(agent_tree):
    trig = _mk_task("trigger", scope="agent", created_by="alice-sub")
    body = _pin({"slug": "b", "html": "<p>x</p>", "actions": [
        {"id": "go", "label": "Go", "type": "fire_task", "task_id": trig},
    ]}).json()
    app_id = body["app_id"]
    listed = client.get(f"/v1/apps?agent={AGENT}").json()["apps"][0]
    assert listed["actions_approved"] is False and listed["can_approve"] is True
    sig = listed["actions_sig"]

    # Stale sig (manifest mutated after the card rendered) → 409.
    r = client.post(f"/v1/apps/{app_id}/approve", json={"sig": "0" * 64})
    assert r.status_code == 409

    assert client.post(f"/v1/apps/{app_id}/approve", json={"sig": sig}).status_code == 200
    listed = client.get(f"/v1/apps?agent={AGENT}").json()["apps"][0]
    assert listed["actions_approved"] is True

    # Re-pinning a CHANGED manifest silently breaks the approval.
    _pin({"slug": "b", "actions": [
        {"id": "go", "label": "Go now", "type": "fire_task", "task_id": trig},
    ]})
    listed = client.get(f"/v1/apps?agent={AGENT}").json()["apps"][0]
    assert listed["actions_approved"] is False


def test_approve_requires_task_run_authority(agent_tree):
    # bob (editor) approving an app whose fire_task targets an agent-scope
    # task ALICE created → editor-not-creator fails the run rule → 403.
    trig = _mk_task("trigger", scope="agent", created_by="alice-sub")
    task_store.upsert_user("bob-sub", "bob@test.com", "Bob", "member")
    _set_username("bob-sub", "bob")
    task_store.add_user_agent("bob-sub", AGENT, "editor", "test")
    app_id = _pin({"slug": "b", "html": "<p>x</p>", "actions": [
        {"id": "go", "label": "Go", "type": "fire_task", "task_id": trig},
    ]}, sid=SID_SHARED).json()["app_id"]

    _as(_user(sub="bob-sub", agent_roles={AGENT: "editor"}))
    listed = client.get(f"/v1/apps?agent={AGENT}").json()["apps"][0]
    assert listed["can_approve"] is False
    r = client.post(f"/v1/apps/{app_id}/approve", json={"sig": listed["actions_sig"]})
    assert r.status_code == 403 and "run authority" in r.json()["detail"]

    # A viewer lacks even the app surface.
    _as(_user(sub="bob-sub", agent_roles={AGENT: "viewer"}))
    r = client.post(f"/v1/apps/{app_id}/approve", json={"sig": listed["actions_sig"]})
    assert r.status_code == 403


def _approve(app_id: str) -> None:
    listed = client.get(f"/v1/apps?agent={AGENT}").json()["apps"]
    row = next(a for a in listed if a["id"] == app_id)
    assert client.post(f"/v1/apps/{app_id}/approve",
                       json={"sig": row["actions_sig"]}).status_code == 200


def test_fire_task_exec_verbatim_and_rate_limit(agent_tree, monkeypatch):
    trig = _mk_task("trigger", scope="agent", created_by="alice-sub")
    app_id = _pin({"slug": "b", "html": "<p>x</p>", "actions": [
        {"id": "go", "label": "Go", "type": "fire_task", "task_id": trig},
        {"id": "ask", "label": "Ask", "type": "send_prompt", "prompt": "hi"},
    ]}, sid=SID_SHARED).json()["app_id"]

    fired = []

    async def fake_trigger(task_def, trigger_type="manual", trigger_source=None,
                           prompt_override=None, trigger_payload=None):
        fired.append((task_def.id, trigger_type, trigger_source,
                      prompt_override, trigger_payload))
        return "run-1"

    from services.scheduler import scheduler
    monkeypatch.setattr(scheduler, "trigger_task_now", fake_trigger)

    # Unapproved → 409, nothing fires.
    r = client.post(f"/v1/apps/{app_id}/actions/go")
    assert r.status_code == 409 and fired == []

    _approve(app_id)
    r = client.post(f"/v1/apps/{app_id}/actions/go")
    assert r.status_code == 200 and r.json()["run_id"] == "run-1"
    # VERBATIM: no page-controlled prompt_override / payload, ever.
    assert fired == [(trig, "app_action", "b:go", None, None)]

    # Immediate second click → 429 (min-interval).
    assert client.post(f"/v1/apps/{app_id}/actions/go").status_code == 429

    # send_prompt actions never execute over REST.
    r = client.post(f"/v1/apps/{app_id}/actions/ask")
    assert r.status_code == 400 and "chat" in r.json()["detail"]
    assert client.post(f"/v1/apps/{app_id}/actions/nope").status_code == 404


def test_fire_task_exec_recheck_and_stale_approver(agent_tree, monkeypatch):
    trig = _mk_task("trigger", scope="agent", created_by="alice-sub")
    app_id = _pin({"slug": "b", "html": "<p>x</p>", "actions": [
        {"id": "go", "label": "Go", "type": "fire_task", "task_id": trig},
    ]}, sid=SID_SHARED).json()["app_id"]
    _approve(app_id)

    async def boom(*a, **k):  # a fire reaching the scheduler would be the bug
        raise AssertionError("must not fire")

    from services.scheduler import scheduler
    monkeypatch.setattr(scheduler, "trigger_task_now", boom)

    # Task deleted since approval → 409 at click time.
    task_store.delete_dynamic_task(trig)
    r = client.post(f"/v1/apps/{app_id}/actions/go")
    assert r.status_code == 409

    # Approver demoted since approval → approval stale.
    trig2 = _mk_task("trigger", scope="agent", created_by="alice-sub")
    app_id2 = _pin({"slug": "c", "html": "<p>x</p>", "actions": [
        {"id": "go", "label": "Go", "type": "fire_task", "task_id": trig2},
    ]}, sid=SID_SHARED).json()["app_id"]
    _approve(app_id2)
    task_store.set_user_agents("alice-sub", [AGENT], "test",
                               agent_roles={AGENT: "viewer"})
    r = client.post(f"/v1/apps/{app_id2}/actions/go")
    assert r.status_code == 409 and "stale" in r.json()["detail"].lower()


def test_reorder_authority(agent_tree):
    s1 = _pin({"slug": "s1", "html": "<p>1</p>"}, sid=SID_SHARED).json()["app_id"]
    s2 = _pin({"slug": "s2", "html": "<p>2</p>"}, sid=SID_SHARED).json()["app_id"]
    m = _pin({"slug": "mine", "html": "<p>m</p>"}).json()["app_id"]

    # Owner/manager reorders everything.
    r = client.put("/v1/apps/order", json={"agent": AGENT, "ids": [s2, s1, m]})
    assert r.status_code == 200
    assert [a["slug"] for a in client.get(f"/v1/apps?agent={AGENT}").json()["apps"]] == \
        ["s2", "s1", "mine"]

    # A viewer may not move the shared rows…
    task_store.upsert_user("bob-sub", "bob@test.com", "Bob", "member")
    _set_username("bob-sub", "bob")
    _as(_user(sub="bob-sub", agent_roles={AGENT: "viewer"}))
    r = client.put("/v1/apps/order", json={"agent": AGENT, "ids": [s1, s2]})
    assert r.status_code == 403
    # …but reordering their own personal rows (shared subsequence unchanged) works.
    b1 = task_store.upsert_app(AGENT, "bob", "bob-sub", "b1", title="1",
                               rel_path="users/bob/workspace/apps/b1.html")["id"]
    b2 = task_store.upsert_app(AGENT, "bob", "bob-sub", "b2", title="2",
                               rel_path="users/bob/workspace/apps/b2.html")["id"]
    r = client.put("/v1/apps/order", json={"agent": AGENT, "ids": [s2, s1, b2, b1]})
    assert r.status_code == 200

    # Stale/incomplete id list → 409.
    r = client.put("/v1/apps/order", json={"agent": AGENT, "ids": [s2, s1, b2]})
    assert r.status_code == 409


def test_unpin_endpoint_authority(agent_tree):
    shared = _pin({"slug": "s", "html": "<p>s</p>"}, sid=SID_SHARED).json()["app_id"]
    _as(_user(sub="bob-sub", agent_roles={AGENT: "viewer"}))
    assert client.delete(f"/v1/apps/{shared}").status_code == 403
    _as(_user(sub="bob-sub", agent_roles={AGENT: "editor"}))
    assert client.delete(f"/v1/apps/{shared}").status_code == 200


def test_dashboard_unpin_is_soft_and_pin_restores(agent_tree):
    """The X only HIDES: manifest + approval survive, every viewer surface
    404s, the agent list flags the slug, and pin_app(slug) alone restores
    the app exactly as approved (the accidental-unpin recovery path)."""
    action = {"id": "go", "label": "Go", "type": "send_prompt", "prompt": "hi"}
    body = _pin({"slug": "brief", "html": "<p>x</p>", "actions": [action]}).json()
    app_id = body["app_id"]
    sig = task_store.actions_sig(task_store.get_app(app_id)["actions"])
    assert task_store.approve_app_actions(app_id, sig, "alice-sub")

    assert client.delete(f"/v1/apps/{app_id}").status_code == 200
    row = task_store.get_app(app_id)
    assert row is not None and row["hidden"] is True
    assert client.get(f"/v1/apps?agent={AGENT}").json()["apps"] == []
    assert client.get(f"/v1/apps/{app_id}/html").status_code == 404
    assert client.post(f"/v1/apps/{app_id}/approve",
                       json={"sig": sig}).status_code == 404
    assert client.delete(f"/v1/apps/{app_id}").status_code == 404
    listed = _hook("list", {}).json()["apps"]
    assert listed[0]["slug"] == "brief" and "unpinned" in listed[0]

    r = _pin({"slug": "brief"})
    assert r.status_code == 200 and "restored" in r.json()
    assert r.json()["actions_approved"] is True
    row = task_store.get_app(app_id)
    assert row["hidden"] is False and task_store.app_actions_approved(row)
    assert [a["id"] for a in client.get(f"/v1/apps?agent={AGENT}").json()["apps"]] \
        == [app_id]


def test_shared_only_human_chat_pins_shared(agent_tree):
    """A Shared-only agent's human chat keeps ctx.username for attribution
    but its MOUNT scope is the agent — pins must land as SHARED rows in the
    shared workspace, never in a per-user dir (that mode has none; found
    live on the trusted VM 2026-07-10)."""
    sid = "sess-app-shared-only"
    session_state.set_session_security(sid, SecurityContext(
        role="manager", username="alice", agent=AGENT, is_admin_agent=False,
        session_scope="agent"))
    try:
        r = _pin({"slug": "ops", "html": "<p>x</p>"}, sid=sid)
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == "shared"
        assert body["path"] == "/workspace/apps/ops.html"
        assert (agent_tree / "workspace/apps/ops.html").exists()
        assert not (agent_tree / "users/alice/workspace/apps/ops.html").exists()
        row = task_store.get_app(body["app_id"])
        assert row["username"] == "" and row["owner_sub"] is None
    finally:
        session_state._session_security.pop(sid, None)


def test_pin_reuses_existing_file_after_hard_unpin(agent_tree):
    """After the agent's own unpin_app (hard delete) the workspace file
    survives at the fixed path — a slug-only re-pin registers over it
    instead of demanding the html again."""
    _pin({"slug": "brief", "html": "<p>keep me</p>"})
    assert _hook("unpin", {"slug": "brief"}).status_code == 200
    r = _pin({"slug": "brief"})
    assert r.status_code == 200 and "reused_file" in r.json()
    saved = agent_tree / "users/alice/workspace/apps/brief.html"
    assert saved.read_text() == "<p>keep me</p>"
    row = task_store.get_app(r.json()["app_id"])
    assert row is not None and row["hidden"] is False


def test_soft_unpin_frees_a_cap_slot_and_bounds_hidden_rows(agent_tree, monkeypatch):
    """The pin cap counts VISIBLE rows (its "unpin one first" advice must
    free a slot now that the X hides) and the parked hidden set has its own
    bound — oldest pruned on each hide."""
    monkeypatch.setattr(task_store, "MAX_APPS_PER_SCOPE", 2)
    monkeypatch.setattr("storage.db_apps.MAX_APPS_PER_SCOPE", 2)
    a = _pin({"slug": "a", "html": "<p>a</p>"}).json()["app_id"]
    b = _pin({"slug": "b", "html": "<p>b</p>"}).json()["app_id"]
    r = _pin({"slug": "c", "html": "<p>c</p>"})
    assert r.status_code == 400 and "app limit" in r.json()["detail"]
    assert client.delete(f"/v1/apps/{a}").status_code == 200
    c = _pin({"slug": "c", "html": "<p>c</p>"}).json()["app_id"]
    assert client.delete(f"/v1/apps/{b}").status_code == 200
    assert client.delete(f"/v1/apps/{c}").status_code == 200
    hidden = [x["slug"] for x in
              task_store.list_apps(AGENT, "alice", include_hidden=True)
              if x["hidden"]]
    assert sorted(hidden) == ["b", "c"]  # "a" (oldest hidden) was pruned


# ───────────────────────── mcp_tool: manifest + schema ──────────────────────


@pytest.fixture
def fake_mcps(monkeypatch):
    """Pretend 'test-mcp' is assigned+local for every agent (the registry has
    no manifests in the test env). The manifest name 'test-mcp-manifest' maps
    to the same canonical server key, mirroring assigned_mcp_keys."""
    monkeypatch.setattr(
        "api.apps.manifest.assigned_mcp_keys",
        lambda agent: {"test-mcp": "test-mcp", "test-mcp-manifest": "test-mcp"},
    )


def _mcp_action(**over) -> dict:
    a = {"id": "run", "label": "Run", "type": "mcp_tool",
         "mcp": "test-mcp", "tool": "echo", "fixed_args": {"flag": True},
         "args_schema": {"type": "object", "properties": {
             "text": {"type": "string", "maxLength": 100}}}}
    a.update(over)
    return a


def test_pin_mcp_tool_matrix(agent_tree, fake_mcps):
    def pin_with(action):
        return _pin({"slug": "m", "html": "<p>x</p>", "actions": [action]})

    ok = pin_with(_mcp_action())
    assert ok.status_code == 200 and ok.json()["approval"] == "pending user approval"

    r = pin_with(_mcp_action(mcp="other-mcp"))
    assert r.status_code == 400 and "not available" in r.json()["detail"]
    # The manifest NAME normalizes to the canonical server key (the tool
    # namespace segment) — exec builds mcp__<key>__<tool> from the stored value.
    ok = pin_with(_mcp_action(mcp="test-mcp-manifest"))
    assert ok.status_code == 200
    stored = _mf.parse_actions(task_store.get_app(ok.json()["app_id"]))[0]
    assert stored["mcp"] == "test-mcp"
    r = pin_with(_mcp_action(tool="bad tool!"))
    assert r.status_code == 400 and "tool name" in r.json()["detail"]
    r = pin_with(_mcp_action(fixed_args={"text": "shadowed"}))
    assert r.status_code == 400 and "overlap" in r.json()["detail"]
    r = pin_with(_mcp_action(fixed_args=[1]))
    assert r.status_code == 400 and "fixed_args" in r.json()["detail"]


def test_args_schema_bounds(agent_tree, fake_mcps):
    def pin_schema(s, fixed=None):
        return _pin({"slug": "m2", "html": "<p>x</p>", "actions": [
            _mcp_action(args_schema=s, fixed_args=fixed or {})]})

    def schema(props, **root):
        s = {"type": "object", "properties": props}
        s.update(root)
        return s

    # Strings must be length-bounded (maxLength or enum).
    assert pin_schema(schema({"t": {"type": "string"}})).status_code == 400
    assert pin_schema(schema({"t": {"type": "string", "enum": ["a", "b"]}})).status_code == 200
    # No escape hatches: unknown root keys, nested types, bad names, dangling required.
    assert pin_schema({**schema({"t": {"type": "string", "maxLength": 5}}),
                       "$defs": {}}).status_code == 400
    assert pin_schema(schema({"t": {"type": "object"}})).status_code == 400
    assert pin_schema(schema({"t": {"type": "array"}})).status_code == 400
    assert pin_schema(schema({"bad name": {"type": "boolean"}})).status_code == 400
    assert pin_schema(schema({"t": {"type": "boolean"}}, required=["x"])).status_code == 400
    assert pin_schema(schema({"t": {"type": "string", "maxLength": 99999}})).status_code == 400
    assert pin_schema(schema({"t": {"type": "boolean", "maximum": 4}})).status_code == 400
    # additionalProperties is FORCED false in the stored manifest.
    ok = pin_schema(schema({"t": {"type": "string", "maxLength": 5}},
                           additionalProperties=True))
    assert ok.status_code == 200
    stored = _mf.parse_actions(task_store.get_app(ok.json()["app_id"]))[0]
    assert stored["args_schema"]["additionalProperties"] is False


def test_validate_args_matrix():
    schema, err = _mf.validate_args_schema({"type": "object", "properties": {
        "text": {"type": "string", "maxLength": 5},
        "n": {"type": "integer", "minimum": 0, "maximum": 10},
        "pick": {"type": "string", "enum": ["a", "b"]},
        "flag": {"type": "boolean"},
    }, "required": ["text"]})
    assert err == ""

    def check(args):
        return _mf.validate_args(schema, args)

    assert check({"text": "hi"}) == ({"text": "hi"}, "")
    assert "missing required" in check({})[1]
    assert "missing required" in check(None)[1]
    assert "unknown args" in check({"text": "x", "zzz": 1})[1]
    assert "too long" in check({"text": "toolong"})[1]
    # bool is NOT an integer (Python isinstance quirk must not leak through).
    assert "must be an integer" in check({"text": "x", "n": True})[1]
    assert "above maximum" in check({"text": "x", "n": 11})[1]
    assert "not in enum" in check({"text": "x", "pick": "c"})[1]
    assert "must be a boolean" in check({"text": "x", "flag": 1})[1]
    assert "must be an object" in check([1])[1]


def test_mcp_tool_exec_endpoint(agent_tree, fake_mcps, monkeypatch):
    app_id = _pin({"slug": "m", "html": "<p>x</p>",
                   "actions": [_mcp_action()]}).json()["app_id"]
    calls = []

    async def fake_exec(row, action, merged):
        calls.append(merged)
        return {"status": "done", "result": "out"}

    monkeypatch.setattr("services.apps.headless_exec.execute_app_tool", fake_exec)

    # Unapproved → 409, nothing runs.
    r = client.post(f"/v1/apps/{app_id}/actions/run", json={"args": {"text": "hi"}})
    assert r.status_code == 409 and calls == []
    _approve(app_id)

    # Schema violations → 400 before any execution.
    assert client.post(f"/v1/apps/{app_id}/actions/run",
                       json={"args": {"text": "x" * 101}}).status_code == 400
    assert client.post(f"/v1/apps/{app_id}/actions/run",
                       json={"args": {"zzz": 1}}).status_code == 400
    assert calls == []

    # Success: validated args merged UNDER the declared fixed_args.
    r = client.post(f"/v1/apps/{app_id}/actions/run", json={"args": {"text": "hi"}})
    assert r.status_code == 200 and r.json() == {"status": "done", "result": "out"}
    assert calls == [{"text": "hi", "flag": True}]

    # Immediate IDENTICAL repeat → 429 (a toggle double-press must not
    # fire twice)…
    assert client.post(f"/v1/apps/{app_id}/actions/run",
                       json={"args": {"text": "hi"}}).status_code == 429
    # …but the SAME declared action with DIFFERENT args is an independent
    # widget (one parameterized action serving a whole control panel).
    r = client.post(f"/v1/apps/{app_id}/actions/run", json={"args": {"text": "b"}})
    assert r.status_code == 200
    assert calls[-1] == {"text": "b", "flag": True}


def test_mcp_tool_exec_recheck_and_list_flags(agent_tree, fake_mcps, monkeypatch):
    app_id = _pin({"slug": "s", "html": "<p>x</p>",
                   "actions": [_mcp_action(fixed_args={})]},
                  sid=SID_SHARED).json()["app_id"]
    _approve(app_id)

    async def boom(row, action, merged):  # reaching the executor would be the bug
        raise AssertionError("must not run")

    monkeypatch.setattr("services.apps.headless_exec.execute_app_tool", boom)

    # MCP unassigned since approval → 409 at click time; the list flags it.
    monkeypatch.setattr("api.apps.manifest.assigned_mcp_keys", lambda agent: {})
    r = client.post(f"/v1/apps/{app_id}/actions/run", json={"args": {"text": "x"}})
    assert r.status_code == 409 and "no longer available" in r.json()["detail"]
    listed = client.get(f"/v1/apps?agent={AGENT}").json()["apps"]
    row = next(a for a in listed if a["id"] == app_id)
    assert row["actions"][0]["mcp_available"] is False
    assert row["can_approve"] is False

    # Approver demoted to viewer → approval stale (mcp_tool runs on the
    # approver's standing surface authority).
    monkeypatch.setattr("api.apps.manifest.assigned_mcp_keys",
                        lambda agent: {"test-mcp": "test-mcp"})
    task_store.set_user_agents("alice-sub", [AGENT], "test",
                               agent_roles={AGENT: "viewer"})
    r = client.post(f"/v1/apps/{app_id}/actions/run", json={"args": {"text": "x"}})
    assert r.status_code == 409 and "stale" in r.json()["detail"].lower()
    listed = client.get(f"/v1/apps?agent={AGENT}").json()["apps"]
    row = next(a for a in listed if a["id"] == app_id)
    assert row["approval_stale"] is True and row["actions_approved"] is False


def test_fire_task_parameterized(agent_tree, monkeypatch):
    trig = _mk_task("trigger", scope="agent")
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE dynamic_tasks SET prompt=%s WHERE id=%s",
                     ("Analyze the {{month}} report", trig))
        conn.commit()
    app_id = _pin({"slug": "p", "html": "<p>x</p>", "actions": [
        {"id": "go", "label": "Go", "type": "fire_task", "task_id": trig,
         "args_schema": {"type": "object",
                         "properties": {"month": {"type": "string", "maxLength": 20}},
                         "required": ["month"]}},
        {"id": "plain", "label": "Plain", "type": "fire_task", "task_id": trig},
    ]}, sid=SID_SHARED).json()["app_id"]
    _approve(app_id)

    fired = []

    async def fake_trigger(task_def, trigger_type="manual", trigger_source=None,
                           prompt_override=None, trigger_payload=None):
        fired.append((trigger_source, prompt_override))
        return "run-1"

    from services.scheduler import scheduler
    monkeypatch.setattr(scheduler, "trigger_task_now", fake_trigger)

    # Schema-validated args substitute into the task prompt.
    r = client.post(f"/v1/apps/{app_id}/actions/go", json={"args": {"month": "July"}})
    assert r.status_code == 200
    assert fired == [("p:go", "Analyze the July report")]

    # Missing required / bad args → 400, nothing fires.
    assert client.post(f"/v1/apps/{app_id}/actions/go", json={"args": {}}).status_code == 400
    # A SCHEMA-LESS fire_task refuses args outright (verbatim contract).
    r = client.post(f"/v1/apps/{app_id}/actions/plain", json={"args": {"month": "July"}})
    assert r.status_code == 400 and "no arguments" in r.json()["detail"]
    # …and still fires verbatim without them.
    r = client.post(f"/v1/apps/{app_id}/actions/plain", json={})
    assert r.status_code == 200
    assert fired[-1] == ("p:plain", None)


# ───────────────────────── data_feed: manifest + runtime ────────────────────


def test_data_feed_manifest_runtime_and_no_exec(agent_tree):
    """Feeds are DECLARED (approval covers them), allowlisted, served with
    the otodock.feed bridge — and never execute over REST (the host page
    answers subscriptions from the viewer's own context)."""
    ok = _pin({"slug": "live", "html": "<p>x</p>", "actions": [
        {"id": "lanes", "label": "Live lanes", "type": "data_feed",
         "feed": "project_lanes"},
        {"id": "act", "label": "Active", "type": "data_feed",
         "feed": "active_chats"},
    ]})
    assert ok.status_code == 200
    assert ok.json()["approval"] == "pending user approval"

    r = _pin({"slug": "bad", "html": "<p>x</p>", "actions": [
        {"id": "x", "label": "X", "type": "data_feed", "feed": "secrets"},
    ]})
    assert r.status_code == 400 and "unknown feed" in r.json()["detail"]

    app_id = ok.json()["app_id"]
    html = client.get(f"/v1/apps/{app_id}/html")
    assert "window.otodock.feed = function" in html.text
    assert "feed_update" in html.text

    _approve(app_id)
    r = client.post(f"/v1/apps/{app_id}/actions/lanes")
    assert r.status_code == 400 and "host page" in r.json()["detail"]


# ───────────────────────── registry cleanup ─────────────────────────────────


def test_delete_agent_removes_app_rows(agent_tree):
    from storage import agent_store
    _pin({"slug": "s", "html": "<p>s</p>"}, sid=SID_SHARED)
    _pin({"slug": "m", "html": "<p>m</p>"})
    agent_store.create_agent(AGENT, AGENT)
    assert agent_store.delete_agent(AGENT)
    assert task_store.list_apps(AGENT, "alice") == []


def test_user_delete_cascades_personal_rows(agent_tree):
    personal = _pin({"slug": "m", "html": "<p>m</p>"}).json()["app_id"]
    shared = _pin({"slug": "s", "html": "<p>s</p>"}, sid=SID_SHARED).json()["app_id"]
    task_store.delete_user("alice-sub")
    assert task_store.get_app(personal) is None      # FK CASCADE
    assert task_store.get_app(shared) is not None    # shared rows survive
