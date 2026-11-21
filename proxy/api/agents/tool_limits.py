"""The tool limits of an agent: which functions of each enabled MCP the
agent may use in its own runs and in people's chats, set by a manager.
``GET`` / ``PUT /v1/agents/{name}/tool-limits``, managers only; a ``PUT``
changes only what it names. The rows are the Outside people picks' table
(``storage/agents/outside_store.py``, one column per kind of session), read
at a session's build by ``services/mcp/tool_limits.py``. A session token
(the agent's own self-config tool, or its shell) may only tighten a limit:
widening or removing one takes a manager at the dashboard. Attaches to the
shared package router."""

import logging

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from api.agents._router import router
from api.agents.outside import _manager, tool_pick_rows
from auth.providers import UserContext, get_current_user
from services.mcp import mcp_permissions, mcp_registry, outside_picks, tool_limits
from services.mcp.mcp_agent_skills import is_skill_package
from storage.agents import agent_store, outside_store
from storage.pg import run_db

logger = logging.getLogger("otodock")

#: The words a limit takes: no limit is the absence of a row.
LIMIT_WORDS = (outside_picks.READ_ONLY, outside_picks.CUSTOM)
#: The columns a manager sets here; the outside column is the External
#: identities card's and is read only in this answer.
SET_HERE = (tool_limits.RUNS, tool_limits.CHATS)
WIDEN_REFUSED = "Only a manager, from Agent Settings, can widen what the agent may use."


class ColumnLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access: str
    functions: list[str] = []


class ToolLimitChange(BaseModel):
    """One tool's limits, each column named written alone; a column left
    out stays as it is."""
    model_config = ConfigDict(extra="forbid")
    mcp: str
    runs: ColumnLimit | None = None
    chats: ColumnLimit | None = None


class ToolLimitRemoval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mcp: str
    governs: str


class ToolLimitsRequest(BaseModel):
    """A change: what is left out stays as it is. An unknown field is
    refused, so a caller sending the outside column here is told."""
    model_config = ConfigDict(extra="forbid")
    tools: list[ToolLimitChange] | None = None
    remove: list[ToolLimitRemoval] | None = None


def _offered_tools(name: str) -> list:
    """The MCPs a manager may limit: the agent's enabled ones, never a
    skill package (no tools)."""
    return sorted(
        (m for m in mcp_registry.get_agent_mcps_all_placements(name) if not is_skill_package(m)),
        key=lambda m: ((m.label or m.name).lower(), m.name),
    )


def _row(pick: dict | None) -> dict | None:
    if pick is None:
        return None
    return {"access": pick["access"], "functions": pick["functions"], "changed": bool(pick["changed_at"])}


def _read_page(name: str) -> dict:
    by_column = {g: outside_store.get_tool_picks(name, governs=g) for g in tool_limits.GOVERNS}
    tools = []
    for m in _offered_tools(name):
        key = outside_picks.server_key(m)
        known = mcp_permissions.known_tools(key)
        tools.append({
            "mcp": m.name,
            "label": m.label or m.name,
            "known_functions": (
                [{"name": t, "reads_only": mcp_permissions.reads_only(mcp_permissions.tool_hints(key, t))}
                 for t in known] if known else None),
            **{g: _row(by_column[g].get(m.name)) for g in tool_limits.GOVERNS},
        })
    return {"tools": tools}


@router.get("/v1/agents/{name}/tool-limits")
async def get_tool_limits(name: str, user: UserContext | None = Depends(get_current_user)):
    """Every enabled tool with its listed functions (``null`` while the
    server's list was never read) and, per column, its row or ``null`` for
    all tools."""
    _manager(user, name)
    if not await run_db(agent_store.agent_exists, name):
        raise HTTPException(404, "Agent not found")
    return await run_db(_read_page, name)


def _tightens(current: dict | None, row: dict) -> bool:
    """Whether a change a session token asks for only narrows: a row where
    none exists, or a custom list that is a subset of the custom list held."""
    if current is None:
        return True
    return (current["access"] == outside_picks.CUSTOM and row["access"] == outside_picks.CUSTOM
            and set(row["functions"]) <= set(current["functions"]))


def _write_page(name: str, req: ToolLimitsRequest, *, session: bool) -> dict:
    offered = {m.name: m for m in _offered_tools(name)}
    rows: list[dict] = []
    for change in req.tools or ():
        for governs in SET_HERE:
            column = getattr(change, governs)
            if column is None:
                continue
            rows += tool_pick_rows(
                offered, [(change.mcp, column.access, column.functions)], words=LIMIT_WORDS, governs=governs,
                refused="{tool} is not one of this agent's tools.")
    removals = []
    for item in req.remove or ():
        if item.governs not in SET_HERE:
            raise HTTPException(400, "A limit is for the agent's own runs or for people's chats.")
        if item.mcp not in offered:
            raise HTTPException(400, f"{item.mcp} is not one of this agent's tools.")
        removals.append((item.mcp, item.governs))
    if session:
        if removals:
            raise HTTPException(403, WIDEN_REFUSED)
        for row in rows:
            current = outside_store.get_tool_picks(name, governs=row["governs"]).get(row["mcp_name"])
            if not _tightens(current, row):
                raise HTTPException(403, WIDEN_REFUSED)
    outside_store.update(name, upsert_tools=rows or None, remove_tools=removals or None)
    return _read_page(name)


@router.put("/v1/agents/{name}/tool-limits")
async def set_tool_limits(
    name: str, req: ToolLimitsRequest, user: UserContext | None = Depends(get_current_user),
):
    """Change the agent's limits: only what the request names, for the
    agent's own runs and people's chats. A limit names an enabled tool and
    is Read only or a custom list; a Read only row records the read-only
    functions the labels give today. A session token may only tighten."""
    u = _manager(user, name)
    if not await run_db(agent_store.agent_exists, name):
        raise HTTPException(404, "Agent not found")
    page = await run_db(_write_page, name, req, session=u.is_session)
    logger.info("tool limits: %s changed the limits of %s", u.sub[:12], name)
    # A warm app manager keeps the pairs it was built with: the idle ones go.
    from services.apps import headless_exec
    await headless_exec.drop_idle(name)
    # A Read only limit on a server never listed: list it now, so the limit
    # has its functions (its first listing fills what it gives).
    from services.mcp import tool_listing
    named = {c.mcp for c in req.tools or ()}
    for tool in page["tools"]:
        if tool["mcp"] in named and tool["known_functions"] is None and any(
                tool[g] and tool[g]["access"] == outside_picks.READ_ONLY for g in SET_HERE):
            tool_listing.schedule(name, tool["mcp"])
    return page
