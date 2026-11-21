"""The reload of a chat's session, asked by the agent inside the chat's own
turn (the ``reload_session`` and ``switch_account`` tools of
agent-config-mcp), and a run's switch of account, which its next round
takes up. The work is ``services/remote/chat_reload.py``.
"""

import asyncio
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints

from api.agents._router import router
from auth.providers import (
    UserContext,
    get_current_user,
    require_auth,
    session_bound_to,
)
from services.remote import chat_move, chat_reload
from storage import database as task_store
from storage.pg import run_db

# A session token reloads only a chat of the agent it was started for.
_BOUND = [Depends(session_bound_to("name"))]


# A tool's name and an account's label as the body may carry them (a label
# from a sign-in runs to 128 characters), and how many tools one call names.
_Name = Annotated[str, StringConstraints(min_length=1, max_length=128)]
_MAX_ACCOUNTS = 16


class ReloadSessionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=300)
    # The chat's choice of account per tool for its person: a label of the
    # set the person lets this agent use, or ``default`` to clear it.
    accounts: dict[_Name, _Name] | None = Field(default=None, max_length=_MAX_ACCOUNTS)


@router.post("/v1/agents/{name}/reload-session", dependencies=_BOUND)
async def reload_session(name: str, req: ReloadSessionRequest,
                         user: UserContext | None = Depends(get_current_user)):
    """Reload the caller's own chat when its turn ends: the chat its session
    token names, never another one, and only for a session that runs as a
    person. A run's session (its own kind, whatever its chat's row) switches
    for its next round instead, as the run's identity, with or without a
    person behind it."""
    from core.session import session_kind
    from core.session.session_state import get_session_client_type
    u = require_auth(user)
    if not u.is_session:
        raise HTTPException(403, "This tool reloads the chat it runs in.")
    chat = await asyncio.to_thread(task_store.get_chat_by_session, u.session_id)
    if not chat or (chat.get("agent") or "") != name:
        raise HTTPException(404, chat_move.NOT_FOUND)
    if get_session_client_type(u.session_id or "") == session_kind.TASK.name:
        run_asker = await run_db(chat_move.run_asker, chat, u.session_id or "")
        if run_asker is None:
            raise HTTPException(409, chat_reload.NO_RUN)
        try:
            res = await chat_reload.schedule_for_run(
                chat["id"], asker=run_asker, session_id=u.session_id or "",
                reason=req.reason, accounts=req.accounts)
        except chat_reload.ReloadRefused as e:
            raise HTTPException(409, str(e)) from None
        return JSONResponse({"scheduled": True, "accounts": res["accounts"], "round": True},
                            status_code=202)
    if u.acting_sub is None:
        raise HTTPException(409, chat_reload.NOBODY)
    from api.remote.chat_placement import asker_of
    asker = asker_of(u, name)
    from ws.dashboard_chat_move import voice_attached
    try:
        res = await chat_reload.schedule_after_turn(
            chat["id"], asker=asker, reason=req.reason, accounts=req.accounts,
            blocked=voice_attached)
    except chat_reload.ReloadRefused as e:
        raise HTTPException(409, str(e)) from None
    return JSONResponse({"scheduled": True, "accounts": res["accounts"]}, status_code=202)
