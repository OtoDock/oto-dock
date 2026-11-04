"""A card's ``say`` button, sent by the server as the presser's own message
(services/cards/cards.py claims the click, this module sends). The message
goes through the chat's input queue, the composer's own path when the agent
works or the machine is away: queued as the presser with the card's mark,
announced to the chat's audience, then run as the next turn through a live
connection object of theirs (the person's own live terminal gets it typed
in, any other chat a headless turn). The route never waits for the
delivery, and the dashboard sends nothing after the click.
"""

from __future__ import annotations

import asyncio
from typing import Any

from auth import roles
from services.cards import status as st


class SayRefused(Exception):
    """The press refused before anything is claimed: ``status`` and the
    sentence the person reads."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def check(chat: dict[str, Any], u) -> None:
    """The gates a composer send takes, before the click is claimed: the
    chat is not being deleted, the person may still open it, somebody is at
    the keyboard (the chat's session, as at the draw), and in the agent's
    shared space the person holds the edit tier. A refusal queues nothing
    and spends no click."""
    from core.events import input_queue
    from core.session import visibility
    from services.cards.cards import person_present
    if input_queue.deleting(chat["id"]):
        raise SayRefused(409, st.CHAT_GONE)
    if not person_present(str(chat.get("session_id") or ""), chat):
        raise SayRefused(403, st.NO_PERSON)
    if not await asyncio.to_thread(visibility.can_access_chat, u, chat):
        raise SayRefused(403, st.CHAT_NOT_OPEN)
    agent = str(chat.get("agent") or "")
    if (visibility.is_shared_chat_owner(chat.get("user_sub")) and not u.is_admin
            and not roles.can_edit(u.effective_role(agent))):
        raise SayRefused(403, st.SHARED_NEEDS_EDITOR)
    q = await input_queue.loaded(chat["id"])
    if len(q.items) >= input_queue.CAP:
        raise SayRefused(409, st.TOO_MANY_QUEUED)


async def send(chat: dict[str, Any], card_id: str, button_id: str, message: str, u) -> bool:
    """The message into the chat's queue as the person's own, marked as the
    card's, announced, and the delivery armed with no grace (a live pump's
    end delivers instead). False when the queue filled up meanwhile: the
    caller gives the click back."""
    from core.events import input_queue
    from core.events.common_events import TurnInput
    q = await input_queue.loaded(chat["id"])
    item = TurnInput(text=message, card={"card_id": card_id, "button": button_id})
    status, queued, index = await q.add(input_queue.mint_queue_id(), u.acting_sub or "", item)
    if status != input_queue.QUEUED or queued is None:
        return False
    input_queue.announce_queued(queued, index)
    input_queue.arm(chat["id"], delay=0.0)
    return True
