"""An agent's GitHub reach on each of its own accounts: the set's add
stores a repository list and the read-only choice and refuses what cannot
hold, the reach route changes them on the caller's own row in place
(keeping what it does not name, never on a row taken out meanwhile or of
another way of acting), the picker lists what the account reaches, the
options show the reach and offer a hosted account as the account alone, an
installation row is narrowed by GitHub itself (one token file per reach,
re-minted the same way, never minted wide), an app step of a narrowed user
token gets nothing, the sessions are told once, and a new row starts from
the person's own reach for the agent.

Uses the service-binding rig (tests/auth/test_github_app_service_binding.py).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from auth import github_reach
from services.mcp import dynamic_context, mcp_registry
from services.oauth import (
    credential_resolver,
    github_app_lifecycle,
    github_apps,
    oauth_refresh_worker,
)
from storage.identity import (
    credential_store,
    oauth_app_kinds as kinds,
    service_account_set,
)
from storage.pg import get_conn
from tests.auth.test_github_app_connect import (  # noqa: F401 (fixtures)
    MCP,
    PROVIDER,
    _connect,
    pem,
    person,
    rig,
)
from tests.auth.test_github_app_service_binding import (  # noqa: F401 (fixtures)
    AGENT,
    _add_svc,
    _expires_at,
    _hosted,
    _mint_route,
    _mints,
    _remove_svc,
    bound,
)

_PERMISSIONS = {"contents": "write", "issues": "write", "metadata": "read", "workflows": "write"}


def _reach(rig, **body):  # noqa: F811
    """Change the agent's GitHub reach on the person's row (``alice`` unless
    named) through the set's reach route; the way the row acts is read from
    the stored row unless the body names it, as the page sends it."""
    label = body.pop("account_label", "alice")
    if "installation_id" not in body:
        row = credential_store.get_service_agent_binding(MCP, AGENT, account_label=label)
        body["installation_id"] = row.installation_id if row is not None else ""
    return rig.client.put(f"/v1/agents/{AGENT}/mcps/{MCP}/service-accounts/reach",
                          json={"account_label": label, **body})


def _account(resp, label="alice"):
    """The answer's account of that label."""
    return next(a for a in resp.json()["accounts"] if a["label"] == label)


def _repos(rig, **params):  # noqa: F811
    return rig.client.get(f"/v1/agents/{AGENT}/mcps/{MCP}/service-account-repositories",
                          params={"account_label": "alice", **params})


@pytest.fixture
def narrow(bound):  # noqa: F811
    """GitHub answers the installation's permissions and repositories."""
    bound.fake.routes[("GET", "/app/installations/10")] = (
        200, {"id": 10, "account": {"login": "alice"}, "permissions": _PERMISSIONS})
    github_apps._listed.clear()
    yield bound
    github_apps._listed.clear()


def _narrowed_file(reach: github_reach.Reach):
    return github_apps.installation_file(PROVIDER, 10, reach)


class TestTheAddAndTheReachRoute:
    def test_a_reach_is_stored_minted_narrowed_and_shown(self, narrow):
        resp = _add_svc(narrow, installation_id="10", repositories=["Alice/Repo"], read_only=True)
        assert resp.status_code == 200, resp.text
        assert _account(resp)["repositories"] == ["Alice/Repo"] and _account(resp)["read_only"] is True
        binding = credential_store.get_service_agent_binding(MCP, AGENT)
        assert binding.repositories == ("Alice/Repo",) and binding.read_only is True
        reach = github_reach.reach_of(["alice/repo"], True)
        path = _narrowed_file(reach)
        assert path.exists() and not github_apps.installation_file(PROVIDER, 10).exists()
        sent = json.loads(_mints(narrow)[-1].content)
        assert sent == {"repositories": ["repo"],
                        "permissions": {"contents": "read", "issues": "read", "metadata": "read"}}
        assert json.loads(path.read_text())["extra"]["reach"] == reach.to_dict()
        opts = narrow.client.get(f"/v1/agents/{AGENT}/mcps/{MCP}/service-account-options").json()
        assert opts["reach_supported"] is True
        cur = opts["accounts"][0]
        assert cur["repositories"] == ["Alice/Repo"] and cur["read_only"] is True and cur["problem"] == ""

    def test_a_label_another_lender_held_has_its_reach_looked_up(self, narrow, monkeypatch):
        """Another lender's row of the label goes between the route's read
        and the store's lock: the caller's account is written new, so its
        reach is looked up as a new row's, and a name it does not reach is
        refused with nothing written."""
        from api.mcp import service_account_set as set_api
        monkeypatch.setattr(set_api, "_row_of", lambda mcp, agent, label: credential_store.ServiceBinding(
            label, "someone-else"))
        resp = _add_svc(narrow, installation_id="10", repositories=["alice/other"])
        assert resp.status_code == 400
        assert resp.json()["detail"] == "alice/other is not among the repositories this account reaches."
        assert credential_store.get_service_agent_binding(MCP, AGENT) is None

    def test_a_name_the_account_does_not_reach_is_refused(self, narrow):
        resp = _add_svc(narrow, installation_id="10", repositories=["alice/other"])
        assert resp.status_code == 400
        assert resp.json()["detail"] == "alice/other is not among the repositories this account reaches."
        resp = _add_svc(narrow, installation_id="10", repositories=["not a name"])
        assert resp.status_code == 400 and "Write it as owner/name." in resp.json()["detail"]
        assert credential_store.get_service_agent_binding(MCP, AGENT) is None
        # The reach route refuses the same on a row the agent holds.
        assert _add_svc(narrow, installation_id="10", repositories=["alice/repo"]).status_code == 200
        resp = _reach(narrow, repositories=["alice/other"])
        assert resp.status_code == 400
        assert resp.json()["detail"] == "alice/other is not among the repositories this account reaches."
        assert credential_store.get_service_agent_binding(MCP, AGENT).repositories == ("alice/repo",)

    def test_a_narrowing_change_never_waits_on_github(self, narrow):
        assert _add_svc(narrow, installation_id="10", repositories=["alice/repo"]).status_code == 200
        github_apps._listed.clear()
        narrow.fake.routes[("GET", "/user/installations/10/repositories")] = (500, {"message": "down"})
        assert _reach(narrow, repositories=["alice/repo"], read_only=True).status_code == 200
        # The installation is on all of alice's repositories: a name of hers is taken.
        assert _reach(narrow, repositories=["alice/repo", "alice/new"]).status_code == 200
        resp = _reach(narrow, repositories=["alice/repo", "bob/new"])
        assert resp.status_code == 503 and "could not be reached" in resp.json()["detail"]

    def test_a_cut_listing_takes_a_name_on_its_account(self):
        listed = [{"id": 10, "account": "alice", "repositories": ["alice/a"], "truncated": True}]
        assert github_apps.unreachable_names(listed, ["alice/a", "alice/z", "bob/x"]) == ["bob/x"]

    def test_a_token_account_can_be_read_only_but_has_no_list(self, narrow):
        resp = narrow.client.post(f"/v1/oauth/{PROVIDER}/pat/save", json={
            "mcp_name": MCP, "token": "ghp_fake", "services": ["repos"], "account_label": "tok"})
        assert resp.status_code == 200, resp.text
        resp = _add_svc(narrow, account_label="tok", repositories=["alice/repo"])
        assert resp.status_code == 400 and "no list of repositories" in resp.json()["detail"]
        assert _add_svc(narrow, account_label="tok", read_only=True).status_code == 200
        assert credential_store.get_service_agent_binding(MCP, AGENT).read_only is True
        resp = _reach(narrow, account_label="tok", repositories=["alice/repo"])
        assert resp.status_code == 400 and "no list of repositories" in resp.json()["detail"]
        assert _reach(narrow, account_label="tok", read_only=False).status_code == 200
        assert credential_store.get_service_agent_binding(MCP, AGENT)[3:5] == ((), False)
        assert _repos(narrow, account_label="tok").json() == {"pickable": False, "installations": [],
                                                             "from_meta": False}

    def test_a_reach_only_for_github(self, narrow, monkeypatch):
        from api.mcp import credentials as credentials_api
        monkeypatch.setattr(credentials_api, "_reach_supported", lambda name: False)
        resp = _add_svc(narrow, read_only=True)
        assert resp.status_code == 400 and "Only GitHub" in resp.json()["detail"]
        assert _add_svc(narrow, repositories=[], read_only=False).status_code == 200
        resp = _reach(narrow, read_only=True)
        assert resp.status_code == 400 and "Only GitHub" in resp.json()["detail"]

    def test_a_reach_save_keeps_what_it_does_not_name(self, narrow):
        """A field the body leaves out keeps what the row holds, so a page
        that knows nothing of one never widens it; a page that names another
        way of acting writes nothing."""
        assert _add_svc(narrow, installation_id="10", repositories=["alice/repo"], read_only=True).status_code == 200
        assert _reach(narrow).status_code == 200
        binding = credential_store.get_service_agent_binding(MCP, AGENT)
        assert binding.installation_id == "10" and binding.repositories == ("alice/repo",) and binding.read_only
        assert _reach(narrow, read_only=False).status_code == 200
        assert credential_store.get_service_agent_binding(MCP, AGENT)[3:5] == (("alice/repo",), False)
        assert _reach(narrow, repositories=[]).status_code == 200
        assert credential_store.get_service_agent_binding(MCP, AGENT)[3:5] == ((), False)
        resp = _reach(narrow, installation_id="", read_only=True)
        assert resp.status_code == 409 and resp.json()["detail"] == (
            "This account is already one of this agent's. Remove it, then add it again the way you want.")
        assert credential_store.get_service_agent_binding(MCP, AGENT)[2:5] == ("10", (), False)

    def test_an_installation_rows_list_is_looked_up_on_the_installation(self, narrow):
        """On an installation every name must be one the installation
        reaches, or GitHub would narrow its token to another repository of
        the same name: at the add and at a reach save alike."""
        resp = _add_svc(narrow, installation_id="10", repositories=["alice/repo", "orgb/repo"])
        assert resp.status_code == 400 and "orgb/repo is not among" in resp.json()["detail"]
        assert credential_store.get_service_agent_binding(MCP, AGENT) is None
        assert _mints(narrow) == []
        assert _add_svc(narrow, installation_id="10", repositories=["alice/repo"]).status_code == 200
        resp = _reach(narrow, repositories=["alice/repo", "orgb/repo"])
        assert resp.status_code == 400 and "orgb/repo is not among" in resp.json()["detail"]
        assert credential_store.get_service_agent_binding(MCP, AGENT).repositories == ("alice/repo",)

    def test_a_row_removed_meanwhile_is_never_put_back(self, narrow):
        """A page left open after the account was taken out saves a reach:
        refused in words, nothing written, nothing minted, the set as the
        removal left it."""
        _connect(narrow, label="work")
        assert _add_svc(narrow, account_label="work").status_code == 200
        assert _add_svc(narrow, installation_id="10").status_code == 200
        assert _remove_svc(narrow).status_code == 200
        mints = len(_mints(narrow))
        resp = _reach(narrow, installation_id="10", repositories=["alice/repo"], read_only=True)
        assert resp.status_code == 404 and resp.json()["detail"] == "This account is no longer one of this agent's."
        assert [(r.account_label, r.installation_id, r.repositories, r.read_only, r.is_default)
                for r in service_account_set.list_service_agent_set(MCP, AGENT, standing_only=False)] == [
            ("work", "", (), False, True)]
        assert len(_mints(narrow)) == mints and github_apps.installation_files(PROVIDER, 10) == []

    def test_a_kept_field_is_read_under_the_lock(self, narrow, monkeypatch):
        """A save that names only Read only keeps the repositories the row
        holds when the store writes, not when the route first read it: the
        row taken out and added again with another list in between keeps
        its own list."""
        from api.mcp import service_account_set as set_api
        assert _add_svc(narrow, installation_id="10", repositories=["alice/repo"]).status_code == 200
        real = set_api._row_of
        reads = {"n": 0}

        def row_then_replaced(mcp, agent, label):
            row = real(mcp, agent, label)
            reads["n"] += 1
            if reads["n"] == 1:
                with get_conn() as conn:
                    conn.execute("UPDATE service_agent_bindings SET repositories=%s "
                                 "WHERE mcp_name=%s AND agent_name=%s AND account_label='alice'",
                                 (json.dumps([]), MCP, AGENT))
                    conn.commit()
            return row

        monkeypatch.setattr(set_api, "_row_of", row_then_replaced)
        assert _reach(narrow, installation_id="10", read_only=True).status_code == 200
        assert credential_store.get_service_agent_binding(MCP, AGENT)[3:5] == ((), True)

    def test_only_the_person_who_added_an_account_changes_its_reach(self, narrow):
        from storage.pg import get_conn
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO service_agent_bindings (mcp_name, agent_name, account_label, account_owner_sub, "
                "set_by, set_at, repositories) VALUES (%s, %s, 'theirs', 'nobody', 'nobody', 'now', %s)",
                (MCP, AGENT, json.dumps(["alice/repo"])))
            conn.commit()
        resp = _reach(narrow, account_label="theirs", installation_id="", repositories=[])
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Only the person who added this account can change its repositories."
        rows = service_account_set.list_service_agent_set(MCP, AGENT, standing_only=False)
        assert [(r.account_label, r.repositories) for r in rows] == [("theirs", ("alice/repo",))]


class TestThePicker:
    def test_the_account_lists_every_installation_it_reaches_once_a_minute(self, narrow):
        out = _repos(narrow).json()
        assert out == {"pickable": True, "from_meta": False, "installations": [
            {"id": 10, "account": "alice", "repositories": ["alice/repo"], "truncated": False}]}
        listings = len([r for r in narrow.fake.requests if r.url.path.endswith("/repositories")])
        assert _repos(narrow).status_code == 200
        assert len([r for r in narrow.fake.requests if r.url.path.endswith("/repositories")]) == listings
        assert _repos(narrow, installation_id="99").json()["installations"] == []

    def test_github_down_reads_the_meta(self, narrow):
        narrow.fake.routes[("GET", "/user/installations/10/repositories")] = (500, {"message": "down"})
        out = _repos(narrow).json()
        assert out["from_meta"] is True and out["installations"][0]["truncated"] is True   # 'all': no list held

    def test_a_hosted_account_is_offered_as_the_account_alone(self, narrow, monkeypatch):
        posted: list[str] = []
        _hosted(monkeypatch, posted)
        opts = narrow.client.get(f"/v1/agents/{AGENT}/mcps/{MCP}/service-account-options").json()
        acc = opts["my_accounts"][0]
        assert acc["hosted"] is True and "installations" not in acc
        credential_store.set_account_meta(narrow.sub, MCP, "alice", {
            **credential_store.get_account_meta(narrow.sub, MCP, "alice"), "hosted": True})
        monkeypatch.setattr("services.billing.relay_client.hosted_oauth_active", lambda *a, **k: False)
        resp = _add_svc(narrow, installation_id="10")
        assert resp.status_code == 400 and "Hosted sign-in has no app identity" in resp.json()["detail"]

    def test_a_hosted_installation_row_left_from_before_says_what_to_do(self, narrow, monkeypatch):
        assert _add_svc(narrow, installation_id="10").status_code == 200
        posted: list[str] = []
        _hosted(monkeypatch, posted)
        cur = narrow.client.get(f"/v1/agents/{AGENT}/mcps/{MCP}/service-account-options").json()["accounts"][0]
        assert cur["problem"] == ("hosted sign-in gives an agent no app identity; a manager adds the "
                                  "account itself in Agent Settings, MCPs")

    @pytest.mark.parametrize("stop", ["suspended", "gone", "not_owned"])
    def test_an_installation_row_that_stopped_working_says_so(self, narrow, stop):
        """A suspended, removed or no longer owned installation fails every
        session on it: its row says so on the card, while a standing row
        says nothing."""
        from api.mcp import service_account_set as set_api
        assert _add_svc(narrow, installation_id="10").status_code == 200
        options = f"/v1/agents/{AGENT}/mcps/{MCP}/service-account-options"
        assert narrow.client.get(options).json()["accounts"][0]["problem"] == ""
        meta = credential_store.get_account_meta(narrow.sub, MCP, "alice")
        installs = kinds.installations_of(meta)
        if stop == "suspended":
            installs = [{**i, "suspended_at": "2026-11-08T00:00:00Z"} for i in installs]
        elif stop == "not_owned":
            installs = [{**i, "admin": False} for i in installs]
        else:
            installs = []
        credential_store.set_account_meta(narrow.sub, MCP, "alice", {**meta, "installations": installs})
        row = narrow.client.get(options).json()["accounts"][0]
        assert row["problem"] == set_api.STOPPED == (
            "This installation no longer works for this agent. Remove it, then add the account again.")


class TestTheNarrowedInstallationToken:
    def test_two_reaches_two_files_and_the_worker_keeps_each_narrowed(self, narrow):
        assert _add_svc(narrow, installation_id="10", repositories=["alice/repo"]).status_code == 200
        reach = github_reach.reach_of(["alice/repo"], False)
        path = _narrowed_file(reach)
        for n in range(2):
            raw = json.loads(path.read_text())
            path.write_text(json.dumps({**raw, "expires_at": _expires_at(100)}))
            narrow.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route(f"ghs_w{n}")
            oauth_refresh_worker._failure_state.clear()
            assert asyncio.run(oauth_refresh_worker._maybe_refresh_token_file(
                token_file=path, username=kinds.APP_TOKEN_DIR, provider_id=PROVIDER)) is True
            assert json.loads(_mints(narrow)[-1].content) == {"repositories": ["repo"]}
            assert json.loads(path.read_text())["extra"]["reach"] == reach.to_dict()
        # A reach change reaps the old file and mints the new one.
        assert _reach(narrow, repositories=["alice/repo"], read_only=True).status_code == 200
        assert not path.exists() and _narrowed_file(github_reach.reach_of(["alice/repo"], True)).exists()
        # The installation removed: every file of it goes, the row is named for the remover.
        detached: list[tuple[str, str]] = []
        github_app_lifecycle._detach(PROVIDER, [MCP], "10", detached)
        assert github_apps.installation_files(PROVIDER, 10) == [] and detached == [(MCP, AGENT)]

    def test_a_read_only_token_is_never_minted_wide(self, narrow):
        narrow.fake.routes[("GET", "/app/installations/10")] = (200, {"id": 10, "permissions": {"workflows": "write"}})
        resp = _add_svc(narrow, installation_id="10", read_only=True)
        assert resp.status_code == 200 and "could not be reached" in resp.json()["warning"]
        assert _mints(narrow) == []                     # refused before GitHub was asked for a token
        assert not _narrowed_file(github_reach.reach_of([], True)).exists()

    def test_a_repository_that_left_the_installation_says_so(self, narrow):
        narrow.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route(
            status=422, body={"message": "There is at least one repository that does not exist"})
        resp = _add_svc(narrow, installation_id="10", repositories=["alice/repo"], read_only=True)
        assert resp.status_code == 200
        assert resp.json()["warning"] == credential_resolver.reconnect_wording(kinds.INSTALLATION_REACH_OUTDATED)

    def test_refused_read_permissions_are_not_a_repository_that_left(self, narrow, caplog):
        """A read-only mint with no list cannot fail on a repository: GitHub's
        refusal is logged and the next try may pass, the file is not marked."""
        narrow.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route(
            status=422, body={"message": "The permissions requested are not granted to this installation."})
        with caplog.at_level("WARNING"):
            resp = _add_svc(narrow, installation_id="10", read_only=True)
        assert resp.status_code == 200
        assert resp.json()["warning"] != credential_resolver.reconnect_wording(kinds.INSTALLATION_REACH_OUTDATED)
        assert "not granted to this installation" in caplog.text
        assert len(_mints(narrow)) == 1
        with pytest.raises(credential_resolver.CredentialUnavailable):
            github_apps.ensure_installation_token(MCP, narrow.sub, "alice", "10",
                                                  reach=github_reach.reach_of([], True))
        path = _narrowed_file(github_reach.reach_of([], True))
        assert not path.exists() or not json.loads(path.read_text()).get("extra", {}).get("refresh_failed")
        # A list and read only together: GitHub's message tells the two apart.
        narrow.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route(
            status=422, body={"message": "The permissions requested are not granted to this installation."})
        resp = _reach(narrow, repositories=["alice/repo"], read_only=True)
        assert resp.json()["warning"] != credential_resolver.reconnect_wording(kinds.INSTALLATION_REACH_OUTDATED)

    def test_a_name_of_another_owner_refuses_the_mint_and_is_never_cut(self, narrow):
        """A row acting as the installation whose stored reach names another
        owner's repository: GitHub is never asked for the bare name."""
        assert service_account_set.add_service_account_to_agent(
            MCP, AGENT, account_label="alice", owner_sub=narrow.sub, set_by=narrow.sub,
            installation_id="10", repositories=["alice/repo", "orgb/repo"]) == ""
        with pytest.raises(credential_resolver.AccountNeedsReconnect) as why:
            github_apps.ensure_installation_token(
                MCP, narrow.sub, "alice", "10", reach=github_reach.reach_of(["alice/repo", "orgb/repo"], False))
        assert str(why.value) == kinds.INSTALLATION_REACH_OUTDATED
        assert _mints(narrow) == []

    def test_an_app_step_gets_a_narrowed_installation_token_and_never_a_user_token(self, narrow, monkeypatch):
        manifest = mcp_registry.get_manifest(MCP)
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda agent, placement=None: [manifest])
        narrow.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route("ghs_narrow")
        assert _add_svc(narrow, installation_id="10", repositories=["alice/repo"]).status_code == 200
        env = credential_resolver.cli_token_env(AGENT, None, task_scope="agent", providers=[PROVIDER])
        assert env["GH_TOKEN"] == "ghs_narrow"
        assert _remove_svc(narrow).status_code == 200
        assert _add_svc(narrow, installation_id="", repositories=["alice/repo"]).status_code == 200
        assert credential_resolver.cli_token_env(AGENT, None, task_scope="agent", providers=[PROVIDER]) == {}


class TestTheSession:
    def test_the_credentials_carry_the_reach_and_the_session_is_told(self, narrow):
        assert _add_svc(narrow, installation_id="", repositories=["alice/repo"], read_only=True).status_code == 200
        from storage.identity import bearer_allowlist
        bearer_allowlist.add_allowed(PROVIDER, "api.githubcopilot.com", "test")
        manifest = mcp_registry.get_manifest(MCP)
        entry = {"type": "http", "url": "https://api.githubcopilot.com/mcp/"}
        cred, reason = mcp_registry.gateway_entry(
            entry, manifest, srv_key=MCP, user_sub=None, agent_name=AGENT, task_scope="agent", bundle_env={})
        assert reason is None and cred.reach == {"repositories": ["alice/repo"], "read_only": True}
        assert cred.forwarded_here
        mine, _ = mcp_registry.gateway_entry(
            {"type": "http", "url": "https://api.githubcopilot.com/mcp/"}, manifest, srv_key=MCP, user_sub=narrow.sub, agent_name=AGENT, task_scope="user",
            bundle_env={})
        assert mine is not None and mine.reach is None          # a person's own session reaches what it reaches
        resolved = credential_resolver.resolve_credentials(AGENT, None, task_scope="agent")
        assert resolved.cli_credentials[MCP].reach == cred.reach
        assert resolved.env_vars.get("GIT_CONFIG_COUNT") == "4"
        lines = asyncio.run(dynamic_context.get_dynamic_contexts(AGENT, [MCP], user_sub=""))
        text = "\n".join(t for name, t in lines if name == MCP)
        assert "This agent may reach only these GitHub repositories: alice/repo." in text
        assert "This agent may only read on GitHub." in text
        assert not [t for name, t in asyncio.run(dynamic_context.get_dynamic_contexts(
            AGENT, [MCP], user_sub=narrow.sub)) if "Your GitHub reach" in t]

    def test_a_reach_is_never_offered_for_a_reach_it_does_not_narrow(self, narrow):
        assert credential_resolver.binding_reach(MCP, AGENT, user_sub=None, task_scope="agent") is None
        assert _add_svc(narrow, installation_id="", repositories=[], read_only=False).status_code == 200
        assert credential_resolver.binding_reach(MCP, AGENT, user_sub=None, task_scope="agent") is None


_GITHUB_ENTRY = {"type": "http", "url": "https://api.githubcopilot.com/mcp/"}


def _own_entry(rig):  # noqa: F811
    """The GitHub tool's credential of the person's own session on the agent."""
    from storage.identity import bearer_allowlist
    bearer_allowlist.add_allowed(PROVIDER, "api.githubcopilot.com", "test")
    cred, reason = mcp_registry.gateway_entry(
        dict(_GITHUB_ENTRY), mcp_registry.get_manifest(MCP), srv_key=MCP, user_sub=rig.sub,
        agent_name=AGENT, task_scope="user", bundle_env={})
    assert reason is None
    return cred


class TestAPersonsOwnReach:
    """A person narrows their own connection for one agent: their user-scope
    sessions carry it everywhere a service reach goes, worded as theirs."""

    def test_the_session_carries_it_and_is_told_who_narrowed(self, narrow):
        from core.credentials import mcp_gateway
        assert credential_store.set_account_agent_binding(
            narrow.sub, MCP, AGENT, "alice", repositories=["alice/repo"], read_only=True)
        cred = _own_entry(narrow)
        assert cred.reach == {"repositories": ["alice/repo"], "read_only": True, "personal": True}
        assert cred.forwarded_here
        refusal, _ = mcp_gateway.judge_reach(cred, MCP, "POST", json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_file_contents", "arguments": {"owner": "evil", "repo": "x"}}}).encode())
        assert refusal == ("You limited this agent to alice/repo in your GitHub connection. "
                           "evil/x is not one of them.")
        resolved = credential_resolver.resolve_credentials(AGENT, narrow.sub, task_scope="user")
        cli = resolved.cli_credentials[MCP]
        assert cli.reach == cred.reach and resolved.env_vars.get("GIT_CONFIG_COUNT") == "4"
        reach = github_reach.Reach.from_dict(cli.reach)
        assert github_reach.judge_git(reach, f"{github_reach.PUSH_MARKER}/alice/repo.git") == (
            "You made this agent read only in your GitHub connection. git push was refused.")
        assert github_reach.judge_gh(reach, ["issue", "list", "-R", "evil/x"], []).startswith(
            "You limited this agent to alice/repo in your GitHub connection.")
        lines = asyncio.run(dynamic_context.get_dynamic_contexts(AGENT, [MCP], user_sub=narrow.sub))
        text = "\n".join(t for name, t in lines if name == MCP)
        assert "Your user limited this agent to alice/repo in their GitHub connection." in text
        assert "Your user made this agent read only in their GitHub connection." in text
        # The agent's own sessions are not narrowed by a person's choice.
        assert credential_resolver.binding_reach(MCP, AGENT, user_sub=None, task_scope="agent") is None
        assert not [t for _n, t in asyncio.run(dynamic_context.get_dynamic_contexts(AGENT, [MCP], user_sub=""))
                    if "Your GitHub reach" in t]

    def test_it_stays_off_a_paired_machine_as_a_service_reach_does(self, narrow):
        import uuid

        from core.credentials import mcp_broker
        from core.remote import mcp_gateway_push as push
        from core.remote.remote_start_payload import _gateway_facts
        from tests.remote.test_mcp_gateway_push import _Cm
        assert credential_store.set_account_agent_binding(narrow.sub, MCP, AGENT, "alice", repositories=["alice/repo"])
        cred = _own_entry(narrow)
        sid = str(uuid.uuid4())
        mcp_broker.provision(sid, {MCP: mcp_broker.SecretBundle(gateway=cred)})
        try:
            cm = _Cm()
            assert asyncio.run(push.push_session(sid, "m-1", "h" * 64, cm=cm)) == 0 and cm.sent == []
            facts = _gateway_facts({MCP: mcp_broker.SecretBundle(gateway=cred)})
            assert facts[MCP]["tunneled"] is True and "value" not in facts[MCP]
        finally:
            mcp_broker.purge_session(sid)

    def test_no_choice_or_a_choice_that_narrows_nothing_reaches_everything(self, narrow):
        assert credential_resolver.binding_reach(MCP, AGENT, user_sub=narrow.sub, task_scope="user") is None
        assert credential_store.set_account_agent_binding(narrow.sub, MCP, AGENT, "alice")
        assert credential_resolver.binding_reach(MCP, AGENT, user_sub=narrow.sub, task_scope="user") is None
        assert credential_store.set_account_agent_binding(narrow.sub, MCP, AGENT, "alice", repositories=["alice/repo"])
        assert _own_entry(narrow).reach is not None
        # Removing the choice is the person's own hand: the default account, no reach.
        credential_store.remove_account_agent_binding(narrow.sub, MCP, AGENT)
        assert _own_entry(narrow).reach is None

    def test_an_app_step_of_a_persons_reach_gets_nothing(self, narrow, monkeypatch):
        """Even when the agent's own account is an installation: a person's
        own account is never one, and a step holds its token unjudged."""
        manifest = mcp_registry.get_manifest(MCP)
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda agent, placement=None: [manifest])
        narrow.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route("ghs_service")
        assert _add_svc(narrow, installation_id="10").status_code == 200
        assert credential_resolver.cli_token_env(AGENT, narrow.sub, task_scope="user", providers=[PROVIDER])
        assert credential_store.set_account_agent_binding(narrow.sub, MCP, AGENT, "alice", read_only=True)
        assert credential_resolver.cli_token_env(AGENT, narrow.sub, task_scope="user", providers=[PROVIDER]) == {}

    def test_a_shared_only_chat_is_told_the_agents_reach_not_the_persons(self, narrow, monkeypatch):
        from core.session import visibility
        assert _add_svc(narrow, installation_id="", repositories=["alice/repo"]).status_code == 200
        assert credential_store.set_account_agent_binding(narrow.sub, MCP, AGENT, "alice", read_only=True)
        monkeypatch.setattr(visibility, "is_shared_only", lambda agent: True)
        text = "\n".join(t for name, t in asyncio.run(dynamic_context.get_dynamic_contexts(
            AGENT, [MCP], user_sub=narrow.sub)) if name == MCP)
        assert "This agent may reach only these GitHub repositories: alice/repo." in text
        assert "read only" not in text


def _own_put(rig, **body):  # noqa: F811
    return rig.client.put(f"/v1/users/me/integrations/{MCP}/agent-binding",
                          json={"agent_name": AGENT, "account_label": "alice", **body})


def _own_repos(rig, **params):  # noqa: F811
    return rig.client.get(f"/v1/users/me/integrations/{MCP}/repositories", params={"account_label": "alice", **params})


def _own(rig):  # noqa: F811
    return credential_store.get_account_agent_binding(rig.sub, MCP, AGENT)


@pytest.fixture
def mine(narrow, monkeypatch):
    """The person uses the agent, and GitHub is enabled on it."""
    manifest = mcp_registry.get_manifest(MCP)
    monkeypatch.setattr(mcp_registry, "get_agent_mcps_all_placements", lambda agent, placement=None: [manifest])
    yield narrow


class TestAPersonsOwnPut:
    def test_a_reach_is_stored_and_listed_on_the_card(self, mine):
        resp = _own_put(mine, repositories=["Alice/Repo"], read_only=True)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "repositories": ["Alice/Repo"], "read_only": True}
        assert _own(mine) == ("alice", ("Alice/Repo",), True, True)
        mine.holder["role"] = "creator"             # the list follows the agents the person uses
        entry = next(e for e in mine.client.get("/v1/users/me/integrations").json() if e["mcp_name"] == MCP)
        assert entry["reach_supported"] is True
        acc = next(a for a in entry["accounts"] if a["account_label"] == "alice")
        assert acc["agent_overrides"] == [AGENT] and acc["agent_defaults"] == [AGENT]
        assert acc["agent_reaches"] == [{"agent": AGENT, "repositories": ["Alice/Repo"], "read_only": True}]
        assert entry["agents_on_own_accounts"] == []

    def test_a_shared_only_agent_is_named_on_the_list(self, mine, monkeypatch):
        """Its chats resolve as the agent: the card offers no repositories for it."""
        from core.session import visibility
        monkeypatch.setattr(visibility, "is_shared_only", lambda agent: agent == AGENT)
        mine.holder["role"] = "creator"
        entry = next(e for e in mine.client.get("/v1/users/me/integrations").json() if e["mcp_name"] == MCP)
        assert entry["candidate_agents"] == [AGENT] and entry["agents_on_own_accounts"] == [AGENT]

    def test_an_older_client_keeps_it_and_a_remove_widens_back(self, mine):
        assert _own_put(mine, repositories=["alice/repo"], read_only=True).status_code == 200
        assert _own_put(mine).json()["read_only"] is True
        assert _own(mine) == ("alice", ("alice/repo",), True, True)
        assert mine.client.delete(f"/v1/users/me/integrations/{MCP}/agent-binding/{AGENT}").status_code == 200
        assert _own(mine) is None
        assert credential_resolver.binding_reach(MCP, AGENT, user_sub=mine.sub, task_scope="user") is None

    def test_what_cannot_hold_is_refused_in_words(self, mine, monkeypatch):
        resp = _own_put(mine, repositories=["alice/other"])
        assert resp.status_code == 400
        assert resp.json()["detail"] == "alice/other is not among the repositories this account reaches."
        resp = _own_put(mine, repositories=["not a name"])
        assert resp.status_code == 400 and "Write it as owner/name." in resp.json()["detail"]
        # A mistyped account is not found, whatever it is asked to hold.
        resp = _own_put(mine, account_label="ghost", repositories=["alice/repo"])
        assert resp.status_code == 404 and resp.json()["detail"] == f"Account 'ghost' not found for {MCP}"
        assert _own(mine) is None
        resp = mine.client.post(f"/v1/oauth/{PROVIDER}/pat/save", json={
            "mcp_name": MCP, "token": "ghp_fake", "services": ["repos"], "account_label": "tok"})
        assert resp.status_code == 200, resp.text
        resp = _own_put(mine, account_label="tok", repositories=["alice/repo"])
        assert resp.status_code == 400 and resp.json()["detail"] == (
            "This account has no list of repositories to pick from. You can still make an agent read only.")
        assert _own_put(mine, account_label="tok", read_only=True).status_code == 200
        assert _own(mine) == ("tok", (), True, True)
        from api.mcp import credentials as credentials_api
        monkeypatch.setattr(credentials_api, "_reach_supported", lambda name: False)
        resp = _own_put(mine, read_only=True)
        assert resp.status_code == 400 and "Only GitHub" in resp.json()["detail"]
        assert _own_put(mine, repositories=[], read_only=False).status_code == 200

    def test_only_names_the_choice_does_not_hold_are_looked_up(self, mine):
        assert _own_put(mine, repositories=["alice/repo"]).status_code == 200
        github_apps._listed.clear()
        mine.fake.routes[("GET", "/user/installations/10/repositories")] = (500, {"message": "down"})
        assert _own_put(mine, repositories=["ALICE/repo"], read_only=True).status_code == 200
        resp = _own_put(mine, repositories=["alice/repo", "bob/new"])
        assert resp.status_code == 503 and "could not be reached" in resp.json()["detail"]

    def test_a_list_kept_onto_another_account_is_looked_up_there(self, mine):
        _connect(mine, label="work")
        assert credential_store.set_account_agent_binding(
            mine.sub, MCP, AGENT, "alice", repositories=["alice/repo", "orgb/repo"])
        resp = _own_put(mine, account_label="work")
        assert resp.status_code == 400 and resp.json()["detail"] == (
            "orgb/repo is one of the repositories you chose for this agent, and this account does not "
            "reach it. Change the repositories first, then choose this account again.")
        assert _own(mine).account_label == "alice"
        assert credential_store.set_account_agent_binding(mine.sub, MCP, AGENT, "alice", repositories=["alice/repo"])
        assert _own_put(mine, account_label="work").status_code == 200
        assert _own(mine) == ("work", ("alice/repo",), False, True)

    def test_a_list_kept_onto_a_token_account_stays_as_it_is(self, mine):
        """A token account has no list to look it up on, and the list only
        narrows: kept, never dropped."""
        resp = mine.client.post(f"/v1/oauth/{PROVIDER}/pat/save", json={
            "mcp_name": MCP, "token": "ghp_fake", "services": ["repos"], "account_label": "tok"})
        assert resp.status_code == 200, resp.text
        assert credential_store.set_account_agent_binding(
            mine.sub, MCP, AGENT, "alice", repositories=["orgb/repo"], read_only=True)
        assert _own_put(mine, account_label="tok").status_code == 200
        assert _own(mine) == ("tok", ("orgb/repo",), True, True)

    def test_the_picker_lists_the_persons_own_account(self, mine, monkeypatch):
        assert _own_repos(mine).json() == {"pickable": True, "from_meta": False, "installations": [
            {"id": 10, "account": "alice", "repositories": ["alice/repo"], "truncated": False}]}
        assert _own_repos(mine, account_label="nobody").json() == {
            "pickable": False, "installations": [], "from_meta": False}
        from api.mcp import credentials as credentials_api
        monkeypatch.setattr(credentials_api, "_reach_supported", lambda name: False)
        assert _own_repos(mine).status_code == 400


class TestTheCopyAtAddTime:
    """A manager adding their own account to the agent's own set starts
    from the reach they chose for their own chats with it, once."""

    def test_a_new_row_starts_from_it_and_each_side_then_changes_alone(self, mine):
        assert credential_store.set_account_agent_binding(
            mine.sub, MCP, AGENT, "alice", repositories=["alice/repo"], read_only=True)
        resp = _add_svc(mine)
        assert resp.status_code == 200, resp.text
        assert resp.json()["reach_copied"] is True
        assert _account(resp)["repositories"] == ["alice/repo"] and _account(resp)["read_only"] is True
        assert credential_store.get_service_agent_binding(MCP, AGENT)[3:5] == (("alice/repo",), True)
        # A copy, never a link.
        resp = _reach(mine, repositories=[], read_only=False)
        assert resp.status_code == 200 and "reach_copied" not in resp.json()
        assert _own(mine) == ("alice", ("alice/repo",), True, True)
        assert _own_put(mine, read_only=False).status_code == 200
        assert credential_store.get_service_agent_binding(MCP, AGENT)[3:5] == ((), False)
        # Taken out and added again, the account is new again: it starts from the reach.
        assert credential_store.set_account_agent_binding(mine.sub, MCP, AGENT, "alice", read_only=True)
        assert _remove_svc(mine).status_code == 200
        resp = _add_svc(mine)
        assert resp.json()["reach_copied"] is True and _account(resp)["read_only"] is True

    def test_nothing_is_copied_otherwise(self, mine):
        assert credential_store.set_account_agent_binding(
            mine.sub, MCP, AGENT, "alice", repositories=["alice/repo"], read_only=True)
        # An add naming a reach.
        resp = _add_svc(mine, read_only=False)
        assert resp.json()["reach_copied"] is False and _account(resp)["repositories"] == []
        # An account the set already holds keeps its own reach.
        assert _add_svc(mine).json()["reach_copied"] is False
        assert credential_store.get_service_agent_binding(MCP, AGENT)[3:5] == ((), False)
        assert _remove_svc(mine).status_code == 200
        # An installation: another identity, narrowed by GitHub.
        resp = _add_svc(mine, installation_id="10")
        assert resp.json()["reach_copied"] is False and _account(resp)["repositories"] == []
        assert _remove_svc(mine).status_code == 200
        # Another account than the one the person chose for the agent.
        _connect(mine, label="work")
        assert _add_svc(mine, account_label="work").json()["reach_copied"] is False

    def test_a_row_a_former_manager_left_keeps_its_own_reach(self, mine):
        """It stays beside the new account, which starts from the person's
        own reach and never from that row."""
        assert credential_store.set_account_agent_binding(mine.sub, MCP, AGENT, "alice", read_only=True)
        from storage.pg import get_conn
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO service_agent_bindings (mcp_name, agent_name, account_label, account_owner_sub, "
                "set_by, set_at, repositories) VALUES (%s, %s, 'gone', 'nobody', 'nobody', 'now', %s)",
                (MCP, AGENT, json.dumps(["alice/repo"])))
            conn.commit()
        resp = _add_svc(mine)
        assert resp.status_code == 200, resp.text
        assert resp.json()["reach_copied"] is True
        assert (_account(resp)["repositories"], _account(resp)["read_only"]) == ([], True)
        gone = _account(resp, "gone")
        assert (gone["repositories"], gone["read_only"], gone["stands"]) == (["alice/repo"], False, False)

    def test_never_the_other_direction(self, mine):
        assert _add_svc(mine, repositories=["alice/repo"], read_only=True).status_code == 200
        assert _own(mine) is None
        assert credential_resolver.binding_reach(MCP, AGENT, user_sub=mine.sub, task_scope="user") is None


@pytest.mark.parametrize("table", ["service_agent_bindings", "agent_account_bindings"])
def test_the_reach_lives_in_two_additive_columns(table):
    from storage.pg import get_conn
    with get_conn() as conn:
        cols = {r["column_name"]: (r["data_type"], r["column_default"]) for r in conn.execute(
            "SELECT column_name, data_type, column_default FROM information_schema.columns "
            "WHERE table_name = %s", (table,)).fetchall()}
    assert cols["repositories"][0] == "text" and "'[]'" in str(cols["repositories"][1])
    assert cols["read_only"][0] == "boolean" and "false" in str(cols["read_only"][1]).lower()

