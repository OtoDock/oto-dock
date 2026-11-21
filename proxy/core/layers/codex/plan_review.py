"""The plan review of a Codex plan-mode turn: the plan becomes a review card
the person answers between turns.
"""

import logging

from storage.pg import run_db

logger = logging.getLogger("codex-layer")


#: The app-server's item type for a plan-mode turn's proposed plan.
PLAN_ITEM = "plan"
#: The plan review's mark that nothing waits on its card: the dashboard keeps
#: it answerable while the plan row is pending (a Claude review whose turn
#: ended unanswered reads as cancelled).
PLAN_BETWEEN_TURNS = "between_turns"


async def queue_plan_review(session_id: str, plan: str) -> None:
    """A Codex plan turn's plan as a plan review: named after the chat's
    plan file (``plan-<chat8>.md``, the same name across revisions; no file
    is written here, the reviewed rows hold the plans) and put on the
    session's permission queue as Claude's ExitPlanMode puts it, so the
    pump writes the plan row (pending) and the review card shows. Nothing
    waits on the card: the turn ends and the answer comes between turns
    (ws/dashboard_prompts)."""
    import uuid

    from core.session.session_state import get_permission_queue
    from storage import database as task_store
    from ws import wire_events as wire
    chat = await run_db(task_store.get_chat_by_session, session_id)
    chat_id = (chat or {}).get("id") or session_id
    filename = f"plan-{chat_id[:8]}.md"
    get_permission_queue(session_id).put_nowait({
        "event_type": wire.ITEM_PLAN_REVIEW,
        "request_id": str(uuid.uuid4()),
        "plan": plan,
        "tool_input": {"plan": plan, "planFilePath": filename, PLAN_BETWEEN_TURNS: True},
    })
