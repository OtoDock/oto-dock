"""Out-of-band thread-goal application.

Codex accounts goal progress AT TURN STOP, so the final ``thread/goal/updated``
of a turn — often the one flipping ``status`` to ``complete`` — can arrive just
AFTER ``turn/completed``, when the turn's consumer (and usually its pump) is
already gone. Goal state is chat-durable, so instead of dropping those
stragglers the notification routers (local ``session._route_notifications`` and
the remote ``_route_remote_notifications`` twin) hand them here: persist to
``chats.thread_goal`` and broadcast a ``goal_update`` frame to the owner's
dashboard connections via the notify queue (which drains exactly between the
viewed chat's turns — the frontend's per-chat gate scopes it to the viewer).
"""

import json
import logging

from core.events.common_events import GOAL_UPDATE, CommonEvent, goal_payload_to_state

logger = logging.getLogger("codex-layer")


def apply_goal_events_oob(session_id: str, events: list[CommonEvent]) -> None:
    """Persist + broadcast GOAL_UPDATE events that arrived with no active turn
    consumer. Best-effort and idempotent — a repeat write of the same goal is
    harmless (this path fires at most once per turn stop)."""
    goal_events = [e for e in events if e.type == GOAL_UPDATE]
    if not goal_events:
        return
    from storage.database import get_chat_by_session, update_chat
    chat = get_chat_by_session(session_id)
    if not chat:
        return
    chat_id = chat["id"]
    for ev in goal_events:
        goal = goal_payload_to_state(ev.data)
        update_chat(chat_id, thread_goal=json.dumps(goal) if goal else None)
        # Keep a bg-residual live snapshot honest for mid-residual reconnects.
        from core.session.session_state import _chat_streaming_state
        live = _chat_streaming_state.get(chat_id)
        if live is not None:
            live["goal"] = goal
        from services.notifications import notification_manager
        notification_manager.broadcast_goal_update(chat.get("user_sub", ""), chat_id, goal)
        logger.info(
            "Codex [%s] out-of-band goal update applied "
            "(status=%s)",
            session_id[:8], (goal or {}).get('status', 'cleared')
        )
