"""The chats whose key turn is being delivered right now, and the request id
of each: in memory only.

A key's message is written as the person's row before the delivery ladder
starts a session (``api/public/key_turns.py``). An engine that rebuilds its
history from the chat's rows at a session's start (Direct LLM) would then
read it twice, once from the rows and once as the turn's prompt, so its
rebuild leaves out the row this module holds (``held_row``). Set before the
row is written, cleared when the turn ends. Any other starter on the chat
stands back while it is set (``taken_by_another``).

The same holds say that nobody sits at the screen of the computer the turn
runs on (``drives``): a key's turn is an unattended run for a paired
computer's device tools. The session the key's own turn builds is asked
apart (``builds``): a person who opens the chat meanwhile builds their own. The session a key's turn ran on keeps a mark after
the turn (``mark_session``), since background work it started calls tools
under that session later; the mark goes when the session closes.
"""

from __future__ import annotations

import contextvars
import json

_ROLE_USER = "user"
_in_flight: dict[str, str] = {}
# The sessions a key's turn ran on, oldest first (bounded: a session gone with
# no close of its own leaves its mark until the bound drops it).
_marked: dict[str, None] = {}
_MARKED_MAX = 1024
# The chat this task's own key turn holds: its start must not stand back from
# itself.
_own_chat: contextvars.ContextVar[str] = contextvars.ContextVar("key_turn_chat", default="")


def hold(chat_id: str, request_id: str) -> None:
    _in_flight[chat_id] = request_id
    _own_chat.set(chat_id)


def taken_by_another(chat_id: str) -> bool:
    """A key's turn holds the chat, and the caller is not that turn."""
    return chat_id in _in_flight and _own_chat.get() != chat_id


def release(chat_id: str, request_id: str) -> None:
    if _in_flight.get(chat_id) == request_id:
        _in_flight.pop(chat_id, None)


def held_row(chat_id: str, row: dict) -> bool:
    """Whether ``row`` is the key's message of the turn being delivered."""
    request_id = _in_flight.get(chat_id)
    if not request_id or row.get("role") != _ROLE_USER or '"api_key"' not in (row.get("event_data") or ""):
        return False
    try:
        mark = (json.loads(row.get("event_data") or "{}") or {}).get("api_key") or {}
    except ValueError:
        return False
    return isinstance(mark, dict) and mark.get("request_id") == request_id


def mark_session(session_id: str) -> None:
    """The session a key's turn ran on: its later calls are the key's too."""
    if session_id:
        _marked.pop(session_id, None)
        _marked[session_id] = None
        while len(_marked) > _MARKED_MAX:
            del _marked[next(iter(_marked))]


def marked(session_id: str) -> bool:
    """Whether a key's turn ran on this session (its mark is kept)."""
    return bool(session_id) and session_id in _marked


def forget_session(session_id: str) -> None:
    """The session closed: a new life of the id is nobody's key turn."""
    _marked.pop(session_id, None)


def builds(chat_id: str) -> bool:
    """Whether the caller is the key's own turn, building a session of the
    chat it holds: a person's warm-up of that chat meanwhile is their own."""
    return bool(chat_id) and chat_id in _in_flight and _own_chat.get() == chat_id


def any_driven() -> bool:
    """Whether a key's turn holds any chat or marked any session."""
    return bool(_in_flight) or bool(_marked)


def drives(chat_id: str, session_id: str = "") -> bool:
    """Whether a key's turn drives this session or chat, so nobody sits at
    the screen of the computer it runs on: the session carries a key's
    mark, or a key's turn holds the chat. With no chat known it answers yes
    while any key's turn is in flight (the safe side)."""
    if session_id and session_id in _marked:
        return True
    if chat_id:
        return chat_id in _in_flight
    return bool(_in_flight)
