"""The tool rows of an agent say which sessions they govern (``governs``,
2.0): an install that made ``agent_outside_tools`` before the column keeps
every outside pick exactly as it was, under ``outside``, and its key widens
to the three columns; a second boot changes nothing; the migrated table has
the catalog a fresh one has. The ``agents`` row gains the team account."""

from __future__ import annotations

import uuid

from storage import pg, schema as pg_schema
from storage.agents import outside_store, schema as agents_schema
from storage.pg import get_conn


def _two_column_shape() -> None:
    """The live table back to the shape before the column: no ``governs``,
    the two-column primary key."""
    pg.close_pool()
    with get_conn() as conn:
        conn.execute("DELETE FROM agent_outside_tools")
        conn.execute("ALTER TABLE agent_outside_tools DROP CONSTRAINT IF EXISTS agent_outside_tools_pkey")
        conn.execute("ALTER TABLE agent_outside_tools DROP COLUMN IF EXISTS governs")
        conn.execute("ALTER TABLE agent_outside_tools ADD PRIMARY KEY (agent_name, mcp_name)")
        conn.commit()
    pg.close_pool()


def _migrate() -> None:
    pg.close_pool()
    with get_conn() as conn:
        # The boot's order: the init's CREATE no-ops on the old table, then
        # the migration carries it forward.
        pg_schema.init_schema(conn)
        pg_schema.run_migrations(conn)
        conn.commit()
    pg.close_pool()


def _catalog(conn, schema: str) -> list[tuple]:
    rows = conn.execute(
        "SELECT c.conname, c.contype, pg_get_constraintdef(c.oid) AS d FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE t.relname = 'agent_outside_tools' AND n.nspname = %s ORDER BY c.conname", (schema,),
    ).fetchall()
    return [(r["conname"], r["contype"], r["d"].replace(f"{schema}.", "")) for r in rows]


def _rows(agent: str) -> list[tuple]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mcp_name, governs, access, functions, granted, changed_at FROM agent_outside_tools "
            "WHERE agent_name = %s ORDER BY mcp_name, governs", (agent,),
        ).fetchall()
    return [tuple(r.values()) for r in rows]


def test_a_table_from_before_the_column_keeps_its_picks_and_widens_its_key():
    agent = f"mig-{uuid.uuid4().hex[:8]}"
    _two_column_shape()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_outside_tools (agent_name, mcp_name, access, functions, granted, changed_at) "
            "VALUES (%s, 'crm', 'read_only', '[]', '[\"find\"]', ''), "
            "(%s, 'mail', 'custom', '[\"list\"]', '[]', '2026-11-01T00:00:00+00:00')", (agent, agent))
        conn.commit()
        assert agents_schema._primary_key(conn, "agent_outside_tools")[1] == ["agent_name", "mcp_name"]
    pg.close_pool()

    _migrate()
    assert _rows(agent) == [
        ("crm", "outside", "read_only", "[]", '["find"]', ""),
        ("mail", "outside", "custom", '["list"]', "[]", "2026-11-01T00:00:00+00:00"),
    ]
    with get_conn() as conn:
        name, cols = agents_schema._primary_key(conn, "agent_outside_tools")
        assert (name, cols) == ("agent_outside_tools_pkey", ["agent_name", "mcp_name", "governs"])
        before = _catalog(conn, conn.execute("SELECT current_schema() AS s").fetchone()["s"])
    # A second boot changes nothing.
    _migrate()
    with get_conn() as conn:
        assert _catalog(conn, conn.execute("SELECT current_schema() AS s").fetchone()["s"]) == before
    assert _rows(agent)[0][1] == "outside"
    # The same tool now takes a limit for the agent's runs beside its pick.
    outside_store.update(agent, upsert_tools=[
        {"mcp_name": "crm", "access": "custom", "functions": ["find"], "governs": outside_store.RUNS}])
    assert [r[:2] for r in _rows(agent)] == [("crm", "outside"), ("crm", "runs"), ("mail", "outside")]
    assert outside_store.get_tool_picks(agent) == {
        "crm": {"access": "read_only", "functions": [], "granted": ["find"], "changed_at": ""},
        "mail": {"access": "custom", "functions": ["list"], "granted": [], "changed_at": "2026-11-01T00:00:00+00:00"},
    }
    assert outside_store.get_tool_picks(agent, governs=outside_store.RUNS) == {
        "crm": {"access": "custom", "functions": ["find"], "granted": [], "changed_at": ""}}
    outside_store.update(agent, remove_tools=[("crm", outside_store.RUNS)])
    assert outside_store.get_tool_picks(agent, governs=outside_store.RUNS) == {}
    assert "crm" in outside_store.get_tool_picks(agent)
    outside_store.delete_agent_picks(agent)


def test_the_migrated_table_has_the_catalog_of_a_fresh_one():
    """The constraints a fresh ``CREATE TABLE`` gives and the migrated
    table's, compared by definition."""
    _two_column_shape()
    _migrate()
    with get_conn() as conn:
        schema = conn.execute("SELECT current_schema() AS s").fetchone()["s"]
        live = _catalog(conn, schema)
        conn.execute("CREATE SCHEMA IF NOT EXISTS fresh_outside_probe")
        conn.execute("SET search_path TO fresh_outside_probe")
        agents_schema.init_outside(conn)
        fresh = _catalog(conn, "fresh_outside_probe")
        conn.execute("DROP SCHEMA fresh_outside_probe CASCADE")
        conn.execute(f"SET search_path TO {schema}")
        conn.commit()
    assert sorted((c, d) for _n, c, d in live) == sorted((c, d) for _n, c, d in fresh)


def test_the_agents_row_carries_the_team_account_at_its_defaults():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT column_name, column_default FROM information_schema.columns "
            "WHERE table_name = 'agents' AND column_name IN ('team_account', 'team_account_floor') "
            "ORDER BY column_name").fetchall()
    assert [(r["column_name"], r["column_default"]) for r in row] == [
        ("team_account", "false"), ("team_account_floor", "'editor'::text")]
