"""Service-scope binding + resolver tests (user-account-binding model).

Platform "service accounts" were removed: a per-agent binding
(``service_agent_bindings``) now ALWAYS points at a USER's own
``user_credential_accounts`` row (a manager designates their connected
account as the agent's service identity). There is no platform tier and no
default fallback. Uses the real PostgreSQL test DB — no mocking.

Each test inserts under a unique ``mcp_name`` so rows don't collide.
"""

import uuid

import pytest

from services.oauth import credential_resolver
from storage.identity import credential_store, oauth_app_kinds
from storage.pg import get_conn


@pytest.fixture
def mcp_name(request):
    """Unique MCP name per test; cleans up service-scope rows on teardown."""
    name = f"test-svc-mcp-{uuid.uuid4().hex[:8]}"

    def cleanup():
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM service_agent_bindings WHERE mcp_name=%s", (name,),
            )
            conn.execute(
                "DELETE FROM user_credentials WHERE mcp_name=%s", (name,),
            )
            conn.execute(
                "DELETE FROM user_credential_accounts WHERE mcp_name=%s", (name,),
            )
            conn.commit()

    request.addfinalizer(cleanup)
    return name


def _make_user(request):
    """Create a real user row + thorough cleanup; return the sub."""
    sub = f"local:test-{uuid.uuid4().hex[:8]}"
    username = f"u{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, role, created_at, last_login, "
            "username, display_name, auth_provider) "
            "VALUES (%s, %s, 'Test User', 'member', NOW()::text, NOW()::text, "
            "%s, 'Test', 'local')",
            (sub, f"{username}@example.com", username),
        )
        conn.commit()

    def cleanup():
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM service_agent_bindings WHERE account_owner_sub=%s", (sub,),
            )
            conn.execute("DELETE FROM user_credentials WHERE user_sub=%s", (sub,))
            conn.execute(
                "DELETE FROM user_credential_accounts WHERE user_sub=%s", (sub,),
            )
            conn.execute("DELETE FROM users WHERE sub=%s", (sub,))
            conn.commit()

    request.addfinalizer(cleanup)
    return sub


@pytest.fixture
def user_sub(request):
    return _make_user(request)


@pytest.fixture
def user_sub2(request):
    return _make_user(request)


def _manages(sub: str, agent: str) -> None:
    """The lender's standing: a binding is live while its owner manages the
    agent (the bind route requires it; the store re-checks on every read)."""
    from storage import database as task_store
    task_store.add_user_agent(sub, agent, "manager", "test")


# ---------------------------------------------------------------------------
# pick_account — service scope (binding-only; NO platform fallback)
# ---------------------------------------------------------------------------


class TestPickAccountServiceScope:
    def test_service_scope_no_binding_returns_none(self, mcp_name):
        # No binding → None. There is no platform default to fall back on.
        assert credential_resolver.pick_account(mcp_name, "agent") is None

    def test_pick_account_signature_user_sub_keyword(self, mcp_name):
        """pick_account requires user_sub as kw-only — positional 3rd arg raises."""
        with pytest.raises(TypeError):
            credential_resolver.pick_account(mcp_name, "agent", "some-sub")


# ---------------------------------------------------------------------------
# Per-agent binding to a user's own account
# ---------------------------------------------------------------------------


class TestServiceAgentBinding:
    def test_binding_returns_bound_user_account(self, mcp_name, user_sub):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="personal",
        )
        _manages(user_sub, "customer-support")
        ok = credential_store.set_service_agent_binding(
            mcp_name, "customer-support", account_label="personal",
            owner_sub=user_sub,
        )
        assert ok is True

        ref = credential_resolver.pick_account(mcp_name, "customer-support")
        assert ref is not None
        assert ref.label == "personal"
        assert ref.owner_sub == user_sub
        # An unbound agent gets nothing (no platform default).
        assert credential_resolver.pick_account(mcp_name, "other-agent") is None

    def test_binding_unknown_account_label_rejected(self, mcp_name, user_sub):
        ok = credential_store.set_service_agent_binding(
            mcp_name, "agent", account_label="ghost", owner_sub=user_sub,
        )
        assert ok is False

    def test_binding_empty_owner_sub_rejected(self, mcp_name):
        # No platform tier — a binding MUST name a real user account owner.
        ok = credential_store.set_service_agent_binding(
            mcp_name, "agent", account_label="whatever", owner_sub="",
        )
        assert ok is False

    def test_binding_is_live_only_while_its_owner_manages_the_agent(self, mcp_name, user_sub):
        """The binding lends the MANAGER's account (CREDENTIALS.md): once the
        lender is demoted or unassigned, the agent no longer acts with it; a
        platform admin manages every agent with no row."""
        from storage import database as task_store
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="personal",
        )
        _manages(user_sub, "customer-support")
        assert credential_store.set_service_agent_binding(
            mcp_name, "customer-support", account_label="personal", owner_sub=user_sub,
        )
        assert credential_resolver.pick_account(mcp_name, "customer-support").owner_sub == user_sub
        task_store.set_user_agent_role(user_sub, "customer-support", "editor")
        assert credential_resolver.pick_account(mcp_name, "customer-support") is None
        assert credential_store.get_service_agent_binding(mcp_name, "customer-support") is None
        task_store.set_user_agents(user_sub, [], "test")
        assert credential_resolver.pick_account(mcp_name, "customer-support") is None
        with get_conn() as conn:
            conn.execute("UPDATE users SET role='admin' WHERE sub=%s", (user_sub,))
            conn.commit()
        assert credential_resolver.pick_account(mcp_name, "customer-support").label == "personal"

    def test_remove_binding(self, mcp_name, user_sub):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="personal",
        )
        _manages(user_sub, "voice-agent")
        credential_store.set_service_agent_binding(
            mcp_name, "voice-agent", account_label="personal", owner_sub=user_sub,
        )
        assert credential_resolver.pick_account(mcp_name, "voice-agent") is not None
        credential_store.remove_service_agent_binding(mcp_name, "voice-agent")
        assert credential_resolver.pick_account(mcp_name, "voice-agent") is None


class TestSetServiceAgentBindingValidatesTarget:
    """``set_service_agent_binding`` validates against user_credential_accounts."""

    def test_validates_against_user_credential_accounts(self, mcp_name, user_sub):
        # No account yet → rejected.
        ok = credential_store.set_service_agent_binding(
            mcp_name, "agent", account_label="missing", owner_sub=user_sub,
        )
        assert ok is False
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="personal",
        )
        ok = credential_store.set_service_agent_binding(
            mcp_name, "agent", account_label="personal", owner_sub=user_sub,
        )
        assert ok is True


class TestSetServiceAgentBindingUniquePerAgent:
    """Second PUT for the same (mcp, agent) replaces the binding."""

    def test_replace_with_another_users_account(self, mcp_name, user_sub, user_sub2):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="acct-a",
        )
        credential_store.set_user_credentials(
            user_sub2, mcp_name, {"k": "v"}, account_label="acct-b",
        )
        _manages(user_sub, "agent")
        _manages(user_sub2, "agent")
        credential_store.set_service_agent_binding(
            mcp_name, "agent", account_label="acct-a", owner_sub=user_sub,
        )
        credential_store.set_service_agent_binding(
            mcp_name, "agent", account_label="acct-b", owner_sub=user_sub2,
        )
        binding = credential_store.get_service_agent_binding(mcp_name, "agent")
        assert binding[:3] == ("acct-b", user_sub2, "")


class TestCleanupServiceAgentBindingsForOwner:
    """User-delete cascade drops bindings pointing at the deleted user's
    accounts; other users' bindings are untouched."""

    def test_cleanup_drops_only_target_owner_bindings(
        self, mcp_name, user_sub, user_sub2,
    ):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="a",
        )
        credential_store.set_user_credentials(
            user_sub2, mcp_name, {"k": "v"}, account_label="b",
        )
        _manages(user_sub, "agent-1")
        _manages(user_sub2, "agent-2")
        credential_store.set_service_agent_binding(
            mcp_name, "agent-1", account_label="a", owner_sub=user_sub,
        )
        credential_store.set_service_agent_binding(
            mcp_name, "agent-2", account_label="b", owner_sub=user_sub2,
        )

        snapshot = credential_store.cleanup_service_agent_bindings_for_owner(user_sub)
        assert len(snapshot) == 1
        assert snapshot[0]["agent_name"] == "agent-1"
        assert credential_store.get_service_agent_binding(mcp_name, "agent-1") is None
        assert credential_store.get_service_agent_binding(
            mcp_name, "agent-2",
        )[:3] == ("b", user_sub2, "")

    def test_cleanup_empty_owner_is_noop(self):
        assert credential_store.cleanup_service_agent_bindings_for_owner("") == []


# ---------------------------------------------------------------------------
# Token map (dynamic_context) — agent scope resolves the bound user account
# ---------------------------------------------------------------------------


class TestTokenMapServiceBinding:
    def test_agent_scope_resolves_bound_user_account(self, mcp_name, user_sub):
        from services.mcp import dynamic_context
        from storage.agents import agent_store

        slug = f"svc-agent-{uuid.uuid4().hex[:6]}"
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO agents (slug, display_name, execution_path, "
                "created_at, updated_at) "
                "VALUES (%s, %s, 'claude-code-cli', NOW()::text, NOW()::text)",
                (slug, "Customer Support"),
            )
            conn.commit()
        agent_store._invalidate_cache()

        try:
            credential_store.set_user_credentials(
                user_sub, mcp_name, {"GOOGLE_EMAIL": "support@org.com"},
                account_label="support",
            )
            credential_store.set_account_display_email(
                user_sub, mcp_name, "support", "support@org.com",
            )
            _manages(user_sub, slug)
            credential_store.set_service_agent_binding(
                mcp_name, slug, account_label="support", owner_sub=user_sub,
            )

            tokens = dynamic_context._build_token_map(
                mcp_name, slug, user_sub="", user_role="", session_ctx={},
            )
            assert tokens["account.label"] == "support"
            assert tokens["account.email"] == "support@org.com"
            assert not any(k.startswith("credential.") for k in tokens)
        finally:
            with get_conn() as conn:
                conn.execute("DELETE FROM agents WHERE slug=%s", (slug,))
                conn.commit()
            agent_store._invalidate_cache()

    def test_agent_scope_no_binding_yields_no_account(self, mcp_name):
        from services.mcp import dynamic_context
        from storage.agents import agent_store

        slug = f"svc-agent-{uuid.uuid4().hex[:6]}"
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO agents (slug, display_name, execution_path, "
                "created_at, updated_at) "
                "VALUES (%s, %s, 'claude-code-cli', NOW()::text, NOW()::text)",
                (slug, "Voice Agent"),
            )
            conn.commit()
        agent_store._invalidate_cache()

        try:
            tokens = dynamic_context._build_token_map(
                mcp_name, slug, user_sub="", user_role="", session_ctx={},
            )
            # No binding → no account.* tokens populated (agent.* still are).
            assert "account.label" not in tokens
        finally:
            with get_conn() as conn:
                conn.execute("DELETE FROM agents WHERE slug=%s", (slug,))
                conn.commit()
            agent_store._invalidate_cache()


# ---------------------------------------------------------------------------
# An installation of a GitHub-App account as the identity
# ---------------------------------------------------------------------------


def _github_app_meta(*installations: dict) -> dict:
    return {"kind": oauth_app_kinds.KIND_GITHUB_APP, "login": "alice", "user_id": 1,
            "installations": list(installations)}


def _installation(iid: int, *, admin: bool = True, suspended: bool = False) -> dict:
    return {"id": iid, "account": "octo", "account_id": 9, "target_type": "Organization",
            "repository_selection": "selected", "repositories": ["octo/a"], "permissions": {},
            "suspended_at": "2026-10-15T00:00:00Z" if suspended else None, "admin": admin}


class TestInstallationBinding:
    def test_the_meta_round_trips(self, mcp_name, user_sub):
        credential_store.set_user_credentials(user_sub, mcp_name, {"k": "v"}, account_label="gh")
        assert credential_store.get_account_meta(user_sub, mcp_name, "gh") == {}
        assert credential_store.list_user_accounts(user_sub, mcp_name)[0]["meta"] == {}
        meta = _github_app_meta(_installation(77))
        assert credential_store.set_account_meta(user_sub, mcp_name, "gh", meta) is True
        assert credential_store.get_account_meta(user_sub, mcp_name, "gh") == meta
        assert credential_store.list_user_accounts(user_sub, mcp_name)[0]["meta"] == meta
        assert credential_store.accounts_holding_installation([mcp_name], 77)[0]["account_label"] == "gh"
        assert credential_store.accounts_holding_installation([mcp_name], 78) == []
        assert credential_store.accounts_by_github_user([mcp_name], 1)[0]["user_sub"] == user_sub
        assert credential_store.accounts_by_github_user([mcp_name], 2) == []
        written = credential_store.update_account_meta(
            user_sub, mcp_name, "gh", lambda m: {**m, "installations": []},
        )
        assert written == {**meta, "installations": []}
        assert credential_store.update_account_meta(user_sub, mcp_name, "gh", lambda m: None) == written
        assert credential_store.set_account_meta(user_sub, mcp_name, "gh", None) is True
        assert credential_store.get_account_meta(user_sub, mcp_name, "gh") == {}
        assert credential_store.set_account_meta(user_sub, mcp_name, "ghost", meta) is False
        assert credential_store.update_account_meta(user_sub, mcp_name, "ghost", lambda m: m) is None

    def test_any_hosted_app_account(self, mcp_name, user_sub):
        credential_store.set_user_credentials(user_sub, mcp_name, {"k": "v"}, account_label="gh")
        assert credential_store.any_hosted_app_account([mcp_name]) is False
        credential_store.set_account_meta(user_sub, mcp_name, "gh", _github_app_meta(_installation(77)))
        assert credential_store.any_hosted_app_account([mcp_name]) is False     # self-managed
        credential_store.set_account_meta(user_sub, mcp_name, "gh", {**_github_app_meta(), "hosted": True})
        assert credential_store.any_hosted_app_account([mcp_name]) is True     # no installation needed
        assert credential_store.any_hosted_app_account(["another-mcp"]) is False
        assert credential_store.any_hosted_app_account([]) is False
        credential_store.set_account_meta(user_sub, mcp_name, "gh", {"kind": oauth_app_kinds.KIND_OAUTH_APP,
                                                                     "hosted": True})
        assert credential_store.any_hosted_app_account([mcp_name]) is False     # not a GitHub-App one

    def test_an_installation_binds_only_while_it_stands(self, mcp_name, user_sub):
        credential_store.set_user_credentials(user_sub, mcp_name, {"k": "v"}, account_label="gh")
        _manages(user_sub, "agent")
        bind = lambda iid: credential_store.set_service_agent_binding(
            mcp_name, "agent", account_label="gh", owner_sub=user_sub, installation_id=iid,
        )
        # Not a GitHub-App account yet: no installation stands.
        assert bind("77") is False
        credential_store.set_account_meta(user_sub, mcp_name, "gh", _github_app_meta(
            _installation(77), _installation(78, admin=False), _installation(79, suspended=True),
        ))
        assert bind("77") is True
        assert credential_store.get_service_agent_binding(mcp_name, "agent")[:3] == ("gh", user_sub, "77")
        ref = credential_resolver.pick_account(mcp_name, "agent")
        assert ref is not None and ref.installation_id == "77"
        assert bind("78") is False     # reachable, not owned
        assert bind("79") is False     # suspended
        assert bind("80") is False     # not among the person's
        assert credential_store.get_service_agent_binding(mcp_name, "agent").installation_id == "77"
        assert credential_store.bindings_naming_installation([mcp_name], 77)[0]["agent_name"] == "agent"
        assert credential_store.bindings_naming_installation([mcp_name], 78) == []

    def test_a_put_without_the_id_keeps_it_and_an_empty_one_clears_it(self, mcp_name, user_sub, user_sub2):
        credential_store.set_user_credentials(user_sub, mcp_name, {"k": "v"}, account_label="gh")
        credential_store.set_user_credentials(user_sub2, mcp_name, {"k": "v"}, account_label="other")
        credential_store.set_account_meta(user_sub, mcp_name, "gh", _github_app_meta(_installation(77)))
        _manages(user_sub, "agent")
        _manages(user_sub2, "agent")
        assert credential_store.set_service_agent_binding(
            mcp_name, "agent", account_label="gh", owner_sub=user_sub, installation_id="77")
        # An older dashboard re-selects the label: the installation stays.
        assert credential_store.set_service_agent_binding(mcp_name, "agent", account_label="gh", owner_sub=user_sub)
        assert credential_store.get_service_agent_binding(mcp_name, "agent").installation_id == "77"
        # Another lender's account: a stored id never carries over.
        assert credential_store.set_service_agent_binding(mcp_name, "agent", account_label="other", owner_sub=user_sub2)
        assert credential_store.get_service_agent_binding(mcp_name, "agent")[:3] == ("other", user_sub2, "")
        assert credential_store.set_service_agent_binding(
            mcp_name, "agent", account_label="gh", owner_sub=user_sub, installation_id="77")
        assert credential_store.set_service_agent_binding(
            mcp_name, "agent", account_label="gh", owner_sub=user_sub, installation_id="")
        assert credential_store.get_service_agent_binding(mcp_name, "agent")[:3] == ("gh", user_sub, "")

    def test_remove_matches_the_installation_too(self, mcp_name, user_sub):
        credential_store.set_user_credentials(user_sub, mcp_name, {"k": "v"}, account_label="gh")
        credential_store.set_account_meta(user_sub, mcp_name, "gh", _github_app_meta(_installation(77)))
        _manages(user_sub, "agent")
        assert credential_store.set_service_agent_binding(
            mcp_name, "agent", account_label="gh", owner_sub=user_sub, installation_id="77")
        assert credential_store.remove_service_agent_binding(
            mcp_name, "agent", owner_sub=user_sub, account_label="gh", installation_id="78") is False
        assert credential_store.remove_service_agent_binding(
            mcp_name, "agent", owner_sub=user_sub, account_label="gh", installation_id="77") is True
        assert credential_store.get_service_agent_binding(mcp_name, "agent") is None
        rows = credential_store.list_service_agent_bindings_for_owner(user_sub)
        assert all("installation_id" in r for r in rows)
