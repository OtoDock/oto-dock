"""Dock file pins — the ``/v1/hooks/files/pin|unpin`` hooks and the
``/v1/chats/{chat_id}/pins`` ``files`` surface.

Load-bearing assertions: scope ids resolve from the pinning SESSION's chat
only (no caller input to forge); the pinned path is confined to the agent
dir (traversal refused), must exist, and must be a renderable text type;
pins are capped per scope with re-pin exempt; unpin removes rows and never
touches files; every pin/unpin broadcasts ``file_updated`` with the ``pin``
marker so open Docks refresh their pins list.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from core.session import session_state
from storage import database as task_store

client = TestClient(app)

SID = "sess-fpin-1"
SID_SHARED = "sess-fpin-shared"
AGENT = "fpin-agent"


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


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    (agents_root / AGENT / "workspace" / "projects" / "p1").mkdir(parents=True)
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
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, owner, agent, project_id=project_id)
    if sid:
        task_store.update_chat(chat_id, session_id=sid)
    return chat_id


def _hook(op: str, payload: dict, sid: str = SID) -> object:
    payload.setdefault("session_id", sid)
    with patch("api.hooks.hooks.verify_session_match"), \
         patch("services.notifications.notification_manager."
               "broadcast_file_updated", new=AsyncMock()):
        return client.post(f"/v1/hooks/files/{op}", json=payload,
                           headers={"Authorization": "Bearer dummy"})


# ───────────────────────── pin: resolution + surface ────────────────────────


def test_pin_resolves_scope_from_session_and_lists(agent_tree):
    chat_id = _mk_chat(sid=SID_SHARED)
    plan = agent_tree / "workspace" / "projects" / "p1" / "plan.md"
    plan.write_text("# The plan\n", "utf-8")
    r = _hook("pin", {"path": "projects/p1/plan.md"}, sid=SID_SHARED)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pin_scope"] == "chat"
    assert body["path"] == "/workspace/projects/p1/plan.md"
    assert body["title"] == "plan.md"  # filename default
    pins = client.get(f"/v1/chats/{chat_id}/pins").json()
    assert [(f["rel_path"], f["pin_scope"], f["agent"]) for f in pins["files"]] \
        == [("workspace/projects/p1/plan.md", "chat", AGENT)]


def test_project_scope_pin_and_no_project_error(agent_tree):
    plan = agent_tree / "workspace" / "projects" / "p1" / "plan.md"
    plan.write_text("# plan", "utf-8")
    # No project on the chat → scope="project" refuses.
    _mk_chat(sid=SID_SHARED)
    r = _hook("pin", {"path": "projects/p1/plan.md", "scope": "project"},
              sid=SID_SHARED)
    assert r.status_code == 400
    assert "project" in r.json()["detail"]
    # A project chat resolves its project id; the pin rides ANY project chat.
    proj_chat = _mk_chat(sid=SID_SHARED, project_id="proj-9")
    r = _hook("pin", {"path": "projects/p1/plan.md", "scope": "project"},
              sid=SID_SHARED)
    assert r.status_code == 200
    sibling = _mk_chat(project_id="proj-9")
    pins = client.get(f"/v1/chats/{sibling}/pins").json()
    assert [(f["rel_path"], f["pin_scope"]) for f in pins["files"]] \
        == [("workspace/projects/p1/plan.md", "project")]
    assert proj_chat  # the anchor chat sees it too (same project query)


def test_personal_scope_resolves_user_workspace(agent_tree):
    _mk_chat(sid=SID)
    notes = agent_tree / "users" / "alice" / "workspace" / "notes.md"
    notes.write_text("hi", "utf-8")
    r = _hook("pin", {"path": "notes.md", "title": "My notes"}, sid=SID)
    assert r.status_code == 200
    assert r.json()["path"] == "/users/alice/workspace/notes.md"
    assert r.json()["title"] == "My notes"


# ───────────────────────── pin: validation ──────────────────────────────────


def test_pin_validation(agent_tree):
    _mk_chat(sid=SID_SHARED)
    # Missing file → 404 (the Dock renders, it can't create).
    r = _hook("pin", {"path": "projects/p1/ghost.md"}, sid=SID_SHARED)
    assert r.status_code == 404
    # Traversal → 403.
    r = _hook("pin", {"path": "../../other-agent/secret.md"}, sid=SID_SHARED)
    assert r.status_code == 403
    # Non-text extension → 400.
    (agent_tree / "workspace" / "img.png").write_bytes(b"\x89PNG")
    r = _hook("pin", {"path": "img.png"}, sid=SID_SHARED)
    assert r.status_code == 400
    assert "text" in r.json()["detail"]
    # No chat-bound session → 400.
    session_state.set_session_security("sess-nochat", SecurityContext(
        role="manager", username="", agent=AGENT, is_admin_agent=False))
    try:
        r = _hook("pin", {"path": "projects/p1/plan.md"}, sid="sess-nochat")
        assert r.status_code == 400
    finally:
        session_state._session_security.pop("sess-nochat", None)


def test_per_scope_cap_with_repin_exempt(agent_tree):
    _mk_chat(sid=SID_SHARED)
    ws = agent_tree / "workspace"
    for i in range(task_store.MAX_FILE_PINS_PER_SCOPE):
        (ws / f"doc{i}.md").write_text("x", "utf-8")
        assert _hook("pin", {"path": f"doc{i}.md"},
                     sid=SID_SHARED).status_code == 200
    (ws / "overflow.md").write_text("x", "utf-8")
    r = _hook("pin", {"path": "overflow.md"}, sid=SID_SHARED)
    assert r.status_code == 400
    assert "limit" in r.json()["detail"]
    # Re-pin of an existing path is an update, not a new slot.
    r = _hook("pin", {"path": "doc0.md", "title": "Renamed"}, sid=SID_SHARED)
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed"


# ───────────────────────── unpin ─────────────────────────────────────────────


def test_unpin_by_path_and_all(agent_tree):
    chat_id = _mk_chat(sid=SID_SHARED)
    ws = agent_tree / "workspace"
    for name in ("a.md", "b.md"):
        (ws / name).write_text("x", "utf-8")
        _hook("pin", {"path": name}, sid=SID_SHARED)
    r = _hook("unpin", {"path": "a.md"}, sid=SID_SHARED)
    assert r.status_code == 200 and r.json()["removed"] == 1
    assert (ws / "a.md").exists()  # the file is never touched
    left = client.get(f"/v1/chats/{chat_id}/pins").json()["files"]
    assert [f["rel_path"] for f in left] == ["workspace/b.md"]
    # Unpin-all clears the scope; a second unpin finds nothing.
    assert _hook("unpin", {}, sid=SID_SHARED).json()["removed"] == 1
    assert _hook("unpin", {}, sid=SID_SHARED).status_code == 404


def test_unpin_resolves_deleted_files(agent_tree):
    """The pinned file may be gone from disk — unpin still matches the row
    (lexical resolution, no existence gate)."""
    _mk_chat(sid=SID_SHARED)
    f = agent_tree / "workspace" / "gone.md"
    f.write_text("x", "utf-8")
    _hook("pin", {"path": "gone.md"}, sid=SID_SHARED)
    f.unlink()
    r = _hook("unpin", {"path": "gone.md"}, sid=SID_SHARED)
    assert r.status_code == 200 and r.json()["removed"] == 1


# ───────────────────────── broadcast marker ─────────────────────────────────


def test_pin_broadcasts_file_updated_with_pin_marker(agent_tree):
    _mk_chat(sid=SID_SHARED)
    (agent_tree / "workspace" / "w.md").write_text("x", "utf-8")
    with patch("api.hooks.hooks.verify_session_match"), \
         patch("services.notifications.notification_manager."
               "broadcast_file_updated", new=AsyncMock()) as bc:
        r = client.post("/v1/hooks/files/pin",
                        json={"session_id": SID_SHARED, "path": "w.md"},
                        headers={"Authorization": "Bearer dummy"})
        assert r.status_code == 200
        bc.assert_awaited_once()
        args, kwargs = bc.await_args
        assert args == (AGENT, "workspace/w.md")
        assert kwargs.get("pin") is True
