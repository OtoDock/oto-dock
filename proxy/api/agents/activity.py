"""Agents-map activity endpoint — heat + live pulse in one round trip.

ONE viewer-scoped aggregate (the bulk-count discipline of ``GET /v1/agents``
and the in-memory composition of ``GET /v1/chats/active``): two GROUP-BY
queries independent of agent count, plus a scan of the in-memory session
registries. Heat rides ``usage_records`` (rollup rows, indexed) + ``task_runs``
(indexed) — NEVER ``chat_messages`` (highest-cardinality table, no timestamp
index).

Scoping: only agents the viewer can access appear — an inaccessible (grayed)
agent on the map renders colorless, so no activity leaks through it.
``live`` = a session process exists for the agent right now (soft glow);
``streaming`` = an open turn is running right now (the hard pulse).
"""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import Depends

from auth.providers import UserContext, get_current_user, require_auth
from storage import agent_store
from storage import database as task_store

from api.agents._router import router

_WINDOW_DAYS = 7


@router.get("/v1/agents/activity")
async def agents_activity(user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)

    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=_WINDOW_DAYS)).isoformat()
    end = now.isoformat()

    usage_counts = await asyncio.to_thread(
        task_store.get_agent_activity_counts, start, end
    )
    run_counts = await asyncio.to_thread(
        task_store.count_recent_runs_by_agent, start, u.sub
    )

    # Live pulse — pure in-memory, no I/O. Union of every session registry.
    from core.layers.cli import session as cli_session
    from core.layers.codex import session as codex_session
    from core.layers.direct import session as direct_session
    from core.session import interactive_session
    from core.session.session_state import streaming_chat_ids as pump_streaming

    live: set[str] = set()
    live |= await cli_session.active_agent_names()
    live |= await codex_session.active_agent_names()
    live |= await direct_session.active_agent_names()
    live |= interactive_session.live_agent_names()

    # Streaming = open turn right now; chat→agent via one get_chat per active
    # id (same cost profile as /v1/chats/active — both sets are tiny).
    streaming: set[str] = set()
    active_ids = set(pump_streaming()) | interactive_session.streaming_chat_ids()
    for cid in active_ids:
        if not cid:
            continue
        chat = await asyncio.to_thread(task_store.get_chat, cid)
        if chat and chat.get("agent"):
            streaming.add(chat["agent"])

    agents = []
    for name in sorted(agent_store.get_agent_slugs()):
        if not u.can_access_agent(name):
            continue
        usage = usage_counts.get(name, {})
        agents.append({
            "name": name,
            "messages_7d": int(usage.get("messages", 0)),
            "usage_records_7d": int(usage.get("records", 0)),
            "task_runs_7d": int(run_counts.get(name, 0)),
            "live": name in live,
            "streaming": name in streaming,
        })

    return {"agents": agents, "window_days": _WINDOW_DAYS}


@router.get("/v1/agents/delegation-edges")
async def delegation_edges(user: UserContext | None = Depends(get_current_user)):
    """Viewer-scoped delegation edges for the map.

    An edge appears only when the viewer can see BOTH endpoints on the map:
    agents they can access, plus (grayed) member agents of departments they
    are a member of — the same visibility rule as GET /v1/departments.
    ``source`` lets the map draw manual edges as explicit bridges while
    department-compiled ones stay implicit in the tree.
    """
    u = require_auth(user)

    from storage import db_departments

    edges = await asyncio.to_thread(agent_store.get_all_delegation_edges)

    if u.is_admin:
        return {"edges": edges}

    visible: set[str] = {
        name for name in agent_store.get_agent_slugs()
        if u.can_access_agent(name)
    }
    # Grayed dept-mates: members of any department the viewer belongs to.
    depts = await asyncio.to_thread(db_departments.list_departments)
    members_by_dept: dict[str, list[str]] = {}
    for a in agent_store.get_all_agents():
        if a.get("department_id"):
            members_by_dept.setdefault(a["department_id"], []).append(a["slug"])
    for dept in depts:
        member_slugs = members_by_dept.get(dept["id"], [])
        if any(s in visible for s in member_slugs):
            visible.update(member_slugs)

    return {
        "edges": [
            e for e in edges if e["from"] in visible and e["to"] in visible
        ]
    }
