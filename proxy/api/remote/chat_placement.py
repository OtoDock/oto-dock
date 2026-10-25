"""Where a chat runs: its "Runs on" places and the move.

- ``GET /v1/chats/{chat_id}/targets``: the chip's answer (read access to the
  chat).
- ``POST /v1/chats/{chat_id}/move``: move a chat to one of its places
  (``{target, from}``); 202 with the move's id, 409 with the sentence.
- ``GET /v1/agents/{name}/run-targets``: where new work of the agent may run
  for the caller (a new chat's chip); with ``task_id``, where that task's
  runs may run, for its run person (the task dialog).
- ``POST /v1/chats/agent-move``: the agent's ``move_chat`` tool (a session
  token): without ``machine`` the places of the caller's chat, with it the
  move scheduled for the end of the caller's turn.

The decisions are ``services/remote/chat_move.py``'s and
``services/remote/chat_targets.py``'s; included by ``remote_machines``'s
router.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import roles
from auth.providers import UserContext, acting_role_of, get_current_user
from services.remote import chat_move, chat_targets
from storage import database as task_store, move_schema
from storage.pg import run_db

router = APIRouter()


def _require_user(user: UserContext | None) -> UserContext:
    if user is None:
        raise HTTPException(401, "Authentication required")
    return user


async def _chat_for(user: UserContext, chat_id: str) -> dict:
    """The chat, when this user may read it (404 otherwise: a chat one may
    not see is answered as one that does not exist)."""
    from core.session.visibility import can_access_chat
    chat = await run_db(task_store.get_chat, chat_id)
    if not chat or not await run_db(can_access_chat, user, chat):
        raise HTTPException(404, chat_move.NOT_FOUND)
    return chat


async def _asker(user: UserContext, agent: str) -> chat_move.Asker:
    role = await run_db(acting_role_of, user.sub, agent)
    return chat_move.Asker(sub=user.sub or "", role=role, platform_admin=roles.is_admin(role))


async def _task_asker(chat: dict) -> chat_move.Asker | None:
    """A task run's own chat moves as the task's identity, in every scope: a
    user-scope run as its creator at the creator's role on the agent, a run
    with no person behind it (an agent-scope task, every task on a
    Shared-only agent) as the agent at the run's role. The run identity is
    the authority here, not the token's principal: a token minted before the
    task builder named the run's person (a session started by an earlier
    release, or one re-adopted after a restart) still names nobody."""
    from core.config.task_config_builder import rewarm_task_identity
    from core.session import session_kind
    cid = chat.get("id") or ""
    if not session_kind.is_task_chat_id(cid):
        return None
    ident = await run_db(rewarm_task_identity, cid, chat.get("agent") or "")
    if ident is None:
        return None
    sub = ident.creds_user_sub or ""
    return chat_move.Asker(sub=sub, role=ident.role,
                           platform_admin=bool(sub) and roles.is_admin(ident.role))


@router.get("/v1/chats/{chat_id}/targets")
async def chat_targets_route(chat_id: str, user: UserContext | None = Depends(get_current_user)):
    u = _require_user(user)
    chat = await _chat_for(u, chat_id)
    asker = await _asker(u, chat.get("agent") or "")
    from ws.dashboard_chat_move import voice_attached
    return await run_db(lambda: chat_move.targets_view(chat, asker,
                                                       blocked=voice_attached(chat_id)))


@router.post("/v1/chats/{chat_id}/move", status_code=202)
async def move_chat_route(chat_id: str, body: dict,
                          user: UserContext | None = Depends(get_current_user)):
    u = _require_user(user)
    if u.is_session or u.is_service:
        raise HTTPException(403, "An agent moves its chat with its move_chat tool.")
    chat = await _chat_for(u, chat_id)
    target = str(body.get("target") or "")
    expect_from = body.get("from")
    asker = await _asker(u, chat.get("agent") or "")
    from ws.dashboard_chat_move import voice_attached
    try:
        return await chat_move.request_move(
            chat_id, target, asker=asker,
            expect_from=str(expect_from) if isinstance(expect_from, str) and expect_from else None,
            origin=move_schema.ORIGIN_CHIP, blocked=voice_attached)
    except chat_move.MoveRefused as e:
        raise HTTPException(409, str(e)) from None


@router.get("/v1/agents/{name}/run-targets")
async def agent_run_targets(name: str, scope: str = "user", task_id: str = "", engine: str = "",
                            user: UserContext | None = Depends(get_current_user)):
    """``engine``: the engine a new chat would run (the page's pick), so a
    machine that does not run it is offered as not pickable."""
    u = _require_user(user)
    if not (u.is_admin or u.can_access_agent(name)):
        raise HTTPException(404, "Agent not found")
    from core.session.visibility import SCOPE_AGENT
    asker = await _asker(u, name)
    if task_id:
        return await _task_run_targets(u, name, task_id, admin_asker=asker.platform_admin)
    agent_scope = scope == SCOPE_AGENT
    sub = None if agent_scope else u.sub
    role = roles.MANAGER if agent_scope else asker.role

    def _view() -> dict:
        from core.session.session_manager import resolve_execution_path
        path = resolve_execution_path(name, engine) if engine else ""
        return chat_targets.agent_view(name, sub, role, admin_asker=asker.platform_admin,
                                       engine_path=path)
    return await run_db(_view)


async def _task_run_targets(u: UserContext, agent: str, task_id: str, *,
                            admin_asker: bool) -> dict:
    """The places a task's runs may run, as its writes validate them: for
    the task's run person (``task_run_person``), the caller one who may
    change the task (404 otherwise)."""
    from api.tasks.tasks import _check_task_permission, task_run_person
    dyn = await run_db(task_store.get_dynamic_task, task_id)
    if not dyn or dyn.get("agent") != agent or dyn.get("use_persistent"):
        raise HTTPException(404, "Task not found")
    try:
        await run_db(_check_task_permission, dyn, u)
    except HTTPException:
        raise HTTPException(404, "Task not found") from None
    person = await run_db(task_run_person, agent, dyn.get("scope") or "user",
                          dyn.get("created_by"))
    engine = dyn.get("override_execution_path") or ""
    return await run_db(lambda: chat_targets.agent_view(agent, person.sub, person.role,
                                                        admin_asker=admin_asker,
                                                        engine_path=engine))


class AgentMoveRequest(BaseModel):
    machine: str | None = None
    chat_id: str | None = None


@router.post("/v1/chats/agent-move")
async def agent_move(req: AgentMoveRequest, user: UserContext | None = Depends(get_current_user)):
    """The ``move_chat`` tool: the caller's chat (its session token names
    it), or another chat its person owns."""
    u = _require_user(user)
    if not u.is_session:
        raise HTTPException(403, "This tool moves the chat it runs in.")
    caller = await asyncio.to_thread(task_store.get_chat_by_session, u.session_id)
    chat_id = (req.chat_id or "").strip() or (caller or {}).get("id") or ""
    if not chat_id:
        raise HTTPException(404, chat_move.NOT_FOUND)
    chat = await run_db(task_store.get_chat, chat_id)
    if not chat:
        raise HTTPException(404, chat_move.NOT_FOUND)
    own = caller is not None and caller.get("id") == chat_id
    if not own and (not u.sub or chat.get("user_sub") != u.sub):
        raise HTTPException(403, "The tool moves this chat, or another chat of your own.")
    asker = (await _task_asker(chat) if own else None) or await _asker(u, chat.get("agent") or "")
    from ws.dashboard_chat_move import voice_attached
    if not req.machine:
        return await run_db(lambda: chat_move.targets_view(chat, asker,
                                                           blocked=voice_attached(chat_id)))
    try:
        if own:
            res = await chat_move.schedule_after_turn(chat_id, req.machine, asker=asker,
                                                      blocked=voice_attached)
        else:
            res = await chat_move.request_move(chat_id, req.machine, asker=asker,
                                               origin=move_schema.ORIGIN_TOOL,
                                               blocked=voice_attached)
    except chat_move.MoveRefused as e:
        raise HTTPException(409, str(e)) from None
    from fastapi.responses import JSONResponse
    return JSONResponse({"scheduled": True, "to_label": res["to_label"],
                         "to_phrase": res.get("to_phrase") or res["to_label"],
                         "memory": res["memory"], "at_turn_end": own}, status_code=202)
