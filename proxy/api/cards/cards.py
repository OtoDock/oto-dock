"""The cards routes (services/cards): a tool draws a card in the calling
session's chat, a person reads its state and presses a button.

``POST /v1/cards`` takes the session token's call: a bearer naming a
person, the session match, no meeting, and a person at the keyboard
(``cards.person_present``); the buttons' parameters are validated and
stored by the service, and the block goes on the session's queue (the pump
or the terminal's drainer draws it). ``POST /v1/cards/list`` draws a list
card the same way (no row: the dashboard fetches the list for each viewer).
``GET /v1/cards/{id}`` is a read by anyone who may open the card's chat (a
sign-in), or by a session token of that chat or of the person who asked.
``POST /v1/cards/{id}/act`` is a sign-in of anyone who may open the chat
pressing one button with the card's declared options: the card acts for
whoever presses it. The team and department actions beside this module
join the catalog here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from api.cards import actions_departments, actions_team
from api.hooks import routing
from api.sessions.sessions import verify_session_match_async
from auth.providers import (
    UserContext,
    get_current_user,
    require_auth,
    require_human,
    require_user,
)
from core.session.session_state import get_permission_queue
from services.cards import actions as catalog, cards, status as st
from storage import database as task_store
from storage.cards import db_cards
from storage.pg import run_db
from ws import wire_events as wire

catalog.register(*actions_team.ACTIONS, *actions_departments.ACTIONS)

# No route here takes an anonymous caller (auth.providers.require_user).
router = APIRouter(dependencies=[Depends(require_user)])


class DrawRequest(BaseModel):
    session_id: str
    buttons: list[dict[str, Any]]
    note: str = ""
    # The agent's own words: a say-only card's title and text (a card with
    # a do button wears the action's and keeps the text as its note).
    title: str = ""
    text: str = ""


class ListRequest(BaseModel):
    session_id: str
    kind: str
    filters: dict[str, Any] | None = None


class ActRequest(BaseModel):
    button: str
    # The declared options' values; anything else a client sends is ignored.
    options: dict[str, Any] | None = None


def _raise(exc: cards.CardRefused) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


async def _drawing_chat(u: UserContext, session_id: str, authorization: str | None) -> tuple[dict, Any]:
    """The chat a tool's session draws in, with the route's queue: a
    session token naming a person, bound to this session, no meeting, and
    a person at the keyboard (``cards.person_present``: a scheduled run's,
    a trigger's or a call's session has nobody to press a button, whoever
    the chat belongs to). Whose chat it is plays no part: the card acts for
    whoever presses it."""
    if not u.is_session:
        raise HTTPException(status_code=403, detail=st.NO_PERSON)
    await verify_session_match_async(authorization, session_id)
    route = routing.resolve_hook_route(session_id)
    if route.is_meeting:
        raise HTTPException(status_code=403, detail=st.NO_PERSON)
    chat = await run_db(task_store.get_chat_by_session, session_id)
    if not chat or not u.acting_sub or not cards.person_present(session_id, chat):
        raise HTTPException(status_code=403, detail=st.NO_PERSON)
    return chat, route


@router.post("/v1/cards")
async def draw_card(
    req: DrawRequest,
    user: UserContext | None = Depends(get_current_user),
    authorization: str | None = Header(None),
) -> dict:
    """A card on the calling session's chat."""
    u = require_auth(user)
    chat, route = await _drawing_chat(u, req.session_id, authorization)
    try:
        card = await cards.draw(chat_id=chat["id"], session_id=req.session_id, agent=u.agent or chat.get("agent") or "",
                                person_sub=u.acting_sub or "", buttons=req.buttons, note=req.note, u=u,
                                title=req.title, text=req.text)
    except cards.CardRefused as exc:
        raise _raise(exc) from exc
    queue = get_permission_queue(route.queue_session_id)
    await queue.put({"event_type": wire.ITEM_ACTION_CARD, **cards.block_of(card)})
    return {"status": "ok", "card_id": card["id"]}


@router.post("/v1/cards/list")
async def draw_list_card(
    req: ListRequest,
    user: UserContext | None = Depends(get_current_user),
    authorization: str | None = Header(None),
) -> dict:
    """A list card on the calling session's chat: the kind, its filters
    and the person it is for; the dashboard fetches the rows."""
    u = require_auth(user)
    chat, route = await _drawing_chat(u, req.session_id, authorization)
    try:
        block = await cards.draw_list(chat_id=chat["id"], agent=u.agent or chat.get("agent") or "",
                                      person_sub=u.acting_sub or "", kind=req.kind, filters=req.filters, u=u)
    except cards.CardRefused as exc:
        raise _raise(exc) from exc
    queue = get_permission_queue(route.queue_session_id)
    await queue.put({"event_type": wire.ITEM_LIST_CARD, **block})
    return {"status": "ok", "kind": block["kind"], "title": block["title"]}


def _card_or_404(card_id: str) -> dict:
    card = db_cards.get_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail=st.NO_SUCH_CARD)
    return card


@router.get("/v1/cards/{card_id}")
async def read_card(card_id: str, user: UserContext | None = Depends(get_current_user)) -> dict:
    """The card's state for its reader: a sign-in that may open the card's
    chat, or a session token on the card's agent whose session is that
    chat's or names the person who asked (the agent learns the outcome).
    Anyone else is refused. A card's row goes with its chat."""
    u = require_auth(user)
    card = await asyncio.to_thread(_card_or_404, card_id)
    if u.is_session:
        # Checked before any admin shortcut: an admin's token on another
        # agent reads nothing here.
        if (u.agent or "") != card["agent"] or not u.session_id:
            raise HTTPException(status_code=403, detail=st.CARD_ELSEWHERE)
        own = await run_db(task_store.get_chat_by_session, u.session_id)
        asked = bool(card["person_sub"]) and (u.acting_sub or "") == card["person_sub"]
        if not asked and (own or {}).get("id") != card["chat_id"]:
            raise HTTPException(status_code=403, detail=st.CARD_ELSEWHERE)
        return await asyncio.to_thread(cards.state, card, u.acting_sub)
    if getattr(u, "is_api_key", False):
        # The master key or a person's own key: no reader of a chat's card.
        raise HTTPException(status_code=403, detail=st.CARD_ELSEWHERE)
    chat = await run_db(task_store.get_chat, card["chat_id"])
    if chat is None:
        raise HTTPException(status_code=404, detail=st.NO_SUCH_CARD)
    if not await asyncio.to_thread(cards.may_open, u, chat):
        raise HTTPException(status_code=403, detail=st.CARD_ELSEWHERE)
    return await asyncio.to_thread(cards.state, card, u.acting_sub)


@router.post("/v1/cards/{card_id}/act")
async def act_on_card(card_id: str, req: ActRequest, user: UserContext | None = Depends(get_current_user)) -> dict:
    """One button pressed by a signed-in person who may open the chat."""
    u = require_human(user)
    card = await asyncio.to_thread(_card_or_404, card_id)
    try:
        return await cards.act(card, req.button, req.options, u)
    except cards.CardRefused as exc:
        raise _raise(exc) from exc
