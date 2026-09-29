"""The lifecycle of folder apps (APPS.md "Lifecycle", "Quota").

Load-bearing: a purge removes the row, the releases, the database and the
workspace folder (with tombstones, so no satellite brings it back) and
needs the slug typed by a person who may manage the row; a hard unpin
keeps the folder and the data and says where they are; every delete site
stops the server first; the quota preflight refuses a deploy that would
not fit on the hard tier and stays silent on the soft one.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from core.session import session_state
from services.apps import app_deploy, app_lifecycle, app_supervisor, releases
from storage import database as task_store

client = TestClient(app)

AGENT = "life-agent"
SID = "sess-life-alice"


def _user(sub: str = "alice-sub", agent_roles: dict[str, str] | None = None,
          is_api_key: bool = False) -> UserContext:
    roles = {AGENT: "manager"} if agent_roles is None else agent_roles
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                       agents=list(roles.keys()), is_api_key=is_api_key, agent_roles=roles)


ALICE = _user()


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _clean():
    from api.apps import apps as apps_api
    _as(ALICE)
    apps_api._fire_rate.clear()
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    (agents_root / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    from storage.pg import get_conn
    task_store.upsert_user("alice-sub", "alice@test.com", "Alice", "member")
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", ("alice", "alice-sub"))
        conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
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


def _deployed(agent_tree, slug: str = "board") -> dict:
    root = agent_tree / "users/alice/workspace/apps" / slug
    (root / "client").mkdir(parents=True)
    (root / "app.json").write_text(json.dumps({"title": slug.title()}))
    (root / "client" / "index.html").write_text("<p>v1</p>")
    body = _hook("deploy", {"slug": slug}).json()
    assert body["status"] == "ok", body
    row = task_store.get_app(body["app_id"])
    data = releases.app_data_dir(row)
    data.mkdir(parents=True, exist_ok=True)
    import sqlite3
    con = sqlite3.connect(data / "app.db")
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    return row


def _fake_instance(row: dict) -> app_supervisor.Instance:
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=releases.app_release_dir(row),
                                   data_dir=releases.app_data_dir(row), state="up")
    app_supervisor._instances[(row["id"], "live")] = inst
    return inst


def test_purge_removes_everything_and_needs_the_slug_from_a_human(agent_tree):
    row = _deployed(agent_tree)
    _fake_instance(row)
    folder = agent_tree / row["rel_path"]
    assert folder.is_dir() and releases.app_release_dir(row).is_dir()
    # The confirmation, the authority and the human at the keyboard.
    assert client.post(f"/v1/apps/{row['id']}/purge", json={"confirm": "nope"}).status_code == 400
    _as(_user(is_api_key=True))
    assert client.post(f"/v1/apps/{row['id']}/purge", json={"confirm": "board"}).status_code == 403
    _as(_user("bob-sub", {AGENT: "viewer"}))
    assert client.post(f"/v1/apps/{row['id']}/purge", json={"confirm": "board"}).status_code == 404
    _as(ALICE)
    with patch("services.infra.file_bookkeeping.push_file_delete") as pushed:
        r = client.post(f"/v1/apps/{row['id']}/purge", json={"confirm": "board"})
    assert r.status_code == 200 and r.json()["files_removed"] == 2, r.text
    assert task_store.get_app(row["id"]) is None
    assert not folder.exists() and not releases.app_release_dir(row).exists()
    assert not releases.app_data_dir(row).exists()
    assert app_supervisor.get(row["id"]) is None
    # Every removed file was announced to the satellites; the folder too.
    announced = {c.args[1] for c in pushed.call_args_list}
    assert "users/alice/workspace/apps/board/app.json" in announced
    assert "users/alice/workspace/apps/board" in announced
    assert client.get(f"/v1/apps/{row['id']}").status_code == 404


def test_the_purge_tool_needs_confirm_and_the_unpin_keeps_folder_and_data(agent_tree):
    row = _deployed(agent_tree)
    inst = _fake_instance(row)
    assert _hook("purge", {"slug": "board"}).status_code == 400
    assert _hook("purge", {"slug": "board", "confirm": "other"}).status_code == 400
    # The hard unpin: the row and releases go, the folder and the data stay.
    body = _hook("unpin", {"slug": "board"}).json()
    assert body["kept_file"] == "users/alice/workspace/apps/board"
    assert body["kept_data"] == "app-data/users/alice/board"
    assert app_supervisor.get(row["id"]) is None and inst.state == "stopped"
    assert (agent_tree / row["rel_path"]).is_dir() and (releases.app_data_dir(row) / "app.db").is_file()
    assert not releases.app_release_dir(row).exists()
    # Deploying the slug again finds the same data directory; a database
    # that cannot be copied stops the deploy instead of losing it.
    again = task_store.get_app(_hook("deploy", {"slug": "board"}).json()["app_id"])
    assert releases.app_data_dir(again) == releases.app_data_dir(row)
    (releases.app_data_dir(again) / "app.db").write_bytes(b"not a database")
    r = _hook("deploy", {"slug": "board"})
    assert r.status_code == 400 and "could not be copied" in r.json()["detail"]
    with patch("services.infra.file_bookkeeping.push_file_delete"):
        assert _hook("purge", {"slug": "board", "confirm": "BOARD"}).status_code == 200
    assert not releases.app_data_dir(again).exists()


def test_a_purge_never_follows_a_symlinked_folder_out_of_the_app(agent_tree):
    """The app's folder swapped for a symlink (a session can plant one) must
    not point the purge's per-file delete at another tree of the agent: the
    shared workspace or another user's files."""
    import shutil
    row = _deployed(agent_tree)
    shared = agent_tree / "workspace" / "team.md"
    shared.write_text("the team's notes")
    folder = agent_tree / row["rel_path"]
    shutil.rmtree(folder)
    folder.symlink_to("../../../../workspace", target_is_directory=True)
    assert (folder / "team.md").is_file()
    with patch("services.infra.file_bookkeeping.push_file_delete"):
        r = _hook("purge", {"slug": "board", "confirm": "board"})
    assert r.status_code == 200 and r.json()["files_removed"] == 0, r.text
    assert shared.read_text() == "the team's notes"
    assert task_store.get_app(row["id"]) is None


def test_soft_unpin_and_agent_delete_stop_the_server(agent_tree):
    row = _deployed(agent_tree)
    inst = _fake_instance(row)
    assert client.delete(f"/v1/apps/{row['id']}").status_code == 200
    assert app_supervisor.get(row["id"]) is None and inst.state == "stopped"
    # The agent-wide stop takes shared and personal rows alike.
    shared = task_store.upsert_app(AGENT, "", None, "team", rel_path="workspace/apps/team", kind="folder")
    a, b = _fake_instance(task_store.get_app(row["id"])), _fake_instance(shared)
    assert asyncio.run(app_lifecycle.stop_agent_apps(AGENT)) == 0  # no real processes
    assert app_supervisor.get(row["id"]) is None and app_supervisor.get(shared["id"]) is None
    assert a.state == "stopped" and b.state == "stopped"
    c = _fake_instance(shared)
    asyncio.run(app_lifecycle.stop_user_apps("alice-sub"))
    assert app_supervisor.get(shared["id"]) is c  # not alice's personal row


def test_quota_preflight_refuses_on_the_hard_tier_only(agent_tree, monkeypatch):
    from services.infra import storage_quota as sq
    row = _deployed(agent_tree)
    src = agent_tree / row["rel_path"]
    assert app_deploy.quota_preflight(row, src) == ""
    monkeypatch.setattr(sq, "hard_enabled", lambda: True)
    monkeypatch.setattr(sq, "limits_for", lambda scope: (100, 1000))
    monkeypatch.setattr(sq, "get_project_id", lambda key: 7)
    monkeypatch.setattr(sq, "report_usage", lambda pid: (90, 3))
    why = app_deploy.quota_preflight(row, src)
    assert "would not fit" in why
    r = _hook("deploy", {"slug": "board"})
    assert r.status_code == 400 and "would not fit" in r.json()["detail"]
    # Room to spare (a fresh SQLite file alone is a few pages).
    monkeypatch.setattr(sq, "limits_for", lambda scope: (10 * 1024 * 1024, 1000))
    monkeypatch.setattr(sq, "report_usage", lambda pid: (10, 3))
    assert app_deploy.quota_preflight(row, src) == ""
    monkeypatch.setattr(sq, "limits_for", lambda scope: (0, 0))
    monkeypatch.setattr(sq, "report_usage", lambda pid: (10 ** 9, 3))
    assert app_deploy.quota_preflight(row, src) == ""


def test_a_purge_that_leaves_a_linked_folder_in_place_announces_no_delete(agent_tree):
    """A folder that is a link is neither walked (the walk would follow it
    into another folder's files) nor announced to the satellites as gone."""
    from unittest.mock import AsyncMock
    from services.infra import file_bookkeeping
    other = agent_tree / "workspace" / "elsewhere"
    other.mkdir()
    (other / "keep.txt").write_text("k")
    (agent_tree / "workspace" / "apps").mkdir(exist_ok=True)
    folder = agent_tree / "workspace" / "apps" / "lnk"
    folder.symlink_to(other)
    push = AsyncMock()
    with patch.object(file_bookkeeping, "push_file_delete", push):
        count = asyncio.run(app_lifecycle._remove_workspace_folder(AGENT, agent_tree, folder))
    assert count == 0
    assert (other / "keep.txt").read_text() == "k"
    push.assert_not_called()
