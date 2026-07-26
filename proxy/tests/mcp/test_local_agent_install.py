"""Tests for the local agent-template endpoints (agent-creator-mcp's backend).

Covers the three things that make this route safe to expose to an LLM:

- **Authorization** — platform creator/admin only, session-backed, and the
  path resolves through the caller's own policy.
- **Snapshotting** — symlinks and oversized trees are rejected before the
  parser (which globs and follows symlinked dirs) ever sees them.
- **Install semantics** — provenance marker, core-MCP assignment,
  ``default_for_new_users`` gated to admins, per-user seeding for later
  joiners.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._paths import PROXY_DIR

_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

ADMIN_SUB = "user-admin"
CREATOR_SUB = "user-manager"   # seeded with platform role "creator"
MEMBER_SUB = "user-viewer"     # seeded with platform role "member"

SESSION_ID = "sess-local-tmpl"


def _write_template(root: Path, slug: str = "demo-template", **kw) -> Path:
    """Minimal valid template dir under ``root/<slug>/``."""
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    agent_json = {
        "schema_version": "1",
        "slug": slug,
        "display_name": slug.replace("-", " ").title(),
        "description": "Test template",
        "color": "#10B981",
        "version": "1.0.0",
    }
    agent_json.update(kw.get("agent_json_extra") or {})
    (d / "agent.json").write_text(json.dumps(agent_json))
    (d / "agent.md").write_text("# Persona\n\nYou are a test agent.\n")
    (d / "mcps.json").write_text(json.dumps({"required": kw.get("mcps") or []}))
    (d / "README.md").write_text("# README\n")
    if kw.get("user_setup_md"):
        (d / "user-setup.md").write_text(kw["user_setup_md"])
    if kw.get("notifications"):
        (d / "notifications.json").write_text(
            json.dumps({"notifications": kw["notifications"]}),
        )
    return d


def _make_app(tmp_path, monkeypatch, *, sub: str, role: str,
              username: str = "alice", with_session: bool = True):
    """Mount local_templates.router with a stubbed session principal.

    The agent tree is redirected to ``tmp_path`` and the session's security
    context is stubbed so path resolution runs the real policy against a
    real directory.
    """
    import config
    from api.mcp import local_templates
    from auth.providers import UserContext, get_current_user
    from auth.path_policy import SecurityContext
    from core.session import session_state

    agents_dir = tmp_path / "agents"
    (agents_dir / "host-agent" / "workspace").mkdir(parents=True, exist_ok=True)
    (agents_dir / "host-agent" / "users" / username / "workspace").mkdir(
        parents=True, exist_ok=True,
    )
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)
    # path_policy resolves against its own module-level snapshot of AGENTS_DIR.
    import auth.path_policy as _pp
    monkeypatch.setattr(_pp, "_AGENTS_DIR", agents_dir.resolve())
    import services.path_policy_v2 as _v2
    if hasattr(_v2, "_AGENTS_DIR"):
        monkeypatch.setattr(_v2, "_AGENTS_DIR", agents_dir.resolve())

    user = UserContext(
        sub=sub,
        email=f"{username}@test.com",
        name=username.title(),
        role=role,
        agents=["host-agent"],
        agent_roles={"host-agent": "manager"},
        is_api_key=True,
        session_id=SESSION_ID if with_session else "",
        agent="host-agent",
    )

    ctx = SecurityContext(
        role="manager", username=username, agent="host-agent",
        is_admin_agent=False,
    )
    monkeypatch.setattr(
        session_state, "get_session_security",
        lambda sid: ctx if sid == SESSION_ID else None,
    )

    async def _stub_user():
        return user

    app = FastAPI()
    app.include_router(local_templates.router)
    app.dependency_overrides[get_current_user] = _stub_user
    return app, agents_dir


def _agent_root(agents_dir: Path, username: str = "alice") -> Path:
    return agents_dir / "host-agent" / "users" / username / "workspace"


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

class TestAuthorization:
    def test_member_is_refused(self, tmp_path, monkeypatch, temp_db):
        app, agents_dir = _make_app(
            tmp_path, monkeypatch, sub=MEMBER_SUB, role="member",
        )
        _write_template(_agent_root(agents_dir))
        client = TestClient(app)
        for path, payload in (
            ("/v1/agents/local-template/validate", {"path": "/users/alice/workspace/demo-template"}),
            ("/v1/agents/install-from-local-template", {"path": "/users/alice/workspace/demo-template"}),
        ):
            resp = client.post(path, json=payload)
            assert resp.status_code == 403
        assert client.get("/v1/agents/local-template/building-blocks").status_code == 403

    def test_session_required(self, tmp_path, monkeypatch, temp_db):
        """A cookie/master-key caller has no session security context, so the
        path can't be resolved against a session policy at all."""
        app, agents_dir = _make_app(
            tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin", with_session=False,
        )
        _write_template(_agent_root(agents_dir))
        resp = TestClient(app).post(
            "/v1/agents/local-template/validate",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        assert resp.status_code == 403

    def test_other_users_tree_is_refused(self, tmp_path, monkeypatch, temp_db):
        app, agents_dir = _make_app(
            tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin",
        )
        other = agents_dir / "host-agent" / "users" / "bob" / "workspace"
        other.mkdir(parents=True, exist_ok=True)
        _write_template(other)
        resp = TestClient(app).post(
            "/v1/agents/local-template/validate",
            json={"path": "/users/bob/workspace/demo-template"},
        )
        assert resp.status_code == 403

    def test_missing_path_is_rejected(self, tmp_path, monkeypatch, temp_db):
        app, _ = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        resp = TestClient(app).post(
            "/v1/agents/local-template/validate", json={"path": "  "},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Snapshot guards
# ---------------------------------------------------------------------------

class TestSnapshotGuards:
    def test_symlink_inside_template_is_rejected(self, tmp_path, monkeypatch, temp_db):
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        d = _write_template(_agent_root(agents_dir))
        (d / "context").mkdir()
        (d / "context" / "leak.md").symlink_to(tmp_path / "outside.md")
        (tmp_path / "outside.md").write_text("secret")
        resp = TestClient(app).post(
            "/v1/agents/local-template/validate",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        assert resp.status_code == 400
        assert "symlink" in str(resp.json()["detail"]).lower()

    def test_symlinked_directory_is_rejected(self, tmp_path, monkeypatch, temp_db):
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        d = _write_template(_agent_root(agents_dir))
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "elsewhere" / "x.md").write_text("secret")
        (d / "context").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
        resp = TestClient(app).post(
            "/v1/agents/local-template/validate",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        assert resp.status_code == 400

    def test_oversized_file_is_rejected(self, tmp_path, monkeypatch, temp_db):
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        d = _write_template(_agent_root(agents_dir))
        (d / "context").mkdir()
        (d / "context" / "big.md").write_text("x" * (1024 * 1024 + 10))
        resp = TestClient(app).post(
            "/v1/agents/local-template/validate",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        assert resp.status_code == 400
        assert "too large" in str(resp.json()["detail"]).lower()

    def test_too_many_files_is_rejected(self, tmp_path, monkeypatch, temp_db):
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        d = _write_template(_agent_root(agents_dir))
        (d / "context").mkdir()
        for i in range(305):
            (d / "context" / f"f{i}.md").write_text("x")
        resp = TestClient(app).post(
            "/v1/agents/local-template/validate",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------

class TestValidate:
    def test_valid_template_reports_plan(self, tmp_path, monkeypatch, temp_db):
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        _write_template(
            _agent_root(agents_dir),
            notifications=[{
                "slug": "hello", "title": "Hi", "body": "There",
                "scope": "user", "schedule": {"type": "cron", "cron": "0 9 * * *"},
            }],
            user_setup_md="# Welcome\n",
        )
        resp = TestClient(app).post(
            "/v1/agents/local-template/validate",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["slug"] == "demo-template"
        assert body["slug_available"] is True
        assert body["seeds"]["notifications"] == 1
        assert body["seeds"]["has_user_setup"] is True

    def test_broken_manifest_returns_errors_not_500(self, tmp_path, monkeypatch, temp_db):
        """Validation problems are the agent's to fix — they come back as
        ``ok: false`` with text, not as an HTTP error."""
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        d = _write_template(_agent_root(agents_dir))
        (d / "agent.json").write_text(json.dumps({"schema_version": "1", "slug": "X!"}))
        resp = TestClient(app).post(
            "/v1/agents/local-template/validate",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["errors"]

    def test_missing_readme_is_an_error(self, tmp_path, monkeypatch, temp_db):
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        d = _write_template(_agent_root(agents_dir))
        (d / "README.md").unlink()
        resp = TestClient(app).post(
            "/v1/agents/local-template/validate",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        assert resp.json()["ok"] is False

    def test_taken_slug_suggests_an_alternative(self, tmp_path, monkeypatch, temp_db):
        from storage import agent_store
        agent_store.create_agent("demo-template", "Demo", admin_only=False)
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        _write_template(_agent_root(agents_dir))
        body = TestClient(app).post(
            "/v1/agents/local-template/validate",
            json={"path": "/users/alice/workspace/demo-template"},
        ).json()
        assert body["ok"] is True          # the template itself is fine
        assert body["slug_available"] is False
        assert body["suggested_slug"] == "demo-template-2"


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

class TestInstall:
    def test_admin_install_end_to_end(self, tmp_path, monkeypatch, temp_db):
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        _write_template(_agent_root(agents_dir), user_setup_md="# Welcome\n")
        resp = TestClient(app).post(
            "/v1/agents/install-from-local-template",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["agent_slug"] == "demo-template"
        assert body["dashboard_path"] == "/agents/demo-template"
        # The DB row is internal detail — the MCP response must not carry it.
        assert "agent" not in body

        from storage import agent_store
        agent = agent_store.get_agent("demo-template")
        # Provenance: a local template must never match a catalog slug.
        assert agent["community_template"] == "local:demo-template"
        assert (agents_dir / "demo-template" / "config" / "agent.md").is_file()
        assert (agents_dir / "demo-template" / "config" / "user-setup.md").is_file()

    def test_installer_becomes_manager(self, tmp_path, monkeypatch, temp_db):
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        _write_template(_agent_root(agents_dir))
        TestClient(app).post(
            "/v1/agents/install-from-local-template",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        from storage import database as task_store
        assert task_store.get_user_agent_roles(ADMIN_SUB).get("demo-template") == "manager"

    def test_core_mcps_are_assigned(self, tmp_path, monkeypatch, temp_db):
        """A template declares what the agent SPECIFICALLY needs; core MCPs
        belong to every agent. This install-time assign (plus the
        create-agent path) is the ONLY core-assignment mechanism — there is
        no boot backfill — so it must happen here."""
        from types import SimpleNamespace
        from services.mcp import mcp_registry

        def _stub(category, assignment_mode, skills=()):
            return SimpleNamespace(
                category=category, assignment_mode=assignment_mode,
                skills=list(skills), path_env={},
            )

        monkeypatch.setattr(mcp_registry, "_manifests", {
            "core-thing": _stub(
                "core", "auto",
                [SimpleNamespace(id="core-skill", default_exclude_from=[])],
            ),
            "explicit-core": _stub("core", "explicit"),
            "community-thing": _stub("community", "auto"),
        })
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        _write_template(_agent_root(agents_dir))
        resp = TestClient(app).post(
            "/v1/agents/install-from-local-template",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        assert resp.status_code == 200, resp.text
        from storage import mcp_store
        assigned = set(mcp_store.get_manager_enabled_mcps("demo-template"))
        assert "core-thing" in assigned
        # ``explicit`` means an admin authorizes it per-agent; ``community``
        # is never auto-assigned.
        assert "explicit-core" not in assigned
        assert "community-thing" not in assigned

    def test_core_mcps_none_opts_out(self, tmp_path, monkeypatch, temp_db):
        """``core_mcps: "none"`` — a single-purpose agent (e.g. a caller
        persona) gets ONLY what mcps.json lists. With no boot backfill the
        opt-out sticks across restarts, which is the whole point."""
        from types import SimpleNamespace
        from services.mcp import mcp_registry

        monkeypatch.setattr(mcp_registry, "_manifests", {
            "core-thing": SimpleNamespace(
                category="core", assignment_mode="auto", skills=[], path_env={},
            ),
        })
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        _write_template(
            _agent_root(agents_dir),
            agent_json_extra={"core_mcps": "none"},
        )
        resp = TestClient(app).post(
            "/v1/agents/install-from-local-template",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        assert resp.status_code == 200, resp.text
        from storage import mcp_store
        assert "core-thing" not in set(
            mcp_store.get_manager_enabled_mcps("demo-template"))

    def test_core_mcps_rejects_unknown_value(self, tmp_path, monkeypatch, temp_db):
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        _write_template(
            _agent_root(agents_dir),
            agent_json_extra={"core_mcps": "some"},
        )
        resp = TestClient(app).post(
            "/v1/agents/local-template/validate",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        body = resp.json()
        # Validation surfaces the schema violation (dry-run contract: 200 +
        # ok=false + the error text) instead of letting install proceed.
        assert body.get("ok") is False
        assert any("core_mcps" in e for e in body.get("errors", []))

    def test_creator_cannot_set_default_for_new_users(self, tmp_path, monkeypatch, temp_db):
        """Auto-attaching every future platform user is an admin decision
        everywhere else — a template must not be a way around that."""
        app, agents_dir = _make_app(
            tmp_path, monkeypatch, sub=CREATOR_SUB, role="creator",
        )
        _write_template(
            _agent_root(agents_dir),
            agent_json_extra={
                "default_for_new_users": {"enabled": True, "role": "manager"},
            },
        )
        body = TestClient(app).post(
            "/v1/agents/install-from-local-template",
            json={"path": "/users/alice/workspace/demo-template"},
        ).json()
        assert body["ignored_fields"] == ["default_for_new_users"]
        from storage import agent_store
        assert not agent_store.get_agent("demo-template").get("default_for_new_users_role")

    def test_admin_can_set_default_for_new_users(self, tmp_path, monkeypatch, temp_db):
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        _write_template(
            _agent_root(agents_dir),
            agent_json_extra={
                "default_for_new_users": {"enabled": True, "role": "viewer"},
            },
        )
        body = TestClient(app).post(
            "/v1/agents/install-from-local-template",
            json={"path": "/users/alice/workspace/demo-template"},
        ).json()
        assert body["ignored_fields"] == []
        from storage import agent_store
        assert agent_store.get_agent("demo-template")["default_for_new_users_role"] == "viewer"

    def test_slug_collision_returns_409_with_suggestion(self, tmp_path, monkeypatch, temp_db):
        from storage import agent_store
        agent_store.create_agent("demo-template", "Demo", admin_only=False)
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        _write_template(_agent_root(agents_dir))
        resp = TestClient(app).post(
            "/v1/agents/install-from-local-template",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["suggested_slug"] == "demo-template-2"

    def test_target_slug_override(self, tmp_path, monkeypatch, temp_db):
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        _write_template(_agent_root(agents_dir))
        body = TestClient(app).post(
            "/v1/agents/install-from-local-template",
            json={"path": "/users/alice/workspace/demo-template",
                  "target_slug": "renamed-agent"},
        ).json()
        assert body["agent_slug"] == "renamed-agent"
        from storage import agent_store
        # Provenance still records the TEMPLATE's slug, not the install name.
        assert agent_store.get_agent("renamed-agent")["community_template"] == "local:demo-template"

    def test_shared_workspace_path_is_accepted(self, tmp_path, monkeypatch, temp_db):
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        _write_template(agents_dir / "host-agent" / "workspace")
        resp = TestClient(app).post(
            "/v1/agents/install-from-local-template",
            json={"path": "/workspace/demo-template"},
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Provenance interactions
# ---------------------------------------------------------------------------

class TestProvenance:
    def test_local_agent_not_reported_as_catalog_install(self, tmp_path, monkeypatch, temp_db):
        """Browse matches ``installed_as`` by bare template slug — a local
        agent sharing a catalog slug must not light up the catalog card."""
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        _write_template(_agent_root(agents_dir), slug="personal-assistant")
        TestClient(app).post(
            "/v1/agents/install-from-local-template",
            json={"path": "/users/alice/workspace/personal-assistant"},
        )
        import asyncio
        from services.community import community_agents_catalog
        state = asyncio.run(community_agents_catalog.collect_local_state())
        assert "personal-assistant" not in state
        assert state.get("local:personal-assistant") == ["personal-assistant"]

    def test_later_joiner_seeding_still_works(self, tmp_path, monkeypatch, temp_db):
        """``community_template_data`` drives per-user seeding for users who
        attach after the install — the ``local:`` marker must not break it."""
        app, agents_dir = _make_app(tmp_path, monkeypatch, sub=ADMIN_SUB, role="admin")
        _write_template(
            _agent_root(agents_dir),
            notifications=[{
                "slug": "daily", "title": "Daily", "body": "Check in",
                "scope": "user", "schedule": {"type": "cron", "cron": "0 9 * * *"},
                "auto_create_for_new_users": True,
            }],
        )
        TestClient(app).post(
            "/v1/agents/install-from-local-template",
            json={"path": "/users/alice/workspace/demo-template"},
        )
        from services.community import community_agent_installer
        counts = community_agent_installer.on_user_added_to_agent(
            "demo-template", MEMBER_SUB, "viewer",
        )
        assert counts["notifications"] == 1
