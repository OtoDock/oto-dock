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
from storage import database as task_store
from storage.agents import agent_store
from tests.conftest import live_session_token

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
    ("PATCH", "/v1/agents/{a}", {"display_name": "Renamed"}),
    ("GET", "/v1/agents/{a}/knowledge-attachments", None),
    ("PUT", "/v1/agents/{a}/knowledge-library", {"subdirs": []}),
    ("PUT", "/v1/agents/{a}/knowledge-attachments", {"source_agent": A, "subdirs": []}),
    ("DELETE", "/v1/agents/{a}/knowledge-attachments/" + A, None),
]


@pytest.mark.parametrize("method, path, body", _BOUND, ids=[f"{m} {p}" for m, p, _ in _BOUND])
def test_a_token_for_one_agent_is_refused_on_anothers_route(method, path, body):
    """The session token's route list binds every per-agent row to the
    token's own agent first (``auth/external_endpoints.SESSION_ROUTES``);
    the route's own ``session_bound_to`` stands behind it."""
    from auth.external_endpoints import SESSION_ROUTE_DETAIL
    r = client.request(method, path.format(a=B), json=body, headers=_token(A))
    assert r.status_code == 403
    assert r.json()["detail"] == SESSION_ROUTE_DETAIL


@pytest.mark.parametrize("method, path, body", [
    ("GET", "/v1/agents/{a}/check-verdicts", None),
    ("GET", "/v1/agents/{a}/check-settings", None),
    ("PATCH", "/v1/agents/{a}/check-settings", {"daily_cap_usd": 1}),
])
def test_a_per_agent_route_no_tool_calls_is_off_the_list_for_the_tokens_own_agent_too(method, path, body):
    """These rows are a person's: the list refuses a session token on them
    before any binding, for the token's own agent as for another's."""
    from auth.external_endpoints import SESSION_ROUTE_DETAIL
    for agent in (A, B):
        r = client.request(method, path.format(a=agent), json=body, headers=_token(A))
        assert r.status_code == 403 and r.json()["detail"] == SESSION_ROUTE_DETAIL, agent


def test_the_routes_own_binding_still_refuses_another_agent():
    """Behind the list: the dependency itself, as a route carries it, with
    no middleware in front."""
    from fastapi import Depends, FastAPI

    from auth.providers import session_bound_to
    mini = FastAPI()

    @mini.get("/t/{a}", dependencies=[Depends(session_bound_to("a"))])
    async def _route(a: str) -> dict:
        return {"ok": a}

    bare = TestClient(mini)
    r = bare.get(f"/t/{B}", headers=_token(A))
    assert r.status_code == 403 and "started for" in r.json()["detail"]
    assert bare.get(f"/t/{A}", headers=_token(A)).json() == {"ok": A}


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


def test_a_session_token_reaches_the_files_api_for_the_tree_and_the_persona_alone(monkeypatch):
    """The files API's moves, copies, zips and reads are no tool's: a session
    token is refused them with the list's sentence at every role, the files
    stay where they are, and the two routes a tool calls (the tree, the
    persona file) answer the token's own agent."""
    import config
    from auth.external_endpoints import SESSION_ROUTE_DETAIL
    contrib = "user-contrib"
    task_store.upsert_user(contrib, "contrib@t.com", "Contrib", "member")
    task_store.set_user_agents(contrib, [A], ADMIN, agent_roles={A: "contributor"})
    (config.AGENTS_DIR / A / "config" / "agent.md").write_text("persona")
    for h in (_token(A, contrib), _token(A, ""), _token(A)):
        for method, path, body in (
            ("POST", f"/v1/agents/{A}/move", {"src_paths": ["config/agent.md"], "dest_dir": "workspace"}),
            ("POST", f"/v1/agents/{A}/copy", {"src_paths": ["config/agent.md"], "dest_dir": "workspace"}),
            ("POST", f"/v1/agents/{A}/zip", {"paths": ["config"]}),
            ("GET", f"/v1/agents/{A}/files/workspace/x.txt", None),
        ):
            r = client.request(method, path, json=body, headers=h)
            assert (r.status_code, r.json()["detail"]) == (403, SESSION_ROUTE_DETAIL), (method, path, r.text)
    assert (config.AGENTS_DIR / A / "config" / "agent.md").read_text() == "persona"
    h = _token(A)
    assert client.get(f"/v1/agents/{A}/files", headers=h).status_code != 403
    r = client.put(f"/v1/agents/{A}/files/config/agent.md", json={"content": "persona"}, headers=h)
    assert not (r.status_code == 403 and r.json().get("detail") == SESSION_ROUTE_DETAIL), r.text
