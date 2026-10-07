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
from storage.automation import run_status
from core import placement
from core.session import session_kind

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
    from core.session.session_manager import (
        get_layer_capabilities, resolve_execution_path,
    )
    path = resolve_execution_path(
        chat.get("agent") or "", chat.get("execution_path") or "",
    )
    caps = get_layer_capabilities(path)
    return bool(caps and caps.runtime.supports_reattach_after_restart)


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
        target = (chat or {}).get("execution_target") or placement.LOCAL
        eligible = (
            bool(session_id) and bool(chat)
            and bool(placement.machine_of(target))
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
    target = chat.get("execution_target") or placement.LOCAL
    return bool(placement.machine_of(target)) and _is_remote_cli_chat(chat)


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
    if run and run_status.is_live(run.get("status")):
        task_store.update_run(
            run_id, status=run_status.FAILED, error_message=reason,
            completed_at=_now(),
        )


def _placed_here(ctx, machine_id: str) -> bool:
    """A reloaded context belongs to the reporting machine: it names it, or
    it names none (an older index) while its placement is not a local one (a
    local context never matches an id a satellite reports)."""
    pl = getattr(ctx, "placement", None)
    placed_on = getattr(pl, "machine_id", "") or ""
    if placed_on:
        return placed_on == machine_id
    return getattr(pl, "kind", "") != placement.KIND_LOCAL


def _readopt_state(machine_id: str, session_id: str, chat: dict | None) -> tuple[str, int] | None:
    """The mode and token floor a reported session comes back with, or None
    when it must not come back. A session the platform closed on purpose (an
    idle reap, offboarding, a sign-out, a delete) lost its security context
    when it closed, and the index is written before the close returns, so a
    missing context is the record that it is closed; a context placed on
    another machine is that machine's session. The mode is the index's;
    an index written before modes were persisted falls back to the chat's
    (an unattended session runs ``auto``, a chat its row's mode). A session
    taken back in the same process (its record dropped at a reconnect after
    an expired grace) has no reloaded state: it keeps the mode and floor this
    process holds for it."""
    from core.session import session_kind
    from core.session.session_state import (
        get_session_client_type, get_session_security, live_state,
        reloaded_state, security_context_expired,
    )
    ctx = get_session_security(session_id)
    if ctx is None or not _placed_here(ctx, machine_id):
        return None
    if security_context_expired(session_id):
        return None  # its process's token expired with it
    mode, floor = reloaded_state(session_id)
    if not mode and not floor:
        mode, floor = live_state(session_id)
    if not mode:
        if not session_kind.attended(get_session_client_type(session_id)):
            mode = "auto"
        else:
            mode = (chat or {}).get("permission_mode") or "default"
    return mode, floor


def _mark_reported_starting(reported: dict[str, dict], machine_id: str,
                            others: dict[str, dict] | None = None) -> None:
    from core.session.session_manager import capabilities_for_path
    from core.session.session_state import (
        READOPT_MARK_TTL_S, get_session_security, mark_starting,
    )
    for sid, info in [*reported.items(), *(others or {}).items()]:
        ctx = get_session_security(sid) if sid else None
        if ctx is None or not _placed_here(ctx, machine_id):
            continue
        try:
            caps = capabilities_for_path(info.get("execution_path") or "")
        except ValueError:
            continue
        if caps.runtime.supports_reattach_after_restart or caps.runtime.readopts_idle_session:
            mark_starting(sid, READOPT_MARK_TTL_S)


def _clear_readopt_mark(session_id: str, machine_id: str) -> None:
    """A reported session this recovery will not adopt: its re-adoption
    mark goes now, not at the end of its window. (A chat that already has a
    live pump keeps it: that pump's own start owns the id; and a mark set
    for the machine the context is placed on is that machine's.)"""
    from core.session.session_state import clear_starting, get_session_security
    ctx = get_session_security(session_id)
    if ctx is None or _placed_here(ctx, machine_id):
        clear_starting(session_id)


def _lost_standing(machine_id: str, reported: dict[str, dict]) -> set[str]:
    """The reported sessions whose person may no longer hold them: the role
    the context started with outranks the person's role now, or the person
    lost the agent (the rule the offboarding closer applies to held
    sessions; these are not held yet). Synchronous: runs on the DB
    executor."""
    from core.session.session_state import get_session_security
    from services.agents import offboarding_sessions as offboard
    contexts = {}
    for sid in reported:
        ctx = get_session_security(sid) if sid else None
        if (ctx is not None and _placed_here(ctx, machine_id)
                and ctx.username and not ctx.is_external):
            contexts[sid] = ctx
    if not contexts:
        return set()
    rows, by_name = offboard._people(set(), {c.username for c in contexts.values()})
    return {
        sid for sid, ctx in contexts.items()
        if offboard._outranked(ctx.role or "", offboard._role_now(
            rows, by_name.get(ctx.username, ""), ctx.agent), False)
    }


async def on_sessions_alive(machine_id: str, sessions: list[dict],
                            other_sessions: list[dict] | None = None) -> None:
    """A satellite reported its headless sessions post-auth: ``sessions``,
    the live ones of the engines whose turn survives a proxy restart, and
    ``other_sessions`` (0.5.138; None when the frame did not carry it),
    every other one it holds.

    First the records of this machine whose stream an expired grace severed
    are dropped. Then: adopt each parked run whose session the satellite
    still holds; fail parked runs on this machine whose session it does NOT
    report; adopt plain (non-run) chats with a live turn so their tail lands;
    take back an idle session the platform still has a chat and a context
    for, and close the rest on the satellite."""
    reported = {s.get("session_id", ""): s for s in sessions}
    others = {s.get("session_id", ""): s for s in (other_sessions or ())
              if s.get("session_id") and s.get("session_id") not in reported}

    # Before the first await: a session the satellite kept alive is live
    # again for the re-adoption window (its context survived the restart in
    # the security index; only an engine whose sessions are re-adopted). The
    # adoption paths clear the mark when they insert or decline.
    _mark_reported_starting(reported, machine_id, others)
    from core.session.session_state import take_recover_pending
    # Every reported session's replay mark is read now, a parked one's too,
    # so none outlives this report.
    marked = {sid for sid in reported if take_recover_pending(sid)}

    from core.session.session_manager import _get_remote_layer
    remote = _get_remote_layer()
    left_to_resume = await _drop_severed(remote, machine_id, reported, others)

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

    async def _decline(sid: str, why: str, *, drop_state: bool = False,
                       incarnation: str = "") -> None:
        """A reported session the platform does not take back: its mark goes
        and its process on the satellite ends, only the object the report
        named (``drop_state``: and the context the index kept for it, when it
        is this machine's, with the session's local caches)."""
        _clear_readopt_mark(sid, machine_id)
        if drop_state:
            _drop_local_state(sid)
        logger.info("Recovery: closing %s on %s (%s)", sid[:8], machine_id[:8], why)
        if remote is not None:
            await remote.close_unadopted(machine_id, sid, incarnation)

    # Adopt parked runs.
    for sid, rec, info in to_adopt:
        state = _readopt_state(machine_id, sid, None)
        if state is None:
            rid = rec.get("run_id")
            if rid:
                await asyncio.to_thread(
                    _fail_run, rid, "Proxy restarted; the remote session was closed")
            await _decline(sid, "no security context for its run",
                           incarnation=info.get("incarnation") or "")
            continue
        asyncio.create_task(_recover_session(
            machine_id, sid, info,
            run_id=rec.get("run_id"), chat_id=rec["chat_id"],
            agent=rec["agent"], state=state,
        ))

    # Adopt plain chats whose turn was live at the shutdown (no parked run)
    # so their output lands and the turn closes: a turn the satellite still
    # runs, and one the shutdown marked that finished while the proxy was
    # down (replayed from the satellite's retained turn). Skip anything
    # already live, being spawned or parked. A chat session whose person
    # lost the standing it started with is closed instead (a run is stopped
    # by its own path). Each step looks again at "held or spawning" right
    # before it acts: a person's start may have taken the id meanwhile.
    lost = await asyncio.to_thread(_lost_standing, machine_id, {**reported, **others})
    already = {sid for sid, _, _ in to_adopt}
    replayed: set[str] = set()
    for sid, info in reported.items():
        if sid in already or sid in _parked or sid in left_to_resume:
            continue
        if not info.get("turn_active") and sid not in marked:
            continue
        replayed.add(sid)
        if _held_or_spawning(remote, sid):
            _clear_readopt_mark(sid, machine_id)
            continue  # already re-registered, or a start owns it
        incarnation = info.get("incarnation") or ""
        if sid in lost:
            await _decline(sid, "its person's access changed", drop_state=True,
                           incarnation=incarnation)
            continue
        chat = await asyncio.to_thread(task_store.get_chat_by_session, sid)
        if _held_or_spawning(remote, sid):
            continue
        if not chat:
            await _decline(sid, "no chat row",
                           drop_state=_readopt_state(machine_id, sid, None) is not None,
                           incarnation=incarnation)
            continue
        state = _readopt_state(machine_id, sid, chat)
        if state is None:
            await _decline(sid, "closed on purpose, or placed on another machine",
                           incarnation=incarnation)
            continue
        asyncio.create_task(_recover_session(
            machine_id, sid, info,
            run_id=None, chat_id=chat["id"], agent=chat.get("agent") or "",
            state=state,
        ))

    # Idle sessions the satellite kept alive across the restart get
    # re-REGISTERED (registry only — nothing streams). Without this they
    # were invisible to the remote idle reaper and lived forever
    # satellite-side, eating the machine's session capacity until every
    # new spawn failed "at capacity" (hit live 2026-08-11 after an
    # evening of proxy redeploys). With a chat row they become normal
    # idle sessions again, in their own mode and with their own context —
    # resumable, reaped on the standard leash; a session with no chat row,
    # or one the platform closed on purpose, is closed on the satellite.
    if remote is not None:
        for sid, info in reported.items():
            if (sid in already or sid in replayed or sid in _parked
                    or sid in left_to_resume or info.get("turn_active")):
                continue
            if _held_or_spawning(remote, sid):
                continue
            incarnation = info.get("incarnation") or ""
            if sid in lost:
                await _decline(sid, "its person's access changed", drop_state=True,
                               incarnation=incarnation)
                continue
            chat = await asyncio.to_thread(task_store.get_chat_by_session, sid)
            if _held_or_spawning(remote, sid):
                continue
            if not chat:
                await _decline(sid, "no chat row",
                               drop_state=_readopt_state(machine_id, sid, None) is not None,
                               incarnation=incarnation)
                continue
            state = _readopt_state(machine_id, sid, chat)
            if state is None:
                await _decline(sid, "closed on purpose, or placed on another machine",
                               incarnation=incarnation)
                continue
            try:
                if await remote.adopt_idle_session(
                    machine_id=machine_id, session_id=sid,
                    agent_name=chat.get("agent") or info.get("agent_slug") or "",
                    # The satellite names the engine each live session runs
                    # (every satellite that reports sessions_alive does).
                    execution_path=info.get("execution_path") or "",
                    use_native_permissions=bool(
                        info.get("use_native_permissions")),
                    mode=state[0], token_floor=state[1],
                ):
                    logger.info(
                        "Recovery: re-leashed idle session %s on %s (mode %s)",
                        sid[:8], machine_id[:8], state[0])
            except Exception:
                logger.exception(
                    "Recovery: idle adoption failed for %s on %s",
                    sid[:8], machine_id[:8])

        for sid, entry in others.items():
            if sid in left_to_resume:
                continue
            try:
                await _other_session(remote, machine_id, sid, entry, lost, _decline)
            except Exception:
                logger.exception("Recovery: the held session %s on %s failed",
                                 sid[:8], machine_id[:8])

    # This machine is back: redeliver any delegate wakes parked on ITS chats —
    # including idle ones the adopt loops above never touch (a wake stored
    # while the machine was away can only deliver now).
    async def _machine_wake_pass() -> None:
        from services.scheduler import scheduler as _sched
        try:
            held = remote.session_ids_on(machine_id) if remote is not None else []
            await _sched.redeliver_pending_wakes(machine_id=machine_id, session_ids=held)
        except Exception:
            logger.exception(
                "Wake redelivery failed for machine %s", machine_id[:8],
            )
    asyncio.create_task(_machine_wake_pass())
    asyncio.create_task(_machine_resume_pass(machine_id, set(reported) | set(others)))


def _held_or_spawning(remote, session_id: str) -> bool:
    return remote is not None and (
        session_id in getattr(remote, "_sessions", {}) or remote.is_spawning(session_id))


def _drop_local_state(session_id: str) -> None:
    """A declined session's context and the platform-side caches kept for it
    (the caches are named by its context, so they go first)."""
    from core.session.session_state import (
        cleanup_session_permission_state, get_session_security,
    )
    try:
        from core.remote import remote_file_flow
        remote_file_flow.cleanup_session(session_id)
    except Exception:
        logger.warning("Recovery: remote file cache of %s not purged", session_id[:8],
                       exc_info=True)
    ctx = get_session_security(session_id)
    if ctx is not None:
        try:
            from services.mcp import mcp_output_relocation
            mcp_output_relocation.cleanup_session(session_id, ctx.agent, ctx.username)
        except Exception:
            logger.warning("Recovery: MCP outputs of %s not purged", session_id[:8],
                           exc_info=True)
    cleanup_session_permission_state(session_id)


async def _drop_severed(remote, machine_id: str, reported: dict[str, dict],
                        others: dict[str, dict]) -> set[str]:
    """The records of this machine whose stream was severed (its reconnect
    grace expired before it came back: the queue was popped and an in-flight
    turn ended at the expiry), dropped: the record only, never the
    satellite's process, the context, mode, floor and binding kept. The
    report then takes an idle one back like any idle session. One the report
    names turn-active is returned: no replay (its turn ended at the expiry
    and was persisted then), its next turn resumes it."""
    left: set[str] = set()
    if remote is None:
        return left
    for sid in remote.session_ids_on(machine_id):
        if remote.is_spawning(sid) or not remote.remote_stream_severed(sid):
            continue
        try:
            await remote.prepare_resume(sid)
        except Exception:
            logger.exception("Recovery: severed record %s on %s not dropped",
                             sid[:8], machine_id[:8])
            continue
        entry = reported.get(sid) or others.get(sid)
        if entry is not None and entry.get("turn_active"):
            left.add(sid)
            _clear_readopt_mark(sid, machine_id)
        logger.info("Recovery: %s on %s lost its stream to an expired grace; its record "
                    "is dropped", sid[:8], machine_id[:8])
    return left


def _adopted_model(chat: dict, execution_path: str, reported: str) -> str:
    """The model an adopted session runs on: the chat's when its engine
    serves it, else the one the satellite reported. Synchronous: call it on
    the DB executor."""
    model = (chat or {}).get("model") or ""
    if model:
        try:
            from storage.billing import subscription_store
            if any((m.get("model_id") or "") == model
                   for m in subscription_store.list_models(execution_path)):
                return model
        except Exception:
            logger.debug("model check failed for %s", execution_path, exc_info=True)
    return reported or model


async def _other_session(remote, machine_id: str, sid: str, entry: dict,
                         lost: set[str], decline) -> None:
    """One ``other_sessions`` entry: an idle session of an engine that takes
    one back, with a thread to resume, its chat on this engine and machine,
    its person's standing and its context, is taken back; anything else is
    closed on the satellite by the incarnation the report named."""
    from core.session.session_manager import capabilities_for_path, resolve_execution_path
    if _held_or_spawning(remote, sid):
        return
    path = entry.get("execution_path") or ""
    try:
        takes_back = capabilities_for_path(path).runtime.readopts_idle_session
    except ValueError:
        takes_back = False
    why, drop, chat, state = "", False, None, None
    if not entry.get("alive"):
        why = "its process is not running"
    elif entry.get("turn_active"):
        why = "a turn nobody follows is running"
    elif not takes_back:
        why = f"{path or 'an unknown engine'} is not taken back"
    elif not entry.get("resume_handle"):
        why = "no thread to resume"
    elif sid in lost:
        why, drop = "its person's access changed", True
    else:
        chat = await asyncio.to_thread(task_store.get_chat_by_session, sid)
        pin = (chat or {}).get("execution_target") or ""
        if not chat:
            why, drop = "no chat row", _readopt_state(machine_id, sid, None) is not None
        elif resolve_execution_path(chat.get("agent") or "",
                                    chat.get("execution_path") or "") != path:
            why = "its chat runs another engine"
        elif pin and pin != machine_id:
            why = "its chat runs on another machine"
        else:
            state = _readopt_state(machine_id, sid, chat)
            if state is None:
                why = "closed on purpose, or placed on another machine"
    if why:
        if not _held_or_spawning(remote, sid):
            await decline(sid, why, drop_state=drop, incarnation=entry.get("incarnation") or "")
        return
    from storage import remote_store
    machine = await asyncio.to_thread(remote_store.get_remote_machine, machine_id)
    model = await asyncio.to_thread(_adopted_model, chat, path, entry.get("model") or "")
    if await remote.adopt_idle_session(
        machine_id=machine_id, session_id=sid,
        agent_name=chat.get("agent") or entry.get("agent_slug") or "",
        execution_path=path,
        use_native_permissions=bool(entry.get("use_native_permissions")),
        mode=state[0], token_floor=state[1],
        resume_handle=entry.get("resume_handle") or "", model=model,
        used_mcps=entry.get("mcp_servers") or (),
        allow_full_fs=bool((machine or {}).get("allow_full_fs")),
    ):
        logger.info("Recovery: took back the idle %s session %s on %s (mode %s)",
                    path, sid[:8], machine_id[:8], state[0])


async def _machine_resume_pass(machine_id: str, reported: set[str]) -> None:
    """The machine is back: what its blip left waiting resumes, beside the
    wake replay (which waits on every replayed turn). A background monitor
    rides out the grace, and one that outlived it has exited (the registries
    keep the still-running jobs): monitors are armed again for each session
    the satellite reported with its stream attached whose chat takes the
    chat contract (an ordinary chat, a worker or task chat outside its run's
    report). And the chats on the machine holding queued messages get their
    delivery armed: a start that fell in the gap deferred them, and nothing
    else would send them before the chat's next turn ends; their chips stop
    saying the machine is reconnecting."""
    from core.events import input_queue
    from core.events.pump_bg_monitors import arm_bg_monitors
    from core.session import background_leash
    from core.session.session_manager import _get_remote_layer
    from services.scheduler import lanes
    remote = _get_remote_layer()
    on_machine: list[str] = []
    try:
        if remote is not None:
            on_machine = remote.session_ids_on(machine_id)
        for sid in on_machine:
            if (sid not in reported or remote.remote_stream_severed(sid)
                    or not background_leash.background_pending_count(sid)):
                continue
            chat = await asyncio.to_thread(task_store.get_chat_by_session, sid)
            if lanes.takes_chat_contract(chat, sid):
                arm_bg_monitors(remote, sid, chat["id"])
    except Exception:
        logger.exception("Resume pass: monitors failed for machine %s", machine_id[:8])
    try:
        for chat_id in await asyncio.to_thread(
                task_store.list_chats_with_waiting_input, machine_id, on_machine):
            input_queue.clear_waiting(chat_id)
            input_queue.arm(chat_id)
    except Exception:
        logger.exception("Resume pass: queued messages failed for machine %s",
                         machine_id[:8])


async def _recover_session(
    machine_id: str, session_id: str, info: dict,
    *, run_id: str | None, chat_id: str, agent: str,
    state: tuple[str, int] = ("", 0),
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
        _clear_readopt_mark(session_id, machine_id)
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
                execution_path=info.get("execution_path") or "",
                use_native_permissions=use_native,
                mode=state[0], token_floor=state[1],
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
        source_type=(session_kind.TASK if run_id else session_kind.DASHBOARD).source_type,
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
    if not run or not run_status.is_live(run.get("status")):
        return  # already terminal (a concurrent path finished it)
    output = await asyncio.to_thread(scheduler._collect_task_output, chat_id)
    chat_row = await asyncio.to_thread(task_store.get_chat, chat_id) or {}
    cost = chat_row.get("total_cost") or 0
    await asyncio.to_thread(
        task_store.update_run, run_id, status=run_status.COMPLETED,
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
            await scheduler._deliver_task_result(task, "completed", output or "", run_id=run_id)
        except Exception:
            logger.exception("Recovery delegate delivery failed for %s", run_id)
