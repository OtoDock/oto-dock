"""End-to-end HTTP tests for the memory API (``/v1/internal/memory/op``).

Exercises the memory-tool command contract + role matrix via FastAPI's
TestClient, minting session JWTs the same way real MCPs would. File content
lands in the per-test ``AGENTS_DIR`` (a tempdir from conftest), so
assertions inspect actual topic files + the generated ``MEMORY.md`` index
under ``knowledge/memory/`` and ``users/{u}/context/memory/``.

Contract-level outcomes (including errors like "File already exists") are
HTTP 200 with ``{output, is_error, warnings}`` — the MCP relays ``output``
verbatim. HTTP errors are reserved for auth-shape failures.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(temp_db):
    from app import app
    return TestClient(app)


def _mint_jwt(session_id: str, agent: str, user_sub: str | None = None) -> str:
    from auth.session_token import create_session_token
    return create_session_token(session_id, agent, user_sub or "")


def _admin_cookie() -> dict[str, str]:
    """Session cookie for the seeded admin user. The clear-* endpoints are
    admin/manager-gated via get_current_user (NOT the S2S master key)."""
    from auth.providers import create_session_jwt
    token = create_session_jwt("user-admin", "admin@test.com", "Admin User", "admin")
    return {"Cookie": f"session={token}"}


def _seed_agent(slug: str, *, default_scope: str = "user") -> None:
    from storage import agent_store
    if not agent_store.agent_exists(slug):
        agent_store.create_agent(
            slug, slug.replace("-", " ").title(),
            default_scope=default_scope,
        )


def _agent_mem_dir(slug: str) -> Path:
    import config
    return config.AGENTS_DIR / slug / "knowledge" / "memory"


def _user_mem_dir(slug: str, username: str) -> Path:
    import config
    return (
        config.AGENTS_DIR / slug / "users" / username / "context" / "memory"
    )


def _assign_roles(user_sub: str, agent_roles: dict[str, str]) -> None:
    """Ensure the user row exists, then set their full agent/role map
    (``set_user_agents`` REPLACES the list — assign all agents at once)."""
    from datetime import datetime, timezone
    from storage import database as task_store
    from storage.pg import get_conn
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as c:
        c.execute(
            "INSERT INTO users (sub, email, name, role, created_at, last_login) "
            "VALUES (%s, %s, %s, 'member', %s, %s) ON CONFLICT (sub) DO NOTHING",
            (user_sub, f"{user_sub}@test.com", user_sub, now, now),
        )
        c.commit()
    task_store.set_user_agents(
        user_sub, list(agent_roles), assigned_by="user-admin",
        agent_roles=agent_roles,
    )
    # Set the user's username if missing (needed for file-path resolution).
    with get_conn() as c:
        c.execute(
            "UPDATE users SET username=%s WHERE sub=%s AND (username IS NULL OR username='')",
            (user_sub.replace("user-", ""), user_sub),
        )
        c.commit()


def _assign_role(user_sub: str, agent: str, role: str) -> None:
    _assign_roles(user_sub, {agent: role})


def _headers(session_id: str, agent: str, user_sub: str | None = None) -> dict:
    h = {
        "Authorization": f"Bearer {_mint_jwt(session_id, agent, user_sub)}",
        "X-Agent-Name": agent,
        "Content-Type": "application/json",
    }
    if user_sub:
        h["X-On-Behalf-Of"] = user_sub
    return h


def _op(client, headers, **body):
    return client.post("/v1/internal/memory/op", json=body, headers=headers)


# ---------------------------------------------------------------------------
# Happy paths — create / view / str_replace / insert / delete / rename
# ---------------------------------------------------------------------------

def test_create_user_topic_writes_file_and_index(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-1", "acme", "user-manager")
    r = _op(
        client, h, command="create", path="/memories/user/preferences.md",
        file_text="# Prefers metric units\n- confirmed (2026-06-12)\n",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_error"] is False
    assert body["output"] == "File created successfully at: preferences.md"
    d = _user_mem_dir("acme", "manager")
    assert (d / "preferences.md").exists()
    index = (d / "MEMORY.md").read_text()
    assert "- preferences.md — Prefers metric units (updated " in index
    # Per-user context repo got the commit.
    from services.infra import git_writer
    log = git_writer.log(d.parent)
    assert any("memory: create (user scope, by manager)" == e["subject"] for e in log)


def test_create_agent_topic_lands_in_knowledge(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-2", "acme", "user-manager")
    r = _op(
        client, h, command="create", path="/memories/agent/infrastructure.md",
        file_text="# Prod cluster is main-eu\n",
    )
    assert r.status_code == 200 and r.json()["is_error"] is False
    d = _agent_mem_dir("acme")
    assert (d / "infrastructure.md").exists()
    assert (d / "MEMORY.md").exists()
    # knowledge/ became a git repo lazily.
    assert (d.parent / ".git").exists()


def test_view_root_lists_available_scopes(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-3", "acme", "user-manager")
    r = _op(client, h, command="view", path="/memories")
    assert r.status_code == 200, r.text
    out = r.json()["output"]
    assert "agent/" in out and "user/" in out


def test_view_file_numbered_lines(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-4", "acme", "user-manager")
    _op(client, h, command="create", path="/memories/user/a.md", file_text="l1\nl2\n")
    r = _op(client, h, command="view", path="/memories/user/a.md")
    out = r.json()["output"]
    assert "Here's the content of a.md with line numbers:" in out
    assert "1\tl1" in out


def test_str_replace_roundtrip(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-5", "acme", "user-manager")
    _op(
        client, h, command="create", path="/memories/user/facts.md",
        file_text="# Facts\ncity: Athens\n",
    )
    r = _op(
        client, h, command="str_replace", path="/memories/user/facts.md",
        old_str="city: Athens", new_str="city: Berlin (was Athens until 2026-06-12)",
    )
    assert r.json()["is_error"] is False
    text = (_user_mem_dir("acme", "manager") / "facts.md").read_text()
    assert "city: Berlin" in text


def test_insert_delete_rename_roundtrip(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-6", "acme", "user-manager")
    _op(client, h, command="create", path="/memories/agent/t.md", file_text="l1\nl3\n")
    r = _op(
        client, h, command="insert", path="/memories/agent/t.md",
        insert_line=1, insert_text="l2",
    )
    assert r.json()["is_error"] is False
    r = _op(
        client, h, command="rename", old_path="/memories/agent/t.md",
        new_path="/memories/agent/renamed.md",
    )
    assert r.json()["output"] == "Successfully renamed t.md to renamed.md"
    r = _op(client, h, command="delete", path="/memories/agent/renamed.md")
    assert r.json()["output"] == "Successfully deleted renamed.md"
    d = _agent_mem_dir("acme")
    assert not (d / "t.md").exists() and not (d / "renamed.md").exists()
    # Index reflects the empty scope again.
    assert "renamed.md" not in (d / "MEMORY.md").read_text()


# ---------------------------------------------------------------------------
# Contract errors → 200 + is_error (the model reads these verbatim)
# ---------------------------------------------------------------------------

def test_create_exists_is_error_not_http_error(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-7", "acme", "user-manager")
    _op(client, h, command="create", path="/memories/user/a.md", file_text="x")
    r = _op(client, h, command="create", path="/memories/user/a.md", file_text="y")
    assert r.status_code == 200
    assert r.json()["is_error"] is True
    assert r.json()["output"] == "Error: File a.md already exists"


def test_str_replace_not_found_verbatim(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-8", "acme", "user-manager")
    _op(client, h, command="create", path="/memories/user/a.md", file_text="x")
    r = _op(
        client, h, command="str_replace", path="/memories/user/a.md",
        old_str="missing", new_str="y",
    )
    assert r.json()["is_error"] is True
    assert "did not appear verbatim in a.md" in r.json()["output"]


def test_traversal_rejected(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-9", "acme", "user-manager")
    r = _op(
        client, h, command="create", path="/memories/user/../../escape.md",
        file_text="x",
    )
    assert r.json()["is_error"] is True
    r = _op(client, h, command="view", path="/etc/passwd")
    assert r.json()["is_error"] is True


def test_index_write_denied_via_api(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-10", "acme", "user-manager")
    r = _op(
        client, h, command="create", path="/memories/user/MEMORY.md",
        file_text="hijack",
    )
    assert r.json()["is_error"] is True
    assert "auto-generated" in r.json()["output"]


def test_rename_across_scopes_refused(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-11", "acme", "user-manager")
    _op(client, h, command="create", path="/memories/user/a.md", file_text="x")
    r = _op(
        client, h, command="rename", old_path="/memories/user/a.md",
        new_path="/memories/agent/a.md",
    )
    assert r.json()["is_error"] is True
    assert "across memory scopes" in r.json()["output"]


# ---------------------------------------------------------------------------
# Role matrix
# ---------------------------------------------------------------------------

def test_viewer_can_write_user_scope(client):
    _seed_agent("acme")
    _assign_role("user-viewer", "acme", "viewer")
    h = _headers("s-12", "acme", "user-viewer")
    r = _op(
        client, h, command="create", path="/memories/user/mine.md",
        file_text="# My preference\n",
    )
    assert r.json()["is_error"] is False
    assert (_user_mem_dir("acme", "viewer") / "mine.md").exists()


def test_viewer_agent_scope_readonly(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    _assign_role("user-viewer", "acme", "viewer")
    hm = _headers("s-13", "acme", "user-manager")
    _op(client, hm, command="create", path="/memories/agent/shared.md", file_text="# S\n")
    hv = _headers("s-14", "acme", "user-viewer")
    # view allowed
    r = _op(client, hv, command="view", path="/memories/agent/shared.md")
    assert r.json()["is_error"] is False
    # writes denied with a model-readable message
    r = _op(
        client, hv, command="str_replace", path="/memories/agent/shared.md",
        old_str="# S", new_str="# hacked",
    )
    assert r.json()["is_error"] is True
    assert "read-only for viewers" in r.json()["output"]


def test_editor_can_write_agent_scope(client):
    """Editors can collaborate on agent memory."""
    _seed_agent("acme")
    _assign_role("user-editor", "acme", "editor")
    h = _headers("s-15", "acme", "user-editor")
    r = _op(
        client, h, command="create", path="/memories/agent/post-history.md",
        file_text="# Posted launch teaser to Instagram (2026-06-12)\n",
    )
    assert r.json()["is_error"] is False, r.text
    assert (_agent_mem_dir("acme") / "post-history.md").exists()
    # Attribution: the editor's username lands in the git subject.
    from services.infra import git_writer
    log = git_writer.log(_agent_mem_dir("acme").parent)
    assert any("by editor" in e["subject"] for e in log)


def test_agent_scope_session_no_user_memory(client):
    """Agent-scoped service sessions (no user owner) get agent scope only."""
    _seed_agent("acme")
    h = _headers("s-16", "acme")  # no X-On-Behalf-Of
    r = _op(
        client, h, command="create", path="/memories/agent/ops.md",
        file_text="# Op note\n",
    )
    assert r.json()["is_error"] is False
    r = _op(
        client, h, command="create", path="/memories/user/x.md", file_text="y",
    )
    assert r.json()["is_error"] is True
    assert "not available in this session" in r.json()["output"]
    # Root view lists only the agent scope.
    r = _op(client, h, command="view", path="/memories")
    assert "user/" not in r.json()["output"]


# ---------------------------------------------------------------------------
# Toggles
# ---------------------------------------------------------------------------

def test_agent_toggle_disables_scope(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    from storage import memory_store
    memory_store.set_agent_toggle("acme", "agent_memory_enabled", False)
    h = _headers("s-17", "acme", "user-manager")
    r = _op(
        client, h, command="create", path="/memories/agent/x.md", file_text="y",
    )
    assert r.json()["is_error"] is True
    assert "disabled" in r.json()["output"]
    # user scope still works
    r = _op(
        client, h, command="create", path="/memories/user/x.md", file_text="y",
    )
    assert r.json()["is_error"] is False


def test_both_toggles_off_memory_disabled(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    from storage import memory_store
    memory_store.update_settings(
        user_memory_enabled=False, agent_memory_enabled=False,
    )
    h = _headers("s-18", "acme", "user-manager")
    r = _op(client, h, command="view", path="/memories")
    assert r.json()["is_error"] is True
    assert "disabled" in r.json()["output"]


# ---------------------------------------------------------------------------
# Auth shape
# ---------------------------------------------------------------------------

def test_op_requires_session_key(client):
    r = client.post("/v1/internal/memory/op", json={"command": "view", "path": "/memories"})
    assert r.status_code in (401, 403)


def test_unknown_agent_404(client):
    h = _headers("s-19", "ghost")
    r = _op(client, h, command="view", path="/memories")
    assert r.status_code == 404


def test_unknown_command_is_contract_error(client):
    # Contract outcome, not an HTTP error: a 400 reaches the model wrapped
    # as "API error 400: {...}" — the 200 shape is what lets it self-correct.
    _seed_agent("acme")
    h = _headers("s-20", "acme")
    r = _op(client, h, command="explode", path="/memories")
    assert r.status_code == 200
    assert r.json()["is_error"] is True
    assert "unknown command" in r.json()["output"]


def test_missing_path_is_contract_error(client):
    _seed_agent("acme")
    h = _headers("s-20b", "acme")
    r = _op(client, h, command="create", file_text="x")
    assert r.status_code == 200
    assert r.json()["is_error"] is True
    assert "path required" in r.json()["output"]


def test_view_bare_slash_lists_scopes(client):
    _seed_agent("acme")
    h = _headers("s-20c", "acme")
    r = _op(client, h, command="view", path="/")
    assert r.status_code == 200, r.text
    assert r.json()["is_error"] is False


def test_git_history_covers_knowledge_scope(client):
    # An agent-scope memory write commits into knowledge/ — its history must
    # be reachable through the git API's knowledge scope so a memory delete
    # there is recoverable.
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-git", "acme", "user-manager")
    r = _op(
        client, h, command="create", path="/memories/agent/ops.md",
        file_text="# Ops\n- fact (2026-07-26)\n",
    )
    assert r.json()["is_error"] is False
    r = client.get(
        "/v1/internal/git/log",
        params={"agent": "acme", "scope": "knowledge"},
        headers=_admin_cookie(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["commits"], "memory write missing from knowledge/ history"


# ---------------------------------------------------------------------------
# Admin settings + clear endpoints
# ---------------------------------------------------------------------------

def test_settings_admin_only(client):
    r = client.get("/v1/internal/memory/settings")
    assert r.status_code in (401, 403)


def test_clear_all_admin_wipes_scope_dirs(client):
    _seed_agent("acme")
    _seed_agent("widget")
    _assign_roles("user-manager", {"acme": "manager", "widget": "manager"})
    for slug, sid in (("acme", "s-21"), ("widget", "s-22")):
        h = _headers(sid, slug, "user-manager")
        r = _op(
            client, h, command="create", path="/memories/agent/t.md",
            file_text="# X\n",
        )
        assert r.json()["is_error"] is False

    r = client.post(
        "/v1/internal/memory/clear-all",
        json={"scope": "agent"},
        headers=_admin_cookie(),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["files_unlinked"] >= 2  # topic + index per agent counted? topics at least
    assert not _agent_mem_dir("acme").exists()
    assert not _agent_mem_dir("widget").exists()


def test_clear_agent_memory_wipes_shared_scope_only(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-23", "acme", "user-manager")
    r = _op(client, h, command="create", path="/memories/agent/shared.md", file_text="# S\n")
    assert r.json()["is_error"] is False
    r = _op(client, h, command="create", path="/memories/user/mine.md", file_text="# M\n")
    assert r.json()["is_error"] is False

    r = client.post("/v1/internal/memory/clear-agent-memory/acme", headers=_admin_cookie())
    assert r.status_code == 200, r.text
    assert r.json()["files_unlinked"] >= 1
    assert not _agent_mem_dir("acme").exists()
    # The user's personal memory is untouched.
    assert (_user_mem_dir("acme", "manager") / "mine.md").exists()


def test_clear_agent_memory_unknown_agent_404(client):
    r = client.post("/v1/internal/memory/clear-agent-memory/ghost", headers=_admin_cookie())
    assert r.status_code == 404
