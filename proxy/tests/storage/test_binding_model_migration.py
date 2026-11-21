"""A session binding records its model (``subscription_session_bindings.model``, 2.0). A database from before the
column keeps every live binding as it was, with no model, and a second boot changes nothing. The pool restores
such a binding after a restart and judges its session by its chat."""

from __future__ import annotations

import uuid

from services.engines import subscription_pool as sp
from storage import pg, schema as pg_schema
from storage.billing import subscription_store
from storage.pg import get_conn


def _shape_before_the_column(rows: list[tuple]) -> None:
    pg.close_pool()
    with get_conn() as conn:
        conn.execute("ALTER TABLE subscription_session_bindings DROP COLUMN IF EXISTS model")
        for row in rows:
            conn.execute(
                "INSERT INTO subscription_session_bindings "
                "(session_id, subscription_id, layer, user_sub, scope_key, bound_at) VALUES (%s,%s,%s,%s,%s,%s)",
                row)
        conn.commit()
    pg.close_pool()


def _boot() -> None:
    pg.close_pool()
    with get_conn() as conn:
        pg_schema.init_schema(conn)
        pg_schema.run_migrations(conn)
        conn.commit()
    pg.close_pool()


def _rows(ids: list[str]) -> list[tuple]:
    with get_conn() as conn:
        found = conn.execute(
            "SELECT session_id, subscription_id, layer, user_sub, scope_key, bound_at, model "
            "FROM subscription_session_bindings WHERE session_id = ANY(%s) ORDER BY session_id", (ids,)).fetchall()
    return [tuple(r.values()) for r in found]


def test_live_bindings_from_before_the_column_survive_the_upgrade_and_restore():
    tag = uuid.uuid4().hex[:8]
    old = [(f"sess-{tag}-a", f"sub-{tag}", "claude-code-cli", "", f"local:/agents/x/workspace/.claude-{tag}",
            "2026-11-01T10:00:00+00:00"),
           (f"sess-{tag}-b", f"sub-{tag}", "codex-cli", "u-1", f"local:/agents/x/users/u/.codex-{tag}",
            "2026-11-01T10:05:00+00:00")]
    ids = [r[0] for r in old]
    _shape_before_the_column(old)
    try:
        _boot()
        assert _rows(ids) == [(*r, None) for r in old]
        _boot()
        assert _rows(ids) == [(*r, None) for r in old]

        # The pool restores a binding of before the column, with no model of its own.
        sp._session_subscriptions.pop(ids[0], None)
        assert sp.restore_session_binding(ids[0]) == f"sub-{tag}"
        assert sp._session_scope_keys[ids[0]] == old[0][4]
        assert ids[0] not in sp._session_models

        # A binding written now records its model.
        subscription_store.upsert_session_binding(ids[1], f"sub-{tag}", layer="codex-cli", user_sub="u-1",
                                                  scope_key=old[1][4], model="gpt-6.1-sol")
        assert _rows([ids[1]])[0][-1] == "gpt-6.1-sol"
    finally:
        sp.release_subscription(ids[0])
        sp.flush_binding_writes()
        with get_conn() as conn:
            conn.execute("DELETE FROM subscription_session_bindings WHERE session_id = ANY(%s)", (ids,))
            conn.commit()


def test_restored_bindings_keep_the_order_they_were_bound_in():
    """After a restart, two sessions of one folder come back in any order: each keeps its own bind instant, in
    memory and in the rows, so the folder's newest account stays the one the sticky pick reads."""
    tag = uuid.uuid4().hex[:8]
    scope = f"local:/agents/x/users/u/.codex-{tag}"
    old, new = f"sess-{tag}-old", f"sess-{tag}-new"
    subscription_store.upsert_session_binding(old, f"sub-a-{tag}", layer="codex-cli", user_sub="u-1",
                                              scope_key=scope, bound_at="2026-11-01T10:00:00+00:00")
    subscription_store.upsert_session_binding(new, f"sub-b-{tag}", layer="codex-cli", user_sub="u-1",
                                              scope_key=scope, bound_at="2026-11-01T11:00:00+00:00")
    try:
        for sid in (old, new):
            sp._session_subscriptions.pop(sid, None)
        assert sp.restore_session_binding(new) == f"sub-b-{tag}"     # the newer first
        assert sp.restore_session_binding(old) == f"sub-a-{tag}"
        sp.flush_binding_writes()
        assert sp._session_bound_at[new] > sp._session_bound_at[old]
        assert [r["session_id"] for r in subscription_store.list_scope_bindings(scope)] == [new, old]
        assert sp._sticky_subscription_id(scope) == f"sub-b-{tag}"
    finally:
        for sid in (old, new):
            sp.release_subscription(sid)
        sp.flush_binding_writes()
        with get_conn() as conn:
            conn.execute("DELETE FROM subscription_session_bindings WHERE session_id = ANY(%s)", ([old, new],))
            conn.commit()
