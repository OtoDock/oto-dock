"""An agent's own accounts of a tool, the store and the resolver
(user-account-binding model).

Platform "service accounts" were removed: each row of an agent's own set
(``service_agent_bindings``) ALWAYS points at a USER's own
``user_credential_accounts`` row (a manager lends a connected account to the
agent), the account itself or one of its installations; a set holds any
number of them, one its default. There is no platform tier and no default
fallback. Uses the real PostgreSQL test DB — no mocking.

Each test inserts under a unique ``mcp_name`` so rows don't collide.
"""

import uuid

import pytest

from services.oauth import credential_resolver
from storage.identity import credential_store, oauth_app_kinds, service_account_set
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
    """The lender's standing: a row is live while its owner manages the
    agent (the add route requires it; the store re-checks on every read)."""
    from storage import database as task_store
    task_store.add_user_agent(sub, agent, "manager", "test")


def _add(mcp: str, agent: str, label: str, owner_sub: str, **kw) -> str:
    """Add ``owner_sub``'s account to the agent's own set; why not, or ``""``."""
    return service_account_set.add_service_account_to_agent(
        mcp, agent, account_label=label, owner_sub=owner_sub, **kw)


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
        assert _add(mcp_name, "customer-support", "personal", user_sub) == ""

        ref = credential_resolver.pick_account(mcp_name, "customer-support")
        assert ref is not None
        assert ref.label == "personal"
        assert ref.owner_sub == user_sub
        # An unbound agent gets nothing (no platform default).
        assert credential_resolver.pick_account(mcp_name, "other-agent") is None

    def test_binding_unknown_account_label_rejected(self, mcp_name, user_sub):
        assert _add(mcp_name, "agent", "ghost", user_sub) == service_account_set.MISSING

    def test_binding_empty_owner_sub_rejected(self, mcp_name):
        # No platform tier — a row MUST name a real user account owner.
        assert _add(mcp_name, "agent", "whatever", "") == service_account_set.MISSING

    def test_binding_is_live_only_while_its_owner_manages_the_agent(self, mcp_name, user_sub):
        """A row lends the MANAGER's account (CREDENTIALS.md): once the
        lender is demoted or unassigned, the agent no longer acts with it; a
        platform admin manages every agent with no row."""
        from storage import database as task_store
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="personal",
        )
        _manages(user_sub, "customer-support")
        assert _add(mcp_name, "customer-support", "personal", user_sub) == ""
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
        assert _add(mcp_name, "voice-agent", "personal", user_sub) == ""
        assert credential_resolver.pick_account(mcp_name, "voice-agent") is not None
        credential_store.remove_service_agent_binding(mcp_name, "voice-agent")
        assert credential_resolver.pick_account(mcp_name, "voice-agent") is None


class TestTheAddValidatesTarget:
    """``add_service_account_to_agent`` validates against user_credential_accounts."""

    def test_validates_against_user_credential_accounts(self, mcp_name, user_sub):
        # No account yet → rejected.
        assert _add(mcp_name, "agent", "missing", user_sub) == service_account_set.MISSING
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="personal",
        )
        assert _add(mcp_name, "agent", "personal", user_sub) == ""


class TestAnotherUsersAccountJoinsTheSet:
    """A second person's account joins the agent's set beside the first;
    made the default, it is the account the agent reads."""

    def test_another_users_account_beside_and_then_the_default(self, mcp_name, user_sub, user_sub2):
        credential_store.set_user_credentials(
            user_sub, mcp_name, {"k": "v"}, account_label="acct-a",
        )
        credential_store.set_user_credentials(
            user_sub2, mcp_name, {"k": "v"}, account_label="acct-b",
        )
        _manages(user_sub, "agent")
        _manages(user_sub2, "agent")
        assert _add(mcp_name, "agent", "acct-a", user_sub) == ""
        assert _add(mcp_name, "agent", "acct-b", user_sub2) == ""
        assert _the_set(mcp_name, "agent") == [("acct-a", True), ("acct-b", False)]
        assert _add(mcp_name, "agent", "acct-b", user_sub2, make_default=True) == ""
        binding = credential_store.get_service_agent_binding(mcp_name, "agent")
        assert binding[:3] == ("acct-b", user_sub2, "")
        assert _the_set(mcp_name, "agent") == [("acct-b", True), ("acct-a", False)]


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
        assert _add(mcp_name, "agent-1", "a", user_sub) == ""
        assert _add(mcp_name, "agent-2", "b", user_sub2) == ""

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
            assert _add(mcp_name, slug, "support", user_sub) == ""

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

    def test_hosted_app_account_mcps(self, mcp_name, user_sub, user_sub2):
        hosted = credential_store.hosted_app_account_mcps
        credential_store.set_user_credentials(user_sub, mcp_name, {"k": "v"}, account_label="gh")
        assert mcp_name not in hosted()
        credential_store.set_account_meta(user_sub, mcp_name, "gh", _github_app_meta(_installation(77)))
        assert mcp_name not in hosted()                                    # self-managed
        credential_store.set_account_meta(user_sub, mcp_name, "gh", {**_github_app_meta(), "hosted": True})
        assert mcp_name in hosted()                                        # no installation needed
        assert mcp_name not in hosted(user_sub)                            # the person being deleted
        credential_store.set_user_credentials(user_sub2, mcp_name, {"k": "v"}, account_label="gh2")
        credential_store.set_account_meta(user_sub2, mcp_name, "gh2", {**_github_app_meta(), "hosted": True})
        assert mcp_name in hosted(user_sub)                                # someone else still holds one
        for sub, label in ((user_sub, "gh"), (user_sub2, "gh2")):
            credential_store.set_account_meta(sub, mcp_name, label, {"kind": oauth_app_kinds.KIND_OAUTH_APP,
                                                                     "hosted": True})
        assert mcp_name not in hosted()                                    # not a GitHub-App one

    def test_list_all_user_accounts(self, mcp_name, user_sub, user_sub2):
        other = f"{mcp_name}-b"
        try:
            credential_store.set_user_credentials(user_sub, mcp_name, {"k": "v"}, account_label="work")
            credential_store.set_user_credentials(user_sub, other, {"k": "v"}, account_label="home")
            credential_store.set_user_credentials(user_sub2, mcp_name, {"k": "v"}, account_label="theirs")
            credential_store.set_account_meta(user_sub, mcp_name, "work", {"kind": "x"})
            mine = credential_store.list_all_user_accounts(user_sub)
            assert [(a["mcp_name"], a["account_label"]) for a in mine] == [(mcp_name, "work"), (other, "home")]
            assert mine[0]["meta"] == {"kind": "x"} and mine[1]["meta"] == {}
            assert credential_store.list_all_user_accounts("local:nobody") == []
        finally:
            with get_conn() as conn:
                conn.execute("DELETE FROM user_credentials WHERE mcp_name=%s", (other,))
                conn.execute("DELETE FROM user_credential_accounts WHERE mcp_name=%s", (other,))
                conn.commit()

    def test_an_installation_row_is_stored_and_resolved(self, mcp_name, user_sub):
        """The store writes the installation the caller judged standing (the
        add route judges it, ``tests/auth/test_github_app_service_binding.py``)
        and the resolver hands it out."""
        credential_store.set_user_credentials(user_sub, mcp_name, {"k": "v"}, account_label="gh")
        credential_store.set_account_meta(user_sub, mcp_name, "gh", _github_app_meta(
            _installation(77), _installation(78, admin=False),
        ))
        _manages(user_sub, "agent")
        assert _add(mcp_name, "agent", "gh", user_sub, installation_id="77") == ""
        assert credential_store.get_service_agent_binding(mcp_name, "agent")[:3] == ("gh", user_sub, "77")
        ref = credential_resolver.pick_account(mcp_name, "agent")
        assert ref is not None and ref.installation_id == "77"
        assert credential_store.bindings_naming_installation([mcp_name], 77)[0]["agent_name"] == "agent"
        assert credential_store.bindings_naming_installation([mcp_name], 78) == []

    def test_a_rows_installation_is_never_changed_in_place(self, mcp_name, user_sub, user_sub2):
        credential_store.set_user_credentials(user_sub, mcp_name, {"k": "v"}, account_label="gh")
        credential_store.set_user_credentials(user_sub2, mcp_name, {"k": "v"}, account_label="other")
        credential_store.set_account_meta(user_sub, mcp_name, "gh", _github_app_meta(_installation(77)))
        _manages(user_sub, "agent")
        _manages(user_sub2, "agent")
        assert _add(mcp_name, "agent", "gh", user_sub, installation_id="77", repositories=["octo/a"]) == ""
        # The same label as itself or as another installation: refused, the row as it was.
        for iid in ("", "78"):
            assert _add(mcp_name, "agent", "gh", user_sub, installation_id=iid) == service_account_set.IDENTITY
        # The same way again keeps what the row holds, its reach included.
        assert _add(mcp_name, "agent", "gh", user_sub, installation_id="77", read_only=True) == ""
        assert credential_store.get_service_agent_binding(mcp_name, "agent")[:5] == (
            "gh", user_sub, "77", ("octo/a",), False)
        # The reach is changed in place only while the row still acts so.
        reach = service_account_set.set_service_reach
        assert reach(mcp_name, "agent", owner_sub=user_sub, account_label="gh", installation_id="",
                     repositories=[], read_only=True) == service_account_set.IDENTITY
        assert reach(mcp_name, "agent", owner_sub=user_sub2, account_label="gh", installation_id="77",
                     repositories=[], read_only=True) == service_account_set.MISSING
        assert reach(mcp_name, "agent", owner_sub=user_sub, account_label="gh", installation_id="77",
                     repositories=[" octo/a ", "octo/b", "octo/a"], read_only=True) == ""
        assert credential_store.get_service_agent_binding(mcp_name, "agent")[2:5] == ("77", ("octo/a", "octo/b"), True)
        # Another lender's account beside it acts as itself: an id never carries over.
        assert _add(mcp_name, "agent", "other", user_sub2) == ""
        assert credential_store.get_service_agent_binding(mcp_name, "agent", account_label="other")[:3] == (
            "other", user_sub2, "")
        # Taken out and added again as the account itself.
        assert credential_store.remove_service_agent_binding(mcp_name, "agent", owner_sub=user_sub, account_label="gh")
        assert _add(mcp_name, "agent", "gh", user_sub, installation_id="") == ""
        assert credential_store.get_service_agent_binding(mcp_name, "agent", account_label="gh")[:3] == (
            "gh", user_sub, "")

    def test_remove_matches_the_installation_too(self, mcp_name, user_sub):
        credential_store.set_user_credentials(user_sub, mcp_name, {"k": "v"}, account_label="gh")
        credential_store.set_account_meta(user_sub, mcp_name, "gh", _github_app_meta(_installation(77)))
        _manages(user_sub, "agent")
        assert _add(mcp_name, "agent", "gh", user_sub, installation_id="77") == ""
        assert credential_store.remove_service_agent_binding(
            mcp_name, "agent", owner_sub=user_sub, account_label="gh", installation_id="78") is False
        assert credential_store.remove_service_agent_binding(
            mcp_name, "agent", owner_sub=user_sub, account_label="gh", installation_id="77") is True
        assert credential_store.get_service_agent_binding(mcp_name, "agent") is None
        rows = credential_store.list_service_agent_bindings_for_owner(user_sub)
        assert all("installation_id" in r for r in rows)


# ---------------------------------------------------------------------------
# The agent's own set of a tool, and a Shared-only chat's choice among it
# ---------------------------------------------------------------------------


def _lend(sub: str, mcp: str, *labels: str, agent: str = "pool-agent") -> None:
    """``sub`` manages the agent and holds these accounts of the tool."""
    _manages(sub, agent)
    for label in labels:
        credential_store.set_user_credentials(sub, mcp, {"k": label}, account_label=label)


def _the_set(mcp: str, agent: str = "pool-agent", **kw) -> list[tuple[str, bool]]:
    return [(b.account_label, b.is_default)
            for b in service_account_set.list_service_agent_set(mcp, agent, **kw)]


@pytest.fixture
def pool_chat():
    from core.session.visibility import shared_chat_owner
    from storage import database as task_store
    chat_id = f"chat-{uuid.uuid4().hex[:12]}"
    task_store.create_chat(chat_id, shared_chat_owner("pool-agent"), "pool-agent")
    yield chat_id
    task_store.delete_chat(chat_id)


class TestTheAgentsOwnSet:
    def test_the_first_account_is_the_default_and_make_default_moves_it(self, mcp_name, user_sub):
        _lend(user_sub, mcp_name, "a", "b", "c")
        add = service_account_set.add_service_account_to_agent
        assert add(mcp_name, "pool-agent", account_label="a", owner_sub=user_sub) == ""
        assert add(mcp_name, "pool-agent", account_label="b", owner_sub=user_sub) == ""
        assert _the_set(mcp_name) == [("a", True), ("b", False)]
        assert add(mcp_name, "pool-agent", account_label="c", owner_sub=user_sub, make_default=True) == ""
        assert _the_set(mcp_name) == [("c", True), ("a", False), ("b", False)]
        assert service_account_set.set_service_default_account(mcp_name, "pool-agent", "b")
        assert _the_set(mcp_name)[0] == ("b", True)
        assert service_account_set.set_service_default_account(mcp_name, "pool-agent", "ghost") is False
        # The default is what every reader of the agent's account gets.
        assert credential_store.get_service_agent_binding(mcp_name, "pool-agent").account_label == "b"
        assert credential_resolver.pick_account(mcp_name, "pool-agent").label == "b"

    def test_a_label_another_lender_holds_a_missing_account_and_another_way_of_acting_are_refused(
            self, mcp_name, user_sub, user_sub2):
        _lend(user_sub, mcp_name, "default", "gh")
        _lend(user_sub2, mcp_name, "default", "other")
        add = service_account_set.add_service_account_to_agent
        assert add(mcp_name, "pool-agent", account_label="default", owner_sub=user_sub) == ""
        assert add(mcp_name, "pool-agent", account_label="default",
                   owner_sub=user_sub2) == service_account_set.TAKEN
        assert add(mcp_name, "pool-agent", account_label="ghost",
                   owner_sub=user_sub2) == service_account_set.MISSING
        assert add(mcp_name, "pool-agent", account_label="other", owner_sub="") == service_account_set.MISSING
        assert add(mcp_name, "pool-agent", account_label="other", owner_sub=user_sub2) == ""
        assert _the_set(mcp_name) == [("default", True), ("other", False)]
        with get_conn() as conn:
            conn.execute(
                "UPDATE service_agent_bindings SET installation_id='42' "
                "WHERE mcp_name=%s AND account_label='default'", (mcp_name,))
            conn.execute(
                "DELETE FROM service_agent_bindings WHERE mcp_name=%s AND account_label='other'",
                (mcp_name,))
            conn.commit()
        # An installation row takes other accounts beside it, never another way of acting.
        assert add(mcp_name, "pool-agent", account_label="default",
                   owner_sub=user_sub) == service_account_set.IDENTITY
        assert add(mcp_name, "pool-agent", account_label="gh", owner_sub=user_sub) == ""
        assert _the_set(mcp_name) == [("default", True), ("gh", False)]

    def test_a_lenders_removal_takes_their_row_alone_and_the_oldest_standing_row_is_promoted(
            self, mcp_name, user_sub, user_sub2):
        from storage import database as task_store
        _lend(user_sub, mcp_name, "a")
        _lend(user_sub2, mcp_name, "b", "c")
        add = service_account_set.add_service_account_to_agent
        for sub, label in ((user_sub, "a"), (user_sub2, "b"), (user_sub2, "c")):
            assert add(mcp_name, "pool-agent", account_label=label, owner_sub=sub) == ""
        # b's lender is demoted on the agent: the promote passes over b's rows.
        task_store.set_user_agent_role(user_sub2, "pool-agent", "editor")
        with get_conn() as conn:
            conn.execute("UPDATE service_agent_bindings SET set_at='2026-01-01' "
                         "WHERE mcp_name=%s AND account_label='b'", (mcp_name,))
            conn.commit()
        credential_store.set_user_credentials(user_sub, mcp_name, {"k": "d"}, account_label="d")
        assert add(mcp_name, "pool-agent", account_label="d", owner_sub=user_sub) == ""
        assert credential_store.remove_service_agent_binding(
            mcp_name, "pool-agent", owner_sub=user_sub, account_label="a")
        # The oldest row is b, but its lender does not stand: d is the default.
        assert _the_set(mcp_name, standing_only=False)[0] == ("d", True)
        assert _the_set(mcp_name) == [("d", True)]
        # A filter that names nobody's row removes nothing.
        assert not credential_store.remove_service_agent_binding(
            mcp_name, "pool-agent", owner_sub=user_sub, account_label="b")
        # No filter: the whole set.
        assert credential_store.remove_service_agent_binding(mcp_name, "pool-agent")
        assert _the_set(mcp_name, standing_only=False) == []

    def test_the_lenders_standing_is_read_before_the_sets_lock(self, mcp_name, user_sub, monkeypatch):
        """Reading a person's standing takes a connection of its own: under
        the lock that is a second checkout from the pool, so a burst of
        removals could wait on itself."""
        _lend(user_sub, mcp_name, "a", "b")
        for label in ("a", "b"):
            service_account_set.add_service_account_to_agent(
                mcp_name, "pool-agent", account_label=label, owner_sub=user_sub)
        order: list[str] = []
        real_lock, real_manages = credential_store._lock_service_set, credential_store._owner_manages
        monkeypatch.setattr(credential_store, "_lock_service_set",
                            lambda *a: (order.append("lock"), real_lock(*a))[1])
        monkeypatch.setattr(credential_store, "_owner_manages",
                            lambda *a: (order.append("standing"), real_manages(*a))[1])
        assert credential_store.remove_service_agent_binding(
            mcp_name, "pool-agent", owner_sub=user_sub, account_label="a")
        assert "standing" in order and order.index("lock") > max(
            i for i, what in enumerate(order) if what == "standing")
        assert _the_set(mcp_name) == [("b", True)]

    def test_an_account_added_again_keeps_the_chats_choices_of_it(self, mcp_name, user_sub, pool_chat):
        _lend(user_sub, mcp_name, "a", "b")
        for label in ("a", "b"):
            service_account_set.add_service_account_to_agent(
                mcp_name, "pool-agent", account_label=label, owner_sub=user_sub)
        assert service_account_set.set_chat_service_choice(pool_chat, "pool-agent", mcp_name, "b")
        assert service_account_set.add_service_account_to_agent(
            mcp_name, "pool-agent", account_label="b", owner_sub=user_sub, make_default=True) == ""
        assert _the_set(mcp_name) == [("b", True), ("a", False)]
        assert service_account_set.get_chat_service_choices(pool_chat) == {mcp_name: "b"}

    def test_a_deleted_lenders_rows_go_and_the_set_keeps_a_default(self, mcp_name, user_sub, user_sub2):
        _lend(user_sub, mcp_name, "a")
        _lend(user_sub2, mcp_name, "b")
        for sub, label in ((user_sub, "a"), (user_sub2, "b")):
            service_account_set.add_service_account_to_agent(
                mcp_name, "pool-agent", account_label=label, owner_sub=sub)
        gone = credential_store.cleanup_service_agent_bindings_for_owner(user_sub)
        assert [r["account_label"] for r in gone] == ["a"]
        assert _the_set(mcp_name) == [("b", True)]


class TestTheChatsChoiceOfTheAgentsAccount:
    def test_set_get_clear_and_a_label_outside_the_set_is_refused(self, mcp_name, user_sub, pool_chat):
        _lend(user_sub, mcp_name, "a", "b", "c")
        for label in ("a", "b"):
            service_account_set.add_service_account_to_agent(
                mcp_name, "pool-agent", account_label=label, owner_sub=user_sub)
        choices = service_account_set
        assert choices.get_chat_service_choices(pool_chat) == {}
        assert choices.set_chat_service_choice(pool_chat, "pool-agent", mcp_name, "b", set_by=user_sub)
        assert choices.get_chat_service_choices(pool_chat) == {mcp_name: "b"}
        assert choices.set_chat_service_choice(pool_chat, "pool-agent", mcp_name, "c") is False
        assert choices.set_chat_service_choice(pool_chat, "elsewhere", mcp_name, "a") is False
        assert choices.chats_choosing(mcp_name, "pool-agent", ["b"]) == [pool_chat]
        assert choices.chats_choosing(mcp_name, "pool-agent", ["a", "b"]) == [pool_chat]
        assert choices.chats_choosing(mcp_name, "pool-agent", ["a"]) == []
        assert choices.chats_choosing(mcp_name, "pool-agent", []) == []
        choices.clear_chat_service_choice(pool_chat, mcp_name)
        assert choices.get_chat_service_choices(pool_chat) == {}

    def test_it_goes_with_its_row_and_its_chat_and_never_comes_back(self, mcp_name, user_sub, pool_chat):
        from storage import database as task_store
        _lend(user_sub, mcp_name, "a", "b")
        for label in ("a", "b"):
            service_account_set.add_service_account_to_agent(
                mcp_name, "pool-agent", account_label=label, owner_sub=user_sub)
        choices = service_account_set
        assert choices.set_chat_service_choice(pool_chat, "pool-agent", mcp_name, "b")
        assert credential_store.remove_service_agent_binding(
            mcp_name, "pool-agent", owner_sub=user_sub, account_label="b")
        assert choices.get_chat_service_choices(pool_chat) == {}
        service_account_set.add_service_account_to_agent(
            mcp_name, "pool-agent", account_label="b", owner_sub=user_sub)
        assert choices.get_chat_service_choices(pool_chat) == {}
        assert choices.set_chat_service_choice(pool_chat, "pool-agent", mcp_name, "b")
        task_store.delete_chat(pool_chat)
        with get_conn() as conn:
            assert conn.execute("SELECT 1 FROM chat_service_account_choices WHERE chat_id=%s",
                                (pool_chat,)).fetchone() is None
