"""A machine's link beside its connection: the windows its reconnect steps
run in, and the alive report of each sign-in with the hooks that read it.

Nothing here is shared between machines. A machine that reconnects every
second costs the platform its sign-in and its alive report: every other
step runs at most once per window (``kick``). The report of a sign-in (the
terminals and the headless sessions the machine holds then) is assembled
from its two frames and handed, in a task of that machine's own, to the
adoption callback and then to every hook, one after another, under no lock.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple

from core.background import spawn

if TYPE_CHECKING:
    from core.session.session_state import SessionFacts

logger = logging.getLogger("otodock.satellite")

# The window of a reconnect step: it runs at most once per window per
# machine, at the window's end when it was asked for inside one.
STEP_WINDOW_S = 30.0
# How long the first frame of an alive report waits for the second.
REPORT_WAIT_S = 10.0
# A machine away this long loses its link state (the row cache, the windows).
LINK_KEEP_S = 3600.0
# An update sent to a machine runs until its next sign-in, or this long at most:
# a Linux apply that builds a new venv takes a few minutes on a small machine.
UPDATE_RUN_S = 300.0
# The satellite's apply blocks its loop and ends in its exit: the connection the
# update went out on sending a frame this long after it means the apply failed
# there (heartbeats come every 20 s).
UPDATE_QUIET_S = 30.0

AliveHook = Callable[["AliveReport"], Awaitable[None]]
TerminalClaim = Callable[[str, frozenset[str]], Awaitable["set[str] | frozenset[str] | None"]]
# The two hooks that run as tasks of their own return a coroutine, the
# argument ``core.background.spawn`` takes.
TerminalsLost = Callable[[str, "list[LostTerminal]"], Coroutine[Any, Any, None]]
IdleHook = Callable[[str], Coroutine[Any, Any, None]]

class LostTerminal(NamedTuple):
    """A held terminal with a turn open that the machine no longer runs, its
    facts, read before the reconcile fired its exit (the close forgets
    them), and the session object the reconcile read, whose close the
    record waits for."""
    session_id: str
    chat_id: str
    facts: SessionFacts
    session: object


_alive_hooks: list[tuple[str, AliveHook]] = []
#: The machines whose complete alive report ran since this process started:
#: what each kept through a restart has been adopted.
_reported: set[str] = set()
#: The kept sessions of each machine whose adoption was handed to a task of
#: its own and has not restored its binding yet (a turn in flight, a
#: terminal): the pool's map does not hold them until it does.
_adopting: dict[str, set[str]] = {}
_terminal_claims: list[tuple[str, TerminalClaim]] = []
_terminals_lost_hooks: list[tuple[str, TerminalsLost]] = []
_idle_hooks: list[tuple[str, IdleHook]] = []


def add_alive_report_hook(fn: AliveHook, *, name: str) -> None:
    """Call ``fn(report)`` once per sign-in of every machine, after the
    adoption callback returned, in the order the hooks were added."""
    _alive_hooks.append((name, fn))


def add_terminal_claim(fn: TerminalClaim, *, name: str) -> None:
    """Ask ``fn(machine_id, ids)`` at every sign-in for the terminals the
    machine reports and the platform does not hold, before they are closed
    as orphans. It returns the ids it took back (their handles registered).
    It runs in the machine's own read loop: it must be short."""
    _terminal_claims.append((name, fn))


def add_terminals_lost_hook(fn: TerminalsLost, *, name: str) -> None:
    """Call ``fn(machine_id, [LostTerminal, ...])`` after a sign-in's
    terminal reconcile ended the terminals the platform held with a turn
    open that the machine no longer runs. Runs as a task of its own."""
    _terminals_lost_hooks.append((name, fn))


def add_idle_hook(fn: IdleHook, *, name: str) -> None:
    """Call ``fn(machine_id)`` once when a machine whose update waits for an
    idle moment (``MachineLinks.update_waits``) reports no live session.
    Runs as a task of its own."""
    _idle_hooks.append((name, fn))


def reported_since_boot(machine_id: str) -> bool:
    """Whether the machine's sessions kept through this platform's restart
    are back: a complete alive report of it ran, and no adoption it handed
    to a task of its own is still on its way to its binding."""
    return machine_id in _reported and not _adopting.get(machine_id)


def adoption_begins(machine_id: str, session_id: str) -> None:
    """An adoption of a kept session is handed to a task of its own (a turn
    in flight, a terminal): on its way until ``adoption_ends``. On the loop."""
    _adopting.setdefault(machine_id, set()).add(session_id)


def adoption_ends(machine_id: str, session_id: str) -> None:
    """The adoption restored its binding, or its task ended with none. Both
    call it: the second call finds nothing. On the loop."""
    held = _adopting.get(machine_id)
    if held is None:
        return
    held.discard(session_id)
    if not held:
        _adopting.pop(machine_id, None)


def reset_hooks() -> None:
    """Forget every hook and claim, and every machine's report (tests)."""
    _alive_hooks.clear()
    _terminal_claims.clear()
    _terminals_lost_hooks.clear()
    _idle_hooks.clear()
    _reported.clear()
    _adopting.clear()


def new_report_id() -> str:
    return secrets.token_hex(8)


@dataclass(frozen=True)
class AliveReport:
    """What a machine held at one sign-in."""

    machine_id: str
    headless: frozenset[str]
    terminals: frozenset[str]
    complete: bool
    sessions: tuple[dict, ...]
    other_sessions: tuple[dict, ...] | None
    signed_in_at: float
    report_id: str
    _current: Callable[[], bool] = field(default=lambda: True, repr=False, compare=False)

    def is_current(self) -> bool:
        """The connection is still the machine's and no newer report came."""
        return bool(self._current())


@dataclass
class _Assembly:
    """The alive report of one connection while its frames arrive."""

    conn: object
    report_id: str
    signed_in_at: float
    terminals: frozenset[str] | None = None
    terminals_id: str = ""
    sessions: dict | None = None
    timer: asyncio.TimerHandle | None = None
    delivered: bool = False


@dataclass
class MachineLink:
    machine_id: str
    touched: float = field(default_factory=time.monotonic)
    # The machine row's cache, carried from one connection to the next.
    name: str = ""
    max_sessions: int | None = None
    step_at: dict[str, float] = field(default_factory=dict)
    step_due: dict[str, asyncio.TimerHandle] = field(default_factory=dict)
    assembly: _Assembly | None = None
    report_task: asyncio.Task | None = None
    report_next: _Assembly | None = None
    # The version an update waits to push until the machine runs no session
    # (``""``: none waits).
    update_waits: str = ""
    # The version an update was sent to bring the machine to (``""``: none
    # runs), when, and the connection it went out on (None at a sign-in, whose
    # connection is never registered).
    update_sent: str = ""
    update_sent_at: float = 0.0
    update_sent_on: object | None = None
    # The session starts on their way to the machine: the remote layer's
    # starts under way, and a build's hold until its start begins (a
    # monotonic deadline). Either keeps a waiting update back.
    starts: int = 0
    start_held_until: float = 0.0


def _ids(entries: object) -> set[str]:
    out: set[str] = set()
    if isinstance(entries, list):
        for entry in entries:
            sid = entry.get("session_id") if isinstance(entry, dict) else None
            if isinstance(sid, str) and sid:
                out.add(sid)
    return out


class MachineLinks:
    """Every machine's link state, owned by the connection manager."""

    def __init__(self, manager) -> None:
        self._manager = manager
        self._links: dict[str, MachineLink] = {}

    def get(self, machine_id: str) -> MachineLink:
        link = self._links.get(machine_id)
        if link is None:
            link = self._links[machine_id] = MachineLink(machine_id)
        link.touched = time.monotonic()
        return link

    def prune(self, connected: set[str], keep: Callable[[str], bool]) -> None:
        """Drop the state of machines away for ``LINK_KEEP_S`` (``keep``: a
        machine something still waits for)."""
        now = time.monotonic()
        for mid, link in list(self._links.items()):
            if mid in connected or keep(mid) or now - link.touched < LINK_KEEP_S:
                continue
            if link.report_task is not None and not link.report_task.done():
                continue
            for timer in link.step_due.values():
                timer.cancel()
            self._links.pop(mid, None)

    # -- reconnect steps ---------------------------------------------------

    def kick(self, machine_id: str, step: str, start: Callable[[], None], *,
             window_s: float | None = None) -> None:
        """Run ``start`` now when ``step`` last ran a window ago or never,
        else once at the end of its window (a second kick inside the window
        adds nothing)."""
        window = STEP_WINDOW_S if window_s is None else window_s
        link = self.get(machine_id)
        now = time.monotonic()
        last = link.step_at.get(step)
        if last is None or now - last >= window:
            link.step_at[step] = now
            start()
            return
        if step in link.step_due:
            return
        link.step_due[step] = asyncio.get_running_loop().call_later(
            last + window - now, self._fire, machine_id, step, start)

    def _fire(self, machine_id: str, step: str, start: Callable[[], None]) -> None:
        link = self._links.get(machine_id)
        if link is None:
            return
        link.step_due.pop(step, None)
        link.step_at[step] = time.monotonic()
        try:
            start()
        except Exception:
            logger.exception("Satellite %s: reconnect step %s failed", machine_id[:8], step)

    # -- the alive report ----------------------------------------------------

    def begin_report(self, machine_id: str, conn: object, report_id: str) -> None:
        """A machine registered: its report is assembled for ``conn`` from now."""
        link = self.get(machine_id)
        if link.assembly is not None and link.assembly.timer is not None:
            link.assembly.timer.cancel()
        link.assembly = _Assembly(conn=conn, report_id=report_id, signed_in_at=time.monotonic())

    def end_connection(self, machine_id: str, conn: object) -> None:
        """The connection is gone: no report is delivered for it."""
        link = self._links.get(machine_id)
        if link is None:
            return
        link.touched = time.monotonic()
        asm = link.assembly
        if asm is not None and asm.conn is conn:
            if asm.timer is not None:
                asm.timer.cancel()
            link.assembly = None
        if link.report_next is not None and link.report_next.conn is conn:
            link.report_next = None

    async def claim_terminals(self, machine_id: str, unheld: frozenset[str]) -> set[str]:
        """Ask every terminal claim for the ids the platform does not hold."""
        taken: set[str] = set()
        for name, fn in list(_terminal_claims):
            left = frozenset(unheld - taken)
            if not left:
                break
            try:
                got = await fn(machine_id, left)
            except Exception:
                logger.exception("Satellite %s: terminal claim %s failed", machine_id[:8], name)
                continue
            taken |= {sid for sid in (got or ()) if sid in left}
        return taken

    def update_waits(self, machine_id: str, target: str) -> None:
        """Mark the machine's update as waiting for an idle moment (``""``
        clears the mark: the machine signed in on the target version)."""
        self.get(machine_id).update_waits = target

    def update_waiting(self, machine_id: str) -> str:
        link = self._links.get(machine_id)
        return link.update_waits if link is not None else ""

    def update_sent(self, machine_id: str, target: str, conn: object | None = None) -> None:
        """Mark an update as sent to the machine now, over ``conn`` (``""``
        ends the mark: the machine signed in again, or the push never left)."""
        if not target:
            link = self._links.get(machine_id)
            if link is not None:
                link.update_sent, link.update_sent_on = "", None
            return
        link = self.get(machine_id)
        link.update_sent, link.update_sent_at, link.update_sent_on = target, time.monotonic(), conn

    def update_running(self, machine_id: str) -> str:
        """The version an update sent to the machine is still bringing it to,
        ``""`` once it ended: at the machine's next sign-in, ``UPDATE_RUN_S``
        after the send, or when the connection it went out on spoke
        ``UPDATE_QUIET_S`` after it (a newer connection is a sign-in, which
        ended the mark first). Reads no link into being (the machine lists
        poll it)."""
        link = self._links.get(machine_id)
        if link is None or not link.update_sent:
            return ""
        sent_at = link.update_sent_at
        if time.monotonic() - sent_at >= UPDATE_RUN_S:
            return ""
        if getattr(link.update_sent_on, "last_frame_at", 0.0) - sent_at > UPDATE_QUIET_S:
            return ""
        return link.update_sent

    def hold_for_start(self, machine_id: str, seconds: float) -> None:
        """A session's build placed it on the machine: its start is on its way
        for ``seconds`` at most, until the remote layer begins it."""
        link = self.get(machine_id)
        link.start_held_until = max(link.start_held_until, time.monotonic() + seconds)

    def start_begins(self, machine_id: str) -> None:
        """The remote layer starts a session on the machine (``start_ends``
        follows in every way out)."""
        self.get(machine_id).starts += 1

    def start_ends(self, machine_id: str) -> None:
        link = self._links.get(machine_id)
        if link is not None and link.starts > 0:
            link.starts -= 1

    def starting(self, machine_id: str) -> bool:
        """A session start is on its way to the machine: one the remote layer
        runs, or one a build placed there and holds."""
        link = self._links.get(machine_id)
        return link is not None and (link.starts > 0 or time.monotonic() < link.start_held_until)

    def idle(self, machine_id: str) -> None:
        """The machine reported no live session: a waiting update is handed
        to the idle hooks once, the mark cleared at the push. A start on its
        way to the machine keeps the mark for the next report: the push would
        end that session as it begins."""
        link = self._links.get(machine_id)
        if link is None or not link.update_waits or self.starting(machine_id):
            return
        link.update_waits = ""
        for name, fn in list(_idle_hooks):
            spawn(fn(machine_id), name=f"idle:{name}", owner="satellite_link")

    def terminals_lost(self, machine_id: str, lost: list[LostTerminal]) -> None:
        """Tell the hooks which held terminals the machine lost, each in a
        task of its own (they wait on the terminals' closes)."""
        if not lost:
            return
        for name, fn in list(_terminals_lost_hooks):
            spawn(fn(machine_id, list(lost)), name=f"terminals_lost:{name}", owner="satellite_link")

    def older_report(self, machine_id: str, conn: object, msg: dict) -> bool:
        """A frame of a pair queued at an earlier sign-in, sent late: the
        machine's queue outlives a link, and this sign-in's own pair follows
        it. A frame with no id is not one."""
        report_id = str(msg.get("report_id") or "")
        asm = self._assembly_of(machine_id, conn)
        return bool(report_id and asm is not None and asm.report_id
                    and report_id != asm.report_id)

    def note_terminals(self, machine_id: str, conn: object, msg: dict) -> None:
        """The ``pty_alive`` frame, after its reconcile ran."""
        asm = self._assembly_of(machine_id, conn)
        if asm is None or asm.delivered:
            return
        ids = msg.get("session_ids")
        asm.terminals = frozenset(s for s in ids if isinstance(s, str)) if isinstance(ids, list) else frozenset()
        asm.terminals_id = str(msg.get("report_id") or "")
        if asm.sessions is not None:
            self._deliver(machine_id, asm)
        elif asm.timer is None:
            asm.timer = asyncio.get_running_loop().call_later(
                REPORT_WAIT_S, self._deliver_late, machine_id, asm)

    def note_sessions(self, machine_id: str, conn: object, msg: dict) -> None:
        """The ``sessions_alive`` frame: the report goes out now, with or
        without the terminals' frame (the adoption callback never waits)."""
        asm = self._assembly_of(machine_id, conn)
        if asm is None or asm.delivered:
            return
        asm.sessions = msg
        self._deliver(machine_id, asm)

    def _assembly_of(self, machine_id: str, conn: object) -> _Assembly | None:
        link = self._links.get(machine_id)
        asm = link.assembly if link is not None else None
        return asm if asm is not None and asm.conn is conn else None

    def _deliver_late(self, machine_id: str, asm: _Assembly) -> None:
        asm.timer = None
        link = self._links.get(machine_id)
        if link is None or link.assembly is not asm or asm.delivered:
            return
        self._deliver(machine_id, asm)

    def _deliver(self, machine_id: str, asm: _Assembly) -> None:
        asm.delivered = True
        if asm.timer is not None:
            asm.timer.cancel()
            asm.timer = None
        link = self.get(machine_id)
        if link.report_task is not None and not link.report_task.done():
            link.report_next = asm     # the newest waits, an older one waiting is skipped
            return
        link.report_task = spawn(self._run_reports(link, asm), name="alive_report",
                                 owner="satellite_link")

    def _build(self, machine_id: str, asm: _Assembly) -> AliveReport:
        frame = asm.sessions or {}
        sessions = frame.get("sessions")
        others = frame.get("other_sessions")
        have_others = isinstance(others, list)
        complete = bool(
            asm.report_id
            and asm.sessions is not None and asm.terminals is not None
            and str(frame.get("report_id") or "") == asm.report_id
            and asm.terminals_id == asm.report_id
            and have_others and not frame.get("partial")
        )
        link = self._links.get(machine_id)

        def current() -> bool:
            return (self._manager.get_connection(machine_id) is asm.conn
                    and (link is None or link.report_next is None))

        return AliveReport(
            machine_id=machine_id,
            headless=frozenset(_ids(sessions) | _ids(others)),
            terminals=asm.terminals or frozenset(),
            complete=complete,
            sessions=tuple(e for e in sessions if isinstance(e, dict)) if isinstance(sessions, list) else (),
            other_sessions=tuple(e for e in others if isinstance(e, dict)) if isinstance(others, list) else None,
            signed_in_at=asm.signed_in_at,
            report_id=asm.report_id,
            _current=current,
        )

    async def _run_reports(self, link: MachineLink, asm: _Assembly) -> None:
        """One machine's reports, one at a time: the adoption callback to its
        end, then each hook. A report whose connection is gone is skipped."""
        machine_id = link.machine_id
        while True:
            if self._manager.get_connection(machine_id) is asm.conn:
                await self._run_one(machine_id, asm)
            nxt, link.report_next = link.report_next, None
            if nxt is None:
                return
            asm = nxt

    async def _run_one(self, machine_id: str, asm: _Assembly) -> None:
        frame = asm.sessions
        cb = getattr(self._manager, "_sessions_alive_callback", None)
        if frame is not None and cb is not None:
            others = frame.get("other_sessions")
            try:
                await cb(machine_id, frame.get("sessions") or [],
                         others if isinstance(others, list) else None)
            except Exception:
                logger.exception("Satellite %s: the sessions_alive callback failed", machine_id[:8])
        if frame is not None:
            # Each session the adoption took back took its held lines.
            self._manager.end_held_lines(machine_id, asm.conn)
        # The adoption may have taken a while: no report for a connection gone.
        if self._manager.get_connection(machine_id) is not asm.conn:
            return
        report = self._build(machine_id, asm)
        if report.complete:
            _reported.add(machine_id)
        for name, fn in list(_alive_hooks):
            try:
                await fn(report)
            except Exception:
                logger.exception("Satellite %s: alive report hook %s failed", machine_id[:8], name)
