"""The routes of an agent's set of a person's accounts
(``api/mcp/account_sets.py``): the PUT that adds an account or changes its
reach, the DELETE of one account, the PUT of the default, their gates and
refusals; the 1.9.1 agent-binding routes on a set of two; the integrations
list with an agent under two accounts; an account added to the agent's own
set that starts from the reach of the person's row of that account.

Uses the GitHub rig of the person's own reach
(``tests/auth/test_github_reach_binding.py``): the person holds the
GitHub-App account ``alice`` (installation 10, reaching ``alice/repo``), is
assigned to the agent, and GitHub is enabled on it. The rig mounts the
credentials router on an app of its own, with its own user override.
"""

from __future__ import annotations

import pytest

from services.mcp import mcp_registry
from services.oauth import credential_resolver, github_apps
from storage.identity import credential_store
from tests.auth.test_github_app_connect import (  # noqa: F401 (fixtures)
    MCP,
    _connect,
    pem,
    person,
    rig,
)
from tests.auth.test_github_app_service_binding import (  # noqa: F401 (fixtures)
    AGENT,
    bound,
)
from tests.auth.test_github_reach_binding import (  # noqa: F401 (fixtures)
    _add_svc,
    _own_put,
    mine,
    narrow,
)

_ROUTE = f"/v1/users/me/integrations/{MCP}/agent-accounts"


@pytest.fixture
def sets(mine):  # noqa: F811
    """The person holds a second account, ``work``."""
    _connect(mine, label="work")
    yield mine


def _add(rig, **body):  # noqa: F811
    return rig.client.put(_ROUTE, json={"agent_name": AGENT, "account_label": "alice", **body})


def _remove(rig, label, agent=AGENT):  # noqa: F811
    return rig.client.delete(f"{_ROUTE}/{agent}", params={"account_label": label})


def _default(rig, label, agent=AGENT):  # noqa: F811
    return rig.client.put(f"{_ROUTE}/{agent}/default", json={"account_label": label})


def _set_of(rig):  # noqa: F811
    return [(r.account_label, r.repositories, r.read_only, r.is_default)
            for r in credential_store.list_agent_account_set(rig.sub, MCP, AGENT)]


def _not_held(label: str) -> str:
    return f"Account '{label}' is not one of the {MCP} accounts of agent '{AGENT}'."


class TestThePut:
    def test_the_first_is_the_default_a_second_is_not_and_make_default_moves_it(self, sets):
        resp = _add(sets, repositories=["alice/repo"])
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "repositories": ["alice/repo"], "read_only": False,
                               "is_default": True}
        resp = _add(sets, account_label="work", read_only=True)
        assert resp.json() == {"status": "ok", "repositories": [], "read_only": True, "is_default": False}
        resp = _add(sets, account_label="work", make_default=True)
        assert resp.json() == {"status": "ok", "repositories": [], "read_only": True, "is_default": True}
        assert _set_of(sets) == [("work", (), True, True), ("alice", ("alice/repo",), False, False)]

    def test_the_reach_is_judged_on_the_named_accounts_own_row(self, sets):
        # The default's row names a repository work does not reach.
        assert credential_store.add_account_to_agent(
            sets.sub, MCP, AGENT, "alice", repositories=["alice/repo", "orgb/repo"])
        # A second account starts whole: the default's list is never carried
        # onto it, nor looked up on it.
        resp = _add(sets, account_label="work")
        assert resp.status_code == 200, resp.text
        assert resp.json()["repositories"] == [] and resp.json()["read_only"] is False
        github_apps._listed.clear()
        sets.fake.routes[("GET", "/user/installations/10/repositories")] = (500, {"message": "down"})
        # The names alice's own row holds are not looked up again.
        assert _add(sets, repositories=["alice/repo", "ORGB/repo"], read_only=True).status_code == 200
        # work's row holds none of these names: they are looked up on it.
        resp = _add(sets, account_label="work", repositories=["alice/repo", "bob/new"])
        assert resp.status_code == 503 and "could not be reached" in resp.json()["detail"]
        assert _set_of(sets) == [("alice", ("alice/repo", "ORGB/repo"), True, True), ("work", (), False, False)]

    def test_what_cannot_hold_is_refused_in_words(self, sets, monkeypatch):
        resp = _add(sets, repositories=["alice/other"])
        assert resp.status_code == 400
        assert resp.json()["detail"] == "alice/other is not among the repositories this account reaches."
        resp = _add(sets, repositories=["not a name"])
        assert resp.status_code == 400 and "Write it as owner/name." in resp.json()["detail"]
        from api.mcp import credentials as credentials_api
        monkeypatch.setattr(credentials_api, "_reach_supported", lambda name: False)
        resp = _add(sets, read_only=True)
        assert resp.status_code == 400 and "Only GitHub" in resp.json()["detail"]
        assert _set_of(sets) == []

    def test_the_gates(self, sets, monkeypatch):
        resp = _add(sets, agent_name="not-mine")
        assert resp.status_code == 403 and "is not in your assigned agents" in resp.json()["detail"]
        resp = _add(sets, account_label="ghost")
        assert resp.status_code == 404 and resp.json()["detail"] == f"Account 'ghost' not found for {MCP}"
        monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements", lambda agent, placement=None: [])
        resp = _add(sets)
        assert resp.status_code == 400 and resp.json()["detail"] == (
            f"MCP {MCP!r} is not enabled on agent {AGENT!r}")
        assert _set_of(sets) == []


class TestTheDeleteAndTheDefault:
    def test_one_account_goes_and_the_oldest_left_becomes_the_default(self, sets):
        assert _add(sets, repositories=["alice/repo"]).status_code == 200
        assert _add(sets, account_label="work").status_code == 200
        resp = _remove(sets, "alice")
        assert resp.status_code == 200 and resp.json() == {"status": "ok"}
        assert _set_of(sets) == [("work", (), False, True)]
        resp = _remove(sets, "alice")
        assert resp.status_code == 404 and resp.json()["detail"] == _not_held("alice")
        assert _remove(sets, "work", agent="not-mine").status_code == 403
        assert _remove(sets, "work").status_code == 200
        assert _set_of(sets) == []
        # An empty set: the person's own default account, with no reach.
        assert credential_resolver.pick_account(MCP, AGENT, user_sub=sets.sub).label == "alice"
        assert credential_resolver.binding_reach(MCP, AGENT, user_sub=sets.sub, task_scope="user") is None

    def test_the_default_moves_within_the_set_only(self, sets):
        assert _add(sets).status_code == 200
        assert _add(sets, account_label="work").status_code == 200
        resp = _default(sets, "work")
        assert resp.status_code == 200 and resp.json() == {"status": "ok"}
        assert _set_of(sets) == [("work", (), False, True), ("alice", (), False, False)]
        assert credential_resolver.pick_account(MCP, AGENT, user_sub=sets.sub).label == "work"
        resp = _default(sets, "ghost")
        assert resp.status_code == 404 and resp.json()["detail"] == _not_held("ghost")
        assert _default(sets, "alice", agent="not-mine").status_code == 403
        assert _remove(sets, "alice").status_code == 200
        assert _default(sets, "alice").status_code == 404
        assert _set_of(sets) == [("work", (), False, True)]


class TestTheKeptRoutes:
    """An open 1.9.1 page: its PUT makes the set one account, its DELETE
    empties the set."""

    def test_the_put_makes_the_set_that_one_account(self, sets):
        assert _add(sets, repositories=["alice/repo"]).status_code == 200
        assert _add(sets, account_label="work", read_only=True).status_code == 200
        resp = _own_put(sets, account_label="work")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "repositories": [], "read_only": True}
        assert _set_of(sets) == [("work", (), True, True)]

    def test_the_delete_empties_the_set(self, sets):
        assert _add(sets).status_code == 200
        assert _add(sets, account_label="work", make_default=True).status_code == 200
        assert sets.client.delete(f"/v1/users/me/integrations/{MCP}/agent-binding/{AGENT}").status_code == 200
        assert _set_of(sets) == []


class TestTheList:
    def test_an_agent_under_two_accounts(self, sets):
        assert _add(sets, repositories=["alice/repo"]).status_code == 200
        assert _add(sets, account_label="work", read_only=True, make_default=True).status_code == 200
        sets.holder["role"] = "creator"             # the list follows the agents the person uses
        entry = next(e for e in sets.client.get("/v1/users/me/integrations").json() if e["mcp_name"] == MCP)
        accounts = {a["account_label"]: a for a in entry["accounts"]}
        assert accounts["alice"]["agent_overrides"] == [AGENT]
        assert accounts["alice"]["agent_defaults"] == []
        assert accounts["alice"]["agent_reaches"] == [
            {"agent": AGENT, "repositories": ["alice/repo"], "read_only": False}]
        assert accounts["work"]["agent_overrides"] == [AGENT]
        assert accounts["work"]["agent_defaults"] == [AGENT]
        assert accounts["work"]["agent_reaches"] == [{"agent": AGENT, "repositories": [], "read_only": True}]
        assert entry["candidate_agents"] == [AGENT]


class TestTheServiceCopy:
    def test_an_agents_own_account_starts_from_the_named_accounts_own_reach(self, sets):
        """The account a manager adds to the agent's own set is not the
        person's default for it: its own row's reach is the one copied, and
        the default's is never, onto it or onto a second account beside it."""
        assert _add(sets, account_label="work").status_code == 200
        assert _add(sets, repositories=["alice/repo"], read_only=True).status_code == 200
        resp = _add_svc(sets)
        assert resp.status_code == 200, resp.text
        assert resp.json()["reach_copied"] is True
        alice = next(a for a in resp.json()["accounts"] if a["label"] == "alice")
        assert alice["repositories"] == ["alice/repo"] and alice["read_only"] is True
        resp = _add_svc(sets, account_label="work")
        assert resp.status_code == 200 and resp.json()["reach_copied"] is False
        work = next(a for a in resp.json()["accounts"] if a["label"] == "work")
        assert work["repositories"] == [] and work["read_only"] is False


@pytest.mark.parametrize("method,path,body", [
    ("PUT", _ROUTE, {"agent_name": "a", "account_label": "b"}),
    ("DELETE", f"{_ROUTE}/a?account_label=b", None),
    ("PUT", f"{_ROUTE}/a/default", {"account_label": "b"}),
])
def test_the_routes_reach_the_app_and_refuse_a_call_with_no_sign_in(method, path, body):
    from fastapi.testclient import TestClient

    from app import app
    resp = TestClient(app, raise_server_exceptions=False).request(method, path, json=body)
    assert resp.status_code == 401, (method, path, resp.status_code)
