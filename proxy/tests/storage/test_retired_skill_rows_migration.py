"""The memory MCP's always-loaded card (``memory-usage``) is gone in 1.9.1:
the boot drops its ``agent_skills`` rows once (its own flag), so no agent
keeps a row for a skill no manifest declares, and leaves every other row.
"""

from __future__ import annotations

from storage import pg, schema as pg_schema
from storage.mcp import mcp_store
from storage.pg import get_conn

AGENT = "card-agent"


def _boot() -> None:
    pg.close_pool()
    with get_conn() as conn:
        pg_schema.run_migrations(conn)
        conn.commit()
    pg.close_pool()


def test_the_retired_cards_rows_go_once_and_the_rest_stay(temp_db):
    with get_conn() as conn:
        conn.execute("DELETE FROM platform_settings WHERE key = 'memory_usage_rows_dropped'")
        conn.commit()
    mcp_store.set_agent_skill(AGENT, "memory-usage", False, ["task"])
    mcp_store.set_agent_skill(AGENT, "memory-guide", True, [])
    mcp_store.set_agent_skill(AGENT, "delegation", True, ["phone"])
    _boot()
    rows = {r["skill_id"]: r for r in mcp_store.get_agent_skills(AGENT)}
    assert set(rows) == {"memory-guide", "delegation"}
    assert rows["delegation"]["exclude_from"] == ["phone"]
    # Once: a later skill that reuses the id keeps its rows.
    mcp_store.set_agent_skill(AGENT, "memory-usage", True, [])
    _boot()
    assert "memory-usage" in {r["skill_id"] for r in mcp_store.get_agent_skills(AGENT)}
