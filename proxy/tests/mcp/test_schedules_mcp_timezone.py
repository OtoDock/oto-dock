"""schedules-mcp ``timezone``: the optional IANA zone on the two create
tools and on ``edit_task``, forwarded to the proxy as ``user_tz``; the
proxy's refusal shown as its sentence; ``get_task`` naming the zone a task
fires in, the platform's when the row has none."""

from __future__ import annotations

import asyncio
import json

import httpx

from tests._paths import CUSTOM_MCPS, load_mcp_server

_MCP_DIR = CUSTOM_MCPS / "schedules-mcp"


def _mod():
    return load_mcp_server(_MCP_DIR)


def _call(mod, name: str, args: dict) -> str:
    result = asyncio.run(mod.call_tool(name, args))
    return "\n".join(c.text for c in result)


def _capture_post(mod, monkeypatch, reply: dict | None = None) -> list[tuple[str, dict]]:
    posted: list[tuple[str, dict]] = []

    async def _post(path, body, headers=None):
        posted.append((path, dict(body)))
        return reply or {"task_id": "dyn-1"}

    monkeypatch.setattr(mod, "_post", _post)
    return posted


def test_the_write_tools_advertise_timezone_and_tell_agents_to_omit_it():
    mod = _mod()
    tools = {t.name: t for t in asyncio.run(mod.list_tools())}
    for name in ("create_scheduled_task", "create_one_time_task", "edit_task"):
        prop = tools[name].inputSchema["properties"]["timezone"]
        assert prop["type"] == "string"
        assert "Omit" in prop["description"] and "[Current time]" in prop["description"]
        assert "timezone" not in tools[name].inputSchema["required"]


def test_create_forwards_the_zone_as_user_tz_only_when_given(monkeypatch):
    mod = _mod()
    posted = _capture_post(mod, monkeypatch)
    text = _call(mod, "create_scheduled_task", {
        "name": "Digest", "prompt": "do", "schedule": "0 7 * * *",
        "notification_mode": "none", "timezone": "Europe/Athens",
    })
    assert posted[-1][0] == "/v1/tasks/scheduled"
    assert posted[-1][1]["user_tz"] == "Europe/Athens"
    assert "Timezone: Europe/Athens" in text

    _call(mod, "create_scheduled_task", {
        "name": "Digest", "prompt": "do", "schedule": "0 7 * * *", "notification_mode": "none",
    })
    assert "user_tz" not in posted[-1][1]

    text = _call(mod, "create_one_time_task", {
        "name": "Once", "prompt": "do", "run_at": "2026-09-20T05:00:00",
        "notification_mode": "none", "timezone": "Asia/Tokyo",
    })
    assert posted[-1][0] == "/v1/tasks/one-time"
    assert posted[-1][1]["user_tz"] == "Asia/Tokyo"
    assert "Timezone: Asia/Tokyo" in text


def test_edit_with_the_zone_alone_is_an_edit_and_is_named_timezone(monkeypatch):
    mod = _mod()
    posted = _capture_post(mod, monkeypatch, reply={})
    text = _call(mod, "edit_task", {"task_id": "dyn-1", "timezone": "Asia/Tokyo"})
    assert posted == [("/v1/tasks/dyn-1/edit", {"user_tz": "Asia/Tokyo"})]
    assert "Updated task dyn-1 (timezone)." in text


def test_the_proxys_refusal_is_shown_as_its_sentence(monkeypatch):
    mod = _mod()

    async def _post(path, body, headers=None):
        request = httpx.Request("POST", "http://proxy" + path)
        response = httpx.Response(
            400, request=request,
            content=json.dumps({"detail": "Invalid user_tz: 'No time zone found with key Mars/Olympus'"}),
        )
        raise httpx.HTTPStatusError("400", request=request, response=response)

    monkeypatch.setattr(mod, "_post", _post)
    text = _call(mod, "edit_task", {"task_id": "dyn-1", "timezone": "Mars/Olympus"})
    assert text == "API error 400: Invalid user_tz: 'No time zone found with key Mars/Olympus'"


def test_get_task_names_the_zone_a_task_fires_in(monkeypatch):
    mod = _mod()
    base = {
        "id": "dyn-1", "name": "Once", "agent": "a", "task_type": "one_time", "scope": "user",
        "created_by": "u", "created_at": "2026-09-19T00:00:00", "schedule": "",
        "run_at": "2026-09-20T05:00:00+03:00", "interval_seconds": None, "delay_seconds": None,
        "enabled": True, "next_run_time": "2026-09-20T05:00:00+03:00", "run_count": 0,
        "timeout_seconds": 600, "notification_mode": "none", "prompt": "do",
        "effective_model": "", "effective_tz": "Europe/Athens",
    }
    rows = iter([
        dict(base, user_tz="Asia/Tokyo"),
        dict(base, user_tz=None),
        dict(base, user_tz=None, task_type="trigger", run_at=None, triggers=[]),
    ])

    async def _get(path, params=None):
        return next(rows)

    monkeypatch.setattr(mod, "_get", _get)
    assert "Fires: 2026-09-20T05:00:00+03:00 (timezone Asia/Tokyo)" in _call(mod, "get_task", {"task_id": "dyn-1"})
    assert "Fires: 2026-09-20T05:00:00+03:00 (platform timezone Europe/Athens)" in _call(mod, "get_task", {"task_id": "dyn-1"})
    assert "Fires: on trigger (none wired yet)\n" in _call(mod, "get_task", {"task_id": "dyn-1"})
