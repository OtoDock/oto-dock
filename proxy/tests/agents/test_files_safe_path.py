"""``safe_agent_path`` — the file API's single path gate.

Canonicalize, confine to the agent root, then role-check the RESOLVED
agent-relative path. Locks the traversal (400), symlink-escape (403) and
in-tree-symlink (allowed, authorized at the target) verdicts.
"""

import os

import pytest
from fastapi import HTTPException

from api.agents.files import safe_agent_path
from auth.providers import UserContext


def _manager() -> UserContext:
    return UserContext(
        sub="user-mgr", email="m@t.com", name="M", role="creator",
        agents=["test-agent"], agent_roles={"test-agent": "manager"},
    )


def _editor() -> UserContext:
    return UserContext(
        sub="user-ed", email="e@t.com", name="E", role="creator",
        agents=["test-agent"], agent_roles={"test-agent": "editor"},
    )


@pytest.fixture
def agent_dir(tmp_path, monkeypatch):
    from storage import database as task_store
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: "mgr")
    d = tmp_path / "agents" / "test-agent"
    (d / "workspace").mkdir(parents=True)
    (d / "config").mkdir()
    return d


def test_plain_workspace_path_resolves(agent_dir):
    resolved, uname = safe_agent_path(
        agent_dir, "test-agent", "workspace/notes.md", _manager())
    assert resolved == agent_dir.resolve() / "workspace" / "notes.md"
    assert uname == "mgr"


def test_dotdot_segment_is_400(agent_dir):
    with pytest.raises(HTTPException) as ei:
        safe_agent_path(
            agent_dir, "test-agent", "workspace/../../etc/passwd", _manager())
    assert ei.value.status_code == 400


def test_symlink_escaping_the_agent_is_403(agent_dir, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, agent_dir / "workspace" / "link")
    with pytest.raises(HTTPException) as ei:
        safe_agent_path(agent_dir, "test-agent", "workspace/link/secret", _manager())
    assert ei.value.status_code == 403


def test_symlink_inside_the_agent_is_authorized_at_its_target(agent_dir):
    """``workspace/link → config``: the role check sees config/, not the
    workspace scope the caller named — a manager passes and gets the real
    location, an editor is refused."""
    os.symlink(agent_dir / "config", agent_dir / "workspace" / "link")
    resolved, _ = safe_agent_path(
        agent_dir, "test-agent", "workspace/link/agent.md", _manager())
    assert resolved == agent_dir.resolve() / "config" / "agent.md"
    with pytest.raises(HTTPException) as ei:
        safe_agent_path(agent_dir, "test-agent", "workspace/link/agent.md", _editor())
    assert ei.value.status_code == 403


# --- a no-user session: judged by its grant (the tree its sandbox mounts) ----

SID = "11111111-2222-4333-8444-000000000006"


def _task_session(sid: str = SID) -> UserContext:
    """The principal of an agent-scope task's token (no person on it)."""
    from auth import roles
    from auth.providers import SESSION_SUB_PREFIX
    return UserContext(
        sub=f"{SESSION_SUB_PREFIX}{sid}", email="session@internal", name="Session Token",
        role=roles.SERVICE, agents=[], is_api_key=True, session_id=sid, agent="test-agent",
    )


def _context(monkeypatch, sid: str = SID, **over):
    """Register the session's live context, as every layer does before the
    spawn (the store itself, so nothing is persisted)."""
    from auth import roles
    from auth.path_policy import SecurityContext
    from core.session import session_state
    ctx = SecurityContext(**{"role": roles.MANAGER, "username": "", "agent": "test-agent",
                             "is_admin_agent": False, "session_scope": "agent",
                             "config_visible": False, **over})
    monkeypatch.setitem(session_state._session_security, sid, ctx)
    return ctx


def _refused(agent_dir, path, user, *, writing=False, **kw) -> bool:
    try:
        safe_agent_path(agent_dir, "test-agent", path, user, writing=writing, **kw)
    except HTTPException as e:
        assert e.status_code == 403
        return True
    return False


def test_a_task_session_reads_and_writes_its_workspace_and_reads_knowledge(agent_dir, monkeypatch):
    _context(monkeypatch)
    task = _task_session()
    assert not _refused(agent_dir, "workspace/report.md", task)
    assert not _refused(agent_dir, "workspace/report.md", task, writing=True)
    assert not _refused(agent_dir, "knowledge/notes.md", task)


def test_a_task_session_is_refused_config_and_every_persons_folder(agent_dir, monkeypatch):
    """Its sandbox mounts neither (no ``/config`` for a service session, no
    ``/users``), whatever role label the task runs with: an admin-only
    agent's tasks run as admin and are still refused."""
    from auth import roles
    for role in (roles.MANAGER, roles.ADMIN):
        _context(monkeypatch, role=role)
        task = _task_session()
        for path in ("config/agent.md", "users/alice/notes.md", "users", "scratch.md",
                     "knowledge/.credentials/left.json"):
            assert _refused(agent_dir, path, task), (role, path)
            assert _refused(agent_dir, path, task, writing=True), (role, path)


def test_a_credentials_folder_is_refused_to_every_principal(agent_dir):
    """Every session's sandbox masks ``.credentials`` whatever is left in
    it: the files API refuses the folder to a person and the master key too."""
    from auth import roles
    master = UserContext(sub="api-key", email="api@internal", name="API Key",
                         role=roles.ADMIN, agents=[], is_api_key=True)
    for user in (_manager(), master):
        for path in ("knowledge/.credentials/left.json", "users/mgr/.credentials/a.json"):
            assert _refused(agent_dir, path, user), path


def test_knowledge_is_written_only_with_the_manager_provenance_grant(agent_dir, monkeypatch):
    task = _task_session()
    _context(monkeypatch)
    assert _refused(agent_dir, "knowledge/new.md", task, writing=True)
    _context(monkeypatch, knowledge_rw=True)
    assert not _refused(agent_dir, "knowledge/new.md", task, writing=True)


def test_a_judge_writes_nothing_and_a_viewer_tier_task_writes_no_workspace(agent_dir, monkeypatch):
    from auth import roles
    task = _task_session()
    _context(monkeypatch, read_only=True)
    assert not _refused(agent_dir, "workspace/a.md", task)
    assert _refused(agent_dir, "workspace/a.md", task, writing=True)
    _context(monkeypatch, role=roles.VIEWER)
    assert _refused(agent_dir, "workspace/a.md", task, writing=True)


def test_a_context_that_shows_config_opens_it_at_the_owner_tier(agent_dir, monkeypatch):
    from auth import roles
    task = _task_session()
    _context(monkeypatch, config_visible=True)
    assert not _refused(agent_dir, "config/agent.md", task, writing=True)
    _context(monkeypatch, config_visible=True, role=roles.EDITOR)
    assert not _refused(agent_dir, "config/agent.md", task)
    assert _refused(agent_dir, "config/agent.md", task, writing=True)


def test_a_personal_only_agents_task_still_writes_its_workspace(agent_dir, monkeypatch):
    """Its sandbox mounts ``/workspace`` whatever the agent's mode."""
    _context(monkeypatch, available_scopes=("user",))
    assert not _refused(agent_dir, "workspace/out.md", _task_session(), writing=True)


def test_a_live_session_with_no_context_is_refused(agent_dir):
    assert _refused(agent_dir, "workspace/a.md", _task_session("no-context-session"))


def test_the_app_identity_keeps_its_own_gate_and_the_master_key_full_access(agent_dir):
    """The catalog's own call for the app identity (its declared prefixes
    are its gate) and the master key are not judged by a grant."""
    from auth import roles
    from services.apps import catalog
    row = {"id": "app-1", "agent": "test-agent", "title": "App"}
    app = catalog.app_principal(row)
    assert catalog.is_app_identity(row, app)
    assert not _refused(agent_dir, "config/agent.md", app, app_identity=True)
    assert _refused(agent_dir, "config/agent.md", app)
    master = UserContext(sub="api-key", email="api@internal", name="API Key",
                         role=roles.ADMIN, agents=[], is_api_key=True)
    assert not _refused(agent_dir, "users/alice/notes.md", master, writing=True)


# --- the routes that gave a no-user session the admin tier ---------------------

@pytest.fixture
def session_app(tmp_path, monkeypatch):
    """The files routes with the caller a task session whose context is
    registered, over an agent tree with a person's folder and config."""
    from fastapi import FastAPI

    import config
    from api.agents import agents
    from auth.providers import get_current_user
    from storage import database as task_store
    from storage.agents import agent_store
    agents_dir = tmp_path / "agents"
    root = agents_dir / "test-agent"
    for d in ("workspace/inbox", "knowledge", "config", "users/alice"):
        (root / d).mkdir(parents=True)
    (root / "workspace" / "w.md").write_text("w")
    (root / "knowledge" / "k.md").write_text("k")
    (root / "config" / "agent.md").write_text("persona")
    (root / "users" / "alice" / "private.md").write_text("alice's")
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(agent_store, "agent_exists", lambda name: name == "test-agent")
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: None)
    _context(monkeypatch)
    task = _task_session()

    async def _user():
        return task
    app = FastAPI()
    app.include_router(agents.router)
    app.dependency_overrides[get_current_user] = _user
    from fastapi.testclient import TestClient
    return TestClient(app), root


def _paths(tree: list[dict]) -> set[str]:
    out = set()
    for node in tree:
        out.add(node["path"])
        out |= _paths(node.get("children") or [])
    return out


def test_the_list_shows_a_task_session_only_its_mounted_tree(session_app):
    client, _root = session_app
    r = client.get("/v1/agents/test-agent/files")
    assert r.status_code == 200
    seen = _paths(r.json()["tree"])
    assert "workspace/w.md" in seen and "knowledge/k.md" in seen
    assert not any(p.startswith(("config", "users")) for p in seen), seen


def test_move_copy_and_zip_refuse_a_task_session_a_persons_file_and_config(session_app):
    client, root = session_app
    for src in ("users/alice/private.md", "config/agent.md"):
        r = client.post("/v1/agents/test-agent/copy",
                        json={"src_paths": [src], "dest_dir": "workspace/inbox"})
        assert r.status_code == 403, (src, r.text)
        r = client.post("/v1/agents/test-agent/move",
                        json={"src_paths": [src], "dest_dir": "workspace/inbox"})
        assert r.status_code == 403, (src, r.text)
        assert client.post("/v1/agents/test-agent/zip", json={"paths": [src]}).status_code == 403
        assert client.post("/v1/agents/test-agent/zip-url", json={"paths": [src]}).status_code == 403
    assert not any((root / "workspace" / "inbox").iterdir())
    assert (root / "users" / "alice" / "private.md").exists()
    # Its own workspace still copies, and a download link carries the
    # session, judged again at the download.
    r = client.post("/v1/agents/test-agent/copy",
                    json={"src_paths": ["workspace/w.md"], "dest_dir": "workspace/inbox"})
    assert r.status_code == 200 and (root / "workspace" / "inbox" / "w.md").exists()
    r = client.post("/v1/agents/test-agent/zip-url", json={"paths": ["workspace/w.md"]})
    assert r.status_code == 200
    url = r.json()["download_url"]
    assert client.get(url).status_code == 200
    from core.session import session_state
    session_state._session_security.pop(SID)
    assert client.get(url).status_code == 403


# --- the grant of a context the task builder itself registers ------------------

def _stub_spawn_side(monkeypatch):
    """Stub what the task builder needs only to spawn (config dirs, the MCP
    set, the prompt, the engine login, the target); the identity, the
    agent's visibility and its admin-only flag stay real."""
    from pathlib import Path as _Path
    from types import SimpleNamespace

    import core.layers.cli.config_dir as cli_cd
    from core import placement
    from core.config import task_config_builder as tcb
    monkeypatch.setattr(cli_cd, "ensure_persistent_claude_dir",
                        lambda *a, **k: _Path("/tmp/agents/x/workspace/.claude"))
    monkeypatch.setattr(tcb.remote_status, "resolve_execution_target", lambda *a, **k: ("local", None))
    monkeypatch.setattr(tcb.remote_store, "placement_of", lambda *a, **k: placement.LOCAL_PLACEMENT)
    monkeypatch.setattr(tcb.remote_store, "get_target_browser_settings", lambda *a, **k: None)
    monkeypatch.setattr(tcb.mcp_registry, "build_session_mcp_config",
                        lambda *a, **k: (None, {}, {}, {}, set()))
    monkeypatch.setattr(tcb.mcp_registry, "get_agent_mcps", lambda *a, **k: [])

    async def _no_dyn(*a, **k):
        return []
    monkeypatch.setattr(tcb.dynamic_context, "get_dynamic_contexts", _no_dyn)
    monkeypatch.setattr(tcb.config, "build_agent_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr("configuration.models.get_cli_model", lambda *a, **k: "m")
    monkeypatch.setattr(tcb.config, "get_cli_effort", lambda *a, **k: "")
    monkeypatch.setattr(tcb.subscription_pool, "resolve_subscription_env",
                        lambda *a, **k: ("sub-test", {}))
    from core.remote import satellite_connection as sc
    monkeypatch.setattr(sc, "get_connection_manager",
                        lambda: SimpleNamespace(satellite_supports_pty=lambda *_: True))
    return tcb


@pytest.mark.parametrize("admin_only", [False, True])
def test_the_context_the_task_builder_registers_grants_the_workspace_only(
        temp_db, tmp_path, monkeypatch, admin_only):
    """An agent-scope one-time task of a real agent (an admin-only one runs
    at the admin label): the context the builder makes writes the
    workspace and never opens config or a person's folder."""
    import asyncio
    from types import SimpleNamespace

    from storage.agents import agent_store
    slug = "grant-real" + ("-admin" if admin_only else "")
    agent_store.create_agent(slug, "Grant Real", admin_only=admin_only)
    tcb = _stub_spawn_side(monkeypatch)
    task = SimpleNamespace(
        scope="agent", created_by="user-admin", task_type="one_time", notification_mode="none",
        override_model=None, override_execution_path=None, override_execution_mode=None,
        judge=None, execution_target_override="", agent=slug, id="dyn-grant", name="t",
        prompt="p", timeout_seconds=60, extra_context=[], continue_session=None,
        on_complete_agent=None)
    sid = SID if not admin_only else SID[:-1] + "7"
    cfg = asyncio.run(tcb.build_task_agent_config(slug, task, sid))
    from core.session import session_state
    monkeypatch.setitem(session_state._session_security, sid, cfg.security_context)
    root = tmp_path / "agents" / slug
    for d in ("workspace", "config", "users/alice"):
        (root / d).mkdir(parents=True)
    task_session = _task_session(sid)
    task_session.agent = slug

    def refused(path, writing=False):
        try:
            safe_agent_path(root, slug, path, task_session, writing=writing)
        except HTTPException as e:
            assert e.status_code == 403
            return True
        return False
    assert not refused("workspace/out.md", writing=True)
    assert refused("config/agent.md") and refused("config/agent.md", writing=True)
    assert refused("users/alice/notes.md")
    assert cfg.security_context.role == ("admin" if admin_only else "manager")
