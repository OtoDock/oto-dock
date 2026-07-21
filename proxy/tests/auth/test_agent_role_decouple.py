"""Platform role and per-agent role are independent axes.

The platform role (admin/creator/member) gates platform-level actions only:
members cannot create agents or reach admin surfaces. Per-agent roles
(manager/editor/viewer) are assignable freely regardless of platform role —
a member can be manager of a specific agent an admin assigns them. The one
remaining platform↔agent coupling is admin-only agents (admins only).

Run: cd proxy && venv/bin/pytest tests/auth/test_agent_role_decouple.py -v
"""

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.providers import UserContext, get_current_user
from storage import agent_store
from storage import database as db

client = TestClient(app)

_MEMBER_SUB = "local:friend"


def _login_as(role: str, sub: str = "local:admin", agent_roles: dict | None = None):
    async def _user():
        return UserContext(
            sub=sub, email=f"{role}@t.com", name=role.title(), role=role,
            agent_roles=agent_roles or {},
        )
    app.dependency_overrides[get_current_user] = _user


@pytest.fixture(autouse=True)
def _fixtures():
    db.upsert_user(_MEMBER_SUB, "friend@t.com", "Friend", "member")
    agent_store.create_agent("helper", "Helper")
    _login_as("admin")
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _assign(agents: list[str], roles: dict[str, str]):
    return client.put(
        f"/v1/admin/users/{_MEMBER_SUB}/agents",
        json={"agents": agents, "agent_roles": roles},
    )


def test_member_can_be_assigned_any_agent_role():
    resp = _assign(["helper"], {"helper": "manager"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_roles"] == {"helper": "manager"}
    assert db.get_user_agent_roles(_MEMBER_SUB) == {"helper": "manager"}

    resp = _assign(["helper"], {"helper": "editor"})
    assert resp.status_code == 200
    assert db.get_user_agent_roles(_MEMBER_SUB) == {"helper": "editor"}


def test_member_agent_role_resolves_in_session_context():
    _assign(["helper"], {"helper": "manager"})
    member = UserContext(
        sub=_MEMBER_SUB, email="friend@t.com", name="Friend", role="member",
        agent_roles=db.get_user_agent_roles(_MEMBER_SUB),
    )
    assert member.get_agent_role("helper") == "manager"
    assert member.can_manage_agent("helper")
    # Unassigned agents still default to viewer.
    assert member.get_agent_role("other") == "viewer"


def test_member_still_cannot_create_agents():
    _login_as("member", sub=_MEMBER_SUB, agent_roles={"helper": "manager"})
    resp = client.post("/v1/agents", json={"display_name": "Nope"})
    assert resp.status_code == 403


def test_admin_only_agent_still_blocked_for_member():
    agent_store.create_agent("ops", "Ops", admin_only=True)
    resp = _assign(["ops"], {"ops": "viewer"})
    assert resp.status_code == 403
    assert "admin role" in resp.json()["detail"]


def test_invalid_agent_role_rejected():
    resp = _assign(["helper"], {"helper": "owner"})
    assert resp.status_code == 400
