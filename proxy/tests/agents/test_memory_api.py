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

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(temp_db):
    from app import app
    return TestClient(app)


def _mint_jwt(session_id: str, agent: str, user_sub: str | None = None) -> str:
    from tests.conftest import live_session_token
    return live_session_token(session_id, agent, user_sub or "")


def _admin_cookie() -> dict[str, str]:
    """Session cookie for the seeded admin user. The clear-* endpoints are
    admin/manager-gated via get_current_user (NOT the S2S master key)."""
    from auth.providers import create_session_jwt
    token = create_session_jwt("user-admin", "admin@test.com", "Admin User", "admin")
    return {"Cookie": f"session={token}"}


def _seed_agent(slug: str, *, default_scope: str = "user") -> None:
    from storage.agents import agent_store
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
    assert "- preferences.md: Prefers metric units (48 B, 1 entry, newest 2026-06-12" in index
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


def test_rename_from_the_shared_scope_to_a_personal_one_is_refused(client):
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-11", "acme", "user-manager")
    _op(client, h, command="create", path="/memories/agent/a.md", file_text="# A\n")
    r = _op(
        client, h, command="rename", old_path="/memories/agent/a.md",
        new_path="/memories/user/a.md",
    )
    assert r.json()["is_error"] is True
    assert r.json()["output"].startswith("Error: rename cannot move a topic from the shared scope")
    assert (_agent_mem_dir("acme") / "a.md").exists()


# ---------------------------------------------------------------------------
# Promotion: a personal topic shared through `rename` and through the route
# ---------------------------------------------------------------------------

def _personal_topic(client, h, name: str = "suppliers.md") -> None:
    r = _op(
        client, h, command="create", path=f"/memories/user/{name}",
        file_text="# Suppliers\nWho ships what.\n\n- Acme ships in two days (2026-10-01)\n",
    )
    assert r.json()["is_error"] is False, r.text


def _subjects(repo: Path) -> list[str]:
    from services.infra import git_writer
    return [e["subject"] for e in git_writer.log(repo)]


@pytest.mark.parametrize("role", ["editor", "manager"])
def test_rename_into_the_shared_scope_promotes_with_two_commits(client, role):
    _seed_agent("acme")
    sub = f"user-{role}"
    _assign_role(sub, "acme", role)
    h = _headers("s-20", "acme", sub)
    _personal_topic(client, h)
    r = _op(
        client, h, command="rename", old_path="/memories/user/suppliers.md",
        new_path="/memories/agent/suppliers.md",
    )
    assert r.json() == {
        "output": "Successfully renamed /memories/user/suppliers.md to /memories/agent/suppliers.md",
        "is_error": False, "warnings": [],
    }
    assert (_agent_mem_dir("acme") / "suppliers.md").read_text().startswith("# Suppliers")
    assert not (_user_mem_dir("acme", role) / "suppliers.md").exists()
    assert "- suppliers.md: Suppliers (" in (_agent_mem_dir("acme") / "MEMORY.md").read_text()
    assert "suppliers.md" not in (_user_mem_dir("acme", role) / "MEMORY.md").read_text()
    assert f"memory: promote (agent scope, by {role})" in _subjects(_agent_mem_dir("acme").parent)
    assert f"memory: delete (user scope, by {role})" in _subjects(_user_mem_dir("acme", role).parent)


def test_a_promotion_may_rename_and_refuses_a_taken_name(client):
    _seed_agent("acme")
    _assign_role("user-editor", "acme", "editor")
    h = _headers("s-21", "acme", "user-editor")
    _personal_topic(client, h)
    _op(client, h, command="create", path="/memories/agent/vendors.md", file_text="# V\n")
    r = _op(
        client, h, command="rename", old_path="/memories/user/suppliers.md",
        new_path="/memories/agent/vendors.md",
    )
    assert r.json()["is_error"] is True
    assert "The destination vendors.md already exists" in r.json()["output"]
    assert (_user_mem_dir("acme", "editor") / "suppliers.md").exists()
    r = _op(
        client, h, command="rename", old_path="/memories/user/suppliers.md",
        new_path="/memories/agent/our-suppliers.md",
    )
    assert r.json()["is_error"] is False
    assert (_agent_mem_dir("acme") / "our-suppliers.md").exists()


@pytest.mark.parametrize("role", ["viewer", "contributor"])
def test_a_promotion_below_the_editor_tier_is_refused(client, role):
    _seed_agent("acme")
    sub = f"user-{role}"
    _assign_role(sub, "acme", role)
    h = _headers("s-22", "acme", sub)
    _personal_topic(client, h)
    r = _op(
        client, h, command="rename", old_path="/memories/user/suppliers.md",
        new_path="/memories/agent/suppliers.md",
    )
    assert r.json()["is_error"] is True
    assert r.json()["output"].startswith("agent memory is read-only for your role")
    assert (_user_mem_dir("acme", role) / "suppliers.md").exists()
    assert not (_agent_mem_dir("acme") / "suppliers.md").exists()


def test_a_session_without_a_user_cannot_promote(client):
    _seed_agent("acme")
    h = _headers("s-23", "acme")  # no X-On-Behalf-Of
    r = _op(
        client, h, command="rename", old_path="/memories/user/a.md",
        new_path="/memories/agent/a.md",
    )
    assert r.json()["is_error"] is True
    assert "no user owner" in r.json()["output"]


def _cookie(sub: str, platform_role: str = "member") -> dict[str, str]:
    from auth.providers import create_session_jwt
    return {"Cookie": f"session={create_session_jwt(sub, f'{sub}@test.com', sub, platform_role)}"}


def test_the_promote_route_follows_the_persons_role(client):
    _seed_agent("acme")
    _assign_role("user-editor", "acme", "editor")
    _assign_role("user-contributor", "acme", "contributor")
    for who in ("user-editor", "user-contributor"):
        _personal_topic(client, _headers(f"s-{who}", "acme", who))
    r = client.post("/v1/internal/memory/promote",
                    json={"agent": "acme", "path": "/memories/user/suppliers.md"},
                    headers=_cookie("user-contributor"))
    assert (r.status_code, r.json()["detail"]) == (
        403, "Only editors and managers can share a memory with everyone on this agent. Your topic stays private.",
    )
    assert (_user_mem_dir("acme", "contributor") / "suppliers.md").exists()
    r = client.post("/v1/internal/memory/promote",
                    json={"agent": "acme", "path": "/memories/user/suppliers.md",
                          "new_path": "/memories/agent/suppliers.md"},
                    headers=_cookie("user-editor"))
    assert r.status_code == 200 and r.json()["is_error"] is False, r.text
    assert (_agent_mem_dir("acme") / "suppliers.md").exists()
    assert "memory: promote (agent scope, by editor)" in _subjects(_agent_mem_dir("acme").parent)


def test_the_promote_route_refusals(client):
    _seed_agent("acme")
    _assign_role("user-editor", "acme", "editor")
    _personal_topic(client, _headers("s-30", "acme", "user-editor"))
    editor = _cookie("user-editor")
    # An unknown agent, a wrong agent on a session token, no user, a path
    # that is not personal, a shared name outside the shared scope.
    r = client.post("/v1/internal/memory/promote", json={"agent": "ghost", "path": "/memories/user/a.md"}, headers=editor)
    assert (r.status_code, r.json()["detail"]) == (404, "This agent no longer exists.")
    r = client.post("/v1/internal/memory/promote", json=["not", "an", "object"], headers=editor)
    assert (r.status_code, r.json()["detail"]) == (404, "This agent no longer exists.")
    _seed_agent("other")
    r = client.post("/v1/internal/memory/promote", json={"agent": "other", "path": "/memories/user/a.md"},
                    headers=_headers("s-31", "acme", "user-editor"))
    assert r.status_code == 403
    r = client.post("/v1/internal/memory/promote", json={"agent": "acme", "path": "/memories/user/a.md"},
                    headers=_headers("s-32", "acme"))
    assert (r.status_code, r.json()["detail"]) == (400, "Only a person signed in can share a memory topic.")
    r = client.post("/v1/internal/memory/promote", json={"agent": "acme", "path": "/memories/agent/a.md"}, headers=editor)
    assert r.json() == {"output": "Only a topic under /memories/user/ can be shared.", "is_error": True, "warnings": []}
    r = client.post("/v1/internal/memory/promote",
                    json={"agent": "acme", "path": "/memories/user/suppliers.md", "new_path": "/memories/user/b.md"},
                    headers=editor)
    assert r.json()["output"] == "A shared topic's new name must be under /memories/agent/."
    r = client.post("/v1/internal/memory/promote", json={"agent": "acme", "path": "/memories/user/nope.md"}, headers=editor)
    assert r.json()["is_error"] is True and "does not exist" in r.json()["output"]
    # An admin promotes on any agent, with no per-agent role (the seeded
    # admin only needs a username).
    from storage.pg import get_conn
    with get_conn() as c:
        c.execute("UPDATE users SET username='admin' WHERE sub='user-admin'")
        c.commit()
    _personal_topic(client, _headers("s-33", "acme", "user-admin"))
    r = client.post("/v1/internal/memory/promote", json={"agent": "acme", "path": "/memories/user/suppliers.md"},
                    headers=_admin_cookie())
    assert r.status_code == 200 and r.json()["is_error"] is False, r.text


# ---------------------------------------------------------------------------
# The settings' ranges, the cap log lines and the nudge reset
# ---------------------------------------------------------------------------

def test_the_two_caps_are_settings_with_ranges(client):
    admin = _admin_cookie()
    r = client.get("/v1/internal/memory/settings", headers=admin)
    assert (r.json()["topic_grow_cap_bytes"], r.json()["entry_cap_bytes"]) == (32768, 2048)
    r = client.patch("/v1/internal/memory/settings",
                     json={"topic_grow_cap_bytes": 40960, "entry_cap_bytes": 4096}, headers=admin)
    assert (r.json()["topic_grow_cap_bytes"], r.json()["entry_cap_bytes"]) == (40960, 4096)
    for body, sentence in (
        ({"topic_grow_cap_bytes": 100}, "The growth cap must be a whole number of bytes from 16,384 to 65,536."),
        ({"topic_grow_cap_bytes": True}, "The growth cap must be a whole number of bytes from 16,384 to 65,536."),
        ({"entry_cap_bytes": 70000}, "The entry cap must be a whole number of bytes from 256 to 65,536."),
        ({"inline_budget_bytes": "big"}, "The inline budget must be a whole number of bytes, at least 1."),
        ({"inline_budget_bytes": 0}, "The inline budget must be a whole number of bytes, at least 1."),
        ({"nudge_turns": 10**10}, "The turns before a nudge must be a whole number."),
        ({"nudge_turns": -1}, "The turns before a nudge must be a whole number."),
    ):
        r = client.patch("/v1/internal/memory/settings", json=body, headers=admin)
        assert (r.status_code, r.json()["detail"]) == (400, sentence), body
    r = client.get("/v1/internal/memory/settings", headers=admin)
    assert r.json()["topic_grow_cap_bytes"] == 40960


def test_a_cap_refusal_and_a_size_warning_log_at_info_the_reminder_does_not(client, caplog):
    import logging
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-40", "acme", "user-manager")
    lines = "# Big\n" + "- fact (2026-10-01)\n" * 1000
    with caplog.at_level(logging.INFO, logger="otodock.memory-api"):
        r = _op(client, h, command="create", path="/memories/agent/big.md", file_text=lines)
        assert r.json()["is_error"] is False and r.json()["warnings"]
        r = _op(client, h, command="insert", path="/memories/agent/big.md",
                insert_line=1, insert_text="- " + "x" * 3000)
        assert r.json()["is_error"] is True
        r = _op(client, h, command="create", path="/memories/agent/small.md", file_text="# Small\n")
        r = _op(client, h, command="insert", path="/memories/agent/small.md",
                insert_line=1, insert_text="- undated line")
        assert r.json()["warnings"] == ["Date this entry (YYYY-MM-DD) so its age stays visible."]
    texts = [rec.getMessage() for rec in caplog.records if rec.name == "otodock.memory-api"]
    assert any(t.startswith("memory: create on big.md (agent acme, agent scope) left it at ") for t in texts)
    assert any(t.startswith("memory: insert on big.md refused (agent acme, agent scope): Error: this edit adds") for t in texts)
    assert not any("small.md" in t for t in texts)


def test_a_landed_write_resets_the_nudge_a_view_and_a_refused_write_do_not(client):
    from services.memory import memory_nudge
    _seed_agent("acme")
    _assign_role("user-manager", "acme", "manager")
    h = _headers("s-nudge-api", "acme", "user-manager")
    memory_nudge._counters["s-nudge-api"] = 4
    _op(client, h, command="view", path="/memories")
    assert memory_nudge._counters["s-nudge-api"] == 4
    r = _op(client, h, command="create", path="/memories/agent/a.md", file_text="x" * 70_000)
    assert r.json()["is_error"] is True
    assert memory_nudge._counters["s-nudge-api"] == 4
    _op(client, h, command="create", path="/memories/agent/a.md", file_text="# A\n")
    assert memory_nudge._counters["s-nudge-api"] == 0
    memory_nudge._counters.pop("s-nudge-api", None)


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
    assert "read-only for your role" in r.json()["output"]


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
    from storage.agents import memory_store
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
    from storage.agents import memory_store
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


def test_git_revert_path_cannot_leave_the_repo(client):
    """Admin-only revert: a ``..`` path resolves outside the scope's repo and
    is refused before git sees it."""
    _seed_agent("acme")
    r = client.post(
        "/v1/internal/git/revert",
        json={"agent": "acme", "scope": "knowledge", "commit": "deadbeef",
              "path": "../config/agent.md"},
        headers=_admin_cookie(),
    )
    assert r.status_code == 400
    assert "escapes" in r.json()["detail"]


def test_clear_all_unknown_or_traversal_agent_404(client):
    """The optional ``agent`` filter is validated like every other agent
    argument — a name that is not an agent (or would leave the agents tree)
    never reaches the wipe."""
    for agent in ("ghost", "../ghost"):
        r = client.post(
            "/v1/internal/memory/clear-all",
            json={"scope": "agent", "agent": agent},
            headers=_admin_cookie(),
        )
        assert r.status_code == 404, agent


# ---------------------------------------------------------------------------
# Refusals a person reads: one plain sentence, no field name or user id
# ---------------------------------------------------------------------------

def _member_cookie(sub: str = "user-member") -> dict[str, str]:
    from auth.providers import create_session_jwt
    _seed_agent("other")
    _assign_role(sub, "other", "viewer")
    return {"Cookie": f"session={create_session_jwt(sub, f'{sub}@test.com', sub, 'member')}"}


def test_memory_settings_refusals_read_as_sentences(client, caplog):
    _seed_agent("acme")
    member = _member_cookie()
    cases = [
        ("get", "/v1/internal/memory/settings", None, 403,
         "Only an admin can see the memory settings."),
        ("patch", "/v1/internal/memory/settings", {"user_memory_enabled": False}, 403,
         "Only an admin can change the memory settings."),
        ("get", "/v1/internal/memory/agent-settings/acme", None, 403,
         "Only a manager of this agent can see its memory settings."),
        ("patch", "/v1/internal/memory/agent-settings/acme", {"key": "user_memory_enabled"}, 403,
         "Only a manager of this agent can change its memory settings."),
        ("post", "/v1/internal/memory/clear-agent-memory/acme", None, 403,
         "Only a manager of this agent can clear its memory."),
        ("post", "/v1/internal/memory/clear-all", {"scope": "agent"}, 403,
         "Only an admin can clear everyone's memory."),
    ]
    for method, path, body, status, sentence in cases:
        r = getattr(client, method)(path, headers=member, **({"json": body} if body else {}))
        assert (r.status_code, r.json()["detail"]) == (status, sentence), path

    admin = _admin_cookie()
    with caplog.at_level("WARNING", logger="otodock.memory-api"):
        r = client.patch("/v1/internal/memory/agent-settings/acme",
                         json={"key": "agent_memory_settings_owner", "value": True}, headers=admin)
    assert (r.status_code, r.json()["detail"]) == (400, "That memory setting does not exist.")
    assert "agent_memory_settings_owner" in caplog.text
    r = client.patch("/v1/internal/memory/agent-settings/acme", json={"value": True}, headers=admin)
    assert (r.status_code, r.json()["detail"]) == (400, "That memory setting does not exist.")
    r = client.post("/v1/internal/memory/clear-all", json={"scope": "x"}, headers=admin)
    assert (r.status_code, r.json()["detail"]) == (400, "Choose whose memory to clear.")
    r = client.post("/v1/internal/memory/clear-agent-memory/ghost", headers=admin)
    assert (r.status_code, r.json()["detail"]) == (404, "This agent no longer exists.")


def test_clear_my_memory_refusals_name_no_identifier(temp_db, caplog):
    from fastapi import HTTPException

    from api.agent_data import memory
    from auth.providers import UserContext

    def refused(sub: str) -> str:
        user = UserContext(sub=sub, email="x@test.com", name="X", role="member", agents=[])
        with pytest.raises(HTTPException) as exc:
            asyncio.run(memory.clear_my_memory(user=user))
        assert exc.value.status_code == 400
        return exc.value.detail

    assert refused("api-key") == "Only a person signed in to the dashboard can clear their own memory."
    with caplog.at_level("WARNING", logger="otodock.memory-api"):
        assert refused("user-ghost") == "Could not find your account. Sign out, sign in again and retry."
    assert "user-ghost" in caplog.text


def test_git_history_refusals_name_no_identifier(temp_db):
    from fastapi import HTTPException

    from api.agent_data import git_history
    _seed_agent("acme")

    def refused(scope: str, agent: str, sub: str | None) -> tuple[int, str]:
        with pytest.raises(HTTPException) as exc:
            git_history._resolve_repo(scope, agent, sub)
        return exc.value.status_code, exc.value.detail

    assert refused("agent", "ghost", None) == (404, "This agent no longer exists.")
    assert refused("user", "acme", None) == (400, "Choose whose history to show.")
    assert refused("user", "acme", "sub-ghost-7") == (404, "That person's account no longer exists.")
    assert refused("bogus", "acme", None) == (400, "That history does not exist.")


def test_a_promotion_is_refused_where_the_agent_has_one_scope(client):
    from storage.agents import agent_store
    if not agent_store.agent_exists("solo"):
        agent_store.create_agent("solo", "Solo", default_scope="user", collaborative=False)
    _assign_role("user-editor", "solo", "editor")
    h = _headers("s-50", "solo", "user-editor")
    _personal_topic(client, h)
    r = _op(client, h, command="rename", old_path="/memories/user/suppliers.md",
            new_path="/memories/agent/suppliers.md")
    assert r.json()["is_error"] is True
    # A Personal-only agent has no shared scope: the promotion refuses it.
    assert r.json()["output"] == (
        "Error: the agent memory scope is not available on this agent, so nothing can be promoted."
    )


def test_a_promotion_that_cannot_move_the_file_answers_a_sentence(client, monkeypatch):
    from services.memory import memory_file
    _seed_agent("acme")
    _assign_role("user-editor", "acme", "editor")
    h = _headers("s-51", "acme", "user-editor")
    _personal_topic(client, h)

    def boom(*_a, **_kw):
        raise OSError("quota")
    monkeypatch.setattr(memory_file, "op_promote", boom)
    r = _op(client, h, command="rename", old_path="/memories/user/suppliers.md",
            new_path="/memories/agent/suppliers.md")
    assert r.json() == {"output": "Error: suppliers.md could not be moved. Try again.",
                        "is_error": True, "warnings": []}


def test_a_promotion_resets_the_nudge_and_refuses_a_bare_scope_as_new_path(client):
    from services.memory import memory_nudge
    _seed_agent("acme")
    _assign_role("user-editor", "acme", "editor")
    h = _headers("s-52", "acme", "user-editor")
    _personal_topic(client, h)
    r = _op(client, h, command="rename", old_path="/memories/user/suppliers.md",
            new_path="/memories/agent/")
    assert r.json()["output"] == "new_path must name a topic under /memories/agent/ or /memories/user/."
    memory_nudge._counters["s-52"] = 6
    r = _op(client, h, command="rename", old_path="/memories/user/suppliers.md",
            new_path="/memories/agent/suppliers.md")
    assert r.json()["is_error"] is False
    assert memory_nudge._counters["s-52"] == 0
    memory_nudge._counters.pop("s-52", None)


def test_a_promotion_of_a_hand_written_topic_still_commits_the_persons_index(client):
    """A topic saved through the Files UI was never tracked in the person's
    repo: the promotion commits the index change there and skips the
    untracked deletion instead of failing the whole commit."""
    _seed_agent("acme")
    _assign_role("user-editor", "acme", "editor")
    h = _headers("s-60", "acme", "user-editor")
    _op(client, h, command="create", path="/memories/user/first.md", file_text="# First\n")
    root = _user_mem_dir("acme", "editor")
    (root / "by-hand.md").write_text("# By hand\n- noted (2026-10-01)\n")
    _op(client, h, command="view", path="/memories")  # heals the index: by-hand.md listed
    assert "by-hand.md" in (root / "MEMORY.md").read_text()
    r = _op(client, h, command="rename", old_path="/memories/user/by-hand.md",
            new_path="/memories/agent/by-hand.md")
    assert r.json()["is_error"] is False, r.text
    assert not (root / "by-hand.md").exists() and (_agent_mem_dir("acme") / "by-hand.md").exists()
    # The person's repo commits the index change; the untracked file's
    # removal is not a change git can record.
    assert "memory: delete (user scope, by editor)" in _subjects(root.parent)
    assert "by-hand.md" not in (root / "MEMORY.md").read_text()
    assert "memory: promote (agent scope, by editor)" in _subjects(_agent_mem_dir("acme").parent)


# ---------------------------------------------------------------------------
# promote() validates its own input, whoever calls it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel,dst,words", [
    ("../x.md", None, "invalid segment"),
    ("MEMORY.md", None, "auto-generated"),
    (".memlock", None, "invalid segment"),
    ("x.exe", None, "unsupported extension"),
    ("x.md", "y.exe", "unsupported extension"),
    ("x.md", "folder", "no extension"),
    ("", None, "names one topic file"),
])
def test_promote_validates_its_own_paths(temp_db, rel, dst, words):
    from services.memory import memory_promote
    from services.memory.memory_file import MemoryOpError
    _seed_agent("acme")
    with pytest.raises(MemoryOpError, match=words):
        asyncio.run(memory_promote.promote("acme", "editor", rel, role="editor", dst_rel=dst))


def test_promote_refuses_a_missing_person(temp_db):
    from services.memory import memory_promote
    from services.memory.memory_file import MemoryOpError
    _seed_agent("acme")
    with pytest.raises(MemoryOpError, match="person's own memory scope"):
        asyncio.run(memory_promote.promote("acme", "", "x.md", role="editor"))
