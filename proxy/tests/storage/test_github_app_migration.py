"""The 1.9.0 GitHub-App columns migrate forward on a released install's
database through the schema module's migration step: an account row
and a binding row written before the columns existed come through the boot
with the defaults (no meta, the account itself as the identity, every
repository with writes allowed, a person's own account choice for an agent
too), the columns are there afterwards, and a second boot changes nothing.
"""

from __future__ import annotations

import uuid

from storage import pg, schema as pg_schema
from storage.identity import credential_store
from storage.pg import get_conn

OWNER = "user-manager"     # seeded by conftest
COLUMNS = (("user_credential_accounts", "meta"), ("service_agent_bindings", "installation_id"),
           ("service_agent_bindings", "repositories"), ("service_agent_bindings", "read_only"),
           ("agent_account_bindings", "repositories"), ("agent_account_bindings", "read_only"))


def _old_shape() -> None:
    pg.close_pool()
    with get_conn() as conn:
        for table, column in COLUMNS:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
        conn.commit()
    pg.close_pool()


def _migrate(times: int = 1) -> None:
    pg.close_pool()
    for _ in range(times):
        with get_conn() as conn:
            pg_schema.init_schema(conn)
            pg_schema.run_migrations(conn)
            conn.commit()
    pg.close_pool()


def _columns(table: str) -> set[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s", (table,),
        ).fetchall()
        return {r["column_name"] for r in rows}


def test_rows_written_before_the_columns_come_through_the_boot():
    mcp = f"mig-github-{uuid.uuid4().hex[:8]}"
    _old_shape()
    try:
        assert "meta" not in _columns("user_credential_accounts")
        assert "installation_id" not in _columns("service_agent_bindings")
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO user_credential_accounts "
                "(user_sub, mcp_name, account_label, display_email, is_default, created_at) "
                "VALUES (%s, %s, 'gh', 'alice', TRUE, NOW()::text)", (OWNER, mcp),
            )
            conn.execute(
                "INSERT INTO service_agent_bindings "
                "(mcp_name, agent_name, account_label, account_owner_sub, set_by, set_at) "
                "VALUES (%s, 'mig-agent', 'gh', %s, %s, NOW()::text)", (mcp, OWNER, OWNER),
            )
            conn.execute(
                "INSERT INTO agent_account_bindings (user_sub, mcp_name, agent_name, account_label, set_at) "
                "VALUES (%s, %s, 'mig-agent', 'gh', NOW()::text)", (OWNER, mcp),
            )
            conn.commit()
        _migrate(times=2)
        assert "meta" in _columns("user_credential_accounts")
        assert "installation_id" in _columns("service_agent_bindings")
        accounts = credential_store.list_user_accounts(OWNER, mcp)
        assert [a["account_label"] for a in accounts] == ["gh"]
        assert accounts[0]["meta"] == {}
        assert credential_store.get_account_meta(OWNER, mcp, "gh") == {}
        with get_conn() as conn:
            row = conn.execute(
                "SELECT installation_id, repositories, read_only FROM service_agent_bindings "
                "WHERE mcp_name=%s AND agent_name='mig-agent'", (mcp,),
            ).fetchone()
        assert (row["installation_id"], row["repositories"], row["read_only"]) == ("", "[]", False)
        assert credential_store.get_account_agent_binding(OWNER, mcp, "mig-agent") == ("gh", (), False)
        with get_conn() as conn:
            assert pg_schema.check_schema_drift(conn) == []
    finally:
        pg.close_pool()
        with get_conn() as conn:
            conn.execute("DELETE FROM service_agent_bindings WHERE mcp_name=%s", (mcp,))
            conn.execute("DELETE FROM agent_account_bindings WHERE mcp_name=%s", (mcp,))
            conn.execute("DELETE FROM user_credential_accounts WHERE mcp_name=%s", (mcp,))
            conn.commit()
        # The DROP and the ADD reordered the rows' columns: every pooled
        # session's cached plan of a SELECT * is stale, so the pools close.
        pg.close_pool(timeout=2.0)

