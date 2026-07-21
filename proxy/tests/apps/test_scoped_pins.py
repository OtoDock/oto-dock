"""Chat/project-scoped mini-app pins (the Dock) — scope resolution, the
replace-on-pin contract, and the ``/v1/chats/{chat_id}/pins`` surface.

Load-bearing assertions: scope ids resolve from the pinning SESSION's chat
only (no caller input to forge); a scoped pin never appears on any standing
surface (list, cap); one pin per scope with REPLACE semantics — approval
carries iff the canonical manifest is byte-identical; the pins route stacks
the chat's own access rule (``can_access_chat``) with the app's serve rule
(``app_access``), so a personal-scope pin stays invisible to other viewers;
scoped rows die with their scope (chat delete / last project chat delete).
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from core.session import session_state
from storage import database as task_store

client = TestClient(app)

SID = "sess-dock-1"
SID_SHARED = "sess-dock-shared"
AGENT = "dock-agent"


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


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    (agents_root / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    task_store.upsert_user("alice-sub", "alice@test.com", "Alice", "member")
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s",
                     ("alice", "alice-sub"))
        conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    session_state.set_session_security(SID, SecurityContext(
        role="manager", username="alice", agent=AGENT, is_admin_agent=False))
    session_state.set_session_security(SID_SHARED, SecurityContext(
        role="manager", username="", agent=AGENT, is_admin_agent=False))
    yield agents_root / AGENT
    for sid in (SID, SID_SHARED):
        session_state._session_security.pop(sid, None)


def _mk_chat(sid: str | None = None, owner: str = "alice-sub",
             project_id: str = "", agent: str = AGENT) -> str:
    """A chat row; ``sid`` binds it as the session's chat (the reverse lookup
    the pin hook resolves scope ids through)."""
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, owner, agent, project_id=project_id)
    if sid:
        task_store.update_chat(chat_id, session_id=sid)
    return chat_id


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


# ───────────────────────── scope resolution ─────────────────────────────────


def test_chat_scope_pin_resolves_from_session(agent_tree):
    chat_id = _mk_chat(sid=SID)
    r = _pin({"slug": "progress", "html": "<p>x</p>", "scope": "chat"})
    assert r.status_code == 200
    body = r.json()
    assert body["pin_scope"] == "chat"
    row = task_store.get_app(body["app_id"])
    assert row["scope_chat_id"] == chat_id and row["scope_project_id"] is None
    # Never on the standing surfaces: dashboard list, agent list hook's
    # standing group (it appends the scoped row flagged with its scope).
    assert client.get(f"/v1/apps?agent={AGENT}").json()["apps"] == []
    listed = _hook("list", {}).json()["apps"]
    assert [(a["slug"], a["pin_scope"]) for a in listed] == [("progress", "chat")]


def test_project_scope_pin_and_error_cases(agent_tree):
    # A session chat WITHOUT a project refuses scope="project" cleanly.
    _mk_chat(sid=SID)
    r = _pin({"slug": "board", "html": "<p>x</p>", "scope": "project"})
    assert r.status_code == 400 and "delegation project" in r.json()["detail"]

    # Re-bind the session to a project lane → the id comes from ITS chat row.
    chat_id = _mk_chat(sid=SID, project_id="proj-1")
    r = _pin({"slug": "board", "html": "<p>x</p>", "scope": "project"})
    assert r.status_code == 200 and r.json()["pin_scope"] == "project"
    row = task_store.get_app(r.json()["app_id"])
    assert row["scope_project_id"] == "proj-1" and row["scope_chat_id"] is None
    assert task_store.get_chat(chat_id)["project_id"] == "proj-1"

    # No chat bound to the session at all → clean 400, not a 500.
    r = _pin({"slug": "b2", "html": "<p>x</p>", "scope": "chat"},
             sid=SID_SHARED)
    assert r.status_code == 400 and "chat-bound" in r.json()["detail"]

    # Unknown scope value.
    r = _pin({"slug": "b3", "html": "<p>x</p>", "scope": "nope"})
    assert r.status_code == 400 and "scope" in r.json()["detail"]


# ───────────────────────── replace-on-pin ───────────────────────────────────


def test_replace_carries_approval_iff_manifest_unchanged(agent_tree):
    _mk_chat(sid=SID)
    action = {"id": "go", "label": "Go", "type": "send_prompt", "prompt": "hi"}
    first = _pin({"slug": "v1", "html": "<p>1</p>", "scope": "chat",
                  "actions": [action]}).json()
    row = task_store.get_app(first["app_id"])
    assert task_store.approve_app_actions(
        row["id"], task_store.actions_sig(row["actions"]), "alice-sub")

    # Same scope, NEW slug, SAME manifest → the old row is replaced, the
    # approval carries (scope is the identity; slug is cosmetic).
    second = _pin({"slug": "v2", "html": "<p>2</p>", "scope": "chat",
                   "actions": [action]})
    assert second.status_code == 200
    body = second.json()
    assert "replaced" in body and "v1" in body["replaced"]
    assert body["actions_approved"] is True
    assert task_store.get_app(first["app_id"]) is None  # one pin per scope
    new_row = task_store.get_app(body["app_id"])
    assert new_row["slug"] == "v2" and task_store.app_actions_approved(new_row)

    # A CHANGED manifest resets the approval on replace.
    third = _pin({"slug": "v3", "html": "<p>3</p>", "scope": "chat",
                  "actions": [{**action, "label": "Go now"}]})
    assert third.status_code == 200
    assert third.json()["actions_approved"] is False
    assert task_store.get_app(body["app_id"]) is None


def test_same_slug_repin_updates_in_place(agent_tree):
    _mk_chat(sid=SID)
    first = _pin({"slug": "dash", "html": "<p>1</p>", "scope": "chat"}).json()
    again = _pin({"slug": "dash", "html": "<p>2</p>", "scope": "chat"}).json()
    assert again["app_id"] == first["app_id"] and "replaced" not in again
    saved = (config.AGENTS_DIR / AGENT / "users/alice/workspace/apps/dash.html")
    assert saved.read_text() == "<p>2</p>"


def test_scoped_pins_exempt_from_standing_cap(agent_tree, monkeypatch):
    monkeypatch.setattr(task_store, "MAX_APPS_PER_SCOPE", 2)
    monkeypatch.setattr("storage.db_apps.MAX_APPS_PER_SCOPE", 2)
    _mk_chat(sid=SID)
    assert _pin({"slug": "a1", "html": "<p>1</p>"}).status_code == 200
    assert _pin({"slug": "a2", "html": "<p>2</p>"}).status_code == 200
    assert _pin({"slug": "a3", "html": "<p>3</p>"}).status_code == 400
    # The scoped pin still lands, and doesn't eat a standing slot either.
    assert _pin({"slug": "dock", "html": "<p>d</p>", "scope": "chat"}).status_code == 200
    assert _pin({"slug": "a3", "html": "<p>3</p>"}).status_code == 400
    assert task_store.count_apps(AGENT, "alice") == 2


def test_slug_collision_across_scopes_refused(agent_tree):
    _mk_chat(sid=SID)
    assert _pin({"slug": "taken", "html": "<p>s</p>"}).status_code == 200
    r = _pin({"slug": "taken", "html": "<p>c</p>", "scope": "chat"})
    assert r.status_code == 400 and "standing" in r.json()["detail"]

    assert _pin({"slug": "dock", "html": "<p>c</p>", "scope": "chat"}).status_code == 200
    r = _pin({"slug": "dock", "html": "<p>s</p>"})
    assert r.status_code == 400 and "chat-scoped" in r.json()["detail"]

    # Same slug for ANOTHER chat's pin is also a collision (scoped upsert
    # must never silently move a dashboard between chats).
    _mk_chat(sid=SID)  # newer chat row wins the session reverse-lookup
    r = _pin({"slug": "dock", "html": "<p>c</p>", "scope": "chat"})
    assert r.status_code == 400 and "chat-scoped" in r.json()["detail"]


# ───────────────────────── GET /v1/chats/{id}/pins ──────────────────────────


def test_pins_route_shapes_and_access(agent_tree):
    # Shared-owner project chat (Shared-only style) so other assigned users
    # may open it; alice's personal chat-scoped pin + a SHARED project pin.
    chat_id = _mk_chat(sid=SID, owner=f"agent::{AGENT}", project_id="proj-9")
    assert _pin({"slug": "mine", "html": "<p>c</p>", "scope": "chat"}).status_code == 200
    task_store.update_chat(chat_id, session_id=SID_SHARED)
    assert _pin({"slug": "board", "html": "<p>p</p>", "scope": "project"},
                sid=SID_SHARED).status_code == 200

    body = client.get(f"/v1/chats/{chat_id}/pins").json()
    assert body["chat"]["slug"] == "mine" and body["chat"]["pin_scope"] == "chat"
    assert body["chat"]["scope"] == "personal" and body["chat"]["agent"] == AGENT
    assert body["project"]["slug"] == "board"
    assert body["project"]["pin_scope"] == "project"
    # Shaped like /v1/apps rows — AppFrame + approval card reuse.
    for key in ("id", "actions", "actions_sig", "actions_approved",
                "can_approve", "can_manage"):
        assert key in body["project"]

    # bob (assigned viewer): the shared project pin serves, alice's personal
    # chat pin does not (app_access) — the documented v1 limit.
    task_store.upsert_user("bob-sub", "bob@test.com", "Bob", "member")
    _as(_user(sub="bob-sub", agent_roles={AGENT: "viewer"}))
    body = client.get(f"/v1/chats/{chat_id}/pins").json()
    assert body["chat"] is None and body["project"]["slug"] == "board"

    # eve (not assigned) can't even anchor on the chat.
    _as(_user(sub="eve-sub", agents=(), agent_roles={}))
    assert client.get(f"/v1/chats/{chat_id}/pins").status_code == 403
    assert client.get(f"/v1/chats/{uuid.uuid4()}/pins").status_code == 404


def test_pins_route_hides_soft_unpinned(agent_tree):
    chat_id = _mk_chat(sid=SID)
    app_id = _pin({"slug": "dash", "html": "<p>x</p>", "scope": "chat"}).json()["app_id"]
    assert client.get(f"/v1/chats/{chat_id}/pins").json()["chat"]["id"] == app_id
    task_store.set_app_hidden(app_id, True)
    assert client.get(f"/v1/chats/{chat_id}/pins").json()["chat"] is None
    # An agent re-pin revives it (same restore contract as standing apps).
    assert _pin({"slug": "dash", "scope": "chat"}).status_code == 200
    assert client.get(f"/v1/chats/{chat_id}/pins").json()["chat"]["id"] == app_id


def test_non_project_chat_has_null_project_pin(agent_tree):
    chat_id = _mk_chat(sid=SID)
    body = client.get(f"/v1/chats/{chat_id}/pins").json()
    assert body == {"chat": None, "project": None, "files": []}


# ───────────────────────── lifecycle ────────────────────────────────────────


def test_scoped_pins_die_with_their_scope(agent_tree):
    # Chat pin dies with its chat; the file stays (user artifact).
    chat_id = _mk_chat(sid=SID)
    chat_pin = _pin({"slug": "c", "html": "<p>c</p>", "scope": "chat"}).json()["app_id"]

    # Project pin survives until the LAST project chat is deleted.
    lane_a = _mk_chat(sid=SID, project_id="proj-2")
    proj_pin = _pin({"slug": "p", "html": "<p>p</p>", "scope": "project"}).json()["app_id"]
    lane_b = _mk_chat(project_id="proj-2")

    task_store.delete_chat(chat_id)
    assert task_store.get_app(chat_pin) is None
    assert (agent_tree / "users/alice/workspace/apps/c.html").exists()

    task_store.delete_chat(lane_a)
    assert task_store.get_app(proj_pin) is not None  # lane_b still holds it
    task_store.delete_chat(lane_b)
    assert task_store.get_app(proj_pin) is None


def test_agent_unpin_hook_works_on_scoped(agent_tree):
    _mk_chat(sid=SID)
    app_id = _pin({"slug": "dash", "html": "<p>x</p>", "scope": "chat"}).json()["app_id"]
    assert _hook("unpin", {"slug": "dash"}).status_code == 200
    assert task_store.get_app(app_id) is None
