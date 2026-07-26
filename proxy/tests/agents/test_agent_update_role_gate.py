"""Per-agent manager vs platform role on agent-configuration endpoints.

The two role axes are independent: a platform member who MANAGES an agent may
configure it; a platform creator who is only a viewer of someone else's agent
may not. Regression tests for the incident where a platform-member +
agent-manager got 403 on every Configuration-tab save (PATCH /v1/agents/{name})
and on the memory master-flag read the tab performs on load.

Run: cd proxy && venv/bin/pytest tests/agents/test_agent_update_role_gate.py -v
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# Platform roles per conftest._seed_users
ADMIN = "user-admin"      # admin
CREATOR = "user-manager"  # creator
MEMBER = "user-viewer"    # member

SLUG = "cfg-demo"


@pytest.fixture
def client(temp_db):
    from app import app
    return TestClient(app)


@pytest.fixture
def agent(temp_db):
    from storage import agent_store
    agent_store.create_agent(SLUG, "Cfg Demo", created_by=ADMIN)
    return SLUG


def _cookie(sub: str, email: str, role: str) -> dict[str, str]:
    from auth.providers import create_session_jwt
    return {"Cookie": f"session={create_session_jwt(sub, email, 'T User', role)}"}


def _assign(sub: str, agent_role: str) -> None:
    from storage import database as task_store
    task_store.set_user_agents(sub, [SLUG], ADMIN, agent_roles={SLUG: agent_role})


class TestUpdateAgentRoleGate:
    def test_member_manager_can_update(self, client, agent):
        _assign(MEMBER, "manager")
        r = client.patch(
            f"/v1/agents/{SLUG}",
            json={"description": "updated by manager"},
            headers=_cookie(MEMBER, "viewer@test.com", "member"),
        )
        assert r.status_code == 200

    def test_member_viewer_denied(self, client, agent):
        _assign(MEMBER, "viewer")
        r = client.patch(
            f"/v1/agents/{SLUG}",
            json={"description": "nope"},
            headers=_cookie(MEMBER, "viewer@test.com", "member"),
        )
        assert r.status_code == 403

    def test_member_without_assignment_denied(self, client, agent):
        r = client.patch(
            f"/v1/agents/{SLUG}",
            json={"description": "nope"},
            headers=_cookie(MEMBER, "viewer@test.com", "member"),
        )
        assert r.status_code == 403

    def test_creator_viewer_of_foreign_agent_denied(self, client, agent):
        # The original reason update_agent checks the per-agent role: platform
        # creator-tier must not grant write on someone else's agent.
        _assign(CREATOR, "viewer")
        r = client.patch(
            f"/v1/agents/{SLUG}",
            json={"description": "nope"},
            headers=_cookie(CREATOR, "manager@test.com", "creator"),
        )
        assert r.status_code == 403

    def test_admin_without_assignment_can_update(self, client, agent):
        r = client.patch(
            f"/v1/agents/{SLUG}",
            json={"description": "updated by admin"},
            headers=_cookie(ADMIN, "admin@test.com", "admin"),
        )
        assert r.status_code == 200

    def test_member_manager_cannot_set_admin_only(self, client, agent):
        _assign(MEMBER, "manager")
        r = client.patch(
            f"/v1/agents/{SLUG}",
            json={"admin_only": True},
            headers=_cookie(MEMBER, "viewer@test.com", "member"),
        )
        assert r.status_code == 403


class TestAgentMemorySettingsRoleGate:
    def test_member_manager_reads_toggles_with_master(self, client, agent):
        _assign(MEMBER, "manager")
        r = client.get(
            f"/v1/internal/memory/agent-settings/{SLUG}",
            headers=_cookie(MEMBER, "viewer@test.com", "member"),
        )
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["user_memory_enabled"], bool)
        assert isinstance(body["master"]["user_memory_enabled"], bool)
        assert isinstance(body["master"]["agent_memory_enabled"], bool)

    def test_member_viewer_denied(self, client, agent):
        _assign(MEMBER, "viewer")
        r = client.get(
            f"/v1/internal/memory/agent-settings/{SLUG}",
            headers=_cookie(MEMBER, "viewer@test.com", "member"),
        )
        assert r.status_code == 403

    def test_platform_settings_stay_admin_only(self, client, agent):
        # Managers never need this endpoint anymore — it remains admin-only.
        _assign(MEMBER, "manager")
        r = client.get(
            "/v1/internal/memory/settings",
            headers=_cookie(MEMBER, "viewer@test.com", "member"),
        )
        assert r.status_code == 403
