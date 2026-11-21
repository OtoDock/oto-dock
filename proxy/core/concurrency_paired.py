"""Admission on a personal or admin machine: its memory in place of a count.

A paired computer whose satellite watches its memory (0.5.177, Linux) reports
it at its sign-in and in every heartbeat. A **new** start there must leave the
machine's reserve free: ``reading − estimate − debit ≥ reserve``, the reserve
of a personal or admin computer (``host_resources.computer_reserve_mb``: 10 %,
at least 1 GB, by the share at most 4 GB, the numbers its guard keeps) and
the debit what was admitted there in the last 90 s (``_grow_debits`` keyed by
the machine, as a compute machine on another host is keyed). Nothing is
tracked: no Gate 1 (its owner's other programs make a reservation budget
meaningless), no per-person cap, no count unless an admin set one. A machine
with no fresh report, or one that says it read nothing, is not checked by
memory. ``core/concurrency`` calls this module at its four doors: an
interactive start (idle sessions there are closed first, as at the count), a
task (it waits in its machine's own line), a background wake and a tool's late
start.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time

from core.concurrency_scan import (
    REPORT_FRESH_S,
    _grow_debits,
    _growin_debit_mb,
    _swap_credit_mb,
)
from core.sandbox import host_resources

logger = logging.getLogger("otodock.concurrency")

_MIB = 1024 * 1024
#: A start admitted on a machine passes again for this long (its second
#: acquire before the session is registered: a terminal's, a task's).
ADMITTED_S = 90.0
#: What closing one idle session there is counted to free until the next
#: report: an idle engine's own memory, on the safe side.
EVICT_CREDIT_MB = 150
_LINE_SLICE_S = 5.0
DENY_SENTENCE = "This computer is low on memory. Close something on it or try again."

_admitted: dict[str, tuple[str, float]] = {}                 # sid -> (machine, when)
_lines: dict[str, collections.deque[str]] = {}               # machine -> waiting task sids, in order
_credits: dict[str, list[tuple[float, int]]] = {}            # machine -> [(when, MB)] freed by evictions


def _reading(machine: str) -> tuple[int, int] | None:
    """``(free MB, reserve MB)`` of a paired machine from its fresh report,
    the evictions since that report credited; None when nothing fresh is known."""
    from core.remote.satellite_connection import get_connection_manager
    held = get_connection_manager().machine_memory_report(machine)
    if held is None:
        return None
    memory, age = held
    if age > REPORT_FRESH_S or memory.get("known") is False:
        return None
    total = int(memory.get("total_bytes", 0)) // _MIB
    if total <= 0:
        return None
    free = int(memory.get("available_bytes", 0)) // _MIB
    free += _swap_credit_mb(int(memory.get("swap_free_bytes", 0)))
    reported_at = time.monotonic() - age
    kept = [c for c in _credits.get(machine, ()) if c[0] > reported_at]
    _credits[machine] = kept
    free += sum(mb for _, mb in kept)
    reserve = host_resources.computer_reserve_mb(total)
    return free, reserve


def room(machine: str, est: int) -> bool | None:
    """Whether a new start of ``est`` MB leaves the machine's reserve free;
    None when its memory is not known (it is then not checked by memory)."""
    read = _reading(machine)
    if read is None:
        return None
    free, reserve = read
    return free - _growin_debit_mb(machine) - est >= reserve


def shortfall_mb(machine: str, est: int) -> int:
    read = _reading(machine)
    if read is None:
        return 0
    free, reserve = read
    return max(0, reserve + est + _growin_debit_mb(machine) - free)


def live(session_id: str, machine: str) -> bool:
    """A start of a session that is already there: admitted on that machine in
    the last ``ADMITTED_S``, a headless session the remote layer holds alive or
    in its grace there, or an alive terminal there. It passes with no check and
    no debit (a viewer attaching again, a recovery, an adopted terminal)."""
    held = _admitted.get(session_id)
    if held is not None and held[0] == machine and time.monotonic() - held[1] < ADMITTED_S:
        return True
    from core.session import interactive_registry, session_manager
    found = session_manager.live_session(session_id)
    if found is not None and found.machine_id == machine:
        holds = getattr(found.layer, "holds_alive", None)
        if found.grace or (holds is not None and holds(session_id)):
            return True
    return interactive_registry.alive_on(session_id, machine)


def note_admitted(session_id: str, machine: str, est: int) -> None:
    """Record a start admitted there and debit it while it grows in."""
    now = time.monotonic()
    _admitted[session_id] = (machine, now)
    for sid in [s for s, (_m, at) in _admitted.items() if now - at >= ADMITTED_S]:
        del _admitted[sid]
    _grow_debits.append((now, est, machine))
    while _grow_debits and now - _grow_debits[0][0] >= 90.0:
        _grow_debits.popleft()


def forget(session_id: str) -> None:
    _admitted.pop(session_id, None)


async def make_room(machine: str, est: int) -> None:
    """Before an interactive refusal: close the machine's idle sessions, as at
    its count (the remote layer's own eviction), while the idle sessions there
    can cover the shortfall, each counted as ``EVICT_CREDIT_MB`` until the next
    report. Outside admission's lock; never under the terminal lock."""
    from core.remote.remote_execution import get_remote_layer
    layer = get_remote_layer()

    def short() -> bool:
        missing = shortfall_mb(machine, est)
        return missing > 0 and layer.idle_count_on_machine(machine) * EVICT_CREDIT_MB >= missing

    def credit() -> None:
        _credits.setdefault(machine, []).append((time.monotonic(), EVICT_CREDIT_MB))

    if short():
        closed = await layer._evict_idle_on_machine(machine, short=short, on_close=credit)
        if closed:
            logger.info("Closed %d idle session(s) on %s for a new start by memory", closed, machine[:8])


async def wait_in_line(session_id: str, machine: str, est: int, cond: asyncio.Condition) -> None:
    """A task on a paired machine: wait, first come first served on that
    machine alone, until a start of ``est`` fits there. Never in its creator's
    line (a full laptop holds no task of that person elsewhere) and not counted
    as a parked task (it frees nothing on the platform). Woken by each memory
    report from that machine (``report_arrived``) and every few seconds.
    A start that fits at its first look logs nothing; a wait logs once when
    it begins and once when it ends."""
    line = _lines.setdefault(machine, collections.deque())
    line.append(session_id)
    began: float | None = None
    admitted = False
    try:
        while True:
            if line[0] == session_id and room(machine, est) is not False:
                note_admitted(session_id, machine, est)
                admitted = True
                return
            if began is None:
                began = time.monotonic()
                logger.info("Task %s waits for memory on %s", session_id[:8], machine[:8])
            async with cond:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(cond.wait(), _LINE_SLICE_S)
    finally:
        if began is not None:
            logger.info("Task %s %s memory on %s after %.0f s", session_id[:8],
                        "got" if admitted else "stopped waiting for", machine[:8], time.monotonic() - began)
        with contextlib.suppress(ValueError):
            line.remove(session_id)
        if not line:
            _lines.pop(machine, None)


def report_arrived(machine: str) -> bool:
    """Whether a task waits on that machine (the caller then wakes the line)."""
    return machine in _lines


def denial():
    from core.concurrency import Admission
    return Admission(False, "machine_memory", DENY_SENTENCE)

