"""Deploying folder apps (APPS.md "Deploy pipeline").

Load-bearing: app.json is validated as a whole (the handlers, exports,
bindings and requires blocks by their shapes, egress public only, file
prefixes by the rules) and every block lands on the row; a deploy cuts a release
and viewers are served it; a manifest that is not approved (or the
per-app switch) parks the release as pending until a human approves it,
which also approves the manifest; a rejected release restores the live
manifest; a viewer's session cannot deploy a shared app; a broken server
leaves the previous release running; rollback restores the database copy
and keeps a snapshot of the current one; pin_app on a folder deploys.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.path_policy import SecurityContext
from auth.providers import UserContext, get_current_user
from core.session import session_state
from services.apps import app_deploy, app_sandbox, app_supervisor, releases
from storage import database as task_store

client = TestClient(app)

AGENT = "deploy-agent"
SID = "sess-deploy-alice"
SID_SHARED = "sess-deploy-shared"
SID_VIEWER = "sess-deploy-bob"

_needs_runtime = pytest.mark.skipif(
    not (shutil.which("pasta") and shutil.which("bwrap") and app_sandbox.bun_binary()),
    reason="requires pasta + bwrap + Bun on this host",
)

SERVER_OK = """
const port = Number(process.env.PORT || 3000);
Bun.serve({ port, hostname: "0.0.0.0", fetch(req) {
  const u = new URL(req.url);
  if (u.pathname === "/_health") return new Response("ok");
  return new Response("v" + (process.env.APP_V || "1"));
}});
"""
SERVER_BROKEN = 'console.log("boom"); process.exit(5);\n'


def _user(sub: str = "alice-sub", agent_roles: dict[str, str] | None = None,
          is_api_key: bool = False) -> UserContext:
    roles = {AGENT: "manager"} if agent_roles is None else agent_roles
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                       agents=list(roles.keys()), is_api_key=is_api_key, agent_roles=roles)


ALICE = _user()
BOB = _user("bob-sub", {AGENT: "viewer"})


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _clean():
    from api.apps import apps as apps_api
    _as(ALICE)
    apps_api._fire_rate.clear()
    yield
    app.dependency_overrides.pop(get_current_user, None)
    import contextlib
    import os
    import signal
    for inst in list(app_supervisor._instances.values()):
        proc = inst.proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    (agents_root / AGENT / "users" / "bob" / "workspace").mkdir(parents=True)
    (agents_root / AGENT / "workspace").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents_root)
    monkeypatch.setattr("auth.path_policy._AGENTS_DIR", agents_root.resolve())
    from storage.pg import get_conn
    for sub, name in (("alice-sub", "alice"), ("bob-sub", "bob")):
        task_store.upsert_user(sub, f"{sub}@test.com", name.title(), "member")
        with get_conn() as conn:
            conn.execute("UPDATE users SET username=%s WHERE sub=%s", (name, sub))
            conn.commit()
    task_store.add_user_agent("alice-sub", AGENT, "manager", "test")
    task_store.add_user_agent("bob-sub", AGENT, "viewer", "test")
    session_state.set_session_security(SID, SecurityContext(
        role="manager", username="alice", agent=AGENT, is_admin_agent=False))
    session_state.set_session_security(SID_SHARED, SecurityContext(
        role="manager", username="", agent=AGENT, is_admin_agent=False))
    session_state.set_session_security(SID_VIEWER, SecurityContext(
        role="viewer", username="bob", agent=AGENT, is_admin_agent=False,
        session_scope="agent"))
    yield agents_root / AGENT
    for sid in (SID, SID_SHARED, SID_VIEWER):
        session_state._session_security.pop(sid, None)


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


def _folder(agent_tree: Path, slug: str = "board", *, username: str = "alice",
            manifest: dict | None = None, html: str = "<p>v1</p>", server: str | None = None) -> Path:
    root = agent_tree / (f"users/{username}/workspace" if username else "workspace") / "apps" / slug
    files = {"app.json": json.dumps(manifest if manifest is not None else {"title": "Board"}),
             "client/index.html": html}
    if server is not None:
        files["server/index.ts"] = server
    return _write(root, files)


def _served(app_id: str, **params) -> str:
    r = client.get(f"/v1/apps/{app_id}/html", params=params)
    assert r.status_code == 200, r.text
    return r.text


# ── app.json ────────────────────────────────────────────────────────────────


def test_app_json_is_validated_as_a_whole(agent_tree):
    v = lambda doc, shared=False: app_deploy.validate_app_json(doc, AGENT, shared)  # noqa: E731
    m = v({"title": "Board", "actions": [], "files": {"read": ["workspace/notes"]},
           "egress": ["api.example.com", "93.184.216.34"]})
    assert m.title == "Board" and m.actions_json == "[]"
    assert json.loads(m.files_json) == {"read": ["users/{owner}/workspace/notes"], "write": []}
    assert json.loads(m.egress_json) == ["api.example.com", "93.184.216.34"]
    assert v({"files": {"read": ["knowledge/docs"]}}, shared=True).files_json.startswith('{"read":["knowledge/docs"]')
    for bad in (
        {"catalog": {"feeds": ["sessions"]}},
        {"nope": 1},
        {"egress": ["10.0.0.5"]},
        {"egress": ["127.0.0.1"]},
        {"egress": ["*.example.com"]},
        {"egress": ["localhost"]},
        {"egress": ["example.com:443"]},
        {"files": {"read": ["../x"]}},
        {"files": {"read": ["config/x"]}},
        {"files": {"read": ["workspace/.credentials/x"]}},
        {"files": {"read": ["workspace/apps/board/data"]}},
        {"files": {"write": ["knowledge/docs"]}},
        {"files": {"read": [f"workspace/{i}" for i in range(9)]}},
        {"files": {"read": ["knowledge/docs"]}},   # personal: the owner's tree only
        {"deploy_requires_approval": "yes"},
        {"actions": [{"id": "x"}]},
        {"title": "x" * 201},
    ):
        with pytest.raises(app_deploy.DeployError):
            v(bad)
    with pytest.raises(app_deploy.DeployError):
        app_deploy.read_app_json(agent_tree)  # no app.json there


BLOCKS = {
    "bindings": [{"name": "crm", "agent": "sales", "app": "leads"}],
    "handlers": {
        "on_schedule": {"digest": {"cron": "0 7 * * 1-5"}},
        "on_trigger": ["github"],
        "on_event": {"refresh": ["task_finished", "app:crm:lead-created"]},
    },
    "exports": {
        "methods": {"summary": {"description": "the week in numbers", "min_role": "editor"}},
        "snapshots": {"board": {"description": "the board as JSON"}},
        "events": {"lead-created": {"description": "a lead landed"}},
    },
    "requires": {"mcps": ["github-mcp"], "providers": ["github"]},
}


def test_the_manifest_blocks_are_validated_signed_and_carried(agent_tree):
    v = lambda doc, shared=True: app_deploy.validate_app_json(doc, AGENT, shared)  # noqa: E731
    m = v({"title": "Flow", **BLOCKS})
    for name in BLOCKS:
        assert json.loads(getattr(m, f"{name}_json")) == BLOCKS[name], name
    assert m.blocks()["requires"] == m.requires_json
    # Empty blocks canonicalise to nothing (the signature keeps the list form).
    e = v({"handlers": {}, "exports": {}, "bindings": [], "requires": {}})
    assert (e.handlers_json, e.exports_json, e.bindings_json, e.requires_json) == ("", "", "", "")
    for bad in (
        {"handlers": [{"on_schedule": "* * * * *"}]},
        {"handlers": {"on_schedule": {"Digest": {"cron": "0 7 * * *"}}}},
        {"handlers": {"on_schedule": {"digest": {"cron": "not a cron"}}}},
        {"handlers": {"on_schedule": {"digest": "0 7 * * *"}}},
        {"handlers": {"on_schedule": {"a": {"cron": "* * * * *"}}, "on_trigger": ["a"]}},
        {"handlers": {"on_event": {"r": ["nope"]}}},
        {"handlers": {"on_event": {"r": ["app:crm:x"]}}},            # no such binding
        {"handlers": {"on_event": {"r": []}}},
        {"handlers": {"on_trigger": [f"h{i}" for i in range(17)]}},
        {"handlers": {"on_event": {"r": ["notification_created"]}}},  # shared: never an inbox
        {"exports": {"x": {}}},
        {"exports": {"methods": {"m": {}}}},
        {"exports": {"methods": {"m": {"description": "x", "min_role": "viewer"}}}},
        {"exports": {"events": {"e": {"description": "x", "min_role": "editor"}}}},
        {"exports": {"methods": {"M": {"description": "x"}}}},
        {"bindings": [{"name": "x"}]},
        {"bindings": [{"name": "x", "agent": "A", "app": "b"}]},
        {"bindings": [{"name": "x", "agent": "a", "app": "b"}, {"name": "x", "agent": "c", "app": "d"}]},
        {"requires": {"tools": []}},
        {"requires": {"mcps": [""]}},
        {"requires": {"mcps": [f"m{i}" for i in range(17)]}},
    ):
        with pytest.raises(app_deploy.DeployError):
            v(bad)
    # A personal app may wake on its owner's notifications.
    assert "notification_created" in v({"handlers": {"on_event": {"r": ["notification_created"]}}},
                                       shared=False).handlers_json
    # Deployed: `requires` lands on the row without touching the signature
    # (an empty manifest goes live at once); the three signed blocks park
    # the release as pending; the list route carries every block and says
    # the manifest is not empty; the approval can be voided outright.
    _folder(agent_tree, "flow", username="", manifest={"title": "Flow", "requires": BLOCKS["requires"]})
    body = _hook("deploy", {"slug": "flow", "visibility": "agent"}, sid=SID_SHARED).json()
    assert body["status"] == "ok", body
    row = task_store.get_app(body["app_id"])
    assert json.loads(row["requires"]) == BLOCKS["requires"]
    assert task_store.canonical_manifest(row) == "[]" and task_store.app_actions_approved(row)
    (agent_tree / "workspace/apps/flow/app.json").write_text(json.dumps({"title": "Flow", **BLOCKS}))
    body = _hook("deploy", {"slug": "flow", "visibility": "agent"}, sid=SID_SHARED).json()
    assert body["status"] == "pending approval" and body["manifest_changed"] is True, body
    row = task_store.get_app(row["id"])
    assert not task_store.app_actions_approved(row)
    assert set(json.loads(task_store.canonical_manifest(row))) == {"actions", "handlers", "exports", "bindings"}
    _as(ALICE)
    listed = next(a for a in client.get(f"/v1/apps?agent={AGENT}").json()["apps"] if a["id"] == row["id"])
    assert listed["manifest_empty"] is False and listed["handlers"] == BLOCKS["handlers"]
    assert listed["exports"] == BLOCKS["exports"] and listed["bindings"] == BLOCKS["bindings"]
    assert listed["requires"] == BLOCKS["requires"]
    assert listed["requires_status"]["mcps"] == [{"name": "github-mcp", "assigned": False}]
    assert listed["requires_status"]["providers"] == [
        {"provider": "github", "mcp": "", "connected": False, "account": "", "identity": ""}]
    assert client.post(f"/v1/apps/{row['id']}/deploy/approve").status_code == 200
    row = task_store.get_app(row["id"])
    assert task_store.app_actions_approved(row)
    task_store.clear_app_approval(row["id"])
    row = task_store.get_app(row["id"])
    assert row["actions_approved_sig"] == "" and not task_store.app_actions_approved(row)


def test_the_shipped_templates_pass_the_validator_and_serve_what_they_declare():
    """The app-authoring templates are copied verbatim by authors and by
    community agents, so a block that no longer validates would ship as a
    broken app.json. Every template's manifest passes, and a declared
    handler has a route in the server that answers it."""
    root = config.MCPS_DIR / "custom" / "display-mcp" / "skills" / "app-authoring" / "templates"
    templates = sorted(p for p in root.iterdir() if p.is_dir())
    assert templates, root
    for tpl in templates:
        doc = json.loads((tpl / "app.json").read_text())
        m = app_deploy.validate_app_json(doc, AGENT, True)
        assert (tpl / "client" / "index.html").is_file(), tpl
        handlers = json.loads(m.handlers_json) if m.handlers_json else {}
        inbound = json.loads(m.inbound_json) if m.inbound_json else {}
        names = (list(handlers.get("on_trigger") or []) + list(handlers.get("on_schedule") or [])
                 + [spec["handler"] for spec in inbound.values()])
        if names:
            server = (tpl / "server" / "index.ts").read_text()
            assert "/_handler/" in server, tpl
            for name in names:
                assert f'"{name}"' in server, (tpl, name)
    # The booking template is the worked example of APPS.md "Secrets",
    # "Inbound hooks" and "External links": every block of the lane, a key
    # that never enters the sandbox, and every release a person's click.
    booking = json.loads((root / "booking" / "app.json").read_text())
    assert booking["deploy_requires_approval"] is True
    assert {s["name"] for s in booking["secrets"]} == {"STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"}
    assert all(s["required"] for s in booking["secrets"])
    assert not any(s.get("env") for s in booking["secrets"])
    assert booking["inbound"]["stripe"] == {"verify": "stripe", "secret": "STRIPE_WEBHOOK_SECRET",
                                             "handler": "payment"}
    assert booking["external"] == {"links": ["checkout.stripe.com"], "challenge": ["/register"],
                                   "session_days": 30}
    assert [a["method"] for a in booking["actions"]] == ["notifications.create"]
    server = (root / "booking" / "server" / "index.ts").read_text()
    for must in ("/egress/api.stripe.com/", "platform/notifications.create", "claim.session",
                 "Bun.password.hash", "x-forwarded-host"):
        assert must in server, must
    assert "process.env.STRIPE" not in server, "the key is the platform's, never the server's"
    page = (root / "booking" / "client" / "index.html").read_text()
    for must in ("otodock.session.set", "otodock.session.clear", "otodock.challenge", "otodock.openExternal",
                 "otodock.sharedLink", "X-OtoDock-Challenge"):
        assert must in page, must


def test_a_folder_without_its_page_never_deploys(agent_tree):
    # The check sits before any row is touched: a folder with an app.json
    # but no client/index.html is refused with the reason, not deployed
    # into a release that 404s.
    bare = agent_tree / "workspace" / "apps" / "bare"
    (bare / "server").mkdir(parents=True)
    (bare / "app.json").write_text('{"title": "Bare"}')
    with pytest.raises(app_deploy.DeployError, match="client/index.html"):
        asyncio.run(app_deploy.deploy_folder(AGENT, "", None, "bare", bare, "workspace/apps/bare"))


def test_no_step_follows_a_link_out_of_the_workspace(agent_tree, tmp_path):
    """``apps/<slug>`` (or ``apps/`` itself) swapped for a link a session
    planted: deploy, check, preview and export would read another user's
    or another agent's folder, and import would write through it. Every one
    of them refuses; the folder behind the link is neither served nor
    touched."""
    other = _write(tmp_path / "elsewhere", {"app.json": json.dumps({"title": "Theirs"}),
                                            "client/index.html": "<p>theirs</p>",
                                            "client/secret.txt": "not yours"})
    _folder(agent_tree)
    row = task_store.get_app(_hook("deploy", {"slug": "board"}).json()["app_id"])
    board = agent_tree / row["rel_path"]
    shutil.rmtree(board)
    board.symlink_to(other, target_is_directory=True)
    for op, payload in (("deploy", {"slug": "board"}), ("check", {"slug": "board"}),
                        ("preview", {"slug": "board"}), ("export", {"slug": "board"}),
                        ("screenshot", {"slug": "board", "source": "working"})):
        r = _hook(op, payload)
        assert r.status_code == 400 and "leads out of the workspace" in r.text, (op, r.text)
    assert task_store.get_app(row["id"])["release_sha256"] == row["release_sha256"]
    assert not (agent_tree / "users/alice/workspace/apps/board.otoapp").exists()
    # The whole apps/ directory as the link: an import would write into it.
    board.unlink()
    _folder(agent_tree)
    bundle = _hook("export", {"slug": "board"}).json()["path"].lstrip("/")
    shutil.copy(agent_tree / bundle, agent_tree / "users/alice/workspace/board.otoapp")
    apps_dir = agent_tree / "users/alice/workspace/apps"
    shutil.rmtree(apps_dir)
    apps_dir.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    before = sorted(p.name for p in (tmp_path / "elsewhere").rglob("*"))
    r = _hook("import", {"path": "board.otoapp", "slug": "copy"})
    assert r.status_code == 400 and "leads out of the workspace" in r.text, r.text
    assert sorted(p.name for p in (tmp_path / "elsewhere").rglob("*")) == before


# ── deploy, pending, approve, reject ────────────────────────────────────────


def test_deploy_cuts_a_release_viewers_are_served_and_files_are_verified(agent_tree):
    _folder(agent_tree, html="<p>v1</p><script src=\"app.js\"></script>")
    (agent_tree / "users/alice/workspace/apps/board/client/app.js").write_text("console.log(1)")
    body = _hook("deploy", {"slug": "board"}).json()
    assert body["status"] == "ok" and body["release"] == 1, body
    row = task_store.get_app(body["app_id"])
    assert row["kind"] == "folder" and row["title"] == "Board"
    assert row["release_path"] == f"app-releases/users/alice/{row['id']}/1"
    assert row["deploy_state"] == "idle" and row["pending_release"] == 0
    assert "<p>v1</p>" in _served(row["id"])
    assert "<p>v1</p>" in client.get(f"/v1/apps/{row['id']}/client/{row['release_sha256']}/").text
    assert client.get(f"/v1/apps/{row['id']}/client/{row['release_sha256']}/app.js").text == "console.log(1)"
    # The working tree changes; viewers keep the release until the next deploy.
    (agent_tree / row["rel_path"] / "client" / "index.html").write_text("<p>v2</p>")
    assert "<p>v1</p>" in _served(row["id"])
    body = _hook("deploy", {"slug": "board"}).json()
    assert body["release"] == 2 and "<p>v2</p>" in _served(row["id"])
    shaped = client.get(f"/v1/apps/{row['id']}").json()
    assert shaped["release"] == 2 and shaped["has_previous_release"] is True
    assert shaped["kind"] == "folder" and shaped["has_server"] is False
    # A single-file slug is not converted.
    single = task_store.upsert_app(AGENT, "alice", "alice-sub", "single",
                                   rel_path="users/alice/workspace/apps/single.html")
    (agent_tree / single["rel_path"]).write_text("<p>s</p>")
    _folder(agent_tree, "single")
    assert "single-file" in _hook("deploy", {"slug": "single"}).json()["detail"]


def test_a_new_manifest_waits_for_approval_which_also_approves_it(agent_tree):
    manifest = {"title": "Board", "actions": [
        {"id": "me", "label": "Who am I", "type": "platform", "method": "viewer.me"}]}
    _folder(agent_tree, manifest=manifest)
    body = _hook("deploy", {"slug": "board"}).json()
    assert body["status"] == "pending approval" and body["release"] == 1
    assert body["manifest_changed"] is True and body["live_release"] == 0
    row = task_store.get_app(body["app_id"])
    assert row["deploy_state"] == "pending" and row["pending_release"] == 1
    assert row["release_path"] == "" and not task_store.app_actions_approved(row)
    # Nothing serves yet; the status names the change; a viewer may not approve.
    assert client.get(f"/v1/apps/{row['id']}/html").status_code == 404
    st = client.get(f"/v1/apps/{row['id']}/deploy/status").json()
    assert st["pending_release"] == 1 and st["manifest_approved"] is False
    assert sorted(st["changes"]["added"]) == ["app.json", "client/index.html"]
    # A viewer cannot even see a personal row; a bearer principal (an agent
    # session) is never a human at the card.
    _as(BOB)
    assert client.post(f"/v1/apps/{row['id']}/deploy/approve").status_code == 404
    _as(_user(is_api_key=True))
    assert client.post(f"/v1/apps/{row['id']}/deploy/approve").status_code == 403
    _as(ALICE)
    r = client.post(f"/v1/apps/{row['id']}/deploy/approve")
    assert r.status_code == 200 and r.json()["release"] == 1, r.text
    row = task_store.get_app(row["id"])
    assert task_store.app_actions_approved(row) and row["approved_by"] == "alice-sub"
    assert row["pending_release"] == 0 and releases.current_number(row) == 1
    assert "<p>v1</p>" in _served(row["id"])
    # The same manifest deploys straight through; a second pending is refused
    # while one waits; the per-app switch parks even an unchanged manifest.
    assert _hook("deploy", {"slug": "board"}).json()["status"] == "ok"
    manifest["deploy_requires_approval"] = True
    (agent_tree / row["rel_path"] / "app.json").write_text(json.dumps(manifest))
    body = _hook("deploy", {"slug": "board"}).json()
    assert body["status"] == "pending approval" and body["manifest_changed"] is False
    assert body["release"] == 3 and body["superseded"] == 0
    # A deploy while one waits replaces the waiting copy (nothing of it went
    # live) under a new number; the answer says which release it replaced.
    (agent_tree / row["rel_path"] / "client" / "index.html").write_text("<p>v3b</p>")
    body = _hook("deploy", {"slug": "board"}).json()
    assert body["status"] == "pending approval" and body["release"] == 4 and body["superseded"] == 3
    row = task_store.get_app(row["id"])
    assert row["pending_release"] == 4 and not (releases.app_release_dir(row) / "3").exists()
    assert "<p>v3b</p>" in (releases.app_release_dir(row) / "4" / "client" / "index.html").read_text()
    assert client.post(f"/v1/apps/{row['id']}/rollback").status_code == 409
    # Reject: the copy goes, the live release and its manifest stay.
    r = client.post(f"/v1/apps/{row['id']}/deploy/reject")
    assert r.status_code == 200 and r.json()["release"] == 4
    row = task_store.get_app(row["id"])
    assert row["pending_release"] == 0 and not (releases.app_release_dir(row) / "4").exists()
    assert releases.current_number(row) == 2 and task_store.app_actions_approved(row)
    assert client.post(f"/v1/apps/{row['id']}/deploy/approve").status_code == 409


def test_an_approval_names_the_manifest_the_card_showed(agent_tree):
    # A deploy while a release waits replaces the waiting copy AND its
    # manifest: the click approves only the pair the card rendered, never
    # what the agent shipped after it was drawn.
    manifest = {"title": "Board", "actions": [
        {"id": "me", "label": "Who am I", "type": "platform", "method": "viewer.me"}]}
    _folder(agent_tree, manifest=manifest)
    body = _hook("deploy", {"slug": "board"}).json()
    assert body["status"] == "pending approval"
    shown = task_store.get_app(body["app_id"])
    shown_sig = task_store.manifest_sig(shown)
    manifest["egress"] = ["api.example.com"]
    (agent_tree / shown["rel_path"] / "app.json").write_text(json.dumps(manifest))
    body = _hook("deploy", {"slug": "board"}).json()
    assert body["status"] == "pending approval" and body["superseded"] == 1 and body["release"] == 2
    row = task_store.get_app(shown["id"])
    assert task_store.manifest_sig(row) != shown_sig
    r = client.post(f"/v1/apps/{row['id']}/deploy/approve", json={"release": 2, "sig": shown_sig})
    assert r.status_code == 409, r.text
    row = task_store.get_app(row["id"])
    assert not task_store.app_actions_approved(row) and row["pending_release"] == 2
    assert client.post(f"/v1/apps/{row['id']}/deploy/approve",
                       json={"release": 1, "sig": task_store.manifest_sig(row)}).status_code == 409
    r = client.post(f"/v1/apps/{row['id']}/deploy/approve",
                    json={"release": 2, "sig": task_store.manifest_sig(row)})
    assert r.status_code == 200 and r.json()["release"] == 2, r.text
    assert task_store.app_actions_approved(task_store.get_app(row["id"]))


def test_a_code_only_replacement_never_rides_the_cards_approval(agent_tree):
    # The per-app switch parks every deploy, and a replacement that keeps
    # the manifest carries the same signature: only its number tells the
    # card's release from the one waiting now.
    _folder(agent_tree, manifest={"title": "Board", "deploy_requires_approval": True})
    body = _hook("deploy", {"slug": "board"}).json()
    shown, sig = body["release"], task_store.manifest_sig(task_store.get_app(body["app_id"]))
    app_id = body["app_id"]
    row = task_store.get_app(app_id)
    (agent_tree / row["rel_path"] / "client" / "index.html").write_text("<p>not shown</p>")
    body = _hook("deploy", {"slug": "board"}).json()
    assert body["status"] == "pending approval" and body["superseded"] == shown
    assert body["release"] != shown and task_store.manifest_sig(task_store.get_app(app_id)) == sig
    r = client.post(f"/v1/apps/{app_id}/deploy/approve", json={"release": shown, "sig": sig})
    assert r.status_code == 409, r.text
    assert task_store.get_app(app_id)["release_path"] == ""
    r = client.post(f"/v1/apps/{app_id}/deploy/approve", json={"release": body["release"], "sig": sig})
    assert r.status_code == 200 and "<p>not shown</p>" in _served(app_id), r.text


def test_a_folder_app_counts_against_the_app_cap(agent_tree, monkeypatch):
    # A folder row is a tab on the same strip as a single-file pin, so a new
    # one meets the same cap; a redeploy of one that exists never does.
    monkeypatch.setattr(task_store, "MAX_APPS_PER_SCOPE", 2)
    monkeypatch.setattr("storage.db_apps.MAX_APPS_PER_SCOPE", 2)
    for slug in ("one", "two", "three"):
        _folder(agent_tree, slug)
    assert _hook("deploy", {"slug": "one"}).json()["status"] == "ok"
    assert _hook("deploy", {"slug": "two"}).json()["status"] == "ok"
    r = _hook("deploy", {"slug": "three"})
    assert "app limit reached" in r.text and task_store.get_app_by_slug(AGENT, "alice", "three") is None, r.text
    assert "app limit reached" in _hook("pin", {"slug": "three"}).text
    assert _hook("deploy", {"slug": "one"}).json()["status"] == "ok"
    assert task_store.count_apps(AGENT, "alice") == 2


def test_an_approval_reads_the_release_again_under_the_lock(agent_tree):
    # The click names what the person was shown (by default the row the
    # route judged): a deploy that replaced the waiting manifest since is
    # refused, never taken live unapproved.
    manifest = {"title": "Board", "actions": [
        {"id": "me", "label": "Who am I", "type": "platform", "method": "viewer.me"}]}
    _folder(agent_tree, manifest=manifest)
    body = _hook("deploy", {"slug": "board"}).json()
    shown = task_store.get_app(body["app_id"])
    manifest["egress"] = ["api.example.com"]
    (agent_tree / shown["rel_path"] / "app.json").write_text(json.dumps(manifest))
    assert _hook("deploy", {"slug": "board"}).json()["status"] == "pending approval"
    with pytest.raises(app_deploy.DeployError, match="changed"):
        asyncio.run(app_deploy.approve_pending(shown, "alice-sub"))
    row = task_store.get_app(shown["id"])
    assert releases.current_number(row) == 0 and row["pending_release"] and not task_store.app_actions_approved(row)


def test_a_deploy_waiting_on_an_approval_never_replaces_what_it_took_live(agent_tree):
    # An approval holds the deploy lock and takes the waiting release live;
    # a deploy that read the row before and waited for the lock must not
    # treat that release as its waiting copy to replace.
    manifest = {"title": "Board", "actions": [
        {"id": "me", "label": "Who am I", "type": "platform", "method": "viewer.me"}]}
    src = _folder(agent_tree, manifest=manifest)
    body = _hook("deploy", {"slug": "board"}).json()
    app_id, n = body["app_id"], body["release"]
    manifest["egress"] = ["api.example.com"]
    (src / "app.json").write_text(json.dumps(manifest))
    (src / "client" / "index.html").write_text("<p>v2</p>")

    async def race() -> dict:
        lock = app_deploy._deploy_lock(app_id)
        await lock.acquire()
        # The approval has approved the manifest it was shown ...
        shown = task_store.get_app(app_id)
        assert task_store.approve_app_actions(app_id, task_store.manifest_sig(shown), "alice-sub")
        deploy = asyncio.create_task(app_deploy.deploy_folder(
            AGENT, "alice", "alice-sub", "board", src, "users/alice/workspace/apps/board", cold=True))
        for _ in range(500):
            if lock._waiters:
                break
            await asyncio.sleep(0.01)
        # ... and takes its release live while the deploy waits.
        await app_deploy.go_live(shown, n, start=False)
        lock.release()
        return await deploy

    out = asyncio.run(race())
    row = task_store.get_app(app_id)
    assert releases.current_number(row) == n and out["release"] != n, out
    assert row["pending_release"] == out["release"] and out["superseded"] == 0
    live = releases.live_release_dir(row)
    assert (live / "client" / "index.html").read_text() == "<p>v1</p>"


def test_a_viewer_session_cannot_deploy_a_shared_app_and_pin_app_aliases_deploy(agent_tree):
    _folder(agent_tree, "team", username="")
    r = _hook("deploy", {"slug": "team", "visibility": "agent"}, sid=SID_VIEWER)
    assert r.status_code == 403
    body = _hook("deploy", {"slug": "team", "visibility": "agent"}, sid=SID_SHARED).json()
    assert body["status"] == "ok" and body["scope"] == "shared"
    row = task_store.get_app(body["app_id"])
    assert row["username"] == "" and row["release_path"].startswith("app-releases/shared/")
    # pin_app on the folder is the same deploy; its html/actions do not apply.
    (agent_tree / "workspace/apps/team/client/index.html").write_text("<p>t2</p>")
    body = _hook("pin", {"slug": "team", "visibility": "agent", "actions": []}, sid=SID_SHARED).json()
    assert body["release"] == 2 and "app.json" in body.get("note", "")
    assert "<p>t2</p>" in _served(row["id"])
    # The server's log is for who manages the app, as on the dashboard.
    assert _hook("logs", {"slug": "team", "visibility": "agent"}, sid=SID_VIEWER).status_code == 403
    assert _hook("logs", {"slug": "team", "visibility": "agent"}, sid=SID_SHARED).status_code == 200
    # A missing folder is a clear refusal.
    assert "no folder" in _hook("deploy", {"slug": "ghost"}).json()["detail"]


def test_rollback_restores_the_database_copy_and_keeps_a_snapshot(agent_tree):
    _folder(agent_tree)
    row = task_store.get_app(_hook("deploy", {"slug": "board"}).json()["app_id"])
    # The app wrote a row before release 2 went live.
    db = releases.app_data_dir(row) / "app.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, t TEXT)")
    con.execute("INSERT INTO cards (t) VALUES ('one')")
    con.commit()
    con.close()
    (agent_tree / row["rel_path"] / "client" / "index.html").write_text("<p>v2</p>")
    assert _hook("deploy", {"slug": "board"}).json()["release"] == 2
    assert releases.db_before_path(row, 2).is_file()
    # More writes after release 2; a rollback keeps them in a snapshot and
    # restores the copy taken before release 2.
    con = sqlite3.connect(db)
    con.execute("INSERT INTO cards (t) VALUES ('two')")
    con.commit()
    con.close()
    r = client.post(f"/v1/apps/{row['id']}/rollback")
    assert r.status_code == 200 and r.json()["release"] == 1 and r.json()["db_restored"] is True
    snaps = list(releases.app_release_dir(row).glob("rollback-*.sqlite"))
    assert len(snaps) == 1 and r.json()["snapshot"] == snaps[0].name
    assert sqlite3.connect(snaps[0]).execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 2
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1
    assert not Path(str(db) + "-wal").exists()
    assert "<p>v1</p>" in _served(row["id"])
    assert client.post(f"/v1/apps/{row['id']}/rollback").status_code == 409


def test_check_and_preview_never_touch_the_live_release(agent_tree):
    _folder(agent_tree)
    row = task_store.get_app(_hook("deploy", {"slug": "board"}).json()["app_id"])
    (agent_tree / row["rel_path"] / "client" / "index.html").write_text("<p>draft</p>")
    body = _hook("check", {"slug": "board"}).json()
    assert body["status"] == "ok" and body["server"] == "none" and body["files"] == 2
    # The ack says which blocks parsed, so a block dropped for a typo shows
    # here and not on the approval card.
    assert body["manifest"] == {"actions": 0, "files": False, "egress": False,
                                "handlers": False, "exports": False, "bindings": False,
                                "requires": False, "steps": False, "secrets": False,
                                "inbound": False, "external": False}
    (agent_tree / row["rel_path"] / "app.json").write_text(json.dumps({
        "title": "Board", "handlers": {"on_trigger": ["gh"]},
        "exports": {"snapshots": {"board": {"description": "the columns and their cards"}}},
        "requires": {"mcps": ["github-mcp"]}}))
    assert _hook("check", {"slug": "board"}).json()["manifest"] == {
        "actions": 0, "files": False, "egress": False, "handlers": True,
        "exports": True, "bindings": False, "requires": True, "steps": False, "secrets": False,
        "inbound": False, "external": False}
    (agent_tree / row["rel_path"] / "app.json").write_text('{"handlers": [1]}')
    assert _hook("check", {"slug": "board"}).json()["status"] == "invalid"
    (agent_tree / row["rel_path"] / "app.json").write_text('{"title": "Board"}')
    # A preview copy with its own hash and its own data; the release is untouched.
    db = releases.app_data_dir(row) / "app.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(db).execute("CREATE TABLE t (x)").connection.commit()
    body = _hook("preview", {"slug": "board"}).json()
    assert body["status"] == "ok" and body["url"] == f"/apps/{row['id']}?preview=1"
    assert (releases.preview_dir(row) / "data" / "app.db").is_file()
    assert client.get(f"/v1/apps/{row['id']}").json()["preview_sha"] == body["sha"]
    assert "<p>draft</p>" in client.get(f"/v1/apps/{row['id']}/client/{body['sha']}/?preview=1").text
    assert "<p>v1</p>" in _served(row["id"])
    _as(BOB)
    assert client.get(f"/v1/apps/{row['id']}").status_code == 404  # personal row


def test_pull_folder_brings_the_satellite_tree_within_the_caps(agent_tree, monkeypatch):
    from core.remote import remote_file_flow as rff
    listing = ["users/alice/workspace/apps/board/app.json",
               "users/alice/workspace/apps/board/client/index.html",
               "users/alice/workspace/apps/board/data/app.db",
               "users/alice/workspace/apps/board/.env",
               "users/alice/workspace/apps/board/server/node_modules/x.js"]
    pulled: list[str] = []

    async def fake_list(sid, prefix):
        return listing

    async def fake_pull(sid, rel):
        pulled.append(rel)
        p = agent_tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}" if rel.endswith("app.json") else "<p>r</p>")
        return p

    class Info:
        agent_name = AGENT

    monkeypatch.setattr(rff, "list_remote_files", fake_list)
    monkeypatch.setattr(rff, "pull_through", fake_pull)
    monkeypatch.setattr(rff, "_get_remote_session_info", lambda sid: Info())
    root = agent_tree / "users/alice/workspace/apps/board"
    _write(root, {"client/stale.js": "gone on the satellite"})
    n = asyncio.run(app_deploy.pull_folder("sid", "users/alice/workspace/apps/board"))
    assert n == 2 and pulled == listing[:2]
    assert not (root / "client" / "stale.js").exists()
    # A link left in the platform copy is the rule's refusal, never an error
    # page (the check hook never answers 500).
    (root / "client" / "linked.js").symlink_to(root / "app.json")
    with pytest.raises(app_deploy.DeployError, match="symlink not allowed in an app"):
        asyncio.run(app_deploy.pull_folder("sid", "users/alice/workspace/apps/board"))
    (root / "client" / "linked.js").unlink()
    monkeypatch.setattr(app_deploy, "PULL_MAX_FILES", 1)
    with pytest.raises(app_deploy.DeployError):
        asyncio.run(app_deploy.pull_folder("sid", "users/alice/workspace/apps/board"))


@_needs_runtime
def test_a_broken_server_leaves_the_previous_release_running(agent_tree):
    """The pipeline itself, on one loop (a child process belongs to the loop
    that spawned it — the proxy's single loop in production; the test
    client's per-request loops would take it down between calls)."""
    import httpx
    src = _folder(agent_tree, server=SERVER_OK)
    rel_dir = "users/alice/workspace/apps/board"

    async def run() -> None:
        body = await app_deploy.deploy_folder(AGENT, "alice", "alice-sub", "board", src, rel_dir)
        assert body["status"] == "ok" and body["release"] == 1, body
        row = task_store.get_app(body["app_id"])
        inst = app_supervisor.get(row["id"])
        assert inst is not None and inst.state == "up"
        assert (await asyncio.to_thread(httpx.get, f"{inst.base_url}/")).text == "v1"
        # A broken server: refused with the reason, release 1 keeps serving
        # on the same instance, the failed copy is gone.
        (src / "server" / "index.ts").write_text(SERVER_BROKEN)
        with pytest.raises(app_deploy.DeployError) as e:
            await app_deploy.deploy_folder(AGENT, "alice", "alice-sub", "board", src, rel_dir)
        assert "previous one keeps serving" in str(e.value)
        row = task_store.get_app(row["id"])
        assert releases.current_number(row) == 1 and releases.release_numbers(row) == [1]
        assert app_supervisor.get(row["id"]) is inst and inst.state == "up"
        assert (await asyncio.to_thread(httpx.get, f"{inst.base_url}/")).text == "v1"
        # A good server switches blue/green: the new instance answers, the
        # old one is stopped, the row points at the new release.
        (src / "server" / "index.ts").write_text(SERVER_OK.replace('"1"', '"2"'))
        body = await app_deploy.deploy_folder(AGENT, "alice", "alice-sub", "board", src, rel_dir)
        assert body["status"] == "ok" and body["release"] == 2  # the failed copy left no number behind
        fresh = app_supervisor.get(row["id"])
        assert fresh is not inst and fresh.state == "up" and inst.proc.returncode is not None
        assert (await asyncio.to_thread(httpx.get, f"{fresh.base_url}/")).text == "v2"
        assert app_deploy.status(task_store.get_app(row["id"]))["server"] == "up"
        # The smoke starts the working tree apart and stops it.
        report = await app_supervisor.smoke(row, src)
        assert report["ok"] and app_supervisor.get(row["id"], "check") is None
        await app_supervisor.stop_all()

    asyncio.run(run())


# ── the static checks ────────────────────────────────────────────────────────


def test_a_page_the_sandbox_would_refuse_never_deploys(agent_tree):
    # The lint sits before the row: a raw fetch, a form and an undeclared
    # feed are refused by deploy, check and pin_app alike, with the file
    # and the line, and no app is registered.
    bad = '<form><input></form><script>fetch("/api/x"); otodock.feed("tasks", f);</script>'
    _folder(agent_tree, "lintbad", html=bad)
    body = _hook("deploy", {"slug": "lintbad"}).json()
    assert body["status"] == "refused" and body["problems"] == 3, body
    rules = sorted(f["rule"] for f in body["findings"])
    assert rules == ["client.form", "client.raw-fetch", "client.undeclared-feed"]
    assert all(f["file"] == "client/index.html" and f["line"] >= 1 for f in body["findings"])
    assert task_store.get_app_by_slug(AGENT, "alice", "lintbad") is None
    body = _hook("check", {"slug": "lintbad"}).json()
    assert body["status"] == "refused" and body["problems"] == 3
    body = _hook("pin", {"slug": "lintbad", "html": ""}).json()
    assert body["status"] == "refused"
    # A warning rides along with a deploy that went through.
    _folder(agent_tree, "lintwarn", html='<script src="/ui-kit/tailwind.js"></script><script>alert("hi")</script>')
    body = _hook("deploy", {"slug": "lintwarn"}).json()
    assert body["status"] == "ok" and body["problems"] == 0 and body["warnings"] == 1, body
    assert body["findings"][0]["rule"] == "client.modal"


def test_a_single_file_pin_gets_the_client_rules(agent_tree):
    body = _hook("pin", {"slug": "single-lint", "html": "<form><input></form>", "actions": []}).json()
    assert body["status"] == "refused" and body["findings"][0]["rule"] == "client.form"
    assert task_store.get_app_by_slug(AGENT, "alice", "single-lint") is None
    body = _hook("pin", {"slug": "single-lint", "html": "<p>fine</p>", "actions": []}).json()
    assert body.get("status") != "refused" and task_store.get_app_by_slug(AGENT, "alice", "single-lint")


@pytest.mark.parametrize("op", ["deploy", "check", "preview", "export"])
def test_the_deploy_hooks_refuse_a_token_whose_holder_is_gone(agent_tree, op):
    """D1-to-verifier-owners: the folder-app hooks judge the session token's
    holder like the session routes: a person who no longer exists is refused
    before the hook runs, a living person's token reaches it."""
    from api.sessions import sessions
    from auth.session_token import create_session_token
    _folder(agent_tree)
    sessions._holder_answers.clear()
    try:
        ghost = create_session_token(SID, AGENT, "ghost-sub")
        r = client.post(f"/v1/hooks/apps/{op}", json={"session_id": SID, "slug": "board"},
                        headers={"Authorization": f"Bearer {ghost}"})
        assert r.status_code == 401, r.text
        alive = create_session_token(SID, AGENT, "alice-sub")
        with patch("api.hooks.pins.verify_session_match_async"):
            r = client.post(f"/v1/hooks/apps/{op}", json={"session_id": SID, "slug": "board"},
                            headers={"Authorization": f"Bearer {alive}"})
        assert r.status_code != 401, r.text
    finally:
        sessions._holder_answers.clear()


def test_a_deploy_never_turns_the_approval_switch_off(agent_tree, monkeypatch):
    """A deploy may raise ``deploy_requires_approval``, never
    lower it: a release whose app.json says false on a row with the switch
    on parks and says so, and only a person's approval of that release
    lowers the switch, once the release is live; an absent key keeps it."""
    manifest = {"title": "Board", "deploy_requires_approval": True}
    root = _folder(agent_tree, "board", username="", manifest=manifest)
    body = _hook("deploy", {"slug": "board", "visibility": "agent"}, sid=SID_SHARED).json()
    assert body["status"] == "pending approval", body
    app_id = body["app_id"]
    assert client.post(f"/v1/apps/{app_id}/deploy/approve").status_code == 200
    assert task_store.get_app(app_id)["deploy_requires_approval"] is True
    # A service session ships new code with the switch set false: it parks.
    manifest["deploy_requires_approval"] = False
    (root / "app.json").write_text(json.dumps(manifest))
    (root / "client" / "index.html").write_text("<p>v2</p>")
    body = _hook("deploy", {"slug": "board", "visibility": "agent"}, sid=SID_SHARED).json()
    assert body["status"] == "pending approval" and body["lowers_approval"] is True, body
    assert body["reason"] == "turns off approval for every deploy"
    assert task_store.get_app(app_id)["deploy_requires_approval"] is True
    assert client.get(f"/v1/apps/{app_id}/deploy/status").json()["lowers_approval"] is True
    # A release that fails to start lowers nothing.
    async def _broken(*a, **kw):
        raise app_sandbox.AppStartError("boom")

    (root / "server").mkdir()
    (root / "server" / "index.ts").write_text(SERVER_BROKEN)
    body = _hook("deploy", {"slug": "board", "visibility": "agent"}, sid=SID_SHARED).json()
    assert body["status"] == "pending approval", body
    with monkeypatch.context() as m:
        m.setattr(app_supervisor, "start", _broken)
        assert client.post(f"/v1/apps/{app_id}/deploy/approve").status_code != 200
    assert task_store.get_app(app_id)["deploy_requires_approval"] is True
    # A pending copy whose app.json can no longer be read lowers nothing.
    (root / "server" / "index.ts").unlink()
    (root / "server").rmdir()
    body = _hook("deploy", {"slug": "board", "visibility": "agent"}, sid=SID_SHARED).json()
    assert body["status"] == "pending approval" and body["lowers_approval"] is True, body
    row = task_store.get_app(app_id)
    (releases.app_release_dir(row) / str(body["release"]) / "app.json").unlink()
    assert client.post(f"/v1/apps/{app_id}/deploy/approve").status_code == 200
    assert task_store.get_app(app_id)["deploy_requires_approval"] is True
    # A person's approval of the lowering release lowers the switch; the
    # next code-only deploy goes live.
    (root / "client" / "index.html").write_text("<p>v2b</p>")
    body = _hook("deploy", {"slug": "board", "visibility": "agent"}, sid=SID_SHARED).json()
    assert body["status"] == "pending approval" and body["lowers_approval"] is True, body
    assert client.post(f"/v1/apps/{app_id}/deploy/approve").status_code == 200
    row = task_store.get_app(app_id)
    assert row["deploy_requires_approval"] is False
    assert client.get(f"/v1/apps/{app_id}/deploy/status").json().get("lowers_approval") is not True
    (root / "client" / "index.html").write_text("<p>v3</p>")
    assert _hook("deploy", {"slug": "board", "visibility": "agent"}, sid=SID_SHARED).json()["status"] == "ok"
    # An absent key keeps the switch: the release parks, nothing is lowered.
    manifest = {"title": "Other", "deploy_requires_approval": True}
    root2 = _folder(agent_tree, "other", username="", manifest=manifest)
    body = _hook("deploy", {"slug": "other", "visibility": "agent"}, sid=SID_SHARED).json()
    other = body["app_id"]
    assert client.post(f"/v1/apps/{other}/deploy/approve").status_code == 200
    (root2 / "app.json").write_text(json.dumps({"title": "Other"}))
    body = _hook("deploy", {"slug": "other", "visibility": "agent"}, sid=SID_SHARED).json()
    assert body["status"] == "pending approval" and not body.get("lowers_approval"), body
    assert client.post(f"/v1/apps/{other}/deploy/approve").status_code == 200
    assert task_store.get_app(other)["deploy_requires_approval"] is True


def test_a_linked_manifest_or_entry_page_is_never_read(agent_tree, tmp_path):
    """An app's ``app.json`` or ``client/`` that is a link is
    refused by the check and the deploy with the rule's own sentence, before
    anything is read through it: nothing of the target reaches the answer
    and no release is cut."""
    outside = tmp_path / "outside"
    (outside / "client").mkdir(parents=True)
    (outside / "keys.json").write_text(json.dumps({"db_password": "x", "smtp_key": "y"}))
    (outside / "client" / "index.html").write_text("<p>elsewhere</p>")
    leak = _folder(agent_tree, "leak")
    (leak / "app.json").unlink()
    (leak / "app.json").symlink_to(outside / "keys.json")
    for op in ("check", "deploy"):
        r = _hook(op, {"slug": "leak"})
        assert "db_password" not in r.text and "smtp_key" not in r.text, r.text
        assert "symlink not allowed in an app: app.json" in r.text, r.text
    page = _folder(agent_tree, "page")
    import shutil as _sh
    _sh.rmtree(page / "client")
    (page / "client").symlink_to(outside / "client")
    for op in ("check", "deploy"):
        r = _hook(op, {"slug": "page"})
        assert "symlink not allowed in an app: client/index.html" in r.text, r.text
    assert task_store.get_app_by_slug(AGENT, "alice", "leak") is None
    assert task_store.get_app_by_slug(AGENT, "alice", "page") is None


def test_a_link_in_a_registered_working_tree_is_a_refusal_on_render(agent_tree, monkeypatch):
    """A registered app's working tree that holds a link to a file answers
    the screenshot with the rule's sentence (400), never an error page; and
    a file swapped for a link between the check's walk and its copy is the
    same refusal on the check."""
    from services.apps import app_render
    src = _folder(agent_tree, "shot")
    assert _hook("deploy", {"slug": "shot"}).json()["status"] == "ok"
    (src / "client" / "linked.js").symlink_to(src / "app.json")
    r = _hook("screenshot", {"slug": "shot", "source": "working"})
    assert r.status_code == 400, r.text
    assert "symlink not allowed in an app: client/linked.js" in r.text, r.text
    (src / "client" / "linked.js").unlink()

    def swapped(row, source_dir):
        raise releases.ReleaseInvalid("symlink not allowed in an app: client/late.js")

    monkeypatch.setattr(app_render, "copy_working_tree", swapped)
    r = _hook("check", {"slug": "shot"})
    assert r.status_code == 400, r.text
    assert "symlink not allowed in an app: client/late.js" in r.text, r.text


def test_the_check_smoke_starts_a_copy_of_the_working_tree(agent_tree, monkeypatch):
    """A check that smoke-starts a server runs a copy of the working tree,
    never the folder a session can still change, and removes the copy."""
    seen: list = []

    async def fake_smoke(row, tree_dir):
        seen.append((tree_dir, (tree_dir / "server" / "index.ts").is_file()))
        return {"ok": True, "server": "up"}

    monkeypatch.setattr(app_supervisor, "smoke", fake_smoke)
    src = _folder(agent_tree, "smoky", server=SERVER_OK)
    body = _hook("check", {"slug": "smoky"}).json()
    assert body["status"] == "ok", body
    assert len(seen) == 1 and seen[0][1] is True
    tree = seen[0][0]
    assert tree.resolve() != src.resolve() and not tree.exists()


def test_the_restore_validates_off_the_loop(agent_tree, monkeypatch):
    """Putting the live manifest back runs the validation (whose step
    check starts a parser) on a worker thread, never the event loop's."""
    import threading
    _folder(agent_tree, "board")
    body = _hook("deploy", {"slug": "board"}).json()
    assert body["status"] == "ok", body
    row = task_store.get_app(body["app_id"])
    task_store.upsert_app(AGENT, "alice", "alice-sub", "board",
                          blocks={"egress": task_store.canonical_actions_json(["api.example.com"])})
    row = task_store.get_app(row["id"])
    assert not task_store.app_actions_approved(row)
    threads: list[bool] = []
    real = app_deploy.validate_app_json

    def recording(*a, **kw):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(*a, **kw)

    monkeypatch.setattr(app_deploy, "validate_app_json", recording)
    asyncio.run(app_deploy._restore_live_manifest(row))
    assert threads == [False]


def test_the_database_snapshot_and_restore_never_follow_a_planted_link(tmp_path, monkeypatch):
    """The data directory is the app's own to write: a link at the database's
    name refuses the snapshot, and the restore replaces a link at the name
    instead of writing through it."""
    from services.apps import app_deploy
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    other = tmp_path / "other.db"
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE t (x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()
    data = tmp_path / "a" / "app-data" / "slug"
    data.mkdir(parents=True)
    (data / "app.db").symlink_to(other)
    with pytest.raises(app_deploy.DeployError):
        app_deploy.snapshot_db(data / "app.db", tmp_path / "a" / "rel" / "db-before.sqlite")
    (data / "app.db").unlink()
    con = sqlite3.connect(data / "app.db")
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    (data / "app.db-shm").symlink_to(other)
    with pytest.raises(app_deploy.DeployError):
        app_deploy.snapshot_db(data / "app.db", tmp_path / "a" / "rel" / "db-before.sqlite")
    (data / "app.db-shm").unlink()
    assert app_deploy.snapshot_db(data / "app.db", tmp_path / "a" / "rel" / "db-before.sqlite")

    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"keep")
    (data / "app.db").unlink()
    (data / "app.db").symlink_to(victim)
    app_deploy.restore_db(tmp_path / "a" / "rel" / "db-before.sqlite", data / "app.db")
    assert victim.read_bytes() == b"keep"
    assert not (data / "app.db").is_symlink()
    assert (data / "app.db").read_bytes()[:15] == b"SQLite format 3"
