"""Outbound-transfer gate (Feature F, 1.4.0, per machine since 1.7.1).

Bounds how many LARGE proxy→satellite pushes run at once to each machine,
across the three push paths: the live fan-out (``workspace_fanout.fan_out_write``),
the initial-sync push branch (``remote_workspace_sync``) and a read's re-push
of a platform-ahead copy (``remote_file_flow``). A machine's bytes in flight
are bounded by its connection's bulk credit (``satellite_connection
._BulkCredit``), which the pushes to it share; this caps how many large ones
take part, so "one 1GB file to a machine" never queues every smaller push
behind it, and one slow machine never holds another machine's pushes (a
push now runs while it moves, up to its ceiling ``push_ceiling_s``).

Config: ``OTODOCK_SYNC_FANOUT_CONCURRENCY`` (default 3 per machine; 0 =
unlimited, gate fully disabled) and ``OTODOCK_SYNC_FANOUT_MIN_MB`` (default
4): pushes SMALLER than the threshold bypass the gate entirely — a small file
is a handful of 512KB frames already paced by the credit, and gating it would
head-of-line-block live edits behind bulk transfers. 4MB matches
``_DEFER_PULL_MIN_BYTES`` (the codebase's "big enough to move off the hot
path" constant).

LOCK-ORDER INVARIANT — THE GATE IS INNERMOST. Established order::

    sync_lock(machine,agent) → _window(8) → path lock(agent,rel) → GATE
        → credit → acks

A gate holder only awaits the credit and ack futures (bounded by the push's
progress deadline; deregister rejects pending futures) — it never acquires
any outer lock, so the wait-for graph is acyclic. Do NOT acquire the gate
around anything that takes a sync/path lock. ``asyncio.Semaphore`` wakes
waiters FIFO → starvation-free.

Registry-agnostic: ``slot`` takes an optional async ``on_state`` callback
(state string) so tracked fan-outs can surface 'queued'/'active' in the
transfer registry without this module importing it. Callbacks are
best-effort — they never block or fail a push.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

logger = logging.getLogger("claude-proxy.transfer-gate")

_sems: dict[str, asyncio.Semaphore] = {}   # one per machine, made on first use
_limit: int | None = None  # None = not yet initialized from config
_waiters: dict[str, int] = {}               # manual count; asyncio.Semaphore exposes none


def _get_limit() -> int:
    global _limit
    if _limit is None:
        import config
        _limit = max(0, getattr(config, "SYNC_FANOUT_CONCURRENCY", 3))
    return _limit


def _get_sem(machine_id: str) -> asyncio.Semaphore | None:
    limit = _get_limit()
    if limit <= 0:
        return None
    sem = _sems.get(machine_id)
    if sem is None:
        sem = _sems[machine_id] = asyncio.Semaphore(limit)
    return sem


def _min_bytes() -> int:
    import config
    return max(0, getattr(config, "SYNC_FANOUT_MIN_MB", 4)) * 1024 * 1024


def reset_for_tests() -> None:
    """Drop cached semaphores/limit so monkeypatched config takes effect."""
    global _limit
    _sems.clear()
    _limit = None
    _waiters.clear()


def is_gated(size_bytes: int) -> bool:
    """True when a push of this size contends for a slot of its machine."""
    return _get_limit() > 0 and size_bytes >= _min_bytes()


async def _notify(on_state, state: str) -> None:
    if on_state is None:
        return
    try:
        await on_state(state)
    except Exception:
        logger.debug("transfer_gate on_state(%s) failed", state, exc_info=True)


@contextlib.asynccontextmanager
async def slot(
    machine_id: str, agent_slug: str, rel_path: str, size_bytes: int, *,
    on_state=None,
):
    """Acquire one of ``machine_id``'s outbound slots for a push.

    Below-threshold pushes and limit=0 bypass instantly (no callbacks, no
    log — behavior identical to pre-gate). Gated pushes log ONE INFO line
    when they actually wait and report 'queued' → 'active' via ``on_state``
    ('active' fires on the fast path too, so tracked rows always pass
    through a consistent lifecycle). Terminal done/failed states are the
    caller's job — the gate only owns admission.
    """
    sem = _get_sem(machine_id)
    if sem is None or size_bytes < _min_bytes():
        yield
        return
    if sem.locked():
        logger.info(
            "fan-out queued: %s/%s -> %s (%d ahead)",
            agent_slug, rel_path, machine_id[:8], _waiters.get(machine_id, 0),
        )
        await _notify(on_state, "queued")
    _waiters[machine_id] = _waiters.get(machine_id, 0) + 1
    try:
        await sem.acquire()
    finally:
        _waiters[machine_id] -= 1
        if not _waiters[machine_id]:
            del _waiters[machine_id]
    try:
        await _notify(on_state, "active")
        yield
    finally:
        sem.release()
