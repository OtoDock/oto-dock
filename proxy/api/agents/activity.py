"""Agents-map activity endpoint — heat + live pulse in one round trip.

ONE viewer-scoped aggregate (the bulk-count discipline of ``GET /v1/agents``):
two GROUP-BY queries independent of agent count, plus the viewer's
Active-now rows. Heat rides ``usage_records`` (rollup rows, indexed) +
``task_runs`` (indexed) — NEVER ``chat_messages`` (highest-cardinality table,
no timestamp index).

Scoping: only agents the viewer can access appear — an inaccessible (grayed)
agent on the map renders colorless, so no activity leaks through it. Inside
them the viewer sees what the Active-now list shows them and nothing else:
another person's personal chat or user-scope task never warms or lights an
agent (``api/agents/chats.py`` ``viewer_active_rows``, the same gates).
``live`` = a chat the viewer may see is warming or streaming right now (soft
glow); ``streaming`` = one has an open turn right now (the hard pulse).
"""

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import Depends

from api.agents._router import router
from auth.providers import UserContext, get_current_user, require_auth
from storage import database as task_store
from storage.agents import agent_store
from ws import chat_phase

_WINDOW_DAYS = 7


@router.get("/v1/agents/activity")
async def agents_activity(user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)

    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=_WINDOW_DAYS)).isoformat()
    end = now.isoformat()

    def _counts() -> tuple[dict, dict]:
        from core.session.visibility import is_shared_only
        shared = [a for a in (u.agents or ()) if is_shared_only(a)]
        return (task_store.get_agent_activity_counts(start, end, u.sub, shared),
                task_store.count_recent_runs_by_agent(start, u.sub))

    usage_counts, run_counts = await asyncio.to_thread(_counts)

    # The pulse: the viewer's own Active-now rows (open turns and warmups),
    # never the process-wide session registries.
    from api.agents.chats import viewer_active_rows
    live: set[str] = set()
    streaming: set[str] = set()
    for row in await viewer_active_rows(u):
        if row["status"] == chat_phase.STREAMING:
            streaming.add(row["agent"])
            live.add(row["agent"])
        elif row["status"] == chat_phase.WARMING:
            live.add(row["agent"])

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

    from storage.agents import db_departments

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
