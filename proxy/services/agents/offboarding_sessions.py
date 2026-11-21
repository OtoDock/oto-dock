"""Offboarding: close a person's live sessions on the agents they lost.

Subscribes to ``services/agents/offboarding.py`` (``sessions``, priority
10, so it runs first). A person removed from an agent, demoted there, or
deleted must not keep a running session on it: the session's mounts, the
role baked into it at start and its token all date from before the change.
A contributor demoted to viewer loses the read-write workspace mount this
way, an editor a Shared-only chat, a manager the read-write ``config/`` and
``knowledge/``.

What closes: the person's chat sessions in every pool (Claude, Codex,
Direct, a paired machine), their interactive terminals, their pre-warms, a
Shared-only chat's session when the person is the one who warmed it, and
their user-scope task runs (delegate workers included) on an agent they can
no longer reach, a chat lane they delegated that runs as them on their
own computer (its session and its run: it took their role and token there),
and a lane that runs as the agent on their own AI accounts once they are
below the editor tier there.
What does not: other agent-scope runs (they act as the agent; their
ownership moves to the admin who made the change), meetings, phone
calls (they end at hangup and the next call re-resolves its route),
external callers and sessions another person warmed. A closed chat stays:
its next message starts a session under the person's standing then.

The event is sent once and may be stale by the time it runs, so the
person's standing is read again before anything closes, and again on two
later passes (a connected dashboard refreshes its access at most every
minute, and a warm-up in flight, a paired machine's especially, can land
after the first pass). Sessions re-adopted from paired machines after a
restart are checked once, a little after boot, in case an event was lost.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from auth import roles
from services.agents import offboarding
from storage.pg import run_db

logger = logging.getLogger("otodock.offboarding")

SUBSCRIBER = "sessions"
REASON = "access_revoked"
RESWEEP_AFTER_S = (75.0, 240.0)
BOOT_RECONCILE_AFTER_S = 120.0
CLOSE_CONCURRENCY = 4
CLOSE_BATCH_TIMEOUT_S = 120.0
# Client types of a person's own chat sessions: "" is a session recorded
# before the type was, or one re-adopted from a paired machine after a
# restart (whatever it ran, its person's standing still decides).
_CHAT_CLIENT_TYPES = ("dashboard", "")
_LOGGED = 10
# The kinds of live session the closer tells apart.
POOL, INTERACTIVE, PREWARM, WORKER = "pool", "interactive", "prewarm", "worker"

_background: set[asyncio.Task] = set()
# The pending later passes per person, with the event they sweep.
_resweeps: dict[str, tuple[asyncio.Task, offboarding.OffboardEvent]] = {}


@dataclass(frozen=True)
class Live:
    """One live chat session with a person behind it."""
    session_id: str
    agent: str
    kind: str       # POOL | INTERACTIVE | PREWARM | WORKER
    role: str       # the role it started with ("" when unknown)
    user_sub: str   # "" when only the username is known
    username: str


def _keep(task: asyncio.Task) -> asyncio.Task:
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def live_sessions() -> list[Live]:
    """Every live chat session with a person behind it. On the loop, with no
    await (the registries are loop-owned; the snapshot is atomic). The
    offboarding passes read it, and so does the team account's flip
    (``chat_reload.team_account_changed``)."""
    from core.session import (
        interactive_session,
        prewarm_session_registry as prewarm,
        session_kind,
    )
    from core.session.session_manager import _iter_session_holders
    from core.session.session_state import (
        _meeting_session_info,
        get_session_client_type,
        get_session_security,
    )
    from core.session.visibility import SCOPE_AGENT
    from services.engines import subscription_pool

    out: dict[str, Live] = {}
    for sid, entry in list(prewarm._entries.items()):
        if entry.user_sub:
            out[sid] = Live(sid, entry.agent, PREWARM, entry.role or "", entry.user_sub, "")
    for sid in interactive_session.live_session_ids():
        s = interactive_session.get(sid)
        if s is None or not s.user_sub or session_kind.is_task_chat_id(s.chat_id):
            continue
        out.setdefault(sid, Live(sid, s.agent_name, INTERACTIVE, s.role or "",
                                  s.user_sub, s.username or ""))
    for layer in _iter_session_holders():
        for sid in layer.local_session_ids():
            if sid in out or sid in _meeting_session_info:
                continue
            ctx = get_session_security(sid)
            if ctx is None or ctx.is_external:
                continue
            client = get_session_client_type(sid)
            if client == session_kind.TASK.name:
                if ctx.session_scope != SCOPE_AGENT:
                    continue
                if ctx.username and ctx.placement.user_paired:
                    # A chat lane on its delegator's own computer runs as
                    # them (their name and role in an agent-scope context).
                    out[sid] = Live(sid, ctx.agent, WORKER, ctx.role or "", "", ctx.username)
                    continue
                # A lane that runs as the agent on a person's own accounts
                # (``core/config/spender.py``) is theirs to lose: it may run
                # on while they hold the editor tier there.
                payer = subscription_pool.bound_attribution(sid)[1]
                if payer:
                    out[sid] = Live(sid, ctx.agent, WORKER, roles.EDITOR, payer, "")
                continue
            if not ctx.username:
                continue
            if client not in _CHAT_CLIENT_TYPES:
                continue
            out[sid] = Live(sid, ctx.agent, POOL, ctx.role or "", "", ctx.username)
    return list(out.values())


def _people(subs: set[str], usernames: set[str]) -> tuple[dict, dict]:
    """``({sub: user row or None}, {username: sub or ""})``. Synchronous:
    runs on the DB executor."""
    from storage import database as task_store
    by_name = {name: task_store.get_user_sub_by_username(name) or "" for name in usernames}
    rows = {}
    for sub in subs | {s for s in by_name.values() if s}:
        user = task_store.get_user(sub)
        rows[sub] = (user, task_store.get_user_agent_roles(sub) if user else {})
    return rows, by_name


def _role_now(rows: dict, sub: str, agent: str) -> str:
    user, agent_roles = rows.get(sub) or (None, {})
    if not user:
        return roles.NO_ACCESS
    return roles.effective_role(user.get("role"), agent_roles, agent)


def _outranked(start: str, now: str, lost_here: bool) -> bool:
    """Whether a session started as ``start`` may not run on as ``now``."""
    if now == roles.NO_ACCESS:
        return True
    if not start:
        return lost_here
    return roles.rank(start) > roles.rank(now)


def stop_turn(session_id: str, *, queue_reason: str = "revoked") -> None:
    """Stop the session's live turn the way the Stop button's hard path
    does, before its session is closed: no check judges the cut turn, the
    chat's queue goes back to its authors (``queue_reason``), the pump is
    aborted. The turn ends here, so the close never reaches the chat as a
    lost machine or an engine that exited. Synchronous. The one owner of this
    stop: offboarding and an account leaving an agent's set
    (``services/remote/chat_reload.py``) call it."""
    from core.events.stream_pump import _active_pumps
    from core.session import session_events
    pump = next((p for p in list(_active_pumps.values())
                 if p.session_id == session_id and not p.is_done), None)
    if pump is None:
        return
    session_events.note_user_message(session_id)
    pump.cancel_all_queued(queue_reason)
    pump.abort()


def _drop_queued_prompts(session_id: str) -> None:
    """An interactive terminal hands its queued server prompts back to the
    delivery ladder on close, under the role they were queued with: the
    person must not get a session back that way."""
    from core.session import interactive_session
    s = interactive_session.get(session_id)
    queue = getattr(s, "_prompt_queue", None)
    if queue:
        queue.clear()


def _stop_worker_run(session_id: str) -> None:
    """The run behind a chat lane's session, stopped as a platform
    interrupt (its chat keeps what it wrote). Synchronous, on the loop."""
    from services.scheduler import shared
    from services.scheduler.scheduler import platform_cancel_run
    from storage import database as task_store
    for rid, running in list(shared._running_tasks.items()):
        run = None if running.done() else task_store.get_run(rid)
        if run and run.get("session_id") == session_id:
            platform_cancel_run(rid, REASON)


async def _close_one(v: Live, reason: str, drop_queued: bool) -> None:
    if v.kind == INTERACTIVE:
        from core.session import interactive_session
        # Revoked: the close itself drops the queued server prompts (a queue
        # that filled between the early drop in close_sessions and this
        # close included).
        await interactive_session.close_session(
            v.session_id, reason=reason, drop_queued=drop_queued)
        return
    from core.session.session_manager import find_layer_for_session
    layer = find_layer_for_session(v.session_id)
    if layer is not None:
        await layer.close_session(v.session_id)


async def close_sessions(victims: list[Live], why: str, *, reason: str = REASON,
                         revoked: bool = True,
                         after: Callable[[Live], Awaitable[None]] | None = None) -> None:
    """Close ``victims``: each turn stopped (``stop_turn``) at once, the
    closes (slow: a CLI waits up to 10 s, a paired machine 15 s) in a bounded
    background batch, with ``after`` run for each once its close is done,
    failed or not. A close is never cancelled half-way (that would leak its
    slot, its seat and its context). ``revoked``: the person lost access, so
    a terminal's queued prompts are dropped instead of handed back and the
    chat's queue goes back as revoked; ``reason``: what the closes are
    recorded under."""
    if not victims:
        return
    from core.session import prewarm_session_registry as prewarm
    for v in victims:
        if v.kind == INTERACTIVE:
            if revoked:
                _drop_queued_prompts(v.session_id)
        else:
            stop_turn(v.session_id, queue_reason="revoked" if revoked else "stopped")
        if v.kind == WORKER:
            _stop_worker_run(v.session_id)
    for v in victims:
        if v.kind == PREWARM:
            await prewarm.claim(v.session_id)
    logger.info("offboarding: closing %d live session(s) (%s) [%s%s]", len(victims), why,
                ", ".join(f"{v.agent}:{v.session_id[:8]}" for v in victims[:_LOGGED]),
                f", +{len(victims) - _LOGGED} more" if len(victims) > _LOGGED else "")
    gate = asyncio.Semaphore(CLOSE_CONCURRENCY)

    async def one(v: Live) -> None:
        async with gate:
            try:
                await _close_one(v, reason, revoked)
            except Exception:  # noqa: BLE001  # offboarding closes each session: one failure must not stop the rest
                logger.warning("offboarding: closing %s session %s on %s failed",
                               v.kind, v.session_id[:8], v.agent, exc_info=True)
            if after is not None:
                try:
                    await after(v)
                except Exception:  # noqa: BLE001  # each session's own follow-up: one failure must not stop the rest
                    logger.warning("offboarding: after closing session %s on %s",
                                   v.session_id[:8], v.agent, exc_info=True)

    async def batch() -> None:
        tasks = [asyncio.ensure_future(one(v)) for v in victims]
        _, pending = await asyncio.wait(tasks, timeout=CLOSE_BATCH_TIMEOUT_S)
        if pending:
            logger.warning("offboarding: %d close(s) still running after %.0f s (%s)",
                           len(pending), CLOSE_BATCH_TIMEOUT_S, why)

    _keep(asyncio.ensure_future(batch()))


def _person_matches(v: Live, sub: str, username: str) -> bool:
    return (bool(v.user_sub) and v.user_sub == sub) or (
        not v.user_sub and bool(username) and v.username == username)


async def _stop_runs(sub: str, lost: set[str] | None, rows: dict) -> int:
    """Stop the person's live user-scope runs on agents they can no longer
    reach (``lost`` None: any agent)."""
    from core.session.visibility import SCOPE_USER
    from services.scheduler import shared
    from services.scheduler.scheduler import platform_cancel_run
    from storage import database as task_store
    run_ids = [rid for rid, t in list(shared._running_tasks.items()) if not t.done()]
    if not run_ids:
        return 0

    def mine() -> list[dict]:
        out = []
        for rid in run_ids:
            run = task_store.get_run(rid)
            if run and run.get("scope") == SCOPE_USER and run.get("created_by") == sub:
                out.append(run)
        return out

    stopped = 0
    for run in await run_db(mine):
        agent = run.get("agent") or ""
        if lost is not None and agent not in lost:
            continue
        if _role_now(rows, sub, agent) != roles.NO_ACCESS:
            continue
        if platform_cancel_run(run["id"], REASON):
            stopped += 1
    if stopped:
        logger.info("offboarding: stopped %d user-scope run(s) of %s", stopped, sub)
    return stopped


async def sweep(event: offboarding.OffboardEvent) -> int:
    """Close what the person may no longer hold; returns how many sessions
    it closes (the closes finish in the background)."""
    deleted = event.reason == offboarding.DELETED
    lost = None if deleted else {a.agent for a in event.agents}
    if lost is not None and not lost:
        return 0
    rows, by_name = await run_db(_people, {event.sub},
                                 {event.username} if event.username else set())
    # A username now held by someone else (the person deleted, a new one
    # created under the same name) is not this person's any more.
    username = event.username if by_name.get(event.username, "") in ("", event.sub) else ""
    victims = []
    for v in live_sessions():
        if not _person_matches(v, event.sub, username):
            continue
        if lost is not None and v.agent not in lost:
            continue
        if _outranked(v.role, _role_now(rows, event.sub, v.agent), True):
            victims.append(v)
    await close_sessions(victims, f"{event.sub} {event.reason}")
    await _stop_runs(event.sub, lost, rows)
    return len(victims)


async def _resweep(event: offboarding.OffboardEvent) -> None:
    try:
        for delay in RESWEEP_AFTER_S:
            await asyncio.sleep(delay)
            await sweep(event)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("offboarding: a later pass for %s failed", event.sub)


def _widened(pending: offboarding.OffboardEvent | None,
             event: offboarding.OffboardEvent) -> offboarding.OffboardEvent:
    """The event the later passes sweep: ``event`` with the agents of the
    person's pending one added. One admin change sends ``removed`` and
    ``demoted`` apart, and each keeps its later passes; a deletion already
    covers every agent."""
    if pending is None or event.reason == offboarding.DELETED:
        return event
    if pending.reason == offboarding.DELETED:
        return pending
    seen = {a.agent for a in event.agents}
    return dataclasses.replace(
        event, agents=event.agents + tuple(a for a in pending.agents if a.agent not in seen))


async def on_offboard(event: offboarding.OffboardEvent) -> None:
    """The subscriber: one pass now, two later ones. A new event for the
    same person replaces the pending later passes with ones over both
    events' agents."""
    await sweep(event)
    previous = _resweeps.pop(event.sub, None)
    pending = None
    if previous is not None:
        prev_task, prev_event = previous
        if not prev_task.done():
            prev_task.cancel()
            pending = prev_event
    later = _widened(pending, event)
    task = _keep(asyncio.ensure_future(_resweep(later)))
    _resweeps[event.sub] = (task, later)

    def _forget(done: asyncio.Task, sub: str = event.sub) -> None:
        entry = _resweeps.get(sub)
        if entry is not None and entry[0] is done:
            _resweeps.pop(sub, None)

    task.add_done_callback(_forget)


async def reconcile() -> int:
    """One pass over every live chat session: close what its person may no
    longer hold (an event a restart dropped, a session re-adopted from a
    paired machine). Returns how many it closes."""
    live = live_sessions()
    if not live:
        return 0
    rows, by_name = await run_db(_people, {v.user_sub for v in live if v.user_sub},
                                 {v.username for v in live if not v.user_sub})
    victims = []
    for v in live:
        sub = v.user_sub or by_name.get(v.username, "")
        if _outranked(v.role, _role_now(rows, sub, v.agent), False):
            victims.append(v)
    await close_sessions(victims, "reconcile")
    return len(victims)


async def _reconcile_after_boot() -> None:
    try:
        await asyncio.sleep(BOOT_RECONCILE_AFTER_S)
        await reconcile()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("offboarding: the boot reconcile failed")


async def close_person_sessions(sub: str, username: str, reason: str) -> int:
    """Close the person's chat sessions on every agent (not their runs): for
    a change that ends their old credentials, a password change, so a warm
    chat starts again with a fresh token. Returns how many."""
    victims = [v for v in live_sessions() if _person_matches(v, sub, username)]
    await close_sessions(victims, f"{sub} {reason}")
    # A remote session the index kept across a restart, not yet reported by
    # its machine, is held by nothing: drop its context so the re-adoption
    # closes it instead of bringing back its old token.
    from core.session import session_state
    dropped = session_state.drop_reloaded_contexts_of(username)
    if dropped:
        # On disk before the change is answered: a crash after it never lets
        # their machine's report take them back.
        await session_state.security_index_written()
        logger.info("offboarding: %s %s: %d reloaded session(s) awaiting re-adoption dropped",
                    sub[:12], reason, len(dropped))
    return len(victims) + len(dropped)


def register() -> None:
    """Subscribe the closer and schedule the boot reconcile. Called once
    from the lifespan (a running loop)."""
    offboarding.subscribe(SUBSCRIBER, on_offboard, priority=10)
    _keep(asyncio.ensure_future(_reconcile_after_boot()))
    logger.info("offboarding: %s subscribed (a person's live sessions close with their access)",
                SUBSCRIBER)
