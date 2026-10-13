"""In-session outbound calls: the grant and the eligibility.

A call the phone MCP places from a dashboard chat on a route tied to the
same person runs INSIDE that chat: the daemon's per-call socket attaches to
the chat's session (``ws/phone_attach.py``) instead of warming a session of
its own. The relay decides here, before the daemon is called, and mints a
one-time grant the daemon's warmup presents; the call id is remembered so
``wait_for_call`` refuses (the outcome arrives in the chat, not through
the poll).

Everything is in-memory, per proxy process: a restart mid-call ends the
attach with the socket (the daemon's note cannot reach the chat).
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass

logger = logging.getLogger("otodock")

#: How long an unclaimed grant lives (the daemon warms up while it dials).
GRANT_TTL_S = 120.0
#: A call id stays known as in-session this long (the daemon's own record
#: outlives the call by minutes, not days).
CALL_MEMORY_S = 6 * 3600.0
_MAX_ENTRIES = 1024

CAPABILITY = "in_session_calls"
BUSY_LINE = "A call is already running in this chat"
WAIT_REFUSED_LINE = (
    "This call runs inside this chat: end your turn now, the person's words "
    "arrive as your next messages and the outcome follows the call's end. "
    "Do not poll it.")


@dataclass
class Grant:
    grant: str
    session_id: str
    chat_id: str
    agent: str
    user_sub: str
    phone_number: str
    created: float
    claimed: bool = False


_grants: dict[str, Grant] = {}
#: chat id → grant: the one call a chat carries at a time.
_by_chat: dict[str, str] = {}
#: call id → (grant, when): the relay's memory for the poll refusal.
_calls: dict[str, tuple[str, float]] = {}


def _sweep(now: float) -> None:
    for g in [g for g in _grants.values() if not g.claimed and now - g.created > GRANT_TTL_S]:
        release(g.grant)
    for cid in [c for c, (_g, at) in _calls.items() if now - at > CALL_MEMORY_S]:
        _calls.pop(cid, None)
    while len(_calls) > _MAX_ENTRIES:
        _calls.pop(next(iter(_calls)))


def mint(*, session_id: str, chat_id: str, agent: str, user_sub: str,
         phone_number: str = "") -> str:
    """A grant for one call in ``chat_id``; the chat must be free."""
    now = time.monotonic()
    _sweep(now)
    if chat_id in _by_chat:
        raise ValueError(BUSY_LINE)
    grant = secrets.token_urlsafe(32)
    _grants[grant] = Grant(grant=grant, session_id=session_id, chat_id=chat_id,
                           agent=agent, user_sub=user_sub, phone_number=phone_number,
                           created=now)
    _by_chat[chat_id] = grant
    return grant


def claim(grant: str, *, agent: str, session_id: str) -> Grant | None:
    """The daemon's warmup presents the grant: the record when it is live,
    unexpired and names this agent and session; the claim is idempotent (a
    reconnect of the same call presents it again)."""
    g = _grants.get(grant or "")
    if g is None:
        return None
    if g.agent != agent or g.session_id != session_id:
        return None
    if not g.claimed and time.monotonic() - g.created > GRANT_TTL_S:
        release(grant)
        return None
    g.claimed = True
    return g


def release(grant: str) -> None:
    g = _grants.pop(grant or "", None)
    if g is not None and _by_chat.get(g.chat_id) == grant:
        _by_chat.pop(g.chat_id, None)


def live_for_chat(chat_id: str) -> Grant | None:
    grant = _by_chat.get(chat_id)
    return _grants.get(grant) if grant else None


def remember_call(call_id: str, grant: str) -> None:
    _calls[call_id] = (grant, time.monotonic())


def is_in_session_call(call_id: str) -> bool:
    return call_id in _calls


def reset() -> None:
    """Tests only."""
    _grants.clear()
    _by_chat.clear()
    _calls.clear()


async def eligible_chat(*, sid: str, agent: str, user_sub: str, route: dict | None,
                        spoken_conversation_live=None) -> tuple[str, str]:
    """``(chat_id, "")`` when the call this session places may run inside
    its chat, else ``("", why)``: the route is tied to the caller (``user``
    identity), the daemon runs calls in chats, the session is a dashboard
    chat of that person (not a pool chat, not a terminal), and no spoken
    conversation or other call holds the chat. ``why`` is a log line, not
    a refusal: an ineligible call runs as its own session, as before.
    ``spoken_conversation_live(chat_id)`` is the caller's check for a duplex
    bridge on the chat (the socket layer owns those; this module never
    imports it)."""
    from storage import database as task_store
    from storage.pg import run_db
    from services.phone import phone_config, phone_identity
    from core.session import session_kind
    from core.session.visibility import is_shared_chat_owner

    if not user_sub or route is None:
        return "", "no person or no route"
    if phone_identity.route_identity_mode(route) != phone_identity.IDENTITY_MODE_USER:
        return "", "the route is not tied to a user"
    if (route.get("identity_user_sub") or "") != user_sub:
        return "", "the caller is not the route's user"
    if not phone_config.daemon_capability(CAPABILITY):
        return "", "the daemon does not run calls in chats"
    chat = await run_db(task_store.get_chat_by_session, sid)
    if not chat:
        return "", "the session has no chat"
    if session_kind.of_chat(chat) is not session_kind.DASHBOARD:
        return "", f"a {session_kind.of_chat(chat).name} chat"
    if is_shared_chat_owner(chat.get("user_sub")) or (chat.get("user_sub") or "") != user_sub:
        return "", "not the caller's own chat"
    if (chat.get("agent") or "") != agent:
        return "", "another agent's chat"
    chat_id = chat["id"]
    from core.session import interactive_session
    if interactive_session.find_live_for_chat(chat_id) is not None:
        return "", "the chat runs in a terminal"
    if spoken_conversation_live is not None and spoken_conversation_live(chat_id) is not None:
        return "", "a spoken conversation holds the chat"
    if live_for_chat(chat_id) is not None:
        return "", BUSY_LINE
    return chat_id, ""
