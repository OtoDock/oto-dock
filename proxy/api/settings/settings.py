"""The settings routes: the settings tool's two calls and the card's answer
for its viewer.

``GET /v1/settings/catalog`` answers every row to a signed-in person (a
session token minted for one included): the rows carry no value, and the
``who`` of each says who changes it. A bearer that names no person (the
master key, an agent-scope session) is refused.

``POST /v1/settings/card`` draws a setting's card in the chat of the
calling session: the settings tool's call, with the session token. The
card holds the catalog's id and label alone, never a value. It is live for
everyone who opens the chat: the dashboard reads and saves the value with
each viewer's own sign-in. The route refuses, in this order, a bearer
naming no person, a token bound to another session, a meeting
participant's session, a session with no chat or nobody at the keyboard
(``services/cards/cards.person_present``), an unknown id and a row with no
card. A queued card waits for the session's next drain (the turn's pump, or
the terminal's drainer): the route never drops it.

``GET /v1/settings/card/{setting}`` is the card's answer for the viewer
signed in: whether their role changes the row, the refusal's sentence when
it does not. The dashboard draws the control only on its yes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from api.hooks import routing
from api.sessions.sessions import verify_session_match_async
from api.settings import catalog
from auth.providers import (
    UserContext,
    get_current_user,
    require_auth,
    require_human,
    require_user,
)
from core.session.session_state import get_permission_queue
from services.cards import cards, status as st
from storage import database as task_store
from storage.pg import run_db
from ws import wire_events as wire

# No route here takes an anonymous caller (auth.providers.require_user).
router = APIRouter(dependencies=[Depends(require_user)])

# Sentences a person may read (the tool repeats a refusal): no instruction
# to the agent in them, and each ends with a full stop, which is how the
# tool tells the routes' own refusals from any other answer.
NO_PERSON_SIGNED_IN = "The settings are read for a signed-in person. No person is signed in here."
NO_SUCH_SETTING = "No such setting."
NO_CARD = "This setting has no card in the chat."
ADMIN_CHANGES_IT = "An administrator changes this setting."


class CardRequest(BaseModel):
    session_id: str
    setting: str


def _person(user: UserContext | None) -> UserContext:
    """The caller as a person: a sign-in, or a session token minted for one."""
    u = require_auth(user)
    if u.acting_sub is None:
        raise HTTPException(status_code=403, detail=NO_PERSON_SIGNED_IN)
    return u


@router.get("/v1/settings/catalog")
async def get_settings_catalog(user: UserContext | None = Depends(get_current_user)):
    """Every row, with the catalog's version."""
    _person(user)
    return {"version": catalog.VERSION, "entries": catalog.ENTRIES}


@router.post("/v1/settings/card")
async def draw_settings_card(
    req: CardRequest,
    user: UserContext | None = Depends(get_current_user),
    authorization: str | None = Header(None),
):
    """A setting's card on the calling session's chat, for whoever opens it."""
    u = require_auth(user)
    if u.acting_sub is None or not u.is_session:
        raise HTTPException(status_code=403, detail=st.NO_PERSON)
    await verify_session_match_async(authorization, req.session_id)
    route = routing.resolve_hook_route(req.session_id)
    if route.is_meeting:
        raise HTTPException(status_code=403, detail=st.NO_PERSON)
    chat = await run_db(task_store.get_chat_by_session, req.session_id)
    if not chat or not cards.person_present(req.session_id, chat):
        raise HTTPException(status_code=403, detail=st.NO_PERSON)
    entry = _card_row(req.setting)
    queue = get_permission_queue(route.queue_session_id)
    await queue.put({
        "event_type": wire.ITEM_SETTINGS_CARD,
        "setting": entry["id"],
        "label": entry["label"],
        "catalog_version": catalog.VERSION,
    })
    return {"status": "ok"}


@router.get("/v1/settings/card/{setting}")
async def read_settings_card(setting: str, user: UserContext | None = Depends(get_current_user)):
    """Whether the viewer signed in changes this setting in its card: the
    server's answer, so the card never guesses a role."""
    u = require_human(user)
    entry = _card_row(setting)
    if not catalog.writes(entry, u.role):
        raise HTTPException(status_code=403, detail=ADMIN_CHANGES_IT)
    return {"setting": entry["id"], "label": entry["label"], "changes": True}


def _card_row(setting: str) -> dict:
    """The row of a setting a card can show, or the refusal."""
    entry = catalog.find(setting)
    if entry is None:
        raise HTTPException(status_code=404, detail=NO_SUCH_SETTING)
    if not entry.get("card"):
        raise HTTPException(status_code=409, detail=NO_CARD)
    return entry
