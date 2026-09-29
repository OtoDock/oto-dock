"""Scheduled fires: same-tick spawn spacing and the self-continuation fire
(coalescing cursors included).

One piece of the task scheduler; ``services/scheduler/scheduler.py`` is the
facade that assembles them and holds the registry API. Every ``core.*`` and
service import stays function-local so the standalone scheduler can import
this package without the platform.
"""

import asyncio
import collections
import functools
import json
import logging
import random
from datetime import datetime, timezone


import config
from storage import database as task_store
from services.scheduler import delivery, shared
from auth import roles
from ws import wire_events as wire

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
# cron semantics untouched); only the session start is deferred. The slot
# only ever moves forward, so a fire takes one only when it will run (after
# the usage and pool checks), at most one fire per task waits at a time
# (``_spacing_pending``), and a fire re-reads its row after the wait: the
# queue is bounded by the enabled tasks times the spacing, a refused fire
# delays nobody, and a delete or a pause takes effect before the spawn.
_spawn_slot_lock = asyncio.Lock()
_next_spawn_slot: float = 0.0
_spacing_pending: set[str] = set()


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


# Fairness across creators: waiting fires are queued per creator (FIFO) and
# the spaced slots go to the creators in round robin, so one person's burst
# of due tasks cannot push everyone else's starts back. With the spacing off
# (0), a spaced fire (a clock-aligned kind) waits a random 0 to
# _CRON_JITTER_S instead, so a cron minute does not start in one instant.
_CRON_JITTER_S = 3.0
_jitter = random.uniform


class _FairGate:
    """One loop's waiting fires: per-creator FIFOs, the round-robin order of
    the creators, and the dispatcher that releases them slot by slot."""

    def __init__(self) -> None:
        self.waiting: dict[str, collections.deque[asyncio.Future]] = {}
        self.order: collections.deque[str] = collections.deque()
        self.dispatcher: asyncio.Task | None = None

    def has_waiters(self) -> bool:
        return any(not f.done() for q in self.waiting.values() for f in q)

    def pop_next(self) -> asyncio.Future | None:
        for _ in range(len(self.order)):
            creator = self.order.popleft()
            q = self.waiting.get(creator)
            while q and q[0].done():
                q.popleft()
            if not q:
                self.waiting.pop(creator, None)
                continue
            fut = q.popleft()
            if q:
                self.order.append(creator)
            else:
                self.waiting.pop(creator, None)
            return fut
        return None

    def release_all(self) -> None:
        for q in self.waiting.values():
            for fut in q:
                if not fut.done():
                    fut.set_result(None)
        self.waiting.clear()
        self.order.clear()


_fair_gates: dict[asyncio.AbstractEventLoop, _FairGate] = {}


def _fair_gate() -> _FairGate:
    loop = asyncio.get_running_loop()
    gate = _fair_gates.get(loop)
    if gate is None:
        for stale in [lp for lp in _fair_gates if lp.is_closed()]:
            _fair_gates.pop(stale, None)
        gate = _fair_gates[loop] = _FairGate()
    return gate


async def _creator_of(task_id: str) -> str:
    """The task's creator (the fairness key); a failed read shares one bucket."""
    from storage.pg import run_db
    try:
        row = await run_db(task_store.get_dynamic_task, task_id)
        return str((row or {}).get("created_by") or "")
    except Exception:
        logger.debug("spawn gate: creator lookup failed for %s", task_id, exc_info=True)
        return ""


async def _dispatch_fair(gate: _FairGate) -> None:
    """Release the waiting fires one spaced slot at a time, the next creator
    in round robin at each slot. A waiter left behind by an ending
    dispatcher is released, never stranded (a stranded fire would keep its
    task id in ``_spacing_pending`` and drop every later fire of it)."""
    try:
        while gate.has_waiters():
            wait = await _reserve_spawn_slot()
            if wait > 0:
                await asyncio.sleep(wait)
            fut = gate.pop_next()
            if fut is not None:
                fut.set_result(None)
    finally:
        gate.release_all()


async def _spawn_spacing_gate(task_id: str) -> None:
    spacing = float(getattr(config, "TASK_SPAWN_SPACING_SECONDS", 0) or 0)
    if spacing <= 0:
        wait = _jitter(0.0, _CRON_JITTER_S)
        if wait > 0:
            await asyncio.sleep(wait)
        return
    creator = await _creator_of(task_id)
    loop = asyncio.get_running_loop()
    gate = _fair_gate()
    fut = loop.create_future()
    if creator not in gate.waiting:
        gate.waiting[creator] = collections.deque()
        gate.order.append(creator)
    gate.waiting[creator].append(fut)
    if gate.dispatcher is None or gate.dispatcher.done():
        gate.dispatcher = loop.create_task(_dispatch_fair(gate))
    queued_at = loop.time()
    await fut
    waited = loop.time() - queued_at
    if waited >= 0.5:
        logger.info("Task %s: spacing session spawn by %.1fs (herd smoothing)",
                    task_id, waited)


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

    from core.events import chat_writer
    from storage.pg import run_db
    # A Shared-only chat is woken as the person who scheduled the wake; a
    # personal chat's wake is its owner's (``scope == "user"``).
    shared_person = await run_db(delivery._shared_chat_person, chat, task.created_by)
    person = shared_person or (task.created_by if task.scope == "user" else "")
    standing = row = ""
    if person:
        standing, row = await run_db(delivery._standing_of, person, task.agent)
    if person and delivery._wake_refused(standing, shared=bool(shared_person)):
        # The delegate rung's rule, before any rung is tried: a person who
        # no longer holds the agent (on a Shared-only chat: the editor tier)
        # wakes nothing and stores nothing. The fire still counts, so the
        # row ends the way a delivered one does.
        logger.info(
            f"Continuation {task.id} not delivered: {person[:8]} no longer "
            f"holds agent '{task.agent}': chat={chat_id[:8]}"
        )
        await _end_fired_row(task)
        return ""

    wake_prompt = task.prompt
    wake_event_data = json.dumps({"prompt": wake_prompt, "task_id": task.id})
    persisted: list[str] = []

    def _persist_wake(target_chat_id: str) -> None:
        # On the chat's lane, off the loop, ahead of the wake turn's rows.
        persisted.append(target_chat_id)
        if target_chat_id:
            chat_writer.submit(
                target_chat_id,
                functools.partial(task_store.add_chat_message, target_chat_id, "event", "",
                                  event_type=wire.PERSISTED_SCHEDULE_WAKE,
                                  event_data=wake_event_data),
                label="schedule_wake",
            )

    # Same identity resolution as delegate delivery: the chat owner's remote
    # pins / role must be honored on the persistent/one-shot rungs.
    if person:
        deliver_user_sub: str | None = person
        # On a Shared-only chat the person's effective role, the one their
        # own turn there runs at; elsewhere the creator's row alone (see
        # delivery.py: the operator's call).
        deliver_role = standing if shared_person else (row or roles.VIEWER)
    else:
        deliver_user_sub = None
        # A person who scheduled a wake from a shared chat never wakes it
        # above their own role; the agent's own wakes keep the agent's.
        creator = task.created_by if task.created_by and task.created_by != task.agent else ""
        deliver_role = ((roles.row_role(await run_db(task_store.get_user_agent_roles, creator),
                                        task.agent)
                         or roles.VIEWER) if creator else roles.MANAGER)

    async def _save_echo(outcome) -> None:
        # Every rung refused (a reservation that timed out on a full box, or
        # gave way to a person opening the chat): the wake is kept for the
        # chat's next turn, unless the person who scheduled it no longer
        # holds the agent (the delegate rung's rule).
        if outcome.path == "none" and chat_id:
            if deliver_user_sub and delivery._wake_refused(
                    (await run_db(delivery._standing_of, deliver_user_sub, task.agent))[0],
                    shared=bool(shared_person)):
                logger.info(
                    f"Continuation {task.id} wake not stored: {deliver_user_sub[:8]} "
                    f"no longer holds agent '{task.agent}': chat={chat_id[:8]}"
                )
            elif await delivery._store_undelivered_wakes(chat_id, [wake_prompt]):
                logger.info(
                    f"Continuation {task.id} wake undeliverable: stored for the "
                    f"chat's next turn: chat={chat_id[:8]}"
                )
        # The echo lands on the chat's lane behind the wake's own rows (the
        # search row rebuild runs off the loop with it).
        if not outcome.response:
            return
        if outcome.chat_id:
            await chat_writer.submit(
                outcome.chat_id,
                functools.partial(task_store.add_chat_message, outcome.chat_id, "assistant",
                                  outcome.response),
                label="wake_echo",
            )
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
                "type": wire.NOTIFY_CONTINUATION_PROMPT,
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
        # A rung that raises never reaches the ladder's hook: the event row
        # is written if no rung wrote it, and the wake is stored the way the
        # hook stores an undelivered one (the standing re-check included).
        logger.exception(f"Continuation {task.id} delivery failed: chat={chat_id[:8]}")
        from core.session.session_delivery import DeliveryOutcome
        if not persisted:
            _persist_wake(chat_id)
        await _save_echo(DeliveryOutcome("none", chat_id=chat_id,
                                         session_id=chat.get("session_id") or ""))
    # A lane job, so the cursor counts the wake's own row written above.
    _continuation_cursors[chat_id] = await chat_writer.submit(
        chat_id, functools.partial(task_store.get_last_chat_message_id, chat_id),
        label="wake_cursor",
    )

    await _end_fired_row(task)
    return ""


async def _end_fired_row(task: shared.TaskDefinition) -> None:
    """Post-fire: a one-shot row has served its purpose; a recurring row
    counts the fire and self-cancels at ``max_runs``."""
    from services.scheduler import scheduler as _api
    if not task.schedule and task.interval_seconds is None:
        await _api.remove_dynamic_task(task.id)
        return
    new_count = await asyncio.to_thread(
        task_store.increment_dynamic_task_run_count, task.id,
    )
    if task.max_runs and new_count >= task.max_runs:
        logger.info(f"Continuation {task.id}: reached max_runs={task.max_runs}, cancelling")
        await _api.remove_dynamic_task(task.id)
