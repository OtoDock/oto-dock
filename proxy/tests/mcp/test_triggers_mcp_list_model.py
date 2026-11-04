"""triggers-mcp ``list_triggers``: a task-linked trigger names the model its
task runs on (pinned or default, with the tier), an app trigger names its
handler, a notify-only trigger stays plain. ``list_subscriptions``: a
subscription whose MCP is off or removed says so instead of its own status."""

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


def _subscription(**over):
    base = {
        "id": "s-1", "provider_id": "github", "account_label": "octo", "vendor_target": "octo/repo",
        "selected_events": ["push"], "scope": "user", "status": "active", "mcp_state": "on",
    }
    base.update(over)
    return base


def test_list_subscriptions_marks_a_subscription_whose_mcp_is_off_or_removed(monkeypatch):
    mod = _mod()

    async def _get(path, params=None):
        assert path == "/v1/subscriptions"
        return {"subscriptions": [
            _subscription(),
            _subscription(id="s-2", mcp_state="off"),
            _subscription(id="s-3", mcp_state="removed"),
            _subscription(id="s-4", mcp_state=None),
        ]}

    monkeypatch.setattr(mod, "_get", _get)
    lines = asyncio.run(mod._handle_list_subscriptions({}))[0].text.splitlines()
    by_id = {line.rsplit("id=", 1)[1]: (i, line) for i, line in enumerate(lines) if "id=" in line}
    assert "status=active" in by_id["s-1"][1]
    # An older proxy sends no mcp_state: the row's own status stands.
    assert "status=active" in by_id["s-4"][1]
    i, off = by_id["s-2"]
    assert "status=mcp_off" in off and "status=active" not in off
    assert lines[i + 1].strip() == ("Its MCP is turned off. It receives no events until an admin "
                                    "turns the MCP on again.")
    i, removed = by_id["s-3"]
    assert "status=mcp_removed" in removed
    assert lines[i + 1].strip() == "Its MCP was removed. It receives no events. Delete it, or install the MCP again."
    assert len(lines) == 1 + 4 + 2
