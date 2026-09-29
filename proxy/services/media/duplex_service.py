"""Full-duplex chat voice — access checks shared by the mint and the bridge.

The duplex token (``ws_audio_token.create_duplex_token``) carries ``{sub,
chat_id}``; both the HTTP mint and the ``/ws/duplex`` attach enforce chat
access against those claims with the SAME two gates the dashboard chat path
applies:

1. the by-id chat rule (``api/agents/agents.py::can_open_chat``, the one
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
from core.session import session_kind

if TYPE_CHECKING:
    from auth.providers import UserContext

logger = logging.getLogger("claude-proxy")


def chat_access_denied_reason(chat_id: str, user: UserContext | None) -> str | None:
    """None when ``user`` may drive this chat over a duplex session; else the
    refusal reason. Both gates, always. Synchronous: call it on the DB
    executor."""
    from api.agents.agents import can_open_chat
    from auth.providers import acting_role_of
    from ws.dashboard import _task_continue_allowed

    chat = task_store.get_chat(chat_id)
    if not chat:
        return "Chat not found"
    if user is None or not can_open_chat(user, chat):
        return "Access denied"
    if session_kind.is_task_chat_id(chat_id):
        run = task_store.get_run(session_kind.run_id_of_chat(chat_id))
        if not run:
            return "Task run not found"
        eff_role = acting_role_of(user.sub, run.get("agent") or "")
        if not _task_continue_allowed(run, effective_role=eff_role, user_sub=user.sub):
            return "Access denied"
    from core.session import visibility
    if visibility.is_shared_chat_owner(chat.get("user_sub")):
        from core.sandbox.session_config_dir import (
            AgentStateRefused, refuse_agent_state_below_editor,
        )
        try:
            refuse_agent_state_below_editor(
                visibility.SCOPE_AGENT, user.acting_role(chat.get("agent") or ""))
        except AgentStateRefused as e:
            return str(e)
    return None
