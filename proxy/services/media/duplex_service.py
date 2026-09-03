"""Full-duplex chat voice — access checks shared by the mint and the bridge.

The duplex token (``ws_audio_token.create_duplex_token``) carries ``{sub,
chat_id}``; both the HTTP mint and the ``/ws/duplex`` attach enforce chat
access against those claims with the SAME two gates the dashboard chat path
applies:

1. the chat ownership / shared-agent predicate (who may drive this chat), and
2. the task continue-gate (a ``task-<run_id>`` chat must never be driven by a
   user who can't continue the run — agent-scoped → editor+, user-scoped →
   creator/admin).

The daemon's master key never grants user access — it only authenticates the
dial-back socket as the engine peer.
"""

from __future__ import annotations

import logging

from storage import database as task_store

logger = logging.getLogger("claude-proxy")


def chat_access_denied_reason(
    chat_id: str, *, user_sub: str, user_role: str, user_agents: list[str],
) -> str | None:
    """None when the user may drive this chat over a duplex session; else the
    refusal reason. Mirrors ``dashboard_chat``'s self-heal predicate plus its
    ``_deny_task_continue`` — both gates, always."""
    from core.session import visibility as _vis
    from ws.dashboard import _effective_agent_role, _task_continue_allowed

    chat = task_store.get_chat(chat_id)
    if not chat:
        return "Chat not found"
    allowed = chat["user_sub"] == user_sub or user_role == "admin"
    if not allowed:
        chat_agent = chat.get("agent", "")
        is_assigned = chat_agent in (user_agents or [])
        is_agent_scoped = (
            _vis.is_shared_only(chat_agent)
            or chat.get("source_type") == "phone"
            or _vis.is_shared_chat_owner(chat["user_sub"])
        )
        allowed = is_assigned and is_agent_scoped
    if not allowed:
        return "Access denied"
    if chat_id.startswith("task-"):
        run = task_store.get_run(chat_id.removeprefix("task-"))
        if not run:
            return "Task run not found"
        eff_role = _effective_agent_role(user_sub, run.get("agent") or "")
        if not _task_continue_allowed(run, effective_role=eff_role, user_sub=user_sub):
            return "Access denied"
    return None
