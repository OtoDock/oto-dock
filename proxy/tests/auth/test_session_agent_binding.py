"""A session token acts on the agent it was started for.

The session JWT in an agent's process carries its owner's roles on every
agent, so the per-agent routes that change an agent's behaviour bind the
token to its own agent: the checks, the agent's PATCH and the knowledge
libraries refuse a token minted for another agent, whatever its owner may
do there from the dashboard. The deliberate cross-agent reach (delegation,
meetings, ``list_sessions`` and ``peek_session``) is untouched, and the
agent delete and default-for-new-users routes are a person's decision.

Run: cd proxy && venv/bin/pytest tests/auth/test_session_agent_binding.py -v
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app import app
from auth.providers import create_session_jwt
from tests.conftest import live_session_token
from storage import database as task_store
from storage.agents import agent_store

client = TestClient(app)

A = "bind-proj-a"
B = "bind-proj-b"
MGR = "user-viewer"     # a platform member who manages both agents
ADMIN = "user-admin"


@pytest.fixture(autouse=True)
def _world(temp_db):
    import config
    for slug in (A, B):
        agent_store.create_agent(slug, slug, created_by=ADMIN)
        for d in ("workspace", "config", "knowledge"):
            (config.AGENTS_DIR / slug / d).mkdir(parents=True, exist_ok=True)
    task_store.set_user_agents(MGR, [A, B], ADMIN, agent_roles={A: "manager", B: "manager"})


def _token(agent: str, sub: str = MGR) -> dict:
    return {"Authorization": f"Bearer {live_session_token(str(uuid.uuid4()), agent, sub)}"}


def _cookie(sub: str = MGR, role: str = "member") -> dict:
    return {"Cookie": f"session={create_session_jwt(sub, f'{sub}@t.com', sub, role)}"}


_CHECK = {"doc": {"description": "d", "kind": "judge", "rubric": "r"}, "script": None}

# (method, path template, json body) on routes bound to the token's agent.
_BOUND = [
    ("GET", "/v1/agents/{a}/checks", None),
    ("PUT", "/v1/agents/{a}/checks/c1", _CHECK),
    ("DELETE", "/v1/agents/{a}/checks/c1", None),
    ("PUT", "/v1/agents/{a}/user-checks/c1", _CHECK),
    ("DELETE", "/v1/agents/{a}/user-checks/c1", None),
    ("GET", "/v1/agents/{a}/check-verdicts", None),
    ("GET", "/v1/agents/{a}/check-settings", None),
    ("PATCH", "/v1/agents/{a}/check-settings", {"daily_cap_usd": 1}),
    ("PATCH", "/v1/agents/{a}", {"display_name": "Renamed"}),
    ("GET", "/v1/agents/{a}/knowledge-attachments", None),
    ("PUT", "/v1/agents/{a}/knowledge-library", {"subdirs": []}),
    ("PUT", "/v1/agents/{a}/knowledge-attachments", {"source_agent": A, "subdirs": []}),
    ("DELETE", "/v1/agents/{a}/knowledge-attachments/" + A, None),
]


@pytest.mark.parametrize("method, path, body", _BOUND, ids=[f"{m} {p}" for m, p, _ in _BOUND])
def test_a_token_for_one_agent_is_refused_on_anothers_route(method, path, body):
    r = client.request(method, path.format(a=B), json=body, headers=_token(A))
    assert r.status_code == 403
    assert "started for" in r.json()["detail"]


@pytest.mark.parametrize("method, path, body", _BOUND, ids=[f"{m} {p}" for m, p, _ in _BOUND])
def test_the_same_route_is_not_refused_by_the_binding_for_its_own_agent(method, path, body):
    r = client.request(method, path.format(a=A), json=body, headers=_token(A))
    assert not (r.status_code == 403 and "started for" in r.json().get("detail", ""))


def test_a_no_user_token_is_bound_too():
    r = client.get(f"/v1/agents/{B}/checks", headers=_token(A, sub=""))
    assert r.status_code == 403


def test_a_cookie_manager_keeps_every_agent():
    assert client.get(f"/v1/agents/{B}/checks", headers=_cookie()).status_code == 200
    assert client.patch(f"/v1/agents/{B}", json={"display_name": "B2"},
                        headers=_cookie()).status_code == 200


def test_the_cross_agent_reads_still_work_for_the_same_token():
    # The token refused on B's checks still lists sessions across agents
    # (delegation-mcp's list_sessions), as the user it acts for may.
    from storage.mcp import mcp_store
    mcp_store.set_mcp_enabled("delegation-mcp", True)
    headers = _token(A)
    assert client.get(f"/v1/agents/{B}/checks", headers=headers).status_code == 403
    r = client.get("/v1/delegation/sessions", params={"agent": B}, headers=headers)
    assert r.status_code == 200


@pytest.mark.parametrize("method, path", [
    ("DELETE", "/v1/agents/{a}"),
    ("PUT", "/v1/admin/agents/{a}/default-for-new-users"),
])
def test_an_admin_owned_session_cannot_delete_or_default_an_agent(method, path):
    r = client.request(method, path.format(a=A), json={"enabled": True, "confirm_slug": A},
                       headers=_token(A, sub=ADMIN))
    assert r.status_code == 403
    assert agent_store.agent_exists(A)


@pytest.mark.parametrize("sub", ["user-manager", ADMIN])   # a creator, an admin
def test_a_creators_session_cannot_create_an_agent(sub):
    r = client.post("/v1/agents", json={"display_name": "From a prompt", "slug": "from-a-prompt"},
                    headers=_token(A, sub=sub))
    assert r.status_code == 403
    assert not agent_store.agent_exists("from-a-prompt")


def test_a_persons_session_token_acts_at_their_role_on_the_files_routes():
    """A session token that names a person moves, copies, zips and lists at
    that person's tier, never the admin tier: a contributor's session cannot
    zip the config folder or move the persona out of it. The agent's own
    session keeps the admin tier."""
    import config
    contrib = "user-contrib"
    task_store.upsert_user(contrib, "contrib@t.com", "Contrib", "member")
    task_store.set_user_agents(contrib, [A], ADMIN, agent_roles={A: "contributor"})
    (config.AGENTS_DIR / A / "config" / "agent.md").write_text("persona")
    h = _token(A, contrib)
    r = client.post(f"/v1/agents/{A}/move", json={"src_paths": ["config/agent.md"], "dest_dir": "workspace"}, headers=h)
    assert r.status_code == 403, r.text
    r = client.post(f"/v1/agents/{A}/copy", json={"src_paths": ["config/agent.md"], "dest_dir": "workspace"}, headers=h)
    assert r.status_code == 403, r.text
    assert client.post(f"/v1/agents/{A}/zip", json={"paths": ["config"]}, headers=h).status_code == 403
    assert (config.AGENTS_DIR / A / "config" / "agent.md").read_text() == "persona"
    assert client.post(f"/v1/agents/{A}/zip", json={"paths": ["config"]}, headers=_token(A, "")).status_code == 200
