"""Releases and rollback of single-file apps (APPS.md "Releases and
rollback").

Load-bearing: a viewer is served the copy made at pin time and never the
working file the agent edits; the pointer and the hash move together; a
rollback serves the previous copy at once; a row from before releases
existed (empty pointer) keeps serving its working file; the copies leave
with their row.
"""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from core.session import session_state
from services.apps import releases
from storage import database as task_store

client = TestClient(app)

SID = "sess-rel-alice"
SID_SHARED = "sess-rel-shared"
AGENT = "rel-agent"


def _user(sub: str = "alice-sub", role: str = "member",
          agent_roles: dict[str, str] | None = None, is_api_key: bool = False) -> UserContext:
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role=role,
                       agents=[AGENT], is_api_key=is_api_key,
                       agent_roles={AGENT: "manager"} if agent_roles is None else agent_roles)


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def authed():
    _as(_user())
    from api.apps import apps as apps_api
    apps_api._fire_rate.clear()
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    (agents_root / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    task_store.upsert_user("alice-sub", "alice@test.com", "Alice", "member")
    task_store.upsert_user("bob-sub", "bob@test.com", "Bob", "member")
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", ("alice", "alice-sub"))
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", ("bob", "bob-sub"))
        conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    task_store.add_user_agent("bob-sub", AGENT, "viewer", "test")
    session_state.set_session_security(SID, SecurityContext(
        role="manager", username="alice", agent=AGENT, is_admin_agent=False))
    session_state.set_session_security(SID_SHARED, SecurityContext(
        role="manager", username="", agent=AGENT, is_admin_agent=False))
    yield agents_root / AGENT
    for sid in (SID, SID_SHARED):
        session_state._session_security.pop(sid, None)


def _hook(op: str, payload: dict, sid: str = SID):
    payload.setdefault("session_id", sid)
    with patch("api.hooks.pins.verify_session_match_async"):
        return client.post(f"/v1/hooks/apps/{op}", json=payload,
                           headers={"Authorization": "Bearer dummy"})


def _pin(payload: dict, sid: str = SID):
    return _hook("pin", payload, sid)


def _served(app_id: str, **params) -> str:
    r = client.get(f"/v1/apps/{app_id}/html", params=params)
    assert r.status_code == 200, r.text
    return r.text


# ───────────────────────── the release copy ─────────────────────────────────


def test_pin_cuts_a_release_and_viewers_read_it_not_the_working_file(agent_tree):
    body = _pin({"slug": "sched", "html": "<p>v1</p>"}).json()
    assert body["release"] == 1
    row = task_store.get_app(body["app_id"])
    assert row["release_path"] == f"app-releases/users/alice/{row['id']}/1/index.html"
    copy = agent_tree / row["release_path"]
    assert copy.read_text() == "<p>v1</p>"
    assert row["release_sha256"] == hashlib.sha256(b"<p>v1</p>").hexdigest()
    # No temp file lingers from the atomic write.
    assert not list((agent_tree / "users/alice/workspace/apps").glob("*.tmp"))
    # The agent edits the working file: viewers see the release until the
    # next pin; the owner may preview the edit.
    (agent_tree / row["rel_path"]).write_text("<p>edited</p>")
    assert "<p>v1</p>" in _served(row["id"])
    assert "<p>edited</p>" in _served(row["id"], preview=1)
    again = _pin({"slug": "sched"}).json()  # html-less re-pin deploys the edit
    assert again["release"] == 2
    assert "<p>edited</p>" in _served(row["id"])


def test_shared_rows_release_under_the_shared_bucket_and_preview_is_for_editors(agent_tree):
    body = _pin({"slug": "team", "html": "<p>t1</p>"}, sid=SID_SHARED).json()
    row = task_store.get_app(body["app_id"])
    assert row["release_path"] == f"app-releases/shared/{row['id']}/1/index.html"
    (agent_tree / row["rel_path"]).write_text("<p>draft</p>")
    # A viewer asking for the preview gets the release, silently.
    _as(_user("bob-sub", agent_roles={AGENT: "viewer"}))
    assert "<p>t1</p>" in _served(row["id"], preview=1)
    _as(_user())
    assert "<p>draft</p>" in _served(row["id"], preview=1)


def test_keeps_three_releases_and_rolls_back_one_at_a_time(agent_tree):
    app_id = _pin({"slug": "s", "html": "<p>1</p>"}).json()["app_id"]
    for i in (2, 3, 4):
        assert _pin({"slug": "s", "html": f"<p>{i}</p>"}).json()["release"] == i
    row = task_store.get_app(app_id)
    assert releases.release_numbers(row) == [2, 3, 4]
    listed = client.get(f"/v1/apps?agent={AGENT}").json()["apps"][0]
    assert listed["release"] == 4 and listed["has_previous_release"] is True
    # The menu's Roll back (human) → release 3, then 2, then nothing.
    assert client.post(f"/v1/apps/{app_id}/rollback").json()["release"] == 3
    assert "<p>3</p>" in _served(app_id)
    assert _hook("rollback", {"slug": "s"}).json()["release"] == 2
    assert "<p>2</p>" in _served(app_id)
    r = client.post(f"/v1/apps/{app_id}/rollback")
    assert r.status_code == 409 and "no previous release" in r.json()["detail"]
    assert client.get(f"/v1/apps?agent={AGENT}").json()["apps"][0]["has_previous_release"] is False
    # The next pin cuts 5 from the working file (which still holds 4).
    assert _pin({"slug": "s"}).json()["release"] == 5
    assert "<p>4</p>" in _served(app_id)


def test_rollback_is_human_only_and_needs_manage_authority(agent_tree):
    app_id = _pin({"slug": "s", "html": "<p>1</p>"}, sid=SID_SHARED).json()["app_id"]
    _pin({"slug": "s", "html": "<p>2</p>"}, sid=SID_SHARED)
    _as(_user(is_api_key=True))
    assert client.post(f"/v1/apps/{app_id}/rollback").status_code == 403
    _as(_user("bob-sub", agent_roles={AGENT: "viewer"}))
    assert client.post(f"/v1/apps/{app_id}/rollback").status_code == 403
    _as(_user())
    assert client.post(f"/v1/apps/{app_id}/rollback").status_code == 200


def test_damaged_release_is_refused_and_a_missing_one_falls_back(agent_tree):
    app_id = _pin({"slug": "s", "html": "<p>1</p>"}).json()["app_id"]
    row = task_store.get_app(app_id)
    copy = agent_tree / row["release_path"]
    copy.write_text("<p>tampered</p>")
    r = client.get(f"/v1/apps/{app_id}/html")
    assert r.status_code == 404 and "damaged" in r.text
    copy.unlink()
    # Gone from disk: the working file serves; the boot reconcile clears the
    # pointer and logs it.
    assert "<p>1</p>" in _served(app_id)
    assert releases.reconcile()["cleared"] == 1
    assert task_store.get_app(app_id)["release_path"] == ""


def test_reconcile_reaps_release_dirs_without_a_row(agent_tree):
    app_id = _pin({"slug": "s", "html": "<p>1</p>"}).json()["app_id"]
    orphan = agent_tree / "app-releases" / "users" / "alice" / str(uuid.uuid4()) / "1"
    orphan.mkdir(parents=True)
    (orphan / "index.html").write_text("x")
    assert releases.reconcile()["reaped"] == 1
    assert not orphan.exists()
    assert (agent_tree / task_store.get_app(app_id)["release_path"]).is_file()


def test_rows_from_before_releases_serve_the_working_file(agent_tree):
    row = task_store.upsert_app(AGENT, "alice", "alice-sub", "old", title="Old",
                                rel_path="users/alice/workspace/apps/old.html")
    path = agent_tree / row["rel_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<p>old</p>")
    assert row["release_path"] == ""
    assert "<p>old</p>" in _served(row["id"])
    assert client.get(f"/v1/apps?agent={AGENT}").json()["apps"][0]["release"] == 0


def test_copies_leave_with_the_row(agent_tree, monkeypatch):
    # Hard unpin from the agent.
    app_id = _pin({"slug": "gone", "html": "<p>1</p>"}).json()["app_id"]
    row = task_store.get_app(app_id)
    assert releases.app_release_dir(row).is_dir()
    assert _hook("unpin", {"slug": "gone"}).status_code == 200
    assert not releases.app_release_dir(row).exists()
    # The hidden-row prune: with a bound of one parked row, hiding a second
    # one prunes the older and removes its copies (the store's own binding
    # drives the prune; the facade's drives the pin cap, left alone).
    monkeypatch.setattr("storage.db_apps.MAX_APPS_PER_SCOPE", 1)
    first = task_store.get_app(_pin({"slug": "a", "html": "<p>a</p>"}).json()["app_id"])
    second = task_store.get_app(_pin({"slug": "b", "html": "<p>b</p>"}).json()["app_id"])
    assert client.delete(f"/v1/apps/{first['id']}").status_code == 200
    assert client.delete(f"/v1/apps/{second['id']}").status_code == 200
    assert task_store.get_app(first["id"]) is None
    assert not releases.app_release_dir(first).exists()
    assert releases.app_release_dir(second).is_dir()


def test_deploy_frame_reaches_the_audience_with_the_manifest_signature(agent_tree):
    from services.notifications import notification_manager as nm
    with patch.object(nm, "push_live", return_value=1) as pushed:
        body = _pin({"slug": "live", "html": "<p>1</p>"}).json()
    frames = [c.args[1] for c in pushed.call_args_list if c.args[1].get("type") == "app_deployed"]
    assert [(f["app_id"], f["release"]) for f in frames] == [(body["app_id"], 1)]
    assert frames[0]["actions_sig"] == task_store.manifest_sig(task_store.get_app(body["app_id"]))
    assert pushed.call_args_list[0].args[0] == "alice-sub"


# ───────────────────────── folder releases (APPS.md) ────────────────────────


def _folder_row(slug: str = "board") -> dict:
    return task_store.upsert_app(AGENT, "alice", "alice-sub", slug, title="Board",
                                 rel_path=f"users/alice/workspace/apps/{slug}", kind="folder")


def _tree(root, files: dict[str, str]):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def test_folder_release_copies_the_tree_with_a_manifest_and_verifies_each_file(agent_tree):
    import json
    row = _folder_row()
    src = _tree(agent_tree / row["rel_path"], {
        "app.json": '{"title":"Board"}',
        "client/index.html": "<p>v1</p>",
        "client/app.js": "console.log(1)",
        "server/index.ts": "export {}",
        "data/app.db": "never copied",
        "server/node_modules/x/index.js": "never copied",
        ".env": "never copied",
    })
    rel, sha, n = releases.cut_folder_release(row, src)
    assert (rel, n) == (f"app-releases/users/alice/{row['id']}/1", 1)
    row = task_store.set_app_release(row["id"], rel, sha)
    d = agent_tree / rel
    manifest = json.loads((d / "manifest.json").read_text())
    assert sorted(manifest["files"]) == ["app.json", "client/app.js", "client/index.html",
                                         "server/index.ts"]
    assert sha == hashlib.sha256((d / "manifest.json").read_bytes()).hexdigest()
    # data/ is never copied (the release carries an EMPTY data/ as the
    # mount point of the app's real data); node_modules and dotfiles stay.
    assert (d / "data").is_dir() and not list((d / "data").iterdir())
    assert not (d / "server/node_modules").exists()
    assert not (d / ".env").exists() and not list(d.parent.glob("*.tmp"))
    # Every served file is verified; the manifest is the truth.
    assert releases.read_release(row) == b"<p>v1</p>"
    assert releases.read_release_file(d, "client/app.js") == b"console.log(1)"
    assert releases.read_release_file(d, "client/missing.js") is None
    assert releases.find_release_by_sha(row, sha) == d
    assert releases.find_release_by_sha(row, "0" * 64) is None
    (d / "client/app.js").write_text("tampered")
    with pytest.raises(releases.ReleaseDamaged):
        releases.read_release_file(d, "client/app.js")
    # An identical tree hashes the same (the client routes address a
    # release by content).
    (d / "client/app.js").write_text("console.log(1)")
    rel2, sha2, n2 = releases.cut_folder_release(row, src)
    assert (n2, sha2) == (2, sha)
    assert releases.current_number(row) == 1
    assert releases.previous_number(task_store.set_app_release(row["id"], rel2, sha2)) == 1


def test_folder_releases_keep_three_skip_the_pending_copy_and_reconcile(agent_tree):
    row = _folder_row("k")
    src = _tree(agent_tree / row["rel_path"], {"app.json": "{}", "client/index.html": "<p>1</p>"})
    for i in range(1, 5):
        (src / "client/index.html").write_text(f"<p>{i}</p>")
        rel, sha, n = releases.cut_folder_release(row, src)
        row = task_store.set_app_release(row["id"], rel, sha)
        releases.prune(row)  # a folder prunes when the release goes live
    assert releases.release_numbers(row) == [2, 3, 4]
    # A pending copy (waiting for approval) is neither counted, nor pruned,
    # nor rolled back to.
    (src / "client/index.html").write_text("<p>5</p>")
    rel5, sha5, n5 = releases.cut_folder_release(row, src)
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute("UPDATE pinned_apps SET pending_release=%s WHERE id=%s", (n5, row["id"]))
        conn.commit()
    row = task_store.get_app(row["id"])
    assert n5 == 5 and releases.release_numbers(row) == [2, 3, 4]
    assert releases.previous_number(row) == 3
    assert (releases.app_release_dir(row) / "5" / "manifest.json").is_file()
    # point_to reads an older tree's hash; a pruned number is damage.
    assert releases.point_to(row, 3)[1] == releases.tree_sha(releases.app_release_dir(row) / "3")
    with pytest.raises(releases.ReleaseDamaged):
        releases.point_to(row, 1)
    # Reconcile: a folder row whose manifest is gone falls back to the
    # working tree; the pointer is cleared.
    (releases.app_release_dir(row) / "4" / "manifest.json").unlink()
    assert releases.reconcile()["cleared"] == 1
    assert task_store.get_app(row["id"])["release_path"] == ""


def test_folder_release_refuses_symlinks_and_caps(agent_tree, monkeypatch):
    row = _folder_row("caps")
    src = _tree(agent_tree / row["rel_path"], {"app.json": "{}", "client/index.html": "<p>1</p>"})
    (src / "client" / "link.js").symlink_to("/etc/hostname")
    with pytest.raises(releases.ReleaseInvalid):
        releases.cut_folder_release(row, src)
    (src / "client" / "link.js").unlink()
    monkeypatch.setattr(releases, "MAX_RELEASE_FILES", 2)
    (src / "client" / "extra.js").write_text("x")
    with pytest.raises(releases.ReleaseInvalid):
        releases.cut_folder_release(row, src)
    (src / "client" / "extra.js").unlink()
    monkeypatch.setattr(releases, "MAX_RELEASE_FILE_BYTES", 4)
    with pytest.raises(releases.ReleaseInvalid):
        releases.cut_folder_release(row, src)
    # Nothing was copied for a refused tree.
    assert not releases.app_release_dir(row).exists()
    # The database lives outside the workspace, keyed by slug: never synced,
    # never listed by the files API, metered by the quota bucket.
    from core.remote import file_sync
    from services.infra import storage_quota
    assert releases.app_data_dir(row) == agent_tree / "app-data" / "users" / "alice" / "caps"
    assert releases.log_path(row) == releases.app_release_dir(row) / "app.log"
    assert file_sync.is_platform_only_tree("app-data/users/alice/caps/app.db")
    assert file_sync.should_sync_to_target("app-data/users/alice/caps/app.db", "alice", "manager") is False
    assert releases.data_root(agent_tree, "alice") in storage_quota.user_scope_dirs(AGENT, "alice")


# ───────────────────────── nothing outside the tree (safe_fs) ───────────────


def test_write_atomic_never_writes_through_a_link(agent_tree, tmp_path):
    """The app writers (a pin's working file, ``files.write``) replace the
    name inside the agent's tree and never write through a link: not one
    planted at the old fixed temporary name, not one in place of a parent."""
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("untouched")
    apps = agent_tree / "workspace" / "apps"
    apps.mkdir(parents=True)
    target = apps / "x.html"
    (apps / "x.html.tmp").symlink_to(victim)
    releases.write_atomic(target, "<p>new</p>")
    assert target.read_text() == "<p>new</p>" and victim.read_text() == "untouched"
    (agent_tree / "workspace" / "apps2").symlink_to(outside)
    with pytest.raises(OSError):
        releases.write_atomic(agent_tree / "workspace" / "apps2" / "y.html", "<p>no</p>")
    assert sorted(p.name for p in outside.iterdir()) == ["victim.txt"]


def test_a_file_apps_release_is_never_cut_from_a_link(agent_tree, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET-CONTENT")
    row = task_store.upsert_app(AGENT, "alice", "alice-sub", "linked", title="Linked",
                                rel_path="users/alice/workspace/apps/linked.html")
    working = agent_tree / row["rel_path"]
    working.parent.mkdir(parents=True, exist_ok=True)
    working.symlink_to(secret)
    with pytest.raises(releases.ReleaseInvalid):
        releases.cut_release(row, working)
    assert not releases.app_release_dir(row).exists()


def test_the_walk_keeps_its_answers_and_refuses_a_linked_root(agent_tree, tmp_path):
    outside = tmp_path / "elsewhere"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "x.js").write_text("x")
    (outside / "f.js").write_text("f")
    src = _tree(agent_tree / "workspace" / "apps" / "walk", {
        "app.json": "{}", "client/index.html": "<p>1</p>", ".hidden": "no"})
    (src / "client" / "linked-dir").symlink_to(outside / "sub")      # skipped, as os.walk did
    (src / ".dot-link").symlink_to(outside / "f.js")                 # a dot name: skipped
    assert [rel for rel, _p in releases.walk_tree(src)] == ["app.json", "client/index.html"]
    (src / "client" / "file-link.js").symlink_to(outside / "f.js")   # refused
    with pytest.raises(releases.ReleaseInvalid):
        releases.walk_tree(src)
    (src / "client" / "file-link.js").unlink()
    assert releases.walk_tree(agent_tree / "workspace" / "apps" / "missing") == []
    (agent_tree / "workspace" / "apps" / "via-link").symlink_to(outside)
    with pytest.raises(releases.ReleaseInvalid):
        releases.walk_tree(agent_tree / "workspace" / "apps" / "via-link")


def test_a_release_is_copied_from_the_tree_without_following_a_swap(agent_tree, tmp_path, monkeypatch):
    """The swap-after-check class on the apps side: the walk judged the tree, then a
    file turned into a link before the copy; the copy refuses it and nothing
    from outside lands in a release, the preview slot or a render's copy."""
    from services.apps import app_deploy, app_render
    import asyncio
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET-CONTENT")
    row = _folder_row("swap")
    src = _tree(agent_tree / row["rel_path"], {"app.json": "{}", "client/index.html": "<p>1</p>",
                                                "client/app.js": "console.log(1)"})
    real_walk = releases.walk_tree

    def swapping(source):
        files = real_walk(source)
        target = source / "client" / "app.js"
        if not target.is_symlink():
            target.unlink()
            target.symlink_to(secret)
        return files

    def reset() -> None:
        target = src / "client" / "app.js"
        target.unlink()
        target.write_text("console.log(1)")

    monkeypatch.setattr(releases, "walk_tree", swapping)
    with pytest.raises(releases.ReleaseInvalid):
        releases.cut_folder_release(row, src)
    reset()
    with pytest.raises((releases.ReleaseInvalid, app_deploy.DeployError)):
        asyncio.run(app_deploy.cut_preview(row, src))
    reset()
    with pytest.raises(releases.ReleaseInvalid):
        app_render.copy_working_tree(row, src)
    base = releases.app_release_dir(row)
    for p in base.rglob("*") if base.exists() else []:
        assert not p.is_file() or "SECRET-CONTENT" not in p.read_text(errors="replace"), p


def test_a_file_named_data_at_the_root_is_a_refusal_not_a_server_error(agent_tree):
    src = agent_tree / "workspace" / "apps" / "datafile"
    src.mkdir(parents=True)
    (src / "app.json").write_text('{"title": "T"}')
    (src / "index.html").write_text("<p>x</p>")
    (src / "data").write_text("not a folder")
    dest = agent_tree / "app-releases" / "shared" / "x" / "1"
    with pytest.raises(releases.ReleaseInvalid, match="reserved"):
        releases.copy_tree(src, dest)
