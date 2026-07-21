"""Mode C — re-attach in-flight remote turns across a proxy restart.

A proxy restart used to blind-fail every in-flight task run at startup
(``mark_orphaned_runs_failed``) and drop the streamed tail; the run showed
as failed even though the satellite kept the CLI alive and the turn either
finished or is still running. This module replaces that for **remote
claude-code-cli** sessions:

1. At startup, ``defer_orphaned_runs`` parks recovery-eligible runs (remote
   CLI, pinned target, chat row present) with a deadline instead of failing
   them; everything else fails as before.
2. When a satellite reconnects it reports its live headless sessions
   (``sessions_alive``). ``on_sessions_alive`` adopts each parked run whose
   session the satellite still holds — replaying the retained turn buffer
   through a fresh ``ChatStreamPump`` (so dashboard viewers see it stream
   as if never disconnected), then finalizing the run. Plain (non-run)
   chats with a live turn are adopted too, so their tail lands and the turn
   closes. Parked runs whose session the satellite does NOT report are
   failed with a clear reason.
3. A sweeper fails runs whose recovery deadline lapses with no reconnect.

Codex (thread-id resume already covers next-turn continuity) and direct-LLM
(in-process) are out of scope.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from storage import database as task_store

logger = logging.getLogger("claude-proxy.recovery")

# How long a parked run waits for its satellite to reconnect before it is
# failed. Generous: a restart + fleet reconnect (jittered backoff, cap 30s)
# plus workspace re-sync can take a while.
RECOVERY_DEADLINE_S = 180.0

# session_id -> parked recovery record. A record may or may not carry a run_id
# (a dashboard-driven continuation turn on a task chat has no scheduler task).
_parked: dict[str, dict] = {}
_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_remote_cli_chat(chat: dict) -> bool:
    """Effective-execution-path test for recovery eligibility. The chat column
    may be EMPTY (= agent default — delegate worker chats never stamp it), so
    resolve it the way the spawn path does before comparing; the literal
    comparison silently excluded every delegate lane from Mode C."""
    from core.session.session_manager import resolve_execution_path
    path = resolve_execution_path(
        chat.get("agent") or "", chat.get("execution_path") or "",
    )
    return path == "claude-code-cli"


def defer_orphaned_runs() -> tuple[int, int]:
    """Startup: park recovery-eligible orphaned runs, fail the rest.

    Returns ``(parked, failed)``. Must run BEFORE the satellite heartbeat
    monitor so a run is parked before its satellite can report it."""
    orphaned = task_store.list_orphaned_runs()
    parked_ids: list[str] = []
    for run in orphaned:
        chat_id = run.get("chat_id") or ""
        session_id = run.get("session_id") or ""
        chat = task_store.get_chat(chat_id) if chat_id else None
        target = (chat or {}).get("execution_target") or "local"
        eligible = (
            bool(session_id) and bool(chat)
            and target not in ("", "local")
            and _is_remote_cli_chat(chat)
        )
        if eligible:
            _parked[session_id] = {
                "run_id": run["id"],
                "chat_id": chat_id,
                "machine_id": target,
                "agent": run.get("agent") or (chat or {}).get("agent") or "",
                "deadline": time.monotonic() + RECOVERY_DEADLINE_S,
            }
            parked_ids.append(run["id"])
    # Blind-fail every orphaned row EXCEPT the ones we just parked.
    failed = task_store.mark_orphaned_runs_failed(exclude_ids=parked_ids)
    if parked_ids:
        logger.info(
            "Startup recovery: parked %d run(s) for satellite re-adopt, "
            "failed %d non-recoverable", len(parked_ids), failed,
        )
    return len(parked_ids), failed


def register(connection_manager) -> None:
    """Wire the sessions_alive callback + start the deadline sweeper."""
    connection_manager.set_sessions_alive_callback(on_sessions_alive)


def is_recovery_eligible(chat_id: str) -> bool:
    """True when a chat's in-flight turn can be re-adopted from its satellite
    after a proxy restart: a remote claude-code-cli session pinned to a
    machine. Used by the shutdown path to LEAVE such a run running (so
    ``defer_orphaned_runs`` parks it next boot) instead of failing + closing
    it."""
    chat = task_store.get_chat(chat_id) if chat_id else None
    if not chat:
        return False
    target = chat.get("execution_target") or "local"
    return target not in ("", "local") and _is_remote_cli_chat(chat)


async def sweep_expired() -> None:
    """Fail parked runs whose recovery deadline lapsed (satellite never
    reconnected). Runs on the reaper cadence."""
    now = time.monotonic()
    async with _lock:
        expired = [
            sid for sid, rec in _parked.items() if now > rec["deadline"]
        ]
        records = [_parked.pop(sid) for sid in expired]
    for rec in records:
        rid = rec.get("run_id")
        if rid:
            await asyncio.to_thread(_fail_run, rid,
                                    "Proxy restarted; satellite did not "
                                    "reconnect in time")
        logger.info(
            "Recovery: parked session %s deadline lapsed — failed run %s",
            (rec.get("chat_id") or "")[:16], rid or "(none)",
        )


def _fail_run(run_id: str, reason: str) -> None:
    run = task_store.get_run(run_id)
    if run and run.get("status") in ("running", "pending"):
        task_store.update_run(
            run_id, status="failed", error_message=reason,
            completed_at=_now(),
        )


async def on_sessions_alive(machine_id: str, sessions: list[dict]) -> None:
    """A satellite reported its live headless sessions post-auth.

    Adopt each parked run whose session the satellite still holds; fail
    parked runs on this machine whose session it does NOT report; and adopt
    plain (non-run) chats with a live turn so their tail lands."""
    reported = {s.get("session_id", ""): s for s in sessions}

    async with _lock:
        # Parked runs for THIS machine.
        mine = {
            sid: rec for sid, rec in _parked.items()
            if rec["machine_id"] == machine_id
        }

    to_adopt: list[tuple[str, dict, dict]] = []
    to_fail: list[tuple[str, dict]] = []
    for sid, rec in mine.items():
        info = reported.get(sid)
        if info is not None:
            to_adopt.append((sid, rec, info))
        else:
            to_fail.append((sid, rec))

    async with _lock:
        for sid, _rec, _info in to_adopt:
            _parked.pop(sid, None)
        for sid, _rec in to_fail:
            _parked.pop(sid, None)

    for sid, rec in to_fail:
        rid = rec.get("run_id")
        if rid:
            await asyncio.to_thread(
                _fail_run, rid,
                "Proxy restarted; the remote session was lost",
            )
        logger.info(
            "Recovery: run %s session not reported by %s — failed",
            rid or "(none)", machine_id[:8],
        )

    # Adopt parked runs.
    for sid, rec, info in to_adopt:
        asyncio.create_task(_recover_session(
            machine_id, sid, info,
            run_id=rec.get("run_id"), chat_id=rec["chat_id"],
            agent=rec["agent"],
        ))

    # Adopt plain chats with a live turn (no parked run) so their tail lands
    # and the turn closes — skip anything already live or parked.
    already = {sid for sid, _, _ in to_adopt}
    for sid, info in reported.items():
        if sid in already or sid in _parked:
            continue
        if not info.get("turn_active"):
            continue
        chat = await asyncio.to_thread(task_store.get_chat_by_session, sid)
        if not chat:
            continue
        from core.session.session_manager import _get_remote_layer
        layer = _get_remote_layer()
        if layer is not None and sid in getattr(layer, "_sessions", {}):
            continue  # already re-registered
        asyncio.create_task(_recover_session(
            machine_id, sid, info,
            run_id=None, chat_id=chat["id"], agent=chat.get("agent") or "",
        ))

    # This machine is back: redeliver any delegate wakes parked on ITS chats —
    # including idle ones the adopt loops above never touch (a wake stored
    # while the machine was away can only deliver now).
    async def _machine_wake_pass() -> None:
        from services.scheduler import scheduler as _sched
        try:
            await _sched.redeliver_pending_wakes(machine_id=machine_id)
        except Exception:
            logger.exception(
                "Wake redelivery failed for machine %s", machine_id[:8],
            )
    asyncio.create_task(_machine_wake_pass())


async def _recover_session(
    machine_id: str, session_id: str, info: dict,
    *, run_id: str | None, chat_id: str, agent: str,
) -> None:
    """Drive the adopted turn through a recovery pump, then finalize the run."""
    from core.events.stream_pump import ChatStreamPump, _active_pumps
    from core.session.session_manager import _get_remote_layer

    # Never double-drive a chat already streaming (a user's warmup may have
    # re-registered it while we were dispatching).
    existing = _active_pumps.get(chat_id)
    if existing is not None and not existing.is_done:
        logger.info(
            "Recovery: chat %s already has a live pump — skipping adopt",
            chat_id[:16],
        )
        return

    layer = _get_remote_layer()
    if layer is None:
        return

    command_id = info.get("command_id", "")
    use_native = bool(info.get("use_native_permissions"))
    event_queue: asyncio.Queue = asyncio.Queue()

    async def _produce():
        from core.events.common_events import CommonEvent, ERROR, PRODUCER_DONE
        try:
            async for event in layer.adopt_session(
                machine_id=machine_id, session_id=session_id,
                agent_name=agent, command_id=command_id,
                use_native_permissions=use_native,
            ):
                await event_queue.put(event)
        except Exception as e:  # noqa: BLE001
            logger.exception("Recovery producer failed for %s", session_id[:8])
            await event_queue.put(CommonEvent(type=ERROR,
                                              data={"message": str(e)}))
        finally:
            await event_queue.put(CommonEvent(type=PRODUCER_DONE, data={}))

    producer = asyncio.create_task(_produce())
    from core.session.session_state import get_permission_queue
    pump = ChatStreamPump(
        chat_id=chat_id,
        session_id=session_id,
        producer=producer,
        event_queue=event_queue,
        perm_queue=get_permission_queue(session_id),
        source_type="task" if run_id else "chat",
    )
    _active_pumps[chat_id] = pump
    pump.start()
    logger.info(
        "Recovery: adopting turn for chat=%s session=%s run=%s",
        chat_id[:16], session_id[:8], run_id or "(none)",
    )
    try:
        await pump._task
    except Exception:
        logger.exception("Recovery pump error for chat %s", chat_id[:16])

    if run_id:
        await _finalize_run(run_id, chat_id)


async def _finalize_run(run_id: str, chat_id: str) -> None:
    """Complete a recovered run: persist the collected output + cost, then
    fire the delegate callback if any. Called after the recovery pump ends."""
    from services.scheduler import scheduler
    run = await asyncio.to_thread(task_store.get_run, run_id)
    if not run or run.get("status") not in ("running", "pending"):
        return  # already terminal (a concurrent path finished it)
    output = await asyncio.to_thread(scheduler._collect_task_output, chat_id)
    chat_row = await asyncio.to_thread(task_store.get_chat, chat_id) or {}
    cost = chat_row.get("total_cost") or 0
    await asyncio.to_thread(
        task_store.update_run, run_id, status="completed",
        output_text=output[:10000] if output else "",
        completed_at=_now(),
        cost_usd=cost if cost > 0 else None,
        chat_id=chat_id,
    )
    logger.info("Recovery: run %s completed after re-adopt", run_id)
    # Delegate hand-off (best-effort; self-gates on on_complete_agent).
    dyn = await asyncio.to_thread(
        task_store.get_dynamic_task, run.get("task_id") or "",
    )
    if dyn:
        task = scheduler._row_to_task(dyn)
        try:
            await scheduler._deliver_task_result(task, "completed", output or "")
        except Exception:
            logger.exception("Recovery delegate delivery failed for %s", run_id)
