"""schedules-mcp ``get_task``: the one read that shows a task's prompt before
it has run, with what it runs on (tiered), the triggers pointing at a
trigger-type task, and the warnings for a pin that no longer resolves."""

from __future__ import annotations

import asyncio

from tests._paths import CUSTOM_MCPS, load_mcp_server

_MCP_DIR = CUSTOM_MCPS / "schedules-mcp"


def _mod():
    return load_mcp_server(_MCP_DIR)


def _call(mod, name: str, args: dict) -> str:
    result = asyncio.run(mod.call_tool(name, args))
    return "\n".join(c.text for c in result)


_TASK = {
    "id": "dyn-abc12345", "name": "Daily briefing", "agent": "briefer",
    "task_type": "scheduled", "scope": "user", "created_by": "user-1",
    "created_at": "2026-09-13T10:00:00", "schedule": "0 7 * * *", "user_tz": "Europe/Athens",
    "run_at": None, "delay_seconds": None, "interval_seconds": None,
    "enabled": True, "next_run_time": "2026-09-14T07:00:00+03:00",
    "run_count": 3, "max_runs": None, "until_at": None,
    "timeout_seconds": 600, "notification_mode": "manual", "notify_severity": "info",
    "target_chat_id": None, "prompt": "Read the digests and brief me.\nKeep it short.",
    "override_model": "claude-opus-5-5", "override_execution_path": "",
    "effective_model": "claude-opus-5-5", "effective_execution_path": "claude-code-cli",
    "effective_model_source": "pinned", "effective_model_tier": 2, "tier_label": "strong",
    "pin_warnings": [], "triggers": [], "fired": False, "on_complete_chat_id": None,
}


def test_get_task_is_advertised_with_the_read_arguments():
    mod = _mod()
    tools = asyncio.run(mod.list_tools())
    tool = next(t for t in tools if t.name == "get_task")
    assert tool.inputSchema["required"] == ["task_id"]
    # Task ids are global: no `agent` argument, a cross-agent read is the id alone.
    assert set(tool.inputSchema["properties"]) == {"task_id"}
    assert "prompt" in tool.description


def test_get_task_renders_the_prompt_and_the_tiered_model(monkeypatch):
    mod = _mod()

    async def _get(path, params=None):
        assert path == "/v1/tasks/dyn-abc12345"
        return dict(_TASK)

    monkeypatch.setattr(mod, "_get", _get)
    text = _call(mod, "get_task", {"task_id": "dyn-abc12345"})
    assert "Task: dyn-abc12345 — Daily briefing" in text
    assert "Fires: 0 7 * * * (timezone Europe/Athens)" in text
    assert "Runs so far: 3" in text
    assert "Runs on: claude-opus-5-5 [pinned, tier 2 strong]" in text
    assert "Notification: manual (info)" in text
    assert text.rstrip().endswith("Read the digests and brief me.\nKeep it short.\n```")


def test_get_task_shows_triggers_and_warnings_for_a_trigger_type_task(monkeypatch):
    mod = _mod()
    row = dict(_TASK, task_type="trigger", schedule="", user_tz="",
               override_model="", effective_model_source="layer default",
               override_execution_path="codex-cli", effective_execution_path="codex-cli",
               effective_model="gpt-6-sol", effective_model_tier=None, tier_label="",
               pin_warnings=["the pinned engine 'codex-cli' is not enabled for this agent"],
               triggers=[{"id": "t-1", "name": "On push", "slug": "on-push", "scope": "user",
                          "enabled": True, "subscription_id": "sub-1", "webhook_path": None,
                          "fired_count": 4, "last_fired_at": "2026-09-13T08:00:00"}])

    async def _get(path, params=None):
        return row

    monkeypatch.setattr(mod, "_get", _get)
    text = _call(mod, "get_task", {"task_id": "dyn-abc12345"})
    assert "Fires: on trigger (1 wired)" in text
    assert "Runs on: gpt-6-sol [layer default, untiered] via codex-cli [pinned layer]" in text
    assert "Warning: the pinned engine 'codex-cli' is not enabled for this agent" in text
    assert "• On push [user] [active] fires=4 last=2026-09-13T08:00:00 vendor subscription id=t-1" in text


def test_get_task_names_the_wiring_call_when_nothing_points_at_it(monkeypatch):
    mod = _mod()
    row = dict(_TASK, task_type="trigger", schedule="", triggers=[])

    async def _get(path, params=None):
        return row

    monkeypatch.setattr(mod, "_get", _get)
    text = _call(mod, "get_task", {"task_id": "dyn-abc12345"})
    assert "Fires: on trigger (none wired yet)" in text
    assert "create_trigger(task_id='dyn-abc12345')" in text


def test_list_tasks_tags_the_tier_and_labels_trigger_rows(monkeypatch):
    mod = _mod()

    async def _get(path, params=None):
        assert path == "/v1/tasks"
        return {"tasks": [dict(_TASK), dict(_TASK, id="dyn-t", task_type="trigger", schedule="",
                                           triggers=None, effective_model_source="agent default",
                                           override_model="", effective_model_tier=1,
                                           tier_label="frontier", effective_model="claude-fable-5-1"),
                          dict(_TASK, id="app-1", name="Kanban: republish", task_type="app",
                               schedule="0 6 * * 1-5")]}

    monkeypatch.setattr(mod, "_get", _get)
    text = _call(mod, "list_tasks", {})
    assert "runs on: claude-opus-5-5 [pinned, tier 2 strong]" in text
    assert "[trigger] dyn-t — Daily briefing (briefer, on trigger, active" in text
    assert "runs on: claude-fable-5-1 [agent default, tier 1 frontier]" in text
    # An app handler row runs no LLM turn: no model claim on it.
    app_line = next(line for line in text.splitlines() if "app-1" in line)
    assert "runs on" not in app_line and "0 6 * * 1-5" in app_line
