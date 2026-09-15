"""Out-of-band hook routing: which chat a hook call belongs to (``resolve_hook_route``,
``resolve_hook_chat_id``) and the meeting turn-end backstop. No routes.

One of the pieces of the hook callback API assembled by ``api/hooks/hooks.py``
(its docstring holds the path-form contract). Routes register on this module's
``router``; the facade includes it.
"""

import asyncio
import logging
from dataclasses import dataclass

from fastapi import APIRouter

from storage import database as task_store
from core.session.session_state import (
    get_meeting_session_info,
    mark_meeting_turn_routed,
)

logger = logging.getLogger("claude-proxy")
router = APIRouter()


# ---------------------------------------------------------------------------
# Hook routing — meeting-aware session rebinding
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class HookRoute:
    """Where a hook callback's out-of-band event belongs.

    ``queue_session_id`` keys the permission queue the event is pushed to
    (``get_permission_queue``). ``chat_id`` is the chat for chat-keyed side
    effects — filled only for meeting participants (the meeting's parent
    chat), empty otherwise; use :func:`resolve_hook_chat_id` when a chat row
    is actually needed (keeps the per-tool-call hook paths free of DB
    lookups).
    """
    queue_session_id: str
    chat_id: str = ""
    meeting_agent: str = ""
    parent_session_id: str = ""
    is_meeting: bool = False
    is_moderator: bool = False
    # The meeting routing tool this turn already passed ("" = none): the
    # turn-end backstop in ``permission._decide_tool_permission`` denies what follows.
    routed_tool: str = ""


def resolve_hook_route(session_id: str) -> HookRoute:
    """The single meeting-awareness chokepoint for every hook family.

    Meeting participants run their own CLI sessions, but their output streams
    through the MEETING's pump (session ``meeting-<id>``) into the parent
    chat — while every hook posts the PARTICIPANT's session_id. Consulting
    this resolver rebinds them: events go to the pump's permission queue,
    chat-keyed side effects to the parent chat, and ``meeting_agent`` carries
    the speaker identity for badges. Normal sessions fall back to identity
    (their own queue, their own chat).
    """
    info = get_meeting_session_info(session_id)
    if info:
        return HookRoute(
            queue_session_id=info["pump_session_id"],
            chat_id=info["parent_chat_id"],
            meeting_agent=info["agent_slug"],
            parent_session_id=info["parent_session_id"],
            is_meeting=True,
            is_moderator=bool(info.get("is_moderator")),
            routed_tool=info.get("routed_tool") or "",
        )
    return HookRoute(queue_session_id=session_id)


# ---------------------------------------------------------------------------
# Meeting turn-end backstop
# ---------------------------------------------------------------------------

# The meeting tools whose call ends the participant's turn. The orchestrator
# routes only at the turn boundary (it reads ``direct_to`` from the finished
# turn), so a participant that keeps calling tools after routing stalls the
# meeting: nobody else speaks until its turn ends. Observed live 2026-09-09:
# a moderator called direct_to, then spent 304 s / 27 API turns on data
# tools and peeking at the addressed agent's sessions for the reply it was
# waiting for — the participant never got a turn and the meeting ended
# with no summary. The prompt rule ("direct_to is your LAST action") is
# reinforced here structurally: once the hook has allowed a routing tool,
# every later tool call in the same turn is denied with a reason that says
# when the replies arrive. Deliberately minimal (operator decision
# 2026-09-09): one rule, no denial counting, no interrupt — the prompts and
# tool results carry the contract, this is only the floor under them.
MEETING_ROUTING_TOOLS = frozenset(
    {"direct_to", "end_meeting", "propose_conclude", "leave_meeting"}
)
# Same threshold as the orchestrator's thin-turn rule: below it the
# end_meeting summary is expected AFTER the call (and is kept).
_MEETING_SUMMARY_MIN_CHARS = 300


def _meeting_tool_short_name(tool_name: str) -> str:
    """``mcp__meetings-mcp__direct_to`` → ``direct_to`` ("" for other tools)."""
    prefix = "mcp__meetings-mcp__"
    return tool_name[len(prefix):] if tool_name.startswith(prefix) else ""


def _meeting_turn_over_reason(route: HookRoute, text_chars: int) -> str:
    routed = route.routed_tool
    if routed == "direct_to":
        return (
            "Your meeting turn ended when you called direct_to. The agents "
            "you addressed speak only after your response ends, and their "
            "replies reach you in your next turn. Stop now: no more tools, "
            "no more text."
        )
    if routed == "end_meeting":
        if text_chars < _MEETING_SUMMARY_MIN_CHARS:
            return (
                "The meeting is concluded and this session closes when your "
                "response ends. Write the meeting summary now as plain "
                "response text, then stop. No more tools."
            )
        return (
            "The meeting is concluded: the summary you wrote above is "
            "the final message and this session closes when your "
            "response ends. Stop now: no more tools, no more text."
        )
    return (
        f"Your meeting turn is over after {routed}. Stop now: no more "
        "tools, no more text."
    )


def _meeting_turn_end_backstop(
    session_id: str, route: HookRoute, tool_name: str, tool_input: dict,
) -> dict | None:
    """Deny a meeting participant's tool call once its turn has routed.

    Returns the deny decision, or None when the call is allowed. Also
    RECORDS the routing tool when it is the one being allowed: the hook is
    the only path that is serialized with the deny (the orchestrator sees
    the tool's close event only at the next content block, and on a
    satellite the event stream and the hook travel separately), and it
    fires only for calls the CLI actually executes.
    """
    short = _meeting_tool_short_name(tool_name)
    if short:
        # A participant's end_meeting is refused by the API (moderator
        # only) and routes nothing — it must not end the caller's turn.
        if short in MEETING_ROUTING_TOOLS and (
                short != "end_meeting" or route.is_moderator):
            mark_meeting_turn_routed(session_id, short)
        return None
    if not route.routed_tool:
        return None
    if tool_name == "ToolSearch" and "meeting" in str(tool_input.get("query", "")).lower():
        return None  # loading end_meeting's schema after direct_to
    if tool_name.startswith("mcp__memory-mcp__"):
        # Persisting what the meeting taught is quick, harmless and often
        # the last thing a participant does — never in the way of routing.
        return None
    info = get_meeting_session_info(session_id) or {}
    reason = _meeting_turn_over_reason(route, int(info.get("turn_text_chars", 0)))
    logger.info(
        f"Hook denied (meeting turn over after {route.routed_tool}): "
        f"session={session_id[:8]}, agent={route.meeting_agent}, tool={tool_name}"
    )
    return {"decision": "deny", "reason": reason}


async def resolve_hook_chat_id(session_id: str) -> str:
    """Effective chat for a hook's chat-keyed side effects: the meeting's
    parent chat for participants, else the session's own chat row (empty
    string if none)."""
    route = resolve_hook_route(session_id)
    if route.chat_id:
        return route.chat_id
    chat = await asyncio.to_thread(task_store.get_chat_by_session, session_id)
    return chat["id"] if chat else ""
