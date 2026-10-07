"""The 1.7.0 ``shares`` table migrates forward (SHARING.md "The shares
table"; DATABASE-SCHEMA.md "Init / migration mechanism"): the one-kind rows
become person shares, a chat needs no decision, a link keeps no kind, the
named CHECKs replace the auto-named one, the per-kind unique indexes replace
the one that would refuse a re-share after a decline, and a second boot
changes nothing a person decided meanwhile.
"""

from __future__ import annotations

import uuid

from storage import database as task_store
from storage import pg
from storage import schema as pg_schema
from storage.pg import get_conn
from storage.sharing import share_store

AGENT = "mig-agent"
OWNER = "user-manager"     # seeded by conftest
GRANTEE = "user-viewer"
OTHER = "user-admin"
OLD_SCOPE_CHECK = ("(scope = 'internal' AND grantee_sub IS NOT NULL) "
                   "OR (scope = 'external' AND grantee_sub IS NULL)")
NEW_COLUMNS = ("grantee_kind", "grantee_agent", "grantee_department", "role_cap",
               "decision", "decided_by", "decided_at", "placed_agent")
NEW_INDEXES = ("idx_shares_person_grant", "idx_shares_agent_grant",
               "idx_shares_department_grant", "idx_shares_grantee_agent",
               "idx_shares_grantee_department", "idx_shares_placed_agent")


def _old_shape() -> None:
    """The live table back to its 1.7.0 shape, with ALTERs (a DROP TABLE
    would take the hides table's foreign key with it for the rest of the
    worker's tests): the eight columns gone, the named CHECKs with them,
    the auto-named scope CHECK and the old unique index back."""
    pg.close_pool()
    with get_conn() as conn:
        for name in ("shares_grantee_check", "shares_grantee_kind_check",
                     "shares_decision_check", "shares_role_cap_check"):
            conn.execute(f"ALTER TABLE shares DROP CONSTRAINT IF EXISTS {name}")
        for idx in NEW_INDEXES:
            conn.execute(f"DROP INDEX IF EXISTS {idx}")
        for col in NEW_COLUMNS:
            conn.execute(f"ALTER TABLE shares DROP COLUMN IF EXISTS {col}")
        conn.execute(f"ALTER TABLE shares ADD CHECK ({OLD_SCOPE_CHECK})")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_shares_internal_grant "
            "ON shares (target_kind, COALESCE(app_id, chat_id), grantee_sub) "
            "WHERE scope = 'internal' AND revoked_at IS NULL"
        )
        conn.commit()
    pg.close_pool()


def _migrate(times: int = 1) -> None:
    pg.close_pool()
    for _ in range(times):
        with get_conn() as conn:
            # The boot's order: the init's CREATEs no-op on the old table,
            # then the migration carries it forward.
            pg_schema.init_schema(conn)
            pg_schema.run_migrations(conn)
            conn.commit()
    pg.close_pool()


def _columns() -> set[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'shares'"
        ).fetchall()
        return {r["column_name"] for r in rows}


def _indexes() -> set[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'shares' "
            "AND schemaname = current_schema()"
        ).fetchall()
        return {r["indexname"] for r in rows}


def _check_defs() -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.conname, pg_get_constraintdef(c.oid) AS d FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE t.relname = 'shares' AND n.nspname = current_schema() AND c.contype = 'c'"
        ).fetchall()
        return {r["conname"]: r["d"] for r in rows}


def _old_insert(conn, *, target_kind: str, target_id: str, scope: str,
                grantee_sub: str | None, created_by: str, revoked: bool = False,
                token_hash: str | None = None) -> str:
    """A 1.7.0 INSERT: none of the new columns named."""
    share_id = str(uuid.uuid4())
    col = "app_id" if target_kind == "app" else "chat_id"
    conn.execute(
        f"INSERT INTO shares (id, target_kind, {col}, scope, grantee_sub, created_by, "
        "created_at, revoked_at, token_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (share_id, target_kind, target_id, scope, grantee_sub, created_by,
         "2026-09-20T00:00:00+00:00", "2026-09-21T00:00:00+00:00" if revoked else None,
         token_hash),
    )
    return share_id


def _world() -> tuple[dict, str]:
    app = task_store.upsert_app(AGENT, "", None, "board", title="Board",
                                rel_path="workspace/apps/board.html")
    chat_id = str(uuid.uuid4())
    task_store.create_chat(chat_id, OWNER, AGENT)
    return app, chat_id


def test_the_one_kind_rows_become_person_shares_and_links_keep_serving():
    app, chat_id = _world()
    _old_shape()
    assert not NEW_COLUMNS[0] in _columns()
    assert "idx_shares_internal_grant" in _indexes()
    with get_conn() as conn:
        app_share = _old_insert(conn, target_kind="app", target_id=app["id"], scope="internal",
                                grantee_sub=GRANTEE, created_by=OWNER)
        chat_share = _old_insert(conn, target_kind="chat", target_id=chat_id, scope="internal",
                                 grantee_sub=GRANTEE, created_by=OWNER)
        link = _old_insert(conn, target_kind="app", target_id=app["id"], scope="external",
                           grantee_sub=None, created_by=OWNER, token_hash="t" * 64)
        gone = _old_insert(conn, target_kind="app", target_id=app["id"], scope="internal",
                           grantee_sub=OTHER, created_by=OWNER, revoked=True)
        conn.commit()
    _migrate(times=2)
    assert set(NEW_COLUMNS) <= _columns()
    rows = {r["id"]: r for r in (share_store.get_share(i)
                                for i in (app_share, chat_share, link, gone))}
    assert (rows[app_share]["grantee_kind"], rows[app_share]["decision"]) == ("person", "pending")
    assert (rows[chat_share]["grantee_kind"], rows[chat_share]["decision"]) == ("person", "accepted")
    assert (rows[link]["grantee_kind"], rows[link]["decision"]) == ("", "")
    assert rows[gone]["grantee_kind"] == "person" and rows[gone]["revoked_at"]
    assert all(r["role_cap"] == "viewer" for r in rows.values())
    # The grant opens as before; the link is live and found by its hash.
    assert share_store.internal_grant("app", app["id"], GRANTEE)["id"] == app_share
    assert share_store.internal_grant("app", app["id"], OTHER) is None
    assert share_store.is_live(share_store.get_share_by_token_hash("t" * 64))
    # The named CHECKs are in and the auto-named scope CHECK is gone.
    defs = _check_defs()
    assert {"shares_grantee_check", "shares_grantee_kind_check", "shares_decision_check",
            "shares_role_cap_check"} <= set(defs)
    assert not [n for n, d in defs.items() if "grantee_sub" in d and "grantee_kind" not in d]
    with get_conn() as conn:
        assert pg_schema._check_constraint_words(conn, "shares", "shares_role_cap_check") == {
            "manager", "editor", "contributor", "viewer"}
    pg.close_pool()
    # The index swap, and no drift afterwards.
    indexes = _indexes()
    assert "idx_shares_internal_grant" not in indexes
    assert set(NEW_INDEXES) <= indexes
    with get_conn() as conn:
        assert pg_schema.check_schema_drift(conn) == []
    pg.close_pool()


def test_a_decision_taken_between_two_boots_is_kept():
    app, chat_id = _world()
    _old_shape()
    with get_conn() as conn:
        app_share = _old_insert(conn, target_kind="app", target_id=app["id"], scope="internal",
                                grantee_sub=GRANTEE, created_by=OWNER)
        conn.commit()
    _migrate()
    assert share_store.set_decision(app_share, share_store.ACCEPTED, GRANTEE,
                                    placed_agent="elsewhere")
    _migrate()
    row = share_store.get_share(app_share)
    assert (row["decision"], row["placed_agent"]) == ("accepted", "elsewhere")


def test_a_downgraded_install_still_writes_and_a_re_upgrade_repairs_it():
    """1.7.0 names none of the new columns: its INSERTs pass the CHECKs
    through the defaults, and the next 1.7.1 boot gives a link its empty
    kind and a chat share its decision."""
    app, chat_id = _world()
    with get_conn() as conn:
        grant = _old_insert(conn, target_kind="app", target_id=app["id"], scope="internal",
                            grantee_sub=GRANTEE, created_by=OWNER)
        chat_share = _old_insert(conn, target_kind="chat", target_id=chat_id, scope="internal",
                                 grantee_sub=GRANTEE, created_by=OWNER)
        link = _old_insert(conn, target_kind="app", target_id=app["id"], scope="external",
                           grantee_sub=None, created_by=OWNER, token_hash="u" * 64)
        conn.commit()
    row = share_store.get_share(link)
    assert (row["grantee_kind"], row["decision"]) == ("person", "pending")
    _migrate()
    assert (share_store.get_share(link)["grantee_kind"],
            share_store.get_share(link)["decision"]) == ("", "")
    assert share_store.get_share(chat_share)["decision"] == "accepted"
    assert share_store.get_share(grant)["decision"] == "pending"


def test_a_declined_share_never_blocks_a_re_share_and_leaves_one_unrevoked_row():
    """The per-kind unique index leaves declined rows out, and a new share
    revokes the declined one in its own transaction, so 1.7.0's old index
    (one unrevoked internal row per target and person) could be rebuilt."""
    app, _chat = _world()
    first = share_store.create_internal_share(target_kind="app", target_id=app["id"],
                                              grantee_sub=GRANTEE, created_by=OWNER)
    assert share_store.set_decision(first["id"], share_store.DECLINED, GRANTEE)
    assert share_store.internal_grant("app", app["id"], GRANTEE) is None
    second = share_store.create_internal_share(target_kind="app", target_id=app["id"],
                                               grantee_sub=GRANTEE, created_by=OWNER)
    assert second["id"] != first["id"]
    assert share_store.get_share(first["id"])["revoked_at"]
    with get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM shares WHERE app_id = %s AND grantee_sub = %s "
            "AND revoked_at IS NULL", (app["id"], GRANTEE),
        ).fetchone()["n"]
    assert n == 1
    with get_conn() as conn:
        conn.execute(
            "CREATE UNIQUE INDEX idx_shares_internal_grant_probe "
            "ON shares (target_kind, COALESCE(app_id, chat_id), grantee_sub) "
            "WHERE scope = 'internal' AND revoked_at IS NULL"
        )
        conn.execute("DROP INDEX idx_shares_internal_grant_probe")
        conn.commit()
