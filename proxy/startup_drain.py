"""The planned stop's wiring: who counts what, and what runs at each change
of state (``core/drain.py`` holds the states and imports none of this).

``register`` runs once at boot. The counters read memory only: they are
called every second while a drain waits, and by a status read. A turn
parked on a person (a permission card, a plan review, a terminal's question)
is not working: the drain does not wait for it, the stop cuts it with its
card unanswered, and the continue asks again.
"""

from __future__ import annotations

import asyncio
import logging

from core import drain, placement
from core.background import spawn
from core.session import session_kind
from ws import wire_events as wire

logger = logging.getLogger("otodock")

#: The frame that tells a machine how long the platform expects to be away
#: (``back_within_s`` seconds, 0 takes it back).
PLATFORM_STOPPING_FRAME = "platform_stopping"
# How long a planned stop waits for the file names the machines are owed: they
# live in this process's memory. A healthy machine takes one in milliseconds. A
# frozen one holds its push until its stall ends (90 s), so it costs each stop
# this long and no more, and the merge at its return settles what it missed.
PUSH_WAIT_S = 5.0

#: The line a terminal's pane shows when the platform is about to stop.
TERMINAL_LINE = ("\r\n\x1b[33mOtoDock is updating. This terminal closes when the update "
                 "starts. Open the chat again in a moment.\x1b[0m\r\n")


def _pumps(*, task: bool, parked: bool) -> int:
    from core.events.stream_pump import _active_pumps
    from core.session.session_state import has_pending_prompt
    task_type = session_kind.TASK.source_type
    return sum(1 for p in _active_pumps.values()
               if not p.is_done and (p.source_type == task_type) == task
               and has_pending_prompt(p.session_id) == parked
               and not _waits_for_its_machine(p.session_id))


def _waits_for_its_machine(session_id: str) -> bool:
    """A turn held for a machine that is away is not work a stop waits
    for: the machine's return continues it, the stop cuts nothing of it."""
    from core.session.session_manager import _get_remote_layer
    remote = _get_remote_layer()
    return bool(remote is not None and remote.is_session_grace_held(session_id))


def _turns() -> int:
    return _pumps(task=False, parked=False)


def _runs() -> int:
    """The runs that work: every run whose round opened and that has not
    ended, less those whose turn waits on a person. A run that waits for its
    slot or at its lane's gate has started nothing: the stop leaves it
    ``pending`` and the boot starts it."""
    from services.scheduler import shared
    live = sum(1 for rid, t in shared._running_tasks.items()
               if not t.done() and rid in shared._open_rounds)
    return max(0, live - _pumps(task=True, parked=True))


def _terminal_turns(*, parked: bool) -> int:
    from core.remote import remote_pty
    from core.session import interactive_session
    from core.session.session_state import has_pending_prompt
    return sum(1 for s in interactive_session._sessions.values()
               if s.alive and s.turn_open
               and (s.question_parked or has_pending_prompt(s.session_id)) == parked
               and not (getattr(s, "is_remote", False) and remote_pty.is_machine_away(s.target)))


def _terminals() -> int:
    return _terminal_turns(parked=False)


def _waiting() -> int:
    return (_pumps(task=False, parked=True) + _pumps(task=True, parked=True)
            + _terminal_turns(parked=True))


def _calls() -> int:
    from core import concurrency
    from ws import phone_attach
    phone = session_kind.PHONE.name
    return sum(1 for kind in concurrency._sessions.values() if kind == phone) + len(
        phone_attach._attaches)


def _duplex() -> int:
    from ws import duplex
    return len(duplex._pending) + len(duplex._active)


def _meetings() -> int:
    from services.meetings import meeting_orchestrator
    return sum(1 for t in meeting_orchestrator._active_meetings.values() if not t.done())


def _moves() -> int:
    from services.remote import chat_move
    return len(chat_move._running)


def _background() -> int:
    """Background commands and sub-agents a session's last turn left
    running, on sessions with no turn of their own open (an open turn is
    counted as its turn)."""
    from core.events.bg_command_state import _bg_command_registries
    from core.events.stream_pump import _active_pumps
    from core.session import background_leash
    from core.session.session_state import _subagent_registries
    busy = {p.session_id for p in _active_pumps.values() if not p.is_done}
    return sum(background_leash.background_pending_count(sid)
               for sid in set(_bg_command_registries) | set(_subagent_registries)
               if sid not in busy)


def _queued() -> int:
    from core.events import input_queue
    return sum(len(input_queue.peek(chat_id)) for chat_id in list(input_queue._registry))


def _sessions() -> int:
    """The sessions that hold a slot (``core/concurrency``: the platform's
    own and those a compute machine runs, each on its box), and a compute
    machine's session that holds none yet (one it kept across a restart,
    until the reconciler gives it a slot): a paired machine's never count."""
    from core import concurrency
    from core.session import interactive_session, session_manager, session_state
    slots = set(concurrency._sessions)
    count = len(slots)
    layer = session_manager._remote_layer
    if layer is not None:
        # A compute record once: a run that still holds a slot of its own is
        # counted there.
        count += sum(1 for sid, info in list(layer._sessions.items())
                     if info.alive and info.compute and sid not in slots)
    # A terminal on a compute machine, by its own placement (its machine's
    # link may be in its reconnect grace, as a headless record's may).
    for sid, s in list(interactive_session._sessions.items()):
        if not s.alive or sid in slots or placement.is_local(getattr(s, "target", "")):
            continue
        ctx = session_state.get_session_security(sid)
        pl = getattr(ctx, "placement", None) if ctx is not None else None
        if pl is not None and pl.kind == placement.KIND_COMPUTE:
            count += 1
    return count


def compute_open() -> int:
    """The open work a compute machine holds for the platform: its turns
    (live pumps on sessions placed on a compute machine, a turn held for its
    away machine included) and its terminals with a turn open."""
    from core.concurrency_scan import placed_on_compute
    from core.events.stream_pump import _active_pumps
    from core.session import interactive_session
    from core.session.session_manager import find_layer_for_session

    def on_the_machine(session_id: str) -> bool:
        # A Direct LLM session's loop is in the proxy: a machine holds its
        # tools alone, and no turn of it.
        holder = find_layer_for_session(session_id)
        return bool(placed_on_compute(session_id)) and (
            holder is None or holder.capabilities_for(session_id).runtime.has_os_process)

    turns = sum(1 for p in list(_active_pumps.values())
                if not getattr(p, "is_done", False) and on_the_machine(getattr(p, "session_id", "") or ""))
    terminals = sum(1 for sid, s in list(interactive_session._sessions.items())
                    if getattr(s, "alive", False) and getattr(s, "turn_open", False)
                    and placed_on_compute(sid))
    return turns + terminals


def _compute() -> dict:
    """``work()["compute"]``: whether the install runs a compute machine of
    its own, the compute machines signed in now, whether one of them shares
    the platform's folders (the install's own service: what readiness asks
    for, ``health_parts``), their open work, and the sessions they hold a
    slot for."""
    from core import concurrency
    from core.remote.satellite_connection import get_connection_manager
    from services.apps import app_compute
    from services.remote import compute_bootstrap
    machines = sorted(get_connection_manager().compute_machine_ids())
    return {
        "own": compute_bootstrap.runs_own(),
        "connected": bool(machines),
        "shared": app_compute.place() is not None,
        "machines": machines,
        "open": compute_open(),
        "sessions": sum(1 for box in concurrency._session_box.values() if box),
    }


#: How often the busy-or-idle clock reads the counters, so a turn shorter
#: than a status read's interval still counts as work.
ACTIVITY_S = 5.0


async def activity_loop() -> None:
    while True:
        try:
            drain.note_activity()
        except Exception:  # noqa: BLE001  # the clock must outlive one bad read
            logger.debug("drain: the activity read failed", exc_info=True)
        await asyncio.sleep(ACTIVITY_S)


def notice_frame() -> dict:
    """What a page is told about the platform: the build it serves and
    ``updating`` while new work is held. The connect frame's own shape, so
    a page that reads one reads the other."""
    import config
    from static_assets import dashboard_build_id
    return {"type": wire.SERVER_INFO, "build_id": dashboard_build_id(),
            "version": config.PINNED_OTODOCK_VERSION or "", "updating": drain.holding()}


def _tell_pages() -> None:
    from core.events.input_queue import _connections_by_user
    from services.notifications import notification_manager
    frame = notice_frame()
    for sub in list(_connections_by_user):
        notification_manager.push_live(sub, frame)


async def _on_change() -> None:
    from services.infra import platform_marks
    _tell_pages()
    if drain.state() == drain.DRAINING:
        await asyncio.to_thread(platform_marks.note_held)


async def _tell_machines(back_within_s: int) -> None:
    """Every connected machine learns how long the platform expects to be
    away, so a permission card that waits there is held for it."""
    from core.remote.satellite_connection import get_connection_manager
    get_connection_manager().broadcast_fire_and_forget({
        "type": PLATFORM_STOPPING_FRAME, "back_within_s": back_within_s})


async def _settle_pushes() -> None:
    from core.remote import machine_push_lane
    if not await machine_push_lane.drain(PUSH_WAIT_S):
        logger.warning("drain: a machine still takes its files after %.0f s, its next merge "
                       "settles them", PUSH_WAIT_S)


async def _on_held() -> None:
    from core.session import interactive_session
    await _tell_machines(drain.away_s())
    for session in list(interactive_session._sessions.values()):
        if session.alive and not session.is_remote:
            try:
                session._fanout_output(TERMINAL_LINE.encode())
            except Exception:  # noqa: BLE001  # one terminal's dead viewer must not stop the others
                logger.debug("terminal %s not told of the stop", session.session_id[:8],
                             exc_info=True)


async def _on_resume() -> None:
    """The stop was called off. The caller is answered at once, and what was
    held goes out beside it: a stored wake runs its whole turn, and the admin
    command waits for this answer."""
    from services.infra import platform_marks
    await asyncio.to_thread(platform_marks.note_resumed)
    spawn(_send_what_was_held(), name="resume", owner="drain")


async def _on_start_ended() -> None:
    """The start's hold ended (the compute machine is back, or its bound ran
    out). The queues' chips stop saying the platform is updating and the
    pages hear it. The boot's continue sends what was held, in its order."""
    from core.events import input_queue
    if not drain.holding():
        input_queue.clear_updating()
        _tell_pages()


async def _send_what_was_held() -> None:
    from core.events import input_queue
    from services.scheduler import continue_pass, run_recovery, scheduler
    await _tell_machines(0)
    input_queue.clear_updating()
    await continue_pass.arm_queues()
    await continue_pass.run_pending()
    await run_recovery.start_pending_runs()
    await scheduler.fire_held()
    await scheduler.redeliver_pending_wakes()


def register() -> None:
    """Boot: the counters and the hooks of the planned stop."""
    for kind, count in (("turns", _turns), ("runs", _runs), ("calls", _calls),
                        ("duplex", _duplex), ("meetings", _meetings), ("terminals", _terminals),
                        ("moves", _moves), ("background", _background)):
        drain.register_counter(kind, count)
    drain.register_waiting(_waiting)
    drain.register_queued(_queued)
    drain.register_sessions(_sessions)
    drain.register_compute(_compute)
    drain.on_change(_on_change)
    drain.on_settle(_settle_pushes)
    drain.on_held(_on_held)
    drain.on_resume(_on_resume)
    drain.on_start_ended(_on_start_ended)
