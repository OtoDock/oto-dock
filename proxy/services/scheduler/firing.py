"""Scheduled fires: same-tick spawn spacing and the self-continuation fire
(coalescing cursors included).

One piece of the task scheduler; ``services/scheduler/scheduler.py`` is the
facade that assembles them and holds the registry API. Every ``core.*`` and
service import stays function-local so the standalone scheduler can import
this package without the platform.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone


import config
from storage import database as task_store
from services.scheduler import delivery, shared

logger = logging.getLogger("claude-proxy.scheduler")


# Continuation coalescing: chat_id → the chat's last message id right after a
# wake was delivered. A new wake is SKIPPED while that id hasn't advanced —
# the previous wake is still unprocessed in the chat (queued behind a turn /
# gate), and stacking wake prompts would burn turns on stale pings. In-memory
# only: a restart forgets the cursor, worst case one extra wake (documented).
_continuation_cursors: dict[str, int] = {}
# Consecutive coalesce-skips per continuation task id. A recurring continuation
# bounded only by max_runs on a permanently-dead chat never increments
# run_count (skips return early), so the row would outlive its bound until
# manual delete. From the 3rd consecutive skip onward each skip counts toward
# max_runs; the first two stay free so a slow live turn never burns runs.
# In-memory like the cursors — a restart just restarts the grace window.
_continuation_skip_counts: dict[str, int] = {}
_SKIP_AGING_GRACE = 2


# ── Same-tick spawn spacing ─────────────────────────────────────────────
# APScheduler fires every job due at the same instant concurrently, so tasks
# sharing a fire time (several 06:30 digests) used to start their CLI +
# sandbox + MCP handshakes in the same second — a burst that spikes RAM/CPU
# on small hosts. Scheduled fires now RESERVE start slots at least
# config.TASK_SPAWN_SPACING_SECONDS apart: reservation happens under a lock,
# the sleep happens outside it, so concurrent fires wait in parallel and
# wake staggered in reservation order. A lone fire reserves the current
# instant and waits zero. Fire TIMES stay exact (run rows, next_run_time and
# cron semantics untouched) — only the session start is deferred.
_spawn_slot_lock = asyncio.Lock()
_next_spawn_slot: float = 0.0


async def _reserve_spawn_slot() -> float:
    """Reserve the next task-session start slot; return seconds to wait."""
    global _next_spawn_slot
    spacing = float(getattr(config, "TASK_SPAWN_SPACING_SECONDS", 0) or 0)
    if spacing <= 0:
        return 0.0
    async with _spawn_slot_lock:
        now = asyncio.get_running_loop().time()
        slot = max(now, _next_spawn_slot)
        _next_spawn_slot = slot + spacing
        return slot - now


async def _spawn_spacing_gate(task_id: str) -> None:
    wait = await _reserve_spawn_slot()
    if wait > 0:
        logger.info("Task %s: spacing session spawn by %.1fs (herd smoothing)",
                    task_id, wait)
        await asyncio.sleep(wait)


async def _fire_continuation(task: shared.TaskDefinition) -> str:
    """Fire one scheduled self-continuation: deliver the wake prompt INTO the
    target chat as a new turn (via the session-delivery ladder — live pump,
    PTY injection, dead-session resume all inherited). No run row, no task
    slot — the driven turn's cost lands on the chat like any other turn.

    Guards, in order: chat gone → self-cancel; past ``until_at`` → self-cancel;
    coalesce (previous wake unprocessed → skip). Post-fire: one-shot rows
    auto-delete; recurring rows count fires and self-cancel at ``max_runs``."""
    from services.scheduler import scheduler as _api  # the registry API; a module import would cycle with the facade
    from core.session.session_delivery import deliver_prompt

    chat_id = task.target_chat_id or ""
    chat = await asyncio.to_thread(task_store.get_chat, chat_id) if chat_id else None
    if not chat:
        logger.info(f"Continuation {task.id}: chat {chat_id[:8] or '-'} gone — cancelling")
        await _api.remove_dynamic_task(task.id)
        _continuation_cursors.pop(chat_id, None)
        _continuation_skip_counts.pop(task.id, None)
        return ""

    if task.until_at:
        try:
            until = datetime.fromisoformat(task.until_at)
            if until.tzinfo is None:
                until = until.replace(tzinfo=_api._resolve_task_tz(task))
            if datetime.now(timezone.utc) >= until:
                logger.info(f"Continuation {task.id}: past until={task.until_at} — cancelling")
                await _api.remove_dynamic_task(task.id)
                return ""
        except ValueError:
            logger.warning(f"Continuation {task.id}: bad until_at {task.until_at!r} — ignoring bound")

    last_id = await asyncio.to_thread(task_store.get_last_chat_message_id, chat_id)
    prev = _continuation_cursors.get(chat_id)
    if prev is not None and last_id <= prev:
        skips = _continuation_skip_counts.get(task.id, 0) + 1
        _continuation_skip_counts[task.id] = skips
        logger.info(
            f"Continuation {task.id}: previous wake still unprocessed on "
            f"chat {chat_id[:8]} — coalesced (skipped ×{skips})"
        )
        recurring = bool(task.schedule) or task.interval_seconds is not None
        if recurring and task.max_runs and skips > _SKIP_AGING_GRACE:
            new_count = await asyncio.to_thread(
                task_store.increment_dynamic_task_run_count, task.id,
            )
            if new_count >= task.max_runs:
                logger.info(
                    f"Continuation {task.id}: dead-chat skips reached "
                    f"max_runs={task.max_runs} — cancelling"
                )
                await _api.remove_dynamic_task(task.id)
                _continuation_skip_counts.pop(task.id, None)
        return ""
    _continuation_skip_counts.pop(task.id, None)

    wake_prompt = task.prompt
    wake_event_data = json.dumps({"prompt": wake_prompt, "task_id": task.id})

    def _persist_wake(target_chat_id: str) -> None:
        if target_chat_id:
            task_store.add_chat_message(target_chat_id, "event", "",
                event_type="schedule_wake", event_data=wake_event_data)

    # Same identity resolution as delegate delivery: the chat owner's remote
    # pins / role must be honored on the persistent/one-shot rungs.
    if task.scope == "user" and task.created_by:
        deliver_user_sub: str | None = task.created_by
        deliver_role = task_store.get_user_agent_roles(task.created_by).get(task.agent, "viewer")
    else:
        deliver_user_sub = None
        deliver_role = "manager"

    def _save_echo(outcome) -> None:
        if not outcome.response:
            return
        if outcome.chat_id:
            task_store.add_chat_message(outcome.chat_id, "assistant", outcome.response)
        else:
            from core.session import session_state as _state_mod
            _state_mod._save_pending_result(outcome.session_id, outcome.response)

    try:
        outcome = await deliver_prompt(
            chat_id, wake_prompt,
            source="schedule_wake",
            session_id=chat.get("session_id") or "",
            agent=task.agent,
            user_sub=deliver_user_sub,
            role=deliver_role,
            notify_payload={
                "type": "continuation_prompt",
                "session_id": chat.get("session_id") or "",
                "chat_id": chat_id,
                "task_id": task.id,
                "task_name": task.name,
                "prompt": wake_prompt,
            },
            persist_event=_persist_wake,
            persistent_fn=delivery._deliver_via_persistent,
            oneshot_fn=delivery._deliver_via_oneshot,
            on_outcome=_save_echo,
        )
        logger.info(f"Continuation {task.id} fired via {outcome.path}: chat={chat_id[:8]}")
    except Exception:
        logger.exception(f"Continuation {task.id} delivery failed: chat={chat_id[:8]}")
    _continuation_cursors[chat_id] = await asyncio.to_thread(
        task_store.get_last_chat_message_id, chat_id,
    )

    if not task.schedule and task.interval_seconds is None:
        # One-shot wake — the row's purpose is served.
        await _api.remove_dynamic_task(task.id)
    else:
        new_count = await asyncio.to_thread(
            task_store.increment_dynamic_task_run_count, task.id,
        )
        if task.max_runs and new_count >= task.max_runs:
            logger.info(f"Continuation {task.id}: reached max_runs={task.max_runs} — cancelling")
            await _api.remove_dynamic_task(task.id)
    return ""
