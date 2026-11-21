"""In-flight dashboard-upload push registry — the turn-start barrier's
source of truth.

``POST /v1/upload`` returns as soon as the platform write lands and pushes
the file to remote satellites in the BACKGROUND (``api/media/uploads.py``),
so the dashboard's upload spinner and workspace listing never stall on a
slow satellite link. That opens a gap: a user can attach a file in chat and
send a prompt referencing it before the push reaches the satellite — the
CLI there would hit ENOENT. This module closes the gap: every backgrounded
upload push registers here, and ``RemoteExecutionLayer.send_message``
awaits the agent's in-flight pushes (bounded) BEFORE dispatching the
prompt to the satellite.

The barrier is bounded twice: ``BARRIER_TIMEOUT_S`` with no movement on the
machine's lane, however long the push has run, and ``BARRIER_MAX_S`` whatever
moves. A wedged push must not hang turns, and neither must a file of hours on a
slow link. When a bound is reached the turn goes on without the file, the push
goes on behind it, and both the agent and the chat are told in one line each
(``turn_goes_on_without_files``). The next turn does not wait for the same files.

Two parts. The tasks registered here are short: a fan-out's preparation
(``workspace_fanout.fan_out_write`` resolves its targets and hands each
machine a file name, it waits for no machine) and a save's fan-out waiting
its turn. A task with no machine bound holds every machine's turn of the
agent for that preparation, which is local work. The pushes themselves run
in each machine's own lane (``core/remote/machine_push_lane``), and a turn
on a machine waits for the files a person or a platform tool wrote that its
OWN machine still lacks, never for a push bound for another machine.

Out of scope by design:
  * Local sessions never touch this module — the barrier lives in the
    remote execution layer only, and local (bind-mounted) workspaces have
    nothing to push.
  * PTY/terminal sessions have no turn-dispatch chokepoint (keystrokes
    stream raw), so they get no barrier; by the time a human has typed a
    prompt the push has usually landed, and the same sweep/session-start
    backstops cover the rest.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine

logger = logging.getLogger("otodock.upload-inflight")

# How long a turn waits with nothing moving on its machine's lane (every chunk
# is acked, a DEAD connection is rejected at once and a stalled one fails after
# ``PUSH_STALL_S`` inside ``push_file``).
BARRIER_TIMEOUT_S = 60.0
# How long a turn waits at most while the lane moves: enough for a large file
# on a slow link, short of a person giving up on the chat. A steer waits in a
# tab's one socket and a script step in its run: both pass ``BARRIER_TIMEOUT_S``.
BARRIER_MAX_S = 300.0
# The same on a spoken turn (a call, a duplex chat). A person on the line hears
# silence or a filler, and past a few seconds a pause reads as a dropped line. A
# small file on a live link is there by then, and the agent is told otherwise.
BARRIER_SPOKEN_S = 5.0
# What the agent reads at the head of a turn that went on without its files. A
# missing file fails loudly; an older copy under the same name does not.
FILES_LATE_PROMPT = ("[Some files sent with this message have not reached this machine yet. A file "
                     "named here may be missing, or may be an older copy than the one sent. Check it "
                     "before relying on it, and say so if you could not use it.]")
# What the person reads in the chat at the same moment, the ``files_late``
# notice, with the machine's name where one is known.
FILES_LATE = ("The agent started before all the files reached {machine}. "
              "They are still on their way, so it may not see them yet.")
FILES_LATE_NO_NAME = "its machine"

_inflight: dict[str, set[asyncio.Task]] = {}
# The machines a tracked push is bound for, when the caller knows them (a
# Collabora save's fan-out: the machines it marked, a set that grows while
# later saves fold into it). A task with none holds every machine's turn.
_bound_for: dict[asyncio.Task, set[str]] = {}


def track(agent_slug: str, coro: Coroutine, *, machines: set[str] | None = None) -> asyncio.Task:
    """Run ``coro`` (an upload push) as a background task, registered so
    ``wait_settled`` can barrier on it. The registry holds a strong ref
    until completion (a bare ``create_task`` is GC-collectable mid-flight).
    The caller is responsible for the coroutine never raising — push
    helpers are best-effort by contract and swallow their own errors.
    ``machines``: the machines the push is bound for (read at each wait,
    so the caller may add to the set), None when unknown."""
    task = asyncio.create_task(coro)
    _inflight.setdefault(agent_slug, set()).add(task)
    if machines is not None:
        _bound_for[task] = machines
    task.add_done_callback(lambda t: _discard(agent_slug, t))
    return task


def unbind(task: asyncio.Task) -> None:
    """The push is bound for no known machine any more: it holds every
    machine's turn from now on."""
    _bound_for.pop(task, None)


def _discard(agent_slug: str, task: asyncio.Task) -> None:
    _bound_for.pop(task, None)
    tasks = _inflight.get(agent_slug)
    if tasks is not None:
        tasks.discard(task)
        if not tasks:
            _inflight.pop(agent_slug, None)


def pending_count(agent_slug: str) -> int:
    """Number of in-flight pushes for ``agent_slug`` (tests / diagnostics)."""
    return sum(1 for t in _inflight.get(agent_slug, ()) if not t.done())


async def wait_settled(
    agent_slug: str, *, machine_id: str = "", timeout: float = BARRIER_TIMEOUT_S,
    ceiling: float = BARRIER_MAX_S,
) -> bool:
    """Wait (bounded) until every upload push in flight for ``agent_slug``
    AT CALL TIME has finished. Pushes that start DURING the wait are not
    awaited — the barrier protects the prompt that was just submitted, not
    future ones — so a steady stream of uploads can't extend the wait
    unboundedly. ``machine_id``: the machine the turn runs on; a push bound
    for other machines only is not awaited. ``timeout`` is the bound with
    nothing moving, ``ceiling`` the bound whatever moves.

    Returns True when everything settled, False on a bound. Callers proceed
    either way — the barrier is best-effort, like the pushes themselves.
    Never raises.
    """
    limit = min(timeout, ceiling)
    tasks = {
        t for t in _inflight.get(agent_slug, ())
        if not t.done() and (not machine_id or t not in _bound_for or machine_id in _bound_for[t])
    }
    if not tasks:
        return await _lane_settled(agent_slug, machine_id, timeout, ceiling)
    started = time.monotonic()
    try:
        done, pending = await asyncio.wait(tasks, timeout=limit)
    except Exception:
        logger.exception("upload barrier wait failed for %s", agent_slug)
        return False
    waited = time.monotonic() - started
    if pending:
        logger.warning(
            "upload barrier: %d push(es) for agent %s still in flight after "
            "%.0fs — dispatching the turn anyway (the push continues in the "
            "background; the satellite reconciles via the fingerprint sweep "
            "or its next session-start sync)",
            len(pending), agent_slug, waited,
        )
        return False
    if waited >= 0.05:
        logger.info(
            "upload barrier: turn for agent %s waited %.2fs for %d upload "
            "push(es)", agent_slug, waited, len(done),
        )
    return await _lane_settled(agent_slug, machine_id, timeout, ceiling - waited)


async def _lane_settled(agent_slug: str, machine_id: str, timeout: float, ceiling: float) -> bool:
    """The machine's own lane: the files a person or a platform tool wrote
    that it does not hold yet (``core/remote/machine_push_lane``). The wait
    follows the push: it gives up ``timeout`` after the lane last moved, so a
    large file that keeps moving still arrives before its turn, and at
    ``ceiling`` whatever moves. Never for another machine's lane."""
    if not machine_id:
        return True
    from core.remote import machine_push_lane
    started = time.monotonic()
    try:
        ok = await asyncio.wait_for(machine_push_lane.settled(
            machine_id, agent_slug, turns_only=True, idle_s=timeout), max(0.0, ceiling))
    except asyncio.TimeoutError:
        ok = False
    except Exception:
        logger.exception("upload barrier lane wait failed for %s", agent_slug)
        return False
    waited = time.monotonic() - started
    if not ok:
        logger.warning(
            "upload barrier: machine %s still lacks files of agent %s after %.0fs "
            "— dispatching the turn anyway (the push continues in the background)",
            machine_id[:8], agent_slug, waited,
        )
    elif waited >= 0.05:
        logger.info("upload barrier: turn for agent %s waited %.2fs for its machine's files",
                    agent_slug, waited)
    return ok


async def turn_goes_on_without_files(session_id: str, machine_id: str, agent_slug: str,
                                     text: str) -> str:
    """A wait for this turn's files reached a bound: ``text`` with the agent's
    line at its head, the chat's line written and the next turn spared the same
    wait. Only the agent can act at this moment, and with an older copy under
    the name nothing else tells it. A machine that has gone runs no turn, and
    the chat says that itself."""
    from core.remote import machine_push_lane
    from core.remote.satellite_connection import get_connection_manager
    if not get_connection_manager().is_connected(machine_id):
        return text
    machine_push_lane.release_turns(machine_id, agent_slug)
    await _tell_chat_files_late(session_id, machine_id)
    return f"{FILES_LATE_PROMPT}\n\n{text}"


async def _tell_chat_files_late(session_id: str, machine_id: str) -> None:
    """The session's chat, in one line, so a person who attached a file knows
    why the agent may not see it: a history row on the chat's lane and the same
    block to whoever views the chat. A session with no chat of its own (a
    meeting participant) has no line. Never raises: the turn goes on either way."""
    import functools
    import json

    from core.events import chat_writer
    from core.session.session_state import broadcast_chat_frame
    from services.remote import chat_targets
    from storage import database as task_store, remote_store
    from storage.pg import run_db
    from ws import wire_events as wire

    def _facts() -> tuple[str, str]:
        chat = task_store.get_chat_by_session(session_id)
        machine = remote_store.get_remote_machine(machine_id) or {}
        return ((chat or {}).get("id") or "",
                chat_targets.label_of(machine_id, machine) if machine.get("name") else "")

    try:
        chat_id, place = await run_db(_facts)
        if not chat_id:
            return
        block = {"type": wire.SYSTEM, "subtype": wire.SUBTYPE_FILES_LATE,
                 "message": FILES_LATE.format(machine=place or FILES_LATE_NO_NAME)}
        await chat_writer.submit(chat_id, functools.partial(
            task_store.add_chat_message, chat_id, "event", "",
            event_type=wire.SYSTEM, event_data=json.dumps(block)), label="files_late")
        broadcast_chat_frame(chat_id, {**block, "chat_id": chat_id})
    except Exception:  # noqa: BLE001  # the line spans the store and the writer: the turn goes on
        logger.warning("session %s: the files-late line was not written", session_id[:8],
                       exc_info=True)
