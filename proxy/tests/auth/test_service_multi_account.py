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

from storage.identity import credential_store
from storage.pg import get_conn
from services.oauth import credential_resolver


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
        assert binding == ("acct-b", user_sub2)


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
        ) == ("b", user_sub2)

    def test_cleanup_empty_owner_is_noop(self):
        assert credential_store.cleanup_service_agent_bindings_for_owner("") == []


# ---------------------------------------------------------------------------
# Token map (dynamic_context) — agent scope resolves the bound user account
# ---------------------------------------------------------------------------


class TestTokenMapServiceBinding:
    def test_agent_scope_resolves_bound_user_account(self, mcp_name, user_sub):
        from storage.agents import agent_store
        from services.mcp import dynamic_context

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
            assert tokens["credential.GOOGLE_EMAIL"] == "support@org.com"
        finally:
            with get_conn() as conn:
                conn.execute("DELETE FROM agents WHERE slug=%s", (slug,))
                conn.commit()
            agent_store._invalidate_cache()

    def test_agent_scope_no_binding_yields_no_account(self, mcp_name):
        from storage.agents import agent_store
        from services.mcp import dynamic_context

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
