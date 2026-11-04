"""The 1.9.1 generation skills migration: an install copied each skill's
old default (``["task"]``) into ``agent_skills`` at assignment, so the
boot rewrites the rows still holding exactly it to the new default
(``["meeting"]``), keeps a row an admin changed, runs once (its flag), and
leaves every other skill alone.
"""

from __future__ import annotations

import json

from storage import pg, schema as pg_schema
from storage.mcp import mcp_store
from storage.pg import get_conn

AGENT = "gen-agent"


def _boot() -> None:
    pg.close_pool()
    with get_conn() as conn:
        pg_schema.run_migrations(conn)
        conn.commit()
    pg.close_pool()


def _rows() -> dict[str, list[str]]:
    return {r["skill_id"]: r["exclude_from"] for r in mcp_store.get_agent_skills(AGENT)}


def _forget_the_flag() -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM platform_settings WHERE key = 'generation_skills_in_tasks'")
        conn.commit()


def test_the_rows_holding_the_old_default_move_and_an_admins_choice_stays(temp_db):
    _forget_the_flag()
    mcp_store.set_agent_skill(AGENT, "music-gen-usage", True, ["task"])
    mcp_store.set_agent_skill(AGENT, "image-gen-usage", True, ["task", "phone"])
    mcp_store.set_agent_skill(AGENT, "video-gen-usage", True, [])
    mcp_store.set_agent_skill(AGENT, "agent-creation", True, ["task"])
    _boot()
    assert _rows() == {"music-gen-usage": ["meeting"], "image-gen-usage": ["task", "phone"],
                       "video-gen-usage": [], "agent-creation": ["task"]}
    # Once: a row an admin sets back to tasks only after the upgrade stays.
    mcp_store.set_agent_skill(AGENT, "music-gen-usage", True, ["task"])
    _boot()
    assert _rows()["music-gen-usage"] == ["task"]
    with get_conn() as conn:
        flag = conn.execute("SELECT value FROM platform_settings WHERE key = "
                            "'generation_skills_in_tasks'").fetchone()
    assert flag["value"] == "done"
    assert json.loads(json.dumps(_rows()["music-gen-usage"])) == ["task"]
