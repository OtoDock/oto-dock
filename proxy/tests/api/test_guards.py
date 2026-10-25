"""The shared route guards (``api/_guards.py``) and the routes that moved
onto them: no principal answers 401 (fail closed), a signed-in member who
lacks the role answers 403 with the route's own sentence, the role passes.
Each route that moved onto a shared guard is pinned here."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api._guards import require_creator_or_admin, require_manager
from app import app
from auth import roles
from auth.providers import UserContext, get_current_user

AGENT = "guard-agent"


def _person(role: str = roles.MEMBER, agent_role: str | None = roles.VIEWER,
            *, bearer: bool = False) -> UserContext:
    return UserContext(
        sub="user-guard", email="guard@test", name="Guard", role=role,
        agents=[AGENT] if agent_role else [],
        agent_roles={AGENT: agent_role} if agent_role else {},
        is_api_key=bearer, session_id="s-guard" if bearer else "", agent=AGENT if bearer else "",
    )


@pytest.mark.parametrize("guard", [
    lambda u: require_manager(u, AGENT, detail="no"),
    lambda u: require_creator_or_admin(u, detail="no"),
])
def test_no_principal_is_401_and_a_member_without_the_role_403_with_the_detail(guard):
    with pytest.raises(HTTPException) as e:
        guard(None)
    assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        guard(_person())
    assert e.value.status_code == 403 and e.value.detail == "no"


def test_the_role_passes():
    manager = _person(agent_role=roles.MANAGER)
    assert require_manager(manager, AGENT, detail="no") is manager
    admin = _person(role=roles.ADMIN, agent_role=None)
    assert require_manager(admin, AGENT, detail="no") is admin
    creator = _person(role=roles.CREATOR)
    assert require_creator_or_admin(creator, detail="no") is creator
    # A session token resolves to its person's role here.
    assert require_creator_or_admin(_person(role=roles.CREATOR, bearer=True), detail="no")
    with pytest.raises(HTTPException):
        require_manager(_person(agent_role=roles.EDITOR), AGENT, detail="no")


@pytest.fixture
def as_principal():
    client = TestClient(app)

    def use(principal: UserContext) -> TestClient:
        app.dependency_overrides[get_current_user] = lambda: principal
        return client
    yield use
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize(("method", "path", "detail"), [
    ("GET", f"/v1/agents/{AGENT}/skills", "Manager access required for this agent"),
    ("GET", f"/v1/agents/{AGENT}/api-keys",
     f"Agent API keys require manager or admin role for '{AGENT}'"),
    ("GET", "/v1/community/mcps", "Admin or creator only"),
    ("GET", "/v1/agents/local-template/building-blocks",
     "Creating agents requires a platform creator or admin account"),
    ("GET", "/v1/admin/execution-layers", "Admin access required"),
])
def test_a_signed_in_member_gets_each_routes_own_403_and_sentence(
        temp_db, as_principal, method, path, detail):
    r = as_principal(_person()).request(method, path)
    assert r.status_code == 403 and r.json()["detail"] == detail


def test_the_dashboard_only_routes_refuse_a_bearer(temp_db, as_principal):
    """The budget caps (``require_human``)
    and a machine owner's settings refuse the session token a subprocess
    holds, whatever its person's role."""
    client = as_principal(_person(role=roles.ADMIN, bearer=True))
    for method, path, body in (
            ("PUT", "/v1/usage/me/pool-cap", {"week_pct": 50}),
            ("PUT", "/v1/users/me/remote-machines/m-1/allow-full-fs", {"enabled": True})):
        r = client.request(method, path, json=body)
        assert r.status_code == 403, path
        assert r.json()["detail"] == "User authentication required (not API key)", path
