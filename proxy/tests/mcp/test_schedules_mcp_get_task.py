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
    assert "Mode: Don't Ask" in text
    assert text.rstrip().endswith("Read the digests and brief me.\nKeep it short.\n```")


def test_get_task_names_an_auto_task(monkeypatch):
    mod = _mod()

    async def _get(path, params=None):
        return {**_TASK, "permission_mode": "autoReview"}

    monkeypatch.setattr(mod, "_get", _get)
    assert "Mode: Auto" in _call(mod, "get_task", {"task_id": "dyn-abc12345"})


def test_the_write_tools_take_a_permission_mode_and_send_it(monkeypatch):
    mod = _mod()
    tools = {t.name: t for t in asyncio.run(mod.list_tools())}
    for name in ("create_scheduled_task", "create_one_time_task", "edit_task"):
        prop = tools[name].inputSchema["properties"]["permission_mode"]
        assert prop["enum"] == ["dontAsk", "autoReview"]
    sent: list[tuple[str, dict]] = []

    async def _post(path, body, headers=None):
        sent.append((path, body))
        return {"id": "dyn-new", "name": body.get("name", ""), "scope": "user"}

    monkeypatch.setattr(mod, "_post", _post)
    _call(mod, "create_scheduled_task", {"name": "n", "prompt": "p", "schedule": "0 7 * * *",
                                         "notification_mode": "manual",
                                         "permission_mode": "autoReview"})
    _call(mod, "edit_task", {"task_id": "dyn-new", "permission_mode": "dontAsk"})
    assert sent[0][0] == "/v1/tasks/scheduled" and sent[0][1]["permission_mode"] == "autoReview"
    assert sent[-1] == ("/v1/tasks/dyn-new/edit", {"permission_mode": "dontAsk"})


def test_every_tool_is_annotated_with_its_tier():
    from services.mcp.mcp_permissions import hint_tier
    tools = {t.name: t for t in asyncio.run(_mod().list_tools())}
    tiers = {name: hint_tier(t.annotations.model_dump(exclude_none=True) if t.annotations else None)
             for name, t in tools.items()}
    assert tiers == {
        "create_scheduled_task": "standard", "create_one_time_task": "standard",
        "schedule_continuation": "standard", "run_task": "sensitive",
        "get_task_result": "open", "list_tasks": "open", "get_task": "open",
        "delete_task": "sensitive", "pause_task": "standard", "resume_task": "standard",
        "edit_task": "standard", "get_task_history": "open", "cancel_task_run": "sensitive",
    }


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


def test_list_and_get_name_the_tasks_own_machine(monkeypatch):
    mod = _mod()
    own = dict(_TASK, target_machine="m-1", target_machine_label="laptop",
               target_machine_state="disconnected")
    plain = dict(_TASK, id="dyn-plain", target_machine="", target_machine_label="",
                 target_machine_state="")

    async def _list(path, params=None):
        return {"tasks": [own, plain]}

    monkeypatch.setattr(mod, "_get", _list)
    lines = _call(mod, "list_tasks", {}).splitlines()
    own_line = next(line for line in lines if "dyn-abc12345" in line)
    assert own_line.endswith(", machine: laptop)")
    assert "machine:" not in next(line for line in lines if "dyn-plain" in line)

    async def _one(path, params=None):
        return own

    monkeypatch.setattr(mod, "_get", _one)
    assert "Machine: laptop (disconnected)" in _call(mod, "get_task", {"task_id": "dyn-abc12345"})

    async def _plain(path, params=None):
        return plain

    monkeypatch.setattr(mod, "_get", _plain)
    assert "Machine:" not in _call(mod, "get_task", {"task_id": "dyn-plain"})


def test_history_says_where_each_run_ran(monkeypatch):
    mod = _mod()

    async def _get(path, params=None):
        assert path == "/v1/tasks/runs"
        return {"total": 3, "runs": [
            {"id": "r1", "task_id": "dyn-1", "status": "completed", "started_at": "t1",
             "duration_ms": 10, "execution_target": "local", "execution_target_label": "OtoDock platform"},
            {"id": "r2", "task_id": "dyn-1", "status": "failed", "started_at": "t2",
             "execution_target": "4f1c2e9a-0000-4000-8000-000000000000"},
            {"id": "r3", "task_id": "dyn-1", "status": "pending", "execution_target": "local",
             "execution_target_label": "OtoDock platform"},
        ]}

    monkeypatch.setattr(mod, "_get", _get)
    lines = _call(mod, "get_task_history", {}).splitlines()
    assert lines[1].endswith("duration=10ms ran on: OtoDock platform")
    # No label from the API: the id's first eight characters.
    assert lines[2].endswith("ran on: 4f1c2e9a")
    # A run that has not started names no place.
    assert "ran on" not in lines[3]
