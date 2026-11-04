"""A vendor account that goes for good (``services/oauth/account_release``):
the revoke, with the access token when there is no refresh token; a
person's deletion revoking, deleting and unbinding every account; and the
paths that only lend an account never unbinding it."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import config
from services.billing import relay_client
from services.mcp import mcp_registry
from services.oauth import account_release, github_apps
from services.webhooks import relay_events
from storage import database as task_store
from storage.identity import credential_store


class _Provider:
    def __init__(self):
        self.revoked: list[str] = []

    async def revoke(self, *, token, client_id, client_secret):
        self.revoked.append(token)
        return True


@pytest.fixture
def vendor(monkeypatch):
    provider = _Provider()
    monkeypatch.setattr(account_release, "get_provider", lambda pid: provider)
    relayed: list[tuple[str, str]] = []

    async def oauth_revoke(*, provider_id, token):
        relayed.append((provider_id, token))
    monkeypatch.setattr(relay_client, "oauth_revoke", oauth_revoke)
    return SimpleNamespace(provider=provider, relayed=relayed)


def _manifest(provider_id):
    return SimpleNamespace(name=f"{provider_id}-mcp", credentials=SimpleNamespace(
        oauth={"provider_id": provider_id}, webhooks={}))


def test_a_hosted_account_without_a_refresh_token_revokes_its_access_token(vendor):
    data = {"access_token": "xoxp-1", "extra": {"via_relay": True}}
    assert asyncio.run(account_release.revoke_account("slack", data, _manifest("slack"))) is True
    assert vendor.relayed == [("slack", "xoxp-1")]


def test_the_refresh_token_is_revoked_first(vendor):
    data = {"access_token": "a", "refresh_token": "r", "extra": {"via_relay": True}}
    asyncio.run(account_release.revoke_account("zoom", data, _manifest("zoom")))
    assert vendor.relayed == [("zoom", "r")]


def test_a_self_managed_account_revokes_with_its_client(vendor):
    data = {"access_token": "a", "client_id": "c", "client_secret": "s", "extra": {}}
    assert asyncio.run(account_release.revoke_account("slack", data, _manifest("slack"))) is True
    assert vendor.provider.revoked == ["a"]


def test_a_personal_access_token_is_never_revoked(vendor):
    data = {"access_token": "ghp_x", "token": "ghp_x", "extra": {"flow": "personal_access_token"}}
    assert asyncio.run(account_release.revoke_account("github", data, _manifest("github"))) is False
    assert vendor.provider.revoked == [] and vendor.relayed == []


def test_a_failed_revoke_reads_as_not_revoked(vendor, monkeypatch):
    async def refused(**kw):
        raise relay_client.RelayError("invalid_grant")
    monkeypatch.setattr(relay_client, "oauth_revoke", refused)
    data = {"refresh_token": "r", "extra": {"via_relay": True}}
    assert asyncio.run(account_release.revoke_account("github", data, _manifest("github"))) is False


def test_a_deleted_person_releases_every_account(vendor, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: "alice")
    manifests = {"slack-mcp": _manifest("slack"), "github-mcp": _manifest("github"),
                 "notes-mcp": SimpleNamespace(name="notes-mcp", credentials=SimpleNamespace(oauth={}, webhooks={}))}
    monkeypatch.setattr(mcp_registry, "get_manifest", manifests.get)
    monkeypatch.setattr(mcp_registry, "get_mcps_by_provider", lambda p: [manifests["github-mcp"]])
    github_meta = {"kind": "github_app", "hosted": True, "user_id": 7}
    monkeypatch.setattr(credential_store, "list_all_user_accounts", lambda sub: [
        {"mcp_name": "slack-mcp", "account_label": "work", "meta": {}},
        {"mcp_name": "github-mcp", "account_label": "gh", "meta": github_meta},
        {"mcp_name": "notes-mcp", "account_label": "n", "meta": {}},
    ])
    monkeypatch.setattr(credential_store, "accounts_by_github_user", lambda names, uid: [
        {"user_sub": "alice-sub", "mcp_name": "github-mcp", "account_label": "gh"},
        {"user_sub": "bob-sub", "mcp_name": "github-mcp", "account_label": "bobs"},
    ])
    relisted: list[str] = []
    monkeypatch.setattr(github_apps, "relist_at_relay", lambda sub, mcp, label: relisted.append(sub))
    unbound: list[tuple[str, bool]] = []

    async def unbind(provider_id, token_data, *, revoked, leaving_user_sub=""):
        assert leaving_user_sub == "alice-sub"
        unbound.append((provider_id, revoked))
    monkeypatch.setattr(relay_events, "unbind_account", unbind)
    for provider_id, label, data in (
        ("slack", "work", {"access_token": "xoxp", "extra": {"via_relay": True}}),
        ("github", "gh", {"access_token": "ghu", "refresh_token": "ghr",
                          "extra": {"via_relay": True, "app_kind": "github_app"}}),
    ):
        d = tmp_path / f"{provider_id}-tokens" / "alice"
        d.mkdir(parents=True)
        (d / f"{label}.json").write_text(json.dumps(data))

    providers = asyncio.run(account_release.release_user_accounts("alice-sub"))
    assert providers == {"github"}
    assert sorted(vendor.relayed) == [("github", "ghr"), ("slack", "xoxp")]
    assert not (tmp_path / "slack-tokens" / "alice" / "work.json").exists()
    assert not (tmp_path / "github-tokens" / "alice" / "gh.json").exists()
    assert sorted(unbound) == [("github", True), ("slack", True)]
    assert relisted == ["bob-sub"]


def _shared_login_rig(monkeypatch, tmp_path, *, bob_shares: bool):
    """alice is deleted; bob, another person, may hold the same Google login."""
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: "alice")
    monkeypatch.setattr(mcp_registry, "get_manifest", lambda n: _manifest("google"))
    monkeypatch.setattr(credential_store, "list_all_user_accounts", lambda sub: [
        {"mcp_name": "workspace-mcp", "account_label": "office", "meta": {}}])

    async def unbind(*a, **kw):
        return None
    monkeypatch.setattr(relay_events, "unbind_account", unbind)
    login = {"access_token": "ya29", "refresh_token": "1//r", "account_id": "office@example.com",
             "client_id": "c", "client_secret": "s", "extra": {}}
    base = tmp_path / "google-tokens"
    for user, data in (("alice", login),
                       ("bob", {**login, "account_id": "office@example.com" if bob_shares else "bob@example.com"})):
        (base / user).mkdir(parents=True)
        (base / user / "office.json").write_text(json.dumps(data))
    return base


def test_a_shared_login_is_not_revoked_when_one_holder_is_deleted(vendor, monkeypatch, tmp_path):
    """Revoking ends the vendor's grant behind the login: the other person's
    connection would stop at its next refresh, with no warning."""
    base = _shared_login_rig(monkeypatch, tmp_path, bob_shares=True)
    asyncio.run(account_release.release_user_accounts("alice-sub"))
    assert vendor.provider.revoked == [] and vendor.relayed == []
    assert not (base / "alice" / "office.json").exists()
    assert (base / "bob" / "office.json").exists()


def test_a_login_nobody_else_holds_is_revoked(vendor, monkeypatch, tmp_path):
    base = _shared_login_rig(monkeypatch, tmp_path, bob_shares=False)
    asyncio.run(account_release.release_user_accounts("alice-sub"))
    assert vendor.provider.revoked == ["1//r"]
    assert not (base / "alice" / "office.json").exists() and (base / "bob" / "office.json").exists()


def test_a_disconnect_skips_the_revoke_of_a_login_held_elsewhere(vendor, monkeypatch, tmp_path):
    base = _shared_login_rig(monkeypatch, tmp_path, bob_shares=True)
    own = base / "alice" / "office.json"
    data = json.loads(own.read_text())
    assert asyncio.run(account_release.revoke_account("google", data, _manifest("google"), path=own)) is False
    (base / "bob" / "office.json").unlink()
    assert asyncio.run(account_release.revoke_account("google", data, _manifest("google"), path=own)) is True
    assert vendor.provider.revoked == ["1//r"]


def test_a_github_app_token_is_revoked_even_when_the_person_holds_another(vendor, monkeypatch, tmp_path):
    """Its revoke ends that one token; the relay's unbind of the person is
    followed by the re-list of their other accounts."""
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    data = {"access_token": "ghu_1", "refresh_token": "ghr_1", "account_id": "1",
            "extra": {"via_relay": True, "app_kind": "github_app"}}
    base = tmp_path / "github-tokens"
    for user in ("alice", "work"):
        (base / user).mkdir(parents=True)
        (base / user / "gh.json").write_text(json.dumps(data))
    own = base / "alice" / "gh.json"
    assert asyncio.run(account_release.revoke_account("github", data, _manifest("github"), path=own)) is True
    assert vendor.relayed == [("github", "ghr_1")]


def test_a_person_without_a_username_releases_nothing(vendor, monkeypatch):
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: "")
    monkeypatch.setattr(credential_store, "list_all_user_accounts",
                        lambda sub: pytest.fail("no accounts read without a username"))
    assert asyncio.run(account_release.release_user_accounts("x")) == set()


def test_the_disconnect_unbinds_after_the_file_and_the_rows_go(monkeypatch, tmp_path):
    """Revoke, the subscriptions, the file, the rows, then the relay's
    route: the sharing check reads the files without this one."""
    from api.auth import oauth as oauth_api
    from core.remote import mcp_gateway_push
    from services.oauth import oauth_account_store
    from services.webhooks import subscription_manager
    data = {"access_token": "xoxp", "extra": {"via_relay": True}}
    order: list[str] = []
    own = tmp_path / "work.json"
    own.write_text(json.dumps(data))
    monkeypatch.setattr(credential_store, "get_account_meta", lambda *a: {})
    monkeypatch.setattr(oauth_account_store, "read_account_token", lambda d, label: dict(data))

    async def revoke(provider_id, token_data, manifest, *, path=None):
        order.append("revoke")
        return True
    monkeypatch.setattr(account_release, "revoke_account", revoke)

    async def cleanup(**kw):
        order.append("subscriptions")
        return 0
    monkeypatch.setattr(subscription_manager, "cleanup_account_subscriptions", cleanup)
    monkeypatch.setattr(credential_store, "list_service_agent_bindings", lambda mcp: [])
    monkeypatch.setattr(oauth_account_store, "delete_account_token",
                        lambda d, label: order.append("file") or own.unlink() or True)
    monkeypatch.setattr(credential_store, "delete_user_credentials", lambda *a: order.append("rows"))

    async def unbind(provider_id, token_data, *, revoked, leaving_user_sub=""):
        order.append(f"unbind:{provider_id}:{revoked}:{own.exists()}")
    monkeypatch.setattr(relay_events, "unbind_account", unbind)
    monkeypatch.setattr(mcp_gateway_push, "tick_soon", lambda: None)
    body = SimpleNamespace(mcp_name="slack-mcp", account_label="work")
    user = SimpleNamespace(sub="alice-sub")
    out = asyncio.run(oauth_api._disconnect_locked("slack", "slack", body, user, None, tmp_path))
    assert out == {"status": "ok", "kept_for": []}
    assert order == ["revoke", "subscriptions", "file", "rows", "unbind:slack:True:False"]

    # Another MCP of the provider still names the label: its file is the
    # sibling's too, so only this MCP's subscriptions and rows go.
    order.clear()
    own.write_text(json.dumps(data))
    monkeypatch.setattr(oauth_account_store, "other_mcps_naming", lambda *a: ["slack-two"])
    out = asyncio.run(oauth_api._disconnect_locked("slack", "slack", body, user, None, tmp_path))
    assert out == {"status": "ok", "kept_for": ["slack-two"]}
    assert order == ["subscriptions", "rows"] and own.exists()


def test_a_binding_clear_never_unbinds(monkeypatch):
    """A service binding lends the person's own account: clearing it ends
    the agent's use, never the account's route at the relay."""
    from services.agents import offboarding_bindings
    from services.webhooks import subscription_manager

    async def cleanup(**kw):
        return 0

    async def unbind(*a, **kw):
        pytest.fail("a binding clear must not unbind")
    monkeypatch.setattr(subscription_manager, "cleanup_account_subscriptions", cleanup)
    monkeypatch.setattr(credential_store, "remove_service_agent_binding", lambda *a, **kw: True)
    monkeypatch.setattr(relay_events, "unbind_account", unbind)
    asyncio.run(offboarding_bindings.clear_binding(
        {"mcp_name": "slack-mcp", "agent_name": "a1", "account_label": "work",
         "account_owner_sub": "alice-sub"}, "alice-sub"))
