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
from unittest.mock import ANY

import pytest

from services.oauth import credential_resolver
from storage.identity import credential_store
from storage.pg import get_conn


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

    def test_a_binding_carries_the_persons_reach_and_a_rebind_keeps_it(self, user_sub, mcp_name):
        for label in ("A", "B"):
            credential_store.set_user_credentials(user_sub, mcp_name, {"k": "v"}, account_label=label)
        assert credential_store.set_account_agent_binding(
            user_sub, mcp_name, "agent", "A", repositories=[" octo/a ", "octo/a", "octo/b"], read_only=True)
        row = credential_store.get_account_agent_binding(user_sub, mcp_name, "agent")
        assert row == ("A", ("octo/a", "octo/b"), True)
        # A client that sends no reach never widens it, on any account.
        assert credential_store.set_account_agent_binding(user_sub, mcp_name, "agent", "B")
        assert credential_store.get_account_agent_binding(user_sub, mcp_name, "agent") == (
            "B", ("octo/a", "octo/b"), True)
        assert credential_store.list_agent_account_bindings(user_sub, mcp_name) == [{
            "agent_name": "agent", "account_label": "B", "set_at": ANY,
            "repositories": ["octo/a", "octo/b"], "read_only": True}]
        assert credential_store.set_account_agent_binding(
            user_sub, mcp_name, "agent", "B", repositories=[], read_only=False)
        assert credential_store.get_account_agent_binding(user_sub, mcp_name, "agent") == ("B", (), False)

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


# ---------------------------------------------------------------------------
# The label a sign-in lands on (oauth_account_store.account_label_for): one
# login per token file, so choosing an account for one MCP never moves
# another MCP of the provider onto another login.
# ---------------------------------------------------------------------------

class TestSigninLabel:
    def _file(self, d, label, account_id, *, issued="app", client_id="cid", extra=None):
        import json
        body = {"provider": "facebook", "account_id": account_id, "client_id": client_id,
                "access_token": "x", "extra": dict(extra or {})}
        if issued == "relay":
            body["extra"]["via_relay"] = True
        (d / f"{label}.json").write_text(json.dumps(body))

    def _pick(self, d, *, hint="", own="Dimitris M", identity="fb-2", email="Dimitris M",
              issued="app:cid", named=()):
        from services.oauth import oauth_account_store as store
        return store.account_label_for(d, hint=hint, identity_label=own, identity=identity, email=email,
                                       issued=issued, named_elsewhere=lambda label: label in named)

    def test_the_same_login_shares_one_file(self, tmp_path):
        self._file(tmp_path, "Dimitris M", "fb-1")
        assert self._pick(tmp_path, identity="fb-1", named=("Dimitris M",)) == "Dimitris M"

    def test_another_login_with_the_same_name_gets_a_number(self, tmp_path):
        self._file(tmp_path, "Dimitris M", "fb-1")
        assert self._pick(tmp_path) == "Dimitris M 2"
        self._file(tmp_path, "Dimitris M 2", "fb-2")
        assert self._pick(tmp_path) == "Dimitris M 2"
        assert self._pick(tmp_path, identity="fb-3") == "Dimitris M 3"

    def test_a_login_already_on_file_under_another_label_lands_there(self, tmp_path):
        self._file(tmp_path, "Dimitris M", "fb-1")
        self._file(tmp_path, "Work", "fb-2")
        assert self._pick(tmp_path) == "Work"

    def test_a_reconnect_keeps_its_card_unless_another_mcp_shares_it(self, tmp_path):
        self._file(tmp_path, "Dimitris M", "fb-1")
        # only this MCP names it: the card is rewritten, as before
        assert self._pick(tmp_path, hint="Dimitris M") == "Dimitris M"
        # a sibling MCP names it: the other login goes to its own label
        assert self._pick(tmp_path, hint="Dimitris M", named=("Dimitris M",)) == "Dimitris M 2"
        # the same login reconnects the shared card
        assert self._pick(tmp_path, hint="Dimitris M", identity="fb-1", named=("Dimitris M",)) == "Dimitris M"

    def test_a_reconnect_takes_its_cards_missing_file(self, tmp_path):
        assert self._pick(tmp_path, hint="Dimitris M", named=("Dimitris M",)) == "Dimitris M"
        assert self._pick(tmp_path) == "Dimitris M"

    def test_an_add_never_lands_on_a_siblings_dangling_row(self, tmp_path):
        """A sibling's row whose file is gone (a disconnect before 1.9.1 left
        it): the new login is numbered, and the row waits for its own card's
        Reconnect, which takes it back."""
        assert self._pick(tmp_path, named=("Dimitris M",)) == "Dimitris M 2"
        self._file(tmp_path, "Dimitris M 2", "fb-2")
        assert self._pick(tmp_path, identity="fb-2", named=("Dimitris M",)) == "Dimitris M 2"
        assert self._pick(tmp_path, hint="Dimitris M", identity="fb-1", named=("Dimitris M",)) == "Dimitris M"

    def test_a_file_issued_another_way_is_kept_from_a_sibling(self, tmp_path):
        self._file(tmp_path, "Dimitris M", "fb-1", issued="relay")
        # app-scoped ids of two apps cannot be compared: only this MCP holds it, overwrite
        assert self._pick(tmp_path, identity="fb-1") == "Dimitris M"
        # a sibling holds it: never overwritten
        assert self._pick(tmp_path, identity="fb-1", named=("Dimitris M",)) == "Dimitris M 2"

    def test_no_vendor_id_keeps_the_label_unless_a_sibling_holds_it(self, tmp_path):
        self._file(tmp_path, "postiz", "postiz")
        assert self._pick(tmp_path, own="postiz", identity="", email="postiz") == "postiz"
        assert self._pick(tmp_path, own="postiz", identity="", email="postiz", named=("postiz",)) == "postiz 2"

    def test_a_legacy_file_that_recorded_the_email_is_the_same_login(self, tmp_path):
        self._file(tmp_path, "a@b.example", "a@b.example")
        assert self._pick(tmp_path, own="a@b.example", identity="g-77", email="a@b.example",
                          named=("a@b.example",)) == "a@b.example"
        # a name is never an email
        self._file(tmp_path, "Ann", "Ann")
        assert self._pick(tmp_path, own="Ann", identity="g-78", email="Ann", named=("Ann",)) == "Ann 2"

    def test_how_a_token_was_issued(self):
        from services.oauth import oauth_account_store as store
        assert store.issued_by({"via_relay": True, "app_kind": "github_app"}, "") == "relay:github_app"
        assert store.issued_by({"flow": "mcp_authorization", "issuer": "https://a.example/"}, "c") == \
            "registered:https://a.example"
        assert store.issued_by({"flow": "personal_access_token"}, "") == "pat"
        assert store.issued_by({}, "cid-1") == "app:cid-1"

    def test_a_vendor_name_the_label_rule_refuses_is_cleaned(self):
        from services.oauth import oauth_account_store as store
        assert store.clean_label("Dimitris M", provider_id="facebook", identity="1") == "Dimitris M"
        assert store.clean_label("O'Brien  Ann", provider_id="facebook", identity="1") == "O Brien Ann"
        assert store.clean_label("Δημήτρης", provider_id="facebook", identity="10023456789") == "facebook 23456789"
        assert store.clean_label("x" * 200, provider_id="facebook", identity="1") == "x" * 128
        store.validate_account_label(store._numbered("x" * 128, 12))


class TestMcpsNamingLabel:
    def test_lists_every_mcp_of_the_person_with_the_label(self, user_sub):
        credential_store.set_user_credentials(user_sub, "fb-a", {"K": "v"}, account_label="Same")
        credential_store.set_user_credentials(user_sub, "fb-b", {"K": "v"}, account_label="Same")
        credential_store.set_user_credentials(user_sub, "fb-b", {"K": "v"}, account_label="Other")
        assert credential_store.mcps_naming_label(user_sub, "Same") == ["fb-a", "fb-b"]
        assert credential_store.mcps_naming_label(user_sub, "Other") == ["fb-b"]
        assert credential_store.mcps_naming_label(user_sub, "None") == []


def test_two_sign_ins_at_once_never_share_a_label(monkeypatch, tmp_path):
    """The person's provider lock covers the choice and the write: two
    logins of one name signing in together land on two labels."""
    import asyncio
    import json

    import config
    from auth.oauth_providers.base import UserInfo
    from services.oauth import oauth_account_store as store
    from storage import database as task_store
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: "alice")
    monkeypatch.setattr(store, "other_mcps_naming", lambda *a: [])

    async def sign_in(login: str) -> str:
        async with store.signin_label(
            user_sub="alice-sub", provider_id="facebook", mcp_name="fb-a", hint="", own_label="Ann",
            userinfo=UserInfo(email="Ann", account_id=login), identity=login, issued="app:cid",
        ) as label:
            await asyncio.sleep(0.05)
            d = store.get_token_dir("alice", provider_id="facebook")
            (d / f"{label}.json").write_text(json.dumps({"account_id": login, "client_id": "cid", "extra": {}}))
            return label

    async def both():
        return await asyncio.gather(sign_in("fb-1"), sign_in("fb-2"))
    assert sorted(asyncio.run(both())) == ["Ann", "Ann 2"]
