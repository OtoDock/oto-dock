"""The allowlist widening (``storage/agents/schema.migrate_agents``) and the
two 1.9.1 columns: every existing agent gains the registered engines it
lacked, once, keeping what it had; a boot that names no engines widens
nothing; the users row gains ``preferred_execution_path``, read only now as
the seed of the engine order by use.
"""

from __future__ import annotations

import json

from storage import schema
from storage.agents import agent_store
from storage.agents.agent_store import _get_execution_paths
from storage.agents.schema import ENGINE_ALLOWLIST_WIDENED_FLAG
from storage.database import get_conn

CLAUDE, CODEX, DIRECT = "claude-code-cli", "codex-cli", "direct-llm"
ENGINES = [CLAUDE, DIRECT, CODEX]   # the registry's order is the caller's


def _clear_flag():
    with get_conn() as conn:
        conn.execute("DELETE FROM platform_settings WHERE key = %s", (ENGINE_ALLOWLIST_WIDENED_FLAG,))
        conn.commit()


def _migrate(engines):
    with get_conn() as conn:
        schema.run_migrations(conn, engines=engines)
        conn.commit()
    agent_store._invalidate_cache()


def _flag() -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM platform_settings WHERE key = %s",
                           (ENGINE_ALLOWLIST_WIDENED_FLAG,)).fetchone()
        return row["value"] if row else ""


def test_mixed_rows_are_widened_once(temp_db):
    _clear_flag()
    agent_store.create_agent("one", "One", execution_path=CLAUDE)
    agent_store.create_agent("two", "Two", execution_path=CLAUDE)
    agent_store.update_agent("two", execution_paths=json.dumps([CODEX]))
    agent_store.create_agent("gone", "Gone", execution_path="retired-engine")
    _migrate(ENGINES)
    assert _flag() == "done"
    # One engine: every other registered engine appended in registry order.
    assert _get_execution_paths(agent_store.get_agent("one")) == [CLAUDE, DIRECT, CODEX]
    # Two engines: the stored extra stays first, the missing one follows.
    assert _get_execution_paths(agent_store.get_agent("two")) == [CLAUDE, CODEX, DIRECT]
    # A primary the registry lacks is never rewritten; the extras are every engine.
    assert _get_execution_paths(agent_store.get_agent("gone")) == ["retired-engine", CLAUDE, DIRECT, CODEX]
    # A second boot changes nothing: a manager's later restriction holds.
    agent_store.update_agent("one", execution_paths="")
    _migrate(ENGINES)
    assert _get_execution_paths(agent_store.get_agent("one")) == [CLAUDE]


def test_a_boot_without_engines_widens_nothing_and_keeps_the_flag_unset(temp_db):
    _clear_flag()
    agent_store.create_agent("one", "One", execution_path=CLAUDE)
    _migrate([])
    assert _flag() == ""
    assert _get_execution_paths(agent_store.get_agent("one")) == [CLAUDE]
    _migrate(ENGINES)
    assert _flag() == "done"
    assert _get_execution_paths(agent_store.get_agent("one")) == [CLAUDE, DIRECT, CODEX]


def test_the_columns_exist_with_their_defaults(temp_db):
    agent_store.create_agent("one", "One")
    assert agent_store.get_agent("one")["local_only_engines"] == ""
    assert agent_store._get_local_only_engines(agent_store.get_agent("one")) == []
    agent_store.update_agent("one", local_only_engines=json.dumps([CODEX, CODEX, ""]))
    assert agent_store._get_local_only_engines(agent_store.get_agent("one")) == [CODEX]
    agent_store.update_agent("one", local_only_engines="not json")
    assert agent_store._get_local_only_engines(agent_store.get_agent("one")) == []
    from storage import database as task_store
    # Read only now: the seed of the engine order by use.
    assert task_store.get_user("user-admin")["preferred_execution_path"] == ""


def test_a_new_agent_stores_the_extras_it_is_given(temp_db):
    row = agent_store.create_agent("one", "One", execution_path=CLAUDE,
                                   execution_paths=[CLAUDE, CODEX, "", DIRECT])
    assert _get_execution_paths(row) == [CLAUDE, CODEX, DIRECT]
    assert _get_execution_paths(agent_store.create_agent("two", "Two")) == [CLAUDE]
