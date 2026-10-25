"""An agent's GitHub reach on its service binding: the PUT stores a
repository list and the read-only choice and refuses what cannot hold, the
picker lists what the account reaches, the options show the reach and
offer a hosted account as the account alone, an installation binding is
narrowed by GitHub itself (one token file per reach, re-minted the same
way, never minted wide), an app step of a narrowed user token gets nothing,
the sessions are told once, and an older client never widens a reach.

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
from storage.identity import credential_store, oauth_app_kinds as kinds
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
    _expires_at,
    _hosted,
    _mint_route,
    _mints,
    bound,
)

_PERMISSIONS = {"contents": "write", "issues": "write", "metadata": "read", "workflows": "write"}


def _put(rig, **body):  # noqa: F811
    return rig.client.put(f"/v1/agents/{AGENT}/mcps/{MCP}/service-binding", json={"account_label": "alice", **body})


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


class TestThePut:
    def test_a_reach_is_stored_minted_narrowed_and_shown(self, narrow):
        resp = _put(narrow, installation_id="10", repositories=["Alice/Repo"], read_only=True)
        assert resp.status_code == 200, resp.text
        assert resp.json()["repositories"] == ["Alice/Repo"] and resp.json()["read_only"] is True
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
        cur = opts["current_binding"]
        assert cur["repositories"] == ["Alice/Repo"] and cur["read_only"] is True and cur["problem"] == ""

    def test_a_name_the_account_does_not_reach_is_refused(self, narrow):
        resp = _put(narrow, installation_id="10", repositories=["alice/other"])
        assert resp.status_code == 400
        assert resp.json()["detail"] == "alice/other is not among the repositories this account reaches."
        resp = _put(narrow, installation_id="10", repositories=["not a name"])
        assert resp.status_code == 400 and "Write it as owner/name." in resp.json()["detail"]
        assert credential_store.get_service_agent_binding(MCP, AGENT) is None

    def test_a_narrowing_change_never_waits_on_github(self, narrow):
        assert _put(narrow, installation_id="10", repositories=["alice/repo"]).status_code == 200
        github_apps._listed.clear()
        narrow.fake.routes[("GET", "/user/installations/10/repositories")] = (500, {"message": "down"})
        assert _put(narrow, repositories=["alice/repo"], read_only=True).status_code == 200
        # The installation is on all of alice's repositories: a name of hers is taken.
        assert _put(narrow, repositories=["alice/repo", "alice/new"]).status_code == 200
        resp = _put(narrow, repositories=["alice/repo", "bob/new"])
        assert resp.status_code == 503 and "could not be reached" in resp.json()["detail"]

    def test_a_cut_listing_takes_a_name_on_its_account(self):
        listed = [{"id": 10, "account": "alice", "repositories": ["alice/a"], "truncated": True}]
        assert github_apps.unreachable_names(listed, ["alice/a", "alice/z", "bob/x"]) == ["bob/x"]

    def test_a_token_account_can_be_read_only_but_has_no_list(self, narrow):
        resp = narrow.client.post(f"/v1/oauth/{PROVIDER}/pat/save", json={
            "mcp_name": MCP, "token": "ghp_fake", "services": ["repos"], "account_label": "tok"})
        assert resp.status_code == 200, resp.text
        resp = _put(narrow, account_label="tok", repositories=["alice/repo"])
        assert resp.status_code == 400 and "no list of repositories" in resp.json()["detail"]
        assert _put(narrow, account_label="tok", read_only=True).status_code == 200
        assert credential_store.get_service_agent_binding(MCP, AGENT).read_only is True
        assert _repos(narrow, account_label="tok").json() == {"pickable": False, "installations": [],
                                                             "from_meta": False}

    def test_a_reach_only_for_github(self, narrow, monkeypatch):
        from api.mcp import credentials as credentials_api
        monkeypatch.setattr(credentials_api, "_reach_supported", lambda name: False)
        resp = _put(narrow, read_only=True)
        assert resp.status_code == 400 and "Only GitHub" in resp.json()["detail"]
        assert _put(narrow, repositories=[], read_only=False).status_code == 200

    def test_an_older_client_never_widens_a_reach(self, narrow):
        assert _put(narrow, installation_id="10", repositories=["alice/repo"], read_only=True).status_code == 200
        assert _put(narrow, installation_id="").status_code == 200       # another identity, no reach sent
        binding = credential_store.get_service_agent_binding(MCP, AGENT)
        assert binding.installation_id == "" and binding.repositories == ("alice/repo",) and binding.read_only
        assert _put(narrow, repositories=[], read_only=False).status_code == 200
        binding = credential_store.get_service_agent_binding(MCP, AGENT)
        assert binding.repositories == () and binding.read_only is False

    def test_a_new_identity_has_the_list_it_keeps_looked_up(self, narrow):
        """A rebind keeps the list: on an installation every name must be
        one the installation reaches, or GitHub would narrow its token to
        another repository of the same name."""
        assert credential_store.set_service_agent_binding(
            MCP, AGENT, account_label="alice", owner_sub=narrow.sub, set_by=narrow.sub,
            installation_id="", repositories=["alice/repo", "orgb/repo"])
        resp = _put(narrow, installation_id="10")
        assert resp.status_code == 400
        assert resp.json()["detail"] == ("The agent's GitHub reach names orgb/repo, which this choice does not "
                                         "reach. Change the repositories first, then choose it again.")
        assert credential_store.get_service_agent_binding(MCP, AGENT).installation_id == ""
        assert _mints(narrow) == []
        # The same list sent with the new identity is looked up as well.
        resp = _put(narrow, installation_id="10", repositories=["alice/repo", "orgb/repo"])
        assert resp.status_code == 400 and "orgb/repo is not among" in resp.json()["detail"]
        assert _put(narrow, installation_id="10", repositories=["alice/repo"]).status_code == 200


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
        resp = _put(narrow, installation_id="10")
        assert resp.status_code == 400 and "Hosted sign-in has no app identity" in resp.json()["detail"]

    def test_a_hosted_binding_left_from_before_says_what_to_do(self, narrow, monkeypatch):
        assert _put(narrow, installation_id="10").status_code == 200
        posted: list[str] = []
        _hosted(monkeypatch, posted)
        cur = narrow.client.get(f"/v1/agents/{AGENT}/mcps/{MCP}/service-account-options").json()["current_binding"]
        assert cur["problem"] == ("hosted sign-in gives an agent no app identity; a manager binds the "
                                  "account itself in Agent Settings, MCPs")


class TestTheNarrowedInstallationToken:
    def test_two_reaches_two_files_and_the_worker_keeps_each_narrowed(self, narrow):
        assert _put(narrow, installation_id="10", repositories=["alice/repo"]).status_code == 200
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
        assert _put(narrow, repositories=["alice/repo"], read_only=True).status_code == 200
        assert not path.exists() and _narrowed_file(github_reach.reach_of(["alice/repo"], True)).exists()
        # The installation removed: every file of it goes.
        github_app_lifecycle._detach(PROVIDER, [MCP], "10")
        assert github_apps.installation_files(PROVIDER, 10) == []

    def test_a_read_only_token_is_never_minted_wide(self, narrow):
        narrow.fake.routes[("GET", "/app/installations/10")] = (200, {"id": 10, "permissions": {"workflows": "write"}})
        resp = _put(narrow, installation_id="10", read_only=True)
        assert resp.status_code == 200 and "could not be reached" in resp.json()["warning"]
        assert _mints(narrow) == []                     # refused before GitHub was asked for a token
        assert not _narrowed_file(github_reach.reach_of([], True)).exists()

    def test_a_repository_that_left_the_installation_says_so(self, narrow):
        narrow.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route(
            status=422, body={"message": "There is at least one repository that does not exist"})
        resp = _put(narrow, installation_id="10", repositories=["alice/repo"], read_only=True)
        assert resp.status_code == 200
        assert resp.json()["warning"] == credential_resolver.reconnect_wording(kinds.INSTALLATION_REACH_OUTDATED)

    def test_refused_read_permissions_are_not_a_repository_that_left(self, narrow, caplog):
        """A read-only mint with no list cannot fail on a repository: GitHub's
        refusal is logged and the next try may pass, the file is not marked."""
        narrow.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route(
            status=422, body={"message": "The permissions requested are not granted to this installation."})
        with caplog.at_level("WARNING"):
            resp = _put(narrow, installation_id="10", read_only=True)
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
        resp = _put(narrow, repositories=["alice/repo"], read_only=True)
        assert resp.json()["warning"] != credential_resolver.reconnect_wording(kinds.INSTALLATION_REACH_OUTDATED)

    def test_a_name_of_another_owner_refuses_the_mint_and_is_never_cut(self, narrow):
        """A reach kept from another identity naming another owner's
        repository: GitHub is never asked for the bare name."""
        assert credential_store.set_service_agent_binding(
            MCP, AGENT, account_label="alice", owner_sub=narrow.sub, set_by=narrow.sub,
            installation_id="10", repositories=["alice/repo", "orgb/repo"])
        with pytest.raises(credential_resolver.AccountNeedsReconnect) as why:
            github_apps.ensure_installation_token(
                MCP, narrow.sub, "alice", "10", reach=github_reach.reach_of(["alice/repo", "orgb/repo"], False))
        assert str(why.value) == kinds.INSTALLATION_REACH_OUTDATED
        assert _mints(narrow) == []

    def test_an_app_step_gets_a_narrowed_installation_token_and_never_a_user_token(self, narrow, monkeypatch):
        manifest = mcp_registry.get_manifest(MCP)
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda agent, placement=None: [manifest])
        narrow.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route("ghs_narrow")
        assert _put(narrow, installation_id="10", repositories=["alice/repo"]).status_code == 200
        env = credential_resolver.cli_token_env(AGENT, None, task_scope="agent", providers=[PROVIDER])
        assert env["GH_TOKEN"] == "ghs_narrow"
        assert _put(narrow, installation_id="", repositories=["alice/repo"]).status_code == 200
        assert credential_resolver.cli_token_env(AGENT, None, task_scope="agent", providers=[PROVIDER]) == {}


class TestTheSession:
    def test_the_credentials_carry_the_reach_and_the_session_is_told(self, narrow):
        assert _put(narrow, installation_id="", repositories=["alice/repo"], read_only=True).status_code == 200
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
        assert _put(narrow, installation_id="", repositories=[], read_only=False).status_code == 200
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
            facts = asyncio.run(_gateway_facts({MCP: mcp_broker.SecretBundle(gateway=cred)}, False))
            assert facts[MCP]["tunneled"] is True and facts[MCP]["value"] == ""
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
        """Even when the agent's service binding is an installation: a person's
        own account is never one, and a step holds its token unjudged."""
        manifest = mcp_registry.get_manifest(MCP)
        monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda agent, placement=None: [manifest])
        narrow.fake.routes[("POST", "/app/installations/10/access_tokens")] = _mint_route("ghs_service")
        assert _put(narrow, installation_id="10").status_code == 200
        assert credential_resolver.cli_token_env(AGENT, narrow.sub, task_scope="user", providers=[PROVIDER])
        assert credential_store.set_account_agent_binding(narrow.sub, MCP, AGENT, "alice", read_only=True)
        assert credential_resolver.cli_token_env(AGENT, narrow.sub, task_scope="user", providers=[PROVIDER]) == {}

    def test_a_shared_only_chat_is_told_the_agents_reach_not_the_persons(self, narrow, monkeypatch):
        from core.session import visibility
        assert _put(narrow, installation_id="", repositories=["alice/repo"]).status_code == 200
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
    monkeypatch.setattr(mcp_registry, "get_agent_mcps", lambda agent, placement=None: [manifest])
    yield narrow


class TestAPersonsOwnPut:
    def test_a_reach_is_stored_and_listed_on_the_card(self, mine):
        resp = _own_put(mine, repositories=["Alice/Repo"], read_only=True)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "ok", "repositories": ["Alice/Repo"], "read_only": True}
        assert _own(mine) == ("alice", ("Alice/Repo",), True)
        mine.holder["role"] = "creator"             # the list follows the agents the person uses
        entry = next(e for e in mine.client.get("/v1/users/me/integrations").json() if e["mcp_name"] == MCP)
        assert entry["reach_supported"] is True
        acc = next(a for a in entry["accounts"] if a["account_label"] == "alice")
        assert acc["agent_overrides"] == [AGENT]
        assert acc["agent_reaches"] == [{"agent": AGENT, "repositories": ["Alice/Repo"], "read_only": True}]
        assert entry["shared_only_agents"] == []

    def test_a_shared_only_agent_is_named_on_the_list(self, mine, monkeypatch):
        """Its chats resolve as the agent: the card offers no repositories for it."""
        from core.session import visibility
        monkeypatch.setattr(visibility, "is_shared_only", lambda agent: agent == AGENT)
        mine.holder["role"] = "creator"
        entry = next(e for e in mine.client.get("/v1/users/me/integrations").json() if e["mcp_name"] == MCP)
        assert entry["candidate_agents"] == [AGENT] and entry["shared_only_agents"] == [AGENT]

    def test_an_older_client_keeps_it_and_a_remove_widens_back(self, mine):
        assert _own_put(mine, repositories=["alice/repo"], read_only=True).status_code == 200
        assert _own_put(mine).json()["read_only"] is True
        assert _own(mine) == ("alice", ("alice/repo",), True)
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
        assert _own(mine) == ("tok", (), True)
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
        assert _own(mine) == ("work", ("alice/repo",), False)

    def test_a_list_kept_onto_a_token_account_stays_as_it_is(self, mine):
        """A token account has no list to look it up on, and the list only
        narrows: kept, never dropped."""
        resp = mine.client.post(f"/v1/oauth/{PROVIDER}/pat/save", json={
            "mcp_name": MCP, "token": "ghp_fake", "services": ["repos"], "account_label": "tok"})
        assert resp.status_code == 200, resp.text
        assert credential_store.set_account_agent_binding(
            mine.sub, MCP, AGENT, "alice", repositories=["orgb/repo"], read_only=True)
        assert _own_put(mine, account_label="tok").status_code == 200
        assert _own(mine) == ("tok", ("orgb/repo",), True)

    def test_the_picker_lists_the_persons_own_account(self, mine, monkeypatch):
        assert _own_repos(mine).json() == {"pickable": True, "from_meta": False, "installations": [
            {"id": 10, "account": "alice", "repositories": ["alice/repo"], "truncated": False}]}
        assert _own_repos(mine, account_label="nobody").json() == {
            "pickable": False, "installations": [], "from_meta": False}
        from api.mcp import credentials as credentials_api
        monkeypatch.setattr(credentials_api, "_reach_supported", lambda name: False)
        assert _own_repos(mine).status_code == 400


class TestTheCopyAtBindTime:
    """A manager binding their own account as the agent's service identity
    starts from the reach they chose for their own chats with it, once."""

    def _clear(self, rig):  # noqa: F811
        assert rig.client.delete(f"/v1/agents/{AGENT}/mcps/{MCP}/service-binding").status_code == 200

    def test_a_new_binding_starts_from_it_and_each_side_then_changes_alone(self, mine):
        assert credential_store.set_account_agent_binding(
            mine.sub, MCP, AGENT, "alice", repositories=["alice/repo"], read_only=True)
        resp = _put(mine)
        assert resp.status_code == 200, resp.text
        assert resp.json()["reach_copied"] is True
        assert resp.json()["repositories"] == ["alice/repo"] and resp.json()["read_only"] is True
        assert credential_store.get_service_agent_binding(MCP, AGENT)[3:] == (("alice/repo",), True)
        # A copy, never a link.
        assert _put(mine, repositories=[], read_only=False).json()["reach_copied"] is False
        assert _own(mine) == ("alice", ("alice/repo",), True)
        assert _own_put(mine, read_only=False).status_code == 200
        assert credential_store.get_service_agent_binding(MCP, AGENT)[3:] == ((), False)
        # A cleared and re-made binding is new again: it starts from the reach.
        assert credential_store.set_account_agent_binding(mine.sub, MCP, AGENT, "alice", read_only=True)
        self._clear(mine)
        resp = _put(mine)
        assert resp.json()["reach_copied"] is True and resp.json()["read_only"] is True

    def test_nothing_is_copied_otherwise(self, mine):
        assert credential_store.set_account_agent_binding(
            mine.sub, MCP, AGENT, "alice", repositories=["alice/repo"], read_only=True)
        # A PUT naming a reach.
        resp = _put(mine, read_only=False)
        assert resp.json()["reach_copied"] is False and resp.json()["repositories"] == []
        # A rebind: the binding holds its own reach.
        assert _put(mine, installation_id="").json()["reach_copied"] is False
        assert credential_store.get_service_agent_binding(MCP, AGENT)[3:] == ((), False)
        self._clear(mine)
        # An installation: another identity, narrowed by GitHub.
        resp = _put(mine, installation_id="10")
        assert resp.json()["reach_copied"] is False and resp.json()["repositories"] == []
        self._clear(mine)
        # Another account than the one the person chose for the agent.
        _connect(mine, label="work")
        assert _put(mine, account_label="work").json()["reach_copied"] is False
        self._clear(mine)
        # A row left by a manager who no longer manages the agent keeps its own reach.
        from storage.pg import get_conn
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO service_agent_bindings (mcp_name, agent_name, account_label, account_owner_sub, "
                "set_by, set_at, repositories) VALUES (%s, %s, 'gone', 'nobody', 'nobody', 'now', %s)",
                (MCP, AGENT, json.dumps(["alice/repo"])))
            conn.commit()
        resp = _put(mine)
        assert resp.json()["reach_copied"] is False and resp.json()["repositories"] == ["alice/repo"]
        assert resp.json()["read_only"] is False

    def test_never_the_other_direction(self, mine):
        assert _put(mine, repositories=["alice/repo"], read_only=True).status_code == 200
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

