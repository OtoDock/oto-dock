"""The person's chats through a key (scope ``chat``): list, create, read,
send a message and wait for its turn's end (``key_turns.py``).

A key reaches what that person's chat reaches on the agents it names: the
person's own chats and a Shared-only pool's, dashboard chats only, never a
colleague's chat (an admin's key included), a task's, a call's or a
meeting's.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.public import key_turns
from api.public.common import key_user, open_wait, record, require_agent
from auth import roles
from auth.providers import UserContext
from storage import database as task_store
from storage.pg import run_db

router = APIRouter(tags=["chat"])

#: The longest a send waits for its turn's end (the default stays under the
#: usual 60 s read timeout of a reverse proxy).
WAIT_DEFAULT_S = 55
WAIT_MAX_S = 120


class ChatView(BaseModel):
    id: str
    agent: str
    title: str
    permission_mode: str
    updated_at: str
    shared: bool


class ChatsAnswer(BaseModel):
    chats: list[ChatView]


class NewChat(BaseModel):
    agent: str
    mode: str = Field("ask", description="ask or plan. autoReview and dontAsk take a key made "
                                         "to run without asking")


class MessageView(BaseModel):
    id: int
    role: str
    content: str
    created_at: str
    author_sub: str
    sent_with_key: str


class ChatAnswer(BaseModel):
    chat: ChatView
    messages: list[MessageView]
    has_more: bool


class Send(BaseModel):
    text: str = Field(..., min_length=1, max_length=100_000)
    request_id: str = Field("", max_length=128, description="Sends once per request id")
    wait_seconds: int = Field(WAIT_DEFAULT_S, ge=0, le=WAIT_MAX_S)


class TurnAnswer(BaseModel):
    chat_id: str
    message_id: int | None
    request_id: str
    state: str = Field(..., description="done, running, no_answer or not_sent")
    answer: str
    waiting_for_you: bool
    detail: str


def _chat_view(chat: dict) -> ChatView:
    from core.session import visibility as _vis
    return ChatView(id=chat["id"], agent=chat.get("agent") or "", title=chat.get("title") or "",
                    permission_mode=chat.get("permission_mode") or "",
                    updated_at=str(chat.get("updated_at") or ""),
                    shared=_vis.is_shared_chat_owner(chat.get("user_sub") or ""))


def _message_view(row: dict) -> MessageView:
    import json
    key = ""
    if row.get("role") == key_turns.ROLE_USER and '"api_key"' in (row.get("event_data") or ""):
        try:
            key = ((json.loads(row["event_data"]) or {}).get("api_key") or {}).get("name") or ""
        except ValueError:
            key = ""
    return MessageView(id=row["id"], role=row.get("role") or "", content=row.get("content") or "",
                       created_at=str(row.get("created_at") or ""),
                       author_sub=row.get("author_sub") or "", sent_with_key=key)


def _reached_chat(u: UserContext, chat_id: str) -> dict:
    chat = task_store.get_chat(chat_id)
    if chat is None or not key_turns.reachable_chat(u, chat):
        raise HTTPException(404, "This key reaches no such chat.")
    return chat


@router.get("/v1/api/chats", response_model=ChatsAnswer)
async def list_chats(agent: str = Query(""), limit: int = Query(50, ge=1, le=200),
                     u: UserContext = Depends(key_user)) -> ChatsAnswer:
    """The person's chats on the agents the key reaches, newest first."""
    from core.session.visibility import chat_history_owner
    if agent:
        require_agent(u, agent)

    def _job() -> list[ChatView]:
        owner = chat_history_owner(agent, u.sub) if agent else u.sub
        rows = task_store.list_chats(owner, agent=agent or None, limit=limit,
                                     hide_agent_runs=True)
        return [_chat_view(r) for r in rows if key_turns.reachable_chat(u, r)]

    return ChatsAnswer(chats=await run_db(_job))


@router.post("/v1/api/chats", response_model=ChatView)
async def create_chat(req: NewChat, u: UserContext = Depends(key_user)) -> ChatView:
    """A new chat on the agent, on the engine and model the person's new
    chat there takes. In Ask unless a mode is named; Auto and Don't Ask take
    a key made to run without asking."""
    from core.permission_modes import ASK_MODE, PERMISSION_MODES, entry_mode
    from core.session import visibility as _vis
    from services.engines import engine_resolution
    require_agent(u, req.agent)
    if req.mode not in PERMISSION_MODES:
        raise HTTPException(400, f"Choose a mode from: {', '.join(PERMISSION_MODES)}.")
    if req.mode not in key_turns.ASKING_MODES and not u.api_key_runs_unasked:
        raise HTTPException(403, "This key was not made to run without asking. It can make a "
                                 "chat in Ask or Plan.")

    def _job() -> dict:
        owner = _vis.chat_history_owner(req.agent, u.sub)
        mode = req.mode
        if _vis.is_shared_chat_owner(owner) and not roles.can_edit(u.acting_role(req.agent)):
            mode = ASK_MODE
        engine = engine_resolution.resolve_session_engine(req.agent, u.sub)
        if not engine.execution_path:
            raise HTTPException(409, "You have no AI engine you can use on this agent. Connect "
                                     "one in your settings, or ask an admin.")
        chat = task_store.create_chat(str(uuid.uuid4()), owner, req.agent,
                                      entry_mode(mode), model=engine.model,
                                      execution_path=engine.execution_path, origin="api")
        record(u, "chat", chat["id"], label="A new chat", agent=req.agent)
        return chat

    chat = await run_db(_job)
    from services.apps import catalog
    catalog.announce_new_chat(chat)
    return _chat_view(chat)


@router.get("/v1/api/chats/{chat_id}", response_model=ChatAnswer)
async def read_chat(chat_id: str, before_id: int | None = Query(None, ge=1),
                    limit: int = Query(50, ge=1, le=200),
                    u: UserContext = Depends(key_user)) -> ChatAnswer:
    """The chat and a page of its messages (the person's and the agent's),
    newest last."""
    def _job() -> tuple[dict, list[dict], bool]:
        chat = _reached_chat(u, chat_id)
        rows, more = task_store.get_chat_messages_page(chat_id, limit, before_id=before_id)
        return chat, rows, more

    chat, rows, more = await run_db(_job)
    shown = [_message_view(r) for r in rows
             if r.get("role") in (key_turns.ROLE_USER, key_turns.ROLE_ASSISTANT)]
    return ChatAnswer(chat=_chat_view(chat), messages=shown, has_more=more)


@router.post("/v1/api/chats/{chat_id}/messages", response_model=TurnAnswer,
             responses={503: {"description": "OtoDock cannot take it now (it is starting, it is updating or "
                                             "its compute machine is not connected): nothing was sent. "
                                             "Send the same request again after Retry-After"}})
async def send_message(chat_id: str, req: Send, u: UserContext = Depends(key_user)) -> dict:
    """Send a message as the person and wait, at most ``wait_seconds``, for
    the end of its turn. A key not made to run without asking sends only
    into a chat in Ask or Plan. A turn that needs approval waits for the
    person in the dashboard. Past the bound the answer says the turn still runs; read
    it with ``GET /v1/api/chats/{chat_id}/messages/{message_id}``. While
    OtoDock cannot take it (it is updating, or its compute machine is not
    connected) the answer is 503 with ``Retry-After``: send the request
    again with the same ``request_id``."""
    chat = await run_db(_reached_chat, u, chat_id)
    require_agent(u, chat.get("agent") or "")
    async with open_wait(u):
        return await key_turns.send(u, chat, req.text,
                                    request_id=req.request_id or str(uuid.uuid4()),
                                    wait_s=float(req.wait_seconds))


@router.get("/v1/api/chats/{chat_id}/messages/{message_id}", response_model=TurnAnswer)
async def read_turn(chat_id: str, message_id: int, wait_seconds: int = Query(0, ge=0, le=WAIT_MAX_S),
                    u: UserContext = Depends(key_user)) -> dict:
    """The state of a message this key sent, and its answer once its turn
    ended. ``wait_seconds`` waits for the end first."""
    from storage.identity import api_key_store
    await run_db(_reached_chat, u, chat_id)
    turn = key_turns.find(u.api_key_id, message_id)
    if turn is not None and turn.chat_id == chat_id:
        async with open_wait(u):
            return await key_turns.wait_for(turn, float(wait_seconds))
    claim = await run_db(api_key_store.get_key_request_by_message, u.api_key_id, message_id)
    if not claim or claim.get("chat_id") != chat_id:
        raise HTTPException(404, "This key sent no such message in this chat.")
    return await key_turns.answer_from_rows(u.api_key_id, chat_id, message_id,
                                            claim.get("request_id") or "")

