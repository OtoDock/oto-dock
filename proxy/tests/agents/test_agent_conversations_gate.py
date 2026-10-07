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


# ---------------------------------------------------------------------------
# GET /v1/chats/{id}/detail — the by-id rule the dashboard's resume shares
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_detail_follows_the_chat_owner_and_phone_calls_open_for_managers(temp_db):
    """On a Shared-only agent an assigned editor reads the ``agent::`` pool's
    detail, not a colleague's per-user chat from before the switch, and not
    a phone call; a manager reads the call. The agent's mode decides nothing
    by itself — the REST ``can_access_chat`` rule plus the phone clause."""
    import uuid
    from core.session import session_kind
    from core.session.visibility import PHONE_CHAT_OWNER, shared_chat_owner
    from storage import database as task_store
    from storage.agents import agent_store
    slug = "so-detail"
    agent_store.create_agent(slug, "SO", collaborative=False, default_scope="agent")
    call, pre_switch, pool = (str(uuid.uuid4()) for _ in range(3))
    task_store.create_chat(call, PHONE_CHAT_OWNER, slug, source_type=session_kind.PHONE.source_type)
    task_store.create_chat(pre_switch, "user-admin", slug)
    task_store.create_chat(pool, shared_chat_owner(slug), slug)
    editor = _user("u-ed", slug, "editor")
    manager = _user("u-mgr", slug, "manager")
    assert (await agents.get_chat_detail(pool, user=editor))["id"] == pool
    for cid in (call, pre_switch):
        with pytest.raises(HTTPException) as exc:
            await agents.get_chat_detail(cid, user=editor)
        assert exc.value.status_code == 403, cid
    assert (await agents.get_chat_detail(call, user=manager))["id"] == call
    with pytest.raises(HTTPException) as exc:
        await agents.get_chat_detail(pre_switch, user=manager)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_one_rule_opens_a_phone_call_to_the_agents_managers(temp_db):
    """``can_access_chat`` is the one by-id rule: the plain REST read and the
    apps audience open a phone call's conversation to the agent's managers
    (a long transcript pages for them), never to an editor, and never to a
    manager's session token or API key."""
    import uuid
    from api.agents import chats
    from core.session import session_kind
    from core.session.visibility import PHONE_CHAT_OWNER
    from services.apps import audience
    from storage import database as task_store
    from storage.agents import agent_store
    slug = "phone-read"
    agent_store.create_agent(slug, "Phone")
    call = str(uuid.uuid4())
    task_store.create_chat(call, PHONE_CHAT_OWNER, slug, source_type=session_kind.PHONE.source_type)
    chat = task_store.get_chat(call)
    manager = _user("u-mgr", slug, "manager")
    got = await chats.get_chat(call, before_id=None, limit=50, user=manager)
    assert (got.get("chat") or got)["id"] == call
    with pytest.raises(HTTPException) as exc:
        await chats.get_chat(call, before_id=None, limit=50, user=_user("u-ed", slug, "editor"))
    assert exc.value.status_code == 403
    bearer = UserContext(sub="u-mgr", email="m@t.com", name="m", role="member",
                         agents=[slug], agent_roles={slug: "manager"}, is_api_key=True)
    assert not chats.can_access_chat(bearer, chat)
    assert audience._may_open_chat("u-mgr", False, True, chat, is_manager=True)
    assert not audience._may_open_chat("u-ed", False, True, chat, is_manager=False)
    assert not hasattr(agents, "can_open_chat")
