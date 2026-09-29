"""triggers-mcp ``list_triggers``: a task-linked trigger names the model its
task runs on (pinned or default, with the tier), an app trigger names its
handler, a notify-only trigger stays plain."""

from __future__ import annotations

import asyncio
import os

from tests._paths import CUSTOM_MCPS, load_mcp_server

_MCP_DIR = CUSTOM_MCPS / "triggers-mcp"


def _mod():
    os.environ["TRIG_MCP_AGENT"] = "briefer"
    return load_mcp_server(_MCP_DIR)


def _row(**over):
    base = {
        "id": "t-1", "slug": "on-push", "name": "On push", "scope": "agent", "agent": "briefer",
        "enabled": True, "fired_count": 2, "last_fired_at": None, "created_by": "u-1",
        "task_id": None, "task_name": None, "notify_enabled": False, "app_slug": None,
    }
    base.update(over)
    return base


def test_list_names_the_linked_task_model_with_source_and_tier(monkeypatch):
    mod = _mod()

    async def _get(path, params=None):
        return {"triggers": [
            _row(task_id="dyn-1", task_name="Review", task_effective_model="claude-opus-5-5",
                 task_override_model="claude-opus-5-5", task_effective_model_source="pinned",
                 task_effective_model_tier=2),
            _row(id="t-2", slug="nightly", task_id="dyn-2", task_name="Nightly",
                 task_effective_model="gpt-6-sol", task_effective_model_source="agent default",
                 task_effective_model_tier=None),
            _row(id="t-3", slug="board", app_slug="deploy-board", handler="github"),
            _row(id="t-4", slug="ping", notify_enabled=True, notify_severity="info"),
        ]}

    monkeypatch.setattr(mod, "_get", _get)
    text = "\n".join(c.text for c in asyncio.run(mod._handle_list({})))
    assert "action=task=Review model=claude-opus-5-5 [pinned, tier 2]" in text
    assert "action=task=Nightly model=gpt-6-sol [agent default]" in text
    assert "action=app=deploy-board/github" in text
    assert "action=notify(info)" in text
