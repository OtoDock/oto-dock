"""Full-duplex chat voice — access checks shared by the mint and the bridge.

The duplex token (``ws_audio_token.create_duplex_token``) carries ``{sub,
chat_id}``; both the HTTP mint and the ``/ws/duplex`` attach enforce chat
access against those claims with the SAME two gates the dashboard chat path
applies:

1. the by-id chat rule (``core/session/visibility.py::can_access_chat``, the one
   ``resume_chat`` binds by: the chat's owner decides, never the agent's
   current mode), and
2. the task continue-gate (a ``task-<run_id>`` chat must never be driven by a
   user who can't continue the run — agent-scoped → editor+, user-scoped →
   creator/admin), and
3. the pool-chat tier (a chat owned by the synthetic ``agent::`` owner runs
   as the agent, so driving it takes the editor role or above, the rule its
   session start applies and the dashboard's drive gate mirrors).

The daemon's master key never grants user access — it only authenticates the
dial-back socket as the engine peer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from storage import database as task_store

if TYPE_CHECKING:
    from auth.providers import UserContext

logger = logging.getLogger("otodock")


def chat_access_denied_reason(chat_id: str, user: UserContext | None) -> str | None:
    """None when ``user`` may drive this chat over a duplex session; else the
    refusal reason: may they open it (``can_access_chat``), and may they
    drive it (``visibility.drive_refusal``, the rule every door asks).
    Synchronous: call it on the DB executor."""
    from core.session.visibility import can_access_chat, drive_refusal_for

    chat = task_store.get_chat(chat_id)
    if not chat:
        return "Chat not found"
    # The voice is a person's: a session or the master key speaks for nobody.
    if user is None or user.acting_sub is None or not can_access_chat(user, chat):
        return "Access denied"
    return drive_refusal_for(user, chat) or None
