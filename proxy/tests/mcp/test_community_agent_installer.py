"""Tests for the community-agents installer.

Covers:
- Template parsing + validation (load_template_from_dir)
- Pre-flight (MCP-not-in-any-catalog hard error)
- Slug collision suggestion
- Admin cascade: all MCPs resolved inline, no requests
- Manager cascade: batch_id generated, mcp_assignment_requests rows created
- Task/trigger/notification seeding (idempotent)
- Cascade cleanup invariants
- Notification batching (one per admin per batch)
- Batch completion follow-up notification

Run: cd proxy && venv/bin/pytest tests/mcp/test_community_agent_installer.py -v
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ADMIN_SUB = "user-admin"
MANAGER_SUB = "user-manager"


def _write_template(
    tmp_path: Path,
    *,
    slug: str = "demo-template",
    mcps: list[dict] | None = None,
    tasks: list[dict] | None = None,
    triggers: list[dict] | None = None,
    notifications: list[dict] | None = None,
    setup_md: str | None = None,
    context_files: dict[str, str] | None = None,
) -> Path:
    """Write a minimal valid template directory under tmp_path/<slug>/."""
    template_dir = tmp_path / slug
    template_dir.mkdir(parents=True, exist_ok=True)

    agent_json = {
        "schema_version": "1",
        "slug": slug,
        "display_name": slug.replace("-", " ").title(),
        "description": "Test template",
        "color": "#10B981",
        "version": "1.0.0",
    }
    (template_dir / "agent.json").write_text(json.dumps(agent_json))
    (template_dir / "prompt.md").write_text("# Test Prompt\n")
    (template_dir / "mcps.json").write_text(
        json.dumps({"required": mcps or []})
    )
    (template_dir / "README.md").write_text("# README\n")
    if tasks:
        (template_dir / "tasks.json").write_text(json.dumps({"tasks": tasks}))
    if triggers:
        (template_dir / "triggers.json").write_text(json.dumps({"triggers": triggers}))
    if notifications:
        (template_dir / "notifications.json").write_text(
            json.dumps({"notifications": notifications})
        )
    if setup_md is not None:
        (template_dir / "setup.md").write_text(setup_md)
    if context_files:
        context_dir = template_dir / "context"
        context_dir.mkdir()
        for rel, content in context_files.items():
            (context_dir / rel).write_text(content)
    return template_dir


def _install_admin(template_dir: Path, target_slug: str = "demo-agent",
                   installer_sub: str | None = ADMIN_SUB,
                   installer_role: str = "admin"):
    # Tests stage templates as plain on-disk dirs and install directly via
    # the shared orchestrator. Production code only uses
    # ``install_from_catalog`` (which fetches the tarball first); this helper
    # skips the fetch since the test owns the dir.
    from services.community.community_agent_installer import install_from_extracted_template
    from storage.community_agent_template_store import load_template_from_dir
    template = load_template_from_dir(template_dir)
    return asyncio.run(install_from_extracted_template(
        template=template, target_slug=target_slug,
        installer_user_sub=installer_sub, installer_role=installer_role,
        source_label="test",
    ))


# ---------------------------------------------------------------------------
# Template parsing
# ---------------------------------------------------------------------------

class TestTemplateLoading:
    def test_minimal_valid_template(self, tmp_path, temp_db):
        from storage.community_agent_template_store import load_template_from_dir
        tdir = _write_template(tmp_path)
        template = load_template_from_dir(tdir)
        assert template.slug == "demo-template"
        assert template.display_name == "Demo Template"
        assert template.mcps == []
        assert template.tasks == []
        assert template.context_files == {}

    def test_missing_agent_json_raises(self, tmp_path, temp_db):
        from storage.community_agent_template_store import (
            load_template_from_dir, TemplateValidationError,
        )
        tdir = tmp_path / "bad"
        tdir.mkdir()
        (tdir / "prompt.md").write_text("x")
        (tdir / "mcps.json").write_text("{}")
        (tdir / "README.md").write_text("x")
        with pytest.raises(TemplateValidationError, match="agent.json"):
            load_template_from_dir(tdir)

    def test_invalid_slug_in_agent_json(self, tmp_path, temp_db):
        from storage.community_agent_template_store import (
            load_template_from_dir, TemplateValidationError,
        )
        tdir = tmp_path / "demo"
        tdir.mkdir()
        (tdir / "agent.json").write_text(json.dumps({
            "slug": "BadSlug", "display_name": "x", "version": "1.0.0",
        }))
        (tdir / "prompt.md").write_text("x")
        (tdir / "mcps.json").write_text("{}")
        (tdir / "README.md").write_text("x")
        with pytest.raises(TemplateValidationError, match="invalid slug"):
            load_template_from_dir(tdir)

    def test_invalid_cron_in_tasks(self, tmp_path, temp_db):
        from storage.community_agent_template_store import (
            load_template_from_dir, TemplateValidationError,
        )
        tdir = _write_template(tmp_path, tasks=[{
            "slug": "bad-task", "description": "x", "scope": "agent",
            "prompt": "echo",
            "schedule": {"type": "cron", "cron": "not a cron"},
        }])
        with pytest.raises(TemplateValidationError, match="invalid cron"):
            load_template_from_dir(tdir)

    def test_valid_task_with_cron_schedule(self, tmp_path, temp_db):
        from storage.community_agent_template_store import load_template_from_dir
        tdir = _write_template(tmp_path, tasks=[{
            "slug": "good-task", "description": "Test",
            "scope": "user", "prompt": "echo",
            "schedule": {"type": "cron", "cron": "0 9 * * *"},
            "default_state": "paused",
        }])
        template = load_template_from_dir(tdir)
        assert len(template.tasks) == 1
        assert template.tasks[0].cron == "0 9 * * *"

    def test_setup_md_parsed_when_present(self, tmp_path, temp_db):
        from storage.community_agent_template_store import load_template_from_dir
        tdir = _write_template(tmp_path, setup_md="## Setup steps\n1. Do X\n")
        template = load_template_from_dir(tdir)
        assert template.setup_md is not None
        assert "Setup steps" in template.setup_md

    def test_context_collected(self, tmp_path, temp_db):
        from storage.community_agent_template_store import load_template_from_dir
        tdir = _write_template(tmp_path, context_files={
            "methodology.md": "## Methodology", "glossary.txt": "terms",
        })
        template = load_template_from_dir(tdir)
        assert set(template.context_files.keys()) == {
            "context/methodology.md", "context/glossary.txt",
        }


# ---------------------------------------------------------------------------
# Pre-flight validation
# ---------------------------------------------------------------------------

class TestPreflight:
    def test_unknown_mcp_blocks_install(self, tmp_path, temp_db):
        from fastapi import HTTPException
        tdir = _write_template(tmp_path, mcps=[{"name": "nonexistent-mcp"}])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ):
            with pytest.raises(HTTPException) as exc:
                _install_admin(tdir)
        assert exc.value.status_code == 400
        assert "missing_mcps" in str(exc.value.detail)

    def test_mcp_in_catalog_passes_preflight(self, tmp_path, temp_db):
        from services.community.community_agent_installer import _preflight_check_mcps
        from storage.community_agent_template_store import McpRequirement

        with patch(
            "services.community.community_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": [{"name": "future-mcp"}]}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ):
            # Should not raise.
            asyncio.run(_preflight_check_mcps([McpRequirement(name="future-mcp")]))


# ---------------------------------------------------------------------------
# Slug collision
# ---------------------------------------------------------------------------

class TestSlugCollision:
    def test_propose_free_slug_appends_2(self, temp_db):
        from services.community.community_agent_installer import _propose_free_slug
        from storage import agent_store
        agent_store.create_agent("foo", "Foo")
        assert _propose_free_slug("foo") == "foo-2"

    def test_propose_free_slug_skips_existing_suffix(self, temp_db):
        from services.community.community_agent_installer import _propose_free_slug
        from storage import agent_store
        agent_store.create_agent("foo", "Foo")
        agent_store.create_agent("foo-2", "Foo Two")
        assert _propose_free_slug("foo") == "foo-3"

    def test_install_collision_returns_409_with_suggestion(self, tmp_path, temp_db):
        from fastapi import HTTPException
        from storage import agent_store
        agent_store.create_agent("demo-agent", "Existing")
        tdir = _write_template(tmp_path)
        with pytest.raises(HTTPException) as exc:
            _install_admin(tdir, target_slug="demo-agent")
        assert exc.value.status_code == 409
        detail = exc.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "slug_taken"
        assert detail["suggested_slug"] == "demo-agent-2"


# ---------------------------------------------------------------------------
# Admin cascade — all MCPs resolved inline
# ---------------------------------------------------------------------------


class _StubAutoManifest:
    """Stand-in for a parsed MCP manifest with auto assignment_mode."""

    def __init__(self, name: str, skills: list | None = None):
        self.name = name
        self.assignment_mode = "auto"
        self.category = "community"
        self.skills = skills or []
        self.exclude_from: list[str] = []


class TestAdminCascade:
    def test_admin_install_with_only_auto_installed_mcps(self, tmp_path, temp_db):
        """Auto-mode MCP already installed → just enable, no requests created."""
        from storage import mcp_store

        tdir = _write_template(tmp_path, mcps=[{"name": "auto-mcp"}])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={"auto-mcp": _StubAutoManifest("auto-mcp")},
        ), patch(
            "services.mcp.mcp_registry.get_manifest",
            side_effect=lambda n: _StubAutoManifest("auto-mcp") if n == "auto-mcp" else None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            result = _install_admin(tdir)

        assert result["agent_slug"] == "demo-agent"
        assert result["batch_id"] is None
        assert result["created_requests"] == []
        assert result["ready_mcps"] == ["auto-mcp"]
        assert "auto-mcp" in mcp_store.get_manager_enabled_mcps("demo-agent")


# ---------------------------------------------------------------------------
# Manager cascade — batch_id generated, requests pending admin
# ---------------------------------------------------------------------------


class TestManagerCascade:
    def test_manager_install_with_missing_mcp_creates_request(self, tmp_path, temp_db):
        from storage import mcp_request_store

        tdir = _write_template(tmp_path, mcps=[{"name": "missing-mcp"}])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.community.community_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": [{"name": "missing-mcp"}]}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.mcp.mcp_registry.get_manifest",
            return_value=None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            result = _install_admin(
                tdir, installer_sub=MANAGER_SUB, installer_role="manager",
            )

        assert result["batch_id"] is not None
        assert len(result["created_requests"]) == 1
        req = result["created_requests"][0]
        assert req["mcp_name"] == "missing-mcp"
        assert req["status"] == "pending"
        assert req["batch_id"] == result["batch_id"]
        # Confirm it's persisted.
        rows = mcp_request_store.list_requests_by_batch(result["batch_id"])
        assert len(rows) == 1

    def test_manager_install_two_missing_mcps_share_one_batch(self, tmp_path, temp_db):
        from storage import mcp_request_store

        tdir = _write_template(tmp_path, mcps=[
            {"name": "first-mcp"}, {"name": "second-mcp"},
        ])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.community.community_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": [
                {"name": "first-mcp"}, {"name": "second-mcp"},
            ]}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.mcp.mcp_registry.get_manifest",
            return_value=None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            result = _install_admin(
                tdir, installer_sub=MANAGER_SUB, installer_role="manager",
            )

        assert result["batch_id"] is not None
        rows = mcp_request_store.list_requests_by_batch(result["batch_id"])
        assert {r["mcp_name"] for r in rows} == {"first-mcp", "second-mcp"}


# ---------------------------------------------------------------------------
# Template-item seeding
# ---------------------------------------------------------------------------

class TestSeeding:
    def test_seeds_agent_scope_task_from_template(self, tmp_path, temp_db):
        from storage import database as db

        tdir = _write_template(tmp_path, tasks=[{
            "slug": "daily-check", "description": "Daily check",
            "scope": "agent", "prompt": "echo",
            "schedule": {"type": "cron", "cron": "0 8 * * *"},
            "default_state": "paused",
        }])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            _install_admin(tdir)

        tasks = db.list_dynamic_tasks(agent="demo-agent")
        assert len(tasks) == 1
        assert tasks[0]["community_template"] == "demo-template"
        assert tasks[0]["community_template_item_slug"] == "daily-check"
        assert tasks[0]["scope"] == "agent"
        # default_state=paused → enabled=False
        assert tasks[0]["enabled"] is False

    def test_seeds_user_scope_task_for_installer(self, tmp_path, temp_db):
        from storage import database as db

        tdir = _write_template(tmp_path, tasks=[{
            "slug": "user-task", "description": "Per-user",
            "scope": "user", "prompt": "echo",
            "schedule": {"type": "cron", "cron": "0 10 * * *"},
            "default_state": "active",
            "auto_create_for_new_users": True,
        }])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            _install_admin(tdir)

        tasks = db.list_dynamic_tasks(agent="demo-agent")
        assert len(tasks) == 1
        assert tasks[0]["scope"] == "user"
        assert tasks[0]["created_by"] == ADMIN_SUB
        assert tasks[0]["enabled"] is True


# ---------------------------------------------------------------------------
# Notification batching
# ---------------------------------------------------------------------------

class TestNotificationBatching:
    def test_batch_create_fires_one_notification_per_admin(self, tmp_path, temp_db):
        """Two missing MCPs in one cascade → one notification per admin, not
        two (per-request notifications are suppressed for batched rows)."""

        fire_mock = AsyncMock()
        tdir = _write_template(tmp_path, mcps=[
            {"name": "missing-a"}, {"name": "missing-b"},
        ])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.community.community_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": [
                {"name": "missing-a"}, {"name": "missing-b"},
            ]}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.mcp.mcp_registry.get_manifest",
            return_value=None,
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=fire_mock,
        ):
            _install_admin(
                tdir, installer_sub=MANAGER_SUB, installer_role="manager",
            )

        # Only one admin in the seed → exactly one notification, with both
        # MCPs in the body. Setup notification fires if setup.md present;
        # template has no setup.md, so this is the only call.
        admin_notifs = [
            call for call in fire_mock.call_args_list
            if call.kwargs.get("target") == ADMIN_SUB
        ]
        assert len(admin_notifs) == 1
        body = admin_notifs[0].kwargs["body"]
        assert "missing-a" in body
        assert "missing-b" in body


# ---------------------------------------------------------------------------
# Cascade cleanup invariants under template-seeded items
# ---------------------------------------------------------------------------

class TestSeededCleanupInvariants:
    def test_delete_agent_removes_seeded_items(self, tmp_path, temp_db):
        from storage import agent_store, database as db

        tdir = _write_template(tmp_path, tasks=[{
            "slug": "smoke-task", "description": "x", "scope": "agent",
            "prompt": "echo",
            "schedule": {"type": "cron", "cron": "0 8 * * *"},
        }])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            _install_admin(tdir, target_slug="doomed-agent")

        assert len(db.list_dynamic_tasks(agent="doomed-agent")) == 1
        agent_store.delete_agent("doomed-agent")
        assert db.list_dynamic_tasks(agent="doomed-agent") == []


# ---------------------------------------------------------------------------
# On_user_added_to_agent hook (catalog-aware seeding)
# ---------------------------------------------------------------------------

def _install_with_user_items(tmp_path, *, default_for_new_users=None):
    """Helper: install a template that declares ALL three user-scope items.

    Returns the installed agent_slug. ADMIN_SUB is the installer/manager.
    """
    agent_json_extras = {}
    if default_for_new_users is not None:
        agent_json_extras["default_for_new_users"] = default_for_new_users

    tdir = _write_template(
        tmp_path,
        slug="hooktpl",
        tasks=[{
            "slug": "user-task", "description": "Per-user task",
            "scope": "user", "prompt": "echo task",
            "schedule": {"type": "cron", "cron": "0 9 * * *"},
            "default_state": "paused",
            "auto_create_for_new_users": True,
        }],
        triggers=[{
            "slug": "user-trig", "description": "Per-user trigger",
            "scope": "user", "prompt": "echo trig",
            "default_state": "paused",
            "auto_create_for_new_users": True,
        }],
        notifications=[{
            "slug": "user-notif", "title": "Per-user notification",
            "body": "body", "scope": "user",
            "schedule": {"type": "cron", "cron": "0 12 * * *"},
            "default_state": "active",
            "auto_create_for_new_users": True,
        }],
    )
    # Patch the agent.json with optional default_for_new_users block.
    if agent_json_extras:
        agent_json_path = tdir / "agent.json"
        existing = json.loads(agent_json_path.read_text())
        existing.update(agent_json_extras)
        agent_json_path.write_text(json.dumps(existing))

    with patch(
        "services.community.community_agents_catalog.fetch_registry",
        new=AsyncMock(return_value={"mcps": []}),
    ), patch(
        "services.mcp.mcp_registry.get_all_manifests",
        return_value={},
    ), patch(
        "services.notifications.notification_manager.fire_notification",
        new=AsyncMock(),
    ):
        _install_admin(tdir, target_slug="hook-agent")
    return "hook-agent"


def _make_user(sub: str, email: str, role: str = "member") -> None:
    """Insert a minimal users row directly so add_user_agent's FK clears."""
    from storage import database as db
    # Use upsert_user — covers both fresh creation and re-runs.
    db.upsert_user(sub, email, email.split("@")[0], role)


class TestUserJoinHook:
    """``on_user_added_to_agent`` re-seeds per-user template items
    when a user is attached to a community-template agent after install."""

    def test_persists_template_data_at_install_time(self, tmp_path, temp_db):
        from storage import agent_store
        agent_slug = _install_with_user_items(tmp_path)
        data = agent_store.get_community_template_data(agent_slug)
        assert data is not None
        assert data["slug"] == "hooktpl"
        assert len(data["tasks"]) == 1
        assert len(data["triggers"]) == 1
        assert len(data["notifications"]) == 1

    def test_hook_seeds_items_for_late_joiner(self, tmp_path, temp_db):
        from storage import database as db
        from services.community.community_agent_installer import on_user_added_to_agent

        agent_slug = _install_with_user_items(tmp_path)
        _make_user("user-bob", "bob@example.com")
        db.add_user_agent("user-bob", agent_slug, "viewer", "system")

        counts = on_user_added_to_agent(agent_slug, "user-bob", "viewer")
        assert counts == {"tasks": 1, "triggers": 1, "notifications": 1}

        # bob owns: the user-task itself + the trigger's paired task
        # (trigger model spawns a dynamic_tasks row with
        # task_type='trigger' alongside the trigger row). Filter to the
        # standalone user-task by its template slug.
        bob_user_tasks = [
            t for t in db.list_dynamic_tasks(agent=agent_slug)
            if t["created_by"] == "user-bob"
            and t["scope"] == "user"
            and t["community_template_item_slug"] == "user-task"
        ]
        assert len(bob_user_tasks) == 1

    def test_hook_is_idempotent(self, tmp_path, temp_db):
        from storage import database as db
        from services.community.community_agent_installer import on_user_added_to_agent

        agent_slug = _install_with_user_items(tmp_path)
        _make_user("user-bob", "bob@example.com")
        db.add_user_agent("user-bob", agent_slug, "viewer", "system")

        first = on_user_added_to_agent(agent_slug, "user-bob", "viewer")
        second = on_user_added_to_agent(agent_slug, "user-bob", "viewer")
        # First call seeds, second call sees the unique-index conflict and
        # returns 0 across the board.
        assert first == {"tasks": 1, "triggers": 1, "notifications": 1}
        assert second == {"tasks": 0, "triggers": 0, "notifications": 0}

    def test_hook_respects_role_filter(self, tmp_path, temp_db):
        """Item with ``roles: ["manager"]`` is NOT seeded for a viewer."""
        from storage import database as db
        from services.community.community_agent_installer import on_user_added_to_agent

        tdir = _write_template(tmp_path, slug="rolefilter", tasks=[{
            "slug": "mgr-only", "description": "Manager-only",
            "scope": "user", "prompt": "echo",
            "schedule": {"type": "cron", "cron": "0 9 * * *"},
            "default_state": "paused",
            "auto_create_for_new_users": True,
            "roles": ["manager"],
        }])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            _install_admin(tdir, target_slug="role-agent")

        _make_user("user-viewer", "viewer@example.com")
        db.add_user_agent("user-viewer", "role-agent", "viewer", "system")
        counts = on_user_added_to_agent("role-agent", "user-viewer", "viewer")
        assert counts == {"tasks": 0, "triggers": 0, "notifications": 0}

        _make_user("user-mgr", "mgr@example.com")
        db.add_user_agent("user-mgr", "role-agent", "manager", "system")
        mgr_counts = on_user_added_to_agent("role-agent", "user-mgr", "manager")
        assert mgr_counts["tasks"] == 1

    def test_hook_noop_for_non_community_agent(self, tmp_path, temp_db):
        """Agents not installed from a template have no template_data; hook
        returns empty counts without raising."""
        from storage import agent_store
        from services.community.community_agent_installer import on_user_added_to_agent

        agent_store.create_agent("native-agent", "Native Agent")
        counts = on_user_added_to_agent("native-agent", "user-anon", "viewer")
        assert counts == {"tasks": 0, "triggers": 0, "notifications": 0}

    def test_hook_skips_auto_create_for_new_users_false(self, tmp_path, temp_db):
        """Items where ``auto_create_for_new_users=False`` are NOT seeded for
        late joiners (only the installer's items at install time)."""
        from storage import database as db
        from services.community.community_agent_installer import on_user_added_to_agent

        tdir = _write_template(tmp_path, slug="noauto", tasks=[{
            "slug": "no-auto", "description": "Don't auto-create",
            "scope": "user", "prompt": "echo",
            "schedule": {"type": "cron", "cron": "0 9 * * *"},
            "default_state": "paused",
            "auto_create_for_new_users": False,
        }])
        with patch(
            "services.community.community_agents_catalog.fetch_registry",
            new=AsyncMock(return_value={"mcps": []}),
        ), patch(
            "services.mcp.mcp_registry.get_all_manifests",
            return_value={},
        ), patch(
            "services.notifications.notification_manager.fire_notification",
            new=AsyncMock(),
        ):
            _install_admin(tdir, target_slug="noauto-agent")
        _make_user("user-late", "late@example.com")
        db.add_user_agent("user-late", "noauto-agent", "manager", "system")
        counts = on_user_added_to_agent("noauto-agent", "user-late", "manager")
        assert counts["tasks"] == 0

    def test_install_writes_default_for_new_users_role(self, tmp_path, temp_db):
        from storage import agent_store
        agent_slug = _install_with_user_items(
            tmp_path,
            default_for_new_users={"enabled": True, "role": "viewer"},
        )
        agent = agent_store.get_agent(agent_slug)
        assert agent["default_for_new_users_role"] == "viewer"

    def test_install_default_for_new_users_disabled_keeps_empty(self, tmp_path, temp_db):
        from storage import agent_store
        agent_slug = _install_with_user_items(
            tmp_path,
            default_for_new_users={"enabled": False},
        )
        agent = agent_store.get_agent(agent_slug)
        assert agent["default_for_new_users_role"] == ""

    def test_invalid_default_role_rejected_at_load(self, tmp_path, temp_db):
        from storage.community_agent_template_store import (
            load_template_from_dir, TemplateValidationError,
        )
        tdir = _write_template(tmp_path, slug="badrole")
        agent_json_path = tdir / "agent.json"
        existing = json.loads(agent_json_path.read_text())
        existing["default_for_new_users"] = {"enabled": True, "role": "owner"}
        agent_json_path.write_text(json.dumps(existing))
        with pytest.raises(TemplateValidationError, match="default_for_new_users"):
            load_template_from_dir(tdir)
