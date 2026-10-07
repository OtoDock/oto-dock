"""A delegated follow-up onto a lane that is WORKING steers its live turn.

``delegate(continue_id=…)`` used to become a second run whatever the lane
was doing: the runner waited for the lane to go quiet
(``lanes._settle_prior_lane``) and, past its ceiling, aborted the running
turn. When the running turn is the lane's own delegate run, reporting to the
same caller, the follow-up belongs INSIDE that turn — the engine reads it at
its next tool boundary, nothing is interrupted, background work survives,
and the run's report covers it. That is the dashboard's steer branch
(``ws/dashboard_dispatch._on_chat``) applied to the delegation API.

Every ``core.*`` import stays function-local so the standalone scheduler can
import this package without the platform.
"""

import logging

from storage import database as task_store
from storage.agents import agent_store
from storage.automation import run_status
from services.scheduler import task_kinds

logger = logging.getLogger("claude-proxy.scheduler")


def _running_delegate_run(chat_id: str) -> dict | None:
    """The delegate run currently driving ``chat_id``, or None."""
    runs = task_store.list_runs(limit=5, chat_id=chat_id, status=run_status.RUNNING)
    for run in runs:
        if run.get("task_type") == task_kinds.DELEGATE:
            return run
    return None


async def try_steer_continue(
    chat: dict, prompt: str, *, parent_chat_id: str, source_agent: str,
) -> dict | None:
    """Steer ``prompt`` into the worker chat's running delegate turn.

    Steers only when every condition holds — a live pump on the chat whose
    kind takes a steer and is a task lane (a person's own dashboard turn on
    the worker chat keeps today's queued run), a RUNNING delegate run on the
    chat whose callback anchor is this caller's chat (the run's report goes
    where this follow-up's answer must go), a layer that declares steering,
    and an accepted steer. Returns the spawn response for the running run
    (``steered: True``) or None, in which case the caller queues a run
    exactly as before. Accept is exactly-once: an accepted steer never also
    creates a run."""
    from core.events import input_queue
    from core.events.common_events import TurnInput
    from core.events.stream_pump import _active_pumps
    from core.session import session_kind
    from core.session.session_delivery import chat_layer
    from storage.pg import run_db

    from core.session import interactive_session

    chat_id = chat.get("id", "")
    if not chat_id or not parent_chat_id:
        return None
    pump = _active_pumps.get(chat_id)
    terminal = None
    if pump is not None and not pump.is_done:
        if getattr(pump, "source_type", "") != session_kind.TASK.source_type:
            return None
    else:
        # No pump: the lane may be a live interactive terminal a running
        # round drives (a fresh interactive round, a borrowed one) — the
        # follow-up goes into the terminal the same way, and that round's
        # report covers it.
        terminal = interactive_session.find_live_for_chat(chat_id)
        if terminal is None or not terminal.alive:
            return None
    run = await run_db(_running_delegate_run, chat_id)
    if not run:
        return None
    task_row = await run_db(task_store.get_dynamic_task, run.get("task_id") or "")
    if not task_row or (task_row.get("on_complete_chat_id") or "") != parent_chat_id:
        return None
    if terminal is not None:
        # Steered into an open turn, pasted at quiescence otherwise; the
        # transcript tailer persists the prompt row (no badge, as every
        # terminal row) and the terminal itself shows it, so no frame.
        if terminal.queue_prompt(
            prompt, "delegate_continue", steer=True, chat_id=chat_id,
        ) is None:
            return None
        logger.info(
            f"lane steer: follow-up queued on the live terminal of run={run['id']} "
            f"chat={chat_id[:8]} by {source_agent}"
        )
        return {
            "task_id": run.get("task_id"),
            "run_id": run["id"],
            "chat_id": chat_id,
            "agent": chat.get("agent"),
            "steered": True,
        }
    layer = await chat_layer(chat)
    if layer is None:
        return None
    try:
        caps = layer.capabilities_for(pump.session_id)
        if not caps.behaviour.supports_steer:
            return None
        accepted = await layer.steer(pump.session_id, prompt)
    except Exception:
        logger.warning(
            f"lane steer: attempt failed for chat={chat_id[:8]}", exc_info=True,
        )
        return None
    if not accepted:
        return None

    # The row and the live bubble, in the shape the runner gives a run's
    # prompt: the user row wears the delegating agent's badge; the frame fans
    # to every viewer of the lane through the pump.
    delegating = await run_db(agent_store.get_agent, source_agent) or {}
    meta = {
        "agent_slug": source_agent,
        "agent_display_name": delegating.get("display_name") or source_agent,
        "agent_color": delegating.get("color", ""),
        "badge": "delegated by",
    }
    # Recorded by the pump in stream order: the blocks the worker produced
    # before it are saved first, so the row sorts where the engine read it.
    qi = input_queue.QueuedInput(queue_id="", chat_id=chat_id, author_sub="",
                                 item=TurnInput(prompt))
    if not pump.record_steer(qi, extra_meta=meta, frame_extra={"event_data": meta}):
        await input_queue.record_steer_late(chat_id, qi, extra_meta=meta,
                                            frame_extra={"event_data": meta})
    logger.info(
        f"lane steer: follow-up steered into run={run['id']} chat={chat_id[:8]} "
        f"by {source_agent}"
    )
    return {
        "task_id": run.get("task_id"),
        "run_id": run["id"],
        "chat_id": chat_id,
        "agent": chat.get("agent"),
        "steered": True,
    }
