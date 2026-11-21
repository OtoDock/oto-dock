"""The chats a call writes in now.

A call on the phone (``ws/phone.py``) mints its chat at warmup and its socket
holds that chat until the call ends: the agent's conversation, or a person's
own chat on a route tied to them (a Shared-only agent's shared chat). While
held, nobody else writes there (``core.session.visibility.drive_refusal``), a
wake rides the call's session or is kept (``chat_rebuild.rebuild_and_resume``),
a wake after it builds the chat as the person's (``visibility.runs_as_chat``)
and recovery keeps the call's session (``run_recovery._a_cut_call``). Once
released, a person's own chat is theirs again.

The call itself is the fact, not its session: a process reaped and started
again mid-call is still the call's, and a restart ends every call, so the
table starts empty. In memory, by chat id; single-key reads, safe from a
worker thread.
"""

from __future__ import annotations

# chat id -> the call's session id
_held: dict[str, str] = {}


def hold(chat_id: str, session_id: str) -> None:
    """The call on ``session_id`` writes in ``chat_id`` from now on."""
    if chat_id:
        _held[chat_id] = session_id


def release(chat_id: str, session_id: str) -> None:
    """The call on ``session_id`` ended: its hold goes, never another call's."""
    if chat_id and _held.get(chat_id) == session_id:
        _held.pop(chat_id, None)


def holds(chat_id: str) -> bool:
    """Whether a call writes in ``chat_id`` now."""
    return bool(chat_id) and chat_id in _held


def held_by(chat_id: str) -> str:
    """The session of the call that writes in ``chat_id`` now, "" for none."""
    return _held.get(chat_id, "") if chat_id else ""
