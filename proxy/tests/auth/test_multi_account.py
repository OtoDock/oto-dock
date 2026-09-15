"""Multi-account credential storage + resolver tests.

Exercises every credential_store account helper plus
``credential_resolver.pick_account``. Uses the real PostgreSQL test
database (same connection that pytest already initializes via schema
migration) — no mocking — because the partial unique index +
constraint behavior is exactly what we want to verify.

Each test inserts under unique user_sub / mcp_name keys so they don't
collide with other tests' data and don't need teardown.
"""

import uuid
import pytest

from storage.identity import credential_store
from storage.pg import get_conn
from services.oauth import credential_resolver


def _fresh_user_sub() -> str:
    """Per-test user identity so rows don't collide with other tests."""
    return f"test-user-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def user_sub(request):
    """Insert a real users row + clean up on teardown.

    The `user_credentials` table has a FOREIGN KEY to users(sub) so we
    can't just invent a sub.
    """
    sub = _fresh_user_sub()
    username = f"u{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, username, role, "
            "auth_provider, created_at, last_login) "
            "VALUES (%s, %s, 'Test', %s, 'creator', 'local', "
            "NOW()::text, NOW()::text)",
            (sub, f"{username}@example.test", username),
        )
        conn.commit()

    def cleanup():
        with get_conn() as conn:
            # Cascade via FK on user_sub.
            conn.execute("DELETE FROM users WHERE sub = %s", (sub,))
            conn.commit()
    request.addfinalizer(cleanup)
    return sub


@pytest.fixture
def mcp_name():
    """Unique MCP name per test so account rows don't collide."""
    return f"test-mcp-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Account-list CRUD + auto-create on first credential set
# ---------------------------------------------------------------------------


class TestAccountAutoCreate:
    def test_set_credentials_auto_creates_account(self, user_sub, mcp_name):
        # No accounts yet.
        assert credential_store.list_user_accounts(user_sub, mcp_name) == []

        # Setting credentials auto-creates the account row.
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"EMAIL_USER": "alice@x.com"},
            account_label="default",
        )
        accounts = credential_store.list_user_accounts(user_sub, mcp_name)
        assert len(accounts) == 1
        assert accounts[0]["account_label"] == "default"
        # First account is automatically the default.
        assert accounts[0]["is_default"] is True

    def test_second_account_does_not_steal_default(self, user_sub, mcp_name):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"EMAIL_USER": "alice@x.com"},
            account_label="work",
        )
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"EMAIL_USER": "bob@x.com"},
            account_label="personal",
        )
        accounts = {a["account_label"]: a for a in
                    credential_store.list_user_accounts(user_sub, mcp_name)}
        assert accounts["work"]["is_default"] is True
        assert accounts["personal"]["is_default"] is False


# ---------------------------------------------------------------------------
# Default-account toggling enforces the partial unique index
# ---------------------------------------------------------------------------


class TestDefaultAccount:
    def test_set_default_flips_correctly(self, user_sub, mcp_name):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v1"}, account_label="A",
        )
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v2"}, account_label="B",
        )
        # A was first → default.
        assert credential_store.get_default_account(user_sub, mcp_name) == "A"

        # Flip to B.
        ok = credential_store.set_default_account(user_sub, mcp_name, "B")
        assert ok is True
        assert credential_store.get_default_account(user_sub, mcp_name) == "B"

        # Old default is no longer default (partial unique index enforced).
        accounts = {a["account_label"]: a for a in
                    credential_store.list_user_accounts(user_sub, mcp_name)}
        assert accounts["A"]["is_default"] is False
        assert accounts["B"]["is_default"] is True

    def test_set_default_unknown_label_returns_false(self, user_sub, mcp_name):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="real",
        )
        ok = credential_store.set_default_account(
            user_sub, mcp_name, "ghost",
        )
        assert ok is False
        # Real account is still default — unaffected.
        assert credential_store.get_default_account(user_sub, mcp_name) == "real"


# ---------------------------------------------------------------------------
# Per-agent binding
# ---------------------------------------------------------------------------


class TestAgentBinding:
    def test_binding_overrides_default(self, user_sub, mcp_name):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "default-v"}, account_label="def",
        )
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "work-v"}, account_label="work",
        )
        ok = credential_store.set_account_agent_binding(
            user_sub, mcp_name, "agent-A", "work",
        )
        assert ok is True

        # pick_account returns the bound account for the bound agent...
        assert credential_resolver.pick_account(
            mcp_name, "agent-A", user_sub=user_sub,
        ).label == "work"
        # ...and the default for any other agent.
        assert credential_resolver.pick_account(
            mcp_name, "agent-B", user_sub=user_sub,
        ).label == "def"

    def test_binding_unknown_account_label_rejected(self, user_sub, mcp_name):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="real",
        )
        ok = credential_store.set_account_agent_binding(
            user_sub, mcp_name, "agent", "ghost",
        )
        assert ok is False

    def test_binding_upsert_replaces_previous(self, user_sub, mcp_name):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="A",
        )
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="B",
        )
        credential_store.set_account_agent_binding(
            user_sub, mcp_name, "agent", "A",
        )
        credential_store.set_account_agent_binding(
            user_sub, mcp_name, "agent", "B",
        )
        # Single row per (user, mcp, agent) — upserted, not duplicated.
        bindings = credential_store.list_agent_account_bindings(
            user_sub, mcp_name,
        )
        assert len(bindings) == 1
        assert bindings[0]["account_label"] == "B"

    def test_remove_binding_reverts_to_default(self, user_sub, mcp_name):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="A",
        )
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="B",
        )
        credential_store.set_account_agent_binding(
            user_sub, mcp_name, "agent", "B",
        )
        credential_store.remove_account_agent_binding(
            user_sub, mcp_name, "agent",
        )
        # Falls back to default (A, since A was first).
        assert credential_resolver.pick_account(
            mcp_name, "agent", user_sub=user_sub,
        ).label == "A"


# ---------------------------------------------------------------------------
# Delete cascade
# ---------------------------------------------------------------------------


class TestDeleteCascade:
    def test_delete_account_label_removes_binding_too(self, user_sub, mcp_name):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="A",
        )
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="B",
        )
        credential_store.set_account_agent_binding(
            user_sub, mcp_name, "agent", "B",
        )
        credential_store.delete_user_credentials(user_sub, mcp_name, "B")

        # Account row, credentials, and binding all gone.
        labels = {a["account_label"] for a in
                  credential_store.list_user_accounts(user_sub, mcp_name)}
        assert labels == {"A"}
        assert credential_store.get_account_agent_binding(
            user_sub, mcp_name, "agent",
        ) is None

# ---------------------------------------------------------------------------
# pick_account resolution order
# ---------------------------------------------------------------------------


class TestPickAccount:
    def test_no_accounts_returns_none(self, user_sub, mcp_name):
        assert credential_resolver.pick_account(
            mcp_name, "agent", user_sub=user_sub,
        ) is None

    def test_only_default_returned_without_explicit_binding(
        self, user_sub, mcp_name,
    ):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="def",
        )
        assert credential_resolver.pick_account(
            mcp_name, "agent", user_sub=user_sub,
        ).label == "def"

    def test_explicit_binding_takes_precedence(self, user_sub, mcp_name):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="A",
        )
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="B",
        )
        credential_store.set_account_agent_binding(
            user_sub, mcp_name, "agent", "B",
        )
        # Default is A; binding pins to B for this agent.
        assert credential_store.get_default_account(user_sub, mcp_name) == "A"
        assert credential_resolver.pick_account(
            mcp_name, "agent", user_sub=user_sub,
        ).label == "B"
