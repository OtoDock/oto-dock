"""Checks routes (CHECKS.md "Governance", API.md).

- ``GET /v1/agents/{slug}/checks`` — the agent's checks (everyone attached
  to the agent), plus the caller's own; a manager also sees the problems.
- ``PUT / DELETE /v1/agents/{slug}/checks/{name}`` — the agent's checks,
  managers and admins only (the config tree).
- ``PUT / DELETE /v1/agents/{slug}/user-checks/{name}`` — the caller's own
  checks in the caller's tree (``users/<u>/checks``), never another's.
- ``GET /v1/agents/{slug}/check-verdicts`` — the verdict list: a manager's
  is the agent's, a member's is their own plus the agent-scope rows.
- ``GET / PATCH /v1/agents/{slug}/check-settings`` — the daily cap.
- ``POST /v1/checks/attach | detach | run`` — the calling session's chat
  (the session JWT is the authority, as continuations); the session tool.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.apps import catalog
from auth.providers import (
    UserContext, get_current_user, require_agent_access, require_auth, session_bound_to,
)
from services.checks import documents
from storage import database as task_store
from storage.checks import db_checks

logger = logging.getLogger("checks")
router = APIRouter()

MAX_ATTACHED = 16
# A session token changes or reads an agent's checks only for the agent it
# was started for (``auth.providers.session_bound_to``).
_BOUND = [Depends(session_bound_to("agent"))]


def _manager(u: UserContext, agent: str) -> None:
    if not u.can_manage_agent(agent):
        raise HTTPException(403, "Manager or admin access required for this agent's checks")


def _not_a_run(u: UserContext) -> None:
    """The agent's checks and their spend cap change with a person there —
    the dashboard, or a chat's session — never from a task's run, which
    the checks judge (the tools are hidden there; this is the route's own
    word for a token used from the run's shell)."""
    if not u.session_id:
        return
    from core.session import session_kind
    from core.session.session_state import _sessions, get_session_client_type
    run = (get_session_client_type(u.session_id) == session_kind.TASK.name
           or bool((_sessions.get(u.session_id) or {}).get("is_task"))
           or session_kind.of_chat(task_store.get_chat_by_session(u.session_id)) is session_kind.TASK)
    if run:
        raise HTTPException(403, "A task's run does not change the agent's checks; a manager does, "
                                 "from a chat or the Checks page")


def _username(u: UserContext, agent: str) -> str:
    from core.session.visibility import is_shared_only
    if is_shared_only(agent):
        return ""
    return task_store.get_username_by_sub(u.sub) or ""


def _agent_exists(agent: str) -> None:
    from storage.agents import agent_store
    if not agent_store.agent_exists(agent):
        raise HTTPException(404, f"Agent '{agent}' not found")


class CheckBody(BaseModel):
    doc: dict
    script: str | None = None


class SettingsBody(BaseModel):
    daily_cap_usd: float | None = None


class AttachBody(BaseModel):
    ref: str


# ── listing ─────────────────────────────────────────────────────────────────


@router.get("/v1/agents/{agent}/checks", dependencies=_BOUND)
async def list_checks(agent: str, user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    require_agent_access(u, agent)
    _agent_exists(agent)
    manages = u.can_manage_agent(agent)
    username = _username(u, agent)
    own = await asyncio.to_thread(documents.load_checks, agent, "")
    mine = await asyncio.to_thread(documents.load_checks, agent, username) if username else []
    items = []
    for it in own:
        if it.problems and not manages:
            continue
        d = documents.describe(it)
        if not manages:
            d.pop("doc", None)
            d.pop("problems", None)
        items.append(d)
    items.extend(documents.describe(it) for it in mine)
    # A new core MCP reaches only agents created after the upgrade (the
    # startup note in startup.py): the page says when the checks tool is
    # off for this agent, so a manager turns it on under MCPs.
    try:
        from services.mcp import mcp_registry
        tool_enabled = any(m.name == "checks-mcp" for m in mcp_registry.get_agent_mcps(agent))
    except Exception:
        tool_enabled = True
    return {"checks": items, "can_manage": manages, "username": username, "tool_enabled": tool_enabled}


# ── the agent's checks (managers) ──────────────────────────────────────────


@router.put("/v1/agents/{agent}/checks/{name}", dependencies=_BOUND)
async def put_check(agent: str, name: str, body: CheckBody,
                    user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    require_agent_access(u, agent)
    _agent_exists(agent)
    _manager(u, agent)
    await asyncio.to_thread(_not_a_run, u)
    doc = dict(body.doc or {})
    doc["name"] = name
    try:
        it = await asyncio.to_thread(documents.write_check, agent, "", doc, body.script,
                                     updated_by=_username(u, agent) or u.sub)
    except documents.CheckError as e:
        raise HTTPException(400, str(e))
    await documents.fan_out(agent, "", name)
    catalog.check_changed(agent, "", name, it)
    return documents.describe(it)


@router.delete("/v1/agents/{agent}/checks/{name}", dependencies=_BOUND)
async def delete_check(agent: str, name: str, user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    require_agent_access(u, agent)
    _agent_exists(agent)
    _manager(u, agent)
    await asyncio.to_thread(_not_a_run, u)
    try:
        existed = await asyncio.to_thread(documents.delete_check, agent, "", name)
    except documents.CheckError as e:
        raise HTTPException(400, str(e))
    if not existed:
        raise HTTPException(404, "No such check")
    catalog.check_changed(agent, "", name, None)
    return {"status": "deleted", "name": name}


# ── a user's own checks ─────────────────────────────────────────────────────


@router.put("/v1/agents/{agent}/user-checks/{name}", dependencies=_BOUND)
async def put_user_check(agent: str, name: str, body: CheckBody,
                         user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    require_agent_access(u, agent)
    _agent_exists(agent)
    username = _username(u, agent)
    if not username:
        raise HTTPException(400, "This agent has no personal sessions — a private check has nowhere to live")
    doc = dict(body.doc or {})
    doc["name"] = name
    try:
        it = await asyncio.to_thread(documents.write_check, agent, username, doc, body.script,
                                     updated_by=username)
    except documents.CheckError as e:
        raise HTTPException(400, str(e))
    await documents.fan_out(agent, username, name)
    catalog.check_changed(agent, username, name, it, u.sub)
    return documents.describe(it)


@router.delete("/v1/agents/{agent}/user-checks/{name}", dependencies=_BOUND)
async def delete_user_check(agent: str, name: str,
                            user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    require_agent_access(u, agent)
    _agent_exists(agent)
    username = _username(u, agent)
    if not username:
        raise HTTPException(400, "This agent has no personal sessions")
    try:
        existed = await asyncio.to_thread(documents.delete_check, agent, username, name)
    except documents.CheckError as e:
        raise HTTPException(400, str(e))
    if not existed:
        raise HTTPException(404, "No such check")
    catalog.check_changed(agent, username, name, None, u.sub)
    return {"status": "deleted", "name": name}


# ── verdicts and settings ──────────────────────────────────────────────────


@router.get("/v1/agents/{agent}/check-verdicts", dependencies=_BOUND)
async def list_verdicts(agent: str, chat_id: str = "", check: str = "", limit: int = 100,
                        before: str = "", user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    require_agent_access(u, agent)
    _agent_exists(agent)
    scope_sub = None if u.can_manage_agent(agent) else u.sub
    rows = await asyncio.to_thread(db_checks.list_verdicts, agent, user_sub=scope_sub,
                                   chat_id=chat_id, check_name=check, limit=limit, before=before[:64])
    return {"verdicts": rows}


@router.get("/v1/agents/{agent}/check-settings", dependencies=_BOUND)
async def get_settings(agent: str, user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    require_agent_access(u, agent)
    _agent_exists(agent)
    row = await asyncio.to_thread(db_checks.get_settings, agent)
    platform_default = await asyncio.to_thread(task_store.get_platform_setting, "checks_daily_cap_usd")
    return {**row, "platform_default_usd": float(platform_default) if platform_default else None,
            "can_manage": u.can_manage_agent(agent)}


@router.patch("/v1/agents/{agent}/check-settings", dependencies=_BOUND)
async def patch_settings(agent: str, body: SettingsBody,
                         user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    require_agent_access(u, agent)
    _agent_exists(agent)
    _manager(u, agent)
    await asyncio.to_thread(_not_a_run, u)
    cap = body.daily_cap_usd
    if cap is not None and (cap < 0 or cap > 1_000_000):
        raise HTTPException(400, "daily_cap_usd must be a non-negative number")
    row = await asyncio.to_thread(db_checks.set_settings, agent, daily_cap_usd=cap, updated_by=u.sub)
    return row


# ── the calling session's chat (the session tool) ───────────────────────────


def _session_chat(u: UserContext) -> dict:
    if not u.session_id:
        raise HTTPException(400, "This needs a session caller.")
    chat = task_store.get_chat_by_session(u.session_id)
    if not chat:
        raise HTTPException(404, "This session has no chat yet.")
    return chat


def _attached(chat: dict) -> list[str]:
    try:
        refs = json.loads(chat.get("checks") or "[]")
    except ValueError:
        refs = []
    return [r for r in refs if isinstance(r, str)] if isinstance(refs, list) else []


@router.get("/v1/checks/attached")
async def attached(user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    chat = await asyncio.to_thread(_session_chat, u)
    agent = chat.get("agent") or ""
    username = _username(u, agent)
    refs = _attached(chat)
    mandatory = [documents.describe(it) for it in await asyncio.to_thread(documents.load_checks, agent, "")
                 if it.mandatory and not it.problems]
    return {"chat_id": chat.get("id"), "agent": agent, "attached": refs,
            "mandatory": [m["ref"] for m in mandatory], "username": username}


@router.post("/v1/checks/attach")
async def attach(body: AttachBody, user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    chat = await asyncio.to_thread(_session_chat, u)
    agent = chat.get("agent") or ""
    username = _username(u, agent)
    it = await asyncio.to_thread(documents.resolve_ref, agent, body.ref, username)
    if it is None:
        raise HTTPException(404, f"No check named {body.ref!r} on this agent (or in your own checks)")
    refs = _attached(chat)
    if it.ref not in refs:
        if len(refs) >= MAX_ATTACHED:
            raise HTTPException(400, f"At most {MAX_ATTACHED} checks on one chat")
        refs.append(it.ref)
        await asyncio.to_thread(task_store.update_chat, chat["id"], checks=json.dumps(refs))
    return {"status": "attached", "ref": it.ref, "attached": refs, "mandatory": it.mandatory}


@router.post("/v1/checks/run")
async def run_now(body: AttachBody, user: UserContext | None = Depends(get_current_user)):
    """The session tool's ``run_check``: the named check on this session's
    chat, now; the verdict comes back (and is recorded like any other)."""
    u = require_auth(user)
    if not u.session_id:
        raise HTTPException(400, "This needs a session caller.")
    from services.checks import evaluator
    try:
        return await evaluator.run_by_hand(u.session_id, body.ref)
    except LookupError as e:
        raise HTTPException(404, str(e))


@router.post("/v1/checks/detach")
async def detach(body: AttachBody, user: UserContext | None = Depends(get_current_user)):
    u = require_auth(user)
    chat = await asyncio.to_thread(_session_chat, u)
    # A task's or a delegation's checks are the ones its maker named: the
    # worker being judged never drops them (a dropped check would let the
    # run report success over a failing verdict).
    from core.session import session_kind
    if session_kind.of_chat(chat) is session_kind.TASK or chat.get("delegate_role") == "worker":
        raise HTTPException(403, "The checks of a task or a delegation are set by whoever made it; "
                                 "a run does not detach them")
    agent = chat.get("agent") or ""
    username = _username(u, agent)
    it = await asyncio.to_thread(documents.resolve_ref, agent, body.ref, username)
    ref = it.ref if it else (body.ref if body.ref.startswith(("agent:", "user:")) else f"agent:{body.ref}")
    if it is not None and it.mandatory:
        raise HTTPException(403, f"{it.name} is mandatory on this agent; a manager removes it in "
                                 "the agent's Checks page or through set_check")
    refs = _attached(chat)
    if ref in refs:
        refs.remove(ref)
        await asyncio.to_thread(task_store.update_chat, chat["id"], checks=json.dumps(refs))
    return {"status": "detached", "ref": ref, "attached": refs}
