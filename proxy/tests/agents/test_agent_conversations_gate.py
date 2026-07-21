"""The Conversations tab (external/phone transcripts) is operators-only.

Phone conversations are agent-scope and shared across the agent's managers/admins
BY DESIGN (phone is not per-user) — so there's no per-user filter, but viewers and
editors must not see them. The backend gate (``require_write(u, agent)``) matches
the frontend tab gate (``canManage``).

Run: cd proxy && python -m pytest tests/agents/test_agent_conversations_gate.py -v
"""

import sys

import pytest
from fastapi import HTTPException

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from auth.providers import UserContext  # noqa: E402
from api.agents import agents  # noqa: E402


def _user(sub, agent, agent_role, platform_role="member"):
    return UserContext(
        sub=sub, email=f"{sub}@t.com", name=sub, role=platform_role,
        agents=[agent], agent_roles={agent: agent_role},
    )


@pytest.mark.asyncio
async def test_conversations_gate_blocks_viewer_and_editor(monkeypatch):
    from storage import database as task_store
    monkeypatch.setattr(task_store, "get_agent_conversations", lambda *a, **k: [])
    monkeypatch.setattr(task_store, "count_agent_conversations", lambda *a, **k: 0)
    for role in ("viewer", "editor"):
        with pytest.raises(HTTPException) as exc:
            await agents.list_agent_conversations("acme", user=_user("u", "acme", role))
        assert exc.value.status_code == 403, role


@pytest.mark.asyncio
async def test_conversations_gate_allows_manager_and_admin(monkeypatch):
    from storage import database as task_store
    monkeypatch.setattr(task_store, "get_agent_conversations", lambda *a, **k: [{"id": "c1"}])
    monkeypatch.setattr(task_store, "count_agent_conversations", lambda *a, **k: 1)

    # per-agent manager who is a platform "member" — allowed
    res = await agents.list_agent_conversations("acme", user=_user("u-mgr", "acme", "manager"))
    assert res["total"] == 1

    # platform admin — allowed
    admin = UserContext(sub="a", email="a@t.com", name="a", role="admin")
    res2 = await agents.list_agent_conversations("acme", user=admin)
    assert res2["total"] == 1
