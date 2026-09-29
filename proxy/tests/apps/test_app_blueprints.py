"""Blueprints (APPS.md "Blueprints and templates").

Load-bearing: an export packs the working folder with its buttons by slug
and the tasks they point at, never the database; an import seeds those
tasks idempotently for the target scope, re-points the buttons, writes the
folder and lands release 1 PENDING on the card — never pre-approved, even
over an approved slug; a bundle with a raw task id, an unsafe member, a
link, an oversize file or an MCP the agent lacks is refused with the
reason; a template's ``apps/<slug>/`` are discovered with their required
MCPs and seeded the same way.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from core.session import session_state
from services.apps import app_blueprints, app_deploy, app_handlers, app_supervisor
from services.community import template_app_seeder as seeder
from storage import database as task_store
from storage.agents import community_agent_template_store as tpl
from storage.pg import get_conn

client = TestClient(app)

AGENT = "blueprint-agent"
SID = "sess-bp-alice"
SID_SHARED = "sess-bp-shared"


def _user(sub: str = "alice-sub", agent_roles: dict[str, str] | None = None) -> UserContext:
    roles = {AGENT: "manager"} if agent_roles is None else agent_roles
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                       agents=list(roles.keys()), agent_roles=roles)


ALICE = _user()


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from api.apps import apps as apps_api
    _as(ALICE)
    apps_api._fire_rate.clear()
    monkeypatch.setattr(app_handlers, "schedule_drain", lambda app_id: None)
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
    task_store.upsert_user("alice-sub", "alice-sub@test.com", "Alice", "member")
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", ("alice", "alice-sub"))
        conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    session_state.set_session_security(SID, SecurityContext(
        role="manager", username="alice", agent=AGENT, is_admin_agent=False))
    session_state.set_session_security(SID_SHARED, SecurityContext(
        role="manager", username="", agent=AGENT, is_admin_agent=False))
    yield agents_root / AGENT
    for sid in (SID, SID_SHARED):
        session_state._session_security.pop(sid, None)


def _hook(op: str, payload: dict, sid: str = SID_SHARED):
    payload.setdefault("session_id", sid)
    with patch("api.hooks.app_deploy.verify_session_match_async"), \
            patch("api.hooks.pins.verify_session_match_async"):
        return client.post(f"/v1/hooks/apps/{op}", json=payload,
                           headers={"Authorization": "Bearer dummy"})


def _folder(root: Path, manifest: dict) -> Path:
    (root / "client").mkdir(parents=True, exist_ok=True)
    (root / "app.json").write_text(json.dumps(manifest))
    (root / "client" / "index.html").write_text("<p>app</p>")
    (root / "client" / "app.js").write_text("console.log(1)")
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "app.db").write_bytes(b"never travels")
    return root


def _bundle(files: dict[str, bytes], blueprint: dict | None, *, link: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if blueprint is not None:
            data = json.dumps(blueprint).encode()
            info = tarfile.TarInfo("blueprint.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(link)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return buf.getvalue()


MINIMAL = {"app.json": json.dumps({"title": "X"}).encode(), "client/index.html": b"<p>x</p>"}
BP = {"format": 1, "slug": "x", "title": "X", "tasks": [], "requires": {}}


def test_export_and_import_round_trip_lands_pending_with_tasks_by_slug(agent_tree):
    task_store.create_dynamic_task("task-bp-1", AGENT, "Weekly report", "write it", "cli", "trigger",
                                   None, None, None, 600, AGENT, scope="agent")
    manifest = {"title": "Board", "requires": {"mcps": ["github-mcp"]}, "actions": [
        {"id": "report", "label": "Report", "type": "fire_task", "task_id": "task-bp-1"}]}
    _folder(agent_tree / "workspace" / "apps" / "board", manifest)
    body = _hook("deploy", {"slug": "board", "visibility": "agent"}).json()
    assert body["status"] == "pending approval", body
    assert client.post(f"/v1/apps/{body['app_id']}/deploy/approve").status_code == 200
    # The export: the working folder, the button by slug, its task inside,
    # nothing of data/.
    r = _hook("export", {"slug": "board", "visibility": "agent"})
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "/workspace/apps/board.otoapp" and r.json()["tasks"] == ["report"]
    data = (agent_tree / "workspace/apps/board.otoapp").read_bytes()
    folder, bp = app_blueprints.read_bundle(data)
    try:
        assert bp["slug"] == "board" and bp["requires"] == {"mcps": ["github-mcp"]}
        assert bp["tasks"] == [{"slug": "report", "description": "Weekly report", "prompt": "write it"}]
        doc = json.loads((folder / "app.json").read_text())
        assert doc["actions"][0]["task"] == "report" and "task_id" not in doc["actions"][0]
        assert (folder / "client" / "app.js").is_file() and not (folder / "data").exists()
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
    # The import into alice's personal scope under another slug: the task
    # seeded for HER scope in the zone her dashboard reported, the button
    # re-pointed, the folder written, release 1 pending and the row
    # unapproved.
    from core.session import session_state
    session_state.set_user_tz("alice-sub", "Europe/Athens")
    try:
        r = _hook("import", {"path": "workspace/apps/board.otoapp", "visibility": "user", "slug": "myboard"}, sid=SID)
    finally:
        session_state._user_tz.pop("alice-sub", None)
    assert r.status_code == 200, r.text
    out = r.json()
    assert _hook("import", {"path": "users/bob/workspace/apps/x.otoapp", "visibility": "user"},
                 sid=SID).status_code == 403
    assert out["status"] == "pending approval" and out["scope"] == "personal" and out["release"] == 1
    new_task = out["tasks"]["report"]
    dyn = task_store.get_dynamic_task(new_task)
    assert dyn["task_type"] == "trigger" and dyn["scope"] == "user" and dyn["created_by"] == "alice-sub"
    assert dyn["community_template_item_slug"] == "myboard__report" and dyn["prompt"] == "write it"
    assert dyn["user_tz"] == "Europe/Athens"
    row = task_store.get_app(out["app_id"])
    assert row["username"] == "alice" and row["deploy_state"] == "pending"
    assert not task_store.app_actions_approved(row)
    stored = json.loads((agent_tree / "users/alice/workspace/apps/myboard/app.json").read_text())
    assert stored["actions"][0]["task_id"] == new_task and "task" not in stored["actions"][0]
    assert (agent_tree / "users/alice/workspace/apps/myboard/client/app.js").is_file()
    # Approved and live; a second import keeps the task and parks a new
    # release again — an import never pre-approves.
    assert client.post(f"/v1/apps/{row['id']}/deploy/approve").status_code == 200
    assert task_store.app_actions_approved(task_store.get_app(row["id"]))
    r = _hook("import", {"path": "workspace/apps/board.otoapp", "visibility": "user", "slug": "myboard"}, sid=SID)
    assert r.status_code == 200 and r.json()["status"] == "pending approval", r.text
    assert r.json()["tasks"]["report"] == new_task
    assert not task_store.app_actions_approved(task_store.get_app(row["id"]))


def test_export_and_import_never_follow_a_link(agent_tree, tmp_path):
    """The apps TOCTOU sites: the export reads the working folder and writes
    the bundle beneath the agent's tree without following a link (a linked
    ``app.json`` echoes nothing of its target, a link at the old fixed
    temporary name of the bundle is never written through), and an import
    into an ``apps/<slug>`` that is a link writes nothing through it."""
    import asyncio
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "manifest.json").write_text(json.dumps(
        {"title": "T", "actions": [{"id": "LEAKED-ID", "type": "fire_task", "task_id": "t"}]}))
    victim = outside / "victim.bin"
    victim.write_bytes(b"untouched")
    board = _folder(agent_tree / "workspace" / "apps" / "board", {"title": "Board"})
    body = _hook("deploy", {"slug": "board", "visibility": "agent"}).json()
    assert body["status"] == "ok", body
    # The bundle's old temporary name, planted as a link.
    (agent_tree / "workspace" / "apps" / "board.otoapp.tmp").symlink_to(victim)
    r = _hook("export", {"slug": "board", "visibility": "agent"})
    assert r.status_code == 200, r.text
    assert victim.read_bytes() == b"untouched"
    # A linked app.json: refused, nothing of the target in the answer.
    (board / "app.json").unlink()
    (board / "app.json").symlink_to(outside / "manifest.json")
    r = _hook("export", {"slug": "board", "visibility": "agent"})
    assert r.status_code == 400 and "LEAKED-ID" not in r.text, r.text
    # An import whose target folder is a link to another folder in scope.
    other = agent_tree / "workspace" / "other"
    other.mkdir()
    (agent_tree / "workspace" / "apps" / "linked").symlink_to(other)
    src = _folder(tmp_path / "bundle-src", {"title": "Imported"})
    with pytest.raises((app_blueprints.BlueprintError, app_deploy.DeployError)):
        asyncio.run(app_blueprints.import_folder(AGENT, "", None, "linked", src,
                                                 template_slug="bundle:linked", tasks=[]))
    assert list(other.iterdir()) == []


def test_a_bad_bundle_is_refused_with_the_reason(agent_tree):
    for files, bp, link, words in (
        (MINIMAL, None, None, "no blueprint.json"),
        ({"app.json": MINIMAL["app.json"]}, BP, None, "no app.json" if False else "client/index.html"),
        ({**MINIMAL, "../x": b"1"}, BP, None, "unsafe path"),
        ({**MINIMAL, "data/app.db": b"1"}, BP, None, "never travel"),
        ({**MINIMAL, ".env": b"1"}, BP, None, "never travel"),
        ({**MINIMAL, "big.bin": b"0" * (3 * 1024 * 1024)}, BP, None, "larger than 2 MB"),
        (MINIMAL, BP, "client/link", "not a regular file"),
        (MINIMAL, {**BP, "format": 2}, None, "format 1"),
    ):
        data = _bundle(files, bp, link=link)
        (agent_tree / "workspace" / "apps").mkdir(parents=True, exist_ok=True)
        (agent_tree / "workspace" / "apps" / "bad.otoapp").write_bytes(data)
        r = _hook("import", {"path": "apps/bad.otoapp", "visibility": "agent"})
        assert r.status_code == 400 and words in r.text, (words, r.text)
    # A raw task id, an unknown bundle task and an MCP the agent lacks.
    raw = {**MINIMAL, "app.json": json.dumps({"title": "X", "actions": [
        {"id": "go", "label": "Go", "type": "fire_task", "task_id": "task-x"}]}).encode()}
    (agent_tree / "workspace/apps/bad.otoapp").write_bytes(_bundle(raw, BP))
    r = _hook("import", {"path": "apps/bad.otoapp", "visibility": "agent"})
    assert r.status_code == 400 and "travel by slug" in r.text
    unknown = {**MINIMAL, "app.json": json.dumps({"title": "X", "actions": [
        {"id": "go", "label": "Go", "type": "fire_task", "task": "nope"}]}).encode()}
    (agent_tree / "workspace/apps/bad.otoapp").write_bytes(_bundle(unknown, BP))
    r = _hook("import", {"path": "apps/bad.otoapp", "visibility": "agent"})
    assert r.status_code == 400 and "no task 'nope'" in r.text
    ghost = {**MINIMAL, "app.json": json.dumps({"title": "X", "actions": [
        {"id": "t", "label": "T", "type": "mcp_tool", "mcp": "ghost-mcp", "tool": "do"}]}).encode()}
    (agent_tree / "workspace/apps/bad.otoapp").write_bytes(_bundle(ghost, BP))
    r = _hook("import", {"path": "apps/bad.otoapp", "visibility": "agent"})
    assert r.status_code == 400 and "ghost-mcp" in r.text
    assert task_store.get_app_by_slug(AGENT, "", "x") is None
    # The path must be a workspace .otoapp; a viewer's session may not
    # import into the shared scope.
    assert _hook("import", {"path": "apps/bad.txt", "visibility": "agent"}).status_code == 400
    assert _hook("import", {"path": "apps/none.otoapp", "visibility": "agent"}).status_code == 404
    session_state.set_session_security("sess-bp-viewer", SecurityContext(
        role="viewer", username="bob", agent=AGENT, is_admin_agent=False, session_scope="agent"))
    try:
        (agent_tree / "workspace/apps/bad.otoapp").write_bytes(_bundle(MINIMAL, BP))
        assert _hook("import", {"path": "apps/bad.otoapp", "visibility": "agent"},
                     sid="sess-bp-viewer").status_code == 403
    finally:
        session_state._session_security.pop("sess-bp-viewer", None)


def test_a_bundle_of_countless_empty_entries_is_refused_before_it_is_indexed():
    # The member cap applies while reading: the whole index up front cost
    # gigabytes for a small archive of empty entries.
    import time
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for i in range(60_000):
            info = tarfile.TarInfo(f"d{i}")
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
    started = time.monotonic()
    with pytest.raises(app_blueprints.BlueprintError, match="500 files"):
        app_blueprints.read_bundle(buf.getvalue())
    assert time.monotonic() - started < 2


def test_a_template_ships_apps_that_land_pending_with_their_needs(agent_tree, tmp_path):
    tdir = tmp_path / "template"
    _folder(tdir / "apps" / "board", {"title": "Board", "requires": {"mcps": ["github-mcp"]},
                                       "actions": [{"id": "go", "label": "Go", "type": "fire_task",
                                                    "task": "report"}]})
    (tdir / "apps" / "board" / "blueprint.json").write_text(json.dumps(
        {"format": 1, "slug": "board", "tasks": [{"slug": "report", "description": "R", "prompt": "do it"}]}))
    (tdir / "apps" / "notes").mkdir(parents=True)          # no app.json: not an app
    items = tpl._discover_apps(tdir)
    assert [i.slug for i in items] == ["board"] and items[0].requires_mcps == ["github-mcp"]
    with pytest.raises(tpl.TemplateValidationError):
        (tdir / "apps" / "Bad Name").mkdir()
        (tdir / "apps" / "Bad Name" / "app.json").write_text("{}")
        tpl._discover_apps(tdir)
    template = SimpleNamespace(slug="ops-team", apps=items)
    # Without the installer's consent the shared app lands pending, cold.
    seeded = asyncio.run(seeder.seed_shared(AGENT, template, {}))
    assert [p["slug"] for p in seeded["pending"]] == ["board"] and seeded["seeded"] == []
    row = task_store.get_app_by_slug(AGENT, "", "board")
    assert row and row["kind"] == "folder" and row["deploy_state"] == "pending"
    assert not task_store.app_actions_approved(row) and row["template_ref"] == "ops-team:board"
    dyn = task_store.find_template_task(AGENT, "board__report")
    assert dyn and dyn["scope"] == "agent" and dyn["created_by"] == AGENT and dyn["community_template"] == "ops-team"
    # A shared app's task follows the platform clock (its created_by is the
    # agent, never a person with a zone).
    assert dyn["user_tz"] is None
    stored = json.loads((agent_tree / "workspace/apps/board/app.json").read_text())
    assert stored["actions"][0]["task_id"] == dyn["id"]
    # Seeding again is idempotent on the task; while release 1 still waits
    # the deploy REPLACES the waiting copy (the supersede rule of
    # 2026-09-14, APPS.md "Deploy pipeline") under a new number; once
    # approved, the next seed of the same manifest goes live as release 3
    # under that approval (a template seed never voids a standing approval;
    # a changed manifest stales it on its own).
    asyncio.run(seeder.seed_shared(AGENT, template, {}))
    assert task_store.get_app(row["id"])["pending_release"] == 2
    assert task_store.find_template_task(AGENT, "board__report")["id"] == dyn["id"]
    assert client.post(f"/v1/apps/{row['id']}/deploy/approve").status_code == 200
    asyncio.run(seeder.seed_shared(AGENT, template, {}))
    assert task_store.find_template_task(AGENT, "board__report")["id"] == dyn["id"]
    row = task_store.get_app(row["id"])
    assert row["pending_release"] == 0 and row["release_path"].endswith("/3")
    assert task_store.app_actions_approved(row)


def test_a_template_copy_opts_out_on_hard_unpin_and_pin_app_restores_it(agent_tree, tmp_path):
    from storage.agents import agent_store
    agent_store.create_agent(AGENT, "Blueprint", collaborative=True)
    tdir = tmp_path / "template"
    _folder(tdir / "user-apps" / "home", {"title": "Home", "actions": [
        {"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}]})
    items = tpl._discover_apps(tdir)
    seeder.write_seed_source(AGENT, items[0])
    agent_store.set_community_template_data(AGENT, {
        "slug": "ops-team", "version": "1.0.0", "tasks": [], "triggers": [], "notifications": [],
        "dashboards": [], "user_apps": [{"slug": "home", "sig": items[0].sig, "tree_sha": items[0].tree_sha}],
        "consent": {"apps": {"home": {"sig": items[0].sig, "by": "alice-sub", "at": "now"}}}})
    assert asyncio.run(seeder.seed_user_copy(AGENT, "alice-sub", "home")) == ("seeded", "release 1")
    row = task_store.get_app_by_slug(AGENT, "alice", "home")
    assert task_store.app_actions_approved(row) and row["template_ref"] == "ops-team:home"
    # The agent's hard unpin parks the row as the owner's opt-out; the
    # working folder stays, so pin_app(slug) redeploys it as any folder.
    out = _hook("unpin", {"slug": "home", "visibility": "user"}, sid=SID).json()
    assert out["status"] == "ok" and "pin_app(slug) restores it" in out["opted_out"]
    parked = task_store.get_app_by_slug(AGENT, "alice", "home")
    assert parked["hidden"] and parked["template_state"] == "opted_out"
    assert asyncio.run(seeder.seed_user_copy(AGENT, "alice-sub", "home")) == ("exists", "opted out")
    out = _hook("pin", {"slug": "home", "visibility": "user"}, sid=SID).json()
    assert out["status"] == "ok", out
    back = task_store.get_app_by_slug(AGENT, "alice", "home")
    assert not back["hidden"] and back["template_state"] == "" and back["template_ref"] == "ops-team:home"
    # A purge takes the folder too; pin_app(slug) then brings the copy back
    # from the template's seed, approved by the recorded consent.
    from services.apps import app_lifecycle
    assert asyncio.run(app_lifecycle.purge(back))["opted_out"] is True
    assert not (agent_tree / "users/alice/workspace/apps/home").exists()
    assert task_store.get_app_by_slug(AGENT, "alice", "home")["template_state"] == "opted_out"
    out = _hook("pin", {"slug": "home", "visibility": "user"}, sid=SID).json()
    assert out["status"] == "ok" and out["restored"], out
    again = task_store.get_app_by_slug(AGENT, "alice", "home")
    assert not again["hidden"] and again["template_state"] == "" and task_store.app_actions_approved(again)
    assert (agent_tree / "users/alice/workspace/apps/home/app.json").is_file()


def test_a_consented_shared_template_app_goes_live_without_a_server_start(agent_tree, tmp_path):
    tdir = tmp_path / "template"
    _folder(tdir / "apps" / "board", {"title": "Board", "actions": [
        {"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}]})
    items = tpl._discover_apps(tdir)
    template = SimpleNamespace(slug="ops-team", apps=items)
    task_store.upsert_user("alice-sub", "alice-sub@test.com", "Alice", "admin")
    data = {"consent": {"apps": {"board": {"sig": items[0].sig, "by": "alice-sub", "at": "now"}}}}
    with patch.object(app_supervisor, "start", side_effect=AssertionError("a seed never starts a server")):
        seeded = asyncio.run(seeder.seed_shared(AGENT, template, data))
    assert seeded["seeded"] == ["board"], seeded
    row = task_store.get_app_by_slug(AGENT, "", "board")
    assert row["deploy_state"] == "idle" and row["release_path"] and row["approved_by"] == "alice-sub"
    assert task_store.app_actions_approved(row)
    # A stale signature approves nothing: a new manifest (more egress) with
    # the old consent parks, and says the app changed since the consent.
    _folder(tdir / "apps" / "board", {"title": "Board", "egress": ["x.example"], "actions": [
        {"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}]})
    changed = SimpleNamespace(slug="ops-team", apps=tpl._discover_apps(tdir))
    stale = {"consent": {"apps": {"board": {"sig": items[0].sig, "by": "alice-sub", "at": "now"}}}}
    seeded = asyncio.run(seeder.seed_shared(AGENT, changed, stale))
    assert [p["slug"] for p in seeded["pending"]] == ["board"], seeded
    assert "changed since" in seeded["pending"][0]["reason"]
    assert not task_store.app_actions_approved(task_store.get_app_by_slug(AGENT, "", "board"))


def test_an_import_into_a_linked_folder_seeds_no_task(agent_tree, tmp_path):
    """The folder is judged before any bundle task is seeded, so a refused
    import leaves no orphan task rows behind."""
    import asyncio
    other = agent_tree / "workspace" / "other2"
    other.mkdir()
    (agent_tree / "workspace" / "apps").mkdir(exist_ok=True)
    (agent_tree / "workspace" / "apps" / "linked2").symlink_to(other)
    src = _folder(tmp_path / "bundle-src2", {"title": "Imported"})
    seeded = []
    with patch.object(app_blueprints, "_seed_task",
                      side_effect=lambda *a: seeded.append(a) or "task-1"), \
            pytest.raises(app_blueprints.BlueprintError, match="link"):
        asyncio.run(app_blueprints.import_folder(
            AGENT, "", None, "linked2", src, template_slug="bundle:linked2",
            tasks=[{"slug": "t", "title": "T", "prompt": "p"}]))
    assert seeded == []
    assert list(other.iterdir()) == []
