"""An engine's question as a card in the chat: the reason the engine is told, the
card, and the answer of a Codex question the permission hook did not see.
"""

import logging

from api.hooks import routing
from core.session import engine_prompts, session_kind
from core.session.session_state import get_permission_queue, get_session_client_type
from ws import wire_events as wire

logger = logging.getLogger("otodock")


async def _card_chat_of(session_id: str) -> str:
    """The chat a hook card shows on; none when the chat row cannot be read
    now, so the card goes onto the queue as before."""
    try:
        return await routing.resolve_hook_chat_id(session_id)
    except Exception:
        logger.exception("Hook permission: the card's chat was not read for session %s",
                         session_id[:8])
        return ""


def question_shown_reason(tool_name: str) -> str:
    """What the engine is told when its question went to the chat as a card:
    the hook's deny reason, and every answer of a Codex question the hook did
    not see. The person's next message is the answer."""
    return ("Questions have been displayed to the user in the chat interface. "
            "The user will reply in their next message. "
            f"Do NOT re-ask these questions or call {tool_name} again this turn.")


async def show_question_card(route: routing.HookRoute, session_id: str, tool_name: str,
                             tool_input: dict, *, journaled: bool = False) -> None:
    """The question card, a block of the turn like Claude's (or shown
    between turns when no pump runs): no answer is awaited, the person's
    next message is it. A ``journaled`` one (a terminal's blocking question,
    which its tailer writes as the question's row) goes onto the queue,
    whose drainer forwards it to a viewer; a terminal's non-blocking one is
    no question row there, so it is shown here like any other."""
    queue = get_permission_queue(route.queue_session_id)
    item = {"event_type": wire.ITEM_QUESTION, "tool_name": tool_name, "tool_input": tool_input}
    if journaled:
        queue.put_nowait(item)
        return
    engine_prompts.offer_hook_card(queue, item, session_id=session_id,
                                   chat_id=await _card_chat_of(session_id))


async def ask_user_question(session_id: str, questions: list, *, tool_name: str = "") -> dict:
    """A Codex ``request_user_input`` question set the PreToolUse hook did not
    see (a satellite whose gate skips the turn): the same card the hook draws,
    and the server request answered at once, every question with
    ``question_shown_reason``, so the turn ends as the hook's deny ends it.
    Reused in-process by the local Codex layer and over the tunnel by
    ``/v1/hooks/codex-question``. Returns the answers MAP
    ``{<id>: {"answers": [...]}}``; ``{}`` for an unattended session (no card:
    nobody answers, the model goes on). ``tool_name`` is the engine's question
    tool as the card records it (the caller's descriptor says)."""
    if not session_kind.attended(get_session_client_type(session_id)):
        return {}
    route = routing.resolve_hook_route(session_id)
    await show_question_card(route, session_id, tool_name, {"questions": questions})
    reason = question_shown_reason(tool_name)
    logger.info("Codex question: shown as a card, answered at once (%s)", len(questions))
    return {str(q["id"]): {"answers": [reason]}
            for q in questions if isinstance(q, dict) and q.get("id")}
