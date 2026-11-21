"""A machine's memory on the platform: the numbers its guard is told, what its
admin's card shows, and the stops it reports.

Each machine's satellite (0.5.177, Linux) runs a memory guard that keeps its
reserve free (``satellite/sessions/memory_guard.py``). The guard and the
platform's admission read the same functions (``host_resources``, vendored);
the platform sends the numbers of the machine's kind at the sign-in
(``memory_policy``), so a guard on a satellite that updates later keeps the
platform's numbers, not its own.
"""

from __future__ import annotations

import logging

import config
from core import host_os, placement
from core.sandbox import host_resources

logger = logging.getLogger("otodock.remote")

_MIB = 1024 * 1024


def policy(machine: dict) -> dict:
    """``memory_policy`` for one machine's sign-in answer: its kind's share,
    floor and the share's cap (none on a compute machine), the swap credit, min_stop's cap, a start's allowance and the ceiling
    per session the key makes on a compute or admin-paired machine (never
    under ``WORK_FLOOR_MB``; none on a personal one)."""
    compute = placement.machine_is_compute(machine)
    if compute:
        share, floor, cap = host_resources.COMPUTE_SHARE, config.SESSION_RESERVE_FLOOR_MB, 0
    else:
        share, floor, cap = (host_resources.COMPUTER_SHARE, host_resources.COMPUTER_FLOOR_MB,
                             host_resources.COMPUTER_CAP_MB)
    key = config.OTODOCK_SESSION_MEMORY_MAX_MB
    shared = compute or placement.machine_is_admin_paired(machine)
    return {
        "share": share,
        "floor_mb": int(floor),
        "cap_mb": cap,
        "credit_mb": int(config.SESSION_SWAP_CREDIT_MB),
        "min_stop_mb": host_resources.MIN_STOP_CAP_MB,
        "start_mb": int(config.SESSION_EST_HEAVY_MB),
        "session_max_mb": max(int(key), host_resources.WORK_FLOOR_MB) if key > 0 and shared else 0,
    }


def stats_fields(conn) -> dict:
    """What the admin's machine card reads beside the count: whether the
    machine watches its memory, its free and total memory, and whether a new
    start there waits now."""
    memory = getattr(conn, "memory", None) or {}
    row = host_os.of((getattr(conn, "capabilities", None) or {}).get("os"))
    # A system the guard does not run on (its reading is not measured there).
    unwatched = {"memory_unwatched": True} if row is not None and not row.memory_guard else {}
    if not memory or getattr(conn, "compute", False):
        return {"memory_watched": getattr(conn, "memory_guard", None) is True, **unwatched}
    total = int(memory.get("total_bytes", 0)) // _MIB
    free = int(memory.get("available_bytes", 0)) // _MIB
    reserve = host_resources.computer_reserve_mb(total)
    return {
        "memory_watched": getattr(conn, "memory_guard", None) is True,
        "mem_total_mb": total,
        "mem_free_mb": free,
        "low_memory": free < reserve,
    }


#: ``memory_stop``'s ``what``, as a log line names it.
_STOPPED = {"own": "its own work, a build or a sync", "session": "a session", "command": "a command"}
#: The guard's words for the stopped unit and the rule: anything else a
#: machine sends is not written into the platform's log.
_KINDS = frozenset({"session", "terminal", "step", "tool", "app", "own"})
_WHYS = frozenset({"reserve", "swap", "ceiling"})


def _mb(value: object) -> object:
    return value if isinstance(value, int) and not isinstance(value, bool) else "?"


def note_stop(machine_id: str, msg: dict) -> None:
    """A machine's guard stopped a command or ended a session (``memory_stop``):
    one line in the platform's log. A session the platform does not place on
    that machine is named as such, never acted on."""
    session_id = str(msg.get("session_id") or "")
    if session_id:
        from core.session import interactive_registry, session_manager
        found = session_manager.live_session(session_id)
        here = (found is not None and found.machine_id == machine_id) or interactive_registry.alive_on(
            session_id, machine_id)
        if not here:
            logger.warning("Machine %s reported a memory stop for session %r it does not run",
                           machine_id[:8], session_id[:8])
            return
    kind = msg.get("kind") if msg.get("kind") in _KINDS else "?"
    why = msg.get("why") if msg.get("why") in _WHYS else ""
    what = _STOPPED.get(str(msg.get("what") or ""), "work")
    if kind != "own":
        what = f"{what} of {kind} {session_id[:8] or '-'}"
    logger.warning("Machine %s stopped %s, for memory: %s MB, %s MB free, reserve %s MB (%s)",
                   machine_id[:8], what, _mb(msg.get("mb")), _mb(msg.get("reading_mb")),
                   _mb(msg.get("reserve_mb")), why)
