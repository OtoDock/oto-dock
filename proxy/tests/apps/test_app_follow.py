"""Apps that follow their source (services/apps/app_follow.py): a bundle
names the row it came from and never the data snapshot a release folder keeps; a copy another agent received records the
lineage (an own re-import and a bundle from elsewhere record none); the
card's states and each marker's settlement; the update's overlay, data
preservation and working-tree refusal; the restore after a refused and
after a rejected update; the merge's folder; the send-back held on the
source with the source's own tasks kept; stop; the routes' roles; the list
shape per viewer; the column on a pre-existing table.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from app import app
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from core.session import session_state
from services.apps import app_blueprints, app_deploy, app_follow, releases
from storage import database as task_store
from storage.pg import get_conn
from tests.apps.test_app_blueprints import _as, _folder, _user, client

SRC = "src-agent"
DST = "dst-agent"
ALICE = _user(agent_roles={SRC: "manager", DST: "manager"})
BOB_DST_ONLY = _user("bob-sub", {DST: "editor"})
VIEWER = _user("vic-sub", {DST: "viewer", SRC: "viewer"})


@pytest.fixture
def two_agents(tmp_path, monkeypatch):
    from storage.agents import agent_store
    agents_root = tmp_path / "agents"
    for a in (SRC, DST):
        (agents_root / a / "workspace").mkdir(parents=True)
        (agents_root / a / "users" / "alice" / "workspace").mkdir(parents=True)
        agent_store.create_agent(a, a.title(), created_by="alice-sub")
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    for sub, name in (("alice-sub", "Alice"), ("bob-sub", "Bob"), ("vic-sub", "Vic")):
        task_store.upsert_user(sub, f"{sub}@test.com", name, "member")
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", ("alice", "alice-sub"))
        conn.commit()
    task_store.add_user_agent("alice-sub", SRC, "manager", "test")
    task_store.add_user_agent("alice-sub", DST, "manager", "test")
    task_store.add_user_agent("bob-sub", DST, "editor", "test")
    task_store.add_user_agent("vic-sub", DST, "viewer", "test")
    task_store.add_user_agent("vic-sub", SRC, "viewer", "test")
    _as(ALICE)
    yield agents_root
    # The override outlives the test otherwise (the blueprint module's own
    # autouse cleanup does not reach this module) and every later request
    # on the worker would run as alice.
    app.dependency_overrides.pop(get_current_user, None)
    for sid in ("sess-src", "sess-dst"):
        session_state._session_security.pop(sid, None)


def _write(agent: str, slug: str, files: dict[str, str], manifest: dict | None = None) -> Path:
    root = config.get_agent_dir(agent) / "workspace" / "apps" / slug
    _folder(root, manifest or {"title": slug.title()})
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _deploy(agent: str, slug: str, files: dict[str, str], manifest: dict | None = None, *,
            approve: bool = True) -> dict:
    """A deploy of the agent's own working folder, approved by alice."""
    root = _write(agent, slug, files, manifest)
    out = asyncio.run(app_deploy.deploy_folder(agent, "", None, slug, root, f"workspace/apps/{slug}", cold=True))
    assert out.get("app_id"), out
    row = task_store.get_app(out["app_id"])
    if approve and out["status"] == app_deploy.RESULT_PENDING_APPROVAL:
        asyncio.run(app_deploy.approve_pending(row, "alice-sub"))
        row = task_store.get_app(out["app_id"])
    return row


def _approve(row: dict) -> dict:
    """The pending release approved by alice; a copy with nothing to approve
    went live at the import already."""
    row = task_store.get_app(row["id"])
    if row["deploy_state"] == "pending":
        asyncio.run(app_deploy.approve_pending(row, "alice-sub"))
    return task_store.get_app(row["id"])


# A manifest with something to approve: its copies wait on the card.
ASKS = {"title": "Board", "actions": [{"id": "me", "label": "Who am I", "type": "platform", "method": "viewer.me"}]}


def _copy_to(target: str, source_row: dict, *, slug: str | None = None) -> dict:
    """The share's path in process: the live release as a bundle, imported
    on the target with the bundle's source as the lineage."""
    live = releases.live_release_dir(source_row)
    data, _bp = app_blueprints.export_bundle(source_row, release_dir=live)
    folder, bp = app_blueprints.read_bundle(data)
    slug = slug or source_row["slug"]
    try:
        out = asyncio.run(app_blueprints.import_folder(
            target, "", None, slug, folder, template_slug=f"bundle:{slug}", tasks=bp.get("tasks") or [],
            origin=app_blueprints.clean_origin(bp) or ("", ""), follows=app_blueprints.clean_source(bp)))
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
    assert out.get("app_id"), out
    return task_store.get_app(out["app_id"])


def _copy_personal(target: str, source_row: dict, username: str = "alice", owner: str = "alice-sub") -> dict:
    """A personal copy: the share's path into a person's own scope."""
    live = releases.live_release_dir(source_row)
    data, _bp = app_blueprints.export_bundle(source_row, release_dir=live)
    folder, bp = app_blueprints.read_bundle(data)
    try:
        out = asyncio.run(app_blueprints.import_folder(
            target, username, owner, source_row["slug"], folder, template_slug=f"bundle:{source_row['slug']}",
            tasks=bp.get("tasks") or [], follows=app_blueprints.clean_source(bp)))
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
    assert out.get("app_id"), out
    return task_store.get_app(out["app_id"])


def test_a_personal_copy_names_the_persons_own_folders(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_personal(DST, src))
    assert copy["username"] == "alice" and app_follow.parse(copy)["slug"] == "board"
    _deploy(SRC, "board", {"client/index.html": "<p>v2</p>"})
    out = asyncio.run(app_follow.stage_merge(task_store.get_app(copy["id"]), ALICE))
    assert out["path"] == "/users/alice/workspace/apps/board.source-r2"
    assert out["text"] == ("Release 2 of Src-Agent's app is unpacked at /users/alice/workspace/apps/board.source-r2. "
                           'Tell the agent to merge it into the app "Board" at /users/alice/workspace/apps/board and '
                           "deploy one release.")
    assert (config.get_agent_dir(DST) / "users" / "alice" / "workspace" / "apps" / "board.source-r2" / "app.json").is_file()
    # Only the owner takes the source's releases into a personal copy.
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.take(task_store.get_app(copy["id"]), BOB_DST_ONLY))
    assert exc.value.status == 403 and exc.value.detail == "Only the person who owns this app updates it."


def _working(row: dict) -> Path:
    return config.get_agent_dir(row["agent"]) / row["rel_path"]


def _live_index(row: dict) -> str:
    return releases.read_release(task_store.get_app(row["id"])).decode()


# ── the bundle and the lineage ──────────────────────────────────────────────

def test_the_bundle_names_its_source_and_never_the_data_snapshot(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    assert releases.current_number(src) == 1
    live = releases.live_release_dir(src)
    (live / releases.DB_BEFORE_NAME).write_bytes(b"the data")
    data, bp = app_blueprints.export_bundle(src, release_dir=live)
    assert bp["source"] == {"id": src["id"], "agent": SRC, "username": "", "slug": "board", "release": 1}
    folder, _bp = app_blueprints.read_bundle(data)
    try:
        names = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
        assert names == ["app.json", "client/app.js", "client/index.html"]
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
    # A working folder exported stands for the live release, edits not
    # released yet included: the copy is never offered an older release.
    assert app_blueprints.export_bundle(src)[1]["source"]["release"] == 1
    (_working(src) / "client" / "index.html").write_text("<p>v1 edited</p>")
    assert app_blueprints.export_bundle(src)[1]["source"]["release"] == 1
    assert app_blueprints.clean_source(bp) == bp["source"]
    assert app_blueprints.clean_source({"source": {**bp["source"], "release": "1"}}) is None
    assert app_blueprints.clean_source({"source": {**bp["source"], "slug": "Bad Slug"}}) is None
    assert app_blueprints.clean_source({"source": {**bp["source"], "username": "../x"}}) is None
    assert app_blueprints.clean_source({}) is None
    # Agent slugs of two characters, or with a digit first, are agents too.
    for agent in ("hr", "qa", "2d-office"):
        assert app_blueprints.clean_source({"source": {**bp["source"], "agent": agent}})["agent"] == agent
    for agent in ("h", "-hr", "hr-", "HR", "a" * 41):
        assert app_blueprints.clean_source({"source": {**bp["source"], "agent": agent}}) is None


def test_the_share_card_copies_the_live_release_and_records_the_lineage(two_agents):
    """The share_app card's own run: the source's live release lands on the
    target, never the working folder's unreleased edits, and the copy
    follows it from that release."""
    from api.cards import actions_team
    from services.cards import status as st
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    (_working(src) / "client" / "index.html").write_text("<p>not released</p>")
    out = asyncio.run(actions_team.SHARE_APP.run({"app_slug": "board", "target_agent": DST, "_agent": SRC}, {}, ALICE))
    assert out.status == st.OUTCOME_DONE, out.text
    copy = task_store.get_app_by_slug(DST, "", "board")
    assert (_working(copy) / "client" / "index.html").read_text() == "<p>v1</p>"
    assert app_follow.parse(copy) == {"id": src["id"], "agent": SRC, "username": "", "slug": "board",
                                      "release": 1, "copy_release": releases.current_number(copy)}


def test_a_copy_records_the_lineage_and_an_own_or_foreign_bundle_does_not(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"}, ASKS)
    copy = _copy_to(DST, src)
    assert copy["deploy_state"] == "pending" and copy["pending_release"] == 1
    assert app_follow.parse(copy) == {"id": src["id"], "agent": SRC, "username": "", "slug": "board",
                                      "release": 1, "copy_release": 1}
    st = app_follow.state(copy)
    assert st["source"] == {"agent": SRC, "agent_name": "Src-Agent", "slug": "board", "release": 1, "exists": True,
                           "held_release": 0}
    assert st["update"] is None
    # The same agent importing its own export under its own slug follows nothing.
    own = _copy_to(SRC, src)
    assert own["id"] == src["id"] and own["follows"] == ""
    # A bundle whose source row is not on this install follows nothing.
    data, bp = app_blueprints.export_bundle(src, release_dir=releases.live_release_dir(src))
    bp["source"]["id"] = "another-install-row"
    folder, _ = app_blueprints.read_bundle(data)
    try:
        out = asyncio.run(app_blueprints.import_folder(
            DST, "", None, "other", folder, template_slug="bundle:other", tasks=[],
            follows=app_blueprints.clean_source(bp)))
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
    assert task_store.get_app(out["app_id"])["follows"] == ""


def test_the_states_update_merge_and_none(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    assert app_follow.state(copy)["update"] is None
    # The source releases again: an update, since the copy never released on its own.
    _deploy(SRC, "board", {"client/index.html": "<p>v2</p>"})
    assert app_follow.state(copy)["update"] == {"release": 2, "mode": "update"}
    # The copy releases on its own: a merge.
    _deploy(DST, "board", {"client/index.html": "<p>mine</p>"})
    copy = task_store.get_app(copy["id"])
    assert releases.current_number(copy) == 2
    assert app_follow.state(copy)["update"] == {"release": 2, "mode": "merge"}
    # A rollback on the source offers nothing.
    asyncio.run(app_deploy.rollback_folder(task_store.get_app(src["id"])))
    assert releases.current_number(task_store.get_app(src["id"])) == 1
    assert app_follow.state(copy)["update"] is None
    # Stop following: the row keeps nothing.
    app_follow.stop(copy)
    assert app_follow.state(task_store.get_app(copy["id"])) is None


# ── the update ───────────────────────────────────────────────────────────────

def test_take_puts_the_release_on_the_copy_held_for_approval_and_keeps_its_data(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>", "client/old.js": "old()"})
    copy = _approve(_copy_to(DST, src))
    # The copy's data, a dotfile and a module folder live beside the files.
    import sqlite3
    data_dir = releases.app_data_dir(copy)
    data_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(data_dir / "app.db")
    con.execute("CREATE TABLE rows (x)")
    con.execute("INSERT INTO rows VALUES ('kept')")
    con.commit()
    con.close()
    (_working(copy) / ".env.local").write_text("secret")
    (_working(copy) / "node_modules").mkdir()
    (_working(copy) / "node_modules" / "x.js").write_text("dep")
    _deploy(SRC, "board", {"client/index.html": "<p>v2</p>", "client/new.js": "fresh()"})
    (_working(task_store.get_app(src["id"])) / "client" / "old.js").unlink()
    _deploy(SRC, "board", {"client/index.html": "<p>v3</p>", "client/new.js": "fresh()"})
    out = asyncio.run(app_follow.take(copy, ALICE))
    assert out["release"] == 2 and out["source_release"] == 3
    assert out["text"] == ('Release 2 of "Board" waits for approval here. It carries release 3 of '
                           "Src-Agent's app. The app's data stays.")
    copy = task_store.get_app(copy["id"])
    assert copy["deploy_state"] == "pending" and copy["pending_release"] == 2
    # The deploy card names where the held release came from.
    assert app_follow.state(copy)["source"]["held_release"] == 2
    assert (_working(copy) / "client" / "index.html").read_text() == "<p>v3</p>"
    assert (_working(copy) / "client" / "new.js").is_file()
    assert not (_working(copy) / "client" / "old.js").exists()
    assert (_working(copy) / ".env.local").read_text() == "secret"
    assert (_working(copy) / "node_modules" / "x.js").is_file()
    con = sqlite3.connect(data_dir / "app.db")
    assert con.execute("SELECT x FROM rows").fetchall() == [("kept",)]
    con.close()
    # The marker names the cut by its number and its tree.
    tree = releases.tree_sha(releases.app_release_dir(copy) / "2")
    assert tree and app_follow.parse(copy)["pending"] == {"release": 2, "source_release": 3, "tree": tree, "tasks": []}
    # While the release waits nothing more is offered; approved, the lineage folds.
    assert app_follow.state(copy)["update"] is None
    copy = _approve(copy)
    assert _live_index(copy) == "<p>v3</p>"
    con = sqlite3.connect(data_dir / "app.db")
    assert con.execute("SELECT x FROM rows").fetchall() == [("kept",)]
    con.close()
    st = app_follow.state(copy)
    assert st["update"] is None and st["source"]["release"] == 3
    assert app_follow.parse(task_store.get_app(copy["id"])) == {
        "id": src["id"], "agent": SRC, "username": "", "slug": "board", "release": 3, "copy_release": 2}
    assert not (releases.app_release_dir(copy) / app_follow.BACKUP_DIRNAME).exists()


def _deletes(monkeypatch) -> list[tuple[str, str]]:
    """The files' own delete's push, recorded: what a paired machine is
    told to drop."""
    from services.infra import file_bookkeeping
    pushed: list[tuple[str, str]] = []

    async def fake_push_delete(agent, rel, *a, **kw):
        pushed.append((agent, rel))
    monkeypatch.setattr(file_bookkeeping, "push_file_delete", fake_push_delete)
    return pushed


def test_the_files_a_move_drops_go_through_the_files_own_delete(two_agents, monkeypatch):
    pushed = _deletes(monkeypatch)
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>", "client/old.js": "old()"})
    copy = _approve(_copy_to(DST, src))
    (_working(task_store.get_app(src["id"])) / "client" / "old.js").unlink()
    _deploy(SRC, "board", {"client/index.html": "<p>v2</p>", "client/new.js": "fresh()"})
    # The update's overlay drops old.js: the machine is told to drop it too.
    asyncio.run(app_follow.take(copy, ALICE))
    assert (DST, "workspace/apps/board/client/old.js") in pushed
    assert not (_working(copy) / "client" / "old.js").exists()
    # A rejected update puts the live files back: new.js goes the same way.
    pushed.clear()
    asyncio.run(app_deploy.reject_pending(task_store.get_app(copy["id"])))
    assert (DST, "workspace/apps/board/client/new.js") in pushed
    assert (_working(copy) / "client" / "old.js").read_text() == "old()"
    assert not (_working(copy) / "client" / "new.js").exists()


def test_take_refuses_without_an_update_a_merge_due_or_an_edited_working_folder(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.take(copy, ALICE))
    assert exc.value.detail == "Src-Agent's app has no newer release."
    _deploy(SRC, "board", {"client/index.html": "<p>v2</p>"})
    (_working(copy) / "client" / "index.html").write_text("<p>edited, not released</p>")
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.take(copy, ALICE))
    assert exc.value.detail == app_follow.WORKING_CHANGED
    _deploy(DST, "board", {"client/index.html": "<p>mine</p>"})
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.take(task_store.get_app(copy["id"]), ALICE))
    assert exc.value.detail == app_follow.MERGE_DUE
    # The roles on both sides: an editor of the copy alone, a viewer, a row that follows nothing.
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.take(task_store.get_app(copy["id"]), BOB_DST_ONLY))
    assert exc.value.status == 403 and exc.value.detail == "Only editors and managers of Src-Agent take its releases."
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.take(task_store.get_app(copy["id"]), VIEWER))
    assert exc.value.status == 403 and exc.value.detail == app_follow.COPY_NEEDS_EDITOR
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.take(task_store.get_app(src["id"]), ALICE))
    assert exc.value.detail == app_follow.NOT_FOLLOWING


def test_a_refused_update_puts_the_working_folder_back_and_removes_what_it_seeded(two_agents, monkeypatch):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    _deploy(SRC, "board", {"client/index.html": "<p>v2</p>", "client/new.js": "fresh()"})
    real = app_deploy.deploy_folder

    async def refusing(*a, **kw):
        return {"status": app_deploy.RESULT_REFUSED, "slug": "board", "problems": 1, "warnings": 0,
                "findings": [{"severity": "fail", "message": "the page calls a host the manifest does not allow"}]}

    monkeypatch.setattr(app_deploy, "deploy_folder", refusing)
    try:
        with pytest.raises(app_follow.FollowRefused) as exc:
            asyncio.run(app_follow.take(copy, ALICE))
    finally:
        monkeypatch.setattr(app_deploy, "deploy_folder", real)
    assert exc.value.detail == "The page calls a host the manifest does not allow."
    assert (_working(copy) / "client" / "index.html").read_text() == "<p>v1</p>"
    assert not (_working(copy) / "client" / "new.js").exists()
    assert "pending" not in (app_follow.parse(task_store.get_app(copy["id"])) or {})
    assert not (releases.app_release_dir(copy) / app_follow.BACKUP_DIRNAME).exists()
    assert app_follow.state(task_store.get_app(copy["id"]))["update"] == {"release": 2, "mode": "update"}


def test_a_restore_that_fails_says_so_and_keeps_the_files_it_holds(two_agents, monkeypatch):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    _deploy(SRC, "board", {"client/index.html": "<p>v2</p>"})

    async def failing(row, from_dir):
        return False

    async def refusing(*a, **kw):
        return {"status": app_deploy.RESULT_REFUSED, "findings": [{"severity": "fail", "message": "no"}]}
    real_deploy, real_restore, real_snapshot = app_deploy.deploy_folder, app_follow._restore, app_follow._snapshot
    monkeypatch.setattr(app_follow, "_restore", failing)
    monkeypatch.setattr(app_deploy, "deploy_folder", refusing)
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.take(task_store.get_app(copy["id"]), ALICE))
    assert exc.value.detail == "No. The app's files could not be put back. Ask the agent to deploy its last release again."
    # The snapshot is kept: it holds the only copy of the files the move replaced.
    backup = releases.app_release_dir(copy) / app_follow.BACKUP_DIRNAME
    assert (backup / "client" / "index.html").read_text() == "<p>v1</p>"
    # A rejection whose restore fails says so in its answer.
    # (Never monkeypatch.undo() here: it would undo the fixture's agents folder too.)
    monkeypatch.setattr(app_deploy, "deploy_folder", real_deploy)
    monkeypatch.setattr(app_follow, "_restore", real_restore)
    # The failed restore left the update's files in the folder: put the live release back first.
    live_copy = task_store.get_app(copy["id"])
    assert asyncio.run(real_restore(live_copy, releases.live_release_dir(live_copy)))
    asyncio.run(app_follow.take(task_store.get_app(copy["id"]), ALICE))
    monkeypatch.setattr(app_follow, "_restore", failing)
    out = asyncio.run(app_deploy.reject_pending(task_store.get_app(copy["id"])))
    assert out["status"] == app_deploy.RESULT_REJECTED and out["warning"] == app_follow.NOT_PUT_BACK
    # A working folder that cannot be read refuses the move, never a server error.
    monkeypatch.setattr(app_follow, "_restore", real_restore)
    assert real_snapshot is app_follow._snapshot
    live_copy = task_store.get_app(copy["id"])
    assert asyncio.run(real_restore(live_copy, releases.live_release_dir(live_copy)))

    def unreadable(row):
        raise OSError("gone")
    monkeypatch.setattr(app_follow, "_snapshot", unreadable)
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.take(task_store.get_app(copy["id"]), ALICE))
    assert exc.value.status == 409 and exc.value.detail == app_follow.NOT_READ


def test_a_rejected_update_removes_the_tasks_its_new_buttons_seeded(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    task_store.create_dynamic_task("task-src-report", SRC, "Weekly report", "write it", "cli", "trigger",
                                   None, None, None, 600, SRC, scope="agent")
    _deploy(SRC, "board", {"client/index.html": "<p>v2</p>"}, {"title": "Board", "actions": [
        {"id": "report", "label": "Report", "type": "fire_task", "task_id": "task-src-report"}]})

    def seeded() -> list[str]:
        return [t["id"] for t in task_store.list_dynamic_tasks(agent=DST) if t.get("community_template") == "bundle:board"]
    assert seeded() == []
    asyncio.run(app_follow.take(task_store.get_app(copy["id"]), ALICE))
    (task_id,) = seeded()
    assert app_follow.parse(task_store.get_app(copy["id"]))["pending"]["tasks"] == [task_id]
    asyncio.run(app_deploy.reject_pending(task_store.get_app(copy["id"])))
    assert seeded() == []
    assert (_working(copy) / "client" / "index.html").read_text() == "<p>v1</p>"


def test_a_rejected_update_puts_the_live_files_back_and_offers_again(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    _deploy(SRC, "board", {"client/index.html": "<p>v2</p>"})
    asyncio.run(app_follow.take(copy, ALICE))
    copy = task_store.get_app(copy["id"])
    assert (_working(copy) / "client" / "index.html").read_text() == "<p>v2</p>"
    asyncio.run(app_deploy.reject_pending(copy))
    copy = task_store.get_app(copy["id"])
    assert (_working(copy) / "client" / "index.html").read_text() == "<p>v1</p>"
    assert "pending" not in app_follow.parse(copy)
    assert app_follow.state(copy)["update"] == {"release": 2, "mode": "update"}


# ── the merge ────────────────────────────────────────────────────────────────

def test_stage_merge_unpacks_the_release_beside_the_app_and_the_copys_release_settles_it(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    _deploy(DST, "board", {"client/index.html": "<p>mine</p>"})
    _deploy(SRC, "board", {"client/index.html": "<p>v2</p>", "client/new.js": "fresh()"})
    copy = task_store.get_app(copy["id"])
    assert app_follow.state(copy)["update"]["mode"] == "merge"
    out = asyncio.run(app_follow.stage_merge(copy, ALICE))
    assert out["release"] == 2 and out["path"] == "/workspace/apps/board.source-r2"
    assert out["text"] == ("Release 2 of Src-Agent's app is unpacked at /workspace/apps/board.source-r2. Tell the "
                           'agent to merge it into the app "Board" at /workspace/apps/board and deploy one release.')
    staged = config.get_agent_dir(DST) / "workspace" / "apps" / "board.source-r2"
    assert (staged / "client" / "new.js").read_text() == "fresh()" and (staged / "app.json").is_file()
    assert not (staged / releases.MANIFEST_NAME).exists()
    # The copy's own files are untouched; the state names where the release is unpacked.
    assert (_working(copy) / "client" / "index.html").read_text() == "<p>mine</p>"
    copy = task_store.get_app(copy["id"])
    assert app_follow.state(copy)["update"] == {"release": 2, "mode": "asked",
                                                "path": "/workspace/apps/board.source-r2"}
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.take(copy, ALICE))
    assert exc.value.detail == ("Release 2 is unpacked beside this app. The card clears when the agent deploys "
                                "the merged release.")
    # The agent's merged release goes live: the lineage stands at release 2.
    _deploy(DST, "board", {"client/index.html": "<p>merged</p>", "client/new.js": "fresh()"})
    copy = task_store.get_app(copy["id"])
    st = app_follow.state(copy)
    assert st["update"] is None and st["source"]["release"] == 2
    assert "staged" not in app_follow.parse(task_store.get_app(copy["id"]))


# ── the send-back ────────────────────────────────────────────────────────────

def test_send_back_holds_the_release_on_the_source_and_keeps_the_sources_own_tasks(two_agents):
    task_store.create_dynamic_task("task-src-report", SRC, "Weekly report", "write it", "cli", "trigger",
                                   None, None, None, 600, SRC, scope="agent")
    manifest = {"title": "Board", "actions": [{"id": "report", "label": "Report", "type": "fire_task",
                                                "task_id": "task-src-report"}]}
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"}, manifest)
    copy = _approve(_copy_to(DST, src))
    seeded = [t for t in task_store.list_dynamic_tasks(agent=DST) if t.get("community_template") == "bundle:board"]
    assert len(seeded) == 1
    # The copy releases a change and sends it back.
    _deploy(DST, "board", {"client/index.html": "<p>better</p>"}, manifest={
        "title": "Board", "actions": [{"id": "report", "label": "Report", "type": "fire_task",
                                       "task_id": seeded[0]["id"]}]})
    copy = task_store.get_app(copy["id"])
    out = asyncio.run(app_follow.send_back(copy, ALICE))
    assert out == {"release": 2, "text": "Release 2 of Src-Agent's app waits for approval there."}
    src = task_store.get_app(src["id"])
    assert src["deploy_state"] == "pending" and src["pending_release"] == 2
    assert (_working(src) / "client" / "index.html").read_text() == "<p>better</p>"
    stored = json.loads((_working(src) / "app.json").read_text())
    assert stored["actions"][0]["task_id"] == "task-src-report"
    assert [t for t in task_store.list_dynamic_tasks(agent=SRC) if t.get("community_template") == "bundle:board"] == []
    assert app_follow.parse(task_store.get_app(copy["id"]))["sent"] == {"release": 2, "copy_release": 2}
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.send_back(task_store.get_app(copy["id"]), ALICE))
    assert exc.value.detail == "A release of this app already waits on Src-Agent."
    # Approved on the source: the copy stands at that release, nothing offered.
    _approve(src)
    st = app_follow.state(task_store.get_app(copy["id"]))
    assert st["update"] is None and st["source"]["release"] == 2
    assert "sent" not in app_follow.parse(task_store.get_app(copy["id"]))
    # The source's note for its reject path goes with the approval.
    assert not (releases.app_release_dir(src) / app_follow.SENT_BACK_NAME).exists()


def test_a_copy_release_made_after_a_send_is_merged_never_overwritten(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    _deploy(DST, "board", {"client/index.html": "<p>sent</p>"})
    asyncio.run(app_follow.send_back(task_store.get_app(copy["id"]), ALICE))
    # The copy ships its own release 3 while release 2 waits on the source.
    _deploy(DST, "board", {"client/index.html": "<p>the copy's own work</p>"})
    _approve(task_store.get_app(src["id"]))
    copy = task_store.get_app(copy["id"])
    st = app_follow.state(copy)
    assert st["source"]["release"] == 2 and st["update"] is None
    assert app_follow.parse(task_store.get_app(copy["id"]))["copy_release"] == 2
    # The source releases again: the copy's release 3 is not in it, so a merge.
    _deploy(SRC, "board", {"client/index.html": "<p>v3 of the source</p>"})
    assert app_follow.state(task_store.get_app(copy["id"]))["update"] == {"release": 3, "mode": "merge"}
    with pytest.raises(app_follow.FollowRefused):
        asyncio.run(app_follow.take(task_store.get_app(copy["id"]), ALICE))
    assert (_working(copy) / "client" / "index.html").read_text() == "<p>the copy's own work</p>"


def test_a_merge_folds_to_the_first_release_after_the_staging(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    _deploy(DST, "board", {"client/index.html": "<p>mine</p>"})
    _deploy(SRC, "board", {"client/index.html": "<p>v2</p>"})
    asyncio.run(app_follow.stage_merge(task_store.get_app(copy["id"]), ALICE))
    assert app_follow.parse(task_store.get_app(copy["id"]))["staged"] == {"release": 2, "above": 2}
    # The merged release 3 and a further own release 4 land before any read.
    _deploy(DST, "board", {"client/index.html": "<p>merged</p>"})
    _deploy(DST, "board", {"client/index.html": "<p>merged, then my own change</p>"})
    st = app_follow.state(task_store.get_app(copy["id"]))
    assert st["source"]["release"] == 2 and st["update"] is None
    assert app_follow.parse(task_store.get_app(copy["id"]))["copy_release"] == 3
    _deploy(SRC, "board", {"client/index.html": "<p>v3</p>"})
    assert app_follow.state(task_store.get_app(copy["id"]))["update"] == {"release": 3, "mode": "merge"}


def test_two_copies_sending_to_one_source_at_once_wait_for_each_other(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    shared = _approve(_copy_to(DST, src))
    personal = _approve(_copy_personal(DST, src))
    _deploy(DST, "board", {"client/index.html": "<p>the team's change</p>"})
    root = _working(personal)
    (root / "client" / "index.html").write_text("<p>alice's change</p>")
    out = asyncio.run(app_deploy.deploy_folder(DST, "alice", "alice-sub", "board", root,
                                               "users/alice/workspace/apps/board", cold=True))
    assert out["status"] == app_deploy.RESULT_OK, out

    async def both():
        return await asyncio.gather(
            app_follow.send_back(task_store.get_app(shared["id"]), ALICE),
            app_follow.send_back(task_store.get_app(personal["id"]), ALICE), return_exceptions=True)

    first, second = asyncio.run(both())
    results = [r for r in (first, second) if isinstance(r, dict)]
    refused = [r for r in (first, second) if isinstance(r, app_follow.FollowRefused)]
    assert len(results) == 1 and len(refused) == 1, (first, second)
    assert refused[0].detail == "Src-Agent's app has a release waiting for approval. Approve or reject it first."
    # One release waits on the source, whole, and no backup is left behind.
    src = task_store.get_app(src["id"])
    assert src["pending_release"] == results[0]["release"]
    assert (releases.app_release_dir(src) / str(results[0]["release"]) / "client" / "index.html").is_file()
    assert not (releases.app_release_dir(src) / app_follow.BACKUP_DIRNAME).exists()
    assert app_follow._locks == {} and app_follow._holders == {}


def test_a_send_back_waits_while_the_source_has_its_own_release_waiting(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    _deploy(DST, "board", {"client/index.html": "<p>better</p>"})
    # The source's own agent ships release 2, which waits on its card.
    waiting = _deploy(SRC, "board", {"client/index.html": "<p>v2 of the source</p>"}, ASKS, approve=False)
    assert waiting["deploy_state"] == "pending" and waiting["pending_release"] == 2
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.send_back(task_store.get_app(copy["id"]), ALICE))
    assert exc.value.detail == "Src-Agent's app has a release waiting for approval. Approve or reject it first."
    # The waiting release, its files and the source's working folder are untouched.
    src = task_store.get_app(src["id"])
    assert src["pending_release"] == 2 and (releases.app_release_dir(src) / "2").is_dir()
    assert (_working(src) / "client" / "index.html").read_text() == "<p>v2 of the source</p>"
    assert "sent" not in app_follow.parse(task_store.get_app(copy["id"]))
    # The card's check says the same at the draw.
    why = app_follow.refusal(task_store.get_app(copy["id"]), ALICE, app_follow.MOVE_SEND)
    assert why is not None and why.detail == exc.value.detail


def test_a_rejected_send_back_restores_the_sources_files_and_frees_the_copy(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    _deploy(DST, "board", {"client/index.html": "<p>better</p>"})
    asyncio.run(app_follow.send_back(task_store.get_app(copy["id"]), ALICE))
    src = task_store.get_app(src["id"])
    assert (_working(src) / "client" / "index.html").read_text() == "<p>better</p>"
    asyncio.run(app_deploy.reject_pending(src))
    assert (_working(src) / "client" / "index.html").read_text() == "<p>v1</p>"
    assert not (releases.app_release_dir(src) / app_follow.SENT_BACK_NAME).exists()
    # The reject clears the copy's marker itself, before any read of the copy.
    assert "sent" not in (app_follow.parse(task_store.get_app(copy["id"])) or {})
    assert app_follow.state(task_store.get_app(copy["id"]))["update"] is None
    # Fresh after the read: the copy may send again; a source whose working
    # folder moved on refuses.
    (_working(src) / "client" / "index.html").write_text("<p>v1 and an edit</p>")
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.send_back(task_store.get_app(copy["id"]), ALICE))
    assert exc.value.detail == ("The files of Src-Agent's app changed since its last release. Ask its agent to "
                                "take this release by hand.")


def test_a_stale_note_never_puts_back_over_the_sources_own_work(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    _deploy(DST, "board", {"client/index.html": "<p>sent</p>"})
    asyncio.run(app_follow.send_back(task_store.get_app(copy["id"]), ALICE))
    assert app_follow._note(task_store.get_app(src["id"]))["copy"] == copy["id"]
    # The source's own agent deploys over the sent release (it is replaced),
    # then has its own release 3 rejected; nobody reads the copy meanwhile.
    _deploy(SRC, "board", {"client/index.html": "<p>the source's own A</p>"}, ASKS, approve=False)
    asyncio.run(app_deploy.reject_pending(task_store.get_app(src["id"])))
    # Release number 2 comes back for the source's next own release.
    again = _deploy(SRC, "board", {"client/index.html": "<p>the source's own B</p>"}, ASKS, approve=False)
    assert again["pending_release"] == 2
    asyncio.run(app_deploy.reject_pending(task_store.get_app(src["id"])))
    # The stale note names release 2 with another tree: the working folder stays the agent's.
    assert (_working(src) / "client" / "index.html").read_text() == "<p>the source's own B</p>"
    # The copy's read clears its marker and drops the note.
    app_follow.state(task_store.get_app(copy["id"]))
    assert "sent" not in app_follow.parse(task_store.get_app(copy["id"]))
    assert not (releases.app_release_dir(src) / app_follow.SENT_BACK_NAME).exists()


def test_a_copy_with_no_live_release_has_nothing_to_send(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"}, ASKS)
    copy = _copy_to(DST, src)
    assert copy["deploy_state"] == "pending"
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.send_back(copy, ALICE))
    assert exc.value.detail == app_follow.NOTHING_TO_SEND


# ── a replaced source ────────────────────────────────────────────────────────

def test_a_purged_source_reads_gone_and_a_re_pinned_one_re_bases(two_agents):
    from services.apps import app_lifecycle
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    asyncio.run(app_lifecycle.purge(src))
    st = app_follow.state(task_store.get_app(copy["id"]))
    assert st["source"]["exists"] is False and st["update"] is None
    with pytest.raises(app_follow.FollowRefused) as exc:
        asyncio.run(app_follow.take(task_store.get_app(copy["id"]), ALICE))
    assert exc.value.detail == app_follow.SOURCE_GONE
    again = _deploy(SRC, "board", {"client/index.html": "<p>new life</p>"})
    assert again["id"] != src["id"] and releases.current_number(again) == 1
    st = app_follow.state(task_store.get_app(copy["id"]))
    assert st["source"]["exists"] is True and st["source"]["release"] == 1 and st["update"] is None
    assert app_follow.parse(task_store.get_app(copy["id"]))["id"] == again["id"]
    _deploy(SRC, "board", {"client/index.html": "<p>new life 2</p>"})
    assert app_follow.state(task_store.get_app(copy["id"]))["update"] == {"release": 2, "mode": "update"}


# ── the routes and the list shape ────────────────────────────────────────────

def test_the_routes_take_a_person_with_rights_on_both_sides(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    _deploy(SRC, "board", {"client/index.html": "<p>v2</p>"})
    # The list shape names the source and the update for whoever may manage the copy.
    apps = {a["slug"]: a for a in client.get(f"/v1/apps?agent={DST}").json()["apps"]}
    assert apps["board"]["follows"] == {"agent": SRC, "agent_name": "Src-Agent", "slug": "board", "release": 1,
                                        "exists": True, "held_release": 0, "can_take": True,
                                        "update": {"release": 2, "mode": "update"}}
    _as(BOB_DST_ONLY)
    apps = {a["slug"]: a for a in client.get(f"/v1/apps?agent={DST}").json()["apps"]}
    assert apps["board"]["follows"]["can_take"] is False
    r = client.post(f"/v1/apps/{copy['id']}/follow/take", json={"mode": "update"})
    assert r.status_code == 403 and r.json()["detail"] == "Only editors and managers of Src-Agent take its releases."
    r = client.post(f"/v1/apps/{copy['id']}/follow/send")
    assert r.status_code == 403 and r.json()["detail"] == "Only editors and managers of Src-Agent take its releases."
    _as(VIEWER)
    apps = {a["slug"]: a for a in client.get(f"/v1/apps?agent={DST}").json()["apps"]}
    assert "follows" not in apps["board"]
    assert client.delete(f"/v1/apps/{copy['id']}/follow").status_code == 403
    # A key or a session token never presses them: a person at the keyboard does.
    _as(UserContext(sub="alice-sub", email="a@test.com", name="Alice", role="member", agents=[SRC, DST],
                    is_api_key=True, agent_roles={SRC: "manager", DST: "manager"}))
    assert client.post(f"/v1/apps/{copy['id']}/follow/take", json={"mode": "update"}).status_code == 403
    assert client.post(f"/v1/apps/{copy['id']}/follow/send").status_code == 403
    assert client.delete(f"/v1/apps/{copy['id']}/follow").status_code == 403
    assert app_follow.parse(task_store.get_app(copy["id"])) is not None
    _as(ALICE)
    r = client.post(f"/v1/apps/{copy['id']}/follow/take", json={"mode": "bad"})
    assert r.status_code == 400
    r = client.post(f"/v1/apps/{copy['id']}/follow/take", json={"mode": "update"})
    assert r.status_code == 200 and r.json()["release"] == 2 and r.json()["mode"] == "update", r.text
    r = client.post(f"/v1/apps/{copy['id']}/follow/take", json={"mode": "update"})
    assert r.status_code == 409
    assert client.delete(f"/v1/apps/{copy['id']}/follow").status_code == 200
    assert task_store.get_app(copy["id"])["follows"] == ""
    assert client.delete(f"/v1/apps/{copy['id']}/follow").status_code == 409
    r = client.post(f"/v1/apps/{copy['id']}/follow/send")
    assert r.status_code == 409 and r.json()["detail"] == app_follow.NOT_FOLLOWING


def test_the_agents_list_hook_names_the_source(two_agents):
    src = _deploy(SRC, "board", {"client/index.html": "<p>v1</p>"})
    copy = _approve(_copy_to(DST, src))
    _deploy(SRC, "board", {"client/index.html": "<p>v2</p>"})
    sid = "sess-dst"
    session_state.set_session_security(sid, SecurityContext(role="manager", username="alice", agent=DST,
                                                             is_admin_agent=False))
    with patch("api.hooks.pins.verify_session_match_async"):
        r = client.post("/v1/hooks/apps/list", json={"session_id": sid}, headers={"Authorization": "Bearer x"})
    assert r.status_code == 200, r.text
    (row,) = [a for a in r.json()["apps"] if a["id"] == copy["id"]]
    assert row["follows"]["agent"] == SRC and row["follows"]["update"] == {"release": 2, "mode": "update"}


def _columns() -> set[str]:
    with get_conn() as conn:
        return {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='pinned_apps'").fetchall()}


def test_the_column_is_added_to_a_table_from_before(temp_db):
    """The forward migration adds the column to a table from before it. The
    pool is closed around each change of the table's shape (the audience
    migration test's pattern): a connection kept across one answers "cached
    plan must not change result type" to the next ``SELECT *``."""
    from storage import db_apps, pg, schema as pg_schema
    from storage.agents import agent_store
    agent_store.create_agent("mig-agent", "Mig")
    old = db_apps.upsert_app("mig-agent", "", None, "board", title="Board", rel_path="workspace/apps/board",
                             kind="folder")
    pg.close_pool()
    with get_conn() as conn:
        conn.execute("ALTER TABLE pinned_apps DROP COLUMN follows")
        conn.commit()
    pg.close_pool()
    assert "follows" not in _columns()
    pg.close_pool()
    with get_conn() as conn:
        pg_schema.run_migrations(conn)
        conn.commit()
    pg.close_pool()
    assert "follows" in _columns()
    # The row from before follows nothing; a second run keeps what a row holds.
    assert task_store.get_app(old["id"])["follows"] == ""
    db_apps.set_follows(old["id"], '{"id": "x"}')
    pg.close_pool()
    with get_conn() as conn:
        pg_schema.run_migrations(conn)
        conn.commit()
    pg.close_pool()
    assert task_store.get_app(old["id"])["follows"] == '{"id": "x"}'
