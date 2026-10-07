"""Platform methods and feeds for app servers (APPS.md "Platform methods
and feeds for servers", "Files").

Load-bearing: a server's call runs as the viewer it forwards, else as the
app identity within the agent-scope slice; a write needs a viewer; the
files methods reach only the declared prefixes, judged on the resolved
path; the subscription socket takes the launch token only and delivers
agent-scope deltas with a sequence.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.providers import UserContext, get_current_user
from tests.conftest import live_session_token
from api.apps import app_proxy, catalog
from services.apps import app_supervisor, app_tokens
from storage import database as task_store

client = TestClient(app)
AGENT = "platform-agent"


def _user(sub: str = "alice-sub", agent_roles: dict[str, str] | None = None) -> UserContext:
    roles = {AGENT: "manager"} if agent_roles is None else agent_roles
    return UserContext(sub=sub, email=f"{sub}@test.com", name=sub, role="member",
                       agents=list(roles.keys()), agent_roles=roles)


ALICE = _user()
BOB = _user("bob-sub", {AGENT: "viewer"})


def _as(user: UserContext | None) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from api.apps import apps as apps_api
    _as(ALICE)
    apps_api._fire_rate.clear()
    app_proxy._buckets.clear()
    catalog._app_subs.clear()
    catalog._seq.clear()
    # The four-a-second bucket is a production rule, not the subject here.
    monkeypatch.setattr(app_proxy, "PLATFORM_RATE", 1000.0)
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app_supervisor._instances.clear()
    app_supervisor._locks.clear()
    app_proxy._buckets.clear()
    catalog._app_subs.clear()


@pytest.fixture
def agent_tree(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    (agents_root / AGENT / "users" / "alice" / "workspace").mkdir(parents=True)
    (agents_root / AGENT / "workspace").mkdir(parents=True)
    (agents_root / AGENT / "knowledge").mkdir(parents=True)
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
    return agents_root / AGENT


def _row(slug: str, actions: list[dict], *, files: dict | None = None,
         username: str = "", owner: str | None = None, approve: bool = True,
         requires: dict | None = None) -> dict:
    from api.apps import manifest as _mf
    root = f"users/{username}/workspace" if username else "workspace"
    blocks = {}
    if files:
        blocks["files"] = task_store.canonical_actions_json(files)
    if requires:
        blocks["requires"] = task_store.canonical_actions_json(requires)
    blocks = blocks or None
    actions_json, err = _mf.validate_actions(actions, AGENT, shared=not username)
    assert actions_json is not None, err
    row = task_store.upsert_app(AGENT, username, owner, slug, title=slug.title(),
                                rel_path=f"{root}/apps/{slug}", kind="folder",
                                actions_json=actions_json, blocks=blocks)
    if approve:
        task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), "alice-sub")
    return task_store.get_app(row["id"])


def _install(row: dict) -> app_supervisor.Instance:
    inst = app_supervisor.Instance(row_id=row["id"], name="live", row=row,
                                   release_dir=config.get_agent_dir(AGENT),
                                   data_dir=config.get_agent_dir(AGENT), host_port=1, state="up")
    inst.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH, {"sub": f"app:{row['id']}"}, 3600)
    app_supervisor._instances[(row["id"], "live")] = inst
    return inst


def _viewer_token(app_id: str) -> str:
    return client.post(f"/v1/apps/{app_id}/viewer-token").json()["token"]


def _call(app_id: str, method: str, token: str, args=None, viewer: str = ""):
    headers = {"Authorization": f"Bearer {token}"}
    if viewer:
        headers["X-OtoDock-Viewer"] = viewer
    return client.post(f"/v1/apps/{app_id}/platform/{method}", json={"args": args}, headers=headers)


ME = [{"id": "me", "label": "Me", "type": "platform", "method": "viewer.me"}]


def test_setup_methods_are_the_viewers_own_and_flip_their_slice(agent_tree, monkeypatch):
    """``setup.status`` / ``setup.complete`` (COMMUNITY-AGENTS-REGISTRY.md
    "Setup guides"): the viewer's own state; completing removes their guide
    with the platform's bookkeeping and tells their file_changes slice; a
    bearer, the app identity and a viewer without a manager's role for the
    agent-wide scope are refused."""
    from unittest.mock import AsyncMock, patch
    from storage.agents import agent_store
    agent_store.create_agent(AGENT, "Platform", collaborative=True)
    row = _row("home", [
        {"id": "s", "label": "State", "type": "platform", "method": "setup.status"},
        {"id": "c", "label": "Done", "type": "platform", "method": "setup.complete"},
    ], username="alice", owner="alice-sub")
    url = f"/v1/apps/{row['id']}/catalog"
    from api.apps import apps as apps_api

    def post(path: str, body: dict) -> dict:
        apps_api._fire_rate.clear()      # the quarter-second window is not the subject
        return client.post(path, json=body).json()

    # No guide shipped: nothing is pending for anyone; the page learns the
    # agent's name, and grades no MCP when its manifest names none.
    r = post(f"{url}/setup.status", {})
    assert r["ok"] and r["result"] == {"user": "none", "agent": "none", "can_complete_agent": True,
                                       "display_name": "Platform", "description": "", "mcps": {}}
    # The template's guide and alice's copy: pending; bob (a viewer) sees his own state.
    (agent_tree / "config").mkdir(exist_ok=True)
    (agent_tree / "config" / "user-setup.md").write_text("welcome")
    (agent_tree / "users" / "alice" / "context").mkdir(parents=True, exist_ok=True)
    (agent_tree / "users" / "alice" / "context" / "user-setup.md").write_text("welcome")
    (agent_tree / "config" / "context").mkdir(parents=True, exist_ok=True)
    (agent_tree / "config" / "context" / "setup.md").write_text("admin setup")
    r = post(f"{url}/setup.status", {})
    assert {k: r["result"][k] for k in ("user", "agent", "can_complete_agent")} == \
        {"user": "pending", "agent": "pending", "can_complete_agent": True}
    emitted: list = []
    monkeypatch.setattr(catalog, "file_changed", lambda users, agent, rel, **kw: emitted.append((list(users), agent, rel, kw)))
    with patch("services.remote.workspace_fanout.fan_out_delete", new=AsyncMock()):
        r = post(f"{url}/setup.complete", {"args": {"scope": "user"}})
    assert r["ok"] and r["result"]["status"] == "user_setup_complete" and r["result"]["user_setup_removed"]
    assert not (agent_tree / "users" / "alice" / "context" / "user-setup.md").exists()
    assert emitted == [(["alice-sub"], AGENT, "users/alice/context/user-setup.md", {"source": "setup"})]
    assert post(f"{url}/setup.status", {})["result"]["user"] == "done"
    # A viewer cannot complete the agent-wide guide; a manager can, once.
    bob_row = _row("home", [
        {"id": "c", "label": "Done", "type": "platform", "method": "setup.complete"}],
        username="bob", owner="bob-sub")
    _as(BOB)
    r = post(f"/v1/apps/{bob_row['id']}/catalog/setup.complete", {"args": {"scope": "agent"}})
    assert not r["ok"] and "manager" in r["reason"]
    _as(ALICE)
    with patch("services.remote.workspace_fanout.fan_out_delete", new=AsyncMock()), \
            patch("services.notifications.notification_manager.fire_notification", new=AsyncMock()):
        r = post(f"{url}/setup.complete", {"args": {"scope": "agent"}})
    assert r["ok"] and r["result"]["status"] == "completed" and r["result"]["setup_md_removed"]
    assert post(f"{url}/setup.status", {})["result"]["agent"] == "done"
    # A bearer principal is refused on the page route; on the server route
    # the app identity is refused and a forwarded viewer admitted.
    _as(UserContext(sub="alice-sub", email="a@test.com", name="a", role="member",
                    agents=[AGENT], agent_roles={AGENT: "manager"}, is_api_key=True, agent=AGENT))
    r = post(f"{url}/setup.status", {})
    assert not r["ok"] and "person" in r["reason"]
    _as(ALICE)
    inst = _install(row)
    assert _call(row["id"], "setup.status", inst.token).status_code == 403
    r = _call(row["id"], "setup.status", inst.token, viewer=_viewer_token(row["id"]))
    assert r.status_code == 200 and r.json()["result"]["user"] == "done"


def test_setup_status_grades_the_mcps_the_page_names(agent_tree, monkeypatch):
    """``setup.status`` names the agent and grades every MCP the page's
    manifest names (``requires.mcps`` and the ``mcp`` of a button): enabled
    for the agent on any placement, connected for THIS viewer when the MCP
    needs a per-user account, and the fix — ``connect`` for the viewer,
    ``admin`` for what an admin has to do — with a printable reason."""
    from types import SimpleNamespace as NS
    from services.mcp import mcp_registry
    from storage.agents import agent_store
    from storage.identity import credential_store
    from storage.mcp import mcp_request_store, mcp_store
    agent_store.create_agent(AGENT, "Kalypso", collaborative=True, description="Your assistant")

    def mf(name, *, per_user=False, mode="auto", server_name="", label=""):
        return NS(name=name, label=label or name, assignment_mode=mode, server_name=server_name,
                  credentials=NS(type="per_user" if per_user else "none", oauth=None))

    enabled = [mf("schedules-mcp", label="Schedules"),
               mf("google-workspace", per_user=True, label="Google Workspace"),
               mf("display-mcp", server_name="display", label="Display")]
    known = {m.name: m for m in enabled}
    known["m365-mcp"] = mf("m365-mcp", per_user=True, label="Microsoft 365")
    known["blender-mcp"] = mf("blender-mcp", mode="explicit", label="Blender")
    known["file-tools"] = mf("file-tools", label="File tools")
    known["camoufox"] = mf("camoufox", label="Browser")
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda agent, **kw: enabled)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements", lambda agent: enabled)
    monkeypatch.setattr(mcp_registry, "get_manifest", known.get)
    monkeypatch.setattr(mcp_registry, "get_manifest_by_config_key", lambda key: None)
    monkeypatch.setattr(mcp_store, "is_agent_authorized_for_mcp", lambda mcp, agent: False)
    monkeypatch.setattr(mcp_store, "get_all_mcp_states", lambda: {"camoufox": False})
    monkeypatch.setattr(mcp_request_store, "open_requests_by_pair", lambda: {("file-tools", AGENT): 1})
    accounts: dict = {}
    monkeypatch.setattr(credential_store, "list_user_accounts", lambda sub, mcp: accounts.get((sub, mcp), []))
    row = _row("home", [
        {"id": "s", "label": "State", "type": "platform", "method": "setup.status"},
        {"id": "shot", "label": "Shot", "type": "mcp_tool", "mcp": "display-mcp", "tool": "screenshot_app"},
    ], username="alice", owner="alice-sub", requires={"mcps": [
        "schedules-mcp", "google-workspace", "m365-mcp", "blender-mcp", "file-tools", "camoufox", "nowhere-mcp"]})
    from api.apps import apps as apps_api

    def status() -> dict:
        apps_api._fire_rate.clear()
        r = client.post(f"/v1/apps/{row['id']}/catalog/setup.status", json={}).json()
        assert r["ok"], r
        return r["result"]

    res = status()
    assert res["display_name"] == "Kalypso" and res["description"] == "Your assistant"
    m = res["mcps"]
    assert m["schedules-mcp"] == {"enabled": True, "connected": True, "fix": "none", "reason": ""}
    g = m["google-workspace"]
    assert (g["enabled"], g["connected"], g["fix"]) == (True, False, "connect")
    assert "Google Workspace" in g["reason"] and "User Settings → Integrations" in g["reason"]
    assert m["m365-mcp"]["fix"] == "admin" and "not enabled for this agent" in m["m365-mcp"]["reason"]
    assert m["m365-mcp"]["enabled"] is False and m["m365-mcp"]["connected"] is False
    assert "attach an instance of Blender" in m["blender-mcp"]["reason"]
    assert "waits for an admin" in m["file-tools"]["reason"]
    assert "disabled on this OtoDock" in m["camoufox"]["reason"]
    assert m["nowhere-mcp"]["fix"] == "admin" and "not installed" in m["nowhere-mcp"]["reason"]
    # The button's MCP is stored as its server key and answered under both names.
    assert m["display"]["fix"] == "none" and m["display-mcp"] == m["display"]
    # An account connected for THIS viewer turns the connect fix into none.
    accounts[("alice-sub", "google-workspace")] = [{"account_label": "d", "display_email": "d@example.com"}]
    assert status()["mcps"]["google-workspace"] == {"enabled": True, "connected": True, "fix": "none", "reason": ""}


def test_methods_run_as_the_forwarded_viewer_else_as_the_app(agent_tree):
    row = _row("board", ME)
    inst = _install(row)
    # The app alone: the app identity, a viewer of its own agent.
    r = _call(row["id"], "viewer.me", inst.token)
    assert r.status_code == 200 and r.json()["ok"], r.text
    me = r.json()["result"]
    assert me["sub"] == f"session:app:{row['id']}" and me["role"] == "viewer"
    # A forwarded viewer claim: that person.
    alice = _viewer_token(row["id"])
    me = _call(row["id"], "viewer.me", inst.token, viewer=alice).json()["result"]
    assert me["sub"] == "alice-sub" and me["role"] == "manager" and me["username"] == "alice"
    # The platform render's claim (it names the owner of a personal app)
    # runs nothing, sent by the page or forwarded by the server.
    render = app_tokens.mint(row["id"], app_tokens.PURPOSE_VIEWER, {
        "principal": "viewer", "sub": "alice-sub", "role": "manager", "external": False,
        "instance": "check", "render": "job-1"}, 120)
    assert _call(row["id"], "viewer.me", render).status_code == 403
    assert _call(row["id"], "viewer.me", inst.token, viewer=render).status_code == 403
    # A claim minted for another app is refused; a page's own token works
    # as the viewer; an agent session with a user runs as that user.
    other = _row("other", ME)
    assert _call(row["id"], "viewer.me", inst.token, viewer=_viewer_token(other["id"])).status_code == 400
    assert _call(row["id"], "viewer.me", alice).json()["result"]["sub"] == "alice-sub"
    sess = live_session_token("s-1", AGENT, "alice-sub")
    assert _call(row["id"], "viewer.me", sess).json()["result"]["sub"] == "alice-sub"
    # The claim an agent's call brought to the server, forwarded on: that
    # session's user, as the agent's own call; a no-user session's claim
    # runs as the app.
    import asyncio as _aio
    for token, want in ((sess, "alice-sub"),
                        (live_session_token("s-2", AGENT, ""), f"session:app:{row['id']}")):
        relayed = _aio.run(app_proxy._agent_caller(
            app_proxy.validate_session_token(token), row)).claim
        r = _call(row["id"], "viewer.me", inst.token, viewer=relayed)
        assert r.status_code == 200 and r.json()["result"]["sub"] == want, r.text
    # Undeclared, unapproved, unknown, cookie-only.
    assert _call(row["id"], "integrations.status", inst.token).status_code == 404
    assert _call(row["id"], "nope", inst.token).status_code == 404
    unapproved = _row("raw", ME, approve=False)
    unapproved_inst = _install(unapproved)
    assert _call(unapproved["id"], "viewer.me", unapproved_inst.token).status_code == 409
    assert client.post(f"/v1/apps/{row['id']}/platform/viewer.me", json={}).status_code == 401
    # A write needs a viewer behind the call — except the app telling its
    # own people (APPS.md "Handlers", 2026-09-18): the launch token alone
    # notifies the members of a shared app, never a named person.
    notify = _row("notify", [{"id": "n", "label": "N", "type": "platform", "method": "notifications.create"}])
    ninst = _install(notify)
    r = _call(notify["id"], "notifications.create", ninst.token, {"title": "x"})
    assert r.status_code == 200 and r.json()["result"]["to"] == "members", r.text
    r = _call(notify["id"], "notifications.create", ninst.token, {"title": "hello"},
              viewer=_viewer_token(notify["id"]))
    assert r.status_code == 200 and r.json()["ok"] and r.json()["result"]["delivered"]


def test_files_methods_follow_the_declared_prefixes_on_the_resolved_path(agent_tree):
    actions = [
        {"id": "r", "label": "Read", "type": "platform", "method": "files.read"},
        {"id": "l", "label": "List", "type": "platform", "method": "files.list"},
        {"id": "w", "label": "Write", "type": "platform", "method": "files.write"},
    ]
    row = _row("notes", actions, files={"read": ["workspace/notes"], "write": ["workspace/out"]})
    inst = _install(row)
    ws = agent_tree / "workspace"
    (ws / "notes").mkdir()
    (ws / "notes" / "a.md").write_text("hello")
    (ws / "secret.md").write_text("no")
    (ws / "notes" / "link.md").symlink_to(ws / "secret.md")
    alice = _viewer_token(row["id"])
    ok = _call(row["id"], "files.read", inst.token, {"path": "workspace/notes/a.md"}).json()
    assert ok["ok"] and ok["result"]["content"] == "hello" and ok["result"]["path"] == "workspace/notes/a.md"
    listing = _call(row["id"], "files.list", inst.token, {"path": "workspace/notes"}).json()["result"]
    assert [e["name"] for e in listing["entries"]] == ["a.md"]  # the symlink is not listed
    for bad in ("workspace/secret.md", "workspace/notes/../secret.md", "workspace/notes/link.md",
                "knowledge/x.md", "workspace/notes/.credentials/t.json", "config/agent.md",
                "app-data/shared/notes/app.db"):
        r = _call(row["id"], "files.read", inst.token, {"path": bad}).json()
        assert not r["ok"], bad
    # Writes: the floor is contributor, the viewer must be behind the call, the
    # write lands under the declared write prefix only and is announced.
    assert _call(row["id"], "files.write", inst.token,
                 {"path": "workspace/out/x.txt", "content": "v"}).status_code == 403
    _as(BOB)
    bob = _viewer_token(row["id"])
    assert _call(row["id"], "files.write", inst.token,
                 {"path": "workspace/out/x.txt", "content": "v"}, viewer=bob).status_code == 403
    _as(ALICE)
    r = _call(row["id"], "files.write", inst.token, {"path": "workspace/out/x.txt", "content": "v"},
              viewer=alice).json()
    assert r["ok"] and (ws / "out" / "x.txt").read_text() == "v" and "_written" not in r["result"]
    r = _call(row["id"], "files.write", inst.token, {"path": "workspace/notes/b.md", "content": "v"},
              viewer=alice).json()
    assert not r["ok"] and "write prefixes" in r["reason"]
    # The manifest gave files.write the workspace tier's floor by default.
    assert next(a for a in json.loads(row["actions"]) if a["method"] == "files.write")["min_role"] == "contributor"
    # A personal app's prefixes resolve under the owner's tree.
    # (The deploy validator stores a personal app's prefixes under {owner}.)
    mine = _row("mine", actions[:1], files={"read": ["users/{owner}/workspace/notes"]},
                username="alice", owner="alice-sub")
    minst = _install(mine)
    (agent_tree / "users/alice/workspace/notes").mkdir(parents=True)
    (agent_tree / "users/alice/workspace/notes/p.md").write_text("mine")
    assert json.loads(mine["files"])["read"] == ["users/{owner}/workspace/notes"]
    r = _call(mine["id"], "files.read", minst.token, {"path": "users/alice/workspace/notes/p.md"}).json()
    assert r["ok"] and r["result"]["content"] == "mine"
    assert not _call(mine["id"], "files.read", minst.token, {"path": "workspace/notes/a.md"}).json()["ok"]
    # A frame's own call (the cookie principal, the platform route of phase 1) reads too.
    r = client.post(f"/v1/apps/{row['id']}/catalog/files.read", json={"args": {"path": "workspace/notes/a.md"}})
    assert r.status_code == 200 and r.json()["result"]["content"] == "hello"


def test_files_answer_the_agents_members_only_never_a_share_alone(agent_tree):
    # A share admits to the app, not to the agent: a grantee with no place
    # on the agent gets a refusal the page can show, on the page's own route
    # and through the server forwarding their claim; a member who also
    # holds the share reads as before.
    from storage.sharing import share_store
    row = _row("notes", [{"id": "r", "label": "Read", "type": "platform", "method": "files.read"},
                         {"id": "l", "label": "List", "type": "platform", "method": "files.list"}],
               files={"read": ["workspace/notes"]})
    inst = _install(row)
    (agent_tree / "workspace" / "notes").mkdir()
    (agent_tree / "workspace" / "notes" / "a.md").write_text("member-only text")
    task_store.upsert_user("carol-sub", "carol-sub@test.com", "Carol", "member")
    carol = UserContext(sub="carol-sub", email="carol-sub@test.com", name="carol", role="member",
                        agents=[], agent_roles={})
    for grantee in ("carol-sub", "bob-sub"):
        share_store.create_internal_share(target_kind="app", target_id=row["id"],
                                          grantee_sub=grantee, created_by="alice-sub")
    read = {"args": {"path": "workspace/notes/a.md"}}
    _as(carol)
    for method, body in (("files.read", read), ("files.list", {"args": {"path": "workspace/notes"}})):
        r = client.post(f"/v1/apps/{row['id']}/catalog/{method}", json=body)
        assert r.status_code == 200 and r.json()["ok"] is False, r.text
        assert "not available" in r.json()["reason"] and "member-only" not in r.text
    carol_claim = _viewer_token(row["id"])
    r = _call(row["id"], "files.read", inst.token, read["args"], viewer=carol_claim).json()
    assert r["ok"] is False and "not available" in r["reason"]
    _as(BOB)
    r = client.post(f"/v1/apps/{row['id']}/catalog/files.read", json=read).json()
    assert r["ok"] and r["result"]["content"] == "member-only text"
    r = _call(row["id"], "files.read", inst.token, read["args"], viewer=_viewer_token(row["id"])).json()
    assert r["ok"] and r["result"]["content"] == "member-only text"
    # The app identity itself is the agent's own.
    assert _call(row["id"], "files.read", inst.token, read["args"]).json()["ok"]


def test_the_subscription_socket_takes_the_launch_token_and_the_agent_slice(agent_tree):
    row = _row("board", [{"id": "t", "label": "Tasks", "type": "data_feed", "feed": "tasks"},
                         {"id": "n", "label": "Inbox", "type": "data_feed", "feed": "notifications"}])
    inst = _install(row)
    from starlette.websockets import WebSocketDisconnect
    with client.websocket_connect(f"/v1/apps/{row['id']}/platform/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": _viewer_token(row["id"])}))
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    assert e.value.code == 4401
    with client.websocket_connect(f"/v1/apps/{row['id']}/platform/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": inst.token}))
        ws.send_text(json.dumps({"type": "catalog_subscribe", "feed": "tasks"}))
        assert ws.receive_json() == {"type": "catalog_ack", "feed": "tasks", "ok": True}
        ws.send_text(json.dumps({"type": "catalog_subscribe", "feed": "notifications"}))
        assert ws.receive_json()["ok"] is True
        ws.send_text(json.dumps({"type": "catalog_snapshot", "feed": "tasks"}))
        snap = ws.receive_json()
        assert snap["type"] == "catalog" and snap["feed"] == "tasks" and snap["snapshot"] == []
        assert snap["seq"] == 0
        # A user-scope delta never reaches the app; an agent-scope one does,
        # with the app's own sequence; an inbox delivery never does.
        catalog.emit(["alice-sub"], AGENT, "tasks", {"id": "t-user"}, shared=False)
        catalog.emit(["alice-sub"], AGENT, "tasks", {"id": "t-shared"}, shared=True)
        catalog.emit(["alice-sub"], "", "notifications", {"id": "n-1"}, shared=False)
        frame = ws.receive_json()
        assert frame["feed"] == "tasks" and frame["delta"]["id"] == "t-shared" and frame["seq"] == 1
        catalog.emit(["bob-sub"], AGENT, "tasks", {"id": "t-shared-2"}, shared=True)
        assert ws.receive_json()["seq"] == 2
        # Another agent's feed is not this app's.
        catalog.emit(["alice-sub"], "other-agent", "tasks", {"id": "x"}, shared=True)
        ws.send_text(json.dumps({"type": "catalog_snapshot", "feed": "tasks"}))
        assert ws.receive_json()["seq"] == 2
        assert catalog.app_subscription(row["id"]).feeds == {"tasks", "notifications"}
    assert catalog.app_subscription(row["id"]) is None


def test_a_server_reads_only_the_feeds_its_approved_manifest_declares(agent_tree):
    # The socket once served any platform feed: an app approved for Tasks
    # could subscribe to the agent's shared chats and file changes.
    row = _row("board", [{"id": "t", "label": "Tasks", "type": "data_feed", "feed": "tasks"}])
    inst = _install(row)
    url = f"/v1/apps/{row['id']}/platform/ws"
    with client.websocket_connect(url) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": inst.token}))
        for kind in ("catalog_subscribe", "catalog_snapshot"):
            for feed in ("sessions", "file_changes", "check_verdicts", "no-such-feed"):
                ws.send_text(json.dumps({"type": kind, "feed": feed}))
                assert ws.receive_json() == {"type": "catalog_ack", "feed": feed, "ok": False}
        ws.send_text(json.dumps({"type": "catalog_subscribe", "feed": "tasks"}))
        assert ws.receive_json()["ok"] is True
        # Dropping a feed never needs a declaration.
        ws.send_text(json.dumps({"type": "catalog_unsubscribe", "feed": "sessions"}))
        assert ws.receive_json()["ok"] is True
        assert catalog.app_subscription(row["id"]).feeds == {"tasks"}
        catalog.emit([], AGENT, "sessions", {"id": "chat-1", "created": True}, shared=True)
        catalog.emit([], AGENT, "tasks", {"id": "t-1"}, shared=True)
        assert ws.receive_json()["delta"]["id"] == "t-1"


def test_a_declared_feed_waits_for_the_approval_the_socket_reads_afresh(agent_tree):
    # A declared feed on a manifest nobody approved yet is refused, and the
    # approval counts on the open socket without a reconnect.
    row = _row("board", [{"id": "t", "label": "Tasks", "type": "data_feed", "feed": "tasks"}],
               approve=False)
    inst = _install(row)
    with client.websocket_connect(f"/v1/apps/{row['id']}/platform/ws") as ws:
        ws.send_text(json.dumps({"type": "auth", "token": inst.token}))
        for kind in ("catalog_subscribe", "catalog_snapshot"):
            ws.send_text(json.dumps({"type": kind, "feed": "tasks"}))
            assert ws.receive_json() == {"type": "catalog_ack", "feed": "tasks", "ok": False}
        task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), "alice-sub")
        ws.send_text(json.dumps({"type": "catalog_subscribe", "feed": "tasks"}))
        assert ws.receive_json()["ok"] is True
        ws.send_text(json.dumps({"type": "catalog_snapshot", "feed": "tasks"}))
        assert ws.receive_json()["type"] == "catalog"


def test_a_preview_or_a_reconnect_never_takes_the_live_servers_feeds(agent_tree):
    # The preview copy's server subscribes beside the live one, and a
    # socket's close takes down its own subscriptions only: the live
    # server kept its socket but heard nothing after either.
    row = _row("board", [{"id": "t", "label": "Tasks", "type": "data_feed", "feed": "tasks"}])
    live = _install(row)
    preview = app_supervisor.Instance(row_id=row["id"], name="preview", row=row,
                                      release_dir=config.get_agent_dir(AGENT),
                                      data_dir=config.get_agent_dir(AGENT), host_port=2, state="up")
    preview.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH,
                                    {"sub": f"app:{row['id']}", "instance": "preview"}, 3600)
    app_supervisor._instances[(row["id"], "preview")] = preview
    url = f"/v1/apps/{row['id']}/platform/ws"
    with client.websocket_connect(url) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": live.token}))
        ws.send_text(json.dumps({"type": "catalog_subscribe", "feed": "tasks"}))
        assert ws.receive_json()["ok"] is True
        with client.websocket_connect(url) as pws:
            pws.send_text(json.dumps({"type": "auth", "token": preview.token}))
            pws.send_text(json.dumps({"type": "catalog_subscribe", "feed": "tasks"}))
            assert pws.receive_json()["ok"] is True
            catalog.emit([], AGENT, "tasks", {"id": "both"}, shared=True)
            assert pws.receive_json()["delta"]["id"] == "both"
        sub = catalog.app_subscription(row["id"])
        assert sub is not None and sub.feeds == {"tasks"}
        assert catalog.app_subscription(row["id"], "preview") is None
        catalog.emit([], AGENT, "tasks", {"id": "after-preview"}, shared=True)
        assert ws.receive_json()["delta"]["id"] == "both"
        assert ws.receive_json()["delta"]["id"] == "after-preview"
    # A reconnect: the newer socket of the live process keeps its feeds
    # when the older one closes after it.
    old = client.websocket_connect(url).__enter__()
    old.send_text(json.dumps({"type": "auth", "token": live.token}))
    old.send_text(json.dumps({"type": "catalog_subscribe", "feed": "tasks"}))
    assert old.receive_json()["ok"] is True
    with client.websocket_connect(url) as new:
        new.send_text(json.dumps({"type": "auth", "token": live.token}))
        new.send_text(json.dumps({"type": "catalog_subscribe", "feed": "tasks"}))
        assert new.receive_json()["ok"] is True
        old.__exit__(None, None, None)
        sub = catalog.app_subscription(row["id"])
        assert sub is not None and sub.feeds == {"tasks"}
        catalog.emit([], AGENT, "tasks", {"id": "after-reconnect"}, shared=True)
        assert new.receive_json()["delta"]["id"] == "after-reconnect"


def test_a_preview_burst_never_spends_the_live_servers_platform_bucket(agent_tree, monkeypatch):
    # The preview copy runs code nobody approved: its calls count in a
    # bucket of their own, so the live server keeps its four a second.
    row = _row("board", ME)
    live = _install(row)
    preview = app_supervisor.Instance(row_id=row["id"], name="preview", row=row,
                                      release_dir=config.get_agent_dir(AGENT),
                                      data_dir=config.get_agent_dir(AGENT), host_port=2, state="up")
    preview.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH,
                                    {"sub": f"app:{row['id']}", "instance": "preview"}, 3600)
    app_supervisor._instances[(row["id"], "preview")] = preview
    monkeypatch.setattr(app_proxy, "PLATFORM_RATE", 2.0)
    codes = [_call(row["id"], "viewer.me", preview.token).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and codes[-1] == 429, codes
    assert _call(row["id"], "viewer.me", live.token).status_code == 200


def test_the_agent_slice_of_a_snapshot(agent_tree):
    import uuid
    from core.session.visibility import SHARED_CHAT_OWNER_PREFIX
    row = _row("board", [])
    shared_chat = task_store.create_chat(str(uuid.uuid4()), f"{SHARED_CHAT_OWNER_PREFIX}{AGENT}",
                                         AGENT, title="Team chat")
    task_store.create_chat(str(uuid.uuid4()), "alice-sub", AGENT, title="Alice's chat")
    rows = catalog.snapshot_for_app("sessions", AGENT, row)
    assert [r["id"] for r in rows] == [shared_chat["id"]]
    assert catalog.snapshot_for_app("notifications", AGENT, row) == []
    assert catalog.in_agent_slice("sessions", {"id": shared_chat["id"]}, None) is True
    assert catalog.in_agent_slice("file_changes", {"rel_path": "users/alice/workspace/x"}, None) is False
    assert catalog.in_agent_slice("file_changes", {"rel_path": "workspace/x"}, None) is True
    assert catalog.in_agent_slice("notifications", {"id": "n"}, True) is False
    assert os.path.basename(str(config.get_agent_dir(AGENT))) == AGENT


def test_an_agent_session_is_judged_at_its_row_not_its_platform_standing(agent_tree):
    """An agent session calling ``platform/`` is judged at its
    per-agent row (viewer without one), directly and when the app's server
    relays its claim; the call still runs as the session's user, and a
    person's own forwarded viewer claim keeps their full standing."""
    import asyncio as _aio
    floored = [{"id": "me", "label": "Me", "type": "platform", "method": "viewer.me",
                "min_role": "manager"}]
    row = _row("floored", floored)
    inst = _install(row)
    task_store.upsert_user("root-sub", "root@test.com", "Root", "admin")  # admin, no row
    sess = live_session_token("s-root", AGENT, "root-sub")
    assert _call(row["id"], "viewer.me", sess).status_code == 403
    relayed = _aio.run(app_proxy._agent_caller(app_proxy.validate_session_token(sess), row)).claim
    assert _call(row["id"], "viewer.me", inst.token, viewer=relayed).status_code == 403
    # The catalog twin already refused it (the real bearer resolves).
    app.dependency_overrides.pop(get_current_user, None)
    assert client.post(f"/v1/apps/{row['id']}/catalog/viewer.me", json={"args": None},
                       headers={"Authorization": f"Bearer {sess}"}).status_code == 403
    _as(ALICE)
    # A manager session passes the manager floor at its row and runs as its user.
    alice_sess = live_session_token("s-alice", AGENT, "alice-sub")
    r = _call(row["id"], "viewer.me", alice_sess)
    assert r.status_code == 200 and r.json()["result"]["sub"] == "alice-sub", r.text
    # A person's own claim, forwarded by the server: their full standing.
    assert _call(row["id"], "viewer.me", inst.token, viewer=_viewer_token(row["id"])).status_code == 200
    # A contributor's session still meets the default contributor floor of files.write.
    task_store.upsert_user("pm-sub", "pm@test.com", "Pm", "member")
    task_store.add_user_agent("pm-sub", AGENT, "contributor", "test")
    writer = _row("writer", [{"id": "w", "label": "W", "type": "platform", "method": "files.write"}],
                  files={"write": ["workspace/out"]})
    (agent_tree / "workspace" / "out").mkdir()
    r = _call(writer["id"], "files.write", live_session_token("s-pm", AGENT, "pm-sub"),
              {"path": "workspace/out/a.txt", "content": "hi"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert (agent_tree / "workspace" / "out" / "a.txt").read_text() == "hi"


def test_the_files_methods_never_follow_a_link_swapped_in_after_the_check(agent_tree, tmp_path, monkeypatch):
    """The apps TOCTOU sites: ``files.read`` opens the resolved, authorized
    path without following a link and ``files.write`` writes beneath the
    row's agent folder, so a link swapped in between the check and the use
    reaches nothing outside the tree."""
    from api.agents import files as files_api
    actions = [{"id": "r", "label": "Read", "type": "platform", "method": "files.read"},
               {"id": "w", "label": "Write", "type": "platform", "method": "files.write"}]
    row = _row("swap", actions, files={"read": ["workspace/notes"], "write": ["workspace/out"]})
    inst = _install(row)
    ws = agent_tree / "workspace"
    (ws / "notes").mkdir()
    (ws / "notes" / "a.md").write_text("hello")
    (ws / "out").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("SECRET-CONTENT")
    alice = _viewer_token(row["id"])
    real = files_api.safe_agent_path

    def swapping(agent_dir, agent, raw, user, writing=False):
        answer = real(agent_dir, agent, raw, user, writing=writing)
        if raw.startswith("workspace/notes"):
            (ws / "notes" / "a.md").unlink()
            (ws / "notes" / "a.md").symlink_to(outside / "secret.md")
        else:
            (ws / "out").rmdir()
            (ws / "out").symlink_to(outside)
        return answer

    monkeypatch.setattr(files_api, "safe_agent_path", swapping)
    r = _call(row["id"], "files.read", inst.token, {"path": "workspace/notes/a.md"}, viewer=alice)
    assert r.status_code == 200 and "SECRET-CONTENT" not in r.text and not r.json()["ok"], r.text
    r = _call(row["id"], "files.write", inst.token, {"path": "workspace/out/b.txt", "content": "x"},
              viewer=alice)
    assert r.status_code == 200 and not r.json()["ok"], r.text
    assert sorted(p.name for p in outside.iterdir()) == ["secret.md"]


def test_a_placed_agents_session_never_reaches_the_platform_route(agent_tree):
    """A session of an agent the app is placed in reaches the app's exports
    alone (APPS.md "Agents call apps"): the platform route refuses it,
    directly and relayed by the app's own server, so the home agent's
    declared files never answer another agent's session."""
    from storage.sharing import share_store
    other = "platform-other"
    row = _row("notes", ME + [{"id": "r", "label": "Read", "type": "platform", "method": "files.read"}],
               files={"read": ["workspace/notes"]})
    inst = _install(row)
    (agent_tree / "workspace" / "notes").mkdir()
    (agent_tree / "workspace" / "notes" / "a.md").write_text("member-only text")
    share_store.create_internal_share(target_kind="app", target_id=row["id"], created_by="alice-sub",
                                      grantee_kind=share_store.AGENT, grantee_agent=other,
                                      role_cap="editor", decision=share_store.ACCEPTED,
                                      decided_by="alice-sub")
    read = {"path": "workspace/notes/a.md"}
    for sess in (live_session_token("s-x", other, ""), live_session_token("s-bob", other, "bob-sub")):
        assert _call(row["id"], "viewer.me", sess).status_code == 403
        r = _call(row["id"], "files.read", sess, read)
        assert r.status_code == 403 and "member-only" not in r.text
    # The server relaying such a claim is refused too: the real claim, and
    # one spelled with the word the app's own sessions carry (the placement
    # marker says what it is, whatever the principal word).
    import asyncio as _aio
    real = _aio.run(app_proxy._agent_caller(
        app_proxy.validate_session_token(live_session_token("s-bob2", other, "bob-sub")), row)).claim
    spelled = app_tokens.mint(row["id"], app_tokens.PURPOSE_CALLER, {
        "principal": "agent", "sub": "session:s-x", "username": "", "role": "editor",
        "agent": other, "session": "s-x", "external": False,
        "placement": {"kind": "agent", "share_id": "sh", "from_agent": AGENT, "agent": other,
                      "role_cap": "editor"}}, 60)
    for placed in (real, spelled):
        assert _call(row["id"], "viewer.me", inst.token, viewer=placed).status_code == 403
        r = _call(row["id"], "files.read", inst.token, read, viewer=placed)
        assert r.status_code == 403 and "member-only" not in r.text
    # The home agent's own no-user session still runs as the app identity.
    assert _call(row["id"], "files.read", live_session_token("s-own", AGENT, ""), read).json()["ok"]


# ── the audience and the per-viewer slice (APPS.md "Platform catalog") ──────

AUDIENCE = [{"id": "aud", "label": "Who", "type": "platform", "method": "app.audience"}]
SLICE = [{"id": "r", "label": "Read", "type": "platform", "method": "viewer.data.read"},
         {"id": "w", "label": "Write", "type": "platform", "method": "viewer.data.write"}]


def _file_row(slug: str, actions: list[dict]) -> dict:
    from api.apps import manifest as _mf
    actions_json, err = _mf.validate_actions(actions, AGENT, shared=True)
    assert actions_json is not None, err
    row = task_store.upsert_app(AGENT, "", None, slug, title=slug.title(),
                                rel_path=f"workspace/apps/{slug}.html", actions_json=actions_json)
    task_store.approve_app_actions(row["id"], task_store.manifest_sig(row), "alice-sub")
    return task_store.get_app(row["id"])


def _catalog(app_id: str, method: str, args=None):
    from api.apps import apps as apps_api
    apps_api._fire_rate.clear()
    return client.post(f"/v1/apps/{app_id}/catalog/{method}", json={"args": args})


def test_the_audience_method_names_who_uses_the_app(agent_tree):
    """``app.audience`` (APPS.md "Platform catalog"): the home agent's
    members, the agents a share placed the app in with their members at the
    capped role, and the people it is shared with; read unattended by the
    app itself (the live instance alone), floored at editor for a person."""
    from storage.agents import agent_store
    from storage.sharing import share_store
    other = "platform-other"
    agent_store.create_agent(other, "Other Desk", created_by="alice-sub")
    row = _row("register", AUDIENCE)
    inst = _install(row)
    # The floor is clamped to editor at validation whatever the author wrote.
    assert [a.get("min_role") for a in json.loads(row["actions"])] == ["editor"]
    task_store.upsert_user("carol-sub", "carol-sub@test.com", "Carol", "member")
    task_store.upsert_user("dan-sub", "dan-sub@test.com", "Dan", "member")
    task_store.add_user_agent("dan-sub", other, "editor", "test")
    task_store.add_user_agent("bob-sub", other, "manager", "test")
    share_store.create_internal_share(target_kind="app", target_id=row["id"], created_by="alice-sub",
                                      grantee_sub="carol-sub", role_cap="viewer")
    theirs = share_store.create_internal_share(target_kind="app", target_id=row["id"],
                                               created_by="alice-sub", grantee_sub="dan-sub",
                                               role_cap="editor")
    share_store.set_decision(theirs["id"], share_store.ACCEPTED, "dan-sub", placed_agent=other)
    team = share_store.create_internal_share(target_kind="app", target_id=row["id"],
                                             created_by="alice-sub", grantee_kind=share_store.AGENT,
                                             grantee_agent=other, role_cap="editor",
                                             decision=share_store.ACCEPTED, decided_by="alice-sub")
    r = _call(row["id"], "app.audience", inst.token)
    assert r.status_code == 200 and r.json()["ok"], r.text
    aud = r.json()["result"]
    assert {(m["sub"], m["role"], m["via"]) for m in aud["members"]} == {
        ("alice-sub", "manager", "membership"), ("bob-sub", "viewer", "membership")}
    assert {m["username"] for m in aud["members"]} == {"alice", "bob"}
    assert [(p["agent"], p["agent_name"], p["kind"], p["share_id"], p["role_cap"], p["via"], p["department"])
            for p in aud["placements"]] == [(other, "Other Desk", "agent", team["id"], "editor", "placement", None)]
    assert {(m["sub"], m["role"]) for m in aud["placements"][0]["members"]} == {
        ("dan-sub", "editor"), ("bob-sub", "editor")}
    assert {(g["sub"], g["role"], g["decision"], g["placed_agent"], g["via"]) for g in aud["grantees"]} == {
        ("carol-sub", "viewer", "pending", "", "share"), ("dan-sub", "editor", "accepted", other, "share")}
    assert aud["truncated"] is False and "email" not in r.text
    # The preview copy's launch token never reads it; a bearer on the page
    # route is a viewer and the editor floor refuses it; a viewer member's
    # forwarded claim is refused at the floor too.
    preview = app_supervisor.Instance(row_id=row["id"], name="preview", row=row,
                                      release_dir=config.get_agent_dir(AGENT),
                                      data_dir=config.get_agent_dir(AGENT), host_port=1, state="up")
    preview.token = app_tokens.mint(row["id"], app_tokens.PURPOSE_LAUNCH,
                                    {"sub": f"app:{row['id']}", "instance": "preview"}, 3600)
    app_supervisor._instances[(row["id"], "preview")] = preview
    assert _call(row["id"], "app.audience", preview.token).status_code == 403
    _as(BOB)
    assert _catalog(row["id"], "app.audience").status_code == 403
    _as(ALICE)
    assert _call(row["id"], "app.audience", inst.token, viewer=_bob_claim(row["id"])).status_code == 403
    app.dependency_overrides.pop(get_current_user, None)
    sess = live_session_token("s-bearer", AGENT, "alice-sub")
    from api.apps import apps as apps_api
    apps_api._fire_rate.clear()
    assert client.post(f"/v1/apps/{row['id']}/catalog/app.audience", json={"args": None},
                       headers={"Authorization": f"Bearer {sess}"}).status_code == 403
    _as(ALICE)
    # A person reads it page-side as an editor or manager of the app's OWN
    # agent (or an admin); a placed editor passes the floor and is refused
    # by the rule, and reads it through the app's server instead.
    assert _catalog(row["id"], "app.audience").json()["ok"] is True
    assert _call(row["id"], "app.audience", inst.token, viewer=_viewer_token(row["id"])).json()["ok"] is True
    _as(UserContext(sub="dan-sub", email="dan-sub@test.com", name="dan", role="member",
                    agents=[other], agent_roles={other: "editor"}))
    r = _catalog(row["id"], "app.audience")
    assert r.status_code == 200 and r.json()["ok"] is False and "own editors" in r.json()["reason"]
    _as(UserContext(sub="root-sub", email="root@test.com", name="root", role="admin", agents=[]))
    assert _catalog(row["id"], "app.audience").json()["ok"] is True
    _as(ALICE)
    # A session of the home agent with no person reads it as the app does;
    # one carrying a viewer is judged at that row.
    assert _call(row["id"], "app.audience", live_session_token("s-own", AGENT, "")).json()["ok"] is True
    assert _call(row["id"], "app.audience", live_session_token("s-bob", AGENT, "bob-sub")).status_code == 403
    assert _call(row["id"], "app.audience", live_session_token("s-alice", AGENT, "alice-sub")).json()["ok"] is True
    # While the directory is closed to members, every reader but an admin
    # gets subs and roles with the names blank; the app itself too.
    task_store.set_platform_setting("user_directory_visible_to_members", "0")
    try:
        for aud in (_catalog(row["id"], "app.audience").json()["result"],
                    _call(row["id"], "app.audience", inst.token).json()["result"]):
            assert {m["sub"] for m in aud["members"]} == {"alice-sub", "bob-sub"}
            assert all(m["username"] == "" and m["display_name"] == "" for m in aud["members"])
            assert all(g["username"] == "" for g in aud["grantees"])
        _as(UserContext(sub="root-sub", email="root@test.com", name="root", role="admin", agents=[]))
        assert {m["username"] for m in _catalog(row["id"], "app.audience").json()["result"]["members"]} == {"alice", "bob"}
    finally:
        task_store.set_platform_setting("user_directory_visible_to_members", "1")
    _as(ALICE)
    # A revoked share leaves the list at once (no cache).
    share_store.revoke_share(team["id"])
    assert _call(row["id"], "app.audience", inst.token).json()["result"]["placements"] == []


def _bob_claim(app_id: str) -> str:
    _as(BOB)
    try:
        return _viewer_token(app_id)
    finally:
        _as(ALICE)


def test_the_per_viewer_slice_is_each_viewers_own(agent_tree, monkeypatch):
    """``viewer.data.read`` / ``viewer.data.write`` (APPS.md "Platform
    catalog"): a single-file app's page keeps one small document per
    viewer, written by that viewer alone and read by nobody else; a
    bearer and a folder app are refused, and the files live beside the
    app's data directory, never inside a server's mount."""
    import re
    from services.apps import app_deploy, releases
    from storage.sharing import share_store
    row = _file_row("notes", SLICE)
    url = f"/v1/apps/{row['id']}"
    assert _catalog(row["id"], "viewer.data.read").json() == {"ok": True, "result": {"doc": {}, "rev": 0}}
    r = _catalog(row["id"], "viewer.data.write", {"doc": {"theme": "dark", "font": "small"}})
    assert r.json() == {"ok": True, "result": {"doc": {"theme": "dark", "font": "small"}, "rev": 1}}, r.text
    r = _catalog(row["id"], "viewer.data.write", {"patch": {"font": None, "lang": "el"}})
    assert r.json()["result"] == {"doc": {"theme": "dark", "lang": "el"}, "rev": 2}
    assert _catalog(row["id"], "viewer.data.read").json()["result"]["rev"] == 2
    # bob's own slice is empty until he writes; alice's stays hers.
    _as(BOB)
    assert _catalog(row["id"], "viewer.data.read").json()["result"] == {"doc": {}, "rev": 0}
    assert _catalog(row["id"], "viewer.data.write", {"doc": {"mine": 1}}).json()["result"]["rev"] == 1
    _as(ALICE)
    assert _catalog(row["id"], "viewer.data.read").json()["result"]["doc"] == {"theme": "dark", "lang": "el"}
    # A person a share admits, with no row on the agent, keeps their own too.
    task_store.upsert_user("carol-sub", "carol-sub@test.com", "Carol", "member")
    share_store.create_internal_share(target_kind="app", target_id=row["id"], created_by="alice-sub",
                                      grantee_sub="carol-sub")
    _as(UserContext(sub="carol-sub", email="carol-sub@test.com", name="carol", role="member",
                    agents=[], agent_roles={}))
    assert _catalog(row["id"], "viewer.data.write", {"doc": {"c": 1}}).json()["result"]["rev"] == 1
    assert _catalog(row["id"], "viewer.data.read").json()["result"]["doc"] == {"c": 1}
    _as(ALICE)
    # The files sit beside the app's data directory (a server's mount never
    # holds them), named by a hash, never a sub.
    folder = releases.app_data_dir(row).parent / "notes.viewers"
    assert not (releases.app_data_dir(row) / "viewers").exists()
    names = sorted(p.name for p in folder.iterdir())
    assert len(names) == 3 and all(re.fullmatch(r"[0-9a-f]{32}\.json", n) for n in names)
    assert "alice" not in "".join(names) and "sub" not in "".join(names)
    # A later app pinned under the slug finds the documents (the names are
    # keyed by the slug and the viewer, as a folder app's database is by the
    # slug), so a hard unpin loses nothing.
    task_store.delete_app(row["id"])
    again = _file_row("notes", SLICE)
    assert again["id"] != row["id"]
    assert _catalog(again["id"], "viewer.data.read").json()["result"] == {
        "doc": {"theme": "dark", "lang": "el"}, "rev": 2}
    row = again
    url = f"/v1/apps/{row['id']}"
    # The cap on the files an app keeps (placed viewers spend the home
    # agent's quota): a fourth viewer's first write is refused at 3.
    from services.apps import viewer_data
    monkeypatch.setattr(viewer_data, "VIEWER_FILES_MAX", 3)
    task_store.upsert_user("dan-sub", "dan-sub@test.com", "Dan", "member")
    task_store.add_user_agent("dan-sub", AGENT, "viewer", "test")
    _as(UserContext(sub="dan-sub", email="dan-sub@test.com", name="dan", role="member",
                    agents=[AGENT], agent_roles={AGENT: "viewer"}))
    r = _catalog(row["id"], "viewer.data.write", {"doc": {"d": 1}})
    assert r.json()["ok"] is False and "3 viewers" in r.json()["reason"]
    _as(ALICE)
    assert _catalog(row["id"], "viewer.data.write", {"patch": {"again": True}}).json()["result"]["rev"] == 3
    monkeypatch.setattr(viewer_data, "VIEWER_FILES_MAX", 1000)
    # A damaged file refuses a patch and takes a whole document; a number
    # JSON cannot carry is refused before anything is written.
    viewer_data.file_for(row, "alice-sub").write_text("{not json")
    assert _catalog(row["id"], "viewer.data.read").json()["ok"] is False
    assert _catalog(row["id"], "viewer.data.write", {"patch": {"x": 1}}).json()["ok"] is False
    assert _catalog(row["id"], "viewer.data.write", {"doc": {"fresh": 1}}).json()["result"] == {
        "doc": {"fresh": 1}, "rev": 1}
    with pytest.raises(ValueError, match="JSON"):
        viewer_data.write(row, "alice-sub", doc={"n": float("nan")})
    # The limits: neither doc nor patch, the state document's size, a bearer
    # (a session token) and a folder app.
    assert _catalog(row["id"], "viewer.data.write", {}).json()["ok"] is False
    from services.apps import viewer_data
    with pytest.raises(ValueError, match="64 KB"):
        viewer_data.write(row, "alice-sub", doc={"k": "x" * (64 * 1024)})
    deep: dict = {}
    for _ in range(17):
        deep = {"d": deep}
    r = _catalog(row["id"], "viewer.data.write", {"doc": deep})
    assert r.json()["ok"] is False and "deeper" in r.json()["reason"]
    # The page shows the reason to the viewer about their own document.
    assert "your saved data" in r.json()["reason"] and "state document" not in r.json()["reason"]
    assert _catalog(row["id"], "viewer.data.read").json()["result"] == {"doc": {"fresh": 1}, "rev": 1}
    app.dependency_overrides.pop(get_current_user, None)
    sess = live_session_token("s-bearer", AGENT, "alice-sub")
    from api.apps import apps as apps_api
    apps_api._fire_rate.clear()
    r = client.post(f"{url}/catalog/viewer.data.read", json={"args": None},
                    headers={"Authorization": f"Bearer {sess}"})
    assert r.status_code == 200 and r.json()["ok"] is False and "page" in r.json()["reason"]
    _as(ALICE)
    folder_app = _row("board", SLICE)
    r = _catalog(folder_app["id"], "viewer.data.read")
    assert r.json()["ok"] is False and "database" in r.json()["reason"]
    with pytest.raises(app_deploy.DeployError, match="single-file"):
        app_deploy.validate_app_json({"actions": SLICE}, AGENT, True)
    # On the platform route a person's own page is required: the launch token
    # alone and an agent session carrying a person are refused; a page's
    # forwarded claim reaches that person's slice.
    inst = _install(folder_app)
    assert _call(folder_app["id"], "viewer.data.read", inst.token).status_code == 403
    assert _call(folder_app["id"], "viewer.data.read", live_session_token("s-a", AGENT, "alice-sub")).status_code == 403
    r = _call(folder_app["id"], "viewer.data.read", inst.token, viewer=_viewer_token(folder_app["id"]))
    assert r.status_code == 200 and r.json()["ok"] is False and "database" in r.json()["reason"]
