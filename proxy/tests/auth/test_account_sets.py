"""An agent's set of a person's accounts (``agent_account_bindings``, one
default per set) and a chat's choice among it (``chat_account_choices``):
the migration of a 1.9.1 table into sets of one and a fresh table ending
the same, the store (the first account is the default, a removal promotes
the oldest left on every delete path, the reach of an added account), the
resolver's pick and reach for a chat's choice, and the choice's own store.

Uses the real PostgreSQL test database, as ``test_multi_account.py`` does.
"""

from __future__ import annotations

import uuid

import pytest

from services.oauth import credential_resolver
from storage import database as task_store, pg, schema as pg_schema
from storage.identity import (
    credential_store,
    schema as identity_schema,
    service_account_set,
)
from storage.pg import get_conn

ADMIN = "user-admin"        # seeded by conftest, a platform admin
OTHER = "user-viewer"       # seeded by conftest


@pytest.fixture
def user_sub():
    """A person of this test alone (``user_credentials`` keys on a real
    users row)."""
    sub = f"test-user-{uuid.uuid4().hex[:12]}"
    username = f"u{uuid.uuid4().hex[:8]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, name, username, role, auth_provider, created_at, last_login) "
            "VALUES (%s, %s, 'Test', %s, 'creator', 'local', NOW()::text, NOW()::text)",
            (sub, f"{username}@example.test", username),
        )
        conn.commit()
    yield sub
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE sub = %s", (sub,))
        conn.commit()


@pytest.fixture
def mcp_name():
    return f"test-mcp-{uuid.uuid4().hex[:8]}"


def _accounts(sub: str, mcp: str, *labels: str) -> None:
    for label in labels:
        credential_store.set_user_credentials(sub, mcp, {"k": label}, account_label=label)


def _set(sub: str, mcp: str, agent: str) -> list[tuple[str, bool]]:
    return [(r.account_label, r.is_default) for r in credential_store.list_agent_account_set(sub, mcp, agent)]


def _picked(sub: str, mcp: str, agent: str, choices: dict[str, str] | None = None) -> str | None:
    ref = credential_resolver.pick_account(mcp, agent, user_sub=sub, account_choices=choices)
    return ref.label if ref is not None else None


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


def _one_account_shape() -> None:
    """The live table back to its 1.9.1 shape: no chat choices, no default
    index, no ``is_default``, the unnamed three-column UNIQUE in place of
    the named key."""
    pg.close_pool()
    with get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS chat_account_choices")
        conn.execute("DROP INDEX IF EXISTS idx_aab_default")
        conn.execute("ALTER TABLE agent_account_bindings DROP CONSTRAINT IF EXISTS agent_account_bindings_set_key")
        conn.execute("ALTER TABLE agent_account_bindings DROP COLUMN IF EXISTS is_default")
        conn.execute("ALTER TABLE agent_account_bindings ADD UNIQUE (user_sub, mcp_name, agent_name)")
        conn.commit()
    pg.close_pool()


def _migrate() -> None:
    pg.close_pool()
    with get_conn() as conn:
        # The boot's order: the init's CREATEs no-op on the old table, then
        # the migration carries it forward.
        pg_schema.init_schema(conn)
        pg_schema.run_migrations(conn)
        conn.commit()
    pg.close_pool()


def _catalog(conn, schema: str) -> dict:
    """The constraints and indexes of the two tables in ``schema``, the
    schema's name taken out of each definition."""
    out: dict = {}
    for table in ("agent_account_bindings", "chat_account_choices"):
        cons = conn.execute(
            "SELECT c.conname, c.contype, pg_get_constraintdef(c.oid) AS d FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE t.relname = %s AND n.nspname = %s ORDER BY c.conname", (table, schema),
        ).fetchall()
        idx = conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s AND schemaname = %s "
            "ORDER BY indexname", (table, schema),
        ).fetchall()
        out[table] = {
            "constraints": [(r["conname"], r["contype"], r["d"].replace(f"{schema}.", "")) for r in cons],
            "indexes": [(r["indexname"], r["indexdef"].replace(f"{schema}.", "")) for r in idx],
        }
    return out


def _live_schema(conn) -> str:
    return conn.execute("SELECT current_schema() AS s").fetchone()["s"]


def _rows(mcp: str) -> list[tuple]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_sub, agent_name, account_label, is_default FROM agent_account_bindings "
            "WHERE mcp_name=%s ORDER BY user_sub, agent_name", (mcp,),
        ).fetchall()
    return [(r["user_sub"], r["agent_name"], r["account_label"], r["is_default"]) for r in rows]


def test_a_1_9_1_table_becomes_sets_of_one_and_a_second_boot_changes_nothing():
    mcp = f"mig-sets-{uuid.uuid4().hex[:8]}"
    _one_account_shape()
    try:
        with get_conn() as conn:
            assert not identity_schema._set_key_exists(conn)
            assert identity_schema._one_account_keys(conn)
            for sub, label in ((ADMIN, "a"), (ADMIN, "b"), (OTHER, "c")):
                conn.execute(
                    "INSERT INTO user_credential_accounts "
                    "(user_sub, mcp_name, account_label, display_email, is_default, created_at) "
                    "VALUES (%s, %s, %s, '', %s, NOW()::text)", (sub, mcp, label, label != "b"),
                )
            for sub, agent, label in ((ADMIN, "pa", "b"), (ADMIN, "dev", "a"), (OTHER, "pa", "c")):
                conn.execute(
                    "INSERT INTO agent_account_bindings (user_sub, mcp_name, agent_name, account_label, set_at) "
                    "VALUES (%s, %s, %s, %s, NOW()::text)", (sub, mcp, agent, label),
                )
            conn.commit()
        _migrate()
        # Every 1.9.1 row is the default of a set of one: the same account.
        assert _rows(mcp) == [(ADMIN, "dev", "a", True), (ADMIN, "pa", "b", True), (OTHER, "pa", "c", True)]
        assert _picked(ADMIN, mcp, "pa") == "b" and _picked(ADMIN, mcp, "dev") == "a"
        with get_conn() as conn:
            assert identity_schema._one_account_keys(conn) == []
            first = _catalog(conn, _live_schema(conn))
        names = {n for n, _t, _d in first["agent_account_bindings"]["constraints"]}
        assert "agent_account_bindings_set_key" in names
        assert dict(first["agent_account_bindings"]["indexes"])["idx_aab_default"].endswith(
            "(user_sub, mcp_name, agent_name) WHERE is_default")
        assert first["chat_account_choices"]["constraints"]
        # A second set row of the same agent is now possible, and one default.
        assert credential_store.add_account_to_agent(ADMIN, mcp, "pa", "a")
        assert _set(ADMIN, mcp, "pa") == [("b", True), ("a", False)]
        before = _rows(mcp)
        _migrate()
        assert _rows(mcp) == before
        with get_conn() as conn:
            assert _catalog(conn, _live_schema(conn)) == first
            assert pg_schema.check_schema_drift(conn) == []
    finally:
        _migrate()
        with get_conn() as conn:
            conn.execute("DELETE FROM agent_account_bindings WHERE mcp_name=%s", (mcp,))
            conn.execute("DELETE FROM user_credential_accounts WHERE mcp_name=%s", (mcp,))
            conn.commit()
        # The DROP and the ADD reordered the rows' columns: every pooled
        # session's cached plan of a SELECT * is stale, so the pools close.
        pg.close_pool(timeout=2.0)


def test_a_set_with_rows_and_no_default_is_healed_at_every_boot():
    """On a migrated table (the 1.9.1 key long gone), a set whose rows are
    all unmarked gets its oldest row as the default. A set that has one
    keeps it."""
    mcp = f"mig-heal-{uuid.uuid4().hex[:8]}"
    try:
        with get_conn() as conn:
            assert identity_schema._one_account_keys(conn) == []
            for label in ("old", "new", "kept", "other"):
                conn.execute(
                    "INSERT INTO user_credential_accounts "
                    "(user_sub, mcp_name, account_label, display_email, is_default, created_at) "
                    "VALUES (%s, %s, %s, '', %s, NOW()::text)", (ADMIN, mcp, label, label == "old"),
                )
            for agent, label, at, marked in (
                    ("pa", "new", "2026-02-02", False), ("pa", "old", "2026-01-01", False),
                    ("dev", "other", "2026-01-01", False), ("dev", "kept", "2026-02-02", True)):
                conn.execute(
                    "INSERT INTO agent_account_bindings "
                    "(user_sub, mcp_name, agent_name, account_label, set_at, is_default) "
                    "VALUES (%s, %s, %s, %s, %s, %s)", (ADMIN, mcp, agent, label, at, marked),
                )
            conn.commit()
        _migrate()
        assert _set(ADMIN, mcp, "pa") == [("old", True), ("new", False)]
        assert _set(ADMIN, mcp, "dev") == [("kept", True), ("other", False)]
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM agent_account_bindings WHERE mcp_name=%s", (mcp,))
            conn.execute("DELETE FROM user_credential_accounts WHERE mcp_name=%s", (mcp,))
            conn.commit()


def test_a_set_with_two_defaults_keeps_the_oldest_and_the_boot_goes_on():
    """A table written past the store before the default index was there
    may hold two marked rows in one set. The migration keeps the oldest
    marked one, so the index is built and no data stops the boot."""
    mcp = f"mig-two-{uuid.uuid4().hex[:8]}"
    try:
        with get_conn() as conn:
            conn.execute("DROP INDEX IF EXISTS idx_aab_default")
            for label in ("first", "second", "third"):
                conn.execute(
                    "INSERT INTO user_credential_accounts "
                    "(user_sub, mcp_name, account_label, display_email, is_default, created_at) "
                    "VALUES (%s, %s, %s, '', %s, NOW()::text)", (ADMIN, mcp, label, label == "first"),
                )
            for label, at, marked in (("third", "2026-03-03", True), ("first", "2026-01-01", False),
                                      ("second", "2026-02-02", True)):
                conn.execute(
                    "INSERT INTO agent_account_bindings "
                    "(user_sub, mcp_name, agent_name, account_label, set_at, is_default) "
                    "VALUES (%s, %s, 'pa', %s, %s, %s)", (ADMIN, mcp, label, at, marked),
                )
            conn.commit()
        _migrate()
        assert _set(ADMIN, mcp, "pa") == [("second", True), ("first", False), ("third", False)]
        with get_conn() as conn:
            assert "idx_aab_default" in dict(
                _catalog(conn, _live_schema(conn))["agent_account_bindings"]["indexes"])
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM agent_account_bindings WHERE mcp_name=%s", (mcp,))
            conn.execute("DELETE FROM user_credential_accounts WHERE mcp_name=%s", (mcp,))
            conn.commit()
        _migrate()


def test_a_step_that_fails_stops_the_boot(monkeypatch):
    """Every chat's start reads ``chat_account_choices``, which the
    migration ends with: a failed step raises, it is never passed over."""
    def broken(conn):
        raise RuntimeError("the catalog read failed")

    monkeypatch.setattr(identity_schema, "_one_account_keys", broken)
    with get_conn() as conn:
        try:
            with pytest.raises(RuntimeError):
                identity_schema._migrate_account_sets(conn)
        finally:
            conn.rollback()


def test_a_fresh_table_has_the_same_key_and_index_as_a_migrated_one():
    """The boot of a new install, built in a scratch schema inside a
    transaction that is never committed, against the live tables."""
    probe = "otodock_account_set_probe"
    with get_conn() as conn:
        live = _catalog(conn, _live_schema(conn))
        try:
            conn.execute(f"DROP SCHEMA IF EXISTS {probe} CASCADE")
            conn.execute(f"CREATE SCHEMA {probe}")
            conn.execute(f"SET LOCAL search_path TO {probe}")
            pg_schema.init_schema(conn)
            identity_schema.migrate_identity(conn)
            fresh = _catalog(conn, probe)
        finally:
            conn.rollback()
    assert fresh == live
    assert "agent_account_bindings_set_key" in {n for n, _t, _d in fresh["agent_account_bindings"]["constraints"]}
    assert "idx_aab_default" in dict(fresh["agent_account_bindings"]["indexes"])


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class TestTheSet:
    def test_the_first_account_is_the_default_and_make_default_moves_it(self, user_sub, mcp_name):
        _accounts(user_sub, mcp_name, "A", "B", "C")
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "B")
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "A")
        assert _set(user_sub, mcp_name, "agent") == [("B", True), ("A", False)]
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "C", make_default=True)
        assert _set(user_sub, mcp_name, "agent") == [("C", True), ("B", False), ("A", False)]
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "A", make_default=True)
        assert _set(user_sub, mcp_name, "agent") == [("A", True), ("B", False), ("C", False)]
        assert credential_store.set_agent_default_account(user_sub, mcp_name, "agent", "B")
        assert _set(user_sub, mcp_name, "agent")[0] == ("B", True)
        assert credential_store.get_account_agent_binding(user_sub, mcp_name, "agent").account_label == "B"
        assert credential_store.get_account_agent_binding(user_sub, mcp_name, "agent", "C").is_default is False
        assert credential_store.get_account_agent_binding(user_sub, mcp_name, "agent", "D") is None
        # Another agent's set is its own.
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "other", "C")
        assert _set(user_sub, mcp_name, "other") == [("C", True)]
        rows = credential_store.list_agent_account_bindings(user_sub, mcp_name)
        assert [(r["agent_name"], r["account_label"], r["is_default"]) for r in rows] == [
            ("agent", "B", True), ("agent", "A", False), ("agent", "C", False), ("other", "C", True)]

    def test_what_the_set_does_not_hold_or_the_person_does_not_own_is_refused(self, user_sub, mcp_name):
        _accounts(user_sub, mcp_name, "A", "B")
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "ghost") is False
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "A")
        assert credential_store.set_agent_default_account(user_sub, mcp_name, "agent", "B") is False
        assert credential_store.remove_account_agent_binding(user_sub, mcp_name, "agent", "B") is False
        assert _set(user_sub, mcp_name, "agent") == [("A", True)]

    def test_removing_the_default_promotes_the_oldest_and_the_last_empties_the_set(self, user_sub, mcp_name):
        _accounts(user_sub, mcp_name, "A", "B", "C")
        for label in ("A", "B", "C"):
            assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", label)
        assert credential_store.set_agent_default_account(user_sub, mcp_name, "agent", "C")
        assert credential_store.remove_account_agent_binding(user_sub, mcp_name, "agent", "C")
        assert _set(user_sub, mcp_name, "agent") == [("A", True), ("B", False)]
        # A non-default leaves the default where it is.
        assert credential_store.remove_account_agent_binding(user_sub, mcp_name, "agent", "B")
        assert _set(user_sub, mcp_name, "agent") == [("A", True)]
        assert credential_store.remove_account_agent_binding(user_sub, mcp_name, "agent", "A")
        assert _set(user_sub, mcp_name, "agent") == []
        assert credential_store.get_account_agent_binding(user_sub, mcp_name, "agent") is None
        # Without a label the whole set goes.
        for label in ("A", "B"):
            assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", label)
        assert credential_store.remove_account_agent_binding(user_sub, mcp_name, "agent")
        assert _set(user_sub, mcp_name, "agent") == []

    def test_a_disconnect_promotes_in_each_agents_set(self, user_sub, mcp_name):
        _accounts(user_sub, mcp_name, "A", "B")
        for agent in ("one", "two"):
            assert credential_store.add_account_to_agent(user_sub, mcp_name, agent, "A")
            assert credential_store.add_account_to_agent(user_sub, mcp_name, agent, "B")
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "alone", "A")
        credential_store.delete_user_credentials(user_sub, mcp_name, "A")
        assert _set(user_sub, mcp_name, "one") == [("B", True)]
        assert _set(user_sub, mcp_name, "two") == [("B", True)]
        assert _set(user_sub, mcp_name, "alone") == []

    def test_an_added_account_takes_the_reach_named_else_the_whole_account(self, user_sub, mcp_name):
        _accounts(user_sub, mcp_name, "A", "B")
        assert credential_store.add_account_to_agent(
            user_sub, mcp_name, "agent", "A", repositories=[" octo/a ", "octo/a"], read_only=True)
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "B")
        get = credential_store.get_account_agent_binding
        assert get(user_sub, mcp_name, "agent", "A") == ("A", ("octo/a",), True, True)
        # Never another account's reach.
        assert get(user_sub, mcp_name, "agent", "B") == ("B", (), False, False)
        # An existing row keeps its own reach for a field left out.
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "A", read_only=False)
        assert get(user_sub, mcp_name, "agent", "A") == ("A", ("octo/a",), False, True)
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "A", make_default=False)
        assert get(user_sub, mcp_name, "agent", "A") == ("A", ("octo/a",), False, True)
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "B", repositories=["octo/b"])
        assert get(user_sub, mcp_name, "agent", "B") == ("B", ("octo/b",), False, False)

    def test_the_one_account_write_makes_the_set_that_account(self, user_sub, mcp_name):
        _accounts(user_sub, mcp_name, "A", "B", "C")
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "A", repositories=["octo/a"])
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "B", read_only=True)
        # The same account keeps its own row's reach.
        assert credential_store.set_account_agent_binding(user_sub, mcp_name, "agent", "B")
        assert credential_store.list_agent_account_set(user_sub, mcp_name, "agent") == [("B", (), True, True)]
        # Another account keeps the prior choice's reach, as in 1.9.1.
        assert credential_store.set_account_agent_binding(user_sub, mcp_name, "agent", "C")
        assert credential_store.list_agent_account_set(user_sub, mcp_name, "agent") == [("C", (), True, True)]


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


class TestThePick:
    def test_an_empty_set_runs_on_the_persons_default(self, user_sub, mcp_name):
        _accounts(user_sub, mcp_name, "star", "A")
        assert _picked(user_sub, mcp_name, "agent") == "star"
        assert _picked(user_sub, mcp_name, "agent", {mcp_name: "A"}) == "star"

    def test_a_set_runs_on_its_default_whatever_the_persons_default(self, user_sub, mcp_name):
        _accounts(user_sub, mcp_name, "star", "A", "B")
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "A")
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "B")
        assert _picked(user_sub, mcp_name, "agent") == "A"
        assert _picked(user_sub, mcp_name, "agent", {}) == "A"

    def test_a_choice_in_the_set_wins_and_any_other_is_ignored(self, user_sub, mcp_name):
        _accounts(user_sub, mcp_name, "star", "A", "B")
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "A")
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "B")
        assert _picked(user_sub, mcp_name, "agent", {mcp_name: "B"}) == "B"
        # The person's own default is outside the set: never picked.
        assert _picked(user_sub, mcp_name, "agent", {mcp_name: "star"}) == "A"
        assert _picked(user_sub, mcp_name, "agent", {mcp_name: "ghost"}) == "A"
        # Another tool's choice.
        assert _picked(user_sub, mcp_name, "agent", {"another-mcp": "B"}) == "A"
        # Another agent's set does not hold it.
        assert _picked(user_sub, mcp_name, "elsewhere", {mcp_name: "B"}) == "star"

    def test_service_scope_takes_a_choice_of_the_agents_own_set_alone(self, mcp_name):
        """A Shared-only chat's choice picks a row of the agent's own set; a
        label the set does not hold (an account of the lender's the agent
        never got) gives no account, never the default in its place."""
        _accounts(ADMIN, mcp_name, "svc", "other")
        assert service_account_set.add_service_account_to_agent(
            mcp_name, "agent", account_label="svc", owner_sub=ADMIN, set_by=ADMIN) == ""
        try:
            pick = credential_resolver.pick_account
            assert pick(mcp_name, "agent", account_choices={mcp_name: "other"}) is None
            ref = pick(mcp_name, "agent", account_choices={mcp_name: "svc"})
            assert ref is not None and (ref.label, ref.owner_sub) == ("svc", ADMIN)
            assert pick(mcp_name, "agent", account_choices={"another-tool": "other"}).label == "svc"
        finally:
            credential_store.delete_all_mcp_credentials(mcp_name)

    def test_the_reach_is_the_picked_accounts(self, user_sub, mcp_name):
        _accounts(user_sub, mcp_name, "A", "B", "C")
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "A", repositories=["octo/a"])
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "B", read_only=True)
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "C")

        def reach(choices=None):
            r = credential_resolver.binding_reach(
                mcp_name, "agent", user_sub=user_sub, task_scope="user", account_choices=choices)
            return None if r is None else (sorted(r.repositories), r.read_only, r.personal)
        assert reach() == (["octo/a"], False, True)
        assert reach({mcp_name: "B"}) == ([], True, True)
        assert reach({mcp_name: "C"}) is None           # the whole account: nothing narrows
        assert reach({mcp_name: "ghost"}) == (["octo/a"], False, True)
        assert credential_store.remove_account_agent_binding(user_sub, mcp_name, "agent")
        assert reach({mcp_name: "B"}) is None


# ---------------------------------------------------------------------------
# A chat's choice
# ---------------------------------------------------------------------------


@pytest.fixture
def chat(user_sub):
    chat_id = f"chat-{uuid.uuid4().hex[:12]}"
    task_store.create_chat(chat_id, user_sub, "agent")
    return chat_id


class TestTheChatsChoice:
    def test_set_get_clear_and_a_label_outside_the_set_is_refused(self, user_sub, mcp_name, chat):
        _accounts(user_sub, mcp_name, "A", "B", "C")
        for label in ("A", "B"):
            assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", label)
        assert credential_store.get_chat_account_choices(chat, user_sub) == {}
        assert credential_store.set_chat_account_choice(chat, user_sub, "agent", mcp_name, "B")
        assert credential_store.get_chat_account_choices(chat, user_sub) == {mcp_name: "B"}
        assert credential_store.set_chat_account_choice(chat, user_sub, "agent", mcp_name, "A")
        assert credential_store.get_chat_account_choices(chat, user_sub) == {mcp_name: "A"}
        assert credential_store.set_chat_account_choice(chat, user_sub, "agent", mcp_name, "C") is False
        assert credential_store.set_chat_account_choice(chat, user_sub, "elsewhere", mcp_name, "A") is False
        assert credential_store.get_chat_account_choices(chat, user_sub) == {mcp_name: "A"}
        # Only the person who chose it.
        assert credential_store.get_chat_account_choices(chat, OTHER) == {}
        credential_store.clear_chat_account_choice(chat, user_sub, mcp_name)
        assert credential_store.get_chat_account_choices(chat, user_sub) == {}

    def test_it_goes_with_its_set_row_and_never_comes_back(self, user_sub, mcp_name, chat):
        _accounts(user_sub, mcp_name, "A", "B")
        for label in ("A", "B"):
            assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", label)
        assert credential_store.set_chat_account_choice(chat, user_sub, "agent", mcp_name, "B")
        assert credential_store.remove_account_agent_binding(user_sub, mcp_name, "agent", "B")
        assert credential_store.get_chat_account_choices(chat, user_sub) == {}
        assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", "B")
        assert credential_store.get_chat_account_choices(chat, user_sub) == {}
        # The one-account write takes the set's other rows, and their choices.
        assert credential_store.set_chat_account_choice(chat, user_sub, "agent", mcp_name, "B")
        assert credential_store.set_account_agent_binding(user_sub, mcp_name, "agent", "A")
        assert credential_store.get_chat_account_choices(chat, user_sub) == {}

    def test_it_goes_when_the_account_is_disconnected_or_the_chat_deleted(self, user_sub, mcp_name, chat):
        _accounts(user_sub, mcp_name, "A", "B")
        for label in ("A", "B"):
            assert credential_store.add_account_to_agent(user_sub, mcp_name, "agent", label)
        assert credential_store.set_chat_account_choice(chat, user_sub, "agent", mcp_name, "B")
        credential_store.delete_user_credentials(user_sub, mcp_name, "B")
        assert credential_store.get_chat_account_choices(chat, user_sub) == {}
        assert credential_store.set_chat_account_choice(chat, user_sub, "agent", mcp_name, "A")
        task_store.delete_chat(chat)
        with get_conn() as conn:
            assert conn.execute("SELECT 1 FROM chat_account_choices WHERE chat_id=%s", (chat,)).fetchone() is None
        assert credential_store.set_chat_account_choice(chat, user_sub, "agent", mcp_name, "A") is False


# ---------------------------------------------------------------------------
# The agent's own set: the migration of ``service_agent_bindings``
# ---------------------------------------------------------------------------


def _one_service_account_shape() -> None:
    """The live service table back to its 1.9.1 shape: no chat choices, no
    default index, no ``is_default``, the unnamed two-column UNIQUE in place
    of the named key."""
    pg.close_pool()
    with get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS chat_service_account_choices")
        conn.execute("DROP INDEX IF EXISTS idx_sab_default")
        conn.execute("ALTER TABLE service_agent_bindings DROP CONSTRAINT IF EXISTS service_agent_bindings_set_key")
        conn.execute("ALTER TABLE service_agent_bindings DROP COLUMN IF EXISTS is_default")
        conn.execute("ALTER TABLE service_agent_bindings ADD UNIQUE (mcp_name, agent_name)")
        conn.commit()
    pg.close_pool()


def _service_catalog(conn, schema: str) -> dict:
    out: dict = {}
    for table in ("service_agent_bindings", "chat_service_account_choices"):
        cons = conn.execute(
            "SELECT c.conname, c.contype, pg_get_constraintdef(c.oid) AS d FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE t.relname = %s AND n.nspname = %s ORDER BY c.conname", (table, schema),
        ).fetchall()
        idx = conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s AND schemaname = %s "
            "ORDER BY indexname", (table, schema),
        ).fetchall()
        out[table] = {
            "constraints": [(r["conname"], r["contype"], r["d"].replace(f"{schema}.", "")) for r in cons],
            "indexes": [(r["indexname"], r["indexdef"].replace(f"{schema}.", "")) for r in idx],
        }
    return out


def _service_rows(mcp: str) -> list[tuple]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT agent_name, account_label, account_owner_sub, is_default FROM service_agent_bindings "
            "WHERE mcp_name=%s ORDER BY agent_name, set_at, id", (mcp,),
        ).fetchall()
    return [(r["agent_name"], r["account_label"], r["account_owner_sub"], r["is_default"]) for r in rows]


def _service_unique_keys(conn) -> list[str]:
    return identity_schema._unique_keys_on(conn, "service_agent_bindings", ["mcp_name", "agent_name"])


def test_a_1_9_1_service_table_becomes_sets_of_one_and_each_reader_sees_the_same_account():
    """Every 1.9.1 binding becomes the default of a set of one, so a released
    install resolves exactly what it did until a manager adds an account;
    a second boot changes nothing and the live schema matches the code."""
    mcp = f"mig-svc-{uuid.uuid4().hex[:8]}"
    _one_service_account_shape()
    try:
        with get_conn() as conn:
            assert _service_unique_keys(conn)
            for label in ("a", "b"):
                conn.execute(
                    "INSERT INTO user_credential_accounts "
                    "(user_sub, mcp_name, account_label, display_email, is_default, created_at) "
                    "VALUES (%s, %s, %s, '', %s, NOW()::text)", (ADMIN, mcp, label, label == "a"),
                )
            for agent, label in (("pa", "b"), ("dev", "a")):
                conn.execute(
                    "INSERT INTO service_agent_bindings "
                    "(mcp_name, agent_name, account_label, account_owner_sub, set_at) "
                    "VALUES (%s, %s, %s, %s, NOW()::text)", (mcp, agent, label, ADMIN),
                )
            conn.commit()
        _migrate()
        assert _service_rows(mcp) == [("dev", "a", ADMIN, True), ("pa", "b", ADMIN, True)]
        assert credential_resolver.pick_account(mcp, "pa").label == "b"
        assert credential_resolver.pick_account(mcp, "dev").label == "a"
        with get_conn() as conn:
            assert _service_unique_keys(conn) == []
            first = _service_catalog(conn, _live_schema(conn))
        assert "service_agent_bindings_set_key" in {
            n for n, _t, _d in first["service_agent_bindings"]["constraints"]}
        assert dict(first["service_agent_bindings"]["indexes"])["idx_sab_default"].startswith(
            "CREATE UNIQUE INDEX")
        assert first["chat_service_account_choices"]["constraints"]
        before = _service_rows(mcp)
        _migrate()
        assert _service_rows(mcp) == before
        with get_conn() as conn:
            assert _service_catalog(conn, _live_schema(conn)) == first
            assert pg_schema.check_schema_drift(conn) == []
    finally:
        _migrate()
        with get_conn() as conn:
            conn.execute("DELETE FROM service_agent_bindings WHERE mcp_name=%s", (mcp,))
            conn.execute("DELETE FROM user_credential_accounts WHERE mcp_name=%s", (mcp,))
            conn.commit()
        pg.close_pool(timeout=2.0)


def test_a_service_set_with_no_default_or_two_is_healed_at_every_boot():
    mcp = f"mig-svc-heal-{uuid.uuid4().hex[:8]}"
    try:
        with get_conn() as conn:
            conn.execute("DROP INDEX IF EXISTS idx_sab_default")
            for label in ("old", "new", "x", "y", "z"):
                conn.execute(
                    "INSERT INTO user_credential_accounts "
                    "(user_sub, mcp_name, account_label, display_email, is_default, created_at) "
                    "VALUES (%s, %s, %s, '', FALSE, NOW()::text)", (ADMIN, mcp, label),
                )
            for agent, label, at, marked in (
                    ("pa", "new", "2026-02-02", False), ("pa", "old", "2026-01-01", False),
                    ("dev", "z", "2026-03-03", True), ("dev", "x", "2026-01-01", False),
                    ("dev", "y", "2026-02-02", True)):
                conn.execute(
                    "INSERT INTO service_agent_bindings "
                    "(mcp_name, agent_name, account_label, account_owner_sub, set_at, is_default) "
                    "VALUES (%s, %s, %s, %s, %s, %s)", (mcp, agent, label, ADMIN, at, marked),
                )
            conn.commit()
        _migrate()
        assert _service_rows(mcp) == [("dev", "x", ADMIN, False), ("dev", "y", ADMIN, True),
                                      ("dev", "z", ADMIN, False), ("pa", "old", ADMIN, True),
                                      ("pa", "new", ADMIN, False)]
    finally:
        with get_conn() as conn:
            conn.execute("DELETE FROM service_agent_bindings WHERE mcp_name=%s", (mcp,))
            conn.execute("DELETE FROM user_credential_accounts WHERE mcp_name=%s", (mcp,))
            conn.commit()
        _migrate()


def test_a_service_set_step_that_fails_stops_the_boot(monkeypatch):
    def broken(conn, table, columns):
        raise RuntimeError("the catalog read failed")

    monkeypatch.setattr(identity_schema, "_unique_keys_on", broken)
    with get_conn() as conn:
        try:
            with pytest.raises(RuntimeError):
                identity_schema._migrate_service_sets(conn)
        finally:
            conn.rollback()


def test_a_fresh_service_table_has_the_same_key_and_index_as_a_migrated_one():
    probe = "otodock_service_set_probe"
    with get_conn() as conn:
        live = _service_catalog(conn, _live_schema(conn))
        try:
            conn.execute(f"DROP SCHEMA IF EXISTS {probe} CASCADE")
            conn.execute(f"CREATE SCHEMA {probe}")
            conn.execute(f"SET LOCAL search_path TO {probe}")
            pg_schema.init_schema(conn)
            identity_schema.migrate_identity(conn)
            fresh = _service_catalog(conn, probe)
        finally:
            conn.rollback()
    assert fresh == live
    assert "idx_sab_default" in dict(fresh["service_agent_bindings"]["indexes"])
