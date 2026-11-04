"""Notebook routes (the rules live in ``services/notebook/``).

The tools' two routes sit under ``/v1/delegation/`` because the delegation
MCP calls them, and that prefix is already on both loopback-tunnel
allowlists (a paired machine's MCP reaches them with no satellite change).
They take a session token only: the token's ``agent`` claim is the entry's
agent, never a header or the body. The posting floor reads the session's
registered security context, so the role that decided the prompt's posting
paragraph is the role that decides the post.

The dashboard's routes read an agent's read set (anyone who can open the
agent) and a department's members (the members the person can open), and a
manager of the posting agent deletes an entry: the row stays, its text is
blanked.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from auth.providers import UserContext, get_current_user, require_auth, require_user
from core.session import session_kind, session_state
from services.notebook import entries, posting, read_set
from storage import database as task_store
from storage.agents import agent_store, db_departments
from storage.notebook import db_notebook

logger = logging.getLogger("otodock.notebook")

router = APIRouter(dependencies=[Depends(require_user)])

_PAGE_DEFAULT = 50

class PostRequest(BaseModel):
    text: str


def _require_session(u: UserContext) -> str:
    """The agent of the calling session; the tools' routes take nothing else."""
    if not u.is_session or not u.agent or u.is_external:
        raise HTTPException(403, "Only an agent's own session can use the Notebook tools.")
    return u.agent


def _surface(session_id: str) -> str:
    """The session's kind in the source-type spelling: the kind the layer
    recorded at spawn (a meeting participant has no chat row), else the kind
    of the session's chat row, else ``""``."""
    kind = session_kind.of(session_state.get_session_client_type(session_id))
    if kind is not None and kind.source_type:
        return kind.source_type
    chat = task_store.get_chat_by_session(session_id) if session_id else None
    return session_kind.of_chat(chat).source_type if chat else ""


def _poster(u: UserContext, agent: str) -> tuple[str | None, str]:
    """(author_sub, surface) for a session allowed to post, else a 403.

    The floor is ``posting.posting_refusal``, the one the prompt's posting
    paragraph reads, fed from the session's registered context (the role it
    was built with, whether it is a judge or an external caller) and its
    recorded kind. Only the route knows the rest: the session is registered
    and belongs to the agent, and a person driving it holds the role now (a
    demotion since the session started is refused at once)."""
    sid = u.session_id or ""
    ctx = session_state.get_session_security(sid) if sid else None
    if ctx is None or getattr(ctx, "agent", "") != agent:
        raise HTTPException(
            403, "This session cannot post to the Notebook. Start a new session and try again.",
        )
    refusal = posting.posting_refusal(
        agent, role=getattr(ctx, "role", ""),
        read_only=bool(getattr(ctx, "read_only", False)),
        client_type=session_state.get_session_client_type(sid),
        external=bool(getattr(ctx, "is_external", False)),
    )
    if refusal is None and u.acting_sub is not None and not u.can_edit_agent(agent):
        refusal = posting.BELOW_EDITOR
    if refusal is not None:
        raise HTTPException(403, refusal)
    return u.acting_sub, _surface(sid)


def _parse_when(value: str | None, *, field: str) -> datetime | None:
    """A date (``2026-10-28``) or a date and time; a value without an offset
    is read in the platform's timezone."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(
            400, f"{field} must be a date like 2026-10-28 or a date and a time.",
        ) from None
    if parsed.tzinfo is None:
        import config
        parsed = parsed.replace(tzinfo=config.get_platform_tz())
    return parsed


def _cursor(before: str | None, before_id: str | None) -> tuple[datetime, str] | None:
    when = _parse_when(before, field="before")
    if when is None:
        return None
    return when, before_id or "~"


def _platform_tz():
    import config
    return config.get_platform_tz()


def _tool_entry(row: dict, tz=None) -> dict:
    """An entry as the tools show it: ``when`` in the platform's timezone,
    the prompt's own format."""
    tz = tz or _platform_tz()
    return {
        "id": row["id"],
        "agent": row["agent"],
        "created_at": row["created_at"].isoformat(),
        "when": row["created_at"].astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        "text": row["text"],
    }


@router.post("/v1/delegation/notebook")
async def post_entry(body: PostRequest, user: UserContext | None = Depends(get_current_user)):
    """``notebook_post``: an entry the session's own agent writes."""
    u = require_auth(user)
    agent = _require_session(u)
    author_sub, surface = await asyncio.to_thread(_poster, u, agent)
    try:
        row = await asyncio.to_thread(entries.post, agent, author_sub, surface, body.text)
    except entries.EntryRefused as e:
        raise HTTPException(400, str(e)) from None
    return {"entry": _tool_entry(row)}


@router.get("/v1/delegation/notebook")
async def read_entries(
    agent: str | None = None,
    since: str | None = None,
    limit: int = entries.READ_LIMIT_DEFAULT,
    user: UserContext | None = Depends(get_current_user),
):
    """``notebook_read``: the read set of the session's own agent."""
    u = require_auth(user)
    own = _require_session(u)
    when = _parse_when(since, field="since")
    try:
        rows = await asyncio.to_thread(
            entries.read, own, about=agent or None, since=when, limit=limit,
        )
    except entries.EntryRefused as e:
        raise HTTPException(403, str(e)) from None
    agents = await asyncio.to_thread(read_set.read_set, own)
    tz = _platform_tz()
    return {"entries": [_tool_entry(r, tz) for r in rows], "read_set": agents,
            "timezone": getattr(tz, "key", "") or str(tz)}


def _author_names(subs: set[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for sub in subs:
        row = task_store.get_user(sub) or {}
        names[sub] = row.get("name") or row.get("username") or ""
    return names


def _page(u: UserContext, agents: list[str], *, about: str | None,
          before: tuple[datetime, str] | None, limit: int) -> dict:
    """One page of entries for a person's view of ``agents``."""
    shown = [about] if about else agents
    limit = max(1, min(limit, entries.READ_LIMIT_MAX))
    rows = db_notebook.latest(shown, limit=limit + 1, before=before)
    more = len(rows) > limit
    rows = rows[:limit]
    names = _author_names({r["author_sub"] for r in rows if r["author_sub"]})
    out = []
    for r in rows:
        sub = r["author_sub"] or ""
        # Who used an agent is shown to the author, the agent's managers and
        # admins: a reader one hop away learns what was done, not by whom.
        sees_author = bool(sub) and (
            u.acting_sub == sub or u.can_manage_agent(r["agent"])
        )
        out.append({
            "id": r["id"],
            "agent": r["agent"],
            "created_at": r["created_at"].isoformat(),
            "text": r["text"],
            "surface": r["surface"],
            "author": names.get(sub, "") if sees_author else "",
            "can_delete": not u.is_session and u.can_manage_agent(r["agent"]),
        })
    return {"entries": out, "more": more}


def _agent_choices(slugs: list[str]) -> list[dict]:
    out = []
    for slug in slugs:
        info = agent_store.get_agent(slug) or {}
        out.append({"slug": slug, "display_name": info.get("display_name") or slug})
    return out


@router.get("/v1/notebook/agents/{name}")
async def agent_notebook(
    name: str,
    agent: str | None = None,
    before: str | None = None,
    before_id: str | None = None,
    limit: int = _PAGE_DEFAULT,
    user: UserContext | None = Depends(get_current_user),
):
    """The agent page's section: the agent's read set, newest first."""
    u = require_auth(user)
    if u.is_external or not u.can_access_agent(name):
        raise HTTPException(404, "Agent not found")
    agents = await asyncio.to_thread(read_set.read_set, name)
    if not agents:
        raise HTTPException(404, "Agent not found")
    if agent and agent not in agents:
        raise HTTPException(
            400, f"{agent} is not one delegation step from {name}.",
        )
    cursor = _cursor(before, before_id)
    page = await asyncio.to_thread(
        _page, u, agents, about=agent, before=cursor, limit=limit,
    )
    page["agents"] = await asyncio.to_thread(_agent_choices, agents)
    return page


@router.get("/v1/notebook/departments/{department_id}")
async def department_notebook(
    department_id: str,
    agent: str | None = None,
    before: str | None = None,
    before_id: str | None = None,
    limit: int = _PAGE_DEFAULT,
    user: UserContext | None = Depends(get_current_user),
):
    """The department page's section: the entries of the members this
    person can open, whatever the department's mode."""
    u = require_auth(user)
    if u.is_external or u.is_session:
        raise HTTPException(403, "The department's Notebook is read in the dashboard.")
    dept = await asyncio.to_thread(db_departments.get_department, department_id)
    if dept is None:
        raise HTTPException(404, "Department not found")
    members = await asyncio.to_thread(read_set.department_members, department_id, u)
    slugs = [m["slug"] for m in members]
    if not read_set.department_visible(u, dept, slugs):
        raise HTTPException(404, "Department not found")
    if agent and agent not in slugs:
        raise HTTPException(400, f"{agent} is not a member of this department you can open.")
    cursor = _cursor(before, before_id)
    page = await asyncio.to_thread(
        _page, u, slugs, about=agent, before=cursor, limit=limit,
    )
    page["agents"] = members
    return page


@router.delete("/v1/notebook/entries/{entry_id}")
async def delete_entry(entry_id: str, user: UserContext | None = Depends(get_current_user)):
    """A manager of the posting agent, or an admin, deletes an entry. The
    row keeps who deleted it and when; the text is blanked. An agent never
    deletes: a session token is refused."""
    u = require_auth(user)
    if u.is_session or u.acting_sub is None:
        raise HTTPException(403, "Entries are deleted by a person in the dashboard.")
    row = await asyncio.to_thread(db_notebook.get_entry, entry_id)
    if row is None or row["deleted_at"] is not None:
        raise HTTPException(404, "This entry is already gone.")
    if not u.can_manage_agent(row["agent"]):
        raise HTTPException(
            403, "Only a manager of the agent that posted an entry can delete it.",
        )
    if not await asyncio.to_thread(db_notebook.soft_delete, entry_id, u.acting_sub):
        raise HTTPException(404, "This entry is already gone.")
    logger.info(
        "notebook entry %s of %s deleted by %s", entry_id, row["agent"], u.acting_sub,
    )
    return Response(status_code=204)
