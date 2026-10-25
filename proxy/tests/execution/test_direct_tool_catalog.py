"""Deferred tool loading for Direct-LLM sessions (tool_catalog.py + the
session wiring): the split by ``always_load``, the mode/threshold decision,
the prompt catalog, ``select:`` and keyword search, sticky loads, the
``tool_search`` builtin, and the history reload after a restart."""

from __future__ import annotations

import json

import pytest

import config as app_config
from core.layers.direct import builtins as B, session as session_mod, tool_catalog as tc
from core.layers.direct.tool_catalog import ToolCatalog
from core.sandbox.sandbox import SandboxConfig


def _tool(name, desc="", props=None):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props or {}}}


MEM = _tool("mcp__memory-mcp__memory", "Save, update and delete memory topics. Long text follows.")
SCHED = _tool("mcp__schedules-mcp__create_scheduled_task",
              "Create a recurring task that runs on a cron schedule. More details follow.")
LIST = _tool("mcp__schedules-mcp__list_tasks", "List scheduled tasks")
NOTIF = _tool("mcp__notifications-mcp__create_notification", "Create a notification for a user")
WEB = {"type": "web_search_20250305", "name": "web_search"}  # provider server tool


# ---------------------------------------------------------------------------
# ToolCatalog
# ---------------------------------------------------------------------------

def test_split_by_always_load():
    read = _tool("Read", "Read a file")
    resident, deferrable = ToolCatalog.split([MEM, SCHED, LIST, read, WEB], {"memory-mcp"})
    assert [t["name"] for t in resident] == ["mcp__memory-mcp__memory", "Read", "web_search"]
    assert [t["name"] for t in deferrable] == [SCHED["name"], LIST["name"]]


def test_should_defer_modes_and_threshold_boundary():
    assert ToolCatalog.should_defer("off", [SCHED], 0) is False
    assert ToolCatalog.should_defer("on", [SCHED], 10**9) is True
    assert ToolCatalog.should_defer("on", [], 0) is False
    size = tc.estimate_tokens([SCHED, LIST])
    assert ToolCatalog.should_defer("auto", [SCHED, LIST], size) is False  # not strictly above
    assert ToolCatalog.should_defer("auto", [SCHED, LIST], size - 1) is True


def test_catalog_text_groups_by_server_with_first_sentence_and_guides():
    cat = ToolCatalog(
        deferred={t["name"]: t for t in (SCHED, LIST, NOTIF)},
        guides={"schedules-mcp": ["task-scheduling-guide"]},
    )
    text = cat.catalog_text()
    assert text.startswith("# Deferred tools")
    assert "`tool_search`" in text
    assert text.index("**notifications-mcp**") < text.index("**schedules-mcp**")
    assert "**schedules-mcp** (guide: `task-scheduling-guide` — Skill tool)" in text
    assert ("- `mcp__schedules-mcp__create_scheduled_task` — Create a recurring "
            "task that runs on a cron schedule.") in text
    assert "More details" not in text
    assert "- `mcp__schedules-mcp__list_tasks` — List scheduled tasks" in text
    assert cat.catalog_text() == text  # byte-stable


def test_first_sentence_and_server_helpers():
    assert tc.first_sentence("  Hello   world. Second.  ") == "Hello world."
    assert tc.first_sentence("No period at all") == "No period at all"
    assert tc.first_sentence("x" * 300).endswith("…")
    assert tc.server_of("mcp__a-b__c_d") == "a-b" and tc.bare_tool("mcp__a-b__c_d") == "c_d"
    assert tc.server_of("Read") == "" and tc.bare_tool("Read") == "Read"


def test_search_select_and_keywords():
    cat = ToolCatalog(deferred={t["name"]: t for t in (SCHED, LIST, NOTIF)})
    assert cat.search("select:mcp__schedules-mcp__list_tasks, create_notification") == [
        LIST["name"], NOTIF["name"],
    ]
    assert cat.search("select:schedules-mcp__list_tasks") == [LIST["name"]]
    assert cat.search("select:nope") == []
    assert cat.search("schedule a recurring task")[0] == SCHED["name"]
    assert cat.search("notification") == [NOTIF["name"]]
    assert cat.search("zzz qqq") == []
    assert len(cat.search("task", max_results=1)) == 1
    assert cat.search("") == [] and cat.search("   ") == []


def test_load_is_sticky_and_ignores_unknown_names():
    cat = ToolCatalog(deferred={SCHED["name"]: SCHED})
    assert cat.load([SCHED["name"], "mcp__x__y"]) == [SCHED]
    assert cat.load([SCHED["name"]]) == []
    assert cat.is_loaded(SCHED["name"]) and cat.loaded == [SCHED["name"]]


def test_whole_server_load_widens_to_the_server(monkeypatch):
    cat = ToolCatalog(deferred={t["name"]: t for t in (SCHED, LIST, NOTIF)})
    got = cat.load([SCHED["name"]], whole_server=True)
    assert [t["name"] for t in got] == [SCHED["name"], LIST["name"]]  # not NOTIF
    assert cat.load(["mcp__x__y"], whole_server=True) == []
    assert tc.batch_loads_for("openai_compatible") and tc.batch_loads_for("ollama")
    assert not tc.batch_loads_for("openai") and not tc.batch_loads_for("")


def test_whole_server_load_is_capped_for_huge_servers():
    big = {f"mcp__ha-mcp__tool_{i:02d}": _tool(f"mcp__ha-mcp__tool_{i:02d}", f"HA tool {i}")
           for i in range(tc.LOCAL_LOAD_MAX_TOOLS + 1)}
    cat = ToolCatalog(deferred={**big, SCHED["name"]: SCHED, LIST["name"]: LIST})
    got = cat.load(["mcp__ha-mcp__tool_03", SCHED["name"]], whole_server=True)
    names = [t["name"] for t in got]
    # The huge server loads exactly what matched; the small one widens.
    assert names == ["mcp__ha-mcp__tool_03", SCHED["name"], LIST["name"]]


@pytest.mark.asyncio
async def test_tool_search_batches_per_server_on_a_local_provider(monkeypatch):
    s = _session(monkeypatch, [MEM, SCHED, LIST, NOTIF])
    s.provider = "openai_compatible"
    out = await B.execute(s, "tool_search", {"query": "select:mcp__schedules-mcp__list_tasks"})
    assert out.startswith("Loaded 2 tool(s)")
    assert "Also loaded from the same server(s): `mcp__schedules-mcp__create_scheduled_task`" in out
    names = [t["name"] for t in s.tools]
    assert SCHED["name"] in names and LIST["name"] in names and NOTIF["name"] not in names
    # A cloud provider loads exactly what matched.
    s2 = _session(monkeypatch, [MEM, SCHED, LIST, NOTIF], sid="catalog-cloud")
    s2.provider = "openai"
    out = await B.execute(s2, "tool_search", {"query": "select:mcp__schedules-mcp__list_tasks"})
    assert out.startswith("Loaded 1 tool(s)") and "Also loaded" not in out


# ---------------------------------------------------------------------------
# Session wiring
# ---------------------------------------------------------------------------

def _session(monkeypatch, tools, mode="on", sid="catalog-sess"):
    monkeypatch.setattr("core.layers.direct.session.config.get_agent_model", lambda agent: "")
    monkeypatch.setattr(app_config, "DIRECT_LLM_TOOL_SEARCH", mode)
    monkeypatch.setattr(
        session_mod, "_registry_facts",
        lambda: ({"memory-mcp"}, {"schedules-mcp": ["task-scheduling-guide"]}),
    )
    s = session_mod.DirectSession(sid, "pa", "You are a test agent.",
                                  mcp_manager=None, provider="openai")
    s.tools = list(tools) + s.tools  # MCP tools first, as the manager lists them
    session_mod._apply_deferred_tools(s)
    return s


def test_apply_deferred_tools_rewires_the_session(monkeypatch):
    s = _session(monkeypatch, [MEM, SCHED, LIST, NOTIF])
    names = [t["name"] for t in s.tools]
    assert MEM["name"] in names and "tool_search" in names and "Read" in names
    assert SCHED["name"] not in names and NOTIF["name"] not in names
    assert s.catalog is not None
    assert set(s.catalog.deferred) == {SCHED["name"], LIST["name"], NOTIF["name"]}
    assert s.system_prompt.startswith("You are a test agent.")
    assert "\n\n---\n\n# Deferred tools" in s.system_prompt


def test_apply_off_keeps_everything_resident(monkeypatch):
    s = _session(monkeypatch, [MEM, SCHED], mode="off")
    names = [t["name"] for t in s.tools]
    assert SCHED["name"] in names and "tool_search" not in names
    assert s.catalog is None and "# Deferred tools" not in s.system_prompt


def test_auto_below_threshold_stays_resident(monkeypatch):
    monkeypatch.setattr(app_config, "DIRECT_LLM_TOOL_SEARCH_THRESHOLD_TOKENS", 10**6)
    s = _session(monkeypatch, [MEM, SCHED], mode="auto")
    assert s.catalog is None
    monkeypatch.setattr(app_config, "DIRECT_LLM_TOOL_SEARCH_THRESHOLD_TOKENS", 0)
    s = _session(monkeypatch, [MEM, SCHED], mode="auto")
    assert s.catalog is not None


@pytest.mark.asyncio
async def test_tool_search_builtin_loads_and_reports(monkeypatch, tmp_path):
    s = _session(monkeypatch, [MEM, SCHED, LIST, NOTIF])
    out = await B.execute(s, "tool_search", {"query": "select:mcp__schedules-mcp__create_scheduled_task"})
    assert out.startswith("Loaded 1 tool(s)")
    assert "`mcp__schedules-mcp__create_scheduled_task` — Create a recurring task" in out
    assert SCHED["name"] in [t["name"] for t in s.tools]
    assert "guide for" not in out  # no skills dir → no Skill pointer

    out = await B.execute(s, "tool_search", {"query": "select:mcp__schedules-mcp__create_scheduled_task"})
    assert out.startswith("Already loaded:") and "(already loaded)" in out
    assert [t["name"] for t in s.tools].count(SCHED["name"]) == 1

    out = await B.execute(s, "tool_search", {"query": "qqq zzz"})
    assert out.startswith("No deferred tools match")
    assert (await B.execute(s, "tool_search", {"query": ""})).startswith("Error: query is required")

    # With the guide materialized in the session's skills dir, the result
    # points at it (pointer only — never the body).
    claude_dir = tmp_path / ".claude"
    (claude_dir / "skills" / "task-scheduling-guide").mkdir(parents=True)
    (claude_dir / "skills" / "task-scheduling-guide" / "SKILL.md").write_text("---\nname: x\n---\nBODY")
    (tmp_path / "agents").mkdir()
    (tmp_path / "mcps").mkdir()
    s.sandbox_cfg = SandboxConfig(
        role="manager", username="", agent_name="pa", is_admin_agent=False,
        host_agents_dir=tmp_path / "agents", host_mcps_dir=tmp_path / "mcps",
        host_claude_dir=claude_dir, net_forwards=["8400"],
    )
    out = await B.execute(s, "tool_search", {"query": "list tasks"})
    assert LIST["name"] in [t["name"] for t in s.tools]
    assert "guide for schedules-mcp: `task-scheduling-guide` (Skill tool)" in out
    assert "BODY" not in out


@pytest.mark.asyncio
async def test_skill_loads_the_deferred_tools_its_guide_names(monkeypatch, tmp_path):
    """Reading a guide loads the deferred tools of the guide's own MCP that
    the guide mentions — the model asked for the guide because it is about
    to call them (the live failure: a local model read `write_xlsx` in the
    guide, had no such tool, and substituted the ones it had)."""
    s = _session(monkeypatch, [MEM, SCHED, LIST, NOTIF])
    claude_dir = tmp_path / ".claude"
    (claude_dir / "skills" / "task-scheduling-guide").mkdir(parents=True)
    (claude_dir / "skills" / "task-scheduling-guide" / "SKILL.md").write_text(
        "---\nname: task-scheduling-guide\n---\n"
        "Use `create_scheduled_task` for cron and `list_tasks` to read back.\n"
    )
    (tmp_path / "agents").mkdir()
    (tmp_path / "mcps").mkdir()
    s.sandbox_cfg = SandboxConfig(
        role="manager", username="", agent_name="pa", is_admin_agent=False,
        host_agents_dir=tmp_path / "agents", host_mcps_dir=tmp_path / "mcps",
        host_claude_dir=claude_dir, net_forwards=["8400"],
    )

    class _Provider:
        name = "schedules-mcp"
        server_name = ""

    monkeypatch.setattr(
        "services.mcp.mcp_registry.find_skill_provider",
        lambda sid: _Provider() if sid == "task-scheduling-guide" else None,
    )
    out = await B.execute(s, "Skill", {"name": "task-scheduling-guide"})
    assert out.startswith("# Skill: task-scheduling-guide")
    assert "Loaded the tools this guide describes" in out
    assert "`mcp__schedules-mcp__create_scheduled_task`" in out
    assert "`mcp__schedules-mcp__list_tasks`" in out
    names = [t["name"] for t in s.tools]
    assert SCHED["name"] in names and LIST["name"] in names
    assert NOTIF["name"] not in names  # another server's tool, not mentioned
    # A second read has nothing new to load → no footer.
    out = await B.execute(s, "Skill", {"name": "task-scheduling-guide"})
    assert "Loaded the tools" not in out


@pytest.mark.asyncio
async def test_tool_search_inactive_without_a_catalog(monkeypatch):
    s = _session(monkeypatch, [MEM, SCHED], mode="off")
    assert "not active" in await B.execute(s, "tool_search", {"query": "task"})


def test_history_reload_reloads_the_deferred_tools_used_in_the_chat(monkeypatch):
    from core.layers.direct.layer import _rebuild_history_from_db
    s = _session(monkeypatch, [MEM, SCHED, LIST, NOTIF])
    s.model = "test-model"
    rows = [
        {"role": "user", "content": "schedule it"},
        {"role": "event", "event_type": "tool",
         "event_data": json.dumps({"type": "tool", "name": LIST["name"], "tool_id": "t1"})},
        {"role": "event", "event_type": "tool",
         "event_data": json.dumps({"type": "tool", "name": "mcp__gone-mcp__x", "tool_id": "t2"})},
        {"role": "event", "event_type": "tool", "event_data": "not json"},
        {"role": "event", "event_type": "tool",
         "event_data": json.dumps({"type": "tool", "name": "Read", "tool_id": "t3"})},
        {"role": "event", "event_type": "artifact", "event_data": "{}"},
        {"role": "assistant", "content": "done"},
    ]
    monkeypatch.setattr("storage.database.get_chat", lambda cid: {"id": cid, "last_turn_aborted": False})
    monkeypatch.setattr("storage.database.get_chat_messages", lambda cid: rows)
    monkeypatch.setattr("core.layers.direct.layer.app_config.get_model_context_window", lambda m: 100_000)
    _rebuild_history_from_db(s, s.session_id, chat_id="chat-1")
    assert s.messages == [{"role": "user", "content": "schedule it"},
                          {"role": "assistant", "content": "done"}]
    names = [t["name"] for t in s.tools]
    assert LIST["name"] in names and SCHED["name"] not in names
    assert s.catalog.loaded == [LIST["name"]]


def test_a_resumed_worker_rebuilds_every_round_and_skips_this_rounds_prompt(monkeypatch):
    """A Direct LLM worker's continue keeps its conversation: the rounds of
    the runs on the session, each in its own chat, in start order; the
    round's own prompt row is left out by its id (a message that landed
    after it stays), and an aborted turn is trimmed in its own chat only."""
    from core.layers.direct.layer import _rebuild_history_from_db
    s = _session(monkeypatch, [MEM])
    s.model = "test-model"
    chats = {
        "run-1": {"id": "run-1", "last_turn_aborted": False},
        "run-2": {"id": "run-2", "last_turn_aborted": True},
        "run-3": {"id": "run-3", "last_turn_aborted": False},
    }
    rows = {
        "run-1": [{"id": 1, "role": "user", "content": "draft the plan"},
                  {"id": 2, "role": "assistant", "content": "plan v1"}],
        "run-2": [{"id": 3, "role": "user", "content": "add costs"},
                  {"id": 4, "role": "assistant", "content": "plan v2"},
                  {"id": 5, "role": "user", "content": "and a timeline"},
                  {"id": 6, "role": "assistant", "content": "partial"}],
        "run-3": [{"id": 7, "role": "user", "content": "now the summary"},
                  {"id": 8, "role": "user", "content": "a person's note"}],
    }
    monkeypatch.setattr("storage.database.round_chats_of_session",
                        lambda sid: ["run-1", "run-2", "run-3"])
    monkeypatch.setattr("storage.database.get_chat", lambda cid: chats[cid])
    monkeypatch.setattr("storage.database.get_chat_messages", lambda cid: rows[cid])
    monkeypatch.setattr("core.layers.direct.layer.app_config.get_model_context_window",
                        lambda m: 100_000)
    _rebuild_history_from_db(s, s.session_id, chat_id="run-3", skip_row=7)
    assert s.messages == [
        {"role": "user", "content": "draft the plan"},
        {"role": "assistant", "content": "plan v1"},
        {"role": "user", "content": "add costs"},
        {"role": "assistant", "content": "plan v2"},
        {"role": "user", "content": "a person's note"},
    ]


def test_the_rounds_of_a_session_are_its_runs_chats_in_start_order(temp_db):
    from storage import database as task_store
    from storage.pg import get_conn
    for run_id, chat_id, started in (("r-b", "chat-b", "2026-10-08T10:05:00+00:00"),
                                     ("r-a", "chat-a", "2026-10-08T10:00:00+00:00"),
                                     ("r-a2", "chat-a", "2026-10-08T10:10:00+00:00"),
                                     ("r-x", "chat-x", "2026-10-08T09:00:00+00:00")):
        task_store.create_run(run_id, "t-1", "agent-a", "delegate", None, "p")
        with get_conn() as conn:
            conn.execute("UPDATE task_runs SET session_id=%s, chat_id=%s, started_at=%s "
                         "WHERE id=%s",
                         ("s-other" if run_id == "r-x" else "s-1", chat_id, started, run_id))
            conn.commit()
    assert task_store.round_chats_of_session("s-1") == ["chat-a", "chat-b"]
    assert task_store.round_chats_of_session("") == []
