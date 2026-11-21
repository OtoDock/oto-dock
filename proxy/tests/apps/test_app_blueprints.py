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
import zlib
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
from services.apps import (
    app_blueprints,
    app_deploy,
    app_handlers,
    app_supervisor,
    releases,
)
from services.community import (
    community_agent_template_store as tpl,
    template_app_seeder as seeder,
)
from storage import database as task_store
from storage.pg import get_conn
from tests._timing import bound

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


def _template_apps(tdir: Path) -> list:
    """A template folder's apps as the loader reads them: from the template
    schema's document of the folder."""
    from services.community import template_sig
    from storage import catalog_schema
    schema = catalog_schema.load_schema(catalog_schema.TEMPLATE_SCHEMA)
    document, problems = catalog_schema.template_document(tdir, schema, template_sig.read_file)
    assert not problems, problems
    return tpl._discover_apps(tdir, document)


def _bundle(files: dict[str, bytes], blueprint: dict | None, *, link: str | None = None,
            mode: str = "w:gz") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:
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
    # The bundle names its source, which is on this install: the hook's import follows it.
    from services.apps import app_follow
    follow = app_follow.parse(row)
    assert follow and follow["id"] == body["app_id"] and follow["slug"] == "board" and follow["release"] == 1
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


def test_an_overlay_import_keeps_the_approval_the_data_folder_and_the_bound_task(agent_tree):
    """The keywords a release another agent's app sends passes:
    ``voids_approval=False`` keeps the standing approval of an unchanged
    manifest, ``replace=False`` leaves ``data/`` alone, ``keep_tasks`` keeps
    the task a button binds and seeds none for it, ``hold`` parks the
    release."""
    import shutil

    from services.apps import app_follow
    task_store.create_dynamic_task("task-bp-keep", AGENT, "Weekly report", "write it", "cli", "trigger",
                                   None, None, None, 600, AGENT, scope="agent")
    manifest = {"title": "Board", "actions": [
        {"id": "report", "label": "Report", "type": "fire_task", "task_id": "task-bp-keep"}]}
    root = _folder(agent_tree / "workspace" / "apps" / "board", manifest)
    body = _hook("deploy", {"slug": "board", "visibility": "agent"}).json()
    assert client.post(f"/v1/apps/{body['app_id']}/deploy/approve").status_code == 200
    assert _hook("export", {"slug": "board", "visibility": "agent"}).status_code == 200
    folder, bp = app_blueprints.read_bundle((agent_tree / "workspace/apps/board.otoapp").read_bytes())
    (root / "data" / "sub").mkdir()
    (root / "data" / "sub" / "keep.txt").write_text("kept")
    (folder / "client" / "index.html").write_text("<p>v2</p>")
    try:
        out = asyncio.run(app_blueprints.import_folder(
            AGENT, "", None, "board", folder, template_slug="bundle:board", tasks=bp["tasks"],
            voids_approval=False, replace=False, hold=True, keep_tasks={"report": "task-bp-keep"}))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    assert out["status"] == "pending approval" and out["release"] == 2, out
    row = task_store.get_app(body["app_id"])
    assert task_store.app_actions_approved(row)
    assert (root / "client" / "index.html").read_text() == "<p>v2</p>"
    assert (root / "data" / "sub" / "keep.txt").read_text() == "kept"
    assert (root / "data" / "app.db").read_bytes() == b"never travels"
    assert json.loads((root / "app.json").read_text())["actions"][0]["task_id"] == "task-bp-keep"
    assert app_follow._seeded_tasks(AGENT, "board") == set()


def test_an_import_never_pulls_a_bundle_the_session_may_not_write_back(agent_tree, monkeypatch):
    """A viewer reads the shared workspace but may not write it back: the
    bundle on its machine is never pulled into the platform tree, and with
    no platform copy the import says why."""
    from core.remote import remote_file_flow as rff, satellite_connection
    sid = "sess-bp-viewer"
    session_state.set_session_security(sid, SecurityContext(
        role="viewer", username="alice", agent=AGENT, is_admin_agent=False))
    monkeypatch.setattr(rff, "is_remote_session", lambda s: True)
    monkeypatch.setattr(rff, "_get_remote_session_info",
                        lambda s: SimpleNamespace(machine_id="m-1", agent_name=AGENT))

    class _CM:
        async def pull_file_to_path(self, *a, **kw):
            raise AssertionError("a held path never reaches the machine")

    monkeypatch.setattr(satellite_connection, "get_connection_manager", _CM)
    try:
        r = _hook("import", {"path": "workspace/apps/board.otoapp", "visibility": "user"}, sid=sid)
    finally:
        session_state._session_security.pop(sid, None)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == rff.held_reason("workspace/apps/board.otoapp")
    assert not (agent_tree / "workspace" / "apps").exists()


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


def test_a_bundle_task_may_declare_its_mode_and_timeout(agent_tree):
    task_id = app_blueprints._seed_task({"slug": "go", "prompt": "p", "permission_mode": "autoReview",
                                         "timeout_seconds": 1800}, AGENT, AGENT, "agent", "board", "tpl")
    row = task_store.get_dynamic_task(task_id)
    assert (row["permission_mode"], row["timeout_seconds"]) == ("autoReview", 1800)
    # A bundle task with neither field seeds today's row.
    plain = task_store.get_dynamic_task(app_blueprints._seed_task(
        {"slug": "plain", "prompt": "p"}, AGENT, AGENT, "agent", "board", "tpl"))
    assert (plain["permission_mode"], plain["timeout_seconds"]) == ("", 600)


@pytest.mark.parametrize(("field", "value", "words"), [
    ("permission_mode", "bypassPermissions", "permission_mode must be"),
    ("timeout_seconds", 30, "timeout_seconds must be"),
    ("timeout_seconds", "600", "timeout_seconds must be"),
])
def test_a_bundle_task_with_a_bad_field_is_refused_with_the_task_named(agent_tree, field, value, words):
    with pytest.raises(app_blueprints.BlueprintError, match=words):
        app_blueprints._seed_task({"slug": "go", "prompt": "p", field: value}, AGENT, AGENT, "agent", "board", "tpl")


def test_auto_on_an_engine_with_no_reviewer_seeds_dont_ask(agent_tree):
    with patch("services.scheduler.task_fields.mode_problem",
               lambda agent, layer, mode: "no reviewer" if mode else ""):
        task_id = app_blueprints._seed_task({"slug": "go", "prompt": "p", "permission_mode": "autoReview"},
                                            AGENT, AGENT, "agent", "board", "tpl")
    assert task_store.get_dynamic_task(task_id)["permission_mode"] == ""


def test_the_export_carries_a_mode_and_timeout_only_when_the_import_would_take_them(agent_tree):
    actions = [{"id": k, "label": k, "type": "fire_task", "task_id": f"task-exp-{k}"}
               for k in ("auto", "long", "plain")]
    task_store.create_dynamic_task("task-exp-auto", AGENT, "A", "p", "cli", "trigger", None, None, None, 1800, AGENT,
                                   scope="agent", permission_mode="autoReview")
    task_store.create_dynamic_task("task-exp-long", AGENT, "L", "p", "cli", "trigger", None, None, None, 14400, AGENT,
                                   scope="agent")
    task_store.create_dynamic_task("task-exp-plain", AGENT, "P", "p", "cli", "trigger", None, None, None, 600, AGENT,
                                   scope="agent")
    items, _ = app_blueprints._task_items(actions)
    by_slug = {i["slug"]: i for i in items}
    assert by_slug["auto"]["permission_mode"] == "autoReview" and by_slug["auto"]["timeout_seconds"] == 1800
    # A person's long task exports without a bound its own import refuses.
    assert "timeout_seconds" not in by_slug["long"] and "permission_mode" not in by_slug["long"]
    assert set(by_slug["plain"]) == {"slug", "description", "prompt"}


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
    assert time.monotonic() - started < bound(2)


MB = 1024 * 1024


def _gz(*parts: bytes | tuple[bytes, int]) -> bytes:
    """A gzip stream of the parts: bytes, or ``(byte, count)`` for that many
    copies, compressed as they come so the test never holds them whole."""
    z = zlib.compressobj(6, zlib.DEFLATED, 31)
    out = []
    for part in parts:
        if isinstance(part, bytes):
            out.append(z.compress(part))
            continue
        byte, count = part
        block = byte * MB
        while count > 0:
            out.append(z.compress(block[:min(count, MB)]))
            count -= MB
    out.append(z.flush())
    return b"".join(out)


def _big_header(kind: bytes, size: int) -> list[bytes | tuple[bytes, int]]:
    """A pax header of one comment record, or a GNU long name, of ``size``
    bytes, its data really there."""
    if kind == tarfile.GNUTYPE_LONGNAME:
        length, head, tail = size + 1, b"", b"\0"
    else:
        body = len(" comment=") + size + 1
        length = body + len(str(body + len(str(body))))
        head, tail = f"{length} comment=".encode(), b"\n"
    info = tarfile.TarInfo("big")
    info.type = kind
    info.size = length
    return [info.tobuf(tarfile.USTAR_FORMAT), head, (b"a", size), tail + b"\0" * (-length % tarfile.BLOCKSIZE)]


@pytest.mark.parametrize("kind", [tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME])
def test_a_bundle_whose_header_unpacks_past_the_cap_is_refused_before_it_is_read_whole(kind):
    # tarfile reads a pax or GNU long name header whole when its data is
    # really there: 48 MB of it in a bundle of 50 KB.
    import tracemalloc
    data = _gz(*_big_header(kind, 48 * MB), _bundle(MINIMAL, BP, mode="w"))
    assert len(data) < MB
    tracemalloc.start()
    try:
        with pytest.raises(app_blueprints.BlueprintError, match="the bundle exceeds 32 MB or 500 files"):
            app_blueprints.read_bundle(data)
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 64 * MB, peak
    # The most an export holds (32 MB of files) still reads.
    files = {**MINIMAL, **{f"client/f{i}.bin": b"\0" * (2 * MB) for i in range(15)}}
    files["client/last.bin"] = b"\0" * (2 * MB - sum(map(len, MINIMAL.values())) - len(json.dumps(BP)))
    folder, bp = app_blueprints.read_bundle(_gz(_bundle(files, BP, mode="w")))
    try:
        assert bp == BP and (folder / "client" / "last.bin").stat().st_size > MB
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)


def test_a_bundle_cut_inside_a_member_is_refused_as_not_an_archive():
    plain = _bundle(MINIMAL, BP, mode="w")
    with tarfile.open(fileobj=io.BytesIO(plain)) as tar:
        last = tar.getmembers()[-1]
    data = _gz(plain[:last.offset_data + 2])
    with pytest.raises(app_blueprints.BlueprintError, match="not a .otoapp archive"):
        app_blueprints.read_bundle(data)


def test_a_template_ships_apps_that_land_pending_with_their_needs(agent_tree, tmp_path):
    tdir = tmp_path / "template"
    _folder(tdir / "apps" / "board", {"title": "Board", "requires": {"mcps": ["github-mcp"]},
                                       "actions": [{"id": "go", "label": "Go", "type": "fire_task",
                                                    "task": "report"}]})
    (tdir / "apps" / "board" / "blueprint.json").write_text(json.dumps(
        {"format": 1, "slug": "board", "tasks": [{"slug": "report", "description": "R", "prompt": "do it"}]}))
    (tdir / "apps" / "notes").mkdir(parents=True)          # no app.json: not an app
    items = _template_apps(tdir)
    assert [i.slug for i in items] == ["board"] and items[0].requires_mcps == ["github-mcp"]
    with pytest.raises(tpl.TemplateValidationError):
        (tdir / "apps" / "Bad Name").mkdir()
        (tdir / "apps" / "Bad Name" / "app.json").write_text("{}")
        _template_apps(tdir)
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
    items = _template_apps(tdir)
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
    items = _template_apps(tdir)
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
    changed = SimpleNamespace(slug="ops-team", apps=_template_apps(tdir))
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


def test_a_template_copy_makes_each_members_folder_and_an_export_carries_the_key(agent_tree, tmp_path):
    from services.community import template_sig
    from storage.agents import agent_store
    agent_store.create_agent(AGENT, "Blueprint", collaborative=True)
    task_store.upsert_user("alice-sub", "alice-sub@test.com", "Alice", "admin")
    task_store.upsert_user("bob-sub", "bob-sub@test.com", "Bob", "member")
    with get_conn() as conn:
        conn.execute("UPDATE users SET username=%s WHERE sub=%s", ("bob", "bob-sub"))
        conn.commit()
    task_store.add_user_agent("bob-sub", AGENT, "viewer", "test")
    (agent_tree / "users" / "bob" / "workspace").mkdir(parents=True, exist_ok=True)
    manifest = {"title": "Timeline", "files": {"read": ["workspace/videos/projects/"],
                                               "create": ["workspace/videos/projects/"]},
                "actions": [{"id": "list", "label": "List", "type": "platform", "method": "files.list"}]}
    # Making an empty folder in a member's own workspace does not act as the
    # member: an admin's consent covers every member's copy.
    assert template_sig.needs_owner(manifest) is False
    tdir = tmp_path / "template"
    _folder(tdir / "user-apps" / "timeline", manifest)
    items = _template_apps(tdir)
    assert items[0].owner_approval is False
    seeder.write_seed_source(AGENT, items[0])
    agent_store.set_community_template_data(AGENT, {
        "slug": "video-team", "version": "1.0.0", "tasks": [], "triggers": [], "notifications": [],
        "dashboards": [], "user_apps": [{"slug": "timeline", "sig": items[0].sig, "tree_sha": items[0].tree_sha}],
        "consent": {"apps": {"timeline": {"sig": items[0].sig, "by": "alice-sub", "at": "now"}}}})
    for sub, name in (("alice-sub", "alice"), ("bob-sub", "bob")):
        assert asyncio.run(seeder.seed_user_copy(AGENT, sub, "timeline")) == ("seeded", "release 1")
        assert (agent_tree / "users" / name / "workspace/videos/projects").is_dir(), name
    # The export carries the key and never the folder's files.
    (agent_tree / "users/alice/workspace/videos/projects/cut.vproj.json").write_text("{}")
    r = _hook("export", {"slug": "timeline", "visibility": "user"}, sid=SID)
    assert r.status_code == 200, r.text
    bundle = next((agent_tree / "users/alice/workspace/apps").glob("timeline.otoapp"))
    folder, _bp = app_blueprints.read_bundle(bundle.read_bytes())
    try:
        doc = json.loads((folder / "app.json").read_text())
        assert doc["files"]["create"] == ["workspace/videos/projects/"]
        assert not [p for p in folder.rglob("*") if p.name == "cut.vproj.json"]
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)


SERVER = 'Bun.serve({ port: Number(process.env.PORT), hostname: "0.0.0.0", fetch: () => new Response("ok") });\n'


def _template_copy(agent_tree: Path, tmp_path: Path, *, server: bool, seed: bool = True,
                   record_tree: bool = True, manifest: dict | None = None, consent: bool = True) -> list:
    """One per-user template app, its seed source written and consented by
    its owner, seeded for alice unless ``seed`` is False; ``record_tree``
    False leaves the record's ``tree_sha`` out, ``consent`` False records
    none."""
    from storage.agents import agent_store
    agent_store.create_agent(AGENT, "Blueprint", collaborative=True)
    tdir = tmp_path / "template"
    home = _folder(tdir / "user-apps" / "home", manifest if manifest is not None else {"title": "Home", "actions": [
        {"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}]})
    if server:
        (home / "server").mkdir()
        (home / "server" / "index.ts").write_text(SERVER)
    items = _template_apps(tdir)
    seeder.write_seed_source(AGENT, items[0])
    agent_store.set_community_template_data(AGENT, {
        "slug": "ops-team", "version": "1.0.0", "tasks": [], "triggers": [], "notifications": [],
        "dashboards": [], "user_apps": [{"slug": "home", "sig": items[0].sig,
                                         **({"tree_sha": items[0].tree_sha} if record_tree else {})}],
        "consent": {"apps": {"home": {"sig": items[0].sig, "by": "alice-sub", "at": "now"}} if consent else {}}})
    if seed:
        assert asyncio.run(seeder.seed_user_copy(AGENT, "alice-sub", "home")) == ("seeded", "release 1")
    return items


def test_a_template_copy_unpinned_and_given_a_server_waits_for_a_person(agent_tree, tmp_path, placed, healthy):
    """The agent's hard unpin clears the copy's live release and keeps its
    approved manifest; a server added to the working folder and deployed
    again is still a server the app gains, so it waits."""
    _template_copy(agent_tree, tmp_path, server=False)
    assert _hook("unpin", {"slug": "home", "visibility": "user"}, sid=SID).json()["status"] == "ok"
    parked = task_store.get_app_by_slug(AGENT, "alice", "home")
    assert parked["template_state"] == "opted_out" and not parked["release_path"]
    assert task_store.app_actions_approved(parked)
    work = agent_tree / "users/alice/workspace/apps/home"
    (work / "server").mkdir()
    (work / "server" / "index.ts").write_text(SERVER)
    out = _hook("pin", {"slug": "home", "visibility": "user"}, sid=SID).json()
    assert out["status"] == "pending approval" and out["adds_server"] is True, out
    assert not placed.of("app_server_start")


def test_the_update_dialog_reads_whether_the_installed_version_runs_a_server(agent_tree, tmp_path):
    """A shared app by its live release, a per-user app by the template's
    stored copy, never through a link: what cannot be read counts as none."""
    import os
    import shutil

    from services.community import community_agent_updater as upd
    tdir = tmp_path / "template"
    board = _folder(tdir / "apps" / "board", {"title": "Board"})
    (board / "server").mkdir()
    (board / "server" / "index.ts").write_text(SERVER)
    items = _template_apps(tdir)
    task_store.upsert_user("alice-sub", "alice-sub@test.com", "Alice", "admin")
    assert upd._installed_runs_server(AGENT, items[0]) is False
    data = {"consent": {"apps": {"board": {"sig": items[0].sig, "by": "alice-sub", "at": "now"}}}}
    assert asyncio.run(seeder.seed_shared(AGENT, SimpleNamespace(slug="ops-team", apps=items), data))["seeded"]
    assert upd._installed_runs_server(AGENT, items[0]) is True
    (home,) = _template_copy(agent_tree, tmp_path / "user", server=True, seed=False)
    assert upd._installed_runs_server(AGENT, home) is True
    source = seeder.seed_source_dir(AGENT, "home")
    aside = tmp_path / "aside"
    source.rename(aside)
    os.symlink(aside, source)
    assert upd._installed_runs_server(AGENT, home) is False
    source.unlink()
    aside.rename(source)
    shutil.rmtree(source / "server")
    assert upd._installed_runs_server(AGENT, home) is False


def test_a_consent_says_the_server_only_where_the_dialog_showed_one_and_the_app_has_it(agent_tree, tmp_path):
    """``record_consent`` flags an app's server for a slug the dialog listed
    whose folder holds one; ``consent_reach`` hands the flag on, and nothing
    for a copy its owner opted out of."""
    tdir = tmp_path / "template"
    board = _folder(tdir / "apps" / "board", {"title": "Board"})
    (board / "server").mkdir()
    (board / "server" / "index.ts").write_text(SERVER)
    _folder(tdir / "apps" / "page", {"title": "Page"})
    items = _template_apps(tdir)
    template = SimpleNamespace(apps=items, checks=[])
    sigs = {i.slug: i.sig for i in items}
    data: dict = {}
    seeder.record_consent(data, template, sigs, None, "alice-sub", servers=["board", "page"])
    assert data["consent"]["apps"]["board"]["server"] is True
    assert "server" not in data["consent"]["apps"]["page"]
    seeder.record_consent(data, template, sigs, None, "alice-sub")
    assert "server" not in data["consent"]["apps"]["board"]
    board_item = next(i for i in items if i.slug == "board")
    flagged = {"sig": board_item.sig, "by": "alice-sub", "server": True}
    assert seeder.consent_reach(board_item, flagged, None) == {"consent_tree": board_item.tree_sha,
                                                               "consent_server": True}
    assert seeder.consent_reach(board_item, flagged, {"template_state": "opted_out"}) == {
        "consent_tree": "", "consent_server": False}


def test_a_consented_restore_of_a_template_copy_with_a_server_waits_for_a_person(agent_tree, tmp_path):
    """A purged copy brought back from the template's own copy brings back
    an app its owner removed: the consent still approves its manifest, and
    its server waits on the card, with nothing started."""
    _template_copy(agent_tree, tmp_path, server=True)
    from services.apps import app_lifecycle
    row = task_store.get_app_by_slug(AGENT, "alice", "home")
    assert asyncio.run(app_lifecycle.purge(row))["opted_out"] is True
    with patch.object(app_supervisor, "start", side_effect=AssertionError("a restore never starts a server")):
        out = _hook("pin", {"slug": "home", "visibility": "user"}, sid=SID).json()
    assert out["status"] == "pending approval" and out["adds_server"] is True and out["restored"], out
    back = task_store.get_app_by_slug(AGENT, "alice", "home")
    assert task_store.app_actions_approved(back) and not back["release_path"] and back["pending_release"] == 1


def test_a_first_copy_no_consent_covers_waits_for_its_server_with_nothing_else_to_approve(agent_tree, tmp_path):
    """A member's first copy of a template app with an empty manifest and a
    server, seeded with no consent: nothing else is to approve, and the
    server waits all the same."""
    _template_copy(agent_tree, tmp_path, server=True, seed=False, manifest={"title": "Home"}, consent=False)
    with patch.object(app_supervisor, "start", side_effect=AssertionError("nothing starts")):
        status, _why = asyncio.run(seeder.seed_user_copy(AGENT, "alice-sub", "home"))
    assert status == "pending", status
    row = task_store.get_app_by_slug(AGENT, "alice", "home")
    assert task_store.app_actions_approved(row) and row["pending_release"] == 1
    assert app_deploy.status(row)["adds_server"] is True


def test_an_import_of_a_new_app_with_a_server_waits_with_nothing_else_to_approve(agent_tree, tmp_path):
    """A bundle imported as a new app (the import tool, and a share to
    another agent) is a first release: its server waits on the card."""
    folder = _folder(tmp_path / "bundle", {"title": "Board"})
    (folder / "server").mkdir()
    (folder / "server" / "index.ts").write_text(SERVER)
    with patch.object(app_supervisor, "start", side_effect=AssertionError("nothing starts")):
        out = asyncio.run(app_blueprints.import_folder(AGENT, "", None, "board", folder,
                                                       template_slug="bundle:board", tasks=[]))
    assert out["status"] == "pending approval" and out["adds_server"] is True, out
    row = task_store.get_app_by_slug(AGENT, "", "board")
    assert task_store.app_actions_approved(row) and row["pending_release"] == 1


def test_a_consented_template_update_that_adds_a_server_waits_for_a_person(agent_tree, tmp_path):
    """A template's new version whose shared app gains a server: the
    consent approves its manifest, never the release past the waiting line,
    and nothing starts."""
    tdir = tmp_path / "template"
    board = _folder(tdir / "apps" / "board", {"title": "Board", "actions": [
        {"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}]})
    items = _template_apps(tdir)
    task_store.upsert_user("alice-sub", "alice-sub@test.com", "Alice", "admin")
    data = {"consent": {"apps": {"board": {"sig": items[0].sig, "by": "alice-sub", "at": "now"}}}}
    seeded = asyncio.run(seeder.seed_shared(AGENT, SimpleNamespace(slug="ops-team", apps=items), data))
    assert seeded["seeded"] == ["board"], seeded
    (board / "server").mkdir()
    (board / "server" / "index.ts").write_text(SERVER)
    newer = _template_apps(tdir)
    data = {"consent": {"apps": {"board": {"sig": newer[0].sig, "by": "alice-sub", "at": "now"}}}}
    with patch.object(app_supervisor, "start", side_effect=AssertionError("an update never starts a server")):
        seeded = asyncio.run(seeder.seed_shared(AGENT, SimpleNamespace(slug="ops-team", apps=newer), data))
    assert [p["slug"] for p in seeded["pending"]] == ["board"], seeded
    row = task_store.get_app_by_slug(AGENT, "", "board")
    assert row["pending_release"] == 2 and task_store.app_actions_approved(row)
    assert app_deploy.status(row)["adds_server"] is True


HANDLER_SERVER = (
    'Bun.serve({ port: Number(process.env.PORT), hostname: "0.0.0.0", fetch(req) {\n'
    '  const u = new URL(req.url);\n'
    '  if (u.pathname === "/_health") return new Response("ok");\n'
    '  if (u.pathname === "/_handler/ping") return new Response("pong");\n'
    '  return new Response("no", { status: 404 });\n'
    '} });\n'
)


class _Answer:
    status_code = 200

    async def aiter_bytes(self):
        yield b"pong"

    async def aclose(self):
        return None


def test_a_wake_held_while_an_update_waits_for_its_server_is_delivered_after_the_approval(
        agent_tree, tmp_path, placed, healthy, monkeypatch):
    """A consented update that brings a server and a trigger handler waits
    with its manifest approved: a wake in the wait is held as an unapproved
    manifest's is, never dead, and the same delivery reaches the server
    once a person approved the release."""
    from storage import db_app_deliveries as deliveries
    me = {"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}
    tdir = tmp_path / "template"
    board = _folder(tdir / "apps" / "board", {"title": "Board", "actions": [me]})
    items = _template_apps(tdir)
    task_store.upsert_user("alice-sub", "alice-sub@test.com", "Alice", "admin")
    data = {"consent": {"apps": {"board": {"sig": items[0].sig, "by": "alice-sub", "at": "now"}}}}
    assert asyncio.run(seeder.seed_shared(AGENT, SimpleNamespace(slug="ops-team", apps=items), data))["seeded"]
    (board / "app.json").write_text(json.dumps({"title": "Board", "actions": [me],
                                                "handlers": {"on_trigger": ["ping"]}}))
    (board / "server").mkdir()
    (board / "server" / "index.ts").write_text(HANDLER_SERVER)
    newer = _template_apps(tdir)
    data = {"consent": {"apps": {"board": {"sig": newer[0].sig, "by": "alice-sub", "at": "now"}}}}
    seeded = asyncio.run(seeder.seed_shared(AGENT, SimpleNamespace(slug="ops-team", apps=newer), data))
    assert [p["slug"] for p in seeded["pending"]] == ["board"], seeded
    row = task_store.get_app_by_slug(AGENT, "", "board")
    assert task_store.app_actions_approved(row) and row["pending_release"] == 2
    sent: list[str] = []

    async def forward(inst, method, path, headers, body, *, timeout):
        sent.append(path)
        return _Answer()

    monkeypatch.setattr(app_supervisor, "forward", forward)

    async def run() -> None:
        d = await app_handlers.enqueue(row, "ping", "trigger:t", {})
        await app_handlers._fire(deliveries.get(d["id"]))
        held = deliveries.get(d["id"])
        assert held["status"] == deliveries.PENDING, held
        assert held["last_error"] == "the app is waiting for approval" and not sent
        await app_deploy.approve_pending(task_store.get_app(row["id"]), "alice-sub")
        await app_handlers._fire(deliveries.get(d["id"]))
        assert deliveries.get(d["id"])["status"] == deliveries.DONE and sent == ["/_handler/ping"]
        await app_supervisor.stop_all()

    asyncio.run(run())


def _server_at_push(monkeypatch) -> None:
    """The folder the import just wrote gains a server before the deploy
    cuts it: written where the import hands the folder to paired machines."""
    from services.infra import file_bookkeeping

    async def push(agent, root, agent_dir, **kw):
        (Path(root) / "server").mkdir(exist_ok=True)
        (Path(root) / "server" / "index.ts").write_text(SERVER)

    monkeypatch.setattr(file_bookkeeping, "push_tree_write", push)


def test_a_consented_restore_whose_folder_gains_a_server_before_the_cut_waits(agent_tree, tmp_path, monkeypatch):
    """The consent covers the template's tree, not what the folder holds at
    the cut: a restore whose cut brings a server the record does not name
    waits for a person, and nothing starts."""
    _template_copy(agent_tree, tmp_path, server=False)
    from services.apps import app_lifecycle
    asyncio.run(app_lifecycle.purge(task_store.get_app_by_slug(AGENT, "alice", "home")))
    _server_at_push(monkeypatch)
    with patch.object(app_supervisor, "start", side_effect=AssertionError("nothing starts")):
        out = _hook("pin", {"slug": "home", "visibility": "user"}, sid=SID).json()
    assert out["status"] == "pending approval" and out["adds_server"] is True, out


def test_a_consented_first_seed_whose_folder_gains_a_server_before_the_cut_waits(agent_tree, tmp_path, monkeypatch):
    _template_copy(agent_tree, tmp_path, server=False, seed=False)
    _server_at_push(monkeypatch)
    with patch.object(app_supervisor, "start", side_effect=AssertionError("nothing starts")):
        status, _why = asyncio.run(seeder.seed_user_copy(AGENT, "alice-sub", "home"))
    assert status == "pending", status
    row = task_store.get_app_by_slug(AGENT, "alice", "home")
    assert row["pending_release"] == 1 and app_deploy.status(row)["adds_server"] is True


def test_a_record_with_no_tree_hash_covers_no_server(agent_tree, tmp_path):
    """A consent whose record names no tree covers no server: the seed of a
    template app with one waits once on a card. Every record a released
    install holds carries the hash beside its signature."""
    _template_copy(agent_tree, tmp_path, server=True, seed=False, record_tree=False)
    with patch.object(app_supervisor, "start", side_effect=AssertionError("nothing starts")):
        status, _why = asyncio.run(seeder.seed_user_copy(AGENT, "alice-sub", "home"))
    assert status == "pending", status


def test_a_restore_no_consent_covers_that_brings_a_server_waits(agent_tree, tmp_path):
    """The stored copy changed, so no consent covers the restore: the row
    kept its approval through the opt-out, and only the server rule makes
    the release wait."""
    _template_copy(agent_tree, tmp_path, server=False)
    from services.apps import app_lifecycle
    asyncio.run(app_lifecycle.purge(task_store.get_app_by_slug(AGENT, "alice", "home")))
    source = seeder.seed_source_dir(AGENT, "home")
    (source / "server").mkdir()
    (source / "server" / "index.ts").write_text(SERVER)
    with patch.object(app_supervisor, "start", side_effect=AssertionError("nothing starts")):
        out = _hook("pin", {"slug": "home", "visibility": "user"}, sid=SID).json()
    assert out["status"] == "pending approval" and out["adds_server"] is True, out


def test_an_honest_shared_seed_with_a_server_goes_live_with_no_card(agent_tree, tmp_path):
    """The consented first seed of a shared template app whose cut is the
    tree the consent names goes live, cold, as before."""
    tdir = tmp_path / "template"
    board = _folder(tdir / "apps" / "board", {"title": "Board", "actions": [
        {"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}]})
    (board / "server").mkdir()
    (board / "server" / "index.ts").write_text(SERVER)
    items = _template_apps(tdir)
    task_store.upsert_user("alice-sub", "alice-sub@test.com", "Alice", "admin")
    data = {"consent": {"apps": {"board": {"sig": items[0].sig, "by": "alice-sub", "at": "now"}}}}
    with patch.object(app_supervisor, "start", side_effect=AssertionError("a seed never starts a server")):
        seeded = asyncio.run(seeder.seed_shared(AGENT, SimpleNamespace(slug="ops-team", apps=items), data))
    assert seeded["seeded"] == ["board"], seeded
    row = task_store.get_app_by_slug(AGENT, "", "board")
    assert row["release_path"] and not row["pending_release"]
    assert releases.code_sha(releases.app_release_dir(row) / "1") == items[0].tree_sha
