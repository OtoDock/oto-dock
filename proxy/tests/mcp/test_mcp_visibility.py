"""Tests for the MCP visibility/enablement two-state model + assigned_to_all
toggle + instance precedence.

The model:
  - VISIBLE = admin's authority (auto MCPs always; explicit MCPs via instance
    authorization, either via explicit agents list or assigned_to_all=True)
  - ENABLED = manager's authority (row in agent_mcps)
  - Runtime requires (visible AND enabled AND platform-enabled).

These tests cover the storage layer, registry intersection, API endpoint
shape, and the integration scenario for the original bug.
"""

from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_manifest(name, assignment_mode="auto", category="custom", has_instances=False, delivery="env"):
    """Construct a minimal McpManifest for tests."""
    from services.mcp.mcp_registry import (
        McpManifest, ServerConfig, CredentialConfig, InstanceConfig,
    )
    instances = None
    if has_instances:
        instances = InstanceConfig(delivery=delivery, fields=[], max_instances=0)
    return McpManifest(
        name=name,
        label=name.replace("-", " ").title(),
        description="",
        version="1.0.0",
        category=category,
        server=ServerConfig(runtime="python", transport="stdio"),
        credentials=CredentialConfig(type="none"),
        config=[],
        env={},
        agent_env={},
        exclude_from=[],
        skills=[],
        assignment_mode=assignment_mode,
        instances=instances,
    )


def _patch_manifests(monkeypatch, manifests: dict):
    """Replace mcp_registry._manifests + enable each in mcp_state."""
    from services.mcp import mcp_registry
    from storage import mcp_store
    monkeypatch.setattr(mcp_registry, "_manifests", manifests)
    for name in manifests:
        mcp_store.set_mcp_enabled(name, True)


def _seed_agent(slug: str):
    from storage.pg import get_conn
    now = _now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO agents
               (slug, display_name, created_at, updated_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (slug, slug.replace("-", " ").title(), now, now),
        )
        conn.commit()


def _make_instance(mcp_name: str, instance_name: str, *,
                   agents: list[str] | None = None,
                   assigned_to_all: bool = False) -> int:
    """Create an MCP instance row. Returns the inserted id."""
    from storage import mcp_store
    return mcp_store.upsert_mcp_instance(
        mcp_name,
        {
            "instance_name": instance_name,
            "field_values": {"API_KEY": f"key-for-{instance_name}"},
            "agents": agents or [],
            "assigned_to_all": assigned_to_all,
        },
    )


# ---------------------------------------------------------------------------
# Storage layer — visibility helpers
# ---------------------------------------------------------------------------


class TestVisibilityHelpers:
    def test_explicit_mcp_no_instances_no_visibility(self, temp_db):
        """An explicit-mode MCP with zero instances authorizes nobody."""
        from storage import mcp_store
        assert mcp_store.is_agent_authorized_for_mcp("image-gen", "alice") is False
        assert mcp_store.get_visible_explicit_mcps("alice") == set()

    def test_explicit_mcp_agent_in_list(self, temp_db):
        """Agent in the instance's agents list → visible. Other agents → not."""
        from storage import mcp_store
        _make_instance("image-gen", "primary", agents=["alice"])
        assert mcp_store.is_agent_authorized_for_mcp("image-gen", "alice") is True
        assert mcp_store.is_agent_authorized_for_mcp("image-gen", "bob") is False
        assert mcp_store.get_visible_explicit_mcps("alice") == {"image-gen"}
        assert mcp_store.get_visible_explicit_mcps("bob") == set()

    def test_explicit_mcp_assigned_to_all(self, temp_db):
        """assigned_to_all=True authorizes every agent (incl. unknown ones)."""
        from storage import mcp_store
        _make_instance("image-gen", "shared", agents=[], assigned_to_all=True)
        for agent in ("alice", "bob", "newcomer"):
            assert mcp_store.is_agent_authorized_for_mcp("image-gen", agent) is True
            assert "image-gen" in mcp_store.get_visible_explicit_mcps(agent)

    def test_explicit_mcp_combined(self, temp_db):
        """agents list + assigned_to_all both set → still visible to every agent."""
        from storage import mcp_store
        _make_instance("image-gen", "combined",
                       agents=["alice"], assigned_to_all=True)
        assert mcp_store.is_agent_authorized_for_mcp("image-gen", "alice") is True
        assert mcp_store.is_agent_authorized_for_mcp("image-gen", "bob") is True

    def test_visibility_after_revoke(self, temp_db):
        """Removing agent from agents list flips visibility off (if not assigned_to_all)."""
        from storage import mcp_store
        _make_instance("image-gen", "primary", agents=["alice"])
        assert mcp_store.is_agent_authorized_for_mcp("image-gen", "alice") is True
        # Update with empty agents list (admin revokes)
        mcp_store.upsert_mcp_instance("image-gen", {
            "instance_name": "primary",
            "field_values": {"API_KEY": "k"},
            "agents": [],
            "assigned_to_all": False,
        })
        assert mcp_store.is_agent_authorized_for_mcp("image-gen", "alice") is False

    def test_visibility_isolated_per_mcp(self, temp_db):
        """Two MCPs, only one authorizes alice."""
        from storage import mcp_store
        _make_instance("image-gen", "primary", agents=["alice"])
        _make_instance("ssh-server", "h1", agents=["bob"])
        assert mcp_store.get_visible_explicit_mcps("alice") == {"image-gen"}
        assert mcp_store.get_visible_explicit_mcps("bob") == {"ssh-server"}


# ---------------------------------------------------------------------------
# Storage layer — instance precedence (env delivery)
# ---------------------------------------------------------------------------


class TestInstancePrecedence:
    def test_env_delivery_explicit_beats_all(self, temp_db):
        """Two instances: one explicit-for-alice, one assigned_to_all → returns explicit."""
        from storage import mcp_store
        # Create the catch-all FIRST (so it has a lower id)
        all_id = _make_instance("image-gen", "shared",
                                agents=[], assigned_to_all=True)
        explicit_id = _make_instance("image-gen", "alice-key",
                                     agents=["alice"])
        assert all_id < explicit_id  # confirm ordering

        chosen = mcp_store.get_instance_for_agent_env_delivery(
            "image-gen", "alice",
        )
        assert chosen is not None
        assert chosen["id"] == explicit_id
        assert chosen["instance_name"] == "alice-key"

    def test_env_delivery_only_assigned_to_all(self, temp_db):
        """Only an assigned_to_all instance → returns it."""
        from storage import mcp_store
        iid = _make_instance("image-gen", "shared",
                             agents=[], assigned_to_all=True)
        chosen = mcp_store.get_instance_for_agent_env_delivery(
            "image-gen", "alice",
        )
        assert chosen is not None
        assert chosen["id"] == iid

    def test_env_delivery_two_explicit_lowest_id(self, temp_db):
        """Two explicit matches → returns lowest id deterministically."""
        from storage import mcp_store
        first_id = _make_instance("image-gen", "instance-a", agents=["alice"])
        second_id = _make_instance("image-gen", "instance-b", agents=["alice"])
        assert first_id < second_id

        chosen = mcp_store.get_instance_for_agent_env_delivery(
            "image-gen", "alice",
        )
        assert chosen["id"] == first_id

    def test_env_delivery_two_all_lowest_id(self, temp_db):
        """Two assigned_to_all matches → returns lowest id."""
        from storage import mcp_store
        first_id = _make_instance("image-gen", "shared-a",
                                  agents=[], assigned_to_all=True)
        _make_instance("image-gen", "shared-b",
                       agents=[], assigned_to_all=True)
        chosen = mcp_store.get_instance_for_agent_env_delivery(
            "image-gen", "alice",
        )
        assert chosen["id"] == first_id

    def test_env_delivery_no_match_returns_none(self, temp_db):
        """No instance authorizes the agent → None."""
        from storage import mcp_store
        _make_instance("image-gen", "primary", agents=["bob"])
        assert mcp_store.get_instance_for_agent_env_delivery(
            "image-gen", "alice",
        ) is None

    def test_config_file_delivery_unions(self, temp_db):
        """get_mcp_instances_for_agent unions explicit + assigned_to_all, ordered by id."""
        from storage import mcp_store
        # Insertion order matters for id assignment
        explicit_id = _make_instance("ssh-server", "host-a", agents=["alice"])
        all_id = _make_instance("ssh-server", "host-b",
                                agents=[], assigned_to_all=True)
        _make_instance("ssh-server", "host-c", agents=["bob"])

        result = mcp_store.get_mcp_instances_for_agent("ssh-server", "alice")
        ids = [i["id"] for i in result]
        # Alice gets host-a (explicit) and host-b (assigned_to_all), not host-c
        assert ids == [explicit_id, all_id]

    def test_upsert_persists_assigned_to_all(self, temp_db):
        """upsert_mcp_instance round-trips assigned_to_all."""
        from storage import mcp_store
        iid = _make_instance("image-gen", "shared", assigned_to_all=True)
        instances = mcp_store.get_mcp_instances("image-gen")
        match = next(i for i in instances if i["id"] == iid)
        assert match["assigned_to_all"] is True

        # Toggle off
        mcp_store.upsert_mcp_instance("image-gen", {
            "instance_name": "shared",
            "field_values": {"API_KEY": "k"},
            "agents": ["alice"],
            "assigned_to_all": False,
        })
        instances = mcp_store.get_mcp_instances("image-gen")
        match = next(i for i in instances if i["id"] == iid)
        assert match["assigned_to_all"] is False
        assert match["agents"] == ["alice"]  # not stripped


# ---------------------------------------------------------------------------
# Registry layer — get_agent_mcps intersection
# ---------------------------------------------------------------------------


class TestRegistryIntersection:
    def test_visible_and_enabled_returns_manifest(self, temp_db, monkeypatch):
        """auto MCP, manager-enabled, platform-enabled → returned."""
        from services.mcp import mcp_registry
        from storage import mcp_store
        _patch_manifests(monkeypatch, {
            "schedules-mcp": _make_manifest("schedules-mcp", assignment_mode="auto"),
        })
        mcp_store.add_agent_mcp("alice", "schedules-mcp")
        result = mcp_registry.get_agent_mcps("alice")
        assert [m.name for m in result] == ["schedules-mcp"]

    def test_enabled_but_not_visible(self, temp_db, monkeypatch):
        """Explicit MCP with no authorizing instance → NOT returned even if enabled."""
        from services.mcp import mcp_registry
        from storage import mcp_store
        _patch_manifests(monkeypatch, {
            "image-gen": _make_manifest(
                "image-gen", assignment_mode="explicit", has_instances=True,
            ),
        })
        # Manager toggled it on, but no instance authorizes
        mcp_store.add_agent_mcp("alice", "image-gen")
        result = mcp_registry.get_agent_mcps("alice")
        assert result == []

    def test_visible_but_not_enabled(self, temp_db, monkeypatch):
        """Admin authorized but manager hasn't enabled → NOT returned."""
        from services.mcp import mcp_registry
        _patch_manifests(monkeypatch, {
            "image-gen": _make_manifest(
                "image-gen", assignment_mode="explicit", has_instances=True,
            ),
        })
        _make_instance("image-gen", "primary", agents=["alice"])
        # No call to add_agent_mcp — manager hasn't toggled on
        result = mcp_registry.get_agent_mcps("alice")
        assert result == []

    def test_state_disabled(self, temp_db, monkeypatch):
        """Visible + manager-enabled but mcp_state.enabled=False → NOT returned."""
        from services.mcp import mcp_registry
        from storage import mcp_store
        _patch_manifests(monkeypatch, {
            "schedules-mcp": _make_manifest("schedules-mcp", assignment_mode="auto"),
        })
        mcp_store.add_agent_mcp("alice", "schedules-mcp")
        mcp_store.set_mcp_enabled("schedules-mcp", False)
        result = mcp_registry.get_agent_mcps("alice")
        assert result == []

    def test_revoke_after_enable_filters_out(self, temp_db, monkeypatch):
        """Manager enabled, admin revoked authorization → runtime drops, but agent_mcps row persists."""
        from services.mcp import mcp_registry
        from storage import mcp_store
        _patch_manifests(monkeypatch, {
            "image-gen": _make_manifest(
                "image-gen", assignment_mode="explicit", has_instances=True,
            ),
        })
        _make_instance("image-gen", "primary", agents=["alice"])
        mcp_store.add_agent_mcp("alice", "image-gen")
        # Pre-revoke: visible at runtime
        assert [m.name for m in mcp_registry.get_agent_mcps("alice")] == ["image-gen"]

        # Admin revokes by emptying agents
        mcp_store.upsert_mcp_instance("image-gen", {
            "instance_name": "primary",
            "field_values": {},
            "agents": [],
            "assigned_to_all": False,
        })
        # Runtime drops
        assert mcp_registry.get_agent_mcps("alice") == []
        # But the manager's intent (agent_mcps row) is preserved
        assert "image-gen" in mcp_store.get_manager_enabled_mcps("alice")

        # Re-authorize → comes back automatically
        mcp_store.upsert_mcp_instance("image-gen", {
            "instance_name": "primary",
            "field_values": {},
            "agents": ["alice"],
            "assigned_to_all": False,
        })
        assert [m.name for m in mcp_registry.get_agent_mcps("alice")] == ["image-gen"]

    def test_get_visible_mcps_returns_disabled_explicit(self, temp_db, monkeypatch):
        """get_visible_mcps_for_agent returns admin-authorized MCPs even when manager hasn't enabled."""
        from services.mcp import mcp_registry
        _patch_manifests(monkeypatch, {
            "image-gen": _make_manifest(
                "image-gen", assignment_mode="explicit", has_instances=True,
            ),
            "schedules-mcp": _make_manifest("schedules-mcp", assignment_mode="auto"),
        })
        _make_instance("image-gen", "primary", agents=["alice"])
        # Manager hasn't enabled either yet
        visible = mcp_registry.get_visible_mcps_for_agent("alice")
        names = sorted(m.name for m in visible)
        assert names == ["image-gen", "schedules-mcp"]

    def test_get_visible_mcps_excludes_unauthorized_explicit(self, temp_db, monkeypatch):
        """Explicit MCP NOT authorized for this agent is hidden."""
        from services.mcp import mcp_registry
        _patch_manifests(monkeypatch, {
            "image-gen": _make_manifest(
                "image-gen", assignment_mode="explicit", has_instances=True,
            ),
            "schedules-mcp": _make_manifest("schedules-mcp", assignment_mode="auto"),
        })
        _make_instance("image-gen", "primary", agents=["bob"])
        visible = mcp_registry.get_visible_mcps_for_agent("alice")
        assert [m.name for m in visible] == ["schedules-mcp"]

    def test_assigned_to_all_makes_visible(self, temp_db, monkeypatch):
        """assigned_to_all=True flips visibility for every agent."""
        from services.mcp import mcp_registry
        _patch_manifests(monkeypatch, {
            "image-gen": _make_manifest(
                "image-gen", assignment_mode="explicit", has_instances=True,
            ),
        })
        _make_instance("image-gen", "shared", agents=[], assigned_to_all=True)
        # Brand-new agent that wasn't anywhere when admin set the flag
        for agent in ("alice", "bob", "newcomer"):
            visible = mcp_registry.get_visible_mcps_for_agent(agent)
            assert [m.name for m in visible] == ["image-gen"]


# ---------------------------------------------------------------------------
# API layer — endpoint shape and validation
# ---------------------------------------------------------------------------


class TestApiEndpoint:
    @pytest.fixture
    def client(self, temp_db, monkeypatch):
        """Bring up the FastAPI app with an admin user injected."""
        from fastapi import Request
        from fastapi.testclient import TestClient
        from auth.providers import UserContext, get_current_user

        admin_ctx = UserContext(
            sub="user-admin", email="admin@test.com", name="Admin",
            role="admin", is_api_key=False,
        )

        # Match the signature of get_current_user so FastAPI doesn't treat
        # *args/**kwargs as required query params.
        async def _get_admin(request: Request) -> UserContext:
            return admin_ctx

        from app import app
        app.dependency_overrides[get_current_user] = _get_admin

        try:
            yield TestClient(app)
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    def test_get_returns_unified_shape(self, client, monkeypatch):
        """GET /v1/agents/{name}/mcps returns {mcps: [...]} with visibility info."""
        _seed_agent("alice-agent")
        _patch_manifests(monkeypatch, {
            "schedules-mcp": _make_manifest("schedules-mcp", assignment_mode="auto", category="core"),
            "image-gen": _make_manifest(
                "image-gen", assignment_mode="explicit", has_instances=True,
            ),
        })
        _make_instance("image-gen", "primary", agents=["alice-agent"])

        r = client.get("/v1/agents/alice-agent/mcps")
        assert r.status_code == 200
        payload = r.json()
        assert "mcps" in payload
        names_to_meta = {m["name"]: m for m in payload["mcps"]}
        assert names_to_meta["schedules-mcp"]["authorized_by"] == "auto"
        assert names_to_meta["schedules-mcp"]["enabled"] is False  # manager hasn't enabled
        assert names_to_meta["image-gen"]["authorized_by"] == "admin"
        assert names_to_meta["image-gen"]["enabled"] is False
        # Service-account capability is surfaced per row (False when the
        # manifest declares none) so the UI never probes non-capable MCPs.
        assert names_to_meta["schedules-mcp"]["has_service_account"] is False

    def test_get_reflects_enabled_state(self, client, monkeypatch):
        """GET reflects manager's prior toggle state."""
        from storage import mcp_store
        _seed_agent("alice-agent")
        _patch_manifests(monkeypatch, {
            "schedules-mcp": _make_manifest("schedules-mcp", assignment_mode="auto"),
        })
        mcp_store.add_agent_mcp("alice-agent", "schedules-mcp")

        payload = client.get("/v1/agents/alice-agent/mcps").json()
        assert payload["mcps"][0]["name"] == "schedules-mcp"
        assert payload["mcps"][0]["enabled"] is True

    def test_put_validates_visibility(self, client, monkeypatch):
        """PUT with non-visible MCP → 400 with not_visible list."""
        _seed_agent("alice-agent")
        _patch_manifests(monkeypatch, {
            "image-gen": _make_manifest(
                "image-gen", assignment_mode="explicit", has_instances=True,
            ),
        })
        # No instance authorizes alice-agent

        r = client.put("/v1/agents/alice-agent/mcps", json={"mcps": ["image-gen"]})
        assert r.status_code == 400
        body = r.json()
        # FastAPI wraps detail
        assert body["detail"]["error"] == "MCPs not visible to this agent"
        assert body["detail"]["not_visible"] == ["image-gen"]

    def test_put_succeeds_for_admin_authorized(self, client, monkeypatch):
        """PUT enabling an admin-authorized explicit MCP succeeds."""
        from storage import mcp_store
        _seed_agent("alice-agent")
        _patch_manifests(monkeypatch, {
            "image-gen": _make_manifest(
                "image-gen", assignment_mode="explicit", has_instances=True,
            ),
        })
        _make_instance("image-gen", "primary", agents=["alice-agent"])

        r = client.put("/v1/agents/alice-agent/mcps", json={"mcps": ["image-gen"]})
        assert r.status_code == 200
        assert "image-gen" in mcp_store.get_manager_enabled_mcps("alice-agent")

    def test_put_keeps_skills_for_disabled_mcp(self, client, monkeypatch):
        """Disabling an MCP doesn't delete its agent_skills rows (preserves manager intent)."""
        from services.mcp.mcp_registry import SkillDef
        from storage import mcp_store
        _seed_agent("alice-agent")

        manifest = _make_manifest("schedules-mcp", assignment_mode="auto")
        manifest.skills = [SkillDef(id="schedules-mcp/usage", file="skills/usage.md",
                                    description="", default_exclude_from=[])]
        _patch_manifests(monkeypatch, {"schedules-mcp": manifest})

        # Enable, customize skill, then disable
        client.put("/v1/agents/alice-agent/mcps", json={"mcps": ["schedules-mcp"]})
        mcp_store.set_agent_skill(
            "alice-agent", "schedules-mcp/usage", enabled=False, exclude_from=["phone"],
        )
        client.put("/v1/agents/alice-agent/mcps", json={"mcps": []})

        # Skill row survives so re-enable restores manager's customization
        skills = mcp_store.get_agent_skills("alice-agent")
        skill = next(s for s in skills if s["skill_id"] == "schedules-mcp/usage")
        assert skill["enabled"] is False
        assert skill["exclude_from"] == ["phone"]


# ---------------------------------------------------------------------------
# Integration test — the original bug
# ---------------------------------------------------------------------------


class TestOriginalBugIntegration:
    """End-to-end: admin-assigned explicit MCP must be visible to the manager."""

    def test_full_round_trip(self, temp_db, monkeypatch):
        """
        Steps:
          1. Admin creates an instance for image-gen-mcp authorizing alice.
          2. GET /v1/agents/alice/mcps shows image-gen with enabled=False, authorized_by=admin.
          3. Manager PUTs to enable image-gen → 200.
          4. GET shows enabled=True.
          5. mcp_registry.get_agent_mcps('alice') returns the manifest.
          6. Admin revokes (empties agents) → runtime drops it, but agent_mcps row persists.
          7. Admin re-authorizes → manifest comes back at runtime, manager's enable is preserved.
        """
        from fastapi.testclient import TestClient
        from auth.providers import UserContext, get_current_user
        from services.mcp import mcp_registry
        from storage import mcp_store

        _seed_agent("alice-agent")
        _patch_manifests(monkeypatch, {
            "image-gen": _make_manifest(
                "image-gen", assignment_mode="explicit", has_instances=True,
            ),
        })

        from fastapi import Request
        admin_ctx = UserContext(
            sub="user-admin", email="a@x", name="A",
            role="admin", is_api_key=False,
        )
        async def _admin(request: Request) -> UserContext:
            return admin_ctx
        from app import app
        app.dependency_overrides[get_current_user] = _admin
        try:
            client = TestClient(app)

            # Admin creates instance authorizing alice-agent
            _make_instance("image-gen", "primary", agents=["alice-agent"])

            # GET shows it as visible-but-disabled
            payload = client.get("/v1/agents/alice-agent/mcps").json()
            row = next(m for m in payload["mcps"] if m["name"] == "image-gen")
            assert row["authorized_by"] == "admin"
            assert row["enabled"] is False

            # & 4: manager enables it
            r = client.put("/v1/agents/alice-agent/mcps",
                           json={"mcps": ["image-gen"]})
            assert r.status_code == 200
            payload = client.get("/v1/agents/alice-agent/mcps").json()
            row = next(m for m in payload["mcps"] if m["name"] == "image-gen")
            assert row["enabled"] is True

            # Registry returns it for runtime
            runtime = mcp_registry.get_agent_mcps("alice-agent")
            assert [m.name for m in runtime] == ["image-gen"]

            # Admin revokes
            mcp_store.upsert_mcp_instance("image-gen", {
                "instance_name": "primary",
                "field_values": {"API_KEY": "k"},
                "agents": [],
                "assigned_to_all": False,
            })
            assert mcp_registry.get_agent_mcps("alice-agent") == []
            # agent_mcps row preserved
            assert "image-gen" in mcp_store.get_manager_enabled_mcps("alice-agent")
            # Tab no longer shows the row
            payload = client.get("/v1/agents/alice-agent/mcps").json()
            names = [m["name"] for m in payload["mcps"]]
            assert "image-gen" not in names

            # Admin re-authorizes
            mcp_store.upsert_mcp_instance("image-gen", {
                "instance_name": "primary",
                "field_values": {"API_KEY": "k"},
                "agents": ["alice-agent"],
                "assigned_to_all": False,
            })
            runtime = mcp_registry.get_agent_mcps("alice-agent")
            assert [m.name for m in runtime] == ["image-gen"]
            payload = client.get("/v1/agents/alice-agent/mcps").json()
            row = next(m for m in payload["mcps"] if m["name"] == "image-gen")
            assert row["enabled"] is True  # manager intent preserved
        finally:
            app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Migration test
# ---------------------------------------------------------------------------


class TestMigration:
    def test_assigned_to_all_default_false(self, temp_db):
        """New rows default to assigned_to_all=False; column is present after migration."""
        from storage import mcp_store
        iid = _make_instance("image-gen", "primary", agents=["alice"])
        instances = mcp_store.get_mcp_instances("image-gen")
        match = next(i for i in instances if i["id"] == iid)
        assert match["assigned_to_all"] is False

    def test_run_migrations_idempotent(self, temp_db):
        """Running run_migrations twice doesn't raise."""
        from storage import schema
        from storage.pg import get_conn
        with get_conn() as conn:
            schema.run_migrations(conn)
            schema.run_migrations(conn)
            conn.commit()
